"""
Admin domain router — /admin/users, /admin/roles, /admin/permissions.

All endpoints require a valid JWT.
All endpoints require AllCompanyBranches scope — enforced inside the service.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncConnection

from app.admin.schemas import (
    UserAdmin,
    UserCreate,
    UserUpdate,
    UserPasswordReset,
    RoleAssignment,
    RoleAssignmentCreate,
    Role,
    Permission,
    CompanyRole,
    CompanyRoleCreate,
    CompanyRoleUpdate,
    CompanyRolePermissions,
    CompanyRolePermissionsUpdate,
    CompanyRoleUser,
    CompanyRoleAssignmentCreate,
    CompanyRoleAssignmentDetail,
    UserPermissionOverridesUpdate,
    OwnerTransferRequest,
    OwnerTransferResult,
)
from app.admin import service
from app.dependencies import get_db, get_current_user

router = APIRouter()

TokenDep = Annotated[dict, Depends(get_current_user)]
DbDep    = Annotated[AsyncConnection, Depends(get_db)]


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@router.get(
    "/users",
    response_model=list[UserAdmin],
    summary="List all users for this company",
    description=(
        "Returns all users belonging to the authenticated user's company, "
        "each with their full role-assignment list.  By default only active "
        "users are returned; pass `include_inactive=true` to include "
        "deactivated accounts.\n\n"
        "Requires: `users.view` OR `settings.manage` OR `setup.manage`."
    ),
    responses={403: {"description": "Insufficient scope"}},
)
async def list_users(
    token: TokenDep,
    db: DbDep,
    include_inactive: bool = Query(False, description="Include inactive (deactivated) users"),
) -> list[UserAdmin]:
    return await service.list_users(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        include_inactive=include_inactive,
    )


@router.get(
    "/users/{user_id}",
    response_model=UserAdmin,
    summary="Get a single user with all role assignments",
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "User not found"},
    },
)
async def get_user(
    user_id: int,
    token: TokenDep,
    db: DbDep,
) -> UserAdmin:
    return await service.get_user_by_id(
        target_user_id=user_id,
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        db=db,
    )


@router.post(
    "/users",
    response_model=UserAdmin,
    status_code=201,
    summary="Create a new user account",
    description=(
        "Creates a new user for this company.  The username must be unique "
        "within the company (case-insensitive).  Password is bcrypt-hashed "
        "before storage.  No role assignments are created — use "
        "`POST /admin/users/{id}/company-role-assignments` to grant access.\n\n"
        "Requires: `users.create` OR `settings.manage` OR `setup.manage`."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        422: {"description": "Validation error or duplicate username"},
    },
)
async def create_user(
    body: UserCreate,
    token: TokenDep,
    db: DbDep,
) -> UserAdmin:
    return await service.create_user(
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.patch(
    "/users/{user_id}",
    response_model=UserAdmin,
    summary="Partially update a user account",
    description=(
        "Applies non-null fields from the request body.  Username cannot be changed.  "
        "Callers cannot deactivate their own account or remove their own login access.\n\n"
        "Field-sensitive permissions:\n"
        "- `display_name`, `email`, `phone`, `can_login`, `must_change_password`: "
        "requires `users.edit` OR fallbacks.\n"
        "- `is_active` alone: requires `users.deactivate` OR `users.edit` OR fallbacks.\n"
        "- Mixed payloads (profile + is_active): requires `users.edit` OR fallbacks."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "User not found"},
        422: {"description": "Validation error or self-deactivation attempt"},
    },
)
async def update_user(
    user_id: int,
    body: UserUpdate,
    token: TokenDep,
    db: DbDep,
) -> UserAdmin:
    return await service.update_user(
        target_user_id=user_id,
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.post(
    "/users/{user_id}/reset-password",
    response_model=UserAdmin,
    summary="Admin password reset",
    description=(
        "Sets a new bcrypt password for the user.  Also clears any account "
        "lock and resets the failed-login counter.  By default sets "
        "`must_change_password=true` so the user is prompted on next login.\n\n"
        "Requires AllCompanyBranches scope and granular permissions (see endpoint summary)."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "User not found"},
        422: {"description": "Password too short (< 8 chars)"},
    },
)
async def reset_password(
    user_id: int,
    body: UserPasswordReset,
    token: TokenDep,
    db: DbDep,
) -> UserAdmin:
    return await service.reset_password(
        target_user_id=user_id,
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        data=body,
        db=db,
    )


# ---------------------------------------------------------------------------
# Driver profile info
# ---------------------------------------------------------------------------

@router.get(
    "/users/{user_id}/driver",
    summary="Get driver profile info for a user",
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "User not found"},
    },
)
async def get_user_driver(
    user_id: int,
    token: TokenDep,
    db: DbDep,
) -> dict:
    return await service.get_user_driver_info(
        user_id=user_id,
        company_id=int(token["cid"]),
        caller_user_id=int(token["sub"]),
        db=db,
    )


# ---------------------------------------------------------------------------
# Role assignments
# ---------------------------------------------------------------------------

@router.get(
    "/users/{user_id}/roles",
    response_model=list[RoleAssignment],
    summary="List all role assignments for a user",
    description=(
        "Returns both active and revoked role assignments so the admin can "
        "see the full access history.  Filter by `is_active` client-side.\n\n"
        "Requires AllCompanyBranches scope and granular permissions (see endpoint summary)."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "User not found"},
    },
)
async def list_user_roles(
    user_id: int,
    token: TokenDep,
    db: DbDep,
) -> list[RoleAssignment]:
    return await service.list_user_roles(
        target_user_id=user_id,
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        db=db,
    )


@router.post(
    "/users/{user_id}/roles",
    response_model=RoleAssignment,
    status_code=201,
    summary="Assign a role to a user",
    description=(
        "Creates a new role assignment.  For `AllCompanyBranches` scope, "
        "`branch_id` must be null.  For `SpecificBranch` or "
        "`OwnDriverDataOnly` scope, `branch_id` is required and must belong "
        "to this company.  A duplicate active assignment (same user + role + "
        "scope + branch) is rejected with 422.\n\n"
        "Requires AllCompanyBranches scope and granular permissions (see endpoint summary)."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "User not found"},
        422: {"description": "Validation error, invalid role, or duplicate assignment"},
    },
)
async def assign_role(
    user_id: int,
    body: RoleAssignmentCreate,
    token: TokenDep,
    db: DbDep,
) -> RoleAssignment:
    return await service.assign_role(
        target_user_id=user_id,
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.delete(
    "/users/{user_id}/roles/{assignment_id}",
    response_model=RoleAssignment,
    summary="Revoke a role assignment (soft delete)",
    description=(
        "Sets `is_active=false` and `revoked_at_utc=NOW()`.  The row is not "
        "removed; it remains visible via `GET /admin/users/{id}/roles`.  "
        "Idempotent: calling DELETE on an already-revoked assignment returns "
        "it unchanged.\n\n"
        "Requires AllCompanyBranches scope and granular permissions (see endpoint summary)."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "Role assignment not found"},
    },
)
async def revoke_role(
    user_id: int,
    assignment_id: int,
    token: TokenDep,
    db: DbDep,
) -> RoleAssignment:
    return await service.revoke_role(
        assignment_id=assignment_id,
        target_user_id=user_id,
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        db=db,
    )


# ---------------------------------------------------------------------------
# Roles catalogue (read-only)
# ---------------------------------------------------------------------------

@router.get(
    "/roles",
    response_model=list[Role],
    summary="List all roles",
    description=(
        "Returns all roles (system and custom).  Used to populate the "
        "role picker when assigning access.\n\n"
        "Requires AllCompanyBranches scope and granular permissions (see endpoint summary)."
    ),
    responses={403: {"description": "Insufficient scope"}},
)
async def list_roles(
    token: TokenDep,
    db: DbDep,
) -> list[Role]:
    return await service.list_roles(
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        db=db,
    )


# ---------------------------------------------------------------------------
# Permissions catalogue (read-only)
# ---------------------------------------------------------------------------

@router.get(
    "/permissions",
    response_model=list[Permission],
    summary="List permission codes",
    description=(
        "Returns permission codes grouped by module.  By default (`ui_only=true`) "
        "legacy/internal codes (modules: core, review, and legacy payroll codes) "
        "are excluded so the UI only shows the current permission model.  "
        "Pass `ui_only=false` to include all codes.\n\n"
        "Requires AllCompanyBranches scope and granular permissions (see endpoint summary)."
    ),
    responses={403: {"description": "Insufficient scope"}},
)
async def list_permissions(
    token: TokenDep,
    db: DbDep,
    ui_only: bool = Query(True, description="Exclude legacy/internal permission codes"),
) -> list[Permission]:
    return await service.list_permissions(
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        db=db,
        ui_only=ui_only,
    )


# ---------------------------------------------------------------------------
# Company Roles
# ---------------------------------------------------------------------------

@router.get(
    "/company-roles",
    response_model=list[CompanyRole],
    summary="List all company roles",
    description=(
        "Returns all roles for this company — both default/protected roles "
        "(Company Owner, Driver) and any custom roles created by the company. "
        "Includes the count of active users assigned to each role.\n\n"
        "Requires: `roles.view` OR `settings.manage` OR `setup.manage`."
    ),
    responses={403: {"description": "Insufficient scope"}},
)
async def list_company_roles(
    token: TokenDep,
    db: DbDep,
) -> list[CompanyRole]:
    return await service.list_company_roles(
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        db=db,
    )


@router.post(
    "/company-roles",
    response_model=CompanyRole,
    status_code=201,
    summary="Create a custom company role",
    description=(
        "Creates a new custom role for this company.  The role code must be "
        "unique within the company.  If `role_code` is omitted it is derived "
        "from `role_name` (uppercase, spaces replaced with underscores).\n\n"
        "Requires AllCompanyBranches scope and granular permissions (see endpoint summary)."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        422: {"description": "Validation error or duplicate role code"},
    },
)
async def create_company_role(
    body: CompanyRoleCreate,
    token: TokenDep,
    db: DbDep,
) -> CompanyRole:
    return await service.create_company_role(
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.get(
    "/company-roles/{role_id}",
    response_model=CompanyRole,
    summary="Get a single company role",
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "Role not found"},
    },
)
async def get_company_role(
    role_id: int,
    token: TokenDep,
    db: DbDep,
) -> CompanyRole:
    return await service.get_company_role(
        company_role_id=role_id,
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        db=db,
    )


@router.patch(
    "/company-roles/{role_id}",
    response_model=CompanyRole,
    summary="Update a company role (name, active state, notes)",
    description=(
        "Updates non-structural fields on a role.  The role code is immutable. "
        "Protected and default flags are system-managed and cannot be changed.\n\n"
        "Requires AllCompanyBranches scope and granular permissions (see endpoint summary)."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "Role not found"},
    },
)
async def update_company_role(
    role_id: int,
    body: CompanyRoleUpdate,
    token: TokenDep,
    db: DbDep,
) -> CompanyRole:
    return await service.update_company_role(
        company_role_id=role_id,
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.delete(
    "/company-roles/{role_id}",
    status_code=204,
    summary="Archive (soft-delete) a custom company role",
    description=(
        "Archives a custom role — the row is preserved in the database for historical "
        "references but disappears from all normal role lists and cannot be assigned.\n\n"
        "Blocked if: (a) the role is protected (Company Owner, Driver), or "
        "(b) the role has active user assignments — reassign those users first.\n\n"
        "Requires AllCompanyBranches scope and granular permissions (see endpoint summary)."
    ),
    responses={
        204: {"description": "Role archived"},
        403: {"description": "Insufficient scope"},
        404: {"description": "Role not found"},
        422: {"description": "Role is protected or has active assignments"},
    },
)
async def delete_company_role(
    role_id: int,
    token: TokenDep,
    db: DbDep,
) -> None:
    await service.delete_company_role(
        company_role_id=role_id,
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        db=db,
    )


@router.get(
    "/company-roles/{role_id}/permissions",
    response_model=CompanyRolePermissions,
    summary="Get permissions for a company role",
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "Role not found"},
    },
)
async def get_company_role_permissions(
    role_id: int,
    token: TokenDep,
    db: DbDep,
) -> CompanyRolePermissions:
    return await service.get_company_role_permissions(
        company_role_id=role_id,
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        db=db,
    )


@router.get(
    "/company-roles/{role_id}/users",
    response_model=list[CompanyRoleUser],
    summary="List users assigned to a company role",
    description=(
        "Returns all user assignments (active and inactive) for this company role, "
        "including scope and branch details.  Read-only — use the role-assignment "
        "endpoints to modify assignments.\n\n"
        "Requires AllCompanyBranches scope and granular permissions (see endpoint summary)."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "Role not found"},
    },
)
async def get_company_role_users(
    role_id: int,
    token: TokenDep,
    db: DbDep,
) -> list[CompanyRoleUser]:
    return await service.get_company_role_users(
        company_role_id=role_id,
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        db=db,
    )


# ---------------------------------------------------------------------------
# User Permission Overrides (member-specific extra ALLOW permissions)
# ---------------------------------------------------------------------------

@router.get(
    "/users/{user_id}/permission-overrides",
    response_model=list[str],
    summary="Get extra permission overrides for a user",
    description=(
        "Returns the active ALLOW permission codes granted directly to this user, "
        "on top of their company role permissions.  Company Owner always has all "
        "permissions — overrides for Company Owner cannot be set.\n\n"
        "Requires AllCompanyBranches scope and granular permissions (see endpoint summary)."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "User not found"},
    },
)
async def get_user_permission_overrides(
    user_id: int,
    token: TokenDep,
    db: DbDep,
) -> list[str]:
    return await service.get_user_permission_overrides(
        target_user_id=user_id,
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        db=db,
    )


@router.put(
    "/users/{user_id}/permission-overrides",
    response_model=list[str],
    summary="Replace extra permission overrides for a user",
    description=(
        "Replaces ALL active ALLOW overrides for this user (PUT semantics). "
        "Send an empty list to clear all overrides. "
        "All codes must exist in the permission catalogue. "
        "Parent permissions are auto-added for any dependency (e.g. payroll.edit → payroll.view).\n\n"
        "Company Owner cannot have overrides set — they already have full permissions.\n\n"
        "Requires AllCompanyBranches scope and granular permissions (see endpoint summary)."
    ),
    responses={
        200: {"description": "Overrides replaced"},
        403: {"description": "Insufficient scope"},
        404: {"description": "User not found"},
        422: {"description": "Unknown permission codes or Company Owner blocked"},
    },
)
async def set_user_permission_overrides(
    user_id: int,
    body: UserPermissionOverridesUpdate,
    token: TokenDep,
    db: DbDep,
) -> list[str]:
    return await service.set_user_permission_overrides(
        target_user_id=user_id,
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        data=body,
        db=db,
    )


# ---------------------------------------------------------------------------
# Company Role Assignments (new path — stored via companyroleId)
# ---------------------------------------------------------------------------

@router.post(
    "/users/{user_id}/company-role-assignments",
    response_model=CompanyRoleAssignmentDetail,
    status_code=201,
    summary="Assign a company role to a user (new path)",
    description=(
        "Assigns a company role to the user using the new companyroleId path. "
        "Any existing active company-role assignment for the user is automatically "
        "revoked (a user holds exactly one company role at a time). "
        "Company Owner cannot be assigned via this endpoint — use "
        "POST /admin/company-owner/transfer instead.\n\n"
        "Requires AllCompanyBranches scope and granular permissions (see endpoint summary)."
    ),
    responses={
        201: {"description": "Assignment created"},
        403: {"description": "Insufficient scope"},
        404: {"description": "User or role not found"},
        422: {"description": "Validation error, Company Owner blocked, or scope mismatch"},
    },
)
async def assign_company_role(
    user_id: int,
    body: CompanyRoleAssignmentCreate,
    token: TokenDep,
    db: DbDep,
) -> CompanyRoleAssignmentDetail:
    return await service.assign_company_role(
        target_user_id=user_id,
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.delete(
    "/users/{user_id}/company-role-assignments/{assignment_id}",
    response_model=CompanyRoleAssignmentDetail,
    summary="Revoke a company-role assignment (soft delete)",
    description=(
        "Sets the assignment isactive=FALSE.  Company Owner assignments cannot "
        "be revoked here — use POST /admin/company-owner/transfer.\n\n"
        "Requires AllCompanyBranches scope and granular permissions (see endpoint summary)."
    ),
    responses={
        200: {"description": "Assignment revoked"},
        403: {"description": "Insufficient scope"},
        404: {"description": "Assignment not found"},
        422: {"description": "Cannot revoke Company Owner assignment"},
    },
)
async def revoke_company_role_assignment(
    user_id: int,
    assignment_id: int,
    token: TokenDep,
    db: DbDep,
) -> CompanyRoleAssignmentDetail:
    return await service.revoke_company_role_assignment(
        assignment_id=assignment_id,
        target_user_id=user_id,
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        db=db,
    )


# ---------------------------------------------------------------------------
# Ownership Transfer
# ---------------------------------------------------------------------------

@router.post(
    "/company-owner/transfer",
    response_model=OwnerTransferResult,
    summary="Transfer Company Owner authority to another user",
    description=(
        "Transfers Company Owner to another active user. "
        "Only the current Company Owner can call this. "
        "``confirmation`` must be the literal string \"TRANSFER\". "
        "Optionally assigns a replacement company role to the outgoing owner.\n\n"
        "Requires AllCompanyBranches scope and granular permissions (see endpoint summary)."
    ),
    responses={
        200: {"description": "Ownership transferred"},
        403: {"description": "Caller is not Company Owner"},
        404: {"description": "Target user not found"},
        422: {"description": "Validation error (inactive target, wrong confirmation, etc.)"},
    },
)
async def transfer_company_owner(
    body: OwnerTransferRequest,
    token: TokenDep,
    db: DbDep,
) -> OwnerTransferResult:
    return await service.transfer_company_owner(
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.put(
    "/company-roles/{role_id}/permissions",
    response_model=CompanyRolePermissions,
    summary="Replace all permissions for a company role",
    description=(
        "Replaces the complete permission set on a role (PUT semantics). "
        "Protected roles reject requests that would remove their required "
        "critical permissions (e.g. Company Owner must always keep "
        "`settings.manage`, `roles.edit`, `users.view`).\n\n"
        "All provided permission codes must exist in the catalogue "
        "(GET /admin/permissions).\n\n"
        "Requires AllCompanyBranches scope and granular permissions (see endpoint summary)."
    ),
    responses={
        403: {"description": "Insufficient scope"},
        404: {"description": "Role not found"},
        422: {"description": "Unknown permission code or protected role violation"},
    },
)
async def set_company_role_permissions(
    role_id: int,
    body: CompanyRolePermissionsUpdate,
    token: TokenDep,
    db: DbDep,
) -> CompanyRolePermissions:
    return await service.set_company_role_permissions(
        company_role_id=role_id,
        company_id=int(token["cid"]),
        caller_id=int(token["sub"]),
        data=body,
        db=db,
    )
