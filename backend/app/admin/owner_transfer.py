"""Company ownership transfer mutation and disabled-owner authority checks."""

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.admin import audit, security
from app.admin.schemas import (
    OwnerTransferRequest,
    OwnerTransferResult,
)


async def _ensure_disabled_owner_transfer_authority(
    company_id: int, user_id: int, db: AsyncConnection,
) -> None:
    """Keep owner scope and admin permission checks for a disabled current owner."""
    result = await db.execute(
        text("""
            SELECT 1
            FROM sec.Users u
            JOIN core.Companies c ON c.CompanyID = u.CompanyID
            JOIN sec.UserBranchRoles ubr ON ubr.UserID = u.UserID AND ubr.CompanyID = u.CompanyID
            JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = ubr.CompanyRoleID
            WHERE u.UserID=:uid AND u.CompanyID=:cid AND u.IsActive AND NOT u.IsStaged
              AND c.Status='Active' AND NOT c.IsSuspended
              AND ubr.IsActive AND ubr.ScopeType='AllCompanyBranches'
              AND ubr.BranchID IS NULL
              AND cr.RoleCode='COMPANY_OWNER'
        """),
        {"uid": user_id, "cid": company_id},
    )
    if result.first() is None:
        raise HTTPException(status_code=403, detail="Only an active Company Owner can transfer ownership.")
    # The explicit active company-wide Owner assignment is the service authority
    # for this exceptional disabled-owner transfer path; generic permission
    # evaluation intentionally rejects login-disabled principals.
    return


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

    caller_user_result = await db.execute(
        text("SELECT CanLogin, IsActive, IsStaged FROM sec.Users WHERE UserID = :caller AND CompanyID = :cid FOR UPDATE"),
        {"caller": caller_id, "cid": company_id},
    )
    caller_user = caller_user_result.mappings().first()
    if caller_user is None or not caller_user["isactive"]:
        raise HTTPException(status_code=404, detail="Company Owner account not found.")
    if caller_user["isstaged"]:
        raise HTTPException(status_code=403, detail="A staged account cannot transfer company ownership.")
    if caller_user["canlogin"]:
        await security.ensure_scope(company_id, caller_id, db)
    else:
        await _ensure_disabled_owner_transfer_authority(company_id, caller_id, db)

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
              AND  ubr.scopetype = 'AllCompanyBranches'
              AND  ubr.branchid IS NULL
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
                SELECT companyroleid, rolecode, rolename, isactive, isarchived
                FROM   sec.companyroles
                WHERE  companyroleid = :rid AND companyid = :cid
                FOR SHARE
            """),
            {"rid": data.replacement_company_role_id, "cid": company_id},
        )
        repl_row = repl_r.mappings().first()
        if repl_row is None:
            raise HTTPException(
                status_code=422,
                detail="Replacement company role not found for this company.",
            )
        if repl_row["isarchived"]:
            raise HTTPException(status_code=422, detail="Replacement company role is archived.")
        if not repl_row["isactive"]:
            raise HTTPException(status_code=422, detail="Replacement company role is inactive.")
        if repl_row["rolecode"] == "COMPANY_OWNER":
            raise HTTPException(
                status_code=422,
                detail="Cannot use Company Owner as the replacement role.",
            )
        if repl_row["rolecode"] == "DRIVER":
            raise HTTPException(status_code=422, detail="DRIVER cannot be used as an ownership replacement role.")
        replacement_role_name = repl_row["rolename"]

    active_caller_roles = await db.execute(
        text("SELECT COUNT(*) FROM sec.UserBranchRoles WHERE UserID = :caller AND CompanyID = :cid AND IsActive"),
        {"caller": caller_id, "cid": company_id},
    )
    caller_has_final_role = active_caller_roles.scalar_one() == 1
    if data.replacement_company_role_id is None and caller_has_final_role and caller_user["canlogin"]:
        raise HTTPException(status_code=422, detail="Assign a replacement role before removing the final active role.")

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

    if data.replacement_company_role_id is None and caller_has_final_role:
        await db.execute(
            text("UPDATE sec.Users SET IsStaged = TRUE, UpdatedAtUtc = NOW() WHERE UserID = :uid AND CompanyID = :cid"),
            {"uid": caller_id, "cid": company_id},
        )
        await audit.write_user_lifecycle_audit(
            db, company_id=company_id, caller_id=caller_id, action="USER_STAGED",
            user_id=caller_id, old_value={"is_staged": False},
            new_value={"is_staged": True, "reason": "final_role_revoked"},
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
                    (userid, companyid, branchid, companyroleId, scopetype,
                     isactive, grantedbyuserid, notes)
                VALUES
                    (:uid, :cid, NULL, :crid, 'AllCompanyBranches',
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
                (userid, companyid, branchid, companyroleId, scopetype,
                 isactive, grantedbyuserid, notes)
            VALUES
                (:uid, :cid, NULL, :crid, 'AllCompanyBranches',
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
    await audit.write_admin_audit(
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
