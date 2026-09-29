from datetime import datetime, timezone

from ..domain.errors import Conflict, ImporterError
from ..domain.accounts import decision_currency
from .classify import validate_accounts
from .maintenance import issues
from .write import matches
from .ports import Ledger, Store


def resolve(
    store: Store,
    ledger: Ledger | None,
    entity_type: str,
    entity_id: str,
    version: int | None,
    action: str,
    reason: str,
    target_id: str | None = None,
    account_id: str | None = None,
    code: str | None = None,
):
    if not reason.strip():
        raise ImporterError("a reason is required")
    if entity_type not in {
        "email_source_item",
        "email",
        "bank_transactions",
        "background_task",
        "bank_report",
    }:
        raise ImporterError("unsupported resolution entity type")
    target = None
    corrected_payload = None
    if action == "link" or account_id:
        if ledger is None or entity_type != "bank_transactions":
            raise ImporterError("ledger connection and bank transaction required")
        transaction = store.one("SELECT * FROM bank_transactions WHERE id=%s", (entity_id,))
        if not transaction:
            raise ImporterError("transaction not found")
        if action == "link" and target_id:
            target = ledger.get(target_id)
            if target:
                validate_accounts(ledger, target, decision_currency(transaction))
        if account_id:
            decision = transaction["import_decision"] or {}
            if (
                action != "retry"
                or not decision.get("payload")
                or transaction["ledger_transaction_id"]
            ):
                raise ImporterError("account correction is allowed only before creation with retry")
            corrected_payload = {**decision["payload"], "sourceAccountId": account_id}
            validate_accounts(ledger, corrected_payload, decision_currency(transaction))
    resolution = {
        "action": action,
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
        "version": version,
        "code": code,
    }
    with store.transaction():
        current_issues = issues(store, entity_type, entity_id)
        if code:
            current_issues = [item for item in current_issues if item["code"] == code]
        if not current_issues:
            raise Conflict("current problem no longer exists")
        if entity_type == "email_source_item":
            item = store.one("SELECT * FROM email_source_item WHERE id=%s FOR UPDATE", (entity_id,))
            if (
                action == "accept-source"
                and item
                and item["status"] == "collected"
                and item["accepted_at"] is None
            ):
                store.execute(
                    "UPDATE email_source_item SET accepted_at=now(),acceptance_reason=%s,updated_at=now() WHERE id=%s",
                    (reason, entity_id),
                )
                store.execute(
                    "UPDATE email SET parse_status='pending' WHERE id=%s AND parse_status<>'ignored'",
                    (item["email_id"],),
                )
            elif action == "retry" and item and item["status"] == "failed":
                store.execute(
                    "UPDATE email_source_item SET status='pending',last_error=NULL,updated_at=now() WHERE id=%s",
                    (entity_id,),
                )
            else:
                raise ImporterError("source item supports acceptance or failed collection retry")
        elif entity_type == "email":
            if action not in {"ignore", "retry"}:
                raise ImporterError("email supports ignore or parser retry")
            store.execute(
                "UPDATE email SET parse_status=%s,last_resolution=%s WHERE id=%s",
                ("ignored" if action == "ignore" else "pending", resolution, entity_id),
            )
        elif entity_type == "bank_report":
            if action != "retry":
                raise ImporterError("report supports reconciliation retry")
            store.execute(
                "UPDATE bank_report SET reconciliation_next_check_at=now() WHERE report_key=%s",
                (entity_id,),
            )
        elif entity_type == "background_task":
            job = store.one("SELECT * FROM background_task WHERE id=%s FOR UPDATE", (entity_id,))
            if (
                not job
                or action != "retry"
                or (job["decision_version"] is not None and job["decision_version"] != version)
            ):
                raise Conflict("stale or unsupported task resolution")
            if job["status"] in {"unknown", "dispatching"}:
                store.execute(
                    "UPDATE background_task SET last_resolution=%s WHERE id=%s",
                    (resolution, entity_id),
                )
                return {
                    "result": "intent recorded; external outcome must be verified",
                    "status": job["status"],
                }
            if job["status"] not in {"queued", "rejected", "cancelled"}:
                raise Conflict("completed operation cannot be retried")
            if job["bank_transaction_id"]:
                transaction = store.one(
                    "SELECT * FROM bank_transactions WHERE id=%s FOR UPDATE",
                    (job["bank_transaction_id"],),
                )
                if not transaction or transaction["decision_version"] != version:
                    raise Conflict("stale decision version")
                store.execute(
                    "UPDATE bank_transactions SET decision_version=decision_version+1 WHERE id=%s",
                    (transaction["id"],),
                )
                if job["task_type"] == "create":
                    decision = {**transaction["import_decision"], "reclassify_requested": True}
                    store.execute(
                        "UPDATE bank_transactions SET import_status='pending',import_decision=%s WHERE id=%s",
                        (decision, transaction["id"]),
                    )
            store.execute(
                """UPDATE background_task SET status=%s,decision_version=CASE WHEN decision_version IS NULL THEN NULL ELSE decision_version+1 END,
                error_code=NULL,last_error=NULL,last_resolution=%s,updated_at=now() WHERE id=%s""",
                ("cancelled" if job["task_type"] == "create" else "queued", resolution, entity_id),
            )
        else:
            transaction = store.one(
                "SELECT * FROM bank_transactions WHERE id=%s FOR UPDATE", (entity_id,)
            )
            if not transaction or transaction["decision_version"] != version:
                raise Conflict("stale decision version")
            active = store.one(
                "SELECT * FROM background_task WHERE bank_transaction_id=%s AND status IN ('dispatching','unknown')",
                (entity_id,),
            )
            if active:
                store.execute(
                    "UPDATE bank_transactions SET last_resolution=%s WHERE id=%s",
                    (resolution, entity_id),
                )
                return {
                    "result": "intent recorded; external outcome must be verified",
                    "status": active["status"],
                }
            if transaction["ledger_transaction_id"]:
                raise Conflict(
                    "booked transactions are maintained in ezBookkeeping; resolve settlement task separately"
                )
            decision = transaction["import_decision"] or {}
            if action == "link":
                if (
                    not target
                    or not decision.get("payload")
                    or not matches(target, decision["payload"])
                    or target.get("time") != decision["payload"].get("time")
                ):
                    raise ImporterError("linked target identity, amount or time does not match")
            elif action not in {"ignore", "retry", "confirm-new"}:
                raise ImporterError("unsupported transaction action")
            if corrected_payload:
                decision = {
                    **decision,
                    "payload": corrected_payload,
                    "account_override": {
                        "account_id": account_id,
                        "currency": decision_currency(transaction),
                    },
                }
            if action == "confirm-new":
                decision = {**decision, "allow_new": True}
            elif action == "retry" and decision.get("payload"):
                decision = {**decision, "reclassify_requested": True}
            store.execute(
                "UPDATE background_task SET status='cancelled',updated_at=now() WHERE bank_transaction_id=%s AND status='queued'",
                (entity_id,),
            )
            store.execute(
                """UPDATE bank_transactions SET decision_version=decision_version+1,import_status=%s,
                ledger_transaction_id=%s,import_decision=%s,import_error=NULL,last_resolution=%s WHERE id=%s""",
                (
                    "ignored"
                    if action == "ignore"
                    else "booked"
                    if action == "link"
                    else "pending",
                    target_id if action == "link" else None,
                    decision,
                    resolution,
                    entity_id,
                ),
            )
    return {"result": "decision saved", "action": action}
