"""Admin company-role lifecycle and legacy role catalogue operations."""
import random
import string

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.admin import audit, security
from app.admin.schemas import (
    CompanyRole,
    CompanyRoleCreate,
    CompanyRoleUpdate,
    CompanyRoleUser,
    Role,
)


# Legacy System Role catalogue.
async def list_roles(
    company_id: int,
    caller_id: int,
    db: AsyncConnection,
) -> list[Role]:
    await security.ensure_any_permission(company_id, caller_id, db, "roles.view", *security.ADMIN_FALLBACKS)

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


# Company Role administration.
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
    await security.ensure_any_permission(company_id, caller_id, db, "roles.view", *security.ADMIN_FALLBACKS)

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
    await security.ensure_any_permission(company_id, caller_id, db, "roles.view", *security.ADMIN_FALLBACKS)
    return await _get_company_role_internal(company_role_id, company_id, db)


async def create_company_role(
    company_id: int,
    caller_id: int,
    data: CompanyRoleCreate,
    db: AsyncConnection,
) -> CompanyRole:
    await security.ensure_any_permission(company_id, caller_id, db, "roles.create", *security.ADMIN_FALLBACKS)

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

    await audit.write_admin_audit(
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
    await security.ensure_any_permission(company_id, caller_id, db, "roles.edit", *security.ADMIN_FALLBACKS)

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
        await audit.write_admin_audit(
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
    await security.ensure_any_permission(company_id, caller_id, db, "roles.delete", *security.ADMIN_FALLBACKS)

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

    await audit.write_admin_audit(
        db,
        company_id=company_id,
        user_id=caller_id,
        action_code="COMPANY_ROLE_ARCHIVED",
        entity_name="CompanyRoles",
        entity_id=str(company_role_id),
        old_value={"role_code": row["rolecode"], "role_name": row["rolename"]},
        new_value={"isarchived": True},
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
    await security.ensure_any_permission(company_id, caller_id, db, "roles.view", "users.view", *security.ADMIN_FALLBACKS)

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
