import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..domain.money import cents
from ..domain.accounts import decision_currency, legacy_cny_estimate, recordable
from decimal import Decimal
from .ports import Ledger, Store


def normalized(value: str) -> str:
    return "".join(value.split()).casefold()


HISTORY_RECHECK_INTERVAL = timedelta(hours=1)
QUERY_RETRY_INTERVAL = timedelta(minutes=10)
CHECKPOINT_KEY = "reconciliation-checkpoint"
CHANGE_EVENTS = (
    "mail_parsed",
    "write_confirmed",
    "issue_resolved",
    "classification_decided",
    "existing_source_recovered",
    "write_interrupted",
)


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
        normalized(row["merchant_raw"]),
    )


def candidate_index(transactions: list[dict]) -> dict[tuple, list[dict]]:
    index: dict[tuple, list[dict]] = defaultdict(list)
    for transaction in transactions:
        index[match_key(transaction["facts"])].append(transaction)
    return index


def reconcile_if_due(
    store: Store, ledger: Ledger, report_dir: Path, now: datetime | None = None
) -> bool:
    now = now or datetime.now(timezone.utc)
    # Counting committed audit rows also detects an earlier sequence ID that commits
    # after a later one; a max-ID-only watermark would miss that concurrent change.
    placeholders = ",".join("%s" for _ in CHANGE_EVENTS)
    change = store.one(
        f"""SELECT
        (SELECT count(*) FROM audit_events WHERE event IN ({placeholders})) AS audit_count,
        (SELECT count(*) FROM reports) AS report_count""",
        CHANGE_EVENTS,
    )
    if change is None:
        raise RuntimeError("reconciliation change signal missing")
    checkpoint = store.one("SELECT * FROM jobs WHERE operation_key=%s", (CHECKPOINT_KEY,))
    if checkpoint:
        saved = checkpoint["payload"]
        if saved["change"] == change and now < datetime.fromisoformat(saved["next_check_at"]):
            return False
    queries_succeeded = reconcile(store, ledger, report_dir)
    interval = HISTORY_RECHECK_INTERVAL if queries_succeeded else QUERY_RETRY_INTERVAL
    payload = {
        "change": change,
        "completed_at": now.isoformat(),
        "next_check_at": (now + interval).isoformat(),
        "queries_succeeded": queries_succeeded,
    }
    with store.transaction():
        store.execute(
            """INSERT INTO jobs(kind,operation_key,payload,status)
            VALUES ('reconcile_checkpoint',%s,%s,'done') ON CONFLICT(operation_key)
            DO UPDATE SET payload=excluded.payload,status='done',updated_at=now()""",
            (CHECKPOINT_KEY, payload),
        )
    return True


