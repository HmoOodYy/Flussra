"""DD3 pure cores: the one payable-rounding function, Min/Max proration and typed-amount
normalization. No database."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal, localcontext

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.company_currency import CompanyCurrency
from app.payroll.calculation import minmax_proration as minmax
from app.payroll.calculation.payable_rounding import (
    INTERNAL_QUANTUM,
    PAYABLE_ROUNDING_VERSION,
    is_payable_precision,
    round_payable,
)
from app.payroll.schemas import (
    BonusBatchItem,
    BonusEventCreate,
    BonusEventUpdate,
    DriverPayRuleCreate,
)
from app.payroll.typed_amount import normalize_typed_amount

USD = CompanyCurrency("USD", 2)
JPY = CompanyCurrency("JPY", 0)
KWD = CompanyCurrency("KWD", 3)

D = Decimal


# ---------------------------------------------------------------------------
# round_payable
# ---------------------------------------------------------------------------

def test_the_versions_and_the_internal_quantum_are_pinned():
    assert PAYABLE_ROUNDING_VERSION == "payable-rounding-v1"
    assert INTERNAL_QUANTUM == D("0.0001")
    assert minmax.MINMAX_PRORATION_VERSION == "minmax-proration-v1"


@pytest.mark.parametrize("amount, digits, expected", [
    # USD: ties go to the even neighbour.
    ("0.125", 2, "0.12"), ("0.135", 2, "0.14"), ("2.675", 2, "2.68"), ("0.115", 2, "0.12"),
    ("0.1251", 2, "0.13"), ("0.1249", 2, "0.12"), ("12.3456", 2, "12.35"),
    # JPY: no decimals.
    ("0.5", 0, "0"), ("1.5", 0, "2"), ("2.5", 0, "2"), ("3.5", 0, "4"), ("2.4999", 0, "2"),
    ("1500.5", 0, "1500"),
    # KWD: three decimals.
    ("1.2345", 3, "1.234"), ("1.2355", 3, "1.236"), ("1.2346", 3, "1.235"),
    # Four decimals is the internal precision itself.
    ("1.23455", 4, "1.2346"), ("1.23445", 4, "1.2344"), ("7.1234", 4, "7.1234"),
    # Negative amounts (a Maximum cap) round symmetrically.
    ("-0.125", 2, "-0.12"), ("-0.135", 2, "-0.14"), ("-2.5", 0, "-2"),
])
def test_round_payable_is_half_even_to_the_currency_decimals(amount, digits, expected):
    result = round_payable(D(amount), digits)
    assert result == D(expected)
    assert result.as_tuple().exponent == -digits   # exactly the currency's decimals


def test_zero_has_no_sign_and_keeps_the_currency_scale():
    for amount in ("-0.004", "-0.0049", "0", "-0.0000"):
        result = round_payable(D(amount), 2)
        assert str(result) == "0.00"


def test_rounding_ignores_the_ambient_decimal_context():
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        assert round_payable(D("12345678901234.5678"), 2) == D("12345678901234.57")
        assert round_payable(D("0.135"), 2) == D("0.14")


def test_sum_then_round_differs_from_per_line_rounding():
    lines = [D("0.3333")] * 3                       # three days of raw line amounts
    official = round_payable(sum(lines, D("0")), 2)
    per_line = sum((round_payable(line, 2) for line in lines), D("0"))
    assert official == D("1.00")
    assert per_line == D("0.99")
    assert official != per_line


@pytest.mark.parametrize("bad", [1.5, 1, "1.5", None])
def test_round_payable_rejects_non_decimals(bad):
    with pytest.raises(TypeError):
        round_payable(bad, 2)


@pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_round_payable_rejects_non_finite_amounts(bad):
    with pytest.raises(ValueError):
        round_payable(D(bad), 2)


@pytest.mark.parametrize("digits", [-1, 5, 10])
def test_round_payable_rejects_digits_outside_zero_to_four(digits):
    with pytest.raises(ValueError):
        round_payable(D("1"), digits)


@pytest.mark.parametrize("digits", [True, 2.0, "2", None])
def test_round_payable_rejects_non_integer_digits(digits):
    with pytest.raises(TypeError):
        round_payable(D("1"), digits)


def test_payable_precision_means_no_decimals_beyond_the_currency():
    assert is_payable_precision(D("12.34"), 2)
    assert is_payable_precision(D("12.3400"), 2)
    assert is_payable_precision(D("12"), 2)
    assert not is_payable_precision(D("12.345"), 2)
    assert is_payable_precision(D("1500"), 0)
    assert not is_payable_precision(D("1500.0001"), 0)
    assert is_payable_precision(D("1.234"), 3)


# ---------------------------------------------------------------------------
# Min/Max whole-day proration
# ---------------------------------------------------------------------------

WEEK = [date(2026, 3, 2) + timedelta(days=i) for i in range(7)]      # Monday .. Sunday
WORK_WEEK = WEEK[:5]


def test_coverage_counts_only_eligible_scheduled_non_off_days():
    assert minmax.coverage(WEEK, WEEK, []) == (7, 7)
    assert minmax.coverage(WEEK, WEEK[3:], []) == (4, 7)                 # hired on the Thursday
    assert minmax.coverage(WEEK, WEEK, WEEK[:2]) == (5, 7)               # two off-reason days
    assert minmax.coverage(WORK_WEEK, WEEK, []) == (5, 5)                # unscheduled days never count
    assert minmax.coverage(WORK_WEEK, WEEK[5:], []) == (0, 5)            # weekend start
    assert minmax.coverage(WORK_WEEK, WEEK[3:], []) == (2, 5)            # worked days after Friday add nothing
    assert minmax.coverage([], WEEK, []) == (0, 0)                       # no scheduled day at all
    assert minmax.coverage(WEEK, WEEK, WEEK) == (0, 7)                   # every day off


def test_a_full_period_returns_the_rule_amount_unchanged():
    assert minmax.prorate(D("123.4567"), 7, 7) == D("123.4567")


def test_prorating_stays_at_internal_precision_half_even():
    assert minmax.prorate(D("700"), 4, 7) == D("400.0000")
    assert minmax.prorate(D("100"), 3, 7) == D("42.8571")
    assert minmax.prorate(D("100"), 4, 7) == D("57.1429")
    assert minmax.prorate(D("100"), 0, 7) == D("0.0000")
    assert minmax.prorate(D("0.0005"), 1, 2) == D("0.0002")             # 0.00025 -> tie, even
    assert minmax.prorate(D("0.0015"), 1, 2) == D("0.0008")             # 0.00075 -> tie, even


def test_two_complementary_coverages_sum_to_about_one_minimum():
    minimum = D("100")
    first, second = minmax.prorate(minimum, 3, 7), minmax.prorate(minimum, 4, 7)
    assert abs(first + second - minimum) <= D("0.0001")
    assert abs(round_payable(first, 2) + round_payable(second, 2) - minimum) <= D("0.01")


@pytest.mark.parametrize("numerator, denominator", [(1, 0), (-1, 7), (8, 7), (0, 0)])
def test_an_impossible_fraction_is_refused(numerator, denominator):
    with pytest.raises(ValueError):
        minmax.prorate(D("100"), numerator, denominator)


def test_raw_adjustments_follow_the_prorated_bounds():
    assert minmax.raw_minimum_adjustment(D("100"), D("400")) == D("300")
    assert minmax.raw_minimum_adjustment(D("400"), D("400")) == D("0")
    assert minmax.raw_minimum_adjustment(D("500"), D("400")) == D("0")
    assert minmax.raw_minimum_adjustment(D("500"), None) == D("0")
    assert minmax.raw_maximum_adjustment(D("500"), D("400")) == D("-100")
    assert minmax.raw_maximum_adjustment(D("400"), D("400")) == D("0")
    assert minmax.raw_maximum_adjustment(D("300"), D("400")) == D("0")
    assert minmax.raw_maximum_adjustment(D("300"), None) == D("0")


# ---------------------------------------------------------------------------
# Typed-in Bonus / Minimum / Maximum amounts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("currency, typed, expected", [
    (USD, "12.345", "12.34"), (USD, "12.355", "12.36"), (USD, "0.005", "0.00"),
    (JPY, "1500.5", "1500"), (JPY, "1501.5", "1502"), (JPY, "0.5", "0"),
    (KWD, "1.2345", "1.234"), (KWD, "100.1235", "100.124"),
])
def test_a_typed_amount_is_normalized_with_the_payable_rounding(currency, typed, expected):
    if D(expected) == 0:
        with pytest.raises(HTTPException) as refused:
            normalize_typed_amount(D(typed), currency, label="Bonus amount")
        assert refused.value.status_code == 422
        assert refused.value.detail["code"] == "AMOUNT_ROUNDS_TO_ZERO"
        return
    assert normalize_typed_amount(D(typed), currency, label="Bonus amount") == D(expected)


def test_an_amount_that_rounds_to_zero_is_refused_with_a_clear_message():
    with pytest.raises(HTTPException) as refused:
        normalize_typed_amount(D("0.004"), USD, label="Bonus amount")
    assert refused.value.status_code == 422
    message = refused.value.detail["message"]
    assert "0.004" in message and "USD" in message and "0.01" in message
    with pytest.raises(HTTPException) as jpy:
        normalize_typed_amount(D("0.4"), JPY, label="Driver pay rule amount")
    assert "at least 1" in jpy.value.detail["message"]


def test_an_amount_that_overflows_numeric_18_4_once_rounded_is_refused():
    assert normalize_typed_amount(D("99999999999999.99"), USD, label="Bonus amount") == D("99999999999999.99")
    with pytest.raises(HTTPException) as refused:
        normalize_typed_amount(D("99999999999999.999"), USD, label="Bonus amount")
    assert refused.value.detail["code"] == "AMOUNT_TOO_LARGE"


@pytest.mark.parametrize("model, payload", [
    (BonusEventCreate, {"driver_assignment_id": 1}),
    (BonusEventUpdate, {}),
    (BonusBatchItem, {"driver_assignment_id": 1}),
    (DriverPayRuleCreate, {"driver_id": 1, "rule_type": "MinimumPay", "effective_from": "2026-01-01"}),
])
def test_the_three_typed_amounts_accept_extra_precision_up_to_ten_decimals(model, payload):
    assert model.model_validate({**payload, "amount": "12.3456789012"}).amount == D("12.3456789012")
    assert model.model_validate({**payload, "amount": "1.5000000000000"}).amount == D("1.5")
    for bad in ("1.12345678901", "0", "-1", "NaN", "Infinity", "100000000000000"):
        with pytest.raises(ValidationError):
            model.model_validate({**payload, "amount": bad})
