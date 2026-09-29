"""USD 原始事实与 CNY 月结决定各自保持权威。"""

from copy import deepcopy
import pytest
import test_pipeline as pipeline
from ezbookkeeping_importer.application.classify import classify_pending
from ezbookkeeping_importer.application.write import write_queued, recover_dispatching
from ezbookkeeping_importer.application.resolve import resolve
from ezbookkeeping_importer.application.reconcile import reconcile
from ezbookkeeping_importer.application.maintenance import issues
from ezbookkeeping_importer.domain.errors import ImporterError

database = pipeline.database
settings = pipeline.settings


def prepared(database, tmp_path, settings):
    store, ledger = database.store, pipeline.Ledger()
    tx = pipeline.queue(store, tmp_path, settings, ledger, currency="USD")
    write_queued(store, ledger)
    target = store.one("SELECT ledger_transaction_id FROM bank_transactions")[
        "ledger_transaction_id"
    ]
    pipeline.statement(store, tx, "72.00")
    return store, ledger, tx, target


def test_usd_monthly_settlement_changes_account_on_same_id_preserving_current_fields(
    database, tmp_path, settings
):
    store, ledger, tx, target = prepared(database, tmp_path, settings)
    assert ledger.create_calls[0]["sourceAccountId"] == "usd-account"
    assert ledger.create_calls[0]["sourceAmount"] == 1000
    ledger.records[target].update(
        categoryId="edited-category",
        comment=tx["source_marker"] + " 用户备注",
        tagIds=["tag"],
        pictures=[{"pictureId": "picture"}],
        hideAmount=True,
        geoLocation={"latitude": 31, "longitude": 121},
    )
    before = deepcopy(ledger.records[target])
    reconcile(store, ledger, tmp_path)
    task = store.one("SELECT * FROM background_task WHERE task_type='settle_currency'")
    assert task is not None
    write_queued(store, ledger)
    assert len(ledger.create_calls) == len(ledger.modify_calls) == 1
    actual = ledger.records[target]
    assert actual["sourceAccountId"] == "account" and actual["sourceAmount"] == 7200
    for key in ("id", "categoryId", "comment", "tagIds", "time", "hideAmount", "geoLocation"):
        assert actual[key] == before[key]
    assert ledger.modify_calls[0]["pictureIds"] == ["picture"]
    updated = store.one("SELECT * FROM bank_transactions")
    assert updated["original_currency"] == "USD" and updated["original_amount"] == 10
    assert updated["source_marker"] == tx["source_marker"]
    assert updated["ledger_transaction_id"] == target
    assert updated["import_decision"]["target_currency"] == "CNY"
    assert updated["import_decision"]["account_match"]["account_id"] == "account"
    assert updated["import_decision"]["account_match"]["currency"] == "CNY"
    assert updated["settlement_adjustment"] is not None
    reconcile(store, ledger, tmp_path)
    write_queued(store, ledger)
    assert len(ledger.modify_calls) == 1


def test_settlement_lost_response_recovers_without_resend(
    database, tmp_path, settings, monkeypatch
):
    store, ledger, tx, target = prepared(database, tmp_path, settings)
    reconcile(store, ledger, tmp_path)
    modify = ledger.modify

    def timeout(payload):
        modify(payload)
        raise TimeoutError("synthetic response lost")

    monkeypatch.setattr(ledger, "modify", timeout)
    write_queued(store, ledger)
    store.close()
    restarted = database.connect()
    recover_dispatching(restarted)
    write_queued(restarted, ledger)
    assert len(ledger.modify_calls) == 1 and len(ledger.create_calls) == 1
    assert (
        restarted.one("SELECT status FROM background_task WHERE task_type='settle_currency'")[
            "status"
        ]
        == "done"
    )
    assert ledger.records[target]["sourceAmount"] == 7200


def test_already_applied_settlement_has_no_write_attempt(database, tmp_path, settings):
    store, ledger, tx, target = prepared(database, tmp_path, settings)
    reconcile(store, ledger, tmp_path)
    ledger.records[target].update(sourceAccountId="account", sourceAmount=7200)
    write_queued(store, ledger)
    task = store.one("SELECT * FROM background_task WHERE task_type='settle_currency'")
    assert task["status"] == "done" and task["completion_method"] == "already_applied"
    assert (
        store.one("SELECT count(*) AS n FROM ledger_write_attempt WHERE task_id=%s", (task["id"],))[
            "n"
        ]
        == 0
    )
    assert ledger.modify_calls == []


def test_missing_settlement_target_never_recreates(database, tmp_path, settings):
    store, ledger, tx, target = prepared(database, tmp_path, settings)
    reconcile(store, ledger, tmp_path)
    del ledger.records[target]
    write_queued(store, ledger)
    assert len(ledger.create_calls) == 1 and ledger.modify_calls == []
    assert any(i["entity_type"] == "background_task" for i in issues(store))


