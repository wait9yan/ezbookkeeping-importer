"""月账单完整集合计算、输入重验和原子发布。网络期间不持数据库锁。"""

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from ..domain.accounts import decision_currency, legacy_cny_estimate, match_account, recordable
from ..domain.errors import Conflict, ImporterError
from ..domain.money import cents
from .records import source_row
from .ports import Ledger, Store

HISTORY_RECHECK_INTERVAL = timedelta(hours=1)
QUERY_RETRY_INTERVAL = timedelta(minutes=10)


def normalized(value: str) -> str:
    return "".join(value.split()).casefold()


def merchant_identity(value: str) -> str:
    # CMB's observed GOOGLE*CHATGPT descriptor carries a telephone or location
    # suffix in daily mail; only this concrete template is shortened, never arbitrary prefixes.
    compact = normalized(value)
    if re.fullmatch(
        r"google\*chatgpt(?:[0-9]{3}-[0-9]{3}-[0-9]{4}caus|mountainviewcaus)?", compact
    ):
        return "google*chatgpt"
    return compact


def match_key(row: dict) -> tuple:
    if row["event_type"] == "repayment":
        return ("repayment", row["occurred_date"], Decimal(row["original_amount"]))
    if row["event_type"] == "statement" and "自动还款" in row["merchant_raw"]:
        return ("repayment", row["occurred_date"], abs(Decimal(row["settlement_amount"])))
    return (
        row["event_type"],
        row["occurred_date"],
        (row["card_reference"] or "")[-4:],
        Decimal(row["original_amount"]),
        merchant_identity(row["merchant_raw"]),
    )


def candidate_index(transactions: list[dict]) -> dict[tuple, list[dict]]:
    index: dict[tuple, list[dict]] = defaultdict(list)
    for transaction in transactions:
        index[match_key(source_row(transaction))].append(transaction)
    return index


def snapshot(store: Store, report_key: str) -> tuple[dict, list[dict], str]:
    report = store.one("SELECT * FROM bank_report WHERE report_key=%s", (report_key,))
    if not report or report["report_type"] != "monthly":
        raise ImporterError("reconciliation requires an accepted monthly report")
    transactions = store.all(
        """SELECT t.* FROM bank_transactions t JOIN bank_report r ON r.report_key=t.report_key
        WHERE r.source_id=%s ORDER BY t.id""",
        (report["source_id"],),
    )
    # Include complete membership and decisions, but not report scheduling timestamps.
    stable = {
        "report": {
            k: report[k]
            for k in ("report_key", "content_fingerprint", "period_start", "period_end")
        },
        "transactions": transactions,
    }
    fingerprint = hashlib.sha256(
        json.dumps(stable, sort_keys=True, default=str).encode()
    ).hexdigest()
    return report, transactions, fingerprint


def result_item(
    direction: str, row_key: str | None, transaction: dict | None, match_status: str, now: datetime
) -> dict:
    return {
        "check_direction": direction,
        "statement_row_key": row_key,
        "bank_transaction_id": transaction["id"] if transaction else None,
        "match_status": match_status,
        "ledger_check_status": "not_checked",
        "expected_amount": None,
        "expected_currency": None,
        "actual_amount": None,
        "actual_currency": None,
        "checked_at": now,
        "ledger_observed_at": None,
        "details": {},
        "last_error": None,
    }


