"""原交易额或交易日对不上时，美元日报仍在原 ID 上转人民币，不重复补录。"""

from copy import deepcopy
from datetime import date, timedelta
from email import policy
from email.parser import BytesParser

import pytest
import test_pipeline as p

from ezbookkeeping_importer.adapters.banks.cmb import BankParser
from ezbookkeeping_importer.adapters.evidence_store import EvidenceStore
from ezbookkeeping_importer.application.classify import classify_pending
from ezbookkeeping_importer.application.parse import parse_pending
from ezbookkeeping_importer.application.reconcile import reconcile
from ezbookkeeping_importer.application.sources import link_source, register_sources
from ezbookkeeping_importer.application.write import write_queued
from ezbookkeeping_importer.domain.errors import ImporterError

database = p.database
settings = p.settings


def configured(settings):
    return settings.model_copy(update={"date_only_time": "12:00:00"})


def add_monthly(store, rows):
    base = BankParser(context="synthetic").parse(p.raw_daily()).rows[0].model_dump(mode="json")
    full = []
    for i, changes in enumerate(rows):
        full.append({**base, "row_key": f"monthly-{i}", "occurred_at": None, "time_precision": "date",
                     "original_currency": None, "settlement_currency": "CNY",
                     "posted_date": base["occurred_date"],
                     "extra": {"statement_group": "消费", "country": "US"}, **changes})
    transaction = dict(report_row_key="unused", event_type="expense", occurred_date=base["occurred_date"],
                       occurred_at=None, time_precision="date", card_reference="1234",
                       merchant_name="x", original_amount="1", original_currency=None, posted_date=None,
                       bank_settlement_amount=None, bank_settlement_currency=None, source_details={})
    p.statement(store, transaction, "0", rows=full)
    with store.transaction():
        register_sources(store, store.one("SELECT * FROM bank_report WHERE report_type='monthly'"))
    return full


def booked_usd(store, tmp_path, settings, ledger, amount="20.79"):
    tx = p.queue(store, tmp_path, settings, ledger, currency="USD", amount=amount,
                 merchant="RESEND +14152134101 CA US")
    write_queued(store, ledger)
    return store.one("SELECT * FROM bank_transactions WHERE id=%s", (tx["id"],))


def settle_cycle(store, ledger, settings, tmp_path):
    reconcile(store, ledger, tmp_path)
    classify_pending(store, configured(settings), ledger, None)
    write_queued(store, ledger)
    write_queued(store, ledger)
    reconcile(store, ledger, tmp_path)


def shifted(day, days):
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


@pytest.mark.parametrize("day_shift", [0, 1])
def test_usd_daily_with_different_statement_original_settles_same_id(
        database, settings, tmp_path, day_shift):
    store, ledger = database.store, p.Ledger()
    daily = booked_usd(store, tmp_path, settings, ledger)
    target = daily["ledger_transaction_id"]
    add_monthly(store, [{"original_amount": "21.00", "settlement_amount": "138.79", "merchant_raw": "RESEND",
                         "occurred_date": shifted(str(daily["occurred_date"]), day_shift)}])
    settle_cycle(store, ledger, settings, tmp_path)
    assert store.one("SELECT count(*) n FROM bank_transactions")["n"] == 1
    assert len(ledger.create_calls) == 1 and len(ledger.modify_calls) == 1
    assert ledger.records[target]["sourceAccountId"] == "account"
    assert ledger.records[target]["sourceAmount"] == 13879
    current = store.one("SELECT * FROM bank_transactions")
    assert current["original_currency"] == "USD" and current["import_decision"]["target_currency"] == "CNY"
    assert store.one("SELECT count(*) n FROM transaction_source WHERE transaction_id=%s", (daily["id"],))["n"] == 2


def two_usd(store, tmp_path, settings, ledger):
    message = BytesParser(policy=policy.default).parsebytes(
        p.raw_daily(count=2, currency="USD", amount="20.79", merchant="FIRST"))
    message.set_content(message.get_content().replace("20.79", "30.00", 1), subtype="html")
    p.ingest_mail(store, EvidenceStore(tmp_path / "evidence"), message.as_bytes(), settings)
    parse_pending(store, BankParser(context="synthetic"))
    classify_pending(store, settings, ledger, None)
    write_queued(store, ledger)
    write_queued(store, ledger)
    return store.all("SELECT * FROM bank_transactions ORDER BY id")


