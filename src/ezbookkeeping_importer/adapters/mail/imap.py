"""Read-only IMAP transport. Checkpoints belong to the collection use case."""

from datetime import date, timedelta
import imaplib
import re
import ssl


MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def imap_date(day: date) -> str:
    return f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}"


class MailClient:
    def __init__(self, config, *, connection_factory=imaplib.IMAP4_SSL):
        self.connection = connection_factory(
            config.host,
            config.port,
            ssl_context=ssl.create_default_context(),
            timeout=config.timeout_seconds,
        )
        self._ok(
            self.connection.login(config.username, config.password.get_secret_value()), "login"
        )
        self._ok(self.connection.capability(), "capability")
        self._validities: dict[str, str] = {}

    @staticmethod
    def _ok(response, operation):
        status, data = response
        if status != "OK":
            # Server responses can contain user identifiers; don't echo them into logs.
            raise RuntimeError(f"IMAP {operation} failed: {status}")
        return data

    def folders(self) -> list[str]:
        data = self._ok(self.connection.list(), "LIST")
        folders = []
        for entry in data:
            if entry is None:
                continue
            # Literal mailbox names arrive as (prefix, literal) from imaplib.
            prefix = entry[0] if isinstance(entry, tuple) else entry
            match = re.fullmatch(rb'\(([^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(.+)', prefix)
            if not match:
                raise ValueError("IMAP LIST response has an unsupported shape")
            if b"\\noselect" in match.group(1).lower().split():
                continue
            name = entry[1] if isinstance(entry, tuple) else match.group(2)
            if not isinstance(entry, tuple) and name.startswith(b'"'):
                if not name.endswith(b'"'):
                    raise ValueError("IMAP LIST mailbox is not correctly quoted")
                name = re.sub(rb"\\(.)", rb"\1", name[1:-1])
            # IMAP modified UTF-7 is ASCII and can be passed back without decoding.
            folders.append(name.decode("ascii"))
        return folders

    def _select(self, folder):
        quoted = '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'
        self._ok(self.connection.select(quoted, readonly=True), "EXAMINE")

    def _validity(self):
        _, validity = self.connection.response("UIDVALIDITY")
        if not validity or not validity[0] or not validity[0].isdigit():
            raise ValueError("IMAP UIDVALIDITY missing or invalid")
        return validity[0].decode("ascii")

    def snapshot(self, folder) -> tuple[str, list[int]]:
        return self.scan(folder)

    def scan(
        self, folder, after_uid: int = 0, since: date | None = None, until: date | None = None
    ) -> tuple[str, list[int]]:
        if after_uid < 0:
            raise ValueError("after_uid must not be negative")
        if until is not None and (since is None or since > until or until == date.max or after_uid):
            raise ValueError("bounded scan requires ordered dates and no UID cursor")
        self._select(folder)
        validity = self._validity()
        criteria = ["ALL"]
        if until is not None:
            # IMAP BEFORE is exclusive; the public requested end date is inclusive.
            assert since is not None
            criteria = ["SINCE", imap_date(since), "BEFORE", imap_date(until + timedelta(days=1))]
        elif after_uid:
            criteria = ["UID", f"{after_uid + 1}:*"]
            if since is not None:
                criteria = ["OR", *criteria, "SINCE", imap_date(since)]
        data = self._ok(self.connection.uid("SEARCH", None, *criteria), "UID SEARCH")
        if len(data) != 1 or not isinstance(data[0], bytes):
            raise ValueError("IMAP UID SEARCH response has an unsupported shape")
        uids = [int(value) for value in data[0].split()]
        if any(uid <= 0 for uid in uids):
            raise ValueError("IMAP returned an invalid UID")
        # IMAP n:* also matches the highest UID when n exceeds it.
        if since is None:
            uids = [uid for uid in uids if uid > after_uid]
        self._validities[folder] = validity
        return validity, sorted(set(uids))

    def fetch(self, folder, uid) -> bytes:
        if not isinstance(uid, int) or uid <= 0:
            raise ValueError("UID must be a positive integer")
        if folder not in self._validities:
            raise ValueError("Take a folder snapshot before fetching messages")
        self._select(folder)
        if self._validity() != self._validities[folder]:
            raise ValueError("IMAP UIDVALIDITY changed; rescan the folder")
        data = self._ok(self.connection.uid("FETCH", str(uid), "(UID BODY.PEEK[])"), "UID FETCH")
        payloads = [entry for entry in data if isinstance(entry, tuple)]
        if len(payloads) != 1:
            raise ValueError("IMAP FETCH did not return exactly one message")
        attributes, raw = payloads[0]
        match = re.search(rb"\bUID (\d+)\b", attributes)
        if not match or int(match.group(1)) != uid or not isinstance(raw, bytes):
            raise ValueError("IMAP FETCH UID or payload mismatch")
        return raw

    def close(self):
        self.connection.logout()
