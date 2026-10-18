"""Access-scoped Branch reads, projection mapping, metrics, and readiness annotations.

Readiness rules remain owned by payroll_setup.readiness; readiness errors
propagate while independent operational metrics retain their existing fallback
behavior.
"""

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.branch_access import check_branch_access
from app.access.permissions import check_permission
from app.access.policy import require_non_driver_subject
from app.business_clock import supported_time_zones
from app.db.sql import build_in_clause
from app.payroll_setup.readiness import branch_schedule_readiness, branch_schedule_readiness_detail
from app.settings.schemas import BranchAdmin
from app.workforce.projection import count_employee_branch_projections


async def _attach_readiness_reasons(
    branches: list[BranchAdmin], company_id: int, user_id: int,
    db: AsyncConnection,
) -> None:
    """Expose reason codes and evaluated dates only to non-drivers with
    payroll.view on each branch."""
    if not branches:
        return
    try:
        await require_non_driver_subject(company_id, user_id, db)
    except HTTPException as exc:
        if exc.status_code == 403:
            return
        raise
    for branch in branches:
        try:
            await check_permission(company_id, user_id, branch.branch_id, "payroll.view", db)
        except HTTPException as exc:
            if exc.status_code == 403:
                continue
            raise
        _, reason, evaluated_date = await branch_schedule_readiness_detail(
            company_id, branch.branch_id, db,
        )
        branch.schedule_readiness_reason = reason
        branch.schedule_readiness_date = evaluated_date


async def fetch_branch_metrics(
    company_id: int,
    branch_ids: list[int],
    db: AsyncConnection,
) -> dict[int, dict]:
    """
    Resolve canonical Payroll Setup readiness for each branch and run four
    operational metric queries against the given branch IDs.

    Each operational query is individually wrapped in try/except so a missing
    table or failing index on one metric does not prevent the other three from
    loading.

    Returns dict[branch_id → {metric_name: value}].  A missing key in the
    inner dict means an operational metric query failed; callers substitute
    safe defaults (0 / None). Readiness resolution failures are not suppressed.
    """
    if not branch_ids:
        return {}

    in_clause, in_params = build_in_clause(branch_ids, "bid")
    base: dict = {"cid": company_id, **in_params}
    result: dict[int, dict] = {}

    def _put(bid: int, key: str, val) -> None:
        result.setdefault(bid, {})[key] = val

    # 1 — Canonical Payroll Setup readiness; never consult legacy settings.
    for branch_id in branch_ids:
        ready, _ = await branch_schedule_readiness(company_id, branch_id, db)
        if ready:
            _put(branch_id, "payroll_setup_done", True)

    # 2 — Active status-keys count.
    try:
        r = await db.execute(
            text(f"""
                SELECT branchid, COUNT(*) AS cnt
                FROM   payroll.payrollstatuskeys
                WHERE  companyid = :cid
                  AND  isactive  = TRUE
                  AND  branchid IN ({in_clause})
                GROUP  BY branchid
            """),
            base,
        )
        for row in r.mappings().all():
            _put(row["branchid"], "status_keys_count", int(row["cnt"]))
    except Exception:
        pass

    # 3 — Total people count (core.employees, all employment types).
    try:
        counts = await count_employee_branch_projections(company_id, branch_ids, db)
        for branch_id, count in counts.items():
            _put(branch_id, "total_people_count", count)
    except Exception:
        pass

    # 4 — Current active drivers. Status can change before a scheduled instant,
    # so the assignment function remains the current-identity authority.
    try:
        r = await db.execute(
            text(f"""
                SELECT asg.branchid, COUNT(*) AS cnt
                FROM   core.driverassignments asg
                JOIN   core.drivers   d ON d.driverid = asg.driverid
                JOIN   core.employees e
                       ON  e.companyid  = asg.companyid
                       AND e.employeeid = asg.employeeid
                WHERE  asg.companyid     = :cid
                  AND  (asg.endreason IS NOT NULL OR d.driverstatus = 'Active')
                  AND  e.employmentstatus IN ('Active', 'Terminated')
                  AND  asg.driverassignmentid = core.fn_EffectiveDriverAssignment(asg.companyid, asg.driverid)
                  AND  asg.branchid IN ({in_clause})
                GROUP  BY asg.branchid
            """),
            base,
        )
        for row in r.mappings().all():
            _put(row["branchid"], "active_drivers_count", int(row["cnt"]))
    except Exception:
        pass

    # 5 — Pending review-items count.
    try:
        r = await db.execute(
            text(f"""
                SELECT branchid, COUNT(*) AS cnt
                FROM   review.managerreviewitems
                WHERE  companyid = :cid
                  AND  status    = 'Pending'
                  AND  branchid IN ({in_clause})
                GROUP  BY branchid
            """),
            base,
        )
        for row in r.mappings().all():
            _put(row["branchid"], "pending_approvals_count", int(row["cnt"]))
    except Exception:
        pass

    return result


