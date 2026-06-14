"""
Pydantic schemas for the auth domain.

These define the exact shape of every auth request body and response.
FastAPI validates inputs against these schemas before any service code runs.
"""
from pydantic import BaseModel, field_validator


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


class UserInfo(BaseModel):
    """The logged-in user's identity, access list, and active permissions."""
    user_id: int
    username: str
    display_name: str
    company_id: int
    company_name: str
    branches: list[BranchAccess]
    active_permissions: list[str] = []
    """
    Distinct permission codes granted to this user across all active role
    assignments.  Derived from sec.CompanyRolePermissions (new path) and
    sec.RolePermissions (legacy path) via UNION so both paths work during
    the transition period.
    """


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserInfo


class TokenPayload(BaseModel):
    """Decoded JWT payload — minimal claims only."""
    sub: int    # user_id
    cid: int    # company_id
