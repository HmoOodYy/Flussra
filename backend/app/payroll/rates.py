"""DriverRate authoring, lifecycle, batch operations, and compatibility endpoint.

Read-only catalog, matrix, and resolution queries live in rate_reads.
Ordinary PayDefinition rates are authored through app.compensation schedules.
The legacy rate-copy endpoint remains mounted but refuses copying.
"""
import json
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.branch_access import check_branch_access
from app.access.permissions import check_any_permission
from app.company_currency import lock_and_get_company_currency_for_monetary_write
from app.payroll import rate_reads
from app.payroll.guards import (
    _require_non_driver_rate_subject,
    check_not_in_finalized_period,
)
from app.payroll.schemas import (
    BatchRateRequest,
    BatchRateSaveResult,
    DriverRateCreate,
    DriverRateSummary,
    DriverRateUpdate,
    OrdinalTierCreate,
    RangeTierCreate,
)
from app.workforce.effective import require_writable_driver_assignment

# ===========================================================================
# Pay Rates â€” authoring, lifecycle, and batch operations
# ===========================================================================

# Rate behaviors backed by DriverRateTiers.
_TIERED_BEHAVIORS: frozenset[str] = frozenset({"OrdinalTier", "RangeBracket", "RangeProgressive"})
# Range behaviors (RangeBracket + RangeProgressive) share the same tier input/storage.
_RANGE_BEHAVIORS: frozenset[str] = frozenset({"RangeBracket", "RangeProgressive"})

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


def _ordinary_rate_authoring_retired() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "ORDINARY_RATE_AUTHORING_RETIRED",
            "message": (
                "Ordinary PayDefinition rates are authored through "
                "/compensation/driver-rate-schedules. The legacy rate path remains "
                "available only for Status pay rates."
            ),
        },
    )


