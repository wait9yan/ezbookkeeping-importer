"""交互问题查询；动作规则由 resolve 共享，写入仍走原用例。"""

from ..domain.errors import Conflict
from .maintenance import issues
from .recheck import RECHECK_CODES


def resolution_actions(entity_type, row, *, active=False):
    if entity_type == "email_source_item":
        if row["status"] == "failed":
            return ["retry"]
        if row["status"] == "collected" and row["accepted_at"] is None:
            return ["accept-source"]
        return []
    if entity_type == "email":
        return ["retry", "ignore"]
    if entity_type == "bank_report":
        return ["retry"]
    if entity_type == "background_task":
        return (
            ["retry"]
            if row["status"] in {"queued", "rejected", "cancelled", "unknown", "dispatching"}
            else []
        )
    if entity_type != "bank_transactions" or row["ledger_transaction_id"]:
        return []
    if active:
        return ["retry"]
    if row.get("import_status") in {"unknown", "dispatching"}:
        return []
    actions = ["retry", "ignore"]
    if (row.get("import_decision") or {}).get("payload"):
        actions += ["link", "confirm-new"]
    return actions


def issue_detail(store, selected):
    current = next(
        (
            item
            for item in issues(store, selected["entity_type"], selected["entity_id"])
            if item["code"] == selected["code"]
        ),
        None,
    )
    if current != selected:
        raise Conflict("问题状态已经变化，请刷新后重新选择")
    tables = {
        "email_source_item": "id",
        "email": "id",
        "bank_report": "report_key",
        "bank_transactions": "id",
        "background_task": "id",
        "bank_statement_reconciliation": "id",
    }
    kind = selected["entity_type"]
    if kind not in tables:
        raise Conflict("不支持的问题对象")
    row = store.one(f"SELECT * FROM {kind} WHERE {tables[kind]}=%s", (selected["entity_id"],))
    if row is None:
        raise Conflict("问题对象已经变化，请刷新后重新选择")
    active = kind == "bank_transactions" and bool(
        store.one(
            "SELECT id FROM background_task WHERE bank_transaction_id=%s "
            "AND status IN ('dispatching','unknown') LIMIT 1",
            (row["id"],),
        )
    )
    actions = resolution_actions(kind, row, active=active)
    if kind == "bank_transactions" and row["import_status"] in {"pending", "queued", "ignored"}:
        actions = []
    decision = row.get("import_decision") or {}
    if (
        kind == "bank_transactions"
        and not active
        and row["import_status"] == "issue"
        and not row["ledger_transaction_id"]
        and decision.get("payload")
        and selected["code"] in RECHECK_CODES
    ):
        actions = ["recheck", *[a for a in actions if a != "retry"]]
    return {"issue": current, "actions": actions, "decision": decision, "active": active}


def issue_candidates(store, ledger, selected, target_id=None):
    detail = issue_detail(store, selected)
    diagnostic = selected.get("detail")
    ids = diagnostic.get("candidate_ids", []) if isinstance(diagnostic, dict) else []
    if target_id:
        ids = [target_id]
    candidates = [{"id": str(key), "transaction": ledger.get(str(key))} for key in ids]
    # Network I/O holds no row lock. Reject a stale comparison before returning it.
    issue_detail(store, selected)
    return {
        **detail,
        "candidates": candidates,
        "accounts": ledger.accounts(),
        "categories": ledger.categories(),
    }
