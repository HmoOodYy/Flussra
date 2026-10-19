"""Core Branch and permanent-Driver compatibility reads.

People and Driver lifecycle adapters live in ``workforce_adapters``.

All SQL is raw parameterised via sqlalchemy.text().
Branch-access enforcement happens at the top of every query function.
"""
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.branch_access import check_branch_access
from app.access.permissions import check_permission, has_any_permission
from app.core.schemas import (
    BranchSummary,
    DriverAssignmentRosterRow,
    DriverAssignmentSummary,
    DriverSummary,
)

# Core routes use workforce_adapters directly; retain these service imports for compatibility.
from app.core.workforce_adapters import create_driver as create_driver
from app.core.workforce_adapters import get_people as get_people
from app.core.workforce_adapters import update_driver as update_driver
from app.db.sql import build_in_clause
from app.workforce.effective import VISIBLE_ASSIGNMENT_JOIN

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


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------

_DRIVER_COLUMNS = """
                dr.driverid,
                e.employeeid,
                va.driverassignmentid,
                va.branchid,
                va.branchname,
                e.fullname,
                e.preferredname,
                e.employeekey,
                dr.drivernumber,
                dr.cdlnumber,
                dr.driverstatus,
                e.employmentstatus,
                e.email,
                e.primaryphone,
                e.hiredate,
                e.terminationdate
"""


def _driver_summary_from_row(r: Any) -> DriverSummary:
    return DriverSummary(
        driver_id=r["driverid"],
        employee_id=r["employeeid"],
        driver_assignment_id=r["driverassignmentid"],
        branch_id=r["branchid"],
        branch_name=r["branchname"],
        full_name=r["fullname"],
        preferred_name=r["preferredname"],
        employee_key=r["employeekey"],
        driver_number=r["drivernumber"],
        cdl_number=r["cdlnumber"],
        driver_status=r["driverstatus"],
        employment_status=r["employmentstatus"],
        email=r["email"],
        primary_phone=r["primaryphone"],
        hire_date=r["hiredate"],
        termination_date=r["terminationdate"],
    )


async def get_drivers(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    branch_id: int | None = None,
    driver_status: str | None = None,
    q: str | None = None,
) -> list[DriverSummary]:
    """Return permanent Drivers in scope; each item shows its current (else pending, else latest) Branch."""
    can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)

    if branch_id is not None:
        await check_permission(company_id, user_id, branch_id, "drivers.view", db)
    elif can_see_all:
        await check_permission(company_id, user_id, None, "drivers.view", db)
    else:
        permitted: list[int] = []
        for bid in branch_ids:
            result = await db.execute(
                text("SELECT sec.fn_UserHasPermission(:uid, :cid, :bid, 'drivers.view')"),
                {"uid": user_id, "cid": company_id, "bid": bid},
            )
            if result.scalar_one():
                permitted.append(bid)
        if not permitted:
            raise HTTPException(status_code=403, detail="You do not have permission to view Drivers.")
        branch_ids = permitted

    conditions: list[str] = ["dr.companyid = :company_id"]
    params: dict[str, Any] = {"company_id": company_id}

    if branch_id is not None:
        if not can_see_all and branch_id not in branch_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to the requested branch.",
            )
        conditions.append("va.branchid = :branch_id")
        params["branch_id"] = branch_id
    elif not can_see_all:
        if not branch_ids:
            return []
        in_clause, in_params = build_in_clause(branch_ids, "db")
        conditions.append(f"va.branchid IN ({in_clause})")
        params.update(in_params)

    if driver_status:
        conditions.append("dr.driverstatus = :driver_status")
        params["driver_status"] = driver_status

    if q:
        conditions.append(
            "(LOWER(e.fullname) LIKE :q"
            " OR LOWER(COALESCE(dr.drivernumber, '')) LIKE :q"
            " OR LOWER(COALESCE(e.email, '')) LIKE :q)"
        )
        params["q"] = f"%{q.lower()}%"

    where = " AND ".join(conditions)

    result = await db.execute(
        text(f"""
            SELECT {_DRIVER_COLUMNS}
            FROM   core.drivers   dr
            JOIN   core.employees e ON e.employeeid = dr.employeeid
            {VISIBLE_ASSIGNMENT_JOIN}
            WHERE  {where}
            ORDER  BY e.fullname, dr.driverid
        """),
        params,
    )

    return [_driver_summary_from_row(r) for r in result.mappings().all()]


_ASSIGNMENT_STATES = ("current", "pending", "ended")


async def _readable_assignment_rows(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    driver_id: int | None = None,
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
    if driver_id is not None:
        conditions.append("a.driverid = :driver_id")
        params["driver_id"] = driver_id
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


def _assignment_summary(row: dict[str, Any]) -> DriverAssignmentSummary:
    return DriverAssignmentSummary(
        driver_assignment_id=row["driverassignmentid"],
        driver_id=row["driverid"],
        branch_id=row["branchid"],
        branch_name=row["branchname"],
        effective_from_at=row["effectivefromat"],
        effective_to_at=row["effectivetoat"],
        end_reason=row["endreason"],
        state=row["state"],
    )


async def get_driver_assignments(
    company_id: int,
    user_id: int,
    driver_id: int,
    db: AsyncConnection,
) -> list[DriverAssignmentSummary]:
    """List a Driver's assignments, newest first, limited to Branches the user may read."""
    exists = await db.execute(
        text("SELECT 1 FROM core.drivers WHERE driverid = :driver_id AND companyid = :company_id"),
        {"driver_id": driver_id, "company_id": company_id},
    )
    if exists.first() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Driver not found.")
    rows, _ = await _readable_assignment_rows(company_id, user_id, db, driver_id=driver_id)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this driver's assignments.",
        )
    return [_assignment_summary(row) for row in rows]


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
            **_assignment_summary(row).model_dump(),
            driver_number=row["drivernumber"],
            driver_status=row["driverstatus"],
            full_name=row["fullname"],
            preferred_name=row["preferredname"],
        )
        for row in rows
    ]


async def get_driver_by_id(
    company_id: int,
    user_id: int,
    driver_id: int,
    db: AsyncConnection,
) -> DriverSummary:
    """Fetch a single permanent Driver; enforces branch access after the DB lookup."""
    can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)

    result = await db.execute(
        text(f"""
            SELECT {_DRIVER_COLUMNS}
            FROM   core.drivers   dr
            JOIN   core.employees e ON e.employeeid = dr.employeeid
            {VISIBLE_ASSIGNMENT_JOIN}
            WHERE  dr.driverid  = :driver_id
              AND  dr.companyid = :company_id
        """),
        {"driver_id": driver_id, "company_id": company_id},
    )
    row = result.mappings().first()

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Driver not found.")

    if not can_see_all and row["branchid"] not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this driver's branch.",
        )

    await check_permission(company_id, user_id, row["branchid"], "drivers.view", db)

    return _driver_summary_from_row(row)
