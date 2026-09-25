"""Focused Phase 5 tests for branch onboarding and canonical readiness."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg2
import pytest
import pytest_asyncio
from psycopg2 import sql as pg_sql
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.auth.security import create_access_token
from app.dashboard.service import _compute_setup_warnings
from app.dependencies import get_db
from app.main import app as real_app
from app.payroll_setup import policy as payroll_policy
from app.payroll_setup import readiness
from app.payroll_setup.errors import PolicyError
from app.settings import service as settings_service

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS = _ROOT / "migrations" / "sql"


@pytest_asyncio.fixture
async def phase5_onboarding_db(pg_instance):
    """Create a fresh migrated database and HTTP app for every Phase5 test."""
    database = "p5_onboard_" + uuid4().hex[:12]
    admin_dsn = dict(pg_instance.dsn())
    admin = psycopg2.connect(**admin_dsn)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(pg_sql.SQL("CREATE DATABASE {}").format(pg_sql.Identifier(database)))
    finally:
        admin.close()

    isolated_dsn = dict(pg_instance.dsn())
    isolated_dsn["database"] = database
    isolated = psycopg2.connect(**isolated_dsn)
    isolated.autocommit = True
    try:
        with isolated.cursor() as cursor:
            for migration in sorted(_MIGRATIONS.glob("*.sql")):
                cursor.execute(migration.read_text(encoding="utf-8"))
            cursor.execute("""
                INSERT INTO core.Companies (CompanyCode, CompanyName, Status, IsSuspended)
                VALUES (%s, 'Phase 5 Onboarding', 'Active', FALSE) RETURNING CompanyID
            """, ("P5_" + uuid4().hex[:10],))
            company_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO sec.Users (CompanyID, Username, DisplayName, IsActive, CanLogin)
                VALUES (%s, %s, 'Phase 5 Admin', TRUE, TRUE) RETURNING UserID
            """, (company_id, "p5_admin_" + uuid4().hex[:10]))
            user_id = cursor.fetchone()[0]
            cursor.execute("UPDATE core.Companies SET OwnerUserID = %s WHERE CompanyID = %s",
                           (user_id, company_id))
            cursor.execute("""
                INSERT INTO sec.Roles (RoleCode, RoleName, RoleLevel, IsSystemRole)
                VALUES (%s, 'Phase 5 Admin', 100, FALSE) RETURNING RoleID
            """, ("P5R_" + uuid4().hex[:10],))
            role_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO sec.CompanyRoles
                    (CompanyID, RoleCode, RoleName, RoleLevel, IsDefault,
                     IsProtected, IsCustom, IsActive)
                VALUES (%s, %s, 'Phase 5 Admin', 100, FALSE, FALSE, TRUE, TRUE)
                RETURNING CompanyRoleID
            """, (company_id, "P5C_" + uuid4().hex[:10]))
            company_role_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO sec.CompanyRolePermissions (CompanyRoleID, PermissionCode)
                SELECT %s, PermissionCode FROM sec.Permissions
                ON CONFLICT (CompanyRoleID, PermissionCode) DO NOTHING
            """, (company_role_id,))
            cursor.execute("""
                INSERT INTO sec.UserBranchRoles
                    (UserID, CompanyID, BranchID, RoleID, CompanyRoleID,
                     ScopeType, IsActive)
                VALUES (%s, %s, NULL, %s, %s, 'AllCompanyBranches', TRUE)
            """, (user_id, company_id, role_id, company_role_id))
            cursor.execute("""
                INSERT INTO core.Branches (CompanyID, BranchCode, BranchName, Status, IsDefault)
                VALUES (%s, 'P5BRANCH', 'Phase 5 Branch', 'Active', FALSE)
                RETURNING BranchID
            """, (company_id,))
            branch_id = cursor.fetchone()[0]
    finally:
        isolated.close()

    url = (f"postgresql+asyncpg://{isolated_dsn['user']}@{isolated_dsn['host']}"
           f":{isolated_dsn['port']}/{database}")
    engine = create_async_engine(url, echo=False)
    previous_override = real_app.dependency_overrides.get(get_db)

    async def override_get_db() -> AsyncConnection:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('lock_timeout', '5s', true)"))
            yield conn

    real_app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=real_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        try:
            yield SimpleNamespace(
                engine=engine, client=client, company_id=int(company_id),
                user_id=int(user_id), branch_id=int(branch_id),
                admin_token=create_access_token(int(user_id), int(company_id)),
                admin_dsn=admin_dsn, database=database,
            )
        finally:
            if previous_override is None:
                real_app.dependency_overrides.pop(get_db, None)
            else:
                real_app.dependency_overrides[get_db] = previous_override
            await engine.dispose()
            cleanup = psycopg2.connect(**admin_dsn)
            cleanup.autocommit = True
            try:
                with cleanup.cursor() as cursor:
                    cursor.execute(pg_sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                                   .format(pg_sql.Identifier(database)))
            finally:
                cleanup.close()


