"""Canonical non-transfer Employee and Driver-profile mutation owner."""

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import _check_branch_access, _check_permission
from app.workforce.clock import company_today
from app.workforce.effective import resolve_effective_driver_profile
from app.workforce.projection import (
    sync_company_employee_branch_projections,
    sync_employee_branch_projection,
)
from app.workforce.schemas import (
    DriverProfileCreate,
    DriverProfileSummary,
    DriverProfileUpdate,
    EmployeeCreate,
    EmployeeDetail,
    EmployeeSummary,
    EmployeeTermination,
    EmployeeUpdate,
    UserSummary,
)

_AUDIT_REASONS = {
    "EMPLOYEE_CREATED": "Workforce Employee created",
    "EMPLOYEE_UPDATED": "Workforce Employee updated",
    "DRIVER_PROFILE_CREATED": "Workforce Driver profile created",
    "DRIVER_PROFILE_UPDATED": "Workforce Driver profile updated",
    "DRIVER_TRANSFER_COMPLETED": "Workforce Driver transfer completed",
    "DRIVER_EMPLOYEE_TERMINATED": "Workforce Driver Employee terminated",
}


async def _write_workforce_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    branch_id: int,
    actor_user_id: int,
    action_code: str,
    entity_name: str,
    entity_id: int,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
) -> None:
    """Write Workforce audit evidence in the caller's transaction."""
    await db.execute(
        text("""
            INSERT INTO audit.AuditLog
                (CompanyID, BranchID, ActorUserID, ActionCode, EntitySchema,
                 EntityName, EntityID, OldValueJson, NewValueJson, Reason, SourceType)
            VALUES
                (:company_id, :branch_id, :actor_id, :action_code, 'core',
                 :entity_name, :entity_id, :old_value, :new_value, :reason, 'Application')
        """),
        {
            "company_id": company_id,
            "branch_id": branch_id,
            "actor_id": actor_user_id,
            "action_code": action_code,
            "entity_name": entity_name,
            "entity_id": str(entity_id),
            "old_value": json.dumps(old_value, default=str) if old_value is not None else None,
            "new_value": json.dumps(new_value, default=str) if new_value is not None else None,
            "reason": _AUDIT_REASONS[action_code],
        },
    )


