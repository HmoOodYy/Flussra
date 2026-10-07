"""
Settings domain router — /settings/company, /settings/branches,
and per-branch payroll setup + status keys.

All endpoints require a valid JWT.
Company and user identity come from the token; access checks are enforced
inside the service layer.

Write endpoints require AllCompanyBranches scope — see _ensure_company_admin().
"""
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncConnection

from app.dependencies import get_current_user, get_db
from app.settings import service
from app.settings.schemas import (
    BranchAdmin,
    BranchCreate,
    BranchUpdate,
    CompanyProfile,
    CompanyUpdate,
    OnboardingOptionsResponse,
    StatusKey,
    StatusKeyCreate,
    StatusKeyUpdate,
    StatusRateColumn,
    StatusRateColumnCreate,
    SupportedCurrency,
)

router = APIRouter()

TokenDep = Annotated[dict, Depends(get_current_user)]
DbDep    = Annotated[AsyncConnection, Depends(get_db)]


# ---------------------------------------------------------------------------
# Company profile
# ---------------------------------------------------------------------------

@router.get("/currencies", response_model=list[SupportedCurrency])
async def list_supported_currencies(token: TokenDep, db: DbDep) -> list[SupportedCurrency]:
    return await service.list_supported_currencies(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )



@router.get(
    "/company",
    response_model=CompanyProfile,
    summary="Get the company profile",
    description=(
        "Returns the company profile for the authenticated user's company, "
        "including the current default branch name.  Readable by any "
        "authenticated user."
    ),
    responses={
        403: {"description": "No branch access for this company"},
        404: {"description": "Company not found"},
    },
)
async def get_company_profile(
    token: TokenDep,
    db: DbDep,
) -> CompanyProfile:
    return await service.get_company_profile(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.patch(
    "/company",
    response_model=CompanyProfile,
    summary="Update the company profile",
    description=(
        "Updates editable fields: **company_name**, **legal_name**, "
        "**timezone_name**, **notes**, **allow_self_approval**.\n\n"
        "**allow_self_approval** controls whether the same user who submitted "
        "a review item may also record a substantive decision (Approved, "
        "Rejected, EditRequested) on it.  Defaults to `true`.  Set to `false` "
        "to require separation of duties in the review workflow.\n\n"
        "Status and IsSuspended are system-controlled and cannot be changed here.\n\n"
        "Requires AllCompanyBranches scope."
    ),
    responses={
        403: {"description": "Insufficient scope (requires all-branches access)"},
        404: {"description": "Company not found"},
    },
)
async def update_company_profile(
    body: CompanyUpdate,
    token: TokenDep,
    db: DbDep,
) -> CompanyProfile:
    return await service.update_company_profile(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

@router.get(
    "/branches",
    response_model=list[BranchAdmin],
    summary="List branches with operational metrics",
    description=(
        "Returns branches visible to the caller with five operational metrics "
        "(payroll setup, status-key count, people count, active-driver count, "
        "pending-approval count).  Company admins see all branches; "
        "branch-scoped users see only their assigned branches."
    ),
    responses={403: {"description": "No branch access for this company"}},
)
async def list_branches(
    token: TokenDep,
    db: DbDep,
) -> list[BranchAdmin]:
    return await service.get_branches(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.get(
    "/branches/onboarding-options",
    response_model=OnboardingOptionsResponse,
    summary="Canonical legal onboarding dates for the company's default Payroll Setup",
    description=(
        "Returns the company's default Payroll Setup (if any) together with "
        "canonical boundary choices — the nearest valid previous/next "
        "first-payroll dates and a server-suggested date, computed by the "
        "same read-only validators the write paths use.  `default_setup` is "
        "null when the company has no default Payroll Setup, in which case "
        "`choices` is also null.\n\n"
        "Feeds `first_payroll_start_date` for `POST /settings/branches` so "
        "the frontend never computes payroll chronology itself.\n\n"
        "Requires company-level (all-branches) access, `branches.create`, "
        "and `payroll_setup.assign` — the same authority "
        "`first_payroll_start_date` requires on branch creation.\n\n"
        "**Must be declared before `GET /branches/{branch_id}`** so "
        "`onboarding-options` is not parsed as a branch id."
    ),
    responses={403: {"description": "Insufficient scope or permission"}},
)
async def get_onboarding_options(
    token: TokenDep,
    db: DbDep,
    around: date | None = Query(None, description="Reference date; defaults to company-local today"),
) -> OnboardingOptionsResponse:
    return await service.get_onboarding_options(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        around=around,
    )


@router.get(
    "/branches/{branch_id}",
    response_model=BranchAdmin,
    summary="Get a single branch with metrics",
    responses={
        403: {"description": "No access to this branch"},
        404: {"description": "Branch not found"},
    },
)
async def get_branch(
    branch_id: int,
    token: TokenDep,
    db: DbDep,
) -> BranchAdmin:
    return await service.get_branch_by_id(
        branch_id=branch_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.post(
    "/branches",
    response_model=BranchAdmin,
    status_code=201,
    summary="Create a new branch",
    description=(
        "Creates a branch for the authenticated user's company.  "
        "`branch_code` is auto-generated from `branch_name` when omitted.  "
        "An optional `first_payroll_start_date` onboards the branch to the "
        "company's current default Payroll Setup; it requires "
        "`payroll_setup.assign`.  "
        "If `is_default=true`, all other branches lose their default flag "
        "atomically; this additionally requires company-wide `setup.manage` "
        "(the same authority as the set-default endpoint).\n\n"
        "Requires AllCompanyBranches scope and `branches.create`; driver-only "
        "roles are not permitted."
    ),
    responses={
        403: {"description": "Insufficient scope (requires all-branches access)"},
        422: {"description": "Validation error or branch code/name conflict"},
    },
)
async def create_branch(
    body: BranchCreate,
    token: TokenDep,
    db: DbDep,
) -> BranchAdmin:
    return await service.create_branch(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.patch(
    "/branches/{branch_id}",
    response_model=BranchAdmin,
    summary="Partially update a branch",
    description=(
        "Applies non-null fields from the request body.  `is_default` is "
        "intentionally excluded — use `POST /settings/branches/{id}/set-default` "
        "to promote a branch.\n\n"
        "Business rule: the current default branch cannot be deactivated.\n\n"
        "Requires AllCompanyBranches scope."
    ),
    responses={
        403: {"description": "Insufficient scope (requires all-branches access)"},
        404: {"description": "Branch not found"},
        422: {"description": "Validation error, uniqueness conflict, or deactivating default"},
    },
)
async def update_branch(
    branch_id: int,
    body: BranchUpdate,
    token: TokenDep,
    db: DbDep,
) -> BranchAdmin:
    return await service.update_branch(
        branch_id=branch_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.post(
    "/branches/{branch_id}/set-default",
    response_model=BranchAdmin,
    summary="Promote a branch to the company default",
    description=(
        "Sets `is_default=true` on the target branch and clears all other "
        "defaults atomically.  The branch must be Active.  Idempotent: "
        "calling this on the already-default branch returns it unchanged.\n\n"
        "Requires AllCompanyBranches scope."
    ),
    responses={
        403: {"description": "Insufficient scope (requires all-branches access)"},
        404: {"description": "Branch not found"},
        422: {"description": "Branch is not Active"},
    },
)
async def set_default_branch(
    branch_id: int,
    token: TokenDep,
    db: DbDep,
) -> BranchAdmin:
    return await service.set_default_branch(
        branch_id=branch_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


# ---------------------------------------------------------------------------
# Payroll status keys
# ---------------------------------------------------------------------------

@router.get(
    "/branches/{branch_id}/status-keys",
    response_model=list[StatusKey],
    summary="List payroll status keys for a branch",
    description=(
        "Returns the work-status codes configured for the branch "
        "(e.g. Vacation, Sick Day, On Leave).  By default only active keys "
        "are returned; pass `include_inactive=true` to see deactivated ones "
        "as well.\n\n"
        "Any authenticated user with branch access can read this."
    ),
    responses={403: {"description": "No access to this branch"}},
)
async def list_status_keys(
    branch_id: int,
    token: TokenDep,
    db: DbDep,
    include_inactive: bool = Query(False, description="Include deactivated keys"),
) -> list[StatusKey]:
    return await service.get_status_keys(
        branch_id=branch_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        include_inactive=include_inactive,
    )


@router.post(
    "/branches/{branch_id}/status-keys",
    response_model=StatusKey,
    status_code=201,
    summary="Create a new payroll status key",
    description=(
        "Adds a new work-status code for the branch.  The normalized code "
        "(uppercased, non-alphanumeric chars replaced by `_`) must be unique "
        "among active keys for this branch.\n\n"
        "Business rules:\n"
        "- `hours_value` must be 0–24.\n"
        "- `deducts_from_yearly_allowance=true` is **not available yet** — "
        "yearly allowance tracking is a future feature; sending `true` returns 422.\n\n"
        "Requires AllCompanyBranches scope."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "Branch not found"},
        422: {"description": "Validation error or duplicate code"},
    },
)
async def create_status_key(
    branch_id: int,
    body: StatusKeyCreate,
    token: TokenDep,
    db: DbDep,
) -> StatusKey:
    return await service.create_status_key(
        branch_id=branch_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.patch(
    "/branches/{branch_id}/status-keys/{key_id}",
    response_model=StatusKey,
    summary="Partially update a status key",
    description=(
        "Applies non-null fields from the request body.  The same business "
        "rules as creation apply to the merged result.\n\n"
        "Requires AllCompanyBranches scope."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "Status key not found"},
        422: {"description": "Validation error or duplicate code"},
    },
)
async def update_status_key(
    branch_id: int,
    key_id: int,
    body: StatusKeyUpdate,
    token: TokenDep,
    db: DbDep,
) -> StatusKey:
    return await service.update_status_key(
        key_id=key_id,
        branch_id=branch_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.delete(
    "/branches/{branch_id}/status-keys/{key_id}",
    response_model=StatusKey,
    summary="Deactivate a status key (soft delete)",
    description=(
        "Sets `is_active=false` on the key.  The key is not removed from the "
        "database; it remains visible with `include_inactive=true`.  "
        "Idempotent: calling DELETE on an already-inactive key returns it "
        "unchanged.\n\n"
        "Requires AllCompanyBranches scope."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "Status key not found"},
    },
)
async def delete_status_key(
    branch_id: int,
    key_id: int,
    token: TokenDep,
    db: DbDep,
) -> StatusKey:
    return await service.delete_status_key(
        key_id=key_id,
        branch_id=branch_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


# ---------------------------------------------------------------------------
# Status rate columns (CP-2D2)
# ---------------------------------------------------------------------------

@router.get(
    "/branches/{branch_id}/status-rate-columns",
    response_model=list[StatusRateColumn],
    summary="List status rate columns for a branch",
    description=(
        "Returns all active status rate columns for the branch.  Each column "
        "is backed by a RateType and can be assigned to a status key to enable "
        "automatic payment calculation (HoursValue × driver rate).\n\n"
        "Any authenticated user with branch access can read this."
    ),
)
async def list_status_rate_columns(
    branch_id: int,
    token: TokenDep,
    db: DbDep,
    include_inactive: bool = Query(False),
) -> list[StatusRateColumn]:
    return await service.list_status_rate_columns(
        branch_id=branch_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        active_only=not include_inactive,
    )


@router.post(
    "/branches/{branch_id}/status-rate-columns",
    response_model=StatusRateColumn,
    status_code=201,
    summary="Create a status rate column",
    description=(
        "Creates a named rate-column config for status payment.  Requires "
        "AllCompanyBranches scope."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "Branch not found"},
        422: {"description": "Validation error"},
    },
)
async def create_status_rate_column(
    branch_id: int,
    body: StatusRateColumnCreate,
    token: TokenDep,
    db: DbDep,
) -> StatusRateColumn:
    return await service.create_status_rate_column(
        branch_id=branch_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )
