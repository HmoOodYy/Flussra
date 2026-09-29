"""P1b Workforce authority, ownership, and transaction regressions."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def suffix() -> str:
    return uuid4().hex[:10]


async def create_employee(client: httpx.AsyncClient, token: str, branch_id: int, **fields) -> dict:
    body = {"branch_id": branch_id, "full_name": f"P1B {suffix()}", **fields}
    response = await client.post("/workforce/employees", json=body, headers=auth(token))
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_staff_employee_without_user_and_generated_key(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    employee = await create_employee(client, auth_token, hq_branch_id)
    assert employee["driver_state"] == "none"
    assert employee["employee_key"] == f"E{employee['employee_id']:06d}"
    linked = (await direct_db.execute(
        text("SELECT COUNT(*) FROM sec.Users WHERE EmployeeID = :id"), {"id": employee["employee_id"]},
    )).scalar_one()
    assert linked == 0


@pytest.mark.asyncio
async def test_driver_employee_without_user_has_profile_and_audit(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    employee = await create_employee(
        client, auth_token, hq_branch_id,
        driver_profile={"driver_code": f"P1B-{suffix()}"},
    )
    profile = employee["current_or_pending_driver"]
    assert employee["driver_state"] == "current"
    assert profile["effective_from"] is not None
    assert profile["branch_id"] == hq_branch_id
    assert employee["employee_key"] == f"E{employee['employee_id']:06d}"
    assert (await direct_db.execute(
        text("SELECT COUNT(*) FROM sec.Users WHERE EmployeeID = :id"), {"id": employee["employee_id"]},
    )).scalar_one() == 0
    audits = (await direct_db.execute(text("""
        SELECT ActionCode, EntitySchema, EntityName, EntityID, NewValueJson FROM audit.AuditLog
        WHERE EntitySchema = 'core'
          AND (
            (EntityName = 'Employees' AND EntityID = :employee_id
             AND ActionCode = 'EMPLOYEE_CREATED')
            OR
            (EntityName = 'Drivers' AND EntityID = :driver_id
             AND ActionCode = 'DRIVER_PROFILE_CREATED')
          )
    """), {"employee_id": str(employee["employee_id"]), "driver_id": str(profile["driver_id"])})).mappings().all()
    assert {
        (row["entityname"], row["actioncode"], int(row["entityid"]))
        for row in audits
    } == {
        ("Employees", "EMPLOYEE_CREATED", employee["employee_id"]),
        ("Drivers", "DRIVER_PROFILE_CREATED", profile["driver_id"]),
    }
    assert all(row["entityschema"] == "core" for row in audits)
    employee_audit = next(row for row in audits if row["entityname"] == "Employees")
    assert json.loads(employee_audit["newvaluejson"])["employee_key"] == employee["employee_key"]


@pytest.mark.asyncio
async def test_workforce_create_rejects_retired_employee_type_and_profile_branch(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int,
):
    retired_field = await client.post(
        "/workforce/employees",
        json={"branch_id": hq_branch_id, "full_name": f"Retired field {suffix()}", "employee_type": "Driver"},
        headers=auth(auth_token),
    )
    assert retired_field.status_code == 422

    employee = await create_employee(client, auth_token, hq_branch_id)
    branch_override = await client.post(
        f"/workforce/employees/{employee['employee_id']}/driver-profiles",
        json={"driver_code": f"P1B-{suffix()}", "branch_id": hq_branch_id},
        headers=auth(auth_token),
    )
    assert branch_override.status_code == 422


@pytest.mark.asyncio
async def test_employee_create_persists_termination_date(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    termination_date = "2025-04-30"
    employee = await create_employee(
        client, auth_token, hq_branch_id, termination_date=termination_date,
    )
    stored = (await direct_db.execute(text(
        "SELECT TerminationDate FROM core.Employees WHERE EmployeeID = :id"
    ), {"id": employee["employee_id"]})).scalar_one()
    assert employee["termination_date"] == termination_date
    assert stored.isoformat() == termination_date


@pytest.mark.asyncio
async def test_non_driver_employee_can_be_terminated_but_driver_lifecycle_edits_are_rejected(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int,
):
    termination_date = "2025-05-31"
    staff = await create_employee(client, auth_token, hq_branch_id)
    terminated = await client.patch(
        f"/workforce/employees/{staff['employee_id']}",
        json={"employment_status": "Terminated", "termination_date": termination_date},
        headers=auth(auth_token),
    )
    assert terminated.status_code == 200, terminated.text
    assert terminated.json()["employment_status"] == "Terminated"
    assert terminated.json()["termination_date"] == termination_date

    driver = await create_employee(
        client, auth_token, hq_branch_id, driver_profile={"driver_code": f"P1B-{suffix()}"},
    )
    rejected = await client.patch(
        f"/workforce/employees/{driver['employee_id']}",
        json={"employment_status": "Terminated", "termination_date": termination_date},
        headers=auth(auth_token),
    )
    assert rejected.status_code == 422


@pytest.mark.asyncio
async def test_historical_driver_profile_edits_are_rejected_but_pending_edits_work(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    historical = await create_employee(
        client, auth_token, hq_branch_id, hire_date="2000-01-01",
        driver_profile={"driver_code": f"P1B-{suffix()}", "effective_from": "2000-01-01"},
    )
    historical_profile = historical["current_or_pending_driver"]
    await direct_db.execute(text("""
        UPDATE core.Drivers
        SET DriverStatus = 'Transferred', EffectiveTo = DATE '2000-12-31'
        WHERE DriverID = :driver_id
    """), {"driver_id": historical_profile["driver_id"]})

    code_edit = await client.patch(
        f"/core/drivers/{historical_profile['driver_id']}",
        json={"driver_code": f"P1B-{suffix()}"}, headers=auth(auth_token),
    )
    cdl_edit = await client.patch(
        f"/core/drivers/{historical_profile['driver_id']}",
        json={"cdl_number": "HISTORICAL-CDL"}, headers=auth(auth_token),
    )
    assert code_edit.status_code == 422
    assert cdl_edit.status_code == 422

    pending = await create_employee(
        client, auth_token, hq_branch_id,
        driver_profile={"driver_code": f"P1B-{suffix()}", "effective_from": "2099-01-01"},
    )
    pending_profile = pending["current_or_pending_driver"]
    changed = await client.patch(
        f"/core/drivers/{pending_profile['driver_id']}",
        json={"cdl_number": "PENDING-CDL"}, headers=auth(auth_token),
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["cdl_number"] == "PENDING-CDL"


@pytest.mark.asyncio
async def test_legacy_null_effective_window_current_profile_remains_editable(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    employee = await create_employee(
        client, auth_token, hq_branch_id, driver_profile={"driver_code": f"P1B-{suffix()}"},
    )
    driver_id = employee["current_or_pending_driver"]["driver_id"]
    await direct_db.execute(text("""
        UPDATE core.Drivers
        SET EffectiveFrom = NULL, EffectiveTo = NULL, DriverStatus = 'Active'
        WHERE DriverID = :driver_id
    """), {"driver_id": driver_id})

    from app.workforce.effective import resolve_current_driver_profile

    resolved = await resolve_current_driver_profile(1, employee["employee_id"], direct_db)
    assert resolved is not None
    assert resolved["driverid"] == driver_id

    new_code = f"P1B-NULL-{suffix()}"
    changed = await client.patch(
        f"/core/drivers/{driver_id}",
        json={"driver_code": new_code}, headers=auth(auth_token),
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["driver_code"] == new_code


@pytest.mark.asyncio
async def test_duplicate_employee_key_returns_409_without_duplicate(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    key = f"P1B-KEY-{suffix()}"
    first = await create_employee(client, auth_token, hq_branch_id, employee_key=key)
    duplicate = await client.post(
        "/workforce/employees",
        json={"branch_id": hq_branch_id, "employee_key": key, "full_name": f"Duplicate {suffix()}"},
        headers=auth(auth_token),
    )
    assert duplicate.status_code == 409
    assert (await direct_db.execute(
        text("SELECT COUNT(*) FROM core.Employees WHERE CompanyID = 1 AND EmployeeKey = :key"), {"key": key},
    )).scalar_one() == 1
    assert first["employee_key"] == key


@pytest.mark.asyncio
async def test_employee_key_is_immutable(client: httpx.AsyncClient, auth_token: str, hq_branch_id: int):
    employee = await create_employee(client, auth_token, hq_branch_id)
    response = await client.patch(
        f"/workforce/employees/{employee['employee_id']}",
        json={"employee_key": "CHANGED"}, headers=auth(auth_token),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_first_profile_uses_employee_branch_and_effective_date(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int,
):
    employee = await create_employee(client, auth_token, hq_branch_id, hire_date="2020-02-03")
    response = await client.post(
        f"/workforce/employees/{employee['employee_id']}/driver-profiles",
        json={"driver_code": f"P1B-{suffix()}"}, headers=auth(auth_token),
    )
    assert response.status_code == 201, response.text
    profile = response.json()["current_or_pending_driver"]
    assert profile["branch_id"] == hq_branch_id
    assert profile["effective_from"] == "2020-02-03"


@pytest.mark.asyncio
async def test_employee_branch_editable_without_profile(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, paytest_branch_id: int,
):
    employee = await create_employee(client, auth_token, hq_branch_id)
    response = await client.patch(
        f"/workforce/employees/{employee['employee_id']}",
        json={"branch_id": paytest_branch_id}, headers=auth(auth_token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["branch_id"] == paytest_branch_id


@pytest.mark.asyncio
@pytest.mark.parametrize("effective_from", [None, "2099-01-01"], ids=["current", "pending"])
async def test_employee_branch_edit_rejected_with_current_or_pending_profile(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, paytest_branch_id: int,
    effective_from: str | None,
):
    profile = {"driver_code": f"P1B-{suffix()}"}
    if effective_from:
        profile["effective_from"] = effective_from
    employee = await create_employee(client, auth_token, hq_branch_id, driver_profile=profile)
    assert employee["driver_state"] == ("pending" if effective_from else "current")
    response = await client.patch(
        f"/workforce/employees/{employee['employee_id']}",
        json={"branch_id": paytest_branch_id}, headers=auth(auth_token),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_wrong_company_or_missing_branch_rejected(client: httpx.AsyncClient, auth_token: str):
    response = await client.post(
        "/workforce/employees", json={"branch_id": 999999, "full_name": f"P1B {suffix()}"},
        headers=auth(auth_token),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_employee_rejects_branch_owned_by_another_company(
    client: httpx.AsyncClient, auth_token: str, direct_db,
):
    tag = suffix()
    other_company = (await direct_db.execute(text("""
        INSERT INTO core.Companies
            (CompanyCode, CompanyName, LegalName, Status, IsSuspended, TimeZoneName)
        VALUES (:code, :name, :name, 'Active', FALSE, 'America/New_York')
        RETURNING CompanyID
    """), {"code": f"P1B{tag}", "name": f"P1B Other {tag}"})).scalar_one()
    other_branch = (await direct_db.execute(text("""
        INSERT INTO core.Branches (CompanyID, BranchCode, BranchName, Status, IsDefault)
        VALUES (:company_id, :code, :name, 'Active', TRUE)
        RETURNING BranchID
    """), {"company_id": other_company, "code": f"B{tag}", "name": f"Branch {tag}"})).scalar_one()
    response = await client.post(
        "/workforce/employees",
        json={"branch_id": other_branch, "full_name": f"P1B Cross Company {tag}"},
        headers=auth(auth_token),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_driver_state_current_pending_none_and_historical_only(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    current = await create_employee(client, auth_token, hq_branch_id, driver_profile={"driver_code": f"P1B-{suffix()}"})
    pending = await create_employee(client, auth_token, hq_branch_id, driver_profile={"driver_code": f"P1B-{suffix()}", "effective_from": "2099-01-01"})
    none = await create_employee(client, auth_token, hq_branch_id)
    historical = await create_employee(client, auth_token, hq_branch_id, driver_profile={"driver_code": f"P1B-{suffix()}", "effective_from": "2000-01-01"})
    historical_driver = historical["current_or_pending_driver"]["driver_id"]
    await direct_db.execute(text("""
        UPDATE core.Drivers
        SET DriverStatus = 'Transferred', EffectiveTo = DATE '2000-12-31'
        WHERE DriverID = :driver_id
    """), {"driver_id": historical_driver})
    reread = await client.get(f"/workforce/employees/{historical['employee_id']}", headers=auth(auth_token))
    assert reread.status_code == 200, reread.text
    assert current["driver_state"] == "current"
    assert pending["driver_state"] == "pending"
    assert none["driver_state"] == "none"
    assert reread.json()["driver_state"] == "none"


@pytest.mark.asyncio
async def test_employee_view_and_manage_permissions_gate_workforce(
    client: httpx.AsyncClient, branch_user_token: str, hq_branch_id: int,
):
    read = await client.get("/workforce/employees", params={"branch_id": hq_branch_id}, headers=auth(branch_user_token))
    write = await client.post(
        "/workforce/employees", json={"branch_id": hq_branch_id, "full_name": f"Denied {suffix()}"},
        headers=auth(branch_user_token),
    )
    assert read.status_code == 403
    assert write.status_code == 403


@pytest.mark.asyncio
async def test_core_driver_patch_rejects_employee_fields_and_allows_driver_fields(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int,
):
    employee = await create_employee(
        client, auth_token, hq_branch_id,
        driver_profile={"driver_code": f"P1B-{suffix()}"},
    )
    driver_id = employee["current_or_pending_driver"]["driver_id"]
    rejected = await client.patch(
        f"/core/drivers/{driver_id}", json={"full_name": "Not Allowed"}, headers=auth(auth_token),
    )
    assert rejected.status_code == 422
    changed = await client.patch(
        f"/core/drivers/{driver_id}", json={"driver_code": f"P1B-{suffix()}"}, headers=auth(auth_token),
    )
    assert changed.status_code == 200, changed.text


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_employee_and_initial_profile(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db, monkeypatch,
):
    from app.workforce import service as workforce_service

    full_name = f"Rollback {suffix()}"
    original = workforce_service._write_workforce_audit

    async def fail_on_profile(db, **kwargs):
        if kwargs["action_code"] == "DRIVER_PROFILE_CREATED":
            raise RuntimeError("forced Workforce audit failure")
        await original(db, **kwargs)

    monkeypatch.setattr(workforce_service, "_write_workforce_audit", fail_on_profile)
    with pytest.raises(RuntimeError, match="forced Workforce audit failure"):
        await client.post(
            "/workforce/employees",
            json={"branch_id": hq_branch_id, "full_name": full_name,
                  "driver_profile": {"driver_code": f"P1B-{suffix()}"}},
            headers=auth(auth_token),
        )
    assert (await direct_db.execute(
        text("SELECT COUNT(*) FROM core.Employees WHERE FullName = :name"), {"name": full_name},
    )).scalar_one() == 0
    assert (await direct_db.execute(
        text("SELECT COUNT(*) FROM audit.AuditLog WHERE NewValueJson LIKE :name"), {"name": f"%{full_name}%"},
    )).scalar_one() == 0


@pytest.mark.asyncio
async def test_profile_insert_conflict_rolls_back_employee_and_audits(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db, monkeypatch,
):
    from app.workforce import service as workforce_service

    driver_code = f"P1B-DUP-{suffix()}"
    existing = await create_employee(
        client, auth_token, hq_branch_id, driver_profile={"driver_code": driver_code},
    )
    failed_name = f"Profile conflict {suffix()}"
    audit_calls: list[tuple[str, int]] = []
    original = workforce_service._write_workforce_audit

    async def capture_employee_audit(db, **kwargs):
        if kwargs["action_code"] == "EMPLOYEE_CREATED":
            audit_calls.append((kwargs["action_code"], kwargs["entity_id"]))
        await original(db, **kwargs)

    monkeypatch.setattr(workforce_service, "_write_workforce_audit", capture_employee_audit)
    conflict = await client.post(
        "/workforce/employees",
        json={"branch_id": hq_branch_id, "full_name": failed_name,
              "driver_profile": {"driver_code": driver_code}},
        headers=auth(auth_token),
    )
    assert conflict.status_code == 409, conflict.text
    assert (await direct_db.execute(text(
        "SELECT COUNT(*) FROM core.Employees WHERE FullName = :name"
    ), {"name": failed_name})).scalar_one() == 0
    assert (await direct_db.execute(text(
        "SELECT COUNT(*) FROM core.Drivers WHERE CompanyID = 1 AND DriverCode = :code"
    ), {"code": driver_code})).scalar_one() == 1

    failed_employee_ids = [entity_id for action, entity_id in audit_calls if action == "EMPLOYEE_CREATED"]
    assert len(failed_employee_ids) == 1
    failed_employee_id = str(failed_employee_ids[0])
    leftover_audits = (await direct_db.execute(text("""
        SELECT COUNT(*) FROM audit.AuditLog
        WHERE EntitySchema = 'core'
          AND (
            (EntityName = 'Employees' AND EntityID = :employee_id)
            OR
            (EntityName = 'Drivers' AND NewValueJson::jsonb ->> 'employee_id' = :employee_id)
          )
    """), {"employee_id": failed_employee_id})).scalar_one()
    assert leftover_audits == 0
    assert existing["current_or_pending_driver"]["driver_code"] == driver_code


@pytest.mark.asyncio
async def test_hire_date_guard_uses_nonvoid_daily_draft_lines_across_driver_history(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int,
    created_period_id: int, direct_db,
):
    employee = await create_employee(
        client, auth_token, hq_branch_id, hire_date="2000-01-01",
        driver_profile={"driver_code": f"P1B-{suffix()}", "effective_from": "2000-01-01"},
    )
    source_id = employee["current_or_pending_driver"]["driver_id"]
    await direct_db.execute(text("""
        UPDATE core.Drivers
        SET DriverStatus = 'Transferred', EffectiveTo = DATE '2019-12-31'
        WHERE DriverID = :driver_id
    """), {"driver_id": source_id})
    target_id = (await direct_db.execute(text("""
        INSERT INTO core.Drivers
            (CompanyID, BranchID, EmployeeID, DriverCode, DriverStatus, EffectiveFrom)
        VALUES (1, :branch_id, :employee_id, :code, 'Active', DATE '2020-01-01')
        RETURNING DriverID
    """), {"branch_id": hq_branch_id, "employee_id": employee["employee_id"], "code": f"P1B-{suffix()}"})).scalar_one()
    await direct_db.execute(text("""
        INSERT INTO payroll.PayrollDraftLines
            (CompanyID, BranchID, PayrollPeriodID, DriverID, WorkDate, LineType,
             Quantity, SourceType, Status, LineScope)
        VALUES (1, :branch_id, :period_id, :driver_id, DATE '2018-06-01',
                :line_type, 1, 'Manual', 'Active', 'Daily')
    """), {"branch_id": hq_branch_id, "period_id": created_period_id,
           "driver_id": source_id, "line_type": f"P1B-{suffix()}"})

    response = await client.patch(
        f"/workforce/employees/{employee['employee_id']}",
        json={"hire_date": "2018-06-02"}, headers=auth(auth_token),
    )
    assert response.status_code == 422
    assert target_id != source_id


@pytest.mark.asyncio
async def test_hire_date_guard_ignores_void_draft_line(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int,
    created_period_id: int, direct_db,
):
    employee = await create_employee(
        client, auth_token, hq_branch_id, hire_date="2000-01-01",
        driver_profile={"driver_code": f"P1B-{suffix()}", "effective_from": "2000-01-01"},
    )
    driver_id = employee["current_or_pending_driver"]["driver_id"]
    await direct_db.execute(text("""
        INSERT INTO payroll.PayrollDraftLines
            (CompanyID, BranchID, PayrollPeriodID, DriverID, WorkDate, LineType,
             Quantity, SourceType, Status, LineScope)
        VALUES (1, :branch_id, :period_id, :driver_id, DATE '2018-06-01',
                :line_type, 1, 'Manual', 'Void', 'Daily')
    """), {"branch_id": hq_branch_id, "period_id": created_period_id,
           "driver_id": driver_id, "line_type": f"P1B-{suffix()}"})
    response = await client.patch(
        f"/workforce/employees/{employee['employee_id']}",
        json={"hire_date": "2018-06-02"}, headers=auth(auth_token),
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_hire_date_guard_protects_final_line_dates(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int,
    created_period_id: int, direct_db,
):
    employee = await create_employee(
        client, auth_token, hq_branch_id, hire_date="2000-01-01",
        driver_profile={"driver_code": f"P1B-{suffix()}", "effective_from": "2000-01-01"},
    )
    driver_id = employee["current_or_pending_driver"]["driver_id"]
    await direct_db.execute(text("SELECT set_config('app.allow_payroll_final_line_insert', 'true', false)"))
    try:
        await direct_db.execute(text("""
            INSERT INTO payroll.PayrollFinalLines
                (CompanyID, BranchID, PayrollPeriodID, DriverID, WorkDate, LineType,
                 Quantity, FinalAmount, SourceType, LineScope)
            VALUES (1, :branch_id, :period_id, :driver_id, DATE '2018-06-01',
                    :line_type, 1, 1, 'Manual', 'Daily')
        """), {"branch_id": hq_branch_id, "period_id": created_period_id,
               "driver_id": driver_id, "line_type": f"P1B-{suffix()}"})
    finally:
        await direct_db.execute(text("SELECT set_config('app.allow_payroll_final_line_insert', '', false)"))
    response = await client.patch(
        f"/workforce/employees/{employee['employee_id']}",
        json={"hire_date": "2018-06-02"}, headers=auth(auth_token),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_driver_role_workforce_failure_rolls_back_access_assignment(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    tag = suffix()
    caller = await client.post(
        "/admin/users",
        json={"username": f"p1b_access_{tag}", "display_name": f"P1B Access {tag}",
              "password": "TestPass123!", "company_role_id": None,
              "scope_type": "AllCompanyBranches", "branch_id": None},
        headers=auth(auth_token),
    )
    target = await client.post(
        "/admin/users",
        json={"username": f"p1b_target_{tag}", "display_name": f"P1B Target {tag}",
              "password": "TestPass123!", "company_role_id": None,
              "scope_type": "AllCompanyBranches", "branch_id": None},
        headers=auth(auth_token),
    )
    assert caller.status_code in {200, 201}, caller.text
    assert target.status_code in {200, 201}, target.text
    caller_id, target_id = caller.json()["user_id"], target.json()["user_id"]
    role_code = f"P1B_ACCESS_{tag.upper()}"
    role_id = (await direct_db.execute(text("""
        INSERT INTO sec.Roles (RoleCode, RoleName, RoleLevel, IsSystemRole)
        VALUES (:code, :name, 40, FALSE) RETURNING RoleID
    """), {"code": role_code, "name": role_code})).scalar_one()
    await direct_db.execute(text("""
        INSERT INTO sec.RolePermissions (RoleID, PermissionID)
        SELECT :role_id, PermissionID FROM sec.Permissions WHERE PermissionCode = 'users.edit'
    """), {"role_id": role_id})
    await direct_db.execute(text("""
        INSERT INTO sec.UserBranchRoles
            (UserID, CompanyID, BranchID, RoleID, CompanyRoleID, ScopeType, IsActive)
        VALUES (:user_id, 1, NULL, :role_id, NULL, 'AllCompanyBranches', TRUE)
    """), {"user_id": caller_id, "role_id": role_id})
    login = await client.post("/auth/login", json={
        "username": f"p1b_access_{tag}", "password": "TestPass123!", "company_code": "DEMO",
    })
    assert login.status_code == 200, login.text
    caller_token = login.json()["access_token"]
    roles = await client.get("/admin/company-roles", headers=auth(auth_token))
    driver_role = next(role for role in roles.json() if role["role_code"] == "DRIVER")
    before = (await direct_db.execute(text("""
        SELECT COUNT(*) FROM sec.UserBranchRoles WHERE UserID = :user_id AND IsActive
    """), {"user_id": target_id})).scalar_one()
    response = await client.post(
        f"/admin/users/{target_id}/company-role-assignments",
        json={"company_role_id": driver_role["company_role_id"],
              "scope_type": "SpecificBranch", "branch_id": hq_branch_id},
        headers=auth(caller_token),
    )
    assert response.status_code == 403
    after = (await direct_db.execute(text("""
        SELECT COUNT(*) FROM sec.UserBranchRoles WHERE UserID = :user_id AND IsActive
    """), {"user_id": target_id})).scalar_one()
    linked_employee = (await direct_db.execute(
        text("SELECT EmployeeID FROM sec.Users WHERE UserID = :user_id"), {"user_id": target_id},
    )).scalar_one()
    assert after == before
    assert linked_employee is None


@pytest.mark.asyncio
async def test_admin_driver_hook_uses_workforce_key_and_links_user(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    tag = suffix()
    created = await client.post(
        "/admin/users",
        json={"username": f"p1b_{tag}", "display_name": f"P1B Hook {tag}", "password": "TestPass123!",
               "company_role_id": None, "scope_type": "AllCompanyBranches", "branch_id": None},
        headers=auth(auth_token),
    )
    assert created.status_code in {200, 201}, created.text
    user_id = created.json()["user_id"]
    roles = await client.get("/admin/company-roles", headers=auth(auth_token))
    driver_role = next(role for role in roles.json() if role["role_code"] == "DRIVER")
    assigned = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json={"company_role_id": driver_role["company_role_id"], "scope_type": "SpecificBranch", "branch_id": hq_branch_id},
        headers=auth(auth_token),
    )
    assert assigned.status_code in {200, 201}, assigned.text
    row = (await direct_db.execute(text("""
        SELECT u.EmployeeID, e.EmployeeKey, d.DriverID
        FROM sec.Users u JOIN core.Employees e ON e.EmployeeID = u.EmployeeID
        JOIN core.Drivers d ON d.EmployeeID = e.EmployeeID
        WHERE u.UserID = :user_id
    """), {"user_id": user_id})).mappings().one()
    assert row["employeekey"] == f"E{row['employeeid']:06d}"
    assert not row["employeekey"].startswith("EMP-")
