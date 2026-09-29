from ezbookkeeping_importer.application.maintenance import status, issues
from ezbookkeeping_importer.adapters.evidence_store import EvidenceStore
from ezbookkeeping_importer.application.collect import collect
from ezbookkeeping_importer.application.ports import HEADER_BATCH_SIZE
import test_pipeline as pipeline
from test_mail_prefilter import raw_message

database = pipeline.database
settings = pipeline.settings


class BatchMail(pipeline.Mail):
    def __init__(self, observer, folders):
        super().__init__(observer, folders)
        self.batches = []
        self.fail_batch_starts = set()
        self.omissions = set()

    def fetch_headers(self, folder, uid):
        raise AssertionError("collector must use the batch header API")

    def fetch_headers_batch(self, folder, uids):
        self.batches.append((folder, uids))
        for uid in uids:
            assert self.observer.one(
                "SELECT id FROM email_source_item WHERE folder=%s AND uid_validity=%s AND uid=%s",
                (folder, self.data[folder][0], uid),
            )
        if uids[0] in self.fail_batch_starts:
            raise TimeoutError("synthetic batch failure")
        return {
            uid: pipeline.Mail.fetch_headers(self, folder, uid)
            for uid in uids
            if uid not in self.omissions
        }


def test_pending_locations_are_paged_in_batches_without_rereading_finished_rows(
    database, settings, tmp_path
):
    store = database.store
    ordinary = raw_message("friend@example.test", "ordinary mail")
    bank = raw_message("ccsvc@message.cmbchina.com", "bank unknown template")
    messages = {uid: bank if uid == 51 else ordinary for uid in range(1, 106)}
    mail = BatchMail(database.connect(), {"INBOX": ("valid", messages)})
    evidence = EvidenceStore(tmp_path / "evidence")
    collect(store, mail, evidence, settings)
    assert [len(uids) for _, uids in mail.batches] == [HEADER_BATCH_SIZE, HEADER_BATCH_SIZE, 5]
    assert mail.fetches == [("INBOX", 51)]
    assert (
        store.one("SELECT count(*) AS n FROM email_source_item WHERE status='skipped'")["n"] == 104
    )
    assert status(store)["email_sync_checkpoint"][0]["historical_complete"] is True
    collect(store, mail, evidence, settings)
    assert len(mail.batches) == 3
    assert mail.fetches == [("INBOX", 51)]


def test_failed_batch_does_not_starve_later_batches_and_retry_skips_completed_rows(
    database, settings, tmp_path
):
    store = database.store
    ordinary = raw_message("friend@example.test", "ordinary mail")
    mail = BatchMail(
        database.connect(), {"INBOX": ("valid", {uid: ordinary for uid in range(1, 103)})}
    )
    mail.fail_batch_starts.add(1)
    evidence = EvidenceStore(tmp_path / "evidence")
    collect(store, mail, evidence, settings)
    assert [uids[0] for _, uids in mail.batches] == [1, 51, 101]
    assert store.one("SELECT count(*) AS n FROM email_source_item WHERE status='failed'")["n"] == 50
    assert (
        store.one("SELECT count(*) AS n FROM email_source_item WHERE status='skipped'")["n"] == 52
    )
    assert status(store)["email_sync_checkpoint"][0]["historical_complete"] is False
    mail.fail_batch_starts.clear()
    mail.batches.clear()
    collect(store, mail, evidence, settings)
    assert mail.batches == [("INBOX", tuple(range(1, 51)))]
    assert not [i for i in issues(store) if i["code"] == "download_failed"]
    assert status(store)["email_sync_checkpoint"][0]["historical_complete"] is True


def test_missing_uid_stays_failed_while_valid_partial_headers_are_processed(
    database, settings, tmp_path
):
    store = database.store
    ordinary = raw_message("friend@example.test", "ordinary mail")
    bank = raw_message("ccsvc@message.cmbchina.com", "bank unknown template")
    mail = BatchMail(database.connect(), {"INBOX": ("valid", {1: ordinary, 2: bank, 3: bank})})
    mail.omissions.add(2)
    evidence = EvidenceStore(tmp_path / "evidence")
    collect(store, mail, evidence, settings)
    states = {
        row["uid"]: row["status"] for row in store.all("SELECT uid,status FROM email_source_item")
    }
    assert states == {1: "skipped", 2: "failed", 3: "collected"}
    assert mail.fetches == [("INBOX", 3)]
    issue = next(i for i in issues(store) if i["code"] == "download_failed")
    assert issue["entity_type"] == "email_source_item" and issue["detail"]
    mail.omissions.clear()
    mail.batches.clear()
    collect(store, mail, evidence, settings)
    assert mail.batches == [("INBOX", (2,))]
    assert mail.fetches == [("INBOX", 3), ("INBOX", 2)]
    assert not [i for i in issues(store) if i["code"] == "download_failed"]


