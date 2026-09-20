from ..domain.errors import Conflict
from .write import recover_dispatching, verify_unknown


def status(store, settings):
    return {
        "cursors": store.all("SELECT * FROM cursors"),
        "downloads": store.all("SELECT status,count(*) FROM downloads GROUP BY status"),
        "messages": store.all("SELECT status,count(*) FROM messages GROUP BY status"),
        "transactions": store.all("SELECT state,count(*) FROM transactions GROUP BY state"),
        "jobs": store.all("SELECT kind,status,count(*) FROM jobs GROUP BY kind,status"),
        "issues": store.one("SELECT count(*) FROM issues WHERE NOT resolved"),
        "writes_enabled": settings.writes_enabled,
    }


def issues(store, issue_id=None, include_resolved=False):
    if issue_id:
        return store.one("SELECT * FROM issues WHERE id=%s", (issue_id,))
    return store.all(
        "SELECT * FROM issues WHERE (%s OR NOT resolved) ORDER BY id", (include_resolved,)
    )


def restore_audit(store, ledger):
    if not store.lock_worker():
        raise Conflict("stop worker before restore audit")
    recover_dispatching(store)
    # Old backups may predate a local success commit; an absent marker is not proof
    # that a delayed external request was never committed.
    with store.transaction():
        store.execute(
            "UPDATE jobs SET status='unknown' WHERE kind IN ('create','settle_amount') AND status='queued'"
        )
        store.execute(
            "UPDATE transactions SET state='unknown' WHERE id IN (SELECT transaction_id FROM jobs WHERE status='unknown')"
        )
        store.audit("restore_audit_started", "database", {})
    verify_unknown(store, ledger)
    return {
        "unknown": store.one("SELECT count(*) FROM jobs WHERE status='unknown'")["count"],
        "note": "旧备份缺失的来源需重新采集并核对，禁止未经核实启用写入",
    }
