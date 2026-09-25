"""
Admin domain service — Users, Roles, Permissions.

Design rules
------------
- All endpoints require AllCompanyBranches scope (_ensure_admin gate).
- All writes emit an audit row inside the same transaction so that a
  rollback on _write_admin_audit also rolls back the primary write.
- _write_admin_audit is a module-level function so tests can monkeypatch
  it to verify full-rollback behaviour.
- Internal read helpers (_get_user_by_id_internal, _fetch_role_assignments)
  bypass the scope check so that post-write reads don't re-execute the
  guard query.
"""
import json
import random
import string

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.admin.schemas import (
    CompanyRole,
    CompanyRoleAssignmentCreate,
    CompanyRoleAssignmentDetail,
    CompanyRoleCreate,
    CompanyRolePermissions,
    CompanyRolePermissionsUpdate,
    CompanyRoleUpdate,
    CompanyRoleUser,
    OwnerTransferRequest,
    OwnerTransferResult,
    Permission,
    Role,
    RoleAssignment,
    RoleAssignmentCreate,
    UserAdmin,
    UserCreate,
    UserPasswordReset,
    UserPermissionOverridesUpdate,
    UserUpdate,
)
from app.auth.security import hash_password
from app.core.service import _check_any_permission, _check_branch_access

# ---------------------------------------------------------------------------
# Audit reason map
# ---------------------------------------------------------------------------

_ADMIN_AUDIT_REASONS: dict[str, str] = {
    "USER_CREATED":                    "User account created",
    "USER_UPDATED":                    "User account updated",
    "USER_PASSWORD_RESET":             "User password reset by administrator",
    "ROLE_ASSIGNED":                   "Role assigned to user",
    "ROLE_REVOKED":                    "Role assignment revoked",
    "COMPANY_ROLE_ASSIGNED":           "Company role assigned to user",
    "COMPANY_ROLE_REVOKED":            "Company role assignment revoked",
    "OWNER_TRANSFERRED":               "Company ownership transferred to new owner",
    "USER_PERMISSION_OVERRIDES_SET":    "User extra permissions replaced",
    "COMPANY_ROLE_CREATED":            "Company role created",
    "COMPANY_ROLE_UPDATED":            "Company role updated",
    "COMPANY_ROLE_DELETED":            "Company role deleted",
    "COMPANY_ROLE_PERMISSIONS_SET":    "Company role permissions replaced",
}


# ---------------------------------------------------------------------------
# Audit helper — module-level for monkeypatching in tests
# ---------------------------------------------------------------------------