def test_non_bank_batch_uses_two_updates_and_one_bulk_registration(
    database, settings, tmp_path, monkeypatch
):
    store = database.store
    ordinary = raw_message("friend@example.test", "ordinary mail")
    mail = BatchMail(
        database.connect(), {"INBOX": ("valid", dict.fromkeys(range(1, 51), ordinary))}
    )
    statements = []
    execute = store.execute

    def record(sql, params=()):
        statements.append(sql)
        return execute(sql, params)

    monkeypatch.setattr(store, "execute", record)
    collect(store, mail, EvidenceStore(tmp_path / "evidence"), settings)
    assert len([sql for sql in statements if "INSERT INTO email_source_item" in sql]) == 1
    assert len([sql for sql in statements if "UPDATE email_source_item" in sql]) == 1
    assert (
        store.one("SELECT count(*) AS n FROM email_source_item WHERE status='skipped'")["n"] == 50
    )
    assert mail.fetches == []
    # A repeated location snapshot is idempotent and never resets completed states.
    collect(store, mail, EvidenceStore(tmp_path / "evidence"), settings)
    assert store.one("SELECT count(*) AS n FROM email_source_item")["n"] == 50
    assert len(mail.batches) == 1


def test_batch_ignore_rolls_back_downloads_when_issue_update_fails(
    database, settings, tmp_path, monkeypatch
):
    import pytest

    store = database.store
    ordinary = raw_message("friend@example.test", "ordinary mail")
    mail = BatchMail(database.connect(), {"INBOX": ("valid", {1: ordinary, 2: ordinary})})
    evidence = EvidenceStore(tmp_path / "evidence")
    execute = store.execute

    def fail(sql, params=()):
        if "UPDATE email_source_item" in sql:
            raise RuntimeError("synthetic database write failure")
        return execute(sql, params)

    monkeypatch.setattr(store, "execute", fail)
    with pytest.raises(RuntimeError, match="synthetic database"):
        collect(store, mail, evidence, settings)
    assert {row["status"] for row in store.all("SELECT status FROM email_source_item")} == {
        "pending"
    }
    assert status(store)["email_sync_checkpoint"][0]["historical_complete"] is False
    monkeypatch.setattr(store, "execute", execute)
    collect(store, mail, evidence, settings)
    assert {row["status"] for row in store.all("SELECT status FROM email_source_item")} == {
        "skipped"
    }
    assert status(store)["email_sync_checkpoint"][0]["historical_complete"] is True


def test_bulk_registration_failure_rolls_back_cursor_and_retries(
    database, settings, tmp_path, monkeypatch
):
    import pytest

    store = database.store
    ordinary = raw_message("friend@example.test", "ordinary mail")
    mail = BatchMail(database.connect(), {"INBOX": ("valid", {1: ordinary, 2: ordinary})})
    evidence = EvidenceStore(tmp_path / "evidence")
    execute = store.execute

    def fail(sql, params=()):
        result = execute(sql, params)
        if "INSERT INTO email_source_item" in sql:
            raise RuntimeError("synthetic registration failure")
        return result

    monkeypatch.setattr(store, "execute", fail)
    with pytest.raises(RuntimeError, match="synthetic registration"):
        collect(store, mail, evidence, settings)
    assert store.one("SELECT count(*) AS n FROM email_sync_checkpoint")["n"] == 0
    assert store.one("SELECT count(*) AS n FROM email_source_item")["n"] == 0
    assert mail.batches == []
    monkeypatch.setattr(store, "execute", execute)
    collect(store, mail, evidence, settings)
    assert store.one("SELECT count(*) AS n FROM email_source_item WHERE status='skipped'")["n"] == 2
