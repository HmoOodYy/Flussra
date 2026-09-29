"""Phase 6 U0 tests: readiness detail (reason + evaluated period start date)."""

from __future__ import annotations

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
from app.dependencies import get_db
from app.main import create_app
from app.payroll_setup import readiness
from app.payroll_setup.errors import PolicyError

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS = _ROOT / "migrations" / "sql"
real_app = create_app()


@pytest_asyncio.fixture
async def phase6_ready_db(pg_instance):
    """Create a fresh migrated database and HTTP app for every Phase6 U0 test."""
    database = "p6_ready_" + uuid4().hex[:12]
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
                VALUES (%s, 'Phase 6 Readiness Date', 'Active', FALSE) RETURNING CompanyID
            """, ("P6_" + uuid4().hex[:10],))
            company_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO sec.Users (CompanyID, Username, DisplayName, IsActive, CanLogin)
                VALUES (%s, %s, 'Phase 6 Admin', TRUE, TRUE) RETURNING UserID
            """, (company_id, "p6_admin_" + uuid4().hex[:10]))
            user_id = cursor.fetchone()[0]
            cursor.execute("UPDATE core.Companies SET OwnerUserID = %s WHERE CompanyID = %s",
                           (user_id, company_id))
            cursor.execute("""
                INSERT INTO sec.Roles (RoleCode, RoleName, RoleLevel, IsSystemRole)
                VALUES (%s, 'Phase 6 Admin', 100, FALSE) RETURNING RoleID
            """, ("P6R_" + uuid4().hex[:10],))
            role_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO sec.CompanyRoles
                    (CompanyID, RoleCode, RoleName, RoleLevel, IsDefault,
                     IsProtected, IsCustom, IsActive)
                VALUES (%s, %s, 'Phase 6 Admin', 100, FALSE, FALSE, TRUE, TRUE)
                RETURNING CompanyRoleID
            """, (company_id, "P6C_" + uuid4().hex[:10]))
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
                VALUES (%s, 'P6BRANCH', 'Phase 6 Branch', 'Active', FALSE)
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
async def phase6_ready_conn(phase6_ready_db):
    """Autocommit seeding connection to the per-test disposable database."""
    async with phase6_ready_db.engine.connect() as conn:
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
        VALUES (:cid, :name, 'Phase 6 readiness date actor', TRUE, TRUE)
        RETURNING UserID
    """), {"cid": company_id, "name": "p6ready_" + suffix})).scalar_one()
    role_id = (await db.execute(text("""
        INSERT INTO sec.Roles (RoleCode, RoleName, RoleLevel, IsSystemRole)
        VALUES (:code, 'Phase 6 readiness date actor', 20, FALSE) RETURNING RoleID
    """), {"code": "P6ONR_" + suffix})).scalar_one()
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
                VALUES (:cid, 'DRIVER', 'Phase 6 Driver', 10, FALSE, TRUE, FALSE, TRUE)
                RETURNING CompanyRoleID
            """), {"cid": company_id})).scalar_one()
        # The DRIVER company role may be reused across tests/actors, so grant
        # any requested permissions onto it too (idempotent) — otherwise a
        # driver actor without payroll.view could never legitimately reach
        # the readiness-visibility check, and a null result would be
        # ambiguous between "blocked as driver" and "missing permission".
        for permission in permissions:
            await db.execute(text("""
                INSERT INTO sec.CompanyRolePermissions (CompanyRoleID, PermissionCode)
                VALUES (:rid, :permission)
                ON CONFLICT (CompanyRoleID, PermissionCode) DO NOTHING
            """), {"rid": company_role_id, "permission": permission})
    else:
        company_role_id = (await db.execute(text("""
            INSERT INTO sec.CompanyRoles
                (CompanyID, RoleCode, RoleName, RoleLevel, IsDefault,
                 IsProtected, IsCustom, IsActive)
            VALUES (:cid, :code, 'Phase 6 readiness date actor', 20, FALSE, FALSE, TRUE, TRUE)
            RETURNING CompanyRoleID
        """), {"cid": company_id, "code": "P6ONC_" + suffix})).scalar_one()
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


async def _mixed_driver_actor(db, *, branch_id: int, permissions: tuple[str, ...]) -> str:
    """Build one user holding TWO active grants: an AllCompanyBranches
    administrative role (with the given permissions, e.g. payroll.view) AND
    a second OwnDriverDataOnly DRIVER-role grant scoped to branch_id.

    Reuses a single sec.Users row for both sec.UserBranchRoles rows, to
    exercise the fail-closed contract: any active driver-shaped grant must
    hide readiness reason/date, even when the same user also legitimately
    holds payroll.view via an unrelated administrative role.
    """
    suffix = uuid4().hex[:10]
    company_id = (await db.execute(text(
        "SELECT CompanyID FROM core.Companies"
    ))).scalar_one()
    user_id = (await db.execute(text("""
        INSERT INTO sec.Users (CompanyID, Username, DisplayName, IsActive, CanLogin)
        VALUES (:cid, :name, 'Phase 6 mixed driver actor', TRUE, TRUE)
        RETURNING UserID
    """), {"cid": company_id, "name": "p6mixed_" + suffix})).scalar_one()

    admin_role_id = (await db.execute(text("""
        INSERT INTO sec.Roles (RoleCode, RoleName, RoleLevel, IsSystemRole)
        VALUES (:code, 'Phase 6 mixed actor admin role', 20, FALSE) RETURNING RoleID
    """), {"code": "P6MXA_" + suffix})).scalar_one()
    admin_company_role_id = (await db.execute(text("""
        INSERT INTO sec.CompanyRoles
            (CompanyID, RoleCode, RoleName, RoleLevel, IsDefault,
             IsProtected, IsCustom, IsActive)
        VALUES (:cid, :code, 'Phase 6 mixed actor admin role', 20, FALSE, FALSE, TRUE, TRUE)
        RETURNING CompanyRoleID
    """), {"cid": company_id, "code": "P6MXC_" + suffix})).scalar_one()
    for permission in permissions:
        await db.execute(text("""
            INSERT INTO sec.CompanyRolePermissions (CompanyRoleID, PermissionCode)
            VALUES (:rid, :permission)
            ON CONFLICT (CompanyRoleID, PermissionCode) DO NOTHING
        """), {"rid": admin_company_role_id, "permission": permission})
    await db.execute(text("""
        INSERT INTO sec.UserBranchRoles
            (UserID, CompanyID, BranchID, RoleID, CompanyRoleID, ScopeType, IsActive)
        VALUES (:uid, :cid, NULL, :rid, :crid, 'AllCompanyBranches', TRUE)
    """), {"uid": user_id, "cid": company_id, "rid": admin_role_id, "crid": admin_company_role_id})

    driver_company_role_id = (await db.execute(text("""
        SELECT CompanyRoleID FROM sec.CompanyRoles
        WHERE CompanyID = :cid AND RoleCode = 'DRIVER'
    """), {"cid": company_id})).scalar_one_or_none()
    if driver_company_role_id is None:
        driver_company_role_id = (await db.execute(text("""
            INSERT INTO sec.CompanyRoles
                (CompanyID, RoleCode, RoleName, RoleLevel, IsDefault,
                 IsProtected, IsCustom, IsActive)
            VALUES (:cid, 'DRIVER', 'Phase 6 Driver', 10, FALSE, TRUE, FALSE, TRUE)
            RETURNING CompanyRoleID
        """), {"cid": company_id})).scalar_one()
    driver_role_id = (await db.execute(text("""
        INSERT INTO sec.Roles (RoleCode, RoleName, RoleLevel, IsSystemRole)
        VALUES (:code, 'Phase 6 mixed actor driver role', 10, FALSE) RETURNING RoleID
    """), {"code": "P6MXD_" + suffix})).scalar_one()
    await db.execute(text("""
        INSERT INTO sec.UserBranchRoles
            (UserID, CompanyID, BranchID, RoleID, CompanyRoleID, ScopeType, IsActive)
        VALUES (:uid, :cid, :bid, :rid, :crid, 'OwnDriverDataOnly', TRUE)
    """), {
        "uid": user_id, "cid": company_id, "bid": branch_id,
        "rid": driver_role_id, "crid": driver_company_role_id,
    })

    return create_access_token(int(user_id), int(company_id))


async def _create_published_setup(client, token: str, marker: str) -> int:
    created = await client.post("/payroll-setup/setups", headers=_auth(token), json={
        "setup_code": "P6ON_" + marker[:10], "setup_name": "Phase 6 readiness date",
    })
    assert created.status_code == 201, created.text
    setup_id = created.json()["setup_id"]
    draft = await client.post(
        f"/payroll-setup/setups/{setup_id}/drafts", headers=_auth(token), json={
            "payroll_frequency": "Week", "anchor_start_date": "2090-01-01",
            "normal_days_off_mask": 0,
        },
    )
    assert draft.status_code == 201, draft.text
    published = await client.post(
        f"/payroll-setup/setups/{setup_id}/drafts/{draft.json()['version_id']}/publish",
        headers=_auth(token), json={"effective_from_date": "2090-01-01"},
    )
    assert published.status_code == 201, published.text
    return setup_id


async def _published_version_id(db, setup_id: int) -> int:
    return (await db.execute(text("""
        SELECT PayrollSetupVersionID FROM payroll.PayrollSetupVersions
        WHERE PayrollSetupID = :sid AND LifecycleState = 'Published'
    """), {"sid": setup_id})).scalar_one()


async def _assignment_id(db, company_id: int, branch_id: int) -> int:
    return (await db.execute(text("""
        SELECT BranchPayrollSetupAssignmentID FROM payroll.BranchPayrollSetupAssignments
        WHERE CompanyID = :cid AND BranchID = :bid AND WithdrawnAtUtc IS NULL
    """), {"cid": company_id, "bid": branch_id})).scalar_one()


async def _insert_period(
    db, company_id: int, branch_id: int, assignment_id: int, version_id: int,
    *, code: str, name: str, start: date, end: date, status: str,
) -> int:
    """Insert a PayrollPeriod row with frozen provenance copied from the
    given assignment/version, following the phase2 domain test pattern."""
    return (await db.execute(text("""
        INSERT INTO payroll.PayrollPeriods
            (CompanyID, BranchID, PeriodCode, PeriodName, PeriodType,
             StartDate, EndDate, Status, BranchPayrollSetupAssignmentID,
             PayrollSetupVersionID, FrozenPayrollSetupID, FrozenPayrollSetupCode,
             FrozenPayrollSetupVersionNumber, FrozenPayrollFrequency,
             FrozenAnchorStartDate, FrozenCustomIntervalDays,
             FrozenNormalDaysOffMask, ScheduleConfigHash)
        SELECT :cid, :bid, :code, :name, 'Week',
               :start, :end, :status, :aid,
               v.PayrollSetupVersionID, s.PayrollSetupID, s.SetupCode,
               v.VersionNumber, v.PayrollFrequency, v.AnchorStartDate,
               v.CustomIntervalDays, v.NormalDaysOffMask, v.ConfigHash
        FROM payroll.PayrollSetupVersions v
        JOIN payroll.PayrollSetups s ON s.PayrollSetupID = v.PayrollSetupID
        WHERE v.PayrollSetupVersionID = :vid AND v.CompanyID = :cid
        RETURNING PayrollPeriodID
    """), {
        "cid": company_id, "bid": branch_id, "code": code, "name": name,
        "start": start, "end": end, "status": status, "aid": assignment_id,
        "vid": version_id,
    })).scalar_one()


@pytest.mark.asyncio
async def test_a_no_payroll_period_ready_reports_assignment_start_date(
    phase6_ready_db, phase6_ready_conn,
):
    fixture = phase6_ready_db
    client, admin_token, db = fixture.client, fixture.admin_token, phase6_ready_conn
    company_id, branch_id = fixture.company_id, fixture.branch_id
    marker = uuid4().hex
    setup_id = await _create_published_setup(client, admin_token, marker)
    assigned = await client.post(
        f"/payroll-setup/branches/{branch_id}/assignments",
        headers=_auth(admin_token), json={
            "setup_id": setup_id, "effective_from_date": "2090-01-01",
        },
    )
    assert assigned.status_code == 201, assigned.text

    detail = await readiness.branch_schedule_readiness_detail(company_id, branch_id, db)
    assert detail == (True, "READY", date(2090, 1, 1))

    wrapper = await readiness.branch_schedule_readiness(company_id, branch_id, db)
    assert wrapper == (True, "READY")

    got = await client.get(f"/settings/branches/{branch_id}", headers=_auth(admin_token))
    assert got.status_code == 200, got.text
    assert got.json()["schedule_readiness_reason"] == "READY"
    assert got.json()["schedule_readiness_date"] == "2090-01-01"


@pytest.mark.asyncio
async def test_mapped_resolver_failure_reports_reason_and_evaluated_date(
    phase6_ready_db, phase6_ready_conn, monkeypatch,
):
    """A mapped PolicyError from resolve_payroll_setup_version (e.g. because
    the assigned setup's published version was withdrawn/replaced after the
    assignment start date was already resolved) must surface as a mapped
    reason code, together with the start date that was actually evaluated —
    not None."""
    fixture = phase6_ready_db
    client, admin_token, db = fixture.client, fixture.admin_token, phase6_ready_conn
    company_id, branch_id = fixture.company_id, fixture.branch_id
    marker = uuid4().hex
    setup_id = await _create_published_setup(client, admin_token, marker)
    assigned = await client.post(
        f"/payroll-setup/branches/{branch_id}/assignments",
        headers=_auth(admin_token), json={
            "setup_id": setup_id, "effective_from_date": "2090-01-01",
        },
    )
    assert assigned.status_code == 201, assigned.text

    async def fail_resolution(*args, **kwargs):
        raise PolicyError("VERSION_NOT_FOUND", "synthetic")

    monkeypatch.setattr(readiness, "resolve_payroll_setup_version", fail_resolution)

    detail = await readiness.branch_schedule_readiness_detail(company_id, branch_id, db)
    assert detail == (False, "NO_PUBLISHED_VERSION", date(2090, 1, 1))

    wrapper = await readiness.branch_schedule_readiness(company_id, branch_id, db)
    assert wrapper == (False, "NO_PUBLISHED_VERSION")


@pytest.mark.asyncio
async def test_b_existing_noncancelled_period_advances_evaluated_date(
    phase6_ready_db, phase6_ready_conn,
):
    fixture = phase6_ready_db
    client, admin_token, db = fixture.client, fixture.admin_token, phase6_ready_conn
    company_id, branch_id = fixture.company_id, fixture.branch_id
    marker = uuid4().hex
    setup_id = await _create_published_setup(client, admin_token, marker)
    assigned = await client.post(
        f"/payroll-setup/branches/{branch_id}/assignments",
        headers=_auth(admin_token), json={
            "setup_id": setup_id, "effective_from_date": "2090-01-01",
        },
    )
    assert assigned.status_code == 201, assigned.text
    assignment_id = await _assignment_id(db, company_id, branch_id)
    version_id = await _published_version_id(db, setup_id)
    await _insert_period(
        db, company_id, branch_id, assignment_id, version_id,
        code="P6B_" + marker[:10], name="Phase 6 period B",
        start=date(2090, 1, 1), end=date(2090, 1, 7), status="Approved",
    )

    detail = await readiness.branch_schedule_readiness_detail(company_id, branch_id, db)
    assert detail == (True, "READY", date(2090, 1, 8))

    got = await client.get(f"/settings/branches/{branch_id}", headers=_auth(admin_token))
    assert got.status_code == 200, got.text
    assert got.json()["schedule_readiness_date"] == "2090-01-08"


@pytest.mark.asyncio
async def test_c_cancelled_period_does_not_advance_evaluated_date(
    phase6_ready_db, phase6_ready_conn,
):
    fixture = phase6_ready_db
    client, admin_token, db = fixture.client, fixture.admin_token, phase6_ready_conn
    company_id, branch_id = fixture.company_id, fixture.branch_id
    marker = uuid4().hex
    setup_id = await _create_published_setup(client, admin_token, marker)
    assigned = await client.post(
        f"/payroll-setup/branches/{branch_id}/assignments",
        headers=_auth(admin_token), json={
            "setup_id": setup_id, "effective_from_date": "2090-01-01",
        },
    )
    assert assigned.status_code == 201, assigned.text
    assignment_id = await _assignment_id(db, company_id, branch_id)
    version_id = await _published_version_id(db, setup_id)
    await _insert_period(
        db, company_id, branch_id, assignment_id, version_id,
        code="P6C1_" + marker[:9], name="Phase 6 period C1",
        start=date(2090, 1, 1), end=date(2090, 1, 7), status="Approved",
    )
    await _insert_period(
        db, company_id, branch_id, assignment_id, version_id,
        code="P6C2_" + marker[:9], name="Phase 6 period C2 (cancelled)",
        start=date(2090, 1, 8), end=date(2090, 1, 14), status="Cancelled",
    )

    detail = await readiness.branch_schedule_readiness_detail(company_id, branch_id, db)
    assert detail == (True, "READY", date(2090, 1, 8))

    got = await client.get(f"/settings/branches/{branch_id}", headers=_auth(admin_token))
    assert got.status_code == 200, got.text
    assert got.json()["schedule_readiness_date"] == "2090-01-08"


@pytest.mark.asyncio
async def test_d_driver_caller_never_sees_reason_or_date(
    phase6_ready_db, phase6_ready_conn,
):
    """Driver / OwnDriverDataOnly callers must never see readiness reason or date,
    even when they genuinely hold payroll.view — isolating the driver gate from
    a merely-missing-permission explanation.

    Observed behavior: GET /settings/branches/{id} returns 200 for a driver
    scoped to that branch (branch access is granted via the OwnDriverDataOnly
    row), but _attach_readiness_reasons short-circuits on
    _require_not_driver_role before setting either field, so both come back
    null. GET /settings/branches (list) behaves the same way for every row
    it returns to that caller. This holds even for a user who ALSO holds a
    genuine AllCompanyBranches payroll.view grant alongside an active
    OwnDriverDataOnly/DRIVER grant (fail-closed: one matching driver-shaped
    row is enough to block) — verified against a positive-control user who
    holds the same payroll.view grant with no driver row at all.
    """
    fixture = phase6_ready_db
    client, admin_token, db = fixture.client, fixture.admin_token, phase6_ready_conn
    branch_id = fixture.branch_id
    marker = uuid4().hex
    setup_id = await _create_published_setup(client, admin_token, marker)
    assigned = await client.post(
        f"/payroll-setup/branches/{branch_id}/assignments",
        headers=_auth(admin_token), json={
            "setup_id": setup_id, "effective_from_date": "2090-01-01",
        },
    )
    assert assigned.status_code == 201, assigned.text

    # (a) Pure OwnDriverDataOnly driver actor, now genuinely holding
    # payroll.view (granted onto the DRIVER company role by _actor).
    driver_token = await _actor(
        db, permissions=("payroll.view",), scope="OwnDriverDataOnly",
        branch_id=branch_id, driver=True,
    )

    single = await client.get(f"/settings/branches/{branch_id}", headers=_auth(driver_token))
    assert single.status_code == 200, single.text
    assert single.json()["schedule_readiness_reason"] is None
    assert single.json()["schedule_readiness_date"] is None

    listing = await client.get("/settings/branches", headers=_auth(driver_token))
    assert listing.status_code == 200, listing.text
    assert len(listing.json()) >= 1
    for row in listing.json():
        assert row["schedule_readiness_reason"] is None
        assert row["schedule_readiness_date"] is None

    # (b) Mixed actor: a genuine AllCompanyBranches payroll.view grant PLUS a
    # second active OwnDriverDataOnly/DRIVER grant on the same branch. The
    # driver-shaped row must still hide both fields.
    mixed_token = await _mixed_driver_actor(
        db, branch_id=branch_id, permissions=("payroll.view",),
    )
    mixed = await client.get(f"/settings/branches/{branch_id}", headers=_auth(mixed_token))
    assert mixed.status_code == 200, mixed.text
    assert mixed.json()["schedule_readiness_reason"] is None
    assert mixed.json()["schedule_readiness_date"] is None

    # (c) Positive control: the same AllCompanyBranches payroll.view grant,
    # on a separate user with no driver row at all, must see real values.
    positive_control_token = await _actor(
        db, permissions=("payroll.view",), scope="AllCompanyBranches",
    )
    control = await client.get(
        f"/settings/branches/{branch_id}", headers=_auth(positive_control_token),
    )
    assert control.status_code == 200, control.text
    assert control.json()["schedule_readiness_reason"] == "READY"
    assert control.json()["schedule_readiness_date"] == "2090-01-01"


@pytest.mark.asyncio
async def test_e_caller_without_payroll_view_sees_nulls_positive_control_sees_values(
    phase6_ready_db, phase6_ready_conn,
):
    fixture = phase6_ready_db
    client, admin_token, db = fixture.client, fixture.admin_token, phase6_ready_conn
    branch_id = fixture.branch_id
    marker = uuid4().hex
    setup_id = await _create_published_setup(client, admin_token, marker)
    assigned = await client.post(
        f"/payroll-setup/branches/{branch_id}/assignments",
        headers=_auth(admin_token), json={
            "setup_id": setup_id, "effective_from_date": "2090-01-01",
        },
    )
    assert assigned.status_code == 201, assigned.text

    no_view_token = await _actor(
        db, permissions=("branches.view",), scope="AllCompanyBranches",
    )
    positive_control_token = await _actor(
        db, permissions=("payroll.view",), scope="SpecificBranch", branch_id=branch_id,
    )

    hidden = await client.get(f"/settings/branches/{branch_id}", headers=_auth(no_view_token))
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["schedule_readiness_reason"] is None
    assert hidden.json()["schedule_readiness_date"] is None

    visible = await client.get(
        f"/settings/branches/{branch_id}", headers=_auth(positive_control_token),
    )
    assert visible.status_code == 200, visible.text
    assert visible.json()["schedule_readiness_reason"] == "READY"
    assert visible.json()["schedule_readiness_date"] == "2090-01-01"


@pytest.mark.asyncio
async def test_f_create_branch_with_company_default_reports_ready_and_date(
    phase6_ready_db, phase6_ready_conn,
):
    fixture = phase6_ready_db
    client, admin_token, db = fixture.client, fixture.admin_token, phase6_ready_conn
    marker = uuid4().hex
    setup_id = await _create_published_setup(client, admin_token, marker)
    set_default = await client.put("/payroll-setup/default", headers=_auth(admin_token),
                                   json={"setup_id": setup_id})
    assert set_default.status_code == 204, set_default.text

    actor = await _actor(
        db, permissions=("branches.create", "payroll_setup.assign"),
        scope="AllCompanyBranches",
    )
    created = await client.post("/settings/branches", headers=_auth(actor), json={
        "branch_name": "Phase6 create ready " + marker[:8],
        "branch_code": "P6F" + marker[:7],
        "first_payroll_start_date": "2090-01-01",
    })
    assert created.status_code == 201, created.text
    assert created.json()["schedule_readiness_reason"] == "READY"
    assert created.json()["schedule_readiness_date"] == "2090-01-01"


@pytest.mark.asyncio
async def test_g_create_branch_without_company_default_reports_reason_and_date(
    phase6_ready_db, phase6_ready_conn,
):
    fixture = phase6_ready_db
    client, admin_token, db = fixture.client, fixture.admin_token, phase6_ready_conn
    company_id = fixture.company_id
    marker = uuid4().hex
    old_default = (await db.execute(text(
        "SELECT DefaultPayrollSetupID FROM core.Companies WHERE CompanyID = :cid"
    ), {"cid": company_id})).scalar_one_or_none()
    assert old_default is None

    cleared = await client.put("/payroll-setup/default", headers=_auth(admin_token),
                               json={"setup_id": None})
    assert cleared.status_code == 204, cleared.text

    actor = await _actor(
        db, permissions=("branches.create", "payroll_setup.assign"),
        scope="AllCompanyBranches",
    )
    branch_code = "P6G" + marker[:7]
    created = await client.post("/settings/branches", headers=_auth(actor), json={
        "branch_name": "Phase6 create no default " + marker[:8],
        "branch_code": branch_code,
        "first_payroll_start_date": "2090-01-01",
    })
    assert created.status_code == 201, created.text
    assert created.json()["schedule_readiness_reason"] == "NO_COMPANY_DEFAULT"
    assert created.json()["schedule_readiness_date"] == "2090-01-01"
    branch_id = created.json()["branch_id"]
    assert (await db.execute(text("""
        SELECT COUNT(*) FROM payroll.BranchPayrollSetupAssignments
        WHERE CompanyID = :cid AND BranchID = :bid
    """), {"cid": company_id, "bid": branch_id})).scalar_one() == 0


@pytest.mark.asyncio
async def test_no_assignment_and_no_period_yields_no_assignment_reason(
    phase6_ready_db, phase6_ready_conn,
):
    fixture = phase6_ready_db
    db = phase6_ready_conn
    company_id, branch_id = fixture.company_id, fixture.branch_id

    detail = await readiness.branch_schedule_readiness_detail(company_id, branch_id, db)
    assert detail == (False, "NO_ASSIGNMENT", None)


@pytest.mark.asyncio
async def test_inactive_branch_yields_branch_not_operational_reason(
    phase6_ready_db, phase6_ready_conn,
):
    fixture = phase6_ready_db
    db = phase6_ready_conn
    company_id, branch_id = fixture.company_id, fixture.branch_id

    await db.execute(text("""
        UPDATE core.Branches SET Status = 'Inactive' WHERE CompanyID = :cid AND BranchID = :bid
    """), {"cid": company_id, "bid": branch_id})

    detail = await readiness.branch_schedule_readiness_detail(company_id, branch_id, db)
    assert detail == (False, "BRANCH_NOT_OPERATIONAL", None)
