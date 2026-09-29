import re
import logging
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..domain.errors import ImporterError, LedgerError, LogPersistenceError
from ..domain.money import cents
from ..domain.accounts import AccountMatchError, match_account, decision_currency
from .ports import Classifier, Ledger, Store
from .records import source_row
from .recheck import is_duplicate_recheck
from .events import emit, blocked, Progress, failure_fields


def effective(mapping, day: date) -> bool:
    return mapping.valid_from <= day and (mapping.valid_until is None or day <= mapping.valid_until)


def category_valid(category: dict, category_type: int) -> bool:
    return (
        int(category.get("type", 0)) == category_type
        and not category.get("hidden", False)
        and str(category.get("parentId", "0")) not in {"", "0", "None"}
    )


def validate_accounts(ledger: Ledger, payload: dict, currency: str):
    accounts = {str(a["id"]): a for a in ledger.accounts()}
    for key in ("sourceAccountId", "destinationAccountId"):
        if key not in payload or str(payload[key]) == "0":
            continue
        account = accounts.get(str(payload[key]))
        if (
            not account
            or account.get("hidden")
            or int(account.get("type", 0)) != 1
            or account.get("currency") != currency
        ):
            raise ImporterError(
                "target account must be visible, recordable and use the frozen target currency"
            )


def validate_target(ledger: Ledger, payload: dict, currency: str):
    validate_accounts(ledger, payload, currency)
    categories = {str(c["id"]): c for c in ledger.categories()}
    category = categories.get(str(payload["categoryId"]))
    if not category or not category_valid(category, 3 if payload["type"] == 4 else 2):
        raise ImporterError("category missing, hidden or incompatible")


def classify_expense(transaction: dict, settings, ledger: Ledger, ai: Classifier | None):
    facts = source_row(transaction)
    categories = ledger.categories()
    eligible = [c for c in categories if category_valid(c, 2)]
    fallback = [c for c in eligible if c.get("path") == "其他杂项 → 待分类"]
    if len(fallback) != 1:
        raise ImporterError("fallback category path must resolve uniquely: 其他杂项 → 待分类")
    fallback_id = str(fallback[0]["id"])
    matched_rules = {
        r.category_id
        for r in settings.rules
        if re.search(r.merchant_pattern, facts["merchant_raw"])
    }
    if len(matched_rules) > 1:
        raise ImporterError("merchant rules conflict")
    if matched_rules:
        category_id = matched_rules.pop()
        classification = {
            "classification_status": "matched",
            "reason": "merchant_rule",
            "category_id": category_id,
        }
    elif ai and settings.classification_mode == "ai":
        classification = ai.classify(
            transaction["id"],
            facts["merchant_raw"],
            [c for c in eligible if str(c["id"]) != fallback_id],
        )
        category_id = (
            str(classification["category_id"])
            if classification["classification_status"] == "matched"
            else fallback_id
        )
    elif settings.classification_mode == "rules_only":
        category_id = fallback_id
        classification = {
            "classification_status": "unmatched",
            "category_id": None,
            "reason": "no matching rule; AI not configured",
        }
    else:
        raise ImporterError("AI configuration required unless classification_mode=rules_only")
    return category_id, classification


def repayment_mapping(facts: dict, settings):
    day = date.fromisoformat(facts["occurred_date"])
    mappings = [
        m
        for m in settings.repayments
        if m.currency == facts["original_currency"] and effective(m, day)
    ]
    if len(mappings) != 1:
        raise ImporterError("repayment requires one effective account mapping")
    return mappings[0]


