"""Immutable values crossing the mail parser boundary."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


MailKind = Literal["daily", "repayment", "monthly", "other", "unknown"]


class SourceRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    row_key: str
    event_type: Literal["expense", "refund", "repayment", "statement"]
    occurred_date: date
    occurred_at: datetime | None
    time_precision: str
    card_reference: str | None
    merchant_raw: str
    original_amount: Decimal
    original_currency: str | None
    posted_date: date | None = None
    settlement_amount: Decimal | None = None
    settlement_currency: str | None = None
    evidence: dict[str, Any]
    extra: dict[str, Any] = Field(default_factory=dict)


class ParseIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    locator: str
    detail: str


class ParsedMail(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: MailKind
    report_key: str
    report_date: date | None
    rows: tuple[SourceRow, ...]
    issues: tuple[ParseIssue, ...]
    metadata: dict[str, Any]