def test_two_unsettled_usd_pause_then_manual_link_settles_only_selected(database, settings, tmp_path):
    store, ledger = database.store, p.Ledger()
    first, second = two_usd(store, tmp_path, settings, ledger)
    add_monthly(store, [{"original_amount": "21.00", "settlement_amount": "138.79"}])
    settle_cycle(store, ledger, settings, tmp_path)
    assert len(ledger.create_calls) == 2 and ledger.modify_calls == []
    result = store.one("SELECT * FROM bank_statement_reconciliation WHERE check_direction='statement_to_transaction'")
    assert result["match_status"] == "ambiguous"
    assert set(result["details"]["candidate_ids"]) == {first["id"], second["id"]}
    from ezbookkeeping_importer.application.source_issues import source_issues
    issue = next(i for i in source_issues(store) if i["context"]["report_type"] == "monthly")
    assert issue["code"] == "source_ambiguous" and set(issue["detail"]["candidate_ids"]) == {first["id"], second["id"]}
    with store.transaction():
        link_source(store, "monthly-report", "monthly-0", first["id"],
                    resolution={"action": "link", "reason": "核实为同一笔"})
    settle_cycle(store, ledger, settings, tmp_path)
    assert len(ledger.create_calls) == 2 and len(ledger.modify_calls) == 1
    assert ledger.records[first["ledger_transaction_id"]]["sourceAmount"] == 13879
    assert ledger.records[second["ledger_transaction_id"]]["sourceAccountId"] == "usd-account"


def test_manual_link_rejects_other_card_or_far_date(database, settings, tmp_path):
    store, ledger = database.store, p.Ledger()
    daily = booked_usd(store, tmp_path, settings, ledger)
    add_monthly(store, [{"original_amount": "21.00", "settlement_amount": "138.79",
                         "occurred_date": shifted(str(daily["occurred_date"]), 3)}])
    with pytest.raises(ImporterError, match="does not match"), store.transaction():
        link_source(store, "monthly-report", "monthly-0", daily["id"],
                    resolution={"action": "link", "reason": "错误目标"})


def test_cny_daily_with_different_statement_amount_pauses_without_duplicate(database, settings, tmp_path):
    store, ledger = database.store, p.Ledger()
    p.queue(store, tmp_path, settings, ledger, currency="CNY", amount="138.78", merchant="RESEND +1 CA US")
    write_queued(store, ledger)
    add_monthly(store, [{"original_amount": "20.79", "settlement_amount": "138.79", "merchant_raw": "RESEND"}])
    settle_cycle(store, ledger, settings, tmp_path)
    assert len(ledger.records) == 1 and len(ledger.create_calls) == 1
    assert store.one("SELECT count(*) n FROM bank_transactions")["n"] == 1


def test_foreign_row_without_any_daily_is_recovered_once(database, settings, tmp_path):
    store, ledger = database.store, p.Ledger()
    add_monthly(store, [{"original_amount": "21.00", "settlement_amount": "138.79"}])
    settle_cycle(store, ledger, settings, tmp_path)
    settle_cycle(store, ledger, settings, tmp_path)
    assert len(ledger.create_calls) == 1 and ledger.create_calls[0]["sourceAmount"] == 13879


def test_monthly_first_then_usd_daily_with_different_original_keeps_cny(database, settings, tmp_path):
    store, ledger = database.store, p.Ledger()
    add_monthly(store, [{"original_amount": "21.00", "settlement_amount": "138.79"}])
    settle_cycle(store, ledger, settings, tmp_path)
    before = store.one("SELECT * FROM bank_transactions")
    p.import_daily(store, tmp_path, settings, currency="USD", amount="20.79")
    classify_pending(store, configured(settings), ledger, None)
    write_queued(store, ledger)
    assert store.one("SELECT count(*) n FROM bank_transactions")["n"] == 1
    assert store.one("SELECT * FROM bank_transactions") == before
    assert len(ledger.create_calls) == 1 and ledger.records[before["ledger_transaction_id"]]["sourceAmount"] == 13879


