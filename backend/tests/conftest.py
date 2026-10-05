"""
pytest fixtures for the backend test suite.

Strategy:
  - testing.postgresql spins up a real isolated PostgreSQL cluster per session.
  - The validated schema is applied once at session start.
  - A seed user is inserted so login tests have something to authenticate against.
  - Each test gets a fresh AsyncClient pointing at the same schema.

Run from Payroll_App_v3/backend/:
    pytest tests/ -v
"""
import glob as _glob
import os
import subprocess
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg2
import pytest
import pytest_asyncio
import testing.postgresql
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from tests.db_state import (
    assert_trigger_fingerprints_equal,
    preserve_test_guc_state,
    read_trigger_fingerprint,
)
from tests.postgresql_compat import create_test_postgresql

# Tests that need process configuration must never inherit a developer DSN or
# secret. Database access itself remains owned by the testing.postgresql fixture.
os.environ["DATABASE_URL"] = "postgresql+asyncpg://test:test@127.0.0.1:1/test_configuration_only"
os.environ["SECRET_KEY"] = "test-only-secret-key-never-use-outside-tests-000000000000000000000000"
os.environ["ENVIRONMENT"] = "test"

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()


# ---------------------------------------------------------------------------
# On Windows, PostgreSQL bin/ is typically NOT on PATH.
# testing.postgresql calls find_program('initdb', ['bin']) which searches PATH,
# so we prepend the highest-version PG bin dir before the first fixture runs.
# Only fixture-time Postgresql() construction reads PATH, so imports need not follow this.
# ---------------------------------------------------------------------------
def _find_pg_bin() -> str | None:
    """Return the PostgreSQL bin directory, preferring the highest version."""
    candidates = sorted(
        _glob.glob(r"C:\Program Files\PostgreSQL\*\bin"),
        reverse=True,  # highest version first (e.g. 18 before 17)
    )
    for candidate in candidates:
        if os.path.exists(os.path.join(candidate, "initdb.exe")):
            return candidate
    return None


_PG_BIN = _find_pg_bin()
if _PG_BIN and _PG_BIN not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _PG_BIN + os.pathsep + os.environ.get("PATH", "")


def _stop_test_postgresql_safely(pg: testing.postgresql.Postgresql) -> None:
    """Stop this fixture's Windows cluster without testing.postgresql's SIGINT path."""
    if os.name != "nt":
        pg.stop()
        return

    process = pg.child_process
    if process is None:
        pg.cleanup()
        return

    data_dir = Path(pg.get_data_directory()).resolve()
    base_dir = Path(pg.base_dir).resolve()
    if data_dir.parent != base_dir or not (data_dir / "PG_VERSION").exists():
        raise RuntimeError(f"Refusing to stop an unowned PostgreSQL cluster: {data_dir}")

    try:
        pg.stop()
    except (ValueError, OSError):
        # testing.postgresql.Postgresql forces SIGINT, unsupported by this
        # Windows/Python process combination. pg_ctl owns the same exact data dir.
        if _PG_BIN is None:
            raise RuntimeError("PostgreSQL bin directory is unavailable for test cleanup")
        pg_ctl = Path(_PG_BIN) / "pg_ctl.exe"
        result = subprocess.run(
            [str(pg_ctl), "stop", "-D", str(data_dir), "-m", "fast", "-w", "-t", "15"],
            capture_output=True,
            text=True,
            check=False,
        )
        deadline = time.monotonic() + 15
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if process.poll() is None:
            # PID-tree termination is limited to the Popen-owned test cluster.
            fallback = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
            process.wait(timeout=10)
            if fallback.returncode != 0:
                raise RuntimeError(
                    "Could not stop the owned temporary PostgreSQL cluster: "
                    f"pg_ctl={result.returncode} {result.stderr.strip()}; "
                    f"taskkill={fallback.returncode} {fallback.stderr.strip()}"
                )
    finally:
        if process.poll() is not None:
            pg.child_process = None
            pg.cleanup()

    if process.poll() is None or base_dir.exists():
        raise RuntimeError(f"Temporary PostgreSQL cleanup incomplete: {base_dir}")

@pytest.fixture
def safe_test_postgresql_stop():
    """Return the shared safe cleanup function for test-owned PostgreSQL clusters."""
    return _stop_test_postgresql_safely


# All migration SQL files, applied in order.
# conftest discovers them automatically so new migrations are picked up
# without editing this file.
_MIGRATIONS_DIR = (
    Path(__file__).parent.parent.parent  # -> Payroll_App_v3/
    / "migrations" / "sql"
)

