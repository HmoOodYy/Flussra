"""
Core domain service — branches, people, drivers.

All SQL is raw parameterised via sqlalchemy.text().
Branch-access enforcement happens at the top of every query function.
"""
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.policy import is_driver_self_subject, require_non_driver_subject
from app.core.schemas import (
    BranchSummary,
    DriverCreate,
    DriverSummary,
    DriverUpdate,
    PersonSummary,
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _check_branch_access(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> tuple[bool, list[int]]:
    """
    Query the user's branch access for this company.

    Returns:
        (can_see_all, branch_ids) where:
            can_see_all = True  → user has AllCompanyBranches scope
            branch_ids         → list of specific branch IDs accessible
                                 (empty when can_see_all is True — not needed)

    Raises 403 if the user has no active access to this company at all,
    or if the account is disabled / the company is suspended.

    Stale-token safety: the view join includes sec.Users and core.Companies,
    so a deactivated user, a user with CanLogin=FALSE, a revoked role
    assignment, or a suspended company will all return 0 rows here,
    causing an immediate 403 regardless of token validity.
    """
    result = await db.execute(
        text("""
            SELECT v.branchid, v.scopetype
            FROM   app.vw_userbranchaccess v
            WHERE  v.userid              = :user_id
              AND  v.companyid           = :company_id
              AND  v.userisactive        = TRUE
              AND  v.canlogin            = TRUE
              AND  v.accessisactive      = TRUE
              AND  v.companystatus       = 'Active'
              AND  v.companyissuspended  = FALSE
        """),
        {"user_id": user_id, "company_id": company_id},
    )
    rows = result.mappings().all()

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No branch access for this company.",
        )

    can_see_all = any(r["scopetype"] == "AllCompanyBranches" for r in rows)
    branch_ids = (
        []
        if can_see_all
        else [r["branchid"] for r in rows if r["branchid"] is not None]
    )
    return can_see_all, branch_ids


async def _check_permission(
    company_id: int,
    user_id: int,
    branch_id: int | None,
    permission_code: str,
    db: AsyncConnection,
) -> None:
    """
    Raise HTTP 403 if the user does not hold *permission_code* for the
    given company + branch.

    Delegates to ``sec.fn_UserHasPermission`` which resolves both
    ``SpecificBranch`` and ``AllCompanyBranches`` scope transparently.

    branch_id may be None for company-level operations (settings, admin
    writes).  Passing None means only AllCompanyBranches assignments will
    match — SpecificBranch users are correctly denied.

    This is a second security gate applied AFTER branch-access has been
    confirmed.  Branch access (``_check_branch_access``) answers "can the
    user see data in this branch?" — this function answers "is the user
    allowed to perform this specific action?"
    """
    await require_non_driver_subject(company_id, user_id, db)
    result = await db.execute(
        text("SELECT sec.fn_UserHasPermission(:uid, :cid, :bid, :perm)"),
        {
            "uid":  user_id,
            "cid":  company_id,
            "bid":  branch_id,
            "perm": permission_code,
        },
    )
    if not result.scalar_one():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You do not have permission to perform this action "
                f"(required: {permission_code})."
            ),
        )


async def _require_not_driver_role(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> None:
    """Compatibility wrapper for the Access-owned DRIVER capability ceiling."""
    await require_non_driver_subject(company_id, user_id, db)

async def _has_any_permission(
    company_id: int,
    user_id: int,
    branch_id: int | None,
    permission_codes: list[str],
    db: AsyncConnection,
) -> bool:
    """Return True if the user holds at least one of the given permission codes; False otherwise.

    Unlike _check_any_permission this never raises — callers decide what to do.
    """
    if await is_driver_self_subject(company_id, user_id, db):
        return False
    for code in permission_codes:
        result = await db.execute(
            text("SELECT sec.fn_UserHasPermission(:uid, :cid, :bid, :perm)"),
            {"uid": user_id, "cid": company_id, "bid": branch_id, "perm": code},
        )
        if result.scalar_one():
            return True
    return False


async def _check_any_permission(
    company_id: int,
    user_id: int,
    branch_id: int | None,
    permission_codes: list[str],
    db: AsyncConnection,
) -> None:
    """
    Raise HTTP 403 if the user holds NONE of the given permission codes.

    Accepts a list and passes when the user has at least one.
    Use this when an action is reachable via multiple roles
    (e.g. payrates.view OR payrates.edit OR settings.manage).
    """
    await require_non_driver_subject(company_id, user_id, db)
    for code in permission_codes:
        result = await db.execute(
            text("SELECT sec.fn_UserHasPermission(:uid, :cid, :bid, :perm)"),
            {"uid": user_id, "cid": company_id, "bid": branch_id, "perm": code},
        )
        if result.scalar_one():
            return  # user has at least one — grant access

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "You do not have permission to perform this action "
            f"(required one of: {', '.join(permission_codes)})."
        ),
    )