def moved_account(ledger, account_id, comment):
    original = ledger.accounts
    ledger.accounts = lambda: [*original(), {"id": account_id, "type": 1, "currency": "USD",
                                             "hidden": False, "comment": comment}]


def test_same_card_usd_account_moved_still_settles_and_records_actual_from(database, settings, tmp_path):
    store, ledger = database.store, p.Ledger()
    daily = booked_usd(store, tmp_path, settings, ledger, amount="21.00")
    moved_account(ledger, "usd-account-2", "卡号 4444333322221234")
    ledger.records[daily["ledger_transaction_id"]]["sourceAccountId"] = "usd-account-2"
    add_monthly(store, [{"original_amount": "21.00", "settlement_amount": "138.79"}])
    settle_cycle(store, ledger, settings, tmp_path)
    assert len(ledger.modify_calls) == 1
    assert ledger.records[daily["ledger_transaction_id"]]["sourceAmount"] == 13879
    adjustment = store.one("SELECT settlement_adjustment FROM bank_transactions")["settlement_adjustment"]
    assert adjustment["from"]["account_id"] == "usd-account-2"


@pytest.mark.parametrize("change", ["other-card", "amount", "time"])
def test_moved_account_with_other_card_or_user_edits_still_blocks(database, settings, tmp_path, change):
    store, ledger = database.store, p.Ledger()
    daily = booked_usd(store, tmp_path, settings, ledger, amount="21.00")
    record = ledger.records[daily["ledger_transaction_id"]]
    if change == "other-card":
        moved_account(ledger, "other-usd", "卡号 5555666677778888")
        record["sourceAccountId"] = "other-usd"
    else:
        moved_account(ledger, "usd-account-2", "卡号 4444333322221234")
        record["sourceAccountId"] = "usd-account-2"
        record["sourceAmount" if change == "amount" else "time"] += 1
    before = deepcopy(record)
    add_monthly(store, [{"original_amount": "21.00", "settlement_amount": "138.79"}])
    settle_cycle(store, ledger, settings, tmp_path)
    assert ledger.modify_calls == [] and ledger.records[daily["ledger_transaction_id"]] == before
    assert store.one("SELECT error_code FROM background_task WHERE task_type='settle_currency'")["error_code"] == "write_preflight_failed"


def test_domestic_monthly_recovery_does_not_swallow_late_usd_daily(database, settings, tmp_path):
    store, ledger = database.store, p.Ledger()
    add_monthly(store, [{"original_amount": "18.00", "settlement_amount": "18.00", "merchant_raw": "国内商户",
                         "extra": {"statement_group": "消费", "country": "CN"}}])
    settle_cycle(store, ledger, settings, tmp_path)
    assert len(ledger.create_calls) == 1
    p.import_daily(store, tmp_path, settings, currency="USD", amount="20.79", merchant="RESEND +1 CA US")
    classify_pending(store, configured(settings), ledger, None)
    write_queued(store, ledger)
    write_queued(store, ledger)
    assert store.one("SELECT count(*) n FROM bank_transactions")["n"] == 2
    assert len(ledger.create_calls) == 2


def test_cross_day_unrelated_merchant_pauses_instead_of_settling(database, settings, tmp_path):
    store, ledger = database.store, p.Ledger()
    daily = booked_usd(store, tmp_path, settings, ledger)
    add_monthly(store, [{"original_amount": "21.00", "settlement_amount": "138.79", "merchant_raw": "OTHER SHOP",
                         "occurred_date": shifted(str(daily["occurred_date"]), 1)}])
    settle_cycle(store, ledger, settings, tmp_path)
    assert ledger.modify_calls == [] and len(ledger.create_calls) == 1
    result = store.one("SELECT * FROM bank_statement_reconciliation WHERE check_direction='statement_to_transaction'")
    assert result["match_status"] == "ambiguous" and result["details"]["candidate_ids"] == [daily["id"]]
    # 人工核实后仍可手工关联并在原 ID 结算。
    with store.transaction():
        link_source(store, "monthly-report", "monthly-0", daily["id"],
                    resolution={"action": "link", "reason": "核实为同一笔"})
    settle_cycle(store, ledger, settings, tmp_path)
    assert len(ledger.modify_calls) == 1 and ledger.records[daily["ledger_transaction_id"]]["sourceAmount"] == 13879