# Pre-hashed bcrypt for password "TestPass123!" — cost=12
# Regenerate with: python -c "import bcrypt; print(bcrypt.hashpw(b'TestPass123!', bcrypt.gensalt(12)).decode())"
_TEST_PASSWORD_HASH = "$2b$12$placeholderreplacedbyconftest"

# Seed statements executed individually so psycopg2 named-parameter syntax
# (%(name)s) only appears in the one statement that actually uses it.
_SEED_STMTS = [
    """
    INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended, timezonename, currencycode)
    VALUES ('DEMO', 'Demo Logistics', 'Demo Logistics Ltd', 'Active', FALSE, 'Africa/Cairo', 'USD')
    """,
    """
    INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
    SELECT companyid, 'HQ', 'Headquarters', 'Active', TRUE
    FROM core.companies WHERE companycode = 'DEMO'
    """,
    # Second branch — used by payroll-period tests so the one-Draft-per-branch
    # and one-Open-per-branch constraints don't conflict with the HQ seed period.
    """
    INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
    SELECT companyid, 'PAYTEST', 'Payroll Test Branch', 'Active', FALSE
    FROM core.companies WHERE companycode = 'DEMO'
    """,
    """
    INSERT INTO sec.roles (rolecode, rolename, rolelevel, issystemrole)
    VALUES ('PAYROLL_ADMIN', 'Payroll Admin', 100, FALSE)
    """,
    # Permission codes for write operations (used by action-level permission guards).
    # ON CONFLICT DO NOTHING: migration 0015 may already have inserted some of these.
    """
    INSERT INTO sec.permissions (permissioncode, permissionname, modulecode) VALUES
        ('payroll.entry',          'Enter Payroll Data',       'payroll'),
        ('payroll.period.create',  'Create Payroll Period',    'payroll'),
        ('payroll.approve_rate',   'Approve Pay Rates',        'payroll'),
        ('payroll.finalize',       'Finalize Payroll',         'payroll'),
        ('drivers.manage',         'Manage Drivers',           'core'),
        ('employees.view',         'View Employees',           'employees'),
        ('employees.manage',       'Manage Employees',         'employees'),
        ('review.decide',          'Decide on Review Items',   'review'),
        ('setup.manage',           'Manage Settings',          'settings')
    ON CONFLICT (permissioncode) DO NOTHING
    """,
    # Grant PAYROLL_ADMIN all write permissions
    """
    INSERT INTO sec.rolepermissions (roleid, permissionid)
    SELECT r.roleid, p.permissionid
    FROM   sec.roles r
    CROSS JOIN sec.permissions p
    WHERE  r.rolecode = 'PAYROLL_ADMIN'
    """,
    # Read-only role — no permissions; used to test that viewers cannot write
    """
    INSERT INTO sec.roles (rolecode, rolename, rolelevel, issystemrole)
    VALUES ('PAYROLL_VIEWER', 'Payroll Viewer', 30, FALSE)
    """,

    # ── Company-scoped roles (migration 0014/0016 new system) ─────────────────
    # COMPANY_OWNER: default protected role, gets all permissions
    """
    INSERT INTO sec.companyroles
        (companyid, rolecode, rolename, rolelevel, isdefault, isprotected, iscustom, isactive)
    SELECT c.companyid, 'COMPANY_OWNER', 'Company Owner', 100, TRUE, TRUE, FALSE, TRUE
    FROM core.companies c WHERE c.companycode = 'DEMO'
    """,
    """
    INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
    SELECT cr.companyroleid, p.permissioncode
    FROM   sec.companyroles cr, sec.permissions p
    WHERE  cr.rolecode = 'COMPANY_OWNER'
    """,
    # DRIVER: default protected role, minimal permissions
    """
    INSERT INTO sec.companyroles
        (companyid, rolecode, rolename, rolelevel, isdefault, isprotected, iscustom, isactive)
    SELECT c.companyid, 'DRIVER', 'Driver', 10, TRUE, TRUE, FALSE, TRUE
    FROM core.companies c WHERE c.companycode = 'DEMO'
    """,
    """
    INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
    SELECT cr.companyroleid, 'drivers.view'
    FROM   sec.companyroles cr
    WHERE  cr.rolecode = 'DRIVER'
    """,
    # PAYROLL_VIEWER_CO: non-driver read-only role for branch_user; grants payroll.view
    # so tests can verify that viewers can read but not write operational payroll data.
    # Must NOT be rolecode='DRIVER' so _require_not_driver_role passes.
    """
    INSERT INTO sec.companyroles
        (companyid, rolecode, rolename, rolelevel, isdefault, isprotected, iscustom, isactive)
    SELECT c.companyid, 'PAYROLL_VIEWER_CO', 'Payroll Viewer (Test)', 20, FALSE, FALSE, FALSE, TRUE
    FROM core.companies c WHERE c.companycode = 'DEMO'
    """,
    """
    INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
    SELECT cr.companyroleid, 'payroll.view'
    FROM   sec.companyroles cr
    WHERE  cr.rolecode = 'PAYROLL_VIEWER_CO'
    """,
    # ── Users ──────────────────────────────────────────────────────────────────
    # psycopg2 named-parameter style: %(name)s  (NOT SQLAlchemy's :name)
    """
    INSERT INTO sec.users (companyid, username, displayname, passwordhash, isactive, canlogin)
    SELECT c.companyid, 'admin', 'Admin User', %(pw_hash)s, TRUE, TRUE
    FROM core.companies c WHERE c.companycode = 'DEMO'
    """,
    # admin user assignment: AllCompanyBranches, PAYROLL_ADMIN global role (legacy path)
    # + COMPANY_OWNER company role (new path).  Both paths grant full access.
    """
    INSERT INTO sec.userbranchroles
        (userid, companyid, branchid, roleid, companyroleId, scopetype, isactive)
    SELECT u.userid, u.companyid, NULL, r.roleid,
           (SELECT cr.companyroleid FROM sec.companyroles cr
            JOIN core.companies c2 ON c2.companyid = cr.companyid
            WHERE c2.companycode = 'DEMO' AND cr.rolecode = 'COMPANY_OWNER'),
           'AllCompanyBranches', TRUE
    FROM sec.users u, sec.roles r
    WHERE u.username = 'admin' AND r.rolecode = 'PAYROLL_ADMIN'
    """,
    # Branch-limited read-only test user: SpecificBranch scope to HQ only,
    # PAYROLL_VIEWER global role (no write permissions) + PAYROLL_VIEWER_CO company role
    # (grants payroll.view only — NOT DRIVER so _require_not_driver_role passes).
    # Password is the same as admin ("TestPass123!") — only the role and scope differ.
    """
    INSERT INTO sec.users (companyid, username, displayname, passwordhash, isactive, canlogin)
    SELECT c.companyid, 'branch_user', 'Branch User', %(pw_hash)s, TRUE, TRUE
    FROM core.companies c WHERE c.companycode = 'DEMO'
    """,
    # branch_user assignment: SpecificBranch HQ, PAYROLL_VIEWER global role + PAYROLL_VIEWER_CO company role
    """
    INSERT INTO sec.userbranchroles
        (userid, companyid, branchid, roleid, companyroleId, scopetype, isactive)
    SELECT u.userid, u.companyid, b.branchid, r.roleid,
           (SELECT cr.companyroleid FROM sec.companyroles cr
            JOIN core.companies c2 ON c2.companyid = cr.companyid
            WHERE c2.companycode = 'DEMO' AND cr.rolecode = 'PAYROLL_VIEWER_CO'),
           'SpecificBranch', TRUE
    FROM   sec.users u, core.branches b, sec.roles r
    WHERE  u.username  = 'branch_user'
      AND  b.branchcode = 'HQ'
      AND  r.rolecode   = 'PAYROLL_VIEWER'
    """,
    # Seed rate types used by pay-rate tests (Milestone 6).
    # The 7 production types (HOURLY, MILEAGE, LOAD, OVERNIGHT, WAIT, PALLET, SILO)
    # are now seeded by Alembic migration 0008.  Only INACTIVE is added here because
    # it is test-only and intentionally absent from the production migration.
    # M13C_* rate types are also test-only — used by test_m13c.py to avoid polluting
    # system rate types with tiered-behavior test items.
    # ON CONFLICT DO NOTHING makes this idempotent whether migration 0008 ran first or not.
    """
    INSERT INTO payroll.ratetypes (ratecode, ratename, unitname, isactive) VALUES
        ('HOURLY',      'Hourly Rate',           'Hour',   TRUE),
        ('MILEAGE',     'Mileage Rate',           'Mile',   TRUE),
        ('LOAD',        'Load Rate',              'Load',   TRUE),
        ('OVERNIGHT',   'Overnight Rate',         'Night',  TRUE),
        ('WAIT',        'Wait Time Rate',         'Hour',   TRUE),
        ('PALLET',      'Pallet Rate',            'Pallet', TRUE),
        ('SILO',        'Silo Rate',              'Silo',   TRUE),
        ('INACTIVE',    'Inactive Type',          'Unit',   FALSE),
        ('M13C_ORDINAL','M13C Ordinal Test Rate', 'Load',   TRUE),
        ('M13C_RBRKT',  'M13C Bracket Test Rate', 'Mile',   TRUE),
        ('M13C_RPROG',  'M13C Progres Test Rate', 'Mile',   TRUE),
        ('M13C_BLOCK',  'M13C Block Test Rate',   'Block',  TRUE)
    ON CONFLICT (ratecode) DO NOTHING
    """,
    # Phase 4B.3: M13C_* rate types must have system PayItemRateTypeMap entries so
    # they remain visible in GET /payroll/rate-types after the stricter filter
    # (only show rate types with system or own-company mappings).
    #
    # Create test-only system-scope PayItems (companyid IS NULL) anchoring M13C_*
    # so they pass the has_system_mapping=true check.  isdefaultbranchactive=FALSE
    # ensures they never appear in any branch's active pay item list — purely used
    # as ownership anchors for the mapping filter.
    """
    INSERT INTO payroll.payitems
        (companyid, payitemcode, payitemname, ratebehavior, requiresrate,
         isdefaultbranchactive, status, category, datatype, itemscope, issystemstandard)
    SELECT NULL, v.code, v.name, v.behavior, TRUE, FALSE, 'Active',
           'Custom', 'Decimal', 'Daily', FALSE
    FROM (VALUES
        ('M13C_SYS_ORDINAL', 'M13C Ordinal Anchor', 'OrdinalTier'),
        ('M13C_SYS_RBRKT',   'M13C Bracket Anchor', 'RangeBracket'),
        ('M13C_SYS_RPROG',   'M13C Progres Anchor', 'RangeProgressive'),
        ('M13C_SYS_BLOCK',   'M13C Block Anchor',   'Block'),
        ('INACTIVE_SYS',     'Inactive RT Anchor',  'PerUnit')
    ) AS v(code, name, behavior)
    WHERE NOT EXISTS (
        SELECT 1 FROM payroll.payitems
        WHERE payitemcode = v.code AND companyid IS NULL
    )
    """,
    """
    INSERT INTO payroll.payitemratetypemap (payitemid, ratetypeid, status, isprimary)
    SELECT pi.payitemid, rt.ratetypeid, 'Active', TRUE
    FROM payroll.payitems pi
    JOIN payroll.ratetypes rt ON (
        (pi.payitemcode = 'M13C_SYS_ORDINAL' AND rt.ratecode = 'M13C_ORDINAL')
     OR (pi.payitemcode = 'M13C_SYS_RBRKT'   AND rt.ratecode = 'M13C_RBRKT')
     OR (pi.payitemcode = 'M13C_SYS_RPROG'   AND rt.ratecode = 'M13C_RPROG')
     OR (pi.payitemcode = 'M13C_SYS_BLOCK'   AND rt.ratecode = 'M13C_BLOCK')
     OR (pi.payitemcode = 'INACTIVE_SYS'     AND rt.ratecode = 'INACTIVE')
    )
    WHERE pi.companyid IS NULL
      AND pi.payitemcode IN ('M13C_SYS_ORDINAL','M13C_SYS_RBRKT','M13C_SYS_RPROG',
                             'M13C_SYS_BLOCK','INACTIVE_SYS')
    ON CONFLICT (payitemid, ratetypeid) DO NOTHING
    """,
]


