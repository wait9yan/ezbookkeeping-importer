"""来源资格属于 IMAP 位置，原件去重不能复制认证权限。"""

import pytest
import test_pipeline as pipeline
from ezbookkeeping_importer.application.collect import ingest
from ezbookkeeping_importer.application.parse import parse_pending
from ezbookkeeping_importer.application.resolve import resolve
from ezbookkeeping_importer.application.maintenance import issues
from ezbookkeeping_importer.adapters.evidence_store import EvidenceStore
from ezbookkeeping_importer.adapters.banks.cmb import BankParser
from ezbookkeeping_importer.domain.errors import ImporterError

database = pipeline.database
settings = pipeline.settings


@pytest.mark.parametrize("trusted_first", [False, True])
def test_trust_arrival_order_does_not_block_same_business_source(
    database, tmp_path, settings, trusted_first
):
    store = database.store
    evidence = EvidenceStore(tmp_path)
    raw = pipeline.raw_daily()
    first = pipeline.ingest_mail(store, evidence, raw, settings, folder="a", accept=trusted_first)
    parse_pending(store, BankParser(context="synthetic"))
    assert store.one("SELECT count(*) AS n FROM bank_transactions")["n"] == int(trusted_first)
    second = pipeline.ingest_mail(
        store, evidence, raw, settings, folder="b", accept=not trusted_first
    )
    parse_pending(store, BankParser(context="synthetic"))
    assert first == second
    assert store.one("SELECT count(*) AS n FROM bank_transactions")["n"] == 1
    sources = store.all("SELECT * FROM email_source_item ORDER BY id")
    assert len(sources) == 2
    assert all(s["source_status"] == "requires_acceptance" for s in sources)
    assert sum(s["accepted_at"] is not None for s in sources) == 1
    assert not [i for i in issues(store) if i["code"] == "source_acceptance"]


def test_cross_business_source_same_raw_is_explicitly_rejected(database, tmp_path, settings):
    store = database.store
    raw = pipeline.raw_daily()
    pipeline.ingest_mail(store, EvidenceStore(tmp_path), raw, settings)
    other = settings.model_copy(
        update={"mail": settings.mail.model_copy(update={"source_id": "other"})}
    )
    item = store.one("""INSERT INTO email_source_item(source_id,folder,uid_validity,uid)
        VALUES ('other','b','v',1) RETURNING id""")
    with pytest.raises(ImporterError, match="source"):
        ingest(store, EvidenceStore(tmp_path), raw, other, item["id"])
    assert store.one(
        "SELECT status,email_id FROM email_source_item WHERE id=%s", (item["id"],)
    ) == {"status": "pending", "email_id": None}
    assert store.one("SELECT count(*) AS n FROM email")["n"] == 1


def test_acceptance_reason_and_reparse_are_atomic(database, tmp_path, settings, monkeypatch):
    store = database.store
    email_id = pipeline.ingest_mail(
        store, EvidenceStore(tmp_path), pipeline.raw_daily(), settings, accept=False
    )
    parse_pending(store, BankParser(context="synthetic"))
    item = store.one("SELECT * FROM email_source_item")
    execute = store.execute

    def fail(query, params=()):
        if "UPDATE email SET" in query:
            raise RuntimeError("synthetic reparse failure")
        return execute(query, params)

    monkeypatch.setattr(store, "execute", fail)
    with pytest.raises(RuntimeError):
        resolve(
            store,
            pipeline.Ledger(),
            "email_source_item",
            str(item["id"]),
            1,
            "accept-source",
            "核实",
        )
    assert store.one("SELECT accepted_at FROM email_source_item")["accepted_at"] is None
    assert (
        store.one("SELECT parse_status FROM email WHERE id=%s", (email_id,))["parse_status"]
        == "parsed"
    )


def test_missing_and_malformed_headers_keep_original_bytes(database, tmp_path, settings):
    store = database.store
    raw = b"Date: not-a-date\n\nraw body"
    identifier = pipeline.ingest_mail(store, EvidenceStore(tmp_path), raw, settings, accept=False)
    email = store.one("SELECT * FROM email WHERE id=%s", (identifier,))
    assert email["header_message_id"] is None and email["sent_at"] is None
    from pathlib import Path

    assert Path(email["raw_path"]).read_bytes() == raw
