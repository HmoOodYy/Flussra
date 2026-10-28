"""Exact, resource-bounded numeric operations for Import quantities."""
from __future__ import annotations

from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    Decimal,
    DecimalException,
    Inexact,
    InvalidOperation,
    Rounded,
    localcontext,
)

from app.imports.mapping import NUMBER


def parse_sum_operand(cell: dict) -> tuple[Decimal | None, str | None, bool]:
    """Parse one Sum operand without applying the final target quantity rules."""
    if cell.get("missing_cache"):
        return None, "Formula without a calculated value.", False
    value = cell.get("value")
    kind = cell.get("kind")
    if kind == "blank" or value is None or isinstance(value, str) and not value.strip():
        return None, None, True
    raw = str(value).strip()
    if kind not in ("number", "text") or kind == "text" and not NUMBER.fullmatch(raw):
        return None, "Numbers only; use digits with an optional decimal point.", False
    try:
        quantity = Decimal(raw)
    except InvalidOperation:
        return None, "Numbers only.", False
    if not quantity.is_finite():
        return None, "Quantity must be a finite number.", False
    return quantity, None, False

def exact_decimal_sum(values: list[Decimal], max_work_digits: int) -> Decimal:
    """Add finite Decimal values exactly, independent of process context."""
    nonzero = [value for value in values if value]
    if not nonzero:
        return Decimal(0)
    lowest_exponent = min(value.as_tuple().exponent for value in nonzero)
    highest_adjusted = max(value.adjusted() for value in nonzero)
    required_span = highest_adjusted - lowest_exponent + len(str(len(nonzero))) + 1
    if required_span > max_work_digits:
        raise ArithmeticError("configured numeric work limit exceeded")
    try:
        with localcontext() as context:
            context.prec = max(1, required_span)
            context.Emax = MAX_EMAX
            context.Emin = MIN_EMIN
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            total = sum(nonzero, Decimal(0))
    except DecimalException as exc:
        raise ArithmeticError("exact decimal sum could not be represented") from exc
    if not total:
        return Decimal(0)
    parts = total.as_tuple()
    digits = list(parts.digits)
    exponent = parts.exponent
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    return Decimal((parts.sign, tuple(digits), exponent))
