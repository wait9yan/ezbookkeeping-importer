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
    transactions = store.all("SELECT * FROM transactions ORDER BY id")
    assert len(transactions) == 3
    assert all(row["state"] == "queued" for row in transactions)
    return transactions


def test_scope_ids_are_bound_parameters_not_sql_text():
    store, ledger = Mock(), Mock()
    store.all.return_value = []
    identifier = "synthetic-id') OR TRUE --"
    write_queued(store, ledger, True, transaction_ids=frozenset({identifier}))
    calls = [call for call in store.all.call_args_list if "status='queued'" in call.args[0]]
    assert len(calls) == 1
    assert identifier not in calls[0].args[0]
    assert "transaction_id IN (%s)" in calls[0].args[0]
    assert calls[0].args[1] == (identifier,)
    ledger.create.assert_not_called()
    ledger.modify.assert_not_called()


@pytest.mark.parametrize(
    "enabled,scope", [(True, frozenset()), (False, frozenset({"synthetic"})), (False, None)]
)
def test_empty_scope_and_disabled_writes_only_perform_unknown_reads(enabled, scope):
    store, ledger = Mock(), Mock()
    store.all.return_value = []
    write_queued(store, ledger, enabled, transaction_ids=scope)
    assert store.all.call_count == 1
    assert "status='unknown'" in store.all.call_args.args[0]
    ledger.create.assert_not_called()
    ledger.modify.assert_not_called()


def test_subset_sends_only_selected_transactions_and_default_still_sends_remaining(
    database, settings, tmp_path
):
    store, ledger = database.store, Ledger()
    transactions = queued_transactions(store, tmp_path, settings, ledger)
    selected = transactions[1]
    write_queued(store, ledger, True, transaction_ids=frozenset({selected["id"]}))
    assert len(ledger.create_calls) == 1
    assert ledger.create_calls[0]["clientSessionId"] == selected["marker"]
    states = {row["id"]: row["state"] for row in store.all("SELECT id,state FROM transactions")}
    assert states[selected["id"]] == "booked"
    assert all(
        state == "queued" for identifier, state in states.items() if identifier != selected["id"]
    )
    write_queued(store, ledger, True)
    assert len(ledger.create_calls) == 3
    assert all(row["state"] == "booked" for row in store.all("SELECT state FROM transactions"))


@pytest.mark.parametrize(
    "scope", [frozenset(), frozenset({"missing-id"}), frozenset({"synthetic-id') OR TRUE --"})]
)
def test_empty_or_unmatched_scope_does_not_send_or_change_queued_jobs(
    database, settings, tmp_path, scope
):
    store, ledger = database.store, Ledger()
    queued_transactions(store, tmp_path, settings, ledger)
    before = store.all("SELECT * FROM jobs ORDER BY id")
    write_queued(store, ledger, True, transaction_ids=scope)
    assert ledger.create_calls == ledger.modify_calls == []
    assert store.all("SELECT * FROM jobs ORDER BY id") == before


@pytest.mark.parametrize("failure", ["rejected", "unknown", "preflight"])
def test_scoped_failure_cannot_release_other_transactions(
    database, settings, tmp_path, monkeypatch, failure
):
    store, ledger = database.store, Ledger()
    transactions = queued_transactions(store, tmp_path, settings, ledger)
    selected = transactions[0]
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
    write_queued(store, ledger, True, transaction_ids=scope)
    write_queued(store, ledger, True, transaction_ids=scope)
    assert len(ledger.create_calls) == (0 if failure == "preflight" else 1)
    assert all(call["clientSessionId"] == selected["marker"] for call in ledger.create_calls)
    outside = store.all("SELECT * FROM jobs WHERE transaction_id<>%s", (selected["id"],))
    assert len(outside) == 2 and all(job["status"] == "queued" for job in outside)
    if failure == "unknown":
        assert (
            store.one("SELECT status FROM jobs WHERE transaction_id=%s", (selected["id"],))[
                "status"
            ]
            == "unknown"
        )


def test_scope_also_bounds_legacy_settlement_posts(database, settings, tmp_path):
    store, ledger = database.store, Ledger()
    transactions = queued_transactions(store, tmp_path, settings, ledger)
    for tx in transactions:
        facts = {**tx["facts"], "original_currency": "USD"}
        decision = {
            **tx["decision"],
            "target_currency": "CNY",
            "rate_snapshot": {"dataSource": "synthetic old quote", "adoptedRate": "7"},
        }
        payload = {**decision["payload"], "sourceAmount": 7000}
        decision["payload"] = payload
        store.execute(
            "UPDATE transactions SET facts=%s,decision=%s WHERE id=%s", (facts, decision, tx["id"])
        )
        store.execute("UPDATE jobs SET payload=%s WHERE transaction_id=%s", (payload, tx["id"]))
    write_queued(store, ledger, True)
    booked = store.all("SELECT * FROM transactions ORDER BY id")
    for tx in booked:
        store.execute(
            "INSERT INTO jobs(transaction_id,kind,version,operation_key,payload,target_id) VALUES (%s,'settle_amount',1,%s,%s,%s)",
            (
                tx["id"],
                "synthetic-settle:" + tx["id"],
                {"type": 3, "sourceAccountId": "account", "sourceAmount": 7200},
                tx["target_id"],
            ),
        )
        store.execute("UPDATE transactions SET state='queued' WHERE id=%s", (tx["id"],))
    selected = booked[1]
    write_queued(store, ledger, True, transaction_ids=frozenset({selected["id"]}))
    assert len(ledger.modify_calls) == 1 and ledger.modify_calls[0]["id"] == selected["target_id"]
    for tx in booked:
        assert ledger.records[tx["target_id"]]["sourceAmount"] == (
            7200 if tx["id"] == selected["id"] else 7000
        )


def test_readonly_unknown_confirmation_outside_scope_does_not_dispatch_queued_writes(
    database, settings, tmp_path
):
    store, ledger = database.store, Ledger()
    transactions = queued_transactions(store, tmp_path, settings, ledger)
    unknown = transactions[0]
    payload = unknown["decision"]["payload"]
    ledger.records["previously-created"] = {"id": "previously-created", **payload}
    store.execute(
        "UPDATE jobs SET status='unknown',target_id='previously-created' WHERE transaction_id=%s",
        (unknown["id"],),
    )
    store.execute("UPDATE transactions SET state='unknown' WHERE id=%s", (unknown["id"],))
    write_queued(store, ledger, True, transaction_ids=frozenset())
    assert (
        store.one("SELECT state FROM transactions WHERE id=%s", (unknown["id"],))["state"]
        == "booked"
    )
    assert ledger.create_calls == ledger.modify_calls == []
    assert store.one("SELECT count(*) AS n FROM jobs WHERE status='queued'")["n"] == 2
