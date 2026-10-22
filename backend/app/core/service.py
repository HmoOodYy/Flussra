"""Core Branch and assignment-centric Driver roster reads."""
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.branch_access import check_branch_access
from app.access.permissions import has_any_permission
from app.core.schemas import BranchSummary, DriverAssignmentRosterRow
from app.db.sql import build_in_clause

# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

async def get_branches(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> list[BranchSummary]:
    """Return branches accessible to the user, ordered default-first."""
    can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)

    if can_see_all:
        result = await db.execute(
            text("""
                SELECT b.branchid, b.branchcode, b.branchname, b.timezoneid,
                       core.fn_BranchToday(b.companyid, b.branchid) AS businessdate, b.status,
                       b.isdefault, b.city, b.stateprovince, b.country
                FROM   core.branches b
                WHERE  b.companyid = :company_id
                ORDER  BY b.isdefault DESC, b.branchname
            """),
            {"company_id": company_id},
        )
    else:
        if not branch_ids:
            return []
        in_clause, in_params = build_in_clause(branch_ids, "b")
        result = await db.execute(
            text(f"""
                SELECT b.branchid, b.branchcode, b.branchname, b.timezoneid,
                       core.fn_BranchToday(b.companyid, b.branchid) AS businessdate, b.status,
                       b.isdefault, b.city, b.stateprovince, b.country
                FROM   core.branches b
                WHERE  b.companyid = :company_id
                  AND  b.branchid  IN ({in_clause})
                ORDER  BY b.isdefault DESC, b.branchname
            """),
            {"company_id": company_id, **in_params},
        )

    return [
        BranchSummary(
            branch_id=r["branchid"],
            branch_code=r["branchcode"],
            branch_name=r["branchname"],
            time_zone_id=r["timezoneid"],
            business_date=r["businessdate"],
            status=r["status"],
            is_default=r["isdefault"],
            city=r["city"],
            state_province=r["stateprovince"],
            country=r["country"],
        )
        for r in result.mappings().all()
    ]


# Workforce owns Employee/Driver lifecycle and profile reads. Core exposes only the
# assignment-centric roster used by Pay Rates.
_ASSIGNMENT_STATES = ("current", "pending", "ended")


async def _readable_assignment_rows(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    branch_id: int | None = None,
    state: str | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """The one filter behind every assignment read: Branch scope AND ``drivers.view``.

    Returns the DriverAssignments the user may read, newest first, and whether the user can
    read Drivers in any Branch at all. Visibility is per assignment: a Branch the user cannot
    see (scope) or cannot read Drivers in (``drivers.view``) never contributes a row, so
    history is not widened.
    """
    can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)
    verdicts: dict[int, bool] = {}

    async def readable(bid: int) -> bool:
        if bid not in verdicts:
            verdicts[bid] = (can_see_all or bid in branch_ids) and await has_any_permission(
                company_id, user_id, bid, ["drivers.view"], db)
        return verdicts[bid]

    conditions = ["a.companyid = :company_id"]
    params: dict[str, Any] = {"company_id": company_id}
    if branch_id is not None:
        conditions.append("a.branchid = :branch_id")
        params["branch_id"] = branch_id
    rows = (await db.execute(
        text(f"""
            SELECT * FROM (
                SELECT a.driverassignmentid, a.driverid, a.branchid, b.branchname,
                       a.effectivefromat, a.effectivetoat, a.endreason,
                       CASE
                           WHEN a.driverassignmentid = core.fn_EffectiveDriverAssignment(a.companyid, a.driverid)
                               THEN 'current'
                           WHEN a.effectivefromat > NOW()
                                AND (a.effectivetoat IS NULL OR a.effectivetoat > a.effectivefromat)
                               THEN 'pending'
                           ELSE 'ended'
                       END AS state,
                       dr.drivernumber, dr.driverstatus, e.fullname, e.preferredname
                FROM   core.driverassignments a
                JOIN   core.branches b ON b.companyid = a.companyid AND b.branchid = a.branchid
                JOIN   core.drivers dr ON dr.driverid = a.driverid
                JOIN   core.employees e ON e.employeeid = a.employeeid
                WHERE  {" AND ".join(conditions)}
            ) assignments
            WHERE (CAST(:state AS TEXT) IS NULL OR state = CAST(:state AS TEXT))
            ORDER  BY fullname, driverid, effectivefromat DESC, driverassignmentid DESC
        """),
        {**params, "state": state},
    )).mappings().all()
    visible = [dict(row) for row in rows if await readable(row["branchid"])]
    if visible:
        return visible, True
    if can_see_all:
        any_branch = await has_any_permission(company_id, user_id, None, ["drivers.view"], db)
    else:
        any_branch = any([await readable(bid) for bid in branch_ids])
    return [], bool(any_branch)


async def list_driver_assignments(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    branch_id: int | None = None,
    state: str | None = None,
) -> list[DriverAssignmentRosterRow]:
    """One row per DriverAssignment the user may read, with the permanent Driver's identity.

    Assignment-centric roster (Pay Rates): the same Branch scope and ``drivers.view`` filter
    as a Driver's own assignment list, so an old-Branch manager keeps the Driver's ended
    assignment and never sees the destination Branch's. A user who can read Drivers in no
    Branch at all is denied, like the other Core driver reads.
    """
    if state is not None and state not in _ASSIGNMENT_STATES:
        raise HTTPException(status_code=422, detail="state must be current, pending or ended.")
    rows, reads_any_branch = await _readable_assignment_rows(
        company_id, user_id, db, branch_id=branch_id, state=state)
    if not rows and not reads_any_branch:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view Drivers.",
        )
    return [
        DriverAssignmentRosterRow(
            driver_assignment_id=row["driverassignmentid"],
            driver_id=row["driverid"],
            branch_id=row["branchid"],
            branch_name=row["branchname"],
            effective_from_at=row["effectivefromat"],
            effective_to_at=row["effectivetoat"],
            end_reason=row["endreason"],
            state=row["state"],
            driver_number=row["drivernumber"],
            driver_status=row["driverstatus"],
            full_name=row["fullname"],
            preferred_name=row["preferredname"],
        )
        for row in rows
    ]
