"""Synthetic QQ delivery shapes; no private message or live mailbox access."""

from types import SimpleNamespace

import pytest

from ezbookkeeping_importer.application.collect import source_status

SETTINGS = SimpleNamespace(
    mail=SimpleNamespace(host="imap.qq.com")
)
AUTH = "mx.qq.com; spf=pass smtp.mailfrom=message.cmbchina.com; dkim=pass header.d=message.cmbchina.com; dmarc=pass header.from=message.cmbchina.com"


def raw(
    auth=AUTH,
    received="from mail.cmbchina.com by newxmmxsza86-1.qq.com with ESMTP; Mon, 28 Sep 2026 12:00:00 +0800",
    sender="ccsvc@message.cmbchina.com",
    subject="Daily bank report",
    extra="",
):
    return (
        f"From: {sender}\r\nSubject: {subject}\r\nReceived: {received}\r\nAuthentication-Results: {auth}\r\n{extra}\r\nbody"
    ).encode()


@pytest.mark.parametrize(
    "host", ["newxmmxsza86-1.qq.com", "newxmmxszb58-0.qq.com", "mx.qq.com", "QQ.COM", "mx.qq.com."]
)
def test_authentication_service_is_distinct_from_real_qq_receiving_host(host):
    message = raw(received=f"from bank by {host} with ESMTP; Mon, 28 Sep 2026 12:00:00 +0800")
    assert source_status(message, SETTINGS, "imap")[0] == "verified"


def test_nested_comments_quoted_values_and_folding_are_parsed():
    auth = (
        "mx.qq.com (receiver (nested));\r\n"
        ' spf=pass (verified (nested comment)) smtp.mailfrom="sender@message.cmbchina.com";\r\n'
        ' dkim/1=pass reason="verified; bank signature" header.d=message.cmbchina.com;\r\n'
        ' dmarc=pass (policy) header.from="message.cmbchina.com"'
    )
    assert source_status(raw(auth=auth), SETTINGS, "imap")[0] == "verified"


@pytest.mark.parametrize(
    "host",
    [
        "mx.qq.com.evil.example",
        "evilqq.com",
        "qq.comevil.example",
        "qq.com@evil.example",
        "-bad.qq.com",
        ".qq.com",
        "a..qq.com",
    ],
)
def test_lookalike_or_malformed_receiving_hosts_are_not_trusted(host):
    assert (
        source_status(raw(received=f"from bank by {host}; date"), SETTINGS, "imap")[0]
        == "requires_acceptance"
    )


@pytest.mark.parametrize(
    "received",
    [
        "from bank (by mx.qq.com) by unrelated.example; date",
        'from "by mx.qq.com" by unrelated.example; date',
        "from bank by unrelated.example; date by mx.qq.com",
        "from bank by mx.qq.com by unrelated.example; date",
        "from bank by mx.qq.com (unbalanced; date",
        "from bank by mx.qq.com",
    ],
)
def test_received_comments_quotes_and_date_cannot_spoof_the_receiver(received):
    assert source_status(raw(received=received), SETTINGS, "imap")[0] == "requires_acceptance"


@pytest.mark.parametrize(
    "auth",
    [
        "mx.qq.com; spf=fail (spf=pass); dkim=pass; dmarc=pass header.from=message.cmbchina.com",
        'mx.qq.com; spf=fail reason="spf=pass"; dkim=pass; dmarc=pass header.from=message.cmbchina.com',
        'mx.qq.com; spf=pass reason="dkim=pass; dmarc=pass header.from=message.cmbchina.com"',
        "mx.qq.com (spf=pass; dkim=pass; dmarc=pass header.from=message.cmbchina.com)",
        "mx.qq.com; spf=pass; dkim=pass; dmarc=fail (dmarc=pass header.from=message.cmbchina.com)",
        "mx.qq.com; spf=pass header.from=message.cmbchina.com; dkim=pass; dmarc=pass",
        "mx.qq.com; spf=pass; dkim=pass; dmarc=pass header.from=message.cmbchina.com.evil.example",
        "mx.qq.com; spf=pass; dkim=pass; dmarc=pass header.from=evilcmbchina.com",
        "mx.qq.com.evil.example; spf=pass; dkim=pass; dmarc=pass header.from=message.cmbchina.com",
        "mx.google.com; spf=pass; dkim=pass; dmarc=pass header.from=message.cmbchina.com",
        "mx.qq.com; spf=pass; spf=fail; dkim=pass; dmarc=pass header.from=message.cmbchina.com",
        "mx.qq.com; spf=pass; spf=pass; dkim=pass; dmarc=pass header.from=message.cmbchina.com",
        "mx.qq.com; spf=pass; dkim=pass; dmarc=pass header.from=message.cmbchina.com header.from=evil.example",
        "mx.qq.com; spf=pass; dkim=pass; dmarc=pass header.from=message.cmbchina.com header.from=message.cmbchina.com",
        'mx.qq.com; spf=pass; dkim=pass; dmarc=pass header.from="message.cmbchina.com',
        "mx.qq.com; spf=pass; dkim=pass; dmarc=pass (unclosed header.from=message.cmbchina.com",
        AUTH + "; unknown=pass",
        AUTH + ";",
    ],
)
def test_forged_conflicting_and_unknown_authentication_structure_requires_acceptance(auth):
    assert source_status(raw(auth=auth), SETTINGS, "imap")[0] == "requires_acceptance"