@pytest_asyncio.fixture
async def phase5_onboarding_conn(phase5_onboarding_db):
    """Autocommit seeding connection to the per-test disposable database."""
    async with phase5_onboarding_db.engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        yield conn


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _actor(db, *, permissions: tuple[str, ...], scope: str,
                 branch_id: int | None = None, driver: bool = False) -> str:
    suffix = uuid4().hex[:10]
    company_id = (await db.execute(text(
        "SELECT CompanyID FROM core.Companies"
    ))).scalar_one()
    user_id = (await db.execute(text("""
        INSERT INTO sec.Users (CompanyID, Username, DisplayName, IsActive, CanLogin)
        VALUES (:cid, :name, 'Phase 5 onboarding actor', TRUE, TRUE)
        RETURNING UserID
    """), {"cid": company_id, "name": "p5onboard_" + suffix})).scalar_one()
    role_id = (await db.execute(text("""
        INSERT INTO sec.Roles (RoleCode, RoleName, RoleLevel, IsSystemRole)
        VALUES (:code, 'Phase 5 onboarding actor', 20, FALSE) RETURNING RoleID
    """), {"code": "P5ONR_" + suffix})).scalar_one()
    if driver:
        company_role_id = (await db.execute(text("""
            SELECT CompanyRoleID FROM sec.CompanyRoles
            WHERE CompanyID = :cid AND RoleCode = 'DRIVER'
        """), {"cid": company_id})).scalar_one_or_none()
        if company_role_id is None:
            company_role_id = (await db.execute(text("""
                INSERT INTO sec.CompanyRoles
                    (CompanyID, RoleCode, RoleName, RoleLevel, IsDefault,
                     IsProtected, IsCustom, IsActive)
                VALUES (:cid, 'DRIVER', 'Phase 5 Driver', 10, FALSE, TRUE, FALSE, TRUE)
                RETURNING CompanyRoleID
            """), {"cid": company_id})).scalar_one()
    else:
        company_role_id = (await db.execute(text("""
            INSERT INTO sec.CompanyRoles
                (CompanyID, RoleCode, RoleName, RoleLevel, IsDefault,
                 IsProtected, IsCustom, IsActive)
            VALUES (:cid, :code, 'Phase 5 onboarding actor', 20, FALSE, FALSE, TRUE, TRUE)
            RETURNING CompanyRoleID
        """), {"cid": company_id, "code": "P5ONC_" + suffix})).scalar_one()
        for permission in permissions:
            await db.execute(text("""
                INSERT INTO sec.CompanyRolePermissions (CompanyRoleID, PermissionCode)
                VALUES (:rid, :permission)
            """), {"rid": company_role_id, "permission": permission})
    await db.execute(text("""
        INSERT INTO sec.UserBranchRoles
            (UserID, CompanyID, BranchID, RoleID, CompanyRoleID, ScopeType, IsActive)
        VALUES (:uid, :cid, :bid, :rid, :crid, :scope, TRUE)
    """), {
        "uid": user_id, "cid": company_id,
        "bid": branch_id if scope in ("SpecificBranch", "OwnDriverDataOnly") else None,
        "rid": role_id, "crid": company_role_id, "scope": scope,
    })
    return create_access_token(int(user_id), int(company_id))


async def _create_published_setup(
    client, token: str, marker: str, *, frequency: str = "Week",
    custom_interval_days: int | None = None,
) -> int:
    created = await client.post("/payroll-setup/setups", headers=_auth(token), json={
        "setup_code": "P5ON_" + marker[:10], "setup_name": "Phase 5 onboarding",
    })
    assert created.status_code == 201, created.text
    setup_id = created.json()["setup_id"]
    draft = await client.post(
        f"/payroll-setup/setups/{setup_id}/drafts", headers=_auth(token), json={
            "payroll_frequency": frequency, "anchor_start_date": "2090-01-01",
            "normal_days_off_mask": 0,
            "custom_interval_days": custom_interval_days,
        },
    )
    assert draft.status_code == 201, draft.text
    published = await client.post(
        f"/payroll-setup/setups/{setup_id}/drafts/{draft.json()['version_id']}/publish",
        headers=_auth(token), json={"effective_from_date": "2090-01-01"},
    )
    assert published.status_code == 201, published.text
    return setup_id


async def _wait_until_blocked(engine, backend_pid: int) -> None:
    async with engine.connect() as observer:
        async def poll():
            while True:
                blocked = await observer.execute(text(
                    "SELECT cardinality(pg_blocking_pids(:pid)) > 0"
                ), {"pid": backend_pid})
                if blocked.scalar_one():
                    return
                await asyncio.sleep(0.02)

        await asyncio.wait_for(poll(), timeout=5)