async def _require_status_owned_rate_type(
    rate_type_id: int, company_id: int, db: AsyncConnection,
) -> None:
    """Legacy rate writers accept only RateTypes owned by a Status rate column.

    Ordinary PayDefinition compensation has a single authoring authority, the
    target DriverRateSchedule lifecycle, so a RateType that no active Status
    rate column of this Company uses is rejected before any write.
    """
    owned = (await db.execute(
        text("""
            SELECT 1 FROM payroll.statusratecolumns
            WHERE ratetypeid = :rtid AND companyid = :cid AND isactive = TRUE
            LIMIT 1
        """),
        {"rtid": rate_type_id, "cid": company_id},
    )).first()
    if owned is None:
        raise _ordinary_rate_authoring_retired()


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
    # Reject DRIVER/Self before resolving the target driver or branch.
    await _require_non_driver_rate_subject(company_id, user_id, db)

    can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)

    # Resolve driver â†’ branch
    drv_result = await db.execute(
        text("SELECT driverassignmentid, branchid FROM core.driverassignments WHERE driverassignmentid = :did AND companyid = :cid"),
        {"did": data.driver_assignment_id, "cid": company_id},
    )
    drv_row = drv_result.mappings().first()
    if drv_row is None:
        raise HTTPException(status_code=422, detail="driver_assignment_id does not exist in this company.")

    driver_branch_id: int = drv_row["branchid"]
    if not can_see_all and driver_branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this driver's branch.",
        )

    # Permission gate: creating a rate requires payrates.edit (or admin fallbacks)
    await check_any_permission(
        company_id, user_id, driver_branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    await _require_status_owned_rate_type(data.rate_type_id, company_id, db)

    # Validate rate type: existence, activity, and company scope (closes P0 cross-company leak).
    await _assert_rate_type_allowed_for_company(db, company_id, data.rate_type_id)

    # CP-2D2: Status-payment RateTypes are branch-scoped; reject if this branch has no
    # active StatusRateColumns row for this RateType.
    await _assert_status_rate_type_for_branch(data.rate_type_id, company_id, driver_branch_id, db)

    # When this rate type is mapped to pay items (via PayItemRateTypeMap),
    # validate that at least one of those pay items is active for this branch.
    # Legacy pay-item activation is PayItems.IsDefaultBranchActive.
    # If the rate type has no PayItemRateTypeMap entries (e.g. Fixed-behavior OVERNIGHT),
    # the branch check is skipped â€” the rate type itself is valid, just not PerUnit-mapped.
    branch_rt_result = await db.execute(
        text("""
            SELECT
                pi.payitemid,
                pi.isdefaultbranchactive
            FROM payroll.payitems pi
            JOIN payroll.payitemratetypemap pirm ON pirm.payitemid = pi.payitemid
              AND pirm.ratetypeid = :rtid AND pirm.status = 'Active'
            WHERE pi.status != 'Retired'
              AND pi.requiresrate = TRUE
              AND (pi.companyid IS NULL OR pi.companyid = :cid)
        """),
        {
            "rtid": data.rate_type_id,
            "cid": company_id,
        },
    )
    branch_rt_rows = branch_rt_result.mappings().all()
    if branch_rt_rows:
        # A RateType may map to multiple PayItems; any eligible active mapping permits it.
        if not any(
            bool(row["isdefaultbranchactive"])
            for row in branch_rt_rows
        ):
            raise HTTPException(
                status_code=422,
                detail="This rate type is not active for this driver's branch.",
            )
    # No eligible mapped PayItems (e.g. Fixed/OVERNIGHT): company validation above suffices.

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

    else:  # PerUnit, Fixed, None
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

    await lock_and_get_company_currency_for_monetary_write(company_id, db)
    await require_writable_driver_assignment(company_id, data.driver_assignment_id, db)
    insert_result = await db.execute(
        text("""
            INSERT INTO payroll.driverrates
                (companyid, branchid, driverassignmentid, ratetypeid, amount,
                 effectivefrom, effectiveto, status, createdbyuserid, notes,
                 blocksize, roundingrule)
            VALUES
                (:company_id, :branch_id, :driver_assignment_id, :rate_type_id, :amount,
                 :effective_from, :effective_to, 'PendingApproval', :created_by, :notes,
                 :block_size, :rounding_rule)
            RETURNING driverrateid
        """),
        {
            "company_id":     company_id,
            "branch_id":      driver_branch_id,
            "driver_assignment_id":      data.driver_assignment_id,
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
            "driver_assignment_id":      data.driver_assignment_id,
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

    return await rate_reads.load_rate_with_tiers(rate_id, company_id, db)


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
    rate = await rate_reads.get_rate_by_id(rate_id, company_id, user_id, db)

    if rate.status != "PendingApproval":
        raise HTTPException(
            status_code=422,
            detail=(
                f"Only PendingApproval rates can be edited "
                f"(current status: '{rate.status}')."
            ),
        )

    # Permission gate: editing a rate requires payrates.edit (or admin fallbacks)
    await check_any_permission(
        company_id, user_id, rate.branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    # Generic rate administration is available only to non-DRIVER subjects.
    await _require_non_driver_rate_subject(company_id, user_id, db)

    await _require_status_owned_rate_type(rate.rate_type_id, company_id, db)

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
    if data.amount is not None:
        fields["amount"] = data.amount
    if data.effective_from is not None:
        fields["effectivefrom"] = data.effective_from
    if data.effective_to is not None:
        fields["effectiveto"] = data.effective_to
    if data.notes is not None:
        fields["notes"] = data.notes

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
    if any((
        data.amount is not None,
        data.effective_from is not None,
        data.effective_to is not None,
        data.ordinal_tiers is not None,
        data.range_tiers is not None,
        data.block_size is not None,
        data.rounding_rule is not None,
    )):
        await lock_and_get_company_currency_for_monetary_write(company_id, db)
    await require_writable_driver_assignment(company_id, rate.driver_assignment_id, db)

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

    return await rate_reads.load_rate_with_tiers(rate_id, company_id, db)


# ---------------------------------------------------------------------------
# Shared DriverRate lifecycle helpers
# ---------------------------------------------------------------------------
# Approval and batch save (via approve_rate) share conflict and supersession
# checks.
#
# Invariants enforced together:
#   1. No future Approved rate for the same company/driver/rate_type may start
#      on or after the new effective_from (conflict guard).
#   2. Any existing Approved rate that started BEFORE effective_from is
#      superseded with EffectiveTo = effective_from - 1 day (supersession).
#   3. Supersession mutations write audit rows inside the same
#      transaction.
# ---------------------------------------------------------------------------

async def _check_no_future_approved_conflict(
    company_id: int,
    driver_assignment_id: int,
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
        "driver_assignment_id":    driver_assignment_id,
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
              AND  driverassignmentid     = :driver_assignment_id
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
    driver_assignment_id: int,
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
        "driver_assignment_id":    driver_assignment_id,
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
              AND  driverassignmentid     = :driver_assignment_id
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
    # then use the lightweight rate_reads.load_rate_summary for subsequent internal operations to
    # avoid reloading tiers unnecessarily during the approval flow.
    rate = await rate_reads.get_rate_by_id(rate_id, company_id, user_id, db)
    if rate.status != "PendingApproval":
        raise HTTPException(
            status_code=422,
            detail=(
                f"Only PendingApproval rates can be approved "
                f"(current status: '{rate.status}')."
            ),
        )

    # Step 1.3 â€” permission gate: approving a rate requires payrates.edit (or admin fallbacks)
    await check_any_permission(
        company_id, user_id, rate.branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    # Step 1.34 — generic rate administration is denied to DRIVER subjects.
    await _require_non_driver_rate_subject(company_id, user_id, db)

    await _require_status_owned_rate_type(rate.rate_type_id, company_id, db)

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
        tiers = await rate_reads.load_rate_tiers(rate_id, db)
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
    # Phase 12B fix: check_not_in_finalized_period is intentionally moved to AFTER
    # the advisory lock.  Running it before the lock creates a TOCTOU window: the
    # guard reads period status (Approved), this request waits for the lock while
    # finalization claims the period (Locked), and when the lock is finally acquired
    # the guard decision is already stale â€” the approval would proceed against a now-
    # finalized period.  By checking under the lock the guard always sees a consistent
    # view of period status.
    await lock_and_get_company_currency_for_monetary_write(company_id, db)
    # Ended DriverAssignments are historical: refuse before the Branch advisory lock so the
    # assignment row is locked before any Branch workflow lock (transfer/termination order).
    await require_writable_driver_assignment(company_id, rate.driver_assignment_id, db)
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:cid, :bid)"),
        {"cid": company_id, "bid": rate.branch_id},
    )

    # Step 1.35 â€” Backdating guard (re-run under lock for TOCTOU safety).
    # Block approval when effective_from falls inside a Locked or Archived period.
    # Must run AFTER the advisory lock is held so the period-status read is fresh.
    await check_not_in_finalized_period(company_id, rate.branch_id, rate.effective_from, db)

    # Step 1.5 â€” conflict guard (shared helper).
    # Rejects if any Approved rate starts on or after this rate's effective_from.
    # Superseding such a rate would produce EffectiveTo < EffectiveFrom (invalid)
    # and would momentarily leave two Approved rows violating the partial unique index.
    # exclude_rate_id=rate_id: exclude this PendingApproval row from the check
    # (it is in the DB but not yet Approved, so the STATUS filter already excludes
    # it â€” the exclude_rate_id is a belt-and-suspenders guard against edge cases).
    await _check_no_future_approved_conflict(
        company_id, rate.driver_assignment_id, rate.rate_type_id, rate.effective_from, db,
        exclude_rate_id=rate_id,
    )

    # Step 2 â€” supersede prior Approved rate(s) FIRST via shared helper.
    # The helper only touches rates with EffectiveFrom < effective_from.
    # Writes RATE_SUPERSEDED audit inside the same transaction.
    await _supersede_current_approved_rates(
        company_id, rate.driver_assignment_id, rate.rate_type_id, rate.effective_from,
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
    return await rate_reads.load_rate_with_tiers(rate_id, company_id, db)


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
    rate = await rate_reads.get_rate_by_id(rate_id, company_id, user_id, db)

    if rate.status == "Voided":
        raise HTTPException(
            status_code=422,
            detail="Rate is already Voided.",
        )

    # Permission gate: voiding a rate requires payrates.edit (or admin fallbacks)
    await check_any_permission(
        company_id, user_id, rate.branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    # Generic rate administration is available only to non-DRIVER subjects.
    await _require_non_driver_rate_subject(company_id, user_id, db)

    await _require_status_owned_rate_type(rate.rate_type_id, company_id, db)

    # Ended DriverAssignments are historical; lock the assignment row before any Branch
    # advisory lock below.
    await require_writable_driver_assignment(company_id, rate.driver_assignment_id, db)

    # Serialize Approved/Superseded rate voids with the branch workflow before
    # checking finalized-line references and mutating the rate. Pending rates
    # do not take this lock; the reference guard below still applies to all rates.
    if rate.status in ("Approved", "Superseded"):
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:cid, :bid)"),
            {"cid": company_id, "bid": rate.branch_id},
        )

    # Block void if this DriverRate is referenced by PayrollFinalLines.
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


# ===========================================================================
# Batch rate save (Phase 2A)
# ===========================================================================

async def batch_save_rates(
    driver_assignment_id: int,
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

    Phase 2A: only PerUnit and Fixed rate behaviors are
    supported.  Tiered (OrdinalTier, RangeBracket, RangeProgressive) and
    Block rates must be saved via individual endpoints.
    """
    # A DRIVER/Self token must be rejected before branch or rate lookup so a
    # generic payroll endpoint never treats Self as branch authority.
    await _require_non_driver_rate_subject(company_id, user_id, db)

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
    can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)

    drv_result = await db.execute(
        text("""
            SELECT driverassignmentid, branchid FROM core.driverassignments
            WHERE driverassignmentid = :did AND companyid = :cid
        """),
        {"did": driver_assignment_id, "cid": company_id},
    )
    drv_row = drv_result.mappings().first()
    if drv_row is None:
        raise HTTPException(status_code=404, detail="Driver not found.")

    driver_branch_id: int = drv_row["branchid"]
    if not can_see_all and driver_branch_id not in branch_ids:
        raise HTTPException(status_code=403, detail="Access denied to this driver's branch.")

    # Step 3 â€” permission gate
    await check_any_permission(
        company_id, user_id, driver_branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    await lock_and_get_company_currency_for_monetary_write(company_id, db)
    await require_writable_driver_assignment(company_id, driver_assignment_id, db)

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
        await check_not_in_finalized_period(
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
            # The PayItem branch of the legacy batch writer ended with the P4a cutover:
            # ordinary rates are authored only through target DriverRateSchedules.
            raise _ordinary_rate_authoring_retired()

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
                WHERE  driverassignmentid   = :did
                  AND  ratetypeid = :rtid
                  AND  companyid  = :cid
                  AND  status     = 'PendingApproval'
                ORDER  BY createdatutc DESC
                LIMIT  1
            """),
            {"did": driver_assignment_id, "rtid": change.rate_type_id, "cid": company_id},
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
                        (companyid, branchid, driverassignmentid, ratetypeid, amount,
                         effectivefrom, effectiveto, status, createdbyuserid, notes)
                    VALUES
                        (:cid, :bid, :did, :rtid, :amount,
                         :effective_from, NULL, 'PendingApproval', :uid, :notes)
                    RETURNING driverrateid
                """),
                {
                    "cid":            company_id,
                    "bid":            driver_branch_id,
                    "did":            driver_assignment_id,
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
                    "driver_assignment_id":      driver_assignment_id,
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
    rate_summaries = [await rate_reads.load_rate_with_tiers(rid, company_id, db) for rid in all_rate_ids]

    return BatchRateSaveResult(
        driver_assignment_id=driver_assignment_id,
        effective_from=data.effective_from,
        allow_self_approval=allow_self_approval,
        created_count=len(created_ids),
        updated_pending_count=len(updated_pending_ids),
        approved_count=approved_count,
        pending_count=pending_count,
        rates=rate_summaries,
    )