async def _require_branch_write(
    company_id: int,
    user_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> None:
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise HTTPException(status_code=403, detail="Access denied to the target branch.")
    result = await db.execute(
        text("SELECT 1 FROM core.Branches WHERE BranchID = :bid AND CompanyID = :cid"),
        {"bid": branch_id, "cid": company_id},
    )
    if result.first() is None:
        raise HTTPException(status_code=422, detail="Branch does not belong to this company.")
    await _check_permission(company_id, user_id, branch_id, "employees.manage", db)


async def _readable_employee_branches(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    branch_id: int | None,
) -> tuple[bool, list[int]]:
    can_see_all, accessible = await _check_branch_access(company_id, user_id, db)
    if branch_id is not None:
        if not can_see_all and branch_id not in accessible:
            raise HTTPException(status_code=403, detail="Access denied to the requested branch.")
        await _check_permission(company_id, user_id, branch_id, "employees.view", db)
        return can_see_all, [branch_id]

    if can_see_all:
        await _check_permission(company_id, user_id, None, "employees.view", db)
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


async def _profile_summary(row: dict[str, Any]) -> DriverProfileSummary:
    return DriverProfileSummary(
        driver_id=row["driverid"],
        branch_id=row["branchid"],
        branch_name=row["branchname"],
        driver_code=row["drivercode"],
        cdl_number=row["cdlnumber"],
        external_driver_id=row["externaldriverid"],
        driver_status=row["driverstatus"],
        effective_from=row["effectivefrom"],
        effective_to=row["effectiveto"],
    )


async def _driver_state(
    company_id: int,
    employee_id: int,
    business_date: date,
    db: AsyncConnection,
) -> tuple[str, dict[str, Any] | None]:
    current = await resolve_effective_driver_profile(company_id, employee_id, business_date, db)
    if current is not None:
        row = await db.execute(
            text("""
                SELECT d.*, b.BranchName
                FROM core.Drivers d JOIN core.Branches b ON b.BranchID = d.BranchID
                WHERE d.DriverID = :driver_id AND d.CompanyID = :company_id
            """),
            {"driver_id": current["driverid"], "company_id": company_id},
        )
        return "current", dict(row.mappings().one())

    result = await db.execute(
        text("""
            SELECT d.*, b.BranchName
            FROM core.Drivers d JOIN core.Branches b ON b.BranchID = d.BranchID
            WHERE d.CompanyID = :company_id
              AND d.EmployeeID = :employee_id
              AND d.EffectiveFrom > :business_date
              AND NOT (
                    d.DriverStatus = 'Terminated'
                AND d.EffectiveTo = d.EffectiveFrom - 1
              )
            ORDER BY d.EffectiveFrom, d.DriverID
            LIMIT 1
        """),
        {"company_id": company_id, "employee_id": employee_id, "business_date": business_date},
    )
    pending = result.mappings().first()
    return ("pending", dict(pending)) if pending is not None else ("none", None)


async def _employee_summary(
    row: dict[str, Any],
    company_id: int,
    business_date: date,
    db: AsyncConnection,
) -> tuple[EmployeeSummary, dict[str, Any] | None]:
    driver_state, profile = await _driver_state(company_id, row["employeeid"], business_date, db)
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
        current_or_pending_driver=await _profile_summary(profile) if profile else None,
    )
    return summary, profile


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
    business_date = await company_today(company_id, db)
    _, permitted_branches = await _readable_employee_branches(company_id, user_id, db, branch_id)
    await sync_company_employee_branch_projections(company_id, business_date, db)
    visible_branch = "COALESCE(current_driver.BranchID, e.BranchID)"
    conditions = ["e.CompanyID = :company_id"]
    params: dict[str, Any] = {"company_id": company_id, "business_date": business_date}
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
             AND core.fn_EffectiveDriverProfile(e.CompanyID, e.EmployeeID, :business_date) = current_driver.DriverID
            JOIN core.Branches b ON b.BranchID = {visible_branch}
            WHERE {' AND '.join(conditions)}
            ORDER BY e.FullName, e.EmployeeID
        """),
        params,
    )
    summaries: list[EmployeeSummary] = []
    for raw in result.mappings().all():
        summary, _ = await _employee_summary(dict(raw), company_id, business_date, db)
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
    business_date = await company_today(company_id, db)
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
    current = await resolve_effective_driver_profile(company_id, employee_id, business_date, db)
    visible_branch = current["branchid"] if current is not None else row["branchid"]
    if require_view:
        await _readable_employee_branches(company_id, user_id, db, visible_branch)
    else:
        can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
        if not can_see_all and visible_branch not in branch_ids:
            raise HTTPException(status_code=403, detail="Access denied to the Employee branch.")
    await sync_employee_branch_projection(company_id, employee_id, business_date, db)
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
    summary, _ = await _employee_summary(dict(row), company_id, business_date, db)
    profiles_result = await db.execute(
        text("""
            SELECT d.*, b.BranchName
            FROM core.Drivers d JOIN core.Branches b ON b.BranchID = d.BranchID
            WHERE d.CompanyID = :company_id AND d.EmployeeID = :employee_id
            ORDER BY d.EffectiveFrom, d.DriverID
        """),
        {"company_id": company_id, "employee_id": employee_id},
    )
    profiles = [await _profile_summary(dict(profile)) for profile in profiles_result.mappings().all()]
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
        driver_profiles=profiles,
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
    if "drivercode" in message or "ux_drivers_company_drivercode" in message:
        return HTTPException(status_code=409, detail="DriverCode already exists in this company.")
    if "excl_drivers_employee_effectivewindow" in message or "exclusion" in message:
        return HTTPException(status_code=409, detail="Driver profile overlaps an existing effective profile.")
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


async def _insert_driver_profile(
    company_id: int,
    employee_id: int,
    branch_id: int,
    hire_date: date | None,
    actor_user_id: int,
    data: DriverProfileCreate,
    db: AsyncConnection,
) -> int:
    effective_from = data.effective_from
    if effective_from is None:
        effective_from = hire_date or await company_today(company_id, db)
    result = await db.execute(
        text("""
            INSERT INTO core.Drivers
                (CompanyID, BranchID, EmployeeID, DriverCode, CDLNumber,
                 ExternalDriverID, DriverStatus, EffectiveFrom)
            VALUES (:company_id, :branch_id, :employee_id, :driver_code, :cdl_number,
                    :external_driver_id, 'Active', :effective_from)
            RETURNING DriverID
        """),
        {
            "company_id": company_id,
            "branch_id": branch_id,
            "employee_id": employee_id,
            "driver_code": data.driver_code,
            "cdl_number": data.cdl_number,
            "external_driver_id": data.external_driver_id,
            "effective_from": effective_from,
        },
    )
    driver_id = result.scalar_one()
    await _write_workforce_audit(
        db, company_id=company_id, branch_id=branch_id, actor_user_id=actor_user_id,
        action_code="DRIVER_PROFILE_CREATED", entity_name="Drivers", entity_id=driver_id,
        new_value={"employee_id": employee_id, "branch_id": branch_id,
                   "effective_from": effective_from, "driver_status": "Active"},
    )
    return driver_id


async def create_employee(
    company_id: int,
    user_id: int,
    data: EmployeeCreate,
    db: AsyncConnection,
) -> EmployeeDetail:
    try:
        employee_id, employee_key = await _insert_employee(company_id, user_id, data, db)
        await _write_workforce_audit(
            db, company_id=company_id, branch_id=data.branch_id, actor_user_id=user_id,
            action_code="EMPLOYEE_CREATED", entity_name="Employees", entity_id=employee_id,
            new_value={"full_name": data.full_name, "branch_id": data.branch_id,
                       "employee_key": employee_key},
        )
        if data.driver_profile is not None:
            await _insert_driver_profile(
                company_id, employee_id, data.branch_id, data.hire_date,
                user_id, data.driver_profile, db,
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
    driver_code: str | None = None,
) -> EmployeeDetail:
    """Canonical Employee+first-profile creation used by Core and temporary Admin adapters."""
    return await create_employee(
        company_id,
        user_id,
        EmployeeCreate(
            branch_id=branch_id,
            full_name=full_name,
            driver_profile=DriverProfileCreate(driver_code=driver_code),
        ),
        db,
    )


async def _has_driver_profiles(company_id: int, employee_id: int, db: AsyncConnection) -> bool:
    result = await db.execute(
        text("SELECT EXISTS (SELECT 1 FROM core.Drivers WHERE CompanyID = :cid AND EmployeeID = :eid)"),
        {"cid": company_id, "eid": employee_id},
    )
    return bool(result.scalar_one())


async def _has_current_or_pending_profile(
    company_id: int,
    employee_id: int,
    db: AsyncConnection,
) -> bool:
    business_date = await company_today(company_id, db)
    if await resolve_effective_driver_profile(company_id, employee_id, business_date, db):
        return True
    result = await db.execute(
        text("""
            SELECT EXISTS (
                SELECT 1 FROM core.Drivers d
                WHERE d.CompanyID = :cid AND d.EmployeeID = :eid
                  AND d.EffectiveFrom > :today
                  AND NOT (d.DriverStatus = 'Terminated' AND d.EffectiveTo = d.EffectiveFrom - 1)
            )
        """),
        {"cid": company_id, "eid": employee_id, "today": business_date},
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
                JOIN core.Drivers d ON d.DriverID = dl.DriverID
                WHERE d.CompanyID = :company_id AND d.EmployeeID = :employee_id
                  AND dl.CompanyID = :company_id AND dl.WorkDate IS NOT NULL
                  AND dl.Status <> 'Void' AND dl.WorkDate < :hire_date
            ) OR EXISTS (
                SELECT 1
                FROM payroll.PayrollFinalLines fl
                JOIN core.Drivers d ON d.DriverID = fl.DriverID
                WHERE d.CompanyID = :company_id AND d.EmployeeID = :employee_id
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
    business_date = await company_today(company_id, db)
    _, projected_branch = await sync_employee_branch_projection(
        company_id, employee_id, business_date, db,
    )
    old = dict(old_row)
    if projected_branch is not None:
        old["branchid"] = projected_branch
    old_values = dict(old)
    target_branch = data.branch_id if data.branch_id is not None else old["branchid"]
    await _require_branch_write(company_id, user_id, old["branchid"], db)
    if target_branch != old["branchid"]:
        await _require_branch_write(company_id, user_id, target_branch, db)
        if await _has_current_or_pending_profile(company_id, employee_id, db):
            raise HTTPException(status_code=422, detail="Employee branch cannot be edited while a current or pending Driver profile exists.")

    updates = data.model_dump(exclude_unset=True)
    if "branch_id" in updates and updates["branch_id"] is None:
        updates.pop("branch_id")
    if "hire_date" in updates and updates["hire_date"] != old["hiredate"]:
        if (
            updates["hire_date"] is not None
            and await _has_driver_profiles(company_id, employee_id, db)
            and await _hire_date_has_payroll_history(company_id, employee_id, updates["hire_date"], db)
        ):
            raise HTTPException(status_code=422, detail="HireDate cannot exclude dates that already have payroll lines.")

    is_driver_employee = await _has_driver_profiles(company_id, employee_id, db)
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
        await _write_workforce_audit(
            db, company_id=company_id, branch_id=target_branch, actor_user_id=user_id,
            action_code="EMPLOYEE_UPDATED", entity_name="Employees", entity_id=employee_id,
            old_value={k: old_values.get(columns[k]) for k in new_values}, new_value=new_values,
        )
    return await get_employee(company_id, user_id, employee_id, db, require_view=False)


async def create_first_driver_profile(
    company_id: int,
    user_id: int,
    employee_id: int,
    data: DriverProfileCreate,
    db: AsyncConnection,
    *,
    requested_branch_id: int | None = None,
) -> EmployeeDetail:
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
        raise HTTPException(status_code=422, detail="First Driver profile must use the Employee's current branch.")
    await _require_branch_write(company_id, user_id, branch_id, db)
    if await _has_driver_profiles(company_id, employee_id, db):
        raise HTTPException(status_code=409, detail="Driver profile history already exists; later profiles require Driver Transfer.")
    try:
        await _insert_driver_profile(
            company_id, employee_id, branch_id, employee["hiredate"], user_id, data, db,
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
) -> EmployeeDetail:
    """Atomically terminate a Driver Employee and close future profile activity."""
    employee_result = await db.execute(
        text("""
            SELECT EmployeeID, BranchID, EmploymentStatus, TerminationDate, HireDate
            FROM core.Employees
            WHERE CompanyID = :company_id AND EmployeeID = :employee_id
            FOR UPDATE
        """),
        {"company_id": company_id, "employee_id": employee_id},
    )
    employee_row = employee_result.mappings().first()
    if employee_row is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    if employee_row["employmentstatus"] == "Terminated":
        raise HTTPException(status_code=409, detail="Employee is already terminated.")

    driver_ids_result = await db.execute(
        text("""
            SELECT DriverID
            FROM core.Drivers
            WHERE CompanyID = :company_id AND EmployeeID = :employee_id
            ORDER BY DriverID
        """),
        {"company_id": company_id, "employee_id": employee_id},
    )
    driver_ids = [row["driverid"] for row in driver_ids_result.mappings().all()]
    if not driver_ids:
        raise HTTPException(
            status_code=422,
            detail="This termination operation is only for Employees with Driver profiles.",
        )

    profiles_result = await db.execute(
        text("""
            SELECT DriverID, DriverStatus, EffectiveFrom, EffectiveTo,
                   TransferredFromDriverID, TransferredToDriverID
            FROM core.Drivers
            WHERE CompanyID = :company_id AND EmployeeID = :employee_id
            ORDER BY DriverID
            FOR UPDATE
        """),
        {"company_id": company_id, "employee_id": employee_id},
    )
    profiles = [dict(row) for row in profiles_result.mappings().all()]
    request_result = await db.execute(
        text("""
            SELECT TransferRequestID, Status
            FROM core.DriverTransferRequests
            WHERE CompanyID = :company_id
              AND DriverID = ANY(:driver_ids)
              AND Status NOT IN ('Completed', 'Cancelled', 'Rejected')
            ORDER BY TransferRequestID
            FOR UPDATE
        """),
        {"company_id": company_id, "driver_ids": driver_ids},
    )
    active_requests = [dict(row) for row in request_result.mappings().all()]

    business_date = await company_today(company_id, db)
    current_today = await resolve_effective_driver_profile(
        company_id, employee_id, business_date, db,
    )
    current = await resolve_effective_driver_profile(
        company_id, employee_id, data.termination_date, db,
    )
    if employee_row["hiredate"] is not None and data.termination_date < employee_row["hiredate"]:
        raise HTTPException(
            status_code=422,
            detail="Termination date cannot precede the Employee hire date.",
        )
    if current is None:
        raise HTTPException(
            status_code=422,
            detail="A Driver profile must be effective on the termination date.",
        )
    branch_id = current_today["branchid"] if current_today is not None else current["branchid"]
    await _require_branch_write(company_id, user_id, branch_id, db)
    if current is not None and current["driverstatus"] == "Terminated":
        raise HTTPException(
            status_code=409,
            detail="The effective Driver profile is already terminated.",
        )

    pending_profiles = []
    for profile in profiles:
        effective_from = profile["effectivefrom"]
        if effective_from is None or effective_from <= data.termination_date:
            continue
        if (
            profile["driverstatus"] == "Terminated"
            and profile["effectiveto"] == effective_from - timedelta(days=1)
        ):
            continue
        if profile["driverstatus"] in {"Transferred", "Terminated"}:
            raise HTTPException(
                status_code=409,
                detail="A terminal pending Driver profile cannot be closed by this lifecycle operation.",
            )
        pending_profiles.append(profile)

    final_line = await db.execute(
        text("""
            SELECT 1
            FROM payroll.PayrollFinalLines fl
            JOIN core.Drivers d ON d.DriverID = fl.DriverID
            WHERE d.CompanyID = :company_id
              AND d.EmployeeID = :employee_id
              AND fl.CompanyID = :company_id
              AND fl.WorkDate IS NOT NULL
              AND fl.WorkDate > :termination_date
            LIMIT 1
        """),
        {
            "company_id": company_id,
            "employee_id": employee_id,
            "termination_date": data.termination_date,
        },
    )
    if final_line.first() is not None:
        raise HTTPException(
            status_code=422,
            detail="Termination date cannot precede dated finalized payroll activity.",
        )

    await db.execute(
        text("SELECT set_config('flussra.workforce_op', 'terminate', TRUE)")
    )
    await db.execute(
        text("""
            UPDATE core.Employees
            SET EmploymentStatus = 'Terminated', TerminationDate = :termination_date,
                UpdatedAtUtc = NOW()
            WHERE CompanyID = :company_id AND EmployeeID = :employee_id
        """),
        {
            "company_id": company_id,
            "employee_id": employee_id,
            "termination_date": data.termination_date,
        },
    )

    if current is not None:
        closed = await db.execute(
            text("""
                UPDATE core.Drivers
                SET DriverStatus = 'Terminated', EffectiveTo = :termination_date,
                    UpdatedAtUtc = NOW()
                WHERE CompanyID = :company_id AND EmployeeID = :employee_id
                  AND DriverID = :driver_id
                  AND DriverStatus IN ('Active', 'Inactive', 'OnLeave', 'Transferred')
                RETURNING DriverID
            """),
            {
                "company_id": company_id,
                "employee_id": employee_id,
                "driver_id": current["driverid"],
                "termination_date": data.termination_date,
            },
        )
        if closed.scalar_one_or_none() is None:
            raise HTTPException(status_code=409, detail="Driver lifecycle changed during termination.")

    pending_ids: list[int] = []
    for profile in pending_profiles:
        effective_from = profile["effectivefrom"]
        await db.execute(
            text("""
                UPDATE core.Drivers
                SET DriverStatus = 'Terminated', EffectiveTo = :effective_to,
                    UpdatedAtUtc = NOW()
                WHERE CompanyID = :company_id AND EmployeeID = :employee_id
                  AND DriverID = :driver_id
            """),
            {
                "company_id": company_id,
                "employee_id": employee_id,
                "driver_id": profile["driverid"],
                "effective_to": effective_from - timedelta(days=1),
            },
        )
        pending_ids.append(profile["driverid"])

    now = datetime.now(UTC)
    cancelled_ids: list[int] = []
    if active_requests:
        request_ids = [row["transferrequestid"] for row in active_requests]
        cancelled_result = await db.execute(
            text("""
                UPDATE core.DriverTransferRequests
                SET Status = 'Cancelled', CancelledAtUtc = :now,
                    CancelledByUserID = :user_id,
                    CancelReason = :reason, UpdatedAtUtc = :now
                WHERE CompanyID = :company_id
                  AND TransferRequestID = ANY(:request_ids)
                  AND Status NOT IN ('Completed', 'Cancelled', 'Rejected')
                RETURNING TransferRequestID
            """),
            {
                "now": now,
                "user_id": user_id,
                "reason": f"Cancelled because Employee {employee_id} was terminated on {data.termination_date}.",
                "company_id": company_id,
                "request_ids": request_ids,
            },
        )
        cancelled_ids = [row["transferrequestid"] for row in cancelled_result.mappings().all()]

    if data.termination_date <= business_date:
        await sync_employee_branch_projection(company_id, employee_id, data.termination_date, db)

    await _write_workforce_audit(
        db,
        company_id=company_id,
        branch_id=branch_id,
        actor_user_id=user_id,
        action_code="DRIVER_EMPLOYEE_TERMINATED",
        entity_name="Employees",
        entity_id=employee_id,
        old_value={
            "employment_status": employee_row["employmentstatus"],
            "termination_date": employee_row["terminationdate"],
        },
        new_value={
            "employee_id": employee_id,
            "termination_date": data.termination_date,
            "reason": data.reason,
            "current_driver_id": current["driverid"] if current is not None else None,
            "pending_driver_ids": pending_ids,
            "cancelled_transfer_request_ids": cancelled_ids,
        },
    )
    return await get_employee(company_id, user_id, employee_id, db, require_view=False)


async def update_driver_profile(
    company_id: int,
    user_id: int,
    driver_id: int,
    data: DriverProfileUpdate,
    db: AsyncConnection,
) -> dict[str, Any]:
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
            SELECT d.DriverID, d.CompanyID, d.BranchID, d.EmployeeID, d.DriverCode,
                   d.CDLNumber, d.ExternalDriverID, d.DriverStatus,
                   d.EffectiveFrom, d.EffectiveTo
            FROM core.Drivers d
            WHERE d.DriverID = :driver_id AND d.CompanyID = :company_id
            FOR UPDATE
        """),
        {"driver_id": driver_id, "company_id": company_id},
    )
    old = result.mappings().first()
    if old is None:
        raise HTTPException(status_code=404, detail="Driver not found.")
    await _require_branch_write(company_id, user_id, old["branchid"], db)
    updates = data.model_dump(exclude_unset=True)
    if updates:
        business_date = await company_today(company_id, db)
        current = await resolve_effective_driver_profile(
            company_id, old["employeeid"], business_date, db,
        )
        is_current = current is not None and current["driverid"] == driver_id
        if old["driverstatus"] in {"Transferred", "Terminated"}:
            raise HTTPException(status_code=422, detail="Historical Driver profiles cannot be edited.")
        if not is_current:
            effective_from = old["effectivefrom"]
            is_pending = (
                effective_from is not None
                and effective_from > business_date
                and not (
                    old["driverstatus"] == "Terminated"
                    and old["effectiveto"] == effective_from - timedelta(days=1)
                )
            )
            if not is_pending:
                raise HTTPException(status_code=422, detail="Only current or pending Driver profiles can be edited.")

    columns = {"driver_code": "drivercode", "cdl_number": "cdlnumber", "external_driver_id": "externaldriverid", "driver_status": "driverstatus"}
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
        await _write_workforce_audit(
            db, company_id=company_id, branch_id=old["branchid"], actor_user_id=user_id,
            action_code="DRIVER_PROFILE_UPDATED", entity_name="Drivers", entity_id=driver_id,
            old_value=old_values, new_value=new_values,
        )
    return await _driver_dict(company_id, driver_id, db)


