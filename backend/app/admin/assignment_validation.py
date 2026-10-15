"""Admin company-role assignment validation shared by user and role mutations."""

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.admin.schemas import (
    CompanyRoleAssignmentCreate,
)
from app.workforce.clock import company_today
from app.workforce.effective import resolve_effective_driver_profile


async def _validate_company_role_scope(
    company_id: int, scope_type: str, branch_id: int | None, db: AsyncConnection,
) -> None:
    if scope_type in {"AllCompanyBranches", "Self"}:
        if branch_id is not None:
            raise HTTPException(
                status_code=422,
                detail=f"branch_id must be null for {scope_type} scope.",
            )
        return
    if scope_type != "SpecificBranch":
        raise HTTPException(status_code=422, detail="Unsupported scope_type.")
    if branch_id is None:
        raise HTTPException(status_code=422, detail="branch_id is required for SpecificBranch scope.")
    branch = await db.execute(
        text("SELECT 1 FROM core.Branches WHERE BranchID = :branch_id AND CompanyID = :company_id"),
        {"branch_id": branch_id, "company_id": company_id},
    )
    if branch.first() is None:
        raise HTTPException(status_code=422, detail="Branch not found for this company.")


async def validate_company_role_assignment(
    company_id: int,
    employee_id: int | None,
    data: CompanyRoleAssignmentCreate,
    db: AsyncConnection,
):
    result = await db.execute(
        text("""
            SELECT CompanyRoleID, RoleCode, RoleName, IsActive, IsArchived
            FROM sec.CompanyRoles
            WHERE CompanyRoleID = :role_id AND CompanyID = :company_id
            FOR SHARE
        """),
        {"role_id": data.company_role_id, "company_id": company_id},
    )
    role = result.mappings().first()
    if role is None or not role["isactive"] or role["isarchived"]:
        raise HTTPException(status_code=422, detail="Active company role not found for this company.")
    if role["rolecode"] == "COMPANY_OWNER":
        raise HTTPException(status_code=422, detail="Company Owner can only be granted through ownership transfer.")

    is_driver = role["rolecode"] == "DRIVER"
    if is_driver:
        if employee_id is None:
            raise HTTPException(status_code=422, detail="DRIVER access requires an explicitly linked Employee.")
        if data.scope_type != "Self":
            raise HTTPException(status_code=422, detail="DRIVER access requires Self scope.")
        if data.branch_id is not None:
            raise HTTPException(status_code=422, detail="branch_id must be null for Self scope.")
        employee = await db.execute(
            text("SELECT EmployeeID FROM core.Employees WHERE EmployeeID = :employee_id AND CompanyID = :company_id FOR UPDATE"),
            {"employee_id": employee_id, "company_id": company_id},
        )
        if employee.first() is None:
            raise HTTPException(status_code=422, detail="Linked Employee not found for this company.")
        business_date = await company_today(company_id, db)
        current = await resolve_effective_driver_profile(company_id, employee_id, business_date, db)
        if current is None:
            raise HTTPException(status_code=422, detail="DRIVER access requires a current effective Driver profile.")
    else:
        if data.scope_type == "Self":
            raise HTTPException(status_code=422, detail="Self scope is currently valid only for the exact DRIVER role.")
        await _validate_company_role_scope(company_id, data.scope_type, data.branch_id, db)
    return role
