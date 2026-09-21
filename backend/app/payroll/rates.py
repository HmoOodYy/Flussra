"""
Pay Rates â€” rate type catalog, driver rate CRUD, tiers/advanced rate methods
(OrdinalTier, RangeBracket, RangeProgressive, Block), approval/void lifecycle,
rate resolution, driver rate matrix, batch save, bulk/pending/history
summaries, and copy_driver_rates.

Extracted from app.payroll.service (Stage B4-4B) as a dependency-closed leaf
module â€” no behavior change, pure relocation. Rates was split across two
separated regions of service.py (rate CRUD/tiers/approvals, and matrix/batch/
copy operations); both were relocated here in full, in their original
relative order.

Depends only on already-extracted/neutral owners: app.core.service (permission/
branch/SQL helpers), app.payroll.guards (own-driver and finalized-period
guards), and app.payroll.driver_pay_rules (_write_pay_rule_audit, used only by
copy_driver_rates's optional include_pay_rules behavior) â€” not on
app.payroll.service.

copy_driver_rates ownership: its primary purpose, request/response contract
(CopyRatesRequest/CopyRatesResult), and core logic (rate copying, conflict
checking, rate audit) are all Rates-domain. The optional include_pay_rules
sub-feature legitimately depends on Driver Pay Rules' own audit function for
that one call â€” the same kind of already-precedented cross-domain dependency
driver_pay_rules.py itself has on guards.py. It is Rates-owned orchestration,
not orchestration that belongs outside this module.
"""
import json
import math
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import _build_in_clause, _check_any_permission, _check_branch_access
from app.payroll.driver_pay_rules import _write_pay_rule_audit
from app.payroll.guards import (
    _check_driver_read_access,
    _check_not_in_finalized_period,
    _check_own_driver_only,
    _get_oda_own_driver_id,
)
from app.payroll.schemas import (
    BatchRateRequest,
    BatchRateSaveResult,
    CopyRatesRequest,
    CopyRatesResult,
    DriverRateCreate,
    DriverRateMatrix,
    DriverRateSummary,
    DriverRateUpdate,
    DriverRatesSummary,
    OrdinalTierCreate,
    RangeTierCreate,
    RateMatrixCurrentRate,
    RateMatrixGroup,
    RateTypeSummary,
    TierSummary,
)


# ===========================================================================
# Pay Rates â€” rate type catalog + driver rate matrix
# ===========================================================================

# M13c: rate behaviors that use DriverRateTiers for calculation.
# Physically defined near the M13a line-type-validation code in
# app.payroll.service until Stage B4-4B; genuinely Rates-owned (classifies
# rate *methods*), not calculation-owned. app.payroll.draft_line_calculation
# imports it directly from this module for _compute_calculated_amount's
# dispatch.
_TIERED_BEHAVIORS: frozenset[str] = frozenset({"OrdinalTier", "RangeBracket", "RangeProgressive"})
# Range behaviors (RangeBracket + RangeProgressive) share the same tier input/storage.
_RANGE_BEHAVIORS: frozenset[str] = frozenset({"RangeBracket", "RangeProgressive"})

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_RATE_SELECT = """
    SELECT
        dr.driverrateid,
        dr.companyid,
        dr.branchid,
        dr.driverid,
        e.fullname       AS drivername,
        dr.ratetypeid,
        rt.ratecode,
        rt.ratename,
        rt.unitname,
        dr.amount,
        dr.effectivefrom,
        dr.effectiveto,
        dr.status,
        dr.createdbyuserid,
        dr.createdatutc,
        dr.approvedbyuserid,
        dr.approvedatutc,
        dr.notes,
        dr.blocksize,
        dr.roundingrule
    FROM   payroll.driverrates      dr
    JOIN   payroll.ratetypes        rt ON rt.ratetypeid  = dr.ratetypeid
    JOIN   core.drivers              d  ON d.driverid    = dr.driverid
    JOIN   core.employees            e  ON e.employeeid  = d.employeeid
"""


def _rate_row_to_summary(r: Any) -> DriverRateSummary:
    return DriverRateSummary(
        driver_rate_id=r["driverrateid"],
        company_id=r["companyid"],
        branch_id=r["branchid"],
        driver_id=r["driverid"],
        driver_name=r["drivername"],
        rate_type_id=r["ratetypeid"],
        rate_code=r["ratecode"],
        rate_name=r["ratename"],
        unit_name=r["unitname"],
        amount=r["amount"],
        effective_from=r["effectivefrom"],
        effective_to=r["effectiveto"],
        status=r["status"],
        created_by_user_id=r["createdbyuserid"],
        created_at_utc=r["createdatutc"],
        approved_by_user_id=r["approvedbyuserid"],
        approved_at_utc=r["approvedatutc"],
        notes=r["notes"],
        block_size=r["blocksize"],
        rounding_rule=r["roundingrule"],
        # tiers are not loaded here (list-endpoint performance).
        # Use _get_rate_with_tiers() for detail responses.
    )


