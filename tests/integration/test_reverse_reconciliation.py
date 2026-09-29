"""核对独立状态、完整集合发布和过期计算隔离。"""

from copy import deepcopy
from unittest.mock import Mock
import pytest
import test_pipeline as pipeline
from ezbookkeeping_importer.application.reconcile import reconcile
from ezbookkeeping_importer.application.write import write_queued
from ezbookkeeping_importer.application.maintenance import issues

database = pipeline.database
settings = pipeline.settings


def booked(database, tmp_path, settings):
    store, ledger = database.store, pipeline.Ledger()
    tx = pipeline.queue(store, tmp_path, settings, ledger)
    write_queued(store, ledger)
    pipeline.statement(store, tx, "10.00")
    return store, ledger, tx


def results(store):
    return store.all("SELECT * FROM bank_statement_reconciliation ORDER BY check_direction,id")


def test_matched_bank_evidence_and_query_failure_coexist_without_old_observation(
    database, tmp_path, settings, monkeypatch
):
    store, ledger, tx = booked(database, tmp_path, settings)
    reconcile(store, ledger, tmp_path)
    before = results(store)
    assert any(r["ledger_observed_at"] for r in before)
    monkeypatch.setattr(ledger, "get", Mock(side_effect=TimeoutError("synthetic query failure")))
    reconcile(store, ledger, tmp_path)
    row = store.one(
        "SELECT * FROM bank_statement_reconciliation WHERE check_direction='statement_to_transaction'"
    )
    assert row["match_status"] == "matched" and row["ledger_check_status"] == "query_failed"
    assert row["actual_amount"] is None and row["actual_currency"] is None
    assert row["ledger_observed_at"] is None and row["last_error"]
    assert row["checked_at"] >= before[0]["checked_at"]


def test_reverse_missing_result_and_current_problem_disappear_when_matched(
    database, tmp_path, settings
):
    store, ledger, tx = booked(database, tmp_path, settings)
    original = store.one("SELECT content FROM bank_report WHERE report_type='monthly'")["content"]
    store.execute(
        "UPDATE bank_report SET content=%s WHERE report_type='monthly'", ({**original, "rows": []},)
    )
    reconcile(store, ledger, tmp_path)
    missing = store.one(
        "SELECT * FROM bank_statement_reconciliation WHERE check_direction='transaction_to_statement'"
    )
    assert missing["match_status"] in {"missing_statement_evidence", "awaiting_statement"}
    store.execute("UPDATE bank_report SET content=%s WHERE report_type='monthly'", (original,))
    reconcile(store, ledger, tmp_path)
    assert not store.one(
        "SELECT * FROM bank_statement_reconciliation WHERE match_status='missing_statement_evidence'"
    )
    assert not [i for i in issues(store) if i["code"] == "missing_statement_evidence"]


def test_half_round_process_interruption_does_not_replace_previous_complete_set(
    database, tmp_path, settings, monkeypatch
):
    store, ledger, tx = booked(database, tmp_path, settings)
    reconcile(store, ledger, tmp_path)
    before = results(store)
    report = store.one("SELECT reconciliation_version FROM bank_report WHERE report_type='monthly'")
    monkeypatch.setattr(
        ledger, "get", Mock(side_effect=KeyboardInterrupt("synthetic process death"))
    )
    with pytest.raises(KeyboardInterrupt):
        reconcile(store, ledger, tmp_path)
    assert results(store) == before
    assert (
        store.one("SELECT reconciliation_version FROM bank_report WHERE report_type='monthly'")
        == report
    )


def test_concurrent_input_mutation_rejects_stale_publication(
    database, tmp_path, settings, monkeypatch
):
    store, ledger, tx = booked(database, tmp_path, settings)
    reconcile(store, ledger, tmp_path)
    before = results(store)
    peer = database.connect()
    get = ledger.get
    changed = False

    def change(target):
        nonlocal changed
        if not changed:
            changed = True
            with peer.transaction():
                peer.execute(
                    "UPDATE bank_transactions SET decision_version=decision_version+1 WHERE id=%s",
                    (tx["id"],),
                )
        return get(target)

    monkeypatch.setattr(ledger, "get", change)
    reconcile(store, ledger, tmp_path)
    assert results(store) == before


