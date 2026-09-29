"""扫描、下载和解析各有独立检查点；网络失败不能吞掉 UID。"""

import re
import logging
import time
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime

from ..domain.errors import ImporterError, LogPersistenceError
from ..domain.mail import bank_candidate, normalize_subject
from .ports import HEADER_BATCH_SIZE, Mail, Store
from .events import emit, Progress, failure_fields


def validate_scan_range(since: date | None, until: date | None):
    if (since is None) != (until is None):
        raise ImporterError("--since and --until must be supplied together")
    if since is not None and until is not None and (since > until or until == date.max):
        raise ImporterError("scan range must be ordered; --until must be before 9999-12-31")


def request_sync(store: Store, since: date | None = None, until: date | None = None) -> bool:
    validate_scan_range(since, until)
    payload = {"since": since.isoformat(), "until": until.isoformat()} if since and until else {}
    # Range requests must not be swallowed by the coalescing index for ordinary sync.
    # A single worker executes both kinds serially; identities deduplicate downloaded evidence.
    kind = "sync_range" if payload else "sync"
    with store.transaction():
        result = store.execute(
            """INSERT INTO background_task(task_type,operation_key,payload) VALUES (%s,%s,%s)
            ON CONFLICT(task_type) WHERE task_type='sync' AND status IN ('queued','dispatching') DO NOTHING RETURNING id""",
            (kind, str(uuid.uuid4()), payload),
        ).fetchone()
        return result is not None


def _without_header_comments(value: str) -> str:
    cleaned = []
    depth = 0
    quoted = False
    escaped = False
    for char in value:
        if escaped:
            if not depth:
                cleaned.append(char)
            escaped = False
            continue
        if char == "\\" and (depth or quoted):
            if not depth:
                cleaned.append(char)
            escaped = True
            continue
        if depth:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            continue
        if char == '"':
            quoted = not quoted
        elif not quoted and char == "(":
            depth = 1
            cleaned.append(" ")
            continue
        elif not quoted and char == ")":
            raise ValueError("unbalanced header comment")
        cleaned.append(char)
    if depth or quoted or escaped:
        raise ValueError("incomplete header structure")
    return "".join(cleaned)


def _header_sections(value: str) -> list[str]:
    sections = []
    start = 0
    quoted = False
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif char == "\\" and quoted:
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == ";" and not quoted:
            sections.append(value[start:index].strip())
            start = index + 1
    if quoted or escaped:
        raise ValueError("incomplete quoted header value")
    sections.append(value[start:].strip())
    return sections


