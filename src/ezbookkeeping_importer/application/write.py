"""短事务冻结请求，网络外置；UNKNOWN 永远只核实原操作。"""

import re
import logging
import time
from collections import Counter
from datetime import datetime, timezone

from ..domain.errors import ImporterError, LedgerRejected, LogPersistenceError
from ..domain.accounts import decision_currency, legacy_cny_estimate
from .classify import validate_target, validate_accounts
from .ports import Ledger, Store
from .events import emit, blocked, failure_fields, failure_identity

WRITE_TYPES = "('create','settle_amount','settle_currency')"


def matches(current: dict | None, payload: dict) -> bool:
    if not current:
        return False
    fields = ["type", "sourceAccountId", "sourceAmount"]
    if payload["type"] == 4:
        fields += ["destinationAccountId", "destinationAmount"]
    return all(str(current.get(key)) == str(payload.get(key)) for key in fields)


def has_marker(current: dict, marker: str) -> bool:
    return bool(
        re.search(r"(?<![\w-])" + re.escape(marker) + r"(?![\w-])", current.get("comment", ""))
    )


def request_payload(job: dict) -> dict:
    return job["payload"].get("request", job["payload"])


def target_currency(job: dict, transaction: dict) -> str:
    return "CNY" if job["task_type"] == "settle_currency" else decision_currency(transaction)


def task_error(store: Store, job: dict, code: str, detail: str):
    return store.execute(
        """UPDATE background_task SET error_code=%s,last_error=%s,updated_at=now()
        WHERE id=%s AND decision_version=%s AND status IN ('queued','dispatching','unknown','rejected')""",
        (code, detail, job["id"], job["decision_version"]),
    ).rowcount


def recover_dispatching(store: Store):
    with store.transaction():
        stale = store.all(f"""UPDATE background_task SET status='unknown',error_code='write_unknown',
            last_error='worker interrupted',updated_at=now() WHERE status='dispatching' AND task_type IN {WRITE_TYPES} RETURNING *""")
        for job in stale:
            if job["task_type"] == "create":
                store.execute(
                    "UPDATE bank_transactions SET import_status='unknown' WHERE id=%s",
                    (job["bank_transaction_id"],),
                )
        store.execute(
            "UPDATE background_task SET status='queued',updated_at=now() WHERE task_type IN ('sync','sync_range') AND status='dispatching'"
        )

    if stale:
        emit(
            "write_interrupted_recovered",
            level=logging.WARNING,
            counts=dict(Counter(job["task_type"] for job in stale)),
            next_action="verify_only",
        )
    return len(stale)


