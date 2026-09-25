"""
Dashboard router — GET /dashboard.

Read-only. Access is granted to any non-driver authenticated user who holds
at least one relevant dashboard permission (payroll.view, payroll.entry,
payroll.period.create, payroll.finalize, review.decide, payrates.view,
payrates.edit, payroll.approve_rate, setup.manage, settings.manage,
drivers.view, drivers.edit, or drivers.manage).

Driver / ODA users are blocked; the frontend shows DriverPlaceholder instead.

Each section in the response is populated only when the user holds a
qualifying permission — see service.py for the full gating logic.
"""
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncConnection

from app.dashboard import service
from app.dashboard.schemas import DashboardResponse
from app.dependencies import get_current_user, get_db

router = APIRouter()

TokenDep = Annotated[dict, Depends(get_current_user)]
DbDep    = Annotated[AsyncConnection, Depends(get_db)]


@router.get(
    "",
    response_model=DashboardResponse,
    summary="Permission-aware dashboard summary",
    description=(
        "Returns a read-only, permission-aware snapshot of the operational "
        "state for the user's branch scope.\n\n"
        "**Access**: any non-driver user holding at least one operational "
        "permission (payroll, review, payrates, setup, or people/drivers).\n\n"
        "**Sections**: each section (payroll_ops, review_queue, rates_health, "
        "setup_health, transfers, approved_periods) is populated only when the "
        "user holds a qualifying permission for that section.\n\n"
        "**Branch scope**:\n"
        "- AllCompanyBranches users see company-wide totals.\n"
        "- SpecificBranch users see only branches where they hold a qualifying "
        "permission for each section.\n\n"
        "Returns 403 if the user is a driver/ODA role, or holds no dashboard "
        "permissions at all."
    ),
    responses={
        200: {"description": "Dashboard snapshot returned"},
        401: {"description": "Missing or invalid token"},
        403: {"description": "Driver/ODA user, or no dashboard permissions granted"},
    },
)
async def get_dashboard(
    token: TokenDep,
    db: DbDep,
) -> DashboardResponse:
    company_id: int = int(token["cid"])
    user_id: int    = int(token["sub"])
    return await service.get_dashboard_summary(company_id, user_id, db)
