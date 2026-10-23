"""
Core domain router — branches and the assignment-centric Driver roster.

All endpoints require a valid JWT.  Company and user identity come from
the token; branch-scoping is enforced inside the service layer.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core import service
from app.core.schemas import (
    BranchSummary,
    DriverAssignmentRosterRow,
)
from app.dependencies import get_current_user, get_db

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


@router.get(
    "/driver-assignments",
    response_model=list[DriverAssignmentRosterRow],
    summary="List DriverAssignments the user may read (assignment-centric roster)",
    responses={403: {"description": "No Branch in which the user may read Drivers"}},
)
async def list_assignments(
    token: TokenDep,
    db: DbDep,
    branch_id: int | None = Query(None),
    state: str | None = Query(None, description="current | pending | ended"),
) -> list[DriverAssignmentRosterRow]:
    return await service.list_driver_assignments(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        branch_id=branch_id,
        state=state,
    )