@pytest.fixture(scope="session")
def pg_instance():
    """
    Spin up a temporary PostgreSQL cluster for the entire test session.

    Windows uses PostgreSQL's native control path because testing.postgresql
    hardcodes SIGINT, which Python cannot send to this child process.
    """
    pg = create_test_postgresql()
    try:
        yield pg
    finally:
        _stop_test_postgresql_safely(pg)


@pytest.fixture(scope="session")
def apply_schema(pg_instance):
    """
    Apply the validated schema SQL and seed one test user.
    Runs once per test session.
    """
    from app.auth.security import hash_password

    pw_hash = hash_password("TestPass123!")

    conn = psycopg2.connect(
        client_encoding="utf-8",
        **pg_instance.dsn(),
    )
    conn.autocommit = True
    cur = conn.cursor()

    # Apply all migration files in sorted order (0001, 0002, …)
    for migration_file in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        cur.execute(migration_file.read_text(encoding="utf-8"))

    for stmt in _SEED_STMTS:
        if "%(pw_hash)s" in stmt:
            cur.execute(stmt, {"pw_hash": pw_hash})
        else:
            cur.execute(stmt)
    conn.close()
    return pg_instance


@pytest.fixture(scope="session")
def test_database_url(apply_schema):
    """Return an asyncpg-compatible URL pointing at the test cluster."""
    dsn = apply_schema.dsn()
    return (
        f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:{dsn['port']}/{dsn['database']}"
    )


