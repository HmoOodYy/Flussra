"""Admin user account lifecycle and employee-link operations."""

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.admin import access_safety, assignment_validation, audit, security, user_reads
from app.admin.schemas import (
    CompanyRoleAssignmentCreate,
    UserAdmin,
    UserCreate,
    UserPasswordReset,
    UserProvision,
    UserUpdate,
)
from app.auth.security import hash_password


async def _lock_employee_link_target(
    company_id: int, employee_id: int, db: AsyncConnection,
) -> None:
    employee = await db.execute(
        text("SELECT EmployeeID FROM core.Employees WHERE EmployeeID = :eid AND CompanyID = :cid FOR UPDATE"),
        {"eid": employee_id, "cid": company_id},
    )
    if employee.first() is None:
        raise HTTPException(status_code=422, detail="Employee not found for this company.")
    await access_safety.ensure_employee_not_terminated(company_id, employee_id, db)
    linked = await db.execute(
        text("SELECT UserID FROM sec.Users WHERE EmployeeID = :eid AND CompanyID = :cid LIMIT 1"),
        {"eid": employee_id, "cid": company_id},
    )
    if linked.first() is not None:
        raise HTTPException(status_code=409, detail="Employee is already linked to another User.")


async def _insert_company_role_assignment(
    user_id: int,
    company_id: int,
    caller_id: int,
    data: CompanyRoleAssignmentCreate,
    db: AsyncConnection,
) -> int:
    result = await db.execute(
        text("""
            INSERT INTO sec.UserBranchRoles
                (UserID, CompanyID, BranchID, CompanyRoleID, ScopeType,
                 IsActive, GrantedByUserID, Notes)
            VALUES (:user_id, :company_id, :branch_id, :role_id, :scope,
                    TRUE, :caller_id, :notes)
            RETURNING UserBranchRoleID
        """),
        {
            "user_id": user_id, "company_id": company_id,
            "branch_id": data.branch_id, "role_id": data.company_role_id,
            "scope": data.scope_type, "caller_id": caller_id, "notes": data.notes,
        },
    )
    return result.scalar_one()


async def create_user(
    company_id: int,
    caller_id: int,
    data: UserCreate,
    db: AsyncConnection,
) -> UserAdmin:
    await security.ensure_any_permission(company_id, caller_id, db, "users.create", *security.ADMIN_FALLBACKS)

    if data.is_staged:
        if data.can_login or data.role_assignment is not None:
            raise HTTPException(status_code=422, detail="Staged creation requires CanLogin=false and no role assignment.")
    else:
        await security.ensure_any_permission(company_id, caller_id, db, "users.edit", "roles.edit", *security.ADMIN_FALLBACKS)
        if data.role_assignment is None:
            raise HTTPException(status_code=422, detail="Provisioned User creation requires a CompanyRole/Scope assignment.")

    if data.employee_id is not None:
        await _lock_employee_link_target(company_id, data.employee_id, db)
    if data.role_assignment is not None:
        await assignment_validation.validate_company_role_assignment(company_id, data.employee_id, data.role_assignment, db)
        await access_safety.ensure_can_assign_role(company_id, caller_id, data.role_assignment.company_role_id, db)

    try:
        async with db.begin_nested():
            result = await db.execute(
                text("""
                    INSERT INTO sec.Users
                        (CompanyID, EmployeeID, Username, DisplayName, Email, Phone,
                         PasswordHash, IsActive, CanLogin, IsStaged, MustChangePassword)
                    VALUES
                        (:company_id, :employee_id, :username, :display_name, :email, :phone,
                         :password_hash, :is_active, :can_login, :is_staged, :must_change)
                    RETURNING UserID
                """),
                {
                    "company_id": company_id, "employee_id": data.employee_id,
                    "username": data.username, "display_name": data.display_name,
                    "email": data.email, "phone": data.phone,
                    "password_hash": hash_password(data.password),
                    "is_active": data.is_active, "can_login": data.can_login,
                    "is_staged": data.is_staged, "must_change": data.must_change_password,
                },
            )
            new_user_id = result.scalar_one()
            if data.role_assignment is not None:
                await _insert_company_role_assignment(new_user_id, company_id, caller_id, data.role_assignment, db)
            await audit.write_user_lifecycle_audit(
                db, company_id=company_id, caller_id=caller_id, action="USER_CREATED",
                user_id=new_user_id,
                new_value={"username": data.username, "display_name": data.display_name,
                           "is_staged": data.is_staged, "can_login": data.can_login,
                           "employee_id": data.employee_id,
                           "company_role_id": data.role_assignment.company_role_id if data.role_assignment else None},
            )
    except SAIntegrityError as exc:
        msg = str(exc.orig).lower() if exc.orig else str(exc).lower()
        if "ux_users_company_username" in msg:
            raise HTTPException(status_code=422, detail=f"Username '{data.username}' already exists for this company.") from exc
        if "employee" in msg and "users" in msg:
            raise HTTPException(status_code=409, detail="Employee is already linked to another User or does not belong to this company.") from exc
        raise HTTPException(status_code=422, detail="User could not be created because an integrity rule was violated.") from exc

    return await user_reads.get_user_by_id_internal(new_user_id, company_id, db)


