"""真实 SIGKILL 验证提交边界；子进程只连接测试夹具的随机 schema。"""

from contextlib import contextmanager
from datetime import timedelta
import importlib
import json
import logging
import multiprocessing
import signal
from types import SimpleNamespace

import pytest

import test_pipeline as pipeline
import test_recheck as recheck
from ezbookkeeping_importer.adapters.banks.cmb import BankParser
from ezbookkeeping_importer.adapters.evidence_store import EvidenceStore
from ezbookkeeping_importer.adapters.persistence.postgres import PostgresStore
from ezbookkeeping_importer.application.collect import request_sync
from ezbookkeeping_importer.application.issue_snapshot import resolve_snapshot, snapshot_issues
from ezbookkeeping_importer.application.maintenance import issues, status
from ezbookkeeping_importer.application.reconcile import reconcile, reconcile_if_due
from ezbookkeeping_importer.application.service import cycle
from ezbookkeeping_importer.application.write import recover_dispatching, write_queued
from ezbookkeeping_importer.domain.errors import Conflict

database = pipeline.database
settings = pipeline.settings
HANDSHAKE_TIMEOUT_SECONDS = 10
JOIN_TIMEOUT_SECONDS = 5


def pause_at_boundary(channel):
    # A handshake replaces timing guesses. SIGKILL bypasses context-manager cleanup.
    channel.send("boundary reached")
    channel.recv()
    raise AssertionError("the parent must kill this process, never release the barrier")


def pause_first_commit(store, channel, committed):
    original = store.transaction

    @contextmanager
    def transaction():
        with original():
            yield store
            if not committed:
                pause_at_boundary(channel)
        if committed:
            pause_at_boundary(channel)

    store.transaction = transaction


@contextmanager
def kill_at_boundary(target, *args):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=target, args=(*args, child))
    process.start()
    child.close()
    try:
        assert parent.poll(HANDSHAKE_TIMEOUT_SECONDS), "child did not reach the selected boundary"
        assert parent.recv() == "boundary reached"
        assert process.is_alive(), "child exited instead of blocking at the selected boundary"
        yield
        process.kill()
        process.join(JOIN_TIMEOUT_SECONDS)
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join(JOIN_TIMEOUT_SECONDS)
        parent.close()
        if not process.is_alive():
            process.close()


class SyntheticMail(pipeline.Mail):
    def close(self):
        self.observer.close()


def mail_runtime(store, settings, directory, *, observer):
    raw = b"From: friend@example.test\nSubject: synthetic non-bank mail\n\nsynthetic body"
    mail = SyntheticMail(observer, {"INBOX": ("v1", {1: raw, 5: raw})})
    return SimpleNamespace(
        store=store,
        settings=settings.model_copy(update={"report_dir": directory / "reports"}),
        ledger=pipeline.Ledger(),
        ai=None,
        parser=BankParser(context="synthetic"),
        evidence=EvidenceStore(directory / "evidence"),
        mail=lambda: mail,
    )


def collecting_child(dsn, settings, directory, committed, channel):
    store = PostgresStore(dsn)
    runtime = mail_runtime(store, settings, directory, observer=PostgresStore(dsn))
    pause_first_commit(store, channel, committed)
    cycle(runtime, logging.getLogger("sigkill-collection"))
    raise AssertionError("collection returned before SIGKILL")


@pytest.mark.parametrize("committed", [False, True], ids=["before-commit", "after-commit"])
def test_sigkill_keeps_uid_registration_and_checkpoint_atomic(
    database, settings, tmp_path, committed
):
    store = database.store
    assert request_sync(store)
    with kill_at_boundary(collecting_child, database.isolated_dsn, settings, tmp_path, committed):
        assert store.one("SELECT status FROM background_task")["status"] == "dispatching"
        checkpoints = store.all("SELECT * FROM email_sync_checkpoint")
        sources = store.all("SELECT * FROM email_source_item ORDER BY uid")
        if committed:
            assert checkpoints[0]["registered_uid"] == checkpoints[0]["initial_scan_upper_uid"] == 5
            assert [item["uid"] for item in sources] == [1, 5]
            assert all(item["status"] == "pending" for item in sources)
            assert not status(store)["email_sync_checkpoint"][0]["historical_complete"]
        else:
            assert checkpoints == sources == []

    restarted = database.connect()
    assert restarted.all("SELECT * FROM email_sync_checkpoint") == checkpoints
    assert restarted.all("SELECT * FROM email_source_item ORDER BY uid") == sources
    assert restarted.one("SELECT status FROM background_task")["status"] == "dispatching"
    recover_dispatching(restarted)
    assert restarted.one("SELECT status FROM background_task")["status"] == "queued"
    runtime = mail_runtime(restarted, settings, tmp_path, observer=database.connect())
    assert cycle(runtime, logging.getLogger("sigkill-recovery")) is True
    assert restarted.one("SELECT status FROM background_task")["status"] == "done"
    assert [
        row["uid"] for row in restarted.all("SELECT uid FROM email_source_item ORDER BY uid")
    ] == [1, 5]
    assert (
        restarted.one("SELECT count(*) AS n FROM email_source_item WHERE status='skipped'")["n"]
        == 2
    )
    assert status(restarted)["email_sync_checkpoint"][0]["historical_complete"]
    assert runtime.ledger.create_calls == []


