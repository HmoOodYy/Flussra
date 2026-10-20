"""
Pure Decimal PerUnit calculation.

The completed quantity-times-rate product is quantized once to four decimal
places with ROUND_HALF_EVEN. Rate resolution, eligibility, workflow, and
persistence remain the caller's responsibility.
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from app.payroll.calculation.payable_rounding import INTERNAL_QUANTUM

PER_UNIT_CALCULATION_VERSION = "cp4a-per-unit-v1"

_LINE_QUANTUM = INTERNAL_QUANTUM


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
