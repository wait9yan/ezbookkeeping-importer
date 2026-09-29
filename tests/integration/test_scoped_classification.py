from unittest.mock import Mock

import pytest

import test_pipeline as pipeline
from test_pipeline import Ledger, import_daily
from ezbookkeeping_importer.application.classify import classify_pending
from ezbookkeeping_importer.domain.errors import ImporterError

database = pipeline.database
settings = pipeline.settings


def ai_classifier():
    ai = Mock()
    ai.classify.return_value = {
        "classification_status": "unmatched",
        "category_id": None,
        "reason": "synthetic insufficient information",
    }
    return ai


def test_empty_scope_does_not_read_store_or_call_any_external_dependency():
    store, ledger, ai = Mock(), Mock(), Mock()
    classify_pending(store, Mock(), ledger, ai, transaction_ids=frozenset())
    assert store.mock_calls == ledger.mock_calls == ai.mock_calls == []


def test_classification_scope_ids_are_parameters_not_sql():
    store, ledger, ai = Mock(), Mock(), Mock()
    store.all.return_value = []
    identifier = "synthetic-id') OR TRUE --"
    classify_pending(store, Mock(), ledger, ai, transaction_ids=frozenset({identifier}))
    query, params = store.all.call_args.args
    assert identifier not in query and "AND id IN (%s)" in query
    assert params == (identifier,)
    assert ledger.mock_calls == ai.mock_calls == []


def test_only_selected_pending_record_is_classified_and_default_keeps_original_behavior(
    database, tmp_path, settings
):
    store, ledger, ai = database.store, Ledger(), ai_classifier()
    import_daily(store, tmp_path, settings, count=3)
    pending = store.all("SELECT * FROM bank_transactions ORDER BY id")
    chosen = pending[1]
    ai_settings = settings.model_copy(update={"classification_mode": "ai"})
    classify_pending(store, ai_settings, ledger, ai, transaction_ids=frozenset({chosen["id"]}))
    assert ai.classify.call_count == 1 and ai.classify.call_args.args[0] == chosen["id"]
    states = {
        row["id"]: row["import_status"]
        for row in store.all("SELECT id,import_status FROM bank_transactions")
    }
    assert states[chosen["id"]] == "queued"
    assert all(
        import_status == "pending"
        for identifier, import_status in states.items()
        if identifier != chosen["id"]
    )
    assert store.one("SELECT count(*) AS n FROM background_task")["n"] == 1
    classify_pending(store, ai_settings, ledger, ai)
    assert ai.classify.call_count == 3
    assert store.one("SELECT count(*) AS n FROM background_task")["n"] == 3


@pytest.mark.parametrize("scope", [frozenset(), frozenset({"not-found"})])
def test_empty_or_missing_scope_does_not_change_pending_transactions(
    database, tmp_path, settings, scope
):
    store, ledger, ai = database.store, Ledger(), ai_classifier()
    import_daily(store, tmp_path, settings, count=3)
    before = store.all("SELECT * FROM bank_transactions ORDER BY id")
    classify_pending(
        store,
        settings.model_copy(update={"classification_mode": "ai"}),
        ledger,
        ai,
        transaction_ids=scope,
    )
    assert store.all("SELECT * FROM bank_transactions ORDER BY id") == before
    assert store.one("SELECT count(*) AS n FROM background_task")["n"] == 0
    ai.classify.assert_not_called()


def test_failure_within_scope_does_not_classify_other_pending_records(database, tmp_path, settings):
    store, ledger, ai = database.store, Ledger(), ai_classifier()
    import_daily(store, tmp_path, settings, count=3)
    chosen = store.one("SELECT * FROM bank_transactions ORDER BY id")
    ai.classify.side_effect = ImporterError("synthetic classification failure")
    classify_pending(
        store,
        settings.model_copy(update={"classification_mode": "ai"}),
        ledger,
        ai,
        transaction_ids=frozenset({chosen["id"]}),
    )
    assert ai.classify.call_count == 1
    states = {
        row["id"]: row["import_status"]
        for row in store.all("SELECT id,import_status FROM bank_transactions")
    }
    assert states[chosen["id"]] == "issue"
    assert all(
        import_status == "pending"
        for identifier, import_status in states.items()
        if identifier != chosen["id"]
    )
    assert (
        store.one("SELECT count(*) AS n FROM bank_transactions WHERE import_error IS NOT NULL")["n"]
        == 1
    )


@pytest.mark.parametrize("import_status", ["unknown", "dispatching", "queued", "booked"])
def test_scope_does_not_make_nonpending_records_eligible(
    database, tmp_path, settings, import_status
):
    store, ledger, ai = database.store, Ledger(), ai_classifier()
    import_daily(store, tmp_path, settings, count=3)
    chosen = store.one("SELECT * FROM bank_transactions ORDER BY id")
    store.execute(
        "UPDATE bank_transactions SET import_status=%s WHERE id=%s", (import_status, chosen["id"])
    )
    before = store.all("SELECT * FROM bank_transactions ORDER BY id")
    classify_pending(
        store,
        settings.model_copy(update={"classification_mode": "ai"}),
        ledger,
        ai,
        transaction_ids=frozenset({chosen["id"]}),
    )
    assert store.all("SELECT * FROM bank_transactions ORDER BY id") == before
    ai.classify.assert_not_called()
