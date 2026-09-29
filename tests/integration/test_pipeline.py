"""Real PostgreSQL, isolated schemas, synthetic mail and an in-process ledger."""

from ezbookkeeping_importer.application.records import source_row
from ezbookkeeping_importer.application.maintenance import issues, status
from copy import deepcopy
from datetime import date, datetime, timezone
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
from ezbookkeeping_importer.application.resolve import resolve
from ezbookkeeping_importer.application.write import recover_dispatching, write_queued
from ezbookkeeping_importer.config import MailSettings, Settings
from ezbookkeeping_importer.domain.errors import Conflict


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

    def get(self, ledger_transaction_id):
        return deepcopy(self.records.get(str(ledger_transaction_id)))

    settlement_payload = staticmethod(EzBookkeepingClient.settlement_payload)

    def modify(self, payload):
        self.modify_calls.append(deepcopy(payload))
        self.records[payload["id"]].update(deepcopy(payload))
        return self.get(payload["id"])


def raw_daily(count=1, currency="CNY", message_id="one", reverse=False, amount="10.00"):
    message = EmailMessage()
    message["Subject"] = "每日信用管家"
    message["Message-ID"] = f"<{message_id}@example.test>"
    rows = [
        "<div><b>12:00:00</b><b>"
        + currency
        + " "
        + amount
        + "</b><b>尾号1234 消费 合成商户</b></div>"
    ] * count
    if reverse:
        rows.reverse()
    message.set_content("<p>2026/01/01 您的消费明细如下：</p>" + "".join(rows), subtype="html")
    return message.as_bytes()


def queue_legacy_estimate(store, tmp_path, settings, ledger):
    """An already persisted pre-original-currency import_decision, not a new FX import."""
    import_daily(store, tmp_path, settings, currency="USD")
    transaction = store.one("SELECT * FROM bank_transactions")
    payload = {
        "type": 3,
        "sourceAccountId": "account",
        "sourceAmount": 7000,
        "categoryId": "category",
        "time": int(datetime.fromisoformat(source_row(transaction)["occurred_at"]).timestamp()),
        "utcOffset": 480,
        "comment": transaction["source_marker"] + " synthetic legacy CNY estimate",
        "clientSessionId": transaction["source_marker"],
    }
    import_decision = {
        "payload": payload,
        "classification": {"classification_status": "unmatched", "category_id": None},
        "rate_snapshot": {"dataSource": "synthetic legacy quote", "adoptedRate": "7"},
    }
    with store.transaction():
        store.execute(
            "UPDATE bank_transactions SET import_decision=%s,import_status='queued' WHERE id=%s",
            (import_decision, transaction["id"]),
        )
        store.execute(
            "INSERT INTO background_task(bank_transaction_id,task_type,decision_version,operation_key,payload) VALUES (%s,'create',1,%s,%s)",
            (transaction["id"], transaction["source_marker"], payload),
        )
    return store.one("SELECT * FROM bank_transactions")


def test_timeout_after_commit_is_verified_without_second_post(database, tmp_path, settings):
    store, ledger = database.store, Ledger("timeout_commit")
    queue(store, tmp_path, settings, ledger)
    write_queued(store, ledger)
    write_queued(store, ledger)
    assert len(ledger.create_calls) == 1
    assert store.one("SELECT import_status FROM bank_transactions")["import_status"] == "booked"
    assert store.one("SELECT outcome FROM ledger_write_attempt")["outcome"] == "confirmed"


def test_postgres_worker_lock_is_exclusive_and_released(database):
    first, second = database.store, database.connect()
    assert first.lock_worker()
    assert not second.lock_worker()
    first.close()
    import time

    deadline = time.monotonic() + 1
    while not second.lock_worker():
        assert time.monotonic() < deadline
        time.sleep(0.01)


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
        uid_validity, messages = self.data[folder]
        return uid_validity, [uid for uid in messages if uid > after_uid or since is not None]

    def fetch_headers_batch(self, folder, uids):
        return {uid: self.fetch_headers(folder, uid) for uid in uids}

    def fetch_headers(self, folder, uid):
        return self.data[folder][1][uid].split(b"\n\n", 1)[0] + b"\n\n"

    def fetch(self, folder, uid):
        self.fetches.append((folder, uid))
        uid_validity, messages = self.data[folder]
        task = self.observer.one(
            "SELECT id FROM email_source_item WHERE folder=%s AND uid_validity=%s AND uid=%s",
            (folder, uid_validity, uid),
        )
        self.persisted_before_fetch.append(task is not None)
        if (folder, uid) in self.interruptions:
            raise KeyboardInterrupt("synthetic process interruption")
        if (folder, uid) in self.failures:
            raise TimeoutError("synthetic read failure")
        return messages[uid]


