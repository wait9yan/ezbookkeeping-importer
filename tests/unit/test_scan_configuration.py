"""IMAP 首次全量、持久 UID 增量和可配置回扫窗口。"""

from contextlib import nullcontext
from datetime import date, datetime, timedelta
from importlib import import_module
from unittest.mock import Mock, call
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from ezbookkeeping_importer.config import MailSettings, Settings

collection = import_module("ezbookkeeping_importer.application.collect")
NOW = datetime(2026, 9, 28, 12, tzinfo=ZoneInfo("Asia/Shanghai"))


def run_scan(monkeypatch, *, previous=None, rescan_days=7, validities=("v1",), **bounds):
    clock = Mock()
    clock.now.return_value = NOW
    monkeypatch.setattr(collection, "datetime", clock)
    settings = Settings(
        timezone="Asia/Shanghai",
        mail=MailSettings(source_id="imap-primary", rescan_days=rescan_days),
    )
    store = Mock()

    def read_one(query, params):
        if "FROM email_sync_checkpoint" in query:
            return previous
        if "SELECT count(*) AS count FROM email_source_item" in query:
            return {"count": 0}
        pytest.fail(f"unexpected scan query: {query}")

    store.one.side_effect = read_one
    store.all.return_value = []
    store.execute.return_value.rowcount = 0
    store.transaction.side_effect = nullcontext
    mail = Mock()
    mail.folders.return_value = ["INBOX"]
    mail.scan.side_effect = [(uid_validity, []) for uid_validity in validities]
    collection.collect(store, mail, Mock(), settings, **bounds)
    return store, mail


def cursor(days_ago=1):
    return {
        "last_scanned_at": NOW - timedelta(days=days_ago),
        "registered_uid": 4321,
        "initial_scan_upper_uid": 4000,
        "uid_validity": "v1",
    }


def test_first_scan_has_no_date_or_uid_limit(monkeypatch):
    _, mail = run_scan(monkeypatch, rescan_days=2)
    mail.scan.assert_called_once_with("INBOX", 0, None)


@pytest.mark.parametrize("days", [1, 7, 30])
def test_next_day_scan_uses_configured_overlap_and_persisted_uid(monkeypatch, days):
    _, mail = run_scan(monkeypatch, previous=cursor(), rescan_days=days)
    mail.scan.assert_called_once_with("INBOX", 4321, NOW.date() - timedelta(days=days))


def test_zero_disables_overlap_without_disabling_uid_scan(monkeypatch):
    _, mail = run_scan(monkeypatch, previous=cursor(), rescan_days=0)
    mail.scan.assert_called_once_with("INBOX", 4321, None)


def test_same_day_scan_only_requests_new_uids(monkeypatch):
    _, mail = run_scan(monkeypatch, previous=cursor(days_ago=0))
    mail.scan.assert_called_once_with("INBOX", 4321, None)


def test_uidvalidity_change_restarts_full_scan_even_with_overlap_disabled(monkeypatch):
    _, mail = run_scan(monkeypatch, previous=cursor(), rescan_days=0, validities=("v2", "v2"))
    assert mail.scan.call_args_list == [call("INBOX", 4321, None), call("INBOX")]


def test_explicit_range_does_not_replace_regular_scan_cursor(monkeypatch):
    since, until = date(2026, 8, 1), date(2026, 8, 31)
    store, mail = run_scan(monkeypatch, previous=cursor(), since=since, until=until)
    mail.scan.assert_called_once_with("INBOX", since=since, until=until)
    assert all("email_sync_checkpoint" not in args.args[0] for args in store.execute.call_args_list)


@pytest.mark.parametrize("value", [-1, 1.5, True, "7"])
def test_rescan_days_requires_nonnegative_integer(value):
    with pytest.raises(ValidationError):
        MailSettings(source_id="imap-primary", rescan_days=value)


def test_rescan_days_defaults_to_seven():
    assert MailSettings(source_id="imap-primary").rescan_days == 7