def _qq_delivery_host(received: str) -> bool:
    sections = _header_sections(_without_header_comments(received))
    if len(sections) != 2 or not sections[1]:
        return False
    # Ignore quoted strings as well as comments: a phrase containing "by qq.com"
    # is not the Received field's actual receiving-host clause.
    transport = re.sub(r'"(?:\\.|[^"\\])*"', " ", sections[0])
    hosts = re.findall(r"(?:^|\s)by\s+([^\s]+)", transport, re.IGNORECASE)
    if len(hosts) != 1:
        return False
    host = hosts[0].lower().removesuffix(".")
    return bool(re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*qq\.com", host))


def _repair_qq_identity_folding(value: str) -> str:
    # QQ folds even inside domain/mailbox tokens. Recover only actual CRLF+WSP
    # inside these observed identity properties, never ordinary spaces or grammar.
    token = r"[a-z0-9@<>._+%-]+"
    pattern = re.compile(
        rf"(?P<prefix>\b(?:header\.from|header\.d|smtp\.mailfrom)\s*=\s*)"
        rf"(?P<value>{token}(?:\r\n[ \t]+{token})+)(?=\s|;|$)",
        re.IGNORECASE,
    )
    pieces = re.split(r'("(?:\\.|[^"\\])*")', value)
    return "".join(
        piece
        if index % 2
        else pattern.sub(
            lambda match: match.group("prefix") + re.sub(r"\r\n[ \t]+", "", match.group("value")),
            piece,
        )
        for index, piece in enumerate(pieces)
    )


def _qq_authentication_results(value: str) -> bool:
    sections = _header_sections(_repair_qq_identity_folding(_without_header_comments(value)))
    if not sections or not re.fullmatch(r"mx\.qq\.com(?:\s+1)?", sections[0], re.IGNORECASE):
        return False
    results = {}
    for section in sections[1:]:
        result = re.match(r"(spf|dkim|dmarc)(?:/1)?\s*=\s*([a-z]+)(?=\s|$)", section, re.IGNORECASE)
        if not result:
            return False
        method, verdict = (part.lower() for part in result.groups())
        if method in results or verdict != "pass":
            return False
        attributes = {}
        remaining = section[result.end() :].strip()
        while remaining:
            attribute = re.match(
                r'([a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)?)\s*=\s*("(?:\\.|[^"\\])*"|[^\s;()"=]+)(?=\s|$)',
                remaining,
                re.IGNORECASE,
            )
            if not attribute:
                return False
            key, text = attribute.groups()
            key = key.lower()
            if key in attributes:
                return False
            if text.startswith('"'):
                text = re.sub(r"\\(.)", r"\1", text[1:-1])
            attributes[key] = text.lower()
            remaining = remaining[attribute.end() :].strip()
        results[method] = attributes
    return set(results) == {"spf", "dkim", "dmarc"} and results["dmarc"].get("header.from") in {
        "cmbchina.com",
        "message.cmbchina.com",
    }


def source_status(raw: bytes, settings, origin: str) -> tuple[str, str]:
    if origin != "imap":
        return "requires_acceptance", "source requires explicit acceptance"
    if settings.mail.host.lower().removesuffix(".") != "imap.qq.com":
        return (
            "requires_acceptance",
            "source authentication is not implemented for the configured IMAP host",
        )
    message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    if message.defects:
        return "requires_acceptance", "malformed source headers"
    if len(message.get_all("Subject", [])) > 1:
        return "requires_acceptance", "duplicate subject headers"
    if normalize_subject(str(message.get("Subject", "")))[1]:
        return "requires_acceptance", "forwarded bank message requires explicit acceptance"
    senders = message.get_all("From", [])
    auth = message.get_all("Authentication-Results", [])
    received = message.get_all("Received", [])
    if len(senders) != 1 or len(auth) != 1 or not received:
        return (
            "requires_acceptance",
            "source identity or authentication headers missing or duplicated",
        )
    addresses: tuple[Any, ...] = getattr(senders[0], "addresses", ())
    if (
        getattr(senders[0], "defects", ())
        or len(addresses) != 1
        or addresses[0].addr_spec.lower() != "ccsvc@message.cmbchina.com"
    ):
        return "requires_acceptance", "bank sender identity is not unambiguous"
    try:
        if not _qq_delivery_host(str(received[0])):
            return "requires_acceptance", "top delivery hop is not a QQ receiving host"
        raw_auth = next(
            value for name, value in message.raw_items() if name.lower() == "authentication-results"
        )
        if not _qq_authentication_results(raw_auth):
            return (
                "requires_acceptance",
                "QQ authentication results are incomplete, conflicting or unaligned",
            )
    except ValueError:
        return "requires_acceptance", "malformed QQ authentication structure"
    return "verified", "QQ receiving host and mx.qq.com bank authentication accepted"


def ingest(store: Store, evidence, raw: bytes, settings, source_item_id: int) -> str:
    digest, path = evidence.put(raw)
    state, reason = source_status(raw, settings, "imap")
    message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    sent_at = None
    header_issues = []
    if message.get("Date"):
        try:
            sent_at = parsedate_to_datetime(str(message["Date"]))
            if sent_at.tzinfo is None:
                raise ValueError("timezone missing")
        except (ValueError, TypeError, OverflowError):
            sent_at = None
            header_issues.append(
                {
                    "code": "invalid_header_date",
                    "locator": "Date",
                    "detail": "邮件时间缺失时区或格式无效",
                }
            )
    with store.transaction():
        item = store.one(
            "SELECT * FROM email_source_item WHERE id=%s FOR UPDATE", (source_item_id,)
        )
        if not item or item["source_id"] != settings.mail.source_id:
            raise ImporterError("source item does not match configured mailbox")
        if item["status"] == "collected" and item["email_id"] != digest:
            raise ImporterError("source position already references different email bytes")
        other = store.one(
            "SELECT id FROM email_source_item WHERE email_id=%s AND source_id<>%s LIMIT 1",
            (digest, item["source_id"]),
        )
        if other:
            raise ImporterError("identical email cannot belong to different business sources")
        store.execute(
            """INSERT INTO email(id,raw_path,subject,sender_address,sent_at,header_message_id,parse_issues)
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
            (
                digest,
                path,
                str(message["Subject"]) if message["Subject"] else None,
                parseaddr(str(message["From"]))[1] or None,
                sent_at,
                str(message["Message-ID"]) if message["Message-ID"] else None,
                header_issues,
            ),
        )
        store.execute(
            """UPDATE email_source_item SET status='collected',email_id=%s,source_status=%s,
            source_reason=%s,collected_at=COALESCE(collected_at,now()),last_error=NULL,skip_reason=NULL,
            updated_at=now() WHERE id=%s""",
            (digest, state, reason, source_item_id),
        )
        if state == "verified":
            store.execute(
                "UPDATE email SET parse_status='pending' WHERE id=%s AND parse_status='parsed' AND report_key IS NULL",
                (digest,),
            )
    return digest


def _download_failure(store: Store, task: dict, error_type: str, stage: str):
    with store.transaction():
        store.execute(
            "UPDATE email_source_item SET status='failed',last_error=%s,updated_at=now() WHERE id=%s",
            (stage + ": " + error_type, task["id"]),
        )


def _ignore_email_source_item(store: Store, ids: list[int]):
    if not ids:
        return
    with store.transaction():
        store.execute(
            """UPDATE email_source_item SET status='skipped',skip_reason='non-bank message',
            last_error=NULL,updated_at=now() WHERE id IN (SELECT value::bigint FROM jsonb_array_elements_text(%s))""",
            (ids,),
        )


def _process_candidate(store: Store, mail: Mail, evidence, settings, folder: str, task: dict):
    raw = mail.fetch(folder, task["uid"])
    ingest(store, evidence, raw, settings, task["id"])


def _collection_batch_progress(store: Store, progress: Progress, batch: list[dict]):
    rows = store.all(
        """SELECT status,source_status,accepted_at IS NOT NULL AS accepted,count(*) AS count
        FROM email_source_item WHERE id IN (SELECT value::bigint FROM jsonb_array_elements_text(%s))
        GROUP BY status,source_status,accepted_at IS NOT NULL""",
        ([item["id"] for item in batch],),
    )
    counts = {name: 0 for name in ("collected", "skipped", "failed", "awaiting_acceptance")}
    for row in rows:
        if row["status"] in counts:
            counts[row["status"]] += row["count"]
        if (
            row["status"] == "collected"
            and row["source_status"] == "requires_acceptance"
            and not row["accepted"]
        ):
            counts["awaiting_acceptance"] += row["count"]
    progress.advance(
        count=sum(counts[name] for name in ("collected", "skipped", "failed")), **counts
    )


def collect(
    store: Store,
    mail: Mail,
    evidence,
    settings,
    since: date | None = None,
    until: date | None = None,
):
    validate_scan_range(since, until)
    bounded = since is not None
    failed_folders = 0
    progress = Progress("collection", source_id=settings.mail.source_id)
    scan_mode = "range" if bounded else "incremental"
    emit(
        "mail_scan_started", source_id=settings.mail.source_id, scan_mode=scan_mode, stage="folders"
    )
    try:
        folders = mail.folders()
    except LogPersistenceError:
        raise
    except Exception as exc:
        emit(
            "mail_scan_failed",
            level=logging.ERROR,
            source_id=settings.mail.source_id,
            **failure_fields(exc, "folder_list_failed", "folders"),
        )
        raise
    for folder in folders:
        scan_started = time.monotonic()
        emit(
            "mail_scan_started",
            source_id=settings.mail.source_id,
            folder=folder,
            scan_mode=scan_mode,
            stage="scan",
        )
        previous = store.one(
            "SELECT * FROM email_sync_checkpoint WHERE source_id=%s AND folder=%s",
            (settings.mail.source_id, folder),
        )
        day = datetime.now(ZoneInfo(settings.timezone)).date()
        overlap = (
            day - timedelta(days=settings.mail.rescan_days)
            if previous
            and settings.mail.rescan_days > 0
            and previous["last_scanned_at"].astimezone(ZoneInfo(settings.timezone)).date() < day
            else None
        )
        try:
            if bounded:
                validity, uids = mail.scan(folder, since=since, until=until)
            else:
                validity, uids = mail.scan(
                    folder, previous["registered_uid"] if previous else 0, overlap
                )
                if previous and validity != previous["uid_validity"]:
                    validity, uids = mail.scan(folder)
        except LogPersistenceError:
            raise
        except Exception as exc:
            emit(
                "mail_scan_failed",
                level=logging.ERROR,
                source_id=settings.mail.source_id,
                folder=folder,
                **failure_fields(exc, "folder_scan_failed", "scan"),
            )
            # A LIST entry can be unreadable; preserve its failure without starving other folders.
            failed_folders += 1
            continue
        upper = max(
            uids
            + (
                [previous["registered_uid"]]
                if previous and validity == previous["uid_validity"]
                else [0]
            )
        )
        new_source_count = 0
        with store.transaction():
            if not bounded:
                previous = store.one(
                    "SELECT * FROM email_sync_checkpoint WHERE source_id=%s AND folder=%s",
                    (settings.mail.source_id, folder),
                )
                initial_upper = (
                    previous["initial_scan_upper_uid"]
                    if previous and previous["uid_validity"] == validity
                    else upper
                )
                store.execute(
                    """INSERT INTO email_sync_checkpoint(source_id,folder,uid_validity,initial_scan_upper_uid,registered_uid)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT(source_id,folder) DO UPDATE SET
                    uid_validity=excluded.uid_validity,initial_scan_upper_uid=excluded.initial_scan_upper_uid,registered_uid=excluded.registered_uid,
                    last_scanned_at=now()""",
                    (settings.mail.source_id, folder, validity, initial_upper, upper),
                )
            # Persist candidate tasks before advancing the scan snapshot. Re-scanning UIDs also
            # covers moved messages; unique locations keep email_source_item incremental.
            if uids:
                inserted = store.execute(
                    """INSERT INTO email_source_item(source_id,folder,uid_validity,uid)
                    SELECT %s,%s,%s,value::bigint FROM jsonb_array_elements_text(%s)
                    ON CONFLICT(source_id,folder,uid_validity,uid) DO NOTHING""",
                    (settings.mail.source_id, folder, validity, uids),
                )
                new_source_count = inserted.rowcount
        emit(
            "mail_scan_completed",
            source_id=settings.mail.source_id,
            folder=folder,
            scan_mode=scan_mode,
            returned_uid_count=len(uids),
            new_source_count=new_source_count,
            duration_ms=int((time.monotonic() - scan_started) * 1000),
        )
        selected_uids = set(uids)
        pending_total = store.one(
            """SELECT count(*) AS count FROM email_source_item WHERE source_id=%s AND folder=%s
            AND uid_validity=%s AND status IN ('pending','failed') AND (NOT %s OR uid IN
            (SELECT value::bigint FROM jsonb_array_elements_text(%s)))""",
            (settings.mail.source_id, folder, validity, bounded, uids),
        )
        if pending_total is None:
            raise ImporterError("collection pending count unavailable")
        progress.total += pending_total["count"]
        after_uid = 0
        while True:
            pending = store.all(
                """SELECT * FROM email_source_item WHERE source_id=%s AND folder=%s
                AND uid_validity=%s AND status IN ('pending','failed') AND uid>%s ORDER BY uid LIMIT %s""",
                (settings.mail.source_id, folder, validity, after_uid, HEADER_BATCH_SIZE),
            )
            if not pending:
                break
            # Advance only this iteration's page position, including failed batches. Failed
            # locations remain durable and will be retried from zero on the next collection.
            after_uid = pending[-1]["uid"]
            batch = [task for task in pending if not bounded or task["uid"] in selected_uids]
            if not batch:
                continue
            try:
                headers_by_uid = mail.fetch_headers_batch(
                    folder, tuple(task["uid"] for task in batch)
                )
            except LogPersistenceError:
                raise
            except Exception as exc:
                for task in batch:
                    _download_failure(store, task, type(exc).__name__, "headers_batch")
                emit(
                    "mail_batch_failed",
                    level=logging.ERROR,
                    folder=folder,
                    affected_count=len(batch),
                    **failure_fields(exc, "headers_batch_failed", "headers_batch"),
                )
                _collection_batch_progress(store, progress, batch)
                continue
            ignored_ids = []
            candidates = []
            for task in batch:
                if task["uid"] not in headers_by_uid:
                    _download_failure(store, task, "MissingHeader", "headers_batch")
                    emit(
                        "mail_item_failed",
                        level=logging.ERROR,
                        source_item_id=task["id"],
                        folder=folder,
                        stage="headers_batch",
                        error_code="missing_header",
                        error_type="MissingHeader",
                    )
                    continue
                try:
                    envelope = BytesParser(policy=policy.default).parsebytes(
                        headers_by_uid[task["uid"]], headersonly=True
                    )
                    sender = parseaddr(str(envelope.get("From", "")))[1].lower()
                    if bank_candidate(sender, str(envelope.get("Subject", ""))):
                        candidates.append(task)
                    else:
                        ignored_ids.append(task["id"])
                except LogPersistenceError:
                    raise
                except Exception as exc:
                    _download_failure(store, task, type(exc).__name__, "headers")
                    emit(
                        "mail_item_failed",
                        level=logging.ERROR,
                        source_item_id=task["id"],
                        folder=folder,
                        **failure_fields(exc, "header_parse_failed", "headers"),
                    )
            # Database failures propagate: rollback preserves all these locations for retry.
            _ignore_email_source_item(store, ignored_ids)
            for task in candidates:
                try:
                    _process_candidate(store, mail, evidence, settings, folder, task)
                except LogPersistenceError:
                    raise
                except Exception as exc:
                    _download_failure(store, task, type(exc).__name__, "message")
                    emit(
                        "mail_item_failed",
                        level=logging.ERROR,
                        source_item_id=task["id"],
                        folder=folder,
                        **failure_fields(exc, "message_collection_failed", "message"),
                    )
            _collection_batch_progress(store, progress, batch)
    progress.finish()
    if failed_folders:
        raise ImporterError(f"{failed_folders} mailbox folders could not be scanned; see issues")
    return progress.summary()