def compare_ledger(
    ledger: Ledger, transaction: dict, item: dict, row: dict | None, now: datetime
) -> tuple[dict | None, list[dict] | None]:
    decision = transaction["import_decision"] or {}
    payload = decision.get("payload")
    if not payload:
        return None, None
    currency = decision_currency(transaction)
    expected = Decimal(payload["sourceAmount"]) / 100
    settlement = row and row.get("settlement_amount") is not None and row.get("settlement_currency")
    if (
        row is not None
        and settlement
        and (
            currency == row["settlement_currency"]
            or legacy_cny_estimate(transaction)
            or transaction["original_currency"] == "USD"
            and row["settlement_currency"] == "CNY"
        )
    ):
        expected = Decimal(row["settlement_amount"])
        currency = row["settlement_currency"]
        if transaction["event_type"] == "repayment":
            expected = abs(expected)
    item.update(expected_amount=expected, expected_currency=currency)
    if not transaction["ledger_transaction_id"]:
        return None, None
    try:
        current = ledger.get(transaction["ledger_transaction_id"])
        accounts = ledger.accounts() if current else []
    except Exception as exc:
        item.update(ledger_check_status="query_failed", last_error=type(exc).__name__)
        return None, None
    if not current:
        item["ledger_check_status"] = "target_missing"
        return None, accounts
    item["ledger_observed_at"] = now
    item["details"]["observed_ledger_transaction_id"] = str(current["id"])
    account_map = {str(a["id"]): a for a in accounts}
    account = account_map.get(str(current.get("sourceAccountId")))
    actual_currency = account.get("currency") if account else None
    actual_amount = (
        Decimal(current["sourceAmount"]) / 100
        if isinstance(current.get("sourceAmount"), int)
        else None
    )
    item.update(actual_currency=actual_currency, actual_amount=actual_amount)
    if not actual_currency or actual_amount is None:
        item["ledger_check_status"] = "not_comparable"
        item["details"]["reason"] = "remote currency or amount unavailable"
        return current, accounts
    differences: dict = {}
    for field in ("type", "time", "sourceAccountId"):
        if str(current.get(field)) != str(payload.get(field)):
            differences[field] = {"expected": payload.get(field), "actual": current.get(field)}
    if actual_currency != currency:
        differences["currency"] = {"expected": currency, "actual": actual_currency}
    elif actual_amount != expected:
        differences["amount"] = {
            "expected": str(expected),
            "actual": str(actual_amount),
            "difference": str(actual_amount - expected),
        }
    if not account or not recordable(account):
        differences["account_visibility"] = "missing or hidden account"
    if payload["type"] == 4:
        for field, expected_value in [
            ("destinationAccountId", payload["destinationAccountId"]),
            ("destinationAmount", cents(expected)),
        ]:
            if str(current.get(field)) != str(expected_value):
                differences[field] = {"expected": expected_value, "actual": current.get(field)}
        destination = account_map.get(str(current.get("destinationAccountId")))
        if (
            not destination
            or not recordable(destination)
            or destination.get("currency") != currency
        ):
            differences["destination_currency"] = (
                "missing, hidden or incompatible destination account"
            )
    item["details"]["differences"] = differences
    item["ledger_check_status"] = "mismatched" if differences else "matched"
    return current, accounts


def settlement_intent(
    transaction: dict, report: dict, row: dict, accounts: list[dict] | None, item: dict
) -> dict | None:
    if (
        transaction["import_status"] != "booked"
        or transaction["settlement_adjustment"]
        or row.get("settlement_amount") is None
    ):
        return None
    currency = decision_currency(transaction)
    is_currency = (
        transaction["original_currency"] == "USD"
        and currency == "USD"
        and row["settlement_currency"] == "CNY"
    )
    if not is_currency and not legacy_cny_estimate(transaction):
        return None
    if accounts is None:
        return None
    decision = transaction["import_decision"]
    previous = decision["payload"]
    account_id = previous["sourceAccountId"]
    account_match = decision.get("account_match")
    if is_currency:
        try:
            account_id, account_match = match_account(accounts, transaction["card_reference"], "CNY")
        except ImporterError as exc:
            item["ledger_check_status"] = "mismatched"
            item["details"]["settlement_error"] = str(exc)
            return None
    desired = {
        "type": 3,
        "sourceAccountId": account_id,
        "sourceAmount": cents(Decimal(row["settlement_amount"])),
    }
    return {
        "bank_transaction_id": transaction["id"],
        "task_type": "settle_currency" if is_currency else "settle_amount",
        "decision_version": transaction["decision_version"],
        "operation_key": f"settle:{transaction['id']}:{report['report_key']}:{row['row_key']}",
        "ledger_transaction_id": transaction["ledger_transaction_id"],
        "payload": {
            "request": desired,
            "from": previous,
            "from_currency": currency,
            "target_currency": "CNY",
            "account_match": account_match,
            "statement": {"report_key": report["report_key"], "row_key": row["row_key"]},
        },
    }


