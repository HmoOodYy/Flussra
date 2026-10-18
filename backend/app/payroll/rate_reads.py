"""Read-only DriverRate catalogs, summaries, matrices, and resolution."""

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.branch_access import check_branch_access
from app.access.permissions import check_any_permission
from app.db.sql import build_in_clause
from app.payroll.guards import _check_driver_read_access, _require_non_driver_rate_subject
from app.payroll.schemas import (
    DriverRateMatrix,
    DriverRatesSummary,
    DriverRateSummary,
    RateMatrixCurrentRate,
    RateMatrixGroup,
    RateTypeSummary,
    TierSummary,
)

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
        # Use load_rate_with_tiers() for detail responses.
    )


async def load_rate_summary(
    rate_id: int,
    company_id: int,
    db: AsyncConnection,
) -> DriverRateSummary:
    """Load a company-scoped summary projection; the caller owns authorization."""
    result = await db.execute(
        text(f"{_RATE_SELECT} WHERE dr.driverrateid = :rid AND dr.companyid = :cid"),
        {"rid": rate_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Driver rate not found.")
    return _rate_row_to_summary(row)


async def load_rate_with_tiers(
    rate_id: int,
    company_id: int,
    db: AsyncConnection,
) -> DriverRateSummary:
    """Load a company-scoped detailed projection; this is not an authorization entrypoint."""
    rate = await load_rate_summary(rate_id, company_id, db)
    tiers = await load_rate_tiers(rate_id, db)
    return rate.model_copy(update={"tiers": tiers})


async def load_rate_tiers(
    rate_id: int,
    db: AsyncConnection,
) -> list[TierSummary]:
    """Load tiers by rate ID for internal projections; no authorization is performed."""
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
    await _require_non_driver_rate_subject(company_id, user_id, db)
    await check_any_permission(
        company_id, user_id, branch_id,
        ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
    )

    can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)

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
        in_clause, in_params = build_in_clause(branch_ids, "rb")
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


async def get_rate_by_id(
    rate_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> DriverRateSummary:
    await _require_non_driver_rate_subject(company_id, user_id, db)
    can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)
    rate = await load_rate_with_tiers(rate_id, company_id, db)
    if not can_see_all and rate.branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this rate's branch.",
        )
    # Permission gate: reading a rate requires payrates.view or payrates.edit (or admin fallbacks)
    await check_any_permission(
        company_id, user_id, rate.branch_id,
        ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
    )
    return rate


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
      - Denies DRIVER/Self subjects from generic rate administration.
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
        await check_any_permission(
            company_id, user_id, None,
            ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
        )
        return None

    driver_branch_id: int = drv_lookup_row["branchid"]

    # Step 2 — generic permission gate on the driver's branch.
    await check_any_permission(
        company_id, user_id, driver_branch_id,
        ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
    )

    # Step 3 â€” branch-scope gate
    can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)
    if not can_see_all and driver_branch_id not in branch_ids:
        raise HTTPException(
            status_code=403,
            detail="Access denied to this driver's branch.",
        )

    # Step 4 — DRIVER/Self accounts are denied generic rate lookup.
    await _require_non_driver_rate_subject(company_id, user_id, db)

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