@pytest.mark.asyncio
async def test_branch_create_permission_contract_and_no_legacy_authority(
    phase5_onboarding_db, phase5_onboarding_conn,
):
    client = phase5_onboarding_db.client
    auth_token = phase5_onboarding_db.admin_token
    db_conn = phase5_onboarding_conn
    marker = uuid4().hex
    company_id = phase5_onboarding_db.company_id
    old_default = (await db_conn.execute(text(
        "SELECT DefaultPayrollSetupID FROM core.Companies WHERE CompanyID = :cid"
    ), {"cid": company_id})).scalar_one_or_none()
    setup_id = await _create_published_setup(client, auth_token, marker)
    set_default = await client.put("/payroll-setup/default", headers=_auth(auth_token),
                                   json={"setup_id": setup_id})
    assert set_default.status_code == 204, set_default.text
    try:
        create_only = await _actor(db_conn, permissions=("branches.create",),
                                   scope="AllCompanyBranches")
        assign_only = await _actor(db_conn, permissions=("payroll_setup.assign",),
                                   scope="AllCompanyBranches")
        manager = await _actor(db_conn, permissions=("setup.manage",),
                               scope="AllCompanyBranches")
        branch_scoped = await _actor(
            db_conn, permissions=("branches.create", "payroll_setup.assign"),
            scope="SpecificBranch", branch_id=phase5_onboarding_db.branch_id,
        )
        driver = await _actor(
            db_conn, permissions=("branches.create", "payroll_setup.assign"),
            scope="AllCompanyBranches", driver=True,
        )
        own_driver_data = await _actor(
            db_conn, permissions=("branches.create", "payroll_setup.assign"),
            scope="OwnDriverDataOnly", branch_id=phase5_onboarding_db.branch_id,
        )
        missing_assign = await client.post("/settings/branches", headers=_auth(create_only), json={
            "branch_name": "Phase5 denied dated " + marker[:8],
            "branch_code": "P5D" + marker[:6], "first_payroll_start_date": "2090-01-01",
        })
        assert missing_assign.status_code == 403
        for token in (manager, assign_only, branch_scoped, driver, own_driver_data):
            denied = await client.post("/settings/branches", headers=_auth(token), json={
                "branch_name": "Phase5 denied " + uuid4().hex[:8],
                "branch_code": "P5X" + uuid4().hex[:6],
            })
            assert denied.status_code == 403, denied.text
        assert (await db_conn.execute(text("""
            SELECT COUNT(*) FROM core.Branches
            WHERE CompanyID = :cid AND BranchCode LIKE 'P5D%'
        """), {"cid": company_id})).scalar_one() == 0

        branch_code = "P5L" + marker[:7]
        branch = await client.post("/settings/branches", headers=_auth(create_only), json={
            "branch_name": "Phase5 no legacy authority " + marker[:8],
            "branch_code": branch_code,
        })
        assert branch.status_code == 201, branch.text
        branch_id = branch.json()["branch_id"]
        assert (await db_conn.execute(text("""
            SELECT COUNT(*) FROM payroll.BranchPayrollSetupAssignments
            WHERE CompanyID = :cid AND BranchID = :bid
        """), {"cid": company_id, "bid": branch_id})).scalar_one() == 0
        assert (await db_conn.execute(text("""
            SELECT COUNT(*) FROM payroll.BranchPayrollSettings
            WHERE CompanyID = :cid AND BranchID = :bid
        """), {"cid": company_id, "bid": branch_id})).scalar_one() == 0
        assert (await db_conn.execute(text(
            "SELECT DefaultPayrollSetupID FROM core.Companies WHERE CompanyID = :cid"
        ), {"cid": company_id})).scalar_one() == setup_id
        await db_conn.execute(text("""
            INSERT INTO payroll.BranchPayrollSettings
                (CompanyID, BranchID, PayrollFrequency, AnchorStartDate, IsActive)
            VALUES (:cid, :bid, 'Week', '2090-01-01', TRUE)
        """), {"cid": company_id, "bid": branch_id})
        warnings = await _compute_setup_warnings(
            db_conn, company_id, False, [branch_id], {},
            has_setup=True, has_payroll=False, has_rates=False,
        )
        assert any(w.code == "BRANCH_NO_PAYROLL_SETTINGS" and w.branch_id == branch_id
                   for w in warnings)
        reread = await client.get(f"/settings/branches/{branch_id}", headers=_auth(create_only))
        assert reread.status_code == 200, reread.text
        assert reread.json()["payroll_setup_done"] is False
        assert reread.json()["schedule_readiness_reason"] is None
        with_view = await _actor(db_conn, permissions=("payroll.view",),
                                 scope="SpecificBranch", branch_id=branch_id)
        detailed = await client.get(f"/settings/branches/{branch_id}", headers=_auth(with_view))
        assert detailed.status_code == 200, detailed.text
        assert detailed.json()["payroll_setup_done"] is False
        assert detailed.json()["schedule_readiness_reason"] == "NO_ASSIGNMENT"
        assert "setup_id" not in detailed.json()
        assert "version_id" not in detailed.json()
    finally:
        restored = await client.put("/payroll-setup/default", headers=_auth(auth_token),
                                    json={"setup_id": old_default})
        assert restored.status_code == 204, restored.text


@pytest.mark.asyncio
async def test_existing_branch_zero_legacy_setup_then_policy_assignment(
    phase5_onboarding_db, phase5_onboarding_conn,
):
    fixture = phase5_onboarding_db
    client, token, db = fixture.client, fixture.admin_token, phase5_onboarding_conn
    company_id, branch_id = fixture.company_id, fixture.branch_id
    baseline = (await db.execute(text("""
        SELECT
            (SELECT COUNT(*) FROM payroll.PayrollSetups WHERE CompanyID = :cid) AS Setups,
            (SELECT COUNT(*) FROM payroll.PayrollSetupVersions WHERE CompanyID = :cid) AS Versions,
            (SELECT COUNT(*) FROM payroll.BranchPayrollSetupAssignments
             WHERE CompanyID = :cid AND BranchID = :bid) AS Assignments,
            (SELECT COUNT(*) FROM payroll.BranchPayrollSettings
             WHERE CompanyID = :cid AND BranchID = :bid) AS Legacy
    """), {"cid": company_id, "bid": branch_id})).mappings().one()
    assert tuple(baseline.values()) == (0, 0, 0, 0)

    setup_id = await _create_published_setup(client, token, uuid4().hex)
    selected_default = await client.put("/payroll-setup/default", headers=_auth(token),
                                        json={"setup_id": setup_id})
    assert selected_default.status_code == 204, selected_default.text
    assigned = await client.post(
        f"/payroll-setup/branches/{branch_id}/assignments",
        headers=_auth(token), json={
            "setup_id": setup_id, "effective_from_date": "2090-01-01",
        },
    )
    assert assigned.status_code == 201, assigned.text
    settings = await client.get(f"/settings/branches/{branch_id}", headers=_auth(token))
    assert settings.status_code == 200, settings.text
    assert settings.json()["payroll_setup_done"] is True
    assert settings.json()["schedule_readiness_reason"] == "READY"
    final_counts = (await db.execute(text("""
        SELECT
            (SELECT COUNT(*) FROM payroll.PayrollSetups WHERE CompanyID = :cid) AS Setups,
            (SELECT COUNT(*) FROM payroll.PayrollSetupVersions WHERE CompanyID = :cid) AS Versions,
            (SELECT COUNT(*) FROM payroll.BranchPayrollSetupAssignments
             WHERE CompanyID = :cid AND BranchID = :bid) AS Assignments,
            (SELECT COUNT(*) FROM payroll.BranchPayrollSettings
             WHERE CompanyID = :cid AND BranchID = :bid) AS Legacy
    """), {"cid": company_id, "bid": branch_id})).mappings().one()
    assert tuple(final_counts.values()) == (1, 1, 1, 0)


