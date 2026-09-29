"""事件必须反映真实提交与远端发送，不能把日志故障变成业务失败。"""

from collections import OrderedDict
from copy import deepcopy
import logging
from unittest.mock import Mock

import pytest

import test_pipeline as pipeline
from ezbookkeeping_importer.adapters.banks.cmb import BankParser
from ezbookkeeping_importer.adapters.evidence_store import EvidenceStore
from ezbookkeeping_importer.adapters.logging import JsonFormatter, StrictFileHandler
from ezbookkeeping_importer.application import events
from ezbookkeeping_importer.application.classify import classify_pending, decide
from ezbookkeeping_importer.application.parse import parse_pending
from ezbookkeeping_importer.application.reconcile import reconcile
from ezbookkeeping_importer.application.write import complete, recover_dispatching, write_queued
from ezbookkeeping_importer.domain.errors import ImporterError, LogPersistenceError

database = pipeline.database
settings = pipeline.settings


class EventCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []
        self.observe = None

    def emit(self, record):
        value = deepcopy(record.event_data)
        self.records.append(value)
        if self.observe:
            self.observe(value)

    def named(self, event):
        return [record for record in self.records if record["event"] == event]

    def one(self, event):
        records = self.named(event)
        assert len(records) == 1, records
        return records[0]

    def clear(self):
        self.records.clear()


@pytest.fixture
def event_log(monkeypatch):
    logger = logging.getLogger("ebki")
    capture = EventCapture()
    previous_level = logger.level
    monkeypatch.setattr(logger, "handlers", [capture])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(events, "_blocked", OrderedDict())
    logger.setLevel(logging.DEBUG)
    try:
        yield capture
    finally:
        logger.setLevel(previous_level)


@pytest.fixture
def fail_log(event_log, tmp_path, monkeypatch):
    handlers = []
    logger = logging.getLogger("ebki")

    def install(event):
        handler = StrictFileHandler(tmp_path / "fault.jsonl", encoding="utf-8")
        handler.setFormatter(JsonFormatter())
        handler.addFilter(lambda record: record.getMessage() == event)

        def fail_write(value):
            raise OSError("synthetic disk full")

        monkeypatch.setattr(handler.stream, "write", fail_write)
        logger.addHandler(handler)
        handlers.append(handler)

    yield install
    for handler in handlers:
        logger.removeHandler(handler)
        handler.close()


def pending_mail(store, tmp_path, settings, **kwargs):
    return pipeline.ingest_mail(
        store, EvidenceStore(tmp_path / "evidence"), pipeline.raw_daily(**kwargs), settings
    )


def existing_source(store, tmp_path, settings, ledger):
    pipeline.import_daily(store, tmp_path, settings)
    transaction = store.one("SELECT * FROM bank_transactions")
    ledger.records["existing"] = {
        "id": "existing",
        **decide(transaction, settings, ledger, None)["payload"],
    }
    return transaction


def booked_statement(store, tmp_path, settings, ledger, *, currency="CNY"):
    transaction = pipeline.queue(store, tmp_path, settings, ledger, currency=currency)
    write_queued(store, ledger)
    pipeline.statement(store, transaction, "72.00" if currency == "USD" else "10.00")
    return store.one("SELECT * FROM bank_transactions")


def test_report_rollback_never_emits_acceptance(
    database, tmp_path, settings, monkeypatch, event_log
):
    from ezbookkeeping_importer.application import parse as module

    store = database.store
    identifier = pending_mail(store, tmp_path, settings, count=2)
    monkeypatch.setattr(module, "transaction_id", lambda *args: "A" * 16)

    parse_pending(store, BankParser(context="synthetic"))

    assert store.all("SELECT * FROM bank_report") == []
    assert store.all("SELECT * FROM bank_transactions") == []
    message = store.one("SELECT * FROM email WHERE id=%s", (identifier,))
    assert message["parse_status"] == "failed" and message["report_key"] is None
    assert event_log.named("report_accepted") == []
    assert event_log.one("parse_failed")["email_id"] == identifier
    assert event_log.one("parse_completed")["counts"]["failed"] == 1


