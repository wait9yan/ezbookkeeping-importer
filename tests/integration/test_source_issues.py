"""真实 PostgreSQL 验证稳定来源决定、完整快照及人工关联。"""

from copy import deepcopy
from datetime import datetime
import json

import pytest
import test_pipeline as pipeline

from ezbookkeeping_importer.application.issue_snapshot import (
    resolve_snapshot, snapshot_candidates, snapshot_issues,
)
from ezbookkeeping_importer.application.recheck import request_snapshot_recheck
from ezbookkeeping_importer.application.sources import register_sources
from ezbookkeeping_importer.domain.errors import Conflict, ImporterError

database = pipeline.database
settings = pipeline.settings


def monthly(store, *, key="monthly-source", count=1):
    transaction = {
        "report_row_key": "row", "event_type": "expense", "occurred_date": "2026-01-15",
        "occurred_at": None, "time_precision": "date", "card_reference": "1234",
        "merchant_name": "合成商户", "original_amount": "10.00", "original_currency": None,
        "posted_date": "2026-01-16", "bank_settlement_amount": "70.00",
        "bank_settlement_currency": "CNY", "source_details": {},
    }
    row = {**pipeline.source_row(transaction), "row_key": "row"}
    pipeline.statement(store, transaction, "70.00", key=key,
                       rows=[{**row, "row_key": f"row-{i}"} for i in range(count)])
    report = store.one("SELECT * FROM bank_report WHERE report_key=%s", (key,))
    with store.transaction():
        register_sources(store, report)
    return report


def remote(ledger):
    ledger.records["remote"] = {
        "id": "remote", "type": 3, "time": int(datetime.fromisoformat("2026-01-15T12:00:00+08:00").timestamp()),
        "utcOffset": 480, "sourceAmount": 7000, "sourceAccountId": "account",
        "categoryId": "category", "comment": "用户手工账", "tagIds": ["keep"],
    }


def snapshot(store, row=0):
    return snapshot_issues(store, "transaction_source", f"monthly-source:row-{row}")


def test_ignore_is_stable_after_projection_refresh_and_retry_is_explicit(database):
    store = database.store
    monthly(store)
    selected = json.loads(json.dumps(snapshot(store)))
    selected["items"][0]["view"] = {"transaction_id": "forged"}
    resolve_snapshot(store, None, selected, "ignore", "确认为范围外")
    store.execute("DELETE FROM bank_statement_reconciliation")
    current = snapshot(store)
    assert current["items"][0]["issue"]["status"] == "ignored"
    assert current["items"][0]["view"]["actions"] == ["retry"]
    with pytest.raises(Conflict):
        resolve_snapshot(store, None, selected, "retry", "旧快照")
    resolve_snapshot(store, None, current, "retry", "重新处理")
    assert snapshot(store)["items"][0]["issue"]["status"] == "issue"
    assert store.one("SELECT last_resolution FROM transaction_source")["last_resolution"]["reason"] == "重新处理"


def test_external_link_preserves_remote_and_original_unknown_currency(database):
    store, ledger = database.store, pipeline.Ledger()
    monthly(store)
    remote(ledger)
    before = deepcopy(ledger.records)
    resolve_snapshot(store, ledger, snapshot(store), "link", "核实已有手工账", target_id="remote")
    tx = store.one("SELECT * FROM bank_transactions")
    assert tx["import_status"] == "booked" and tx["ledger_transaction_id"] == "remote"
    assert tx["original_currency"] is None
    assert tx["import_decision"]["amount_basis"] == "statement_settlement"
    assert ledger.records == before and ledger.create_calls == []
    assert snapshot(store)["items"] == []
    assert store.all("SELECT * FROM background_task") == []


def test_candidate_network_change_invalidates_entire_source_snapshot(database, monkeypatch):
    store, other, ledger = database.store, database.connect(), pipeline.Ledger()
    monthly(store)
    remote(ledger)
    selected = snapshot(store)
    original = ledger.accounts

    def accounts():
        other.execute("UPDATE transaction_source SET resolution_version=resolution_version+1")
        return original()

    monkeypatch.setattr(ledger, "accounts", accounts)
    with pytest.raises(Conflict):
        snapshot_candidates(store, ledger, selected, target_id="remote")
    assert store.all("SELECT * FROM bank_transactions") == []


def test_external_link_rechecks_after_remote_read(database, monkeypatch):
    store, other, ledger = database.store, database.connect(), pipeline.Ledger()
    monthly(store)
    remote(ledger)
    selected = snapshot(store)
    original = ledger.get

    def get(key):
        other.execute("UPDATE transaction_source SET resolution_version=resolution_version+1")
        return original(key)

    monkeypatch.setattr(ledger, "get", get)
    with pytest.raises(Conflict):
        resolve_snapshot(store, ledger, selected, "link", "过期关联", target_id="remote")
    assert store.all("SELECT * FROM bank_transactions") == []


def test_same_report_cannot_consume_one_transaction_twice(database):
    store, ledger = database.store, pipeline.Ledger()
    monthly(store, count=2)
    remote(ledger)
    resolve_snapshot(store, ledger, snapshot(store), "link", "第一笔关联", target_id="remote")
    tid = store.one("SELECT id FROM bank_transactions")["id"]
    with pytest.raises(Conflict):
        resolve_snapshot(store, None, snapshot(store, 1), "link", "错误重复关联", transaction_id=tid)
    assert store.one("SELECT count(*) AS n FROM transaction_source WHERE transaction_id IS NOT NULL")["n"] == 1


