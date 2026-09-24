"""
Core domain service — branches, people, drivers.

All SQL is raw parameterised via sqlalchemy.text().
Branch-access enforcement happens at the top of every query function.
"""
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

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
    """Raise HTTP 403 if the user holds any driver-role assignment.

    Blocks any active assignment that meets at least one condition:
      - scopetype = 'OwnDriverDataOnly'  (catches all ODA rows, including
        legacy rows where CompanyRoleID IS NULL)
      - company role has rolecode = 'DRIVER'
      - legacy global role has rolecode = 'DRIVER'

    Uses LEFT JOINs so rows where CompanyRoleID or RoleID is NULL are still
    evaluated against the scopetype condition.  Fails closed: one matching
    row is enough to block.

    Call this at the top of every service function that touches operational
    payroll data (Current Payroll, Review, Ledger) before any data is read.
    """
    result = await db.execute(
        text("""
            SELECT 1
            FROM   sec.userbranchroles ubr
            LEFT JOIN sec.companyroles cr ON cr.companyroleid = ubr.companyroleid
            LEFT JOIN sec.roles        r  ON r.roleid         = ubr.roleid
            WHERE  ubr.userid    = :uid
              AND  ubr.companyid = :cid
              AND  ubr.isactive  = TRUE
              AND (
                    ubr.scopetype = 'OwnDriverDataOnly'
                 OR cr.rolecode  = 'DRIVER'
                 OR r.rolecode   = 'DRIVER'
              )
            LIMIT 1
        """),
        {"uid": user_id, "cid": company_id},
    )
    if result.first() is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This operational area is not accessible to driver-role users. "
                "Driver pay data will be available through a future Driver Screen."
            ),
        )


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
    employee_type: str | None = None,
    employment_status: str | None = None,
    q: str | None = None,
) -> list[PersonSummary]:
    """
    Return all employees joined with their driver record (if any).
    Filters: branch_id, employee_type, employment_status, free-text q.
    """
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    conditions: list[str] = ["e.companyid = :company_id"]
    params: dict[str, Any] = {"company_id": company_id}

    if branch_id is not None:
        if not can_see_all and branch_id not in branch_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to the requested branch.",
            )
        conditions.append("e.branchid = :branch_id")
        params["branch_id"] = branch_id
    elif not can_see_all:
        if not branch_ids:
            return []
        in_clause, in_params = _build_in_clause(branch_ids, "eb")
        conditions.append(f"e.branchid IN ({in_clause})")
        params.update(in_params)

    if employee_type:
        conditions.append("e.employeetype = :employee_type")
        params["employee_type"] = employee_type

    if employment_status:
        conditions.append("e.employmentstatus = :employment_status")
        params["employment_status"] = employment_status

    if q:
        conditions.append(
            "(LOWER(e.fullname) LIKE :q"
            " OR LOWER(COALESCE(e.email, '')) LIKE :q"
            " OR LOWER(COALESCE(e.employeekey, '')) LIKE :q)"
        )
        params["q"] = f"%{q.lower()}%"

    where = " AND ".join(conditions)

    result = await db.execute(
        text(f"""
            SELECT
                e.employeeid,
                e.branchid,
                b.branchname,
                e.employeekey,
                e.fullname,
                e.preferredname,
                e.employeetype,
                e.employmentstatus,
                e.email,
                e.primaryphone,
                e.hiredate,
                d.driverid,
                d.drivercode,
                d.driverstatus,
                d.cdlnumber
            FROM   core.employees e
            JOIN   core.branches  b ON b.branchid  = e.branchid
            -- One active driver profile per employee; excludes Transferred/Terminated
            -- so a completed transfer does not produce a duplicate person row.
            LEFT JOIN LATERAL (
                SELECT driverid, drivercode, driverstatus, cdlnumber
                FROM   core.drivers
                WHERE  employeeid = e.employeeid
                  AND  companyid  = :company_id
                  AND  driverstatus NOT IN ('Transferred', 'Terminated')
                ORDER  BY driverid DESC
                LIMIT  1
            ) d ON true
            WHERE  {where}
            ORDER  BY e.fullname
        """),
        params,
    )

    return [
        PersonSummary(
            employee_id=r["employeeid"],
            branch_id=r["branchid"],
            branch_name=r["branchname"],
            employee_key=r["employeekey"],
            full_name=r["fullname"],
            preferred_name=r["preferredname"],
            employee_type=r["employeetype"],
            employment_status=r["employmentstatus"],
            email=r["email"],
            primary_phone=r["primaryphone"],
            hire_date=r["hiredate"],
            driver_id=r["driverid"],
            driver_code=r["drivercode"],
            driver_status=r["driverstatus"],
            cdl_number=r["cdlnumber"],
        )
        for r in result.mappings().all()
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
# Driver mutations
# ---------------------------------------------------------------------------

async def create_driver(
    company_id: int,
    user_id: int,
    data: DriverCreate,
    db: AsyncConnection,
) -> DriverSummary:
    """
    Create a new driver:
      1. Verify branch access
      2. Verify branch belongs to this company
      3. INSERT into core.employees (type = Driver, status = Active)
      4. INSERT into core.drivers
      5. Return the full DriverSummary
    """
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    if not can_see_all and data.branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to the target branch.",
        )

    # Permission gate: branch access is necessary but not sufficient.
    # Creating a driver is a write operation that requires drivers.manage.
    await _check_permission(company_id, user_id, data.branch_id, "drivers.manage", db)

    # Verify branch exists in this company
    br_result = await db.execute(
        text(
            "SELECT branchid FROM core.branches "
            "WHERE branchid = :bid AND companyid = :cid"
        ),
        {"bid": data.branch_id, "cid": company_id},
    )
    if br_result.first() is None:
        raise HTTPException(
            status_code=422,
            detail="branch_id does not exist in this company.",
        )

    # Insert employee
    emp_result = await db.execute(
        text("""
            INSERT INTO core.employees
                (companyid, branchid, employeekey, fullname, preferredname,
                 employeetype, employmentstatus, email, primaryphone, hiredate,
                 createdbyuserid)
            VALUES
                (:company_id, :branch_id, :employee_key, :full_name, :preferred_name,
                 'Driver', 'Active', :email, :primary_phone, :hire_date,
                 :created_by)
            RETURNING employeeid
        """),
        {
            "company_id": company_id,
            "branch_id": data.branch_id,
            "employee_key": data.employee_key,
            "full_name": data.full_name,
            "preferred_name": data.preferred_name,
            "email": data.email,
            "primary_phone": data.primary_phone,
            "hire_date": data.hire_date,
            "created_by": user_id,
        },
    )
    employee_id: int = emp_result.scalar_one()

    # Insert driver
    drv_result = await db.execute(
        text("""
            INSERT INTO core.drivers
                (companyid, branchid, employeeid, drivercode, cdlnumber,
                 externaldriverid, driverstatus)
            VALUES
                (:company_id, :branch_id, :employee_id, :driver_code, :cdl_number,
                 :external_driver_id, 'Active')
            RETURNING driverid
        """),
        {
            "company_id": company_id,
            "branch_id": data.branch_id,
            "employee_id": employee_id,
            "driver_code": data.driver_code,
            "cdl_number": data.cdl_number,
            "external_driver_id": data.external_driver_id,
        },
    )
    driver_id: int = drv_result.scalar_one()

    return await get_driver_by_id(company_id, user_id, driver_id, db)


