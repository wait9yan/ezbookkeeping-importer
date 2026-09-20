"""扫描、下载和解析各有独立检查点；网络失败不能吞掉 UID。"""

import re
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr

from ..domain.errors import ImporterError
from ..domain.mail import bank_candidate, normalize_subject
from .ports import Mail, Store


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


def source_status(raw: bytes, settings, origin: str) -> tuple[str, str]:
    if origin != "imap" or settings.source_policy == "manual_acceptance":
        return "requires_acceptance", "source requires explicit acceptance"
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if normalize_subject(str(message.get("Subject", "")))[1]:
        return "requires_acceptance", "forwarded bank message requires explicit acceptance"
    sender = parseaddr(str(message.get("From", "")))[1].lower()
    auth = message.get_all("Authentication-Results", [])
    if sender != "ccsvc@message.cmbchina.com" or not auth:
        return "requires_acceptance", "bank identity or trusted authentication missing"
    # Only the first receiver assertion is considered; imported historical assertions do not qualify.
    first = str(auth[0]).lower()
    trusted = settings.trusted_authserv_id.lower()
    received = message.get_all("Received", [])
    if not received or not re.search(
        r"\bby\s+" + re.escape(trusted) + r"(?:\s|[;(])", str(received[0]).lower()
    ):
        return "requires_acceptance", "top delivery hop is not configured QQ receiver"
    if first.split(";", 1)[0].strip() != trusted:
        return "requires_acceptance", "unexpected authentication receiver"
    if not all(
        re.search(rf"\b{method}\s*=\s*pass\b", first) for method in ("spf", "dkim", "dmarc")
    ):
        return "requires_acceptance", "bank authentication incomplete or conflicting"
    if not re.search(r"header\.from\s*=\s*(?:message\.)?cmbchina\.com\b", first):
        return "requires_acceptance", "authentication not aligned to bank domain"
    return "verified", "configured QQ receiver authentication accepted"


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
        pending = store.all(
            """SELECT * FROM downloads WHERE source_id=%s AND folder=%s
            AND validity=%s AND status IN ('pending','failed') ORDER BY uid""",
            (settings.mail.source_id, folder, validity),
        )
        selected_uids = set(uids)
        for task in pending:
            if bounded and task["uid"] not in selected_uids:
                continue
            try:
                raw = mail.fetch(folder, task["uid"])
                envelope = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
                sender = parseaddr(str(envelope.get("From", "")))[1].lower()
                if not bank_candidate(sender, str(envelope.get("Subject", ""))):
                    store.execute(
                        "UPDATE downloads SET status='ignored',error='non-bank message' WHERE id=%s",
                        (task["id"],),
                    )
                    continue
                message_id = ingest(store, evidence, raw, settings, "imap")
                store.execute(
                    "UPDATE downloads SET status='done',message_id=%s,error=NULL WHERE id=%s",
                    (message_id, task["id"]),
                )
                store.execute(
                    "UPDATE issues SET resolved=true WHERE code='download_failed' AND entity_id=%s",
                    (str(task["id"]),),
                )
            except Exception as exc:
                # Per-message isolation preserves both failure and the remaining mail backlog.
                store.execute(
                    "UPDATE downloads SET status='failed',error=%s WHERE id=%s",
                    (type(exc).__name__, task["id"]),
                )
                store.issue("download_failed", str(task["id"]), {"error_type": type(exc).__name__})
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