@pytest_asyncio.fixture(scope="session")
async def test_engine(test_database_url):
    """One async SQLAlchemy engine/pool shared for the entire test session.

    It also owns the shared-database trigger sentinel. The baseline is read once
    the schema and seed are complete; the comparison runs at teardown BEFORE the
    engine is disposed. Every fixture that holds a connection from this engine
    (test_app, session_db_conn, ...) depends on it, so pytest has already torn
    them down by then, and pg_instance (which test_database_url depends on) is
    still running. Any missing, extra or enable-state-changed trigger fails the
    session.
    """
    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.connect() as conn:
            baseline = await read_trigger_fingerprint(conn)
        yield engine
        async with engine.connect() as conn:
            final = await read_trigger_fingerprint(conn)
        assert_trigger_fingerprints_equal(baseline, final)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def test_app(test_engine) -> FastAPI:
    """
    Build a FastAPI test app that uses the isolated test database.

    Uses FastAPI's dependency_overrides to redirect get_db() to the test
    engine.  This is the correct pattern — module-level monkey-patching of
    engine does NOT work because dependencies.py captures the reference at
    import time.
    """
    from app.dependencies import get_db
    from app.main import create_app

    real_app = create_app()
    real_app.state.engine = test_engine

    async def _override_get_db() -> AsyncGenerator[AsyncConnection, None]:
        async with test_engine.begin() as conn:
            yield conn

    real_app.dependency_overrides[get_db] = _override_get_db

    yield real_app

    real_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def db_conn(test_engine) -> AsyncGenerator[AsyncConnection, None]:
    """Function-scoped raw AsyncConnection for direct DB seeding (bypasses HTTP layer).
    Uses AUTOCOMMIT so inserts are immediately visible to other connections."""
    async with test_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        async with preserve_test_guc_state(conn):
            yield conn


