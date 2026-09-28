"""Real PostgreSQL, isolated schemas, synthetic mail and an in-process ledger."""

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from email.message import EmailMessage
import os
from types import SimpleNamespace
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from ezbookkeeping_importer.adapters.banks.cmb import BankParser
from ezbookkeeping_importer.adapters.evidence_store import EvidenceStore
from ezbookkeeping_importer.adapters.ezbookkeeping.client import EzBookkeepingClient
from ezbookkeeping_importer.adapters.persistence.postgres import PostgresStore
from ezbookkeeping_importer.application.classify import classify_pending
from ezbookkeeping_importer.application.collect import collect, ingest
from ezbookkeeping_importer.application.parse import parse_pending
from ezbookkeeping_importer.application.reconcile import reconcile
from ezbookkeeping_importer.application.resolve import resolve
from ezbookkeeping_importer.application.write import complete, recover_dispatching, write_queued
from ezbookkeeping_importer.config import MailSettings, Settings
from ezbookkeeping_importer.domain.errors import Conflict, ImporterError


@pytest.fixture
def database():
    dsn = os.environ.get("EBKI_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("EBKI_TEST_DATABASE_URL is required for real PostgreSQL tests")
    schema = "ebki_test_" + uuid.uuid4().hex
    admin = psycopg.connect(dsn, autocommit=True)
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    isolated_dsn = make_conninfo(dsn, options=f"-c search_path={schema}")
    stores = []

    def connect():
        store = PostgresStore(isolated_dsn)
        stores.append(store)
        return store

    try:
        store = connect()
        store.migrate()
        yield SimpleNamespace(store=store, connect=connect)
    finally:
        for store in stores:
            store.close()
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


@pytest.fixture
def settings():
    return Settings(
        ledger_url="http://synthetic.invalid",
        mail=MailSettings(username="synthetic@example.test", source_id="synthetic"),
        timezone="Asia/Shanghai",
        classification_mode="rules_only",
    )


class Ledger:
    def __init__(self, mode="success"):
        self.mode = mode
        self.records = {}
        self.create_calls = []
        self.modify_calls = []
        self.before_create = None

    def accounts(self):
        return [
            {
                "id": "account",
                "type": 1,
                "currency": "CNY",
                "hidden": False,
                "comment": "卡号 4444333322221234",
            },
            {
                "id": "usd-account",
                "type": 1,
                "currency": "USD",
                "hidden": False,
                "comment": "卡号 4444333322221234",
            },
        ]

    def categories(self):
        return [
            {"id": "category", "type": 2, "parentId": "parent", "path": "其他杂项 → 待分类"},
            {"id": "edited-category", "type": 2, "parentId": "parent"},
        ]

    def rates(self):
        return {
            "baseCurrency": "USD",
            "dataSource": "synthetic",
            "updateTime": int(datetime.now(timezone.utc).timestamp()),
            "exchangeRates": [{"currency": "CNY", "rate": "7"}],
        }

    def create(self, payload):
        self.create_calls.append(deepcopy(payload))
        if self.before_create:
            self.before_create()
        if self.mode != "timeout_empty":
            key = str(len(self.records) + 1)
            self.records[key] = {
                "id": key,
                "hideAmount": False,
                "tagIds": [],
                "pictures": [],
                **deepcopy(payload),
            }
            if self.mode == "timeout_multi":
                self.records[key + "-duplicate"] = {**self.records[key], "id": key + "-duplicate"}
        if self.mode.startswith("timeout"):
            raise TimeoutError("synthetic lost response")
        return deepcopy(self.records[key])

    def search(self, start, end, marker=None):
        return [
            deepcopy(row)
            for row in self.records.values()
            if marker is None or marker in row["comment"]
        ]

    def get(self, target_id):
        return deepcopy(self.records.get(str(target_id)))

    settlement_payload = staticmethod(EzBookkeepingClient.settlement_payload)

    def modify(self, payload):
        self.modify_calls.append(deepcopy(payload))
        self.records[payload["id"]].update(deepcopy(payload))
        return self.get(payload["id"])


def raw_daily(count=1, currency="CNY", message_id="one", reverse=False):
    message = EmailMessage()
    message["Subject"] = "每日信用管家"
    message["Message-ID"] = f"<{message_id}@example.test>"
    rows = [
        "<div><b>12:00:00</b><b>" + currency + " 10.00</b><b>尾号1234 消费 合成商户</b></div>"
    ] * count
    if reverse:
        rows.reverse()
    message.set_content("<p>2026/01/01 您的消费明细如下：</p>" + "".join(rows), subtype="html")
    return message.as_bytes()


def import_daily(store, tmp_path, settings, **kwargs):
    identifier = ingest(
        store, EvidenceStore(tmp_path / "evidence"), raw_daily(**kwargs), settings, "file"
    )
    store.execute("UPDATE messages SET accepted=true WHERE id=%s", (identifier,))
    parse_pending(store, BankParser())
    return identifier


def queue(store, tmp_path, settings, ledger, **kwargs):
    import_daily(store, tmp_path, settings, **kwargs)
    classify_pending(store, settings, ledger, None)
    assert store.one("SELECT count(*) AS n FROM jobs WHERE status='queued'")["n"] == 1, store.all(
        "SELECT code,data FROM issues"
    )
    return store.one("SELECT * FROM transactions")


def queue_legacy_estimate(store, tmp_path, settings, ledger):
    """An already persisted pre-original-currency decision, not a new FX import."""
    import_daily(store, tmp_path, settings, currency="USD")
    transaction = store.one("SELECT * FROM transactions")
    payload = {
        "type": 3,
        "sourceAccountId": "account",
        "sourceAmount": 7000,
        "categoryId": "category",
        "time": int(datetime.fromisoformat(transaction["facts"]["occurred_at"]).timestamp()),
        "utcOffset": 480,
        "comment": transaction["marker"] + " synthetic legacy CNY estimate",
        "clientSessionId": transaction["marker"],
    }
    decision = {
        "payload": payload,
        "classification": {"classification_status": "unmatched", "category_id": None},
        "rate_snapshot": {"dataSource": "synthetic legacy quote", "adoptedRate": "7"},
    }
    with store.transaction():
        store.execute(
            "UPDATE transactions SET decision=%s,state='queued' WHERE id=%s",
            (decision, transaction["id"]),
        )
        store.execute(
            "INSERT INTO jobs(transaction_id,kind,version,operation_key,payload) VALUES (%s,'create',1,%s,%s)",
            (transaction["id"], transaction["marker"], payload),
        )
    return store.one("SELECT * FROM transactions")


def test_ingest_replay_preserves_identical_real_multiplicity(database, tmp_path, settings):
    store = database.store
    first = import_daily(store, tmp_path, settings, count=2)
    import_daily(store, tmp_path, settings, count=2)
    second = import_daily(store, tmp_path, settings, count=2, message_id="resent")
    assert first != second
    assert store.one("SELECT count(*) AS n FROM messages")["n"] == 2
    assert store.one("SELECT count(*) AS n FROM reports")["n"] == 1
    assert store.one("SELECT count(*) AS n FROM transactions")["n"] == 2
    assert store.one("SELECT count(*) AS n FROM transaction_evidence")["n"] == 4
    assert store.one("SELECT count(*) AS n FROM issues")["n"] == 0


def test_resolve_version_cancels_queued_job_and_blocks_stale_decision(database, tmp_path, settings):
    store, ledger = database.store, Ledger()
    transaction = queue(store, tmp_path, settings, ledger)
    store.issue("synthetic_review", transaction["id"], {"version": 1})
    issue = store.one("SELECT * FROM issues")
    with pytest.raises(Conflict, match="stale"):
        resolve(store, ledger, issue["id"], 0, "ignore", "synthetic")
    resolve(store, ledger, issue["id"], 1, "ignore", "synthetic")
    write_queued(store, ledger, True)
    assert ledger.create_calls == []
    updated = store.one("SELECT * FROM transactions")
    assert (updated["version"], updated["state"]) == (2, "ignored")
    assert store.one("SELECT status FROM jobs")["status"] == "cancelled"


def test_claimed_job_records_followup_intent_without_false_cancellation(
    database, tmp_path, settings
):
    store, ledger = database.store, Ledger()
    transaction = queue(store, tmp_path, settings, ledger)
    store.issue("synthetic_review", transaction["id"], {"version": 1})
    issue = store.one("SELECT * FROM issues")
    decisions = []
    ledger.before_create = lambda: decisions.append(
        resolve(store, ledger, issue["id"], 1, "ignore", "synthetic")
    )
    write_queued(store, ledger, True)
    assert decisions[0]["state"] == "dispatching"
    assert len(ledger.create_calls) == 1
    assert store.one("SELECT state FROM transactions")["state"] == "booked"
    assert (
        store.one("SELECT count(*) AS n FROM audit_events WHERE event='followup_intent'")["n"] == 1
    )


def test_timeout_after_commit_is_verified_without_second_post(database, tmp_path, settings):
    store, ledger = database.store, Ledger("timeout_commit")
    queue(store, tmp_path, settings, ledger)
    write_queued(store, ledger, True)
    write_queued(store, ledger, True)
    assert len(ledger.create_calls) == 1
    assert store.one("SELECT state FROM transactions")["state"] == "booked"
    assert store.one("SELECT outcome FROM write_attempts")["outcome"] == "confirmed"


@pytest.mark.parametrize("mode,candidates", [("timeout_empty", 0), ("timeout_multi", 2)])
def test_unknown_without_unique_candidate_never_retries(
    database, tmp_path, settings, mode, candidates
):
    store, ledger = database.store, Ledger(mode)
    queue(store, tmp_path, settings, ledger)
    write_queued(store, ledger, True)
    recover_dispatching(store)
    write_queued(store, ledger, True)
    assert len(ledger.records) == candidates
    assert len(ledger.create_calls) == 1
    assert store.one("SELECT status FROM jobs")["status"] == "unknown"
    assert store.one("SELECT state FROM transactions")["state"] == "unknown"


def test_settlement_updates_same_id_and_preserves_current_fields(database, tmp_path, settings):
    store, ledger = database.store, Ledger()
    transaction = queue_legacy_estimate(store, tmp_path, settings, ledger)
    write_queued(store, ledger, True)
    target = store.one("SELECT target_id FROM transactions")["target_id"]
    edited = {
        "categoryId": "edited-category",
        "comment": "user edited comment",
        "tagIds": ["tag"],
        "pictures": [{"pictureId": "picture"}],
        "hideAmount": True,
        "geoLocation": {"longitude": "1", "latitude": "2"},
        "time": 1700000000,
    }
    ledger.records[target].update(deepcopy(edited))
    statement(store, transaction, "72.00")
    reconcile(store, ledger, tmp_path / "reports")
    write_queued(store, ledger, True)
    reconcile(store, ledger, tmp_path / "reports")
    write_queued(store, ledger, True)
    assert len(ledger.create_calls) == len(ledger.modify_calls) == 1
    payload = ledger.modify_calls[0]
    assert payload["id"] == target and payload["sourceAmount"] == 7200
    for field in ("categoryId", "comment", "tagIds", "hideAmount", "geoLocation", "time"):
        assert payload[field] == edited[field]
    assert payload["pictureIds"] == ["picture"]
    assert store.one("SELECT status FROM reconciliation_items")["status"] == "target_changed"
    assert store.one("SELECT target_id FROM transactions")["target_id"] == target


def test_postgres_worker_lock_is_exclusive_and_released(database):
    first, second = database.store, database.connect()
    assert first.lock_worker()
    assert not second.lock_worker()
    first.close()
    assert second.lock_worker()


def test_restart_recovers_dispatching_as_unknown_without_post(database, tmp_path, settings):
    store, ledger = database.store, Ledger()
    transaction = queue(store, tmp_path, settings, ledger)
    job = store.one("SELECT * FROM jobs")
    with store.transaction():
        store.execute("UPDATE jobs SET status='dispatching' WHERE id=%s", (job["id"],))
        store.execute(
            "UPDATE transactions SET state='dispatching' WHERE id=%s", (transaction["id"],)
        )
        store.execute(
            "INSERT INTO write_attempts(job_id,request,outcome) VALUES (%s,%s,'unknown')",
            (job["id"], job["payload"]),
        )
    store.close()
    restarted = database.connect()
    recover_dispatching(restarted)
    write_queued(restarted, ledger, True)
    assert ledger.create_calls == []
    assert restarted.one("SELECT state FROM transactions")["state"] == "unknown"
    assert restarted.one("SELECT status FROM jobs")["status"] == "unknown"
    assert (
        restarted.one("SELECT count(*) AS n FROM audit_events WHERE event='write_interrupted'")["n"]
        == 1
    )


def statement(store, transaction, amount):
    row = {
        **transaction["facts"],
        "row_key": "monthly-row",
        "original_currency": None,
        "settlement_amount": amount,
        "settlement_currency": "CNY",
    }
    source_message = store.one("SELECT id FROM messages")["id"]
    store.execute(
        "INSERT INTO reports(report_key,kind,message_id,fingerprint,parsed) VALUES (%s,'monthly',%s,%s,%s)",
        ("monthly-report", source_message, "synthetic", {"rows": [row], "metadata": {}}),
    )


def test_settlement_preflight_retry_reuses_modify_intent(database, tmp_path, settings):
    store, ledger = database.store, Ledger()
    transaction = queue_legacy_estimate(store, tmp_path, settings, ledger)
    write_queued(store, ledger, True)
    target = store.one("SELECT target_id FROM transactions")["target_id"]
    statement(store, transaction, "72.00")
    reconcile(store, ledger, tmp_path / "reports")
    ledger.records[target]["categoryId"] = "missing-category"
    write_queued(store, ledger, True)
    issue = store.one("SELECT * FROM issues WHERE code='write_preflight_failed'")
    assert issue is not None
    resolve(store, ledger, issue["id"], 1, "retry", "synthetic category restored")
    ledger.records[target]["categoryId"] = "category"
    classify_pending(store, settings, ledger, None)
    write_queued(store, ledger, True)
    assert len(ledger.create_calls) == len(ledger.modify_calls) == 1
    assert store.one("SELECT count(*) AS n FROM jobs WHERE kind='create'")["n"] == 1
    job = store.one("SELECT * FROM jobs WHERE kind='settle_amount'")
    assert job["version"] == 2 and job["status"] == "done"
    assert ledger.modify_calls[0]["id"] == target


def test_stale_completion_cannot_confirm_new_decision(database, tmp_path, settings):
    store, ledger = database.store, Ledger()
    transaction = queue(store, tmp_path, settings, ledger)
    stale = store.one("SELECT * FROM jobs")
    store.issue("synthetic_review", transaction["id"], {"version": 1})
    issue = store.one("SELECT * FROM issues")
    resolve(store, ledger, issue["id"], 1, "retry", "synthetic corrected decision")
    classify_pending(store, settings, ledger, None)
    complete(store, stale, {"id": "stale-target", **stale["payload"]})
    current = store.one("SELECT * FROM transactions")
    assert current["version"] == 2
    assert current["state"] == "queued" and current["target_id"] is None
    assert store.one("SELECT status FROM jobs")["status"] == "queued"
    assert (
        store.one("SELECT count(*) AS n FROM audit_events WHERE event='write_confirmed'")["n"] == 0
    )


def test_equal_estimate_is_finalized_then_user_amount_edit_is_reported(
    database, tmp_path, settings
):
    store, ledger = database.store, Ledger()
    transaction = queue_legacy_estimate(store, tmp_path, settings, ledger)
    write_queued(store, ledger, True)
    target = store.one("SELECT target_id FROM transactions")["target_id"]
    statement(store, transaction, "70.00")
    reconcile(store, ledger, tmp_path / "reports")
    write_queued(store, ledger, True)
    assert not ledger.modify_calls
    assert store.one("SELECT settlement FROM transactions")["settlement"]["amount"] == 7000
    ledger.records[target]["sourceAmount"] = 7500
    reconcile(store, ledger, tmp_path / "reports")
    write_queued(store, ledger, True)
    assert store.one("SELECT status FROM reconciliation_items")["status"] == "target_changed"
    assert store.one("SELECT count(*) AS n FROM jobs WHERE kind='settle_amount'")["n"] == 1
    assert len(ledger.create_calls) == 1 and not ledger.modify_calls


class Mail:
    def __init__(self, observer, folders):
        self.observer = observer
        self.data = folders
        self.scans = []
        self.fetches = []
        self.failures = set()
        self.interruptions = set()
        self.persisted_before_fetch = []

    def folders(self):
        return list(self.data)

    def scan(self, folder, after_uid=0, since=None, until=None):
        self.scans.append((folder, after_uid, since))
        validity, messages = self.data[folder]
        return validity, [uid for uid in messages if uid > after_uid or since is not None]

    def fetch_headers(self, folder, uid):
        return self.data[folder][1][uid].split(b"\n\n", 1)[0] + b"\n\n"

    def fetch(self, folder, uid):
        self.fetches.append((folder, uid))
        validity, messages = self.data[folder]
        task = self.observer.one(
            "SELECT id FROM downloads WHERE folder=%s AND validity=%s AND uid=%s",
            (folder, validity, uid),
        )
        self.persisted_before_fetch.append(task is not None)
        if (folder, uid) in self.interruptions:
            raise KeyboardInterrupt("synthetic process interruption")
        if (folder, uid) in self.failures:
            raise TimeoutError("synthetic read failure")
        return messages[uid]


def bank_raw():
    return b"From: ccsvc@message.cmbchina.com\n" + raw_daily()


def test_collect_persists_tasks_before_download_and_retries_after_restart(
    database, tmp_path, settings
):
    store = database.store
    mail = Mail(
        database.connect(),
        {
            "INBOX": ("v1", {1: bank_raw(), 5: bank_raw()}),
            "&ZeVnLIqe-": ("v2", {20: bank_raw()}),
        },
    )
    mail.failures.add(("INBOX", 1))
    evidence = EvidenceStore(tmp_path / "evidence")
    collect(store, mail, evidence, settings)
    assert all(mail.persisted_before_fetch)
    assert [(folder, after) for folder, after, _ in mail.scans] == [("INBOX", 0), ("&ZeVnLIqe-", 0)]
    assert (
        store.one("SELECT status FROM downloads WHERE folder='INBOX' AND uid=1")["status"]
        == "failed"
    )
    assert (
        store.one("SELECT historical_complete FROM cursors WHERE folder='INBOX'")[
            "historical_complete"
        ]
        is False
    )
    assert store.one("SELECT count(*) AS n FROM downloads WHERE status='done'")["n"] == 2
    store.close()
    restarted = database.connect()
    mail.failures.clear()
    mail.scans.clear()
    mail.fetches.clear()
    mail.data["INBOX"][1][8] = bank_raw()
    mail.data["NewFolder"] = ("v3", {2: bank_raw()})
    collect(restarted, mail, evidence, settings)
    assert [(folder, after) for folder, after, _ in mail.scans] == [
        ("INBOX", 5),
        ("&ZeVnLIqe-", 20),
        ("NewFolder", 0),
    ]
    assert set(mail.fetches) == {("INBOX", 1), ("INBOX", 8), ("NewFolder", 2)}
    assert restarted.one("SELECT count(*) AS n FROM downloads WHERE status='done'")["n"] == 5
    assert all(
        row["historical_complete"]
        for row in restarted.all("SELECT historical_complete FROM cursors")
    )
    assert restarted.one("SELECT count(*) AS n FROM messages")["n"] == 1


def test_uidvalidity_reset_rescans_all_without_duplicate_business_rows(
    database, tmp_path, settings
):
    store = database.store
    mail = Mail(database.connect(), {"INBOX": ("old", {10: bank_raw()})})
    evidence = EvidenceStore(tmp_path / "evidence")
    collect(store, mail, evidence, settings)
    store.execute("UPDATE messages SET accepted=true")
    parse_pending(store, BankParser())
    mail.data["INBOX"] = ("new", {1: bank_raw(), 2: bank_raw()})
    mail.scans.clear()
    collect(store, mail, evidence, settings)
    parse_pending(store, BankParser())
    assert [(folder, after) for folder, after, _ in mail.scans] == [("INBOX", 10), ("INBOX", 0)]
    assert store.one("SELECT count(*) AS n FROM downloads")["n"] == 3
    assert store.one("SELECT count(*) AS n FROM transactions")["n"] == 1
    cursor = store.one("SELECT * FROM cursors")
    assert (cursor["validity"], cursor["scanned_uid"], cursor["historical_complete"]) == (
        "new",
        2,
        True,
    )


def test_collection_interruption_resumes_pending_snapshot(database, tmp_path, settings):
    store = database.store
    mail = Mail(database.connect(), {"INBOX": ("v1", {1: bank_raw(), 2: bank_raw()})})
    mail.interruptions.add(("INBOX", 2))
    evidence = EvidenceStore(tmp_path / "evidence")
    with pytest.raises(KeyboardInterrupt):
        collect(store, mail, evidence, settings)
    assert store.one("SELECT status FROM downloads WHERE uid=1")["status"] == "done"
    assert store.one("SELECT status FROM downloads WHERE uid=2")["status"] == "pending"
    assert store.one("SELECT historical_complete FROM cursors")["historical_complete"] is False
    store.close()
    restarted = database.connect()
    mail.interruptions.clear()
    mail.fetches.clear()
    collect(restarted, mail, evidence, settings)
    assert mail.fetches == [("INBOX", 2)]
    assert restarted.one("SELECT historical_complete FROM cursors")["historical_complete"] is True


def test_reordered_rows_and_locators_do_not_create_report_revision(database, tmp_path, settings):
    store = database.store
    evidence = EvidenceStore(tmp_path / "evidence")
    for clocks in [("12:00:00", "13:00:00", "12:00:00"), ("13:00:00", "12:00:00", "12:00:00")]:
        message = EmailMessage()
        message["Subject"] = "每日信用管家"
        message.set_content(
            "<p>2026/01/01 您的消费明细如下：</p>"
            + "".join(
                f"<div><b>{clock}</b><b>CNY 10.00</b><b>尾号1234 消费 合成商户</b></div>"
                for clock in clocks
            ),
            subtype="html",
        )
        identifier = ingest(store, evidence, message.as_bytes(), settings, "file")
        store.execute("UPDATE messages SET accepted=true WHERE id=%s", (identifier,))
        parse_pending(store, BankParser())
    assert store.one("SELECT count(*) AS n FROM reports")["n"] == 1
    assert store.one("SELECT count(*) AS n FROM transactions")["n"] == 3
    assert store.one("SELECT count(*) AS n FROM transaction_evidence")["n"] == 6
    assert store.one("SELECT count(*) AS n FROM issues WHERE code='report_revision'")["n"] == 0


def test_restored_pending_exact_marker_blocks_create_even_when_amount_changed(
    database, tmp_path, settings
):
    store, ledger = database.store, Ledger()
    transaction = queue_legacy_estimate(store, tmp_path, settings, ledger)
    payload = store.one("SELECT payload FROM jobs")["payload"]
    ledger.records["existing"] = {"id": "existing", **payload, "sourceAmount": 7200}
    # Restore a snapshot predating the outbox and settled linkage.
    store.execute("DELETE FROM jobs")
    store.execute("UPDATE transactions SET state='pending',decision=NULL")
    classify_pending(store, settings, ledger, None)
    write_queued(store, ledger, True)
    assert ledger.create_calls == []
    assert store.one("SELECT state FROM transactions")["state"] == "unknown"
    assert store.one("SELECT target_id FROM jobs")["target_id"] == "existing"
    assert store.one(
        "SELECT * FROM issues WHERE entity_id=%s AND code='write_unknown'", (transaction["id"],)
    )


def test_restore_audit_never_reposts_queued_settlement(database, tmp_path, settings):
    from ezbookkeeping_importer.application.maintenance import restore_audit

    store, ledger = database.store, Ledger()
    transaction = queue_legacy_estimate(store, tmp_path, settings, ledger)
    write_queued(store, ledger, True)
    statement(store, transaction, "72.00")
    reconcile(store, ledger, tmp_path / "reports")
    assert restore_audit(store, ledger)["unknown"] == 1
    write_queued(store, ledger, True)
    assert ledger.modify_calls == []
    assert store.one("SELECT status FROM jobs WHERE kind='settle_amount'")["status"] == "unknown"


def test_unreadable_folder_preserves_failure_and_does_not_starve_later_folders(
    database, tmp_path, settings
):
    class PartlyUnreadableMail(Mail):
        unreadable = True

        def scan(self, folder, after_uid=0, since=None, until=None):
            if folder == "Blocked" and self.unreadable:
                raise PermissionError("synthetic EXAMINE rejection")
            return super().scan(folder, after_uid, since)

    mail = PartlyUnreadableMail(
        database.connect(),
        {
            "Blocked": ("v1", {1: bank_raw()}),
            "Readable": ("v2", {2: bank_raw()}),
        },
    )
    store = database.store
    evidence = EvidenceStore(tmp_path / "evidence")
    with pytest.raises(ImporterError, match="1 mailbox folders"):
        collect(store, mail, evidence, settings)
    assert store.one("SELECT status FROM downloads WHERE folder='Readable'")["status"] == "done"
    assert store.one("SELECT * FROM cursors WHERE folder='Blocked'") is None
    issue = store.one("SELECT * FROM issues WHERE code='folder_scan_failed'")
    assert issue["data"]["folder"] == "Blocked"
    assert issue["resolved"] is False
    mail.unreadable = False
    collect(store, mail, evidence, settings)
    assert store.one("SELECT status FROM downloads WHERE folder='Blocked'")["status"] == "done"
    assert store.one("SELECT resolved FROM issues WHERE id=%s", (issue["id"],))["resolved"] is True


def test_range_queue_coexists_with_ordinary_sync_and_recovers_payload(
    database, monkeypatch, settings
):
    from ezbookkeeping_importer.application.collect import request_sync
    from ezbookkeeping_importer.application import service
    import logging

    store = database.store
    start, end = date(2025, 12, 31), date(2026, 1, 1)
    assert request_sync(store)
    assert request_sync(store) is False
    assert request_sync(store, start, end)
    assert request_sync(store, start, end)
    assert store.one("SELECT count(*) AS n FROM jobs WHERE status='queued'")["n"] == 3
    store.execute("UPDATE jobs SET status='dispatching' WHERE kind='sync_range'")
    recover_dispatching(store)
    assert all(
        row["payload"] == {"since": "2025-12-31", "until": "2026-01-01"}
        for row in store.all("SELECT * FROM jobs WHERE kind='sync_range'")
    )
    seen = []
    fail_once = True

    def recording_collect(store, mail, evidence, settings, since, until):
        nonlocal fail_once
        if since is None and fail_once:
            fail_once = False
            raise TimeoutError("synthetic first ordinary sync failure")
        seen.append((since, until))

    monkeypatch.setattr(service, "collect", recording_collect)
    for name in ("parse_pending", "classify_pending", "write_queued", "reconcile"):
        monkeypatch.setattr(service, name, lambda *args: None)
    runtime = SimpleNamespace(
        store=store,
        settings=settings,
        evidence=None,
        parser=None,
        ledger=None,
        ai=None,
        mail=lambda: SimpleNamespace(close=lambda: None),
    )
    logger = logging.getLogger("synthetic-test")
    assert service.cycle(runtime, logger) is False
    assert service.cycle(runtime, logger) is True
    assert seen == [(start, end)]  # Failed oldest ordinary sync cannot starve bounded requests.
    assert service.cycle(runtime, logger) is True
    assert service.cycle(runtime, logger) is True
    assert seen == [(start, end), (start, end), (None, None)]
    assert store.one("SELECT count(*) AS n FROM jobs WHERE status='done'")["n"] == 3
    assert (
        store.one("SELECT count(*) AS n FROM issues WHERE code='sync_failed' AND NOT resolved")["n"]
        == 0
    )


def test_range_scan_does_not_change_historical_cursor_or_drain_other_tasks(
    database, tmp_path, settings, monkeypatch
):
    store = database.store
    evidence = EvidenceStore(tmp_path / "evidence")
    mail = Mail(database.connect(), {"INBOX": ("old", {10: bank_raw()})})
    collect(store, mail, evidence, settings)
    before = store.one("SELECT * FROM cursors")
    mail.data = {
        "INBOX": ("new", {1: bank_raw(), 2: bank_raw()}),
        "Archive": ("new", {2: bank_raw()}),
    }
    store.execute(
        "INSERT INTO downloads(source_id,folder,validity,uid) VALUES (%s,'INBOX','new',1)",
        (settings.mail.source_id,),
    )
    bounded_calls = []

    def bounded_scan(folder, after_uid=0, since=None, until=None):
        bounded_calls.append((folder, after_uid, since, until))
        return "new", [2]

    monkeypatch.setattr(mail, "scan", bounded_scan)
    mail.fetches.clear()
    start = end = date(2026, 1, 1)
    collect(store, mail, evidence, settings, start, end)
    assert store.one("SELECT * FROM cursors") == before
    assert store.one("SELECT count(*) AS n FROM cursors")["n"] == 1
    assert bounded_calls == [("INBOX", 0, start, end), ("Archive", 0, start, end)]
    assert mail.fetches == [("INBOX", 2), ("Archive", 2)]
    assert (
        store.one("SELECT status FROM downloads WHERE validity='new' AND uid=1")["status"]
        == "pending"
    )
    collect(store, mail, evidence, settings, start, end)
    assert len(mail.fetches) == 2
    assert store.one("SELECT count(*) AS n FROM messages")["n"] == 1


def test_forwarded_mail_reaches_explicit_acceptance_not_silent_ignore(database, tmp_path, settings):
    message = EmailMessage()
    message["Subject"] = "Fwd: 每日信用管家"
    message["From"] = "synthetic-forwarder@example.test"
    message.set_content(
        "<p>2026/01/01 您的消费明细如下：</p><b>12:00:00</b><b>CNY 10.00</b><b>尾号1234 消费 合成商户</b>",
        subtype="html",
    )
    ordinary = EmailMessage()
    ordinary["From"] = "synthetic-forwarder@example.test"
    ordinary["Subject"] = "普通邮件"
    ordinary.set_content("不属于账务输入")
    store = database.store
    mail = Mail(
        database.connect(), {"INBOX": ("v1", {1: message.as_bytes(), 2: ordinary.as_bytes()})}
    )
    collect(store, mail, EvidenceStore(tmp_path / "evidence"), settings)
    parse_pending(store, BankParser())
    assert store.one("SELECT status FROM downloads WHERE uid=2")["status"] == "ignored"
    issue = store.one("SELECT * FROM issues WHERE code='source_acceptance'")
    assert issue is not None
    assert store.one("SELECT count(*) AS n FROM transactions")["n"] == 0
    resolve(store, Ledger(), issue["id"], 1, "accept-source", "synthetic explicit verification")
    parse_pending(store, BankParser())
    assert store.one("SELECT count(*) AS n FROM transactions")["n"] == 1


def test_explicit_retry_refreshes_deleted_category_without_repricing(
    database, tmp_path, settings, monkeypatch
):
    from ezbookkeeping_importer.config import Rule

    store, ledger = database.store, Ledger()
    transaction = queue_legacy_estimate(store, tmp_path, settings, ledger)
    original = deepcopy(store.one("SELECT decision FROM transactions")["decision"])
    monkeypatch.setattr(
        ledger,
        "categories",
        lambda: [
            {"id": "replacement", "type": 2, "parentId": "parent", "path": "其他杂项 → 待分类"}
        ],
    )
    monkeypatch.setattr(
        ledger, "rates", lambda: pytest.fail("frozen quote must not be fetched again")
    )
    write_queued(store, ledger, True)
    issue = store.one("SELECT * FROM issues WHERE code='write_preflight_failed'")
    assert issue["data"]["version"] == 1
    new_settings = settings.model_copy(
        update={"rules": (Rule(merchant_pattern="合成", category_id="replacement"),)}
    )
    resolve(store, ledger, issue["id"], 1, "retry", "synthetic rule correction")
    assert (
        store.one("SELECT decision FROM transactions")["decision"]["reclassify_requested"] is True
    )
    classify_pending(store, new_settings, ledger, None)
    decision = store.one("SELECT decision FROM transactions")["decision"]
    assert decision["payload"] == {**original["payload"], "categoryId": "replacement"}
    assert decision["rate_snapshot"] == original["rate_snapshot"]
    assert decision["reclassify_requested"] is False
    write_queued(store, ledger, True)
    assert len(ledger.create_calls) == 1 and ledger.create_calls[0]["categoryId"] == "replacement"
    assert store.one("SELECT marker FROM transactions")["marker"] == transaction["marker"]


def test_reclassification_keeps_explicit_account_correction(
    database, tmp_path, settings, monkeypatch
):
    store, ledger = database.store, Ledger()
    transaction = queue_legacy_estimate(store, tmp_path, settings, ledger)
    monkeypatch.setattr(
        ledger,
        "accounts",
        lambda: [
            {"id": account, "type": 1, "currency": "CNY"} for account in ("account", "corrected")
        ],
    )
    store.issue("synthetic_mapping", transaction["id"], {"version": 1})
    issue = store.one("SELECT * FROM issues WHERE code='synthetic_mapping'")
    resolve(
        store,
        ledger,
        issue["id"],
        1,
        "retry",
        "synthetic account correction",
        account_id="corrected",
    )
    classify_pending(store, settings, ledger, None)
    assert store.one("SELECT payload FROM jobs")["payload"]["sourceAccountId"] == "corrected"
    assert store.one("SELECT payload FROM jobs")["payload"]["sourceAmount"] == 7000


def test_ai_reclassification_only_follows_explicit_retry(database, tmp_path, settings):
    from unittest.mock import Mock

    store, ledger = database.store, Ledger()
    import_daily(store, tmp_path, settings)
    ai_settings = settings.model_copy(update={"classification_mode": "ai"})
    ai = Mock()
    ai.classify.return_value = {
        "classification_status": "matched",
        "category_id": "edited-category",
        "reason": "synthetic",
    }
    classify_pending(store, ai_settings, ledger, ai)
    classify_pending(store, ai_settings, ledger, ai)
    recover_dispatching(store)
    classify_pending(store, ai_settings, ledger, ai)
    assert ai.classify.call_count == 1
    transaction = store.one("SELECT * FROM transactions")
    store.issue("synthetic_retry", transaction["id"], {"version": 1})
    issue = store.one("SELECT * FROM issues WHERE code='synthetic_retry'")
    resolve(store, ledger, issue["id"], 1, "retry", "synthetic retry")
    classify_pending(store, ai_settings, ledger, ai)
    classify_pending(store, ai_settings, ledger, ai)
    assert ai.classify.call_count == 2


@pytest.mark.parametrize("state", ["dispatching", "unknown"])
def test_retry_during_uncertain_write_does_not_reclassify(database, tmp_path, settings, state):
    from unittest.mock import Mock

    store, ledger = database.store, Ledger()
    transaction = queue(store, tmp_path, settings, ledger)
    store.execute("UPDATE transactions SET state=%s", (state,))
    store.execute("UPDATE jobs SET status=%s", (state,))
    store.issue("synthetic_retry", transaction["id"], {"version": 1})
    issue = store.one("SELECT * FROM issues WHERE code='synthetic_retry'")
    resolve(store, ledger, issue["id"], 1, "retry", "synthetic uncertain write")
    ai = Mock()
    classify_pending(store, settings, ledger, ai)
    ai.classify.assert_not_called()
    assert store.one("SELECT state,version FROM transactions") == {"state": state, "version": 1}
    assert not store.one("SELECT decision FROM transactions")["decision"].get(
        "reclassify_requested"
    )


@pytest.mark.parametrize("settled,actual", [(True, "70.00"), (False, "70.00"), (False, "72.00")])
def test_foreign_date_change_reported_while_authorized_amount_settlement_continues(
    database, tmp_path, settings, settled, actual
):
    store, ledger = database.store, Ledger()
    transaction = queue_legacy_estimate(store, tmp_path, settings, ledger)
    write_queued(store, ledger, True)
    target = store.one("SELECT target_id FROM transactions")["target_id"]
    statement(store, transaction, actual)
    if settled:
        reconcile(store, ledger, tmp_path / "reports")
        write_queued(store, ledger, True)
    changed_time = ledger.records[target]["time"] + 86400
    ledger.records[target]["time"] = changed_time
    reconcile(store, ledger, tmp_path / "reports")
    assert store.one("SELECT status FROM reconciliation_items")["status"] == "target_changed"
    write_queued(store, ledger, True)
    reconcile(store, ledger, tmp_path / "reports")
    assert ledger.records[target]["sourceAmount"] == int(Decimal(actual) * 100)
    assert ledger.records[target]["time"] == changed_time
    assert store.one("SELECT status FROM reconciliation_items")["status"] == "target_changed"
    assert store.one("SELECT settlement FROM transactions")["settlement"] is not None


def test_success_closes_transient_preflight_issue_without_manual_resolution(
    database, tmp_path, settings, monkeypatch
):
    store, ledger = database.store, Ledger()
    queue(store, tmp_path, settings, ledger)
    accounts = ledger.accounts
    monkeypatch.setattr(ledger, "accounts", lambda: [])
    write_queued(store, ledger, True)
    issue = store.one("SELECT * FROM issues WHERE code='write_preflight_failed'")
    assert not issue["resolved"] and issue["data"]["version"] == 1
    monkeypatch.setattr(ledger, "accounts", accounts)
    write_queued(store, ledger, True)
    assert store.one("SELECT resolved FROM issues WHERE code='write_preflight_failed'")["resolved"]


@pytest.mark.parametrize(
    "issue_job_offset,issue_version,job_version,resolved",
    [
        (0, 1, 1, True),
        (1, 1, 1, False),
        (0, 2, 1, False),
        (0, None, 1, True),
        (0, None, 2, False),
    ],
)
def test_completion_closes_only_provably_same_preflight_operation(
    database, tmp_path, settings, issue_job_offset, issue_version, job_version, resolved
):
    store, ledger = database.store, Ledger()
    transaction = queue(store, tmp_path, settings, ledger)
    store.execute("UPDATE transactions SET version=%s", (job_version,))
    store.execute("UPDATE jobs SET version=%s", (job_version,))
    job = store.one("SELECT * FROM jobs")
    data = {"job_id": job["id"] + issue_job_offset}
    if issue_version is not None:
        data["version"] = issue_version
    store.issue("write_preflight_failed", transaction["id"], data)
    complete(store, job, {"id": "synthetic-target", **job["payload"]})
    assert (
        store.one("SELECT resolved FROM issues WHERE code='write_preflight_failed'")["resolved"]
        is resolved
    )
