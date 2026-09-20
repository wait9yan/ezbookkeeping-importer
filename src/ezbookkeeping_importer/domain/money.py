from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .errors import ImporterError


def cents(value: Decimal) -> int:
    if not value.is_finite() or value != value.quantize(Decimal("0.01")):
        raise ImporterError("amount must have at most two decimal places")
    return int(value * 100)


def convert_cny(
    amount: Decimal,
    currency: str,
    snapshot: dict,
    max_age_hours: int | None,
    now: datetime | None = None,
) -> tuple[int, dict]:
    if max_age_hours is None:
        raise ImporterError("exchange_rate_max_age_hours must be configured")
    now = now or datetime.now(timezone.utc)
    try:
        updated = datetime.fromtimestamp(int(snapshot["updateTime"]), timezone.utc)
        quotes = {item["currency"]: Decimal(item["rate"]) for item in snapshot["exchangeRates"]}
        quotes[snapshot["baseCurrency"]] = Decimal(1)
        source, target = quotes[currency], quotes["CNY"]
        if not source.is_finite() or not target.is_finite() or source <= 0 or target <= 0:
            raise ValueError("non-positive quote")
        age = (now - updated).total_seconds()
        if age < 0 or age > max_age_hours * 3600:
            raise ValueError("quote outside configured age")
    except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
        raise ImporterError("rate_unavailable: invalid or stale quote") from exc
    ratio = target / source
    value = (amount * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return cents(value), {**snapshot, "fetchedAt": now.isoformat(), "adoptedRate": str(ratio)}
