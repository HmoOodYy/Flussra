"""
Pydantic schemas for the admin domain.

Covers:
  - User administration (list, create, update, password reset)
  - Role assignment management (assign, revoke)
  - Roles and permissions catalogue (read-only)
  - Company roles (create, update, delete, permission management)
"""
from datetime import datetime
from pydantic import BaseModel, field_validator


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCOPE_TYPES = {"AllCompanyBranches", "SpecificBranch", "OwnDriverDataOnly"}


# ---------------------------------------------------------------------------
# Role assignment
# ---------------------------------------------------------------------------

class RoleAssignment(BaseModel):
    """
    One row from sec.UserBranchRoles, enriched with role and branch names.
    Returned by role-assignment list and mutate endpoints.
    """
    assignment_id: int
    user_id: int
    role_id: int
    role_code: str
    role_name: str
    scope_type: str            # AllCompanyBranches | SpecificBranch | OwnDriverDataOnly
    branch_id: int | None
    branch_name: str | None
    is_active: bool
    granted_by_user_id: int | None
    granted_at_utc: datetime
    revoked_at_utc: datetime | None
    notes: str | None = None


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class UserAdmin(BaseModel):
    """
    Full user record with all role assignments.
    Returned by list, get, create, update, and password-reset endpoints.
    """
    user_id: int
    company_id: int
    username: str
    display_name: str
    email: str | None = None
    phone: str | None = None
    is_active: bool
    can_login: bool
    must_change_password: bool
    last_login_at_utc: datetime | None = None
    created_at_utc: datetime
    updated_at_utc: datetime | None = None
    role_assignments: list[RoleAssignment]
    # ── Company-role (new path) — None when no active company role is assigned ──
    company_role_assignment_id: int | None = None
    company_role_id: int | None = None
    company_role_code: str | None = None
    company_role_name: str | None = None
    company_role_scope: str | None = None
    company_role_branch_id: int | None = None
    company_role_branch_name: str | None = None
    # ── Driver profile — non-None when the user is linked to a core.Drivers row ──
    driver_id: int | None = None
    # ── Permission codes ──
    # Permissions granted by the current company role (or all perms for Company Owner)
    role_permission_codes: list[str] = []
    # Additional ALLOW overrides granted directly to this user (on top of role)
    extra_permission_codes: list[str] = []


class UserCreate(BaseModel):
    """Payload to create a new user account."""
    username: str
    display_name: str
    password: str
    email: str | None = None
    phone: str | None = None
    is_active: bool = True
    can_login: bool = True
    must_change_password: bool = True

    @field_validator("username")
    @classmethod
    def username_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("username must not be blank")
        return v.strip().lower()

    @field_validator("display_name")
    @classmethod
    def display_name_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("display_name must not be blank")
        return v.strip()

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("password must be at least 8 characters")
        return v


class UserUpdate(BaseModel):
    """
    Partial user update — only explicitly provided fields are applied.

    Omitting a field leaves the column unchanged.
    Sending a field as null clears it (email and phone only).
    Username cannot be changed (stable system identifier).
    Cannot deactivate or remove login from your own account.
    """
    display_name: str | None = None
    email: str | None = None
    phone: str | None = None
    is_active: bool | None = None
    can_login: bool | None = None
    must_change_password: bool | None = None

    @field_validator("display_name")
    @classmethod
    def display_name_non_empty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("display_name must not be blank if provided")
        return v.strip() if v else None