async def _get_rate_by_id(
    rate_id: int,
    company_id: int,
    db: AsyncConnection,
) -> DriverRateSummary:
    result = await db.execute(
        text(f"{_RATE_SELECT} WHERE dr.driverrateid = :rid AND dr.companyid = :cid"),
        {"rid": rate_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Driver rate not found.")
    return _rate_row_to_summary(row)


async def _get_rate_with_tiers(
    rate_id: int,
    company_id: int,
    db: AsyncConnection,
) -> DriverRateSummary:
    """Like _get_rate_by_id but also loads DriverRateTiers into the response."""
    rate = await _get_rate_by_id(rate_id, company_id, db)
    tiers = await _load_tiers(rate_id, db)
    return rate.model_copy(update={"tiers": tiers})


# ---------------------------------------------------------------------------
# M13c: Tier helpers
# ---------------------------------------------------------------------------

async def _assert_rate_type_allowed_for_company(
    db: AsyncConnection,
    company_id: int,
    rate_type_id: int,
) -> None:
    """
    Guard: ensure rate_type_id is usable by company_id.

    Phase 4C: uses the structural RateTypes.CompanyID column as the primary check.

    Decision table:
      RateTypes.CompanyID IS NULL                  -> system type, ALLOW
      RateTypes.CompanyID = company_id             -> own custom type, ALLOW
      RateTypes.CompanyID != company_id (NOT NULL) -> foreign type, REJECT
      RateType inactive or not found               -> REJECT

    The mapping-based three-flag logic from Phase 4B is preserved as a secondary
    defence-in-depth validation for system types (ensuring a system-type RateType
    actually has a system PayItem mapping and is not merely an unmapped row that
    somehow kept companyid=NULL after the migration).
    """
    # Step 1: fetch the rate type row (existence, activity, and structural ownership).
    rt_result = await db.execute(
        text("""
            SELECT ratetypeid, ratecode, companyid, isactive
            FROM   payroll.ratetypes
            WHERE  ratetypeid = :rtid
        """),
        {"rtid": rate_type_id},
    )
    rt_row = rt_result.mappings().first()

    if rt_row is None or not rt_row["isactive"]:
        raise HTTPException(status_code=422, detail="rate_type_id does not exist or is inactive.")

    rt_company = rt_row["companyid"]  # NULL = system, non-NULL = company-owned

    # Step 2: structural ownership check (Phase 4C primary guard).
    if rt_company is None:
        return  # system/global type -- allowed for any company
    if rt_company == company_id:
        return  # own-company custom type -- allowed
    # rt_company is set to a different company: reject.
    raise HTTPException(
        status_code=422,
        detail="Rate type does not belong to this company.",
    )


async def _is_status_rate_type(rate_type_id: int, db: AsyncConnection) -> bool:
    """Return True if rate_type_id is referenced by any active StatusRateColumns row."""
    row = (await db.execute(
        text("""
            SELECT 1 FROM payroll.statusratecolumns
            WHERE ratetypeid = :rtid AND isactive = TRUE
            LIMIT 1
        """),
        {"rtid": rate_type_id},
    )).first()
    return row is not None


async def _assert_status_rate_type_for_branch(
    rate_type_id: int,
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> None:
    """
    CP-2D2 branch guard for status-payment RateTypes.

    If rate_type_id is referenced by payroll.StatusRateColumns (anywhere), then it is a
    status-only RateType and must have an active StatusRateColumns row for (branch_id,
    company_id) specifically.  This prevents a driver in branch A from creating rates
    using a status RateType that only exists for branch B.

    Raises HTTPException 422 if the RateType is status-only but does not belong to the
    driver's branch.  Does nothing for ordinary PayItem RateTypes.
    """
    # First: is this a status-backed RateType at all?
    is_status = await _is_status_rate_type(rate_type_id, db)
    if not is_status:
        return  # ordinary PayItem rate â€” existing validation applies

    # Second: require an active StatusRateColumns row for this specific branch/company.
    branch_row = (await db.execute(
        text("""
            SELECT 1 FROM payroll.statusratecolumns
            WHERE ratetypeid = :rtid
              AND branchid   = :bid
              AND companyid  = :cid
              AND isactive   = TRUE
            LIMIT 1
        """),
        {"rtid": rate_type_id, "bid": branch_id, "cid": company_id},
    )).first()
    if branch_row is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"rate_type_id={rate_type_id} is a status-payment RateType but is not "
                "configured for this driver's branch. Use the batch rate save endpoint "
                "with status_rate_column_id to set status pay rates."
            ),
        )


async def _resolve_rate_behavior(
    rate_type_id: int,
    company_id: int,
    db: AsyncConnection,
) -> str:
    """
    Return the RateBehavior of the PayItem mapped to this RateType for this company.

    Company-specific (custom) items take priority over system items (companyid IS NULL).

    Phase 8 â€” Fail-closed: raises HTTPException 422 when no PayItemRateTypeMap entry
    exists for this RateType.

    CP-2D2 exception: RateTypes that are referenced by active StatusRateColumns rows are
    status-payment-only types that always use PerUnit behavior (HoursValue Ã— Amount).
    Membership is verified via a DB lookup â€” RateCode prefix alone is not sufficient.
    """
    # CP-2D2: StatusRateColumn-backed types skip PayItemRateTypeMap resolution.
    # Verified by actual DB membership, not by RateCode prefix.
    if await _is_status_rate_type(rate_type_id, db):
        return "PerUnit"

    result = await db.execute(
        text("""
            SELECT pi.ratebehavior
            FROM   payroll.payitemratetypemap pirtm
            JOIN   payroll.payitems pi ON pi.payitemid = pirtm.payitemid
            WHERE  pirtm.ratetypeid = :rtid
              AND  pirtm.status     = 'Active'
              AND  pi.status        = 'Active'
              AND  (pi.companyid = :cid OR pi.companyid IS NULL)
            ORDER BY
                CASE WHEN pi.companyid = :cid THEN 0 ELSE 1 END,
                pirtm.isprimary DESC
            LIMIT 1
        """),
        {"rtid": rate_type_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Rate behavior could not be resolved for this rate type. "
                "Please configure the pay item rate mapping before creating "
                "or approving driver rates for this rate type."
            ),
        )
    return row["ratebehavior"]


async def _load_tiers(
    rate_id: int,
    db: AsyncConnection,
) -> list[TierSummary]:
    """Load all DriverRateTiers for a rate, ordered by TierSequence."""
    result = await db.execute(
        text("""
            SELECT tiersequence, fromunit, tounit, tieramount
            FROM   payroll.driverratetiers
            WHERE  driverrateid = :rid
            ORDER  BY tiersequence
        """),
        {"rid": rate_id},
    )
    return [
        TierSummary(
            tier_sequence=r["tiersequence"],
            from_unit=r["fromunit"],
            to_unit=r["tounit"],
            tier_amount=r["tieramount"],
        )
        for r in result.mappings().all()
    ]


async def _insert_tiers(
    rate_id: int,
    prepared: list[dict],
    db: AsyncConnection,
) -> None:
    """Delete existing tiers for rate_id, then bulk-insert the prepared list."""
    await db.execute(
        text("DELETE FROM payroll.driverratetiers WHERE driverrateid = :rid"),
        {"rid": rate_id},
    )
    for t in prepared:
        await db.execute(
            text("""
                INSERT INTO payroll.driverratetiers
                    (driverrateid, tiersequence, fromunit, tounit, tieramount)
                VALUES (:rid, :seq, :from_unit, :to_unit, :amount)
            """),
            {
                "rid":       rate_id,
                "seq":       t["tier_sequence"],
                "from_unit": t["from_unit"],
                "to_unit":   t["to_unit"],
                "amount":    t["tier_amount"],
            },
        )


def _validate_ordinal_tiers(
    tiers: list[OrdinalTierCreate],
) -> list[dict]:
    """
    Validate OrdinalTier tier list and return prepared dicts ready for DB insert.

    Rules:
      - At least 1 tier.
      - TierSequence gapless: 1, 2, ..., N.
      - Tier 1 from_unit must == 1.
      - Each tier i > 1: from_unit == prev to_unit + 1 (integer, contiguous).
      - Last tier: to_unit must be None.
      - All other tiers: to_unit must not be None.
      - All from_unit / to_unit must be integers (no fractional ordinal positions).
      - tier_amount > 0 (enforced by OrdinalTierCreate validator).
    """
    if not tiers:
        raise HTTPException(422, "OrdinalTier rates require at least one tier.")

    seqs = [t.tier_sequence for t in tiers]
    if seqs != list(range(1, len(tiers) + 1)):
        raise HTTPException(422, "ordinal_tiers: tier_sequence must be gapless starting at 1.")

    sorted_tiers = sorted(tiers, key=lambda t: t.tier_sequence)

    if sorted_tiers[0].from_unit != 1:
        raise HTTPException(422, "OrdinalTier: first tier from_unit must be 1.")

    result: list[dict] = []
    for i, tier in enumerate(sorted_tiers):
        is_last = (i == len(sorted_tiers) - 1)

        if not is_last:
            if tier.to_unit is None:
                raise HTTPException(
                    422,
                    f"Tier {tier.tier_sequence}: only the last tier can have to_unit=NULL.",
                )
            if tier.to_unit < tier.from_unit:
                raise HTTPException(
                    422,
                    f"Tier {tier.tier_sequence}: to_unit must be >= from_unit.",
                )
            next_tier = sorted_tiers[i + 1]
            expected_from = tier.to_unit + 1
            if next_tier.from_unit != expected_from:
                raise HTTPException(
                    422,
                    f"Tier {next_tier.tier_sequence}: from_unit must equal previous to_unit + 1 "
                    f"(expected {expected_from}, got {next_tier.from_unit}).",
                )
        else:
            if tier.to_unit is not None:
                raise HTTPException(
                    422,
                    "OrdinalTier: the last tier must have to_unit=NULL (open-ended).",
                )

        result.append({
            "tier_sequence": tier.tier_sequence,
            "from_unit":     Decimal(str(tier.from_unit)),
            "to_unit":       None if tier.to_unit is None else Decimal(str(tier.to_unit)),
            "tier_amount":   tier.tier_amount,
        })

    return result


def _validate_range_tiers(
    tiers: list[RangeTierCreate],
) -> list[dict]:
    """
    Validate RangeBracket / RangeProgressive tiers and derive from_unit for each.

    Rules:
      - At least 2 tiers.
      - TierSequence gapless: 1, 2, ..., N.
      - Tier 1 from_unit is derived as 0.
      - Each tier i > 1: from_unit derived == previous tier's to_unit.
      - Each to_unit must be strictly greater than the previous to_unit.
      - Last tier: to_unit must be None.
      - All other tiers: to_unit must not be None.
      - tier_amount > 0 (enforced by RangeTierCreate validator).

    Boundary semantics (inclusive upper, exclusive upper for next tier):
      Tier 1: 0   <= qty <= to_unit_1
      Tier i: to_unit_{i-1} < qty <= to_unit_i
      Last:   qty > to_unit_{N-1}
    """
    if len(tiers) < 2:
        raise HTTPException(422, "Range tiers require at least 2 tiers.")

    seqs = [t.tier_sequence for t in tiers]
    if seqs != list(range(1, len(tiers) + 1)):
        raise HTTPException(422, "range_tiers: tier_sequence must be gapless starting at 1.")

    sorted_tiers = sorted(tiers, key=lambda t: t.tier_sequence)

    result: list[dict] = []
    for i, tier in enumerate(sorted_tiers):
        is_last = (i == len(sorted_tiers) - 1)

        from_unit = Decimal("0") if i == 0 else Decimal(str(sorted_tiers[i - 1].to_unit))

        if not is_last:
            if tier.to_unit is None:
                raise HTTPException(
                    422,
                    f"Tier {tier.tier_sequence}: only the last tier can have to_unit=NULL.",
                )
            if i > 0:
                prev_to = Decimal(str(sorted_tiers[i - 1].to_unit))
                if Decimal(str(tier.to_unit)) <= prev_to:
                    raise HTTPException(
                        422,
                        f"Tier {tier.tier_sequence}: to_unit must be strictly greater than "
                        f"previous tier's to_unit ({prev_to}).",
                    )
        else:
            if tier.to_unit is not None:
                raise HTTPException(
                    422,
                    "Range tiers: the last tier must have to_unit=NULL (open-ended).",
                )

        result.append({
            "tier_sequence": tier.tier_sequence,
            "from_unit":     from_unit,
            "to_unit":       None if tier.to_unit is None else Decimal(str(tier.to_unit)),
            "tier_amount":   tier.tier_amount,
        })

    return result


# ---------------------------------------------------------------------------
# M13c: Calculation helpers for tiered / block behaviors
# ---------------------------------------------------------------------------

async def _compute_ordinal_tier(
    quantity: Decimal,
    driver_id: int,
    company_id: int,
    as_of_date: date,
    rate_code: str,
    db: AsyncConnection,
) -> tuple[Decimal | None, bool, int | None, int | None]:
    """
    OrdinalTier: each successive unit gets the rate for its ordinal position.
    quantity must be a positive integer (validated at draft-line entry).

    Returns (calculated_amount, needs_manager_review, driver_rate_id, rate_type_id).
    Phase 3B: driver_rate_id and rate_type_id added for source snapshot.

    Example: tiers [(1,1,$50),(2,5,$40),(6+,$35)], qty=7 â†’
      1Ã—$50 + 4Ã—$40 + 2Ã—$35 = $50 + $160 + $70 = $280
    """
    rate_result = await db.execute(
        text("""
            SELECT dr.driverrateid, dr.ratetypeid
            FROM   payroll.driverrates  dr
            JOIN   payroll.ratetypes    rt ON rt.ratetypeid = dr.ratetypeid
            WHERE  dr.driverid         = :did
              AND  dr.companyid        = :cid
              AND  rt.ratecode         = :rcode
              AND  dr.status           IN ('Approved', 'Superseded')
              AND  dr.effectivefrom   <= :dt
              AND  (dr.effectiveto IS NULL OR dr.effectiveto >= :dt)
            ORDER BY dr.effectivefrom DESC
            LIMIT 1
        """),
        {"did": driver_id, "cid": company_id, "rcode": rate_code, "dt": as_of_date},
    )
    rate_row = rate_result.mappings().first()
    if rate_row is None:
        return (None, True, None, None)

    rid  = int(rate_row["driverrateid"])
    rtid = int(rate_row["ratetypeid"])

    tiers = await _load_tiers(rid, db)
    if not tiers:
        return (None, True, None, None)

    units = int(quantity)
    result = Decimal("0")
    for i in range(1, units + 1):
        tier = next(
            (
                t for t in tiers
                if Decimal(str(t.from_unit)) <= i
                and (t.to_unit is None or Decimal(str(t.to_unit)) >= i)
            ),
            None,
        )
        if tier is None:
            # Ordinal position not covered â€” defensive; should not happen with valid tiers
            return (None, True, None, None)
        result += Decimal(str(tier.tier_amount))

    return (result.quantize(Decimal("0.0001")), False, rid, rtid)


async def _compute_range_bracket(
    quantity: Decimal,
    driver_id: int,
    company_id: int,
    as_of_date: date,
    rate_code: str,
    db: AsyncConnection,
) -> tuple[Decimal | None, bool, int | None, int | None]:
    """
    RangeBracket: entire quantity gets one rate â€” whichever bracket contains qty.

    Returns (calculated_amount, needs_manager_review, driver_rate_id, rate_type_id).
    Phase 3B: driver_rate_id and rate_type_id added for source snapshot.

    Boundary semantics:
      Tier 1: 0   <= qty <= to_unit_1   (inclusive upper; qty=to_unit belongs to tier 1)
      Tier i: to_unit_{i-1} < qty <= to_unit_i
      Last:   qty > to_unit_{N-1}
    """
    rate_result = await db.execute(
        text("""
            SELECT dr.driverrateid, dr.ratetypeid
            FROM   payroll.driverrates  dr
            JOIN   payroll.ratetypes    rt ON rt.ratetypeid = dr.ratetypeid
            WHERE  dr.driverid         = :did
              AND  dr.companyid        = :cid
              AND  rt.ratecode         = :rcode
              AND  dr.status           IN ('Approved', 'Superseded')
              AND  dr.effectivefrom   <= :dt
              AND  (dr.effectiveto IS NULL OR dr.effectiveto >= :dt)
            ORDER BY dr.effectivefrom DESC
            LIMIT 1
        """),
        {"did": driver_id, "cid": company_id, "rcode": rate_code, "dt": as_of_date},
    )
    rate_row = rate_result.mappings().first()
    if rate_row is None:
        return (None, True, None, None)

    rid  = int(rate_row["driverrateid"])
    rtid = int(rate_row["ratetypeid"])

    tiers = await _load_tiers(rid, db)
    if not tiers:
        return (None, True, None, None)

    bracket = None
    for i, tier in enumerate(tiers):
        if i == 0:
            # First tier: from_unit (0) <= qty <= to_unit
            if tier.to_unit is None or quantity <= Decimal(str(tier.to_unit)):
                bracket = tier
                break
        else:
            prev_to = Decimal(str(tiers[i - 1].to_unit))
            if quantity > prev_to:
                if tier.to_unit is None or quantity <= Decimal(str(tier.to_unit)):
                    bracket = tier
                    break

    if bracket is None:
        return (None, True, None, None)

    result = (quantity * Decimal(str(bracket.tier_amount))).quantize(Decimal("0.0001"))
    return (result, False, rid, rtid)


async def _compute_range_progressive(
    quantity: Decimal,
    driver_id: int,
    company_id: int,
    as_of_date: date,
    rate_code: str,
    db: AsyncConnection,
) -> tuple[Decimal | None, bool, int | None, int | None]:
    """
    RangeProgressive: each slice of quantity is rated at the rate for that tier.

    Same tier structure as RangeBracket; different calculation (marginal not flat).

    Example: tiers [(0,10,$1),(10,20,$2),(20+,$3)], qty=25 â†’
      10Ã—$1 + 10Ã—$2 + 5Ã—$3 = $10 + $20 + $15 = $45
    """
    rate_result = await db.execute(
        text("""
            SELECT dr.driverrateid, dr.ratetypeid
            FROM   payroll.driverrates  dr
            JOIN   payroll.ratetypes    rt ON rt.ratetypeid = dr.ratetypeid
            WHERE  dr.driverid         = :did
              AND  dr.companyid        = :cid
              AND  rt.ratecode         = :rcode
              AND  dr.status           IN ('Approved', 'Superseded')
              AND  dr.effectivefrom   <= :dt
              AND  (dr.effectiveto IS NULL OR dr.effectiveto >= :dt)
            ORDER BY dr.effectivefrom DESC
            LIMIT 1
        """),
        {"did": driver_id, "cid": company_id, "rcode": rate_code, "dt": as_of_date},
    )
    rate_row = rate_result.mappings().first()
    if rate_row is None:
        return (None, True, None, None)

    rid  = int(rate_row["driverrateid"])
    rtid = int(rate_row["ratetypeid"])
    tiers = await _load_tiers(rid, db)
    if not tiers:
        return (None, True, None, None)

    result = Decimal("0")
    remaining = quantity

    for i, tier in enumerate(tiers):
        if remaining <= 0:
            break
        from_unit = Decimal(str(tier.from_unit))
        to_unit   = Decimal(str(tier.to_unit)) if tier.to_unit is not None else None

        # Capacity of this tier slice
        if to_unit is not None:
            tier_capacity = to_unit - from_unit
        else:
            tier_capacity = remaining  # open-ended last tier: consume all remaining

        units_in_tier = min(remaining, tier_capacity)
        result   += units_in_tier * Decimal(str(tier.tier_amount))
        remaining -= units_in_tier

    return (result.quantize(Decimal("0.0001")), False, rid, rtid)


async def _compute_block(
    quantity: Decimal,
    driver_id: int,
    company_id: int,
    as_of_date: date,
    rate_code: str,
    db: AsyncConnection,
) -> tuple[Decimal | None, bool, int | None, int | None]:
    """
    Block: flat dollar amount per complete block of units.

    number_of_blocks = qty / block_size, rounded per RoundingRule:
      Floor:         floor(raw_blocks)
      Ceiling:       ceil(raw_blocks)
      NearestHalfUp: floor(raw_blocks + 0.5)  â€” always rounds .5 up (no banker's rounding)

    A result of 0 blocks is valid (Floor rounding with qty < block_size) â€” not flagged
    for review.
    """
    rate_result = await db.execute(
        text("""
            SELECT dr.driverrateid, dr.ratetypeid, dr.amount, dr.blocksize, dr.roundingrule
            FROM   payroll.driverrates  dr
            JOIN   payroll.ratetypes    rt ON rt.ratetypeid = dr.ratetypeid
            WHERE  dr.driverid         = :did
              AND  dr.companyid        = :cid
              AND  rt.ratecode         = :rcode
              AND  dr.status           IN ('Approved', 'Superseded')
              AND  dr.effectivefrom   <= :dt
              AND  (dr.effectiveto IS NULL OR dr.effectiveto >= :dt)
            ORDER BY dr.effectivefrom DESC
            LIMIT 1
        """),
        {"did": driver_id, "cid": company_id, "rcode": rate_code, "dt": as_of_date},
    )
    rate_row = rate_result.mappings().first()
    if rate_row is None:
        return (None, True, None, None)

    rid           = int(rate_row["driverrateid"])
    rtid          = int(rate_row["ratetypeid"])
    amount        = Decimal(str(rate_row["amount"]))
    block_size    = Decimal(str(rate_row["blocksize"]))
    rounding_rule = rate_row["roundingrule"]

    raw_blocks = quantity / block_size
    if rounding_rule == "Floor":
        blocks = math.floor(raw_blocks)
    elif rounding_rule == "Ceiling":
        blocks = math.ceil(raw_blocks)
    else:  # NearestHalfUp: floor(x + 0.5) â€” never uses Python's banker's rounding
        blocks = math.floor(raw_blocks + Decimal("0.5"))

    result = (Decimal(str(blocks)) * amount).quantize(Decimal("0.0001"))
    return (result, False, rid, rtid)


# ---------------------------------------------------------------------------
# Audit helper for rate events
# ---------------------------------------------------------------------------

_RATE_AUDIT_REASONS: dict[str, str] = {
    "RATE_CREATED":    "Driver rate created",
    "RATE_UPDATED":    "Driver rate updated",
    "RATE_APPROVED":   "Driver rate approved",
    "RATE_SUPERSEDED": "Driver rate superseded by newer approved rate",
    "RATE_VOIDED":     "Driver rate voided",
}


async def _write_rate_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    branch_id: int,
    user_id: int,
    rate_id: int,
    action_code: str,
    old_value: dict | None = None,
    new_value: dict | None = None,
) -> None:
    """
    Insert one row into audit.AuditLog for a rate event.

    Extracted as a module-level function so tests can monkeypatch it to verify
    that all preceding writes roll back when this raises.
    """
    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid,
                 oldvaluejson, newvaluejson, reason, sourcetype)
            VALUES
                (:company_id, :branch_id, :actor_id, :action_code,
                 'payroll', 'DriverRates', :entity_id,
                 :old_val, :new_val, :reason, 'Application')
        """),
        {
            "company_id":  company_id,
            "branch_id":   branch_id,
            "actor_id":    user_id,
            "action_code": action_code,
            "entity_id":   str(rate_id),
            "old_val":     json.dumps(old_value)  if old_value  is not None else None,
            "new_val":     json.dumps(new_value)  if new_value  is not None else None,
            "reason":      _RATE_AUDIT_REASONS.get(action_code, action_code),
        },
    )


# ---------------------------------------------------------------------------
# List rate types (reference catalog â€” no branch scope needed)
# ---------------------------------------------------------------------------

async def get_rate_types(
    company_id: int,
    db: AsyncConnection,
    *,
    active_only: bool = True,
) -> list[RateTypeSummary]:
    """
    Return rate types visible to the requesting company.

    Phase 4C structural ownership rule:
      SHOW if:
        - rt.companyid IS NULL          -- system/global type (usable by all companies)
        - rt.companyid = company_id     -- own company's custom type

      HIDE everything else:
        - rt.companyid = other company  -- foreign-owned custom type
        - rt.isactive = FALSE           -- inactive (when active_only=True)

    This replaces the Phase 4B.4 three-flag PayItemRateTypeMap analysis.
    The structural column makes the check O(1) and cannot be contaminated
    by direct-DB PayItemRateTypeMap inserts.
    """
    params: dict[str, Any] = {"cid": company_id}
    active_filter = "AND rt.isactive = TRUE" if active_only else ""

    result = await db.execute(
        text(f"""
            SELECT rt.ratetypeid, rt.ratecode, rt.ratename, rt.unitname, rt.isactive
            FROM   payroll.ratetypes rt
            WHERE
              (TRUE {active_filter})
              -- Phase 4C: structural ownership -- system OR own company
              AND (rt.companyid IS NULL OR rt.companyid = :cid)
            ORDER BY rt.ratename
        """),
        params,
    )
    return [
        RateTypeSummary(
            rate_type_id=r["ratetypeid"],
            rate_code=r["ratecode"],
            rate_name=r["ratename"],
            unit_name=r["unitname"],
            is_active=r["isactive"],
        )
        for r in result.mappings().all()
    ]


# ---------------------------------------------------------------------------
# List driver rates
# ---------------------------------------------------------------------------

async def get_rates(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    branch_id: int | None = None,
    driver_id: int | None = None,
    rate_status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[DriverRateSummary]:
    # OwnDriverDataOnly: detect scope early so we can pass the correct branch_id
    # to the permission check.  ODA users have permission scoped to a specific branch
    # (fn_UserHasPermission returns FALSE with NULL branch_id for ODA users).
    # Resolving own_driver_id first also lets us force the driver_id filter.
    own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)

    if own_driver_id is not None:
        # ODA user: look up own driver's branch for the permission check.
        oda_drv_result = await db.execute(
            text("SELECT branchid FROM core.drivers WHERE driverid = :did AND companyid = :cid"),
            {"did": own_driver_id, "cid": company_id},
        )
        oda_drv_row = oda_drv_result.mappings().first()
        perm_branch_id: int | None = oda_drv_row["branchid"] if oda_drv_row else branch_id

        # Permission gate using ODA driver's branch (so fn_UserHasPermission resolves correctly).
        await _check_any_permission(
            company_id, user_id, perm_branch_id,
            ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
        )

        # ODA enforcement: caller may only list their own driver's rates.
        if driver_id is not None and driver_id != own_driver_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="OwnDriverDataOnly: you may only list your own driver's rates.",
            )
        driver_id = own_driver_id  # force filter to own driver
    else:
        # Non-ODA: standard permission gate (branch_id=None is acceptable for AllCompanyBranches).
        await _check_any_permission(
            company_id, user_id, branch_id,
            ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
        )

    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    conditions: list[str] = ["dr.companyid = :company_id"]
    params: dict[str, Any] = {"company_id": company_id}

    # Branch scope
    if branch_id is not None:
        if not can_see_all and branch_id not in branch_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to the requested branch.",
            )
        conditions.append("dr.branchid = :branch_id")
        params["branch_id"] = branch_id
    elif not can_see_all:
        if not branch_ids:
            return []
        in_clause, in_params = _build_in_clause(branch_ids, "rb")
        conditions.append(f"dr.branchid IN ({in_clause})")
        params.update(in_params)

    if driver_id is not None:
        conditions.append("dr.driverid = :driver_id")
        params["driver_id"] = driver_id

    if rate_status is not None:
        conditions.append("dr.status = :rate_status")
        params["rate_status"] = rate_status

    where = " AND ".join(conditions)
    params["limit"] = limit
    params["offset"] = offset

    result = await db.execute(
        text(
            f"{_RATE_SELECT} "
            f"WHERE {where} "
            f"ORDER BY e.fullname, rt.ratename, dr.effectivefrom DESC "
            f"LIMIT :limit OFFSET :offset"
        ),
        params,
    )
    return [_rate_row_to_summary(r) for r in result.mappings().all()]


# ---------------------------------------------------------------------------
# Get single rate
# ---------------------------------------------------------------------------

async def get_rate_by_id(
    rate_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> DriverRateSummary:
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    rate = await _get_rate_with_tiers(rate_id, company_id, db)
    if not can_see_all and rate.branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this rate's branch.",
        )
    # Permission gate: reading a rate requires payrates.view or payrates.edit (or admin fallbacks)
    await _check_any_permission(
        company_id, user_id, rate.branch_id,
        ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
    )
    # OwnDriverDataOnly: caller may only read their own driver's rates
    await _check_own_driver_only(company_id, user_id, rate.driver_id, db)
    return rate


# ---------------------------------------------------------------------------
# Create rate
# ---------------------------------------------------------------------------

async def create_rate(
    company_id: int,
    user_id: int,
    data: DriverRateCreate,
    db: AsyncConnection,
) -> DriverRateSummary:
    """
    Create a new driver rate in PendingApproval status.

    Guards:
      - Driver must exist in this company.
      - User must have access to the driver's branch.
      - rate_type_id must exist and be active.
    """
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    # Resolve driver â†’ branch
    drv_result = await db.execute(
        text("SELECT driverid, branchid FROM core.drivers WHERE driverid = :did AND companyid = :cid"),
        {"did": data.driver_id, "cid": company_id},
    )
    drv_row = drv_result.mappings().first()
    if drv_row is None:
        raise HTTPException(status_code=422, detail="driver_id does not exist in this company.")

    driver_branch_id: int = drv_row["branchid"]
    if not can_see_all and driver_branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this driver's branch.",
        )

    # Permission gate: creating a rate requires payrates.edit (or admin fallbacks)
    await _check_any_permission(
        company_id, user_id, driver_branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    # OwnDriverDataOnly: caller may only create rates for their own driver
    await _check_own_driver_only(company_id, user_id, data.driver_id, db)

    # Validate rate type: existence, activity, and company scope (closes P0 cross-company leak).
    await _assert_rate_type_allowed_for_company(db, company_id, data.rate_type_id)

    # CP-2D2: Status-payment RateTypes are branch-scoped; reject if this branch has no
    # active StatusRateColumns row for this RateType.
    await _assert_status_rate_type_for_branch(data.rate_type_id, company_id, driver_branch_id, db)

    # Fix 8: When this rate type is mapped to pay items (via PayItemRateTypeMap),
    # validate that at least one of those pay items is active for this branch.
    # Mirrors the activation logic in _validate_line_type:
    #   1. Explicit BranchPayItemConfig row â†’ use isactive from that row.
    #   2. No config row â†’ fall back to PayItems.IsDefaultBranchActive.
    # If the rate type has no PayItemRateTypeMap entries (e.g. Fixed-behavior OVERNIGHT),
    # the branch check is skipped â€” the rate type itself is valid, just not PerUnit-mapped.
    branch_rt_result = await db.execute(
        text("""
            SELECT
                pi.payitemid,
                pi.isdefaultbranchactive,
                bpic.isactive AS cfg_isactive
            FROM payroll.payitems pi
            JOIN payroll.payitemratetypemap pirm ON pirm.payitemid = pi.payitemid
              AND pirm.ratetypeid = :rtid AND pirm.status = 'Active'
            LEFT JOIN payroll.branchpayitemconfig bpic ON bpic.payitemid = pi.payitemid
              AND bpic.companyid = :cid AND bpic.branchid = :bid
              AND (bpic.effectiveto IS NULL OR bpic.effectiveto >= :effective_from)
            WHERE pi.status != 'Retired'
              AND pi.requiresrate = TRUE
              AND (pi.companyid IS NULL OR pi.companyid = :cid)
            ORDER BY bpic.effectivefrom DESC NULLS LAST
            LIMIT 1
        """),
        {
            "rtid": data.rate_type_id,
            "cid": company_id,
            "bid": driver_branch_id,
            "effective_from": data.effective_from,
        },
    )
    branch_rt_row = branch_rt_result.mappings().first()
    if branch_rt_row is not None:
        # Rate type IS mapped to a pay item â€” check branch activation
        if branch_rt_row["cfg_isactive"] is not None:
            rt_is_active = bool(branch_rt_row["cfg_isactive"])
        else:
            rt_is_active = bool(branch_rt_row["isdefaultbranchactive"])
        if not rt_is_active:
            raise HTTPException(
                status_code=422,
                detail="This rate type is not active for this driver's branch.",
            )
    # branch_rt_row is None: rate type has no PayItemRateTypeMap entry (e.g. Fixed/OVERNIGHT);
    # it is not restricted to branch pay item config â€” allow through (company scope already
    # validated above by _assert_rate_type_allowed_for_company).

    # M13c: resolve rate behavior and validate tier / block inputs BEFORE inserting.
    rate_behavior = await _resolve_rate_behavior(data.rate_type_id, company_id, db)

    # Validate and prepare tier / block data.
    prepared_tiers: list[dict] = []
    block_size:    Decimal | None = None
    rounding_rule: str | None = None

    if rate_behavior == "OrdinalTier":
        if not data.ordinal_tiers:
            raise HTTPException(422, "OrdinalTier rates require ordinal_tiers.")
        if data.range_tiers or data.block_size or data.rounding_rule:
            raise HTTPException(422, "OrdinalTier rates must not include range_tiers or block params.")
        prepared_tiers = _validate_ordinal_tiers(data.ordinal_tiers)

    elif rate_behavior in _RANGE_BEHAVIORS:
        if not data.range_tiers:
            raise HTTPException(422, f"{rate_behavior} rates require range_tiers.")
        if data.ordinal_tiers or data.block_size or data.rounding_rule:
            raise HTTPException(422, f"{rate_behavior} rates must not include ordinal_tiers or block params.")
        prepared_tiers = _validate_range_tiers(data.range_tiers)

    elif rate_behavior == "Block":
        if data.block_size is None or data.rounding_rule is None:
            raise HTTPException(422, "Block rates require both block_size and rounding_rule.")
        if data.block_size <= 0:
            raise HTTPException(422, "block_size must be > 0.")
        if data.ordinal_tiers or data.range_tiers:
            raise HTTPException(422, "Block rates must not include tier lists.")
        block_size = data.block_size
        rounding_rule = data.rounding_rule

    else:  # PerUnit, EnteredAmount, Fixed, None
        if data.ordinal_tiers or data.range_tiers:
            raise HTTPException(
                422,
                f"Tiers are not applicable to '{rate_behavior}' items. "
                "Remove ordinal_tiers or range_tiers from the request.",
            )
        if data.block_size or data.rounding_rule:
            raise HTTPException(
                422,
                f"block_size / rounding_rule are not applicable to '{rate_behavior}' items.",
            )

    insert_result = await db.execute(
        text("""
            INSERT INTO payroll.driverrates
                (companyid, branchid, driverid, ratetypeid, amount,
                 effectivefrom, effectiveto, status, createdbyuserid, notes,
                 blocksize, roundingrule)
            VALUES
                (:company_id, :branch_id, :driver_id, :rate_type_id, :amount,
                 :effective_from, :effective_to, 'PendingApproval', :created_by, :notes,
                 :block_size, :rounding_rule)
            RETURNING driverrateid
        """),
        {
            "company_id":     company_id,
            "branch_id":      driver_branch_id,
            "driver_id":      data.driver_id,
            "rate_type_id":   data.rate_type_id,
            "amount":         data.amount,
            "effective_from": data.effective_from,
            "effective_to":   data.effective_to,
            "created_by":     user_id,
            "notes":          data.notes,
            "block_size":     block_size,
            "rounding_rule":  rounding_rule,
        },
    )
    rate_id: int = insert_result.scalar_one()

    # Insert tier rows (inside same transaction).
    if prepared_tiers:
        await _insert_tiers(rate_id, prepared_tiers, db)

    # Audit write â€” inside the same transaction; failure rolls back everything.
    await _write_rate_audit(
        db,
        company_id=company_id,
        branch_id=driver_branch_id,
        user_id=user_id,
        rate_id=rate_id,
        action_code="RATE_CREATED",
        new_value={
            "driver_id":      data.driver_id,
            "rate_type_id":   data.rate_type_id,
            "rate_behavior":  rate_behavior,
            "amount":         str(data.amount),
            "effective_from": str(data.effective_from),
            "effective_to":   str(data.effective_to) if data.effective_to else None,
            "status":         "PendingApproval",
            "tier_count":     len(prepared_tiers) if prepared_tiers else None,
            "block_size":     str(block_size) if block_size else None,
            "rounding_rule":  rounding_rule,
        },
    )

    return await _get_rate_with_tiers(rate_id, company_id, db)


# ---------------------------------------------------------------------------
# Update rate (PendingApproval only)
# ---------------------------------------------------------------------------

async def update_rate(
    rate_id: int,
    company_id: int,
    user_id: int,
    data: DriverRateUpdate,
    db: AsyncConnection,
) -> DriverRateSummary:
    """
    Partially update a rate.  Only PendingApproval rates may be edited;
    Approved/Superseded/Voided rates are immutable.
    """
    rate = await get_rate_by_id(rate_id, company_id, user_id, db)

    if rate.status != "PendingApproval":
        raise HTTPException(
            status_code=422,
            detail=(
                f"Only PendingApproval rates can be edited "
                f"(current status: '{rate.status}')."
            ),
        )

    # Permission gate: editing a rate requires payrates.edit (or admin fallbacks)
    await _check_any_permission(
        company_id, user_id, rate.branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    # OwnDriverDataOnly: caller may only edit their own driver's rates
    await _check_own_driver_only(company_id, user_id, rate.driver_id, db)

    # Defensive cross-company scope guard: reject contaminated rows that reference
    # another company's RateType (e.g. created before P0 was closed).
    await _assert_rate_type_allowed_for_company(db, company_id, rate.rate_type_id)

    # CP-2D2: Status-payment RateTypes are branch-scoped; reject if this branch has no
    # active StatusRateColumns row for this RateType.
    await _assert_status_rate_type_for_branch(rate.rate_type_id, company_id, rate.branch_id, db)

    # Validate date range if either date is being changed
    new_from = data.effective_from if data.effective_from is not None else rate.effective_from
    new_to   = data.effective_to   if data.effective_to   is not None else rate.effective_to
    if new_to is not None and new_to <= new_from:
        raise HTTPException(
            status_code=422,
            detail="effective_to must be strictly after effective_from.",
        )

    # M13c: resolve behavior to validate any tier / block updates.
    rate_behavior = await _resolve_rate_behavior(rate.rate_type_id, company_id, db)

    fields: dict[str, Any] = {}
    if data.amount         is not None: fields["amount"]        = data.amount
    if data.effective_from is not None: fields["effectivefrom"] = data.effective_from
    if data.effective_to   is not None: fields["effectiveto"]   = data.effective_to
    if data.notes          is not None: fields["notes"]         = data.notes

    # M13c: block metadata updates
    if data.block_size is not None or data.rounding_rule is not None:
        if rate_behavior != "Block":
            raise HTTPException(
                422,
                f"block_size / rounding_rule are not applicable to '{rate_behavior}' items.",
            )
        if data.block_size is not None:
            if data.block_size <= 0:
                raise HTTPException(422, "block_size must be > 0.")
            fields["blocksize"] = data.block_size
        if data.rounding_rule is not None:
            fields["roundingrule"] = data.rounding_rule

    # Phase 12C â€” status-predicated scalar UPDATE.
    # Do NOT rely solely on the earlier status check: a concurrent approve_rate can
    # transition the row from PendingApproval â†’ Approved between that check and this
    # UPDATE.  Adding AND status='PendingApproval' to the WHERE clause makes the UPDATE
    # a no-op (0 rows) if the row was concurrently approved, and RETURNING lets us
    # detect that atomically.  This prevents updating the amount/dates/block fields of
    # an already-Approved rate through a stale pending path.
    fields_updated = False
    if fields:
        set_clause = ", ".join(f"{col} = :{col}" for col in fields)
        upd_result = await db.execute(
            text(f"""
                UPDATE payroll.driverrates
                SET    {set_clause}
                WHERE  driverrateid = :rid
                  AND  companyid    = :company_id
                  AND  status       = 'PendingApproval'
                RETURNING driverrateid
            """),
            {**fields, "rid": rate_id, "company_id": company_id},
        )
        if upd_result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=409,
                detail="Rate changed while editing. Refresh and try again.",
            )
        fields_updated = True

    # M13c: tier replacement (replace-all if provided).
    # Phase 12C â€” tier lock: before replacing tiers, guarantee the parent row is still
    # PendingApproval.  If a scalar UPDATE ran above, its RETURNING already confirmed
    # the row status at UPDATE time.  If only tiers are being replaced (no scalar
    # fields), acquire a row-level lock with a status predicate so the DELETE/INSERT
    # cannot run against a concurrently-approved rate.
    tiers_updated = False
    if data.ordinal_tiers is not None or data.range_tiers is not None:
        if data.ordinal_tiers is not None and data.range_tiers is not None:
            raise HTTPException(422, "Provide either ordinal_tiers or range_tiers â€” not both.")
        if rate_behavior == "OrdinalTier":
            if data.range_tiers is not None:
                raise HTTPException(422, "OrdinalTier rates require ordinal_tiers, not range_tiers.")
            prepared = _validate_ordinal_tiers(data.ordinal_tiers)  # type: ignore[arg-type]
        elif rate_behavior in _RANGE_BEHAVIORS:
            if data.ordinal_tiers is not None:
                raise HTTPException(422, f"{rate_behavior} rates require range_tiers, not ordinal_tiers.")
            prepared = _validate_range_tiers(data.range_tiers)  # type: ignore[arg-type]
        else:
            raise HTTPException(
                422,
                f"Tiers are not applicable to '{rate_behavior}' items. Remove tier fields.",
            )
        if not fields_updated:
            # No scalar UPDATE confirmed the row is PendingApproval â€” lock it now.
            lock_row = await db.execute(
                text("""
                    SELECT driverrateid FROM payroll.driverrates
                    WHERE  driverrateid = :rid
                      AND  companyid    = :company_id
                      AND  status       = 'PendingApproval'
                    FOR UPDATE
                """),
                {"rid": rate_id, "company_id": company_id},
            )
            if lock_row.scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=409,
                    detail="Rate changed while editing. Refresh and try again.",
                )
        await _insert_tiers(rate_id, prepared, db)
        tiers_updated = True

    # Audit write (only when something actually changed) â€” inside the transaction.
    if fields or tiers_updated:
        await _write_rate_audit(
            db,
            company_id=company_id,
            branch_id=rate.branch_id,
            user_id=user_id,
            rate_id=rate_id,
            action_code="RATE_UPDATED",
            old_value={
                "amount":         str(rate.amount),
                "effective_from": str(rate.effective_from),
                "effective_to":   str(rate.effective_to) if rate.effective_to else None,
                "notes":          rate.notes,
            },
            new_value={
                **{k: str(v) if v is not None else None
                   for k, v in data.model_dump(exclude_none=True).items()
                   if k not in ("ordinal_tiers", "range_tiers")},
                "tiers_replaced": tiers_updated,
            },
        )

    return await _get_rate_with_tiers(rate_id, company_id, db)


# ---------------------------------------------------------------------------
# Shared DriverRate lifecycle helpers
# ---------------------------------------------------------------------------
# These three helpers centralise the approval/supersession invariants so that
# approve_rate, batch_save_rates (via approve_rate), and copy_driver_rates all
# enforce exactly the same rules.
#
# Invariants enforced together:
#   1. No future Approved rate for the same company/driver/rate_type may start
#      on or after the new effective_from (conflict guard).
#   2. Any existing Approved rate that started BEFORE effective_from is
#      superseded with EffectiveTo = effective_from - 1 day (supersession).
#   3. All supersession and void mutations write audit rows inside the same
#      transaction.
# ---------------------------------------------------------------------------

async def _check_no_future_approved_conflict(
    company_id: int,
    driver_id: int,
    rate_type_id: int,
    effective_from: date,
    db: AsyncConnection,
    *,
    exclude_rate_id: int | None = None,
) -> None:
    """
    Raise HTTP 422 if an Approved rate for this company/driver/rate_type starts
    on or after ``effective_from``.

    Superseding such a rate would set its EffectiveTo BEFORE its own
    EffectiveFrom (invalid dates) and would leave two Approved rows violating
    the partial unique index ``ux_DriverRates_Driver_Type_Approved``.

    ``exclude_rate_id`` â€” when approving an existing PendingApproval row that
    already lives in the DB, pass its ID so the SELECT does not match itself.
    """
    params: dict[str, Any] = {
        "company_id":   company_id,
        "driver_id":    driver_id,
        "rate_type_id": rate_type_id,
        "new_from":     effective_from,
    }
    exclude_clause = ""
    if exclude_rate_id is not None:
        exclude_clause = "AND driverrateid != :exclude_id"
        params["exclude_id"] = exclude_rate_id

    conflict = await db.execute(
        text(f"""
            SELECT driverrateid, effectivefrom
            FROM   payroll.driverrates
            WHERE  companyid    = :company_id
              AND  driverid     = :driver_id
              AND  ratetypeid   = :rate_type_id
              AND  status       = 'Approved'
              AND  effectivefrom >= CAST(:new_from AS date)
              {exclude_clause}
            LIMIT 1
        """),
        params,
    )
    row = conflict.mappings().first()
    if row is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot apply this rate â€” an Approved rate already exists "
                f"starting on or after {effective_from} "
                f"(rate ID {row['driverrateid']}, effective from {row['effectivefrom']}). "
                f"Void that rate first, or choose a later effective date."
            ),
        )


async def _supersede_current_approved_rates(
    company_id: int,
    driver_id: int,
    rate_type_id: int,
    effective_from: date,
    changed_by_user_id: int,
    branch_id: int,
    db: AsyncConnection,
    *,
    exclude_rate_id: int | None = None,
) -> list[int]:
    """
    Supersede any Approved rate whose ``EffectiveFrom < effective_from``.

    The closing rule (prevents date-range gaps or overlaps):
      â€¢ EffectiveTo IS NULL (open-ended)      â†’ close at effective_from - 1 day.
      â€¢ EffectiveTo >= effective_from (overlap) â†’ trim to effective_from - 1 day.
      â€¢ EffectiveTo < effective_from (already closed before new range) â†’ unchanged.

    Only rates with EffectiveFrom strictly BEFORE effective_from are touched;
    future Approved rates (EffectiveFrom >= effective_from) are never modified
    here â€” the caller must have run _check_no_future_approved_conflict first.

    Returns a list of superseded DriverRateIDs.
    Writes RATE_SUPERSEDED audit for each inside the same transaction.
    """
    params: dict[str, Any] = {
        "new_from":     effective_from,
        "company_id":   company_id,
        "driver_id":    driver_id,
        "rate_type_id": rate_type_id,
    }
    exclude_clause = ""
    if exclude_rate_id is not None:
        exclude_clause = "AND driverrateid != :exclude_id"
        params["exclude_id"] = exclude_rate_id

    supersede_result = await db.execute(
        text(f"""
            UPDATE payroll.driverrates
            SET    status      = 'Superseded',
                   effectiveto = CASE
                       WHEN effectiveto IS NULL
                            OR effectiveto >= CAST(:new_from AS date)
                       THEN CAST(:new_from AS date) - 1
                       ELSE effectiveto
                   END
            WHERE  companyid    = :company_id
              AND  driverid     = :driver_id
              AND  ratetypeid   = :rate_type_id
              AND  status       = 'Approved'
              AND  effectivefrom < CAST(:new_from AS date)
              {exclude_clause}
            RETURNING driverrateid
        """),
        params,
    )
    superseded_ids: list[int] = [row[0] for row in supersede_result.fetchall()]

    for sid in superseded_ids:
        await _write_rate_audit(
            db,
            company_id=company_id,
            branch_id=branch_id,
            user_id=changed_by_user_id,
            rate_id=sid,
            action_code="RATE_SUPERSEDED",
            old_value={"status": "Approved"},
            new_value={
                "status":                        "Superseded",
                "superseded_at_effective_from":  str(effective_from),
            },
        )

    return superseded_ids


async def _void_pending_rates_for_rate_type(
    company_id: int,
    driver_id: int,
    rate_type_id: int,
    changed_by_user_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> list[int]:
    """
    Void all PendingApproval rows for this company/driver/rate_type.

    Called *after* all validations pass so that a pre-validation failure leaves
    no partial state.

    Returns a list of voided DriverRateIDs.
    Writes RATE_VOIDED audit for each inside the same transaction.
    """
    void_result = await db.execute(
        text("""
            UPDATE payroll.driverrates
            SET    status = 'Voided'
            WHERE  companyid  = :cid
              AND  driverid   = :did
              AND  ratetypeid = :rtid
              AND  status     = 'PendingApproval'
            RETURNING driverrateid
        """),
        {"cid": company_id, "did": driver_id, "rtid": rate_type_id},
    )
    voided_ids: list[int] = [row[0] for row in void_result.fetchall()]

    for vid in voided_ids:
        await _write_rate_audit(
            db,
            company_id=company_id,
            branch_id=branch_id,
            user_id=changed_by_user_id,
            rate_id=vid,
            action_code="RATE_VOIDED",
            old_value={"status": "PendingApproval"},
            new_value={"status": "Voided"},
        )

    return voided_ids


# ---------------------------------------------------------------------------
# Approve rate (PendingApproval â†’ Approved; supersedes prior Approved rate)
# ---------------------------------------------------------------------------

async def approve_rate(
    rate_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> DriverRateSummary:
    """
    Approve a PendingApproval rate.  All steps run inside the single
    engine.begin() transaction; any failure rolls back everything.

    Operation order is critical:
      Step 1 â€” Access check + friendly status guard (read-only).
      Step 1.5 â€” Conflict guard: reject if an existing Approved rate starts on or after
               this rate's effective_from.  Superseding such a rate would set its
               effective_to BEFORE its own effective_from (invalid dates) and would
               leave two Approved rows violating the partial unique index.
               The caller must void the conflicting rate first.
      Step 2 â€” Supersede any prior Approved rate(s) that started BEFORE this rate.
               MUST happen BEFORE Step 3 so the partial unique index
               (ux_DriverRates_Driver_Type_Approved) is clear when we approve.
               Trims effective_to to (new_from - 1) whenever the old rate's effective_to
               is NULL or overlaps with the new rate's range â€” ensuring no date gaps or
               overlaps are left in the historical record.
               Only rates with effectivefrom < new_from are touched.
      Step 3 â€” Atomic claim: UPDATE WHERE status='PendingApproval' RETURNING.
               If 0 rows, a concurrent request won the race â†’ 422.
               Wrapped in try/except SAIntegrityError as a belt-and-suspenders guard.
      Step 4 â€” Audit log for each superseded rate.
      Step 5 â€” Audit log for this rate's approval.
      Step 6 â€” Return refreshed DriverRateSummary.
    """
    # Step 1 â€” access check + friendly guard
    # Use get_rate_by_id (public â€” includes branch scope check) for the access check,
    # then use the lightweight _get_rate_by_id for subsequent internal operations to
    # avoid reloading tiers unnecessarily during the approval flow.
    rate = await get_rate_by_id(rate_id, company_id, user_id, db)
    if rate.status != "PendingApproval":
        raise HTTPException(
            status_code=422,
            detail=(
                f"Only PendingApproval rates can be approved "
                f"(current status: '{rate.status}')."
            ),
        )

    # Step 1.3 â€” permission gate: approving a rate requires payrates.edit (or admin fallbacks)
    await _check_any_permission(
        company_id, user_id, rate.branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    # Step 1.34 â€” OwnDriverDataOnly: caller may only approve their own driver's rates
    await _check_own_driver_only(company_id, user_id, rate.driver_id, db)

    # Step 1.36 â€” Defensive cross-company scope guard: reject contaminated rows
    # (e.g. created before P0 was closed) that reference another company's RateType.
    await _assert_rate_type_allowed_for_company(db, company_id, rate.rate_type_id)

    # Step 1.37 â€” CP-2D2: status-payment RateTypes are branch-scoped. Reject approval of a
    # contaminated pending rate that references a status RateType not active for this branch.
    await _assert_status_rate_type_for_branch(rate.rate_type_id, company_id, rate.branch_id, db)

    # Step 1.4 â€” M13c: belt-and-suspenders tier-existence check.
    # The create/update paths already enforce this, but a defensive check here
    # prevents approval of a tiered rate with no tiers (e.g. if tiers were deleted
    # directly from the DB or the rate was created before M13c was deployed).
    rate_behavior = await _resolve_rate_behavior(rate.rate_type_id, company_id, db)
    if rate_behavior in _TIERED_BEHAVIORS:
        tiers = await _load_tiers(rate_id, db)
        if not tiers:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Cannot approve a {rate_behavior} rate with no tiers defined. "
                    "Update the rate to add tiers first."
                ),
            )
    if rate_behavior == "Block":
        if rate.block_size is None:
            raise HTTPException(
                status_code=422,
                detail="Cannot approve a Block rate without block_size. Update the rate first.",
            )

    # Phase 12 / 12B â€” Finalization atomicity advisory lock.
    # Acquire the same transaction-level advisory lock that finalize_period holds
    # during its refreshâ†’claimâ†’insert window.  This serialises rate approval against
    # ongoing finalization for the same company+branch so that:
    #   â€¢ If finalization is in progress: this approval waits until finalize commits.
    #   â€¢ If no finalization is in progress: lock is acquired immediately.
    # After this lock is released (on commit/rollback), any subsequent finalization
    # for this branch will see the approved/superseded rate from the start of its
    # refresh step, ensuring finalamount and SourceSnapshot agree.
    # See the Phase 12 comment in finalize_period for the full race description.
    #
    # Phase 12B fix: _check_not_in_finalized_period is intentionally moved to AFTER
    # the advisory lock.  Running it before the lock creates a TOCTOU window: the
    # guard reads period status (Approved), this request waits for the lock while
    # finalization claims the period (Locked), and when the lock is finally acquired
    # the guard decision is already stale â€” the approval would proceed against a now-
    # finalized period.  By checking under the lock the guard always sees a consistent
    # view of period status.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:cid, :bid)"),
        {"cid": company_id, "bid": rate.branch_id},
    )

    # Step 1.35 â€” Backdating guard (re-run under lock for TOCTOU safety).
    # Block approval when effective_from falls inside a Locked or Archived period.
    # Must run AFTER the advisory lock is held so the period-status read is fresh.
    await _check_not_in_finalized_period(company_id, rate.branch_id, rate.effective_from, db)

    # Step 1.5 â€” conflict guard (shared helper).
    # Rejects if any Approved rate starts on or after this rate's effective_from.
    # Superseding such a rate would produce EffectiveTo < EffectiveFrom (invalid)
    # and would momentarily leave two Approved rows violating the partial unique index.
    # exclude_rate_id=rate_id: exclude this PendingApproval row from the check
    # (it is in the DB but not yet Approved, so the STATUS filter already excludes
    # it â€” the exclude_rate_id is a belt-and-suspenders guard against edge cases).
    await _check_no_future_approved_conflict(
        company_id, rate.driver_id, rate.rate_type_id, rate.effective_from, db,
        exclude_rate_id=rate_id,
    )

    # Step 2 â€” supersede prior Approved rate(s) FIRST via shared helper.
    # The helper only touches rates with EffectiveFrom < effective_from.
    # Writes RATE_SUPERSEDED audit inside the same transaction.
    await _supersede_current_approved_rates(
        company_id, rate.driver_id, rate.rate_type_id, rate.effective_from,
        user_id, rate.branch_id, db,
        exclude_rate_id=rate_id,
    )

    # Step 3 â€” atomic claim (AFTER supersede so the unique index is clear).
    # UPDATE WHERE status='PendingApproval' acquires a row lock and transitions the
    # rate in one statement.  A concurrent request issuing the same UPDATE gets 0
    # rows (the row is already Approved) and is safely rejected below.
    # SAIntegrityError is caught as a belt-and-suspenders guard for the rare race
    # where two concurrent approvals of different rates for the same driver+type
    # slip past the supersede gate simultaneously.
    try:
        claimed = await db.execute(
            text("""
                UPDATE payroll.driverrates
                SET    status           = 'Approved',
                       approvedbyuserid = :approver,
                       approvedatutc    = NOW()
                WHERE  driverrateid = :rate_id
                  AND  companyid    = :company_id
                  AND  status       = 'PendingApproval'
                RETURNING driverrateid
            """),
            {"approver": user_id, "rate_id": rate_id, "company_id": company_id},
        )
    except SAIntegrityError:
        raise HTTPException(
            status_code=422,
            detail=(
                "Rate could not be approved â€” another rate was approved concurrently. "
                "Please refresh and try again."
            ),
        )
    if claimed.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Rate could not be approved â€” its status may have changed concurrently."
            ),
        )

    # Step 4 â€” supersede audit already written by _supersede_current_approved_rates.

    # Step 5 â€” audit this approval (failure here rolls back steps 2+3+4)
    await _write_rate_audit(
        db,
        company_id=company_id,
        branch_id=rate.branch_id,
        user_id=user_id,
        rate_id=rate_id,
        action_code="RATE_APPROVED",
        old_value={"status": "PendingApproval"},
        new_value={
            "status":         "Approved",
            "amount":         str(rate.amount),
            "effective_from": str(rate.effective_from),
            "effective_to":   str(rate.effective_to) if rate.effective_to else None,
        },
    )

    # Step 6
    return await _get_rate_with_tiers(rate_id, company_id, db)


# ---------------------------------------------------------------------------
# Void rate (PendingApproval or Approved â†’ Voided)
# ---------------------------------------------------------------------------

async def void_rate(
    rate_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> None:
    """
    Void a rate.

    PendingApproval, Approved, and Superseded rates can all be voided.
    Superseded rates represent historical closed periods; an admin may need
    to void one to correct a data-entry mistake.  Once voided the row is
    excluded from the effective-date uniqueness constraint so the date range
    can be reused.

    Already-Voided rates cannot be voided again.
    """
    rate = await get_rate_by_id(rate_id, company_id, user_id, db)

    if rate.status == "Voided":
        raise HTTPException(
            status_code=422,
            detail="Rate is already Voided.",
        )

    # Permission gate: voiding a rate requires payrates.edit (or admin fallbacks)
    await _check_any_permission(
        company_id, user_id, rate.branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    # OwnDriverDataOnly: caller may only void their own driver's rates
    await _check_own_driver_only(company_id, user_id, rate.driver_id, db)

    # Phase 12B â€” advisory lock for Approved / Superseded void path.
    #
    # WHY: voiding an Approved or Superseded DriverRate changes the set of rates
    # visible to finalization's LATERAL rate_sub query.  If finalize_period has
    # already run _refresh_draft_calculations (step 1.6) using this rate and is
    # about to INSERT final lines (step 3), a concurrent void would make the LATERAL
    # return NULL for driverrateid â€” the finalamount would be computed from a rate
    # that no longer exists in the snapshot.
    #
    # Acquire the same transaction-level advisory lock used by finalize_period and
    # approve_rate before the used-lines check and the status mutation.  This
    # serialises void against any in-progress finalization for the same branch.
    #
    # PendingApproval rates are never referenced by finalised lines (the Phase 5
    # guard below would have caught that), and they are not visible to the
    # finalization LATERAL (which filters dr.status IN ('Approved','Superseded')).
    # The lock is therefore only needed for Approved/Superseded rates.
    if rate.status in ("Approved", "Superseded"):
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:cid, :bid)"),
            {"cid": company_id, "bid": rate.branch_id},
        )

    # Phase 5 guard: block void if this DriverRate is referenced by PayrollFinalLines.
    # Runs after ownership validation so cross-company ID probing does not leak info.
    # Runs before mutation and before any audit write.
    # (For Approved/Superseded rates this now also runs under the advisory lock.)
    used = await db.execute(
        text(
            "SELECT 1 FROM payroll.payrollfinallines "
            "WHERE driverrateid = :rid LIMIT 1"
        ),
        {"rid": rate_id},
    )
    if used.first() is not None:
        raise HTTPException(
            status_code=422,
            detail="This rate has been used in finalized payroll and cannot be voided.",
        )

    # Phase 12C â€” status-predicated void UPDATE.
    # Do NOT rely solely on the earlier status check: for PendingApproval rates a
    # concurrent approve_rate can transition the row to Approved between the initial
    # status read and this UPDATE.  Without a predicate, the UPDATE would void an
    # already-Approved rate, bypassing the Phase 12B active-rate advisory lock path.
    #
    # Use the status read at the top (rate.status) as the expected value.  For
    # Approved/Superseded rates the Phase 12B advisory lock was already acquired above,
    # so the lock serialises against concurrent finalization; the predicate here is an
    # additional belt-and-suspenders guard.  For PendingApproval rates the predicate
    # is the primary protection: if the row was concurrently approved we detect 0 rows
    # and raise a clean 409 rather than silently voiding an active rate.
    void_result = await db.execute(
        text("""
            UPDATE payroll.driverrates
            SET    status = 'Voided'
            WHERE  driverrateid = :rid
              AND  companyid    = :company_id
              AND  status       = :expected_status
            RETURNING driverrateid
        """),
        {"rid": rate_id, "company_id": company_id, "expected_status": rate.status},
    )
    if void_result.scalar_one_or_none() is None:
        # 0 rows â€” the rate's status changed concurrently (e.g. PendingApproval â†’ Approved).
        re_read = await db.execute(
            text("SELECT status FROM payroll.driverrates WHERE driverrateid = :rid"),
            {"rid": rate_id},
        )
        current_status = re_read.scalar_one_or_none()
        if current_status in ("Approved", "Superseded"):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Rate was approved or superseded concurrently. "
                    "To void an active rate, refresh and try again."
                ),
            )
        raise HTTPException(
            status_code=409,
            detail="Rate changed while voiding. Refresh and try again.",
        )

    # Audit write â€” inside the same transaction; failure rolls back the UPDATE
    await _write_rate_audit(
        db,
        company_id=company_id,
        branch_id=rate.branch_id,
        user_id=user_id,
        rate_id=rate_id,
        action_code="RATE_VOIDED",
        old_value={"status": rate.status},
        new_value={"status": "Voided"},
    )


# ---------------------------------------------------------------------------
# Resolve the applicable rate for a specific driver, rate type, and work date
# ---------------------------------------------------------------------------

async def resolve_rate_for_date(
    driver_id: int,
    rate_type_id: int,
    work_date: date,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> "DriverRateSummary | None":
    """
    Return the rate row that applies to *driver_id* / *rate_type_id* on *work_date*.

    Both **Approved** and **Superseded** rows are considered.  Superseded rows
    represent historically-closed rates whose effective date range is still valid
    for payroll lines whose work date falls inside that range â€” they must not be
    excluded from the lookup.

    Security:
      - Requires payrates.view / payrates.edit / settings.manage / setup.manage.
      - Enforces branch-scope: caller must have access to the driver's branch.
      - Enforces OwnDriverDataOnly: caller may only look up their own driver.
      - Cross-company lookup is impossible (company_id filter on driver lookup).

    Returns None (found=False) when the driver does not exist in this company or
    no rate covers the requested date.
    """
    # Step 1 â€” driver lookup (company-scoped; cross-company driver â†’ None, not 403).
    # Done first so we have the branch_id for the permission check below.
    drv_lookup = await db.execute(
        text("SELECT branchid FROM core.drivers WHERE driverid = :did AND companyid = :cid"),
        {"did": driver_id, "cid": company_id},
    )
    drv_lookup_row = drv_lookup.mappings().first()
    if drv_lookup_row is None:
        # Driver not in this company â€” return None without revealing existence via 403.
        # Permission check still runs (with None branch_id) to gate unauthenticated calls.
        await _check_any_permission(
            company_id, user_id, None,
            ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
        )
        return None

    driver_branch_id: int = drv_lookup_row["branchid"]

    # Step 2 â€” permission gate with the driver's branch so OwnDriverDataOnly users
    # (whose permission is granted per-branch, not company-wide) can pass.
    await _check_any_permission(
        company_id, user_id, driver_branch_id,
        ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
    )

    # Step 3 â€” branch-scope gate
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and driver_branch_id not in branch_ids:
        raise HTTPException(
            status_code=403,
            detail="Access denied to this driver's branch.",
        )

    # Step 4 â€” OwnDriverDataOnly: caller may only look up their own driver
    await _check_own_driver_only(company_id, user_id, driver_id, db)

    result = await db.execute(
        text(f"""
            {_RATE_SELECT}
            WHERE  dr.companyid    = :company_id
              AND  dr.driverid     = :driver_id
              AND  dr.ratetypeid   = :rate_type_id
              AND  dr.status       IN ('Approved', 'Superseded')
              AND  dr.effectivefrom <= CAST(:work_date AS date)
              AND  (dr.effectiveto IS NULL OR dr.effectiveto >= CAST(:work_date AS date))
            ORDER BY dr.effectivefrom DESC, dr.driverrateid DESC
            LIMIT 1
        """),
        {
            "company_id":    company_id,
            "driver_id":     driver_id,
            "rate_type_id":  rate_type_id,
            "work_date":     work_date,
        },
    )
    row = result.mappings().fetchone()
    return _rate_row_to_summary(row) if row is not None else None



# ===========================================================================
# Driver Rate Matrix (Phase 1)
# ===========================================================================

async def get_driver_rate_matrix(
    driver_id: int,
    company_id: int,
    user_id: int,
    as_of_date: date,
    db: AsyncConnection,
) -> DriverRateMatrix:
    """
    Return the full rate matrix for a driver as-of a given date.

    Steps:
      1. Get driver + branch info.
      2. Get active pay items for the branch that RequiresRate=TRUE.
      3. For each pay item / rate type combination, look up the current
         Approved rate and any PendingApproval rate.
    """
    # Step 1 â€” driver + branch
    drv_result = await db.execute(
        text("""
            SELECT d.driverid, d.drivercode, d.driverstatus,
                   e.fullname AS drivername,
                   d.branchid,
                   b.branchname
            FROM   core.drivers   d
            JOIN   core.employees e ON e.employeeid = d.employeeid
            JOIN   core.branches  b ON b.branchid   = d.branchid
            WHERE  d.driverid  = :did
              AND  d.companyid = :cid
        """),
        {"did": driver_id, "cid": company_id},
    )
    drv_row = drv_result.mappings().first()
    if drv_row is None:
        raise HTTPException(status_code=404, detail="Driver not found.")

    branch_id: int = drv_row["branchid"]

    # Permission gate â€” requires payrates.view, payrates.edit, settings.manage, or setup.manage
    await _check_any_permission(
        company_id=company_id,
        user_id=user_id,
        branch_id=branch_id,
        permission_codes=["payrates.view", "payrates.edit", "settings.manage", "setup.manage"],
        db=db,
    )

    # OwnDriverDataOnly scope â€” caller may only view their own driver's matrix.
    # Uses the shared fail-closed helper (_check_own_driver_only â†’ _get_oda_own_driver_id)
    # which rejects ambiguous overlapping active assignments rather than trusting latest row.
    await _check_own_driver_only(company_id, user_id, driver_id, db)

    # Step 2 â€” active pay items for this branch.
    # Uses LEFT JOIN on BranchPayItemConfig so that system items with
    # IsDefaultBranchActive=TRUE appear even when no explicit config row exists for
    # this branch.  Explicit config rows (IsActive=FALSE) override the default.
    items_result = await db.execute(
        text("""
            SELECT pi.payitemid,
                   pi.payitemname,
                   pi.itemscope,
                   pi.ratebehavior,
                   rt.ratetypeid,
                   rt.ratecode,
                   rt.ratename,
                   rt.unitname,
                   bpic.effectivefrom AS pay_item_effective_from
            FROM   payroll.payitems pi
            JOIN   payroll.payitemratetypemap pirm
                   ON pirm.payitemid = pi.payitemid AND pirm.status = 'Active'
            JOIN   payroll.ratetypes rt
                   ON rt.ratetypeid = pirm.ratetypeid AND rt.isactive = TRUE
            LEFT JOIN payroll.branchpayitemconfig bpic
                   ON bpic.payitemid   = pi.payitemid
                  AND bpic.companyid   = :company_id
                  AND bpic.branchid    = :branch_id
                  AND (bpic.effectiveto IS NULL OR bpic.effectiveto >= :as_of)
            WHERE  pi.status       != 'Retired'
              AND  pi.requiresrate  = TRUE
              AND  (pi.companyid IS NULL OR pi.companyid = :company_id)
              AND  COALESCE(bpic.isactive, pi.isdefaultbranchactive) = TRUE
              -- Phase 4C: structural RateType ownership via RateTypes.CompanyID.
              -- Only show system types (companyid IS NULL) or own-company types.
              AND (rt.companyid IS NULL OR rt.companyid = :company_id)
            ORDER BY pi.sortorder, pi.payitemname
        """),
        {"company_id": company_id, "branch_id": branch_id, "as_of": as_of_date},
    )
    items = items_result.mappings().all()

    groups: list[RateMatrixGroup] = []
    for row in items:
        rate_type_id: int = row["ratetypeid"]

        # Step 3a â€” current approved/superseded rate (Fix 6: include Superseded for historical as_of)
        approved_result = await db.execute(
            text("""
                SELECT driverrateid, amount, effectivefrom, effectiveto, status
                FROM   payroll.driverrates
                WHERE  driverid       = :did
                  AND  companyid      = :cid
                  AND  ratetypeid     = :rtid
                  AND  status         IN ('Approved', 'Superseded')
                  AND  effectivefrom <= :as_of
                  AND  (effectiveto IS NULL OR effectiveto >= :as_of)
                ORDER BY effectivefrom DESC
                LIMIT 1
            """),
            {"did": driver_id, "cid": company_id, "rtid": rate_type_id, "as_of": as_of_date},
        )
        approved_row = approved_result.mappings().first()

        # Step 3b â€” pending rate
        pending_result = await db.execute(
            text("""
                SELECT driverrateid, amount, effectivefrom, effectiveto, status
                FROM   payroll.driverrates
                WHERE  driverid   = :did
                  AND  companyid  = :cid
                  AND  ratetypeid = :rtid
                  AND  status     = 'PendingApproval'
                ORDER BY effectivefrom DESC
                LIMIT 1
            """),
            {"did": driver_id, "cid": company_id, "rtid": rate_type_id},
        )
        pending_row = pending_result.mappings().first()

        current_rate = (
            RateMatrixCurrentRate(
                driver_rate_id=approved_row["driverrateid"],
                amount=Decimal(str(approved_row["amount"])),
                effective_from=approved_row["effectivefrom"],
                effective_to=approved_row["effectiveto"],
                status=approved_row["status"],
            )
            if approved_row else None
        )
        pending_rate = (
            RateMatrixCurrentRate(
                driver_rate_id=pending_row["driverrateid"],
                amount=Decimal(str(pending_row["amount"])),
                effective_from=pending_row["effectivefrom"],
                effective_to=pending_row["effectiveto"],
                status=pending_row["status"],
            )
            if pending_row else None
        )

        pay_item_id: int = row["payitemid"]
        groups.append(
            RateMatrixGroup(
                group_key=f"{pay_item_id}:{rate_type_id}",
                rate_source="PayItem",
                pay_item_id=pay_item_id,
                pay_item_name=row["payitemname"],
                item_scope=row["itemscope"],
                rate_behavior=row["ratebehavior"],
                status_rate_column_id=None,
                rate_type_id=rate_type_id,
                rate_code=row["ratecode"],
                rate_name=row["ratename"],
                unit_name=row["unitname"],
                current_rate=current_rate,
                pending_rate=pending_rate,
                is_required=True,
                is_missing=(current_rate is None),
                pay_item_effective_from=row["pay_item_effective_from"],
            )
        )

    # Step 4 â€” StatusRateColumn groups for the branch.
    # Each active StatusRateColumn produces a separate rate group so the
    # driver can have a STATUS_PAY (or custom SRC_) rate set here.
    src_result = await db.execute(
        text("""
            SELECT src.statusratecolumnid, src.columnname,
                   src.ratetypeid, rt.ratecode, rt.ratename, rt.unitname
            FROM   payroll.statusratecolumns src
            JOIN   payroll.ratetypes rt ON rt.ratetypeid = src.ratetypeid
            WHERE  src.branchid  = :bid
              AND  src.companyid = :cid
              AND  src.isactive  = TRUE
            ORDER BY src.isdefault DESC, src.statusratecolumnid
        """),
        {"bid": branch_id, "cid": company_id},
    )
    for src_row in src_result.mappings().all():
        src_rate_type_id: int = src_row["ratetypeid"]
        src_col_id: int       = src_row["statusratecolumnid"]

        approved_result2 = await db.execute(
            text("""
                SELECT driverrateid, amount, effectivefrom, effectiveto, status
                FROM   payroll.driverrates
                WHERE  driverid       = :did
                  AND  companyid      = :cid
                  AND  ratetypeid     = :rtid
                  AND  status         IN ('Approved', 'Superseded')
                  AND  effectivefrom <= :as_of
                  AND  (effectiveto IS NULL OR effectiveto >= :as_of)
                ORDER BY effectivefrom DESC
                LIMIT 1
            """),
            {"did": driver_id, "cid": company_id, "rtid": src_rate_type_id, "as_of": as_of_date},
        )
        src_approved = approved_result2.mappings().first()

        pending_result2 = await db.execute(
            text("""
                SELECT driverrateid, amount, effectivefrom, effectiveto, status
                FROM   payroll.driverrates
                WHERE  driverid   = :did
                  AND  companyid  = :cid
                  AND  ratetypeid = :rtid
                  AND  status     = 'PendingApproval'
                ORDER BY effectivefrom DESC
                LIMIT 1
            """),
            {"did": driver_id, "cid": company_id, "rtid": src_rate_type_id},
        )
        src_pending = pending_result2.mappings().first()

        src_current = (
            RateMatrixCurrentRate(
                driver_rate_id=src_approved["driverrateid"],
                amount=Decimal(str(src_approved["amount"])),
                effective_from=src_approved["effectivefrom"],
                effective_to=src_approved["effectiveto"],
                status=src_approved["status"],
            )
            if src_approved else None
        )
        src_pending_rate = (
            RateMatrixCurrentRate(
                driver_rate_id=src_pending["driverrateid"],
                amount=Decimal(str(src_pending["amount"])),
                effective_from=src_pending["effectivefrom"],
                effective_to=src_pending["effectiveto"],
                status=src_pending["status"],
            )
            if src_pending else None
        )

        groups.append(
            RateMatrixGroup(
                group_key=f"SRC:{src_col_id}:{src_rate_type_id}",
                rate_source="StatusRateColumn",
                pay_item_id=None,
                pay_item_name=None,
                item_scope="Daily",
                rate_behavior="PerUnit",
                status_rate_column_id=src_col_id,
                rate_type_id=src_rate_type_id,
                rate_code=src_row["ratecode"],
                rate_name=src_row["columnname"],
                unit_name=src_row["unitname"],
                current_rate=src_current,
                pending_rate=src_pending_rate,
                is_required=True,
                is_missing=(src_current is None),
                pay_item_effective_from=None,
            )
        )

    return DriverRateMatrix(
        driver_id=driver_id,
        driver_name=drv_row["drivername"],
        driver_code=drv_row["drivercode"],
        branch_id=branch_id,
        branch_name=drv_row["branchname"],
        as_of=as_of_date,
        groups=groups,
    )


# ===========================================================================
# Batch rate save (Phase 2A)
# ===========================================================================

async def batch_save_rates(
    driver_id: int,
    company_id: int,
    user_id: int,
    data: BatchRateRequest,
    db: AsyncConnection,
) -> BatchRateSaveResult:
    """
    Atomically create or update pending rates for a driver and optionally
    auto-approve them when AllowSelfApproval is True.

    All validation runs before any writes so the operation is all-or-nothing
    within the caller's transaction.

    Phase 2A: only PerUnit, EnteredAmount, and Fixed rate behaviors are
    supported.  Tiered (OrdinalTier, RangeBracket, RangeProgressive) and
    Block rates must be saved via individual endpoints.
    """
    # Step 1 â€” validate changes list: empty and duplicate rate_type_id entries.
    # Empty list is caught by the schema validator; double-check here.
    if not data.changes:
        raise HTTPException(status_code=422, detail="changes must not be empty.")

    seen_rt_ids: set[int] = set()
    for change in data.changes:
        if change.rate_type_id in seen_rt_ids:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Duplicate rate_type_id={change.rate_type_id} in the same batch request."
                ),
            )
        seen_rt_ids.add(change.rate_type_id)

    # Step 2 â€” driver lookup + branch check
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    drv_result = await db.execute(
        text("""
            SELECT driverid, branchid FROM core.drivers
            WHERE driverid = :did AND companyid = :cid
        """),
        {"did": driver_id, "cid": company_id},
    )
    drv_row = drv_result.mappings().first()
    if drv_row is None:
        raise HTTPException(status_code=404, detail="Driver not found.")

    driver_branch_id: int = drv_row["branchid"]
    if not can_see_all and driver_branch_id not in branch_ids:
        raise HTTPException(status_code=403, detail="Access denied to this driver's branch.")

    # Step 3 â€” permission gate
    await _check_any_permission(
        company_id, user_id, driver_branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    # Step 3.5 â€” OwnDriverDataOnly: caller may only batch-save their own driver's rates
    await _check_own_driver_only(company_id, user_id, driver_id, db)

    # Step 4 â€” read AllowSelfApproval from company settings
    settings_result = await db.execute(
        text("SELECT allowselfapproval FROM core.companies WHERE companyid = :cid"),
        {"cid": company_id},
    )
    settings_row = settings_result.mappings().first()
    allow_self_approval: bool = bool(settings_row["allowselfapproval"]) if settings_row else True

    # Step 5 â€” finalized-period guard (check once for the shared effective_from)
    # Only relevant when we will auto-approve.  PendingApproval rows created when
    # allow_self_approval=False can be approved later â€” the guard runs at that point.
    if allow_self_approval:
        await _check_not_in_finalized_period(
            company_id, driver_branch_id, data.effective_from, db
        )

    # Step 6 â€” validate each change.
    #
    # All validation runs before any writes (all-or-nothing).
    #
    # PayItem path:  validates PayItemRateTypeMap + branch-active + behavior.
    # StatusRateColumn path: validates StatusRateColumns membership + rate_type_id match.
    for change in data.changes:
        if change.status_rate_column_id is not None:
            # ---- StatusRateColumn validation path ----
            src_result = await db.execute(
                text("""
                    SELECT src.ratetypeid
                    FROM   payroll.statusratecolumns src
                    WHERE  src.statusratecolumnid = :src_id
                      AND  src.branchid           = :bid
                      AND  src.companyid          = :cid
                      AND  src.isactive           = TRUE
                """),
                {
                    "src_id": change.status_rate_column_id,
                    "bid":    driver_branch_id,
                    "cid":    company_id,
                },
            )
            src_row = src_result.mappings().first()
            if src_row is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"status_rate_column_id={change.status_rate_column_id} not found "
                        "or not active for this driver's branch."
                    ),
                )
            if src_row["ratetypeid"] != change.rate_type_id:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"rate_type_id={change.rate_type_id} does not match the backing RateType "
                        f"for status_rate_column_id={change.status_rate_column_id}."
                    ),
                )
            # StatusRateColumn RateTypes are company-owned or system â€” ownership already
            # enforced by trg_src_ratetype_owner trigger; skip redundant check here.
        else:
            # ---- PayItem validation path ----
            # Exact pay_item_id + rate_type_id mapping check
            map_result = await db.execute(
                text("""
                    SELECT
                        pi.payitemid,
                        pi.ratebehavior,
                        pi.isdefaultbranchactive,
                        bpic.isactive AS cfg_isactive
                    FROM payroll.payitemratetypemap pirm
                    JOIN payroll.payitems  pi ON pi.payitemid    = pirm.payitemid
                    JOIN payroll.ratetypes rt ON rt.ratetypeid   = pirm.ratetypeid
                                             AND rt.isactive      = TRUE
                    LEFT JOIN payroll.branchpayitemconfig bpic
                           ON bpic.payitemid  = pi.payitemid
                          AND bpic.companyid  = :cid
                          AND bpic.branchid   = :bid
                          AND (bpic.effectiveto IS NULL OR bpic.effectiveto >= CAST(:effective_from AS date))
                    WHERE pirm.payitemid  = :piid
                      AND pirm.ratetypeid = :rtid
                      AND pirm.status     = 'Active'
                      AND pi.status       != 'Retired'
                      AND pi.requiresrate = TRUE
                      AND (pi.companyid IS NULL OR pi.companyid = :cid)
                    ORDER BY bpic.effectivefrom DESC NULLS LAST
                    LIMIT 1
                """),
                {
                    "piid":           change.pay_item_id,
                    "rtid":           change.rate_type_id,
                    "cid":            company_id,
                    "bid":            driver_branch_id,
                    "effective_from": data.effective_from,
                },
            )
            map_row = map_result.mappings().first()

            if map_row is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"pay_item_id={change.pay_item_id} is not actively mapped to "
                        f"rate_type_id={change.rate_type_id} for this company. "
                        "Verify the PayItem â†’ RateType mapping is Active."
                    ),
                )

            # Phase 4B.3 â€” defense-in-depth: validate RateType ownership
            await _assert_rate_type_allowed_for_company(db, company_id, change.rate_type_id)

            cfg = map_row["cfg_isactive"]
            is_branch_active = bool(cfg) if cfg is not None else bool(map_row["isdefaultbranchactive"])
            if not is_branch_active:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"pay_item_id={change.pay_item_id} is not active for this driver's branch "
                        f"as of {data.effective_from}."
                    ),
                )

            rate_behavior: str = map_row["ratebehavior"] or "PerUnit"
            if rate_behavior in _TIERED_BEHAVIORS or rate_behavior == "Block":
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"pay_item_id={change.pay_item_id} uses '{rate_behavior}' behavior "
                        "which requires tier or block configuration. "
                        "Use the individual rate endpoint to save this rate."
                    ),
                )

    # Step 7 â€” write: create or update PendingApproval rows
    # All validation passed â€” now write inside the same transaction.
    created_ids: list[int] = []
    updated_pending_ids: list[int] = []

    batch_note = data.notes  # batch-level note

    for change in data.changes:
        # Check for an existing PendingApproval rate for this driver + rate type
        existing = await db.execute(
            text("""
                SELECT driverrateid FROM payroll.driverrates
                WHERE  driverid   = :did
                  AND  ratetypeid = :rtid
                  AND  companyid  = :cid
                  AND  status     = 'PendingApproval'
                ORDER  BY createdatutc DESC
                LIMIT  1
            """),
            {"did": driver_id, "rtid": change.rate_type_id, "cid": company_id},
        )
        existing_row = existing.mappings().first()

        if existing_row is not None:
            # Update the existing pending row.
            # effectiveto is reset to NULL to clear any stale bounded end date
            # that may have been set by the individual rate endpoint or a prior
            # batch edit â€” the batch always creates open-ended rates.
            #
            # Phase 12C â€” status-predicated UPDATE.
            # The SELECT above found a PendingApproval row, but a concurrent
            # approve_rate can approve it between that SELECT and this UPDATE.
            # Without a status predicate the UPDATE would silently mutate an
            # Approved rate's amount/date through a stale pending path.
            # AND status='PendingApproval' + RETURNING makes this atomic: if
            # the row was concurrently approved, 0 rows are returned â†’ 409.
            existing_rate_id: int = existing_row["driverrateid"]
            batch_upd = await db.execute(
                text("""
                    UPDATE payroll.driverrates
                    SET    amount        = :amount,
                           effectivefrom = :effective_from,
                           effectiveto   = NULL,
                           notes         = :notes
                    WHERE  driverrateid  = :rid
                      AND  companyid     = :cid
                      AND  status        = 'PendingApproval'
                    RETURNING driverrateid
                """),
                {
                    "amount":         change.amount,
                    "effective_from": data.effective_from,
                    "notes":          change.notes or batch_note,
                    "rid":            existing_rate_id,
                    "cid":            company_id,
                },
            )
            if batch_upd.scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Rate (id={existing_rate_id}) was concurrently approved or "
                        "changed. Refresh and try again."
                    ),
                )
            await _write_rate_audit(
                db,
                company_id=company_id,
                branch_id=driver_branch_id,
                user_id=user_id,
                rate_id=existing_rate_id,
                action_code="RATE_UPDATED",
                old_value=None,
                new_value={
                    "amount":         str(change.amount),
                    "effective_from": str(data.effective_from),
                    "source":         "batch_save",
                },
            )
            updated_pending_ids.append(existing_rate_id)
        else:
            # Create a new PendingApproval row
            ins = await db.execute(
                text("""
                    INSERT INTO payroll.driverrates
                        (companyid, branchid, driverid, ratetypeid, amount,
                         effectivefrom, effectiveto, status, createdbyuserid, notes)
                    VALUES
                        (:cid, :bid, :did, :rtid, :amount,
                         :effective_from, NULL, 'PendingApproval', :uid, :notes)
                    RETURNING driverrateid
                """),
                {
                    "cid":            company_id,
                    "bid":            driver_branch_id,
                    "did":            driver_id,
                    "rtid":           change.rate_type_id,
                    "amount":         change.amount,
                    "effective_from": data.effective_from,
                    "uid":            user_id,
                    "notes":          change.notes or batch_note,
                },
            )
            new_rate_id: int = ins.scalar_one()
            await _write_rate_audit(
                db,
                company_id=company_id,
                branch_id=driver_branch_id,
                user_id=user_id,
                rate_id=new_rate_id,
                action_code="RATE_CREATED",
                new_value={
                    "driver_id":      driver_id,
                    "rate_type_id":   change.rate_type_id,
                    "amount":         str(change.amount),
                    "effective_from": str(data.effective_from),
                    "status":         "PendingApproval",
                    "source":         "batch_save",
                },
            )
            created_ids.append(new_rate_id)

    all_rate_ids = created_ids + updated_pending_ids

    # Step 8 â€” auto-approve if allowed
    approved_count = 0
    pending_count = 0

    if allow_self_approval:
        for rate_id in all_rate_ids:
            # Call approve_rate â€” it re-checks permissions (safe, same user),
            # handles the supersede logic, and writes the approval audit.
            await approve_rate(rate_id, company_id, user_id, db)
        approved_count = len(all_rate_ids)
    else:
        pending_count = len(all_rate_ids)

    # Step 9 â€” fetch final state of all affected rates
    rate_summaries = [await _get_rate_with_tiers(rid, company_id, db) for rid in all_rate_ids]

    return BatchRateSaveResult(
        driver_id=driver_id,
        effective_from=data.effective_from,
        allow_self_approval=allow_self_approval,
        created_count=len(created_ids),
        updated_pending_count=len(updated_pending_ids),
        approved_count=approved_count,
        pending_count=pending_count,
        rates=rate_summaries,
    )


# ===========================================================================
# Phase 2B â€” driver rate summary / pending / history
# ===========================================================================

async def get_driver_rates_summary(
    driver_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> DriverRatesSummary:
    """
    Return quick status counts for a driver:
    - pending_count: PendingApproval rates
    - future_approved_count: Approved rates with effective_from > today
    - missing_required_count: required matrix slots with no current Approved/Superseded rate

    missing_required_count uses the same INNER JOIN logic as get_driver_rate_matrix
    (BranchPayItemConfig INNER JOIN) so items with only IsDefaultBranchActive=TRUE
    and no explicit config row are NOT counted as required.
    """
    branch_id = await _check_driver_read_access(driver_id, company_id, user_id, db)

    # Counts from driverrates
    counts_result = await db.execute(
        text("""
            SELECT
                SUM(CASE WHEN status = 'PendingApproval' THEN 1 ELSE 0 END)
                    AS pending_count,
                SUM(CASE WHEN status = 'Approved'
                          AND effectivefrom > CURRENT_DATE THEN 1 ELSE 0 END)
                    AS future_approved_count
            FROM payroll.driverrates
            WHERE driverid  = :did
              AND companyid = :cid
              AND status IN ('PendingApproval', 'Approved')
        """),
        {"did": driver_id, "cid": company_id},
    )
    counts_row = counts_result.mappings().first()
    pending_count: int       = int(counts_row["pending_count"] or 0)
    future_approved_count: int = int(counts_row["future_approved_count"] or 0)

    # Missing required count â€” mirrors matrix logic exactly (LEFT JOIN + COALESCE
    # fallback so that system items with IsDefaultBranchActive=TRUE are counted
    # even when no explicit BranchPayItemConfig row exists for this branch).
    missing_result = await db.execute(
        text("""
            SELECT
                COUNT(*) AS total_required,
                COUNT(approved.driverrateid) AS with_current_rate
            FROM payroll.payitems pi
            JOIN payroll.payitemratetypemap pirm
                 ON pirm.payitemid = pi.payitemid AND pirm.status = 'Active'
            JOIN payroll.ratetypes rt
                 ON rt.ratetypeid = pirm.ratetypeid AND rt.isactive = TRUE
            LEFT JOIN payroll.branchpayitemconfig bpic
                 ON bpic.payitemid    = pi.payitemid
                AND bpic.companyid   = :cid
                AND bpic.branchid    = :bid
                AND (bpic.effectiveto IS NULL OR bpic.effectiveto >= CURRENT_DATE)
                AND bpic.effectivefrom <= CURRENT_DATE
            LEFT JOIN LATERAL (
                SELECT driverrateid FROM payroll.driverrates
                WHERE  driverid      = :did
                  AND  companyid     = :cid
                  AND  ratetypeid    = pirm.ratetypeid
                  AND  status        IN ('Approved', 'Superseded')
                  AND  effectivefrom <= CURRENT_DATE
                  AND  (effectiveto IS NULL OR effectiveto >= CURRENT_DATE)
                ORDER BY effectivefrom DESC
                LIMIT 1
            ) approved ON TRUE
            WHERE pi.status      != 'Retired'
              AND pi.requiresrate = TRUE
              AND (pi.companyid IS NULL OR pi.companyid = :cid)
              AND COALESCE(bpic.isactive, pi.isdefaultbranchactive) = TRUE
        """),
        {"did": driver_id, "cid": company_id, "bid": branch_id},
    )
    missing_row = missing_result.mappings().first()
    if missing_row is not None:
        total = int(missing_row["total_required"] or 0)
        with_rate = int(missing_row["with_current_rate"] or 0)
        missing_required_count: int | None = max(0, total - with_rate)
    else:
        missing_required_count = None

    return DriverRatesSummary(
        driver_id=driver_id,
        pending_count=pending_count,
        future_approved_count=future_approved_count,
        missing_required_count=missing_required_count,
    )


async def get_driver_rates_pending(
    driver_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> list[DriverRateSummary]:
    """
    Return all PendingApproval rates for a driver, newest first.

    Security: same as get_driver_rates_summary (_check_driver_read_access).
    Returns 404 if the driver doesn't exist in this company.
    """
    await _check_driver_read_access(driver_id, company_id, user_id, db)

    result = await db.execute(
        text(
            f"{_RATE_SELECT} "
            "WHERE dr.driverid  = :did "
            "  AND dr.companyid = :cid "
            "  AND dr.status    = 'PendingApproval' "
            "ORDER BY dr.createdatutc DESC"
        ),
        {"did": driver_id, "cid": company_id},
    )
    return [_rate_row_to_summary(r) for r in result.mappings().all()]


async def get_driver_rates_history(
    driver_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    limit: int = 200,
    offset: int = 0,
) -> list[DriverRateSummary]:
    """
    Return the full rate history for a driver across all statuses:
    Approved, PendingApproval, Superseded, Voided.

    Sorted newest-first: effective_from DESC, created_at_utc DESC.
    Defaults to 200 rows; callers may paginate via limit/offset.

    Security: same as get_driver_rates_summary (_check_driver_read_access).
    Returns 404 if the driver doesn't exist in this company.
    """
    await _check_driver_read_access(driver_id, company_id, user_id, db)

    result = await db.execute(
        text(
            f"{_RATE_SELECT} "
            "WHERE dr.driverid  = :did "
            "  AND dr.companyid = :cid "
            "ORDER BY dr.effectivefrom DESC, dr.createdatutc DESC "
            "LIMIT :limit OFFSET :offset"
        ),
        {"did": driver_id, "cid": company_id, "limit": limit, "offset": offset},
    )
    return [_rate_row_to_summary(r) for r in result.mappings().all()]


# ---------------------------------------------------------------------------
# Bulk driver rates summary (Phase 2C â€” D)
# ---------------------------------------------------------------------------

async def get_bulk_driver_rates_summary(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    branch_id: int | None = None,
) -> list[DriverRatesSummary]:
    """
    Return DriverRatesSummary for every driver the caller can access.

    Security:
      - ODA users: only own driver.
      - SpecificBranch users: only drivers in their assigned branch(es).
      - AllCompanyBranches users: all company drivers (filtered by branch_id param).

    This is designed for the left-panel badge overlay â€” it must NOT leak driver
    existence across companies.

    missing_required_count is NOT computed here (it would require N LATERAL subqueries).
    It is always None in bulk responses; callers may use the single-driver summary
    endpoint for the full count on a selected driver.
    """
    # ODA: only own driver
    own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)
    if own_driver_id is not None:
        accessible_driver_ids = [own_driver_id]
    else:
        await _check_any_permission(
            company_id, user_id, branch_id,
            ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
        )
        can_see_all, allowed_branch_ids = await _check_branch_access(company_id, user_id, db)
        drv_q_parts = ["companyid = :cid"]
        drv_params: dict = {"cid": company_id}
        if not can_see_all:
            if not allowed_branch_ids:
                return []
            in_clause, extra = _build_in_clause(allowed_branch_ids, "bid")
            drv_q_parts.append(f"branchid IN ({in_clause})")
            drv_params.update(extra)
        elif branch_id is not None:
            drv_q_parts.append("branchid = :filter_bid")
            drv_params["filter_bid"] = branch_id
        where = " AND ".join(drv_q_parts)
        drv_result = await db.execute(
            text(f"SELECT driverid FROM core.drivers WHERE {where}"),
            drv_params,
        )
        accessible_driver_ids = [r["driverid"] for r in drv_result.mappings().all()]

    if not accessible_driver_ids:
        return []

    in_clause2, params2 = _build_in_clause(accessible_driver_ids, "did")

    counts_result = await db.execute(
        text(f"""
            SELECT
                driverid,
                SUM(CASE WHEN status = 'PendingApproval' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN status = 'Approved'
                          AND effectivefrom > CURRENT_DATE THEN 1 ELSE 0 END) AS future_approved_count
            FROM payroll.driverrates
            WHERE companyid = :cid
              AND driverid IN ({in_clause2})
              AND status IN ('PendingApproval', 'Approved')
            GROUP BY driverid
        """),
        {"cid": company_id, **params2},
    )
    counts_map: dict[int, dict] = {
        r["driverid"]: {
            "pending_count": int(r["pending_count"] or 0),
            "future_approved_count": int(r["future_approved_count"] or 0),
        }
        for r in counts_result.mappings().all()
    }

    return [
        DriverRatesSummary(
            driver_id=did,
            pending_count=counts_map.get(did, {}).get("pending_count", 0),
            future_approved_count=counts_map.get(did, {}).get("future_approved_count", 0),
            missing_required_count=None,
        )
        for did in accessible_driver_ids
    ]


# ---------------------------------------------------------------------------
# Copy Rates From Driver (Phase 2C â€” C)
# ---------------------------------------------------------------------------

async def copy_driver_rates(
    target_driver_id: int,
    source_driver_id: int,
    company_id: int,
    user_id: int,
    data: CopyRatesRequest,
    db: AsyncConnection,
) -> CopyRatesResult:
    """
    Copy current Approved rates from source_driver to target_driver.

    Rules:
    - Caller must have payrates.edit on BOTH source and target driver branches.
    - ODA users cannot use this endpoint.
    - Same company only (enforced by driver lookup).
    - Only current Approved rates as-of data.effective_from are copied.
    - PendingApproval rates are never copied.
    - Atomic â€” validated before any writes.
    - Backdating guard applies (same as batch save).
    - AllowSelfApproval controls whether copied rates auto-approve or become
      PendingApproval.
    - include_pay_rules=True copies Active MinimumPay/MaximumPay rules from source;
      rejected with 422 if target already has an overlapping rule of same type.
    - Rates with tiered/block structure: only flat amount is copied; tier details
      are not copied (this is safe â€” the new rate row is a simple Flat rate).
    """
    # ODA users must not use copy-from
    own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)
    if own_driver_id is not None:
        raise HTTPException(
            status_code=403,
            detail="OwnDriverDataOnly users cannot use copy-rates-from.",
        )

    # Verify both drivers exist in this company
    src_result = await db.execute(
        text("SELECT branchid FROM core.drivers WHERE driverid = :did AND companyid = :cid"),
        {"did": source_driver_id, "cid": company_id},
    )
    src_row = src_result.mappings().first()
    if src_row is None:
        raise HTTPException(status_code=404, detail="Source driver not found in this company.")
    source_branch_id: int = src_row["branchid"]

    tgt_result = await db.execute(
        text("SELECT branchid FROM core.drivers WHERE driverid = :did AND companyid = :cid"),
        {"did": target_driver_id, "cid": company_id},
    )
    tgt_row = tgt_result.mappings().first()
    if tgt_row is None:
        raise HTTPException(status_code=404, detail="Target driver not found in this company.")
    target_branch_id: int = tgt_row["branchid"]

    # Require payrates.edit on both branches
    await _check_any_permission(
        company_id, user_id, source_branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )
    await _check_any_permission(
        company_id, user_id, target_branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    # AllowSelfApproval
    settings_result = await db.execute(
        text("SELECT allowselfapproval FROM core.companies WHERE companyid = :cid"),
        {"cid": company_id},
    )
    settings_row = settings_result.mappings().first()
    allow_self_approval: bool = bool(settings_row["allowselfapproval"]) if settings_row else True

    # Fetch current Approved rates from source as-of effective_from.
    # Also fetch:
    #   - rate_name / rate_code (from ratetypes) for error messages
    #   - has_tiers: TRUE if at least one row exists in driverratetiers for this rate
    #   - has_block: TRUE if blocksizecustomunit IS NOT NULL (Block-behavior rates)
    #
    # Note: ratebehavior is NOT used here because PerUnit/PerHour/etc. pay items
    # produce simple flat-amount rate rows (no tiers). Only the actual presence of
    # tier rows or block metadata on a specific rate row indicates an advanced rate.
    source_rates_result = await db.execute(
        text("""
            SELECT DISTINCT ON (dr.ratetypeid)
                dr.driverrateid,
                dr.ratetypeid,
                dr.amount,
                rt.ratename,
                rt.ratecode,
                (dr.blocksize IS NOT NULL) AS has_block,
                EXISTS (
                    SELECT 1 FROM payroll.driverratetiers dt
                    WHERE dt.driverrateid = dr.driverrateid
                ) AS has_tiers
            FROM payroll.driverrates dr
            JOIN payroll.ratetypes rt ON rt.ratetypeid = dr.ratetypeid
            WHERE dr.driverid      = :src_did
              AND dr.companyid     = :cid
              AND dr.status        = 'Approved'
              AND dr.effectivefrom <= :eff_from
              AND (dr.effectiveto IS NULL OR dr.effectiveto >= :eff_from)
            ORDER BY dr.ratetypeid, dr.effectivefrom DESC
        """),
        {"src_did": source_driver_id, "cid": company_id, "eff_from": data.effective_from},
    )
    source_rates = source_rates_result.mappings().all()

    if not source_rates:
        return CopyRatesResult(
            target_driver_id=target_driver_id,
            source_driver_id=source_driver_id,
            effective_from=data.effective_from,
            allow_self_approval=allow_self_approval,
            rates_copied=0, rates_approved=0, rates_pending=0, pay_rules_copied=0,
        )

    # Fix 3 â€” Reject advanced/tier/block rates.
    # Copying only the flat amount of a tiered or block rate would silently discard
    # the rate structure that payroll finalization relies on.  Reject the entire
    # copy request if any source rate has actual tier rows (driverratetiers) or
    # block metadata (blocksizecustomunit) on the specific rate row.
    advanced_rate_names = [
        r["ratename"] or r["ratecode"]
        for r in source_rates
        if r["has_tiers"] or r["has_block"]
    ]
    if advanced_rate_names:
        raise HTTPException(
            status_code=422,
            detail=(
                "Advanced/tiered rates cannot be copied: "
                f"{', '.join(advanced_rate_names)}. "
                "Copy individual advanced rates manually after the copy, "
                "or first replace them with flat rates in the source driver."
            ),
        )

    # Fix 2 â€” All-or-nothing: every source rate must be valid for the target branch.
    # Previously invalid rates were silently skipped. Now any mismatch rejects the
    # entire request so no partial copy occurs.
    rate_type_ids = [r["ratetypeid"] for r in source_rates]
    in_clause, in_params = _build_in_clause(rate_type_ids, "rtid")
    valid_result = await db.execute(
        text(f"""
            SELECT DISTINCT pirm.ratetypeid
            FROM payroll.payitemratetypemap pirm
            JOIN payroll.payitems  pi ON pi.payitemid    = pirm.payitemid
                                     AND pi.status       != 'Retired'
                                     AND pi.requiresrate = TRUE
            JOIN payroll.ratetypes rt ON rt.ratetypeid   = pirm.ratetypeid
                                     AND rt.isactive      = TRUE
            LEFT JOIN payroll.branchpayitemconfig bpic
                                 ON bpic.payitemid    = pi.payitemid
                                AND bpic.companyid    = :cid
                                AND bpic.branchid     = :bid
                                AND bpic.effectivefrom <= :eff_from
                                AND (bpic.effectiveto IS NULL OR bpic.effectiveto >= :eff_from)
            WHERE pirm.status    = 'Active'
              AND pirm.ratetypeid IN ({in_clause})
              AND (pi.companyid IS NULL OR pi.companyid = :cid)
              AND COALESCE(bpic.isactive, pi.isdefaultbranchactive) = TRUE
        """),
        {"cid": company_id, "bid": target_branch_id, "eff_from": data.effective_from, **in_params},
    )
    valid_rate_type_ids = {r["ratetypeid"] for r in valid_result.mappings().all()}

    # All-or-nothing: any invalid rate type for target branch â†’ reject
    invalid_rates = [
        r["ratename"] or r["ratecode"]
        for r in source_rates
        if r["ratetypeid"] not in valid_rate_type_ids
    ]
    if invalid_rates:
        raise HTTPException(
            status_code=422,
            detail=(
                "Cannot copy: the following rate type(s) are not configured for the "
                f"target driver's branch: {', '.join(invalid_rates)}. "
                "Ensure the target branch has these rate types active "
                "in its pay item configuration before copying."
            ),
        )

    # All source rates are valid for target branch
    copyable_rates = list(source_rates)

    # â”€â”€ Pre-write conflict validation (ALL checks before ANY mutation) â”€â”€â”€â”€â”€â”€â”€â”€
    #
    # For every rate type we are about to copy, verify that the target driver
    # has no Approved rate starting on or after data.effective_from.  Superseding
    # such a rate would set its EffectiveTo BEFORE its own EffectiveFrom (invalid
    # dates), corrupt the effective-date history, and trigger the DB EXCLUDE
    # constraint with an error rather than a clean 422.
    #
    # This mirrors the conflict guard in approve_rate (Step 1.5) and ensures
    # copy_driver_rates and approve_rate enforce the same lifecycle invariants.
    # Phase 12 / 12B â€” Finalization atomicity advisory lock (copy path).
    # copy_driver_rates with allow_self_approval=True directly writes Approved rates
    # and calls _supersede_current_approved_rates, bypassing approve_rate.  Acquire
    # the same transaction-level advisory lock as finalize_period and approve_rate so
    # that this copy path is also serialised against concurrent finalization.
    #
    # Phase 12B fix: _check_not_in_finalized_period and _check_no_future_approved_conflict
    # are intentionally placed AFTER this lock so their guard decisions reflect the
    # post-lock state of the DB (TOCTOU safety).  Previously these ran before the lock;
    # a concurrent finalize_period could Lock the period while we waited, making the
    # guard decisions stale.
    # The lock is a no-op when allow_self_approval=False (PendingApproval only,
    # no rate-state change that could affect finalization).
    if allow_self_approval:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:cid, :bid)"),
            {"cid": company_id, "bid": target_branch_id},
        )
        # Backdating guard â€” run under lock for TOCTOU safety.
        await _check_not_in_finalized_period(
            company_id, target_branch_id, data.effective_from, db
        )

    # â”€â”€ Pre-write conflict validation (ALL checks before ANY mutation) â”€â”€â”€â”€â”€â”€â”€â”€
    # Phase 12B: moved to after the advisory lock so guard decisions are post-lock.
    for src_rate in copyable_rates:
        await _check_no_future_approved_conflict(
            company_id, target_driver_id, src_rate["ratetypeid"],
            data.effective_from, db,
        )
    # â”€â”€ End pre-write validation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    rates_approved = 0
    rates_pending = 0
    _copy_note = f"Copied from driver {source_driver_id}"

    for src_rate in copyable_rates:
        rate_type_id = src_rate["ratetypeid"]
        amount = src_rate["amount"]

        # Void existing PendingApproval rows for this rate type (shared helper).
        # Must happen BEFORE supersession so the pending rows are cleared first.
        await _void_pending_rates_for_rate_type(
            company_id, target_driver_id, rate_type_id, user_id, target_branch_id, db,
        )

        if allow_self_approval:
            # Supersede any existing Approved rate that started BEFORE effective_from
            # (shared helper â€” uses the same CASE/WHERE logic as approve_rate Step 2).
            # The conflict guard above already ensures no Approved rate starts on
            # or after effective_from, so this UPDATE is safe.
            await _supersede_current_approved_rates(
                company_id, target_driver_id, rate_type_id,
                data.effective_from, user_id, target_branch_id, db,
            )

        # Use two separate SQL statements to avoid asyncpg NULL type-inference issues
        # with nullable TIMESTAMPTZ parameters.
        if allow_self_approval:
            insert_result = await db.execute(
                text("""
                    INSERT INTO payroll.driverrates
                        (companyid, branchid, driverid, ratetypeid,
                         amount, effectivefrom, status,
                         createdbyuserid, approvedbyuserid, approvedatutc, notes)
                    VALUES
                        (:cid, :bid, :did, :rtid,
                         :amount, :eff_from, 'Approved',
                         :creator, :creator, NOW(), :notes)
                    RETURNING driverrateid
                """),
                {
                    "cid": company_id, "bid": target_branch_id, "did": target_driver_id,
                    "rtid": rate_type_id, "amount": amount, "eff_from": data.effective_from,
                    "creator": user_id, "notes": _copy_note,
                },
            )
        else:
            insert_result = await db.execute(
                text("""
                    INSERT INTO payroll.driverrates
                        (companyid, branchid, driverid, ratetypeid,
                         amount, effectivefrom, status,
                         createdbyuserid, notes)
                    VALUES
                        (:cid, :bid, :did, :rtid,
                         :amount, :eff_from, 'PendingApproval',
                         :creator, :notes)
                    RETURNING driverrateid
                """),
                {
                    "cid": company_id, "bid": target_branch_id, "did": target_driver_id,
                    "rtid": rate_type_id, "amount": amount, "eff_from": data.effective_from,
                    "creator": user_id, "notes": _copy_note,
                },
            )
        new_rate_id: int = insert_result.scalar_one()
        await _write_rate_audit(
            db,
            company_id=company_id,
            branch_id=target_branch_id,
            user_id=user_id,
            rate_id=new_rate_id,
            action_code="RATE_CREATED",
            new_value={
                "driver_id":             target_driver_id,
                "rate_type_id":          rate_type_id,
                "amount":                str(amount),
                "effective_from":        str(data.effective_from),
                "status":                "Approved" if allow_self_approval else "PendingApproval",
                "copied_from_driver_id": source_driver_id,
            },
        )

        if allow_self_approval:
            rates_approved += 1
        else:
            rates_pending += 1

    # Copy pay rules if requested
    pay_rules_copied = 0
    if data.include_pay_rules:
        src_rules_result = await db.execute(
            text("""
                SELECT ruletype, amount, effectivefrom, effectiveto, notes
                FROM   payroll.driverpayrules
                WHERE  driverid  = :src_did
                  AND  companyid = :cid
                  AND  status    = 'Active'
                ORDER BY ruletype, effectivefrom
            """),
            {"src_did": source_driver_id, "cid": company_id},
        )
        src_rules = src_rules_result.mappings().all()

        for src_rule in src_rules:
            # P1 #2 â€” Finalized-period guard for copied pay rules.
            #
            # Copied rules use data.effective_from (the request date), NOT the source
            # driver's historical effectivefrom.  This is consistent with how rates are
            # copied: all copied data starts from the single request effective_from date.
            #
            # Guard behaviour by allow_self_approval value:
            #   True  â€” the rates backdating guard (earlier in this function) already
            #           raised 422 for the same date, so this guard is never reached.
            #           It is kept here for correctness when future callers bypass the
            #           rates section (e.g. no Approved source rates exist).
            #   False â€” rates are written as PendingApproval (no backdating guard for
            #           rates); this guard is the primary protection for pay rules.
            await _check_not_in_finalized_period(
                company_id, target_branch_id, data.effective_from, db,
                label="pay rule",
            )

            # Overlap check uses data.effective_from; the new rule is open-ended (no
            # effectiveto) so any existing active/ended rule whose effectiveto is NULL
            # or >= data.effective_from would conflict.
            overlap = await db.execute(
                text("""
                    SELECT driverpayruleid
                    FROM   payroll.driverpayrules
                    WHERE  companyid = :cid
                      AND  driverid  = :did
                      AND  ruletype  = :rtype
                      AND  status    IN ('Active', 'Ended')
                      AND  (effectiveto IS NULL OR effectiveto >= :eff_from)
                    LIMIT 1
                """),
                {
                    "cid":      company_id,
                    "did":      target_driver_id,
                    "rtype":    src_rule["ruletype"],
                    "eff_from": data.effective_from,
                },
            )
            if overlap.first() is not None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Cannot copy {src_rule['ruletype']} rule: target driver already has "
                        f"an overlapping rule. End the existing rule first."
                    ),
                )

            # Insert with request effective_from and open effectiveto.
            # The source rule's historical effectiveto is not carried over â€” the target
            # gets a fresh open-ended rule starting at the requested date.
            rule_insert_result = await db.execute(
                text("""
                    INSERT INTO payroll.driverpayrules
                        (companyid, branchid, driverid, ruletype, amount,
                         effectivefrom, effectiveto, status, createdbyuserid, notes)
                    VALUES
                        (:cid, :bid, :did, :rtype, :amount,
                         :eff_from, NULL, 'Active', :creator, :notes)
                    RETURNING driverpayruleid
                """),
                {
                    "cid":      company_id,
                    "bid":      target_branch_id,
                    "did":      target_driver_id,
                    "rtype":    src_rule["ruletype"],
                    "amount":   src_rule["amount"],
                    "eff_from": data.effective_from,   # â† request date, not source date
                    "creator":  user_id,
                    "notes":    f"Copied from driver {source_driver_id}",
                },
            )
            new_rule_id: int = rule_insert_result.scalar_one()
            await _write_pay_rule_audit(
                db,
                company_id=company_id,
                branch_id=target_branch_id,
                user_id=user_id,
                rule_id=new_rule_id,
                action_code="DRIVER_PAY_RULE_CREATED",
                new_value={
                    "driver_id":             target_driver_id,
                    "rule_type":             src_rule["ruletype"],
                    "amount":                str(src_rule["amount"]),
                    "effective_from":        str(data.effective_from),
                    "effective_to":          None,
                    "status":                "Active",
                    "copied_from_driver_id": source_driver_id,
                },
            )
            pay_rules_copied += 1

    return CopyRatesResult(
        target_driver_id=target_driver_id,
        source_driver_id=source_driver_id,
        effective_from=data.effective_from,
        allow_self_approval=allow_self_approval,
        rates_copied=len(copyable_rates),
        rates_approved=rates_approved,
        rates_pending=rates_pending,
        pay_rules_copied=pay_rules_copied,
    )