def bank_raw():
    return b"From: ccsvc@message.cmbchina.com\n" + raw_daily()


def ingest_mail(store, evidence, raw, settings, folder="synthetic", uid=None, accept=True):
    if uid is None:
        uid = store.one("SELECT coalesce(max(uid),0)+1 AS uid FROM email_source_item")["uid"]
    item = store.one(
        """INSERT INTO email_source_item(source_id,folder,uid_validity,uid)
        VALUES (%s,%s,'fixture',%s) RETURNING id""",
        (settings.mail.source_id, folder, uid),
    )
    identifier = ingest(store, evidence, raw, settings, item["id"])
    if accept and issues(store, "email_source_item", str(item["id"])):
        resolve(
            store,
            Ledger(),
            "email_source_item",
            str(item["id"]),
            1,
            "accept-source",
            "合成来源已人工核实",
        )
    return identifier


def import_daily(store, tmp_path, settings, **kwargs):
    identifier = ingest_mail(
        store, EvidenceStore(tmp_path / "evidence"), raw_daily(**kwargs), settings
    )
    parse_pending(store, BankParser(context="synthetic"))
    return identifier


def queue(store, tmp_path, settings, ledger, **kwargs):
    import_daily(store, tmp_path, settings, **kwargs)
    classify_pending(store, settings, ledger, None)
    assert store.one("SELECT count(*) AS n FROM background_task WHERE status='queued'")["n"] == 1, (
        issues(store)
    )
    return store.one("SELECT * FROM bank_transactions")


def statement(store, transaction, amount, *, rows=None, key="monthly-report"):
    row = {
        **source_row(transaction),
        "row_key": "monthly-row",
        "original_currency": None,
        "settlement_amount": amount,
        "settlement_currency": "CNY",
    }
    email_id = __import__("hashlib").sha256(key.encode()).hexdigest()
    with store.transaction():
        store.execute(
            "INSERT INTO email(id,raw_path,parse_status) VALUES (%s,'synthetic','parsed')",
            (email_id,),
        )
        store.execute(
            """INSERT INTO bank_report(report_key,source_id,bank_code,report_type,
            period_start,period_end,source_email_id,parser_version,content_fingerprint,content)
            VALUES (%s,'synthetic','cmb','monthly','2025-12-01','2026-01-31',%s,'fixture','synthetic',%s)""",
            (
                key,
                email_id,
                {"rows": [row] if rows is None else rows, "controls": {}, "extensions": {}},
            ),
        )
        store.execute("UPDATE email SET report_key=%s WHERE id=%s", (key, email_id))


def test_ingest_replay_preserves_identical_real_multiplicity(database, tmp_path, settings):
    store = database.store
    first = import_daily(store, tmp_path, settings, count=2)
    import_daily(store, tmp_path, settings, count=2)
    second = import_daily(store, tmp_path, settings, count=2, message_id="resent")
    assert first != second
    assert store.one("SELECT count(*) AS n FROM email")["n"] == 2
    assert store.one("SELECT count(*) AS n FROM bank_report")["n"] == 1
    txs = store.all("SELECT * FROM bank_transactions")
    assert len(txs) == 2
    assert all(len(tx["id"]) == 16 and tx["source_marker"] == "ebki-" + tx["id"] for tx in txs)
    assert not issues(store)


def test_resolve_version_cancels_queued_task_and_blocks_stale_decision(
    database, tmp_path, settings
):
    store, ledger = database.store, Ledger()
    tx = queue(store, tmp_path, settings, ledger)
    store.execute(
        "UPDATE bank_transactions SET import_error=%s",
        ({"code": "synthetic_review", "detail": "核实", "decision_version": 1},),
    )
    with pytest.raises(Conflict, match="stale"):
        resolve(store, ledger, "bank_transactions", tx["id"], 0, "ignore", "合成核实")
    resolve(store, ledger, "bank_transactions", tx["id"], 1, "ignore", "合成核实")
    write_queued(store, ledger)
    assert ledger.create_calls == []
    updated = store.one("SELECT * FROM bank_transactions")
    assert (updated["decision_version"], updated["import_status"]) == (2, "ignored")
    assert updated["last_resolution"]["reason"] == "合成核实"
    assert store.one("SELECT status FROM background_task")["status"] == "cancelled"