@pytest.mark.asyncio
async def test_branch_onboarding_default_null_and_assignment_readiness(
    phase5_onboarding_db, phase5_onboarding_conn,
):
    client = phase5_onboarding_db.client
    auth_token = phase5_onboarding_db.admin_token
    db_conn = phase5_onboarding_conn
    marker = uuid4().hex
    company_id = phase5_onboarding_db.company_id
    old_default = (await db_conn.execute(text(
        "SELECT DefaultPayrollSetupID FROM core.Companies WHERE CompanyID = :cid"
    ), {"cid": company_id})).scalar_one_or_none()
    actor = await _actor(db_conn, permissions=("branches.create", "payroll_setup.assign"),
                         scope="AllCompanyBranches")
    setup_id = await _create_published_setup(client, auth_token, marker)
    try:
        cleared = await client.put("/payroll-setup/default", headers=_auth(auth_token),
                                   json={"setup_id": None})
        assert cleared.status_code == 204, cleared.text
        create_only = await _actor(db_conn, permissions=("branches.create",),
                                   scope="AllCompanyBranches")
        no_default_missing_assign = await client.post(
            "/settings/branches", headers=_auth(create_only), json={
                "branch_name": "Phase5 no default denied " + marker[:8],
                "branch_code": "P5Q" + marker[:7],
                "first_payroll_start_date": "2090-01-01",
            },
        )
        assert no_default_missing_assign.status_code == 403, no_default_missing_assign.text
        assert (await db_conn.execute(text("""
            SELECT COUNT(*) FROM core.Branches WHERE CompanyID = :cid AND BranchCode = :code
        """), {"cid": company_id, "code": "P5Q" + marker[:7]})).scalar_one() == 0
        unassigned = await client.post("/settings/branches", headers=_auth(actor), json={
            "branch_name": "Phase5 no default " + marker[:8],
            "branch_code": "P5N" + marker[:7],
            "first_payroll_start_date": "2090-01-01",
        })
        assert unassigned.status_code == 201, unassigned.text
        assert unassigned.json()["payroll_setup_done"] is False
        assert unassigned.json()["schedule_readiness_reason"] == "NO_COMPANY_DEFAULT"
        no_default_branch = unassigned.json()["branch_id"]
        assert (await db_conn.execute(text("""
            SELECT COUNT(*) FROM payroll.BranchPayrollSetupAssignments
            WHERE CompanyID = :cid AND BranchID = :bid
        """), {"cid": company_id, "bid": no_default_branch})).scalar_one() == 0

        restored_setup = await client.put("/payroll-setup/default", headers=_auth(auth_token),
                                          json={"setup_id": setup_id})
        assert restored_setup.status_code == 204, restored_setup.text
        assigned = await client.post("/settings/branches", headers=_auth(actor), json={
            "branch_name": "Phase5 assigned " + marker[:8],
            "branch_code": "P5A" + marker[:7],
            "first_payroll_start_date": "2090-01-01",
        })
        assert assigned.status_code == 201, assigned.text
        assert assigned.json()["payroll_setup_done"] is True
        assert assigned.json()["schedule_readiness_reason"] == "READY"
        branch_id = assigned.json()["branch_id"]
        row = (await db_conn.execute(text("""
            SELECT PayrollSetupID, EffectiveFromDate, WithdrawnAtUtc
            FROM payroll.BranchPayrollSetupAssignments
            WHERE CompanyID = :cid AND BranchID = :bid
        """), {"cid": company_id, "bid": branch_id})).mappings().one()
        assert row["payrollsetupid"] == setup_id
        assert row["effectivefromdate"] == date(2090, 1, 1)
        assert row["withdrawnatutc"] is None

        future_draft = await client.post(
            f"/payroll-setup/setups/{setup_id}/drafts", headers=_auth(auth_token), json={
                "payroll_frequency": "Week", "anchor_start_date": "2090-01-01",
                "normal_days_off_mask": 0,
            },
        )
        assert future_draft.status_code == 201, future_draft.text
        future_publish = await client.post(
            f"/payroll-setup/setups/{setup_id}/drafts/{future_draft.json()['version_id']}/publish",
            headers=_auth(auth_token), json={"effective_from_date": "2090-01-08"},
        )
        assert future_publish.status_code == 201, future_publish.text
        future_readiness = await client.get(
            f"/settings/branches/{branch_id}", headers=_auth(auth_token),
        )
        assert future_readiness.status_code == 200, future_readiness.text
        assert future_readiness.json()["payroll_setup_done"] is True
        assert future_readiness.json()["schedule_readiness_reason"] == "READY"

        later_boundaries = (
            ("Week", None, "2090-01-08"),
            ("Biweek", None, "2090-01-15"),
            ("Month", None, "2090-02-01"),
            ("Custom", 10, "2090-01-11"),
        )
        for frequency, interval, boundary in later_boundaries:
            cadence_setup = await _create_published_setup(
                client, auth_token, frequency + marker[:8],
                frequency=frequency, custom_interval_days=interval,
            )
            changed_default = await client.put(
                "/payroll-setup/default", headers=_auth(auth_token),
                json={"setup_id": cadence_setup},
            )
            assert changed_default.status_code == 204, changed_default.text
            cadence_branch = await client.post("/settings/branches", headers=_auth(actor), json={
                "branch_name": f"Phase5 {frequency} cadence " + marker[:6],
                "branch_code": "P5" + frequency[:3].upper() + marker[:6],
                "first_payroll_start_date": boundary,
            })
            assert cadence_branch.status_code == 201, cadence_branch.text
            assert cadence_branch.json()["payroll_setup_done"] is True
            assert cadence_branch.json()["schedule_readiness_reason"] == "READY"

        invalid_code = "P5I" + marker[:7]
        invalid = await client.post("/settings/branches", headers=_auth(actor), json={
            "branch_name": "Phase5 invalid cadence " + marker[:8],
            "branch_code": invalid_code, "first_payroll_start_date": "2090-01-02",
        })
        assert invalid.status_code == 409, invalid.text
        assert (await db_conn.execute(text("""
            SELECT COUNT(*) FROM core.Branches WHERE CompanyID = :cid AND BranchCode = :code
        """), {"cid": company_id, "code": invalid_code})).scalar_one() == 0
    finally:
        restored = await client.put("/payroll-setup/default", headers=_auth(auth_token),
                                    json={"setup_id": old_default})
        assert restored.status_code == 204, restored.text