async def _write_admin_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    user_id: int,
    action_code: str,
    entity_name: str,
    entity_id: str,
    old_value: dict | None = None,
    new_value: dict | None = None,
) -> None:
    """
    Insert one row into audit.AuditLog for an admin-domain event.

    Module-level so tests can monkeypatch it to verify that all preceding
    writes roll back when this raises.
    """
    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid,
                 oldvaluejson, newvaluejson, reason, sourcetype)
            VALUES
                (:company_id, NULL, :actor_id, :action_code,
                 'sec', :entity_name, :entity_id,
                 :old_val, :new_val, :reason, 'Application')
        """),
        {
            "company_id":  company_id,
            "actor_id":    user_id,
            "action_code": action_code,
            "entity_name": entity_name,
            "entity_id":   entity_id,
            "old_val":     json.dumps(old_value) if old_value is not None else None,
            "new_val":     json.dumps(new_value) if new_value is not None else None,
            "reason":      _ADMIN_AUDIT_REASONS.get(action_code, action_code),
        },
    )


# ---------------------------------------------------------------------------
# Access gates
# ---------------------------------------------------------------------------

# Admin permission fallbacks — any of these are sufficient when paired with
# a granular permission check.  Callers with setup.manage or settings.manage
# retain full access during the migration to granular permissions.
_ADMIN_FALLBACKS: tuple[str, ...] = ("settings.manage", "setup.manage")


async def _ensure_scope(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> None:
    """
    Gate 1/2: user/company active + AllCompanyBranches scope.

    Catches stale JWTs for deactivated users, revoked roles, or suspended
    companies without relying on the app.vw_UserBranchAccess view.
    """
    result = await db.execute(
        text("""
            SELECT 1
            FROM   sec.userbranchroles ubr
            JOIN   sec.users           u ON u.userid    = ubr.userid
            JOIN   core.companies      c ON c.companyid = ubr.companyid
            WHERE  ubr.userid    = :uid
              AND  ubr.companyid = :cid
              AND  ubr.isactive  = TRUE
              AND  ubr.scopetype = 'AllCompanyBranches'
              AND  u.isactive    = TRUE
              AND  u.canlogin    = TRUE
              AND  c.status      = 'Active'
              AND  c.issuspended = FALSE
        """),
        {"uid": user_id, "cid": company_id},
    )
    if not result.fetchone():
        raise HTTPException(
            status_code=403,
            detail="This action requires company-level (all-branches) access.",
        )


async def _ensure_any_perm(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *permission_codes: str,
) -> None:
    """
    Raise 403 if the caller has NONE of the given permission codes.

    Always enforces scope first (via _ensure_scope), then checks permissions
    using sec.fn_UserHasPermission — which resolves both new and legacy paths
    plus extra overrides.  Returns as soon as one passing code is found.

    Callers pass granular codes first, then ``*_ADMIN_FALLBACKS`` so legacy
    roles (setup.manage / settings.manage) remain valid:

        await _ensure_any_perm(cid, uid, db, "users.view", *_ADMIN_FALLBACKS)
    """
    await _ensure_scope(company_id, user_id, db)
    for code in permission_codes:
        result = await db.execute(
            text("SELECT sec.fn_UserHasPermission(:uid, :cid, NULL, :perm)"),
            {"uid": user_id, "cid": company_id, "perm": code},
        )
        if result.scalar_one():
            return
    raise HTTPException(
        status_code=403,
        detail=(
            "You do not have permission to perform this action "
            f"(required: one of {list(permission_codes)})."
        ),
    )


async def _ensure_admin(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> None:
    """
    Backward-compatible admin gate: scope + (settings.manage OR setup.manage).
    Used for endpoints that don't yet have a more specific permission mapping,
    and for ownership transfer (where the caller-is-owner check inside the
    service is the real gate).
    """
    await _ensure_any_perm(company_id, user_id, db, *_ADMIN_FALLBACKS)


# ---------------------------------------------------------------------------
# Internal helpers — no permission gate, used after writes
# ---------------------------------------------------------------------------

async def _fetch_role_assignments(
    company_id: int,
    user_ids: list[int],
    db: AsyncConnection,
) -> dict[int, list[RoleAssignment]]:
    """
    Return all role assignments (active and revoked) for the given user list,
    keyed by user_id.  Fetched in one query to avoid N+1 on list endpoints.
    """
    if not user_ids:
        return {}

    placeholders = ", ".join(f":uid{i}" for i in range(len(user_ids)))
    params: dict = {
        "cid": company_id,
        **{f"uid{i}": uid for i, uid in enumerate(user_ids)},
    }
    result = await db.execute(
        text(f"""
            SELECT
                ubr.userbranchroleid   AS assignment_id,
                ubr.userid,
                ubr.roleid,
                r.rolecode,
                r.rolename,
                ubr.scopetype,
                ubr.branchid,
                b.branchname,
                ubr.isactive,
                ubr.grantedbyuserid,
                ubr.grantedatutc,
                ubr.revokedatutc,
                ubr.notes
            FROM  sec.userbranchroles ubr
            JOIN  sec.roles r          ON r.roleid    = ubr.roleid
            LEFT  JOIN core.branches b ON b.branchid  = ubr.branchid
            WHERE ubr.companyid = :cid
              AND ubr.userid IN ({placeholders})
            ORDER BY ubr.userid, ubr.grantedatutc
        """),
        params,
    )
    groups: dict[int, list[RoleAssignment]] = {}
    for row in result.mappings().all():
        ra = RoleAssignment(
            assignment_id=row["assignment_id"],
            user_id=row["userid"],
            role_id=row["roleid"],
            role_code=row["rolecode"],
            role_name=row["rolename"],
            scope_type=row["scopetype"],
            branch_id=row["branchid"],
            branch_name=row["branchname"],
            is_active=row["isactive"],
            granted_by_user_id=row["grantedbyuserid"],
            granted_at_utc=row["grantedatutc"],
            revoked_at_utc=row["revokedatutc"],
            notes=row["notes"],
        )
        groups.setdefault(row["userid"], []).append(ra)
    return groups


async def _fetch_company_roles_for_users(
    company_id: int,
    user_ids: list[int],
    db: AsyncConnection,
) -> dict[int, dict]:
    """
    Return the active company-role assignment for each user (new path).
    Keyed by user_id.  Only the most recently granted active row is kept.
    """
    if not user_ids:
        return {}

    placeholders = ", ".join(f":uid{i}" for i in range(len(user_ids)))
    params: dict = {
        "cid": company_id,
        **{f"uid{i}": uid for i, uid in enumerate(user_ids)},
    }
    result = await db.execute(
        text(f"""
            SELECT
                ubr.userbranchroleid   AS assignment_id,
                ubr.userid,
                cr.companyroleid,
                cr.rolecode            AS company_role_code,
                cr.rolename            AS company_role_name,
                ubr.scopetype          AS company_role_scope,
                ubr.branchid           AS company_role_branch_id,
                b.branchname           AS company_role_branch_name,
                ubr.grantedatutc
            FROM  sec.userbranchroles ubr
            JOIN  sec.companyroles cr  ON cr.companyroleid = ubr.companyroleId
            LEFT  JOIN core.branches b ON b.branchid       = ubr.branchid
            WHERE ubr.companyid   = :cid
              AND ubr.userid      IN ({placeholders})
              AND ubr.companyroleId IS NOT NULL
              AND ubr.isactive    = TRUE
            ORDER BY ubr.grantedatutc DESC
        """),
        params,
    )
    out: dict[int, dict] = {}
    for row in result.mappings().all():
        uid = row["userid"]
        if uid not in out:          # keep first (most recent due to ORDER BY)
            out[uid] = dict(row)
    return out


async def _fetch_driver_ids_for_users(
    company_id: int,
    user_ids: list[int],
    db: AsyncConnection,
) -> dict[int, int]:
    """
    Return {user_id: driver_id} for users who are linked to a core.Drivers row.
    The link path is: sec.Users.EmployeeID → core.Employees.EmployeeID → core.Drivers.EmployeeID.
    """
    if not user_ids:
        return {}

    placeholders = ", ".join(f":uid{i}" for i in range(len(user_ids)))
    params: dict = {
        "cid": company_id,
        **{f"uid{i}": uid for i, uid in enumerate(user_ids)},
    }
    result = await db.execute(
        text(f"""
            SELECT u.userid, d.driverid
            FROM   sec.users        u
            JOIN   core.employees   e ON e.employeeid = u.employeeid
            JOIN   core.drivers     d ON d.employeeid = e.employeeid
                                      AND d.driverstatus NOT IN ('Transferred','Terminated')
            WHERE  u.companyid = :cid
              AND  u.userid    IN ({placeholders})
        """),
        params,
    )
    return {row["userid"]: row["driverid"] for row in result.mappings().all()}


async def _fetch_extra_perms_for_users(
    company_id: int,
    user_ids: list[int],
    db: AsyncConnection,
) -> dict[int, list[str]]:
    """
    Return active ALLOW permission overrides for each user, keyed by user_id.
    """
    if not user_ids:
        return {}
    placeholders = ", ".join(f":uid{i}" for i in range(len(user_ids)))
    params: dict = {
        "cid": company_id,
        **{f"uid{i}": uid for i, uid in enumerate(user_ids)},
    }
    result = await db.execute(
        text(f"""
            SELECT userid, permissioncode
            FROM   sec.userpermissionoverrides
            WHERE  companyid = :cid
              AND  userid    IN ({placeholders})
              AND  isactive  = TRUE
              AND  effect    = 'ALLOW'
            ORDER BY permissioncode
        """),
        params,
    )
    out: dict[int, list[str]] = {}
    for row in result.mappings().all():
        out.setdefault(row["userid"], []).append(row["permissioncode"])
    return out


async def _fetch_role_perms_for_roles(
    company_id: int,
    role_ids: set[int],
    db: AsyncConnection,
) -> dict[int, list[str]]:
    """
    Return permission codes for each company_role_id, keyed by company_role_id.

    Fix 5: COMPANY_OWNER roles return ALL permissions from sec.Permissions
    dynamically, so new permissions added to the catalogue are automatically
    included without re-seeding CompanyRolePermissions.
    All other roles read from sec.CompanyRolePermissions as normal.
    """
    if not role_ids:
        return {}

    placeholders = ", ".join(f":rid{i}" for i in range(len(role_ids)))
    params: dict = {**{f"rid{i}": rid for i, rid in enumerate(role_ids)}}

    # Identify which role IDs are COMPANY_OWNER
    owner_r = await db.execute(
        text(f"""
            SELECT companyroleid
            FROM   sec.companyroles
            WHERE  companyroleid IN ({placeholders})
              AND  rolecode = 'COMPANY_OWNER'
        """),
        params,
    )
    owner_ids: set[int] = {row["companyroleid"] for row in owner_r.mappings().all()}

    out: dict[int, list[str]] = {}

    # For COMPANY_OWNER roles: return ALL permission codes from the catalogue
    if owner_ids:
        all_r = await db.execute(
            text("SELECT permissioncode FROM sec.permissions ORDER BY permissioncode"),
        )
        all_perms = [r["permissioncode"] for r in all_r.mappings().all()]
        for rid in owner_ids:
            out[rid] = all_perms

    # For non-owner roles: read from companyrolepermissions as normal
    non_owner_ids = role_ids - owner_ids
    if non_owner_ids:
        placeholders2 = ", ".join(f":rid{i}" for i in range(len(non_owner_ids)))
        params2: dict = {**{f"rid{i}": rid for i, rid in enumerate(non_owner_ids)}}
        result = await db.execute(
            text(f"""
                SELECT companyroleid, permissioncode
                FROM   sec.companyrolepermissions
                WHERE  companyroleid IN ({placeholders2})
                ORDER  BY permissioncode
            """),
            params2,
        )
        for row in result.mappings().all():
            out.setdefault(row["companyroleid"], []).append(row["permissioncode"])

    return out


def _row_to_user(
    row,
    assignments: list[RoleAssignment],
    company_role: dict | None = None,
    driver_id: int | None = None,
    extra_perm_codes: list[str] | None = None,
    role_perm_codes: list[str] | None = None,
) -> UserAdmin:
    return UserAdmin(
        user_id=row["userid"],
        company_id=row["companyid"],
        username=row["username"],
        display_name=row["displayname"],
        email=row["email"],
        phone=row["phone"],
        is_active=row["isactive"],
        can_login=row["canlogin"],
        must_change_password=row["mustchangepassword"],
        last_login_at_utc=row["lastloginatutc"],
        created_at_utc=row["createdatutc"],
        updated_at_utc=row["updatedatutc"],
        role_assignments=assignments,
        # Company-role new path
        company_role_assignment_id=company_role["assignment_id"] if company_role else None,
        company_role_id=company_role["companyroleid"] if company_role else None,
        company_role_code=company_role["company_role_code"] if company_role else None,
        company_role_name=company_role["company_role_name"] if company_role else None,
        company_role_scope=company_role["company_role_scope"] if company_role else None,
        company_role_branch_id=company_role["company_role_branch_id"] if company_role else None,
        company_role_branch_name=company_role["company_role_branch_name"] if company_role else None,
        # Driver profile
        driver_id=driver_id,
        # Permission details
        extra_permission_codes=extra_perm_codes or [],
        role_permission_codes=role_perm_codes or [],
    )


async def _get_user_by_id_internal(
    target_user_id: int,
    company_id: int,
    db: AsyncConnection,
) -> UserAdmin:
    """Fetch user + assignments without permission check — for internal post-write reads."""
    result = await db.execute(
        text("""
            SELECT
                u.userid, u.companyid, u.username, u.displayname,
                u.email, u.phone, u.isactive, u.canlogin,
                u.mustchangepassword, u.lastloginatutc,
                u.createdatutc, u.updatedatutc
            FROM  sec.users u
            WHERE u.userid    = :uid
              AND u.companyid = :cid
        """),
        {"uid": target_user_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="User not found.")
    uid_list = [target_user_id]
    assignments_map = await _fetch_role_assignments(company_id, uid_list, db)
    cr_map = await _fetch_company_roles_for_users(company_id, uid_list, db)
    drv_map = await _fetch_driver_ids_for_users(company_id, uid_list, db)
    extra_map = await _fetch_extra_perms_for_users(company_id, uid_list, db)
    cr_info = cr_map.get(target_user_id)
    role_ids_set = {cr_info["companyroleid"]} if cr_info else set()
    role_perms_map = await _fetch_role_perms_for_roles(company_id, role_ids_set, db)
    return _row_to_user(
        row,
        assignments_map.get(target_user_id, []),
        company_role=cr_info,
        driver_id=drv_map.get(target_user_id),
        extra_perm_codes=extra_map.get(target_user_id),
        role_perm_codes=role_perms_map.get(cr_info["companyroleid"]) if cr_info else None,
    )


# ===========================================================================
# Users
# ===========================================================================

async def list_users(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    include_inactive: bool = False,
) -> list[UserAdmin]:
    await _ensure_any_perm(company_id, user_id, db, "users.view", *_ADMIN_FALLBACKS)

    filter_clause = "" if include_inactive else "AND u.isactive = TRUE"
    result = await db.execute(
        text(f"""
            SELECT
                u.userid, u.companyid, u.username, u.displayname,
                u.email, u.phone, u.isactive, u.canlogin,
                u.mustchangepassword, u.lastloginatutc,
                u.createdatutc, u.updatedatutc
            FROM  sec.users u
            WHERE u.companyid = :cid
              {filter_clause}
            ORDER BY u.displayname
        """),
        {"cid": company_id},
    )
    rows = result.mappings().all()
    if not rows:
        return []
    uid_list = [r["userid"] for r in rows]
    assignments = await _fetch_role_assignments(company_id, uid_list, db)
    cr_map = await _fetch_company_roles_for_users(company_id, uid_list, db)
    drv_map = await _fetch_driver_ids_for_users(company_id, uid_list, db)
    extra_map = await _fetch_extra_perms_for_users(company_id, uid_list, db)
    # Batch role permissions for all distinct company_role_ids in this result set
    role_ids_set = {d["companyroleid"] for d in cr_map.values() if d}
    role_perms_map = await _fetch_role_perms_for_roles(company_id, role_ids_set, db)
    return [
        _row_to_user(
            r,
            assignments.get(r["userid"], []),
            company_role=(cr_info := cr_map.get(r["userid"])),
            driver_id=drv_map.get(r["userid"]),
            extra_perm_codes=extra_map.get(r["userid"]),
            role_perm_codes=role_perms_map.get(cr_info["companyroleid"]) if cr_info else None,
        )
        for r in rows
    ]


async def get_user_by_id(
    target_user_id: int,
    company_id: int,
    caller_id: int,
    db: AsyncConnection,
) -> UserAdmin:
    await _ensure_any_perm(company_id, caller_id, db, "users.view", *_ADMIN_FALLBACKS)
    return await _get_user_by_id_internal(target_user_id, company_id, db)


async def create_user(
    company_id: int,
    caller_id: int,
    data: UserCreate,
    db: AsyncConnection,
) -> UserAdmin:
    await _ensure_any_perm(company_id, caller_id, db, "users.create", *_ADMIN_FALLBACKS)

    pw_hash = hash_password(data.password)

    try:
        result = await db.execute(
            text("""
                INSERT INTO sec.users
                    (companyid, username, displayname, email, phone,
                     passwordhash, isactive, canlogin, mustchangepassword)
                VALUES
                    (:cid, :username, :display_name, :email, :phone,
                     :pw_hash, :is_active, :can_login, :must_change)
                RETURNING userid
            """),
            {
                "cid":          company_id,
                "username":     data.username,
                "display_name": data.display_name,
                "email":        data.email,
                "phone":        data.phone,
                "pw_hash":      pw_hash,
                "is_active":    data.is_active,
                "can_login":    data.can_login,
                "must_change":  data.must_change_password,
            },
        )
        new_user_id: int = result.scalar_one()
    except SAIntegrityError as exc:
        msg = str(exc.orig).lower() if exc.orig else str(exc).lower()
        if "ux_users_company_username" in msg:
            raise HTTPException(
                status_code=422,
                detail=f"Username '{data.username}' already exists for this company.",
            )
        raise HTTPException(
            status_code=422,
            detail="User could not be created — a uniqueness constraint was violated.",
        )

    await _write_admin_audit(
        db,
        company_id=company_id,
        user_id=caller_id,
        action_code="USER_CREATED",
        entity_name="Users",
        entity_id=str(new_user_id),
        new_value={"username": data.username, "display_name": data.display_name},
    )

    return await _get_user_by_id_internal(new_user_id, company_id, db)


async def update_user(
    target_user_id: int,
    company_id: int,
    caller_id: int,
    data: UserUpdate,
    db: AsyncConnection,
) -> UserAdmin:
    # ── Field-sensitive permission check ────────────────────────────────────
    # Profile / login fields (display_name, email, phone, can_login,
    # must_change_password) require users.edit or admin fallbacks.
    # The is_active toggle additionally allows users.deactivate.
    # Mixed payloads: if any profile/login field is present, users.edit is
    # required — users.deactivate alone is not sufficient.
    _has_profile_fields = (
        data.display_name is not None
        or "email" in data.model_fields_set
        or "phone" in data.model_fields_set
        or data.can_login is not None
        or data.must_change_password is not None
    )
    _has_active_field = data.is_active is not None

    if _has_profile_fields:
        # users.edit required; fallbacks accepted; is_active may also be present
        await _ensure_any_perm(company_id, caller_id, db, "users.edit", *_ADMIN_FALLBACKS)
    elif _has_active_field:
        # Pure activate/deactivate — users.deactivate is sufficient
        await _ensure_any_perm(
            company_id, caller_id, db, "users.deactivate", "users.edit", *_ADMIN_FALLBACKS
        )
    else:
        # Empty payload — enforce scope but no write permission needed
        await _ensure_scope(company_id, caller_id, db)

    # Fetch current values with row lock for snapshot + validation
    result = await db.execute(
        text("""
            SELECT userid, displayname, email, phone,
                   isactive, canlogin, mustchangepassword
            FROM  sec.users
            WHERE userid = :uid AND companyid = :cid
            FOR UPDATE
        """),
        {"uid": target_user_id, "cid": company_id},
    )
    current = result.mappings().first()
    if current is None:
        raise HTTPException(status_code=404, detail="User not found.")

    # Self-protection: cannot strip your own access
    if target_user_id == caller_id:
        if data.is_active is False:
            raise HTTPException(
                status_code=422,
                detail="Cannot deactivate your own account.",
            )
        if data.can_login is False:
            raise HTTPException(
                status_code=422,
                detail="Cannot remove login access from your own account.",
            )

    # Build dynamic SET clause.
    #
    # Non-nullable fields (bool / required string): include only when
    # the caller explicitly provided a non-None value.
    #
    # Nullable clearable fields (email, phone): include whenever the
    # field appears in model_fields_set — even when the value is None —
    # so that an explicit null clears the column rather than being
    # silently ignored.
    updates: dict[str, object] = {}

    if data.display_name is not None:
        updates["displayname"] = data.display_name
    if data.is_active is not None:
        updates["isactive"] = data.is_active
    if data.can_login is not None:
        updates["canlogin"] = data.can_login
    if data.must_change_password is not None:
        updates["mustchangepassword"] = data.must_change_password

    # email and phone: honour explicit null (clears the column)
    if "email" in data.model_fields_set:
        updates["email"] = data.email
    if "phone" in data.model_fields_set:
        updates["phone"] = data.phone

    if updates:
        set_clause = ", ".join(f"{col} = :{col}" for col in updates)
        params: dict = {"uid": target_user_id, "cid": company_id, **updates}
        await db.execute(
            text(f"""
                UPDATE sec.users
                SET {set_clause}, updatedatutc = NOW()
                WHERE userid = :uid AND companyid = :cid
            """),
            params,
        )
        await _write_admin_audit(
            db,
            company_id=company_id,
            user_id=caller_id,
            action_code="USER_UPDATED",
            entity_name="Users",
            entity_id=str(target_user_id),
            old_value={col: current[col] for col in updates},
            new_value=updates,
        )

    return await _get_user_by_id_internal(target_user_id, company_id, db)


async def reset_password(
    target_user_id: int,
    company_id: int,
    caller_id: int,
    data: UserPasswordReset,
    db: AsyncConnection,
) -> UserAdmin:
    await _ensure_any_perm(company_id, caller_id, db, "users.edit", *_ADMIN_FALLBACKS)

    # Lock row — also confirms user belongs to this company
    result = await db.execute(
        text("""
            SELECT userid FROM sec.users
            WHERE userid = :uid AND companyid = :cid
            FOR UPDATE
        """),
        {"uid": target_user_id, "cid": company_id},
    )
    if not result.fetchone():
        raise HTTPException(status_code=404, detail="User not found.")

    pw_hash = hash_password(data.new_password)
    await db.execute(
        text("""
            UPDATE sec.users
            SET passwordhash       = :pw_hash,
                mustchangepassword = :must_change,
                failedlogincount   = 0,
                lockeduntilutc     = NULL,
                updatedatutc       = NOW()
            WHERE userid = :uid AND companyid = :cid
        """),
        {
            "pw_hash":     pw_hash,
            "must_change": data.must_change_password,
            "uid":         target_user_id,
            "cid":         company_id,
        },
    )

    await _write_admin_audit(
        db,
        company_id=company_id,
        user_id=caller_id,
        action_code="USER_PASSWORD_RESET",
        entity_name="Users",
        entity_id=str(target_user_id),
        new_value={"must_change_password": data.must_change_password},
    )

    return await _get_user_by_id_internal(target_user_id, company_id, db)


# ===========================================================================
# Role assignments
# ===========================================================================

async def list_user_roles(
    target_user_id: int,
    company_id: int,
    caller_id: int,
    db: AsyncConnection,
) -> list[RoleAssignment]:
    await _ensure_any_perm(company_id, caller_id, db, "users.view", *_ADMIN_FALLBACKS)

    # Confirm user exists in this company
    chk = await db.execute(
        text("SELECT 1 FROM sec.users WHERE userid = :uid AND companyid = :cid"),
        {"uid": target_user_id, "cid": company_id},
    )
    if not chk.fetchone():
        raise HTTPException(status_code=404, detail="User not found.")

    assignments_map = await _fetch_role_assignments(company_id, [target_user_id], db)
    return assignments_map.get(target_user_id, [])


async def assign_role(
    target_user_id: int,
    company_id: int,
    caller_id: int,
    data: RoleAssignmentCreate,
    db: AsyncConnection,
) -> RoleAssignment:
    await _ensure_any_perm(company_id, caller_id, db, "users.edit", "roles.edit", *_ADMIN_FALLBACKS)

    # Confirm target user exists
    chk_user = await db.execute(
        text("SELECT 1 FROM sec.users WHERE userid = :uid AND companyid = :cid"),
        {"uid": target_user_id, "cid": company_id},
    )
    if not chk_user.fetchone():
        raise HTTPException(status_code=404, detail="User not found.")

    # Confirm role exists
    role_result = await db.execute(
        text("SELECT roleid, rolecode, rolename FROM sec.roles WHERE roleid = :rid"),
        {"rid": data.role_id},
    )
    role_row = role_result.mappings().first()
    if role_row is None:
        raise HTTPException(status_code=422, detail="Role not found.")

    # Validate scope / branch alignment
    if data.scope_type == "AllCompanyBranches":
        if data.branch_id is not None:
            raise HTTPException(
                status_code=422,
                detail="branch_id must be null for AllCompanyBranches scope.",
            )
    else:
        # SpecificBranch or OwnDriverDataOnly — branch required
        if data.branch_id is None:
            raise HTTPException(
                status_code=422,
                detail=f"branch_id is required for {data.scope_type} scope.",
            )
        branch_chk = await db.execute(
            text("SELECT 1 FROM core.branches WHERE branchid = :bid AND companyid = :cid"),
            {"bid": data.branch_id, "cid": company_id},
        )
        if not branch_chk.fetchone():
            raise HTTPException(
                status_code=422,
                detail="Branch not found for this company.",
            )

    # Prevent duplicate active assignment — branch_id NULL vs. specific handled separately
    # to avoid the SQLAlchemy text() parser tripping over :param::TYPE cast syntax.
    if data.branch_id is None:
        dup = await db.execute(
            text("""
                SELECT userbranchroleid FROM sec.userbranchroles
                WHERE userid    = :uid
                  AND companyid = :cid
                  AND roleid    = :rid
                  AND scopetype = :scope
                  AND branchid IS NULL
                  AND isactive  = TRUE
            """),
            {"uid": target_user_id, "cid": company_id, "rid": data.role_id, "scope": data.scope_type},
        )
    else:
        dup = await db.execute(
            text("""
                SELECT userbranchroleid FROM sec.userbranchroles
                WHERE userid    = :uid
                  AND companyid = :cid
                  AND roleid    = :rid
                  AND scopetype = :scope
                  AND branchid  = :bid
                  AND isactive  = TRUE
            """),
            {"uid": target_user_id, "cid": company_id, "rid": data.role_id, "scope": data.scope_type, "bid": data.branch_id},
        )
    if dup.fetchone():
        raise HTTPException(
            status_code=422,
            detail="This role assignment already exists and is active.",
        )

    # Insert — wrap in try/except to catch the DB-level unique index
    # (ux_UserBranchRoles_Active_AllCompany / ux_UserBranchRoles_Active_Branch)
    # in case a concurrent request slips through the soft-check above.
    try:
        ins = await db.execute(
            text("""
                INSERT INTO sec.userbranchroles
                    (userid, companyid, branchid, roleid, scopetype,
                     isactive, grantedbyuserid, notes)
                VALUES
                    (:uid, :cid, :bid, :rid, :scope,
                     TRUE, :grantor, :notes)
                RETURNING userbranchroleid
            """),
            {
                "uid":     target_user_id,
                "cid":     company_id,
                "bid":     data.branch_id,
                "rid":     data.role_id,
                "scope":   data.scope_type,
                "grantor": caller_id,
                "notes":   data.notes,
            },
        )
    except SAIntegrityError:
        raise HTTPException(
            status_code=422,
            detail="This role assignment already exists and is active.",
        )
    new_assignment_id: int = ins.scalar_one()

    await _write_admin_audit(
        db,
        company_id=company_id,
        user_id=caller_id,
        action_code="ROLE_ASSIGNED",
        entity_name="UserBranchRoles",
        entity_id=str(new_assignment_id),
        new_value={
            "target_user_id": target_user_id,
            "role_code":      role_row["rolecode"],
            "scope_type":     data.scope_type,
            "branch_id":      data.branch_id,
        },
    )

    # Return the new assignment from DB
    assignments_map = await _fetch_role_assignments(company_id, [target_user_id], db)
    for ra in assignments_map.get(target_user_id, []):
        if ra.assignment_id == new_assignment_id:
            return ra
    # Should never be reached
    raise HTTPException(status_code=500, detail="Assignment created but could not be retrieved.")


async def revoke_role(
    assignment_id: int,
    target_user_id: int,
    company_id: int,
    caller_id: int,
    db: AsyncConnection,
) -> RoleAssignment:
    await _ensure_any_perm(company_id, caller_id, db, "users.edit", "roles.edit", *_ADMIN_FALLBACKS)

    # Fetch and lock the assignment
    result = await db.execute(
        text("""
            SELECT
                ubr.userbranchroleid AS assignment_id,
                ubr.userid,
                ubr.roleid,
                r.rolecode,
                r.rolename,
                ubr.scopetype,
                ubr.branchid,
                b.branchname,
                ubr.isactive,
                ubr.grantedbyuserid,
                ubr.grantedatutc,
                ubr.revokedatutc,
                ubr.notes
            FROM  sec.userbranchroles ubr
            JOIN  sec.roles r          ON r.roleid   = ubr.roleid
            LEFT  JOIN core.branches b ON b.branchid = ubr.branchid
            WHERE ubr.userbranchroleid = :aid
              AND ubr.userid           = :uid
              AND ubr.companyid        = :cid
            FOR UPDATE OF ubr
        """),
        {"aid": assignment_id, "uid": target_user_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Role assignment not found.")

    # Idempotent: already inactive → return as-is without audit write
    if not row["isactive"]:
        return RoleAssignment(
            assignment_id=row["assignment_id"],
            user_id=row["userid"],
            role_id=row["roleid"],
            role_code=row["rolecode"],
            role_name=row["rolename"],
            scope_type=row["scopetype"],
            branch_id=row["branchid"],
            branch_name=row["branchname"],
            is_active=row["isactive"],
            granted_by_user_id=row["grantedbyuserid"],
            granted_at_utc=row["grantedatutc"],
            revoked_at_utc=row["revokedatutc"],
            notes=row["notes"],
        )

    await db.execute(
        text("""
            UPDATE sec.userbranchroles
            SET isactive = FALSE, revokedatutc = NOW()
            WHERE userbranchroleid = :aid
        """),
        {"aid": assignment_id},
    )

    await _write_admin_audit(
        db,
        company_id=company_id,
        user_id=caller_id,
        action_code="ROLE_REVOKED",
        entity_name="UserBranchRoles",
        entity_id=str(assignment_id),
        old_value={
            "target_user_id": target_user_id,
            "role_code":      row["rolecode"],
            "scope_type":     row["scopetype"],
            "branch_id":      row["branchid"],
        },
    )

    # Fetch the updated row to return is_active=False and revoked_at
    updated = await db.execute(
        text("""
            SELECT
                ubr.userbranchroleid AS assignment_id,
                ubr.userid,
                ubr.roleid,
                r.rolecode,
                r.rolename,
                ubr.scopetype,
                ubr.branchid,
                b.branchname,
                ubr.isactive,
                ubr.grantedbyuserid,
                ubr.grantedatutc,
                ubr.revokedatutc,
                ubr.notes
            FROM  sec.userbranchroles ubr
            JOIN  sec.roles r          ON r.roleid   = ubr.roleid
            LEFT  JOIN core.branches b ON b.branchid = ubr.branchid
            WHERE ubr.userbranchroleid = :aid
        """),
        {"aid": assignment_id},
    )
    u = updated.mappings().first()
    return RoleAssignment(
        assignment_id=u["assignment_id"],
        user_id=u["userid"],
        role_id=u["roleid"],
        role_code=u["rolecode"],
        role_name=u["rolename"],
        scope_type=u["scopetype"],
        branch_id=u["branchid"],
        branch_name=u["branchname"],
        is_active=u["isactive"],
        granted_by_user_id=u["grantedbyuserid"],
        granted_at_utc=u["grantedatutc"],
        revoked_at_utc=u["revokedatutc"],
        notes=u["notes"],
    )


# ===========================================================================
# Roles & Permissions catalogue (read-only)
# ===========================================================================

async def list_roles(
    company_id: int,
    caller_id: int,
    db: AsyncConnection,
) -> list[Role]:
    await _ensure_any_perm(company_id, caller_id, db, "roles.view", *_ADMIN_FALLBACKS)

    result = await db.execute(
        text("""
            SELECT roleid, rolecode, rolename, rolelevel, issystemrole, notes
            FROM   sec.roles
            ORDER  BY rolelevel DESC, rolename
        """),
    )
    return [
        Role(
            role_id=row["roleid"],
            role_code=row["rolecode"],
            role_name=row["rolename"],
            role_level=row["rolelevel"],
            is_system_role=row["issystemrole"],
            notes=row["notes"],
        )
        for row in result.mappings().all()
    ]


# UI-visible module codes — excludes legacy modules (core, review) and legacy
# permission codes whose modulecode uses old capitalisation or naming.
_UI_MODULE_CODES = (
    'company', 'roles', 'users', 'payroll', 'payitems',
    'payrates', 'drivers', 'dispatch', 'reports', 'settings', 'payroll_setup',
)

# Dependency map: child permission → required parent permission.
# Used server-side to auto-add parent when a child is saved.
_PERM_DEPS: dict[str, str] = {
    'company.edit':     'company.view',
    'branches.create':  'branches.view',
    'branches.edit':    'branches.view',
    'roles.create':     'roles.view',
    'roles.edit':       'roles.view',
    'roles.delete':     'roles.view',
    'users.create':     'users.view',
    'users.edit':       'users.view',
    'users.deactivate': 'users.view',
    'payroll.edit':          'payroll.view',
    'payroll.entry':         'payroll.view',
    'payroll.period.create': 'payroll.view',
    'payroll.approve':       'payroll.view',
    'payroll.finalize':      'payroll.view',
    'payitems.edit':    'payitems.view',
    'payrates.edit':    'payrates.view',
    'drivers.create':   'drivers.view',
    'drivers.edit':     'drivers.view',
    'dispatch.edit':    'dispatch.view',
    'settings.manage':  'settings.view',
}


async def list_permissions(
    company_id: int,
    caller_id: int,
    db: AsyncConnection,
    *,
    ui_only: bool = True,
) -> list[Permission]:
    await _ensure_any_perm(company_id, caller_id, db, "roles.view", "users.view", *_ADMIN_FALLBACKS)

    if ui_only:
        result = await db.execute(
            text("""
                SELECT permissionid, permissioncode, permissionname, modulecode, notes
                FROM   sec.permissions
                WHERE  modulecode IN ('company','roles','users','payroll','payitems',
                                      'payrates','drivers','dispatch','reports','settings',
                                      'payroll_setup','review')
                ORDER  BY modulecode, permissioncode
            """),
        )
    else:
        result = await db.execute(
            text("""
                SELECT permissionid, permissioncode, permissionname, modulecode, notes
                FROM   sec.permissions
                ORDER  BY modulecode, permissioncode
            """),
        )
    return [
        Permission(
            permission_id=row["permissionid"],
            permission_code=row["permissioncode"],
            permission_name=row["permissionname"],
            module_code=row["modulecode"],
            notes=row["notes"],
        )
        for row in result.mappings().all()
    ]


# ===========================================================================
# Company Roles
# ===========================================================================

# Random role-code generator.  Format: CR_XXXXXXXX  (8 uppercase alphanumerics).
# Each call to create_company_role retries up to 5 times on a collision.
_ROLE_CODE_CHARS = string.ascii_uppercase + string.digits


def _generate_role_code() -> str:
    suffix = "".join(random.choices(_ROLE_CODE_CHARS, k=8))
    return f"CR_{suffix}"


def _row_to_company_role(row) -> CompanyRole:
    return CompanyRole(
        company_role_id=row["companyroleid"],
        company_id=row["companyid"],
        role_code=row["rolecode"],
        role_name=row["rolename"],
        role_level=row["rolelevel"],
        is_default=row["isdefault"],
        is_protected=row["isprotected"],
        is_custom=row["iscustom"],
        is_active=row["isactive"],
        notes=row["notes"],
        created_at_utc=row["createdatutc"],
        updated_at_utc=row["updatedatutc"],
        user_count=row["user_count"] or 0,
    )


async def _get_company_role_internal(
    company_role_id: int,
    company_id: int,
    db: AsyncConnection,
) -> CompanyRole:
    """Fetch a single company role without permission check (for post-write reads)."""
    result = await db.execute(
        text("""
            SELECT
                cr.companyroleid, cr.companyid, cr.rolecode, cr.rolename,
                cr.rolelevel, cr.isdefault, cr.isprotected, cr.iscustom,
                cr.isactive, cr.notes, cr.createdatutc, cr.updatedatutc,
                COUNT(ubr.userid) FILTER (WHERE ubr.isactive = TRUE) AS user_count
            FROM  sec.companyroles cr
            LEFT  JOIN sec.userbranchroles ubr ON ubr.companyroleId = cr.companyroleid
            WHERE cr.companyroleid = :rid
              AND cr.companyid     = :cid
              AND cr.isarchived    = FALSE
            GROUP BY cr.companyroleid
        """),
        {"rid": company_role_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Company role not found.")
    return _row_to_company_role(row)


async def list_company_roles(
    company_id: int,
    caller_id: int,
    db: AsyncConnection,
) -> list[CompanyRole]:
    """Return all active (non-archived) company roles for this company."""
    await _ensure_any_perm(company_id, caller_id, db, "roles.view", *_ADMIN_FALLBACKS)

    result = await db.execute(
        text("""
            SELECT
                cr.companyroleid, cr.companyid, cr.rolecode, cr.rolename,
                cr.rolelevel, cr.isdefault, cr.isprotected, cr.iscustom,
                cr.isactive, cr.notes, cr.createdatutc, cr.updatedatutc,
                COUNT(ubr.userid) FILTER (WHERE ubr.isactive = TRUE) AS user_count
            FROM  sec.companyroles cr
            LEFT  JOIN sec.userbranchroles ubr ON ubr.companyroleId = cr.companyroleid
            WHERE cr.companyid   = :cid
              AND cr.isarchived  = FALSE
            GROUP BY cr.companyroleid
            ORDER BY cr.isprotected DESC, cr.isdefault DESC, cr.rolelevel DESC, cr.rolename
        """),
        {"cid": company_id},
    )
    return [_row_to_company_role(row) for row in result.mappings().all()]


async def get_company_role(
    company_role_id: int,
    company_id: int,
    caller_id: int,
    db: AsyncConnection,
) -> CompanyRole:
    await _ensure_any_perm(company_id, caller_id, db, "roles.view", *_ADMIN_FALLBACKS)
    return await _get_company_role_internal(company_role_id, company_id, db)


async def create_company_role(
    company_id: int,
    caller_id: int,
    data: CompanyRoleCreate,
    db: AsyncConnection,
) -> CompanyRole:
    await _ensure_any_perm(company_id, caller_id, db, "roles.create", *_ADMIN_FALLBACKS)

    role_name = data.role_name.strip()

    # Generate a unique role code with up to 5 retries on collision.
    new_role_id: int | None = None
    role_code: str = ""
    for _ in range(5):
        role_code = _generate_role_code()
        try:
            result = await db.execute(
                text("""
                    INSERT INTO sec.companyroles
                        (companyid, rolecode, rolename, rolelevel,
                         isdefault, isprotected, iscustom, isactive, notes)
                    VALUES
                        (:cid, :code, :name, 0,
                         FALSE, FALSE, TRUE, TRUE, :notes)
                    RETURNING companyroleid
                """),
                {
                    "cid":   company_id,
                    "code":  role_code,
                    "name":  role_name,
                    "notes": data.notes,
                },
            )
            new_role_id = result.scalar_one()
            break
        except SAIntegrityError:
            # Unique violation on (companyid, rolecode) — retry with new code
            continue

    if new_role_id is None:
        raise HTTPException(
            status_code=500,
            detail="Could not generate a unique role code after 5 attempts. Please try again.",
        )

    await _write_admin_audit(
        db,
        company_id=company_id,
        user_id=caller_id,
        action_code="COMPANY_ROLE_CREATED",
        entity_name="CompanyRoles",
        entity_id=str(new_role_id),
        new_value={"role_code": role_code, "role_name": role_name},
    )

    return await _get_company_role_internal(new_role_id, company_id, db)


async def update_company_role(
    company_role_id: int,
    company_id: int,
    caller_id: int,
    data: CompanyRoleUpdate,
    db: AsyncConnection,
) -> CompanyRole:
    await _ensure_any_perm(company_id, caller_id, db, "roles.edit", *_ADMIN_FALLBACKS)

    # Fetch + lock
    result = await db.execute(
        text("""
            SELECT companyroleid, rolename, isactive, notes, isprotected
            FROM   sec.companyroles
            WHERE  companyroleid = :rid AND companyid = :cid
              AND  isarchived    = FALSE
            FOR UPDATE
        """),
        {"rid": company_role_id, "cid": company_id},
    )
    current = result.mappings().first()
    if current is None:
        raise HTTPException(status_code=404, detail="Company role not found.")

    if data.role_name is not None and current["isprotected"]:
        raise HTTPException(status_code=422, detail="Protected roles cannot be renamed.")

    if data.role_name is not None:
        dup = await db.execute(
            text("""
                SELECT 1 FROM sec.companyroles
                WHERE  companyid = :cid
                  AND  LOWER(rolename) = LOWER(:name)
                  AND  companyroleid != :rid
            """),
            {"cid": company_id, "name": data.role_name, "rid": company_role_id},
        )
        if dup.first() is not None:
            raise HTTPException(status_code=422, detail="Role name already exists for this company.")

    updates: dict = {}
    if data.role_name is not None:
        updates["rolename"] = data.role_name
    if data.is_active is not None:
        updates["isactive"] = data.is_active
    if "notes" in data.model_fields_set:
        updates["notes"] = data.notes

    if updates:
        set_clause = ", ".join(f"{col} = :{col}" for col in updates)
        await db.execute(
            text(f"""
                UPDATE sec.companyroles
                SET {set_clause}, updatedatutc = NOW()
                WHERE companyroleid = :rid AND companyid = :cid
            """),
            {"rid": company_role_id, "cid": company_id, **updates},
        )
        await _write_admin_audit(
            db,
            company_id=company_id,
            user_id=caller_id,
            action_code="COMPANY_ROLE_UPDATED",
            entity_name="CompanyRoles",
            entity_id=str(company_role_id),
            old_value={col: current[col] for col in updates},
            new_value=updates,
        )

    return await _get_company_role_internal(company_role_id, company_id, db)


async def delete_company_role(
    company_role_id: int,
    company_id: int,
    caller_id: int,
    db: AsyncConnection,
) -> None:
    await _ensure_any_perm(company_id, caller_id, db, "roles.delete", *_ADMIN_FALLBACKS)

    # Fetch + lock
    result = await db.execute(
        text("""
            SELECT companyroleid, rolecode, rolename, isprotected
            FROM   sec.companyroles
            WHERE  companyroleid = :rid AND companyid = :cid
            FOR UPDATE
        """),
        {"rid": company_role_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Company role not found.")

    if row["isprotected"]:
        raise HTTPException(
            status_code=422,
            detail=f"'{row['rolename']}' is a protected role and cannot be deleted.",
        )

    # Block if active users are assigned
    count_r = await db.execute(
        text("""
            SELECT COUNT(*) FROM sec.userbranchroles
            WHERE companyroleId = :rid AND isactive = TRUE
        """),
        {"rid": company_role_id},
    )
    if (count_r.scalar() or 0) > 0:
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{row['rolename']}' has active user assignments and cannot be deleted. "
                "Reassign or revoke those assignments first."
            ),
        )

    await db.execute(
        text("""
            UPDATE sec.companyroles
            SET    isarchived = TRUE,
                   archivedat = NOW(),
                   updatedatutc = NOW()
            WHERE  companyroleid = :rid AND companyid = :cid
        """),
        {"rid": company_role_id, "cid": company_id},
    )

    await _write_admin_audit(
        db,
        company_id=company_id,
        user_id=caller_id,
        action_code="COMPANY_ROLE_ARCHIVED",
        entity_name="CompanyRoles",
        entity_id=str(company_role_id),
        old_value={"role_code": row["rolecode"], "role_name": row["rolename"]},
        new_value={"isarchived": True},
    )


async def get_company_role_permissions(
    company_role_id: int,
    company_id: int,
    caller_id: int,
    db: AsyncConnection,
) -> CompanyRolePermissions:
    await _ensure_any_perm(company_id, caller_id, db, "roles.view", *_ADMIN_FALLBACKS)

    # Verify role belongs to this company and get role metadata
    chk = await db.execute(
        text("""
            SELECT rolecode, isprotected
            FROM   sec.companyroles
            WHERE  companyroleid = :rid AND companyid = :cid
              AND  isarchived    = FALSE
        """),
        {"rid": company_role_id, "cid": company_id},
    )
    role_row = chk.mappings().first()
    if role_row is None:
        raise HTTPException(status_code=404, detail="Company role not found.")

    # Company Owner always has ALL permissions — return the full catalogue
    if role_row["isprotected"] and role_row["rolecode"] == "COMPANY_OWNER":
        all_r = await db.execute(
            text("SELECT permissioncode FROM sec.permissions ORDER BY permissioncode"),
        )
        return CompanyRolePermissions(
            company_role_id=company_role_id,
            permission_codes=[r["permissioncode"] for r in all_r.mappings().all()],
        )

    result = await db.execute(
        text("""
            SELECT permissioncode
            FROM   sec.companyrolepermissions
            WHERE  companyroleid = :rid
            ORDER  BY permissioncode
        """),
        {"rid": company_role_id},
    )
    return CompanyRolePermissions(
        company_role_id=company_role_id,
        permission_codes=[r["permissioncode"] for r in result.mappings().all()],
    )


async def set_company_role_permissions(
    company_role_id: int,
    company_id: int,
    caller_id: int,
    data: CompanyRolePermissionsUpdate,
    db: AsyncConnection,
) -> CompanyRolePermissions:
    """
    Replace ALL permissions on a company role (PUT semantics).

    Protected roles (Company Owner) cannot have their critical permissions
    removed — the request is rejected rather than silently enforced, so the
    caller knows exactly what happened.
    """
    await _ensure_any_perm(company_id, caller_id, db, "roles.edit", *_ADMIN_FALLBACKS)

    # Fetch + lock role
    result = await db.execute(
        text("""
            SELECT companyroleid, rolecode, rolename, isprotected
            FROM   sec.companyroles
            WHERE  companyroleid = :rid AND companyid = :cid
              AND  isarchived    = FALSE
            FOR UPDATE
        """),
        {"rid": company_role_id, "cid": company_id},
    )
    role = result.mappings().first()
    if role is None:
        raise HTTPException(status_code=404, detail="Company role not found.")

    # Company Owner permissions are managed automatically — block all manual changes
    if role["isprotected"] and role["rolecode"] == "COMPANY_OWNER":
        raise HTTPException(
            status_code=422,
            detail=(
                "Company Owner permissions are managed automatically. "
                "Company Owner always has full access to all permissions and cannot be modified."
            ),
        )

    new_codes: set[str] = {c.strip() for c in data.permission_codes if c.strip()}

    # Validate all provided codes exist in sec.Permissions
    if new_codes:
        in_clause = ", ".join(f":p{i}" for i in range(len(new_codes)))
        params = {f"p{i}": c for i, c in enumerate(new_codes)}
        valid_r = await db.execute(
            text(f"SELECT permissioncode FROM sec.permissions WHERE permissioncode IN ({in_clause})"),
            params,
        )
        valid_codes = {r["permissioncode"] for r in valid_r.mappings().all()}
        unknown = new_codes - valid_codes
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown permission code(s): {sorted(unknown)}",
            )

    # Auto-add parent permissions to satisfy dependencies (e.g. users.edit -> users.view)
    normalized: set[str] = set(new_codes)
    for code in list(new_codes):
        parent = _PERM_DEPS.get(code)
        if parent and parent not in normalized:
            normalized.add(parent)
    new_codes = normalized

    # Fetch current permissions for audit
    cur_r = await db.execute(
        text("SELECT permissioncode FROM sec.companyrolepermissions WHERE companyroleid = :rid ORDER BY permissioncode"),
        {"rid": company_role_id},
    )
    old_codes = [r["permissioncode"] for r in cur_r.mappings().all()]

    # Replace: delete all, then insert new set
    await db.execute(
        text("DELETE FROM sec.companyrolepermissions WHERE companyroleid = :rid"),
        {"rid": company_role_id},
    )
    for code in sorted(new_codes):
        await db.execute(
            text("""
                INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
                VALUES (:rid, :code)
                ON CONFLICT (companyroleid, permissioncode) DO NOTHING
            """),
            {"rid": company_role_id, "code": code},
        )

    await _write_admin_audit(
        db,
        company_id=company_id,
        user_id=caller_id,
        action_code="COMPANY_ROLE_PERMISSIONS_SET",
        entity_name="CompanyRolePermissions",
        entity_id=str(company_role_id),
        old_value={"permission_codes": old_codes},
        new_value={"permission_codes": sorted(new_codes)},
    )

    return CompanyRolePermissions(
        company_role_id=company_role_id,
        permission_codes=sorted(new_codes),
    )


async def get_company_role_users(
    company_role_id: int,
    company_id: int,
    caller_id: int,
    db: AsyncConnection,
) -> list[CompanyRoleUser]:
    """
    Return all user assignments for a company role (both active and inactive).
    Ordered by display name.
    """
    await _ensure_any_perm(company_id, caller_id, db, "roles.view", "users.view", *_ADMIN_FALLBACKS)

    # Verify role belongs to this company
    chk = await db.execute(
        text("SELECT 1 FROM sec.companyroles WHERE companyroleid = :rid AND companyid = :cid AND isarchived = FALSE"),
        {"rid": company_role_id, "cid": company_id},
    )
    if not chk.fetchone():
        raise HTTPException(status_code=404, detail="Company role not found.")

    result = await db.execute(
        text("""
            SELECT
                ubr.userbranchroleid  AS assignment_id,
                u.userid,
                u.username,
                u.displayname,
                u.email,
                ubr.scopetype,
                ubr.branchid,
                b.branchname,
                ubr.isactive,
                ubr.grantedatutc
            FROM  sec.userbranchroles ubr
            JOIN  sec.users u         ON u.userid    = ubr.userid
            LEFT  JOIN core.branches b ON b.branchid = ubr.branchid
            WHERE ubr.companyroleId = :rid
              AND ubr.companyid     = :cid
            ORDER BY u.displayname, ubr.grantedatutc
        """),
        {"rid": company_role_id, "cid": company_id},
    )
    return [
        CompanyRoleUser(
            assignment_id=row["assignment_id"],
            user_id=row["userid"],
            username=row["username"],
            display_name=row["displayname"],
            email=row["email"],
            scope_type=row["scopetype"],
            branch_id=row["branchid"],
            branch_name=row["branchname"],
            is_active=row["isactive"],
            granted_at_utc=row["grantedatutc"],
        )
        for row in result.mappings().all()
    ]


# ===========================================================================
# Company Role Assignments (new path — via companyroleId)
# ===========================================================================

async def _fetch_company_role_assignment(
    assignment_id: int,
    user_id: int,
    company_id: int,
    db: AsyncConnection,
) -> CompanyRoleAssignmentDetail:
    """Fetch a single company-role assignment row (new path), enriched."""
    result = await db.execute(
        text("""
            SELECT
                ubr.userbranchroleid  AS assignment_id,
                ubr.userid,
                cr.companyroleid,
                cr.rolecode           AS company_role_code,
                cr.rolename           AS company_role_name,
                ubr.scopetype,
                ubr.branchid,
                b.branchname,
                ubr.isactive,
                ubr.grantedbyuserid,
                ubr.grantedatutc,
                ubr.revokedatutc,
                ubr.notes
            FROM  sec.userbranchroles ubr
            JOIN  sec.companyroles cr  ON cr.companyroleid = ubr.companyroleId
            LEFT  JOIN core.branches b ON b.branchid       = ubr.branchid
            WHERE ubr.userbranchroleid = :aid
              AND ubr.userid           = :uid
              AND ubr.companyid        = :cid
        """),
        {"aid": assignment_id, "uid": user_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Company role assignment not found.")
    return CompanyRoleAssignmentDetail(
        assignment_id=row["assignment_id"],
        user_id=row["userid"],
        company_role_id=row["companyroleid"],
        company_role_code=row["company_role_code"],
        company_role_name=row["company_role_name"],
        scope_type=row["scopetype"],
        branch_id=row["branchid"],
        branch_name=row["branchname"],
        is_active=row["isactive"],
        granted_by_user_id=row["grantedbyuserid"],
        granted_at_utc=row["grantedatutc"],
        revoked_at_utc=row["revokedatutc"],
        notes=row["notes"],
    )


async def assign_company_role(
    target_user_id: int,
    company_id: int,
    caller_id: int,
    data: CompanyRoleAssignmentCreate,
    db: AsyncConnection,
) -> CompanyRoleAssignmentDetail:
    """
    Assign a company role to a user (new-path assignment via companyroleId).

    Rules:
    - Company Owner cannot be assigned via this endpoint (use transfer flow).
    - Validates role belongs to this company.
    - Validates scope / branch alignment.
    - Revokes any existing active company-role assignment for the user.
    - Inserts a new userbranchroles row with companyroleId set, roleid NULL.
    """
    await _ensure_any_perm(company_id, caller_id, db, "users.edit", "roles.edit", *_ADMIN_FALLBACKS)

    # Confirm target user exists
    chk_user = await db.execute(
        text("SELECT 1 FROM sec.users WHERE userid = :uid AND companyid = :cid AND isactive = TRUE"),
        {"uid": target_user_id, "cid": company_id},
    )
    if not chk_user.fetchone():
        raise HTTPException(status_code=404, detail="User not found or inactive.")

    # Fetch company role — must belong to this company
    cr_result = await db.execute(
        text("""
            SELECT companyroleid, rolecode, rolename, isprotected, isactive
            FROM   sec.companyroles
            WHERE  companyroleid = :rid AND companyid = :cid
        """),
        {"rid": data.company_role_id, "cid": company_id},
    )
    cr_row = cr_result.mappings().first()
    if cr_row is None:
        raise HTTPException(status_code=422, detail="Company role not found for this company.")
    if not cr_row["isactive"]:
        raise HTTPException(status_code=422, detail="Cannot assign an inactive company role.")

    # Block normal assignment of Company Owner — must use transfer endpoint
    if cr_row["rolecode"] == "COMPANY_OWNER":
        raise HTTPException(
            status_code=422,
            detail=(
                "Company Owner cannot be assigned via this endpoint. "
                "Use POST /admin/company-owner/transfer to transfer ownership."
            ),
        )

    # Driver role requires a home branch (must have a SpecificBranch or OwnDriverDataOnly scope).
    # AllCompanyBranches carries no branch_id — the driver would have no profile and pay rates
    # would be impossible.  Reject clearly so the UI can guide the user.
    if "DRIVER" in cr_row["rolecode"].upper() and data.scope_type == "AllCompanyBranches":
        raise HTTPException(
            status_code=422,
            detail=(
                "Driver access requires a home branch. "
                "Please assign this role with SpecificBranch or OwnDriverDataOnly scope "
                "and select the driver's home branch."
            ),
        )

    # Validate scope / branch alignment
    if data.scope_type == "AllCompanyBranches":
        if data.branch_id is not None:
            raise HTTPException(
                status_code=422,
                detail="branch_id must be null for AllCompanyBranches scope.",
            )
    else:
        if data.branch_id is None:
            raise HTTPException(
                status_code=422,
                detail=f"branch_id is required for {data.scope_type} scope.",
            )
        branch_chk = await db.execute(
            text("SELECT 1 FROM core.branches WHERE branchid = :bid AND companyid = :cid"),
            {"bid": data.branch_id, "cid": company_id},
        )
        if not branch_chk.fetchone():
            raise HTTPException(status_code=422, detail="Branch not found for this company.")

    # Revoke all existing active company-role assignments for this user
    await db.execute(
        text("""
            UPDATE sec.userbranchroles
            SET isactive = FALSE, revokedatutc = NOW()
            WHERE userid        = :uid
              AND companyid     = :cid
              AND companyroleId IS NOT NULL
              AND isactive      = TRUE
        """),
        {"uid": target_user_id, "cid": company_id},
    )

    # Insert new assignment (new path: roleid = NULL, companyroleId = role)
    try:
        ins = await db.execute(
            text("""
                INSERT INTO sec.userbranchroles
                    (userid, companyid, branchid, roleid, companyroleId, scopetype,
                     isactive, grantedbyuserid, notes)
                VALUES
                    (:uid, :cid, :bid, NULL, :crid, :scope,
                     TRUE, :grantor, :notes)
                RETURNING userbranchroleid
            """),
            {
                "uid":     target_user_id,
                "cid":     company_id,
                "bid":     data.branch_id,
                "crid":    data.company_role_id,
                "scope":   data.scope_type,
                "grantor": caller_id,
                "notes":   data.notes,
            },
        )
    except SAIntegrityError:
        raise HTTPException(status_code=422, detail="Role assignment could not be created.")
    new_assignment_id: int = ins.scalar_one()

    await _write_admin_audit(
        db,
        company_id=company_id,
        user_id=caller_id,
        action_code="COMPANY_ROLE_ASSIGNED",
        entity_name="UserBranchRoles",
        entity_id=str(new_assignment_id),
        new_value={
            "target_user_id":    target_user_id,
            "company_role_code": cr_row["rolecode"],
            "company_role_name": cr_row["rolename"],
            "scope_type":        data.scope_type,
            "branch_id":         data.branch_id,
        },
    )

    # Auto-create driver profile when the assigned role is a driver role.
    if "DRIVER" in cr_row["rolecode"].upper() and data.branch_id is not None:
        await ensure_driver_profile(
            db=db,
            user_id=target_user_id,
            company_id=company_id,
            branch_id=data.branch_id,
        )

    return await _fetch_company_role_assignment(new_assignment_id, target_user_id, company_id, db)


async def revoke_company_role_assignment(
    assignment_id: int,
    target_user_id: int,
    company_id: int,
    caller_id: int,
    db: AsyncConnection,
) -> CompanyRoleAssignmentDetail:
    """
    Revoke a company-role assignment (soft delete).
    Company Owner assignments cannot be revoked here — use transfer endpoint.
    """
    await _ensure_any_perm(company_id, caller_id, db, "users.edit", "roles.edit", *_ADMIN_FALLBACKS)

    # Fetch and lock
    result = await db.execute(
        text("""
            SELECT
                ubr.userbranchroleid AS assignment_id,
                ubr.userid,
                cr.companyroleid,
                cr.rolecode          AS company_role_code,
                cr.rolename          AS company_role_name,
                ubr.isactive
            FROM  sec.userbranchroles ubr
            JOIN  sec.companyroles cr ON cr.companyroleid = ubr.companyroleId
            WHERE ubr.userbranchroleid = :aid
              AND ubr.userid           = :uid
              AND ubr.companyid        = :cid
              AND ubr.companyroleId IS NOT NULL
            FOR UPDATE OF ubr
        """),
        {"aid": assignment_id, "uid": target_user_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Company role assignment not found.")

    if row["company_role_code"] == "COMPANY_OWNER":
        raise HTTPException(
            status_code=422,
            detail=(
                "Company Owner assignment cannot be revoked directly. "
                "Use POST /admin/company-owner/transfer to transfer ownership."
            ),
        )

    if row["isactive"]:
        await db.execute(
            text("""
                UPDATE sec.userbranchroles
                SET isactive = FALSE, revokedatutc = NOW()
                WHERE userbranchroleid = :aid
            """),
            {"aid": assignment_id},
        )
        await _write_admin_audit(
            db,
            company_id=company_id,
            user_id=caller_id,
            action_code="COMPANY_ROLE_REVOKED",
            entity_name="UserBranchRoles",
            entity_id=str(assignment_id),
            old_value={
                "target_user_id":    target_user_id,
                "company_role_code": row["company_role_code"],
            },
        )

    return await _fetch_company_role_assignment(assignment_id, target_user_id, company_id, db)


# ===========================================================================
# Ownership Transfer
# ===========================================================================

async def transfer_company_owner(
    company_id: int,
    caller_id: int,
    data: OwnerTransferRequest,
    db: AsyncConnection,
) -> OwnerTransferResult:
    """
    Transfer Company Owner authority to another active user.

    Rules:
    - Caller must currently be the Company Owner.
    - data.confirmation must be "TRANSFER".
    - Target must be active, can_login=TRUE, same company, different from caller.
    - Atomically:
        1. Revoke caller's COMPANY_OWNER assignment.
        2. Optionally assign replacement company role to caller.
        3. Revoke any existing company role for target, assign COMPANY_OWNER.
    - Audited as OWNER_TRANSFERRED.
    """
    await _ensure_admin(company_id, caller_id, db)

    # Fetch COMPANY_OWNER company role for this company
    co_result = await db.execute(
        text("""
            SELECT companyroleid FROM sec.companyroles
            WHERE companyid = :cid AND rolecode = 'COMPANY_OWNER' AND isactive = TRUE
        """),
        {"cid": company_id},
    )
    co_row = co_result.mappings().first()
    if co_row is None:
        raise HTTPException(status_code=500, detail="COMPANY_OWNER role not found for this company.")
    owner_company_role_id: int = co_row["companyroleid"]

    # Verify caller is the current Company Owner
    caller_owner_result = await db.execute(
        text("""
            SELECT ubr.userbranchroleid, u.displayname
            FROM   sec.userbranchroles ubr
            JOIN   sec.users u ON u.userid = ubr.userid
            WHERE  ubr.userid        = :caller
              AND  ubr.companyid     = :cid
              AND  ubr.companyroleId = :co_rid
              AND  ubr.isactive      = TRUE
            FOR UPDATE OF ubr
        """),
        {"caller": caller_id, "cid": company_id, "co_rid": owner_company_role_id},
    )
    caller_owner_row = caller_owner_result.mappings().first()
    if caller_owner_row is None:
        raise HTTPException(
            status_code=403,
            detail="Only the current Company Owner can transfer ownership.",
        )
    caller_assignment_id: int = caller_owner_row["userbranchroleid"]
    caller_display_name: str = caller_owner_row["displayname"]

    # Validate target user
    if data.target_user_id == caller_id:
        raise HTTPException(status_code=422, detail="Cannot transfer ownership to yourself.")

    target_result = await db.execute(
        text("""
            SELECT userid, displayname, isactive, canlogin
            FROM   sec.users
            WHERE  userid = :uid AND companyid = :cid
            FOR UPDATE
        """),
        {"uid": data.target_user_id, "cid": company_id},
    )
    target_row = target_result.mappings().first()
    if target_row is None:
        raise HTTPException(status_code=404, detail="Target user not found in this company.")
    if not target_row["isactive"]:
        raise HTTPException(status_code=422, detail="Target user must be active to receive ownership.")
    if not target_row["canlogin"]:
        raise HTTPException(status_code=422, detail="Target user must have login enabled to receive ownership.")
    target_display_name: str = target_row["displayname"]

    # Validate replacement role if provided
    replacement_role_name: str | None = None
    if data.replacement_company_role_id is not None:
        repl_r = await db.execute(
            text("""
                SELECT companyroleid, rolecode, rolename, isactive
                FROM   sec.companyroles
                WHERE  companyroleid = :rid AND companyid = :cid
            """),
            {"rid": data.replacement_company_role_id, "cid": company_id},
        )
        repl_row = repl_r.mappings().first()
        if repl_row is None:
            raise HTTPException(
                status_code=422,
                detail="Replacement company role not found for this company.",
            )
        if not repl_row["isactive"]:
            raise HTTPException(status_code=422, detail="Replacement company role is inactive.")
        if repl_row["rolecode"] == "COMPANY_OWNER":
            raise HTTPException(
                status_code=422,
                detail="Cannot use Company Owner as the replacement role.",
            )
        replacement_role_name = repl_row["rolename"]

    # ── Atomic transfer ──

    # 1. Revoke caller's COMPANY_OWNER assignment
    await db.execute(
        text("""
            UPDATE sec.userbranchroles
            SET isactive = FALSE, revokedatutc = NOW()
            WHERE userbranchroleid = :aid
        """),
        {"aid": caller_assignment_id},
    )

    # 2. If replacement role provided, assign it to the old owner (AllCompanyBranches)
    if data.replacement_company_role_id is not None:
        await db.execute(
            text("""
                UPDATE sec.userbranchroles
                SET isactive = FALSE, revokedatutc = NOW()
                WHERE userid        = :uid
                  AND companyid     = :cid
                  AND companyroleId IS NOT NULL
                  AND isactive      = TRUE
            """),
            {"uid": caller_id, "cid": company_id},
        )
        await db.execute(
            text("""
                INSERT INTO sec.userbranchroles
                    (userid, companyid, branchid, roleid, companyroleId, scopetype,
                     isactive, grantedbyuserid, notes)
                VALUES
                    (:uid, :cid, NULL, NULL, :crid, 'AllCompanyBranches',
                     TRUE, :grantor, 'Assigned after ownership transfer')
            """),
            {
                "uid":     caller_id,
                "cid":     company_id,
                "crid":    data.replacement_company_role_id,
                "grantor": caller_id,
            },
        )

    # 3. Revoke any existing company role for target, then assign COMPANY_OWNER
    await db.execute(
        text("""
            UPDATE sec.userbranchroles
            SET isactive = FALSE, revokedatutc = NOW()
            WHERE userid        = :uid
              AND companyid     = :cid
              AND companyroleId IS NOT NULL
              AND isactive      = TRUE
        """),
        {"uid": data.target_user_id, "cid": company_id},
    )
    await db.execute(
        text("""
            INSERT INTO sec.userbranchroles
                (userid, companyid, branchid, roleid, companyroleId, scopetype,
                 isactive, grantedbyuserid, notes)
            VALUES
                (:uid, :cid, NULL, NULL, :crid, 'AllCompanyBranches',
                 TRUE, :grantor, 'Company Owner role assigned via ownership transfer')
        """),
        {
            "uid":     data.target_user_id,
            "cid":     company_id,
            "crid":    owner_company_role_id,
            "grantor": caller_id,
        },
    )

    # Audit
    await _write_admin_audit(
        db,
        company_id=company_id,
        user_id=caller_id,
        action_code="OWNER_TRANSFERRED",
        entity_name="UserBranchRoles",
        entity_id=str(data.target_user_id),
        old_value={"previous_owner_user_id": caller_id, "previous_owner": caller_display_name},
        new_value={
            "new_owner_user_id": data.target_user_id,
            "new_owner":         target_display_name,
            "replacement_role":  replacement_role_name,
        },
    )

    return OwnerTransferResult(
        new_owner_user_id=data.target_user_id,
        new_owner_display_name=target_display_name,
        previous_owner_user_id=caller_id,
        previous_owner_display_name=caller_display_name,
        previous_owner_new_role_id=data.replacement_company_role_id,
        previous_owner_new_role_name=replacement_role_name,
    )


# ===========================================================================
# User Permission Overrides (member-specific extra ALLOW permissions)
# ===========================================================================

async def get_user_permission_overrides(
    target_user_id: int,
    company_id: int,
    caller_id: int,
    db: AsyncConnection,
) -> list[str]:
    """Return active ALLOW override permission codes for one user."""
    await _ensure_any_perm(company_id, caller_id, db, "users.view", *_ADMIN_FALLBACKS)
    chk = await db.execute(
        text("SELECT 1 FROM sec.users WHERE userid = :uid AND companyid = :cid"),
        {"uid": target_user_id, "cid": company_id},
    )
    if not chk.fetchone():
        raise HTTPException(status_code=404, detail="User not found.")

    result = await db.execute(
        text("""
            SELECT permissioncode
            FROM   sec.userpermissionoverrides
            WHERE  userid    = :uid
              AND  companyid = :cid
              AND  isactive  = TRUE
              AND  effect    = 'ALLOW'
            ORDER  BY permissioncode
        """),
        {"uid": target_user_id, "cid": company_id},
    )
    return [r["permissioncode"] for r in result.mappings().all()]


async def set_user_permission_overrides(
    target_user_id: int,
    company_id: int,
    caller_id: int,
    data: UserPermissionOverridesUpdate,
    db: AsyncConnection,
) -> list[str]:
    """
    Replace all active ALLOW overrides for a user (PUT semantics).

    Rules:
    - Validates all codes exist in sec.permissions.
    - Blocks setting overrides on Company Owner (they already have all permissions).
    - Auto-adds parent permissions when a child is included (dependency normalisation).
    - Soft-deletes existing overrides then inserts new set in one transaction.
    """
    # Permission overrides are person-specific — only users.edit (not roles.edit)
    # can mutate individual member permissions.
    await _ensure_any_perm(company_id, caller_id, db, "users.edit", *_ADMIN_FALLBACKS)

    # Confirm target user exists + get their company role for COMPANY_OWNER guard
    u_r = await db.execute(
        text("""
            SELECT u.userid
            FROM   sec.users u
            WHERE  u.userid    = :uid
              AND  u.companyid = :cid
        """),
        {"uid": target_user_id, "cid": company_id},
    )
    if not u_r.fetchone():
        raise HTTPException(status_code=404, detail="User not found.")

    # Block Company Owner — already has all permissions
    co_r = await db.execute(
        text("""
            SELECT 1
            FROM   sec.userbranchroles ubr
            JOIN   sec.companyroles cr ON cr.companyroleid = ubr.companyroleId
            WHERE  ubr.userid    = :uid
              AND  ubr.companyid = :cid
              AND  ubr.isactive  = TRUE
              AND  cr.rolecode   = 'COMPANY_OWNER'
        """),
        {"uid": target_user_id, "cid": company_id},
    )
    if co_r.fetchone():
        raise HTTPException(
            status_code=422,
            detail=(
                "Company Owner already has all permissions. "
                "Extra permission overrides cannot be set for the Company Owner."
            ),
        )

    new_codes: set[str] = {c.strip() for c in data.permission_codes if c.strip()}

    # Validate all codes exist
    if new_codes:
        in_clause = ", ".join(f":p{i}" for i in range(len(new_codes)))
        params = {f"p{i}": c for i, c in enumerate(new_codes)}
        valid_r = await db.execute(
            text(f"SELECT permissioncode FROM sec.permissions WHERE permissioncode IN ({in_clause})"),
            params,
        )
        valid_codes = {r["permissioncode"] for r in valid_r.mappings().all()}
        unknown = new_codes - valid_codes
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown permission code(s): {sorted(unknown)}",
            )

    # Auto-add parent permissions for dependency satisfaction
    normalized: set[str] = set(new_codes)
    for code in list(new_codes):
        parent = _PERM_DEPS.get(code)
        if parent and parent not in normalized:
            normalized.add(parent)
    new_codes = normalized

    # Fetch old overrides for audit
    old_r = await db.execute(
        text("""
            SELECT permissioncode
            FROM   sec.userpermissionoverrides
            WHERE  userid = :uid AND companyid = :cid AND isactive = TRUE AND effect = 'ALLOW'
            ORDER  BY permissioncode
        """),
        {"uid": target_user_id, "cid": company_id},
    )
    old_codes = [r["permissioncode"] for r in old_r.mappings().all()]

    # Soft-delete all existing active overrides
    await db.execute(
        text("""
            UPDATE sec.userpermissionoverrides
            SET isactive = FALSE, revokedatutc = NOW()
            WHERE userid = :uid AND companyid = :cid AND isactive = TRUE AND effect = 'ALLOW'
        """),
        {"uid": target_user_id, "cid": company_id},
    )

    # Insert new set
    for code in sorted(new_codes):
        await db.execute(
            text("""
                INSERT INTO sec.userpermissionoverrides
                    (userid, companyid, permissioncode, effect, isactive, grantedbyuserid)
                VALUES
                    (:uid, :cid, :code, 'ALLOW', TRUE, :grantor)
                ON CONFLICT DO NOTHING
            """),
            {"uid": target_user_id, "cid": company_id, "code": code, "grantor": caller_id},
        )

    await _write_admin_audit(
        db,
        company_id=company_id,
        user_id=caller_id,
        action_code="USER_PERMISSION_OVERRIDES_SET",
        entity_name="UserPermissionOverrides",
        entity_id=str(target_user_id),
        old_value={"permission_codes": old_codes},
        new_value={"permission_codes": sorted(new_codes)},
    )

    return sorted(new_codes)


# ===========================================================================
# Driver profile auto-linking (Phase 1)
# ===========================================================================

async def _driver_has_payroll_history(driver_id: int, db: AsyncConnection) -> bool:
    """Return True if the driver has any payroll/rate/pay-rule history.

    Used to block unsafe direct Driver.BranchID mutation: if history exists,
    callers must use the Driver Transfer workflow instead.
    """
    # Check each table individually to avoid asyncpg's limitation with repeated
    # positional parameters ($1) across UNION ALL branches.
    for query in (
        "SELECT 1 FROM payroll.payrolldraftlines WHERE driverid = :did LIMIT 1",
        "SELECT 1 FROM payroll.payrollfinallines  WHERE driverid = :did LIMIT 1",
        "SELECT 1 FROM payroll.driverrates        WHERE driverid = :did LIMIT 1",
        "SELECT 1 FROM payroll.driverpayrules     WHERE driverid = :did LIMIT 1",
    ):
        row = await db.execute(text(query), {"did": driver_id})
        if row.first() is not None:
            return True
    return False


async def ensure_driver_profile(
    db: AsyncConnection,
    user_id: int,
    company_id: int,
    branch_id: int | None,
) -> dict:
    """
    Ensure a Driver profile exists for a user with the Driver role.
    Creates core.Employees + core.Drivers rows if missing.
    Links sec.Users.EmployeeID.
    Returns {"employee_id": ..., "driver_id": ..., "created": bool}.
    Idempotent — safe to call multiple times.
    """
    # Look up the user to get display_name and existing employee_id
    u_result = await db.execute(
        text("""
            SELECT userid, displayname, employeeid
            FROM   sec.users
            WHERE  userid = :uid AND companyid = :cid
        """),
        {"uid": user_id, "cid": company_id},
    )
    u_row = u_result.mappings().first()
    if u_row is None:
        raise HTTPException(status_code=404, detail="User not found.")

    existing_emp_id: int | None = u_row["employeeid"]

    # Case 1 — user already linked to an employee
    if existing_emp_id is not None:
        drv_result = await db.execute(
            text("""
                SELECT driverid, branchid FROM core.drivers
                WHERE  employeeid  = :eid
                  AND  companyid   = :cid
                  AND  driverstatus NOT IN ('Transferred', 'Terminated')
                ORDER  BY driverid DESC
                LIMIT  1
            """),
            {"eid": existing_emp_id, "cid": company_id},
        )
        drv_row = drv_result.mappings().first()
        if drv_row is not None:
            # Driver already exists — sync branch if the role assignment changed it.
            # Guard: if the target branch differs and driver has payroll/rate history,
            # refuse the direct mutation — use Driver Transfer workflow instead.
            if branch_id is not None and branch_id != drv_row["branchid"]:
                if await _driver_has_payroll_history(drv_row["driverid"], db):
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "Driver has payroll/rate history attached to their current branch. "
                            "Use Driver Transfer workflow to move the driver to a new branch."
                        ),
                    )
                # No history — safe to reassign branch directly
                await db.execute(
                    text("""
                        UPDATE core.drivers
                        SET    branchid = :bid
                        WHERE  driverid  = :did
                          AND  companyid = :cid
                    """),
                    {"bid": branch_id, "did": drv_row["driverid"], "cid": company_id},
                )
                await db.execute(
                    text("""
                        UPDATE core.employees
                        SET    branchid = :bid
                        WHERE  employeeid = :eid
                          AND  companyid  = :cid
                    """),
                    {"bid": branch_id, "eid": existing_emp_id, "cid": company_id},
                )
            return {"employee_id": existing_emp_id, "driver_id": drv_row["driverid"], "created": False}
        # Employee exists but no driver row — create it
        drv_ins = await db.execute(
            text("""
                INSERT INTO core.drivers (companyid, employeeid, branchid, drivercode, driverstatus)
                VALUES (:cid, :eid, :bid, :code, 'Active')
                RETURNING driverid
            """),
            {
                "cid":  company_id,
                "eid":  existing_emp_id,
                "bid":  branch_id,
                "code": f"DRV-{existing_emp_id:05d}",
            },
        )
        new_drv_id: int = drv_ins.scalar_one()
        return {"employee_id": existing_emp_id, "driver_id": new_drv_id, "created": True}

    # Case 2 — no employee link yet; check for an existing Employee by name/company
    display_name: str = u_row["displayname"] or f"User {user_id}"
    emp_key = f"EMP-{user_id:05d}"

    existing_emp_result = await db.execute(
        text("""
            SELECT employeeid FROM core.employees
            WHERE  companyid    = :cid
              AND  employeekey  = :ekey
        """),
        {"cid": company_id, "ekey": emp_key},
    )
    existing_emp_row = existing_emp_result.mappings().first()

    if existing_emp_row is not None:
        employee_id: int = existing_emp_row["employeeid"]
    else:
        # Create Employee row
        emp_ins = await db.execute(
            text("""
                INSERT INTO core.employees
                    (companyid, branchid, employeekey, fullname, employeetype, employmentstatus)
                VALUES (:cid, :bid, :ekey, :name, 'Driver', 'Active')
                RETURNING employeeid
            """),
            {
                "cid":  company_id,
                "bid":  branch_id,
                "ekey": emp_key,
                "name": display_name,
            },
        )
        employee_id = emp_ins.scalar_one()

    # Link user to employee
    await db.execute(
        text("UPDATE sec.users SET employeeid = :eid WHERE userid = :uid"),
        {"eid": employee_id, "uid": user_id},
    )

    # Check if driver row already exists for this employee
    drv_check = await db.execute(
        text("""
            SELECT driverid, branchid FROM core.drivers
            WHERE  employeeid = :eid AND companyid = :cid
        """),
        {"eid": employee_id, "cid": company_id},
    )
    existing_drv = drv_check.mappings().first()
    if existing_drv is not None:
        # Sync branch — same guard as Case 1: block if driver has history
        if branch_id is not None and branch_id != existing_drv["branchid"]:
            if await _driver_has_payroll_history(existing_drv["driverid"], db):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Driver has payroll/rate history attached to their current branch. "
                        "Use Driver Transfer workflow to move the driver to a new branch."
                    ),
                )
            await db.execute(
                text("""
                    UPDATE core.drivers
                    SET    branchid = :bid
                    WHERE  driverid  = :did
                      AND  companyid = :cid
                """),
                {"bid": branch_id, "did": existing_drv["driverid"], "cid": company_id},
            )
            await db.execute(
                text("""
                    UPDATE core.employees
                    SET    branchid = :bid
                    WHERE  employeeid = :eid
                      AND  companyid  = :cid
                """),
                {"bid": branch_id, "eid": employee_id, "cid": company_id},
            )
        return {"employee_id": employee_id, "driver_id": existing_drv["driverid"], "created": False}

    # Create Driver row
    drv_ins2 = await db.execute(
        text("""
            INSERT INTO core.drivers (companyid, employeeid, branchid, drivercode, driverstatus)
            VALUES (:cid, :eid, :bid, :code, 'Active')
            RETURNING driverid
        """),
        {
            "cid":  company_id,
            "eid":  employee_id,
            "bid":  branch_id,
            "code": f"DRV-{employee_id:05d}",
        },
    )
    new_driver_id: int = drv_ins2.scalar_one()
    return {"employee_id": employee_id, "driver_id": new_driver_id, "created": True}


async def get_user_driver_info(
    user_id: int,
    company_id: int,
    caller_user_id: int,
    db: AsyncConnection,
) -> dict:
    """
    Return driver profile info for a user (target: user_id).

    Security rules:
      1. Caller must have one of: users.view, payrates.view, payrates.edit,
         settings.manage, setup.manage.
      2. If caller has OwnDriverDataOnly scope, they may only look up their own user_id.
      3. If caller has SpecificBranch scope, they may only see targets whose
         driver profile belongs to their allowed branch.
    """
    # Rule 1 — permission gate
    await _check_any_permission(
        company_id, caller_user_id, None,
        ["users.view", "payrates.view", "payrates.edit", "settings.manage", "setup.manage"],
        db,
    )

    # Rule 2 — OwnDriverDataOnly: caller may only look up themselves
    scope_result = await db.execute(
        text("""
            SELECT scopetype FROM sec.userbranchroles
            WHERE userid = :uid AND companyid = :cid AND isactive = TRUE
            ORDER BY userbranchroleid DESC LIMIT 1
        """),
        {"uid": caller_user_id, "cid": company_id},
    )
    scope_row = scope_result.mappings().first()
    if scope_row and scope_row["scopetype"] == "OwnDriverDataOnly":
        if user_id != caller_user_id:
            raise HTTPException(
                status_code=403,
                detail="You can only look up your own driver profile.",
            )

    result = await db.execute(
        text("""
            SELECT
                u.userid,
                u.employeeid,
                d.driverid,
                e.fullname       AS driver_name,
                d.drivercode,
                d.driverstatus,
                d.branchid,
                b.branchname
            FROM   sec.users         u
            LEFT JOIN core.employees e ON e.employeeid = u.employeeid
                                      AND e.companyid  = :cid
            LEFT JOIN core.drivers   d ON d.employeeid = e.employeeid
                                      AND d.companyid  = :cid
                                      AND d.driverstatus NOT IN ('Transferred','Terminated')
            LEFT JOIN core.branches  b ON b.branchid   = d.branchid
            WHERE  u.userid    = :uid
              AND  u.companyid = :cid
        """),
        {"uid": user_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="User not found.")

    # Rule 3 — SpecificBranch scope: verify caller can see the driver's branch
    if row["branchid"] is not None:
        # _check_branch_access returns (can_see_all, branch_ids)
        can_see_all, caller_branches = await _check_branch_access(company_id, caller_user_id, db)
        if not can_see_all and row["branchid"] not in caller_branches:
            raise HTTPException(
                status_code=403,
                detail="Access denied to this driver's branch.",
            )

    has_profile = row["driverid"] is not None
    return {
        "user_id":          user_id,
        "employee_id":      row["employeeid"],
        "driver_id":        row["driverid"],
        "driver_name":      row["driver_name"],
        "driver_code":      row["drivercode"],
        "branch_id":        row["branchid"],
        "branch_name":      row["branchname"],
        "driver_status":    row["driverstatus"],
        "has_driver_profile": has_profile,
    }
