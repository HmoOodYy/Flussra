"""Focused HTTP and security coverage for the Payroll Setup API."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from app.auth.security import create_access_token
from app.payroll_setup import policy
from app.payroll_setup.errors import PolicyError


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _make_actor(db_conn, *, permissions: tuple[str, ...], scope: str,
                      branch_id: int | None = None, driver: bool = False) -> str:
    """Seed a narrowly scoped HTTP principal and return its JWT."""
    suffix = uuid4().hex[:10]
    user_id = (await db_conn.execute(text("""
        INSERT INTO sec.Users (CompanyID, Username, DisplayName, IsActive, CanLogin)
        SELECT CompanyID, :username, 'Phase 4 HTTP Actor', TRUE, TRUE
        FROM core.Companies WHERE CompanyCode = 'DEMO'
        RETURNING UserID
    """), {"username": "p4http_" + suffix})).scalar_one()
    company_id = (await db_conn.execute(text(
        "SELECT CompanyID FROM core.Companies WHERE CompanyCode = 'DEMO'"
    ))).scalar_one()
    role_id = (await db_conn.execute(text("""
        INSERT INTO sec.Roles (RoleCode, RoleName, RoleLevel, IsSystemRole)
        VALUES (:code, 'Phase 4 HTTP Actor', 20, FALSE) RETURNING RoleID
    """), {"code": "P4HR_" + suffix})).scalar_one()
    company_role_code = "DRIVER" if driver else "P4HC_" + suffix
    if driver:
        company_role_id = (await db_conn.execute(text("""
            SELECT CompanyRoleID FROM sec.CompanyRoles
            WHERE CompanyID = :cid AND RoleCode = 'DRIVER'
        """), {"cid": company_id})).scalar_one()
    else:
        company_role_id = (await db_conn.execute(text("""
            INSERT INTO sec.CompanyRoles
                (CompanyID, RoleCode, RoleName, RoleLevel, IsDefault,
                 IsProtected, IsCustom, IsActive)
            VALUES (:cid, :code, 'Phase 4 HTTP Actor', 20, FALSE, FALSE, TRUE, TRUE)
            RETURNING CompanyRoleID
        """), {"cid": company_id, "code": company_role_code})).scalar_one()
        for permission in permissions:
            await db_conn.execute(text("""
                INSERT INTO sec.CompanyRolePermissions (CompanyRoleID, PermissionCode)
                VALUES (:rid, :permission)
            """), {"rid": company_role_id, "permission": permission})
    await db_conn.execute(text("""
        INSERT INTO sec.UserBranchRoles
            (UserID, CompanyID, BranchID, RoleID, CompanyRoleID, ScopeType, IsActive)
        VALUES (:uid, :cid, :bid, :rid, :crid, :scope, TRUE)
    """), {
        "uid": user_id, "cid": company_id,
        "bid": branch_id if scope in ("SpecificBranch", "OwnDriverDataOnly") else None,
        "rid": role_id, "crid": company_role_id, "scope": scope,
    })
    return create_access_token(int(user_id), int(company_id))


async def _make_other_company_manager(db_conn, *, company_code: str) -> tuple[int, str]:
    """Create an isolated tenant and company-wide manager for HTTP tenant checks."""
    suffix = uuid4().hex[:10]
    company_id = (await db_conn.execute(text("""
        INSERT INTO core.Companies (CompanyCode, CompanyName, Status, IsSuspended)
        VALUES (:code, 'Phase 4 Duplicate-Code Tenant', 'Active', FALSE)
        RETURNING CompanyID
    """), {"code": company_code})).scalar_one()
    user_id = (await db_conn.execute(text("""
        INSERT INTO sec.Users (CompanyID, Username, DisplayName, IsActive, CanLogin)
        VALUES (:cid, :username, 'Phase 4 Tenant Manager', TRUE, TRUE)
        RETURNING UserID
    """), {"cid": company_id, "username": "p4tenant_" + suffix})).scalar_one()
    await db_conn.execute(text(
        "UPDATE core.Companies SET OwnerUserID = :uid WHERE CompanyID = :cid"
    ), {"uid": user_id, "cid": company_id})
    role_id = (await db_conn.execute(text("""
        INSERT INTO sec.Roles (RoleCode, RoleName, RoleLevel, IsSystemRole)
        VALUES (:code, 'Phase 4 Tenant Manager', 20, FALSE) RETURNING RoleID
    """), {"code": "P4TR_" + suffix})).scalar_one()
    company_role_id = (await db_conn.execute(text("""
        INSERT INTO sec.CompanyRoles
            (CompanyID, RoleCode, RoleName, RoleLevel, IsDefault,
             IsProtected, IsCustom, IsActive)
        VALUES (:cid, :code, 'Phase 4 Tenant Manager', 20, FALSE, FALSE, TRUE, TRUE)
        RETURNING CompanyRoleID
    """), {"cid": company_id, "code": "P4TC_" + suffix})).scalar_one()
    await db_conn.execute(text("""
        INSERT INTO sec.CompanyRolePermissions (CompanyRoleID, PermissionCode)
        VALUES (:rid, 'payroll_setup.manage')
    """), {"rid": company_role_id})
    await db_conn.execute(text("""
        INSERT INTO sec.UserBranchRoles
            (UserID, CompanyID, BranchID, RoleID, CompanyRoleID, ScopeType, IsActive)
        VALUES (:uid, :cid, NULL, :role_id, :company_role_id,
                'AllCompanyBranches', TRUE)
    """), {"uid": user_id, "cid": company_id, "role_id": role_id,
            "company_role_id": company_role_id})
    return int(company_id), create_access_token(int(user_id), int(company_id))


@pytest.mark.asyncio
async def test_payroll_setup_routes_are_registered(test_app):
    paths = test_app.openapi()["paths"]
    expected = {
        "/payroll-setup/setups", "/payroll-setup/setups/{setup_id}",
        "/payroll-setup/setups/{setup_id}/archive",
        "/payroll-setup/setups/{setup_id}/drafts",
        "/payroll-setup/setups/{setup_id}/drafts/{draft_id}",
        "/payroll-setup/setups/{setup_id}/drafts/{draft_id}/publication-impact",
        "/payroll-setup/setups/{setup_id}/drafts/{draft_id}/publish",
        "/payroll-setup/setups/{setup_id}/versions", "/payroll-setup/default",
        "/payroll-setup/branches/{branch_id}/assignments",
        "/payroll-setup/branches/{branch_id}/reassignments",
        "/payroll-setup/branches/{branch_id}/reassignment-impact",
        "/payroll-setup/assignments/{assignment_id}/withdraw",
        "/payroll-setup/branches/{branch_id}/history",
        "/payroll-setup/branches/{branch_id}/effective",
    }
    assert expected <= paths.keys()


@pytest.mark.asyncio
async def test_company_and_branch_read_authority(client: httpx.AsyncClient, auth_token, db_conn):
    unauthenticated = await client.get("/payroll-setup/setups")
    assert unauthenticated.status_code == 401
    company_read = await client.get("/payroll-setup/setups", headers=_auth(auth_token))
    assert company_read.status_code == 200
    assert isinstance(company_read.json(), list)

    branch_user = await client.post("/auth/login", json={
        "username": "branch_user",
        "password": "TestPass123!",
        "company_code": "DEMO",
    })
    assert branch_user.status_code == 200
    branch_token = branch_user.json()["access_token"]

    denied_company_read = await client.get(
        "/payroll-setup/setups", headers=_auth(branch_token),
    )
    assert denied_company_read.status_code == 403

    branch_id = (await db_conn.execute(text("""
        SELECT b.BranchID FROM core.Branches b
        JOIN core.Companies c ON c.CompanyID = b.CompanyID
        WHERE c.CompanyCode = 'DEMO' AND b.BranchCode = 'HQ'
    """))).scalar_one()
    branch_history = await client.get(
        f"/payroll-setup/branches/{branch_id}/history",
        headers=_auth(branch_token),
    )
    assert branch_history.status_code == 200
    history = branch_history.json()
    assert history["branch_id"] == branch_id
    assert isinstance(history["assignments"], list)
    assert all(row["branch_id"] == branch_id for row in history["assignments"])
    other_branch_id = (await db_conn.execute(text("""
        SELECT b.BranchID FROM core.Branches b
        JOIN core.Companies c ON c.CompanyID = b.CompanyID
        WHERE c.CompanyCode = 'DEMO' AND b.BranchCode = 'PAYTEST'
    """))).scalar_one()
    denied_branch = await client.get(
        f"/payroll-setup/branches/{other_branch_id}/history", headers=_auth(branch_token),
    )
    assert denied_branch.status_code == 403
    denied_effective = await client.get(
        f"/payroll-setup/branches/{other_branch_id}/effective",
        params={"period_start_date": "2090-01-01"}, headers=_auth(branch_token),
    )
    assert denied_effective.status_code == 403


@pytest.mark.asyncio
async def test_default_permission_gates_manage_assign_scope_and_driver(client, auth_token, db_conn):
    created = await client.post("/payroll-setup/setups", headers=_auth(auth_token), json={
        "setup_code": "P4DEF_" + uuid4().hex[:8], "setup_name": "Default gate",
    })
    assert created.status_code == 201, created.text
    sid = created.json()["setup_id"]
    branch_id = (await db_conn.execute(text("""
        SELECT b.BranchID FROM core.Branches b JOIN core.Companies c USING (CompanyID)
        WHERE c.CompanyCode = 'DEMO' AND b.BranchCode = 'HQ'
    """))).scalar_one()
    manage = await _make_actor(
        db_conn, permissions=("payroll_setup.manage",), scope="AllCompanyBranches",
    )
    assign = await _make_actor(
        db_conn, permissions=("payroll_setup.assign",), scope="AllCompanyBranches",
    )
    scoped_assign = await _make_actor(
        db_conn, permissions=("payroll_setup.assign",), scope="SpecificBranch",
        branch_id=branch_id,
    )
    driver = await _make_actor(
        db_conn, permissions=("payroll_setup.assign",),
        scope="AllCompanyBranches", driver=True,
    )
    for token, value in ((manage, sid), (manage, None), (scoped_assign, sid), (driver, sid)):
        response = await client.put(
            "/payroll-setup/default", headers=_auth(token), json={"setup_id": value},
        )
        assert response.status_code == 403, response.text
    for token in (assign, scoped_assign, driver):
        response = await client.get(
            f"/payroll-setup/branches/{branch_id}/assignments", headers=_auth(token),
        )
        assert response.status_code == 403, response.text
    forbidden_preview = await client.post(
        "/payroll-setup/setups/987654/drafts/987654/publication-impact",
        headers=_auth(manage), json={"effective_from_date": "2090-01-01"},
    )
    assert forbidden_preview.status_code == 403, forbidden_preview.text
    for value in (sid, None):
        if value is None:
            current_default = await client.get(
                "/payroll-setup/default", headers=_auth(auth_token),
            )
            assert current_default.status_code == 200, current_default.text
            assert current_default.json()["setup"]["setup_id"] == sid
        response = await client.put(
            "/payroll-setup/default", headers=_auth(assign), json={"setup_id": value},
        )
        assert response.status_code == 204, response.text
    cleared_default = await client.get("/payroll-setup/default", headers=_auth(auth_token))
    assert cleared_default.status_code == 200, cleared_default.text
    assert cleared_default.json() == {"setup": None}
    updated = await client.put(
        f"/payroll-setup/setups/{sid}", headers=_auth(auth_token),
        json={"setup_name": "Default gate updated", "description": None},
    )
    assert updated.status_code == 200, updated.text
    archived = await client.post(f"/payroll-setup/setups/{sid}/archive",
                                 headers=_auth(auth_token))
    assert archived.status_code == 204, archived.text
    audit_types = (await db_conn.execute(text("""
        SELECT EventType FROM payroll.PayrollSetupPolicyAuditEvents
        WHERE PayrollSetupID = :sid
          AND EventType IN ('SetupMetadataChanged', 'SetupArchived')
        ORDER BY PayrollSetupPolicyAuditEventID
    """), {"sid": sid})).scalars().all()
    assert list(audit_types) == ["SetupMetadataChanged", "SetupArchived"]


@pytest.mark.asyncio
async def test_drafts_publication_versions_and_discarded_draft_hidden(
    client, auth_token, db_conn,
):
    headers = _auth(auth_token)
    created = await client.post("/payroll-setup/setups", headers=headers, json={
        "setup_code": "P4VER_" + uuid4().hex[:8], "setup_name": "Version lifecycle",
    })
    assert created.status_code == 201, created.text
    sid = created.json()["setup_id"]
    schedule = {"payroll_frequency": "Week", "anchor_start_date": "2090-01-01",
                "custom_interval_days": None, "normal_days_off_mask": 0}
    draft = await client.post(f"/payroll-setup/setups/{sid}/drafts", headers=headers,
                              json=schedule)
    discarded = await client.post(f"/payroll-setup/setups/{sid}/drafts", headers=headers,
                                  json=schedule)
    assert draft.status_code == discarded.status_code == 201, (draft.text, discarded.text)
    did, discard_id = draft.json()["version_id"], discarded.json()["version_id"]
    edited = await client.put(
        f"/payroll-setup/setups/{sid}/drafts/{did}", headers=headers, json=schedule,
    )
    assert edited.status_code == 200, edited.text
    deleted = await client.delete(
        f"/payroll-setup/setups/{sid}/drafts/{discard_id}", headers=headers,
    )
    assert deleted.status_code == 204, deleted.text
    draft_list = await client.get(f"/payroll-setup/setups/{sid}/drafts", headers=headers)
    assert draft_list.status_code == 200, draft_list.text
    assert [row["version_id"] for row in draft_list.json()] == [did]
    impact = await client.post(
        f"/payroll-setup/setups/{sid}/drafts/{did}/publication-impact", headers=headers,
        json={"effective_from_date": "2090-01-01"},
    )
    assert impact.status_code == 200, impact.text
    manager = await _make_actor(
        db_conn, permissions=("payroll_setup.manage",), scope="AllCompanyBranches",
    )
    denied_publish = await client.post(
        f"/payroll-setup/setups/{sid}/drafts/{did}/publish", headers=_auth(manager),
        json={"effective_from_date": "2090-01-01"},
    )
    assert denied_publish.status_code == 403, denied_publish.text
    first = await client.post(f"/payroll-setup/setups/{sid}/drafts/{did}/publish",
                              headers=headers, json={"effective_from_date": "2090-01-01"})
    assert first.status_code == 201, first.text
    first_id = first.json()["version_id"]
    draft2 = await client.post(f"/payroll-setup/setups/{sid}/drafts", headers=headers,
                               json=schedule)
    assert draft2.status_code == 201, draft2.text
    did2 = draft2.json()["version_id"]
    conflict = await client.post(f"/payroll-setup/setups/{sid}/drafts/{did2}/publish",
                                 headers=headers,
                                 json={"effective_from_date": "2090-01-01"})
    assert conflict.status_code == 409, conflict.text
    second = await client.post(f"/payroll-setup/setups/{sid}/drafts/{did2}/publish",
                               headers=headers,
                               json={"effective_from_date": "2090-01-01",
                                     "replaces_version_id": first_id})
    assert second.status_code == 201, second.text
    second_id = second.json()["version_id"]
    versions = await client.get(f"/payroll-setup/setups/{sid}/versions", headers=headers)
    assert versions.status_code == 200, versions.text
    by_id = {v["version_id"]: v for v in versions.json()}
    assert set(by_id) == {first_id, second_id}
    assert by_id[first_id]["effective_to_date"] == "2090-01-01"
    assert not by_id[first_id]["is_terminal"]
    assert by_id[first_id]["replaced_by_version_id"] == second_id
    assert by_id[second_id]["is_terminal"]
    assert by_id[second_id]["replaces_version_id"] == first_id
    future_draft = await client.post(
        f"/payroll-setup/setups/{sid}/drafts", headers=headers,
        json={**schedule, "normal_days_off_mask": 1},
    )
    assert future_draft.status_code == 201, future_draft.text
    future_id = future_draft.json()["version_id"]
    future = await client.post(
        f"/payroll-setup/setups/{sid}/drafts/{future_id}/publish", headers=headers,
        json={"effective_from_date": "2090-01-08"},
    )
    assert future.status_code == 201, future.text
    timeline = await client.get(f"/payroll-setup/setups/{sid}/versions", headers=headers)
    assert timeline.status_code == 200, timeline.text
    timeline_by_id = {v["version_id"]: v for v in timeline.json()}
    assert timeline_by_id[second_id]["effective_to_date"] == "2090-01-08"
    assert timeline_by_id[future_id]["effective_from_date"] == "2090-01-08"
    assert timeline_by_id[future_id]["effective_to_date"] is None
    assert timeline_by_id[future_id]["is_terminal"] is True


@pytest.mark.asyncio
async def test_cross_tenant_assignments_and_legacy_setup_routes(client, auth_token, db_conn):
    headers = _auth(auth_token)
    foreign_company = (await db_conn.execute(text("""
        INSERT INTO core.Companies (CompanyCode, CompanyName, Status, IsSuspended)
        VALUES (:code, 'Foreign tenant', 'Active', FALSE) RETURNING CompanyID
    """), {"code": "P4X_" + uuid4().hex[:8]})).scalar_one()
    foreign_branch = (await db_conn.execute(text("""
        INSERT INTO core.Branches (CompanyID, BranchCode, BranchName, Status, IsDefault)
        VALUES (:cid, 'FOREIGN', 'Foreign branch', 'Active', FALSE) RETURNING BranchID
    """), {"cid": foreign_company})).scalar_one()
    foreign_setup = (await db_conn.execute(text("""
        INSERT INTO payroll.PayrollSetups (CompanyID, SetupCode, SetupName)
        VALUES (:cid, :code, 'Foreign setup') RETURNING PayrollSetupID
    """), {"cid": foreign_company, "code": "P4FS_" + uuid4().hex[:8]})).scalar_one()
    response = await client.get(f"/payroll-setup/setups/{foreign_setup}", headers=headers)
    assert response.status_code == 404, response.text
    response = await client.get(
        f"/payroll-setup/branches/{foreign_branch}/assignments", headers=headers,
    )
    assert response.status_code == 404, response.text
    branch_id = (await db_conn.execute(text("""
        SELECT b.BranchID FROM core.Branches b JOIN core.Companies c USING (CompanyID)
        WHERE c.CompanyCode = 'DEMO' AND b.BranchCode = 'HQ'
    """))).scalar_one()
    for method, body in (("get", None), ("put", {
        "payroll_frequency": "Week", "anchor_start_date": "2090-01-01",
    })):
        kwargs = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        legacy = await getattr(client, method)(
            f"/settings/branches/{branch_id}/payroll-setup", **kwargs,
        )
        assert legacy.status_code == 410, legacy.text


@pytest.mark.asyncio
async def test_mutation_schema_and_not_found_error_contract(client, auth_token):
    headers = _auth(auth_token)
    extra = await client.post("/payroll-setup/setups", headers=headers, json={
        "setup_code": "P4STRICT_" + uuid4().hex[:8], "setup_name": "Strict body",
        "company_id": 1,
    })
    assert extra.status_code == 422, extra.text
    missing = await client.get("/payroll-setup/setups/987654321", headers=headers)
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"]["code"] == "SETUP_NOT_FOUND"


@pytest.mark.asyncio
async def test_setup_code_conflict_is_atomic_tenant_scoped_and_transaction_recovers(
    client, auth_token, db_conn,
):
    code = "P4DUP_" + uuid4().hex[:10]
    body = {"setup_code": code, "setup_name": "Tenant-scoped duplicate"}
    first = await client.post("/payroll-setup/setups", headers=_auth(auth_token), json=body)
    assert first.status_code == 201, first.text
    first_id = first.json()["setup_id"]
    duplicate = await client.post(
        "/payroll-setup/setups", headers=_auth(auth_token), json=body,
    )
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["detail"]["code"] == "SETUP_CODE_CONFLICT"

    demo_id = (await db_conn.execute(text(
        "SELECT CompanyID FROM core.Companies WHERE CompanyCode = 'DEMO'"
    ))).scalar_one()
    same_company_rows = (await db_conn.execute(text("""
        SELECT COUNT(*) FROM payroll.PayrollSetups
        WHERE CompanyID = :cid AND SetupCode = :code
    """), {"cid": demo_id, "code": code})).scalar_one()
    same_company_audits = (await db_conn.execute(text("""
        SELECT COUNT(*) FROM payroll.PayrollSetupPolicyAuditEvents
        WHERE CompanyID = :cid AND EventType = 'SetupCreated'
          AND PayrollSetupID = :sid
    """), {"cid": demo_id, "sid": first_id})).scalar_one()
    assert same_company_rows == 1
    assert same_company_audits == 1

    recovered = await client.post("/payroll-setup/setups", headers=_auth(auth_token), json={
        "setup_code": "P4RECOVER_" + uuid4().hex[:8], "setup_name": "After conflict",
    })
    assert recovered.status_code == 201, recovered.text

    other_company_id, other_company_token = await _make_other_company_manager(
        db_conn, company_code="P4T_" + uuid4().hex[:8],
    )
    other_tenant = await client.post(
        "/payroll-setup/setups", headers=_auth(other_company_token), json=body,
    )
    assert other_tenant.status_code == 201, other_tenant.text
    other_setup_id = other_tenant.json()["setup_id"]
    assert other_setup_id != first_id
    tenant_rows = (await db_conn.execute(text("""
        SELECT COUNT(*) FROM payroll.PayrollSetups
        WHERE CompanyID = :cid AND SetupCode = :code
    """), {"cid": other_company_id, "code": code})).scalar_one()
    tenant_audits = (await db_conn.execute(text("""
        SELECT COUNT(*) FROM payroll.PayrollSetupPolicyAuditEvents
        WHERE CompanyID = :cid AND EventType = 'SetupCreated'
          AND PayrollSetupID = :sid
    """), {"cid": other_company_id, "sid": other_setup_id})).scalar_one()
    assert tenant_rows == 1
    assert tenant_audits == 1


@pytest.mark.asyncio
async def test_concurrent_same_company_setup_code_posts_have_one_winner(
    client, auth_token, db_conn,
):
    code = "P4RACE_" + uuid4().hex[:10]
    body = {"setup_code": code, "setup_name": "Concurrent duplicate"}
    headers = _auth(auth_token)
    responses = await asyncio.gather(*(
        client.post("/payroll-setup/setups", headers=headers, json=body)
        for _ in range(2)
    ))
    assert sorted(response.status_code for response in responses) == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["detail"]["code"] == "SETUP_CODE_CONFLICT"
    winner = next(response for response in responses if response.status_code == 201)
    winner_id = winner.json()["setup_id"]
    company_id = (await db_conn.execute(text(
        "SELECT CompanyID FROM core.Companies WHERE CompanyCode = 'DEMO'"
    ))).scalar_one()
    row_count = (await db_conn.execute(text("""
        SELECT COUNT(*) FROM payroll.PayrollSetups
        WHERE CompanyID = :cid AND SetupCode = :code
    """), {"cid": company_id, "code": code})).scalar_one()
    audit_count = (await db_conn.execute(text("""
        SELECT COUNT(*) FROM payroll.PayrollSetupPolicyAuditEvents
        WHERE CompanyID = :cid AND EventType = 'SetupCreated'
          AND PayrollSetupID = :sid
    """), {"cid": company_id, "sid": winner_id})).scalar_one()
    assert row_count == 1
    assert audit_count == 1


@pytest.mark.asyncio
async def test_assignment_reassignment_withdrawal_branch_history_and_effective(
    client, auth_token, db_conn,
):
    headers = _auth(auth_token)
    company_id = (await db_conn.execute(text(
        "SELECT CompanyID FROM core.Companies WHERE CompanyCode = 'DEMO'"
    ))).scalar_one()
    branch_id = (await db_conn.execute(text("""
        INSERT INTO core.Branches (CompanyID, BranchCode, BranchName, Status, IsDefault)
        VALUES (:cid, :code, 'Phase 4 authority test', 'Active', FALSE)
        RETURNING BranchID
    """), {"cid": company_id, "code": "P4B_" + uuid4().hex[:8]})).scalar_one()
    second_branch_id = (await db_conn.execute(text("""
        INSERT INTO core.Branches (CompanyID, BranchCode, BranchName, Status, IsDefault)
        VALUES (:cid, :code, 'Second Phase 4 authority test', 'Active', FALSE)
        RETURNING BranchID
    """), {"cid": company_id, "code": "P4B_" + uuid4().hex[:8]})).scalar_one()

    async def publish(code: str) -> int:
        response = await client.post("/payroll-setup/setups", headers=headers, json={
            "setup_code": code, "setup_name": code,
        })
        assert response.status_code == 201, response.text
        sid = response.json()["setup_id"]
        draft = await client.post(f"/payroll-setup/setups/{sid}/drafts", headers=headers,
                                  json={"payroll_frequency": "Week",
                                        "anchor_start_date": "2090-01-01",
                                        "custom_interval_days": None,
                                        "normal_days_off_mask": 0})
        assert draft.status_code == 201, draft.text
        result = await client.post(
            f"/payroll-setup/setups/{sid}/drafts/{draft.json()['version_id']}/publish",
            headers=headers, json={"effective_from_date": "2090-01-01"},
        )
        assert result.status_code == 201, result.text
        return sid

    source_id = await publish("P4SRC_" + uuid4().hex[:8])
    destination_id = await publish("P4DST_" + uuid4().hex[:8])
    assignment = await client.post(
        f"/payroll-setup/branches/{branch_id}/assignments", headers=headers,
        json={"setup_id": source_id, "effective_from_date": "2090-01-01",
              "reason": "initial"},
    )
    assert assignment.status_code == 201, assignment.text
    initial_aid = assignment.json()["assignment_id"]
    second_assignment = await client.post(
        f"/payroll-setup/branches/{second_branch_id}/assignments", headers=headers,
        json={"setup_id": source_id, "effective_from_date": "2090-01-01",
              "reason": "second affected branch"},
    )
    assert second_assignment.status_code == 201, second_assignment.text
    source_timeline = await client.get(
        f"/payroll-setup/setups/{source_id}/versions", headers=headers,
    )
    assert source_timeline.status_code == 200, source_timeline.text
    original_version = source_timeline.json()[0]["version_id"]
    future_draft = await client.post(
        f"/payroll-setup/setups/{source_id}/drafts", headers=headers,
        json={"payroll_frequency": "Week", "anchor_start_date": "2090-01-01",
              "custom_interval_days": None, "normal_days_off_mask": 1},
    )
    assert future_draft.status_code == 201, future_draft.text
    impact_publish = await client.post(
        f"/payroll-setup/setups/{source_id}/drafts/{future_draft.json()['version_id']}"
        "/publication-impact",
        headers=headers, json={"effective_from_date": "2090-01-08"},
    )
    assert impact_publish.status_code == 200, impact_publish.text
    affected_branch_ids = sorted([branch_id, second_branch_id])
    assert impact_publish.json()["affected_branch_ids"] == affected_branch_ids
    assert impact_publish.json()["allowed"] is True
    future_publish = await client.post(
        f"/payroll-setup/setups/{source_id}/drafts/{future_draft.json()['version_id']}"
        "/publish",
        headers=headers, json={"effective_from_date": "2090-01-08"},
    )
    assert future_publish.status_code == 201, future_publish.text
    published_version = future_publish.json()["version_id"]
    audit = (await db_conn.execute(text("""
        SELECT e.PayrollSetupPolicyAuditEventID, e.EventType, b.BranchID
        FROM payroll.PayrollSetupPolicyAuditEvents e
        JOIN payroll.PayrollSetupPolicyAuditEventBranches b
          ON b.PayrollSetupPolicyAuditEventID = e.PayrollSetupPolicyAuditEventID
         AND b.CompanyID = e.CompanyID
        WHERE e.CompanyID = :cid AND e.PayrollSetupID = :sid
          AND e.PayrollSetupVersionID = :vid
        ORDER BY b.BranchID
    """), {"cid": company_id, "sid": source_id, "vid": published_version})).all()
    assert len(audit) == 2
    assert {row[1] for row in audit} == {"FutureVersionScheduled"}
    assert [row[2] for row in audit] == affected_branch_ids
    audit_event_id = audit[0][0]
    assert {row[0] for row in audit} == {audit_event_id}
    assert original_version != published_version
    impact = await client.post(
        f"/payroll-setup/branches/{branch_id}/reassignment-impact", headers=headers,
        json={"destination_setup_id": destination_id,
              "effective_from_date": "2090-01-08"},
    )
    assert impact.status_code == 200, impact.text
    assert impact.json()["allowed"] is True
    reassigned = await client.post(
        f"/payroll-setup/branches/{branch_id}/reassignments", headers=headers,
        json={"destination_setup_id": destination_id,
              "effective_from_date": "2090-01-08", "reason": "planned"},
    )
    assert reassigned.status_code == 201, reassigned.text
    second_aid = reassigned.json()["assignment_id"]
    effective = await client.get(
        f"/payroll-setup/branches/{branch_id}/effective",
        params={"period_start_date": "2090-01-08"}, headers=headers,
    )
    assert effective.status_code == 200, effective.text
    assert effective.json()["assignment_id"] == second_aid
    assert effective.json()["setup_id"] == destination_id
    assert effective.json()["period_start_date"] == "2090-01-08"
    assert effective.json()["period_end_date"] == "2090-01-14"
    history_before_withdrawal = await client.get(
        f"/payroll-setup/branches/{branch_id}/history", headers=headers,
    )
    assert history_before_withdrawal.status_code == 200, history_before_withdrawal.text
    before_rows = {
        row["assignment_id"]: row
        for row in history_before_withdrawal.json()["assignments"]
    }
    assert before_rows[initial_aid]["effective_to_date"] == "2090-01-08"

    branch_viewer = await _make_actor(
        db_conn, permissions=("payroll.view",), scope="SpecificBranch",
        branch_id=branch_id,
    )
    denied_assignment = await client.post(
        f"/payroll-setup/branches/{branch_id}/assignments", headers=_auth(branch_viewer),
        json={"setup_id": source_id, "effective_from_date": "2090-01-15"},
    )
    assert denied_assignment.status_code == 403, denied_assignment.text
    own_history = await client.get(
        f"/payroll-setup/branches/{branch_id}/history", headers=_auth(branch_viewer),
    )
    assert own_history.status_code == 200, own_history.text
    own_effective = await client.get(
        f"/payroll-setup/branches/{branch_id}/effective",
        params={"period_start_date": "2090-01-08"}, headers=_auth(branch_viewer),
    )
    assert own_effective.status_code == 200, own_effective.text
    other_branch = (await db_conn.execute(text("""
        SELECT b.BranchID FROM core.Branches b JOIN core.Companies c USING (CompanyID)
        WHERE c.CompanyCode = 'DEMO' AND b.BranchCode = 'PAYTEST'
    """))).scalar_one()
    for path, method, kwargs in (
        ("/payroll-setup/setups", "get", {}),
        ("/payroll-setup/setups", "post", {"json": {
            "setup_code": "P4DENY_" + uuid4().hex[:8], "setup_name": "Denied",
        }}),
        (f"/payroll-setup/branches/{other_branch}/history", "get", {}),
    ):
        denied = await getattr(client, method)(path, headers=_auth(branch_viewer), **kwargs)
        assert denied.status_code == 403, denied.text
    driver = await _make_actor(
        db_conn, permissions=("payroll.view",), scope="AllCompanyBranches", driver=True,
    )
    denied_driver = await client.get(
        f"/payroll-setup/branches/{branch_id}/effective",
        params={"period_start_date": "2090-01-08"}, headers=_auth(driver),
    )
    assert denied_driver.status_code == 403, denied_driver.text
    denied_other = await client.get(
        f"/payroll-setup/branches/{other_branch}/history", headers=_auth(branch_viewer),
    )
    assert denied_other.status_code == 403, denied_other.text
    oda = await _make_actor(
        db_conn, permissions=("payroll.view",), scope="OwnDriverDataOnly",
        branch_id=branch_id,
    )
    denied_oda = await client.get(
        f"/payroll-setup/branches/{branch_id}/history", headers=_auth(oda),
    )
    assert denied_oda.status_code == 403, denied_oda.text

    withdrawn = await client.post(
        f"/payroll-setup/assignments/{second_aid}/withdraw", headers=headers,
        json={"reason": "withdraw test"},
    )
    assert withdrawn.status_code == 204, withdrawn.text
    assignments = await client.get(
        f"/payroll-setup/branches/{branch_id}/assignments", headers=headers,
    )
    assert assignments.status_code == 200, assignments.text
    retained = {row["assignment_id"]: row for row in assignments.json()}
    assert set(retained) == {initial_aid, second_aid}
    assert retained[second_aid]["withdrawn_at_utc"] is not None
    history = await client.get(
        f"/payroll-setup/branches/{branch_id}/history", headers=headers,
    )
    assert history.status_code == 200, history.text
    history_rows = {row["assignment_id"]: row for row in history.json()["assignments"]}
    assert history_rows[second_aid]["withdrawn_at_utc"] is not None
    assert history_rows[initial_aid]["effective_to_date"] is None
    retained_audit = (await db_conn.execute(text("""
        SELECT EventType FROM payroll.PayrollSetupPolicyAuditEvents
        WHERE PayrollSetupPolicyAuditEventID = :event_id
    """), {"event_id": audit_event_id})).scalar_one()
    assert retained_audit == "FutureVersionScheduled"
    restored = await client.get(
        f"/payroll-setup/branches/{branch_id}/effective",
        params={"period_start_date": "2090-01-08"}, headers=headers,
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["setup_id"] == source_id


@pytest.mark.asyncio
async def test_setup_mutation_audit_policy_error_mapping_and_transaction_rollback(
    client: httpx.AsyncClient, auth_token, db_conn, monkeypatch,
):
    token_headers = _auth(auth_token)
    unique = uuid4().hex[:10]
    create_response = await client.post(
        "/payroll-setup/setups",
        headers=token_headers,
        json={
            "setup_code": f"P4HTTP_{unique}",
            "setup_name": "Phase 4 HTTP setup",
            "description": None,
        },
    )
    assert create_response.status_code == 201, create_response.text
    created = create_response.json()
    assert created["setup_code"] == f"P4HTTP_{unique}"

    success_audit_count = (await db_conn.execute(text("""
        SELECT COUNT(*) FROM payroll.PayrollSetupPolicyAuditEvents
        WHERE CompanyID = (SELECT CompanyID FROM core.Companies WHERE CompanyCode = 'DEMO')
          AND EventType = 'SetupCreated' AND PayrollSetupID = :sid
    """), {"sid": created["setup_id"]})).scalar_one()
    assert success_audit_count == 1

    real_create_setup = policy.create_setup
    failed_code = f"P4ROLLBACK_{unique}"
    inserted_setup_ids: list[int] = []

    async def create_then_raise_policy_error(
        company_id, user_id, setup_code, setup_name, db, *, description=None,
    ):
        inserted_id = await real_create_setup(
            company_id, user_id, setup_code, setup_name, db,
            description=description,
        )
        inserted_setup_ids.append(inserted_id)
        raise PolicyError("SETUP_IN_USE", "Synthetic post-write conflict for rollback check")

    monkeypatch.setattr(policy, "create_setup", create_then_raise_policy_error)
    failed_response = await client.post(
        "/payroll-setup/setups",
        headers=token_headers,
        json={"setup_code": failed_code, "setup_name": "Must roll back"},
    )
    assert failed_response.status_code == 409
    assert failed_response.json() == {
        "detail": {
            "code": "SETUP_IN_USE",
            "message": "Synthetic post-write conflict for rollback check",
        }
    }

    failed_setup_count = (await db_conn.execute(text("""
        SELECT COUNT(*) FROM payroll.PayrollSetups
        WHERE CompanyID = (SELECT CompanyID FROM core.Companies WHERE CompanyCode = 'DEMO')
          AND SetupCode = :code
    """), {"code": failed_code})).scalar_one()
    assert len(inserted_setup_ids) == 1
    failed_audit_count = (await db_conn.execute(text("""
        SELECT COUNT(*) FROM payroll.PayrollSetupPolicyAuditEvents
        WHERE CompanyID = (SELECT CompanyID FROM core.Companies WHERE CompanyCode = 'DEMO')
          AND EventType = 'SetupCreated' AND PayrollSetupID = :sid
    """), {"sid": inserted_setup_ids[0]})).scalar_one()
    assert failed_setup_count == 0
    assert failed_audit_count == 0
