"""来源人工决定编排，复用共享候选与来源关联契约。"""

from datetime import datetime, timezone
from decimal import Decimal

from ..domain.accounts import match_account
from ..domain.errors import Conflict, ImporterError
from ..domain.money import cents
from .issue_interaction import issue_actions
from .source_issues import source_key, source_state
from .sources import (
    accept_daily, create_transaction, link_source, load_transactions, report_row,
    schedule_source, source_candidates,
)
from .statement_candidates import statement_windows


def _remote_decision(ledger, report, row, target_id):
    if report["report_type"] != "monthly" or row.get("settlement_currency") != "CNY":
        raise ImporterError("external source link requires monthly CNY settlement evidence")
    # An explicitly selected target need not share the merchant comment used by
    # automatic discovery. Keep the original date, amount and account constraints.
    target = ledger.get(target_id)
    windows = statement_windows(row)
    if (not target or str(target["id"]) != target_id or target.get("type") != 3
            or not any(start <= target["time"] <= end for start, end in windows)):
        raise ImporterError("linked target is not eligible for statement association")
    account_id, account_match = match_account(ledger.accounts(), row["card_reference"], "CNY")
    if str(target["sourceAccountId"]) != account_id:
        raise ImporterError("linked target must use the matching CNY account")
    amount = row.get("settlement_amount")
    if amount is None or target.get("sourceAmount") != cents(Decimal(amount)):
        raise ImporterError("linked target must match the signed CNY settlement amount")
    audit = {"windows": [list(window) for window in windows], "account_ids": [account_id],
             "observed_at": datetime.now(timezone.utc).isoformat()}
    payload = {key: target[key] for key in
               ("type", "time", "utcOffset", "sourceAmount", "sourceAccountId", "categoryId")}
    return {"amount_basis": "statement_settlement", "target_currency": "CNY",
            "payload": payload, "account_match": account_match,
            "duplicate_query": audit, "classification": {"reason": "manual_link"}}


def resolve_source(store, ledger, entity_id, version, action, reason, *, target_id=None,
                   transaction_id=None, expected_snapshot=None):
    from .issue_snapshot import assert_snapshot_item

    if not reason.strip() or action not in {"retry", "link", "ignore"}:
        raise ImporterError("source resolution requires a reason and retry, link or ignore")
    if bool(target_id) + bool(transaction_id) != (1 if action == "link" else 0):
        raise ImporterError("link requires exactly one remote or internal target")
    if expected_snapshot is None:
        raise ImporterError("source resolution requires a complete issue snapshot")
    assert_snapshot_item(store, expected_snapshot)
    report_key, row_key = source_key(entity_id)
    report = store.one("SELECT * FROM bank_report WHERE report_key=%s", (report_key,))
    row = report_row(report, row_key)
    decision = None
    if target_id:
        if ledger is None:
            raise ImporterError("external source link requires a ledger connection")
        decision = _remote_decision(ledger, report, row, target_id)
    resolution = {"action": action, "reason": reason,
                  "at": datetime.now(timezone.utc).isoformat(), "version": version}
    with store.transaction():
        assert_snapshot_item(store, expected_snapshot, lock=True)
        current = source_state(store, entity_id)["row"]
        if current["resolution_version"] != version:
            raise Conflict("source resolution version changed")
        if action not in issue_actions("transaction_source", current, expected_snapshot["issue"]["code"]):
            raise ImporterError("action is not available for this source")
        if action == "link":
            if target_id:
                existing = store.one("SELECT * FROM bank_transactions WHERE ledger_transaction_id=%s",
                                     (target_id,))
                if existing:
                    transaction_id = existing["id"]
                else:
                    transactions, _ = load_transactions(store, report["source_id"])
                    if source_candidates([row], transactions)[row_key]:
                        raise Conflict("link an existing internal candidate before associating a remote target")
                    transaction_id = create_transaction(store, report, row)
                    store.execute("""UPDATE bank_transactions SET import_status='booked',
                        import_decision=%s,ledger_transaction_id=%s,last_resolution=%s
                        WHERE id=%s""", (decision, target_id, resolution, transaction_id))
            link_source(store, report_key, row_key, transaction_id, resolution=resolution)
        else:
            store.execute("""UPDATE transaction_source SET resolution_version=resolution_version+1,
                last_resolution=%s,updated_at=now() WHERE report_key=%s AND row_key=%s""",
                          (resolution, report_key, row_key))
            if action == "retry" and report["report_type"] == "daily":
                accept_daily(store, report, row_keys=frozenset({row_key}))
            schedule_source(store, report["source_id"])
    return {"result": "decision saved", "action": action}
