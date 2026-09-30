"""统一启动只管理自己的 worker；停止边界保留已经提交的真实数据库状态。"""

import logging
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import test_event_logging as logging_tests
import test_pipeline as pipeline
from ezbookkeeping_importer.adapters.banks.cmb import BankParser
from ezbookkeeping_importer.adapters.evidence_store import EvidenceStore
from ezbookkeeping_importer.application import service
from ezbookkeeping_importer.application.classify import classify_pending
from ezbookkeeping_importer.application.collect import request_sync
from ezbookkeeping_importer.application.write import write_queued

database = pipeline.database
settings = pipeline.settings
event_log = logging_tests.event_log


def runtime_for(store, settings, tmp_path, ledger=None):
    return SimpleNamespace(
        store=store,
        settings=settings.model_copy(update={"report_dir": tmp_path / "reports"}),
        parser=BankParser(context="synthetic"),
        evidence=EvidenceStore(tmp_path / "evidence"),
        ledger=ledger or pipeline.Ledger(),
        ai=None,
        mail=Mock(side_effect=AssertionError("unexpected mailbox connection")),
    )


def assert_no_failure(event_log):
    for event in ("sync_failed", "cycle_failed", "worker_failed"):
        assert event_log.named(event) == []


def test_stop_before_claim_preserves_queued_sync_and_write(database, settings, tmp_path, event_log):
    store, ledger = database.store, pipeline.Ledger()
    pipeline.queue(store, tmp_path, settings, ledger)
    request_sync(store)
    before = store.all("SELECT * FROM background_task ORDER BY id")
    event_log.clear()
    runtime = runtime_for(store, settings, tmp_path, ledger)

    result = service.cycle(runtime, logging.getLogger("ebki"), should_stop=lambda: True)

    assert result is None
    assert store.all("SELECT * FROM background_task ORDER BY id") == before
    assert store.all("SELECT * FROM ledger_write_attempt") == []
    assert ledger.create_calls == []
    assert event_log.records == []
    runtime.mail.assert_not_called()


def test_stop_during_collection_requeues_sync_without_starting_parse(
    database, settings, tmp_path, event_log
):
    store = database.store
    runtime = runtime_for(store, settings, tmp_path)
    identifier = pipeline.ingest_mail(store, runtime.evidence, pipeline.raw_daily(), settings)
    request_sync(store)
    stop = Event()
    mail = Mock()

    def folders():
        assert (
            database.connect().one("SELECT status FROM background_task WHERE task_type='sync'")[
                "status"
            ]
            == "dispatching"
        )
        stop.set()
        return []

    mail.folders.side_effect = folders
    runtime.mail = Mock(return_value=mail)
    event_log.clear()

    result = service.cycle(runtime, logging.getLogger("ebki"), should_stop=stop.is_set)

    assert result is None
    mail.close.assert_called_once()
    task = store.one("SELECT * FROM background_task WHERE task_type='sync'")
    assert task["status"] == "queued" and task["error_code"] is None
    assert store.one("SELECT parse_status FROM email WHERE id=%s", (identifier,)) == {
        "parse_status": "pending"
    }
    assert store.all("SELECT * FROM bank_transactions") == []
    assert event_log.named("sync_completed") == []
    assert event_log.named("parse_started") == []
    assert_no_failure(event_log)


def test_stop_after_parse_preserves_report_and_unclassified_transactions(
    database, settings, tmp_path, event_log
):
    store = database.store
    runtime = runtime_for(store, settings, tmp_path)
    pipeline.ingest_mail(store, runtime.evidence, pipeline.raw_daily(count=2), settings)
    stop = Event()
    event_log.clear()
    event_log.observe = lambda event: stop.set() if event["event"] == "parse_completed" else None

    result = service.cycle(runtime, logging.getLogger("ebki"), should_stop=stop.is_set)

    assert result is None
    assert store.one("SELECT count(*) AS n FROM bank_report")["n"] == 1
    rows = store.all("SELECT import_status FROM bank_transactions")
    assert len(rows) == 2 and all(row["import_status"] == "pending" for row in rows)
    assert store.all("SELECT * FROM background_task") == []
    assert event_log.one("parse_completed")["counts"]["accepted"] == 1
    assert event_log.named("classification_started") == []
    assert event_log.named("sync_completed") == []
    assert_no_failure(event_log)


def test_stop_during_classification_finishes_current_item_but_sends_no_writes(
    database, settings, tmp_path, monkeypatch, event_log
):
    store, ledger = database.store, pipeline.Ledger()
    pipeline.import_daily(store, tmp_path, settings, count=2)
    stop = Event()
    accounts = ledger.accounts

    def stop_on_accounts():
        stop.set()
        return accounts()

    monkeypatch.setattr(ledger, "accounts", stop_on_accounts)
    runtime = runtime_for(store, settings, tmp_path, ledger)
    event_log.clear()

    result = service.cycle(runtime, logging.getLogger("ebki"), should_stop=stop.is_set)

    assert result is None
    tasks = database.connect().all("SELECT * FROM background_task")
    assert len(tasks) == 1
    assert all(task["task_type"] == "create" and task["status"] == "queued" for task in tasks)
    assert store.all("SELECT * FROM ledger_write_attempt") == []
    assert ledger.create_calls == []
    assert event_log.named("classification_completed") == []
    assert store.one("SELECT count(*) AS n FROM bank_transactions WHERE import_status='pending'")["n"] == 1
    assert event_log.named("write_attempt_registered") == []
    assert event_log.named("sync_completed") == []
    assert_no_failure(event_log)