def _build_in_clause(ids: list[int], prefix: str) -> tuple[str, dict[str, int]]:
    """
    Build a SQL IN clause fragment and matching params dict for sqlalchemy.text().

    Example:
        _build_in_clause([1, 2, 3], "b")
        → (":b0, :b1, :b2", {"b0": 1, "b1": 2, "b2": 3})
    """
    keys = [f"{prefix}{i}" for i in range(len(ids))]
    clause = ", ".join(f":{k}" for k in keys)
    params = dict(zip(keys, ids))
    return clause, params


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

async def get_branches(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> list[BranchSummary]:
    """Return branches accessible to the user, ordered default-first."""
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    if can_see_all:
        result = await db.execute(
            text("""
                SELECT b.branchid, b.branchcode, b.branchname, b.status,
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
        in_clause, in_params = _build_in_clause(branch_ids, "b")
        result = await db.execute(
            text(f"""
                SELECT b.branchid, b.branchcode, b.branchname, b.status,
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
            status=r["status"],
            is_default=r["isdefault"],
            city=r["city"],
            state_province=r["stateprovince"],
            country=r["country"],
        )
        for r in result.mappings().all()
    ]


# ---------------------------------------------------------------------------
# People (unified employees + drivers view)
# ---------------------------------------------------------------------------

async def get_people(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    branch_id: int | None = None,
    driver_state: str | None = None,
    employment_status: str | None = None,
    q: str | None = None,
) -> list[PersonSummary]:
    """Compatibility read over the canonical Workforce Employee read model."""
    from app.workforce.service import list_employees

    employees = await list_employees(
        company_id,
        user_id,
        db,
        branch_id=branch_id,
        employment_status=employment_status,
        driver_state=driver_state,
        q=q,
    )
    return [
        PersonSummary(
            employee_id=employee.employee_id,
            branch_id=employee.branch_id,
            branch_name=employee.branch_name,
            employee_key=employee.employee_key,
            full_name=employee.full_name,
            preferred_name=employee.preferred_name,
            driver_state=employee.driver_state,
            employment_status=employee.employment_status,
            email=employee.email,
            primary_phone=employee.primary_phone,
            hire_date=employee.hire_date,
            driver_id=employee.current_or_pending_driver.driver_id if employee.current_or_pending_driver else None,
            driver_code=employee.current_or_pending_driver.driver_code if employee.current_or_pending_driver else None,
            driver_status=employee.current_or_pending_driver.driver_status if employee.current_or_pending_driver else None,
            cdl_number=employee.current_or_pending_driver.cdl_number if employee.current_or_pending_driver else None,
        )
        for employee in employees
    ]


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------

async def get_drivers(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    branch_id: int | None = None,
    driver_status: str | None = None,
    q: str | None = None,
) -> list[DriverSummary]:
    """Return drivers in scope with optional filters."""
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    if branch_id is not None:
        await _check_permission(company_id, user_id, branch_id, "drivers.view", db)
    elif can_see_all:
        await _check_permission(company_id, user_id, None, "drivers.view", db)
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
            raise HTTPException(status_code=403, detail="You do not have permission to view Driver profiles.")
        branch_ids = permitted

    conditions: list[str] = ["d.companyid = :company_id"]
    params: dict[str, Any] = {"company_id": company_id}

    if branch_id is not None:
        if not can_see_all and branch_id not in branch_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to the requested branch.",
            )
        conditions.append("d.branchid = :branch_id")
        params["branch_id"] = branch_id
    elif not can_see_all:
        if not branch_ids:
            return []
        in_clause, in_params = _build_in_clause(branch_ids, "db")
        conditions.append(f"d.branchid IN ({in_clause})")
        params.update(in_params)

    if driver_status:
        conditions.append("d.driverstatus = :driver_status")
        params["driver_status"] = driver_status

    if q:
        conditions.append(
            "(LOWER(e.fullname) LIKE :q"
            " OR LOWER(COALESCE(d.drivercode, '')) LIKE :q"
            " OR LOWER(COALESCE(e.email, '')) LIKE :q)"
        )
        params["q"] = f"%{q.lower()}%"

    where = " AND ".join(conditions)

    result = await db.execute(
        text(f"""
            SELECT
                d.driverid,
                e.employeeid,
                d.branchid,
                b.branchname,
                e.fullname,
                e.preferredname,
                e.employeekey,
                d.drivercode,
                d.cdlnumber,
                d.externaldriverid,
                d.driverstatus,
                e.employmentstatus,
                e.email,
                e.primaryphone,
                e.hiredate,
                e.terminationdate
            FROM   core.drivers   d
            JOIN   core.employees e ON e.employeeid = d.employeeid
            JOIN   core.branches  b ON b.branchid   = d.branchid
            WHERE  {where}
            ORDER  BY e.fullname
        """),
        params,
    )

    return [
        DriverSummary(
            driver_id=r["driverid"],
            employee_id=r["employeeid"],
            branch_id=r["branchid"],
            branch_name=r["branchname"],
            full_name=r["fullname"],
            preferred_name=r["preferredname"],
            employee_key=r["employeekey"],
            driver_code=r["drivercode"],
            cdl_number=r["cdlnumber"],
            external_driver_id=r["externaldriverid"],
            driver_status=r["driverstatus"],
            employment_status=r["employmentstatus"],
            email=r["email"],
            primary_phone=r["primaryphone"],
            hire_date=r["hiredate"],
            termination_date=r["terminationdate"],
        )
        for r in result.mappings().all()
    ]