def test_concurrent_new_candidate_is_included_in_input_validation(
    database, tmp_path, settings, monkeypatch
):
    store, ledger, tx = booked(database, tmp_path, settings)
    reconcile(store, ledger, tmp_path)
    before = results(store)
    peer = database.connect()
    get = ledger.get
    changed = False

    def insert(target):
        nonlocal changed
        if not changed:
            changed = True
            with peer.transaction():
                peer.execute(
                    """INSERT INTO bank_transactions(id,report_key,report_row_key,event_type,
                    occurred_date,occurred_at,time_precision,merchant_name,card_reference,original_amount,original_currency)
                    SELECT 'AAAAAAAAAAAAAAAA',report_key,'new-candidate',event_type,occurred_date,occurred_at,
                    time_precision,merchant_name,card_reference,original_amount,original_currency
                    FROM bank_transactions WHERE id=%s""",
                    (tx["id"],),
                )
        return get(target)

    monkeypatch.setattr(ledger, "get", insert)
    reconcile(store, ledger, tmp_path)
    assert results(store) == before


def test_older_round_cannot_overwrite_newer_published_version(
    database, tmp_path, settings, monkeypatch
):
    store, ledger, tx = booked(database, tmp_path, settings)
    reconcile(store, ledger, tmp_path)
    peer = database.connect()
    get = ledger.get
    published = []

    def publish_newer(target):
        if not published:
            published.append(True)
            reconcile(peer, ledger, tmp_path / "newer")
            published.append(deepcopy(results(peer)))
        return get(target)

    monkeypatch.setattr(ledger, "get", publish_newer)
    reconcile(store, ledger, tmp_path)
    assert results(store) == published[1]


def test_nonexistent_json_row_cannot_be_published(database, tmp_path, settings, monkeypatch):
    from ezbookkeeping_importer.application import reconcile as module

    store, ledger, tx = booked(database, tmp_path, settings)
    reconcile(store, ledger, tmp_path)
    before = results(store)
    compute = module.compute_results

    def invalid(*args):
        items, intents, supplements = compute(*args)
        items[0]["statement_row_key"] = "not-in-report-content"
        return items, intents, supplements

    monkeypatch.setattr(module, "compute_results", invalid)
    reconcile(store, ledger, tmp_path)
    assert results(store) == before
    assert store.one(
        "SELECT reconciliation_last_error FROM bank_report WHERE report_type='monthly'"
    )["reconciliation_last_error"]


def test_ambiguous_candidates_have_no_arbitrary_transaction_link(database, tmp_path, settings):
    store, ledger = database.store, pipeline.Ledger()
    pipeline.import_daily(store, tmp_path, settings, count=2)
    tx = store.one("SELECT * FROM bank_transactions")
    pipeline.statement(store, tx, "10.00")
    reconcile(store, ledger, tmp_path)
    row = store.one(
        "SELECT * FROM bank_statement_reconciliation WHERE check_direction='statement_to_transaction'"
    )
    assert row["match_status"] == "ambiguous"
    assert row["bank_transaction_id"] is None
    assert len(row["details"]["candidate_ids"]) == 2


def test_cross_currency_observation_does_not_compare_numeric_amounts(database, tmp_path, settings):
    store, ledger = database.store, pipeline.Ledger()
    tx = pipeline.queue(store, tmp_path, settings, ledger, currency="USD")
    write_queued(store, ledger)
    pipeline.statement(store, tx, "72.00")
    reconcile(store, ledger, tmp_path)
    row = store.one(
        "SELECT * FROM bank_statement_reconciliation WHERE check_direction='statement_to_transaction'"
    )
    assert row["match_status"] == "matched"
    assert row["expected_currency"] == "CNY" and row["expected_amount"] == 72
    assert row["actual_currency"] == "USD" and row["actual_amount"] == 10
    assert row["ledger_check_status"] == "mismatched"
    assert "amount" not in row["details"].get("differences", {})