@pytest.mark.parametrize(
    "extra",
    [
        "From: other@example.test\r\n",
        "Authentication-Results: " + AUTH + "\r\n",
        "Authentication-Results: mx.qq.com; spf=fail\r\n",
    ],
)
def test_duplicate_identity_or_authentication_headers_are_not_accepted(extra):
    assert source_status(raw(extra=extra), SETTINGS, "imap")[0] == "requires_acceptance"


@pytest.mark.parametrize(
    "sender",
    [
        "other@example.test",
        "ccsvc@message.cmbchina.com, other@example.test",
        "ccsvc@message.cmbchina.com.evil.example",
    ],
)
def test_sender_must_be_one_unambiguous_bank_mailbox(sender):
    assert source_status(raw(sender=sender), SETTINGS, "imap")[0] == "requires_acceptance"


@pytest.mark.parametrize("origin", ["eml", "file", "upload"])
def test_original_files_do_not_inherit_qq_authentication(origin):
    assert source_status(raw(), SETTINGS, origin)[0] == "requires_acceptance"


def test_forwarding_still_needs_acceptance():
    assert source_status(raw(subject="Fwd: bank report"), SETTINGS, "imap")[0] == "requires_acceptance"


@pytest.mark.parametrize("host", ["imap.google.com", "imap.qq.com.evil.example", "evilqq.com"])
def test_unsupported_imap_host_cannot_trust_qq_headers(host):
    settings = SimpleNamespace(mail=SimpleNamespace(host=host))
    state, reason = source_status(raw(), settings, "imap")
    assert state == "requires_acceptance"
    assert "not implemented" in reason


def test_qq_host_selection_is_case_insensitive_and_accepts_dns_root_dot():
    settings = SimpleNamespace(mail=SimpleNamespace(host="IMAP.QQ.COM."))
    assert source_status(raw(), settings, "imap")[0] == "verified"


def test_untrusted_top_hop_cannot_be_replaced_by_older_trusted_hop():
    message = raw(
        received="from bank by unrelated.example; date",
        extra="Received: from bank by mx.qq.com; date\r\n",
    )
    assert source_status(message, SETTINGS, "imap")[0] == "requires_acceptance"


@pytest.mark.parametrize(
    "from_domain", ["message.cmbchina\r\n\t .com", "message.cmbchina.\r\n\t com"]
)
def test_observed_qq_domain_folding_is_recovered_from_raw_header_only(from_domain):
    auth = (
        "mx.qq.com; spf=pass(synthetic receiver address) smtp.mailfrom=<ccsvc\r\n\t @message.cmbchina.com>; "
        "dkim=pass(signature was verified) header.d=message.c\r\n\t mbchina.com; "
        "dmarc=pass(p=NONE sp=NONE pct=100) header.from=" + from_domain
    )
    assert source_status(raw(auth=auth), SETTINGS, "imap")[0] == "verified"


@pytest.mark.parametrize(
    "from_domain",
    [
        "message.cmbchina .com",
        "message.cmbchina.\t com",
        "message.cmbchina.\n\t com",
        "message.cmbchina.com\r\n\t .evil.example",
        "message.cmbchina\r\n\t .comevil.example",
        "message.cmbchina\r\n\t header.from=message.cmbchina.com",
    ],
)
def test_domain_repair_does_not_join_plain_whitespace_or_accept_evil_suffixes(from_domain):
    auth = "mx.qq.com; spf=pass; dkim=pass; dmarc=pass header.from=" + from_domain
    assert source_status(raw(auth=auth), SETTINGS, "imap")[0] == "requires_acceptance"


@pytest.mark.parametrize(
    "auth",
    [
        "mx.qq.com; sp\r\n\t f=pass; dkim=pass; dmarc=pass header.from=message.cmbchina.com",
        "mx.qq.com; spf=pa\r\n\t ss; dkim=pass; dmarc=pass header.from=message.cmbchina.com",
        "mx.qq.com; spf=pass; dkim=pass; dmarc=pass header.fr\r\n\t om=message.cmbchina.com",
        "mx.qq.com; spf=pass; dkim=pass; dmarc=pass header.from=message.cmbchina.\r\n\t com; dmarc=fail header.from=message.cmbchina.com",
        'mx.qq.com; spf=pass; dkim=pass; dmarc=fail reason="header.from=message.cmbchina.\r\n\t com"',
    ],
)
def test_folding_repair_cannot_rewrite_method_grammar_or_override_failures(auth):
    assert source_status(raw(auth=auth), SETTINGS, "imap")[0] == "requires_acceptance"


def test_domain_folding_repair_never_changes_quoted_reason_text():
    from ezbookkeeping_importer.application.collect import _repair_qq_identity_folding

    reason = 'reason="header.from=message.cmbchina.\r\n\t com"'
    assert _repair_qq_identity_folding(reason) == reason


def test_duplicate_subject_cannot_hide_a_forward_marker():
    assert (
        source_status(raw(extra="Subject: Fwd: bank report\r\n"), SETTINGS, "imap")[0]
        == "requires_acceptance"
    )
