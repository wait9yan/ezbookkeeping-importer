from unittest.mock import Mock

import pytest

import test_pipeline as pipeline
from ezbookkeeping_importer.application.reconcile import reconcile
from ezbookkeeping_importer.application.write import write_queued

database = pipeline.database
settings = pipeline.settings


@pytest.mark.parametrize("recovered_status", ["awaiting_statement", "import_pending", "target_missing"])
def test_reverse_query_recovery_closes_only_its_resolved_issue(
    database, settings, tmp_path, monkeypatch, recovered_status
):
    store, ledger = database.store, pipeline.Ledger()
    transaction = pipeline.queue(store, tmp_path, settings, ledger)
    write_queued(store, ledger, True)
    pipeline.statement(store, transaction, "10.00")
    store.execute(
        "UPDATE reports SET parsed=%s WHERE report_key='monthly-report'",
        ({"rows": [], "metadata": {"cycle_start": "2025-12-01", "cycle_end": "2026-01-01"}},),
    )
    issue_key = "monthly-report:daily:" + transaction["id"]
    get = ledger.get
    monkeypatch.setattr(ledger, "get", Mock(side_effect=TimeoutError("synthetic failure")))
    reconcile(store, ledger, tmp_path / "reports")
    issue = store.one(
        "SELECT * FROM issues WHERE code='reconciliation' AND entity_id=%s", (issue_key,)
    )
    assert issue["data"]["status"] == "query_failed" and not issue["resolved"]
    store.issue("reconciliation", "another-report:daily:" + transaction["id"], {})
    store.issue("unrelated", issue_key, {})

    monkeypatch.setattr(ledger, "get", get)
    if recovered_status == "import_pending":
        store.execute(
            "UPDATE transactions SET target_id=NULL,state='pending' WHERE id=%s",
            (transaction["id"],),
        )
    elif recovered_status == "target_missing":
        ledger.records.clear()
    reconcile(store, ledger, tmp_path / "reports")
    assert store.one("SELECT status FROM reconciliation_items")["status"] == recovered_status
    updated = store.one("SELECT * FROM issues WHERE id=%s", (issue["id"],))
    assert updated["resolved"] is (recovered_status != "target_missing")
    assert all(
        not row["resolved"]
        for row in store.all(
            "SELECT resolved FROM issues WHERE entity_id=%s OR (code='unrelated' AND entity_id=%s)",
            ("another-report:daily:" + transaction["id"], issue_key),
        )
    )
    assert len(ledger.create_calls) == 1 and ledger.modify_calls == []

    if recovered_status == "awaiting_statement":
        monkeypatch.setattr(ledger, "get", Mock(side_effect=TimeoutError("failure recurred")))
        reconcile(store, ledger, tmp_path / "reports")
        reopened = store.one("SELECT * FROM issues WHERE id=%s", (issue["id"],))
        assert not reopened["resolved"] and reopened["data"]["status"] == "query_failed"
