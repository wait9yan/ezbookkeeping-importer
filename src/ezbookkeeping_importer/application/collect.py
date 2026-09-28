"""扫描、下载和解析各有独立检查点；网络失败不能吞掉 UID。"""

import re
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr

from ..domain.errors import ImporterError
from ..domain.mail import bank_candidate, normalize_subject
from .ports import HEADER_BATCH_SIZE, Mail, Store


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
            """INSERT INTO jobs(kind,operation_key,payload) VALUES (%s,%s,%s)
            ON CONFLICT DO NOTHING RETURNING id""",
            (kind, str(uuid.uuid4()), payload),
        ).fetchone()
        if result:
            store.audit("sync_requested", str(result["id"]), payload)
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
    if origin != "imap" or settings.source_policy == "manual_acceptance":
        return "requires_acceptance", "source requires explicit acceptance"
    if (settings.trusted_authserv_id or "").lower() != "mx.qq.com":
        return (
            "requires_acceptance",
            "QQ policy requires the configured mx.qq.com authentication service",
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


def ingest(store: Store, evidence, raw: bytes, settings, origin: str) -> str:
    digest, path = evidence.put(raw)
    state, reason = source_status(raw, settings, origin)
    with store.transaction():
        inserted = store.execute(
            """INSERT INTO messages(id,evidence_path,origin,source_status,source_reason)
            VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id""",
            (digest, path, origin, state, reason),
        ).fetchone()
        if inserted:
            store.audit("evidence_saved", digest, {"origin": origin})
    return digest


def _download_failure(store: Store, task: dict, error_type: str, stage: str):
    with store.transaction():
        store.execute(
            "UPDATE downloads SET status='failed',error=%s WHERE id=%s", (error_type, task["id"])
        )
        store.issue("download_failed", str(task["id"]), {"error_type": error_type, "stage": stage})


def _process_header(
    store: Store, mail: Mail, evidence, settings, folder: str, task: dict, headers: bytes
):
    envelope = BytesParser(policy=policy.default).parsebytes(headers, headersonly=True)
    sender = parseaddr(str(envelope.get("From", "")))[1].lower()
    if not bank_candidate(sender, str(envelope.get("Subject", ""))):
        with store.transaction():
            store.execute(
                "UPDATE downloads SET status='ignored',error='non-bank message' WHERE id=%s",
                (task["id"],),
            )
            store.execute(
                "UPDATE issues SET resolved=true WHERE code='download_failed' AND entity_id=%s",
                (str(task["id"]),),
            )
        return
    raw = mail.fetch(folder, task["uid"])
    message_id = ingest(store, evidence, raw, settings, "imap")
    with store.transaction():
        store.execute(
            "UPDATE downloads SET status='done',message_id=%s,error=NULL WHERE id=%s",
            (message_id, task["id"]),
        )
        store.execute(
            "UPDATE issues SET resolved=true WHERE code='download_failed' AND entity_id=%s",
            (str(task["id"]),),
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
    for folder in mail.folders():
        previous = store.one(
            "SELECT * FROM cursors WHERE source_id=%s AND folder=%s",
            (settings.mail.source_id, folder),
        )
        day = datetime.now(ZoneInfo(settings.timezone)).date()
        overlap = (
            day - timedelta(days=7)
            if previous
            and previous["checked_at"].astimezone(ZoneInfo(settings.timezone)).date() < day
            else None
        )
        folder_key = str(uuid.uuid5(uuid.NAMESPACE_URL, settings.mail.source_id + ":" + folder))
        try:
            if bounded:
                validity, uids = mail.scan(folder, since=since, until=until)
            else:
                validity, uids = mail.scan(
                    folder, previous["scanned_uid"] if previous else 0, overlap
                )
                if previous and validity != previous["validity"]:
                    validity, uids = mail.scan(folder)
        except Exception as exc:
            # A LIST entry can be unreadable; preserve its failure without starving other folders.
            store.issue(
                "folder_scan_failed",
                folder_key,
                {
                    "source_id": settings.mail.source_id,
                    "folder": folder,
                    "error_type": type(exc).__name__,
                },
            )
            failed_folders += 1
            continue
        store.execute(
            "UPDATE issues SET resolved=true WHERE code='folder_scan_failed' AND entity_id=%s",
            (folder_key,),
        )
        upper = max(
            uids
            + ([previous["scanned_uid"]] if previous and validity == previous["validity"] else [0])
        )
        with store.transaction():
            if not bounded:
                previous = store.one(
                    "SELECT * FROM cursors WHERE source_id=%s AND folder=%s",
                    (settings.mail.source_id, folder),
                )
                initial_upper = (
                    previous["scan_upper"]
                    if previous and previous["validity"] == validity
                    else upper
                )
                store.execute(
                    """INSERT INTO cursors(source_id,folder,validity,scan_upper,scanned_uid)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT(source_id,folder) DO UPDATE SET
                    validity=excluded.validity,scan_upper=excluded.scan_upper,scanned_uid=excluded.scanned_uid,
                    historical_complete=false,checked_at=now()""",
                    (settings.mail.source_id, folder, validity, initial_upper, upper),
                )
            # Persist candidate tasks before advancing the scan snapshot. Re-scanning UIDs also
            # covers moved messages; unique locations keep downloads incremental.
            for uid in uids:
                store.execute(
                    """INSERT INTO downloads(source_id,folder,validity,uid)
                    VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (settings.mail.source_id, folder, validity, uid),
                )
        selected_uids = set(uids)
        after_uid = 0
        while True:
            pending = store.all(
                """SELECT * FROM downloads WHERE source_id=%s AND folder=%s
                AND validity=%s AND status IN ('pending','failed') AND uid>%s ORDER BY uid LIMIT %s""",
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
            except Exception as exc:
                for task in batch:
                    _download_failure(store, task, type(exc).__name__, "headers_batch")
                continue
            for task in batch:
                if task["uid"] not in headers_by_uid:
                    _download_failure(store, task, "MissingHeader", "headers_batch")
                    continue
                try:
                    _process_header(
                        store, mail, evidence, settings, folder, task, headers_by_uid[task["uid"]]
                    )
                except Exception as exc:
                    _download_failure(store, task, type(exc).__name__, "message")
        if not bounded:
            store.execute(
                """UPDATE cursors SET historical_complete=NOT EXISTS(
                SELECT 1 FROM downloads d WHERE d.source_id=cursors.source_id AND d.folder=cursors.folder
                AND d.validity=cursors.validity AND d.uid<=cursors.scan_upper AND d.status IN ('pending','failed'))
                WHERE source_id=%s AND folder=%s""",
                (settings.mail.source_id, folder),
            )
    if failed_folders:
        raise ImporterError(f"{failed_folders} mailbox folders could not be scanned; see issues")
