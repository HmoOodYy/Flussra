"""Admin user reads, role-assignment projections, and Driver reads."""

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.branch_access import check_branch_access
from app.access.permissions import check_any_permission
from app.access.policy import require_non_driver_subject
from app.admin import security
from app.admin.schemas import (
    EmployeeLinkCandidate,
    RoleAssignment,
    UserAdmin,
)


async def fetch_role_assignments(
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
    Return {user_id: driver_id} for users linked to a permanent core.Drivers row.
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
                                      AND e.companyid = u.companyid
            JOIN   core.drivers     d ON d.employeeid = e.employeeid
                                      AND d.companyid = e.companyid
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
    owner_authority_role_ids: set[int],
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

    # Dynamic Owner permissions require an active AllCompanyBranches assignment
    # with NULL BranchID; role identity alone does not establish that authority.
    owner_ids = role_ids & owner_authority_role_ids

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
        employee_id=row["employeeid"],
        employee_name=row["employeename"],
        is_staged=row["isstaged"],
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


async def get_user_by_id_internal(
    target_user_id: int,
    company_id: int,
    db: AsyncConnection,
) -> UserAdmin:
    """Fetch user + assignments without permission check — for internal post-write reads."""
    result = await db.execute(
        text("""
            SELECT
                u.userid, u.companyid, u.employeeid, u.isstaged, u.username, u.displayname,
                u.email, u.phone, u.isactive, u.canlogin,
                u.mustchangepassword, u.lastloginatutc,
                u.createdatutc, u.updatedatutc, e.fullname AS employeename
            FROM  sec.users u
            LEFT  JOIN core.employees e ON e.employeeid = u.employeeid AND e.companyid = u.companyid
            WHERE u.userid    = :uid
              AND u.companyid = :cid
        """),
        {"uid": target_user_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="User not found.")
    uid_list = [target_user_id]
    assignments_map = await fetch_role_assignments(company_id, uid_list, db)
    cr_map = await _fetch_company_roles_for_users(company_id, uid_list, db)
    drv_map = await _fetch_driver_ids_for_users(company_id, uid_list, db)
    extra_map = await _fetch_extra_perms_for_users(company_id, uid_list, db)
    cr_info = cr_map.get(target_user_id)
    role_ids_set = {cr_info["companyroleid"]} if cr_info else set()
    owner_role_ids = (
        {cr_info["companyroleid"]}
        if cr_info
        and cr_info["company_role_code"] == "COMPANY_OWNER"
        and cr_info["company_role_scope"] == "AllCompanyBranches"
        and cr_info["company_role_branch_id"] is None
        else set()
    )
    role_perms_map = await _fetch_role_perms_for_roles(company_id, role_ids_set, owner_role_ids, db)
    return _row_to_user(
        row,
        assignments_map.get(target_user_id, []),
        company_role=cr_info,
        driver_id=drv_map.get(target_user_id),
        extra_perm_codes=extra_map.get(target_user_id),
        role_perm_codes=role_perms_map.get(cr_info["companyroleid"]) if cr_info else None,
    )


async def list_users(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    include_inactive: bool = False,
) -> list[UserAdmin]:
    await security.ensure_any_permission(company_id, user_id, db, "users.view", *security.ADMIN_FALLBACKS)

    filter_clause = "" if include_inactive else "AND u.isactive = TRUE"
    result = await db.execute(
        text(f"""
            SELECT
                u.userid, u.companyid, u.employeeid, u.isstaged, u.username, u.displayname,
                u.email, u.phone, u.isactive, u.canlogin,
                u.mustchangepassword, u.lastloginatutc,
                u.createdatutc, u.updatedatutc, e.fullname AS employeename
            FROM  sec.users u
            LEFT  JOIN core.employees e ON e.employeeid = u.employeeid AND e.companyid = u.companyid
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
    assignments = await fetch_role_assignments(company_id, uid_list, db)
    cr_map = await _fetch_company_roles_for_users(company_id, uid_list, db)
    drv_map = await _fetch_driver_ids_for_users(company_id, uid_list, db)
    extra_map = await _fetch_extra_perms_for_users(company_id, uid_list, db)
    # Batch role permissions for all distinct company_role_ids in this result set
    role_ids_set = {d["companyroleid"] for d in cr_map.values() if d}
    owner_role_ids = {
        info["companyroleid"] for info in cr_map.values()
        if info
        and info["company_role_code"] == "COMPANY_OWNER"
        and info["company_role_scope"] == "AllCompanyBranches"
        and info["company_role_branch_id"] is None
    }
    role_perms_map = await _fetch_role_perms_for_roles(company_id, role_ids_set, owner_role_ids, db)
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
    await security.ensure_any_permission(company_id, caller_id, db, "users.view", *security.ADMIN_FALLBACKS)
    return await get_user_by_id_internal(target_user_id, company_id, db)


async def get_user_driver_info(
    user_id: int,
    company_id: int,
    caller_user_id: int,
    db: AsyncConnection,
) -> dict:
    """
    Return the permanent Driver and its current assignment for a user (target: user_id).

    Security rules:
      1. DRIVER/Self subjects are denied generic Driver administration.
      2. Caller must have one of: users.view, payrates.view, payrates.edit,
         settings.manage, setup.manage.
      3. If caller has SpecificBranch scope, they may only see targets whose
         current Driver assignment belongs to their allowed branch.
    """
    await require_non_driver_subject(company_id, caller_user_id, db)
    # Rule 1 — permission gate
    await check_any_permission(
        company_id, caller_user_id, None,
        ["users.view", "payrates.view", "payrates.edit", "settings.manage", "setup.manage"],
        db,
    )

    result = await db.execute(
        text("""
            SELECT
                u.userid,
                u.employeeid,
                d.driverid,
                e.fullname       AS driver_name,
                d.drivernumber,
                d.driverstatus,
                a.driverassignmentid,
                a.branchid,
                b.branchname
            FROM   sec.users         u
            LEFT JOIN core.employees e ON e.employeeid = u.employeeid
                                      AND e.companyid  = :cid
            LEFT JOIN core.drivers   d ON d.employeeid = e.employeeid
                                      AND d.companyid  = :cid
            LEFT JOIN core.driverassignments a
                                      ON a.driverassignmentid = core.fn_EffectiveDriverAssignment(
                                          d.companyid, d.driverid
                                      )
            LEFT JOIN core.branches  b ON b.branchid   = a.branchid
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
        # check_branch_access returns (can_see_all, branch_ids)
        can_see_all, caller_branches = await check_branch_access(company_id, caller_user_id, db)
        if not can_see_all and row["branchid"] not in caller_branches:
            raise HTTPException(
                status_code=403,
                detail="Access denied to this driver's branch.",
            )

    has_driver = row["driverid"] is not None
    return {
        "user_id":            user_id,
        "employee_id":        row["employeeid"],
        "driver_id":          row["driverid"],
        "driver_assignment_id": row["driverassignmentid"],
        "driver_name":        row["driver_name"],
        "driver_number":      row["drivernumber"],
        "branch_id":          row["branchid"],
        "branch_name":        row["branchname"],
        "driver_status":      row["driverstatus"],
        "has_driver":         has_driver,
    }


async def list_employee_link_candidates(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    q: str | None = None,
    driver_state: str | None = None,
    employee_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[EmployeeLinkCandidate]:
    """Employees that no User is linked to, with the minimum needed to choose one.

    Gated by the Access permissions that create or link accounts, never by Workforce
    permissions: an Access administrator can link without being able to read the roster.
    The result is searchable (`q`), pageable (`limit`/`offset`, stable name order) and can
    resolve one specific Employee (`employee_id`) wherever it sits in that order.
    """
    await security.ensure_any_permission(
        company_id, user_id, db, "users.create", "users.edit", "roles.edit", *security.ADMIN_FALLBACKS)
    result = await db.execute(
        text("""
            WITH candidate AS (
                SELECT e.employeeid, e.employeekey, e.fullname, e.employmentstatus,
                       COALESCE(a.branchid, e.branchid) AS branchid,
                       CASE
                           WHEN d.driverid IS NULL THEN 'none'
                           WHEN a.driverassignmentid IS NOT NULL THEN 'current'
                           WHEN EXISTS (
                               SELECT 1 FROM core.driverassignments x
                               WHERE x.companyid = e.companyid AND x.driverid = d.driverid
                                 AND x.effectivefromat > NOW()
                                 AND (x.effectivetoat IS NULL OR x.effectivetoat > x.effectivefromat)
                           ) THEN 'pending'
                           ELSE 'none'
                       END AS driverstate
                FROM core.employees e
                LEFT JOIN core.drivers d
                       ON d.companyid = e.companyid AND d.employeeid = e.employeeid
                LEFT JOIN core.driverassignments a
                       ON a.driverassignmentid = core.fn_EffectiveDriverAssignment(e.companyid, d.driverid)
                WHERE e.companyid = :cid
                  AND NOT EXISTS (
                      SELECT 1 FROM sec.users u
                      WHERE u.companyid = e.companyid AND u.employeeid = e.employeeid)
                  AND (CAST(:employee_id AS integer) IS NULL OR e.employeeid = :employee_id)
                  AND (CAST(:q AS text) IS NULL
                       OR LOWER(e.fullname) LIKE :q
                       OR LOWER(COALESCE(e.employeekey, '')) LIKE :q)
            )
            SELECT c.employeeid, c.employeekey, c.fullname, c.employmentstatus, c.branchid,
                   b.branchname, c.driverstate
            FROM candidate c JOIN core.branches b ON b.branchid = c.branchid
            WHERE (CAST(:driver_state AS text) IS NULL OR c.driverstate = :driver_state)
            ORDER BY c.fullname, c.employeeid
            LIMIT :limit OFFSET :offset
        """),
        {"cid": company_id, "q": f"%{q.strip().lower()}%" if q and q.strip() else None,
         "driver_state": driver_state, "employee_id": employee_id,
         "limit": limit, "offset": offset},
    )
    return [
        EmployeeLinkCandidate(
            employee_id=row["employeeid"], employee_key=row["employeekey"],
            full_name=row["fullname"], branch_id=row["branchid"], branch_name=row["branchname"],
            employment_status=row["employmentstatus"], driver_state=row["driverstate"])
        for row in result.mappings().all()
    ]