@pytest_asyncio.fixture(scope="session")
async def session_db_conn(test_engine) -> AsyncGenerator[AsyncConnection, None]:
    """Session-scoped raw AsyncConnection for seeding in session-scoped tests.
    Uses AUTOCOMMIT so inserts are immediately visible to other connections.

    The GUC preservation here runs once, at session end -- a boundary safety net,
    NOT per-test isolation. Per-test safety comes from temporary_test_guc and the
    static scan that forbids raw session-level app.* mutation."""
    async with test_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        async with preserve_test_guc_state(conn):
            yield conn


@pytest_asyncio.fixture
async def client(test_app) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async HTTP client for each test function."""
    # httpx >= 0.20 removed the app= shorthand; use ASGITransport instead.
    transport = ASGITransport(app=test_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Session-scoped client + auth token (reused across all core/domain tests)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session")
async def session_client(test_app) -> AsyncGenerator[httpx.AsyncClient, None]:
    """
    A single long-lived async client shared by the entire test session.
    Used by fixtures that need to make authenticated requests to set up
    shared state (e.g. creating a driver once and reusing the ID).
    """
    transport = ASGITransport(app=test_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as c:
        yield c


@pytest_asyncio.fixture(scope="session")
async def auth_token(session_client: httpx.AsyncClient) -> str:
    """
    Obtain a JWT for the seed admin user; cached for the whole session.
    Any test that needs an Authorization header should depend on this fixture.
    """
    resp = await session_client.post("/auth/login", json={
        "username": "admin",
        "password": "TestPass123!",
        "company_code": "DEMO",
    })
    assert resp.status_code == 200, f"Auth setup failed: {resp.text}"
    return resp.json()["access_token"]


@pytest_asyncio.fixture(scope="session")
async def created_driver_id(session_client: httpx.AsyncClient, auth_token: str) -> int:
    """
    Create one driver record at session start; return its driver_id.
    All GET/PATCH tests for a specific driver use this fixture so only
    one INSERT happens for the entire session.
    """
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": 1,          # seeded HQ branch
            "full_name": "Test Driver One",
            "preferred_name": "TD1",
            "driver_code": "TD-001",
            "cdl_number": "CDL-TEST-001",
            "email": "td1@example.com",
        },
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 201, f"Driver seed failed: {resp.text}"
    return resp.json()["driver_id"]


@pytest_asyncio.fixture(scope="session")
async def created_period_id(session_db_conn) -> int:
    """
    Insert one Draft payroll period at session start; return its payroll_period_id.
    CP-1D: POST /payroll/periods requires an existing Open period (B1 guard), so
    we insert directly. Used by read-only period tests.
    """
    from sqlalchemy import text as _sqla_text
    # Cancel any stale HQ Draft/Open periods from prior runs
    await session_db_conn.execute(
        _sqla_text(
            "UPDATE payroll.payrollperiods SET status = 'Cancelled' "
            "WHERE branchid = 1 AND status IN ('Draft', 'Open')"
        ),
    )
    row = (await session_db_conn.execute(
        _sqla_text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype,
                 startdate, enddate, paydate)
            VALUES (1, 1, 'Draft', 'HQ-2026-0106', 'Week of Jan 6, 2026', 'Week',
                    '2026-01-06', '2026-01-12', '2026-01-14')
            RETURNING payrollperiodid
        """),
    )).mappings().first()
    return row["payrollperiodid"]


