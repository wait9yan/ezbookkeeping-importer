"""真实行锁验证单次命令快照和解析发布的一致性。"""

import json
from types import SimpleNamespace

import pytest

import test_recheck as recheck
import test_pipeline as pipeline
from ezbookkeeping_importer.adapters.banks.cmb import BankParser
from ezbookkeeping_importer.adapters.evidence_store import EvidenceStore
from ezbookkeeping_importer.application.issue_snapshot import (
    resolve_snapshot,
    snapshot_candidates,
    snapshot_issues,
)
from ezbookkeeping_importer.application.issue_interaction import issue_detail
from ezbookkeeping_importer.application.maintenance import issues
from ezbookkeeping_importer.application.parse import parse_pending
from ezbookkeeping_importer.application.recheck import request_snapshot_recheck
from ezbookkeeping_importer.domain.errors import Conflict, ImporterError

database = pipeline.database
settings = pipeline.settings


def test_json_roundtrip_can_resolve_without_replacing_frozen_payload(database, settings, tmp_path):
    store = database.store
    recheck.blocked_transactions(store, tmp_path, settings)
    snapshot = json.loads(json.dumps(snapshot_issues(store, "bank_transactions")))
    original = store.one("SELECT * FROM bank_transactions")
    snapshot["items"][0]["view"]["decision"] = {"payload": {"sourceAmount": 999999}}
    result = resolve_snapshot(store, None, snapshot, "ignore", "确认忽略")
    assert result["result"] == "decision saved"
    updated = store.one("SELECT * FROM bank_transactions")
    assert updated["import_status"] == "ignored"
    assert updated["import_decision"] == original["import_decision"]


def test_recheck_snapshot_deduplicates_and_never_expands_scope(database, settings, tmp_path):
    store = database.store
    recheck.blocked_transactions(store, tmp_path, settings, count=3)
    snapshot = snapshot_issues(store, "bank_transactions")
    selected = snapshot["items"][:2]
    snapshot["items"] = [selected[0], selected[0], selected[1]]
    store.execute(
        "UPDATE bank_transactions SET decision_version=decision_version+1 WHERE id=%s",
        (selected[1]["issue"]["entity_id"],),
    )
    result = request_snapshot_recheck(store, snapshot)
    assert result["scheduled"] == result["skipped"] == 1
    assert len(result["items"]) == 2
    assert result["items"][1]["reason"] == "snapshot changed; query again"
    assert (
        store.one("SELECT count(*) AS n FROM bank_transactions WHERE import_status='issue'")["n"]
        == 2
    )


def test_multiple_same_code_diagnostics_remain_selectable_and_guard_entire_email(database):
    store = database.store
    identifier = "a" * 64
    diagnostics = [{"code": "parse_failed", "locator": place} for place in ("first", "second")]
    store.execute(
        "INSERT INTO email(id,raw_path,parse_status,parse_issues) VALUES (%s,'synthetic','failed',%s)",
        (identifier, diagnostics),
    )
    selected = issues(store, "email", identifier)
    assert issue_detail(store, selected[1])["issue"] == selected[1]
    snapshot = snapshot_issues(store, "email", identifier)
    assert len(snapshot["items"]) == 2
    with pytest.raises(ImporterError, match="exactly one"):
        resolve_snapshot(store, None, snapshot, "ignore", "忽略")
    snapshot["items"] = [snapshot["items"][1]]
    store.execute(
        "UPDATE email SET parse_issues=%s WHERE id=%s",
        ([{"code": "other_diagnostic"}, diagnostics[1]], identifier),
    )
    with pytest.raises(Conflict):
        resolve_snapshot(store, None, snapshot, "ignore", "过期选择")
    assert store.one("SELECT parse_status FROM email")["parse_status"] == "failed"


def test_state_change_after_remote_account_read_is_rejected(
    database, settings, tmp_path, monkeypatch
):
    store, other = database.store, database.connect()
    context = recheck.blocked_transactions(store, tmp_path, settings)
    snapshot = snapshot_issues(store, "bank_transactions")
    original = context.ledger.categories

    def categories():
        other.execute("UPDATE bank_transactions SET decision_version=decision_version+1")
        return original()

    monkeypatch.setattr(context.ledger, "categories", categories)
    with pytest.raises(Conflict):
        snapshot_candidates(store, context.ledger, snapshot)
    assert store.one("SELECT import_status FROM bank_transactions")["import_status"] == "issue"


