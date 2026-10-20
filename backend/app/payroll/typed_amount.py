"""Entry-time normalization of typed-in Bonus, Minimum and Maximum amounts.

A typed-in monetary amount may carry more precision than the Company currency stores. It is
rounded exactly once, with the one payable-rounding function, to the currency's stored
minor-unit digits, immediately after the Company currency is locked for the monetary write.
The normalized value is what is persisted, audited, used by the calculation and returned.
Rates and quantities are not monetary amounts and keep four decimals.
"""
from decimal import Decimal

from app.company_currency import CompanyCurrency, currency_error
from app.payroll.calculation.payable_rounding import round_payable
from app.payroll.schemas.bonus import _NUMERIC_18_4_MAX_EXCLUSIVE


def normalize_typed_amount(
    amount: Decimal, currency: CompanyCurrency, *, label: str,
) -> Decimal:
    """Round a typed-in amount to the Company currency; it must stay positive and storable."""
    normalized = round_payable(amount, currency.minor_unit_digits)
    if normalized <= 0:
        smallest = Decimal(1).scaleb(-currency.minor_unit_digits)
        raise currency_error(
            "AMOUNT_ROUNDS_TO_ZERO",
            f"{label} {amount} rounds to zero in {currency.code}, which has "
            f"{currency.minor_unit_digits} decimal place(s). Enter at least {smallest}.",
        )
    if normalized >= _NUMERIC_18_4_MAX_EXCLUSIVE:
        raise currency_error(
            "AMOUNT_TOO_LARGE",
            f"{label} {amount} exceeds the maximum allowed once rounded to {currency.code}.",
        )
    return normalized
