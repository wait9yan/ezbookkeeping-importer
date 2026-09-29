from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from test_mail import FakeIMAP
from ezbookkeeping_importer.adapters.mail import MailClient


class BatchIMAP(FakeIMAP):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.result = None

    def uid(self, *args):
        self.calls.append(args)
        if args[0] == "SEARCH":
            return "OK", [b"1 2 3 4"]
        return "OK", self.result


def client():
    return MailClient(
        SimpleNamespace(
            host="synthetic.invalid",
            port=993,
            username="synthetic",
            password=SecretStr("synthetic"),
        ),
        connection_factory=BatchIMAP,
    )


def item(uid, headers=b"From: synthetic@example.test\r\n\r\n", after=False):
    prefix = (
        f"999 (BODY[HEADER] {{{len(headers)}}}"
        if after
        else f"999 (UID {uid} BODY[HEADER] {{{len(headers)}}}"
    )
    suffix = f" UID {uid})".encode() if after else b")"
    return [(prefix.encode(), headers), suffix]


def test_one_batch_is_one_readonly_fetch_and_results_are_keyed_by_uid():
    mail = client()
    mail.scan("INBOX")
    mail.connection.calls.clear()
    mail.connection.result = item(2) + item(1, after=True)
    result = mail.fetch_headers_batch("INBOX", (1, 2))
    assert set(result) == {1, 2}
    assert mail.connection.calls == [
        ("select", '"INBOX"', True),
        ("FETCH", "1,2", "(UID BODY.PEEK[HEADER])"),
    ]


def test_partial_and_empty_responses_preserve_explicit_missing_uid_information():
    mail = client()
    mail.scan("INBOX")
    mail.connection.result = item(1)
    assert set(mail.fetch_headers_batch("INBOX", (1, 2))) == {1}
    mail.connection.result = [None]
    assert mail.fetch_headers_batch("INBOX", (1, 2)) == {}


@pytest.mark.parametrize(
    "response",
    [
        item(99),
        item(1) + item(1),
        [(b"1 (BODY[HEADER] {1}", b"x"), b")"],
        [(b"1 (UID 1 BODY[HEADER] {1}", "not bytes"), b")"],
        [(b"1 (UID 1 BODY[HEADER] {2}", b"x"), b")"],
        [(b"1 (UID 1 BODY[] {1}", b"x"), b")"],
        [(b"1 (UID 1 BODY[HEADER] {1}", b"x"), b" UID 1)"],
        [b")"],
        [(b"1 (UID 1 BODY[HEADER] {1}", b"x")],
        [(b"prefix", b"x", b"extra"), b")"],
        item(1) + [None],
    ],
)
def test_invalid_or_conflicting_batch_structure_is_rejected(response):
    mail = client()
    mail.scan("INBOX")
    mail.connection.result = response
    with pytest.raises(ValueError):
        mail.fetch_headers_batch("INBOX", (1, 2))


@pytest.mark.parametrize("uids", [(0,), (-1,), (True,), (1, 1), tuple(range(1, 52))])
def test_invalid_request_cannot_generate_imap_sequence_text(uids):
    mail = client()
    with pytest.raises(ValueError):
        mail.fetch_headers_batch("INBOX", uids)
    assert not mail.connection.calls


def test_empty_batch_has_no_network_operation():
    mail = client()
    assert mail.fetch_headers_batch("INBOX", ()) == {}
    assert not mail.connection.calls


def test_uidvalidity_change_is_detected_before_batch_fetch():
    mail = client()
    mail.scan("INBOX")
    mail.connection.calls.clear()
    mail.connection.response = lambda _: ("UIDVALIDITY", [b"98765"])
    with pytest.raises(ValueError, match="UIDVALIDITY changed"):
        mail.fetch_headers_batch("INBOX", (1, 2))
    assert not any(call[0] == "FETCH" for call in mail.connection.calls)


def test_batch_requires_an_existing_folder_snapshot():
    mail = client()
    with pytest.raises(ValueError, match="snapshot"):
        mail.fetch_headers_batch("INBOX", (1,))
