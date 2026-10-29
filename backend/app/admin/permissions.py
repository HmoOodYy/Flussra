"""Admin permission catalogue, company-role permissions, and user overrides."""

from fastapi import HTTPException
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.admin import audit, security
from app.admin.schemas import (
    CompanyRolePermissions,
    CompanyRolePermissionsUpdate,
    Permission,
    UserPermissionOverridesUpdate,
)

# Modules whose permissions are offered by the role editor and the per-user
# extra-permission picker (``ui_only=True``).
_UI_MODULE_CODES = (
    'company', 'roles', 'users', 'payroll', 'payitems',
    'payrates', 'drivers', 'dispatch', 'reports', 'settings', 'payroll_setup',
    'review', 'employees', 'ledger',
)


# Child permission -> required parent normalization.
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
    'employees.manage': 'employees.view',
    'ledger.audit.view': 'ledger.view',
}


async def list_permissions(
    company_id: int,
    caller_id: int,
    db: AsyncConnection,
    *,
    ui_only: bool = True,
) -> list[Permission]:
    await security.ensure_any_permission(company_id, caller_id, db, "roles.view", "users.view", *security.ADMIN_FALLBACKS)

    if ui_only:
        result = await db.execute(
            text("""
                SELECT permissionid, permissioncode, permissionname, modulecode, notes
                FROM   sec.permissions
                WHERE  modulecode IN :ui_modules
                ORDER  BY modulecode, permissioncode
            """).bindparams(bindparam("ui_modules", expanding=True)),
            {"ui_modules": list(_UI_MODULE_CODES)},
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


async def get_company_role_permissions(
    company_role_id: int,
    company_id: int,
    caller_id: int,
    db: AsyncConnection,
) -> CompanyRolePermissions:
    await security.ensure_any_permission(company_id, caller_id, db, "roles.view", *security.ADMIN_FALLBACKS)

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
    await security.ensure_any_permission(company_id, caller_id, db, "roles.edit", *security.ADMIN_FALLBACKS)

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
    if role["rolecode"] == "DRIVER" and new_codes:
        raise HTTPException(status_code=422, detail="The DRIVER role cannot receive generic permissions in P2b.")

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

    await audit.write_admin_audit(
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


async def get_user_permission_overrides(
    target_user_id: int,
    company_id: int,
    caller_id: int,
    db: AsyncConnection,
) -> list[str]:
    """Return active ALLOW override permission codes for one user."""
    await security.ensure_any_permission(company_id, caller_id, db, "users.view", *security.ADMIN_FALLBACKS)
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
    await security.ensure_any_permission(company_id, caller_id, db, "users.edit", *security.ADMIN_FALLBACKS)

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
              AND  ubr.scopetype = 'AllCompanyBranches'
              AND  ubr.branchid IS NULL
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
    driver_subject = await db.execute(
        text("""
            SELECT EXISTS (
                SELECT 1 FROM sec.UserBranchRoles ubr
                LEFT JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = ubr.CompanyRoleID
                WHERE ubr.UserID = :uid AND ubr.CompanyID = :cid AND ubr.IsActive
                  AND (ubr.ScopeType = 'Self' OR cr.RoleCode = 'DRIVER')
            )
        """),
        {"uid": target_user_id, "cid": company_id},
    )
    if new_codes and driver_subject.scalar_one():
        raise HTTPException(status_code=422, detail="DRIVER Self accounts cannot receive generic permission overrides.")

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

    await audit.write_admin_audit(
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