def test_stop_during_write_saves_current_result_without_next_write(
    database, settings, tmp_path, monkeypatch, event_log
):
    store, ledger = database.store, pipeline.Ledger()
    pipeline.import_daily(store, tmp_path, settings, count=2)
    classify_pending(store, settings, ledger, None)
    stop = Event()
    ledger.before_create = stop.set
    reconcile = Mock(side_effect=AssertionError("new reconciliation stage after stop"))
    monkeypatch.setattr(service, "reconcile", reconcile)
    runtime = runtime_for(store, settings, tmp_path, ledger)
    event_log.clear()

    result = service.cycle(runtime, logging.getLogger("ebki"), should_stop=stop.is_set)

    assert result is None
    assert len(ledger.create_calls) == 1
    assert store.all("SELECT outcome FROM ledger_write_attempt") == [
        {"outcome": "unknown"},
    ]
    assert sorted(row["status"] for row in store.all("SELECT status FROM background_task")) == ["queued", "unknown"]
    assert event_log.named("write_verified") == []
    reconcile.assert_not_called()
    assert_no_failure(event_log)


def test_stop_after_reconciliation_leaves_new_settlement_queued(
    database, settings, tmp_path, event_log
):
    store, ledger = database.store, pipeline.Ledger()
    pipeline.queue(store, tmp_path, settings, ledger, currency="USD")
    write_queued(store, ledger)
    transaction = store.one("SELECT * FROM bank_transactions")
    pipeline.statement(store, transaction, "72.00")
    stop = Event()
    runtime = runtime_for(store, settings, tmp_path, ledger)
    event_log.clear()
    event_log.observe = (
        lambda event: stop.set() if event["event"] == "reconciliation_published" else None
    )

    result = service.cycle(runtime, logging.getLogger("ebki"), should_stop=stop.is_set)

    assert result is None
    settlement = database.connect().one(
        "SELECT * FROM background_task WHERE task_type='settle_currency'"
    )
    assert settlement["status"] == "queued" and settlement["last_error"] is None
    assert (
        store.all("SELECT * FROM ledger_write_attempt WHERE task_id=%s", (settlement["id"],)) == []
    )
    assert ledger.modify_calls == []
    assert (
        store.one("SELECT import_decision FROM bank_transactions")["import_decision"][
            "target_currency"
        ]
        == "USD"
    )
    event_log.one("reconciliation_published")
    assert event_log.named("write_verified") == []
    assert_no_failure(event_log)



def test_stop_after_preflight_does_not_register_or_send(database, settings, tmp_path, monkeypatch):
    from ezbookkeeping_importer.application import write

    store, ledger = database.store, pipeline.Ledger()
    pipeline.queue(store, tmp_path, settings, ledger)
    stopped = Event()
    validate = write.validate_target

    def stop_after_validation(*args):
        validate(*args)
        stopped.set()

    monkeypatch.setattr(write, "validate_target", stop_after_validation)
    write.write_queued(store, ledger, should_stop=stopped.is_set)

    assert store.one("SELECT status FROM background_task")["status"] == "queued"
    assert store.all("SELECT * FROM ledger_write_attempt") == []
    assert ledger.create_calls == []


def test_stop_after_registration_finishes_send_then_recovery_verifies(
    database, settings, tmp_path, event_log
):
    from ezbookkeeping_importer.application import write

    store, ledger = database.store, pipeline.Ledger()
    pipeline.queue(store, tmp_path, settings, ledger)
    stopped = Event()
    event_log.observe = (
        lambda event: stopped.set() if event["event"] == "write_attempt_registered" else None
    )
    write.write_queued(store, ledger, should_stop=stopped.is_set)

    assert len(ledger.create_calls) == 1
    assert store.one("SELECT status FROM background_task")["status"] == "unknown"
    assert store.one("SELECT response FROM ledger_write_attempt")["response"] is not None
    write.recover_dispatching(store)
    write.verify_unknown(store, ledger)
    assert store.one("SELECT status FROM background_task")["status"] == "done"
    assert len(ledger.create_calls) == 1

def test_single_process_service_releases_lock_after_signal(
    database, settings, tmp_path, monkeypatch
):
    import os
    import signal
    from ezbookkeeping_importer.entrypoints import run, worker

    store = database.connect()
    observer = database.connect()
    runtime = runtime_for(store, settings, tmp_path)
    runtime.close = Mock(side_effect=store.close)
    monkeypatch.setattr(run, "load_settings", Mock(return_value=settings))
    monkeypatch.setattr(run, "Runtime", Mock(return_value=runtime))
    monkeypatch.setattr(worker, "configure_logging", Mock())

    def stop_cycle(actual, logger, *, should_stop):
        assert actual is runtime
        assert not observer.lock_worker()
        os.kill(os.getpid(), signal.SIGTERM)
        assert should_stop()

    monkeypatch.setattr(worker, "cycle", stop_cycle)
    assert run.run_service("unused-synthetic-config") == 0
    runtime.close.assert_called_once()
    assert observer.lock_worker()


def test_single_process_lock_conflict_preserves_existing_owner(
    database, settings, tmp_path, monkeypatch
):
    import pytest
    from ezbookkeeping_importer.domain.errors import Conflict
    from ezbookkeeping_importer.entrypoints import run, worker

    assert database.store.lock_worker()
    store = database.connect()
    runtime = runtime_for(store, settings, tmp_path)
    runtime.close = Mock(side_effect=store.close)
    monkeypatch.setattr(run, "load_settings", Mock(return_value=settings))
    monkeypatch.setattr(run, "Runtime", Mock(return_value=runtime))
    configure = Mock()
    monkeypatch.setattr(worker, "configure_logging", configure)
    with pytest.raises(Conflict, match="another worker"):
        run.run_service("unused-synthetic-config")
    configure.assert_not_called()
    runtime.close.assert_called_once()
    assert not database.connect().lock_worker()
    assert database.store.one("SELECT 1 AS alive") == {"alive": 1}
