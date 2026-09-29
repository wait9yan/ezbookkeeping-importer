"""身份协议固定向量与业务指纹边界。"""

import pytest
from ezbookkeeping_importer.domain.identity import transaction_id, content_fingerprint
from ezbookkeeping_importer.domain.errors import ImporterError


def test_transaction_identity_uses_raw_96_bit_digest():
    assert transaction_id("report", "row") == "eFVFpJ9DLopA6THQ"
    assert transaction_id("报告", "明细") == "DWo0tfag2PTLq7qp"


@pytest.mark.parametrize("report,row", [("a:b", "c"), ("a", "b:c"), ("", "b"), ("a", "")])
def test_ambiguous_or_empty_identity_is_rejected(report, row):
    with pytest.raises(ImporterError):
        transaction_id(report, row)


def test_fingerprint_ignores_locator_and_row_order_but_includes_control_totals():
    first = {
        "rows": [
            {"row_key": "a", "original_amount": "10", "evidence": {"line": 1}},
            {"row_key": "b", "original_amount": "20", "evidence": {"line": 2}},
        ],
        "controls": {"total": "30"},
        "extensions": {},
    }
    reordered = {
        **first,
        "rows": [{**r, "evidence": {"line": 99}} for r in reversed(first["rows"])],
    }
    assert content_fingerprint(first) == content_fingerprint(reordered)
    assert content_fingerprint(first) != content_fingerprint({**first, "controls": {"total": "31"}})
