"""真实 PostgreSQL 的九表、注释、约束与报告事务边界。"""

import re

import psycopg
import pytest

import test_pipeline as pipeline
from ezbookkeeping_importer.application.parse import parse_pending
from ezbookkeeping_importer.adapters.banks.cmb import BankParser
from ezbookkeeping_importer.adapters.evidence_store import EvidenceStore
from ezbookkeeping_importer.domain.errors import ImporterError

database = pipeline.database
settings = pipeline.settings

TABLES = {
    "schema_version",
    "email_sync_checkpoint",
    "email_source_item",
    "email",
    "bank_report",
    "bank_transactions",
    "background_task",
    "ledger_write_attempt",
    "bank_statement_reconciliation",
}


def test_exact_nine_tables_and_all_catalog_comments_are_chinese(database):
    store = database.store
    tables = store.all("""SELECT c.relname,obj_description(c.oid) AS comment FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=current_schema() AND c.relkind='r'""")
    assert {row["relname"] for row in tables} == TABLES
    columns = store.all("""SELECT c.relname,a.attname,col_description(c.oid,a.attnum) AS comment
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=current_schema() AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped""")
    assert all(re.search(r"[\u4e00-\u9fff]", row["comment"] or "") for row in tables + columns)
    assert not {(r["relname"], r["attname"]) for r in columns} & {
        ("email", "parsed"),
        ("email", "source_status"),
        ("bank_transactions", "facts"),
    }


def test_repeated_initialization_preserves_evidence_and_facts(database, tmp_path, settings):
    store = database.store
    identifier = pipeline.import_daily(store, tmp_path, settings)
    before = store.all("SELECT * FROM bank_transactions")
    store.migrate()
    assert store.all("SELECT * FROM bank_transactions") == before
    assert store.one("SELECT source_email_id FROM bank_report")["source_email_id"] == identifier
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with store.transaction():
            store.execute("DELETE FROM email WHERE id=%s", (identifier,))


def test_incomplete_schema_is_rejected_instead_of_silently_repaired(database):
    store = database.store
    store.execute("ALTER TABLE email DROP COLUMN subject")
    with pytest.raises(ImporterError, match="schema contract differs"):
        store.migrate()


def test_deferred_report_pairing_fails_at_commit_and_rolls_back(database):
    store = database.store
    email_id = "a" * 64
    reached_commit = False
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with store.transaction():
            store.execute("INSERT INTO email(id,raw_path) VALUES (%s,'synthetic')", (email_id,))
            store.execute(
                """INSERT INTO bank_report(report_key,source_id,bank_code,report_type,
                content_fingerprint,source_email_id,parser_version,content)
                VALUES ('r','synthetic','cmb','daily','f',%s,'test','{}')""",
                (email_id,),
            )
            reached_commit = True
    assert reached_commit
    assert store.one("SELECT count(*) AS n FROM email")["n"] == 0
    assert store.one("SELECT count(*) AS n FROM bank_report")["n"] == 0


@pytest.mark.parametrize("status,extra", [("collected", ""), ("failed", ""), ("skipped", "")])
def test_source_status_requires_corresponding_evidence(database, status, extra):
    with pytest.raises(psycopg.errors.CheckViolation):
        database.store.execute(
            """INSERT INTO email_source_item(source_id,folder,uid_validity,uid,status)
            VALUES ('s','f','v',1,%s)""",
            (status,),
        )


def test_short_id_collision_rolls_back_entire_report(database, tmp_path, settings, monkeypatch):
    from ezbookkeeping_importer.application import parse as module

    store = database.store
    monkeypatch.setattr(module, "transaction_id", lambda *args: "A" * 16)
    email_id = pipeline.ingest_mail(
        store, EvidenceStore(tmp_path), pipeline.raw_daily(count=2), settings
    )
    parse_pending(store, BankParser(context="synthetic"))
    assert store.one("SELECT count(*) AS n FROM bank_report")["n"] == 0
    assert store.one("SELECT count(*) AS n FROM bank_transactions")["n"] == 0
    email = store.one("SELECT * FROM email WHERE id=%s", (email_id,))
    assert email["report_key"] is None and email["parse_status"] == "failed"
    assert email["parse_issues"][0]["code"] == "parse_failed"
    assert email["parser_version"] == "cmb-1"


