"""Pure Min/Max whole-day proration (``minmax-proration-v1``).

A DriverPayRule Minimum or Maximum guarantees an amount for one payroll period of the
Driver's home Branch. When the DriverAssignment only covers part of that period, both
amounts are prorated by the same whole-day fraction:

    fraction = eligible scheduled days / scheduled days of the period

* A scheduled day is a frozen ``PayrollPeriodDays`` row that is a default or added work day.
  A period without day rows has no scheduled days to infer: the rules are blocked.
* Unscheduled days never count, even if worked.
* A day counts for the numerator only when it is in the DriverAssignment's frozen
  eligible work dates, the DriverAssignment owns that date (a date belongs to exactly one
  assignment of a Driver, rule D1) and its selected Status is not an off reason.
* Whole days only; the prorated amount stays at internal precision (``INTERNAL_QUANTUM``)
  until the one official payable rounding of the adjustment.

This module is pure: no SQL, no I/O, no currency rounding.
"""
from collections.abc import Collection
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal

from app.payroll.calculation.payable_rounding import INTERNAL_QUANTUM

MINMAX_PRORATION_VERSION = "minmax-proration-v1"

_CONTEXT = Context(prec=60, rounding=ROUND_HALF_EVEN)

# Why a driver's Minimum/Maximum were (not) applied. Everything but APPLIED applies neither.
APPLIED = "APPLIED"
NO_RULE = "NO_RULE"
NO_SCHEDULED_DAYS = "NO_SCHEDULED_DAYS"
NO_ELIGIBLE_SCHEDULED_DAYS = "NO_ELIGIBLE_SCHEDULED_DAYS"
CALENDAR_MISMATCH = "CALENDAR_MISMATCH"
MIN_EXCEEDS_MAX = "MIN_EXCEEDS_MAX"
SOURCE_AMOUNT_PRECISION = "SOURCE_AMOUNT_PRECISION"
MINMAX_PERIOD_DAYS_MISSING = "MINMAX_PERIOD_DAYS_MISSING"


def counted_days(
    scheduled_days: Collection[date],
    eligible_days: Collection[date],
    off_reason_days: Collection[date],
) -> tuple[date, ...]:
    """The eligible scheduled non-off days, ascending: the days the numerator counts."""
    counted = (set(scheduled_days) & set(eligible_days)) - set(off_reason_days)
    return tuple(sorted(counted))


def coverage(
    scheduled_days: Collection[date],
    eligible_days: Collection[date],
    off_reason_days: Collection[date],
) -> tuple[int, int]:
    """(numerator, denominator): eligible scheduled non-off days over scheduled days."""
    return (
        len(counted_days(scheduled_days, eligible_days, off_reason_days)),
        len(set(scheduled_days)),
    )


def prorate(amount: Decimal, numerator: int, denominator: int) -> Decimal:
    """``amount * numerator / denominator`` at internal precision, half-even.

    A full period (numerator == denominator) returns the rule amount unchanged.
    """
    if denominator <= 0 or not 0 <= numerator <= denominator:
        raise ValueError("a proration needs 0 <= numerator <= denominator and denominator > 0")
    if numerator == denominator:
        return amount
    exact = _CONTEXT.divide(_CONTEXT.multiply(amount, Decimal(numerator)), Decimal(denominator))
    return exact.quantize(INTERNAL_QUANTUM, rounding=ROUND_HALF_EVEN, context=_CONTEXT)


def raw_minimum_adjustment(base: Decimal, prorated_minimum: Decimal | None) -> Decimal:
    """The top-up to the prorated Minimum, before the one official rounding."""
    if prorated_minimum is not None and base < prorated_minimum:
        return prorated_minimum - base
    return Decimal("0")


def raw_maximum_adjustment(base: Decimal, prorated_maximum: Decimal | None) -> Decimal:
    """The (negative) cap to the prorated Maximum, before the one official rounding."""
    if prorated_maximum is not None and base > prorated_maximum:
        return prorated_maximum - base
    return Decimal("0")
