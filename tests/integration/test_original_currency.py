from copy import deepcopy
from email.message import EmailMessage

import pytest

import test_pipeline as pipeline
from test_pipeline import Ledger, queue, queue_legacy_estimate, import_daily, statement
from ezbookkeeping_importer.application.classify import classify_pending
from ezbookkeeping_importer.application.collect import ingest
from ezbookkeeping_importer.application.parse import parse_pending
from ezbookkeeping_importer.application.write import write_queued, verify_unknown
from ezbookkeeping_importer.application.resolve import resolve
from ezbookkeeping_importer.application.reconcile import reconcile
from ezbookkeeping_importer.adapters.evidence_store import EvidenceStore
from ezbookkeeping_importer.adapters.banks.cmb import BankParser
from ezbookkeeping_importer.domain.errors import ImporterError

database = pipeline.database
settings = pipeline.settings


def test_usd_creation_and_monthly_cny_settlement_never_modify_usd_amount(
    database, tmp_path, settings
):
    store, ledger = database.store, Ledger()
    tx = queue(store, tmp_path, settings, ledger, currency="USD")
    assert tx["decision"]["target_currency"] == "USD"
    assert tx["decision"]["payload"]["sourceAmount"] == 1000
    write_queued(store, ledger, True)
    statement(store, tx, "72.00")
    reconcile(store, ledger, tmp_path / "reports")
    write_queued(store, ledger, True)
    result = store.one("SELECT * FROM reconciliation_items")
    assert result["status"] == "matched"
    assert result["data"]["bank_settlement"] == {"amount": "72.00", "currency": "CNY"}
    assert result["data"]["comparison_currency"] == "USD"
    assert ledger.modify_calls == []
    assert store.one("SELECT count(*) AS n FROM jobs WHERE kind='settle_amount'")["n"] == 0
    target = store.one("SELECT target_id FROM transactions")["target_id"]
    assert ledger.records[target]["sourceAmount"] == 1000
    ledger.records[target]["time"] += 86400
    reconcile(store, ledger, tmp_path / "reports")
    assert store.one("SELECT status FROM reconciliation_items")["status"] == "target_changed"


def test_usd_refund_is_negative_usd_expense(database, tmp_path, settings):
    store, ledger = database.store, Ledger()
    settings = settings.model_copy(update={"refund_ownership_confirmed": True})
    message = EmailMessage()
    message["Subject"] = "每日信用管家"
    message.set_content(
        "<p>2026/01/01 您的消费明细如下：</p><b>12:00:00</b><b>USD -10.00</b><b>尾号1234 退货 合成商户</b>",
        subtype="html",
    )
    identifier = ingest(
        store, EvidenceStore(tmp_path / "evidence"), message.as_bytes(), settings, "eml"
    )
    store.execute("UPDATE messages SET accepted=true WHERE id=%s", (identifier,))
    parse_pending(store, BankParser())
    classify_pending(store, settings, ledger, None)
    write_queued(store, ledger, True)
    assert ledger.create_calls[0]["type"] == 3
    assert ledger.create_calls[0]["sourceAccountId"] == "usd-account"
    assert ledger.create_calls[0]["sourceAmount"] == -1000


@pytest.mark.parametrize("ambiguous", [False, True])
def test_nonunique_account_match_is_a_persisted_issue(
    database, tmp_path, settings, monkeypatch, ambiguous
):
    store, ledger = database.store, Ledger()
    import_daily(store, tmp_path, settings, currency="USD")
    accounts = ledger.accounts()
    accounts = [a for a in accounts if a["currency"] == "USD"] if ambiguous else []
    if ambiguous:
        accounts.append({**accounts[0], "id": "second-usd"})
    monkeypatch.setattr(ledger, "accounts", lambda: accounts)
    classify_pending(store, settings, ledger, None)
    issue = store.one("SELECT * FROM issues")
    assert issue["code"] == ("account_ambiguous" if ambiguous else "account_not_found")
    assert store.one("SELECT state FROM transactions")["state"] == "issue"
    assert store.one("SELECT count(*) AS n FROM jobs")["n"] == 0


def test_currency_change_before_send_is_rejected(database, tmp_path, settings, monkeypatch):
    store, ledger = database.store, Ledger()
    queue(store, tmp_path, settings, ledger, currency="USD")
    accounts = [{**a, "currency": "CNY"} for a in ledger.accounts()]
    monkeypatch.setattr(ledger, "accounts", lambda: accounts)
    write_queued(store, ledger, True)
    assert ledger.create_calls == []
    assert store.one("SELECT * FROM issues WHERE code='write_preflight_failed'")


