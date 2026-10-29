"""
Settings domain router — /settings/company, /settings/branches,
per-branch payroll setup, and the Company Status catalog (keys, Branch usage limits and
Status rate columns).

All endpoints require a valid JWT.
Company and user identity come from the token; access checks are enforced
inside the owning Settings module.

Write endpoints require AllCompanyBranches scope — see the Settings security module.
"""
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncConnection

from app.dependencies import get_current_user, get_db
from app.settings import (
    branch_reads,
    branches,
    company,
    status_key_limits,
    status_keys,
    status_rate_columns,
)
from app.settings.schemas import (
    BranchAdmin,
    BranchCreate,
    BranchUpdate,
    CompanyProfile,
    CompanyUpdate,
    OnboardingOptionsResponse,
    StatusKey,
    StatusKeyBranchLimits,
    StatusKeyBranchLimitsUpdate,
    StatusKeyCreate,
    StatusKeyUpdate,
    StatusRateColumn,
    StatusRateColumnCreate,
    StatusRateColumnUpdate,
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
    return await company.list_supported_currencies(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.get("/time-zones", response_model=list[str])
async def list_supported_time_zones(token: TokenDep, db: DbDep) -> list[str]:
    return await branch_reads.get_supported_time_zones(
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
    return await company.get_company_profile(
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
        "**notes**, **allow_self_approval**.\n\n"
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
    return await company.update_company_profile(
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
    return await branch_reads.get_branches(
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
    time_zone_id: str = Query(..., min_length=1, max_length=100, description="Prospective Branch timezone"),
    around: date | None = Query(None, description="Optional reference date"),
) -> OnboardingOptionsResponse:
    return await branches.get_onboarding_options(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        time_zone_id=time_zone_id,
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
    return await branch_reads.get_branch_by_id(
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
        "atomically; this additionally requires company-wide `settings.manage` or `setup.manage` "
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
    return await branches.create_branch(
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
    return await branches.update_branch(
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
    return await branches.set_default_branch(
        branch_id=branch_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


# ---------------------------------------------------------------------------
# Company Status catalog
# ---------------------------------------------------------------------------

@router.get(
    "/status-keys",
    response_model=list[StatusKey],
    summary="List the Company's payroll Statuses",
    description=(
        "Returns the Company-wide Status catalog shared by every Branch (for example "
        "Vacation, Sick Day), each with its canonical name, aliases, Category, hours, "
        "full-day-off setting and rate column. By default only active Statuses are "
        "returned; pass `include_inactive=true` to see deactivated ones as well.\n\n"
        "Any authenticated user with access to the Company can read this."
    ),
    responses={403: {"description": "No access to this company"}},
)
async def list_status_keys(
    token: TokenDep,
    db: DbDep,
    include_inactive: bool = Query(False, description="Include deactivated Statuses"),
) -> list[StatusKey]:
    return await status_keys.get_status_keys(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        include_inactive=include_inactive,
    )


@router.post(
    "/status-keys",
    response_model=StatusKey,
    status_code=201,
    summary="Create a Company Status",
    description=(
        "Adds a Status to the Company catalog. The technical code is generated. The "
        "normalized name must be unique among the Company's active Statuses and, with each "
        "alias, must not equal another active Status's name or alias "
        "(`STATUS_NAME_NOT_UNIQUE`, `STATUS_ALIAS_AMBIGUOUS`).\n\n"
        "Business rules:\n"
        "- `category` is required, from a fixed set.\n"
        "- `hours_value` must be 0-24, and greater than 0 when a rate column is set.\n"
        "- `deducts_from_yearly_allowance=true` is **not available yet**.\n\n"
        "Requires AllCompanyBranches scope."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        422: {"description": "Validation error, duplicate name or ambiguous alias"},
    },
)
async def create_status_key(
    body: StatusKeyCreate,
    token: TokenDep,
    db: DbDep,
) -> StatusKey:
    return await status_keys.create_status_key(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.patch(
    "/status-keys/{key_id}",
    response_model=StatusKey,
    summary="Partially update a Company Status",
    description=(
        "Applies the fields sent. `aliases` replaces the whole alias set atomically. Once a "
        "Status has been selected its hours, full-day-off setting, Category and rate "
        "column cannot change (409 `STATUS_KEY_IN_USE`): deactivate it and create a new "
        "Status. Name, display order, active flag and aliases stay editable.\n\n"
        "Requires AllCompanyBranches scope."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "Status key not found"},
        409: {"description": "STATUS_KEY_IN_USE"},
        422: {"description": "Validation error, duplicate name or ambiguous alias"},
    },
)
async def update_status_key(
    key_id: int,
    body: StatusKeyUpdate,
    token: TokenDep,
    db: DbDep,
) -> StatusKey:
    return await status_keys.update_status_key(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        key_id=key_id,
        data=body,
        db=db,
    )


@router.delete(
    "/status-keys/{key_id}",
    response_model=StatusKey,
    summary="Deactivate a Company Status (soft delete)",
    description=(
        "Sets `is_active=false`. The Status is not removed: selections and frozen evidence "
        "keep naming it, and it stays visible with `include_inactive=true`. Idempotent.\n\n"
        "Requires AllCompanyBranches scope."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "Status key not found"},
    },
)
async def delete_status_key(
    key_id: int,
    token: TokenDep,
    db: DbDep,
) -> StatusKey:
    return await status_keys.delete_status_key(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        key_id=key_id,
        db=db,
    )


# ---------------------------------------------------------------------------
# Branch usage limits of Company Statuses
# ---------------------------------------------------------------------------

@router.get(
    "/branches/{branch_id}/status-key-limits",
    response_model=list[StatusKeyBranchLimits],
    summary="List the Branch usage limits of the Company's Statuses",
    description=(
        "One entry per active Status of the Company with the Branch's four optional hard "
        "limits (all disabled when the Branch has none).\n\n"
        "Any authenticated user with access to the Branch can read this."
    ),
    responses={403: {"description": "No access to this branch"}, 404: {"description": "Branch not found"}},
)
async def list_status_key_limits(
    branch_id: int,
    token: TokenDep,
    db: DbDep,
) -> list[StatusKeyBranchLimits]:
    return await status_key_limits.list_branch_limits(
        branch_id=branch_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.put(
    "/branches/{branch_id}/status-keys/{key_id}/limits",
    response_model=StatusKeyBranchLimits,
    summary="Set the Branch usage limits of one Status",
    description=(
        "Replaces the four optional hard limits of the Status in this Branch. Every limit "
        "disabled removes the Branch's row (no limits).\n\n"
        "Requires AllCompanyBranches scope."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "Branch or Status key not found"},
        422: {"description": "Validation error"},
    },
)
async def put_status_key_limits(
    branch_id: int,
    key_id: int,
    body: StatusKeyBranchLimitsUpdate,
    token: TokenDep,
    db: DbDep,
) -> StatusKeyBranchLimits:
    return await status_key_limits.put_branch_limits(
        branch_id=branch_id,
        key_id=key_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


# ---------------------------------------------------------------------------
# Status rate columns (Company-owned)
# ---------------------------------------------------------------------------

@router.get(
    "/status-rate-columns",
    response_model=list[StatusRateColumn],
    summary="List the Company's Status rate columns",
    description=(
        "Returns the Company's active Status rate columns. A Status bound to a column is "
        "paid hours x the Driver's rate for that column; each column owns one scalar rate "
        "definition against which a Driver's Status rates are authored.\n\n"
        "Any authenticated user with access to the Company can read this."
    ),
)
async def list_status_rate_columns(
    token: TokenDep,
    db: DbDep,
    include_inactive: bool = Query(False),
) -> list[StatusRateColumn]:
    return await status_rate_columns.list_status_rate_columns(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        active_only=not include_inactive,
    )


@router.post(
    "/status-rate-columns",
    response_model=StatusRateColumn,
    status_code=201,
    summary="Create a Status rate column",
    description=(
        "Creates a Company Status rate column together with its scalar rate definition. "
        "`is_default=true` makes it the Company default (the previous default is "
        "demoted). Requires AllCompanyBranches scope."
    ),
    responses={403: {"description": "Insufficient scope"}, 422: {"description": "Validation error"}},
)
async def create_status_rate_column(
    body: StatusRateColumnCreate,
    token: TokenDep,
    db: DbDep,
) -> StatusRateColumn:
    return await status_rate_columns.create_status_rate_column(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.patch(
    "/status-rate-columns/{column_id}",
    response_model=StatusRateColumn,
    summary="Rename, activate or make a Status rate column the default",
    description=(
        "Applies the fields sent. The default column cannot be cleared or deactivated "
        "(409 `STATUS_RATE_COLUMN_IS_DEFAULT`): make another column the default instead. A "
        "column active Statuses are bound to cannot be deactivated (409 "
        "`STATUS_RATE_COLUMN_IN_USE`). Requires AllCompanyBranches scope."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "Status rate column not found"},
        409: {"description": "Default or in-use column"},
        422: {"description": "Validation error"},
    },
)
async def update_status_rate_column(
    column_id: int,
    body: StatusRateColumnUpdate,
    token: TokenDep,
    db: DbDep,
) -> StatusRateColumn:
    return await status_rate_columns.update_status_rate_column(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        column_id=column_id,
        data=body,
        db=db,
    )


@router.delete(
    "/status-rate-columns/{column_id}",
    response_model=StatusRateColumn,
    summary="Deactivate a Status rate column (soft delete)",
    description=(
        "Sets `is_active=false` (idempotent). The column and its rate definition stay for "
        "the Driver schedules and frozen evidence that name them. Refused for the default "
        "column and for a column active Statuses are bound to. Requires AllCompanyBranches "
        "scope."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "Status rate column not found"},
        409: {"description": "Default or in-use column"},
    },
)
async def delete_status_rate_column(
    column_id: int,
    token: TokenDep,
    db: DbDep,
) -> StatusRateColumn:
    return await status_rate_columns.deactivate_status_rate_column(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        column_id=column_id,
        db=db,
    )