def decide(transaction: dict, settings, ledger: Ledger, ai: Classifier | None) -> dict:
    facts = source_row(transaction)
    event = facts["event_type"]
    day = date.fromisoformat(facts["occurred_date"])
    if facts["occurred_at"]:
        instant = datetime.fromisoformat(facts["occurred_at"])
    elif settings.date_only_time:
        instant = datetime.combine(day, settings.date_only_time, ZoneInfo(settings.timezone))
    else:
        raise ImporterError("date_only_time requires an explicit bookkeeping convention")
    if instant.tzinfo is None:
        raise ImporterError("source time must have configured timezone")
    utc_offset = instant.utcoffset()
    if utc_offset is None:
        raise ImporterError("timezone offset missing")
    offset = int(utc_offset.total_seconds() // 60)
    amount = Decimal(facts["original_amount"])
    currency = facts["original_currency"]
    account_audit = None
    classification = None
    if event == "repayment":
        if not settings.repayment_ownership_confirmed:
            raise ImporterError("repayment cross-channel ownership requires configuration")
        if currency != "CNY" or amount <= 0:
            raise ImporterError(
                "repayment requires one effective CNY account pair and positive amount"
            )
        mapping = repayment_mapping(facts, settings)
        payload = {
            "type": 4,
            "sourceAccountId": mapping.source_account_id,
            "destinationAccountId": mapping.destination_account_id,
            "categoryId": mapping.category_id,
            "sourceAmount": cents(amount),
            "destinationAmount": cents(amount),
        }
    elif event in {"expense", "refund"}:
        if (event == "expense" and amount <= 0) or (event == "refund" and amount >= 0):
            raise ImporterError("event sign contradicts bank evidence")
        if currency not in {"CNY", "USD"}:
            raise ImporterError("unsupported original currency")
        account_id, account_audit = match_account(
            ledger.accounts(), facts["card_reference"], currency
        )
        source_amount = cents(amount)
        category_id, classification = classify_expense(transaction, settings, ledger, ai)
        payload = {
            "type": 3,
            "sourceAccountId": account_id,
            "categoryId": category_id,
            "sourceAmount": source_amount,
        }
    else:
        raise ImporterError("unsupported event type")
    source_marker = transaction["source_marker"]
    detail = facts["merchant_raw"]
    if facts["time_precision"] != "second":
        detail += " 来源仅提供日期"
    payload.update(
        {
            "time": int(instant.timestamp()),
            "utcOffset": offset,
            "comment": source_marker + " " + detail[: 254 - len(source_marker)],
            "clientSessionId": source_marker,
        }
    )
    validate_target(ledger, payload, currency)
    return {
        "payload": payload,
        "classification": classification,
        "rate_snapshot": None,
        "target_currency": currency,
        "account_match": account_audit,
    }


def refresh_classification(
    transaction: dict, settings, ledger: Ledger, ai: Classifier | None
) -> dict:
    saved = transaction["import_decision"]
    payload = dict(saved["payload"])
    if payload["type"] == 3:
        category_id, classification = classify_expense(transaction, settings, ledger, ai)
        payload["categoryId"] = category_id
    else:
        payload["categoryId"] = repayment_mapping(source_row(transaction), settings).category_id
        classification = saved.get("classification")
    validate_target(ledger, payload, decision_currency(transaction))
    # A retry refreshes only category choice. The previously persisted amount, quote,
    # time, source source_marker and any manually corrected account remain authoritative.
    return {
        **saved,
        "payload": payload,
        "classification": classification,
        "reclassify_requested": False,
    }


def _search_candidates(ledger, payload, search_results):
    window = (payload["time"] - 86400, payload["time"] + 86400)
    if window not in search_results:
        try:
            search_results[window] = ledger.search(*window)
        except LedgerError as exc:
            search_results[window] = exc
    result = search_results[window]
    if isinstance(result, LedgerError):
        raise result
    return result


def _save_duplicate_query_failure(store, transaction, import_decision, exc):
    with store.transaction():
        changed = store.execute(
            """UPDATE bank_transactions SET import_status='issue',import_decision=%s,import_error=%s
            WHERE id=%s AND decision_version=%s AND import_status='pending'""",
            (
                import_decision,
                {
                    "code": "duplicate_check_failed",
                    "detail": {
                        "error_type": type(exc).__name__,
                        "reason": "账本查重查询未完成；修复后执行 recheck 再检查一次",
                    },
                    "decision_version": transaction["decision_version"],
                },
                transaction["id"],
                transaction["decision_version"],
            ),
        )
    return ("duplicate_check_failed" if changed.rowcount else "unchanged"), {
        "error_type": type(exc).__name__
    }


def _classify_one(store, settings, ledger, ai, transaction, search_results):
    saved = transaction.get("import_decision") or {}
    rechecking = is_duplicate_recheck(transaction)
    if saved.get("payload") and saved.get("reclassify_requested") and not rechecking:
        import_decision = refresh_classification(transaction, settings, ledger, ai)
    else:
        import_decision = (
            saved if saved.get("payload") else decide(transaction, settings, ledger, ai)
        )
    classification = import_decision.get("classification") or {}
    category = (
        "decision_reused"
        if rechecking
        else None
        if not classification
        else (
            "rule_matched"
            if classification.get("reason") == "merchant_rule"
            else "ai_matched"
            if classification.get("classification_status") == "matched"
            else "unmatched"
        )
    )
    payload = import_decision["payload"]
    try:
        candidates = _search_candidates(ledger, payload, search_results)
    except LedgerError as exc:
        return _save_duplicate_query_failure(store, transaction, import_decision, exc)
    exact_sources = [
        c
        for c in candidates
        if re.search(
            r"(?<![\w-])" + re.escape(transaction["source_marker"]) + r"(?![\w-])",
            c.get("comment", ""),
        )
    ]
    if exact_sources:
        recovered = False
        with store.transaction():
            current = store.one(
                "SELECT * FROM bank_transactions WHERE id=%s FOR UPDATE",
                (transaction["id"],),
            )
            if (
                current
                and current["decision_version"] == transaction["decision_version"]
                and current["import_status"] == "pending"
            ):
                store.execute(
                    """INSERT INTO background_task(bank_transaction_id,task_type,decision_version,operation_key,payload,status,ledger_transaction_id)
                    VALUES (%s,'create',%s,%s,%s,'unknown',%s) ON CONFLICT(operation_key) DO UPDATE SET
                    status='unknown',decision_version=excluded.decision_version,payload=excluded.payload,ledger_transaction_id=excluded.ledger_transaction_id
                    WHERE background_task.status IN ('cancelled','rejected')""",
                    (
                        transaction["id"],
                        transaction["decision_version"],
                        transaction["source_marker"],
                        payload,
                        str(exact_sources[0]["id"]) if len(exact_sources) == 1 else None,
                    ),
                )
                store.execute(
                    "UPDATE bank_transactions SET import_status='unknown',import_decision=%s,import_error=NULL WHERE id=%s",
                    (import_decision, transaction["id"]),
                )
                recovered = True
                store.execute(
                    "UPDATE background_task SET error_code='write_unknown',last_error='persistent source marker requires verification' WHERE operation_key=%s AND decision_version=%s",
                    (transaction["source_marker"], transaction["decision_version"]),
                )
        return ("verification_pending" if recovered else "unchanged"), {
            "classification_count": category
        }
    duplicates = [
        c
        for c in candidates
        if str(c.get("sourceAccountId")) == str(payload["sourceAccountId"])
        and c.get("sourceAmount") == payload["sourceAmount"]
        and c.get("type") == payload["type"]
        and datetime.fromtimestamp(c.get("time", 0), ZoneInfo(settings.timezone)).date()
        == datetime.fromtimestamp(payload["time"], ZoneInfo(settings.timezone)).date()
        and source_row(transaction)["merchant_raw"] in c.get("comment", "")
    ]
    with store.transaction():
        current = store.one(
            "SELECT * FROM bank_transactions WHERE id=%s FOR UPDATE", (transaction["id"],)
        )
        if not current:
            raise ImporterError("transaction disappeared")
        if (
            current["decision_version"] != transaction["decision_version"]
            or current["import_status"] != "pending"
        ):
            return "unchanged", {}
        # A marker in remote text is not proof of another local source.
        # Read confirmed links inside the decision transaction so an unknown
        # or old marker cannot bypass duplicate review after an empty rebuild.
        if duplicates:
            linked_ids = {
                row["ledger_transaction_id"]
                for row in store.all(
                    """SELECT ledger_transaction_id FROM bank_transactions
                    WHERE id<>%s AND ledger_transaction_id IN
                    (SELECT value FROM jsonb_array_elements_text(%s))""",
                    (transaction["id"], [str(c["id"]) for c in duplicates]),
                )
            }
            duplicates = [c for c in duplicates if str(c["id"]) not in linked_ids]
        store.execute(
            "UPDATE bank_transactions SET import_decision=%s WHERE id=%s",
            (import_decision, transaction["id"]),
        )
        if duplicates and (
            rechecking or not (current.get("import_decision") or {}).get("allow_new")
        ):
            store.execute(
                "UPDATE bank_transactions SET import_status='issue' WHERE id=%s",
                (transaction["id"],),
            )
            store.execute(
                "UPDATE bank_transactions SET import_error=%s WHERE id=%s AND decision_version=%s",
                (
                    {
                        "code": "duplicate_candidates",
                        "detail": {
                            "decision_version": transaction["decision_version"],
                            "candidate_ids": [str(c["id"]) for c in duplicates],
                        },
                        "decision_version": transaction["decision_version"],
                    },
                    transaction["id"],
                    transaction["decision_version"],
                ),
            )
            return "duplicate_candidates", {
                "candidate_count": len(duplicates),
                "classification_count": category,
            }
        queued = store.execute(
            """INSERT INTO background_task(bank_transaction_id,task_type,decision_version,operation_key,payload)
            VALUES (%s,'create',%s,%s,%s) ON CONFLICT(operation_key) DO UPDATE SET
            decision_version=excluded.decision_version,payload=excluded.payload,status='queued',last_error=NULL,error_code=NULL
            WHERE background_task.status IN ('cancelled','rejected')""",
            (
                transaction["id"],
                transaction["decision_version"],
                transaction["source_marker"],
                payload,
            ),
        )
        store.execute(
            "UPDATE bank_transactions SET import_status='queued',import_error=NULL WHERE id=%s",
            (transaction["id"],),
        )
    return ("queued" if queued.rowcount else "unchanged"), {"classification_count": category}


def classify_pending(
    store: Store,
    settings,
    ledger: Ledger,
    ai: Classifier | None,
    *,
    transaction_ids: frozenset[str] | None = None,
):
    if transaction_ids == frozenset():
        return
    query = "SELECT * FROM bank_transactions WHERE import_status='pending'"
    params: tuple[str, ...] = ()
    if transaction_ids is not None:
        params = tuple(sorted(transaction_ids))
        placeholders = ",".join("%s" for _ in params)
        query += f" AND id IN ({placeholders})"
    transactions = store.all(query + " ORDER BY id", params)
    if not transactions:
        return
    progress = Progress("classification", len(transactions))
    search_results: dict[tuple[int, int], list[dict] | LedgerError] = {}
    blocked_objects = defaultdict(list)
    emit("classification_started", stage="classification", total=len(transactions))
    for transaction in transactions:
        try:
            outcome, fields = _classify_one(
                store, settings, ledger, ai, transaction, search_results
            )
        except LogPersistenceError:
            raise
        except Exception as exc:
            with store.transaction():
                changed = store.execute(
                    "UPDATE bank_transactions SET import_status='issue' WHERE id=%s AND decision_version=%s AND import_status='pending'",
                    (transaction["id"], transaction["decision_version"]),
                )
                if changed.rowcount:
                    code = (
                        exc.code if isinstance(exc, AccountMatchError) else "classification_failed"
                    )
                    store.execute(
                        "UPDATE bank_transactions SET import_error=%s WHERE id=%s AND decision_version=%s",
                        (
                            {
                                "code": code,
                                "detail": {
                                    "error_type": type(exc).__name__,
                                    "candidate_ids": exc.candidate_ids
                                    if isinstance(exc, AccountMatchError)
                                    else [],
                                    "reason": str(exc)
                                    if isinstance(exc, ImporterError)
                                    else "external dependency failed",
                                },
                                "decision_version": transaction["decision_version"],
                            },
                            transaction["id"],
                            transaction["decision_version"],
                        ),
                    )
            if changed.rowcount:
                if isinstance(exc, AccountMatchError):
                    blocked_objects[exc.code].append(
                        (transaction["id"], transaction["decision_version"])
                    )
                    emit(
                        "transaction_blocked",
                        level=logging.DEBUG,
                        transaction_id=transaction["id"],
                        decision_version=transaction["decision_version"],
                        reason_code=exc.code,
                        candidate_count=len(exc.candidate_ids),
                        next_action="inspect_issues",
                    )
                    progress.advance(blocked=1)
                else:
                    emit(
                        "classification_failed",
                        level=logging.ERROR,
                        transaction_id=transaction["id"],
                        decision_version=transaction["decision_version"],
                        **failure_fields(exc, "classification_failed", "classification"),
                    )
                    progress.advance(failed=1)
            continue
        if outcome == "verification_pending":
            emit(
                "existing_marker_found",
                transaction_id=transaction["id"],
                decision_version=transaction["decision_version"],
                next_action="verify_only",
            )
        elif outcome == "duplicate_check_failed":
            blocked_objects[outcome].append((transaction["id"], transaction["decision_version"]))
            emit(
                "duplicate_check_failed",
                level=logging.DEBUG,
                transaction_id=transaction["id"],
                decision_version=transaction["decision_version"],
                error_code=outcome,
                error_type=fields["error_type"],
                stage="duplicate_check",
                next_action="recheck",
            )
        elif outcome == "duplicate_candidates":
            blocked_objects[outcome].append((transaction["id"], transaction["decision_version"]))
            emit(
                "transaction_blocked",
                level=logging.DEBUG,
                transaction_id=transaction["id"],
                decision_version=transaction["decision_version"],
                reason_code=outcome,
                candidate_count=fields["candidate_count"],
                next_action="inspect_issues",
            )
        if outcome != "unchanged":
            counts = {outcome: 1}
            if fields.get("classification_count"):
                counts[fields["classification_count"]] = 1
            progress.advance(**counts)
    for reason, objects in blocked_objects.items():
        if reason == "duplicate_check_failed":
            emit(
                "duplicate_check_failed",
                level=logging.ERROR,
                stage="duplicate_check",
                error_code=reason,
                affected_count=len(objects),
                next_action="recheck",
            )
            continue
        blocked(
            "transaction_blocked",
            identity=repr(objects),
            reason_code=reason,
            affected_count=len(objects),
            next_action="inspect_issues",
        )
    if progress.counts.get("queued"):
        emit("write_tasks_queued", task_type="create", counts={"create": progress.counts["queued"]})
    progress.finish()