@pytest.mark.parametrize("legacy", [False, True])
def test_manual_account_correction_cannot_change_frozen_currency(
    database, tmp_path, settings, legacy
):
    store, ledger = database.store, Ledger()
    tx = (
        queue_legacy_estimate(store, tmp_path, settings, ledger)
        if legacy
        else queue(store, tmp_path, settings, ledger, currency="USD")
    )
    store.issue("synthetic", tx["id"], {"version": 1})
    issue = store.one("SELECT * FROM issues WHERE code='synthetic'")
    with pytest.raises(ImporterError, match="frozen target currency"):
        resolve(
            store,
            ledger,
            issue["id"],
            1,
            "retry",
            "synthetic",
            account_id="usd-account" if legacy else "account",
        )
    assert store.one("SELECT version FROM transactions")["version"] == 1


def test_unknown_recovery_and_link_verify_underlying_account_currency(
    database, tmp_path, settings, monkeypatch
):
    store, ledger = database.store, Ledger()
    tx = queue(store, tmp_path, settings, ledger, currency="USD")
    job = store.one("SELECT * FROM jobs")
    ledger.records["existing"] = {"id": "existing", **deepcopy(job["payload"])}
    monkeypatch.setattr(
        ledger, "accounts", lambda: [{"id": "usd-account", "type": 1, "currency": "CNY"}]
    )
    store.issue("synthetic", tx["id"], {"version": 1})
    issue = store.one("SELECT * FROM issues WHERE code='synthetic'")
    with pytest.raises(ImporterError, match="frozen target currency"):
        resolve(store, ledger, issue["id"], 1, "link", "synthetic", target_id="existing")
    store.execute("UPDATE jobs SET status='unknown',target_id='existing'")
    store.execute("UPDATE transactions SET state='unknown'")
    verify_unknown(store, ledger)
    assert store.one("SELECT state FROM transactions")["state"] == "unknown"
    assert ledger.create_calls == []


def test_new_original_currency_cannot_execute_legacy_settlement_task(database, tmp_path, settings):
    store, ledger = database.store, Ledger()
    tx = queue(store, tmp_path, settings, ledger, currency="USD")
    write_queued(store, ledger, True)
    target = store.one("SELECT target_id FROM transactions")["target_id"]
    store.execute(
        "INSERT INTO jobs(transaction_id,kind,version,operation_key,payload,target_id) VALUES (%s,'settle_amount',1,'synthetic-invalid-settle',%s,%s)",
        (tx["id"], {"type": 3, "sourceAccountId": "usd-account", "sourceAmount": 7200}, target),
    )
    store.execute("UPDATE transactions SET state='queued'")
    write_queued(store, ledger, True)
    assert ledger.modify_calls == []
    assert ledger.records[target]["sourceAmount"] == 1000


def test_monthly_unknown_currency_keeps_same_card_cny_usd_candidates_ambiguous(
    database, tmp_path, settings
):
    store, ledger = database.store, Ledger()
    import_daily(store, tmp_path, settings, currency="USD")
    tx = store.one("SELECT * FROM transactions")
    # Two independent source facts with the same card/date/merchant/amount but different currencies.
    second_facts = {**tx["facts"], "original_currency": "CNY"}
    store.execute(
        "INSERT INTO transactions(id,report_key,row_key,facts,marker) VALUES ('second',%s,'second',%s,'ebki-second')",
        (tx["report_key"], second_facts),
    )
    statement(store, tx, "72.00")
    reconcile(store, ledger, tmp_path / "reports")
    assert store.one("SELECT status FROM reconciliation_items")["status"] == "ambiguous"


def test_cny_settlement_difference_is_reported_without_overwriting(database, tmp_path, settings):
    store, ledger = database.store, Ledger()
    tx = queue(store, tmp_path, settings, ledger)
    write_queued(store, ledger, True)
    statement(store, tx, "11.00")
    reconcile(store, ledger, tmp_path / "reports")
    write_queued(store, ledger, True)
    item = store.one("SELECT * FROM reconciliation_items")
    assert item["status"] == "target_changed" and item["data"]["comparison_amount"] == 1100
    assert ledger.modify_calls == []
    assert ledger.create_calls[0]["sourceAmount"] == 1000
