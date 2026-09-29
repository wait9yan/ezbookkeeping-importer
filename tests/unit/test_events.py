"""统一事件边界的脱敏、进度限频与短期降噪。"""

import json
import logging

import pytest

from ezbookkeeping_importer.application import events
from ezbookkeeping_importer.adapters.logging import JsonFormatter
from ezbookkeeping_importer.domain.errors import LogPersistenceError


@pytest.fixture
def captured_events():
    logger = logging.getLogger("ebki")
    handlers, level, propagate = logger.handlers[:], logger.level, logger.propagate
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(json.loads(JsonFormatter().format(record)))

    logger.handlers = [Capture()]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    events._blocked.clear()
    try:
        yield records
    finally:
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate
        events._blocked.clear()


def test_event_fields_preserve_counts_but_never_raw_content(captured_events):
    events.emit(
        "reconciliation_published",
        report_key="synthetic-report",
        counts={"matched": 12, "bad": -1, "nested": {"secret": "hidden"}},
        publication_committed=True,
        request={"token": "hidden"},
        password="hidden",
        error_code="https://secret.invalid/private",
    )
    record = captured_events[0]
    assert record["counts"] == {"matched": 12}
    assert record["report_key"] == "synthetic-report"
    assert record["publication_committed"] is True
    assert "hidden" not in json.dumps(record)
    assert "secret.invalid" not in json.dumps(record)


def test_error_frames_do_not_include_exception_text_or_source(captured_events):
    try:
        raise RuntimeError("password=synthetic-secret https://private.invalid")
    except RuntimeError as exc:
        events.emit(
            "parse_failed",
            level=logging.ERROR,
            **events.failure_fields(exc, "parse_failed", "parse"),
        )
    rendered = json.dumps(captured_events)
    assert "synthetic-secret" not in rendered
    assert "private.invalid" not in rendered
    assert captured_events[0]["frames"][0]["file"] == "test_events.py"
    assert captured_events[0]["error_type"] == "RuntimeError"


def test_5019_items_emit_progress_at_most_every_five_seconds(captured_events, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(events.time, "monotonic", lambda: clock[0])
    progress = events.Progress("collection", 5019)
    for _ in range(5019):
        clock[0] += 0.01
        progress.advance(skipped=1)
    progress.finish()
    updates = [row for row in captured_events if row["event"] == "collection_progress"]
    assert 9 <= len(updates) <= 10
    assert all(b["duration_ms"] - a["duration_ms"] >= 5000 for a, b in zip(updates, updates[1:]))
    final = captured_events[-1]
    assert final["processed"] == final["total"] == 5019
    assert final["counts"] == {"skipped": 5019}


def test_repeat_blocking_changes_only_logging_level(captured_events, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(events.time, "monotonic", lambda: clock[0])
    kwargs = {
        "identity": "task-1",
        "task_id": 1,
        "decision_version": 1,
        "error_code": "target_missing",
    }
    events.blocked("write_verification_pending", **kwargs)
    clock[0] = 1
    events.blocked("write_verification_pending", **kwargs)
    events.blocked("write_verification_pending", **{**kwargs, "error_code": "query_failed"})
    clock[0] = 301
    events.blocked("write_verification_pending", **kwargs)
    assert [row["level"] for row in captured_events] == ["WARNING", "DEBUG", "WARNING", "WARNING"]


def test_safe_projection_validates_base_fields_and_legacy_aliases():
    row = events.safe_event(
        {
            "time": {"secret": "hidden"},
            "level": "INFO\nforged",
            "event": "worker_started",
            "job_id": 5,
            "target_id": "remote-1",
            "source_row_id": "source-1",
        }
    )
    assert row["time"] is None
    assert row["level"] == "UNKNOWN"
    assert row["task_id"] == 5
    assert row["ledger_transaction_id"] == "remote-1"
    assert row["transaction_id"] == "source-1"
    assert "hidden" not in str(row)


def test_direct_library_event_does_not_configure_logging(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    logger = logging.getLogger("ebki")
    handlers = logger.handlers
    logger.handlers = []
    try:
        events.emit("worker_started")
        assert list(tmp_path.iterdir()) == []
        assert logger.handlers == []
    finally:
        logger.handlers = handlers


def test_broken_sink_propagates_a_distinct_safe_failure(captured_events):
    class Broken(logging.Handler):
        def emit(self, record):
            raise OSError("disk path with private token")

    logging.getLogger("ebki").handlers = [Broken()]
    with pytest.raises(LogPersistenceError, match="runtime log persistence failed") as error:
        events.emit("report_accepted", report_key="synthetic")
    assert "private" not in str(error.value)


def test_blocking_reason_returning_to_previous_value_is_a_new_change(captured_events):
    for state in ("first", "first", "second", "first"):
        events.blocked(
            "write_preflight_blocked", identity="task-1", state=state,
            task_id=1, decision_version=1, error_code="write_preflight_failed",
        )
    assert [row["level"] for row in captured_events] == ["WARNING", "DEBUG", "WARNING", "WARNING"]
    assert all("state" not in row for row in captured_events)
