"""
Draft-line calculation kernel — computes a calculated amount for a
Draft/source line from already-supplied inputs and rates.

Extracted from app.payroll.service (Stage B4-13C) as a dependency-closed leaf
module — no behavior change, pure relocation.

_compute_calculated_amount is the pure rate-behavior dispatch kernel:
EnteredAmount, Fixed/None (informational/unimplemented), the three tiered
behaviors (OrdinalTier, RangeBracket, RangeProgressive) and Block (delegated
to app.payroll.rates), and PerUnit (driver-rate lookup + the CP-4A pure core
in app.payroll.calculation.per_unit). It performs no DraftLine mutation, no
orchestration, and no snapshot/packet construction — it is reusable by any
caller that needs to resolve an amount from a rate_behavior/rate_code/
quantity/driver/date tuple. _CalcResult is its return-value NamedTuple,
co-located here because it exists only to describe this function's output
shape.

Reused (not owned) by Draft CRUD (add_draft_line, update_draft_line, in
app.payroll.draft_line_mutation), _refresh_draft_calculations, and
_compute_draft_line_preview_amounts (both in app.payroll.period_calculation).
"""
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.calculation.per_unit import (
    PerUnitInput as _PerUnitInput,
)
from app.payroll.calculation.per_unit import (
    calculate_per_unit as _calculate_per_unit,
)
from app.payroll.rates import (
    _TIERED_BEHAVIORS,
    _compute_block,
    _compute_ordinal_tier,
    _compute_range_bracket,
    _compute_range_progressive,
)


class _CalcResult(NamedTuple):
    """
    Return value of _compute_calculated_amount.

    Phase 3B adds source fields so callers can store them in PayrollFinalLines:
      driver_rate_id       -- DriverRates.DriverRateID used (PerUnit / Tiered / Block)
      rate_type_id         -- RateTypes.RateTypeID of the matched rate
      resolved_rate_amount -- dr.Amount (PerUnit only; NULL for tiered/block)

    Fields default to None so existing callers that only unpack (calc, nmr)
    are unaffected as long as they use positional unpacking of the first two
    fields or attribute access.
    """
    calculated_amount:    Decimal | None
    needs_manager_review: bool
    driver_rate_id:       int | None = None
    rate_type_id:         int | None = None
    resolved_rate_amount: Decimal | None = None
    rate_behavior:        str | None = None