@pytest.mark.asyncio
async def test_branch_onboarding_policy_audit_failure_rolls_back_all_branch_effects(
    phase5_onboarding_db, phase5_onboarding_conn, monkeypatch,
):
    client = phase5_onboarding_db.client
    auth_token = phase5_onboarding_db.admin_token
    db_conn = phase5_onboarding_conn
    marker = uuid4().hex
    company_id = phase5_onboarding_db.company_id
    setup_id = await _create_published_setup(client, auth_token, marker)
    old_default = (await db_conn.execute(text(
        "SELECT DefaultPayrollSetupID FROM core.Companies WHERE CompanyID = :cid"
    ), {"cid": company_id})).scalar_one_or_none()
    await client.put("/payroll-setup/default", headers=_auth(auth_token),
                     json={"setup_id": setup_id})
    actor = await _actor(db_conn, permissions=("branches.create", "payroll_setup.assign"),
                         scope="AllCompanyBranches")
    branch_code = "P5R" + marker[:7]

    original_policy_audit = payroll_policy.write_policy_audit

    async def fail_assignment_audit(db, **kwargs):
        if kwargs.get("event_type") == "BranchAssigned":
            raise RuntimeError("Phase5 injected BranchAssigned audit failure")
        await original_policy_audit(db, **kwargs)

    original_audit = settings_service._write_settings_audit
    captured_branch_ids: list[str] = []

    async def capture_audit(*args, **kwargs):
        if kwargs.get("action_code") == "BRANCH_CREATED":
            captured_branch_ids.append(kwargs["entity_id"])
        await original_audit(*args, **kwargs)

    monkeypatch.setattr(payroll_policy, "write_policy_audit", fail_assignment_audit)
    monkeypatch.setattr(settings_service, "_write_settings_audit", capture_audit)
    try:
        with pytest.raises(RuntimeError, match="Phase5 injected BranchAssigned audit failure"):
            await client.post("/settings/branches", headers=_auth(actor), json={
                "branch_name": "Phase5 rollback " + marker[:8],
                "branch_code": branch_code, "first_payroll_start_date": "2090-01-01",
            })
        assert len(captured_branch_ids) == 1
        branch_id = captured_branch_ids[0]
        assert (await db_conn.execute(text("""
            SELECT COUNT(*) FROM core.Branches WHERE CompanyID = :cid AND BranchCode = :code
        """), {"cid": company_id, "code": branch_code})).scalar_one() == 0
        assert (await db_conn.execute(text("""
            SELECT COUNT(*) FROM audit.AuditLog
            WHERE CompanyID = :cid AND EntityName = 'Branches' AND EntityID = :eid
        """), {"cid": company_id, "eid": branch_id})).scalar_one() == 0
        assert (await db_conn.execute(text("""
            SELECT COUNT(*) FROM payroll.BranchPayrollSetupAssignments a
            WHERE a.CompanyID = :cid AND a.BranchID = :bid
        """), {"cid": company_id, "bid": int(branch_id)})).scalar_one() == 0
        assert (await db_conn.execute(text("""
            SELECT COUNT(*) FROM payroll.PayrollSetupPolicyAuditEvents
            WHERE CompanyID = :cid AND BranchID = :bid AND EventType = 'BranchAssigned'
        """), {"cid": company_id, "bid": int(branch_id)})).scalar_one() == 0
        assert (await db_conn.execute(text("""
            SELECT COUNT(*) FROM payroll.StatusRateColumns
            WHERE CompanyID = :cid AND BranchID = :bid
        """), {"cid": company_id, "bid": int(branch_id)})).scalar_one() == 0
    finally:
        monkeypatch.undo()
        restored = await client.put("/payroll-setup/default", headers=_auth(auth_token),
                                    json={"setup_id": old_default})
        assert restored.status_code == 204, restored.text


