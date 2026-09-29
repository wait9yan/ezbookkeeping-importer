from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ezbookkeeping_importer.entrypoints import worker
from ezbookkeeping_importer.application import service
from ezbookkeeping_importer.domain.errors import Conflict, LogPersistenceError


@pytest.fixture
def runtime(monkeypatch):
    store = Mock()
    store.lock_worker.return_value = True
    state = SimpleNamespace(store=store, settings=SimpleNamespace(timezone="Asia/Shanghai"))
    monkeypatch.setattr(worker, "configure_logging", Mock())
    monkeypatch.setattr(worker, "recover_dispatching", Mock(return_value=0))
    monkeypatch.setattr(worker, "request_sync", Mock())
    monkeypatch.setattr(worker.signal, "signal", Mock())
    monkeypatch.setattr(
        worker.time, "sleep", lambda _: pytest.fail("must not poll in this scenario")
    )
    return state


def capture(monkeypatch):
    records = []
    monkeypatch.setattr(worker, "emit", lambda event, **fields: records.append((event, fields)))
    return records


def test_once_finishes_with_stopped_event(runtime, monkeypatch):
    records = capture(monkeypatch)
    monkeypatch.setattr(worker, "cycle", Mock(return_value=True))
    worker.run(runtime, once=True)
    assert [name for name, _ in records] == ["worker_starting", "worker_started", "worker_stopped"]
    assert records[-1][1]["reason"] == "once"


def test_lock_failure_never_opens_log_file(runtime):
    runtime.store.lock_worker.return_value = False
    with pytest.raises(Conflict):
        worker.run(runtime)
    worker.configure_logging.assert_not_called()


def test_recovery_failure_has_no_normal_stopped_event(runtime, monkeypatch):
    records = capture(monkeypatch)
    worker.recover_dispatching.side_effect = RuntimeError("private detail")
    with pytest.raises(RuntimeError):
        worker.run(runtime, once=True)
    assert [name for name, _ in records] == ["worker_starting", "worker_failed"]
    assert "private detail" not in str(records)


def test_logging_failure_bypasses_cycle_recovery(runtime, monkeypatch):
    records = capture(monkeypatch)
    monkeypatch.setattr(worker, "cycle", Mock(side_effect=LogPersistenceError("safe log failure")))
    with pytest.raises(LogPersistenceError):
        worker.run(runtime, once=True)
    assert [name for name, _ in records] == ["worker_starting", "worker_started"]
    runtime.store.is_connection_usable.assert_not_called()


def test_signal_reports_request_before_stopped(runtime, monkeypatch):
    records = capture(monkeypatch)
    handlers = {}
    monkeypatch.setattr(
        worker.signal, "signal", lambda number, handler: handlers.update({number: handler})
    )

    def stop_cycle(*args, **kwargs):
        handlers[worker.signal.SIGINT](worker.signal.SIGINT, None)
        handlers[worker.signal.SIGINT](worker.signal.SIGINT, None)
        return True

    monkeypatch.setattr(worker, "cycle", stop_cycle)
    worker.run(runtime)
    assert [name for name, _ in records] == [
        "worker_starting",
        "worker_started",
        "worker_stop_requested",
        "worker_stopped",
    ]


def test_empty_cycle_does_not_emit_info_events(monkeypatch):
    store = Mock()
    store.one.return_value = None
    runtime = SimpleNamespace(store=store, parser=Mock(), settings=Mock(), ledger=Mock(), ai=None)
    for name in ("parse_pending", "classify_pending", "write_queued", "reconcile"):
        monkeypatch.setattr(service, name, Mock())
    output = Mock()
    monkeypatch.setattr(service, "emit", output)
    assert service.cycle(runtime, Mock())
    output.assert_not_called()