def complete(store: Store, job: dict, current: dict, method: str | None = None):
    with store.transaction():
        transaction = store.one(
            "SELECT * FROM bank_transactions WHERE id=%s FOR UPDATE", (job["bank_transaction_id"],)
        )
        latest = store.one("SELECT * FROM background_task WHERE id=%s FOR UPDATE", (job["id"],))
        if (
            not transaction
            or not latest
            or latest["status"] not in {"queued", "dispatching", "unknown"}
        ):
            return
        if (
            transaction["decision_version"] != latest["decision_version"]
            or job["decision_version"] != latest["decision_version"]
        ):
            return
        if job["task_type"] != "create" and transaction["ledger_transaction_id"] != str(
            current["id"]
        ):
            raise ImporterError("settlement target identity changed")
        attempted = store.one(
            "SELECT id FROM ledger_write_attempt WHERE task_id=%s AND decision_version=%s LIMIT 1",
            (job["id"], job["decision_version"]),
        )
        method = method or ("write_verified" if attempted else "existing_link")
        store.execute(
            """UPDATE background_task SET status='done',ledger_transaction_id=%s,
            completion_method=%s,last_error=NULL,error_code=NULL,updated_at=now() WHERE id=%s""",
            (str(current["id"]), method, job["id"]),
        )
        if job["task_type"] == "create":
            store.execute(
                "UPDATE bank_transactions SET import_status='booked',ledger_transaction_id=%s,import_error=NULL WHERE id=%s",
                (str(current["id"]), transaction["id"]),
            )
        else:
            decision = transaction["import_decision"]
            payload = request_payload(job)
            updated_payload = {
                **decision["payload"],
                "sourceAccountId": payload["sourceAccountId"],
                "sourceAmount": payload["sourceAmount"],
            }
            adjustment = {
                "from": {
                    "account_id": decision["payload"]["sourceAccountId"],
                    "currency": decision_currency(transaction),
                    "amount": decision["payload"]["sourceAmount"],
                },
                "to": {
                    "account_id": payload["sourceAccountId"],
                    "currency": target_currency(job, transaction),
                    "amount": payload["sourceAmount"],
                },
                "task_id": job["id"],
                "ledger_transaction_id": str(current["id"]),
                "statement": job["payload"].get("statement"),
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
            }
            updated_decision = {
                **decision,
                "payload": updated_payload,
                "target_currency": target_currency(job, transaction),
            }
            if job["task_type"] == "settle_currency":
                updated_decision["account_match"] = job["payload"]["account_match"]
                updated_decision.pop("account_override", None)
            store.execute(
                """UPDATE bank_transactions SET import_status='booked',import_decision=%s,
                settlement_adjustment=%s,decision_version=decision_version+1 WHERE id=%s""",
                (updated_decision, adjustment, transaction["id"]),
            )
        store.execute(
            """UPDATE ledger_write_attempt SET outcome='confirmed',response=%s,verified_at=now()
            WHERE task_id=%s AND decision_version=%s AND outcome='unknown'""",
            (current, job["id"], job["decision_version"]),
        )

    event = {
        "write_verified": "write_verified",
        "existing_link": "existing_link_restored",
        "already_applied": "settlement_already_applied",
    }[method]
    emit(
        event,
        task_id=job["id"],
        task_type=job["task_type"],
        transaction_id=job["bank_transaction_id"],
        decision_version=job["decision_version"],
        ledger_transaction_id=str(current["id"]),
        completion_method=method,
        **({"attempt_id": attempted["id"]} if attempted else {}),
    )
    return True


def preserved_fields_match(ledger: Ledger, current: dict, payload: dict) -> bool:
    # The adapter's full modify projection is the canonical list of writable fields,
    # including pictures, rather than a second independently maintained field list.
    observed = ledger.settlement_payload(current, payload["sourceAmount"])
    return all(observed.get(key) == value for key, value in payload.items())


def verify_unknown(store: Store, ledger: Ledger):
    for job in store.all("SELECT * FROM background_task WHERE status='unknown' ORDER BY id"):
        try:
            transaction = store.one(
                "SELECT * FROM bank_transactions WHERE id=%s", (job["bank_transaction_id"],)
            )
            if not transaction:
                raise ImporterError("missing transaction for write operation")
            if transaction["decision_version"] != job["decision_version"]:
                raise ImporterError("stale unknown decision requires investigation")
            payload = request_payload(job)
            if job["ledger_transaction_id"]:
                current = ledger.get(job["ledger_transaction_id"])
            else:
                candidates = [
                    c
                    for c in ledger.search(
                        payload["time"] - 86400,
                        payload["time"] + 86400,
                        transaction["source_marker"],
                    )
                    if has_marker(c, transaction["source_marker"])
                ]
                if len(candidates) != 1:
                    raise ImporterError(
                        "source marker does not identify exactly one remote transaction"
                    )
                current = ledger.get(str(candidates[0]["id"]))
            if (
                not current
                or not matches(current, payload)
                or not has_marker(current, transaction["source_marker"])
            ):
                raise ImporterError("remote result does not match frozen operation")
            if job["task_type"] == "create" and current.get("time") != payload.get("time"):
                raise ImporterError("remote time differs from frozen creation")
            if job["task_type"] != "create" and not preserved_fields_match(
                ledger, current, payload
            ):
                raise ImporterError("remote settlement fields differ from frozen request")
            validate_accounts(ledger, current, target_currency(job, transaction))
            complete(store, job, current)
        except LogPersistenceError:
            raise
        except Exception as exc:
            with store.transaction():
                changed = task_error(
                    store,
                    job,
                    "verification_failed",
                    str(exc) if isinstance(exc, ImporterError) else type(exc).__name__,
                )

            if changed:
                blocked(
                    "write_verification_pending",
                    identity=str(job["id"]),
                    state=failure_identity(exc),
                    task_id=job["id"],
                    transaction_id=job["bank_transaction_id"],
                    decision_version=job["decision_version"],
                    next_action="verify_only",
                    **failure_fields(exc, "verification_failed", "verification"),
                )