class UserPasswordReset(BaseModel):
    """Payload for admin password reset."""
    new_password: str
    must_change_password: bool = True

    @field_validator("new_password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("new_password must be at least 8 characters")
        return v


# ---------------------------------------------------------------------------
# Role assignment — write
# ---------------------------------------------------------------------------

class RoleAssignmentCreate(BaseModel):
    """Payload to assign a role to a user."""
    role_id: int
    scope_type: str
    branch_id: int | None = None   # required for SpecificBranch / OwnDriverDataOnly
    notes: str | None = None

    @field_validator("scope_type")
    @classmethod
    def scope_valid(cls, v: str) -> str:
        if v not in _SCOPE_TYPES:
            raise ValueError(f"scope_type must be one of {sorted(_SCOPE_TYPES)}")
        return v


# ---------------------------------------------------------------------------
# Roles catalogue (read-only)
# ---------------------------------------------------------------------------

class Role(BaseModel):
    """A role record returned by GET /admin/roles."""
    role_id: int
    role_code: str
    role_name: str
    role_level: int
    is_system_role: bool
    notes: str | None = None


# ---------------------------------------------------------------------------
# Permissions catalogue (read-only)
# ---------------------------------------------------------------------------

class Permission(BaseModel):
    """A permission record returned by GET /admin/permissions."""
    permission_id: int
    permission_code: str
    permission_name: str
    module_code: str
    notes: str | None = None


# ---------------------------------------------------------------------------
# Company Roles (company-scoped role management)
# ---------------------------------------------------------------------------

class CompanyRole(BaseModel):
    """
    One company role.  Returned by list, create, update, and get endpoints.
    """
    company_role_id: int
    company_id: int
    role_code: str
    role_name: str
    role_level: int
    is_default: bool
    is_protected: bool
    is_custom: bool
    is_active: bool
    notes: str | None = None
    created_at_utc: datetime
    updated_at_utc: datetime | None = None
    user_count: int = 0          # active users assigned to this role


class CompanyRoleCreate(BaseModel):
    """
    Payload to create a custom company role.

    The role_code is generated server-side (format: CR_XXXXXXXX) and is NOT
    user-facing.  Only role_name and optional notes are accepted.
    """
    role_name: str
    notes: str | None = None

    @field_validator("role_name")
    @classmethod
    def role_name_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("role_name must not be blank")
        return v.strip()


class CompanyRoleUpdate(BaseModel):
    """Partial update for a company role (name, active state, notes)."""
    role_name: str | None = None
    is_active: bool | None = None
    notes: str | None = None

    @field_validator("role_name")
    @classmethod
    def role_name_non_empty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("role_name must not be blank if provided")
        return v.strip() if v else None


class CompanyRolePermissions(BaseModel):
    """The current permission codes for one company role."""
    company_role_id: int
    permission_codes: list[str]


class CompanyRolePermissionsUpdate(BaseModel):
    """
    Replaces ALL permissions on a company role (PUT semantics).
    Protected roles reject requests that would remove critical permissions.
    """
    permission_codes: list[str]


class CompanyRoleUser(BaseModel):
    """
    One user assignment row for a company role.
    Returned by GET /admin/company-roles/{id}/users.
    """
    assignment_id: int
    user_id: int
    username: str
    display_name: str
    email: str | None = None
    scope_type: str                  # AllCompanyBranches | SpecificBranch | ...
    branch_id: int | None = None
    branch_name: str | None = None
    is_active: bool
    granted_at_utc: datetime


# ---------------------------------------------------------------------------
# Company Role Assignments (new path — stored via companyroleId)
# ---------------------------------------------------------------------------

class CompanyRoleAssignmentCreate(BaseModel):
    """Payload to assign a company role to a user (new-path assignment)."""
    company_role_id: int
    scope_type: str
    branch_id: int | None = None
    notes: str | None = None

    @field_validator("scope_type")
    @classmethod
    def scope_valid(cls, v: str) -> str:
        if v not in _SCOPE_TYPES:
            raise ValueError(f"scope_type must be one of {sorted(_SCOPE_TYPES)}")
        return v


class CompanyRoleAssignmentDetail(BaseModel):
    """
    A company-role-path assignment row, fully enriched.
    Returned by POST and DELETE /admin/users/{id}/company-role-assignments.
    """
    assignment_id: int
    user_id: int
    company_role_id: int
    company_role_code: str
    company_role_name: str
    scope_type: str
    branch_id: int | None = None
    branch_name: str | None = None
    is_active: bool
    granted_by_user_id: int | None = None
    granted_at_utc: datetime
    revoked_at_utc: datetime | None = None
    notes: str | None = None


# ---------------------------------------------------------------------------
# Ownership Transfer
# ---------------------------------------------------------------------------

class OwnerTransferRequest(BaseModel):
    """
    Payload to transfer Company Owner authority to another user.

    The caller must be the current Company Owner.
    ``confirmation`` must be the literal string "TRANSFER".
    ``replacement_company_role_id`` optionally assigns a different company role
    to the outgoing owner after the transfer completes.
    """
    target_user_id: int
    replacement_company_role_id: int | None = None
    confirmation: str

    @field_validator("confirmation")
    @classmethod
    def confirm_transfer(cls, v: str) -> str:
        if v.strip().upper() != "TRANSFER":
            raise ValueError('confirmation must be the exact string "TRANSFER"')
        return v.strip().upper()


# ---------------------------------------------------------------------------
# User Permission Overrides (extra per-member ALLOW permissions)
# ---------------------------------------------------------------------------

class UserPermissionOverridesUpdate(BaseModel):
    """
    Replace all extra ALLOW permission overrides for a user (PUT semantics).

    Codes must exist in sec.permissions.
    Company Owner overrides are blocked (COMPANY_OWNER already has all permissions).
    """
    permission_codes: list[str]


class OwnerTransferResult(BaseModel):
    """Returned after a successful ownership transfer."""
    new_owner_user_id: int
    new_owner_display_name: str
    previous_owner_user_id: int
    previous_owner_display_name: str
    previous_owner_new_role_id: int | None = None
    previous_owner_new_role_name: str | None = None