def test_report_acceptance_is_visible_from_another_connection_before_event(
    database, tmp_path, settings, event_log
):
    store, observer = database.store, database.connect()
    pending_mail(store, tmp_path, settings)
    seen = []

    def observe(event):
        if event["event"] == "report_accepted":
            seen.extend(observer.all("SELECT * FROM bank_report"))

    event_log.observe = observe
    parse_pending(store, BankParser(context="synthetic"))

    assert len(seen) == 1
    event = event_log.one("report_accepted")
    assert event["report_key"] == seen[0]["report_key"]
    assert event["created_transaction_count"] == 1
    assert observer.one("SELECT count(*) AS n FROM bank_transactions")["n"] == 1


@pytest.mark.parametrize("stale", [True, False], ids=["stale-version", "already-completed"])
def test_completion_without_transition_does_not_emit_success(
    database, tmp_path, settings, event_log, stale
):
    store, ledger = database.store, pipeline.Ledger()
    pipeline.queue(store, tmp_path, settings, ledger)
    job = store.one("SELECT * FROM background_task")
    if stale:
        with store.transaction():
            store.execute("UPDATE bank_transactions SET decision_version=2")
            store.execute("UPDATE background_task SET decision_version=2")
    else:
        write_queued(store, ledger)
    before = store.one("SELECT * FROM bank_transactions")
    event_log.clear()

    complete(store, job, {"id": "old-target", **job["payload"]})

    assert store.one("SELECT * FROM bank_transactions") == before
    assert not any(
        event_log.named(event)
        for event in ("write_verified", "existing_link_restored", "settlement_already_applied")
    )


def test_api_response_is_not_verification_and_retry_never_reposts(
    database, tmp_path, settings, monkeypatch, event_log
):
    store, ledger = database.store, pipeline.Ledger()
    pipeline.queue(store, tmp_path, settings, ledger)
    readback = ledger.get
    monkeypatch.setattr(ledger, "get", lambda identifier: None)
    event_log.clear()

    write_queued(store, ledger)

    assert len(ledger.create_calls) == 1
    task = store.one("SELECT * FROM background_task")
    attempt = store.one("SELECT * FROM ledger_write_attempt")
    assert task["status"] == attempt["outcome"] == "unknown"
    assert attempt["response"] and attempt["response_received_at"]
    assert attempt["verified_at"] is None
    assert event_log.one("write_attempt_registered")["attempt_id"] == attempt["id"]
    assert event_log.one("write_response_received")["attempt_id"] == attempt["id"]
    assert event_log.named("write_verified") == []
    assert event_log.one("write_result_unknown")["next_action"] == "verify_only"

    event_log.clear()
    write_queued(store, ledger)
    assert len(ledger.create_calls) == 1
    assert event_log.named("write_verified") == []
    assert all(event["level"] == "DEBUG" for event in event_log.named("write_verification_pending"))

    observer = database.connect()
    confirmed = []

    def observe(event):
        if event["event"] == "write_verified":
            confirmed.extend(observer.all("SELECT * FROM ledger_write_attempt"))

    event_log.observe = observe
    monkeypatch.setattr(ledger, "get", readback)
    write_queued(store, ledger)
    assert len(ledger.create_calls) == 1
    assert len(confirmed) == 1 and confirmed[0]["outcome"] == "confirmed"
    event = event_log.one("write_verified")
    assert event["completion_method"] == "write_verified"
    assert event["ledger_transaction_id"] == task["ledger_transaction_id"]
    assert store.one("SELECT import_status FROM bank_transactions")["import_status"] == "booked"