BRANCH_COLUMNS = """
    branchid, companyid, branchcode, branchname, timezoneid,
    core.fn_BranchToday(companyid, branchid) AS businessdate,
    status, isdefault,
    addressline1, city, stateprovince, postalcode, country,
    notes, createdatutc, updatedatutc
"""


def row_to_branch_admin(row, metrics: dict) -> BranchAdmin:
    """Build a BranchAdmin schema object from a DB row and a metrics dict."""
    bid = row["branchid"]
    m = metrics.get(bid, {})
    return BranchAdmin(
        branch_id=bid,
        company_id=row["companyid"],
        branch_code=row["branchcode"],
        branch_name=row["branchname"],
        time_zone_id=row["timezoneid"],
        business_date=row["businessdate"],
        status=row["status"],
        is_default=bool(row["isdefault"]),
        address_line1=row.get("addressline1"),
        city=row.get("city"),
        state_province=row.get("stateprovince"),
        postal_code=row.get("postalcode"),
        country=row.get("country"),
        notes=row.get("notes"),
        created_at_utc=row["createdatutc"],
        updated_at_utc=row.get("updatedatutc"),
        payroll_setup_done=m.get("payroll_setup_done", False),
        status_keys_count=m.get("status_keys_count"),
        total_people_count=m.get("total_people_count"),
        active_drivers_count=m.get("active_drivers_count"),
        pending_approvals_count=m.get("pending_approvals_count"),
    )


async def get_branches(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> list[BranchAdmin]:
    """
    Return branches visible to the user, ordered default-first, with metrics.

    Company admins see all branches; branch-scoped users see only their own.
    """
    can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)

    filters = ["companyid = :cid"]
    params: dict = {"cid": company_id}

    if not can_see_all:
        if not branch_ids:
            return []
        in_clause, in_params = build_in_clause(branch_ids, "bid")
        filters.append(f"branchid IN ({in_clause})")
        params.update(in_params)

    where = " AND ".join(filters)
    result = await db.execute(
        text(f"""
            SELECT {BRANCH_COLUMNS}
            FROM   core.branches
            WHERE  {where}
            ORDER  BY isdefault DESC, status, branchname, branchcode
        """),
        params,
    )
    rows = result.mappings().all()
    if not rows:
        return []

    all_ids = [r["branchid"] for r in rows]
    metrics = await fetch_branch_metrics(company_id, all_ids, db)
    branches = [row_to_branch_admin(r, metrics) for r in rows]
    await _attach_readiness_reasons(branches, company_id, user_id, db)
    return branches


async def get_branch_by_id(
    branch_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> BranchAdmin:
    """Fetch a single branch with metrics. Enforces branch-level access."""
    can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this branch.",
        )

    result = await db.execute(
        text(f"""
            SELECT {BRANCH_COLUMNS}
            FROM   core.branches
            WHERE  branchid  = :bid
              AND  companyid = :cid
        """),
        {"bid": branch_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Branch {branch_id} not found.",
        )

    metrics = await fetch_branch_metrics(company_id, [branch_id], db)
    branch = row_to_branch_admin(row, metrics)
    await _attach_readiness_reasons([branch], company_id, user_id, db)
    return branch


async def get_supported_time_zones(company_id: int, user_id: int, db: AsyncConnection) -> list[str]:
    """Return the shared PostgreSQL zone catalog to authenticated non-drivers."""
    await require_non_driver_subject(company_id, user_id, db)
    return await supported_time_zones(db)
