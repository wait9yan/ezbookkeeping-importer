"""调度水位属于月账单，输入变化无需审计计数。"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
import pytest
import test_pipeline as pipeline
from ezbookkeeping_importer.application.reconcile import reconcile_if_due
from ezbookkeeping_importer.application.write import write_queued, recover_dispatching
from ezbookkeeping_importer.entrypoints import worker
from ezbookkeeping_importer.domain.errors import ImporterError

database = pipeline.database
settings = pipeline.settings


@pytest.fixture
def booked_statement(database, settings, tmp_path):
    store, ledger = database.store, pipeline.Ledger()
    tx = pipeline.queue(store, tmp_path, settings, ledger)
    write_queued(store, ledger)
    pipeline.statement(store, tx, "10.00")
    return store, ledger, tx


def test_idle_cycles_preserve_output_and_skip_remote_reads_after_restart(
    database, booked_statement, tmp_path, monkeypatch
):
    store, ledger, _ = booked_statement
    get = Mock(wraps=ledger.get)
    monkeypatch.setattr(ledger, "get", get)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert reconcile_if_due(store, ledger, tmp_path, now)
    calls = get.call_count
    assert calls > 0
    first = store.one("SELECT * FROM bank_report WHERE report_type='monthly'")
    assert not reconcile_if_due(store, ledger, tmp_path, now + timedelta(seconds=30))
    store.close()
    fresh = database.connect()
    assert not reconcile_if_due(fresh, ledger, tmp_path, now + timedelta(minutes=5))
    assert get.call_count == calls
    assert fresh.one("SELECT * FROM bank_report WHERE report_type='monthly'") == first
    assert reconcile_if_due(fresh, ledger, tmp_path, now + timedelta(hours=1))
    assert get.call_count > calls


def test_transaction_input_change_triggers_before_deadline(booked_statement, tmp_path):
    store, ledger, tx = booked_statement
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    reconcile_if_due(store, ledger, tmp_path, now)
    store.execute(
        "UPDATE bank_transactions SET decision_version=decision_version+1 WHERE id=%s", (tx["id"],)
    )
    assert reconcile_if_due(store, ledger, tmp_path, now + timedelta(seconds=30))


def test_query_failure_has_ten_minute_retry_not_thirty_second_loop(
    booked_statement, tmp_path, monkeypatch
):
    store, ledger, _ = booked_statement
    get = ledger.get
    monkeypatch.setattr(ledger, "get", Mock(side_effect=TimeoutError("synthetic")))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert reconcile_if_due(store, ledger, tmp_path, now)
    report = store.one("SELECT * FROM bank_report WHERE report_type='monthly'")
    assert report["reconciliation_queries_succeeded"] is False
    monkeypatch.setattr(ledger, "get", get)
    assert not reconcile_if_due(store, ledger, tmp_path, now + timedelta(minutes=9))
    assert reconcile_if_due(store, ledger, tmp_path, now + timedelta(minutes=10))
    assert (
        store.one(
            "SELECT reconciliation_queries_succeeded FROM bank_report WHERE report_type='monthly'"
        )["reconciliation_queries_succeeded"]
        is True
    )


def test_disconnect_after_external_commit_exits_and_restart_verifies(
    database, settings, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    store, ledger = database.store, pipeline.Ledger()
    pipeline.queue(store, tmp_path, settings, ledger)
    logger = Mock()
    runtime = SimpleNamespace(store=store, settings=settings)
    monkeypatch.setattr(worker, "configure_logging", lambda _: logger)
    monkeypatch.setattr(worker.signal, "signal", Mock())
    monkeypatch.setattr(worker.time, "sleep", lambda _: pytest.fail("must exit immediately"))
    ledger.before_create = store.close
    monkeypatch.setattr(worker, "cycle", lambda *args: write_queued(store, ledger))
    with pytest.raises(ImporterError, match="database connection lost"):
        worker.run(runtime)
    fresh = database.connect()
    assert fresh.lock_worker()
    recover_dispatching(fresh)
    ledger.before_create = None
    write_queued(fresh, ledger)
    assert len(ledger.create_calls) == 1
    assert fresh.one("SELECT import_status FROM bank_transactions")["import_status"] == "booked"
