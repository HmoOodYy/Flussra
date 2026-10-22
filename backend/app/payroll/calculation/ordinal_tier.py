"""
Pure Decimal OrdinalTier calculation.

OrdinalTier is progressive per ordinal unit, not a bracket rate applied to the whole quantity:
the Nth unit of a quantity earns the value of the tier that covers ordinal N. With the tiers
``1 -> 3``, ``2 -> 4`` and ``3+ -> 5`` a quantity of 4 earns ``3 + 4 + 5 + 5 = 17``.

The quantity is a whole number of ordinal units (zero earns nothing). The tiers are the
definition-owned topology filled with one Driver schedule's values: ordered, 1-based, without
gaps or overlaps, and the last tier is the only open-ended one. Every tier carries a value,
zero included. Each tier contributes ``units covered x value`` exactly; the sum is quantized
once to the internal precision, like every method calculation, and never rounded to a
currency: payable rounding stays the aggregation boundary's job. Rate resolution, eligibility
and persistence remain the caller's responsibility.
"""
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from app.compensation.ordinal_topology import topology_error
from app.payroll.calculation.payable_rounding import INTERNAL_QUANTUM

ORDINAL_TIER_CALCULATION_VERSION = "ordinal-tier-v1"

_QUANTUM = INTERNAL_QUANTUM


@dataclass(frozen=True)
class OrdinalTier:
    """One tier of the topology with the value a schedule gave it."""

    sequence_no: int
    ordinal_from: int
    ordinal_to: int | None
    rate_amount: Decimal


@dataclass(frozen=True)
class OrdinalTierContribution:
    """What one tier earned: the ordinal units of the quantity it covered times its value."""

    sequence_no: int
    ordinal_from: int
    ordinal_to: int | None
    units: int
    rate_amount: Decimal
    amount: Decimal


@dataclass(frozen=True)
class OrdinalTierResult:
    """The deterministic result: the amount and the expansion that explains it."""

    calculated_amount: Decimal
    contributions: tuple[OrdinalTierContribution, ...]
    calculation_version: str = ORDINAL_TIER_CALCULATION_VERSION


def calculate_ordinal_tier(quantity: Decimal, tiers: Sequence[OrdinalTier]) -> OrdinalTierResult:
    """Compute the OrdinalTier amount of a whole ``quantity`` against filled ``tiers``.

    Raises ``ValueError`` for a quantity that is not a non-negative whole number, a topology
    that is not valid or a tier value that is not a non-negative Decimal: the caller decides
    what those mean for a payroll line, this function never guesses.
    """
    if not isinstance(quantity, Decimal):
        raise TypeError(f"quantity must be a Decimal, got {type(quantity).__name__}")
    if not quantity.is_finite() or quantity < 0 or quantity != quantity.to_integral_value():
        raise ValueError("OrdinalTier needs a non-negative whole-number quantity.")
    ordered = sorted(tiers, key=lambda tier: tier.sequence_no)
    if [tier.sequence_no for tier in ordered] != [tier.sequence_no for tier in tiers]:
        raise ValueError("Tiers must be given in sequence order.")
    problem = topology_error([(tier.ordinal_from, tier.ordinal_to) for tier in ordered])
    if problem is not None:
        raise ValueError(problem)
    for tier in ordered:
        if (
            not isinstance(tier.rate_amount, Decimal)
            or not tier.rate_amount.is_finite()
            or tier.rate_amount < 0
        ):
            raise ValueError("Every tier needs a non-negative Decimal value.")

    units_total = int(quantity)
    contributions = []
    for tier in ordered:
        last_ordinal = units_total if tier.ordinal_to is None else min(units_total, tier.ordinal_to)
        units = max(0, last_ordinal - tier.ordinal_from + 1)
        contributions.append(OrdinalTierContribution(
            sequence_no=tier.sequence_no,
            ordinal_from=tier.ordinal_from,
            ordinal_to=tier.ordinal_to,
            units=units,
            rate_amount=tier.rate_amount,
            amount=(units * tier.rate_amount).quantize(_QUANTUM, rounding=ROUND_HALF_EVEN),
        ))
    total = sum((c.amount for c in contributions), Decimal("0"))
    return OrdinalTierResult(
        calculated_amount=total.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN),
        contributions=tuple(contributions),
    )


def contribution_evidence(
    contributions: Sequence[OrdinalTierContribution],
) -> list[dict[str, object]]:
    """The expanded explanation of one calculation as it is frozen on its evidence line."""
    return [
        {
            "SequenceNo": c.sequence_no,
            "OrdinalFrom": c.ordinal_from,
            "OrdinalTo": c.ordinal_to,
            "Units": c.units,
            "RateAmount": c.rate_amount,
            "Amount": c.amount,
        }
        for c in contributions
    ]
