"""Admin legacy and company-role assignment mutations."""

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.admin import assignment_validation, audit, security, user_reads
from app.admin.schemas import (
    CompanyRoleAssignmentCreate,
    CompanyRoleAssignmentDetail,
    RoleAssignment,
    RoleAssignmentCreate,
)


# Legacy Role assignments (RoleID).
async def list_user_roles(
    target_user_id: int,
    company_id: int,
    caller_id: int,
    db: AsyncConnection,
) -> list[RoleAssignment]:
    await security.ensure_any_permission(company_id, caller_id, db, "users.view", *security.ADMIN_FALLBACKS)

    # Confirm user exists in this company
    chk = await db.execute(
        text("SELECT 1 FROM sec.users WHERE userid = :uid AND companyid = :cid"),
        {"uid": target_user_id, "cid": company_id},
    )
    if not chk.fetchone():
        raise HTTPException(status_code=404, detail="User not found.")

    assignments_map = await user_reads.fetch_role_assignments(company_id, [target_user_id], db)
    return assignments_map.get(target_user_id, [])


async def assign_role(
    target_user_id: int,
    company_id: int,
    caller_id: int,
    data: RoleAssignmentCreate,
    db: AsyncConnection,
) -> RoleAssignment:
    await security.ensure_any_permission(company_id, caller_id, db, "users.edit", "roles.edit", *security.ADMIN_FALLBACKS)

    # Confirm target user exists
    chk_user = await db.execute(
        text("SELECT IsStaged FROM sec.users WHERE userid = :uid AND companyid = :cid FOR UPDATE"),
        {"uid": target_user_id, "cid": company_id},
    )
    user_row = chk_user.mappings().first()
    if user_row is None:
        raise HTTPException(status_code=404, detail="User not found.")
    if user_row["isstaged"]:
        raise HTTPException(status_code=422, detail="Staged Users must receive a role through the provision operation.")

    # Confirm role exists
    role_result = await db.execute(
        text("SELECT roleid, rolecode, rolename FROM sec.roles WHERE roleid = :rid"),
        {"rid": data.role_id},
    )
    role_row = role_result.mappings().first()
    if role_row is None:
        raise HTTPException(status_code=422, detail="Role not found.")
    if data.scope_type == "Self":
        raise HTTPException(status_code=422, detail="Self scope is available only through the company-role Access flow.")
    if role_row["rolecode"] == "DRIVER":
        raise HTTPException(status_code=422, detail="DRIVER must be assigned through the company-role Access flow.")

    # Validate scope / branch alignment
    if data.scope_type == "AllCompanyBranches":
        if data.branch_id is not None:
            raise HTTPException(
                status_code=422,
                detail="branch_id must be null for AllCompanyBranches scope.",
            )
    elif data.scope_type == "SpecificBranch":
        # Branch-scoped legacy roles require a same-company branch.
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

    else:
        raise HTTPException(status_code=422, detail="Unsupported scope_type.")

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
        async with db.begin_nested():
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

    await audit.write_admin_audit(
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
    assignments_map = await user_reads.fetch_role_assignments(company_id, [target_user_id], db)
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
    await security.ensure_any_permission(company_id, caller_id, db, "users.edit", "roles.edit", *security.ADMIN_FALLBACKS)

    # Lock User before assignment rows so role lifecycle mutations serialize.
    user_result = await db.execute(
        text("SELECT CanLogin FROM sec.Users WHERE UserID = :uid AND CompanyID = :cid FOR UPDATE"),
        {"uid": target_user_id, "cid": company_id},
    )
    locked_user = user_result.mappings().first()
    if locked_user is None:
        raise HTTPException(status_code=404, detail="User not found.")

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

    active_count = await db.execute(
        text("SELECT COUNT(*) FROM sec.UserBranchRoles WHERE UserID = :uid AND CompanyID = :cid AND IsActive"),
        {"uid": target_user_id, "cid": company_id},
    )
    is_final = active_count.scalar_one() == 1
    if is_final and locked_user["canlogin"]:
        raise HTTPException(status_code=422, detail="Disable login before revoking the final active role.")

    await db.execute(
        text("""
            UPDATE sec.userbranchroles
            SET isactive = FALSE, revokedatutc = NOW()
            WHERE userbranchroleid = :aid
        """),
        {"aid": assignment_id},
    )

    if is_final:
        await db.execute(
            text("UPDATE sec.Users SET IsStaged = TRUE, UpdatedAtUtc = NOW() WHERE UserID = :uid AND CompanyID = :cid"),
            {"uid": target_user_id, "cid": company_id},
        )
        await audit.write_user_lifecycle_audit(
            db, company_id=company_id, caller_id=caller_id, action="USER_STAGED",
            user_id=target_user_id, old_value={"is_staged": False},
            new_value={"is_staged": True, "reason": "final_role_revoked"},
        )

    await audit.write_admin_audit(
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


# CompanyRole assignments (CompanyRoleID).
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
    await security.ensure_any_permission(company_id, caller_id, db, "users.edit", "roles.edit", *security.ADMIN_FALLBACKS)

    # Confirm target user exists
    chk_user = await db.execute(
        text("SELECT IsStaged, EmployeeID FROM sec.users WHERE userid = :uid AND companyid = :cid AND isactive = TRUE FOR UPDATE"),
        {"uid": target_user_id, "cid": company_id},
    )
    user_row = chk_user.mappings().first()
    if user_row is None:
        raise HTTPException(status_code=404, detail="User not found or inactive.")
    if user_row["isstaged"]:
        raise HTTPException(status_code=422, detail="Staged Users must receive a role through the provision operation.")

    # Fetch company role — must belong to this company
    cr_result = await db.execute(
        text("""
            SELECT companyroleid, rolecode, rolename, isprotected, isactive, isarchived
            FROM   sec.companyroles
            WHERE  companyroleid = :rid AND companyid = :cid
            FOR SHARE
        """),
        {"rid": data.company_role_id, "cid": company_id},
    )
    cr_row = cr_result.mappings().first()
    if cr_row is None:
        raise HTTPException(status_code=422, detail="Company role not found for this company.")
    if cr_row["isarchived"]:
        raise HTTPException(status_code=422, detail="Cannot assign an archived company role.")
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

    cr_row = await assignment_validation.validate_company_role_assignment(
        company_id, user_row["employeeid"], data, db
    )
    if cr_row["rolecode"] == "DRIVER":
        active_overrides = await db.execute(
            text("""
                SELECT 1 FROM sec.UserPermissionOverrides
                WHERE UserID = :uid AND CompanyID = :cid AND IsActive
                LIMIT 1
            """),
            {"uid": target_user_id, "cid": company_id},
        )
        if active_overrides.first() is not None:
            raise HTTPException(
                status_code=422,
                detail="Clear active permission overrides before assigning DRIVER.",
            )
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
        async with db.begin_nested():
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

    await audit.write_admin_audit(
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
    await security.ensure_any_permission(company_id, caller_id, db, "users.edit", "roles.edit", *security.ADMIN_FALLBACKS)

    user_result = await db.execute(
        text("SELECT CanLogin FROM sec.Users WHERE UserID = :uid AND CompanyID = :cid FOR UPDATE"),
        {"uid": target_user_id, "cid": company_id},
    )
    locked_user = user_result.mappings().first()
    if locked_user is None:
        raise HTTPException(status_code=404, detail="User not found.")

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
        active_count = await db.execute(
            text("SELECT COUNT(*) FROM sec.UserBranchRoles WHERE UserID = :uid AND CompanyID = :cid AND IsActive"),
            {"uid": target_user_id, "cid": company_id},
        )
        is_final = active_count.scalar_one() == 1
        if is_final and locked_user["canlogin"]:
            raise HTTPException(status_code=422, detail="Disable login before revoking the final active role.")
        await db.execute(
            text("""
                UPDATE sec.userbranchroles
                SET isactive = FALSE, revokedatutc = NOW()
                WHERE userbranchroleid = :aid
            """),
            {"aid": assignment_id},
        )
        if is_final:
            await db.execute(
                text("UPDATE sec.Users SET IsStaged = TRUE, UpdatedAtUtc = NOW() WHERE UserID = :uid AND CompanyID = :cid"),
                {"uid": target_user_id, "cid": company_id},
            )
            await audit.write_user_lifecycle_audit(
                db, company_id=company_id, caller_id=caller_id, action="USER_STAGED",
                user_id=target_user_id, old_value={"is_staged": False},
                new_value={"is_staged": True, "reason": "final_role_revoked"},
            )
        await audit.write_admin_audit(
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
