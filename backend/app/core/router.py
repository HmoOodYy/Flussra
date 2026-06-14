"""
Core domain router — /core/branches, /core/people, /core/drivers.

All endpoints require a valid JWT.  Company and user identity come from
the token; branch-scoping is enforced inside the service layer.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.schemas import (
    BranchSummary,
    PersonSummary,
    DriverSummary,
    DriverCreate,
    DriverUpdate,
)
from app.core import service
from app.dependencies import get_db, get_current_user

router = APIRouter()

# Convenience aliases so route signatures stay readable
TokenDep = Annotated[dict, Depends(get_current_user)]
DbDep    = Annotated[AsyncConnection, Depends(get_db)]


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

@router.get(
    "/branches",
    response_model=list[BranchSummary],
    summary="List branches accessible to the current user",
)
async def list_branches(token: TokenDep, db: DbDep) -> list[BranchSummary]:
    return await service.get_branches(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


# ---------------------------------------------------------------------------
# People (unified employees + drivers)
# ---------------------------------------------------------------------------

@router.get(
    "/people",
    response_model=list[PersonSummary],
    summary="List all employees (drivers included) in the user's scope",
)
async def list_people(
    token: TokenDep,
    db: DbDep,
    branch_id: int | None = Query(None, description="Filter to a specific branch"),
    employee_type: str | None = Query(
        None,
        description="Driver | OfficeStaff | Manager | PayrollUser",
    ),
    employment_status: str | None = Query(
        None,
        description="Active | Inactive | Terminated",
    ),
    q: str | None = Query(None, description="Case-insensitive search on name, email, key"),
) -> list[PersonSummary]:
    return await service.get_people(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        branch_id=branch_id,
        employee_type=employee_type,
        employment_status=employment_status,
        q=q,
    )


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------

@router.get(
    "/drivers",
    response_model=list[DriverSummary],
    summary="List drivers in the user's scope",
)
async def list_drivers(
    token: TokenDep,
    db: DbDep,
    branch_id: int | None = Query(None),
    driver_status: str | None = Query(
        None,
        description="Active | Inactive | Terminated | OnLeave",
    ),
    q: str | None = Query(None, description="Search by name, driver code, or email"),
) -> list[DriverSummary]:
    return await service.get_drivers(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        branch_id=branch_id,
        driver_status=driver_status,
        q=q,
    )


@router.get(
    "/drivers/{driver_id}",
    response_model=DriverSummary,
    summary="Get a single driver by ID",
    responses={
        403: {"description": "No access to this driver's branch"},
        404: {"description": "Driver not found"},
    },
)
async def get_driver(
    driver_id: int,
    token: TokenDep,
    db: DbDep,
) -> DriverSummary:
    return await service.get_driver_by_id(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        driver_id=driver_id,
        db=db,
    )


@router.post(
    "/drivers",
    response_model=DriverSummary,
    status_code=201,
    summary="Create a new driver (also creates the underlying employee record)",
    responses={
        403: {"description": "No access to the target branch"},
        422: {"description": "Validation error or branch_id not in this company"},
    },
)
async def create_driver(
    body: DriverCreate,
    token: TokenDep,
    db: DbDep,
) -> DriverSummary:
    return await service.create_driver(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.patch(
    "/drivers/{driver_id}",
    response_model=DriverSummary,
    summary="Partially update a driver — only supplied fields are changed",
    responses={
        403: {"description": "No access to this driver's branch"},
        404: {"description": "Driver not found"},
    },
)
async def patch_driver(
    driver_id: int,
    body: DriverUpdate,
    token: TokenDep,
    db: DbDep,
) -> DriverSummary:
    return await service.update_driver(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        driver_id=driver_id,
        data=body,
        db=db,
    )
