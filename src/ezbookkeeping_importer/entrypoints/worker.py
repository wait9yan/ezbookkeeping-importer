import logging
import signal
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..adapters.logging import configure_logging
from ..application.collect import request_sync
from ..application.write import recover_dispatching, verify_unknown
from ..application.service import cycle
from ..application.events import emit, event_context, failure_fields
from ..domain.errors import Conflict, ImporterError, LogPersistenceError


def next_check(now: datetime) -> datetime:
    minutes = 10 if now.hour >= 17 else 60
    boundary = now.replace(second=0, microsecond=0)
    return boundary + timedelta(minutes=minutes - now.minute % minutes)


class StopSignals:
    """信号处理器只设置本地标志，日志与资源清理留在正常控制流。"""

    def __init__(self):
        self.reason: str | None = None
        self.previous = {}

    def is_set(self):
        return self.reason is not None

    def _request(self, signum, frame):
        if self.reason is None:
            self.reason = signal.Signals(signum).name

    def __enter__(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.previous[sig] = signal.signal(sig, self._request)
        return self

    def __exit__(self, *args):
        for sig, handler in self.previous.items():
            signal.signal(sig, handler)


def run(runtime, *, stop_event=None):
    if stop_event is not None:
        _run(runtime, stop_event)
        return
    with StopSignals() as stopped:
        _run(runtime, stopped)


def _run(runtime, stopped):
    if stopped.is_set():
        return
    if not runtime.store.lock_worker():
        raise Conflict("another worker is already running")
    logger = configure_logging(runtime.settings)
    started = time.monotonic()
    with event_context(run_id=uuid.uuid4().hex):
        emit("worker_starting", mode="continuous")
        try:
            recovered = recover_dispatching(runtime.store)
            verify_unknown(runtime.store, runtime.ledger, should_stop=stopped.is_set)
            if not stopped.is_set():
                request_sync(runtime.store)
            due = datetime.now(ZoneInfo(runtime.settings.timezone))
            if not stopped.is_set():
                emit("worker_started", counts={"recovered": recovered}, next_check_at=due.isoformat())
            cycle_was_failed = False
            while not stopped.is_set():
                now = datetime.now(ZoneInfo(runtime.settings.timezone))
                if now >= due:
                    request_sync(runtime.store)
                    due = next_check(now)
                try:
                    success = cycle(runtime, logger, should_stop=stopped.is_set)
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
                else:
                    if cycle_was_failed and success is True:
                        emit("cycle_recovered", stage="cycle")
                        cycle_was_failed = False
                for _ in range(30):
                    if stopped.is_set():
                        break
                    time.sleep(1)
        except LogPersistenceError:
            # 由入口的安全诊断通道报告；不能再次写入同一个故障日志文件。
            raise
        except Exception as exc:
            emit(
                "worker_failed",
                level=logging.ERROR,
                **failure_fields(exc, "worker_failed", getattr(exc, "processing_stage", "worker")),
            )
            raise
        stop_reason = getattr(stopped, "reason", None) or "requested"
        if stopped.is_set():
            emit("worker_stop_requested", reason=stop_reason)
        emit(
            "worker_stopped",
            reason=stop_reason,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