def test_resolve_rechecks_snapshot_after_network_before_commit(
    database, settings, tmp_path, monkeypatch
):
    store, other = database.store, database.connect()
    context = recheck.blocked_transactions(store, tmp_path, settings)
    snapshot = snapshot_issues(store, "bank_transactions")
    original = context.ledger.get

    def get(key):
        other.execute("UPDATE bank_transactions SET last_resolution=%s", ({"reason": "并发修改"},))
        return original(key)

    monkeypatch.setattr(context.ledger, "get", get)
    with pytest.raises(Conflict):
        resolve_snapshot(store, context.ledger, snapshot, "link", "关联", target_id="7001")
    assert (
        store.one("SELECT ledger_transaction_id FROM bank_transactions")["ledger_transaction_id"]
        is None
    )


def test_unknown_resolution_records_intent_without_resending(database, settings, tmp_path):
    store = database.store
    ledger = pipeline.Ledger()
    pipeline.queue(store, tmp_path, settings, ledger)
    store.execute(
        "UPDATE bank_transactions SET import_status='unknown', import_error=%s",
        ({"code": "write_unknown", "detail": "需要核实"},),
    )
    store.execute("UPDATE background_task SET status='unknown',error_code='write_unknown'")
    snapshot = snapshot_issues(store, "bank_transactions")
    result = resolve_snapshot(store, None, snapshot, "retry", "下次核实")
    assert "intent recorded" in result["result"]
    assert store.one("SELECT status FROM background_task")["status"] == "unknown"
    assert ledger.create_calls == []


@pytest.mark.parametrize("failure", [False, True])
def test_parse_late_success_and_failure_do_not_overwrite_ignore(
    database, settings, tmp_path, failure
):
    store, other = database.store, database.connect()
    identifier = pipeline.ingest_mail(
        store, EvidenceStore(tmp_path), pipeline.raw_daily(), settings
    )
    diagnostics = [{"code": "invalid_header_synthetic", "detail": "可人工忽略"}]
    store.execute("UPDATE email SET parse_issues=%s WHERE id=%s", (diagnostics, identifier))
    parser = BankParser(context="synthetic")

    def parse(raw):
        snapshot = snapshot_issues(other, "email", identifier)
        resolve_snapshot(other, None, snapshot, "ignore", "解析中忽略")
        if failure:
            raise ImporterError("合成解析失败")
        return parser.parse(raw)

    parse_pending(store, SimpleNamespace(version=parser.version, parse=parse))
    row = store.one("SELECT * FROM email WHERE id=%s", (identifier,))
    assert row["parse_status"] == "ignored"
    assert row["parse_issues"] == diagnostics
    assert row["last_resolution"]["reason"] == "解析中忽略"
    assert store.all("SELECT * FROM bank_report") == []
    assert store.all("SELECT * FROM bank_transactions") == []


def test_parse_publication_invalidates_older_manual_snapshot(database, settings, tmp_path):
    store = database.store
    identifier = pipeline.ingest_mail(
        store, EvidenceStore(tmp_path), pipeline.raw_daily(), settings
    )
    store.execute(
        "UPDATE email SET parse_issues=%s WHERE id=%s",
        ([{"code": "invalid_header_synthetic", "detail": "旧诊断"}], identifier),
    )
    snapshot = snapshot_issues(store, "email", identifier)
    parse_pending(store, BankParser(context="synthetic"))
    with pytest.raises(Conflict):
        resolve_snapshot(store, None, snapshot, "ignore", "过期忽略")
    assert store.one("SELECT parse_status FROM email")["parse_status"] == "parsed"
    assert store.all("SELECT * FROM bank_transactions")


def test_parse_stop_does_not_start_next_email(database, settings, tmp_path):
    store = database.store
    for amount in (1, 2):
        pipeline.ingest_mail(
            store, EvidenceStore(tmp_path), pipeline.raw_daily(amount=str(amount)), settings
        )
    parser = BankParser(context="synthetic")
    calls = []

    def parse(raw):
        calls.append(raw)
        return parser.parse(raw)

    parse_pending(
        store, SimpleNamespace(version=parser.version, parse=parse), should_stop=lambda: bool(calls)
    )
    assert len(calls) == 1
    assert store.one("SELECT count(*) AS n FROM email WHERE parse_status='pending'")["n"] == 1
