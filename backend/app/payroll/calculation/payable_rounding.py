"""The one payable-rounding authority.

Internal calculation precision is ``INTERNAL_QUANTUM`` (0.0001): rates, quantities and
line amounts are never currency-rounded. An official payable amount is rounded exactly once
with ``ROUND_HALF_EVEN`` to the stored ISO minor-unit digits of the Company currency
(``core.SupportedCurrencies.MinorUnitDigits``, read through ``app.company_currency``).

``round_payable`` is the only function that currency-rounds money. It serves both the
official amounts of the live calculation packet and the normalization of typed-in Bonus,
Minimum and Maximum amounts. Nothing else (readers, read models, the frontend) rounds.
"""
from decimal import ROUND_HALF_EVEN, Context, Decimal

PAYABLE_ROUNDING_VERSION = "payable-rounding-v1"
INTERNAL_QUANTUM = Decimal("0.0001")

MAX_MINOR_UNIT_DIGITS = 4

# Enough precision for any NUMERIC(18,4) magnitude plus the extra scale of a typed amount,
# independent of the ambient Decimal context.
_ROUNDING_CONTEXT = Context(prec=60, rounding=ROUND_HALF_EVEN)


def _validated_digits(minor_unit_digits: int) -> int:
    if isinstance(minor_unit_digits, bool) or not isinstance(minor_unit_digits, int):
        raise TypeError(
            f"minor_unit_digits must be an int, got {type(minor_unit_digits).__name__}")
    if not 0 <= minor_unit_digits <= MAX_MINOR_UNIT_DIGITS:
        raise ValueError(
            f"minor_unit_digits must be between 0 and {MAX_MINOR_UNIT_DIGITS}, "
            f"got {minor_unit_digits}")
    return minor_unit_digits


def round_payable(amount: Decimal, minor_unit_digits: int) -> Decimal:
    """Round ``amount`` half-even to ``minor_unit_digits`` decimal places."""
    if not isinstance(amount, Decimal):
        raise TypeError(f"amount must be a Decimal, got {type(amount).__name__}")
    if not amount.is_finite():
        raise ValueError("amount must be a finite Decimal")
    digits = _validated_digits(minor_unit_digits)
    rounded = amount.quantize(
        Decimal(1).scaleb(-digits), rounding=ROUND_HALF_EVEN, context=_ROUNDING_CONTEXT)
    # A tiny negative adjustment rounds to a signed zero; an official zero has no sign.
    return abs(rounded) if rounded.is_zero() else rounded


def is_payable_precision(amount: Decimal, minor_unit_digits: int) -> bool:
    """True when ``amount`` already has no more decimals than the currency stores."""
    return round_payable(amount, minor_unit_digits) == amount
