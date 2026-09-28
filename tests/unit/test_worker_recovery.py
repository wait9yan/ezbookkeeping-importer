from types import SimpleNamespace
from unittest.mock import Mock

import psycopg
import pytest

from ezbookkeeping_importer.entrypoints import worker
from ezbookkeeping_importer.domain.errors import ImporterError
from ezbookkeeping_importer.adapters.persistence.postgres import PostgresStore


def setup_worker(monkeypatch):
    store = Mock()
    store.lock_worker.return_value = True
    logger = Mock()
    runtime = SimpleNamespace(store=store, settings=SimpleNamespace(timezone="Asia/Shanghai"))
    monkeypatch.setattr(worker, "configure_logging", lambda _: logger)
    recovery = Mock()
    monkeypatch.setattr(worker, "recover_dispatching", recovery)
    monkeypatch.setattr(worker, "request_sync", Mock())
    monkeypatch.setattr(worker.signal, "signal", Mock())
    monkeypatch.setattr(
        worker.time, "sleep", lambda _: pytest.fail("broken connection must exit without waiting")
    )
    return runtime, logger, recovery


def test_database_disconnect_exits_without_waiting_for_next_scheduled_sync(monkeypatch):
    runtime, logger, recovery = setup_worker(monkeypatch)
    runtime.store.is_connection_usable.return_value = False
    cycle = Mock(side_effect=psycopg.OperationalError("synthetic private driver detail"))
    monkeypatch.setattr(worker, "cycle", cycle)
    with pytest.raises(ImporterError, match="restart the worker") as error:
        worker.run(runtime)
    assert "private" not in str(error.value)
    assert cycle.call_count == 1
    logger.error.assert_called_once_with(
        "worker_database_disconnected", extra={"error_type": "OperationalError"}
    )
    recovery.assert_called_once_with(runtime.store)
    runtime.store.lock_worker.assert_called_once()


@pytest.mark.parametrize(
    "closed,broken,expected", [(False, False, True), (True, False, False), (False, True, False)]
)
def test_connection_health_is_local_and_does_not_reconnect(closed, broken, expected):
    store = object.__new__(PostgresStore)
    store.connection = SimpleNamespace(closed=closed, broken=broken)
    assert store.is_connection_usable() is expected