def test_restart_logs_interrupted_write_then_readback_without_resend(
    database, tmp_path, settings, monkeypatch, event_log
):
    store, ledger = database.store, pipeline.Ledger()
    pipeline.queue(store, tmp_path, settings, ledger)
    create = ledger.create

    def interrupted(payload):
        create(payload)
        raise KeyboardInterrupt("synthetic process exit after remote commit")

    monkeypatch.setattr(ledger, "create", interrupted)
    with pytest.raises(KeyboardInterrupt):
        write_queued(store, ledger)
    assert store.one("SELECT status FROM background_task")["status"] == "dispatching"
    store.close()
    restarted = database.connect()
    event_log.clear()

    recover_dispatching(restarted)
    write_queued(restarted, ledger)
    recover_dispatching(restarted)

    assert len(ledger.create_calls) == 1
    assert restarted.one("SELECT status FROM background_task")["status"] == "done"
    assert restarted.one("SELECT outcome FROM ledger_write_attempt")["outcome"] == "confirmed"
    assert event_log.one("write_interrupted_recovered")["counts"] == {"create": 1}
    event_log.one("write_verified")
    assert event_log.named("write_attempt_registered") == []


def test_existing_marker_restores_link_with_no_attempt_or_creation(
    database, tmp_path, settings, event_log
):
    store, ledger = database.store, pipeline.Ledger()
    transaction = existing_source(store, tmp_path, settings, ledger)
    event_log.clear()

    classify_pending(store, settings, ledger, None)
    assert store.one("SELECT import_status FROM bank_transactions")["import_status"] == "unknown"
    assert event_log.one("existing_marker_found")["transaction_id"] == transaction["id"]
    assert event_log.named("existing_link_restored") == []
    write_queued(store, ledger)

    task = store.one("SELECT * FROM background_task")
    assert task["status"] == "done" and task["completion_method"] == "existing_link"
    assert store.all("SELECT * FROM ledger_write_attempt") == []
    assert ledger.create_calls == []
    event = event_log.one("existing_link_restored")
    assert event["completion_method"] == "existing_link"
    assert event["ledger_transaction_id"] == "existing"
    assert event_log.named("write_verified") == event_log.named("write_attempt_registered") == []


def test_already_applied_settlement_logs_zero_send_and_preserves_identity(
    database, tmp_path, settings, event_log
):
    store, ledger = database.store, pipeline.Ledger()
    transaction = booked_statement(store, tmp_path, settings, ledger, currency="USD")
    reconcile(store, ledger, tmp_path)
    target = transaction["ledger_transaction_id"]
    ledger.records[target].update(sourceAccountId="account", sourceAmount=7200)
    event_log.clear()

    write_queued(store, ledger)

    task = store.one("SELECT * FROM background_task WHERE task_type='settle_currency'")
    assert task["status"] == "done" and task["completion_method"] == "already_applied"
    assert store.all("SELECT * FROM ledger_write_attempt WHERE task_id=%s", (task["id"],)) == []
    assert len(ledger.create_calls) == 1 and ledger.modify_calls == []
    current = store.one("SELECT * FROM bank_transactions")
    assert current["ledger_transaction_id"] == target
    assert current["original_currency"] == "USD"
    assert current["import_decision"]["target_currency"] == "CNY"
    assert event_log.one("settlement_already_applied")["task_id"] == task["id"]
    assert event_log.named("write_verified") == event_log.named("write_attempt_registered") == []


def test_valid_ai_unmatched_is_counted_separately_from_model_failure(
    database, tmp_path, settings, event_log
):
    store, ledger = database.store, pipeline.Ledger()
    pipeline.import_daily(store, tmp_path, settings, count=2)
    ai = Mock()
    ai.classify.side_effect = [
        {"classification_status": "unmatched", "category_id": None, "reason": "合成未匹配"},
        ImporterError("synthetic model protocol failure"),
    ]
    event_log.clear()

    classify_pending(store, settings.model_copy(update={"classification_mode": "ai"}), ledger, ai)

    queued = store.one("SELECT * FROM bank_transactions WHERE import_status='queued'")
    failed = store.one("SELECT * FROM bank_transactions WHERE import_status='issue'")
    assert queued["import_decision"]["classification"]["classification_status"] == "unmatched"
    assert queued["import_decision"]["payload"]["categoryId"] == "category"
    assert failed["import_error"]["code"] == "classification_failed"
    assert store.one("SELECT count(*) AS n FROM background_task")["n"] == 1
    assert ai.classify.call_count == 2 and ledger.create_calls == []
    summary = event_log.one("classification_completed")
    assert summary["processed"] == summary["total"] == 2
    assert summary["counts"]["unmatched"] == summary["counts"]["failed"] == 1
    assert summary["counts"]["queued"] == 1
    assert event_log.one("classification_failed")["transaction_id"] == failed["id"]


