"""当前对象问题的只读投影，不创建问题身份或问题历史。"""

from collections import defaultdict

from ..domain.errors import Conflict
from .write import recover_dispatching, verify_unknown, WRITE_TYPES


def issues(store, entity_type=None, entity_id=None):
    result = []

    def add(kind, row, code, detail, version=None):
        if entity_type is not None and entity_type != kind:
            return
        if entity_id is not None and str(entity_id) != str(row["id"]):
            return
        result.append(
            {
                "entity_type": kind,
                "entity_id": str(row["id"]),
                "code": code,
                "detail": detail,
                "version": version,
                "status": row.get("status", row.get("import_status", row.get("parse_status"))),
                "context": _issue_context(row),
            }
        )

    for row in store.all("SELECT * FROM email_source_item WHERE status='failed'"):
        add("email_source_item", row, "download_failed", row["last_error"])
    for row in store.all("""SELECT s.* FROM email_source_item s WHERE s.status='collected'
        AND s.source_status='requires_acceptance' AND s.accepted_at IS NULL
        AND NOT EXISTS(SELECT 1 FROM email_source_item t WHERE t.email_id=s.email_id
        AND t.source_id=s.source_id AND (t.source_status='verified' OR t.accepted_at IS NOT NULL))"""):
        add("email_source_item", row, "source_acceptance", row["source_reason"])
    for row in store.all(
        "SELECT * FROM email WHERE parse_status<>'ignored' AND parse_issues<>'[]'::jsonb"
    ):
        for issue in row["parse_issues"]:
            add("email", row, issue["code"], issue)
    for row in store.all(
        "SELECT *,report_key AS id FROM bank_report WHERE reconciliation_last_error IS NOT NULL"
    ):
        add("bank_report", row, "reconciliation_failed", row["reconciliation_last_error"])
    for row in store.all("SELECT * FROM bank_transactions WHERE import_error IS NOT NULL"):
        add(
            "bank_transactions",
            row,
            row["import_error"]["code"],
            row["import_error"].get("detail"),
            row["decision_version"],
        )
    for row in store.all("SELECT * FROM background_task WHERE error_code IS NOT NULL"):
        add("background_task", row, row["error_code"], row["last_error"], row["decision_version"])
    for row in store.all("""SELECT * FROM bank_statement_reconciliation WHERE
        match_status IN ('missing_source_transaction','missing_statement_evidence','ambiguous')
        OR ledger_check_status IN ('mismatched','target_missing','query_failed','not_comparable')"""):
        add("bank_statement_reconciliation", row, "reconciliation", row)
    return result


def _issue_context(row):
    fields = {
        "occurred_date": "transaction_date",
        "merchant_name": "merchant",
        "original_amount": "original_amount",
        "original_currency": "original_currency",
        "report_key": "report_key",
        "statement_report_key": "statement_report_key",
        "bank_transaction_id": "bank_transaction_id",
        "task_type": "task_type",
        "report_date": "report_date",
        "period_start": "period_start",
        "period_end": "period_end",
        "report_type": "report_type",
        "folder": "folder",
        "uid": "uid",
    }
    return {target: row[source] for source, target in fields.items() if source in row}


def issue_groups(items):
    grouped = defaultdict(list)
    for item in items:
        detail = item["detail"] if item["code"] == "reconciliation" else {}
        key = (
            item["entity_type"],
            item["code"],
            item["status"],
            detail.get("match_status"),
            detail.get("ledger_check_status"),
        )
        grouped[key].append(item)
    result = []
    for (entity_type, code, state, match, ledger_check), group in grouped.items():
        summary = {
            "entity_type": entity_type,
            "code": code,
            "status": state,
            "count": len(group),
            "object_count": len({item["entity_id"] for item in group}),
        }
        if code == "reconciliation":
            summary.update(match_status=match, ledger_check_status=ledger_check)
        result.append(summary)
    return sorted(
        result,
        key=lambda group: (
            group["entity_type"],
            group["code"],
            group["status"] or "",
            group.get("match_status") or "",
            group.get("ledger_check_status") or "",
        ),
    )


def status(store):
    current_issues = issues(store)
    return {
        "email_sync_checkpoint": store.all("""SELECT c.*,NOT EXISTS(SELECT 1 FROM email_source_item s
            WHERE s.source_id=c.source_id AND s.folder=c.folder AND s.uid_validity=c.uid_validity
            AND s.uid<=c.initial_scan_upper_uid AND s.status IN ('pending','failed')) AS historical_complete
            FROM email_sync_checkpoint c ORDER BY source_id,folder"""),
        "email_source_item": store.all(
            "SELECT status,count(*) FROM email_source_item GROUP BY status"
        ),
        "email": store.all("SELECT parse_status,count(*) FROM email GROUP BY parse_status"),
        "bank_transactions": store.all(
            "SELECT import_status,count(*) FROM bank_transactions GROUP BY import_status"
        ),
        "background_task": store.all(
            "SELECT task_type,status,count(*) FROM background_task GROUP BY task_type,status"
        ),
        "issues": len(current_issues),
        "issue_object_count": len(
            {(item["entity_type"], item["entity_id"]) for item in current_issues}
        ),
        "issue_groups": issue_groups(current_issues),
    }


def restore_audit(store, ledger):
    if not store.lock_worker():
        raise Conflict("stop worker before restore audit")
    recover_dispatching(store)
    with store.transaction():
        store.execute(
            f"UPDATE background_task SET status='unknown',error_code='write_unknown',last_error='restored backup requires verification' WHERE task_type IN {WRITE_TYPES} AND status='queued'"
        )
        store.execute("""UPDATE bank_transactions SET import_status='unknown' WHERE id IN
            (SELECT bank_transaction_id FROM background_task WHERE status='unknown' AND task_type='create')""")
    verify_unknown(store, ledger)
    return {
        "unknown": store.one("SELECT count(*) FROM background_task WHERE status='unknown'")[
            "count"
        ],
        "note": "旧备份缺失的来源需重新采集并核对，禁止未经核实启用写入",
    }
