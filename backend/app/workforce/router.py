"""Workforce Employee and Driver-profile API."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncConnection

from app.dependencies import get_current_user, get_db
from app.workforce import service
from app.workforce.schemas import (
    DriverProfileCreate,
    EmployeeCreate,
    EmployeeDetail,
    EmployeeSummary,
    EmployeeTermination,
    EmployeeUpdate,
)

router = APIRouter()
TokenDep = Annotated[dict, Depends(get_current_user)]
DbDep = Annotated[AsyncConnection, Depends(get_db)]


@router.get("/employees", response_model=list[EmployeeSummary])
async def list_employees(
    token: TokenDep,
    db: DbDep,
    branch_id: int | None = Query(None),
    employment_status: str | None = Query(None),
    driver_state: str | None = Query(None, pattern="^(current|pending|none)$"),
    q: str | None = Query(None),
) -> list[EmployeeSummary]:
    return await service.list_employees(
        int(token["cid"]), int(token["sub"]), db,
        branch_id=branch_id,
        employment_status=employment_status,
        driver_state=driver_state,
        q=q,
    )


@router.get("/employees/{employee_id}", response_model=EmployeeDetail)
async def get_employee(employee_id: int, token: TokenDep, db: DbDep) -> EmployeeDetail:
    return await service.get_employee(int(token["cid"]), int(token["sub"]), employee_id, db)


@router.post("/employees", response_model=EmployeeDetail, status_code=201)
async def create_employee(body: EmployeeCreate, token: TokenDep, db: DbDep) -> EmployeeDetail:
    return await service.create_employee(int(token["cid"]), int(token["sub"]), body, db)


@router.patch("/employees/{employee_id}", response_model=EmployeeDetail)
async def update_employee(
    employee_id: int,
    body: EmployeeUpdate,
    token: TokenDep,
    db: DbDep,
) -> EmployeeDetail:
    return await service.update_employee(int(token["cid"]), int(token["sub"]), employee_id, body, db)


@router.post("/employees/{employee_id}/terminate", response_model=EmployeeDetail)
async def terminate_employee(
    employee_id: int,
    body: EmployeeTermination,
    token: TokenDep,
    db: DbDep,
) -> EmployeeDetail:
    return await service.terminate_driver_employee(
        int(token["cid"]), int(token["sub"]), employee_id, body, db,
    )


@router.post(
    "/employees/{employee_id}/driver-profiles",
    response_model=EmployeeDetail,
    status_code=201,
)
async def create_first_driver_profile(
    employee_id: int,
    body: DriverProfileCreate,
    token: TokenDep,
    db: DbDep,
) -> EmployeeDetail:
    return await service.create_first_driver_profile(
        int(token["cid"]), int(token["sub"]), employee_id, body, db,
    )