@pytest_asyncio.fixture(scope="session")
async def seeded_paytest_branch_id(session_client: httpx.AsyncClient, auth_token: str) -> int:
    """
    The canonical seeded PAYTEST branch (core.branches.branchcode = 'PAYTEST').

    Infrastructure that must target THIS branch regardless of what a test module
    does (e.g. `activate_paytest_system_items`) depends on this fixture and never on
    `paytest_branch_id`: many modules shadow `paytest_branch_id` with a module-owned
    branch, and a session-scoped fixture's dependency on a shadowable name resolves
    by whichever module requests it first.
    """
    resp = await session_client.get(
        "/core/branches",
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 200, f"Branch lookup failed: {resp.text}"
    for branch in resp.json():
        if branch["branch_code"] == "PAYTEST":
            return branch["branch_id"]
    raise AssertionError("PAYTEST branch not found — check seed data")


@pytest_asyncio.fixture(scope="session")
async def paytest_branch_id(seeded_paytest_branch_id: int) -> int:
    """
    The shared seeded PAYTEST branch, for tests that genuinely want it.  Modules
    that recycle workflow slots shadow this name with a module-owned branch.
    """
    return seeded_paytest_branch_id


@pytest_asyncio.fixture(scope="session")
async def paytest_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    seeded_paytest_branch_id: int,
) -> int:
    """
    Create one driver on the seeded PAYTEST branch at session start; return its driver_id.
    Depends on `seeded_paytest_branch_id`, not the shadowable `paytest_branch_id`: this
    session-cached driver must never land on whichever module-owned branch happened to
    request it first. Modules that own their branch shadow this fixture as well.
    """
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id":      seeded_paytest_branch_id,
            "full_name":      "Paytest Driver",
            "preferred_name": "PTD",
            "driver_code":    "PTD-001",
            "cdl_number":     "CDL-PT-001",
            "email":          "ptd1@example.com",
        },
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 201, f"PAYTEST driver seed failed: {resp.text}"
    return resp.json()["driver_id"]


@pytest_asyncio.fixture
async def owned_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
) -> int:
    """Create and remove a test-owned Driver used by isolated rule scenarios."""
    from sqlalchemy import text

    marker = uuid4().hex
    created = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": paytest_branch_id,
            "full_name": f"Owned rule scenario {marker}",
            "driver_code": f"OWN-{marker[:10]}",
        },
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert created.status_code == 201, f"Owned Driver create failed: {created.text}"
    body = created.json()
    driver_id = body["driver_id"]
    employee_id = body["employee_id"]
    try:
        yield driver_id
    finally:
        dependents = (await direct_db.execute(
            text("""
                SELECT
                    (SELECT COUNT(*) FROM payroll.payrollfinallines WHERE driverid = :did) AS final_lines,
                    (SELECT COUNT(*) FROM payroll.payrolldraftlines WHERE driverid = :did) AS draft_lines,
                    (SELECT COUNT(*) FROM payroll.payrollbonusevents WHERE driverid = :did) AS bonuses,
                    (SELECT COUNT(*) FROM payroll.driverrates WHERE driverid = :did) AS rates,
                    (SELECT COUNT(*) FROM payroll.payrollperioddriverdayentrystate WHERE driverid = :did) AS day_state
            """), {"did": driver_id},
        )).mappings().one()
        assert all(value == 0 for value in dependents.values()), (
            f"Owned Driver {driver_id} has unexpected non-rule dependents: {dict(dependents)}"
        )
        rule_ids = [row[0] for row in (await direct_db.execute(
            text("SELECT driverpayruleid FROM payroll.driverpayrules WHERE driverid = :did"),
            {"did": driver_id},
        )).all()]
        rule_id_strings = [str(rule_id) for rule_id in rule_ids] or [""]
        await direct_db.execute(
            text("DELETE FROM audit.auditlog WHERE entityname = 'DriverPayRules' AND entityid = ANY(:ids)"),
            {"ids": rule_id_strings},
        )
        await direct_db.execute(
            text("DELETE FROM payroll.driverpayrules WHERE driverid = :did"), {"did": driver_id},
        )
        await direct_db.execute(
            text("DELETE FROM audit.auditlog WHERE entityname = 'Drivers' AND entityid = :eid"),
            {"eid": str(driver_id)},
        )
        await direct_db.execute(text("DELETE FROM core.drivers WHERE driverid = :did"), {"did": driver_id})
        await direct_db.execute(
            text("DELETE FROM audit.auditlog WHERE entityname = 'Employees' AND entityid = :eid"),
            {"eid": str(employee_id)},
        )
        await direct_db.execute(
            text("DELETE FROM core.employees WHERE employeeid = :eid"), {"eid": employee_id},
        )
        residue = (await direct_db.execute(
            text("""
                SELECT
                    (SELECT COUNT(*) FROM core.drivers WHERE driverid = :did) AS drivers,
                    (SELECT COUNT(*) FROM core.employees WHERE employeeid = :eid) AS employees,
                    (SELECT COUNT(*) FROM payroll.driverpayrules WHERE driverid = :did) AS rules,
                    (SELECT COUNT(*) FROM audit.auditlog
                     WHERE entityname = 'DriverPayRules' AND entityid = ANY(:ids)) AS rule_audit
            """), {"did": driver_id, "eid": employee_id, "ids": rule_id_strings},
        )).mappings().one()
        assert all(value == 0 for value in residue.values()), (
            f"Owned Driver cleanup left residue for {driver_id}: {dict(residue)}"
        )


