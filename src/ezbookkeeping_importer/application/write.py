"""本地领取是不可取消边界；不明确的 POST 结果永远不盲重发。"""

import re

from ..domain.errors import ImporterError, LedgerRejected
from .classify import validate_target, validate_accounts
from ..domain.accounts import decision_currency, legacy_cny_estimate
from .ports import Ledger, Store


def matches(current: dict | None, payload: dict) -> bool:
    if not current:
        return False
    fields = ["type", "sourceAccountId", "sourceAmount"]
    if payload["type"] == 4:
        fields += ["destinationAccountId", "destinationAmount"]
    return all(str(current.get(key)) == str(payload.get(key)) for key in fields)


def recover_dispatching(store: Store):
    with store.transaction():
        stale = store.all(
            "UPDATE jobs SET status='unknown',error='worker interrupted',updated_at=now() WHERE status='dispatching' AND kind IN ('create','settle_amount') RETURNING *"
        )
        for job in stale:
            store.execute(
                "UPDATE transactions SET state='unknown' WHERE id=%s", (job["transaction_id"],)
            )
            store.issue(
                "write_unknown",
                job["transaction_id"],
                {"job_id": job["id"], "version": job["version"]},
            )
            store.audit("write_interrupted", job["transaction_id"], {"job_id": job["id"]})
        store.execute(
            "UPDATE jobs SET status='queued' WHERE kind IN ('sync','sync_range') AND status='dispatching'"
        )


def complete(store: Store, job: dict, current: dict):
    with store.transaction():
        transaction = store.one(
            "SELECT * FROM transactions WHERE id=%s FOR UPDATE", (job["transaction_id"],)
        )
        latest = store.one("SELECT * FROM jobs WHERE id=%s FOR UPDATE", (job["id"],))
        if (
            not transaction
            or not latest
            or latest["status"] not in {"queued", "dispatching", "unknown"}
        ):
            return
        if (
            transaction["version"] != latest["version"]
            or job["version"] != latest["version"]
            or transaction["state"] not in {"queued", "dispatching", "unknown"}
        ):
            return
        if job["kind"] == "settle_amount" and transaction["target_id"] != str(current["id"]):
            return
        store.execute(
            "UPDATE jobs SET status='done',target_id=%s,error=NULL,updated_at=now() WHERE id=%s",
            (str(current["id"]), job["id"]),
        )
        if job["kind"] == "create":
            store.execute(
                "UPDATE transactions SET state='booked',target_id=%s WHERE id=%s",
                (str(current["id"]), job["transaction_id"]),
            )
        else:
            store.execute(
                "UPDATE transactions SET state='booked',settlement=%s WHERE id=%s",
                (
                    {"amount": job["payload"]["sourceAmount"], "job_id": job["id"]},
                    job["transaction_id"],
                ),
            )
        store.execute(
            "UPDATE write_attempts SET outcome='confirmed',response=%s WHERE job_id=%s AND outcome='unknown'",
            (current, job["id"]),
        )
        store.execute(
            "UPDATE issues SET resolved=true WHERE entity_id=%s AND code IN ('write_unknown','write_rejected','verification_failed')",
            (job["transaction_id"],),
        )
        # Versionless legacy issues can be attributed only while the operation is still
        # on its first version. Reused jobs cannot prove which older decision failed.
        store.execute(
            """UPDATE issues SET resolved=true WHERE entity_id=%s AND code='write_preflight_failed'
            AND data->>'job_id'=%s AND (data->>'version'=%s OR (NOT (data ? 'version') AND %s=1))""",
            (job["transaction_id"], str(job["id"]), str(job["version"]), job["version"]),
        )
        store.audit(
            "write_confirmed",
            job["transaction_id"],
            {"job_id": job["id"], "target_id": str(current["id"])},
        )


def verify_unknown(store: Store, ledger: Ledger):
    for job in store.all("SELECT * FROM jobs WHERE status='unknown' ORDER BY id"):
        try:
            transaction = store.one(
                "SELECT * FROM transactions WHERE id=%s", (job["transaction_id"],)
            )
            if not transaction:
                raise ImporterError("missing transaction for write operation")
            currency = decision_currency(transaction)
            if job["kind"] == "settle_amount" and not legacy_cny_estimate(transaction):
                raise ImporterError("settlement is limited to legacy CNY estimates")
            if job["target_id"]:
                current = ledger.get(job["target_id"])
            else:
                payload = job["payload"]
                marker = transaction["marker"]
                candidates = ledger.search(payload["time"] - 86400, payload["time"] + 86400, marker)
                candidates = [
                    c
                    for c in candidates
                    if re.search(
                        r"(?<![\w-])" + re.escape(marker) + r"(?![\w-])", c.get("comment", "")
                    )
                ]
                if len(candidates) != 1:
                    store.issue(
                        "write_unknown",
                        job["transaction_id"],
                        {
                            "job_id": job["id"],
                            "candidate_ids": [str(c["id"]) for c in candidates],
                            "version": job["version"],
                        },
                    )
                    continue
                current = ledger.get(str(candidates[0]["id"]))
            if (
                current
                and matches(current, job["payload"])
                and (job["kind"] != "create" or current.get("time") == job["payload"].get("time"))
            ):
                validate_accounts(ledger, current, currency)
                complete(store, job, current)
            else:
                store.issue(
                    "verification_failed",
                    job["transaction_id"],
                    {"job_id": job["id"], "target_id": job["target_id"]},
                )
        except Exception as exc:
            store.issue(
                "verification_failed",
                job["transaction_id"],
                {"job_id": job["id"], "error_type": type(exc).__name__},
            )


