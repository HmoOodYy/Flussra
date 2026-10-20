"""P1b Workforce authority, ownership, and transaction regressions."""

from __future__ import annotations

import json
from datetime import date
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from tests.builders.final_evidence import discard_chain, insert_final_line, seed_snapshot_component
from tests.builders.workforce_db import end_driver_assignment, insert_driver_assignment


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def suffix() -> str:
    return uuid4().hex[:10]


async def create_provisioned_test_user(
    client: httpx.AsyncClient, token: str, username: str, display_name: str,
) -> httpx.Response:
    roles = await client.get("/admin/company-roles", headers=auth(token))
    assert roles.status_code == 200, roles.text
    role = next(item for item in roles.json() if item["role_code"] == "PAYROLL_VIEWER_CO")
    return await client.post(
        "/admin/users",
        json={
            "username": username,
            "display_name": display_name,
            "password": "TestPass123!",
            "can_login": True,
            "role_assignment": {
                "company_role_id": role["company_role_id"],
                "scope_type": "AllCompanyBranches",
            },
        },
        headers=auth(token),
    )


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
async def test_driver_employee_without_user_has_driver_assignment_and_audit(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    employee = await create_employee(
        client, auth_token, hq_branch_id,
        driver={"driver_number": f"P1B-{suffix()}"},
    )
    driver = employee["driver"]
    assignment = employee["current_or_pending_assignment"]
    assert employee["driver_state"] == "current"
    assert driver["driver_status"] == "Active"
    assert assignment["driver_id"] == driver["driver_id"]
    assert assignment["effective_from_at"] is not None
    assert assignment["effective_to_at"] is None and assignment["end_reason"] is None
    assert assignment["branch_id"] == hq_branch_id
    assert [a["driver_assignment_id"] for a in employee["driver_assignments"]] == [
        assignment["driver_assignment_id"]
    ]
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
             AND ActionCode = 'DRIVER_CREATED')
          )
    """), {"employee_id": str(employee["employee_id"]), "driver_id": str(driver["driver_id"])})).mappings().all()
    assert {
        (row["entityname"], row["actioncode"], int(row["entityid"]))
        for row in audits
    } == {
        ("Employees", "EMPLOYEE_CREATED", employee["employee_id"]),
        ("Drivers", "DRIVER_CREATED", driver["driver_id"]),
    }
    assert all(row["entityschema"] == "core" for row in audits)
    employee_audit = next(row for row in audits if row["entityname"] == "Employees")
    assert json.loads(employee_audit["newvaluejson"])["employee_key"] == employee["employee_key"]


@pytest.mark.asyncio
async def test_workforce_create_rejects_retired_employee_type_and_driver_branch(
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
        f"/workforce/employees/{employee['employee_id']}/driver",
        json={"driver_number": f"P1B-{suffix()}", "branch_id": hq_branch_id},
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
        client, auth_token, hq_branch_id, driver={"driver_number": f"P1B-{suffix()}"},
    )
    rejected = await client.patch(
        f"/workforce/employees/{driver['employee_id']}",
        json={"employment_status": "Terminated", "termination_date": termination_date},
        headers=auth(auth_token),
    )
    assert rejected.status_code == 422


@pytest.mark.asyncio
async def test_driver_without_current_or_pending_assignment_cannot_be_edited_but_pending_can(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    historical = await create_employee(
        client, auth_token, hq_branch_id, hire_date="2000-01-01",
        driver={"driver_number": f"P1B-{suffix()}", "effective_from_at": "2000-01-01T00:00:00Z"},
    )
    historical_driver = historical["driver"]
    await end_driver_assignment(
        direct_db, historical["current_or_pending_assignment"]["driver_assignment_id"],
        ended_at="2001-01-01T00:00:00Z", end_reason="Terminated",
    )

    code_edit = await client.patch(
        f"/core/drivers/{historical_driver['driver_id']}",
        json={"driver_number": f"P1B-{suffix()}"}, headers=auth(auth_token),
    )
    cdl_edit = await client.patch(
        f"/core/drivers/{historical_driver['driver_id']}",
        json={"cdl_number": "HISTORICAL-CDL"}, headers=auth(auth_token),
    )
    assert code_edit.status_code == 422
    assert cdl_edit.status_code == 422

    pending = await create_employee(
        client, auth_token, hq_branch_id,
        driver={"driver_number": f"P1B-{suffix()}", "effective_from_at": "2099-01-01T00:00:00Z"},
    )
    pending_driver = pending["driver"]
    changed = await client.patch(
        f"/core/drivers/{pending_driver['driver_id']}",
        json={"cdl_number": "PENDING-CDL"}, headers=auth(auth_token),
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["cdl_number"] == "PENDING-CDL"


@pytest.mark.asyncio
async def test_current_instant_driver_remains_editable_through_both_surfaces(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    employee = await create_employee(
        client, auth_token, hq_branch_id, driver={"driver_number": f"P1B-{suffix()}"},
    )
    driver_id = employee["driver"]["driver_id"]
    assignment_id = employee["current_or_pending_assignment"]["driver_assignment_id"]
    stored_from_at = (await direct_db.execute(text(
        "SELECT EffectiveFromAt FROM core.DriverAssignments WHERE DriverAssignmentID = :assignment_id"
    ), {"assignment_id": assignment_id})).scalar_one()
    assert stored_from_at.tzinfo is not None
    from app.workforce.effective import resolve_effective_employee_assignment

    resolved = await resolve_effective_employee_assignment(1, employee["employee_id"], None, direct_db)
    assert resolved is not None
    assert resolved["driverassignmentid"] == assignment_id
    assert resolved["driverid"] == driver_id

    new_number = f"P1B-NULL-{suffix()}"
    changed = await client.patch(
        f"/core/drivers/{driver_id}",
        json={"driver_number": new_number}, headers=auth(auth_token),
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["driver_number"] == new_number
    assert changed.json()["driver_assignment_id"] == assignment_id

    # The Workforce surface edits the same permanent Driver.
    workforce_edit = await client.patch(
        f"/workforce/drivers/{driver_id}",
        json={"cdl_number": "WF-CDL", "driver_status": "OnLeave"}, headers=auth(auth_token),
    )
    assert workforce_edit.status_code == 200, workforce_edit.text
    assert workforce_edit.json()["driver"]["cdl_number"] == "WF-CDL"
    assert workforce_edit.json()["driver"]["driver_status"] == "OnLeave"
    assert workforce_edit.json()["driver"]["driver_number"] == new_number


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
async def test_first_driver_uses_employee_branch_and_database_instant(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int,
):
    employee = await create_employee(client, auth_token, hq_branch_id, hire_date="2020-02-03")
    response = await client.post(
        f"/workforce/employees/{employee['employee_id']}/driver",
        json={"driver_number": f"P1B-{suffix()}"}, headers=auth(auth_token),
    )
    assert response.status_code == 201, response.text
    assignment = response.json()["current_or_pending_assignment"]
    assert assignment["branch_id"] == hq_branch_id
    assert assignment["effective_from_at"] is not None
    assert response.json()["driver"]["driver_id"] == assignment["driver_id"]

    # A Driver already exists: later assignments are transfers, and there is no rehire flow.
    again = await client.post(
        f"/workforce/employees/{employee['employee_id']}/driver",
        json={"driver_number": f"P1B-{suffix()}"}, headers=auth(auth_token),
    )
    assert again.status_code == 409


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
@pytest.mark.parametrize("effective_from_at", [None, "2099-01-01T00:00:00Z"], ids=["current", "pending"])
async def test_employee_branch_edit_rejected_with_current_or_pending_profile(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, paytest_branch_id: int,
    effective_from_at: str | None,
):
    profile = {"driver_number": f"P1B-{suffix()}"}
    if effective_from_at:
        profile["effective_from_at"] = effective_from_at
    employee = await create_employee(client, auth_token, hq_branch_id, driver=profile)
    assert employee["driver_state"] == ("pending" if effective_from_at else "current")
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
            (CompanyCode, CompanyName, LegalName, Status, IsSuspended)
        VALUES (:code, :name, :name, 'Active', FALSE)
        RETURNING CompanyID
    """), {"code": f"P1B{tag}", "name": f"P1B Other {tag}"})).scalar_one()
    other_branch = (await direct_db.execute(text("""
        INSERT INTO core.Branches (CompanyID, BranchCode, BranchName, Status, IsDefault, timezoneid)
        VALUES (:company_id, :code, :name, 'Active', TRUE, 'UTC')
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
    current = await create_employee(client, auth_token, hq_branch_id, driver={"driver_number": f"P1B-{suffix()}"})
    pending = await create_employee(client, auth_token, hq_branch_id, driver={"driver_number": f"P1B-{suffix()}", "effective_from_at": "2099-01-01T00:00:00Z"})
    none = await create_employee(client, auth_token, hq_branch_id)
    historical = await create_employee(client, auth_token, hq_branch_id, driver={"driver_number": f"P1B-{suffix()}", "effective_from_at": "2000-01-01T00:00:00Z"})
    await end_driver_assignment(
        direct_db, historical["current_or_pending_assignment"]["driver_assignment_id"],
        ended_at="2001-01-01T00:00:00Z", end_reason="Terminated",
    )
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
        driver={"driver_number": f"P1B-{suffix()}"},
    )
    driver_id = employee["driver"]["driver_id"]
    rejected = await client.patch(
        f"/core/drivers/{driver_id}", json={"full_name": "Not Allowed"}, headers=auth(auth_token),
    )
    assert rejected.status_code == 422
    changed = await client.patch(
        f"/core/drivers/{driver_id}", json={"driver_number": f"P1B-{suffix()}"}, headers=auth(auth_token),
    )
    assert changed.status_code == 200, changed.text


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_employee_driver_and_first_assignment(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db, monkeypatch,
):
    from app.workforce import audit as workforce_audit

    full_name = f"Rollback {suffix()}"
    original = workforce_audit.write_workforce_audit

    async def fail_on_profile(db, **kwargs):
        if kwargs["action_code"] == "DRIVER_CREATED":
            raise RuntimeError("forced Workforce audit failure")
        await original(db, **kwargs)

    monkeypatch.setattr(workforce_audit, "write_workforce_audit", fail_on_profile)
    with pytest.raises(RuntimeError, match="forced Workforce audit failure"):
        await client.post(
            "/workforce/employees",
            json={"branch_id": hq_branch_id, "full_name": full_name,
                  "driver": {"driver_number": f"P1B-{suffix()}"}},
            headers=auth(auth_token),
        )
    assert (await direct_db.execute(
        text("SELECT COUNT(*) FROM core.Employees WHERE FullName = :name"), {"name": full_name},
    )).scalar_one() == 0
    assert (await direct_db.execute(
        text("SELECT COUNT(*) FROM audit.AuditLog WHERE NewValueJson LIKE :name"), {"name": f"%{full_name}%"},
    )).scalar_one() == 0


@pytest.mark.asyncio
async def test_driver_number_conflict_rolls_back_employee_and_audits(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db, monkeypatch,
):
    from app.workforce import audit as workforce_audit

    driver_number = f"P1B-DUP-{suffix()}"
    existing = await create_employee(
        client, auth_token, hq_branch_id, driver={"driver_number": driver_number},
    )
    failed_name = f"Driver number conflict {suffix()}"
    audit_calls: list[tuple[str, int]] = []
    original = workforce_audit.write_workforce_audit

    async def capture_employee_audit(db, **kwargs):
        if kwargs["action_code"] == "EMPLOYEE_CREATED":
            audit_calls.append((kwargs["action_code"], kwargs["entity_id"]))
        await original(db, **kwargs)

    monkeypatch.setattr(workforce_audit, "write_workforce_audit", capture_employee_audit)
    conflict = await client.post(
        "/workforce/employees",
        json={"branch_id": hq_branch_id, "full_name": failed_name,
              "driver": {"driver_number": driver_number}},
        headers=auth(auth_token),
    )
    assert conflict.status_code == 409, conflict.text
    assert (await direct_db.execute(text(
        "SELECT COUNT(*) FROM core.Employees WHERE FullName = :name"
    ), {"name": failed_name})).scalar_one() == 0
    assert (await direct_db.execute(text(
        "SELECT COUNT(*) FROM core.Drivers WHERE CompanyID = 1 AND DriverNumber = :number"
    ), {"number": driver_number})).scalar_one() == 1

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
    assert existing["driver"]["driver_number"] == driver_number


@pytest.mark.asyncio
async def test_hire_date_guard_uses_nonvoid_daily_draft_lines_across_assignment_history(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int,
    created_period_id: int, direct_db,
):
    employee = await create_employee(
        client, auth_token, hq_branch_id, hire_date="2000-01-01",
        driver={"driver_number": f"P1B-{suffix()}", "effective_from_at": "2000-01-01T00:00:00Z"},
    )
    source_id = employee["current_or_pending_assignment"]["driver_assignment_id"]
    await end_driver_assignment(
        direct_db, source_id, ended_at="2020-01-01T00:00:00Z", end_reason="Transferred",
    )
    target_id = await insert_driver_assignment(
        direct_db, company_id=1, branch_id=hq_branch_id, employee_id=employee["employee_id"],
        driver_id=employee["driver"]["driver_id"],
        effective_from_at="2020-01-01T00:00:00Z", transferred_from_assignment_id=source_id,
    )
    await direct_db.execute(text("""
        INSERT INTO payroll.PayrollDraftLines
            (CompanyID, BranchID, PayrollPeriodID, DriverAssignmentID, WorkDate, LineType,
             Quantity, SourceType, Status)
        VALUES (1, :branch_id, :period_id, :assignment_id, DATE '2018-06-01',
                :line_type, 1, 'Manual', 'Active')
    """), {"branch_id": hq_branch_id, "period_id": created_period_id,
           "assignment_id": source_id, "line_type": f"P1B-{suffix()}"})

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
        driver={"driver_number": f"P1B-{suffix()}", "effective_from_at": "2000-01-01T00:00:00Z"},
    )
    assignment_id = employee["current_or_pending_assignment"]["driver_assignment_id"]
    await direct_db.execute(text("""
        INSERT INTO payroll.PayrollDraftLines
            (CompanyID, BranchID, PayrollPeriodID, DriverAssignmentID, WorkDate, LineType,
             Quantity, SourceType, Status)
        VALUES (1, :branch_id, :period_id, :assignment_id, DATE '2018-06-01',
                :line_type, 1, 'Manual', 'Void')
    """), {"branch_id": hq_branch_id, "period_id": created_period_id,
           "assignment_id": assignment_id, "line_type": f"P1B-{suffix()}"})
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
        driver={"driver_number": f"P1B-{suffix()}", "effective_from_at": "2000-01-01T00:00:00Z"},
    )
    assignment_id = employee["current_or_pending_assignment"]["driver_assignment_id"]
    chain = await seed_snapshot_component(
        direct_db, period_id=created_period_id, driver_assignment_id=assignment_id,
        work_date=date(2018, 6, 1))
    await insert_final_line(direct_db, chain)
    try:
        response = await client.patch(
            f"/workforce/employees/{employee['employee_id']}",
            json={"hire_date": "2018-06-02"}, headers=auth(auth_token),
        )
        assert response.status_code == 422
    finally:
        # The evidence sits on the session-shared HQ period; remove exactly this chain.
        await discard_chain(direct_db, chain)


@pytest.mark.asyncio
async def test_driver_role_requires_employee_link_before_assignment(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    tag = suffix()
    caller = await create_provisioned_test_user(
        client, auth_token, f"p1b_access_{tag}", f"P1B Access {tag}",
    )
    target = await create_provisioned_test_user(
        client, auth_token, f"p1b_target_{tag}", f"P1B Target {tag}",
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
    assert response.status_code == 422
    assert "explicitly linked employee" in response.text.lower()
    after = (await direct_db.execute(text("""
        SELECT COUNT(*) FROM sec.UserBranchRoles WHERE UserID = :user_id AND IsActive
    """), {"user_id": target_id})).scalar_one()
    linked_employee = (await direct_db.execute(
        text("SELECT EmployeeID FROM sec.Users WHERE UserID = :user_id"), {"user_id": target_id},
    )).scalar_one()
    assert after == before
    assert linked_employee is None


@pytest.mark.asyncio
async def test_admin_driver_assignment_uses_explicit_workforce_driver(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    tag = suffix()
    created = await create_provisioned_test_user(
        client, auth_token, f"p1b_{tag}", f"P1B Hook {tag}",
    )
    assert created.status_code in {200, 201}, created.text
    user_id = created.json()["user_id"]
    employee = await create_employee(
        client, auth_token, hq_branch_id,
        driver={"driver_number": f"P1B-{tag}"},
    )
    linked = await client.put(
        f"/admin/users/{user_id}/employee-link",
        json={"employee_id": employee["employee_id"]},
        headers=auth(auth_token),
    )
    assert linked.status_code == 200, linked.text
    roles = await client.get("/admin/company-roles", headers=auth(auth_token))
    driver_role = next(role for role in roles.json() if role["role_code"] == "DRIVER")
    assigned = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json={"company_role_id": driver_role["company_role_id"], "scope_type": "Self"},
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
