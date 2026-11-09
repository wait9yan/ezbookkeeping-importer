"""稳定来源问题的只读投影与快照；关联规则统一由 sources 提供。"""

from ..domain.errors import Conflict, ImporterError
from .sources import load_transactions, lock_source, report_row, reverse_daily_pool, source_candidates


def source_key(entity_id):
    parts = entity_id.split(":")
    if len(parts) != 2 or not all(parts):
        raise ImporterError("transaction source id requires report_key:row_key")
    return tuple(parts)


def source_row(store, entity_id):
    return store.one(
        "SELECT * FROM transaction_source WHERE report_key=%s AND row_key=%s",
        source_key(entity_id),
    )


def source_issues(store):
    rows = store.all("""SELECT s.*,r.source_id,r.content,r.content_fingerprint,r.report_type
        FROM transaction_source s JOIN bank_report r ON r.report_key=s.report_key
        WHERE s.transaction_id IS NULL ORDER BY s.report_key,s.row_key""")
    loaded = {}
    for row in rows:
        if row["source_id"] not in loaded:
            loaded[row["source_id"]] = load_transactions(store, row["source_id"])[0]
        facts = report_row(row, row["row_key"])
        candidates = source_candidates([facts], loaded[row["source_id"]])[row["row_key"]]
        observation = store.one("""SELECT * FROM bank_statement_reconciliation
            WHERE statement_report_key=%s AND statement_row_key=%s
            AND check_direction='statement_to_transaction'""", (row["report_key"], row["row_key"]))
        ignored = (row["last_resolution"] or {}).get("action") == "ignore"
        status = "ignored" if ignored else "issue"
        residual_ids = ((observation or {}).get("details") or {}).get("candidate_ids", []) \
            if (observation or {}).get("match_status") == "ambiguous" else []
        if row["report_type"] == "daily" and not candidates:
            residual_ids = [t["id"] for t in reverse_daily_pool(facts, loaded[row["source_id"]])]
        detail = {
            "candidate_ids": [t["id"] for t in candidates] or residual_ids,
            "observation": observation,
            "last_resolution": row["last_resolution"],
            "ledger_candidates": (observation or {}).get("details", {}).get("ledger_candidates", []),
        }
        if ignored:
            code = "source_ignored"
        elif candidates or residual_ids:
            code = "source_ambiguous"
        elif observation:
            code = "source_" + observation["ledger_check_status"]
        else:
            code = "source_unassociated"
        yield {
            "entity_type": "transaction_source",
            "entity_id": row["report_key"] + ":" + row["row_key"],
            "code": code, "detail": detail, "version": row["resolution_version"],
            "status": status,
            "context": {"report_key": row["report_key"], "row_key": row["row_key"],
                        "transaction_date": facts["occurred_date"],
                        "merchant": facts["merchant_raw"], "report_type": row["report_type"]},
        }


def source_state(store, entity_id, *, lock=False):
    report_key, row_key = source_key(entity_id)
    report = store.one("SELECT * FROM bank_report WHERE report_key=%s", (report_key,))
    if not report:
        raise Conflict("来源报告已经变化，请重新查询")
    if lock:
        lock_source(store, report["source_id"])
        store.all("""SELECT t.id FROM bank_transactions t JOIN bank_report r
            ON r.report_key=t.report_key WHERE r.source_id=%s ORDER BY t.id FOR UPDATE OF t""",
                  (report["source_id"],))
        store.all("""SELECT b.id FROM background_task b JOIN bank_transactions t
            ON t.id=b.bank_transaction_id JOIN bank_report r ON r.report_key=t.report_key
            WHERE r.source_id=%s ORDER BY b.id FOR UPDATE OF b""", (report["source_id"],))
    row = store.one("SELECT * FROM transaction_source WHERE report_key=%s AND row_key=%s"
                    + (" FOR UPDATE" if lock else ""), (report_key, row_key))
    if not row:
        raise Conflict("来源对象已经变化，请重新查询")
    transactions, sources = load_transactions(store, report["source_id"])
    tasks = store.all("""SELECT b.* FROM background_task b JOIN bank_transactions t
        ON t.id=b.bank_transaction_id JOIN bank_report r ON r.report_key=t.report_key
        WHERE r.source_id=%s ORDER BY b.id""", (report["source_id"],))
    return {"row": row, "related": {
        "report": {key: report[key] for key in ("report_key", "source_id", "content_fingerprint", "content")},
        "sources": sources, "transactions": transactions, "tasks": tasks,
    }}