def test_scoped_recheck_does_not_expand_or_restore_ignored_source(database):
    store = database.store
    monthly(store, count=2)
    selected = snapshot(store)
    resolve_snapshot(store, None, snapshot(store, 1), "ignore", "保留忽略")
    # Related source changed, so the older whole-source snapshot is intentionally stale.
    assert request_snapshot_recheck(store, selected)["skipped"] == 1
    assert request_snapshot_recheck(store, snapshot(store))["scheduled"] == 1
    with pytest.raises(ImporterError):
        request_snapshot_recheck(store, snapshot(store, 1))
    assert store.one("SELECT last_resolution FROM transaction_source WHERE row_key='row-1'")["last_resolution"]["action"] == "ignore"


def test_internal_link_reuses_identity_and_exposes_local_candidates(database):
    store, ledger = database.store, pipeline.Ledger()
    monthly(store)
    remote(ledger)
    resolve_snapshot(store, ledger, snapshot(store), "link", "核实手工账", target_id="remote")
    original = store.one("SELECT * FROM bank_transactions")
    monthly(store, key="second-report")
    selected = snapshot_issues(store, "transaction_source", "second-report:row-0")
    comparison = snapshot_candidates(store, ledger, selected)
    assert [item["id"] for item in comparison["internal_candidates"]] == [original["id"]]
    resolve_snapshot(store, None, selected, "link", "同笔补充来源", transaction_id=original["id"])
    assert store.one("SELECT count(*) AS n FROM bank_transactions")["n"] == 1
    assert store.one("SELECT count(*) AS n FROM transaction_source WHERE transaction_id=%s", (original["id"],))["n"] == 2
    assert store.one("SELECT * FROM bank_transactions") == original


def test_cross_currency_candidate_cannot_become_cny_external_link(database):
    store, ledger = database.store, pipeline.Ledger()
    monthly(store)
    remote(ledger)
    ledger.records["remote"].update(sourceAccountId="usd-account", sourceAmount=1000)
    with pytest.raises(ImporterError, match="matching CNY"):
        resolve_snapshot(store, ledger, snapshot(store), "link", "错误币种", target_id="remote")
    assert store.all("SELECT * FROM bank_transactions") == []


def test_new_local_candidate_after_snapshot_blocks_external_creation(database, monkeypatch):
    store, other, ledger = database.store, database.connect(), pipeline.Ledger()
    monthly(store)
    remote(ledger)
    selected = snapshot(store)
    original = ledger.get

    def get(key):
        monthly(other, key="concurrent-report")
        return original(key)

    monkeypatch.setattr(ledger, "get", get)
    with pytest.raises(Conflict):
        resolve_snapshot(store, ledger, selected, "link", "并发来源", target_id="remote")
    assert store.all("SELECT * FROM bank_transactions") == []


@pytest.mark.parametrize("action", ["retry", "recheck"])
def test_daily_source_retry_only_publishes_selected_row(database, action):
    store = database.store
    report = monthly(store, count=2)
    content = deepcopy(report["content"])
    for row in content["rows"]:
        row.update(occurred_at="2026-01-15T12:00:00+08:00", time_precision="second", original_currency="CNY")
    store.execute("UPDATE bank_report SET report_type='daily',content=%s WHERE report_key=%s",
                  (content, report["report_key"]))
    selected = snapshot(store)
    if action == "retry":
        resolve_snapshot(store, None, selected, "retry", "仅重试选中行")
    else:
        assert request_snapshot_recheck(store, selected)["scheduled"] == 1
    assert store.one("SELECT transaction_id FROM transaction_source WHERE row_key='row-0'")["transaction_id"]
    assert store.one("SELECT transaction_id FROM transaction_source WHERE row_key='row-1'")["transaction_id"] is None


@pytest.mark.parametrize("changes", [
    {"type": 2}, {"sourceAmount": -7000}, {"sourceAmount": 7001},
    {"time": int(datetime.fromisoformat("2026-02-15T12:00:00+08:00").timestamp())},
])
def test_explicit_manual_target_keeps_safety_constraints(database, changes):
    store, ledger = database.store, pipeline.Ledger()
    monthly(store)
    remote(ledger)
    ledger.records["remote"].update(changes)
    with pytest.raises(ImporterError):
        resolve_snapshot(store, ledger, snapshot(store), "link", "显式目标仍需安全校验", target_id="remote")
    assert store.all("SELECT * FROM bank_transactions") == []


def test_explicit_manual_target_can_use_posted_day_without_automatic_candidate(database):
    from ezbookkeeping_importer.application.statement_candidates import query_candidates
    store, ledger = database.store, pipeline.Ledger()
    report = monthly(store)
    remote(ledger)
    ledger.records["remote"]["time"] += 86400
    candidates, _ = query_candidates(ledger, report["content"]["rows"][0])
    assert candidates == []
    resolve_snapshot(store, ledger, snapshot(store), "link", "按入账日核实手工账", target_id="remote")
    assert store.one("SELECT ledger_transaction_id FROM bank_transactions")["ledger_transaction_id"] == "remote"
    assert ledger.create_calls == []