@pytest.mark.parametrize("account_case", ["absent", "hidden", "ambiguous"])
def test_cny_account_must_be_unique_visible_before_settlement(
    database, tmp_path, settings, monkeypatch, account_case
):
    store, ledger, tx, target = prepared(database, tmp_path, settings)
    accounts = ledger.accounts()
    if account_case == "absent":
        accounts = accounts[1:]
    elif account_case == "hidden":
        accounts[0]["hidden"] = True
    else:
        accounts.append({**accounts[0], "id": "second-cny"})
    monkeypatch.setattr(ledger, "accounts", lambda: accounts)
    reconcile(store, ledger, tmp_path)
    write_queued(store, ledger)
    assert ledger.modify_calls == [] and ledger.records[target]["sourceAccountId"] == "usd-account"
    assert issues(store)


def test_account_failure_is_current_transaction_diagnostic(
    database, tmp_path, settings, monkeypatch
):
    store, ledger = database.store, pipeline.Ledger()
    pipeline.import_daily(store, tmp_path, settings, currency="USD")
    monkeypatch.setattr(ledger, "accounts", lambda: [])
    classify_pending(store, settings, ledger, None)
    problem = next(i for i in issues(store) if i["entity_type"] == "bank_transactions")
    assert problem["code"] == "account_not_found"
    assert store.one("SELECT count(*) AS n FROM background_task")["n"] == 0


def test_manual_retry_cannot_change_frozen_currency(database, tmp_path, settings):
    store, ledger = database.store, pipeline.Ledger()
    tx = pipeline.queue(store, tmp_path, settings, ledger, currency="USD")
    with pytest.raises(ImporterError, match="frozen target currency"):
        resolve(
            store, ledger, "bank_transactions", tx["id"], 1, "retry", "核实", account_id="account"
        )
    assert store.one("SELECT decision_version FROM bank_transactions")["decision_version"] == 1


def test_cny_difference_is_diagnostic_not_automatic_repricing(database, tmp_path, settings):
    store, ledger = database.store, pipeline.Ledger()
    tx = pipeline.queue(store, tmp_path, settings, ledger)
    write_queued(store, ledger)
    pipeline.statement(store, tx, "11.00")
    reconcile(store, ledger, tmp_path)
    write_queued(store, ledger)
    result = store.one(
        "SELECT * FROM bank_statement_reconciliation WHERE check_direction='statement_to_transaction'"
    )
    assert result["ledger_check_status"] == "mismatched" and result["expected_amount"] == 11
    assert ledger.modify_calls == []


@pytest.mark.parametrize("changed", [{"sourceAmount": 999}, {"sourceAccountId": "other-account"}])
def test_remote_settlement_amount_or_account_change_is_not_overwritten(
    database, tmp_path, settings, changed
):
    store, ledger, tx, target = prepared(database, tmp_path, settings)
    reconcile(store, ledger, tmp_path)
    ledger.records[target].update(changed)
    write_queued(store, ledger)
    assert ledger.modify_calls == []
    task = store.one("SELECT * FROM background_task WHERE task_type='settle_currency'")
    assert task["error_code"] == "write_preflight_failed"
    assert store.one("SELECT import_decision FROM bank_transactions")["import_decision"][
        "target_currency"
    ] == "USD"


def test_rejected_currency_settlement_retries_same_identity_and_frozen_target(
    database, tmp_path, settings, monkeypatch
):
    from ezbookkeeping_importer.domain.errors import LedgerRejected

    store, ledger, tx, target = prepared(database, tmp_path, settings)
    reconcile(store, ledger, tmp_path)
    original_modify = ledger.modify

    def reject(payload):
        raise LedgerRejected("synthetic rejection")

    monkeypatch.setattr(ledger, "modify", reject)
    write_queued(store, ledger)
    task = store.one("SELECT * FROM background_task WHERE task_type='settle_currency'")
    assert task["status"] == "rejected"
    assert store.one("SELECT import_status FROM bank_transactions")["import_status"] == "booked"
    resolve(store, ledger, "background_task", str(task["id"]), task["decision_version"],
            "retry", "修复拒绝原因后核实重试")
    monkeypatch.setattr(ledger, "modify", original_modify)
    write_queued(store, ledger)
    retried = store.one("SELECT * FROM background_task WHERE task_type='settle_currency'")
    assert retried["id"] == task["id"] and retried["operation_key"] == task["operation_key"]
    assert retried["status"] == "done" and retried["ledger_transaction_id"] == target
    assert retried["payload"]["request"]["sourceAmount"] == 7200
    attempts = store.all("SELECT * FROM ledger_write_attempt WHERE task_id=%s ORDER BY id", (task["id"],))
    assert [a["outcome"] for a in attempts] == ["rejected", "confirmed"]
    assert [a["decision_version"] for a in attempts] == [1, 2]
    assert len(ledger.create_calls) == 1 and len(ledger.modify_calls) == 1