def prepare_settlement(ledger: Ledger, job: dict, transaction: dict) -> tuple[dict, dict]:
    if job["task_type"] == "settle_amount" and not legacy_cny_estimate(transaction):
        raise ImporterError("settlement is limited to legacy CNY estimates")
    current = ledger.get(job["ledger_transaction_id"])
    if not current or str(current.get("id")) != transaction["ledger_transaction_id"]:
        raise ImporterError("settlement target missing")
    if current.get("type") != 3 or not has_marker(current, transaction["source_marker"]):
        raise ImporterError("settlement target identity invalid")
    desired = request_payload(job)
    if matches(current, desired):
        validate_accounts(ledger, current, target_currency(job, transaction))
        return desired, current
    previous = job["payload"].get("from", transaction["import_decision"]["payload"])
    if not matches(current, previous):
        raise ImporterError("settlement account or amount changed remotely")
    validate_accounts(ledger, current, decision_currency(transaction))
    payload = ledger.settlement_payload(current, desired["sourceAmount"])
    payload["sourceAccountId"] = desired["sourceAccountId"]
    return payload, current


def record_preflight_failure(store: Store, candidate: dict, error: Exception):
    changed = 0
    with store.transaction():
        current = store.one(
            "SELECT * FROM background_task WHERE id=%s FOR UPDATE", (candidate["id"],)
        )
        transaction = store.one(
            "SELECT * FROM bank_transactions WHERE id=%s", (candidate["bank_transaction_id"],)
        )
        if (
            current
            and transaction
            and current["status"] == "queued"
            and current["decision_version"]
            == candidate["decision_version"]
            == transaction["decision_version"]
        ):
            changed = task_error(
                store,
                current,
                "write_preflight_failed",
                str(error) if isinstance(error, ImporterError) else type(error).__name__,
            )

    if changed:
        blocked(
            "write_preflight_blocked",
            identity=str(candidate["id"]),
            state=failure_identity(error),
            task_id=candidate["id"],
            transaction_id=candidate["bank_transaction_id"],
            decision_version=candidate["decision_version"],
            next_action="inspect_issues",
            **failure_fields(error, "write_preflight_failed", "preflight"),
        )


