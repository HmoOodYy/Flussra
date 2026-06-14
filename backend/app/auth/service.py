"""
Auth service — business logic for login and identity resolution.

All database access is raw parameterised SQL via sqlalchemy.text().
No ORM models.
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fastapi import HTTPException, status

from app.auth.schemas import (
    LoginRequest,
    LoginResponse,
    UserInfo,
    BranchAccess,
)
from app.auth.security import hash_password, verify_password, create_access_token

# ---------------------------------------------------------------------------
# Constant-time sentinel: prevents username enumeration via response timing.
#
# When a username is not found we still call verify_password() against this
# dummy hash so the code path takes the same ~100 ms as a real bcrypt check.
# Computed once at import time with bcrypt cost=12 (same as real passwords).
# ---------------------------------------------------------------------------
_DUMMY_HASH: str = hash_password("__sentinel_never_matches_any_real_password__")


async def login(request: LoginRequest, db: AsyncConnection) -> LoginResponse:
    """
    Authenticate a user and return a signed JWT + user info.

    Steps:
      1. Fetch the user row joined with their company
      2. Enforce guard checks in order (each has a distinct message)
      3. Verify bcrypt password
      4. Load branch access from app.vw_UserBranchAccess
      5. Fire-and-forget last-login timestamp update
      6. Build and return the JWT + UserInfo response
    """

    # ------------------------------------------------------------------
    # 1. Fetch user + company
    # ------------------------------------------------------------------
    result = await db.execute(
        text("""
            SELECT
                u.userid,
                u.username,
                u.displayname,
                u.passwordhash,
                u.isactive,
                u.canlogin,
                u.mustchangepassword,
                u.lockeduntilutc,
                c.companyid,
                c.companycode,
                c.companyname,
                c.issuspended,
                c.status AS companystatus
            FROM   sec.users u
            JOIN   core.companies c ON c.companyid = u.companyid
            WHERE  LOWER(u.username)    = LOWER(:username)
              AND  LOWER(c.companycode) = LOWER(:company_code)
        """),
        {"username": request.username, "company_code": request.company_code},
    )
    row = result.mappings().first()

    # ------------------------------------------------------------------
    # 2. Guard checks — ordered from most-generic to most-specific
    #    Each returns a distinct message so the caller knows exactly what failed.
    # ------------------------------------------------------------------
    if row is None:
        # Run a dummy bcrypt check so this branch takes the same ~100 ms
        # as a real password-mismatch.  This prevents an attacker from
        # distinguishing "user not found" from "wrong password" via timing.
        verify_password(request.password, _DUMMY_HASH)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
        )

    if not row["canlogin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is not permitted to log in.",
        )

    if not row["isactive"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is inactive. Contact your administrator.",
        )

    if row["issuspended"] or row["companystatus"] != "Active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Company account is suspended.",
        )

    # Locked-out check (brute-force protection — field exists in schema)
    from datetime import datetime, timezone
    if row["lockeduntilutc"] and row["lockeduntilutc"] > datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is temporarily locked due to too many failed attempts.",
        )

    # ------------------------------------------------------------------
    # 3. Verify password
    # ------------------------------------------------------------------
    if not row["passwordhash"] or not verify_password(request.password, row["passwordhash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
        )

    user_id: int = row["userid"]
    company_id: int = row["companyid"]

    # ------------------------------------------------------------------
    # 3b. Role completeness gate
    #
    # A user with no active role assignment cannot reach the app —
    # they would get 403 on every data request.  Block at login instead
    # so they receive a clear, actionable error message.
    # ------------------------------------------------------------------
    role_count_r = await db.execute(
        text("""
            SELECT COUNT(*)
            FROM   sec.userbranchroles
            WHERE  userid    = :uid
              AND  companyid = :cid
              AND  isactive  = TRUE
        """),
        {"uid": user_id, "cid": company_id},
    )
    if (role_count_r.scalar() or 0) == 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "no_active_role",
                "message": (
                    "Your account has no active role assignment. "
                    "Contact your company administrator to be granted access."
                ),
            },
        )

    # ------------------------------------------------------------------
    # 4. Load branch access
    # ------------------------------------------------------------------
    branches_result = await db.execute(
        text("""
            SELECT
                v.branchid,
                v.branchname,
                v.scopetype,
                v.rolecode,
                v.rolename
            FROM   app.vw_userbranchaccess v
            WHERE  v.userid          = :uid
              AND  v.userisactive    = TRUE
              AND  v.accessisactive  = TRUE
        """),
        {"uid": user_id},
    )
    branches = [
        BranchAccess(
            branch_id=b["branchid"],
            branch_name=b["branchname"],
            scope=b["scopetype"],
            role_code=b["rolecode"],
            role_name=b["rolename"],
        )
        for b in branches_result.mappings().all()
    ]

    # Fix 4 — completeness gate: role exists but view produces no accessible branches.
    # (Normally impossible in production, but guards against edge cases like a branch
    # being hard-deleted or a migration inconsistency.)
    if not branches:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "no_active_access",
                "message": (
                    "Your account has an active role assignment but no accessible branches. "
                    "Contact your company administrator."
                ),
            },
        )

    # ------------------------------------------------------------------
    # 4b. Load active permissions (UNION of new + legacy paths)
    # ------------------------------------------------------------------
    active_permissions = await _load_active_permissions(user_id, company_id, db)

    # ------------------------------------------------------------------
    # 5. Update last-login timestamp (write already in this transaction)
    # ------------------------------------------------------------------
    await db.execute(
        text("UPDATE sec.users SET lastloginatutc = NOW() WHERE userid = :uid"),
        {"uid": user_id},
    )

    # ------------------------------------------------------------------
    # 6. Build response
    # ------------------------------------------------------------------
    token = create_access_token(user_id=user_id, company_id=company_id)

    return LoginResponse(
        access_token=token,
        user=UserInfo(
            user_id=user_id,
            username=row["username"],
            display_name=row["displayname"],
            company_id=company_id,
            company_name=row["companyname"],
            branches=branches,
            active_permissions=active_permissions,
        ),
    )


async def _load_active_permissions(
    user_id: int, company_id: int, db: AsyncConnection
) -> list[str]:
    """
    Return distinct permission codes active for this user across ALL active role
    assignments.  Checks both paths so the transition is seamless:

      • New path  — sec.CompanyRolePermissions (via CompanyRoleID)
      • Legacy    — sec.RolePermissions        (via RoleID, no CompanyRoleID)
      • Overrides — sec.UserPermissionOverrides (extra ALLOW grants)

    Fix 5: If the user is the Company Owner, return ALL permission codes from
    sec.Permissions dynamically — this ensures new permissions added to the
    catalogue are automatically available without re-seeding CompanyRolePermissions.
    """
    # Check if user has an active COMPANY_OWNER company role assignment
    owner_r = await db.execute(
        text("""
            SELECT 1
            FROM   sec.userbranchroles ubr
            JOIN   sec.companyroles    cr ON cr.companyroleid = ubr.companyroleId
            WHERE  ubr.userid    = :uid
              AND  ubr.companyid = :cid
              AND  ubr.isactive  = TRUE
              AND  cr.rolecode   = 'COMPANY_OWNER'
        """),
        {"uid": user_id, "cid": company_id},
    )
    if owner_r.fetchone():
        all_r = await db.execute(
            text("SELECT permissioncode FROM sec.permissions ORDER BY permissioncode"),
        )
        return [r["permissioncode"] for r in all_r.mappings().all()]

    result = await db.execute(
        text("""
            SELECT DISTINCT permissioncode
            FROM (
                -- New path: company-scoped roles
                SELECT crp.permissioncode
                FROM   sec.userbranchroles        AS ubr
                JOIN   sec.companyrolepermissions AS crp
                       ON crp.companyroleid = ubr.companyroleId
                WHERE  ubr.userid    = :uid
                  AND  ubr.companyid = :cid
                  AND  ubr.isactive  = TRUE

                UNION

                -- Legacy path: global role permissions
                SELECT perm.permissioncode
                FROM   sec.userbranchroles AS ubr
                JOIN   sec.rolepermissions AS rp
                       ON rp.roleid = ubr.roleid
                JOIN   sec.permissions AS perm
                       ON perm.permissionid = rp.permissionid
                WHERE  ubr.userid    = :uid
                  AND  ubr.companyid = :cid
                  AND  ubr.isactive  = TRUE
                  AND  ubr.roleid   IS NOT NULL

                UNION

                -- Member-specific extra ALLOW overrides
                SELECT upo.permissioncode
                FROM   sec.userpermissionoverrides AS upo
                WHERE  upo.userid    = :uid
                  AND  upo.companyid = :cid
                  AND  upo.isactive  = TRUE
                  AND  upo.effect    = 'ALLOW'
            ) AS all_perms
            ORDER BY permissioncode
        """),
        {"uid": user_id, "cid": company_id},
    )
    return [r["permissioncode"] for r in result.mappings().all()]


async def get_me(user_id: int, company_id: int, db: AsyncConnection) -> UserInfo:
    """
    Re-fetch the full user identity from the DB using the user_id from the JWT.
    Used by GET /auth/me to let the client refresh its in-memory user object
    without re-entering credentials.
    """
    result = await db.execute(
        text("""
            SELECT u.userid, u.username, u.displayname,
                   c.companyid, c.companyname
            FROM   sec.users u
            JOIN   core.companies c ON c.companyid = u.companyid
            WHERE  u.userid      = :user_id
              AND  u.companyid   = :company_id
              AND  u.isactive    = TRUE
              AND  u.canlogin    = TRUE
              AND  c.issuspended = FALSE
              AND  c.status      = 'Active'
        """),
        {"user_id": user_id, "company_id": company_id},
    )
    row = result.mappings().first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or no longer active.",
        )

    branches_result = await db.execute(
        text("""
            SELECT v.branchid, v.branchname, v.scopetype, v.rolecode, v.rolename
            FROM   app.vw_userbranchaccess v
            WHERE  v.userid = :uid AND v.userisactive = TRUE AND v.accessisactive = TRUE
        """),
        {"uid": user_id},
    )
    branches = [
        BranchAccess(
            branch_id=b["branchid"],
            branch_name=b["branchname"],
            scope=b["scopetype"],
            role_code=b["rolecode"],
            role_name=b["rolename"],
        )
        for b in branches_result.mappings().all()
    ]

    # Fix 6: Consistent with login — if access view returns no rows, deny.
    if not branches:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "no_active_access",
                "message": (
                    "Your account has no accessible branches. "
                    "Contact your company administrator."
                ),
            },
        )

    active_permissions = await _load_active_permissions(user_id, company_id, db)

    return UserInfo(
        user_id=row["userid"],
        username=row["username"],
        display_name=row["displayname"],
        company_id=row["companyid"],
        company_name=row["companyname"],
        branches=branches,
        active_permissions=active_permissions,
    )
