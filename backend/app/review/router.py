"""
Review domain router — /review/items.

All endpoints require a valid JWT.
Company and user identity come from the token; branch-scoping and
action-level permission checks are enforced inside the service layer.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncConnection

from app.dependencies import get_current_user, get_db
from app.review import service
from app.review.schemas import (
    _VALID_STATUSES,
    ReviewDecide,
    ReviewItemCreate,
    ReviewItemDetail,
    ReviewItemSummary,
    ReviewPayrollSnapshot,
)

router = APIRouter()

TokenDep = Annotated[dict, Depends(get_current_user)]
DbDep    = Annotated[AsyncConnection, Depends(get_db)]


# ---------------------------------------------------------------------------
# Review items
# ---------------------------------------------------------------------------

@router.get(
    "/items",
    response_model=list[ReviewItemSummary],
    summary="List review items in the user's branch scope",
    description=(
        "Returns items the user has branch access to, ordered newest-first. "
        "Filter by `branch_id` and/or `status`.\n\n"
        "Valid status values: "
        + ", ".join(sorted(_VALID_STATUSES))
    ),
)
async def list_review_items(
    token: TokenDep,
    db: DbDep,
    branch_id: int | None = Query(None, description="Filter to a specific branch"),
    item_status: str | None = Query(
        None,
        alias="status",
        description="Pending | Approved | Rejected | EditRequested | Cancelled",
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[ReviewItemSummary]:
    return await service.get_review_items(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        branch_id=branch_id,
        item_status=item_status,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/items/{item_id}",
    response_model=ReviewItemDetail,
    summary="Get a single review item with its decision history",
    responses={
        403: {"description": "No access to this item's branch"},
        404: {"description": "Review item not found"},
    },
)
async def get_review_item(
    item_id: int,
    token: TokenDep,
    db: DbDep,
) -> ReviewItemDetail:
    return await service.get_review_item_by_id(
        review_item_id=item_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.get(
    "/items/{item_id}/payroll-snapshot",
    response_model=ReviewPayrollSnapshot,
    summary="Get the immutable submitted payroll packet for a PeriodApproval review item",
    responses={
        403: {"description": "No access to this item's branch or review data"},
        404: {"description": "Review item not found"},
        422: {"description": "Item is not linked to a valid submitted payroll snapshot"},
    },
)
async def get_review_item_payroll_snapshot(
    item_id: int,
    token: TokenDep,
    db: DbDep,
) -> ReviewPayrollSnapshot:
    return await service.get_review_item_payroll_snapshot(
        review_item_id=item_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.post(
    "/items",
    response_model=ReviewItemDetail,
    status_code=201,
    summary="Create a new review item (requires payroll.entry permission)",
    responses={
        403: {"description": "No access to the target branch, or missing payroll.entry permission"},
        422: {"description": "Validation error or unknown branch"},
    },
)
async def create_review_item(
    body: ReviewItemCreate,
    token: TokenDep,
    db: DbDep,
) -> ReviewItemDetail:
    return await service.create_review_item(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.post(
    "/items/{item_id}/decide",
    response_model=ReviewItemDetail,
    summary="Record a decision on a review item (requires review.decide permission)",
    description=(
        "Valid decisions: **Approved**, **Rejected**, **EditRequested**, **Comment**.\n\n"
        "- *Approved / Rejected / EditRequested* — updates the item's status and "
        "records the final decision metadata.\n"
        "- *Comment* — appends a note to the item's decision history without "
        "changing its status. Useful for back-and-forth during review.\n\n"
        "The item must be in **Pending** or **EditRequested** status."
    ),
    responses={
        403: {"description": "No access to this item's branch, or missing review.decide permission"},
        404: {"description": "Review item not found"},
        422: {"description": "Item is not in a decidable status"},
    },
)
async def decide_review_item(
    item_id: int,
    body: ReviewDecide,
    token: TokenDep,
    db: DbDep,
) -> ReviewItemDetail:
    return await service.decide_review_item(
        review_item_id=item_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )
