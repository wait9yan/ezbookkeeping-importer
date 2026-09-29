"""合成邮件与隔离 PostgreSQL 验证邮件头预筛选、持久进度及重试。"""

from ezbookkeeping_importer.application.maintenance import status, issues
from email.message import EmailMessage

from ezbookkeeping_importer.adapters.evidence_store import EvidenceStore
from ezbookkeeping_importer.application.collect import collect

import test_pipeline

database = test_pipeline.database
settings = test_pipeline.settings


def raw_message(sender, subject):
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = subject
    message.set_content("合成全文证据")
    return message.as_bytes()


class HeaderMail(test_pipeline.Mail):
    def __init__(self, observer, folders):
        super().__init__(observer, folders)
        self.headers = []
        self.header_failures = set()

    def fetch_headers_batch(self, folder, uids):
        result = {}
        for uid in uids:
            try:
                result[uid] = self.fetch_headers(folder, uid)
            except TimeoutError:
                pass  # Simulate a valid partial server response with this UID omitted.
        return result

    def fetch_headers(self, folder, uid):
        self.headers.append((folder, uid))
        uid_validity = self.data[folder][0]
        assert self.observer.one(
            "SELECT id FROM email_source_item WHERE folder=%s AND uid_validity=%s AND uid=%s",
            (folder, uid_validity, uid),
        )
        if (folder, uid) in self.header_failures:
            raise TimeoutError("synthetic header failure")
        return super().fetch_headers(folder, uid)


def test_nonbank_mail_skips_full_body_but_persists_ignored_cursor(database, tmp_path, settings):
    store = database.store
    mail = HeaderMail(
        database.connect(),
        {
            "INBOX": (
                "v1",
                {
                    1: raw_message("friend@example.test", "普通邮件"),
                    2: raw_message("ccsvc@message.cmbchina.com", "银行未知新模板"),
                    3: raw_message("friend@example.test", "Fwd: 转发： 每日信用管家"),
                },
            )
        },
    )
    evidence = EvidenceStore(tmp_path / "evidence")
    collect(store, mail, evidence, settings)
    assert mail.headers == [("INBOX", 1), ("INBOX", 2), ("INBOX", 3)]
    assert mail.fetches == [("INBOX", 2), ("INBOX", 3)]
    assert store.one("SELECT status FROM email_source_item WHERE uid=1")["status"] == "skipped"
    assert store.one("SELECT count(*) AS n FROM email")["n"] == 2
    cursor = store.one("SELECT * FROM email_sync_checkpoint")
    assert (
        cursor["registered_uid"] == 3
        and status(store)["email_sync_checkpoint"][0]["historical_complete"]
    )
    collect(store, mail, evidence, settings)
    assert len(mail.headers) == 3 and len(mail.fetches) == 2


def test_header_and_body_failures_retry_after_cursor_advances(database, tmp_path, settings):
    store = database.store
    mail = HeaderMail(
        database.connect(),
        {
            "INBOX": (
                "v1",
                {
                    1: raw_message("friend@example.test", "普通邮件"),
                    2: raw_message("ccsvc@message.cmbchina.com", "每日信用管家"),
                },
            )
        },
    )
    mail.header_failures.add(("INBOX", 1))
    mail.failures.add(("INBOX", 2))
    evidence = EvidenceStore(tmp_path / "evidence")
    collect(store, mail, evidence, settings)
    assert store.one("SELECT count(*) AS n FROM email_source_item WHERE status='failed'")["n"] == 2
    assert store.one("SELECT registered_uid FROM email_sync_checkpoint")["registered_uid"] == 2
    assert not status(store)["email_sync_checkpoint"][0]["historical_complete"]
    assert mail.fetches == [("INBOX", 2)]
    mail.header_failures.clear()
    mail.failures.clear()
    store.close()
    restarted = database.connect()
    collect(restarted, mail, evidence, settings)
    assert restarted.one("SELECT status FROM email_source_item WHERE uid=1")["status"] == "skipped"
    assert (
        restarted.one("SELECT status FROM email_source_item WHERE uid=2")["status"] == "collected"
    )
    assert not [i for i in issues(restarted) if i["code"] == "download_failed"]
    assert status(restarted)["email_sync_checkpoint"][0]["historical_complete"]
    assert mail.headers == [("INBOX", 1), ("INBOX", 2)] * 2


def test_ignored_uid_is_rechecked_when_uidvalidity_changes(database, tmp_path, settings):
    store = database.store
    mail = HeaderMail(
        database.connect(),
        {
            "INBOX": (
                "old",
                {
                    4: raw_message("friend@example.test", "普通邮件"),
                },
            )
        },
    )
    evidence = EvidenceStore(tmp_path / "evidence")
    collect(store, mail, evidence, settings)
    mail.data["INBOX"] = ("new", {4: raw_message("ccsvc@message.cmbchina.com", "新模板")})
    collect(store, mail, evidence, settings)
    assert mail.headers == [("INBOX", 4)] * 2
    assert mail.fetches == [("INBOX", 4)]
    assert (
        store.one("SELECT status FROM email_source_item WHERE uid_validity='old'")["status"]
        == "skipped"
    )
    assert (
        store.one("SELECT status FROM email_source_item WHERE uid_validity='new'")["status"]
        == "collected"
    )
