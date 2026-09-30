import json
from unittest.mock import Mock

import pytest

from ezbookkeeping_importer.domain.accounts import (
    AccountMatchError,
    card_numbers,
    match_account,
    decision_currency,
)
from ezbookkeeping_importer.application.classify import decide, refresh_classification
from ezbookkeeping_importer.config import Settings, MailSettings
from ezbookkeeping_importer.domain.errors import ImporterError

CARD = "4444333322221234"
OTHER = "5555666677771234"


def account(identifier, currency="USD", **kwargs):
    return {
        "id": identifier,
        "type": 1,
        "currency": currency,
        "hidden": False,
        "comment": f"卡号 {CARD}",
        **kwargs,
    }


@pytest.mark.parametrize(
    "comment,expected",
    [
        (f"卡号：{CARD}，用途备注", {CARD}),
        ("卡号 4444 3333 2222 1234", {CARD}),
        ("卡号 4444  3333   2222 1234", {CARD}),
        ("卡号 4444-3333-2222-1234", {CARD}),
        (f"{CARD} {OTHER}", {CARD, OTHER}),
        ("2026-09-28 / 尾号1234", set()),
        ("123456789012345678901", set()),
        ("12345678 90123456", set()),
    ],
)
def test_comment_card_numbers_have_defined_boundaries(comment, expected):
    assert card_numbers(comment) == expected


def test_currency_selects_unique_account_for_same_card():
    accounts = [account("cny", "CNY"), account("usd", "USD")]
    identifier, evidence = match_account(accounts, "1234", "USD")
    assert identifier == "usd"
    assert evidence == {
        "account_id": "usd",
        "card_last4": "1234",
        "match_method": "suffix4",
        "currency": "USD",
    }
    assert CARD not in json.dumps(evidence)


def test_full_card_uses_exact_match_not_common_suffix():
    accounts = [account("one"), account("two", comment=OTHER)]
    assert match_account(accounts, CARD, "USD")[0] == "one"
    with pytest.raises(AccountMatchError) as error:
        match_account(accounts, "1234", "USD")
    assert error.value.code == "account_ambiguous" and error.value.candidate_ids == ["one", "two"]
    assert CARD not in str(error.value) and OTHER not in str(error.value)


def test_hidden_parent_and_wrong_currency_never_resolve():
    accounts = [account("hidden", hidden=True), account("parent", type=2), account("cny", "CNY")]
    with pytest.raises(AccountMatchError) as error:
        match_account(accounts, "1234", "USD")
    assert error.value.code == "account_not_found"


def test_multiple_card_mentions_in_one_account_are_one_candidate():
    assert (
        match_account([account("one", comment=f"{CARD}, {CARD}, {OTHER}")], "1234", "USD")[0]
        == "one"
    )


def transaction(event="expense", currency="USD"):
    return {
        "id": "synthetic",
        "source_marker": "ebki-synthetic",
        "event_type": event,
        "occurred_date": "2026-01-01",
        "occurred_at": "2026-01-01T12:00:00+08:00",
        "time_precision": "second",
        "card_reference": "1234",
        "merchant_name": "合成商户",
        "original_currency": currency,
        "original_amount": "-10.00" if event == "refund" else "10.00",
        "report_row_key": "row",
        "posted_date": None,
        "bank_settlement_amount": None,
        "bank_settlement_currency": None,
        "source_details": {},
    }


def settings():
    return Settings(
        mail=MailSettings(source_id="synthetic"),
        timezone="Asia/Shanghai",
        classification_mode="rules_only",
    )


@pytest.mark.parametrize(
    "event,currency,amount",
    [("expense", "USD", 1000), ("refund", "USD", -1000), ("expense", "CNY", 1000)],
)
def test_new_decisions_use_original_currency_without_rate_request(event, currency, amount):
    ledger = Mock()
    ledger.accounts.return_value = [account("cny", "CNY"), account("usd", "USD")]
    ledger.categories.return_value = [
        {"id": "category", "type": 2, "parentId": "parent", "path": "其他杂项 → 待分类"}
    ]
    ledger.rates.side_effect = AssertionError("no currency conversion")
    tx = transaction(event, currency)
    import_decision = decide(tx, settings(), ledger, None)
    assert import_decision["target_currency"] == currency
    assert import_decision["payload"]["sourceAmount"] == amount
    assert import_decision["payload"]["sourceAccountId"] == currency.lower()
    assert import_decision["rate_snapshot"] is None
    assert CARD not in json.dumps(import_decision)
    ledger.rates.assert_not_called()
    tx["import_decision"] = import_decision
    refreshed = refresh_classification(tx, settings(), ledger, None)
    assert (
        refreshed["target_currency"] == currency
        and refreshed["payload"] == import_decision["payload"]
    )


def test_legacy_decision_currency_is_cny_not_original_usd():
    tx = transaction()
    tx["import_decision"] = {
        "payload": {"sourceAmount": 7000},
        "rate_snapshot": {"adoptedRate": "7"},
    }
    assert decision_currency(tx) == "CNY"
    with pytest.raises(ImporterError, match="persisted"):
        decision_currency(transaction())
