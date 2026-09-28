from datetime import date

from .collect import collect
from .parse import parse_pending
from .classify import classify_pending
from .write import write_queued
from .reconcile import reconcile_if_due as reconcile


def cycle(runtime, logger):
    store = runtime.store
    sync_failed = False
    job = store.one("""UPDATE jobs SET status='dispatching' WHERE id=(SELECT id FROM jobs
        WHERE kind IN ('sync','sync_range') AND status='queued' ORDER BY updated_at,id LIMIT 1) RETURNING *""")
    if job:
        try:
            mail = runtime.mail()
            try:
                payload = job["payload"]
                since = (
                    date.fromisoformat(payload["since"]) if job["kind"] == "sync_range" else None
                )
                until = (
                    date.fromisoformat(payload["until"]) if job["kind"] == "sync_range" else None
                )
                collect(store, mail, runtime.evidence, runtime.settings, since, until)
            finally:
                mail.close()
            with store.transaction():
                store.execute(
                    "UPDATE jobs SET status='done',updated_at=now() WHERE id=%s", (job["id"],)
                )
                store.execute(
                    "UPDATE issues SET resolved=true WHERE code='sync_failed' AND entity_id=%s",
                    (str(job["id"]),),
                )
            logger.info("sync_completed", extra={"job_id": job["id"]})
        except Exception as exc:
            sync_failed = True
            store.execute(
                "UPDATE jobs SET status='queued',error=%s,updated_at=now() WHERE id=%s",
                (type(exc).__name__, job["id"]),
            )
            store.issue("sync_failed", str(job["id"]), {"error_type": type(exc).__name__})
            logger.error(
                "sync_failed", extra={"job_id": job["id"], "error_type": type(exc).__name__}
            )
    parse_pending(store, runtime.parser)
    classify_pending(store, runtime.settings, runtime.ledger, runtime.ai)
    write_queued(store, runtime.ledger, runtime.settings.writes_enabled)
    reconcile(store, runtime.ledger, runtime.settings.report_dir)
    write_queued(store, runtime.ledger, runtime.settings.writes_enabled)

    return not sync_failed
