import signal
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..adapters.logging import configure_logging
from ..application.collect import request_sync
from ..application.write import recover_dispatching
from ..application.service import cycle
from ..domain.errors import Conflict, ImporterError


def next_check(now: datetime) -> datetime:
    minutes = 10 if now.hour >= 17 else 60
    boundary = now.replace(second=0, microsecond=0)
    return boundary + timedelta(minutes=minutes - now.minute % minutes)


def run(runtime, once: bool = False):
    if not runtime.store.lock_worker():
        raise Conflict("another worker is already running")
    logger = configure_logging(runtime.settings)
    recover_dispatching(runtime.store)
    request_sync(runtime.store)
    stopped = False

    def stop(signum, frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    due = datetime.now(ZoneInfo(runtime.settings.timezone))
    logger.info("worker_started")
    while not stopped:
        now = datetime.now(ZoneInfo(runtime.settings.timezone))
        if now >= due:
            request_sync(runtime.store)
            due = next_check(now)
        try:
            success = cycle(runtime, logger)
            if once and not success:
                raise ImporterError("synchronization failed; persisted tasks were still processed")
        except Exception as exc:
            if not runtime.store.is_connection_usable():
                logger.error(
                    "worker_database_disconnected", extra={"error_type": type(exc).__name__}
                )
                raise ImporterError(
                    "database connection lost; restart the worker to reacquire its lock and verify interrupted writes"
                ) from None
            logger.error("cycle_failed", extra={"error_type": type(exc).__name__})
            if once:
                raise
        if once:
            return
        # CLI jobs are observed promptly; failed external work retries after a bounded delay.
        for _ in range(30):
            if stopped:
                break
            time.sleep(1)
    logger.info("worker_stopped")