def test_fifty_seven_duplicates_have_one_warning_and_no_write_task(
    database, tmp_path, settings, event_log
):
    store, ledger = database.store, pipeline.Ledger()
    pipeline.import_daily(store, tmp_path, settings, count=57)
    transaction = store.one("SELECT * FROM bank_transactions ORDER BY id")
    ledger.records["existing"] = {
        **decide(transaction, settings, ledger, None)["payload"],
        "id": "existing",
        "comment": transaction["merchant_name"],
    }
    event_log.clear()

    classify_pending(store, settings, ledger, None)
    write_queued(store, ledger)

    blocked = store.all("SELECT * FROM bank_transactions WHERE import_status='issue'")
    assert len(blocked) == 57
    assert all(row["import_error"]["code"] == "duplicate_candidates" for row in blocked)
    assert store.all("SELECT * FROM background_task") == [] and ledger.create_calls == []
    warnings = [record for record in event_log.records if record["level"] == "WARNING"]
    assert len(warnings) == 1
    assert warnings[0]["event"] == "transaction_blocked"
    assert warnings[0]["reason_code"] == "duplicate_candidates"
    assert warnings[0]["affected_count"] == 57
    summary = event_log.one("classification_completed")
    assert summary["processed"] == summary["total"] == 57
    assert summary["counts"]["duplicate_candidates"] == 57
    assert summary["counts"].get("failed", 0) == 0


def test_export_failure_does_not_relabel_committed_reconciliation(
    database, tmp_path, settings, monkeypatch, event_log
):
    from ezbookkeeping_importer.application import reconcile as module

    store, ledger = database.store, pipeline.Ledger()
    booked_statement(store, tmp_path, settings, ledger)
    monkeypatch.setattr(
        module, "write_report", Mock(side_effect=OSError("synthetic export failure"))
    )
    event_log.clear()

    with pytest.raises(OSError, match="synthetic export failure"):
        reconcile(store, ledger, tmp_path)

    report = store.one("SELECT * FROM bank_report WHERE report_type='monthly'")
    results = store.all("SELECT * FROM bank_statement_reconciliation")
    assert report["reconciliation_version"] == 1 and report["reconciled_at"]
    assert report["reconciliation_last_error"] is None
    assert results and all(row["ledger_check_status"] == "matched" for row in results)
    assert event_log.one("reconciliation_published")["result_count"] == len(results)
    assert event_log.one("report_export_failed")["publication_committed"] is True
    assert event_log.named("reconciliation_failed") == []


def test_log_disk_failure_after_acceptance_preserves_committed_report(
    database, tmp_path, settings, event_log, fail_log, capsys
):
    store = database.store
    identifier = pending_mail(store, tmp_path, settings)
    fail_log("report_accepted")

    with pytest.raises(LogPersistenceError):
        parse_pending(store, BankParser(context="synthetic"))

    assert "runtime log persistence failed" in capsys.readouterr().err
    observer = database.connect()
    message = observer.one("SELECT * FROM email WHERE id=%s", (identifier,))
    assert message["parse_status"] == "parsed" and message["report_key"]
    assert observer.one("SELECT count(*) AS n FROM bank_transactions")["n"] == 1
    assert event_log.named("parse_failed") == []


