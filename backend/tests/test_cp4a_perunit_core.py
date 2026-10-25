"""Focused unit tests for the pure PerUnit calculation core."""
import decimal
from decimal import ROUND_HALF_EVEN, Decimal

import pytest

from app.payroll.calculation.per_unit import (
    PER_UNIT_CALCULATION_VERSION,
    PerUnitInput,
    calculate_per_unit,
)

# ---------------------------------------------------------------------------
# 1. Pure-core tests (no database)
# ---------------------------------------------------------------------------

class TestPureCorePerUnit:

    def test_ordinary_multiplication(self):
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("6.5000"), rate_amount=Decimal("12.3400"))
        )
        assert result.calculated_amount == Decimal("80.2100")

    def test_exact_scale_result(self):
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("6.5000"), rate_amount=Decimal("12.3400"))
        )
        assert result.calculated_amount.as_tuple().exponent == -4, (
            "Completed PerUnit amount must have exactly four decimal places"
        )

    def test_round_half_even_lower_tie_stays(self):
        # 4.0961 x 2.5000 = 10.24025000 exact; retained 4th digit EVEN (2) stays.
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("4.0961"), rate_amount=Decimal("2.5000"))
        )
        assert result.calculated_amount == Decimal("10.2402")

    def test_round_half_even_upper_tie_rounds_up(self):
        # 7.12345 x 3.0000 = 21.370350000 exact; retained 4th digit ODD (3) rounds to even (4).
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("7.12345"), rate_amount=Decimal("3.0000"))
        )
        assert result.calculated_amount == Decimal("21.3704")

    def test_full_precision_multiply_then_quantize_once(self):
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("7.12345"), rate_amount=Decimal("3.0000"))
        )
        raw_product = Decimal("7.12345") * Decimal("3.0000")
        assert raw_product == Decimal("21.370350000"), "Full-precision product must not be pre-rounded"
        assert result.calculated_amount == raw_product.quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_EVEN
        )

    def test_rounding_inputs_first_would_differ_from_sum_then_quantize(self):
        """
        Proves the core does NOT round-then-multiply: rounding the quantity
        to 4dp first before multiplying would silently change this result,
        demonstrating the implemented core preserves full-precision-multiply-
        then-quantize-once behavior instead.
        """
        quantity = Decimal("7.123456")
        rate = Decimal("3.0000")
        exact = calculate_per_unit(PerUnitInput(quantity=quantity, rate_amount=rate)).calculated_amount

        rounded_quantity_first = quantity.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)
        wrong_order = (rounded_quantity_first * rate).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)

        assert exact != wrong_order, (
            "This case must demonstrate that pre-rounding an input changes the "
            "result -- otherwise it does not prove multiply-then-quantize order"
        )
        assert exact == (quantity * rate).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)

    def test_zero_quantity(self):
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("0"), rate_amount=Decimal("99.0000"))
        )
        assert result.calculated_amount == Decimal("0.0000")

    def test_zero_rate(self):
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("8.0000"), rate_amount=Decimal("0"))
        )
        assert result.calculated_amount == Decimal("0.0000")

    def test_negative_value_preserved_no_new_validation(self):
        """
        Characterizes current behavior only: the pure core itself performs no
        sign validation (that responsibility, if any, belongs to callers
        upstream, unchanged by CP-4A) -- a negative product passes through.
        """
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("-2.0000"), rate_amount=Decimal("5.0000"))
        )
        assert result.calculated_amount == Decimal("-10.0000")

    def test_large_but_currently_valid_decimal_values(self):
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("999999.9999"), rate_amount=Decimal("9999.9999"))
        )
        expected = (Decimal("999999.9999") * Decimal("9999.9999")).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_EVEN
        )
        assert result.calculated_amount == expected

    def test_ambient_decimal_precision_compatibility(self):
        """
        The ambient default context (precision=28) must remain unmodified
        and sufficient for the multiplication -- confirmed by comparing
        against Python's context-free `decimal` arithmetic directly.
        """
        import decimal
        ctx = decimal.getcontext()
        assert ctx.prec == 28, "Pure core must not rely on a non-default ambient precision"
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("7.12345"), rate_amount=Decimal("3.0000"))
        )
        assert result.calculated_amount == Decimal("21.3704")

    def test_ambient_decimal_trap_compatibility(self):
        """
        Proves the core itself does not suppress an ambient Decimal trap: with
        `Inexact` enabled in a local context, a real `calculate_per_unit` call
        whose completed-line quantization genuinely loses precision
        (7.12345 x 3.0000 = 21.370350000 exact, quantized to 4dp) must raise
        -- confirming the core's single `.quantize(...)` call is not wrapped
        in any trap-suppressing logic -- and that the outer/global context's
        trap configuration is restored once the local context exits.
        """
        outer_traps_before = dict(decimal.getcontext().traps)
        with decimal.localcontext() as ctx:
            ctx.traps[decimal.Inexact] = True
            with pytest.raises(decimal.Inexact):
                calculate_per_unit(
                    PerUnitInput(quantity=Decimal("7.12345"), rate_amount=Decimal("3.0000"))
                )
        assert dict(decimal.getcontext().traps) == outer_traps_before, (
            "The ambient/global Decimal context's trap configuration must be "
            "unchanged after the local context used by this test exits"
        )

    def test_deterministic_repeated_calls(self):
        data = PerUnitInput(quantity=Decimal("3.25"), rate_amount=Decimal("2.0002"))
        first = calculate_per_unit(data)
        second = calculate_per_unit(data)
        assert first.calculated_amount == second.calculated_amount == Decimal("6.5006")

    def test_input_is_immutable(self):
        data = PerUnitInput(quantity=Decimal("1"), rate_amount=Decimal("1"))
        with pytest.raises(Exception):
            data.quantity = Decimal("2")  # type: ignore[misc]

    def test_result_is_immutable(self):
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("1"), rate_amount=Decimal("1"))
        )
        with pytest.raises(Exception):
            result.calculated_amount = Decimal("2")  # type: ignore[misc]

    def test_explicit_calculation_version(self):
        result = calculate_per_unit(
            PerUnitInput(quantity=Decimal("1"), rate_amount=Decimal("1"))
        )
        assert result.calculation_version == PER_UNIT_CALCULATION_VERSION
        assert isinstance(PER_UNIT_CALCULATION_VERSION, str) and PER_UNIT_CALCULATION_VERSION

    def test_float_quantity_rejected(self):
        with pytest.raises(TypeError):
            PerUnitInput(quantity=6.5, rate_amount=Decimal("12.3400"))  # type: ignore[arg-type]

    def test_float_rate_rejected(self):
        with pytest.raises(TypeError):
            PerUnitInput(quantity=Decimal("6.5000"), rate_amount=12.34)  # type: ignore[arg-type]

    def test_no_global_decimal_context_mutation(self):
        import decimal
        before = decimal.getcontext().prec
        before_rounding = decimal.getcontext().rounding
        calculate_per_unit(
            PerUnitInput(quantity=Decimal("7.12345"), rate_amount=Decimal("3.0000"))
        )
        assert decimal.getcontext().prec == before
        assert decimal.getcontext().rounding == before_rounding


# ---------------------------------------------------------------------------
