from copy import deepcopy
from unittest.mock import Mock

import pytest

import test_pipeline as pipeline
from test_pipeline import Ledger, import_daily
from ezbookkeeping_importer.application.classify import classify_pending
from ezbookkeeping_importer.application.write import write_queued
from ezbookkeeping_importer.domain.errors import LedgerRejected

database = pipeline.database
settings = pipeline.settings


def queued_transactions(store, tmp_path, settings, ledger):
    import_daily(store, tmp_path, settings, count=3)
    classify_pending(store, settings, ledger, None)
    bank_transactions = store.all("SELECT * FROM bank_transactions ORDER BY id")
    assert len(bank_transactions) == 3
    assert all(row["import_status"] == "queued" for row in bank_transactions)
    return bank_transactions


def test_scope_ids_are_bound_parameters_not_sql_text():
    store, ledger = Mock(), Mock()
    store.all.return_value = []
    identifier = "synthetic-id') OR TRUE --"
    write_queued(store, ledger, transaction_ids=frozenset({identifier}))
    calls = [call for call in store.all.call_args_list if "status='queued'" in call.args[0]]
    assert len(calls) == 1
    assert identifier not in calls[0].args[0]
    assert "transaction_id IN (%s)" in calls[0].args[0]
    assert calls[0].args[1] == (identifier,)
    ledger.create.assert_not_called()
    ledger.modify.assert_not_called()


def test_empty_scope_only_performs_unknown_reads():
    store, ledger = Mock(), Mock()
    store.all.return_value = []
    write_queued(store, ledger, transaction_ids=frozenset())
    assert store.all.call_count == 1
    assert "status='unknown'" in store.all.call_args.args[0]
    ledger.create.assert_not_called()
    ledger.modify.assert_not_called()


def test_subset_sends_only_selected_transactions_and_default_still_sends_remaining(
    database, settings, tmp_path
):
    store, ledger = database.store, Ledger()
    bank_transactions = queued_transactions(store, tmp_path, settings, ledger)
    selected = bank_transactions[1]
    write_queued(store, ledger, transaction_ids=frozenset({selected["id"]}))
    assert len(ledger.create_calls) == 1
    assert ledger.create_calls[0]["clientSessionId"] == selected["source_marker"]
    states = {
        row["id"]: row["import_status"]
        for row in store.all("SELECT id,import_status FROM bank_transactions")
    }
    assert states[selected["id"]] == "booked"
    assert all(
        import_status == "queued"
        for identifier, import_status in states.items()
        if identifier != selected["id"]
    )
    write_queued(store, ledger)
    assert len(ledger.create_calls) == 3
    assert all(
        row["import_status"] == "booked"
        for row in store.all("SELECT import_status FROM bank_transactions")
    )


@pytest.mark.parametrize(
    "scope", [frozenset(), frozenset({"missing-id"}), frozenset({"synthetic-id') OR TRUE --"})]
)
def test_empty_or_unmatched_scope_does_not_send_or_change_queued_jobs(
    database, settings, tmp_path, scope
):
    store, ledger = database.store, Ledger()
    queued_transactions(store, tmp_path, settings, ledger)
    before = store.all("SELECT * FROM background_task ORDER BY id")
    write_queued(store, ledger, transaction_ids=scope)
    assert ledger.create_calls == ledger.modify_calls == []
    assert store.all("SELECT * FROM background_task ORDER BY id") == before


