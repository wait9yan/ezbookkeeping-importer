from decimal import Decimal

from .errors import ImporterError


def cents(value: Decimal) -> int:
    if not value.is_finite() or value != value.quantize(Decimal("0.01")):
        raise ImporterError("amount must have at most two decimal places")
    return int(value * 100)
