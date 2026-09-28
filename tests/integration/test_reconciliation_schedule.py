from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

import test_pipeline as pipeline
from test_pipeline import queue, queue_legacy_estimate, statement, Ledger

from ezbookkeeping_importer.application.reconcile import CHECKPOINT_KEY, reconcile_if_due
from ezbookkeeping_importer.application.write import write_queued
from ezbookkeeping_importer.entrypoints import worker
from ezbookkeeping_importer.domain.errors import ImporterError

database = pipeline.database
settings = pipeline.settings


@pytest.fixture
def booked_statement(database, settings, tmp_path):
    store, ledger = database.store, Ledger()
    transaction = queue(store, tmp_path, settings, ledger)
    write_queued(store, ledger, True)
    statement(store, transaction, "10.00")
    return store, ledger, transaction


def test_idle_cycles_skip_remote_reads_and_report_rewrites_and_survive_restart(
    database, booked_statement, tmp_path, monkeypatch
):
    store, ledger, _ = booked_statement
    get = Mock(wraps=ledger.get)
    monkeypatch.setattr(ledger, "get", get)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    report_dir = tmp_path / "reports"
    assert reconcile_if_due(store, ledger, report_dir, now)
    first_calls = get.call_count
    assert first_calls == 1
    report = next(report_dir.glob("*.json"))
    first_content, first_mtime = report.read_bytes(), report.stat().st_mtime_ns
    assert not reconcile_if_due(store, ledger, report_dir, now + timedelta(seconds=30))
    store.close()
    restarted = database.connect()
    assert not reconcile_if_due(restarted, ledger, report_dir, now + timedelta(minutes=5))
    assert get.call_count == first_calls
    assert (report.read_bytes(), report.stat().st_mtime_ns) == (first_content, first_mtime)
    assert reconcile_if_due(restarted, ledger, report_dir, now + timedelta(hours=1))
    assert get.call_count == 2


@pytest.mark.parametrize(
    "event", ["mail_parsed", "write_confirmed", "issue_resolved", "classification_decided"]
)
def test_business_changes_trigger_reconciliation_before_history_deadline(
    booked_statement, tmp_path, event
):
    store, ledger, transaction = booked_statement
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert reconcile_if_due(store, ledger, tmp_path / "reports", now)
    store.audit(event, transaction["id"], {})
    assert reconcile_if_due(store, ledger, tmp_path / "reports", now + timedelta(seconds=30))


def test_new_monthly_report_triggers_even_without_parse_audit(database, settings, tmp_path):
    store, ledger = database.store, Ledger()
    transaction = queue(store, tmp_path, settings, ledger)
    write_queued(store, ledger, True)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert reconcile_if_due(store, ledger, tmp_path / "reports", now)
    statement(store, transaction, "10.00")
    assert reconcile_if_due(store, ledger, tmp_path / "reports", now + timedelta(seconds=30))


def test_periodic_check_discovers_remote_changes_without_local_changes(booked_statement, tmp_path):
    store, ledger, _ = booked_statement
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    target = store.one("SELECT target_id FROM transactions")["target_id"]
    reconcile_if_due(store, ledger, tmp_path / "reports", now)
    ledger.records[target]["sourceAmount"] += 100
    assert not reconcile_if_due(store, ledger, tmp_path / "reports", now + timedelta(minutes=59))
    assert reconcile_if_due(store, ledger, tmp_path / "reports", now + timedelta(hours=1))
    assert store.one("SELECT status FROM reconciliation_items")["status"] == "target_changed"
    del ledger.records[target]
    assert reconcile_if_due(store, ledger, tmp_path / "reports", now + timedelta(hours=2))
    assert store.one("SELECT status FROM reconciliation_items")["status"] == "target_missing"


def test_query_failure_gets_shorter_retry_deadline(booked_statement, tmp_path, monkeypatch):
    store, ledger, _ = booked_statement
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    get = ledger.get
    monkeypatch.setattr(ledger, "get", Mock(side_effect=TimeoutError("synthetic")))
    assert reconcile_if_due(store, ledger, tmp_path / "reports", now)
    assert store.one("SELECT status FROM reconciliation_items")["status"] == "query_failed"
    monkeypatch.setattr(ledger, "get", get)
    assert not reconcile_if_due(store, ledger, tmp_path / "reports", now + timedelta(minutes=9))
    assert reconcile_if_due(store, ledger, tmp_path / "reports", now + timedelta(minutes=10))
    assert store.one("SELECT status FROM reconciliation_items")["status"] == "matched"


def test_report_failure_does_not_advance_checkpoint(booked_statement, tmp_path, monkeypatch):
    from ezbookkeeping_importer.application import reconcile as module

    store, ledger, _ = booked_statement
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "reconcile", Mock(side_effect=OSError("synthetic disk failure")))
    with pytest.raises(OSError):
        reconcile_if_due(store, ledger, tmp_path / "reports", now)
    assert store.one("SELECT * FROM jobs WHERE operation_key=%s", (CHECKPOINT_KEY,)) is None


def test_disconnect_after_external_commit_exits_then_restart_verifies_without_resend(
    database, settings, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    store, ledger = database.store, Ledger()
    queue(store, tmp_path, settings, ledger)
    logger = Mock()
    runtime = SimpleNamespace(store=store, settings=settings)
    monkeypatch.setattr(worker, "configure_logging", lambda _: logger)
    monkeypatch.setattr(worker.signal, "signal", Mock())
    monkeypatch.setattr(worker.time, "sleep", lambda _: pytest.fail("must exit immediately"))
    ledger.before_create = store.close
    monkeypatch.setattr(worker, "cycle", lambda *args: write_queued(store, ledger, True))
    with pytest.raises(ImporterError, match="database connection lost"):
        worker.run(runtime)
    assert len(ledger.create_calls) == 1
    fresh = database.connect()
    assert fresh.lock_worker()  # The broken session released its advisory lock.
    from ezbookkeeping_importer.application.write import recover_dispatching

    recover_dispatching(fresh)
    ledger.before_create = None
    write_queued(fresh, ledger, True)
    assert len(ledger.create_calls) == 1
    assert fresh.one("SELECT state FROM transactions")["state"] == "booked"


def test_successful_settlement_immediately_refreshes_pending_report(database, settings, tmp_path):
    store, ledger = database.store, Ledger()
    transaction = queue_legacy_estimate(store, tmp_path, settings, ledger)
    write_queued(store, ledger, True)
    statement(store, transaction, "72.00")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert reconcile_if_due(store, ledger, tmp_path / "reports", now)
    assert (
        store.one("SELECT status FROM reconciliation_items")["status"]
        == "estimated_pending_settlement"
    )
    write_queued(store, ledger, True)
    assert reconcile_if_due(store, ledger, tmp_path / "reports", now + timedelta(seconds=30))
    assert store.one("SELECT status FROM reconciliation_items")["status"] == "matched"
    assert not reconcile_if_due(store, ledger, tmp_path / "reports", now + timedelta(seconds=60))


def test_late_committing_earlier_audit_id_still_triggers_check(
    database, booked_statement, tmp_path
):
    store, ledger, transaction = booked_statement
    peer = database.connect()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with peer.transaction():
        peer.audit("issue_resolved", transaction["id"], {})
        store.audit("write_confirmed", transaction["id"], {})
        assert reconcile_if_due(store, ledger, tmp_path / "reports", now)
    assert reconcile_if_due(store, ledger, tmp_path / "reports", now + timedelta(seconds=30))