async def get_driver_by_id(
    company_id: int,
    user_id: int,
    driver_id: int,
    db: AsyncConnection,
) -> DriverSummary:
    """Fetch a single driver; enforces branch access after the DB lookup."""
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    result = await db.execute(
        text("""
            SELECT
                d.driverid,
                e.employeeid,
                d.branchid,
                b.branchname,
                e.fullname,
                e.preferredname,
                e.employeekey,
                d.drivercode,
                d.cdlnumber,
                d.externaldriverid,
                d.driverstatus,
                e.employmentstatus,
                e.email,
                e.primaryphone,
                e.hiredate,
                e.terminationdate
            FROM   core.drivers   d
            JOIN   core.employees e ON e.employeeid = d.employeeid
            JOIN   core.branches  b ON b.branchid   = d.branchid
            WHERE  d.driverid  = :driver_id
              AND  d.companyid = :company_id
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

    await _check_permission(company_id, user_id, row["branchid"], "drivers.view", db)

    return DriverSummary(
        driver_id=row["driverid"],
        employee_id=row["employeeid"],
        branch_id=row["branchid"],
        branch_name=row["branchname"],
        full_name=row["fullname"],
        preferred_name=row["preferredname"],
        employee_key=row["employeekey"],
        driver_code=row["drivercode"],
        cdl_number=row["cdlnumber"],
        external_driver_id=row["externaldriverid"],
        driver_status=row["driverstatus"],
        employment_status=row["employmentstatus"],
        email=row["email"],
        primary_phone=row["primaryphone"],
        hire_date=row["hiredate"],
        termination_date=row["terminationdate"],
    )


# ---------------------------------------------------------------------------
# Driver compatibility mutations: all business writes delegate to Workforce.

async def create_driver(
    company_id: int,
    user_id: int,
    data: DriverCreate,
    db: AsyncConnection,
) -> DriverSummary:
    """Compatibility alias to canonical Workforce Employee/profile creation."""
    from app.workforce.schemas import DriverProfileCreate, EmployeeCreate
    from app.workforce.service import create_employee

    employee = await create_employee(
        company_id,
        user_id,
        EmployeeCreate(
            branch_id=data.branch_id,
            employee_key=data.employee_key,
            full_name=data.full_name,
            preferred_name=data.preferred_name,
            email=data.email,
            primary_phone=data.primary_phone,
            hire_date=data.hire_date,
            driver_profile=DriverProfileCreate(
                driver_code=data.driver_code,
                cdl_number=data.cdl_number,
                external_driver_id=data.external_driver_id,
            ),
        ),
        db,
    )
    profile = employee.current_or_pending_driver
    if profile is None:
        raise HTTPException(status_code=500, detail="Workforce creation did not return its Driver profile.")
    return DriverSummary(
        driver_id=profile.driver_id,
        employee_id=employee.employee_id,
        branch_id=profile.branch_id,
        branch_name=profile.branch_name,
        full_name=employee.full_name,
        preferred_name=employee.preferred_name,
        employee_key=employee.employee_key,
        driver_code=profile.driver_code,
        cdl_number=profile.cdl_number,
        external_driver_id=profile.external_driver_id,
        driver_status=profile.driver_status,
        employment_status=employee.employment_status,
        email=employee.email,
        primary_phone=employee.primary_phone,
        hire_date=employee.hire_date,
        termination_date=employee.termination_date,
    )


async def update_driver(
    company_id: int,
    user_id: int,
    driver_id: int,
    data: DriverUpdate,
    db: AsyncConnection,
) -> DriverSummary:
    """Compatibility alias to canonical Workforce Driver-profile update."""
    from app.workforce.schemas import DriverProfileUpdate
    from app.workforce.service import update_driver_profile

    profile = await update_driver_profile(
        company_id,
        user_id,
        driver_id,
        DriverProfileUpdate(**data.model_dump(exclude_unset=True)),
        db,
    )
    return DriverSummary(
        driver_id=profile["driverid"],
        employee_id=profile["employeeid"],
        branch_id=profile["branchid"],
        branch_name=profile["branchname"],
        full_name=profile["fullname"],
        preferred_name=profile["preferredname"],
        employee_key=profile["employeekey"],
        driver_code=profile["drivercode"],
        cdl_number=profile["cdlnumber"],
        external_driver_id=profile["externaldriverid"],
        driver_status=profile["driverstatus"],
        employment_status=profile["employmentstatus"],
        email=profile["email"],
        primary_phone=profile["primaryphone"],
        hire_date=profile["hiredate"],
        termination_date=profile["terminationdate"],
    )
