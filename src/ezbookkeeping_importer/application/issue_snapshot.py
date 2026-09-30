"""单次问题命令共享的 JSON 快照与乐观并发前置条件。"""

from datetime import date, datetime
from decimal import Decimal
import math

from ..domain.errors import Conflict, ImporterError
from .maintenance import issues
from .issue_interaction import issue_detail, issue_actions

# SQL identifiers are fixed here, never taken from a snapshot or CLI argument.
ROW_QUERIES = {
    "email_source_item": "SELECT * FROM email_source_item WHERE id=%s",
    "email": "SELECT * FROM email WHERE id=%s",
    "bank_report": "SELECT * FROM bank_report WHERE report_key=%s",
    "bank_transactions": "SELECT * FROM bank_transactions WHERE id=%s",
    "background_task": "SELECT * FROM background_task WHERE id=%s",
    "bank_statement_reconciliation": "SELECT * FROM bank_statement_reconciliation WHERE id=%s",
}
ISSUE_KEYS = {"entity_type", "entity_id", "code", "detail", "version", "status", "context"}


def normalize_json(value):
    """数据库值采用稳定表示；不隐藏未知类型或非有限数值。"""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ImporterError("snapshot contains a non-finite number")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ImporterError("snapshot contains a non-finite decimal")
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return [normalize_json(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ImporterError("snapshot keys must be strings")
        return {key: normalize_json(item) for key, item in value.items()}
    raise ImporterError(f"unsupported snapshot value type: {type(value).__name__}")


def validate_snapshot(document, *, single=False):
    if not isinstance(document, dict) or set(document) != {"snapshot_version", "items"}:
        raise ImporterError("snapshot requires snapshot_version and items")
    if type(document["snapshot_version"]) is not int or document["snapshot_version"] != 1:
        raise ImporterError("unsupported snapshot_version")
    if not isinstance(document["items"], list):
        raise ImporterError("snapshot items must be an array")
    if single and len(document["items"]) != 1:
        raise ImporterError("this operation requires exactly one snapshot item")
    for item in document["items"]:
        if not isinstance(item, dict) or set(item) != {"issue", "state", "view"}:
            raise ImporterError("snapshot item requires issue, state and view")
        issue = item["issue"]
        if not isinstance(issue, dict) or set(issue) != ISSUE_KEYS:
            raise ImporterError("invalid snapshot issue fields")
        if not isinstance(issue["entity_type"], str) or issue["entity_type"] not in ROW_QUERIES:
            raise ImporterError("unsupported snapshot entity type")
        if any(
            not isinstance(issue[key], str) or not issue[key].strip()
            for key in ("entity_id", "code")
        ):
            raise ImporterError("snapshot entity_id and code must be nonempty strings")
        version = issue["version"]
        if version is not None and (type(version) is not int or version < 0):
            raise ImporterError("snapshot version must be a nonnegative integer or null")
        if issue["status"] is not None and not isinstance(issue["status"], str):
            raise ImporterError("invalid snapshot status")
        if not isinstance(issue["context"], dict):
            raise ImporterError("invalid snapshot context")
        state = item["state"]
        if not isinstance(state, dict) or set(state) != {"row", "related"}:
            raise ImporterError("invalid snapshot state")
        if not isinstance(state["row"], dict) or not isinstance(state["related"], dict):
            raise ImporterError("invalid snapshot state projection")
        if not isinstance(item["view"], dict):
            raise ImporterError("invalid snapshot view")
    return normalize_json(document)


def _state(store, kind, entity_id, *, lock=False):
    if lock and kind == "background_task":
        observed = store.one(ROW_QUERIES[kind], (entity_id,))
        if observed and observed["bank_transaction_id"]:
            # Match the worker's transaction -> task lock order.
            store.one(
                ROW_QUERIES["bank_transactions"] + " FOR UPDATE", (observed["bank_transaction_id"],)
            )
    row = store.one(ROW_QUERIES[kind] + (" FOR UPDATE" if lock else ""), (entity_id,))
    if row is None:
        raise Conflict("问题对象已经变化，请重新查询")
    related = {}
    if kind == "bank_transactions":
        related["tasks"] = store.all(
            "SELECT * FROM background_task WHERE bank_transaction_id=%s ORDER BY id",
            (entity_id,),
        )
    elif kind == "background_task" and row["bank_transaction_id"]:
        related["transaction"] = store.one(
            ROW_QUERIES["bank_transactions"], (row["bank_transaction_id"],)
        )
    return normalize_json({"row": row, "related": related})


def assert_snapshot_item(store, item, *, lock=False):
    """在写入事务内调用 lock=True；网络期间不持有锁。"""
    selected = item["issue"]
    state = _state(store, selected["entity_type"], selected["entity_id"], lock=lock)
    current = normalize_json(issues(store, selected["entity_type"], selected["entity_id"]))
    if selected not in current or state != item["state"]:
        raise Conflict("问题状态已经变化，请重新查询并明确选择")
    return state


def snapshot_issues(store, entity_type=None, entity_id=None, code=None, status=None):
    selected = issues(store, entity_type, entity_id)
    result = []
    for issue in selected:
        if code is not None and issue["code"] != code:
            continue
        if status is not None and issue["status"] != status:
            continue
        # A short row lock ties the displayed diagnostic and state to one observation.
        with store.transaction():
            state = _state(store, issue["entity_type"], issue["entity_id"], lock=True)
            detail = issue_detail(store, issue)
            result.append(
                {"issue": normalize_json(issue), "state": state, "view": normalize_json(detail)}
            )
    return {"snapshot_version": 1, "items": result}


def snapshot_candidates(store, ledger, snapshot, target_id=None):
    item = validate_snapshot(snapshot, single=True)["items"][0]
    issue = item["issue"]
    if issue["entity_type"] != "bank_transactions":
        raise ImporterError("candidate comparison requires a bank transaction")
    assert_snapshot_item(store, item)
    diagnostic = issue["detail"]
    ids = diagnostic.get("candidate_ids", []) if isinstance(diagnostic, dict) else []
    if target_id is not None:
        ids = [target_id]
    candidates = [{"id": str(key), "transaction": ledger.get(str(key))} for key in ids]
    accounts, categories = ledger.accounts(), ledger.categories()
    assert_snapshot_item(store, item)
    return normalize_json(
        {
            "issue": issue,
            "decision": item["state"]["row"].get("import_decision") or {},
            "candidates": candidates,
            "accounts": accounts,
            "categories": categories,
        }
    )


def resolve_snapshot(store, ledger, snapshot, action, reason, target_id=None, account_id=None):
    from .resolve import resolve

    item = validate_snapshot(snapshot, single=True)["items"][0]
    issue = item["issue"]
    if action not in {"retry", "ignore", "accept-source", "link", "confirm-new"}:
        raise ImporterError("unsupported resolution action")
    if not isinstance(reason, str) or not reason.strip():
        raise ImporterError("a reason is required")
    if (action == "link") != bool(target_id):
        raise ImporterError("target_id is required only for link")
    if account_id is not None and (
        action != "retry" or issue["entity_type"] != "bank_transactions"
    ):
        raise ImporterError("account_id requires bank transaction retry")
    state = assert_snapshot_item(store, item)
    row = state["row"]
    active = any(
        task["status"] in {"unknown", "dispatching"} for task in state["related"].get("tasks", [])
    )
    available = issue_actions(issue["entity_type"], row, issue["code"], active=active)
    account_correction = action == "retry" and bool(account_id) and "recheck" in available
    if action not in available and not account_correction:
        raise ImporterError("action is not available for this issue")
    return resolve(
        store,
        ledger,
        issue["entity_type"],
        issue["entity_id"],
        issue["version"],
        action,
        reason,
        target_id=target_id,
        account_id=account_id,
        code=issue["code"],
        expected_snapshot=item,
    )
