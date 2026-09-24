"""
Settings domain router — /settings/company, /settings/branches,
and per-branch payroll setup + status keys.

All endpoints require a valid JWT.
Company and user identity come from the token; access checks are enforced
inside the service layer.

Write endpoints require AllCompanyBranches scope — see _ensure_company_admin().
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncConnection

from app.settings.schemas import (
    BranchAdmin,
    BranchCreate,
    BranchUpdate,
    CompanyProfile,
    CompanyUpdate,
    PayrollSetup,
    PayrollSetupUpsert,
    StatusKey,
    StatusKeyCreate,
    StatusKeyUpdate,
    StatusRateColumn,
    StatusRateColumnCreate,
    BranchPayItemState,
    BranchPayItemConfigVersion,
    PayItemConfigUpdate,
    CustomPayItem,
    CustomPayItemCreate,
    CustomPayItemUpdate,
    CustomPayItemUsage,
    CustomPayItemDeleteResult,
    CustomPayItemRequest,
    CustomPayItemRequestCreate,
    CustomPayItemRequestDecide,
    PayItemRateTypeMapCreate,
    PayItemRateTypeMapSummary,
    BulkPayItemConfigUpdate,
    BulkPayItemConfigResult,
    PayItemOrderUpdate,
)
from app.settings import service
from app.dependencies import get_db, get_current_user

router = APIRouter()

TokenDep = Annotated[dict, Depends(get_current_user)]
DbDep    = Annotated[AsyncConnection, Depends(get_db)]


# ---------------------------------------------------------------------------
# Company profile
# ---------------------------------------------------------------------------

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
        "If `is_default=true`, all other branches lose their default flag "
        "atomically.\n\n"
        "Requires AllCompanyBranches scope."
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
# Branch payroll setup
# ---------------------------------------------------------------------------

@router.get(
    "/branches/{branch_id}/payroll-setup",
    response_model=PayrollSetup,
    summary="Legacy payroll setup route (gone)",
    description=(
        "This legacy branch-owned payroll setup route has been disabled. "
        "Payroll setup is managed through the Phase 2 policy domain."
    ),
    responses={
        410: {"description": "Legacy branch-owned payroll setup route is disabled"},
        403: {"description": "No access to this branch"},
    },
)
async def get_payroll_setup(
    branch_id: int,
    token: TokenDep,
    db: DbDep,
) -> PayrollSetup:
    raise HTTPException(
        status_code=410,
        detail={
            "code": "LEGACY_PAYROLL_SETUP_ROUTE_DISABLED",
            "message": "Branch-owned payroll setup is disabled; use the payroll setup policy domain.",
        },
    )


@router.put(
    "/branches/{branch_id}/payroll-setup",
    response_model=PayrollSetup,
    summary="Legacy payroll setup route (gone)",
    description=(
        "This legacy branch-owned payroll setup route has been disabled. "
        "Payroll setup is managed through the Phase 2 policy domain."
    ),
    responses={
        410: {"description": "Legacy branch-owned payroll setup route is disabled"},
        403: {"description": "Insufficient scope (requires all-branches access)"},
    },
)
async def upsert_payroll_setup(
    branch_id: int,
    body: PayrollSetupUpsert,
    token: TokenDep,
    db: DbDep,
) -> PayrollSetup:
    raise HTTPException(
        status_code=410,
        detail={
            "code": "LEGACY_PAYROLL_SETUP_ROUTE_DISABLED",
            "message": "Branch-owned payroll setup is disabled; use the payroll setup policy domain.",
        },
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


# ---------------------------------------------------------------------------
# Pay items & branch configuration
# ---------------------------------------------------------------------------

@router.get(
    "/branches/{branch_id}/pay-items",
    response_model=list[BranchPayItemState],
    summary="List pay items with branch configuration",
    description=(
        "Returns all non-Retired pay items together with the branch-specific "
        "activation state and notes.  When no config row exists for an item "
        "the item falls back to its system default (`IsDefaultBranchActive`) "
        "and `is_using_default` is true.\n\n"
        "Any authenticated user with branch access can read this."
    ),
    responses={403: {"description": "No access to this branch"}},
)
async def list_pay_items(
    branch_id: int,
    token: TokenDep,
    db: DbDep,
) -> list[BranchPayItemState]:
    return await service.get_pay_items(
        branch_id=branch_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.patch(
    "/branches/pay-items/{item_id}/bulk-config",
    response_model=BulkPayItemConfigResult,
    summary="Bulk-update branch configuration for a pay item",
    description=(
        "Applies a single pay item configuration change atomically across one or more "
        "branches **within a single transaction**.\n\n"
        "**target = AllBranches**: updates every active branch in the company.\n\n"
        "**target = SelectedBranches**: updates only the listed `branch_ids`; all must "
        "belong to this company.\n\n"
        "**Atomicity**: all target branches are validated before any write.  If any "
        "branch fails validation (e.g. the supplied `effective_from` falls inside an "
        "open payroll period for that branch), the entire request is rejected with "
        "HTTP 422 and a per-branch error list.  No changes are written.\n\n"
        "Once the validation phase passes, all writes occur in the same database "
        "transaction — a write failure automatically rolls back every branch.\n\n"
        "Effective-date versioning follows the same rules as the single-branch PATCH. "
        "Omit `effective_from` to let the backend schedule a safe date per branch "
        "(today, or day after any open period).\n\n"
        "Requires AllCompanyBranches scope + `setup.manage`."
    ),
    responses={
        403: {"description": "Insufficient scope (requires AllCompanyBranches + setup.manage)"},
        404: {"description": "Pay item not found or retired"},
        422: {
            "description": (
                "Validation error.  When branch-level period-protection fails the "
                "response body contains `branch_errors` with per-branch detail messages."
            )
        },
    },
)
async def bulk_update_pay_item_config(
    item_id: int,
    body: BulkPayItemConfigUpdate,
    token: TokenDep,
    db: DbDep,
) -> BulkPayItemConfigResult:
    return await service.bulk_update_pay_item_config(
        pay_item_id=item_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.patch(
    "/branches/{branch_id}/pay-items/{item_id}",
    response_model=BranchPayItemState,
    summary="Update branch configuration for a pay item",
    description=(
        "Sets the branch-level `is_active` flag and `notes` for a pay item, "
        "with full effective-dated versioning.\n\n"
        "**Period-protection rules:**\n"
        "- If a payroll period is currently open (its date range contains today), "
        "a config change cannot take effect during that period.\n"
        "- When `effective_from` is omitted and an open period exists, the change "
        "is automatically scheduled to `current_period_end_date + 1` (the day "
        "after the period closes).  This assumes periods are contiguous; supply "
        "`effective_from` explicitly if your schedule has gaps between periods.\n"
        "- When `effective_from` is supplied but falls on or before the current "
        "open period's end date, the request is rejected with HTTP 422.\n"
        "- A future open period whose start date is after today does **not** block "
        "a change applied today.\n\n"
        "**Versioning semantics:**\n"
        "- No existing config row → creates a new row effective from `effective_from`.\n"
        "- Same effective date as the existing open row → updates in place (amendment).\n"
        "- Later effective date than the existing open row → closes the existing row "
        "and inserts a new one (version history preserved).\n"
        "- Earlier effective date than a pending future row → replaces the pending "
        "row in place.\n\n"
        "**Response fields:**\n"
        "- `current_config`: the config whose date range covers today.\n"
        "- `pending_config`: a future-dated config not yet in effect (if any).\n"
        "- `has_open_periods`: `true` when period-protection is currently active.\n\n"
        "Requires AllCompanyBranches scope."
    ),
    responses={
        403: {"description": "Insufficient scope (requires all-branches access)"},
        404: {"description": "Pay item not found or retired"},
        422: {
            "description": (
                "Validation error, or effective_from falls inside the current "
                "open payroll period"
            )
        },
    },
)
async def update_pay_item_config(
    branch_id: int,
    item_id: int,
    body: PayItemConfigUpdate,
    token: TokenDep,
    db: DbDep,
) -> BranchPayItemState:
    return await service.update_pay_item_config(
        pay_item_id=item_id,
        branch_id=branch_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.get(
    "/branches/{branch_id}/pay-items/{item_id}/history",
    response_model=list[BranchPayItemConfigVersion],
    summary="Get the full config version history for a pay item",
    description=(
        "Returns all BranchPayItemConfig rows (open + closed) for this "
        "branch + item, ordered newest-first.  Allows the admin UI to show "
        "when and how the config was changed over time.\n\n"
        "Any authenticated user with branch access can read this."
    ),
    responses={
        403: {"description": "No access to this branch"},
        404: {"description": "Branch not found"},
    },
)
async def get_pay_item_config_history(
    branch_id: int,
    item_id: int,
    token: TokenDep,
    db: DbDep,
) -> list[BranchPayItemConfigVersion]:
    return await service.get_pay_item_config_history(
        pay_item_id=item_id,
        branch_id=branch_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.get(
    "/branches/{branch_id}/pay-items/missing",
    response_model=list[str],
    summary="List pay item codes with no branch configuration",
    description=(
        "Returns pay item codes that have no open BranchPayItemConfig row "
        "for this branch.  An empty list means every item is explicitly "
        "configured.  Used by the admin repair banner in the Settings UI.\n\n"
        "Requires AllCompanyBranches scope."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "Branch not found"},
    },
)
async def list_missing_pay_item_configs(
    branch_id: int,
    token: TokenDep,
    db: DbDep,
) -> list[str]:
    return await service.get_missing_pay_item_configs(
        branch_id=branch_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


# ---------------------------------------------------------------------------
# M12: Custom Pay Items — admin catalog endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/pay-items",
    response_model=list[CustomPayItem],
    summary="List all custom pay items for the company",
    description=(
        "Returns company-owned custom pay items (non-system items).  "
        "By default excludes Retired items; pass `include_retired=true` for the "
        "full catalog including retired items.\n\n"
        "Requires AllCompanyBranches scope."
    ),
    responses={403: {"description": "Insufficient scope (requires all-branches access)"}},
)
async def list_custom_pay_items(
    token: TokenDep,
    db: DbDep,
    include_retired: bool = Query(False, description="Include retired (archived) items"),
) -> list[CustomPayItem]:
    return await service.get_custom_pay_items(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        include_retired=include_retired,
    )


@router.post(
    "/pay-items",
    response_model=CustomPayItem,
    status_code=201,
    summary="Create a custom pay item (admin direct)",
    description=(
        "Admin-direct path: creates a company-level custom pay item without "
        "the branch request/approval flow.\n\n"
        "M12 scope rules:\n"
        "- `item_scope = 'Daily'`  → `rate_behavior` must be `'PerUnit'`; `unit` is required.\n"
        "- `item_scope = 'Period'` → `rate_behavior` must be `'EnteredAmount'`; `unit` is omitted.\n\n"
        "The item starts **inactive on all branches** (IsDefaultBranchActive=FALSE, no "
        "BranchPayItemConfig row created).  Branches activate it via "
        "`PATCH /settings/branches/{id}/pay-items/{item_id}`.\n\n"
        "Requires AllCompanyBranches scope + `setup.manage`."
    ),
    responses={
        403: {"description": "Insufficient scope or permission"},
        422: {"description": "Validation error, duplicate code, or system-reserved code"},
    },
)
async def create_custom_pay_item(
    body: CustomPayItemCreate,
    token: TokenDep,
    db: DbDep,
) -> CustomPayItem:
    return await service.create_custom_pay_item(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.get(
    "/pay-items/{item_id}",
    response_model=CustomPayItem,
    summary="Get a single custom pay item",
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "Custom pay item not found"},
    },
)
async def get_custom_pay_item(
    item_id: int,
    token: TokenDep,
    db: DbDep,
) -> CustomPayItem:
    return await service.get_custom_pay_item_by_id(
        item_id=item_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.patch(
    "/pay-items/{item_id}",
    response_model=CustomPayItem,
    summary="Update mutable metadata on a custom pay item",
    description=(
        "Applies non-null fields from the request body.  "
        "Mutable fields: `display_label`, `pay_item_name`, `category`, `unit`, "
        "`sort_order`, `notes`.\n\n"
        "Immutable after creation: `pay_item_code`, `item_scope`, `rate_behavior` "
        "— these define the meaning of historical payroll lines and cannot be changed.\n\n"
        "System items and Retired items cannot be updated through this endpoint.\n\n"
        "Requires AllCompanyBranches scope + `setup.manage`."
    ),
    responses={
        403: {"description": "Insufficient scope or permission"},
        404: {"description": "Custom pay item not found"},
        422: {"description": "Attempt to update system or Retired item, or immutable field"},
    },
)
async def update_custom_pay_item(
    item_id: int,
    body: CustomPayItemUpdate,
    token: TokenDep,
    db: DbDep,
) -> CustomPayItem:
    return await service.update_custom_pay_item(
        item_id=item_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.get(
    "/pay-items/{item_id}/usage",
    response_model=CustomPayItemUsage,
    summary="Check usage of a custom pay item before deletion",
    description=(
        "Returns draft-line and final-line usage counts for a custom pay item.  "
        "`can_physical_delete=true` means no meaningful usage exists and the item "
        "can be removed from the database.  `deletion_would_retire=true` means "
        "meaningful or finalized lines exist and deletion will archive the item "
        "(Status=Retired) rather than remove it.\n\n"
        "Requires AllCompanyBranches scope."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "Custom pay item not found"},
    },
)
async def get_custom_pay_item_usage(
    item_id: int,
    token: TokenDep,
    db: DbDep,
) -> CustomPayItemUsage:
    return await service.get_custom_pay_item_usage(
        item_id=item_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.delete(
    "/pay-items/{item_id}",
    response_model=CustomPayItemDeleteResult,
    summary="Delete or retire a custom pay item",
    description=(
        "Smart delete — the actual action depends on usage history:\n\n"
        "- **Never used** → physical delete (row removed).\n"
        "- **Only empty/voided draft lines** → those lines are cleaned up, "
        "then physical delete.\n"
        "- **Meaningful usage** (any active draft line with quantity/amount, "
        "or any final payroll line) → item is **retired** (Status=Retired).  "
        "The code is permanently locked to protect historical records.\n\n"
        "System items (`is_system_standard=true`) cannot be deleted.\n"
        "Calling DELETE on an already-Retired item is idempotent.\n\n"
        "Requires AllCompanyBranches scope + `setup.manage`."
    ),
    responses={
        403: {"description": "Insufficient scope or permission"},
        404: {"description": "Custom pay item not found"},
        422: {"description": "System pay items cannot be deleted"},
    },
)
async def delete_custom_pay_item(
    item_id: int,
    token: TokenDep,
    db: DbDep,
) -> CustomPayItemDeleteResult:
    return await service.delete_custom_pay_item(
        item_id=item_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


# ---------------------------------------------------------------------------
# M12: Custom Pay Items — branch request / admin approval endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/pay-item-requests",
    response_model=CustomPayItemRequest,
    status_code=201,
    summary="Submit a branch request for a new custom pay item",
    description=(
        "A branch user submits a request for a new company-level custom pay item.  "
        "The request enters `PendingApproval` status and must be approved by an "
        "admin before the item is created.\n\n"
        "Duplicate requests: if a `PendingApproval` or `Approved` request already "
        "exists for the same `pay_item_code` in this company, the request is "
        "rejected with HTTP 422.\n\n"
        "Requires branch access + `payroll.entry` permission on the requesting branch."
    ),
    responses={
        403: {"description": "No branch access or missing payroll.entry permission"},
        422: {"description": "Validation error, duplicate code, or system-reserved code"},
    },
)
async def submit_pay_item_request(
    body: CustomPayItemRequestCreate,
    token: TokenDep,
    db: DbDep,
) -> CustomPayItemRequest:
    return await service.create_pay_item_request(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.get(
    "/pay-item-requests",
    response_model=list[CustomPayItemRequest],
    summary="List custom pay item requests",
    description=(
        "Admin (AllCompanyBranches) sees all requests for the company.  "
        "Branch-scoped users see only their own branch's requests.  "
        "Optionally filter by `status` (PendingApproval | Approved | Rejected)."
    ),
    responses={403: {"description": "No branch access"}},
)
async def list_pay_item_requests(
    token: TokenDep,
    db: DbDep,
    request_status: str | None = Query(None, alias="status",
                                       description="Filter by status: PendingApproval, Approved, or Rejected"),
) -> list[CustomPayItemRequest]:
    return await service.get_pay_item_requests(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        request_status=request_status,
    )


@router.get(
    "/pay-item-requests/{request_id}",
    response_model=CustomPayItemRequest,
    summary="Get a single custom pay item request",
    responses={
        403: {"description": "No access to this request's branch"},
        404: {"description": "Request not found"},
    },
)
async def get_pay_item_request(
    request_id: int,
    token: TokenDep,
    db: DbDep,
) -> CustomPayItemRequest:
    return await service.get_pay_item_request_by_id(
        request_id=request_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.post(
    "/pay-item-requests/{request_id}/decide",
    response_model=CustomPayItemRequest,
    summary="Approve or reject a custom pay item request",
    description=(
        "Admin decision on a `PendingApproval` request.\n\n"
        "**Approved**: atomically creates the pay item (company-level, Active) and "
        "a `BranchPayItemConfig` row for the requesting branch (IsActive=TRUE, "
        "effective today).  All other branches start inactive.  They can activate "
        "the item later via `PATCH /settings/branches/{id}/pay-items/{item_id}`.\n\n"
        "**Rejected**: marks the request as Rejected; no pay item is created.  "
        "The same `pay_item_code` can be re-requested after rejection.\n\n"
        "Attempting to decide an already-Approved or Rejected request returns HTTP 422.\n\n"
        "Requires AllCompanyBranches scope + `setup.manage`."
    ),
    responses={
        403: {"description": "Insufficient scope or permission"},
        404: {"description": "Request not found"},
        422: {"description": "Request already decided, or code conflict on approval"},
    },
)
async def decide_pay_item_request(
    request_id: int,
    body: CustomPayItemRequestDecide,
    token: TokenDep,
    db: DbDep,
) -> CustomPayItemRequest:
    return await service.decide_pay_item_request(
        request_id=request_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


# ---------------------------------------------------------------------------
# Pay item ordering
# ---------------------------------------------------------------------------

@router.patch(
    "/pay-items/order",
    status_code=204,
    summary="Save the display order of pay items",
    description=(
        "Replaces the `sort_order` of each listed pay item in one atomic "
        "transaction.  Send the complete ordered list after the user has "
        "rearranged items via drag-and-drop.\n\n"
        "All `pay_item_ids` must be valid and visible to this company "
        "(system items or company-owned custom items).  Retired items and "
        "IDs belonging to other companies are rejected with HTTP 422 before "
        "any writes occur.\n\n"
        "**Note**: `sort_order` is stored on the catalog-level `PayItems` "
        "table, which is shared across all branches.  Reordering affects the "
        "display order in every branch simultaneously.\n\n"
        "Requires AllCompanyBranches scope + `setup.manage`."
    ),
    responses={
        403: {"description": "Insufficient scope or permission"},
        422: {"description": "Invalid or inaccessible pay_item_id(s)"},
    },
)
async def update_pay_item_order(
    body: PayItemOrderUpdate,
    token: TokenDep,
    db: DbDep,
) -> None:
    await service.update_pay_item_order(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


# ---------------------------------------------------------------------------
# M13: Assign a rate type to a custom PerUnit pay item
# ---------------------------------------------------------------------------

@router.post(
    "/pay-items/{item_id}/rate-type-map",
    response_model=PayItemRateTypeMapSummary,
    status_code=201,
    summary="Assign a rate type to a custom PerUnit pay item",
    description=(
        "Creates or updates the `PayItemRateTypeMap` entry for a custom PerUnit pay item.\n\n"
        "This mapping is required for the calculation engine to look up the driver's "
        "approved `DriverRate` when inserting daily draft lines for the item.  "
        "Without this mapping the engine sets `calculatedamount = NULL` and flags "
        "`needs_manager_review = True`.\n\n"
        "Idempotent: calling this endpoint twice for the same `(item_id, rate_type_id)` "
        "pair just refreshes the row.\n\n"
        "Requires AllCompanyBranches scope + `setup.manage`."
    ),
    responses={
        403: {"description": "Insufficient scope or permission"},
        404: {"description": "Pay item not found for this company"},
        422: {"description": "Item is not PerUnit, or rate_type_id is inactive"},
    },
)
async def assign_rate_type_to_pay_item(
    item_id: int,
    body: PayItemRateTypeMapCreate,
    token: TokenDep,
    db: DbDep,
) -> PayItemRateTypeMapSummary:
    return await service.assign_rate_type_to_pay_item(
        item_id=item_id,
        data=body,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )
