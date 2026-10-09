"""月报非消费行保留银行控制校验，但不进入交易匹配。"""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from ezbookkeeping_importer.application.reconcile import compute_results


@pytest.mark.parametrize("merchant", ["自动还款", "主动还款", "其他账单调整"])
def test_statement_only_rows_do_not_match_or_query_ledger(merchant):
    ledger = Mock()
    report = {
        "content": {"rows": [{
            "row_key": "statement-only", "event_type": "statement",
            "merchant_raw": merchant, "original_amount": None,
            "settlement_amount": "-10.00", "settlement_currency": "CNY",
        }]},
        "period_start": None, "period_end": None,
    }
    results, intents, supplements = compute_results(
        ledger, report, [], datetime.now(timezone.utc)
    )
    assert len(results) == 1
    assert results[0]["match_status"] == "out_of_scope"
    assert results[0]["ledger_check_status"] == "not_checked"
    assert results[0]["details"]["candidate_ids"] == []
    assert not intents and not supplements
    assert not ledger.mock_calls
