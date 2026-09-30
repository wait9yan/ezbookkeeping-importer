import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import psycopg
import pytest

from ezbookkeeping_importer.entrypoints import worker
from ezbookkeeping_importer.domain.errors import ImporterError
from ezbookkeeping_importer.adapters.persistence.postgres import PostgresStore


def setup_worker(monkeypatch, caplog):
    store = Mock()
    store.lock_worker.return_value = True
    logger = logging.getLogger("ebki")
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "disabled", False)
    caplog.set_level(logging.INFO, logger="ebki")
    runtime = SimpleNamespace(store=store, ledger=Mock(), settings=SimpleNamespace(timezone="Asia/Shanghai"))
    monkeypatch.setattr(worker, "configure_logging", lambda _, **kwargs: logger)
    recovery = Mock(return_value=0)
    monkeypatch.setattr(worker, "recover_dispatching", recovery)
    monkeypatch.setattr(worker, "request_sync", Mock())
    monkeypatch.setattr(worker, "verify_unknown", Mock())
    monkeypatch.setattr(worker.signal, "signal", Mock())
    monkeypatch.setattr(
        worker.time, "sleep", lambda _: pytest.fail("broken connection must exit without waiting")
    )
    return runtime, logger, recovery


def test_database_disconnect_exits_without_waiting_for_next_scheduled_sync(monkeypatch, caplog):
    runtime, logger, recovery = setup_worker(monkeypatch, caplog)
    runtime.store.is_connection_usable.return_value = False
    cycle = Mock(side_effect=psycopg.OperationalError("synthetic private driver detail"))
    monkeypatch.setattr(worker, "cycle", cycle)
    with pytest.raises(ImporterError, match="restart the worker") as error:
        worker.run(runtime)
    assert "private" not in str(error.value)
    assert cycle.call_count == 1
    assert cycle.call_args.args == (runtime, logger)
    assert callable(cycle.call_args.kwargs["should_stop"])
    events = [record.event_data for record in caplog.records if record.name == "ebki"]
    assert [event["event"] for event in events] == [
        "worker_starting",
        "worker_started",
        "worker_database_disconnected",
        "worker_failed",
    ]
    disconnected = events[2]
    assert disconnected["level"] == "ERROR"
    assert disconnected["error_type"] == "OperationalError"
    assert disconnected["error_code"] == "database_disconnected"
    assert disconnected["stage"] == "cycle"
    assert disconnected["next_action"] == "restart_and_verify"
    assert events[3]["level"] == "ERROR"
    assert len({event["run_id"] for event in events}) == 1 and events[0]["run_id"]
    assert "private" not in json.dumps(events)
    recovery.assert_called_once_with(runtime.store)
    runtime.store.lock_worker.assert_called_once()


@pytest.mark.parametrize(
    "closed,broken,expected", [(False, False, True), (True, False, False), (False, True, False)]
)
def test_connection_health_is_local_and_does_not_reconnect(closed, broken, expected):
    store = object.__new__(PostgresStore)
    store.connection = SimpleNamespace(closed=closed, broken=broken)
    assert store.is_connection_usable() is expected