@pytest_asyncio.fixture
async def owned_branch_id(direct_db) -> int:
    """Create a function-owned branch and require its business graph to be removed."""
    from sqlalchemy import text

    marker = uuid4().hex
    branch_id = (await direct_db.execute(
        text("""
            INSERT INTO core.branches
                (companyid, branchcode, branchname, status, isdefault)
            VALUES (1, :code, :name, 'Active', FALSE)
            RETURNING branchid
        """), {"code": f"OWN-{marker[:12]}", "name": f"Owned test branch {marker[:8]}"},
    )).scalar_one()
    try:
        yield int(branch_id)
    finally:
        residue = (await direct_db.execute(
            text("""
                SELECT
                    (SELECT COUNT(*) FROM payroll.payrollperiods WHERE branchid = :bid) AS periods,
                    (SELECT COUNT(*) FROM core.drivers WHERE branchid = :bid) AS drivers,
                    (SELECT COUNT(*) FROM payroll.branchpayitemconfig WHERE branchid = :bid) AS item_config,
                    (SELECT COUNT(*) FROM sec.userbranchroles WHERE branchid = :bid) AS user_roles
            """), {"bid": branch_id},
        )).mappings().one()
        assert all(value == 0 for value in residue.values()), (
            f"Owned branch {branch_id} retained business state: {dict(residue)}"
        )
        await direct_db.execute(
            text("DELETE FROM core.branches WHERE branchid = :bid"), {"bid": branch_id},
        )
        remaining = (await direct_db.execute(
            text("SELECT COUNT(*) FROM core.branches WHERE branchid = :bid"), {"bid": branch_id},
        )).scalar_one()
        assert remaining == 0, f"Owned branch {branch_id} cleanup left {remaining} row(s)"


@pytest_asyncio.fixture(scope="session")
async def hq_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    hq_branch_id: int,
) -> int:
    """Create one driver on the HQ branch at session start; return its driver_id."""
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id":      hq_branch_id,
            "full_name":      "HQ Driver",
            "preferred_name": "HQD",
            "driver_code":    "HQD-001",
            "cdl_number":     "CDL-HQ-001",
            "email":          "hqd1@example.com",
        },
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 201, f"HQ driver seed failed: {resp.text}"
    return resp.json()["driver_id"]


