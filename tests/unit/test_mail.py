from types import SimpleNamespace
import ssl

import pytest
from pydantic import SecretStr

from ezbookkeeping_importer.adapters.mail import MailClient


class FakeIMAP:
    def __init__(self, *args, **kwargs):
        assert kwargs["ssl_context"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["ssl_context"].check_hostname
        self.calls = []

    def login(self, *args):
        return "OK", []

    def capability(self):
        return "OK", [b"IMAP4rev1"]

    def list(self):
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\Noselect) "/" "parent"',
            b'() "/" "Archive Folder"',
            b'() "/" "&ZeVnLIqe-"',
        ]

    def select(self, folder, readonly):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"2"]

    def response(self, code):
        return code, [b"12345"]

    def uid(self, *args):
        self.calls.append(args)
        if args[0] == "SEARCH":
            return "OK", [b"10 1 90"]
        return "OK", [(b"1 (UID 10 BODY[] {3}", b"raw"), b")"]

    def logout(self):
        self.calls.append(("logout",))


def client():
    return MailClient(
        SimpleNamespace(
            host="example.test",
            port=993,
            username="synthetic",
            password=SecretStr("synthetic"),
            timeout_seconds=20,
        ),
        connection_factory=FakeIMAP,
    )


def test_all_folders_and_historical_uids_readonly_peek():
    imap = client()
    assert imap.folders() == ["INBOX", "Archive Folder", "&ZeVnLIqe-"]
    assert imap.snapshot("Archive Folder") == ("12345", [1, 10, 90])
    assert imap.fetch("Archive Folder", 10) == b"raw"
    assert ("select", '"Archive Folder"', True) in imap.connection.calls
    assert ("SEARCH", None, "ALL") in imap.connection.calls
    assert ("FETCH", "10", "(UID BODY.PEEK[])") in imap.connection.calls
    imap.close()


def test_fetch_mismatched_uid_is_error():
    imap = client()
    imap.snapshot("INBOX")
    with pytest.raises(ValueError, match="mismatch"):
        imap.fetch("INBOX", 11)


def test_uidvalidity_change_prevents_reading_wrong_message():
    imap = client()
    imap.snapshot("INBOX")
    imap.connection.response = lambda code: (code, [b"98765"])
    with pytest.raises(ValueError, match="UIDVALIDITY changed"):
        imap.fetch("INBOX", 10)
    assert not any(call[0] == "FETCH" for call in imap.connection.calls)


def test_incremental_search_excludes_star_range_old_uid():
    imap = client()
    assert imap.scan("INBOX", after_uid=90) == ("12345", [])
    assert ("SEARCH", None, "UID", "91:*") in imap.connection.calls


def test_range_uses_inclusive_internal_dates_and_preserves_cross_year():
    from datetime import date

    imap = client()
    assert imap.scan("INBOX", since=date(2025, 12, 31), until=date(2026, 1, 1)) == (
        "12345",
        [1, 10, 90],
    )
    assert (
        "SEARCH",
        None,
        "SINCE",
        "31-Dec-2025",
        "BEFORE",
        "02-Jan-2026",
    ) in imap.connection.calls
    with pytest.raises(ValueError, match="bounded scan"):
        imap.scan("INBOX", until=date(2026, 1, 1))
    with pytest.raises(ValueError, match="bounded scan"):
        imap.scan("INBOX", after_uid=10, since=date(2026, 1, 1), until=date(2026, 1, 2))