def compute_results(
    ledger: Ledger, report: dict, transactions: list[dict], now: datetime
) -> tuple[list[dict], list[dict], list[tuple]]:
    rows = report["content"]["rows"]
    keys = [r["row_key"] for r in rows]
    if len(keys) != len(set(keys)):
        raise ImporterError("statement row keys are not unique")
    index = candidate_index(transactions)
    candidate_map = {
        row["row_key"]: [
            t
            for t in index.get(match_key(row), [])
            if row.get("original_currency") is None
            or row["original_currency"] == t["original_currency"]
        ]
        for row in rows
    }
    counts = Counter(t["id"] for group in candidate_map.values() for t in group)
    results = []
    intents = []
    supplements = []
    for row in rows:
        candidates = candidate_map[row["row_key"]]
        transaction = None
        match_status = "missing_source_transaction"
        if row["event_type"] == "statement" and "自动还款" not in row["merchant_raw"]:
            match_status = "out_of_scope"
        elif len(candidates) > 1 or len(candidates) == 1 and counts[candidates[0]["id"]] > 1:
            match_status = "ambiguous"
        elif len(candidates) == 1:
            transaction = candidates[0]
            match_status = "matched"
        item = result_item(
            "statement_to_transaction", row["row_key"], transaction, match_status, now
        )
        item["details"]["candidate_ids"] = [t["id"] for t in candidates]
        if transaction:
            _, accounts = compare_ledger(ledger, transaction, item, row, now)
            intent = settlement_intent(transaction, report, row, accounts, item)
            if intent:
                intents.append(intent)
            if row.get("settlement_amount") is not None:
                supplements.append(
                    (
                        row.get("posted_date"),
                        row["settlement_amount"],
                        row["settlement_currency"],
                        transaction["id"],
                    )
                )
        results.append(item)
    covered = {t["id"] for group in candidate_map.values() for t in group}
    start, end = report["period_start"], report["period_end"]
    if start and end:
        for transaction in transactions:
            day = transaction["posted_date"] or transaction["occurred_date"]
            if transaction["id"] in covered or not start <= day <= end:
                continue
            item = result_item(
                "transaction_to_statement",
                None,
                transaction,
                "awaiting_statement" if day == end else "missing_statement_evidence",
                now,
            )
            compare_ledger(ledger, transaction, item, None, now)
            results.append(item)
    return results, intents, supplements


def publish(
    store: Store,
    report: dict,
    fingerprint: str,
    results: list[dict],
    intents: list[dict],
    supplements: list[tuple],
    now: datetime,
):
    with store.transaction():
        latest, _, current_fingerprint = snapshot(store, report["report_key"])
        if (
            latest["reconciliation_version"] != report["reconciliation_version"]
            or current_fingerprint != fingerprint
        ):
            raise Conflict("reconciliation inputs changed; discard stale complete set")
        row_keys = {row["row_key"] for row in report["content"]["rows"]}
        kept = []
        for item in results:
            if item["statement_row_key"] is not None and item["statement_row_key"] not in row_keys:
                raise ImporterError("reconciliation references a nonexistent statement row")
            columns = tuple(item)
            direction = item["check_direction"]
            identity = (
                "statement_row_key"
                if direction == "statement_to_transaction"
                else "bank_transaction_id"
            )
            assignments = ",".join(
                f"{column}=excluded.{column}"
                for column in columns
                if column not in {"check_direction", identity}
            )
            saved = store.one(
                f"""INSERT INTO bank_statement_reconciliation(statement_report_key,{",".join(columns)})
                VALUES (%s,{",".join("%s" for _ in columns)})
                ON CONFLICT(statement_report_key,{identity}) WHERE check_direction='{direction}'
                DO UPDATE SET {assignments} RETURNING id""",
                (report["report_key"], *(item[column] for column in columns)),
            )
            if not saved:
                raise ImporterError("reconciliation result could not be published")
            kept.append(saved["id"])
        store.execute(
            """DELETE FROM bank_statement_reconciliation WHERE statement_report_key=%s
            AND id NOT IN (SELECT value::bigint FROM jsonb_array_elements_text(%s))""",
            (report["report_key"], kept),
        )
        for posted, amount, currency, tid in supplements:
            store.execute(
                """UPDATE bank_transactions SET posted_date=COALESCE(posted_date,%s),
                bank_settlement_amount=COALESCE(bank_settlement_amount,%s),bank_settlement_currency=COALESCE(bank_settlement_currency,%s)
                WHERE id=%s""",
                (posted, amount, currency, tid),
            )
        for intent in intents:
            active = store.one(
                "SELECT id FROM background_task WHERE bank_transaction_id=%s AND status IN ('queued','dispatching','unknown')",
                (intent["bank_transaction_id"],),
            )
            if active:
                continue
            store.execute(
                """INSERT INTO background_task(bank_transaction_id,task_type,decision_version,operation_key,ledger_transaction_id,payload)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(operation_key) DO NOTHING""",
                tuple(
                    intent[key]
                    for key in (
                        "bank_transaction_id",
                        "task_type",
                        "decision_version",
                        "operation_key",
                        "ledger_transaction_id",
                        "payload",
                    )
                ),
            )
        succeeded = all(item["ledger_check_status"] != "query_failed" for item in results)
        # Supplementing absent bank facts is part of this publication, so freeze the
        # resulting input to avoid an immediate redundant reconciliation cycle.
        _, _, published_fingerprint = snapshot(store, report["report_key"])
        store.execute(
            """UPDATE bank_report SET reconciliation_last_attempt_at=%s,reconciled_at=%s,
            reconciliation_next_check_at=%s,reconciliation_input_fingerprint=%s,reconciliation_queries_succeeded=%s,
            reconciliation_last_error=NULL,reconciliation_version=reconciliation_version+1 WHERE report_key=%s""",
            (
                now,
                now,
                now + (HISTORY_RECHECK_INTERVAL if succeeded else QUERY_RETRY_INTERVAL),
                published_fingerprint,
                succeeded,
                report["report_key"],
            ),
        )