@pytest_asyncio.fixture(scope="session")
async def paytest_rate_type_id(session_client: httpx.AsyncClient, auth_token: str) -> int:
    """
    Return the rate_type_id for HOURLY (seeded in _SEED_STMTS).
    Rate tests use this fixture to avoid hard-coding database-assigned IDs.
    """
    resp = await session_client.get(
        "/payroll/rate-types",
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 200, f"Rate-type lookup failed: {resp.text}"
    for rt in resp.json():
        if rt["rate_code"] == "HOURLY":
            return rt["rate_type_id"]
    raise AssertionError("HOURLY rate type not found — check seed data")


@pytest_asyncio.fixture(scope="session")
async def paytest_mileage_rate_type_id(session_client: httpx.AsyncClient, auth_token: str) -> int:
    """Return the rate_type_id for MILEAGE."""
    resp = await session_client.get(
        "/payroll/rate-types",
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 200
    for rt in resp.json():
        if rt["rate_code"] == "MILEAGE":
            return rt["rate_type_id"]
    raise AssertionError("MILEAGE rate type not found — check seed data")


@pytest_asyncio.fixture(scope="session")
async def branch_user_token(session_client: httpx.AsyncClient) -> str:
    """
    JWT for the branch-limited read-only test user.

    branch_user has:
      - SpecificBranch scope restricted to HQ only
      - PAYROLL_VIEWER role with no write permissions

    Used by tests that verify cross-branch denial (403) and
    action-level permission denial (403).
    """
    resp = await session_client.post("/auth/login", json={
        "username":     "branch_user",
        "password":     "TestPass123!",
        "company_code": "DEMO",
    })
    assert resp.status_code == 200, f"Branch user auth failed: {resp.text}"
    return resp.json()["access_token"]


async def _activate_branch_items(
    session_client,
    auth_token: str,
    branch_id: int,
    codes_to_activate: set,
    *,
    force: bool = False,
) -> None:
    """
    Helper: activate a set of pay items by code on a specific branch.

    When force=True, PATCH is sent even if the item already reports is_active=True.
    This is required for items with IsDefaultBranchActive=TRUE: they report
    is_active=True without having a BranchPayItemConfig row.  The rate matrix
    uses an INNER JOIN on BranchPayItemConfig, so items without an explicit row
    do NOT appear in the matrix.  Sending the PATCH creates the config row.

    Idempotent — safe to call more than once (ON CONFLICT DO NOTHING in service).
    """
    items_resp = await session_client.get(
        f"/settings/branches/{branch_id}/pay-items",
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert items_resp.status_code == 200, f"pay-items lookup failed: {items_resp.text}"
    for item in items_resp.json():
        if item["pay_item_code"] in codes_to_activate:
            if force or not item.get("is_active", True):
                await session_client.patch(
                    f"/settings/branches/{branch_id}/pay-items/{item['pay_item_id']}",
                    json={"is_active": True},
                    headers={"Authorization": f"Bearer {auth_token}"},
                )


@pytest_asyncio.fixture(scope="session", autouse=True)
async def activate_paytest_system_items(
    session_client: httpx.AsyncClient,
    auth_token: str,
    seeded_paytest_branch_id: int,
) -> None:
    """
    Activate system pay items on the PAYTEST branch so that tests using
    'Overnight', 'Wait', 'PTO', etc. pass the branch-activation check.

    HOURS and MILES are force-activated even though IsDefaultBranchActive=TRUE
    so that explicit BranchPayItemConfig rows exist.  These rows are required
    by the rate matrix (INNER JOIN on BranchPayItemConfig) to show HOURLY and
    MILEAGE for PAYTEST drivers.

    Called once per test session (autouse + session scope).
    """
    await _activate_branch_items(
        session_client, auth_token, seeded_paytest_branch_id,
        {"OVERNIGHT", "WAIT_TIME", "PALLETS", "SILOS"},
    )
    # Force-activate so BranchPayItemConfig rows are created for matrix queries
    await _activate_branch_items(
        session_client, auth_token, seeded_paytest_branch_id,
        {"HOURS", "MILES"},
        force=True,
    )


    # NOTE: HQ branch is intentionally NOT force-activated here.
    # test_settings_payitems::test_hours_default_active asserts that HOURS on HQ
    # has is_using_default=True (no explicit BranchPayItemConfig row).  Creating
    # an explicit row would break that test.  Tests that need the rate matrix for
    # HQ drivers should use paytest_driver_id (PAYTEST branch) which is activated
    # above, or query pay_item_id from the settings API rather than the matrix.


@pytest_asyncio.fixture
async def direct_db(test_engine):
    """
    Function-scoped direct AsyncConnection for tests that need raw DB manipulation,
    e.g. deleting tier rows to test approval guards that cannot be reached via the API.

    Uses AUTOCOMMIT isolation so every statement is immediately visible to other
    connections (including the FastAPI test app's connections).

    Usage in a test:
        from sqlalchemy import text as _text
        async def test_foo(direct_db):
            await direct_db.execute(_text("DELETE FROM ... WHERE ..."), {"k": v})
    """

    # AUTOCOMMIT: each statement committed immediately — no BEGIN/COMMIT needed
    async with test_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        async with preserve_test_guc_state(conn):
            yield conn


@pytest_asyncio.fixture(scope="session")
async def hq_branch_id(session_client: httpx.AsyncClient, auth_token: str) -> int:
    """Return the branch_id for the HQ branch."""
    resp = await session_client.get(
        "/core/branches",
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 200
    for branch in resp.json():
        if branch["branch_code"] == "HQ":
            return branch["branch_id"]
    raise AssertionError("HQ branch not found — check seed data")