def test_restart_recovers_dispatching_as_unknown_without_post(database, tmp_path, settings):
    store, ledger = database.store, Ledger()
    queue(store, tmp_path, settings, ledger)
    task = store.one("SELECT * FROM background_task")
    with store.transaction():
        store.execute("UPDATE background_task SET status='dispatching'")
        store.execute("UPDATE bank_transactions SET import_status='dispatching'")
        store.execute(
            """INSERT INTO ledger_write_attempt(task_id,decision_version,request,outcome)
            VALUES (%s,1,%s,'unknown')""",
            (task["id"], task["payload"]),
        )
    store.close()
    restarted = database.connect()
    recover_dispatching(restarted)
    write_queued(restarted, ledger)
    assert ledger.create_calls == []
    assert (
        restarted.one("SELECT import_status FROM bank_transactions")["import_status"] == "unknown"
    )
    assert restarted.one("SELECT status FROM background_task")["status"] == "unknown"
    assert restarted.one("SELECT count(*) AS n FROM ledger_write_attempt")["n"] == 1
    assert any(item["entity_type"] == "background_task" for item in issues(restarted))


def test_attempt_is_committed_before_network_and_keeps_exact_request(database, tmp_path, settings):
    store, ledger = database.store, Ledger()
    queue(store, tmp_path, settings, ledger)
    peer = database.connect()
    seen = []
    ledger.before_create = lambda: seen.extend(peer.all("SELECT * FROM ledger_write_attempt"))
    write_queued(store, ledger)
    assert len(seen) == 1 and seen[0]["outcome"] == "unknown"
    assert seen[0]["request"] == ledger.create_calls[0]
    attempt = store.one("SELECT * FROM ledger_write_attempt")
    assert attempt["decision_version"] == 1 and attempt["outcome"] == "confirmed"


def test_unknown_empty_or_ambiguous_never_reposts(database, tmp_path, settings):
    store, ledger = database.store, Ledger("timeout_empty")
    tx = queue(store, tmp_path, settings, ledger)
    write_queued(store, ledger)
    write_queued(store, ledger)
    assert len(ledger.create_calls) == 1
    assert store.one("SELECT import_status FROM bank_transactions")["import_status"] == "unknown"
    payload = tx["import_decision"]["payload"]
    ledger.records = {str(i): {"id": str(i), **payload} for i in range(2)}
    write_queued(store, ledger)
    assert len(ledger.create_calls) == 1
    assert store.one("SELECT import_status FROM bank_transactions")["import_status"] == "unknown"


def test_preflight_success_removes_only_current_task_error(
    database, tmp_path, settings, monkeypatch
):
    store, ledger = database.store, Ledger()
    queue(store, tmp_path, settings, ledger)
    accounts = ledger.accounts
    monkeypatch.setattr(ledger, "accounts", lambda: [])
    write_queued(store, ledger)
    assert any(i["code"] == "write_preflight_failed" for i in issues(store))
    assert store.one("SELECT count(*) AS n FROM ledger_write_attempt")["n"] == 0
    monkeypatch.setattr(ledger, "accounts", accounts)
    write_queued(store, ledger)
    assert not issues(store)
    assert len(ledger.create_calls) == 1


def test_initial_boundary_stays_fixed_as_incremental_scan_advances(database, tmp_path, settings):
    store = database.store
    mail = Mail(database.connect(), {"INBOX": ("v1", {1: bank_raw(), 5: bank_raw()})})
    evidence = EvidenceStore(tmp_path / "evidence")
    mail.failures.add(("INBOX", 1))
    collect(store, mail, evidence, settings)
    cp = store.one("SELECT * FROM email_sync_checkpoint")
    assert cp["registered_uid"] == cp["initial_scan_upper_uid"] == 5
    mail.data["INBOX"][1][9] = bank_raw()
    mail.failures = {("INBOX", 9)}
    collect(store, mail, evidence, settings)
    cp = store.one("SELECT * FROM email_sync_checkpoint")
    assert cp["registered_uid"] == 9 and cp["initial_scan_upper_uid"] == 5
    assert store.one("SELECT status FROM email_source_item WHERE uid=1")["status"] == "collected"
    assert store.one("SELECT status FROM email_source_item WHERE uid=9")["status"] == "failed"
    assert status(store)["email_sync_checkpoint"][0]["historical_complete"]
    mail.data["INBOX"] = ("v2", {2: bank_raw()})
    collect(store, mail, evidence, settings)
    cp = store.one("SELECT * FROM email_sync_checkpoint")
    assert cp["uid_validity"] == "v2" and cp["initial_scan_upper_uid"] == 2


