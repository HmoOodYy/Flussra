"""CompanyRole assignment mutations for the target Access authority."""

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.admin import access_safety, assignment_validation, audit, security
from app.admin.schemas import CompanyRoleAssignmentCreate, CompanyRoleAssignmentDetail

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
    - Inserts a new userbranchroles row with CompanyRoleID set.
    """
    await security.ensure_any_permission(company_id, caller_id, db, "users.edit", "roles.edit")
    access_safety.forbid_self_access_change(caller_id, target_user_id)

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
    await access_safety.ensure_can_assign_role(company_id, caller_id, data.company_role_id, db)
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

    # Insert the CompanyRole assignment.
    try:
        async with db.begin_nested():
            ins = await db.execute(
            text("""
                INSERT INTO sec.userbranchroles
                    (userid, companyid, branchid, companyroleId, scopetype,
                     isactive, grantedbyuserid, notes)
                VALUES
                    (:uid, :cid, :bid, :crid, :scope,
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
    await security.ensure_any_permission(company_id, caller_id, db, "users.edit", "roles.edit")
    access_safety.forbid_self_access_change(caller_id, target_user_id)

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
