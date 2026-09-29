"""远端备注标记不能替代本地已确认的交易关联。"""

import pytest

import test_pipeline as pipeline
from test_pipeline import Ledger, import_daily
from ezbookkeeping_importer.application.classify import classify_pending, decide
from ezbookkeeping_importer.application.write import write_queued


database = pipeline.database
settings = pipeline.settings


def remote_record(transaction, settings, ledger, *, marker, identifier="existing"):
    payload = decide(transaction, settings, ledger, None)["payload"]
    return {
        **payload,
        "id": identifier,
        "comment": f"{marker} {transaction['merchant_name']}",
    }


@pytest.mark.parametrize(
    "marker",
    ["ebki-0123456789abcdef", "ebki-550e8400-e29b-41d4-a716-446655440000", ""],
    ids=["unknown-current-marker", "unknown-old-marker", "no-marker"],
)
def test_unlinked_duplicate_blocks_creation(database, tmp_path, settings, marker):
    store, ledger = database.store, Ledger()
    import_daily(store, tmp_path, settings)
    transaction = store.one("SELECT * FROM bank_transactions")
    ledger.records["existing"] = remote_record(transaction, settings, ledger, marker=marker)

    classify_pending(store, settings, ledger, None)
    write_queued(store, ledger)

    current = store.one("SELECT * FROM bank_transactions")
    assert current["import_status"] == "issue"
    assert current["import_error"]["code"] == "duplicate_candidates"
    assert current["import_error"]["detail"]["candidate_ids"] == ["existing"]
    assert current["ledger_transaction_id"] is None
    assert store.all("SELECT * FROM background_task") == []
    assert ledger.create_calls == []


@pytest.mark.parametrize("remove_marker", [False, True])
def test_confirmed_other_source_allows_real_equal_value_transaction(
    database, tmp_path, settings, remove_marker
):
    store, ledger = database.store, Ledger()
    import_daily(store, tmp_path, settings, count=2)
    first, second = store.all("SELECT * FROM bank_transactions ORDER BY id")
    classify_pending(store, settings, ledger, None, transaction_ids=frozenset({first["id"]}))
    write_queued(store, ledger, transaction_ids=frozenset({first["id"]}))
    booked = store.one("SELECT * FROM bank_transactions WHERE id=%s", (first["id"],))
    assert booked["import_status"] == "booked"
    if remove_marker:
        ledger.records[booked["ledger_transaction_id"]]["comment"] = first["merchant_name"]

    classify_pending(store, settings, ledger, None, transaction_ids=frozenset({second["id"]}))
    write_queued(store, ledger)

    assert len(ledger.create_calls) == 2
    assert len(ledger.records) == 2
    assert all(
        row["import_status"] == "booked" for row in store.all("SELECT * FROM bank_transactions")
    )


def test_only_confirmed_candidate_is_excluded(database, tmp_path, settings):
    store, ledger = database.store, Ledger()
    import_daily(store, tmp_path, settings, count=2)
    first, second = store.all("SELECT * FROM bank_transactions ORDER BY id")
    classify_pending(store, settings, ledger, None, transaction_ids=frozenset({first["id"]}))
    write_queued(store, ledger, transaction_ids=frozenset({first["id"]}))
    ledger.records["unknown"] = remote_record(
        second, settings, ledger, marker="ebki-0123456789abcdef", identifier="unknown"
    )

    classify_pending(store, settings, ledger, None, transaction_ids=frozenset({second["id"]}))
    write_queued(store, ledger)

    blocked = store.one("SELECT * FROM bank_transactions WHERE id=%s", (second["id"],))
    assert blocked["import_error"]["detail"]["candidate_ids"] == ["unknown"]
    assert len(ledger.create_calls) == 1


def test_exact_source_marker_still_recovers_without_creation(database, tmp_path, settings):
    store, ledger = database.store, Ledger()
    import_daily(store, tmp_path, settings)
    transaction = store.one("SELECT * FROM bank_transactions")
    ledger.records["existing"] = remote_record(
        transaction, settings, ledger, marker=transaction["source_marker"]
    )

    classify_pending(store, settings, ledger, None)
    assert store.one("SELECT status FROM background_task")["status"] == "unknown"
    write_queued(store, ledger)

    current = store.one("SELECT * FROM bank_transactions")
    assert current["import_status"] == "booked"
    assert current["ledger_transaction_id"] == "existing"
    assert (
        store.one("SELECT completion_method FROM background_task")["completion_method"]
        == "existing_link"
    )
    assert store.all("SELECT * FROM ledger_write_attempt") == []
    assert ledger.create_calls == []


@pytest.mark.parametrize(
    "difference",
    [
        {"sourceAccountId": "different-account"},
        {"sourceAmount": 1001},
        {"type": 4},
        {"time": 0},
        {"comment": "ebki-0123456789abcdef different merchant"},
    ],
    ids=["account", "amount", "type", "day", "merchant"],
)
def test_marker_does_not_replace_business_duplicate_conditions(
    database, tmp_path, settings, difference
):
    store, ledger = database.store, Ledger()
    import_daily(store, tmp_path, settings)
    transaction = store.one("SELECT * FROM bank_transactions")
    ledger.records["existing"] = {
        **remote_record(transaction, settings, ledger, marker="ebki-0123456789abcdef"),
        **difference,
    }

    classify_pending(store, settings, ledger, None)

    assert store.one("SELECT import_status FROM bank_transactions")["import_status"] == "queued"
    assert store.one("SELECT status FROM background_task")["status"] == "queued"


def test_other_local_marker_without_confirmed_link_is_still_duplicate(database, tmp_path, settings):
    store, ledger = database.store, Ledger()
    import_daily(store, tmp_path, settings, count=2)
    first, second = store.all("SELECT * FROM bank_transactions ORDER BY id")
    ledger.records["existing"] = remote_record(
        second, settings, ledger, marker=first["source_marker"]
    )

    classify_pending(store, settings, ledger, None, transaction_ids=frozenset({second["id"]}))

    blocked = store.one("SELECT * FROM bank_transactions WHERE id=%s", (second["id"],))
    assert blocked["import_error"]["code"] == "duplicate_candidates"
    assert blocked["import_error"]["detail"]["candidate_ids"] == ["existing"]
    assert store.all("SELECT * FROM background_task") == []


def test_unverified_task_target_does_not_exclude_duplicate(database, tmp_path, settings):
    store, ledger = database.store, Ledger()
    import_daily(store, tmp_path, settings, count=2)
    first, second = store.all("SELECT * FROM bank_transactions ORDER BY id")
    classify_pending(store, settings, ledger, None, transaction_ids=frozenset({first["id"]}))
    ledger.records["existing"] = remote_record(
        first, settings, ledger, marker=first["source_marker"]
    )
    with store.transaction():
        store.execute(
            "UPDATE background_task SET status='unknown',ledger_transaction_id='existing' WHERE bank_transaction_id=%s",
            (first["id"],),
        )
        store.execute(
            "UPDATE bank_transactions SET import_status='unknown' WHERE id=%s", (first["id"],)
        )

    classify_pending(store, settings, ledger, None, transaction_ids=frozenset({second["id"]}))

    blocked = store.one("SELECT * FROM bank_transactions WHERE id=%s", (second["id"],))
    assert blocked["import_error"]["code"] == "duplicate_candidates"
    assert blocked["import_error"]["detail"]["candidate_ids"] == ["existing"]
    assert store.one("SELECT status FROM background_task")["status"] == "unknown"
    assert ledger.create_calls == []