def test_same_report_content_links_but_revision_never_overwrites_source(
    database, tmp_path, settings
):
    store = database.store
    a = pipeline.import_daily(store, tmp_path, settings)
    original = store.one("SELECT * FROM bank_report")
    b = pipeline.import_daily(store, tmp_path, settings, message_id="resent")
    raw = pipeline.raw_daily(message_id="revision", amount="11.00")
    c = pipeline.ingest_mail(store, EvidenceStore(tmp_path), raw, settings)
    parse_pending(store, BankParser(context="synthetic"))
    assert len({a, b, c}) == 3
    assert store.one("SELECT * FROM bank_report") == original
    assert (
        store.one("SELECT report_key FROM email WHERE id=%s", (b,))["report_key"]
        == original["report_key"]
    )
    assert (
        store.one("SELECT parse_issues FROM email WHERE id=%s", (c,))["parse_issues"][0]["code"]
        == "report_revision"
    )
    tx = store.one("SELECT * FROM bank_transactions")
    assert tx["original_amount"] == 10
    assert "source_locator" not in tx["source_details"]
    assert (
        not {"kind", "report_type", "report_date", "parser_version", "period_start", "period_end"}
        & original["content"].keys()
    )


def test_null_acceptance_reason_is_rejected(database, tmp_path, settings):
    store = database.store
    pipeline.import_daily(store, tmp_path, settings)
    with pytest.raises(psycopg.errors.CheckViolation):
        store.execute("UPDATE email_source_item SET accepted_at=now(),acceptance_reason=NULL")


def test_write_task_requires_nonnull_decision_version(database, tmp_path, settings):
    store = database.store
    pipeline.import_daily(store, tmp_path, settings)
    tx = store.one("SELECT id FROM bank_transactions")
    with pytest.raises(psycopg.errors.CheckViolation):
        store.execute(
            """INSERT INTO background_task(task_type,bank_transaction_id,decision_version,operation_key)
            VALUES ('create',%s,NULL,'invalid')""",
            (tx["id"],),
        )


def test_reconciliation_direction_constraints_and_partial_uniqueness(database, tmp_path, settings):
    store = database.store
    pipeline.import_daily(store, tmp_path, settings)
    tx = store.one("SELECT * FROM bank_transactions")
    pipeline.statement(store, tx, "10.00")
    with pytest.raises(psycopg.errors.CheckViolation):
        store.execute("""INSERT INTO bank_statement_reconciliation(statement_report_key,check_direction,
            match_status,ledger_check_status) VALUES ('monthly-report','statement_to_transaction','matched','not_checked')""")
    query = """INSERT INTO bank_statement_reconciliation(statement_report_key,check_direction,
        statement_row_key,match_status,ledger_check_status)
        VALUES ('monthly-report','statement_to_transaction','monthly-row','missing_source_transaction','not_checked')"""
    store.execute(query)
    with pytest.raises(psycopg.errors.UniqueViolation):
        store.execute(query)
    with pytest.raises(psycopg.errors.CheckViolation):
        store.execute(
            """UPDATE bank_statement_reconciliation SET bank_transaction_id=%s,
            ledger_check_status='query_failed',last_error='synthetic',actual_amount=10""",
            (tx["id"],),
        )


def test_missing_active_write_unique_index_is_rejected(database):
    database.store.execute("DROP INDEX active_write")
    with pytest.raises(ImporterError, match="schema contract differs"):
        database.store.migrate()


@pytest.mark.parametrize("failure", ["parser", "collision", "revision"])
def test_failed_parse_preserves_version_and_header_diagnostics(
    database, tmp_path, settings, monkeypatch, failure
):
    from ezbookkeeping_importer.application import parse as module

    store = database.store
    if failure == "revision":
        pipeline.import_daily(store, tmp_path, settings)
    raw = b"Date: invalid date\n" + pipeline.raw_daily(count=2, amount="11.00")
    email_id = pipeline.ingest_mail(store, EvidenceStore(tmp_path), raw, settings)
    parser = BankParser(context="synthetic")
    if failure == "collision":
        monkeypatch.setattr(module, "transaction_id", lambda *args: "A" * 16)
    elif failure == "parser":
        def broken_parse(raw):
            raise ValueError("synthetic parser failure")
        monkeypatch.setattr(parser, "parse", broken_parse)
    parse_pending(store, parser)
    email = store.one("SELECT * FROM email WHERE id=%s", (email_id,))
    assert email["parser_version"] == parser.version
    assert email["parsed_at"] is not None
    assert {issue["code"] for issue in email["parse_issues"]} == {
        "invalid_header_date",
        "report_revision" if failure == "revision" else "parse_failed",
    }
