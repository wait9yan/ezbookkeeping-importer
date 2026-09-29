import logging
import signal
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..adapters.logging import configure_logging
from ..application.collect import request_sync
from ..application.write import recover_dispatching
from ..application.service import cycle
from ..application.events import emit, event_context, failure_fields
from ..domain.errors import Conflict, ImporterError, LogPersistenceError


def next_check(now: datetime) -> datetime:
    minutes = 10 if now.hour >= 17 else 60
    boundary = now.replace(second=0, microsecond=0)
    return boundary + timedelta(minutes=minutes - now.minute % minutes)


def run(runtime, once: bool = False):
    if not runtime.store.lock_worker():
        raise Conflict("another worker is already running")
    logger = configure_logging(runtime.settings)
    started = time.monotonic()
    with event_context(run_id=uuid.uuid4().hex):
        emit("worker_starting", mode="once" if once else "continuous")
        stopped = False
        stop_reason = "once" if once else "requested"

        def stop(signum, frame):
            nonlocal stopped, stop_reason
            if not stopped:
                stopped = True
                stop_reason = signal.Signals(signum).name
                emit("worker_stop_requested", reason=stop_reason)

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            recovered = recover_dispatching(runtime.store)
            request_sync(runtime.store)
            due = datetime.now(ZoneInfo(runtime.settings.timezone))
            emit("worker_started", counts={"recovered": recovered}, next_check_at=due.isoformat())
            cycle_was_failed = False
            while not stopped:
                now = datetime.now(ZoneInfo(runtime.settings.timezone))
                if now >= due:
                    request_sync(runtime.store)
                    due = next_check(now)
                try:
                    success = cycle(runtime, logger)
                    if once and not success:
                        raise ImporterError(
                            "synchronization failed; persisted tasks were still processed"
                        )
                except LogPersistenceError:
                    raise
                except Exception as exc:
                    if not runtime.store.is_connection_usable():
                        emit(
                            "worker_database_disconnected",
                            level=logging.ERROR,
                            next_action="restart_and_verify",
                            **failure_fields(exc, "database_disconnected", "cycle"),
                        )
                        raise ImporterError(
                            "database connection lost; restart the worker to reacquire its lock and verify interrupted writes"
                        ) from None
                    emit(
                        "cycle_failed",
                        level=logging.ERROR,
                        **failure_fields(
                            exc, "cycle_failed", getattr(exc, "processing_stage", "cycle")
                        ),
                    )
                    cycle_was_failed = True
                    if once:
                        raise
                else:
                    if cycle_was_failed and success:
                        emit("cycle_recovered", stage="cycle")
                        cycle_was_failed = False
                if once:
                    break
                for _ in range(30):
                    if stopped:
                        break
                    time.sleep(1)
        except LogPersistenceError:
            # The handler already reported an explicit safe stderr failure. Retrying
            # the same broken log sink cannot make a fatal event durable.
            raise
        except Exception as exc:
            emit(
                "worker_failed",
                level=logging.ERROR,
                **failure_fields(exc, "worker_failed", getattr(exc, "processing_stage", "worker")),
            )
            raise
        emit(
            "worker_stopped",
            reason=stop_reason,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
