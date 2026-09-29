from datetime import date
import logging
import time
from ..domain.errors import ImporterError, LogPersistenceError

from .events import emit, event_context, failure_fields
from .collect import collect
from .parse import parse_pending
from .classify import classify_pending
from .write import write_queued
from .reconcile import reconcile_if_due as reconcile


def cycle(runtime, logger):
    store = runtime.store
    sync_failed = False
    job = store.one("""UPDATE background_task SET status='dispatching' WHERE id=(SELECT id FROM background_task
        WHERE task_type IN ('sync','sync_range') AND status='queued' ORDER BY updated_at,id LIMIT 1) RETURNING *""")
    if job:
        started = time.monotonic()
        emit(
            "sync_started",
            task_id=job["id"],
            task_type=job["task_type"],
            scan_mode="range" if job["task_type"] == "sync_range" else "incremental",
        )
        try:
            mail = runtime.mail()
            try:
                payload = job["payload"]
                since = (
                    date.fromisoformat(payload["since"])
                    if job["task_type"] == "sync_range"
                    else None
                )
                until = (
                    date.fromisoformat(payload["until"])
                    if job["task_type"] == "sync_range"
                    else None
                )
                summary = collect(store, mail, runtime.evidence, runtime.settings, since, until)
            finally:
                mail.close()
            with store.transaction():
                store.execute(
                    "UPDATE background_task SET status='done',error_code=NULL,last_error=NULL,updated_at=now() WHERE id=%s",
                    (job["id"],),
                )
            emit(
                "sync_completed",
                task_id=job["id"],
                counts=summary["counts"],
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except LogPersistenceError:
            raise
        except Exception as exc:
            sync_failed = True
            store.execute(
                "UPDATE background_task SET status='queued',error_code='sync_failed',last_error=%s,updated_at=now() WHERE id=%s",
                (str(exc) if isinstance(exc, ImporterError) else type(exc).__name__, job["id"]),
            )
            emit(
                "sync_failed",
                level=logging.ERROR,
                task_id=job["id"],
                next_action="queued_for_retry",
                **failure_fields(exc, "sync_failed", "collection"),
            )
    stages = [
        ("parse", lambda: parse_pending(store, runtime.parser)),
        (
            "classification",
            lambda: classify_pending(store, runtime.settings, runtime.ledger, runtime.ai),
        ),
        ("write", lambda: write_queued(store, runtime.ledger)),
        ("reconciliation", lambda: reconcile(store, runtime.ledger, runtime.settings.report_dir)),
        ("write", lambda: write_queued(store, runtime.ledger)),
    ]
    for stage, operation in stages:
        with event_context(stage=stage):
            try:
                operation()
            except LogPersistenceError:
                raise
            except Exception as exc:
                setattr(exc, "processing_stage", stage)
                raise

    return not sync_failed