async def link_user_employee(
    target_user_id: int, company_id: int, caller_id: int, employee_id: int, db: AsyncConnection,
) -> UserAdmin:
    await security.ensure_any_permission(company_id, caller_id, db, "users.edit", *security.ADMIN_FALLBACKS)
    user_result = await db.execute(
        text("SELECT EmployeeID FROM sec.Users WHERE UserID = :uid AND CompanyID = :cid FOR UPDATE"),
        {"uid": target_user_id, "cid": company_id},
    )
    user = user_result.mappings().first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    if user["employeeid"] == employee_id:
        return await user_reads.get_user_by_id_internal(target_user_id, company_id, db)
    if user["employeeid"] is not None:
        raise HTTPException(status_code=409, detail="User is already linked to another Employee; unlink it first.")
    try:
        async with db.begin_nested():
            await _lock_employee_link_target(company_id, employee_id, db)
            await db.execute(
                text("UPDATE sec.Users SET EmployeeID = :eid, UpdatedAtUtc = NOW() WHERE UserID = :uid AND CompanyID = :cid"),
                {"eid": employee_id, "uid": target_user_id, "cid": company_id},
            )
            await audit.write_user_lifecycle_audit(
                db, company_id=company_id, caller_id=caller_id, action="USER_EMPLOYEE_LINKED",
                user_id=target_user_id, new_value={"employee_id": employee_id},
            )
    except SAIntegrityError as exc:
        raise HTTPException(status_code=409, detail="Employee is already linked to another User.") from exc
    return await user_reads.get_user_by_id_internal(target_user_id, company_id, db)


async def unlink_user_employee(
    target_user_id: int, company_id: int, caller_id: int, db: AsyncConnection,
) -> UserAdmin:
    await security.ensure_any_permission(company_id, caller_id, db, "users.edit", *security.ADMIN_FALLBACKS)
    result = await db.execute(
        text("SELECT EmployeeID FROM sec.Users WHERE UserID = :uid AND CompanyID = :cid FOR UPDATE"),
        {"uid": target_user_id, "cid": company_id},
    )
    user = result.mappings().first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    employee_id = user["employeeid"]
    if employee_id is None:
        return await user_reads.get_user_by_id_internal(target_user_id, company_id, db)
    employee = await db.execute(
        text("SELECT EmployeeID, EmploymentStatus FROM core.Employees WHERE EmployeeID = :eid AND CompanyID = :cid FOR UPDATE"),
        {"eid": employee_id, "cid": company_id},
    )
    employee_row = employee.mappings().first()
    if employee_row is None:
        raise HTTPException(status_code=409, detail="Linked Employee no longer belongs to this company.")
    if employee_row["employmentstatus"] == "Terminated":
        raise access_safety.access_error(
            422,
            access_safety.EMPLOYEE_TERMINATED,
            "This person has been terminated. Their account stays linked so it can't be switched back on.",
        )
    driver_role = await db.execute(
        text("""
            SELECT 1 FROM sec.UserBranchRoles ubr
            JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = ubr.CompanyRoleID
            WHERE ubr.UserID = :uid AND ubr.CompanyID = :cid AND ubr.IsActive
              AND cr.RoleCode = 'DRIVER'
            LIMIT 1
        """),
        {"uid": target_user_id, "cid": company_id},
    )
    if driver_role.first() is not None:
        raise HTTPException(status_code=422, detail="Remove the active DRIVER role before unlinking the Employee.")
    await db.execute(
        text("UPDATE sec.Users SET EmployeeID = NULL, UpdatedAtUtc = NOW() WHERE UserID = :uid AND CompanyID = :cid"),
        {"uid": target_user_id, "cid": company_id},
    )
    await audit.write_user_lifecycle_audit(
        db, company_id=company_id, caller_id=caller_id, action="USER_EMPLOYEE_UNLINKED",
        user_id=target_user_id, old_value={"employee_id": employee_id},
    )
    return await user_reads.get_user_by_id_internal(target_user_id, company_id, db)