def write_report(store: Store, report_key: str, report_dir: Path):
    report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    rows = store.all(
        "SELECT * FROM bank_statement_reconciliation WHERE statement_report_key=%s ORDER BY id",
        (report_key,),
    )
    target = report_dir / (hashlib.sha256(report_key.encode()).hexdigest() + ".json")
    temp = target.with_suffix(".tmp")
    fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(rows, file, ensure_ascii=False, indent=2, default=str)
    temp.replace(target)


def run_reports(
    store: Store, ledger: Ledger, report_dir: Path, now: datetime, force: bool
) -> tuple[bool, bool]:
    ran = False
    queries_succeeded = True
    for key in store.all(
        "SELECT report_key FROM bank_report WHERE report_type='monthly' ORDER BY report_key"
    ):
        with store.transaction():
            report, transactions, fingerprint = snapshot(store, key["report_key"])
        if (
            not force
            and report["reconciliation_input_fingerprint"] == fingerprint
            and report["reconciliation_next_check_at"]
            and now < report["reconciliation_next_check_at"]
        ):
            continue
        ran = True
        try:
            results, intents, supplements = compute_results(ledger, report, transactions, now)
            publish(store, report, fingerprint, results, intents, supplements, now)
        except Exception as exc:
            with store.transaction():
                # A late old attempt may not overwrite a newer publication's summary.
                store.execute(
                    """UPDATE bank_report SET reconciliation_last_attempt_at=%s,
                    reconciliation_next_check_at=%s,reconciliation_input_fingerprint=%s,
                    reconciliation_last_error=%s,reconciliation_queries_succeeded=false
                    WHERE report_key=%s AND reconciliation_version=%s""",
                    (
                        now,
                        now + QUERY_RETRY_INTERVAL,
                        fingerprint,
                        str(exc) if isinstance(exc, ImporterError) else type(exc).__name__,
                        report["report_key"],
                        report["reconciliation_version"],
                    ),
                )
            queries_succeeded = False
            continue
        queries_succeeded = queries_succeeded and all(
            item["ledger_check_status"] != "query_failed" for item in results
        )
        # File errors propagate after commit; callers must not mistake them for rollback.
        write_report(store, report["report_key"], report_dir)
    return ran, queries_succeeded


def reconcile_if_due(
    store: Store, ledger: Ledger, report_dir: Path, now: datetime | None = None
) -> bool:
    return run_reports(store, ledger, report_dir, now or datetime.now(timezone.utc), False)[0]


def reconcile(store: Store, ledger: Ledger, report_dir: Path):
    return run_reports(store, ledger, report_dir, datetime.now(timezone.utc), True)[1]
