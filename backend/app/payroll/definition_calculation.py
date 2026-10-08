"""Method-owned calculation boundary for target PayDefinition earnings.

The one place that turns a period definition, a WHOLE resolved compensation
schedule and a quantity into money. Consumers (Day Grid, source reads, the live
calculation packet, Current Payroll) call ``calculate_definition_input`` and never
branch on the calculation method themselves.

The boundary accepts the complete ``ResolvedCompensation``: the effective
DriverRateAssignment with ALL of its component values. It never receives a single
scalar rate, so a method that needs several components (OrdinalTier) attaches here
without changing the resolver contract, the period snapshot or any consumer.

Method dispatch is keyed by the FROZEN (method, version) pair of the period
definition, never by the method name alone and never by live PayDefinition metadata:
  ("PerUnit", 1)  -> implemented (quantity * the one scalar component, via the CP-4A kernel)
  ("OrdinalTier", any) and any unregistered pair -> not operational: fails closed with
  METHOD_NOT_READY. An unknown or future version of PerUnit never runs the V1 algorithm.

Missing, zero and invalid are distinct:
  no authoritative assignment               -> MISSING_RATE
  assignment but a required value is absent -> INCOMPLETE_RATE
  amount 0                                  -> a valid rate; the result is 0
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.compensation.schemas import ResolvedCompensation
from app.payroll.calculation.per_unit import (
    PER_UNIT_CALCULATION_VERSION,
    PerUnitInput,
    calculate_per_unit,
)


class CalculationStatus(StrEnum):
    CALCULATED = "Calculated"
    MISSING_RATE = "MissingRate"
    INCOMPLETE_RATE = "IncompleteRate"
    METHOD_NOT_READY = "MethodNotReady"


@dataclass(frozen=True)
class PeriodDefinition:
    """The frozen definition a source quantity is entered against (period snapshot)."""

    payroll_period_definition_id: int
    pay_definition_id: int
    rate_definition_id: int
    code: str
    name: str
    input_type: str
    unit: str | None
    calculation_method: str
    calculation_method_version: int
    rate_shape: str
    is_active: bool


@dataclass(frozen=True)
class DefinitionCalculation:
    """The outcome of one calculation.

    ``amount`` is set only when ``status`` is CALCULATED. ``driver_rate_assignment_id``
    identifies the whole schedule that was used (None when none applied).
    """

    status: CalculationStatus
    amount: Decimal | None = None
    calculation_version: str | None = None
    driver_rate_assignment_id: int | None = None

    @property
    def is_calculated(self) -> bool:
        return self.status is CalculationStatus.CALCULATED

    @property
    def needs_attention(self) -> bool:
        return self.status is not CalculationStatus.CALCULATED


def _calculate_per_unit(
    resolved: ResolvedCompensation | None, quantity: Decimal,
) -> DefinitionCalculation:
    if resolved is None:
        return DefinitionCalculation(CalculationStatus.MISSING_RATE)
    assignment_id = resolved.driver_rate_assignment_id
    if resolved.rate_shape != "Scalar" or len(resolved.components) != 1:
        return DefinitionCalculation(
            CalculationStatus.INCOMPLETE_RATE, driver_rate_assignment_id=assignment_id)
    amount = resolved.components[0].amount
    if amount is None or amount < 0:
        return DefinitionCalculation(
            CalculationStatus.INCOMPLETE_RATE, driver_rate_assignment_id=assignment_id)
    result = calculate_per_unit(PerUnitInput(quantity=quantity, rate_amount=amount))
    return DefinitionCalculation(
        CalculationStatus.CALCULATED,
        amount=result.calculated_amount,
        calculation_version=PER_UNIT_CALCULATION_VERSION,
        driver_rate_assignment_id=assignment_id,
    )


# Future methods and versions attach here, not in the consumers.
_CALCULATORS = {
    ("PerUnit", 1): _calculate_per_unit,
}


def is_method_operational(calculation_method: str, calculation_method_version: int) -> bool:
    """True only for a registered frozen (method, version) pair."""
    return (calculation_method, calculation_method_version) in _CALCULATORS


def calculate_definition_input(
    definition: PeriodDefinition,
    resolved: ResolvedCompensation | None,
    quantity: Decimal,
) -> DefinitionCalculation:
    """Calculate one source quantity against the complete resolved schedule."""
    calculator = _CALCULATORS.get(
        (definition.calculation_method, definition.calculation_method_version))
    if calculator is None:
        return DefinitionCalculation(CalculationStatus.METHOD_NOT_READY)
    return calculator(resolved, quantity)


# DraftLines.Quantity is NUMERIC(18,4): at most 14 integer digits.
_QUANTITY_LIMIT = Decimal("100000000000000")


def quantity_error(input_type: str, quantity: Decimal) -> str | None:
    """Server-side quantity validation against the frozen InputType.

    Returns an error message, or None when the quantity is acceptable. Zero is a
    valid quantity for either type.
    """
    if not quantity.is_finite():
        return "Quantity must be a finite number."
    if quantity < 0:
        return "Quantity must not be negative."
    if quantity >= _QUANTITY_LIMIT:
        return "Quantity is too large."
    if input_type == "WholeNumber" and quantity != quantity.to_integral_value():
        return "This item requires a whole number quantity."
    return None