def test_log_disk_failure_after_existing_marker_keeps_verification_state(
    database, tmp_path, settings, event_log, fail_log
):
    store, ledger = database.store, pipeline.Ledger()
    existing_source(store, tmp_path, settings, ledger)
    fail_log("existing_marker_found")

    with pytest.raises(LogPersistenceError):
        classify_pending(store, settings, ledger, None)

    observer = database.connect()
    transaction = observer.one("SELECT * FROM bank_transactions")
    assert transaction["import_status"] == "unknown" and transaction["import_error"] is None
    assert observer.one("SELECT status FROM background_task")["status"] == "unknown"
    assert observer.all("SELECT * FROM ledger_write_attempt") == []
    assert event_log.named("classification_failed") == [] and ledger.create_calls == []


def test_log_disk_failure_after_verified_write_does_not_reopen_completed_task(
    database, tmp_path, settings, event_log, fail_log
):
    store, ledger = database.store, pipeline.Ledger()
    pipeline.queue(store, tmp_path, settings, ledger)
    fail_log("write_verified")

    with pytest.raises(LogPersistenceError):
        write_queued(store, ledger)

    observer = database.connect()
    task = observer.one("SELECT * FROM background_task")
    assert task["status"] == "done" and task["last_error"] is None
    assert task["error_code"] is None
    assert observer.one("SELECT outcome FROM ledger_write_attempt")["outcome"] == "confirmed"
    assert observer.one("SELECT import_status FROM bank_transactions")["import_status"] == "booked"
    assert len(ledger.create_calls) == 1
    assert event_log.named("write_verification_pending") == []


def test_log_disk_failure_after_publication_does_not_schedule_business_retry(
    database, tmp_path, settings, event_log, fail_log
):
    store, ledger = database.store, pipeline.Ledger()
    booked_statement(store, tmp_path, settings, ledger)
    event_log.clear()
    fail_log("reconciliation_published")

    with pytest.raises(LogPersistenceError):
        reconcile(store, ledger, tmp_path)

    observer = database.connect()
    report = observer.one("SELECT * FROM bank_report WHERE report_type='monthly'")
    assert report["reconciliation_version"] == 1 and report["reconciled_at"]
    assert report["reconciliation_last_error"] is None
    assert report["reconciliation_queries_succeeded"] is True
    assert (
        report["reconciliation_next_check_at"] - report["reconciled_at"]
    ).total_seconds() == 3600
    assert observer.one("SELECT count(*) AS n FROM bank_statement_reconciliation")["n"] > 0
    assert event_log.named("reconciliation_failed") == []


@pytest.mark.parametrize(
    "subject,reason", [("新的信用卡账单", "unknown_template"), ("每日信用管家", "invalid_template")]
)
def test_parse_blocking_event_exposes_specific_safe_reason(
    database, tmp_path, settings, event_log, subject, reason
):
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = subject
    message.set_content("<p>unsupported synthetic content</p>", subtype="html")
    pipeline.ingest_mail(database.store, EvidenceStore(tmp_path), message.as_bytes(), settings)
    parse_pending(database.store, BankParser(context="synthetic"))
    assert event_log.one("report_blocked")["reason_code"] == reason


def test_stale_round_does_not_claim_it_scheduled_retry(
    database, tmp_path, settings, event_log, monkeypatch
):
    store, ledger = database.store, pipeline.Ledger()
    booked_statement(store, tmp_path, settings, ledger)
    reconcile(store, ledger, tmp_path)
    event_log.clear()
    peer = database.connect()
    get = ledger.get
    published = []

    def publish_newer(target):
        if not published:
            published.append(True)
            reconcile(peer, ledger, tmp_path / "newer")
        return get(target)

    monkeypatch.setattr(ledger, "get", publish_newer)
    reconcile(store, ledger, tmp_path)
    stale = event_log.one("reconciliation_stale")
    assert stale["retry_scheduled"] is False
    assert stale["next_action"] == "newer_publication_retained"
