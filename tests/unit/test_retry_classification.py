from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ezbookkeeping_importer.application.classify import refresh_classification
from ezbookkeeping_importer.domain.errors import ImporterError


@pytest.fixture
def transaction():
    return {
        "id": "synthetic-row",
        "state": "pending",
        "facts": {
            "merchant_raw": "合成商户",
            "event_type": "expense",
            "original_amount": "10.00",
            "original_currency": "USD",
        },
        "decision": {
            "reclassify_requested": True,
            "rate_snapshot": {"dataSource": "synthetic", "adoptedRate": "7"},
            "classification": {"category_id": "deleted"},
            "payload": {
                "type": 3,
                "sourceAccountId": "manual-account",
                "sourceAmount": 7000,
                "time": 1000,
                "utcOffset": 480,
                "categoryId": "deleted",
                "comment": "ebki-synthetic 原币USD10",
                "clientSessionId": "ebki-synthetic",
            },
        },
    }


@pytest.fixture
def ledger():
    ledger = Mock()
    ledger.accounts.return_value = [
        {"id": "manual-account", "type": 1, "currency": "CNY", "hidden": False}
    ]
    ledger.categories.return_value = [
        {"id": "fallback", "type": 2, "parentId": "parent", "path": "其他杂项 → 待分类"},
        {"id": "replacement", "type": 2, "parentId": "parent", "path": "类别 → 新分类"},
    ]
    ledger.rates.side_effect = AssertionError("must not reprice a queued intent")
    return ledger


def test_retry_changes_only_classification_and_preserves_manual_account_and_quote(
    transaction, ledger
):
    original = deepcopy(transaction)
    settings = SimpleNamespace(
        rules=[SimpleNamespace(merchant_pattern="合成", category_id="replacement")],
        classification_mode="rules_only",
    )
    decision = refresh_classification(transaction, settings, ledger, None)
    assert decision["payload"] == {**original["decision"]["payload"], "categoryId": "replacement"}
    assert decision["rate_snapshot"] == original["decision"]["rate_snapshot"]
    assert decision["reclassify_requested"] is False
    assert transaction == original
    ledger.rates.assert_not_called()


def test_retry_rule_conflict_is_explicit(transaction, ledger):
    settings = SimpleNamespace(
        rules=[
            SimpleNamespace(merchant_pattern="合成", category_id=name)
            for name in ("fallback", "replacement")
        ],
        classification_mode="rules_only",
    )
    with pytest.raises(ImporterError, match="conflict"):
        refresh_classification(transaction, settings, ledger, None)
    assert transaction["decision"]["reclassify_requested"] is True


def test_retry_ai_failure_is_not_unmatched(transaction, ledger):
    settings = SimpleNamespace(rules=[], classification_mode="ai")
    ai = Mock()
    ai.classify.side_effect = ImporterError("synthetic AI protocol failure")
    with pytest.raises(ImporterError, match="AI protocol failure"):
        refresh_classification(transaction, settings, ledger, ai)
    assert transaction["decision"]["payload"]["categoryId"] == "deleted"


def test_retry_unmatched_ai_uses_current_fallback(transaction, ledger):
    settings = SimpleNamespace(rules=[], classification_mode="ai")
    ai = Mock()
    ai.classify.return_value = {
        "classification_status": "unmatched",
        "category_id": None,
        "reason": "synthetic insufficient information",
    }
    decision = refresh_classification(transaction, settings, ledger, ai)
    assert decision["payload"]["categoryId"] == "fallback"
    assert decision["classification"]["reason"] == "synthetic insufficient information"
    assert decision["payload"]["sourceAmount"] == 7000
