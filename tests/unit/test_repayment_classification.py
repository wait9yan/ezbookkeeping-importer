"""Synthetic repayment decisions exercise the default configuration contract."""

from datetime import datetime
from unittest.mock import Mock

import pytest

from ezbookkeeping_importer.application.classify import decide
from ezbookkeeping_importer.config import ENV_FIELDS, load_settings
from ezbookkeeping_importer.domain.errors import ImporterError


@pytest.fixture
def repayment_context(tmp_path, monkeypatch):
    for variable in ENV_FIELDS.values():
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("EBKI_DATABASE_URL", "postgresql://synthetic@example.test/importer")
    path = tmp_path / "config.toml"
    path.write_text(
        'timezone = "Asia/Shanghai"\ndate_only_time = "12:00:00"\n'
        '[mail]\nsource_id = "synthetic"\n'
        '[[repayments]]\nsource_account_id = "payment"\n'
        'destination_account_id = "credit"\ncategory_id = "transfer"\n'
        'currency = "CNY"\nvalid_from = 2026-01-01\nvalid_until = 2026-12-31\n'
    )
    settings = load_settings(str(path), command="status")
    ledger = Mock()
    ledger.accounts.return_value = [
        {"id": identifier, "type": 1, "currency": "CNY", "hidden": False}
        for identifier in ("payment", "credit")
    ]
    ledger.categories.return_value = [{"id": "transfer", "type": 3, "parentId": "parent"}]
    transaction = {
        "id": "synthetic-repayment",
        "source_marker": "ebki-synthetic",
        "event_type": "repayment",
        "occurred_date": "2026-01-05",
        "occurred_at": None,
        "time_precision": "date",
        "card_reference": None,
        "merchant_name": "自动还款",
        "original_currency": "CNY",
        "original_amount": "8.00",
        "report_row_key": "repayment:1",
        "posted_date": None,
        "bank_settlement_amount": None,
        "bank_settlement_currency": None,
        "source_details": {"due_amount": "10.00", "actual_amount": "8.00"},
    }
    return transaction, settings, ledger


def test_repayment_defaults_to_transfer_for_actual_amount(repayment_context):
    transaction, settings, ledger = repayment_context
    ai = Mock()
    decision = decide(transaction, settings, ledger, ai)
    payload = decision["payload"]
    assert payload["type"] == 4
    assert payload["sourceAccountId"] == "payment"
    assert payload["destinationAccountId"] == "credit"
    assert payload["categoryId"] == "transfer"
    assert payload["sourceAmount"] == payload["destinationAmount"] == 800
    assert payload["time"] == int(datetime.fromisoformat("2026-01-05T12:00:00+08:00").timestamp())
    assert payload["utcOffset"] == 480
    assert payload["clientSessionId"] == transaction["source_marker"]
    assert decision["target_currency"] == "CNY"
    ai.classify.assert_not_called()


@pytest.mark.parametrize("mapping_count", [0, 2])
def test_repayment_requires_unique_mapping(repayment_context, mapping_count):
    transaction, settings, ledger = repayment_context
    settings = settings.model_copy(update={"repayments": settings.repayments * mapping_count})
    with pytest.raises(ImporterError, match="one effective account mapping"):
        decide(transaction, settings, ledger, None)


@pytest.mark.parametrize("day", ["2025-12-31", "2027-01-01"])
def test_repayment_requires_mapping_effective_on_transaction_date(repayment_context, day):
    transaction, settings, ledger = repayment_context
    with pytest.raises(ImporterError, match="one effective account mapping"):
        decide({**transaction, "occurred_date": day}, settings, ledger, None)


@pytest.mark.parametrize("currency,amount", [("USD", "8.00"), ("CNY", "0"), ("CNY", "-8.00")])
def test_repayment_retains_currency_and_amount_checks(repayment_context, currency, amount):
    transaction, settings, ledger = repayment_context
    with pytest.raises(ImporterError, match="CNY account pair and positive amount"):
        decide(
            {**transaction, "original_currency": currency, "original_amount": amount},
            settings, ledger, None,
        )


def test_repayment_requires_date_only_convention(repayment_context):
    transaction, settings, ledger = repayment_context
    with pytest.raises(ImporterError, match="date_only_time"):
        decide(transaction, settings.model_copy(update={"date_only_time": None}), ledger, None)


@pytest.mark.parametrize("index", [0, 1])
def test_repayment_validates_both_accounts(repayment_context, index):
    transaction, settings, ledger = repayment_context
    ledger.accounts.return_value[index]["hidden"] = True
    with pytest.raises(ImporterError, match="target account must be visible"):
        decide(transaction, settings, ledger, None)


def test_repayment_requires_transfer_category(repayment_context):
    transaction, settings, ledger = repayment_context
    ledger.categories.return_value[0]["type"] = 2
    with pytest.raises(ImporterError, match="category missing, hidden or incompatible"):
        decide(transaction, settings, ledger, None)