def write_queued(store: Store, ledger: Ledger, *, transaction_ids: frozenset[str] | None = None):
    verify_unknown(store, ledger)
    if transaction_ids == frozenset():
        return
    query = f"SELECT * FROM background_task WHERE status='queued' AND task_type IN {WRITE_TYPES}"
    params: tuple = ()
    if transaction_ids is not None:
        params = tuple(sorted(transaction_ids))
        query += " AND bank_transaction_id IN (" + ",".join("%s" for _ in params) + ")"
    for candidate in store.all(query + " ORDER BY id", params):
        try:
            transaction = store.one(
                "SELECT * FROM bank_transactions WHERE id=%s", (candidate["bank_transaction_id"],)
            )
            if not transaction:
                raise ImporterError("missing transaction for write operation")
            if transaction["decision_version"] != candidate["decision_version"]:
                with store.transaction():
                    cancelled = store.execute(
                        "UPDATE background_task SET status='cancelled',updated_at=now() WHERE id=%s AND status='queued' AND decision_version=%s",
                        (candidate["id"], candidate["decision_version"]),
                    )
                if cancelled.rowcount:
                    emit(
                        "write_task_cancelled",
                        task_id=candidate["id"],
                        transaction_id=candidate["bank_transaction_id"],
                        reason_code="stale_decision",
                    )
                continue
            payload = request_payload(candidate)
            if candidate["task_type"] != "create":
                payload, current = prepare_settlement(ledger, candidate, transaction)
                if matches(current, payload):
                    complete(store, candidate, current, "already_applied")
                    continue
            validate_target(ledger, payload, target_currency(candidate, transaction))
        except LogPersistenceError:
            raise
        except Exception as exc:
            record_preflight_failure(store, candidate, exc)
            continue
        with store.transaction():
            transaction = store.one(
                "SELECT * FROM bank_transactions WHERE id=%s FOR UPDATE",
                (candidate["bank_transaction_id"],),
            )
            job = store.one(
                "SELECT * FROM background_task WHERE id=%s FOR UPDATE", (candidate["id"],)
            )
            if not transaction or not job:
                raise ImporterError("missing write operation")
            if (
                job["status"] != "queued"
                or transaction["decision_version"] != job["decision_version"]
                or candidate["decision_version"] != job["decision_version"]
            ):
                continue
            if transaction["import_status"] != (
                "queued" if job["task_type"] == "create" else "booked"
            ):
                continue
            frozen = (
                payload if job["task_type"] == "create" else {**job["payload"], "request": payload}
            )
            store.execute(
                "UPDATE background_task SET status='dispatching',payload=%s,error_code=NULL,last_error=NULL,updated_at=now() WHERE id=%s",
                (frozen, job["id"]),
            )
            if job["task_type"] == "create":
                store.execute(
                    "UPDATE bank_transactions SET import_status='dispatching' WHERE id=%s",
                    (transaction["id"],),
                )
            attempt = store.one(
                "INSERT INTO ledger_write_attempt(task_id,decision_version,request,outcome) VALUES (%s,%s,%s,'unknown') RETURNING id",
                (job["id"], job["decision_version"], payload),
            )
            if not attempt:
                raise ImporterError("attempt insert failed")
        emit(
            "write_attempt_registered",
            task_id=job["id"],
            attempt_id=attempt["id"],
            task_type=job["task_type"],
            transaction_id=job["bank_transaction_id"],
            decision_version=job["decision_version"],
        )
        job["payload"] = frozen
        request_started = time.monotonic()
        try:
            result = (
                ledger.create(payload) if job["task_type"] == "create" else ledger.modify(payload)
            )
        except LedgerRejected as exc:
            with store.transaction():
                store.execute(
                    "UPDATE background_task SET status='rejected',error_code='write_rejected',last_error=%s,updated_at=now() WHERE id=%s",
                    (str(exc), job["id"]),
                )
                if job["task_type"] == "create":
                    store.execute(
                        "UPDATE bank_transactions SET import_status='issue' WHERE id=%s",
                        (transaction["id"],),
                    )
                store.execute(
                    "UPDATE ledger_write_attempt SET outcome='rejected',error=%s,response_received_at=now() WHERE id=%s",
                    (str(exc), attempt["id"]),
                )
            emit(
                "write_rejected",
                level=logging.WARNING,
                task_id=job["id"],
                attempt_id=attempt["id"],
                error_code=str(exc.code),
                next_action="inspect_issues",
            )
            continue
        except LogPersistenceError:
            raise
        except Exception as exc:
            result = None
            failure = type(exc).__name__
        else:
            failure = "awaiting readback"
        with store.transaction():
            target_id = (
                str(result["id"])
                if isinstance(result, dict) and result.get("id")
                else job["ledger_transaction_id"]
            )
            store.execute(
                "UPDATE background_task SET status='unknown',ledger_transaction_id=%s,error_code='write_unknown',last_error=%s,updated_at=now() WHERE id=%s",
                (target_id, failure, job["id"]),
            )
            store.execute(
                "UPDATE ledger_write_attempt SET response=%s,error=%s,response_received_at=CASE WHEN %s THEN now() ELSE NULL END WHERE id=%s",
                (result, None if result else failure, result is not None, attempt["id"]),
            )
            if job["task_type"] == "create":
                store.execute(
                    "UPDATE bank_transactions SET import_status='unknown' WHERE id=%s",
                    (transaction["id"],),
                )
        if result is not None:
            emit(
                "write_response_received",
                level=logging.DEBUG,
                task_id=job["id"],
                attempt_id=attempt["id"],
                response_status="received",
                duration_ms=int((time.monotonic() - request_started) * 1000),
            )
        emit(
            "write_result_unknown",
            level=logging.WARNING,
            task_id=job["id"],
            attempt_id=attempt["id"],
            task_type=job["task_type"],
            error_type=failure if result is None else None,
            next_action="verify_only",
        )
    verify_unknown(store, ledger)
