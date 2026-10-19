"""Canonical non-transfer Employee, Driver and DriverAssignment mutation owner."""

from datetime import date, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.branch_access import check_branch_access
from app.access.permissions import check_permission
from app.workforce import audit as workforce_audit
from app.workforce.effective import (
    VISIBLE_ASSIGNMENT_JOIN,
    resolve_effective_employee_assignment,
)
from app.workforce.ops import decision_instant, workforce_operation
from app.workforce.projection import (
    sync_company_employee_branch_projections,
    sync_employee_branch_projection,
)
from app.workforce.schemas import (
    DriverAssignmentSummary,
    DriverCreate,
    DriverSummary,
    DriverUpdate,
    EmployeeCreate,
    EmployeeDetail,
    EmployeeSummary,
    EmployeeTermination,
    EmployeeTerminationResult,
    EmployeeUpdate,
    UserSummary,
)


async def _require_branch_write(
    company_id: int,
    user_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> None:
    can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise HTTPException(status_code=403, detail="Access denied to the target branch.")
    result = await db.execute(
        text("SELECT 1 FROM core.Branches WHERE BranchID = :bid AND CompanyID = :cid"),
        {"bid": branch_id, "cid": company_id},
    )
    if result.first() is None:
        raise HTTPException(status_code=422, detail="Branch does not belong to this company.")
    await check_permission(company_id, user_id, branch_id, "employees.manage", db)


async def _readable_employee_branches(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    branch_id: int | None,
) -> tuple[bool, list[int]]:
    can_see_all, accessible = await check_branch_access(company_id, user_id, db)
    if branch_id is not None:
        if not can_see_all and branch_id not in accessible:
            raise HTTPException(status_code=403, detail="Access denied to the requested branch.")
        await check_permission(company_id, user_id, branch_id, "employees.view", db)
        return can_see_all, [branch_id]

    if can_see_all:
        await check_permission(company_id, user_id, None, "employees.view", db)
        return True, []

    permitted: list[int] = []
    for bid in accessible:
        allowed = await db.execute(
            text("SELECT sec.fn_UserHasPermission(:uid, :cid, :bid, 'employees.view')"),
            {"uid": user_id, "cid": company_id, "bid": bid},
        )
        if allowed.scalar_one():
            permitted.append(bid)
    if not permitted:
        raise HTTPException(status_code=403, detail="You do not have permission to view Employees.")
    return False, permitted


def _assignment_summary(row: dict[str, Any]) -> DriverAssignmentSummary:
    return DriverAssignmentSummary(
        driver_assignment_id=row["driverassignmentid"],
        driver_id=row["driverid"],
        branch_id=row["branchid"],
        branch_name=row["branchname"],
        effective_from_at=row["effectivefromat"],
        effective_to_at=row["effectivetoat"],
        end_reason=row["endreason"],
        transferred_from_assignment_id=row["transferredfromassignmentid"],
        transferred_to_assignment_id=row["transferredtoassignmentid"],
    )


def _driver_summary(row: dict[str, Any]) -> DriverSummary:
    return DriverSummary(
        driver_id=row["driverid"],
        driver_number=row["drivernumber"],
        cdl_number=row["cdlnumber"],
        driver_status=row["driverstatus"],
    )


async def _get_driver_row(
    company_id: int,
    employee_id: int,
    db: AsyncConnection,
) -> dict[str, Any] | None:
    result = await db.execute(
        text("SELECT * FROM core.Drivers WHERE CompanyID = :company_id AND EmployeeID = :employee_id"),
        {"company_id": company_id, "employee_id": employee_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def _driver_state(
    company_id: int,
    employee_id: int,
    as_of: datetime | None,
    db: AsyncConnection,
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    """Return (state, permanent Driver row, current or pending assignment row)."""
    driver = await _get_driver_row(company_id, employee_id, db)
    if driver is None:
        return "none", None, None
    current = await resolve_effective_employee_assignment(company_id, employee_id, as_of, db)
    if current is not None:
        row = await db.execute(
            text("""
                SELECT a.*, b.BranchName
                FROM core.DriverAssignments a JOIN core.Branches b ON b.BranchID = a.BranchID
                WHERE a.DriverAssignmentID = :assignment_id AND a.CompanyID = :company_id
            """),
            {"assignment_id": current["driverassignmentid"], "company_id": company_id},
        )
        return "current", driver, dict(row.mappings().one())

    result = await db.execute(
        text("""
            SELECT a.*, b.BranchName
            FROM core.DriverAssignments a JOIN core.Branches b ON b.BranchID = a.BranchID
            WHERE a.CompanyID = :company_id
              AND a.DriverID = :driver_id
              AND a.EffectiveFromAt > COALESCE(:as_of, NOW())
              AND (a.EffectiveToAt IS NULL OR a.EffectiveToAt > a.EffectiveFromAt)
            ORDER BY a.EffectiveFromAt, a.DriverAssignmentID
            LIMIT 1
        """),
        {"company_id": company_id, "driver_id": driver["driverid"], "as_of": as_of},
    )
    pending = result.mappings().first()
    if pending is not None:
        return "pending", driver, dict(pending)
    return "none", driver, None


async def _employee_summary(
    row: dict[str, Any],
    company_id: int,
    as_of: datetime | None,
    db: AsyncConnection,
) -> tuple[EmployeeSummary, dict[str, Any] | None]:
    driver_state, driver, assignment = await _driver_state(
        company_id, row["employeeid"], as_of, db,
    )
    summary = EmployeeSummary(
        employee_id=row["employeeid"],
        company_id=row["companyid"],
        branch_id=row["branchid"],
        branch_name=row["branchname"],
        employee_key=row["employeekey"],
        full_name=row["fullname"],
        preferred_name=row["preferredname"],
        email=row["email"],
        primary_phone=row["primaryphone"],
        hire_date=row["hiredate"],
        employment_status=row["employmentstatus"],
        termination_date=row["terminationdate"],
        driver_state=driver_state,
        driver=_driver_summary(driver) if driver else None,
        current_or_pending_assignment=_assignment_summary(assignment) if assignment else None,
    )
    return summary, assignment


async def list_employees(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    branch_id: int | None = None,
    employment_status: str | None = None,
    driver_state: str | None = None,
    q: str | None = None,
) -> list[EmployeeSummary]:
    _, permitted_branches = await _readable_employee_branches(company_id, user_id, db, branch_id)
    await sync_company_employee_branch_projections(company_id, None, db)
    visible_branch = "COALESCE(current_assignment.BranchID, e.BranchID)"
    conditions = ["e.CompanyID = :company_id"]
    params: dict[str, Any] = {"company_id": company_id}
    if branch_id is not None:
        conditions.append(f"{visible_branch} = :branch_id")
        params["branch_id"] = branch_id
    elif permitted_branches:
        keys = [f"bid{i}" for i in range(len(permitted_branches))]
        conditions.append(f"{visible_branch} IN ({', '.join(':' + key for key in keys)})")
        params.update(dict(zip(keys, permitted_branches)))
    if employment_status:
        conditions.append("e.EmploymentStatus = :employment_status")
        params["employment_status"] = employment_status
    if q:
        conditions.append("(LOWER(e.FullName) LIKE :q OR LOWER(COALESCE(e.Email, '')) LIKE :q OR LOWER(COALESCE(e.EmployeeKey, '')) LIKE :q)")
        params["q"] = f"%{q.lower()}%"

    result = await db.execute(
        text(f"""
            SELECT e.EmployeeID, e.CompanyID, {visible_branch} AS BranchID, b.BranchName,
                   e.EmployeeKey, e.FullName, e.PreferredName, e.Email,
                   e.PrimaryPhone, e.HireDate, e.EmploymentStatus, e.TerminationDate
            FROM core.Employees e
            LEFT JOIN core.Drivers current_driver
              ON current_driver.CompanyID = e.CompanyID
             AND current_driver.EmployeeID = e.EmployeeID
            LEFT JOIN core.DriverAssignments current_assignment
              ON current_assignment.DriverAssignmentID =
                 core.fn_EffectiveDriverAssignment(e.CompanyID, current_driver.DriverID)
            JOIN core.Branches b ON b.BranchID = {visible_branch}
            WHERE {' AND '.join(conditions)}
            ORDER BY e.FullName, e.EmployeeID
        """),
        params,
    )
    summaries: list[EmployeeSummary] = []
    for raw in result.mappings().all():
        summary, _ = await _employee_summary(dict(raw), company_id, None, db)
        if driver_state is None or summary.driver_state == driver_state:
            summaries.append(summary)
    return summaries


async def get_employee(
    company_id: int,
    user_id: int,
    employee_id: int,
    db: AsyncConnection,
    *,
    require_view: bool = True,
) -> EmployeeDetail:
    result = await db.execute(
        text("""
            SELECT e.EmployeeID, e.CompanyID, e.BranchID, b.BranchName,
                   e.EmployeeKey, e.FullName, e.PreferredName, e.Email,
                   e.PrimaryPhone, e.HireDate, e.EmploymentStatus, e.TerminationDate
            FROM core.Employees e JOIN core.Branches b ON b.BranchID = e.BranchID
            WHERE e.EmployeeID = :employee_id AND e.CompanyID = :company_id
            FOR UPDATE OF e
        """),
        {"employee_id": employee_id, "company_id": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    current = await resolve_effective_employee_assignment(company_id, employee_id, None, db)
    visible_branch = current["branchid"] if current is not None else row["branchid"]
    if require_view:
        await _readable_employee_branches(company_id, user_id, db, visible_branch)
    else:
        can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)
        if not can_see_all and visible_branch not in branch_ids:
            raise HTTPException(status_code=403, detail="Access denied to the Employee branch.")
    await sync_employee_branch_projection(company_id, employee_id, None, db)
    refreshed = await db.execute(
        text("""
            SELECT e.EmployeeID, e.CompanyID, e.BranchID, b.BranchName,
                   e.EmployeeKey, e.FullName, e.PreferredName, e.Email,
                   e.PrimaryPhone, e.HireDate, e.EmploymentStatus, e.TerminationDate
            FROM core.Employees e JOIN core.Branches b ON b.BranchID = e.BranchID
            WHERE e.EmployeeID = :employee_id AND e.CompanyID = :company_id
        """),
        {"employee_id": employee_id, "company_id": company_id},
    )
    row = refreshed.mappings().one()
    summary, _ = await _employee_summary(dict(row), company_id, None, db)
    assignments_result = await db.execute(
        text("""
            SELECT a.*, b.BranchName
            FROM core.DriverAssignments a JOIN core.Branches b ON b.BranchID = a.BranchID
            WHERE a.CompanyID = :company_id AND a.EmployeeID = :employee_id
            ORDER BY a.EffectiveFromAt, a.DriverAssignmentID
        """),
        {"company_id": company_id, "employee_id": employee_id},
    )
    assignments = [_assignment_summary(dict(a)) for a in assignments_result.mappings().all()]
    user_result = await db.execute(
        text("""
            SELECT UserID, Username, DisplayName, IsActive, CanLogin
            FROM sec.Users WHERE CompanyID = :company_id AND EmployeeID = :employee_id
            ORDER BY UserID LIMIT 1
        """),
        {"company_id": company_id, "employee_id": employee_id},
    )
    linked = user_result.mappings().first()
    return EmployeeDetail(
        **summary.model_dump(),
        driver_assignments=assignments,
        linked_user=UserSummary(
            user_id=linked["userid"], username=linked["username"],
            display_name=linked["displayname"], is_active=linked["isactive"],
            can_login=linked["canlogin"],
        ) if linked else None,
    )


def _conflict_from_integrity(exc: IntegrityError, *, employee: bool = False) -> HTTPException:
    message = str(exc.orig).lower()
    if "employeekey" in message or "ux_employees_company_employeekey" in message:
        return HTTPException(status_code=409, detail="EmployeeKey already exists in this company.")
    if "drivernumber" in message or "ux_drivers_company_drivernumber" in message:
        return HTTPException(status_code=409, detail="DriverNumber already exists in this company.")
    if "uq_drivers_employee" in message:
        return HTTPException(status_code=409, detail="A Driver already exists for this Employee.")
    if "excl_driverassignments_driver_window" in message or "exclusion" in message:
        return HTTPException(status_code=409, detail="Driver assignment overlaps an existing effective assignment.")
    return HTTPException(status_code=409, detail="Workforce record conflicts with an existing record.")


async def _insert_employee(
    company_id: int,
    actor_user_id: int,
    data: EmployeeCreate,
    db: AsyncConnection,
) -> tuple[int, str]:
    await _require_branch_write(company_id, actor_user_id, data.branch_id, db)
    inserted = await db.execute(
        text("""
            INSERT INTO core.Employees
                (CompanyID, BranchID, EmployeeKey, FullName, PreferredName,
                 EmploymentStatus, Email, PrimaryPhone, HireDate, TerminationDate, CreatedByUserID)
            VALUES (:company_id, :branch_id, :employee_key, :full_name, :preferred_name,
                    :employment_status, :email, :primary_phone, :hire_date, :termination_date, :actor_id)
            RETURNING EmployeeID
        """),
        {
            "company_id": company_id,
            "branch_id": data.branch_id,
            "employee_key": data.employee_key,
            "full_name": data.full_name,
            "preferred_name": data.preferred_name,
            "employment_status": data.employment_status,
            "email": data.email,
            "primary_phone": data.primary_phone,
            "hire_date": data.hire_date,
            "termination_date": data.termination_date,
            "actor_id": actor_user_id,
        },
    )
    employee_id = inserted.scalar_one()
    employee_key = data.employee_key
    if data.employee_key is None:
        employee_key = f"E{employee_id:06d}"
        await db.execute(
            text("UPDATE core.Employees SET EmployeeKey = :key WHERE EmployeeID = :employee_id"),
            {"key": employee_key, "employee_id": employee_id},
        )
    return employee_id, employee_key


async def _insert_driver_with_first_assignment(
    company_id: int,
    employee_id: int,
    branch_id: int,
    actor_user_id: int,
    data: DriverCreate,
    db: AsyncConnection,
) -> tuple[int, int]:
    """Create the permanent Driver and its first home-Branch assignment together."""
    created = await db.execute(
        text("""
            INSERT INTO core.Drivers (CompanyID, EmployeeID, DriverNumber, CDLNumber, DriverStatus)
            VALUES (:company_id, :employee_id, :driver_number, :cdl_number, 'Active')
            RETURNING DriverID
        """),
        {
            "company_id": company_id,
            "employee_id": employee_id,
            "driver_number": data.driver_number,
            "cdl_number": data.cdl_number,
        },
    )
    driver_id = created.scalar_one()
    result = await db.execute(
        text("""
            INSERT INTO core.DriverAssignments
                (CompanyID, BranchID, EmployeeID, DriverID, EffectiveFromAt)
            VALUES (:company_id, :branch_id, :employee_id, :driver_id,
                    COALESCE(:effective_from_at, NOW()))
            RETURNING DriverAssignmentID, EffectiveFromAt
        """),
        {
            "company_id": company_id,
            "branch_id": branch_id,
            "employee_id": employee_id,
            "driver_id": driver_id,
            "effective_from_at": data.effective_from_at,
        },
    )
    inserted = result.mappings().one()
    assignment_id = inserted["driverassignmentid"]
    await workforce_audit.write_workforce_audit(
        db, company_id=company_id, branch_id=branch_id, actor_user_id=actor_user_id,
        action_code="DRIVER_CREATED", entity_name="Drivers", entity_id=driver_id,
        new_value={"employee_id": employee_id, "driver_assignment_id": assignment_id,
                   "branch_id": branch_id, "driver_number": data.driver_number,
                   "effective_from_at": inserted["effectivefromat"], "driver_status": "Active"},
    )
    from app.payroll.eligibility import append_driver_assignment_to_editable_periods

    await append_driver_assignment_to_editable_periods(
        company_id, branch_id, assignment_id, actor_user_id, db,
    )
    return driver_id, assignment_id


async def create_employee(
    company_id: int,
    user_id: int,
    data: EmployeeCreate,
    db: AsyncConnection,
) -> EmployeeDetail:
    try:
        employee_id, employee_key = await _insert_employee(company_id, user_id, data, db)
        await workforce_audit.write_workforce_audit(
            db, company_id=company_id, branch_id=data.branch_id, actor_user_id=user_id,
            action_code="EMPLOYEE_CREATED", entity_name="Employees", entity_id=employee_id,
            new_value={"full_name": data.full_name, "branch_id": data.branch_id,
                       "employee_key": employee_key},
        )
        if data.driver is not None:
            await _insert_driver_with_first_assignment(
                company_id, employee_id, data.branch_id,
                user_id, data.driver, db,
            )
        return await get_employee(company_id, user_id, employee_id, db, require_view=False)
    except IntegrityError as exc:
        raise _conflict_from_integrity(exc, employee=True) from exc


async def create_driver_employee(
    company_id: int,
    user_id: int,
    *,
    branch_id: int,
    full_name: str,
    db: AsyncConnection,
    driver_number: str | None = None,
) -> EmployeeDetail:
    """Create an Employee with its permanent Driver and first assignment through Workforce."""
    return await create_employee(
        company_id,
        user_id,
        EmployeeCreate(
            branch_id=branch_id,
            full_name=full_name,
            driver=DriverCreate(driver_number=driver_number),
        ),
        db,
    )


async def _has_driver(company_id: int, employee_id: int, db: AsyncConnection) -> bool:
    result = await db.execute(
        text("SELECT EXISTS (SELECT 1 FROM core.Drivers WHERE CompanyID = :cid AND EmployeeID = :eid)"),
        {"cid": company_id, "eid": employee_id},
    )
    return bool(result.scalar_one())


async def _has_current_or_pending_assignment(
    company_id: int,
    employee_id: int,
    db: AsyncConnection,
) -> bool:
    if await resolve_effective_employee_assignment(company_id, employee_id, None, db):
        return True
    result = await db.execute(
        text("""
            SELECT EXISTS (
                SELECT 1 FROM core.DriverAssignments a
                WHERE a.CompanyID = :cid AND a.EmployeeID = :eid
                  AND a.EffectiveFromAt > NOW()
                  AND (a.EffectiveToAt IS NULL OR a.EffectiveToAt > a.EffectiveFromAt)
            )
        """),
        {"cid": company_id, "eid": employee_id},
    )
    return bool(result.scalar_one())


async def _hire_date_has_payroll_history(
    company_id: int,
    employee_id: int,
    hire_date: date,
    db: AsyncConnection,
) -> bool:
    result = await db.execute(
        text("""
            SELECT EXISTS (
                SELECT 1
                FROM payroll.PayrollDraftLines dl
                JOIN core.DriverAssignments a ON a.DriverAssignmentID = dl.DriverAssignmentID
                WHERE a.CompanyID = :company_id AND a.EmployeeID = :employee_id
                  AND dl.CompanyID = :company_id
                  AND dl.Status <> 'Void' AND dl.WorkDate < :hire_date
            ) OR EXISTS (
                SELECT 1
                FROM payroll.PayrollFinalLines fl
                JOIN core.DriverAssignments a ON a.DriverAssignmentID = fl.DriverAssignmentID
                WHERE a.CompanyID = :company_id AND a.EmployeeID = :employee_id
                  AND fl.CompanyID = :company_id AND fl.WorkDate IS NOT NULL
                  AND fl.WorkDate < :hire_date
            )
        """),
        {"company_id": company_id, "employee_id": employee_id, "hire_date": hire_date},
    )
    return bool(result.scalar_one())


async def update_employee(
    company_id: int,
    user_id: int,
    employee_id: int,
    data: EmployeeUpdate,
    db: AsyncConnection,
) -> EmployeeDetail:
    result = await db.execute(
        text("""
            SELECT EmployeeID, CompanyID, BranchID, FullName, PreferredName,
                   Email, PrimaryPhone, HireDate, EmploymentStatus, TerminationDate
            FROM core.Employees
            WHERE EmployeeID = :employee_id AND CompanyID = :company_id
            FOR UPDATE
        """),
        {"employee_id": employee_id, "company_id": company_id},
    )
    old_row = result.mappings().first()
    if old_row is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    _, projected_branch = await sync_employee_branch_projection(
        company_id, employee_id, None, db,
    )
    old = dict(old_row)
    if projected_branch is not None:
        old["branchid"] = projected_branch
    old_values = dict(old)
    target_branch = data.branch_id if data.branch_id is not None else old["branchid"]
    await _require_branch_write(company_id, user_id, old["branchid"], db)
    if target_branch != old["branchid"]:
        await _require_branch_write(company_id, user_id, target_branch, db)
        if await _has_current_or_pending_assignment(company_id, employee_id, db):
            raise HTTPException(status_code=422, detail="Employee branch cannot be edited while a current or pending Driver assignment exists.")

    updates = data.model_dump(exclude_unset=True)
    if "branch_id" in updates and updates["branch_id"] is None:
        updates.pop("branch_id")
    if "hire_date" in updates and updates["hire_date"] != old["hiredate"]:
        if (
            updates["hire_date"] is not None
            and await _has_driver(company_id, employee_id, db)
            and await _hire_date_has_payroll_history(company_id, employee_id, updates["hire_date"], db)
        ):
            raise HTTPException(status_code=422, detail="HireDate cannot exclude dates that already have payroll lines.")

    is_driver_employee = await _has_driver(company_id, employee_id, db)
    if is_driver_employee:
        for field, column in (("employment_status", "employmentstatus"), ("termination_date", "terminationdate")):
            if field in updates and updates[field] != old[column]:
                raise HTTPException(status_code=422, detail="Employment lifecycle fields for Driver Employees are controlled by Workforce termination workflow.")

    columns = {
        "branch_id": "branchid", "full_name": "fullname", "preferred_name": "preferredname",
        "email": "email", "primary_phone": "primaryphone", "hire_date": "hiredate",
        "employment_status": "employmentstatus", "termination_date": "terminationdate",
    }
    assignments: list[str] = []
    params: dict[str, Any] = {"employee_id": employee_id, "company_id": company_id}
    new_values: dict[str, Any] = {}
    for field, value in updates.items():
        column = columns[field]
        if value == old[column]:
            continue
        assignments.append(f"{column} = :{field}")
        params[field] = value
        new_values[field] = value
    if assignments:
        await db.execute(
            text(f"UPDATE core.Employees SET {', '.join(assignments)}, UpdatedAtUtc = NOW() WHERE EmployeeID = :employee_id AND CompanyID = :company_id"),
            params,
        )
        await workforce_audit.write_workforce_audit(
            db, company_id=company_id, branch_id=target_branch, actor_user_id=user_id,
            action_code="EMPLOYEE_UPDATED", entity_name="Employees", entity_id=employee_id,
            old_value={k: old_values.get(columns[k]) for k in new_values}, new_value=new_values,
        )
    return await get_employee(company_id, user_id, employee_id, db, require_view=False)


async def create_driver(
    company_id: int,
    user_id: int,
    employee_id: int,
    data: DriverCreate,
    db: AsyncConnection,
    *,
    requested_branch_id: int | None = None,
) -> EmployeeDetail:
    """Create the Employee's permanent Driver with its first assignment.

    A Driver already existing for the Employee is a conflict: any later branch
    assignment of that Driver is a Driver Transfer, and no rehire flow exists yet.
    """
    result = await db.execute(
        text("""
            SELECT EmployeeID, CompanyID, BranchID, HireDate
            FROM core.Employees
            WHERE EmployeeID = :employee_id AND CompanyID = :company_id
            FOR UPDATE
        """),
        {"employee_id": employee_id, "company_id": company_id},
    )
    employee = result.mappings().first()
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    branch_id = employee["branchid"]
    if requested_branch_id is not None and requested_branch_id != branch_id:
        raise HTTPException(status_code=422, detail="First Driver assignment must use the Employee's current branch.")
    await _require_branch_write(company_id, user_id, branch_id, db)
    if await _has_driver(company_id, employee_id, db):
        raise HTTPException(status_code=409, detail="A Driver already exists for this Employee; later assignments require Driver Transfer.")
    try:
        await _insert_driver_with_first_assignment(
            company_id, employee_id, branch_id, user_id, data, db,
        )
    except IntegrityError as exc:
        raise _conflict_from_integrity(exc) from exc
    return await get_employee(company_id, user_id, employee_id, db, require_view=False)


async def terminate_driver_employee(
    company_id: int,
    user_id: int,
    employee_id: int,
    data: EmployeeTermination,
    db: AsyncConnection,
) -> EmployeeTerminationResult:
    """Terminate employment and close each assignment at its Branch-local day end.

    Termination is authoritative over future-effective transfers. A Scheduled transfer
    whose EffectiveAt is at or after the source's termination cutoff is disposed of
    through the same routine a transfer cancel uses: the request becomes Cancelled with
    no destination, the never-effective destination assignment is deleted, and the source
    ends at its own cutoff as Terminated. A Scheduled transfer that takes effect before
    the cutoff survives and its destination is capped like any other assignment.

    Lock order: Employee, transfer requests, the Driver's assignments (ascending), Branch
    workflow advisory locks (ascending), PayrollPeriod rows (ascending); the decision
    instant is read once after all of them.
    """
    from app.payroll.eligibility import BoundaryWindow, lock_boundary_periods
    from app.transfer.disposal import dispose_scheduled_transfer

    employee_result = await db.execute(text("""
        SELECT EmployeeID, BranchID, EmploymentStatus, TerminationDate, HireDate
        FROM core.Employees
        WHERE CompanyID = :company_id AND EmployeeID = :employee_id
        FOR UPDATE
    """), {"company_id": company_id, "employee_id": employee_id})
    employee_row = employee_result.mappings().first()
    if employee_row is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    if employee_row["employmentstatus"] == "Terminated":
        raise HTTPException(status_code=409, detail="Employee is already terminated.")
    if employee_row["hiredate"] is not None and data.termination_date < employee_row["hiredate"]:
        raise HTTPException(status_code=422, detail="Termination date cannot precede the Employee hire date.")

    driver = await _get_driver_row(company_id, employee_id, db)
    if driver is None:
        raise HTTPException(status_code=422, detail="This operation requires a Driver.")
    driver_id = driver["driverid"]

    # Canonical order: Employee, transfer requests, then the Driver's assignments. The
    # assignments are locked FOR NO KEY UPDATE (like the transfer operations): a payroll
    # writer holds its period row lock and then takes FOR KEY SHARE on the assignment for
    # its foreign key, which must not conflict with this lock or the two would deadlock.
    request_result = await db.execute(text("""
        SELECT dtr.TransferRequestID, dtr.Status AS stored_status, dtr.EffectiveAt,
               dtr.SourceBranchID, dtr.TargetBranchID,
               dtr.SourceDriverAssignmentID, dtr.DestinationDriverAssignmentID,
               core.fn_LocalDateBoundary(sb.TimeZoneId, CAST(:termination_date AS DATE) + 1)
                   AS source_cutoff
        FROM core.DriverTransferRequests dtr
        JOIN core.Branches sb ON sb.CompanyID = dtr.CompanyID AND sb.BranchID = dtr.SourceBranchID
        WHERE dtr.CompanyID = :company_id AND dtr.DriverID = :driver_id
          AND dtr.Status NOT IN ('Completed', 'Cancelled', 'Rejected')
        ORDER BY dtr.TransferRequestID FOR UPDATE OF dtr
    """), {"company_id": company_id, "driver_id": driver_id,
           "termination_date": data.termination_date})
    open_requests = [dict(row) for row in request_result.mappings().all()]

    assignments_result = await db.execute(text("""
        SELECT DriverAssignmentID, BranchID, EndReason, EffectiveFromAt, EffectiveToAt,
               TransferredFromAssignmentID, TransferredToAssignmentID
        FROM core.DriverAssignments
        WHERE CompanyID = :company_id AND DriverID = :driver_id
        ORDER BY DriverAssignmentID FOR NO KEY UPDATE
    """), {"company_id": company_id, "driver_id": driver_id})
    assignments = [dict(row) for row in assignments_result.mappings().all()]
    if not assignments:
        raise HTTPException(status_code=422, detail="This operation requires Driver assignments.")

    covering = await db.execute(text("""
        SELECT a.DriverAssignmentID, a.BranchID
        FROM core.DriverAssignments a
        WHERE a.CompanyID = :company_id AND a.DriverID = :driver_id
          AND core.fn_DriverAssignmentCoversWorkDate(a.CompanyID, a.DriverAssignmentID, a.BranchID, :work_date)
        ORDER BY a.EffectiveFromAt DESC, a.DriverAssignmentID DESC
        LIMIT 1
    """), {"company_id": company_id, "driver_id": driver_id, "work_date": data.termination_date})
    target = covering.mappings().first()
    if target is None:
        raise HTTPException(status_code=422, detail="A Driver assignment must overlap the termination WorkDate in its Branch.")
    current = await resolve_effective_employee_assignment(company_id, employee_id, None, db)
    permission_branch_id = current["branchid"] if current is not None else target["branchid"]
    await _require_branch_write(company_id, user_id, permission_branch_id, db)

    final_line = await db.execute(text("""
        SELECT 1
        FROM payroll.PayrollFinalLines fl
        WHERE fl.DriverID = :driver_id
          AND fl.CompanyID = :company_id AND fl.WorkDate > :termination_date
        LIMIT 1
    """), {"company_id": company_id, "driver_id": driver_id,
           "termination_date": data.termination_date})
    if final_line.first() is not None:
        raise HTTPException(status_code=422, detail="Termination date cannot precede dated finalized payroll activity.")

    # Branch-local cutoff of the termination date, per Branch the Driver touches.
    branch_ids = sorted(
        {a["branchid"] for a in assignments}
        | {r["sourcebranchid"] for r in open_requests}
        | {r["targetbranchid"] for r in open_requests}
    )
    cutoff_result = await db.execute(text("""
        SELECT b.BranchID,
               core.fn_LocalDateBoundary(b.TimeZoneId, CAST(:termination_date AS DATE) + 1) AS cutoff
        FROM core.Branches b
        WHERE b.CompanyID = :company_id AND b.BranchID = ANY(:branch_ids)
    """), {"company_id": company_id, "branch_ids": branch_ids,
           "termination_date": data.termination_date})
    cutoffs: dict[int, datetime] = {
        row["branchid"]: row["cutoff"] for row in cutoff_result.mappings().all()
    }

    # Capping removes coverage from each Branch's cutoff onward, and disposing of a
    # Scheduled transfer removes its destination from EffectiveAt onward: lock the
    # Branch workflow and every overlapping PayrollPeriod before anything changes. No
    # period state blocks a termination.
    scheduled_candidates = [
        r for r in open_requests
        if r["stored_status"] == "Scheduled" and r["effectiveat"] >= r["source_cutoff"]
    ]
    windows = [BoundaryWindow(branch_id, cutoffs[branch_id], None) for branch_id in branch_ids]
    for request in scheduled_candidates:
        windows.append(BoundaryWindow(request["sourcebranchid"], request["effectiveat"], None))
        windows.append(BoundaryWindow(request["targetbranchid"], request["effectiveat"], None))
    await lock_boundary_periods(company_id, windows, db, lock_only=True)
    decided_at = await decision_instant(db)

    # A Scheduled transfer still in the future at the decision instant and at or after
    # the source cutoff can never happen: dispose of it. One whose EffectiveAt has
    # already passed is effective history and is left to the capping below; one before
    # the cutoff survives and takes effect at EffectiveAt.
    disposed = [r for r in scheduled_candidates if r["effectiveat"] > decided_at]
    cancelled_ids: list[int] = []
    closed_ids: list[int] = []

    await db.execute(text("""
        UPDATE core.Employees
        SET EmploymentStatus = 'Terminated', TerminationDate = :termination_date,
            UpdatedAtUtc = NOW()
        WHERE CompanyID = :company_id AND EmployeeID = :employee_id
    """), {"company_id": company_id, "employee_id": employee_id,
           "termination_date": data.termination_date})

    reason = f"Cancelled because Employee {employee_id} was terminated on {data.termination_date}."
    assignments_by_id = {a["driverassignmentid"]: a for a in assignments}
    for request in disposed:
        source = assignments_by_id[request["sourcedriverassignmentid"]]
        cap = max(source["effectivefromat"], request["source_cutoff"])
        await dispose_scheduled_transfer(
            company_id=company_id, user_id=user_id, request=request,
            cancel_reason=reason, source_cap=cap, db=db,
        )
        cancelled_ids.append(request["transferrequestid"])
        closed_ids.append(source["driverassignmentid"])

    # Transfers that have not been scheduled yet are cancelled outright.
    unscheduled_ids = [r["transferrequestid"] for r in open_requests if r["stored_status"] != "Scheduled"]
    if unscheduled_ids:
        cancelled_result = await db.execute(text("""
            UPDATE core.DriverTransferRequests
            SET Status = 'Cancelled', CancelledAtUtc = NOW(),
                CancelledByUserID = :user_id,
                CancelReason = :reason, UpdatedAtUtc = NOW()
            WHERE CompanyID = :company_id AND TransferRequestID = ANY(:request_ids)
              AND Status NOT IN ('Scheduled', 'Completed', 'Cancelled', 'Rejected')
            RETURNING TransferRequestID
        """), {"user_id": user_id, "reason": reason, "company_id": company_id,
               "request_ids": unscheduled_ids})
        cancelled_ids.extend(row["transferrequestid"] for row in cancelled_result.mappings().all())

    assignments_result = await db.execute(text("""
        SELECT DriverAssignmentID, BranchID, EndReason, EffectiveFromAt, EffectiveToAt,
               TransferredFromAssignmentID, TransferredToAssignmentID
        FROM core.DriverAssignments
        WHERE CompanyID = :company_id AND DriverID = :driver_id
        ORDER BY DriverAssignmentID
    """), {"company_id": company_id, "driver_id": driver_id})
    assignments = [dict(row) for row in assignments_result.mappings().all()]

    async with workforce_operation(db, "terminate"):
        for assignment in assignments:
            cutoff = cutoffs[assignment["branchid"]]
            start = assignment["effectivefromat"]
            end = assignment["effectivetoat"]
            if assignment["endreason"] == "Terminated" and end is not None and end <= cutoff:
                continue
            if end is not None and end <= cutoff and start < cutoff:
                continue
            new_end = start if start >= cutoff else min(end, cutoff) if end is not None else cutoff
            await db.execute(text("""
                UPDATE core.DriverAssignments
                SET EndReason = 'Terminated', EffectiveToAt = :effective_to_at,
                    UpdatedAtUtc = NOW()
                WHERE CompanyID = :company_id AND DriverAssignmentID = :assignment_id
            """), {"company_id": company_id,
                   "assignment_id": assignment["driverassignmentid"], "effective_to_at": new_end})
            closed_ids.append(assignment["driverassignmentid"])

    await sync_employee_branch_projection(company_id, employee_id, None, db)
    await workforce_audit.write_workforce_audit(
        db, company_id=company_id, branch_id=permission_branch_id, actor_user_id=user_id,
        action_code="DRIVER_EMPLOYEE_TERMINATED", entity_name="Employees", entity_id=employee_id,
        old_value={"employment_status": employee_row["employmentstatus"],
                   "termination_date": employee_row["terminationdate"]},
        new_value={"employee_id": employee_id, "termination_date": data.termination_date,
                   "reason": data.reason, "workdate_driver_assignment_id": target["driverassignmentid"],
                   "driver_id": driver_id,
                   "closed_driver_assignment_ids": closed_ids,
                   "cancelled_transfer_request_ids": cancelled_ids},
    )
    detail = await get_employee(company_id, user_id, employee_id, db, require_view=False)
    return EmployeeTerminationResult(
        **detail.model_dump(), cancelled_transfer_request_ids=sorted(cancelled_ids),
    )


async def update_driver(
    company_id: int,
    user_id: int,
    driver_id: int,
    data: DriverUpdate,
    db: AsyncConnection,
) -> dict[str, Any]:
    """Edit the permanent Driver's number, CDL number or work status."""
    identity_result = await db.execute(
        text("""
            SELECT EmployeeID
            FROM core.Drivers
            WHERE DriverID = :driver_id AND CompanyID = :company_id
        """),
        {"driver_id": driver_id, "company_id": company_id},
    )
    identity = identity_result.mappings().first()
    if identity is None:
        raise HTTPException(status_code=404, detail="Driver not found.")
    await db.execute(
        text("""
            SELECT EmployeeID FROM core.Employees
            WHERE EmployeeID = :employee_id AND CompanyID = :company_id
            FOR UPDATE
        """),
        {"employee_id": identity["employeeid"], "company_id": company_id},
    )
    result = await db.execute(
        text("""
            SELECT dr.DriverID, dr.EmployeeID, dr.DriverNumber, dr.CDLNumber, dr.DriverStatus
            FROM core.Drivers dr
            WHERE dr.DriverID = :driver_id AND dr.CompanyID = :company_id
            FOR UPDATE
        """),
        {"driver_id": driver_id, "company_id": company_id},
    )
    old = result.mappings().first()
    if old is None:
        raise HTTPException(status_code=404, detail="Driver not found.")
    visible = await _driver_dict(company_id, driver_id, db)
    await _require_branch_write(company_id, user_id, visible["branchid"], db)
    updates = data.model_dump(exclude_unset=True)
    if updates and not visible["is_current_or_pending"]:
        raise HTTPException(status_code=422, detail="Only a Driver with a current or pending assignment can be edited.")

    columns = {"driver_number": "drivernumber", "cdl_number": "cdlnumber", "driver_status": "driverstatus"}
    assignments: list[str] = []
    params: dict[str, Any] = {"driver_id": driver_id, "company_id": company_id}
    old_values: dict[str, Any] = {}
    new_values: dict[str, Any] = {}
    for field, value in updates.items():
        column = columns[field]
        if value == old[column]:
            continue
        assignments.append(f"{column} = :{field}")
        params[field] = value
        old_values[field] = old[column]
        new_values[field] = value
    if assignments:
        try:
            await db.execute(text(f"UPDATE core.Drivers SET {', '.join(assignments)}, UpdatedAtUtc = NOW() WHERE DriverID = :driver_id AND CompanyID = :company_id"), params)
        except IntegrityError as exc:
            raise _conflict_from_integrity(exc) from exc
        await workforce_audit.write_workforce_audit(
            db, company_id=company_id, branch_id=visible["branchid"], actor_user_id=user_id,
            action_code="DRIVER_UPDATED", entity_name="Drivers", entity_id=driver_id,
            old_value=old_values, new_value=new_values,
        )
    return await _driver_dict(company_id, driver_id, db)


async def _driver_dict(company_id: int, driver_id: int, db: AsyncConnection) -> dict[str, Any]:
    """The permanent Driver with its Employee and its current, pending or latest assignment."""
    result = await db.execute(
        text(f"""
            SELECT dr.DriverID, dr.EmployeeID, dr.CompanyID,
                   dr.DriverNumber, dr.CDLNumber, dr.DriverStatus,
                   va.DriverAssignmentID, va.BranchID, va.BranchName,
                   va.EffectiveFromAt, va.EffectiveToAt, va.EndReason,
                   (va.DriverAssignmentID = core.fn_EffectiveDriverAssignment(dr.CompanyID, dr.DriverID)
                    OR (va.EffectiveFromAt > NOW()
                        AND (va.EffectiveToAt IS NULL OR va.EffectiveToAt > va.EffectiveFromAt))
                   ) AS is_current_or_pending,
                   e.FullName, e.PreferredName,
                   e.EmployeeKey, e.EmploymentStatus, e.Email, e.PrimaryPhone,
                   e.HireDate, e.TerminationDate
            FROM core.Drivers dr
            JOIN core.Employees e ON e.EmployeeID = dr.EmployeeID
            {VISIBLE_ASSIGNMENT_JOIN}
            WHERE dr.DriverID = :driver_id AND dr.CompanyID = :company_id
        """),
        {"driver_id": driver_id, "company_id": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Driver not found.")
    return dict(row)
