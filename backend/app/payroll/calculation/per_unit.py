"""
Pure PerUnit calculation core (CP-4A).

Behavior-preserving extraction of the current authoritative PerUnit formula
out of `app.payroll.service._compute_calculated_amount`:

    calculated_amount = (quantity * resolved_rate_amount).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_EVEN
    )

This module is intentionally standard-library only. It contains no database
access, SQL, permissions, audit, persistence, workflow, HTTP/request
handling, or dry_run logic -- it accepts already-resolved typed Decimal
inputs and returns a deterministic typed result. Rate resolution,
eligibility, source lookups, and orchestration remain the caller's
responsibility, unchanged.

Only the current PerUnit RateBehavior is represented here. EnteredAmount,
Fixed, None, the CalculatedAmount-null fallback, and the dormant M13c
legacy methods (OrdinalTier, RangeBracket, RangeProgressive, Block) are out
of scope for this module and remain unchanged in their current call sites.
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

PER_UNIT_CALCULATION_VERSION = "cp4a-per-unit-v1"

_LINE_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True)
class PerUnitInput:
    """Already-resolved typed inputs for one PerUnit calculation."""

    quantity: Decimal
    rate_amount: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.quantity, Decimal):
            raise TypeError(
                f"PerUnitInput.quantity must be a Decimal, got {type(self.quantity).__name__}"
            )
        if not isinstance(self.rate_amount, Decimal):
            raise TypeError(
                f"PerUnitInput.rate_amount must be a Decimal, got {type(self.rate_amount).__name__}"
            )


@dataclass(frozen=True)
class PerUnitResult:
    """Deterministic typed result of one PerUnit calculation."""

    calculated_amount: Decimal
    calculation_version: str = PER_UNIT_CALCULATION_VERSION


def calculate_per_unit(data: PerUnitInput) -> PerUnitResult:
    """
    Compute the current PerUnit `calculated_amount`.

    Multiplication occurs at the ambient Decimal context's full precision
    (no intermediate rounding); the completed line is quantized exactly
    once, to `Decimal("0.0001")`, with `ROUND_HALF_EVEN` explicitly
    supplied. Neither the ambient context's precision nor its trap
    configuration is modified by this function.
    """
    product = data.quantity * data.rate_amount
    calculated_amount = product.quantize(_LINE_QUANTUM, rounding=ROUND_HALF_EVEN)
    return PerUnitResult(calculated_amount=calculated_amount)