def reconciling_child(dsn, ledger, directory, channel):
    module = importlib.import_module("ezbookkeeping_importer.application.reconcile")

    def export(*args):
        pause_at_boundary(channel)

    module.write_report = export
    reconcile(PostgresStore(dsn), ledger, directory)
    raise AssertionError("reconciliation returned before SIGKILL")


def test_sigkill_after_publication_retains_database_truth_and_old_export(
    database, settings, tmp_path
):
    store, ledger = database.store, pipeline.Ledger()
    transaction = pipeline.queue(store, tmp_path, settings, ledger)
    write_queued(store, ledger)
    pipeline.statement(store, transaction, "10.00")
    report_dir = tmp_path / "reports"
    assert reconcile(store, ledger, report_dir)
    report_file = next(report_dir.glob("*.json"))
    old_bytes = report_file.read_bytes()
    old_report = store.one("SELECT * FROM bank_report WHERE report_type='monthly'")
    assert json.loads(old_bytes)[0]["ledger_check_status"] == "matched"
    transaction_id = store.one("SELECT ledger_transaction_id FROM bank_transactions")[
        "ledger_transaction_id"
    ]
    ledger.records[transaction_id]["sourceAmount"] = 1200

    with kill_at_boundary(reconciling_child, database.isolated_dsn, ledger, report_dir):
        published = store.one("SELECT * FROM bank_report WHERE report_type='monthly'")
        assert published["reconciliation_version"] == old_report["reconciliation_version"] + 1
        assert published["reconciliation_queries_succeeded"]
        result = store.one("SELECT * FROM bank_statement_reconciliation")
        assert result["ledger_check_status"] == "mismatched"
        assert result["actual_amount"] == 12
        assert report_file.read_bytes() == old_bytes

    restarted = database.connect()
    assert restarted.one("SELECT * FROM bank_report WHERE report_type='monthly'") == published
    assert restarted.one("SELECT * FROM bank_statement_reconciliation") == result
    assert (
        issues(restarted, "bank_statement_reconciliation")[0]["detail"]["ledger_check_status"]
        == "mismatched"
    )
    # Restart does not promise immediate repair of derived files before the next due check.
    now = published["reconciled_at"] + timedelta(seconds=1)
    assert not reconcile_if_due(restarted, ledger, report_dir, now)
    assert report_file.read_bytes() == old_bytes
    assert ledger.records[transaction_id]["sourceAmount"] == 1200
    assert reconcile(restarted, ledger, report_dir)
    assert json.loads(report_file.read_bytes())[0]["ledger_check_status"] == "mismatched"
    assert len(ledger.create_calls) == 1 and ledger.modify_calls == []


def resolving_child(dsn, snapshot, committed, channel):
    store = PostgresStore(dsn)
    pause_first_commit(store, channel, committed)
    resolve_snapshot(store, None, snapshot, "ignore", "合成强杀测试人工决定")
    raise AssertionError("resolution returned before SIGKILL")


@pytest.mark.parametrize("committed", [False, True], ids=["before-commit", "after-commit"])
def test_sigkill_manual_resolution_is_observed_by_commit_not_process_exit(
    database, settings, tmp_path, committed
):
    store = database.store
    recheck.blocked_transactions(store, tmp_path, settings)
    snapshot = snapshot_issues(store, "bank_transactions")
    old = store.one("SELECT * FROM bank_transactions")
    with kill_at_boundary(resolving_child, database.isolated_dsn, snapshot, committed):
        observed = store.one("SELECT * FROM bank_transactions")
        if committed:
            assert observed["import_status"] == "ignored"
            assert observed["decision_version"] == old["decision_version"] + 1
            assert observed["last_resolution"]["reason"] == "合成强杀测试人工决定"
        else:
            assert observed == old

    restarted = database.connect()
    assert restarted.one("SELECT * FROM bank_transactions") == observed
    if committed:
        assert issues(restarted, "bank_transactions") == []
        with pytest.raises(Conflict):
            resolve_snapshot(restarted, None, snapshot, "ignore", "不能重放旧决定")
    else:
        assert snapshot_issues(restarted, "bank_transactions") == snapshot
        assert (
            resolve_snapshot(restarted, None, snapshot, "ignore", "重新明确提交")["result"]
            == "decision saved"
        )
    assert (
        restarted.one("SELECT import_decision FROM bank_transactions")["import_decision"]
        == old["import_decision"]
    )
    assert restarted.all("SELECT * FROM ledger_write_attempt") == []