async def provision_user(
    target_user_id: int, company_id: int, caller_id: int, data: UserProvision, db: AsyncConnection,
) -> UserAdmin:
    await security.ensure_any_permission(company_id, caller_id, db, "users.edit", *security.ADMIN_FALLBACKS)
    result = await db.execute(
        text("SELECT EmployeeID, IsStaged, CanLogin FROM sec.Users WHERE UserID = :uid AND CompanyID = :cid FOR UPDATE"),
        {"uid": target_user_id, "cid": company_id},
    )
    user = result.mappings().first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    if not user["isstaged"]:
        raise HTTPException(status_code=422, detail="Only explicitly staged Users can be provisioned.")
    count = await db.execute(
        text("SELECT COUNT(*) FROM sec.UserBranchRoles WHERE UserID = :uid AND CompanyID = :cid AND IsActive"),
        {"uid": target_user_id, "cid": company_id},
    )
    if count.scalar_one():
        raise HTTPException(status_code=409, detail="Staged User already has an active role assignment.")
    await assignment_validation.validate_company_role_assignment(company_id, user["employeeid"], data.role_assignment, db)
    await access_safety.ensure_can_assign_role(company_id, caller_id, data.role_assignment.company_role_id, db)
    try:
        async with db.begin_nested():
            assignment_id = await _insert_company_role_assignment(target_user_id, company_id, caller_id, data.role_assignment, db)
            await db.execute(
                text("UPDATE sec.Users SET IsStaged = FALSE, CanLogin = :can_login, UpdatedAtUtc = NOW() WHERE UserID = :uid AND CompanyID = :cid"),
                {"can_login": data.can_login, "uid": target_user_id, "cid": company_id},
            )
            await audit.write_user_lifecycle_audit(
                db, company_id=company_id, caller_id=caller_id, action="USER_PROVISIONED",
                user_id=target_user_id,
                old_value={"is_staged": True, "can_login": user["canlogin"]},
                new_value={"is_staged": False, "can_login": data.can_login,
                           "company_role_id": data.role_assignment.company_role_id,
                           "assignment_id": assignment_id},
            )
    except SAIntegrityError as exc:
        raise HTTPException(status_code=409, detail="The role assignment conflicts with an existing assignment.") from exc
    return await user_reads.get_user_by_id_internal(target_user_id, company_id, db)


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
        await security.ensure_any_permission(company_id, caller_id, db, "users.edit", *security.ADMIN_FALLBACKS)
    elif _has_active_field:
        # Pure activate/deactivate — users.deactivate is sufficient
        await security.ensure_any_permission(
            company_id, caller_id, db, "users.deactivate", "users.edit", *security.ADMIN_FALLBACKS
        )
    else:
        # Empty payload — enforce scope but no write permission needed
        await security.ensure_scope(company_id, caller_id, db)

    # Fetch current values with row lock for snapshot + validation
    result = await db.execute(
        text("""
            SELECT userid, employeeid, displayname, email, phone,
                   isactive, canlogin, isstaged, mustchangepassword
            FROM  sec.users
            WHERE userid = :uid AND companyid = :cid
            FOR UPDATE
        """),
        {"uid": target_user_id, "cid": company_id},
    )
    current = result.mappings().first()
    if current is None:
        raise HTTPException(status_code=404, detail="User not found.")

    if data.can_login is True:
        if current["isstaged"]:
            raise HTTPException(status_code=422, detail="A staged User must be provisioned before login can be enabled.")
        await access_safety.ensure_employee_not_terminated(company_id, current["employeeid"], db)
        role_count = await db.execute(
            text("SELECT COUNT(*) FROM sec.UserBranchRoles WHERE UserID = :uid AND CompanyID = :cid AND IsActive"),
            {"uid": target_user_id, "cid": company_id},
        )
        if not role_count.scalar_one():
            raise HTTPException(status_code=422, detail="Login cannot be enabled without an active role assignment.")

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
        await audit.write_admin_audit(
            db,
            company_id=company_id,
            user_id=caller_id,
            action_code="USER_UPDATED",
            entity_name="Users",
            entity_id=str(target_user_id),
            old_value={col: current[col] for col in updates},
            new_value=updates,
        )

    return await user_reads.get_user_by_id_internal(target_user_id, company_id, db)


async def reset_password(
    target_user_id: int,
    company_id: int,
    caller_id: int,
    data: UserPasswordReset,
    db: AsyncConnection,
) -> UserAdmin:
    await security.ensure_any_permission(company_id, caller_id, db, "users.edit", *security.ADMIN_FALLBACKS)

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

    await audit.write_admin_audit(
        db,
        company_id=company_id,
        user_id=caller_id,
        action_code="USER_PASSWORD_RESET",
        entity_name="Users",
        entity_id=str(target_user_id),
        new_value={"must_change_password": data.must_change_password},
    )

    return await user_reads.get_user_by_id_internal(target_user_id, company_id, db)
