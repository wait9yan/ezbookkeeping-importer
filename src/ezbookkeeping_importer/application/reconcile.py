import hashlib
import json
import os
from pathlib import Path

from ..domain.money import cents
from decimal import Decimal
from .ports import Ledger, Store


def normalized(value: str) -> str:
    return "".join(value.split()).casefold()


def candidates_for(row: dict, transactions: list[dict]) -> list[dict]:
    if row["event_type"] == "statement" and "自动还款" in row["merchant_raw"]:
        return [
            t
            for t in transactions
            if t["facts"]["event_type"] == "repayment"
            and t["facts"]["occurred_date"] == row["occurred_date"]
            and Decimal(t["facts"]["original_amount"]) == abs(Decimal(row["settlement_amount"]))
        ]
    return [
        t
        for t in transactions
        if t["facts"]["event_type"] == row["event_type"]
        and t["facts"]["occurred_date"] == row["occurred_date"]
        and t["facts"]["card_reference"] == row["card_reference"]
        and Decimal(t["facts"]["original_amount"]) == Decimal(row["original_amount"])
        and normalized(t["facts"]["merchant_raw"]) == normalized(row["merchant_raw"])
    ]


def reconcile(store: Store, ledger: Ledger, report_dir: Path):
    report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    transactions = store.all("SELECT * FROM transactions")
    for report in store.all("SELECT * FROM reports WHERE kind='monthly'"):
        results = []
        rows = report["parsed"]["rows"]
        candidate_map = {r["row_key"]: candidates_for(r, transactions) for r in rows}
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
                if (
                    sum(transaction["id"] in {t["id"] for t in c} for c in candidate_map.values())
                    > 1
                ):
                    status = "ambiguous"
                elif not transaction["target_id"]:
                    status = "import_pending"
                else:
                    data["target_id"] = transaction["target_id"]
                    try:
                        current = ledger.get(transaction["target_id"])
                        amount = cents(Decimal(row["settlement_amount"]))
                        if transaction["facts"]["event_type"] == "repayment":
                            amount = abs(amount)
                        payload = (transaction["decision"] or {}).get("payload", {})
                        if current is None:
                            status = "target_missing"
                        elif str(current.get("sourceAccountId")) != str(
                            payload.get("sourceAccountId")
                        ) or current.get("type") != payload.get("type"):
                            status = (
                                "settlement_target_invalid"
                                if transaction["facts"]["original_currency"] != "CNY"
                                else "target_changed"
                            )
                        elif (
                            transaction["facts"]["original_currency"] != "CNY"
                            and transaction["facts"]["event_type"] == "expense"
                        ):
                            status = (
                                "matched"
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
                                )
                            status = "matched" if fields_match else "target_changed"
                        data["observed"] = current
                    except Exception as exc:
                        status = "query_failed"
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
                results.append({"status": status, **data})
        filename = hashlib.sha256(report["report_key"].encode()).hexdigest() + ".json"
        target = report_dir / filename
        temp = target.with_suffix(".tmp")
        fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(results, file, ensure_ascii=False, indent=2)
        temp.replace(target)