def test_manual_residual_link_is_limited_to_foreign_rows(database, settings, tmp_path):
    store, ledger = database.store, p.Ledger()
    daily = booked_usd(store, tmp_path, settings, ledger)
    add_monthly(store, [{"original_amount": "21.00", "settlement_amount": "138.79",
                         "extra": {"statement_group": "消费", "country": "CN"}}])
    with pytest.raises(ImporterError, match="does not match"), store.transaction():
        link_source(store, "monthly-report", "monthly-0", daily["id"],
                    resolution={"action": "link", "reason": "国内行不走剩余配对"})


def test_late_usd_daily_with_two_foreign_monthly_candidates_is_reported_ambiguous(database, settings, tmp_path):
    store, ledger = database.store, p.Ledger()
    add_monthly(store, [{"original_amount": "21.00", "settlement_amount": "138.79"},
                        {"original_amount": "30.00", "settlement_amount": "200.00"}])
    settle_cycle(store, ledger, settings, tmp_path)
    assert len(ledger.create_calls) == 2
    p.import_daily(store, tmp_path, settings, currency="USD", amount="20.79")
    classify_pending(store, configured(settings), ledger, None)
    write_queued(store, ledger)
    assert store.one("SELECT count(*) n FROM bank_transactions")["n"] == 2 and len(ledger.create_calls) == 2
    from ezbookkeeping_importer.application.source_issues import source_issues
    issue = next(i for i in source_issues(store) if i["context"]["report_type"] == "daily")
    assert issue["code"] == "source_ambiguous" and len(issue["detail"]["candidate_ids"]) == 2


def test_manual_residual_link_rejects_confirmed_original_currency_conflict(database, settings, tmp_path):
    store, ledger = database.store, p.Ledger()
    daily = booked_usd(store, tmp_path, settings, ledger)
    add_monthly(store, [{"original_amount": "21.00", "original_currency": "HKD", "settlement_amount": "138.79"}])
    with pytest.raises(ImporterError, match="does not match"), store.transaction():
        link_source(store, "monthly-report", "monthly-0", daily["id"],
                    resolution={"action": "link", "reason": "原币冲突"})


def test_two_late_usd_daily_rows_sharing_one_monthly_candidate_pause_without_failing(
        database, settings, tmp_path):
    store, ledger = database.store, p.Ledger()
    add_monthly(store, [{"original_amount": "21.00", "settlement_amount": "138.79"}])
    settle_cycle(store, ledger, settings, tmp_path)
    assert len(ledger.create_calls) == 1
    message = BytesParser(policy=policy.default).parsebytes(
        p.raw_daily(count=2, currency="USD", amount="20.79", message_id="late"))
    message.set_content(message.get_content().replace("20.79", "30.00", 1), subtype="html")
    p.ingest_mail(store, EvidenceStore(tmp_path / "evidence"), message.as_bytes(), settings)
    parse_pending(store, BankParser(context="synthetic"))
    assert store.one("SELECT count(*) n FROM email WHERE parse_status='failed'")["n"] == 0
    assert store.one("SELECT count(*) n FROM bank_transactions")["n"] == 1
    daily = store.one("SELECT count(*) n FROM transaction_source s JOIN bank_report r USING(report_key) "
                      "WHERE r.report_type='daily' AND s.transaction_id IS NOT NULL")
    assert daily["n"] == 0