def record_preflight_failure(store: Store, candidate: dict, error: Exception):
    with store.transaction():
        transaction = store.one(
            "SELECT * FROM transactions WHERE id=%s FOR UPDATE", (candidate["transaction_id"],)
        )
        current = store.one("SELECT * FROM jobs WHERE id=%s FOR UPDATE", (candidate["id"],))
        if not transaction or not current or current["status"] != "queued":
            return
        if transaction["state"] != "queued" or not (
            transaction["version"] == current["version"] == candidate["version"]
        ):
            return
        store.issue(
            "write_preflight_failed",
            candidate["transaction_id"],
            {
                "job_id": candidate["id"],
                "version": candidate["version"],
                "error_type": type(error).__name__,
            },
        )


def write_queued(store: Store, ledger: Ledger, enabled: bool):
    verify_unknown(store, ledger)
    if not enabled:
        return
    for candidate in store.all(
        "SELECT * FROM jobs WHERE status='queued' AND kind IN ('create','settle_amount') ORDER BY id"
    ):
        try:
            transaction = store.one(
                "SELECT * FROM transactions WHERE id=%s", (candidate["transaction_id"],)
            )
            if not transaction:
                raise ImporterError("missing transaction for write operation")
            currency = decision_currency(transaction)
            payload = candidate["payload"]
            if candidate["kind"] == "settle_amount":
                if not legacy_cny_estimate(transaction):
                    raise ImporterError("settlement is limited to legacy CNY estimates")
                current = ledger.get(candidate["target_id"])
                if (
                    not current
                    or current.get("type") != 3
                    or str(current.get("sourceAccountId")) != str(payload["sourceAccountId"])
                ):
                    raise ImporterError("settlement_target_invalid")
                validate_accounts(ledger, current, currency)
                if matches(current, payload):
                    complete(store, candidate, current)
                    continue
                payload = ledger.settlement_payload(current, payload["sourceAmount"])
            validate_target(ledger, payload, currency)
        except Exception as exc:
            record_preflight_failure(store, candidate, exc)
            continue
        with store.transaction():
            transaction = store.one(
                "SELECT * FROM transactions WHERE id=%s FOR UPDATE", (candidate["transaction_id"],)
            )
            job = store.one("SELECT * FROM jobs WHERE id=%s FOR UPDATE", (candidate["id"],))
            if not transaction or not job:
                raise ImporterError("missing write operation")
            if (
                job["status"] != "queued"
                or transaction["version"] != job["version"]
                or candidate["version"] != job["version"]
                or transaction["state"] != "queued"
            ):
                continue
            store.execute(
                "UPDATE jobs SET status='dispatching',payload=%s,updated_at=now() WHERE id=%s",
                (payload, job["id"]),
            )
            store.execute(
                "UPDATE transactions SET state='dispatching' WHERE id=%s", (transaction["id"],)
            )
            attempt = store.one(
                "INSERT INTO write_attempts(job_id,request,outcome) VALUES (%s,%s,'unknown') RETURNING id",
                (job["id"], payload),
            )
            if not attempt:
                raise ImporterError("attempt insert failed")
            store.audit(
                "write_dispatching",
                transaction["id"],
                {"job_id": job["id"], "attempt_id": attempt["id"]},
            )
        job["payload"] = payload
        try:
            result = ledger.create(payload) if job["kind"] == "create" else ledger.modify(payload)
        except LedgerRejected as exc:
            with store.transaction():
                store.execute(
                    "UPDATE jobs SET status='rejected',error=%s WHERE id=%s", (str(exc), job["id"])
                )
                store.execute(
                    "UPDATE transactions SET state='issue' WHERE id=%s", (transaction["id"],)
                )
                store.execute(
                    "UPDATE write_attempts SET outcome='rejected' WHERE id=%s", (attempt["id"],)
                )
                store.issue(
                    "write_rejected",
                    transaction["id"],
                    {"job_id": job["id"], "version": job["version"], "reason": str(exc)},
                )
            continue
        except Exception as exc:
            result = None
            failure = type(exc).__name__
        else:
            failure = "awaiting readback"
        with store.transaction():
            target_id = (
                str(result["id"])
                if isinstance(result, dict) and result.get("id")
                else job["target_id"]
            )
            store.execute(
                "UPDATE jobs SET status='unknown',target_id=%s,error=%s WHERE id=%s",
                (target_id, failure, job["id"]),
            )
            store.execute(
                "UPDATE transactions SET state='unknown' WHERE id=%s", (transaction["id"],)
            )
            store.issue(
                "write_unknown", transaction["id"], {"job_id": job["id"], "version": job["version"]}
            )
    verify_unknown(store, ledger)
