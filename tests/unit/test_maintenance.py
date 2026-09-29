from datetime import date
from decimal import Decimal
from unittest.mock import Mock

from ezbookkeeping_importer.application import maintenance


def issue(entity_type, entity_id, code, status="issue", detail=None):
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "code": code,
        "status": status,
        "detail": detail,
        "version": 1,
    }


def test_issue_groups_distinguish_diagnostics_objects_and_recovery_states():
    items = [
        issue("email", "a", "unknown_template", "failed"),
        issue("email", "a", "invalid_header", "failed"),
        issue("email", "b", "unknown_template", "failed"),
        issue("bank_transactions", "a", "duplicate_candidates"),
        issue("bank_transactions", "b", "duplicate_candidates", "pending"),
        issue(
            "bank_statement_reconciliation",
            "1",
            "reconciliation",
            None,
            {"match_status": "missing_source_transaction", "ledger_check_status": "not_checked"},
        ),
        issue(
            "bank_statement_reconciliation",
            "2",
            "reconciliation",
            None,
            {"match_status": "matched", "ledger_check_status": "mismatched"},
        ),
    ]
    groups = maintenance.issue_groups(items)
    unknown = next(group for group in groups if group["code"] == "unknown_template")
    assert unknown["count"] == unknown["object_count"] == 2
    duplicates = [group for group in groups if group["code"] == "duplicate_candidates"]
    assert {(group["status"], group["count"]) for group in duplicates} == {
        ("issue", 1),
        ("pending", 1),
    }
    reconciliation = [group for group in groups if group["code"] == "reconciliation"]
    assert {(group["match_status"], group["ledger_check_status"]) for group in reconciliation} == {
        ("missing_source_transaction", "not_checked"),
        ("matched", "mismatched"),
    }
    assert sum(group["count"] for group in groups) == len(items)


def test_status_uses_one_diagnostic_projection_for_all_counts(monkeypatch):
    items = [
        issue("email", "a", "unknown_template"),
        issue("email", "a", "invalid_header"),
        issue("bank_transactions", "a", "duplicate_candidates"),
    ]
    source = Mock(return_value=items)
    monkeypatch.setattr(maintenance, "issues", source)
    store = Mock()
    store.all.return_value = []

    result = maintenance.status(store)

    source.assert_called_once_with(store)
    assert result["issues"] == 3 and result["issue_object_count"] == 2
    assert sum(group["count"] for group in result["issue_groups"]) == 3
    assert result["bank_transactions"] == []


def test_issue_details_keep_original_fields_and_add_limited_business_context():
    transaction = {
        "id": "abcdefghijklmnop",
        "decision_version": 5,
        "import_status": "issue",
        "import_error": {"code": "duplicate_candidates", "detail": {"candidate_ids": ["old"]}},
        "occurred_date": date(2026, 9, 29),
        "merchant_name": "合成商户",
        "original_amount": Decimal("12.30"),
        "original_currency": "CNY",
        "report_key": "synthetic-report",
        "source_details": {"private": "not presented"},
        "import_decision": {"payload": {"private": "not presented"}},
    }
    store = Mock()
    store.all.side_effect = lambda sql: [transaction] if "FROM bank_transactions" in sql else []

    result = maintenance.issues(store, "bank_transactions", transaction["id"])

    assert len(result) == 1
    assert result[0]["detail"] == {"candidate_ids": ["old"]}
    assert result[0]["version"] == 5
    assert result[0]["context"] == {
        "transaction_date": date(2026, 9, 29),
        "merchant": "合成商户",
        "original_amount": Decimal("12.30"),
        "original_currency": "CNY",
        "report_key": "synthetic-report",
    }