def test_blocked_foreign_row_can_still_link_explicit_remote_target(database, settings, tmp_path):
    from datetime import datetime

    from ezbookkeeping_importer.application.issue_snapshot import resolve_snapshot, snapshot_issues
    store, ledger = database.store, p.Ledger()
    p.queue(store, tmp_path, settings, ledger, currency="CNY", amount="18.00", merchant="国内商户")
    write_queued(store, ledger)
    add_monthly(store, [{"original_amount": "21.00", "settlement_amount": "138.79", "merchant_raw": "RESEND"}])
    settle_cycle(store, ledger, settings, tmp_path)
    assert len(ledger.create_calls) == 1
    ledger.records["manual"] = {
        "id": "manual", "type": 3, "time": int(datetime.fromisoformat("2026-01-01T12:00:00+08:00").timestamp()),
        "utcOffset": 480, "sourceAmount": 13879, "sourceAccountId": "account",
        "categoryId": "category", "comment": "用户手工补记", "tagIds": [],
    }
    snapshot = snapshot_issues(store, "transaction_source", "monthly-report:monthly-0")
    resolve_snapshot(store, ledger, snapshot, "link", "近日那笔是国内消费，手工账才是本笔", target_id="manual")
    linked = store.one("SELECT t.* FROM transaction_source s JOIN bank_transactions t ON t.id=s.transaction_id "
                       "WHERE s.report_key='monthly-report'")
    assert linked["ledger_transaction_id"] == "manual" and len(ledger.create_calls) == 1


def test_ignored_daily_row_no_longer_competes_for_reverse_candidate(database, settings, tmp_path):
    from ezbookkeeping_importer.application.issue_snapshot import resolve_snapshot, snapshot_issues
    store, ledger = database.store, p.Ledger()
    add_monthly(store, [{"original_amount": "21.00", "settlement_amount": "138.79"}])
    settle_cycle(store, ledger, settings, tmp_path)
    recovered = store.one("SELECT * FROM bank_transactions")
    message = BytesParser(policy=policy.default).parsebytes(
        p.raw_daily(count=2, currency="USD", amount="20.79", message_id="late"))
    message.set_content(message.get_content().replace("20.79", "30.00", 1), subtype="html")
    p.ingest_mail(store, EvidenceStore(tmp_path / "evidence"), message.as_bytes(), settings)
    parse_pending(store, BankParser(context="synthetic"))
    rows = store.all("SELECT s.* FROM transaction_source s JOIN bank_report r USING(report_key) "
                     "WHERE r.report_type='daily' ORDER BY s.row_key")
    assert [r["transaction_id"] for r in rows] == [None, None]
    first, second = (f"{r['report_key']}:{r['row_key']}" for r in rows)
    resolve_snapshot(store, None, snapshot_issues(store, "transaction_source", first), "ignore", "不是本笔")
    resolve_snapshot(store, None, snapshot_issues(store, "transaction_source", second), "retry", "重新关联")
    linked = store.one("SELECT transaction_id FROM transaction_source WHERE report_key=%s AND row_key=%s",
                       tuple(second.split(":")))
    assert linked["transaction_id"] == recovered["id"]
    assert store.one("SELECT count(*) n FROM bank_transactions")["n"] == 1


def test_legacy_cny_estimate_settlement_does_not_accept_moved_account(database, settings, tmp_path):
    store, ledger = database.store, p.Ledger()
    tx = p.queue_legacy_estimate(store, tmp_path, settings, ledger)
    write_queued(store, ledger)
    tx = store.one("SELECT * FROM bank_transactions WHERE id=%s", (tx["id"],))
    original = ledger.accounts
    ledger.accounts = lambda: [*original(), {"id": "cny-2", "type": 1, "currency": "CNY",
                                             "hidden": False, "comment": "卡号 4444333322221234"}]
    ledger.records[tx["ledger_transaction_id"]]["sourceAccountId"] = "cny-2"
    add_monthly(store, [{"original_amount": "10.00", "settlement_amount": "71.00"}])
    settle_cycle(store, ledger, settings, tmp_path)
    assert ledger.modify_calls == []
    assert store.one("SELECT error_code FROM background_task WHERE task_type='settle_amount'")["error_code"] \
        == "write_preflight_failed"
