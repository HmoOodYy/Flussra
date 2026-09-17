"""
Pydantic schemas for the auth domain.

These define the exact shape of every auth request body and response.
FastAPI validates inputs against these schemas before any service code runs.
"""
from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    username: str
    password: str
    company_code: str

    @field_validator("username", "company_code")
    @classmethod
    def must_not_be_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("field must not be blank")
        return v.strip()

    @field_validator("password")
    @classmethod
    def password_must_not_be_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("password must not be empty")
        return v


class BranchAccess(BaseModel):
    """One branch (or company-wide scope) the user can access."""
    branch_id: int | None       # None for AllCompanyBranches scope
    branch_name: str | None     # None when branch_id is None
    scope: str                  # AllCompanyBranches | SpecificBranch | OwnDriverDataOnly
    role_code: str
    role_name: str


class BranchPermissions(BaseModel):
    """Effective permission codes for one distinct concrete branch."""
    branch_id: int
    permissions: list[str]


class PermissionAuthority(BaseModel):
    """
    Backend-computed, branch-aware permission authority.

    Distinct from `active_permissions` (a flat union kept for existing
    consumers).  Every list here is produced by evaluating each catalogue
    permission through sec.fn_UserHasPermission — the frontend must not
    reconstruct role/override semantics from this data.
    """
    company_permissions: list[str] = []
    """
    Permissions effective at company scope (branch_id=NULL).  Only
    populated when the user holds an active AllCompanyBranches assignment;
    otherwise empty — company-scope actions must be denied.
    """
    branch_permissions: list[BranchPermissions] = []
    """
    One entry per distinct concrete branch backing an active SpecificBranch
    or OwnDriverDataOnly assignment.  Each branch's permission list already
    includes any company-wide grant (fn_UserHasPermission unions across all
    of the user's active assignments for that branch).
    """


class UserInfo(BaseModel):
    """The logged-in user's identity, access list, and active permissions."""
    user_id: int
    username: str
    display_name: str
    company_id: int
    company_name: str
    branches: list[BranchAccess]
    active_permissions: list[str] = Field(default=[], deprecated=True)
    """
    Distinct permission codes granted to this user across all active role
    assignments.  Derived from sec.CompanyRolePermissions (new path) and
    sec.RolePermissions (legacy path) via UNION so both paths work during
    the transition period.
    """
    authority: PermissionAuthority


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserInfo
