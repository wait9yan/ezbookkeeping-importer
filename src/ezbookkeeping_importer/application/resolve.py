from ..domain.errors import Conflict, ImporterError
from .ports import Ledger, Store
from .write import matches
from .classify import validate_accounts
from ..domain.accounts import decision_currency, legacy_cny_estimate


def resolve(
    store: Store,
    ledger: Ledger | None,
    issue_id: int,
    version: int,
    action: str,
    reason: str,
    target_id: str | None = None,
    account_id: str | None = None,
):
    if not reason.strip():
        raise ImporterError("a reason is required")
    issue = store.one("SELECT * FROM issues WHERE id=%s", (issue_id,))
    if not issue:
        raise ImporterError("issue not found")
    if (action == "link" or account_id) and ledger is None:
        raise ImporterError("ledger connection is required for linking or account correction")
    target = (
        ledger.get(target_id) if action == "link" and target_id and ledger is not None else None
    )
    if action == "link" and target is not None:
        transaction = store.one("SELECT * FROM transactions WHERE id=%s", (issue["entity_id"],))
        if transaction and (transaction.get("decision") or {}).get("payload"):
            assert ledger is not None
            validate_accounts(ledger, target, decision_currency(transaction))
    corrected_payload = None
    if account_id:
        transaction = store.one("SELECT * FROM transactions WHERE id=%s", (issue["entity_id"],))
        decision = (transaction or {}).get("decision") or {}
        if (
            action != "retry"
            or not decision.get("payload")
            or not transaction
            or transaction.get("target_id")
        ):
            raise ImporterError("account correction is allowed only before creation with retry")
        corrected_payload = {**decision["payload"], "sourceAccountId": account_id}
        assert ledger is not None
        validate_accounts(ledger, corrected_payload, decision_currency(transaction))
    with store.transaction():
        issue = store.one("SELECT * FROM issues WHERE id=%s FOR UPDATE", (issue_id,))
        if not issue:
            raise ImporterError("issue not found")
        if issue["resolved"]:
            raise Conflict("issue already resolved")
        entity_id = issue["entity_id"]
        transaction = store.one("SELECT * FROM transactions WHERE id=%s FOR UPDATE", (entity_id,))
        if not transaction:
            message = store.one("SELECT * FROM messages WHERE id=%s FOR UPDATE", (entity_id,))
            if not message or version != 1:
                raise ImporterError("this issue has no resolvable source message")
            if action == "accept-source" and issue["code"] == "source_acceptance":
                store.execute(
                    "UPDATE messages SET accepted=true,status='pending' WHERE id=%s", (entity_id,)
                )
            elif action == "ignore":
                store.execute("UPDATE messages SET status='ignored' WHERE id=%s", (entity_id,))
            elif action == "retry" and message["status"] in {"failed", "parsed"}:
                store.execute("UPDATE messages SET status='pending' WHERE id=%s", (entity_id,))
            else:
                raise ImporterError("this source issue supports ignore or corrected-parser retry")
        else:
            if version != transaction["version"]:
                raise Conflict("stale decision version")
            if transaction["state"] in {"dispatching", "unknown"}:
                store.audit("followup_intent", entity_id, {"action": action, "reason": reason})
                return {
                    "state": transaction["state"],
                    "result": "intent recorded; external outcome must be verified",
                }
            if transaction["state"] == "booked":
                raise Conflict("booked transactions are maintained in ezBookkeeping")
            if action == "link":
                payload = (transaction["decision"] or {}).get("payload")
                if (
                    not target
                    or not payload
                    or not matches(target, payload)
                    or target.get("time") != payload.get("time")
                ):
                    raise ImporterError("linked target identity, amount or time does not match")
            elif action == "confirm-new" and transaction["target_id"]:
                raise Conflict("linked transaction cannot acquire a new create intent")
            elif action not in {"ignore", "retry", "confirm-new"}:
                raise ImporterError("unsupported resolution action")
            store.execute(
                "UPDATE jobs SET status='cancelled' WHERE transaction_id=%s AND status='queued'",
                (entity_id,),
            )
            state = "ignored" if action == "ignore" else "booked" if action == "link" else "pending"
            decision = transaction["decision"] or {}
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
            failed_settlement = store.one(
                "SELECT * FROM jobs WHERE transaction_id=%s AND kind='settle_amount' AND status IN ('rejected','cancelled') ORDER BY id DESC LIMIT 1",
                (entity_id,),
            )
            if action == "retry" and failed_settlement:
                if not legacy_cny_estimate(transaction):
                    raise ImporterError("settlement retry is limited to legacy CNY estimates")
                state = "queued"
                store.execute(
                    "UPDATE jobs SET status='queued',version=%s,error=NULL WHERE id=%s",
                    (version + 1, failed_settlement["id"]),
                )
            elif action == "retry" and not transaction["target_id"] and decision.get("payload"):
                decision = {**decision, "reclassify_requested": True}
            store.execute(
                "UPDATE transactions SET version=version+1,state=%s,target_id=%s,decision=%s WHERE id=%s",
                (
                    state,
                    target_id if action == "link" else transaction["target_id"],
                    decision,
                    entity_id,
                ),
            )
        store.execute("UPDATE issues SET resolved=true WHERE id=%s", (issue_id,))
        store.audit(
            "issue_resolved",
            entity_id,
            {"issue_id": issue_id, "action": action, "reason": reason, "version": version},
        )
    return {"result": "decision saved", "action": action}
