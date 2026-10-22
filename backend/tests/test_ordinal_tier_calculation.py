"""OrdinalTier arithmetic and its place behind the method-owned calculation boundary.

OrdinalTier is progressive per ordinal unit: the Nth unit earns the value of the tier covering
ordinal N. These tests need no database; the boundary receives the whole resolved schedule.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.compensation.ordinal_topology import topology_error
from app.compensation.schemas import ResolvedCompensation, ResolvedComponent
from app.payroll.calculation.ordinal_tier import (
    ORDINAL_TIER_CALCULATION_VERSION,
    OrdinalTier,
    calculate_ordinal_tier,
    contribution_evidence,
)
from app.payroll.definition_calculation import (
    CalculationStatus,
    PeriodDefinition,
    calculate_definition_input,
    is_method_operational,
    quantity_error,
)

D = Decimal


def _tiers(*values, bounds=((1, 1), (2, 2), (3, None))):
    return [
        OrdinalTier(sequence_no=index + 1, ordinal_from=low, ordinal_to=high, rate_amount=D(value))
        for index, ((low, high), value) in enumerate(zip(bounds, values, strict=True))
    ]


# ---------------------------------------------------------------------------
# The pure arithmetic
# ---------------------------------------------------------------------------

def test_the_canonical_example_is_progressive_per_ordinal_unit():
    """1=3, 2=4, 3+=5 and a quantity of 4 earn 3 + 4 + 5 + 5."""
    result = calculate_ordinal_tier(D("4"), _tiers("3", "4", "5"))
    assert result.calculated_amount == D("17")
    assert result.calculation_version == ORDINAL_TIER_CALCULATION_VERSION
    assert [(c.units, c.amount) for c in result.contributions] == [
        (1, D("3")), (1, D("4")), (2, D("10"))]


def test_it_is_not_a_bracket_rate_applied_to_the_whole_quantity():
    # A bracket reading would pay 4 x 5 = 20; the progressive meaning pays 17.
    assert calculate_ordinal_tier(D("4"), _tiers("3", "4", "5")).calculated_amount != D("20")


@pytest.mark.parametrize(("quantity", "expected"), [
    ("0", "0"), ("1", "3"), ("2", "7"), ("3", "12"), ("4", "17"), ("10", "47"),
])
def test_each_quantity_pays_the_tiers_it_reaches(quantity, expected):
    assert calculate_ordinal_tier(D(quantity), _tiers("3", "4", "5")).calculated_amount == D(expected)


def test_a_zero_quantity_earns_nothing_but_still_explains_every_tier():
    result = calculate_ordinal_tier(D("0"), _tiers("3", "4", "5"))
    assert result.calculated_amount == D("0")
    assert [c.units for c in result.contributions] == [0, 0, 0]


def test_an_explicit_zero_tier_value_is_a_valid_value():
    assert calculate_ordinal_tier(D("4"), _tiers("3", "0", "5")).calculated_amount == D("13")
    assert calculate_ordinal_tier(D("4"), _tiers("0", "0", "0")).calculated_amount == D("0")


def test_multi_unit_tiers_and_a_single_open_tier():
    tiers = _tiers("2", "3", "1", bounds=((1, 3), (4, 5), (6, None)))
    assert calculate_ordinal_tier(D("7"), tiers).calculated_amount == D("2") * 3 + D("3") * 2 + D("2")
    assert calculate_ordinal_tier(D("2"), tiers).calculated_amount == D("4")
    only = _tiers("2.5", bounds=((1, None),))
    assert calculate_ordinal_tier(D("4"), only).calculated_amount == D("10")


def test_the_result_is_exact_at_internal_precision_with_no_currency_rounding():
    result = calculate_ordinal_tier(D("3"), _tiers("0.3333", "0.3333", "0.3333"))
    assert result.calculated_amount == D("0.9999")
    big = calculate_ordinal_tier(D("99999999999999"), _tiers("0.0001", "0.0001", "0.0001"))
    assert big.calculated_amount == D("9999999999.9999")


def test_an_integral_decimal_quantity_is_a_whole_number():
    assert calculate_ordinal_tier(D("4.0000"), _tiers("3", "4", "5")).calculated_amount == D("17")


@pytest.mark.parametrize("quantity", ["2.5", "-1", "NaN", "Infinity"])
def test_a_quantity_that_is_not_a_non_negative_whole_number_is_refused(quantity):
    with pytest.raises(ValueError):
        calculate_ordinal_tier(D(quantity), _tiers("3", "4", "5"))


def test_a_tier_value_must_be_a_non_negative_decimal():
    with pytest.raises(ValueError):
        calculate_ordinal_tier(D("2"), _tiers("3", "-1", "5"))


def test_an_invalid_topology_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        calculate_ordinal_tier(D("2"), _tiers("3", "5", bounds=((1, 1), (3, None))))


def test_the_explanation_names_every_tier_exactly_as_calculated():
    evidence = contribution_evidence(calculate_ordinal_tier(D("4"), _tiers("3", "4", "5")).contributions)
    assert evidence == [
        {"SequenceNo": 1, "OrdinalFrom": 1, "OrdinalTo": 1, "Units": 1,
         "RateAmount": D("3"), "Amount": D("3")},
        {"SequenceNo": 2, "OrdinalFrom": 2, "OrdinalTo": 2, "Units": 1,
         "RateAmount": D("4"), "Amount": D("4")},
        {"SequenceNo": 3, "OrdinalFrom": 3, "OrdinalTo": None, "Units": 2,
         "RateAmount": D("5"), "Amount": D("10")},
    ]


# ---------------------------------------------------------------------------
# The topology a definition owns
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tiers", [
    [(1, None)],
    [(1, 1), (2, 2), (3, None)],
    [(1, 5), (6, 10), (11, None)],
])
def test_a_gapless_one_based_topology_with_one_final_open_tier_is_valid(tiers):
    assert topology_error(tiers) is None


@pytest.mark.parametrize(("tiers", "fragment"), [
    ([], "At least one"),
    ([(2, None)], "start at 1"),
    ([(0, 1), (2, None)], "positive"),
    ([(1, 1), (3, None)], "gap"),
    ([(1, 3), (3, None)], "gap or overlap"),
    ([(1, 2), (2, 3), (4, None)], "gap or overlap"),
    ([(1, None), (2, None)], "Only the last"),
    ([(1, 1), (2, 2)], "must be open-ended"),
    ([(1, 0), (1, None)], "positive"),
    ([(3, 2), (3, None)], "start at 1"),
    ([(1, -1), (0, None)], "positive"),
])
def test_gaps_overlaps_bad_bounds_and_open_ends_are_rejected(tiers, fragment):
    assert fragment in (topology_error(tiers) or "")


def test_a_tier_cannot_end_before_it_starts():
    assert "before it starts" in topology_error([(1, 2), (3, 2), (3, None)])


# ---------------------------------------------------------------------------
# The method-owned boundary
# ---------------------------------------------------------------------------

def _definition(method="OrdinalTier", shape="OrdinalTierSchedule", version=1):
    return PeriodDefinition(
        payroll_period_definition_id=1, pay_definition_id=2, rate_definition_id=3,
        code="ITEM_BETA", name="Item beta", input_type="WholeNumber", unit="stop",
        calculation_method=method, calculation_method_version=version, rate_shape=shape,
        is_active=True)


def _resolved(*amounts, bounds=((1, 1), (2, 2), (3, None)), shape="OrdinalTierSchedule"):
    return ResolvedCompensation(
        company_id=1, driver_id=1, pay_definition_id=2, rate_definition_id=3, rate_shape=shape,
        calculation_method="OrdinalTier", driver_rate_schedule_id=11, schedule_status="Approved",
        effective_from=date(2026, 1, 1),
        components=[
            ResolvedComponent(
                rate_component_definition_id=100 + index, shape=shape, sequence_no=index + 1,
                ordinal_from=low, ordinal_to=high, amount=None if amount is None else D(amount))
            for index, ((low, high), amount) in enumerate(zip(bounds, amounts, strict=True))
        ])


def test_the_registered_pairs_are_exactly_the_two_first_production_methods():
    assert is_method_operational("PerUnit", 1)
    assert is_method_operational("OrdinalTier", 1)
    for method, version in (("OrdinalTier", 0), ("OrdinalTier", 2), ("PerUnit", 2),
                            ("RangeBracket", 1), ("RangeProgressive", 1), ("Block", 1),
                            ("Fixed", 1), ("", 1)):
        assert not is_method_operational(method, version)


def test_the_boundary_receives_the_whole_schedule_and_returns_the_canonical_17():
    result = calculate_definition_input(_definition(), _resolved("3", "4", "5"), D("4"))
    assert result.status is CalculationStatus.CALCULATED
    assert result.amount == D("17")
    assert result.calculation_version == ORDINAL_TIER_CALCULATION_VERSION
    assert result.driver_rate_schedule_id == 11
    assert (result.schedule_status, result.schedule_effective_from) == (
        "Approved", date(2026, 1, 1))
    # No single scalar rate describes the line: the schedule's tier values and the expansion do.
    assert result.rate_amount is None and result.rate_component_definition_id is None
    assert [(v.sequence_no, v.rate_component_definition_id, v.ordinal_from, v.ordinal_to, v.amount)
            for v in result.rate_values] == [
        (1, 100, 1, 1, D("3")), (2, 101, 2, 2, D("4")), (3, 102, 3, None, D("5"))]
    assert sum(c.amount for c in result.tier_contributions) == D("17")


def test_zero_quantity_and_explicit_zero_values_are_valid():
    assert calculate_definition_input(
        _definition(), _resolved("3", "4", "5"), D("0")).amount == D("0")
    zero = calculate_definition_input(_definition(), _resolved("0", "0", "0"), D("9"))
    assert (zero.status, zero.amount) == (CalculationStatus.CALCULATED, D("0"))


def test_no_schedule_is_missing_never_zero():
    result = calculate_definition_input(_definition(), None, D("4"))
    assert result.status is CalculationStatus.MISSING_RATE and result.amount is None
    assert result.needs_attention


def test_a_schedule_missing_any_tier_value_is_incomplete_never_zero():
    for amounts in (("3", None, "5"), (None, None, None), ("3", "4", None)):
        result = calculate_definition_input(_definition(), _resolved(*amounts), D("4"))
        assert result.status is CalculationStatus.INCOMPLETE_RATE and result.amount is None
        assert result.driver_rate_schedule_id == 11
        assert result.needs_attention
    # A schedule of another shape cannot stand in for the tiers.
    wrong_shape = calculate_definition_input(
        _definition(), _resolved("3", bounds=((1, None),), shape="Scalar"), D("4"))
    assert wrong_shape.status is CalculationStatus.INCOMPLETE_RATE
    # Neither can one whose components do not form the topology.
    broken = calculate_definition_input(
        _definition(), _resolved("3", "5", bounds=((1, 1), (3, None))), D("4"))
    assert broken.status is CalculationStatus.INCOMPLETE_RATE


@pytest.mark.parametrize("quantity", ["2.5", "-1"])
def test_a_quantity_the_method_cannot_accept_is_a_distinct_blocker(quantity):
    result = calculate_definition_input(_definition(), _resolved("3", "4", "5"), D(quantity))
    assert result.status is CalculationStatus.INVALID_QUANTITY
    assert result.amount is None and result.needs_attention
    assert result.driver_rate_schedule_id == 11


def test_whole_number_entry_validation_is_unchanged():
    assert quantity_error("WholeNumber", D("4")) is None
    assert quantity_error("WholeNumber", D("0")) is None
    assert "whole number" in quantity_error("WholeNumber", D("4.5"))


def test_a_future_version_never_runs_the_first_algorithm():
    for version in (0, 2):
        result = calculate_definition_input(
            _definition(version=version), _resolved("3", "4", "5"), D("4"))
        assert result.status is CalculationStatus.METHOD_NOT_READY and result.amount is None


def test_per_unit_is_unchanged_beside_it():
    per_unit = PeriodDefinition(
        payroll_period_definition_id=1, pay_definition_id=2, rate_definition_id=3, code="A",
        name="A", input_type="Decimal", unit="u", calculation_method="PerUnit",
        calculation_method_version=1, rate_shape="Scalar", is_active=True)
    resolved = ResolvedCompensation(
        company_id=1, driver_id=1, pay_definition_id=2, rate_definition_id=3, rate_shape="Scalar",
        calculation_method="PerUnit", driver_rate_schedule_id=11, schedule_status="Approved",
        effective_from=date(2026, 1, 1),
        components=[ResolvedComponent(
            rate_component_definition_id=100, shape="Scalar", sequence_no=1, amount=D("25"))])
    result = calculate_definition_input(per_unit, resolved, D("8"))
    assert (result.status, result.amount, result.rate_amount) == (
        CalculationStatus.CALCULATED, D("200"), D("25"))
    assert result.rate_values == () and result.tier_contributions == ()