def test_range_scan_keeps_normal_checkpoint_and_unrelated_pending_items(
    database, tmp_path, settings, monkeypatch
):
    store = database.store
    mail = Mail(database.connect(), {"INBOX": ("v1", {1: bank_raw()})})
    evidence = EvidenceStore(tmp_path / "evidence")
    collect(store, mail, evidence, settings)
    before = store.one("SELECT * FROM email_sync_checkpoint")
    mail.data["INBOX"] = ("v2", {2: bank_raw(), 3: bank_raw()})
    store.execute(
        "INSERT INTO email_source_item(source_id,folder,uid_validity,uid) VALUES ('synthetic','INBOX','v2',3)"
    )
    monkeypatch.setattr(mail, "scan", lambda *args, **kwargs: ("v2", [2]))
    collect(store, mail, evidence, settings, date(2026, 1, 1), date(2026, 1, 1))
    assert store.one("SELECT * FROM email_sync_checkpoint") == before
    assert store.one("SELECT status FROM email_source_item WHERE uid=3")["status"] == "pending"


def test_collection_interruption_resumes_pending_snapshot(database, tmp_path, settings):
    store = database.store
    mail = Mail(database.connect(), {"INBOX": ("v1", {1: bank_raw(), 2: bank_raw()})})
    mail.interruptions.add(("INBOX", 2))
    evidence = EvidenceStore(tmp_path / "evidence")
    with pytest.raises(KeyboardInterrupt):
        collect(store, mail, evidence, settings)
    assert store.one("SELECT status FROM email_source_item WHERE uid=2")["status"] == "pending"
    store.close()
    fresh = database.connect()
    mail.interruptions.clear()
    mail.fetches.clear()
    collect(fresh, mail, evidence, settings)
    assert mail.fetches == [("INBOX", 2)]
    assert fresh.one("SELECT count(*) AS n FROM email")["n"] == 1


def test_rejected_create_retry_preserves_operation_and_frozen_amount(
    database, tmp_path, settings, monkeypatch
):
    from ezbookkeeping_importer.domain.errors import LedgerRejected

    store, ledger = database.store, Ledger()
    tx = queue_legacy_estimate(store, tmp_path, settings, ledger)
    original = store.one("SELECT * FROM background_task")
    create = ledger.create
    monkeypatch.setattr(
        ledger, "create", lambda payload: (_ for _ in ()).throw(LedgerRejected(200008))
    )
    write_queued(store, ledger)
    task = store.one("SELECT * FROM background_task")
    assert task["status"] == "rejected"
    resolve(store, ledger, "background_task", str(task["id"]), 1, "retry", "远端条件已修复")
    classify_pending(store, settings, ledger, None)
    monkeypatch.setattr(ledger, "create", create)
    write_queued(store, ledger)
    assert ledger.create_calls[0]["sourceAmount"] == 7000
    assert ledger.create_calls[0]["clientSessionId"] == tx["source_marker"]
    assert (
        store.one("SELECT operation_key FROM background_task")["operation_key"]
        == original["operation_key"]
    )
    attempts = store.all("SELECT * FROM ledger_write_attempt ORDER BY id")
    assert [a["outcome"] for a in attempts] == ["rejected", "confirmed"]


def test_existing_remote_link_has_zero_actual_attempts(database, tmp_path, settings):
    store, ledger = database.store, Ledger()
    tx = queue(store, tmp_path, settings, ledger)
    payload = tx["import_decision"]["payload"]
    ledger.records["existing"] = {"id": "existing", **payload}
    store.execute("DELETE FROM background_task")
    store.execute("UPDATE bank_transactions SET import_status='pending',import_decision=NULL")
    classify_pending(store, settings, ledger, None)
    write_queued(store, ledger)
    assert ledger.create_calls == []
    assert store.one("SELECT count(*) AS n FROM ledger_write_attempt")["n"] == 0
    assert (
        store.one("SELECT completion_method FROM background_task")["completion_method"]
        == "existing_link"
    )


def test_stale_completion_cannot_confirm_a_new_decision(database, tmp_path, settings):
    from ezbookkeeping_importer.application.write import complete

    store, ledger = database.store, Ledger()
    queue(store, tmp_path, settings, ledger)
    old = store.one("SELECT * FROM background_task")
    store.execute("UPDATE bank_transactions SET decision_version=2")
    store.execute("UPDATE background_task SET decision_version=2")
    complete(store, old, {"id": "old-target", **old["payload"]})
    tx = store.one("SELECT * FROM bank_transactions")
    assert tx["decision_version"] == 2 and tx["ledger_transaction_id"] is None
    assert store.one("SELECT status FROM background_task")["status"] == "queued"
