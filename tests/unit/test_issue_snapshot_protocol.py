"""快照协议独立于终端，拒绝不完整或含歧义的数据。"""

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from unittest.mock import Mock

import pytest

from ezbookkeeping_importer.application.issue_snapshot import (
    normalize_json,
    resolve_snapshot,
    validate_snapshot,
)
from ezbookkeeping_importer.application.recheck import request_snapshot_recheck
from ezbookkeeping_importer.domain.errors import ImporterError


def document():
    return {
        "snapshot_version": 1,
        "items": [
            {
                "issue": {
                    "entity_type": "email",
                    "entity_id": "synthetic",
                    "code": "parse_failed",
                    "detail": {"locator": "row1"},
                    "version": None,
                    "status": "failed",
                    "context": {},
                },
                "state": {"row": {"parse_status": "failed"}, "related": {}},
                "view": {},
            }
        ],
    }


def test_normalization_round_trips_database_values():
    value = {
        "date": date(2026, 9, 30),
        "time": datetime(2026, 9, 30, tzinfo=timezone.utc),
        "amount": Decimal("10.20"),
        "items": [None, True, 2, 1.5],
    }
    result = normalize_json(value)
    assert json.loads(json.dumps(result)) == result
    assert result["amount"] == "10.20"
    assert result["date"] == "2026-09-30"
    assert result["time"] == "2026-09-30T00:00:00+00:00"


@pytest.mark.parametrize("value", [object(), float("nan"), Decimal("Infinity"), {1: "x"}])
def test_normalization_rejects_unsupported_or_nonfinite_values(value):
    with pytest.raises(ImporterError):
        normalize_json(value)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(snapshot_version=True),
        lambda d: d.update(snapshot_version=2),
        lambda d: d.update(unexpected=True),
        lambda d: d.update(items={}),
        lambda d: d["items"][0].pop("state"),
        lambda d: d["items"][0]["issue"].update(entity_type=[]),
        lambda d: d["items"][0]["issue"].update(entity_id=""),
        lambda d: d["items"][0]["issue"].update(version=True),
        lambda d: d["items"][0].update(state={}),
    ],
)
def test_invalid_snapshot_is_rejected(change):
    data = document()
    change(data)
    with pytest.raises(ImporterError):
        validate_snapshot(data)


def test_empty_snapshot_is_valid_for_collection_but_not_single_action():
    data = {"snapshot_version": 1, "items": []}
    assert validate_snapshot(data) == data
    with pytest.raises(ImporterError, match="exactly one"):
        validate_snapshot(data, single=True)
    assert request_snapshot_recheck(Mock(), data) == {
        "scheduled": 0,
        "already_pending": 0,
        "skipped": 0,
        "items": [],
    }


def test_multiple_diagnostics_do_not_select_first_item():
    data = document()
    data["items"].append(deepcopy(data["items"][0]))
    data["items"][1]["issue"]["detail"] = {"locator": "row2"}
    assert len(validate_snapshot(data)["items"]) == 2
    store = Mock()
    with pytest.raises(ImporterError, match="exactly one"):
        resolve_snapshot(store, None, data, "ignore", "明确忽略")
    store.one.assert_not_called()


@pytest.mark.parametrize(
    "action,target,account,reason",
    [
        ("link", None, None, "reason"),
        ("ignore", "42", None, "reason"),
        ("ignore", None, "42", "reason"),
        ("retry", None, None, "  "),
        ("unknown", None, None, "reason"),
    ],
)
def test_invalid_resolution_parameters_do_not_read_or_write(action, target, account, reason):
    store = Mock()
    with pytest.raises(ImporterError):
        resolve_snapshot(store, None, document(), action, reason, target, account)
    store.one.assert_not_called()
    store.execute.assert_not_called()


def test_recheck_rejects_ineligible_collection_before_any_write():
    store = Mock()
    with pytest.raises(ImporterError, match="duplicate bank transaction"):
        request_snapshot_recheck(store, document())
    store.one.assert_not_called()
    store.execute.assert_not_called()


@pytest.mark.parametrize("state", ["pending", "queued", "ignored"])
def test_issue_actions_do_not_enable_commands_for_readonly_transaction_state(state):
    from ezbookkeeping_importer.application.issue_interaction import issue_actions

    row = {
        "import_status": state,
        "ledger_transaction_id": None,
        "import_decision": {"payload": {"sourceAmount": 100}},
    }
    assert issue_actions("bank_transactions", row, "duplicate_candidates") == []


def test_duplicate_issue_uses_recheck_instead_of_retry():
    from ezbookkeeping_importer.application.issue_interaction import issue_actions

    row = {
        "import_status": "issue",
        "ledger_transaction_id": None,
        "import_decision": {"payload": {"sourceAmount": 100}},
    }
    actions = issue_actions("bank_transactions", row, "duplicate_candidates")
    assert "recheck" in actions
    assert "retry" not in actions