@pytest.mark.asyncio
async def test_concurrent_onboarding_uses_company_default_lock(
    phase5_onboarding_db, phase5_onboarding_conn,
):
    client = phase5_onboarding_db.client
    auth_token = phase5_onboarding_db.admin_token
    db_conn = phase5_onboarding_conn
    marker = uuid4().hex
    company_id = phase5_onboarding_db.company_id
    old_default = (await db_conn.execute(text(
        "SELECT DefaultPayrollSetupID FROM core.Companies WHERE CompanyID = :cid"
    ), {"cid": company_id})).scalar_one_or_none()
    setup_id = await _create_published_setup(client, auth_token, marker)
    set_default = await client.put("/payroll-setup/default", headers=_auth(auth_token),
                                   json={"setup_id": setup_id})
    assert set_default.status_code == 204, set_default.text
    actor = await _actor(db_conn, permissions=("branches.create", "payroll_setup.assign"),
                         scope="AllCompanyBranches")
    try:
        async def create(suffix: str):
            return await client.post("/settings/branches", headers=_auth(actor), json={
                "branch_name": f"Phase5 concurrent {suffix} {marker[:6]}",
                "branch_code": f"P5C{suffix}{marker[:6]}",
                "first_payroll_start_date": "2090-01-01",
            })

        first, second = await asyncio.wait_for(
            asyncio.gather(create("A"), create("B")), timeout=30,
        )
        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        branch_ids = [first.json()["branch_id"], second.json()["branch_id"]]
        rows = (await db_conn.execute(text("""
            SELECT BranchID, PayrollSetupID FROM payroll.BranchPayrollSetupAssignments
            WHERE CompanyID = :cid AND BranchID = ANY(:bids)
            ORDER BY BranchID
        """), {"cid": company_id, "bids": branch_ids})).mappings().all()
        assert len(rows) == 2
        assert {row["payrollsetupid"] for row in rows} == {setup_id}
    finally:
        restored = await client.put("/payroll-setup/default", headers=_auth(auth_token),
                                    json={"setup_id": old_default})
        assert restored.status_code == 204, restored.text


