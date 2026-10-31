"""End a terminated Employee's sign-in and access, inside the termination transaction."""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.admin import access_safety, audit


@dataclass(frozen=True)
class LinkedAccount:
    user_id: int
    is_active: bool
    can_login: bool
    is_staged: bool


async def prepare_employee_termination(
    company_id: int,
    caller_id: int,
    employee_id: int,
    db: AsyncConnection,
) -> LinkedAccount | None:
    """Lock the Employee's linked account and refuse terminations that must not end it.

    The Company Owner's Employee cannot be terminated until ownership is transferred, and
    nobody ends their own sign-in by terminating their own Employee record.
    """
    result = await db.execute(
        text("""
            SELECT UserID, IsActive, CanLogin, IsStaged
            FROM sec.Users
            WHERE EmployeeID = :eid AND CompanyID = :cid
            FOR UPDATE
        """),
        {"eid": employee_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        return None
    user_id = row["userid"]
    if await access_safety.is_company_owner(company_id, user_id, db):
        raise access_safety.access_error(
            422,
            access_safety.COMPANY_OWNER_TERMINATION_FORBIDDEN,
            "This Employee is the Company Owner. Transfer ownership to someone else "
            "before terminating them.",
        )
    if user_id == caller_id:
        raise access_safety.access_error(
            422,
            access_safety.OWN_EMPLOYEE_TERMINATION_FORBIDDEN,
            "You cannot terminate your own Employee record. Someone else needs to do it.",
        )
    return LinkedAccount(
        user_id=user_id,
        is_active=row["isactive"],
        can_login=row["canlogin"],
        is_staged=row["isstaged"],
    )


async def end_terminated_employee_access(
    company_id: int,
    caller_id: int,
    employee_id: int,
    account: LinkedAccount | None,
    db: AsyncConnection,
) -> None:
    """Turn off the linked login, revoke its role assignments and extra permissions, and audit.

    An account left without any role is staged, the same state a final role revocation
    produces, so it cannot be provisioned again for a terminated Employee.
    """
    if account is None:
        return
    uid, cid = account.user_id, company_id

    roles = await db.execute(
        text("""
            UPDATE sec.UserBranchRoles ubr
            SET IsActive = FALSE, RevokedAtUtc = NOW()
            WHERE ubr.UserID = :uid AND ubr.CompanyID = :cid AND ubr.IsActive
            RETURNING ubr.UserBranchRoleID AS assignment_id,
                      (SELECT cr.RoleCode FROM sec.CompanyRoles cr
                       WHERE cr.CompanyRoleID = ubr.CompanyRoleID) AS role_code
        """),
        {"uid": uid, "cid": cid},
    )
    revoked_roles = sorted(roles.mappings().all(), key=lambda r: r["assignment_id"])

    overrides = await db.execute(
        text("""
            UPDATE sec.UserPermissionOverrides
            SET IsActive = FALSE, RevokedAtUtc = NOW()
            WHERE UserID = :uid AND CompanyID = :cid AND IsActive
            RETURNING PermissionCode
        """),
        {"uid": uid, "cid": cid},
    )
    revoked_codes = sorted(row["permissioncode"] for row in overrides.mappings().all())

    await db.execute(
        text("""
            UPDATE sec.Users
            SET CanLogin = FALSE, IsActive = FALSE, IsStaged = TRUE, UpdatedAtUtc = NOW()
            WHERE UserID = :uid AND CompanyID = :cid
        """),
        {"uid": uid, "cid": cid},
    )

    for role in revoked_roles:
        await audit.write_admin_audit(
            db, company_id=company_id, user_id=caller_id, action_code="COMPANY_ROLE_REVOKED",
            entity_name="UserBranchRoles", entity_id=str(role["assignment_id"]),
            old_value={"target_user_id": uid, "company_role_code": role["role_code"]},
            new_value={"reason": "employee_terminated"},
        )
    if revoked_codes:
        await audit.write_admin_audit(
            db, company_id=company_id, user_id=caller_id,
            action_code="USER_PERMISSION_OVERRIDES_SET",
            entity_name="UserPermissionOverrides", entity_id=str(uid),
            old_value={"permission_codes": revoked_codes},
            new_value={"permission_codes": [], "reason": "employee_terminated"},
        )
    await audit.write_user_lifecycle_audit(
        db, company_id=company_id, caller_id=caller_id,
        action="USER_ACCESS_ENDED_BY_TERMINATION", user_id=uid,
        old_value={"is_active": account.is_active, "can_login": account.can_login,
                   "is_staged": account.is_staged,
                   "role_assignment_ids": [r["assignment_id"] for r in revoked_roles],
                   "permission_override_codes": revoked_codes},
        new_value={"is_active": False, "can_login": False, "is_staged": True,
                   "employee_id": employee_id},
    )