async def get_driver_rate_matrix(
    driver_id: int,
    company_id: int,
    user_id: int,
    as_of_date: date | None,
    db: AsyncConnection,
) -> DriverRateMatrix:
    """
    Return the Status pay rate matrix for a driver as-of a given date.

    Ordinary PayDefinition rates are not part of this legacy matrix; they are read
    through the target Compensation pay-rates endpoint. Only the Branch's active
    StatusRateColumn groups are returned until Status compensation is cut over.
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
    if as_of_date is None:
        from app.business_clock import branch_today

        as_of_date = await branch_today(company_id, branch_id, db)

    # Permission gate â€” requires payrates.view, payrates.edit, settings.manage, or setup.manage
    await check_any_permission(
        company_id=company_id,
        user_id=user_id,
        branch_id=branch_id,
        permission_codes=["payrates.view", "payrates.edit", "settings.manage", "setup.manage"],
        db=db,
    )

    # The matrix is generic rate administration and excludes DRIVER/Self accounts.
    # which rejects ambiguous overlapping active assignments rather than trusting latest row.
    await _require_non_driver_rate_subject(company_id, user_id, db)

    groups: list[RateMatrixGroup] = []

    # StatusRateColumn groups for the branch (ordinary PayDefinition rates are target-only).
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
    (PayItems.IsDefaultBranchActive).
    """
    await _check_driver_read_access(driver_id, company_id, user_id, db)

    branch_id = (await db.execute(text("""
        SELECT branchid FROM core.drivers WHERE driverid = :did AND companyid = :cid
    """), {"did": driver_id, "cid": company_id})).scalar_one()
    from app.business_clock import branch_today
    as_of = await branch_today(company_id, branch_id, db)

    # Counts from driverrates
    counts_result = await db.execute(
        text("""
            SELECT
                SUM(CASE WHEN status = 'PendingApproval' THEN 1 ELSE 0 END)
                    AS pending_count,
                SUM(CASE WHEN status = 'Approved'
                          AND effectivefrom > :as_of THEN 1 ELSE 0 END)
                    AS future_approved_count
            FROM payroll.driverrates
            WHERE driverid  = :did
              AND companyid = :cid
              AND status IN ('PendingApproval', 'Approved')
        """),
        {"did": driver_id, "cid": company_id, "as_of": as_of},
    )
    counts_row = counts_result.mappings().first()
    pending_count: int       = int(counts_row["pending_count"] or 0)
    future_approved_count: int = int(counts_row["future_approved_count"] or 0)

    # Missing required count â€” mirrors the matrix logic (PayItems.IsDefaultBranchActive).
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
            LEFT JOIN LATERAL (
                SELECT driverrateid FROM payroll.driverrates
                WHERE  driverid      = :did
                  AND  companyid     = :cid
                  AND  ratetypeid    = pirm.ratetypeid
                  AND  status        IN ('Approved', 'Superseded')
                  AND  effectivefrom <= :as_of
                  AND  (effectiveto IS NULL OR effectiveto >= :as_of)
                ORDER BY effectivefrom DESC
                LIMIT 1
            ) approved ON TRUE
            WHERE pi.status      != 'Retired'
              AND pi.requiresrate = TRUE
              AND (pi.companyid IS NULL OR pi.companyid = :cid)
              AND pi.isdefaultbranchactive = TRUE
        """),
        {"did": driver_id, "cid": company_id, "as_of": as_of},
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


async def get_bulk_driver_rates_summary(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    branch_id: int | None = None,
) -> list[DriverRatesSummary]:
    """
    Return DriverRatesSummary for every driver the caller can access.

    Security:
      - DRIVER/Self users are denied generic rate administration.
      - SpecificBranch users: only drivers in their assigned branch(es).
      - AllCompanyBranches users: all company drivers (filtered by branch_id param).

    This is designed for the left-panel badge overlay â€” it must NOT leak driver
    existence across companies.

    missing_required_count is NOT computed here (it would require N LATERAL subqueries).
    It is always None in bulk responses; callers may use the single-driver summary
    endpoint for the full count on a selected driver.
    """
    await _require_non_driver_rate_subject(company_id, user_id, db)
    await check_any_permission(
        company_id, user_id, branch_id,
        ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
    )
    can_see_all, allowed_branch_ids = await check_branch_access(company_id, user_id, db)
    drv_q_parts = ["companyid = :cid"]
    drv_params: dict = {"cid": company_id}
    if not can_see_all:
        if not allowed_branch_ids:
            return []
        in_clause, extra = build_in_clause(allowed_branch_ids, "bid")
        drv_q_parts.append(f"branchid IN ({in_clause})")
        drv_params.update(extra)
    elif branch_id is not None:
        drv_q_parts.append("branchid = :filter_bid")
        drv_params["filter_bid"] = branch_id
    where = " AND ".join(drv_q_parts)
    drv_result = await db.execute(
        text(f"SELECT driverid, branchid FROM core.drivers WHERE {where}"),
        drv_params,
    )
    accessible_rows = drv_result.mappings().all()
    accessible_driver_ids = [r["driverid"] for r in accessible_rows]

    if not accessible_driver_ids:
        return []

    in_clause2, params2 = build_in_clause(accessible_driver_ids, "did")

    counts_result = await db.execute(
        text(f"""
            SELECT
                d.driverid,
                SUM(CASE WHEN status = 'PendingApproval' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN status = 'Approved'
                          AND effectivefrom > core.fn_BranchToday(d.companyid, d.branchid, NOW()) THEN 1 ELSE 0 END) AS future_approved_count
            FROM payroll.driverrates r
            JOIN core.drivers d ON d.driverid = r.driverid AND d.companyid = r.companyid
            WHERE r.companyid = :cid
              AND r.driverid IN ({in_clause2})
              AND r.status IN ('PendingApproval', 'Approved')
            GROUP BY d.driverid, d.companyid, d.branchid
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