async def update_driver(
    company_id: int,
    user_id: int,
    driver_id: int,
    data: DriverUpdate,
    db: AsyncConnection,
) -> DriverSummary:
    """
    Partially update a driver (only supplied non-None fields are touched).
    Branch access is confirmed by get_driver_by_id; permission is checked
    explicitly afterward.
    """
    # Raises 404 / 403 if not found or no access
    existing = await get_driver_by_id(company_id, user_id, driver_id, db)

    # Permission gate: branch access is necessary but not sufficient.
    await _check_permission(company_id, user_id, existing.branch_id, "drivers.manage", db)

    # --- Update core.employees ---
    emp_fields: dict[str, Any] = {}
    if data.full_name is not None:
        emp_fields["fullname"] = data.full_name
    if data.preferred_name is not None:
        emp_fields["preferredname"] = data.preferred_name
    if data.email is not None:
        emp_fields["email"] = data.email
    if data.primary_phone is not None:
        emp_fields["primaryphone"] = data.primary_phone
    if data.employment_status is not None:
        emp_fields["employmentstatus"] = data.employment_status
    # Use model_fields_set so callers can explicitly clear termination_date by
    # sending {"termination_date": null}.  The old `is not None` check silently
    # ignored None, making it impossible to remove a previously-set date.
    if "termination_date" in data.model_fields_set:
        emp_fields["terminationdate"] = data.termination_date

    if emp_fields:
        set_clause = ", ".join(f"{col} = :{col}" for col in emp_fields)
        await db.execute(
            text(
                f"UPDATE core.employees SET {set_clause}, updatedatutc = NOW() "
                f"WHERE employeeid = :employee_id"
            ),
            {**emp_fields, "employee_id": existing.employee_id},
        )

    # --- Update core.drivers ---
    drv_fields: dict[str, Any] = {}
    if data.driver_code is not None:
        drv_fields["drivercode"] = data.driver_code
    if data.cdl_number is not None:
        drv_fields["cdlnumber"] = data.cdl_number
    if data.external_driver_id is not None:
        drv_fields["externaldriverid"] = data.external_driver_id
    if data.driver_status is not None:
        drv_fields["driverstatus"] = data.driver_status

    if drv_fields:
        set_clause = ", ".join(f"{col} = :{col}" for col in drv_fields)
        await db.execute(
            text(
                f"UPDATE core.drivers SET {set_clause}, updatedatutc = NOW() "
                f"WHERE driverid = :driver_id"
            ),
            {**drv_fields, "driver_id": driver_id},
        )

    return await get_driver_by_id(company_id, user_id, driver_id, db)