async def _compute_calculated_amount(
    rate_behavior: str,
    rate_code: str | None,
    quantity: Decimal,
    rate_amount_override: Decimal | None,
    driver_id: int,
    company_id: int,
    as_of_date: date,
    db: AsyncConnection,
) -> "_CalcResult":
    """
    Compute the calculated amount for a draft line.

    Returns a _CalcResult NamedTuple:
      (calculated_amount, needs_manager_review,
       driver_rate_id, rate_type_id, resolved_rate_amount)

    Phase 3B: the last three fields are populated for rate-based lookups so
    callers (finalization, preview) can snapshot the source into FinalLines.
    Existing callers that only unpack the first two positional fields are
    unaffected — NamedTuple positional access still works.

    rate_behavior dispatch:
      PerUnit:       qty × approved DriverRate for rate_code (looked up by date).
                     No approved rate → (None, True) — flagged for review.
                     No rate_code mapping → (None, True).
      EnteredAmount: (rate_amount_override, False) — user-supplied dollar amount.
      Fixed:         (None, False) — fixed amounts from PayItemSettings (M13c+).
      None / other:  (None, False) — informational or unimplemented behavior.
    Tiered/Block:    delegates to dedicated helpers which return (amount, nmr,
                     driver_rate_id, rate_type_id).
    """
    if rate_behavior == "EnteredAmount":
        return _CalcResult(rate_amount_override, False, rate_behavior="EnteredAmount")

    # M13c tiered / block behaviors — dispatch to dedicated helpers.
    if rate_behavior in _TIERED_BEHAVIORS or rate_behavior == "Block":
        if not rate_code:
            # No RateType mapping → calculation unresolvable → flag for review.
            return _CalcResult(None, True, rate_behavior=rate_behavior)
        if rate_behavior == "OrdinalTier":
            amt, nmr, rid, rtid = await _compute_ordinal_tier(
                quantity, driver_id, company_id, as_of_date, rate_code, db
            )
            return _CalcResult(amt, nmr, driver_rate_id=rid, rate_type_id=rtid, rate_behavior="OrdinalTier")
        if rate_behavior == "RangeBracket":
            amt, nmr, rid, rtid = await _compute_range_bracket(
                quantity, driver_id, company_id, as_of_date, rate_code, db
            )
            return _CalcResult(amt, nmr, driver_rate_id=rid, rate_type_id=rtid, rate_behavior="RangeBracket")
        if rate_behavior == "RangeProgressive":
            amt, nmr, rid, rtid = await _compute_range_progressive(
                quantity, driver_id, company_id, as_of_date, rate_code, db
            )
            return _CalcResult(amt, nmr, driver_rate_id=rid, rate_type_id=rtid, rate_behavior="RangeProgressive")
        # Block
        amt, nmr, rid, rtid = await _compute_block(
            quantity, driver_id, company_id, as_of_date, rate_code, db
        )
        return _CalcResult(amt, nmr, driver_rate_id=rid, rate_type_id=rtid, rate_behavior="Block")

    if rate_behavior != "PerUnit":
        # Fixed, None — not computed yet.
        return _CalcResult(None, False, rate_behavior=rate_behavior)

    # PerUnit: look up the driver's approved rate for rate_code as-of as_of_date.
    if not rate_code:
        # PerUnit item but no rate type mapping in PayItemRateTypeMap.
        # If the caller supplied a manual rate_amount, the finalization COALESCE
        # will produce a non-zero result (qty * rate_amount) — no review needed.
        # If no rate_amount either, the line would finalize as zero — flag for review.
        if rate_amount_override is not None:
            return _CalcResult(None, False, rate_behavior="PerUnit")
        return _CalcResult(None, True, rate_behavior="PerUnit")

    # Include Superseded rows — a superseded rate is still the correct rate
    # for work dates that fall within its original effective range.
    # (Mirrors the lookup logic in get_driver_rate_on_date.)
    rate_result = await db.execute(
        text("""
            SELECT dr.driverrateid, dr.ratetypeid, dr.amount
            FROM   payroll.driverrates dr
            JOIN   payroll.ratetypes   rt ON rt.ratetypeid = dr.ratetypeid
            WHERE  dr.driverid       = :did
              AND  dr.companyid      = :cid
              AND  rt.ratecode       = :rcode
              AND  dr.status         IN ('Approved', 'Superseded')
              AND  dr.effectivefrom <= :dt
              AND  (dr.effectiveto IS NULL OR dr.effectiveto >= :dt)
            ORDER BY dr.effectivefrom DESC
            LIMIT 1
        """),
        {
            "did":   driver_id,
            "cid":   company_id,
            "rcode": rate_code,
            "dt":    as_of_date,
        },
    )
    rate_row = rate_result.mappings().first()

    if rate_row is None:
        # No approved rate for this driver / type / date.
        # If the caller supplied a manual rate_amount, the finalization COALESCE
        # will produce a non-zero result — no review needed.
        # If no rate_amount either, the line would finalize as zero — flag for review.
        if rate_amount_override is not None:
            return _CalcResult(None, False, rate_behavior="PerUnit")
        return _CalcResult(None, True, rate_behavior="PerUnit")

    resolved_amt = Decimal(str(rate_row["amount"]))
    # CP-4A: authoritative PerUnit multiply/quantize now lives in the pure core.
    calculated = _calculate_per_unit(
        _PerUnitInput(quantity=quantity, rate_amount=resolved_amt)
    ).calculated_amount
    return _CalcResult(
        calculated,
        False,
        driver_rate_id=int(rate_row["driverrateid"]),
        rate_type_id=int(rate_row["ratetypeid"]),
        resolved_rate_amount=resolved_amt,
        rate_behavior="PerUnit",
    )
