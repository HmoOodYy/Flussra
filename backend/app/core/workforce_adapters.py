"""Core People and Driver adapters over the canonical Workforce owner.

These adapters preserve Core schemas and map to Workforce operations without
owning independent lifecycle SQL.
"""
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.schemas import DriverCreate, DriverSummary, DriverUpdate, PersonSummary


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
