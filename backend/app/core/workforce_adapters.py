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
            driver_id=employee.driver.driver_id if employee.driver else None,
            driver_assignment_id=(
                employee.current_or_pending_assignment.driver_assignment_id
                if employee.current_or_pending_assignment else None
            ),
            driver_number=employee.driver.driver_number if employee.driver else None,
            driver_status=employee.driver.driver_status if employee.driver else None,
            cdl_number=employee.driver.cdl_number if employee.driver else None,
        )
        for employee in employees
    ]


async def create_driver(
    company_id: int,
    user_id: int,
    data: DriverCreate,
    db: AsyncConnection,
) -> DriverSummary:
    """Compatibility alias to canonical Workforce Employee, Driver and first-assignment creation."""
    from app.workforce.schemas import DriverCreate as WorkforceDriverCreate
    from app.workforce.schemas import EmployeeCreate
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
            driver=WorkforceDriverCreate(
                driver_number=data.driver_number,
                cdl_number=data.cdl_number,
            ),
        ),
        db,
    )
    driver = employee.driver
    assignment = employee.current_or_pending_assignment
    if driver is None or assignment is None:
        raise HTTPException(status_code=500, detail="Workforce creation did not return its Driver.")
    return DriverSummary(
        driver_id=driver.driver_id,
        employee_id=employee.employee_id,
        driver_assignment_id=assignment.driver_assignment_id,
        branch_id=assignment.branch_id,
        branch_name=assignment.branch_name,
        full_name=employee.full_name,
        preferred_name=employee.preferred_name,
        employee_key=employee.employee_key,
        driver_number=driver.driver_number,
        cdl_number=driver.cdl_number,
        driver_status=driver.driver_status,
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
    """Compatibility alias to the canonical Workforce permanent-Driver update."""
    from app.workforce.schemas import DriverUpdate as WorkforceDriverUpdate
    from app.workforce.service import update_driver as workforce_update_driver

    driver = await workforce_update_driver(
        company_id,
        user_id,
        driver_id,
        WorkforceDriverUpdate(**data.model_dump(exclude_unset=True)),
        db,
    )
    return DriverSummary(
        driver_id=driver["driverid"],
        employee_id=driver["employeeid"],
        driver_assignment_id=driver["driverassignmentid"],
        branch_id=driver["branchid"],
        branch_name=driver["branchname"],
        full_name=driver["fullname"],
        preferred_name=driver["preferredname"],
        employee_key=driver["employeekey"],
        driver_number=driver["drivernumber"],
        cdl_number=driver["cdlnumber"],
        driver_status=driver["driverstatus"],
        employment_status=driver["employmentstatus"],
        email=driver["email"],
        primary_phone=driver["primaryphone"],
        hire_date=driver["hiredate"],
        termination_date=driver["terminationdate"],
    )