@pytest.mark.asyncio
async def test_onboarding_serializes_with_company_default_change(
    phase5_onboarding_db, phase5_onboarding_conn, monkeypatch,
):
    fixture = phase5_onboarding_db
    client, token, db = fixture.client, fixture.admin_token, phase5_onboarding_conn
    marker = uuid4().hex
    setup_a = await _create_published_setup(client, token, "A" + marker[:9])
    setup_b = await _create_published_setup(client, token, "B" + marker[:9])
    actor = await _actor(db, permissions=("branches.create", "payroll_setup.assign"),
                         scope="AllCompanyBranches")
    await client.put("/payroll-setup/default", headers=_auth(token),
                     json={"setup_id": setup_a})
    held = asyncio.Event()
    default_lock_attempted = asyncio.Event()
    release = asyncio.Event()
    competitor_pid: list[int] = []
    original_service_lock = settings_service.lock_company
    original_policy_lock = payroll_policy.lock_company

    async def pause_after_onboarding_company_lock(company_id, conn):
        await original_service_lock(company_id, conn)
        if asyncio.current_task().get_name() == "p5-onboard-default":
            held.set()
            await release.wait()

    async def observe_default_lock(company_id, conn):
        if asyncio.current_task().get_name() == "p5-change-default":
            competitor_pid.append((await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one())
            default_lock_attempted.set()
        await original_policy_lock(company_id, conn)

    monkeypatch.setattr(settings_service, "lock_company", pause_after_onboarding_company_lock)
    monkeypatch.setattr(payroll_policy, "lock_company", observe_default_lock)
    onboard = asyncio.create_task(client.post("/settings/branches", headers=_auth(actor), json={
        "branch_name": "P5 default race " + marker[:8], "branch_code": "P5D" + marker[:7],
        "first_payroll_start_date": "2090-01-01",
    }), name="p5-onboard-default")
    change = None
    try:
        await asyncio.wait_for(held.wait(), timeout=5)
        change = asyncio.create_task(client.put(
            "/payroll-setup/default", headers=_auth(token), json={"setup_id": setup_b},
        ), name="p5-change-default")
        await asyncio.wait_for(default_lock_attempted.wait(), timeout=5)
        await _wait_until_blocked(fixture.engine, competitor_pid[0])
    finally:
        release.set()
    onboarded, changed = await asyncio.wait_for(asyncio.gather(onboard, change), timeout=20)
    assert onboarded.status_code == 201, onboarded.text
    assert changed.status_code == 204, changed.text
    branch_id = onboarded.json()["branch_id"]
    assigned = (await db.execute(text("""
        SELECT PayrollSetupID FROM payroll.BranchPayrollSetupAssignments
        WHERE CompanyID = :cid AND BranchID = :bid
    """), {"cid": fixture.company_id, "bid": branch_id})).scalar_one()
    final_default = (await db.execute(text(
        "SELECT DefaultPayrollSetupID FROM core.Companies WHERE CompanyID = :cid"
    ), {"cid": fixture.company_id})).scalar_one()
    assert assigned == setup_a
    assert final_default == setup_b


@pytest.mark.asyncio
async def test_onboarding_serializes_with_version_replacement(
    phase5_onboarding_db, phase5_onboarding_conn, monkeypatch,
):
    fixture = phase5_onboarding_db
    client, token, db = fixture.client, fixture.admin_token, phase5_onboarding_conn
    marker = uuid4().hex
    setup_id = await _create_published_setup(client, token, marker)
    old_version_id = (await db.execute(text("""
        SELECT PayrollSetupVersionID FROM payroll.PayrollSetupVersions
        WHERE PayrollSetupID = :sid AND LifecycleState = 'Published'
    """), {"sid": setup_id})).scalar_one()
    draft = await client.post(f"/payroll-setup/setups/{setup_id}/drafts",
                              headers=_auth(token), json={
        "payroll_frequency": "Week", "anchor_start_date": "2090-01-01",
        "normal_days_off_mask": 0,
    })
    assert draft.status_code == 201, draft.text
    actor = await _actor(db, permissions=("branches.create", "payroll_setup.assign"),
                         scope="AllCompanyBranches")
    await client.put("/payroll-setup/default", headers=_auth(token),
                     json={"setup_id": setup_id})
    held = asyncio.Event()
    publish_lock_attempted = asyncio.Event()
    release = asyncio.Event()
    competitor_pid: list[int] = []
    original_service_locks = settings_service.lock_setups
    original_policy_locks = payroll_policy.lock_setups

    async def pause_after_onboarding_setup_lock(company_id, setup_ids, conn):
        await original_service_locks(company_id, setup_ids, conn)
        if asyncio.current_task().get_name() == "p5-onboard-version":
            held.set()
            await release.wait()

    async def observe_publish_setup_lock(company_id, setup_ids, conn):
        if asyncio.current_task().get_name() == "p5-publisher":
            competitor_pid.append((await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one())
            publish_lock_attempted.set()
        await original_policy_locks(company_id, setup_ids, conn)

    monkeypatch.setattr(settings_service, "lock_setups", pause_after_onboarding_setup_lock)
    monkeypatch.setattr(payroll_policy, "lock_setups", observe_publish_setup_lock)
    onboard = asyncio.create_task(client.post("/settings/branches", headers=_auth(actor), json={
        "branch_name": "P5 publish race " + marker[:8], "branch_code": "P5V" + marker[:7],
        "first_payroll_start_date": "2090-01-01",
    }), name="p5-onboard-version")
    publisher = None
    try:
        await asyncio.wait_for(held.wait(), timeout=5)
        publisher = asyncio.create_task(client.post(
            f"/payroll-setup/setups/{setup_id}/drafts/{draft.json()['version_id']}/publish",
            headers=_auth(token), json={"effective_from_date": "2090-01-01",
                                        "replaces_version_id": old_version_id},
        ), name="p5-publisher")
        await asyncio.wait_for(publish_lock_attempted.wait(), timeout=5)
        await _wait_until_blocked(fixture.engine, competitor_pid[0])
    finally:
        release.set()
    onboarded, published = await asyncio.wait_for(asyncio.gather(onboard, publisher), timeout=20)
    assert onboarded.status_code == 201, onboarded.text
    assert published.status_code == 201, published.text
    assert onboarded.json()["payroll_setup_done"] is True
    versions = (await db.execute(text("""
        SELECT LifecycleState, ReplacesVersionID FROM payroll.PayrollSetupVersions
        WHERE PayrollSetupID = :sid ORDER BY PayrollSetupVersionID
    """), {"sid": setup_id})).mappings().all()
    assert len(versions) == 2
    assert versions[1]["lifecyclestate"] == "Published"
    assert versions[1]["replacesversionid"] == old_version_id


@pytest.mark.asyncio
async def test_onboarding_serializes_with_setup_archive(
    phase5_onboarding_db, phase5_onboarding_conn, monkeypatch,
):
    fixture = phase5_onboarding_db
    client, token, db = fixture.client, fixture.admin_token, phase5_onboarding_conn
    marker = uuid4().hex
    setup_id = await _create_published_setup(client, token, marker)
    actor = await _actor(db, permissions=("branches.create", "payroll_setup.assign"),
                         scope="AllCompanyBranches")
    await client.put("/payroll-setup/default", headers=_auth(token),
                     json={"setup_id": setup_id})
    held = asyncio.Event()
    archive_lock_attempted = asyncio.Event()
    release = asyncio.Event()
    competitor_pid: list[int] = []
    original_service_locks = settings_service.lock_setups
    original_policy_locks = payroll_policy.lock_setups

    async def pause_after_onboarding_setup_lock(company_id, setup_ids, conn):
        await original_service_locks(company_id, setup_ids, conn)
        if asyncio.current_task().get_name() == "p5-onboard-archive":
            held.set()
            await release.wait()

    async def observe_archive_setup_lock(company_id, setup_ids, conn):
        if asyncio.current_task().get_name() == "p5-archiver":
            competitor_pid.append((await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one())
            archive_lock_attempted.set()
        await original_policy_locks(company_id, setup_ids, conn)

    monkeypatch.setattr(settings_service, "lock_setups", pause_after_onboarding_setup_lock)
    monkeypatch.setattr(payroll_policy, "lock_setups", observe_archive_setup_lock)
    onboard = asyncio.create_task(client.post("/settings/branches", headers=_auth(actor), json={
        "branch_name": "P5 archive race " + marker[:8], "branch_code": "P5X" + marker[:7],
        "first_payroll_start_date": "2090-01-01",
    }), name="p5-onboard-archive")
    archiver = None
    try:
        await asyncio.wait_for(held.wait(), timeout=5)
        archiver = asyncio.create_task(client.post(
            f"/payroll-setup/setups/{setup_id}/archive", headers=_auth(token),
        ), name="p5-archiver")
        await asyncio.wait_for(archive_lock_attempted.wait(), timeout=5)
        await _wait_until_blocked(fixture.engine, competitor_pid[0])
    finally:
        release.set()
    onboarded, archived = await asyncio.wait_for(asyncio.gather(onboard, archiver), timeout=20)
    assert onboarded.status_code == 201, onboarded.text
    assert archived.status_code == 409, archived.text
    assert archived.json()["detail"]["code"] == "DEFAULT_SETUP_IN_USE"
    state = (await db.execute(text("""
        SELECT s.Status, COUNT(a.BranchPayrollSetupAssignmentID) AS AssignmentCount
        FROM payroll.PayrollSetups s
        LEFT JOIN payroll.BranchPayrollSetupAssignments a
          ON a.PayrollSetupID = s.PayrollSetupID AND a.WithdrawnAtUtc IS NULL
        WHERE s.CompanyID = :cid AND s.PayrollSetupID = :sid
        GROUP BY s.Status
    """), {"cid": fixture.company_id, "sid": setup_id})).mappings().one()
    assert state["status"] == "Active"
    assert state["assignmentcount"] == 1


@pytest.mark.asyncio
async def test_no_applicable_version_and_inactive_branch_both_roll_back(
    phase5_onboarding_db, phase5_onboarding_conn,
):
    fixture = phase5_onboarding_db
    client, token, db = fixture.client, fixture.admin_token, phase5_onboarding_conn
    marker = uuid4().hex
    actor = await _actor(db, permissions=("branches.create", "payroll_setup.assign"),
                         scope="AllCompanyBranches")
    setup = await client.post("/payroll-setup/setups", headers=_auth(token), json={
        "setup_code": "P5NV_" + marker[:10], "setup_name": "No published version",
    })
    assert setup.status_code == 201, setup.text
    setup_id = setup.json()["setup_id"]
    default = await client.put("/payroll-setup/default", headers=_auth(token),
                               json={"setup_id": setup_id})
    assert default.status_code == 204, default.text
    for suffix, extra, expected in (
        ("N", {"status": "Active"}, "VERSION_NOT_FOUND"),
        ("I", {"status": "Inactive"}, "BRANCH_NOT_OPERATIONAL"),
    ):
        branch_code = "P5Z" + suffix + marker[:6]
        response = await client.post("/settings/branches", headers=_auth(actor), json={
            "branch_name": f"Phase5 rollback {suffix} " + marker[:8],
            "branch_code": branch_code, "first_payroll_start_date": "2090-01-01",
            **extra,
        })
        expected_status = 404 if expected == "VERSION_NOT_FOUND" else 409
        assert response.status_code == expected_status, response.text
        assert response.json()["detail"]["code"] == expected
        assert (await db.execute(text("""
            SELECT COUNT(*) FROM core.Branches WHERE CompanyID = :cid AND BranchCode = :code
        """), {"cid": fixture.company_id, "code": branch_code})).scalar_one() == 0
        assert (await db.execute(text("""
            SELECT COUNT(*) FROM audit.AuditLog
            WHERE CompanyID = :cid AND ActionCode = 'BRANCH_CREATED'
              AND NewValueJson::jsonb ->> 'branch_code' = :code
        """), {"cid": fixture.company_id, "code": branch_code})).scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("policy_code", "expected"), [
    ("ASSIGNMENT_NOT_FOUND", "NO_ASSIGNMENT"),
    ("VERSION_NOT_FOUND", "NO_PUBLISHED_VERSION"),
    ("AUTHORITY_BOUNDARY_CROSSING", "AUTHORITY_BOUNDARY_CONFLICT"),
    ("SETUP_NOT_ACTIVE", "SETUP_NOT_ACTIVE"),
    ("INVALID_SCHEDULE_BOUNDARY", "INVALID_SCHEDULE_BOUNDARY"),
    ("BRANCH_NOT_OPERATIONAL", "BRANCH_NOT_OPERATIONAL"),
])
async def test_readiness_reason_codes_are_normalized(
    phase5_onboarding_db, phase5_onboarding_conn, monkeypatch,
    policy_code, expected,
):
    db_conn = phase5_onboarding_conn
    company_id, branch_id = (await db_conn.execute(text("""
        SELECT c.CompanyID, b.BranchID FROM core.Companies c
        JOIN core.Branches b ON b.CompanyID = c.CompanyID
        WHERE b.BranchCode = 'P5BRANCH'
    """))).one()

    async def fail_resolution(*args, **kwargs):
        raise PolicyError(policy_code, "synthetic readiness result")

    monkeypatch.setattr(readiness, "resolve_payroll_setup_version", fail_resolution)
    ready, reason = await readiness.branch_schedule_readiness(
        int(company_id), int(branch_id), db_conn, period_start_date=date(2090, 1, 1),
    )
    assert ready is False
    assert reason == expected
