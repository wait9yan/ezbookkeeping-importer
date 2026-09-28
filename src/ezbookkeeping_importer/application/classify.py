import re
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..domain.errors import ImporterError
from ..domain.money import cents
from ..domain.accounts import AccountMatchError, match_account, decision_currency
from .ports import Classifier, Ledger, Store


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
    facts = transaction["facts"]
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
    facts = transaction["facts"]
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
        if event == "refund" and not settings.refund_ownership_confirmed:
            raise ImporterError("refund cross-channel ownership requires configuration")
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
    marker = transaction["marker"]
    detail = facts["merchant_raw"]
    if facts["time_precision"] != "second":
        detail += " 来源仅提供日期"
    payload.update(
        {
            "time": int(instant.timestamp()),
            "utcOffset": offset,
            "comment": marker + " " + detail[: 254 - len(marker)],
            "clientSessionId": marker,
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
    saved = transaction["decision"]
    payload = dict(saved["payload"])
    if payload["type"] == 3:
        category_id, classification = classify_expense(transaction, settings, ledger, ai)
        payload["categoryId"] = category_id
    else:
        payload["categoryId"] = repayment_mapping(transaction["facts"], settings).category_id
        classification = saved.get("classification")
    validate_target(ledger, payload, decision_currency(transaction))
    # A retry refreshes only category choice. The previously persisted amount, quote,
    # time, source marker and any manually corrected account remain authoritative.
    return {
        **saved,
        "payload": payload,
        "classification": classification,
        "reclassify_requested": False,
    }


def classify_pending(store: Store, settings, ledger: Ledger, ai: Classifier | None):
    for transaction in store.all("SELECT * FROM transactions WHERE state='pending' ORDER BY id"):
        try:
            saved = transaction.get("decision") or {}
            if saved.get("payload") and saved.get("reclassify_requested"):
                decision = refresh_classification(transaction, settings, ledger, ai)
            else:
                decision = (
                    saved if saved.get("payload") else decide(transaction, settings, ledger, ai)
                )
            payload = decision["payload"]
            candidates = ledger.search(payload["time"] - 86400, payload["time"] + 86400)
            exact_sources = [
                c
                for c in candidates
                if re.search(
                    r"(?<![\w-])" + re.escape(transaction["marker"]) + r"(?![\w-])",
                    c.get("comment", ""),
                )
            ]
            if exact_sources:
                with store.transaction():
                    current = store.one(
                        "SELECT * FROM transactions WHERE id=%s FOR UPDATE", (transaction["id"],)
                    )
                    if (
                        current
                        and current["version"] == transaction["version"]
                        and current["state"] == "pending"
                    ):
                        store.execute(
                            """INSERT INTO jobs(transaction_id,kind,version,operation_key,payload,status,target_id)
                            VALUES (%s,'create',%s,%s,%s,'unknown',%s) ON CONFLICT(operation_key) DO UPDATE SET
                            status='unknown',version=excluded.version,payload=excluded.payload,target_id=excluded.target_id
                            WHERE jobs.status IN ('cancelled','rejected')""",
                            (
                                transaction["id"],
                                transaction["version"],
                                transaction["marker"],
                                payload,
                                str(exact_sources[0]["id"]) if len(exact_sources) == 1 else None,
                            ),
                        )
                        store.execute(
                            "UPDATE transactions SET state='unknown',decision=%s WHERE id=%s",
                            (decision, transaction["id"]),
                        )
                        store.issue(
                            "write_unknown",
                            transaction["id"],
                            {
                                "version": transaction["version"],
                                "candidate_ids": [str(c["id"]) for c in exact_sources],
                                "reason": "persistent source marker already exists",
                            },
                        )
                        store.audit(
                            "existing_source_recovered",
                            transaction["id"],
                            {"matches": len(exact_sources)},
                        )
                continue
            duplicates = [
                c
                for c in candidates
                if str(c.get("sourceAccountId")) == str(payload["sourceAccountId"])
                and c.get("sourceAmount") == payload["sourceAmount"]
                and c.get("type") == payload["type"]
                and datetime.fromtimestamp(c.get("time", 0), ZoneInfo(settings.timezone)).date()
                == datetime.fromtimestamp(payload["time"], ZoneInfo(settings.timezone)).date()
                and transaction["facts"]["merchant_raw"] in c.get("comment", "")
                and not (
                    "ebki-" in c.get("comment", "")
                    and transaction["marker"] not in c.get("comment", "")
                )
            ]
            with store.transaction():
                current = store.one(
                    "SELECT * FROM transactions WHERE id=%s FOR UPDATE", (transaction["id"],)
                )
                if not current:
                    raise ImporterError("transaction disappeared")
                if current["version"] != transaction["version"] or current["state"] != "pending":
                    continue
                store.execute(
                    "UPDATE transactions SET decision=%s WHERE id=%s", (decision, transaction["id"])
                )
                if duplicates and not (current.get("decision") or {}).get("allow_new"):
                    store.execute(
                        "UPDATE transactions SET state='issue' WHERE id=%s", (transaction["id"],)
                    )
                    store.issue(
                        "duplicate_candidates",
                        transaction["id"],
                        {
                            "version": transaction["version"],
                            "candidate_ids": [str(c["id"]) for c in duplicates],
                        },
                    )
                    continue
                store.execute(
                    """INSERT INTO jobs(transaction_id,kind,version,operation_key,payload)
                    VALUES (%s,'create',%s,%s,%s) ON CONFLICT(operation_key) DO UPDATE SET
                    version=excluded.version,payload=excluded.payload,status='queued',error=NULL
                    WHERE jobs.status IN ('cancelled','rejected')""",
                    (transaction["id"], transaction["version"], transaction["marker"], payload),
                )
                store.execute(
                    "UPDATE transactions SET state='queued' WHERE id=%s", (transaction["id"],)
                )
                store.audit("classification_decided", transaction["id"], decision)
        except Exception as exc:
            with store.transaction():
                store.execute(
                    "UPDATE transactions SET state='issue' WHERE id=%s AND version=%s AND state='pending'",
                    (transaction["id"], transaction["version"]),
                )
                store.issue(
                    exc.code if isinstance(exc, AccountMatchError) else "classification_failed",
                    transaction["id"],
                    {
                        "version": transaction["version"],
                        **(
                            {"candidate_ids": exc.candidate_ids}
                            if isinstance(exc, AccountMatchError)
                            else {}
                        ),
                        "error_type": type(exc).__name__,
                        "reason": str(exc)
                        if isinstance(exc, ImporterError)
                        else "external dependency failed",
                    },
                )
