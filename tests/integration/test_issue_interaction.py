"""交互快照、只读候选和指定范围复查使用真实 PostgreSQL。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import test_recheck as recheck
from ezbookkeeping_importer.application.maintenance import issues
from ezbookkeeping_importer.application.issue_interaction import issue_detail, issue_candidates
from ezbookkeeping_importer.application.recheck import request_recheck
from ezbookkeeping_importer.application.resolve import resolve
from ezbookkeeping_importer.domain.errors import Conflict, LedgerError
from ezbookkeeping_importer.entrypoints import cli

database = recheck.database
settings = recheck.settings


def test_selected_recheck_preserves_scope_and_skips_changed_snapshot(database, settings, tmp_path):
    database = database.store
    recheck.blocked_transactions(database, tmp_path, settings, count=3)
    selected = issues(database, "bank_transactions")
    frozen = database.all(
        "SELECT id,import_decision,decision_version FROM bank_transactions ORDER BY id"
    )
    database.execute(
        "UPDATE bank_transactions SET import_error=%s WHERE id=%s",
        (
            {"code": "duplicate_candidates", "detail": {"candidate_ids": ["changed"]}},
            selected[1]["entity_id"],
        ),
    )
    result = request_recheck(database, selected[:2])
    assert result == {"scheduled": 1, "already_pending": 0, "skipped": 1}
    assert (
        database.one(
            "SELECT import_status FROM bank_transactions WHERE id=%s", (selected[2]["entity_id"],)
        )["import_status"]
        == "issue"
    )
    assert (
        database.all(
            "SELECT id,import_decision,decision_version FROM bank_transactions ORDER BY id"
        )
        == frozen
    )
    current = issues(database, "bank_transactions", selected[0]["entity_id"])[0]
    assert issue_detail(database, current)["actions"] == []


def test_candidate_query_is_live_and_does_not_interpret_failure_as_missing(
    database, settings, tmp_path
):
    database = database.store
    environment = recheck.blocked_transactions(database, tmp_path, settings)
    selected = issues(database, "bank_transactions")[0]
    data = issue_candidates(database, environment.ledger, selected)
    assert data["candidates"][0]["transaction"] == environment.ledger.records["7001"]
    environment.ledger.records.clear()
    assert (
        issue_candidates(database, environment.ledger, selected)["candidates"][0]["transaction"]
        is None
    )
    ledger = Mock()
    ledger.get.side_effect = LedgerError("read failed")
    with pytest.raises(LedgerError):
        issue_candidates(database, ledger, selected)
    assert issues(database, "bank_transactions") == [selected]


def test_interactive_resolution_rejects_changed_issue_without_replay(database, settings, tmp_path):
    database = database.store
    environment = recheck.blocked_transactions(database, tmp_path, settings)
    selected = issues(database, "bank_transactions")[0]
    database.execute(
        "UPDATE bank_transactions SET import_status='pending' WHERE id=%s", (selected["entity_id"],)
    )
    with pytest.raises(Conflict):
        resolve(
            database,
            environment.ledger,
            "bank_transactions",
            selected["entity_id"],
            selected["version"],
            "ignore",
            "已核实",
            code=selected["code"],
            expected_issue=selected,
        )
    assert (
        database.one(
            "SELECT import_status FROM bank_transactions WHERE id=%s", (selected["entity_id"],)
        )["import_status"]
        == "pending"
    )


def test_internal_dispatch_retains_account_correction_without_public_command(
    database, settings, tmp_path
):
    database = database.store
    environment = recheck.blocked_transactions(database, tmp_path, settings)
    selected = issues(database, "bank_transactions")[0]
    account = environment.rows[0]["import_decision"]["payload"]["sourceAccountId"]
    args = SimpleNamespace(
        command="issues",
        operation="resolve",
        entity_type="bank_transactions",
        entity_id=selected["entity_id"],
        version=selected["version"],
        action="retry",
        reason="核实账户",
        target_id=None,
        account_id=account,
        code=selected["code"],
        selected=selected,
    )
    result = cli._execute(args, SimpleNamespace(store=database, optional_ledger=environment.ledger))
    assert result["result"] == "decision saved"
    updated = database.one("SELECT * FROM bank_transactions WHERE id=%s", (selected["entity_id"],))
    assert updated["import_decision"]["account_override"]["account_id"] == account
    assert updated["decision_version"] == selected["version"] + 1
