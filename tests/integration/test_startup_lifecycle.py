"""统一启动只管理自己的 worker；停止边界保留已经提交的真实数据库状态。"""

from contextlib import contextmanager
import json
import logging
import multiprocessing
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import test_event_logging as logging_tests
import test_pipeline as pipeline
from ezbookkeeping_importer.adapters.banks.cmb import BankParser
from ezbookkeeping_importer.adapters.evidence_store import EvidenceStore
from ezbookkeeping_importer.adapters.persistence.postgres import PostgresStore
from ezbookkeeping_importer.application import service
from ezbookkeeping_importer.application.classify import classify_pending
from ezbookkeeping_importer.application.collect import request_sync
from ezbookkeeping_importer.application.write import write_queued
from ezbookkeeping_importer.config import MailSettings, Settings
from ezbookkeeping_importer.domain.errors import ImporterError

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


def test_stop_during_collection_finishes_sync_without_starting_parse(
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
    assert task["status"] == "done" and task["error_code"] is None
    assert store.one("SELECT parse_status FROM email WHERE id=%s", (identifier,)) == {
        "parse_status": "pending"
    }
    assert store.all("SELECT * FROM bank_transactions") == []
    assert event_log.one("sync_completed")["task_id"] == task["id"]
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


def test_stop_during_classification_finishes_batch_but_sends_no_writes(
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
    assert len(tasks) == 2
    assert all(task["task_type"] == "create" and task["status"] == "queued" for task in tasks)
    assert store.all("SELECT * FROM ledger_write_attempt") == []
    assert ledger.create_calls == []
    assert event_log.one("classification_completed")["counts"]["queued"] == 2
    assert event_log.named("write_attempt_registered") == []
    assert event_log.named("sync_completed") == []
    assert_no_failure(event_log)


def test_stop_during_write_finishes_current_batch_without_reconciliation(
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
    assert len(ledger.create_calls) == 2
    assert store.all("SELECT outcome FROM ledger_write_attempt") == [
        {"outcome": "confirmed"},
        {"outcome": "confirmed"},
    ]
    assert all(row["status"] == "done" for row in store.all("SELECT status FROM background_task"))
    assert len(event_log.named("write_verified")) == 2
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


class DatabaseRuntime:
    """子进程仅使用真实 PG，不装配网络客户端或读取项目配置。"""

    def __init__(self, settings, **kwargs):
        self.settings = settings
        self.store = PostgresStore(settings.database_url.get_secret_value())

    def close(self):
        self.store.close()


def controlled_worker(config_path, stop_event, status_connection):
    """保留真实托管入口/worker，只替换外部能力与可控周期耗时。"""
    from ezbookkeeping_importer.entrypoints import run as startup, worker

    payload = json.loads(Path(config_path).read_text())
    settings = Settings(
        database_url=payload["database_url"],
        log_dir=Path(payload["log_dir"]),
        log_level=payload["log_level"],
        mail=MailSettings(source_id="synthetic-startup"),
        timezone="Asia/Shanghai",
    )
    startup.load_settings = lambda *args, **kwargs: settings
    startup.Runtime = DatabaseRuntime

    def waiting_cycle(runtime, logger, *, should_stop):
        while not should_stop():
            time.sleep(0.01)
        return None

    def failed_recovery(store):
        raise ImporterError("synthetic recovery failure")

    worker.cycle = waiting_cycle
    if payload["failure"] == "recovery":
        worker.recover_dispatching = failed_recovery
    startup.worker_process(config_path, stop_event, status_connection)


@contextmanager
def managed_worker(database, tmp_path, *, failure=None, log_level="INFO"):
    config_path = tmp_path / "synthetic-worker.json"
    log_dir = tmp_path / "logs"
    if failure == "logging":
        log_dir.write_text("synthetic non-directory")
    config_path.write_text(
        json.dumps(
            {
                "database_url": database.store.connection.info.dsn,
                "log_dir": str(log_dir),
                "log_level": log_level,
                "failure": failure,
            }
        )
    )
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    stop = context.Event()
    process = context.Process(target=controlled_worker, args=(config_path, stop, sender))
    process.start()
    sender.close()
    try:
        yield SimpleNamespace(process=process, stop=stop, receiver=receiver, log_dir=log_dir)
    finally:
        stop.set()
        process.join(timeout=5)
        receiver.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
            pytest.fail("synthetic worker did not stop at a phase boundary")
        process.close()


def receive_status(child):
    assert child.receiver.poll(5), "worker produced no readiness or failure diagnostic"
    return child.receiver.recv()


def assert_lock_released(store):
    deadline = time.monotonic() + 1
    while not store.lock_worker():
        assert time.monotonic() < deadline, "worker lock remained held after process exit"
        time.sleep(0.01)


@pytest.mark.parametrize("log_level", ["INFO", "ERROR"])
def test_managed_worker_ready_and_normal_stop_release_database_lock(database, tmp_path, log_level):
    observer = database.connect()
    with managed_worker(database, tmp_path, log_level=log_level) as child:
        assert receive_status(child) == ("ready", None)
        assert child.process.is_alive()
        assert not observer.lock_worker()
        task = observer.one("SELECT * FROM background_task WHERE task_type='sync'")
        assert task["status"] == "queued" and task["error_code"] is None

        child.stop.set()
        child.process.join(timeout=5)

        assert child.process.exitcode == 0
        assert_lock_released(observer)
        assert observer.one("SELECT * FROM background_task WHERE task_type='sync'") == task
        records = [
            json.loads(line) for line in (child.log_dir / "worker.jsonl").read_text().splitlines()
        ]
        names = [record["event"] for record in records]
        if log_level == "INFO":
            assert names.count("worker_started") == names.count("worker_stopped") == 1
        else:
            assert records == []
        assert not set(names) & {"sync_failed", "sync_completed", "worker_failed"}


@pytest.mark.parametrize("failure", ["logging", "recovery"])
def test_startup_failure_reports_error_and_releases_database_lock(database, tmp_path, failure):
    with managed_worker(database, tmp_path, failure=failure) as child:
        kind, detail = receive_status(child)
        assert kind == "error" and detail["message"]
        assert detail["error_type"] == (
            "FileExistsError" if failure == "logging" else "ImporterError"
        )
        child.process.join(timeout=5)
        assert child.process.exitcode == 1
        assert_lock_released(database.connect())
        assert database.store.all("SELECT * FROM background_task") == []
        if failure == "recovery":
            names = [
                json.loads(line)["event"]
                for line in (child.log_dir / "worker.jsonl").read_text().splitlines()
            ]
            assert "worker_started" not in names and "worker_stopped" not in names


def test_managed_worker_conflict_does_not_stop_or_reconfigure_existing_owner(database, tmp_path):
    existing = database.store
    assert existing.lock_worker()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    original = b'{"event":"synthetic_existing_worker"}\n'
    log_path = log_dir / "worker.jsonl"
    log_path.write_bytes(original)
    with managed_worker(database, tmp_path) as child:
        kind, detail = receive_status(child)
        assert kind == "error" and detail["error_type"] == "Conflict"
        assert "another worker" in detail["message"]
        child.process.join(timeout=5)
        assert child.process.exitcode == 1
        observer = database.connect()
        assert not observer.lock_worker()
        assert existing.one("SELECT 1 AS alive") == {"alive": 1}
        assert existing.all("SELECT * FROM background_task") == []
        assert log_path.read_bytes() == original
        existing.close()
        assert_lock_released(observer)