@pytest.mark.parametrize("failure", ["rejected", "unknown", "preflight"])
def test_scoped_failure_cannot_release_other_transactions(
    database, settings, tmp_path, monkeypatch, failure
):
    store, ledger = database.store, Ledger()
    bank_transactions = queued_transactions(store, tmp_path, settings, ledger)
    selected = bank_transactions[0]
    if failure == "rejected":

        def reject(payload):
            ledger.create_calls.append(deepcopy(payload))
            raise LedgerRejected(200008)

        monkeypatch.setattr(ledger, "create", reject)
    elif failure == "unknown":
        ledger.mode = "timeout_empty"
    else:
        monkeypatch.setattr(ledger, "accounts", lambda: [])
    scope = frozenset({selected["id"]})
    write_queued(store, ledger, transaction_ids=scope)
    write_queued(store, ledger, transaction_ids=scope)
    assert len(ledger.create_calls) == (0 if failure == "preflight" else 1)
    assert all(call["clientSessionId"] == selected["source_marker"] for call in ledger.create_calls)
    outside = store.all(
        "SELECT * FROM background_task WHERE bank_transaction_id<>%s", (selected["id"],)
    )
    assert len(outside) == 2 and all(job["status"] == "queued" for job in outside)
    if failure == "unknown":
        assert (
            store.one(
                "SELECT status FROM background_task WHERE bank_transaction_id=%s", (selected["id"],)
            )["status"]
            == "unknown"
        )


def test_scope_also_bounds_legacy_settlement_posts(database, settings, tmp_path):
    store, ledger = database.store, Ledger()
    bank_transactions = queued_transactions(store, tmp_path, settings, ledger)
    for tx in bank_transactions:
        import_decision = {
            **tx["import_decision"],
            "target_currency": "CNY",
            "rate_snapshot": {"dataSource": "synthetic old quote", "adoptedRate": "7"},
        }
        payload = {**import_decision["payload"], "sourceAmount": 7000}
        import_decision["payload"] = payload
        store.execute(
            "UPDATE bank_transactions SET original_currency='USD',import_decision=%s WHERE id=%s",
            (import_decision, tx["id"]),
        )
        store.execute(
            "UPDATE background_task SET payload=%s WHERE bank_transaction_id=%s",
            (payload, tx["id"]),
        )
    write_queued(store, ledger)
    booked = store.all("SELECT * FROM bank_transactions ORDER BY id")
    for tx in booked:
        store.execute(
            "INSERT INTO background_task(bank_transaction_id,task_type,decision_version,operation_key,payload,ledger_transaction_id) VALUES (%s,'settle_amount',1,%s,%s,%s)",
            (
                tx["id"],
                "synthetic-settle:" + tx["id"],
                {"type": 3, "sourceAccountId": "account", "sourceAmount": 7200},
                tx["ledger_transaction_id"],
            ),
        )

    selected = booked[1]
    write_queued(store, ledger, transaction_ids=frozenset({selected["id"]}))
    assert (
        len(ledger.modify_calls) == 1
        and ledger.modify_calls[0]["id"] == selected["ledger_transaction_id"]
    )
    for tx in booked:
        assert ledger.records[tx["ledger_transaction_id"]]["sourceAmount"] == (
            7200 if tx["id"] == selected["id"] else 7000
        )


def test_readonly_unknown_confirmation_outside_scope_does_not_dispatch_queued_writes(
    database, settings, tmp_path
):
    store, ledger = database.store, Ledger()
    bank_transactions = queued_transactions(store, tmp_path, settings, ledger)
    unknown = bank_transactions[0]
    payload = unknown["import_decision"]["payload"]
    ledger.records["previously-created"] = {"id": "previously-created", **payload}
    store.execute(
        "UPDATE background_task SET status='unknown',ledger_transaction_id='previously-created' WHERE bank_transaction_id=%s",
        (unknown["id"],),
    )
    store.execute(
        "UPDATE bank_transactions SET import_status='unknown' WHERE id=%s", (unknown["id"],)
    )
    write_queued(store, ledger, transaction_ids=frozenset())
    assert (
        store.one("SELECT import_status FROM bank_transactions WHERE id=%s", (unknown["id"],))[
            "import_status"
        ]
        == "booked"
    )
    assert ledger.create_calls == ledger.modify_calls == []
    assert store.one("SELECT count(*) AS n FROM background_task WHERE status='queued'")["n"] == 2