def reconcile(store: Store, ledger: Ledger, report_dir: Path):
    report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    transactions = store.all("SELECT * FROM transactions")
    index = candidate_index(transactions)
    queries_succeeded = True
    accounts = None
    for report in store.all("SELECT * FROM reports WHERE kind='monthly'"):
        results = []
        rows = report["parsed"]["rows"]
        candidate_map = {r["row_key"]: index.get(match_key(r), []) for r in rows}
        candidate_counts = Counter(
            t["id"] for candidates in candidate_map.values() for t in candidates
        )
        for row in rows:
            data = {
                "report_key": report["report_key"],
                "row_key": row["row_key"],
                "cycle": report["parsed"]["metadata"],
            }
            candidates = candidate_map[row["row_key"]]
            status = "missing_daily_evidence"
            if row["event_type"] == "statement" and "自动还款" not in row["merchant_raw"]:
                status = "out_of_scope"
                data["reason"] = "monthly repayment/reward rows do not create transactions"
            elif len(candidates) > 1:
                status = "ambiguous"
            elif len(candidates) == 1:
                transaction = candidates[0]
                data["transaction_id"] = transaction["id"]
                # The inverse uniqueness check prevents two statement rows consuming one daily row.
                if candidate_counts[transaction["id"]] > 1:
                    status = "ambiguous"
                elif not transaction["target_id"]:
                    status = "import_pending"
                else:
                    data["target_id"] = transaction["target_id"]
                    try:
                        current = ledger.get(transaction["target_id"])
                        currency = decision_currency(transaction)
                        legacy_estimate = legacy_cny_estimate(transaction)
                        is_repayment = transaction["facts"]["event_type"] == "repayment"
                        data["bank_settlement"] = {
                            "amount": row["settlement_amount"],
                            "currency": row["settlement_currency"],
                        }
                        amount = cents(
                            Decimal(
                                row["settlement_amount"]
                                if legacy_estimate
                                or is_repayment
                                or currency == row["settlement_currency"]
                                else row["original_amount"]
                            )
                        )
                        if is_repayment:
                            amount = abs(amount)
                        data["comparison_currency"] = currency
                        data["comparison_amount"] = amount
                        payload = (transaction["decision"] or {}).get("payload", {})
                        if accounts is None:
                            accounts = {
                                str(account["id"]): account for account in ledger.accounts()
                            }
                        target_account = (
                            accounts.get(str(current.get("sourceAccountId"))) if current else None
                        )
                        if current is None:
                            status = "target_missing"
                        elif (
                            str(current.get("sourceAccountId"))
                            != str(payload.get("sourceAccountId"))
                            or current.get("type") != payload.get("type")
                            or not target_account
                            or not recordable(target_account)
                            or target_account.get("currency") != currency
                        ):
                            status = (
                                "settlement_target_invalid" if legacy_estimate else "target_changed"
                            )
                        elif legacy_estimate:
                            date_changed = current.get("time") != payload.get("time")
                            data["date_changed"] = date_changed
                            data["expected_time"] = payload.get("time")
                            status = (
                                "target_changed"
                                if date_changed
                                else "matched"
                                if current["sourceAmount"] == amount
                                else "target_changed"
                                if transaction["settlement"]
                                else "estimated_pending_settlement"
                            )
                            if not transaction["settlement"] and transaction["state"] == "booked":
                                key = f"settle:{transaction['id']}:{report['report_key']}:{row['row_key']}"
                                with store.transaction():
                                    locked = store.one(
                                        "SELECT * FROM transactions WHERE id=%s FOR UPDATE",
                                        (transaction["id"],),
                                    )
                                    if locked and locked["state"] == "booked":
                                        inserted = store.one(
                                            """INSERT INTO jobs(transaction_id,kind,version,operation_key,payload,target_id)
                                            VALUES (%s,'settle_amount',%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id""",
                                            (
                                                transaction["id"],
                                                locked["version"],
                                                key,
                                                {
                                                    "type": 3,
                                                    "sourceAccountId": payload["sourceAccountId"],
                                                    "sourceAmount": amount,
                                                },
                                                transaction["target_id"],
                                            ),
                                        )
                                        if inserted:
                                            store.execute(
                                                "UPDATE transactions SET state='queued' WHERE id=%s",
                                                (transaction["id"],),
                                            )
                                            store.audit(
                                                "settlement_queued", transaction["id"], data
                                            )
                        else:
                            fields_match = current["sourceAmount"] == amount and current.get(
                                "time"
                            ) == payload.get("time")
                            if transaction["facts"]["event_type"] == "repayment":
                                fields_match = (
                                    fields_match
                                    and str(current.get("destinationAccountId"))
                                    == str(payload.get("destinationAccountId"))
                                    and current.get("destinationAmount") == amount
                                    and bool(accounts.get(str(current.get("destinationAccountId"))))
                                    and accounts[str(current.get("destinationAccountId"))].get(
                                        "currency"
                                    )
                                    == currency
                                    and recordable(
                                        accounts[str(current.get("destinationAccountId"))]
                                    )
                                )
                            status = "matched" if fields_match else "target_changed"
                        data["observed"] = current
                    except Exception as exc:
                        status = "query_failed"
                        queries_succeeded = False
                        data["error_type"] = type(exc).__name__
            data["candidate_ids"] = [c["id"] for c in candidates]
            with store.transaction():
                store.execute(
                    """INSERT INTO reconciliation_items(report_key,row_key,status,data) VALUES (%s,%s,%s,%s)
                    ON CONFLICT(report_key,row_key) DO UPDATE SET status=excluded.status,data=excluded.data,observed_at=now()""",
                    (report["report_key"], row["row_key"], status, data),
                )
                issue_key = report["report_key"] + ":" + row["row_key"]
                if status not in {
                    "matched",
                    "out_of_scope",
                    "estimated_pending_settlement",
                    "import_pending",
                }:
                    store.issue("reconciliation", issue_key, {"status": status, **data})
                else:
                    store.execute(
                        "UPDATE issues SET resolved=true WHERE code='reconciliation' AND entity_id=%s",
                        (issue_key,),
                    )
            results.append({"status": status, **data})
        cycle_start = report["parsed"]["metadata"].get("cycle_start")
        cycle_end = report["parsed"]["metadata"].get("cycle_end")
        covered_ids = {t["id"] for candidates in candidate_map.values() for t in candidates}
        if cycle_start and cycle_end:
            for transaction in transactions:
                facts = transaction["facts"]
                if (
                    transaction["id"] in covered_ids
                    or not cycle_start <= facts["occurred_date"] <= cycle_end
                ):
                    continue
                reverse_key = "daily:" + transaction["id"]
                data = {
                    "transaction_id": transaction["id"],
                    "target_id": transaction["target_id"],
                    "cycle_start": cycle_start,
                    "cycle_end": cycle_end,
                }
                if facts["occurred_date"] == cycle_end:
                    status = "awaiting_statement"
                else:
                    status = "missing_statement_evidence"
                if transaction["target_id"]:
                    try:
                        data["observed"] = ledger.get(transaction["target_id"])
                        if data["observed"] is None:
                            status = "target_missing"
                    except Exception as exc:
                        status = "query_failed"
                        queries_succeeded = False
                        data["error_type"] = type(exc).__name__
                elif transaction["state"] not in {"ignored", "booked"}:
                    status = "import_pending"
                with store.transaction():
                    store.execute(
                        """INSERT INTO reconciliation_items(report_key,row_key,status,data)
                        VALUES (%s,%s,%s,%s) ON CONFLICT(report_key,row_key) DO UPDATE SET
                        status=excluded.status,data=excluded.data,observed_at=now()""",
                        (report["report_key"], reverse_key, status, data),
                    )
                    if status not in {"awaiting_statement", "import_pending"}:
                        store.issue(
                            "reconciliation",
                            report["report_key"] + ":" + reverse_key,
                            {"status": status, **data},
                        )
                    else:
                        store.execute(
                            "UPDATE issues SET resolved=true WHERE code='reconciliation' AND entity_id=%s",
                            (report["report_key"] + ":" + reverse_key,),
                        )
                results.append({"status": status, **data})
        filename = hashlib.sha256(report["report_key"].encode()).hexdigest() + ".json"
        target = report_dir / filename
        temp = target.with_suffix(".tmp")
        fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(results, file, ensure_ascii=False, indent=2)
        temp.replace(target)

    return queries_succeeded