async def _driver_dict(company_id: int, driver_id: int, db: AsyncConnection) -> dict[str, Any]:
    result = await db.execute(
        text("""
            SELECT d.DriverID, d.EmployeeID, d.CompanyID, d.BranchID, b.BranchName,
                   d.DriverCode, d.CDLNumber, d.ExternalDriverID, d.DriverStatus,
                   d.EffectiveFrom, d.EffectiveTo, e.FullName, e.PreferredName,
                   e.EmployeeKey, e.EmploymentStatus, e.Email, e.PrimaryPhone,
                   e.HireDate, e.TerminationDate
            FROM core.Drivers d JOIN core.Employees e ON e.EmployeeID = d.EmployeeID
            JOIN core.Branches b ON b.BranchID = d.BranchID
            WHERE d.DriverID = :driver_id AND d.CompanyID = :company_id
        """),
        {"driver_id": driver_id, "company_id": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Driver not found.")
    return dict(row)


async def find_employee_driver_profile(
    company_id: int,
    employee_id: int,
    db: AsyncConnection,
) -> dict[str, Any] | None:
    """Return current, else nearest pending, profile for the temporary Admin adapter."""
    business_date = await company_today(company_id, db)
    current = await resolve_effective_driver_profile(company_id, employee_id, business_date, db)
    profile_id = current["driverid"] if current else None
    if profile_id is None:
        result = await db.execute(
            text("""
                SELECT DriverID FROM core.Drivers
                WHERE CompanyID = :cid AND EmployeeID = :eid
                  AND EffectiveFrom > :today
                  AND NOT (DriverStatus = 'Terminated' AND EffectiveTo = EffectiveFrom - 1)
                ORDER BY EffectiveFrom, DriverID LIMIT 1
            """),
            {"cid": company_id, "eid": employee_id, "today": business_date},
        )
        found = result.scalar_one_or_none()
        if found is None:
            return None
        profile_id = found
    return await _driver_dict(company_id, profile_id, db)
