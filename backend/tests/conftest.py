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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from tests.database_templates import SchemaDatabases
from tests.db_state import (
    assert_trigger_fingerprints_equal,
    preserve_test_guc_state,
    read_trigger_fingerprint,
)
from tests.ownership import delete_driver_and_residue, pay_definition_ownership_scope
from tests.postgresql_compat import create_test_postgresql

# Tests that need process configuration must never inherit a developer DSN or
# secret. Database access itself remains owned by the testing.postgresql fixture.
os.environ["DATABASE_URL"] = "postgresql+asyncpg://test:test@127.0.0.1:1/test_configuration_only"
os.environ["SECRET_KEY"] = "test-only-secret-key-never-use-outside-tests-000000000000000000000000"
os.environ["ENVIRONMENT"] = "test"

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

pytest_plugins = ["tests.execution"]


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
    INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended, currencycode)
    VALUES ('DEMO', 'Demo Logistics', 'Demo Logistics Ltd', 'Active', FALSE, 'USD')
    """,
    # Every Company starts with its default Status rate column (and the rate definition it owns).
    """
    SELECT payroll.fn_EnsureDefaultStatusRateColumn(companyid)
    FROM core.companies WHERE companycode = 'DEMO'
    """,
    """
    INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault, timezoneid)
    SELECT companyid, 'HQ', 'Headquarters', 'Active', TRUE, 'America/New_York'
    FROM core.companies WHERE companycode = 'DEMO'
    """,
    # Second branch — used by payroll-period tests so the one-Draft-per-branch
    # and one-Open-per-branch constraints don't conflict with the HQ seed period.
    """
    INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault, timezoneid)
    SELECT companyid, 'PAYTEST', 'Payroll Test Branch', 'Active', FALSE, 'America/New_York'
    FROM core.companies WHERE companycode = 'DEMO'
    """,
    # Permission codes for write operations (used by action-level permission guards).
    # ON CONFLICT DO NOTHING: migration 0015 may already have inserted some of these.
    """
    INSERT INTO sec.permissions (permissioncode, permissionname, modulecode) VALUES
        ('payroll.entry',          'Enter Payroll Data',       'payroll'),
        ('payroll.period.create',  'Create Payroll Period',    'payroll'),
        ('payroll.approve_rate',   'Approve Pay Rates',        'payroll'),
        ('payroll.finalize',       'Finalize Payroll',         'payroll'),
        ('employees.view',         'View Employees',           'employees'),
        ('employees.manage',       'Manage Employees',         'employees'),
        ('review.decide',          'Decide on Review Items',   'review'),
        ('setup.manage',           'Manage Settings',          'settings')
    ON CONFLICT (permissioncode) DO NOTHING
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
    # Must NOT be rolecode='DRIVER' so require_non_driver_subject passes.
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
    # Admin user assignment: CompanyRole is the sole role authority.
    """
    INSERT INTO sec.userbranchroles
        (userid, companyid, branchid, companyroleId, scopetype, isactive)
    SELECT u.userid, u.companyid, NULL,
           (SELECT cr.companyroleid FROM sec.companyroles cr
            JOIN core.companies c2 ON c2.companyid = cr.companyid
            WHERE c2.companycode = 'DEMO' AND cr.rolecode = 'COMPANY_OWNER'),
           'AllCompanyBranches', TRUE
    FROM sec.users u
    WHERE u.username = 'admin'
    """,
    # Branch-limited read-only test user: SpecificBranch scope to HQ only,
    # PAYROLL_VIEWER_CO CompanyRole (grants payroll.view only — NOT DRIVER so
    # require_non_driver_subject passes).
    # Password is the same as admin ("TestPass123!") — only the role and scope differ.
    """
    INSERT INTO sec.users (companyid, username, displayname, passwordhash, isactive, canlogin)
    SELECT c.companyid, 'branch_user', 'Branch User', %(pw_hash)s, TRUE, TRUE
    FROM core.companies c WHERE c.companycode = 'DEMO'
    """,
    # branch_user assignment: SpecificBranch HQ, CompanyRole PAYROLL_VIEWER_CO
    """
    INSERT INTO sec.userbranchroles
        (userid, companyid, branchid, companyroleId, scopetype, isactive)
    SELECT u.userid, u.companyid, b.branchid,
           (SELECT cr.companyroleid FROM sec.companyroles cr
            JOIN core.companies c2 ON c2.companyid = cr.companyid
            WHERE c2.companycode = 'DEMO' AND cr.rolecode = 'PAYROLL_VIEWER_CO'),
           'SpecificBranch', TRUE
    FROM   sec.users u, core.branches b
    WHERE  u.username  = 'branch_user'
      AND  b.branchcode = 'HQ'
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
def schema_databases(pg_instance):
    """One pristine schema per authority/worker; every consumer gets an isolated clone."""
    databases = SchemaDatabases(pg_instance.dsn(), Path(__file__).resolve().parents[2])
    try:
        yield databases
    finally:
        databases.close()


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
async def db_conn(test_engine, pay_definition_ownership) -> AsyncGenerator[AsyncConnection, None]:
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
async def pay_definition_ownership(test_engine):
    """Per-test exact-ID ownership scope shared by HTTP and raw DB fixtures."""
    async with pay_definition_ownership_scope(test_engine) as ownership:
        yield ownership


@pytest_asyncio.fixture
async def client(test_app, pay_definition_ownership) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async HTTP client for each test function."""
    from tests.ownership import track_function_client_pay_definitions

    # httpx >= 0.20 removed the app= shorthand; use ASGITransport instead.
    transport = ASGITransport(app=test_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as c:
        async with track_function_client_pay_definitions(c, test_app.state.engine):
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
    Create one driver record at session start; return its permanent driver_id.
    Target Workforce setup is performed once for tests using this fixture.
    one INSERT happens for the entire session.
    """
    resp = await session_client.post(
        "/workforce/employees",
        json={
            "branch_id": 1,
            "full_name": "Test Driver One",
            "preferred_name": "TD1",
            "employee_key": "TD-EMP-001",
            "email": "td1@example.com",
            "hire_date": "2020-01-01",
            "driver": {
                "driver_number": "TD-001",
                "cdl_number": "CDL-TEST-001",
                "effective_from_at": "2020-01-01T00:00:00Z",
            },
        },
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 201, f"Driver seed failed: {resp.text}"
    return resp.json()["driver"]["driver_id"]


async def _current_driver_assignment_id(
    client: httpx.AsyncClient, token: str, driver_id: int,
) -> int:
    resp = await client.get(
        "/core/driver-assignments", headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, f"Driver roster lookup failed: {resp.text}"
    row = next((item for item in resp.json() if item["driver_id"] == driver_id and item["state"] == "current"), None)
    assert row is not None, f"Current DriverAssignment not found for Driver {driver_id}"
    return row["driver_assignment_id"]


@pytest_asyncio.fixture(scope="session")
async def created_driver_assignment_id(
    session_client: httpx.AsyncClient, auth_token: str, created_driver_id: int,
) -> int:
    """The DriverAssignmentID (home-Branch tenure) of ``created_driver_id``."""
    return await _current_driver_assignment_id(session_client, auth_token, created_driver_id)


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
    Create one driver on the seeded PAYTEST branch at session start; return its permanent driver_id.
    Depends on `seeded_paytest_branch_id`, not the shadowable `paytest_branch_id`: this
    session-cached driver must never land on whichever module-owned branch happened to
    request it first. Modules that own their branch shadow this fixture as well.
    """
    resp = await session_client.post(
        "/workforce/employees",
        json={
            "branch_id":      seeded_paytest_branch_id,
            "full_name":      "Paytest Driver",
            "preferred_name": "PTD",
            "employee_key":   "PTD-EMP-001",
            "email":          "ptd1@example.com",
            "hire_date":      "2020-01-01",
            "driver": {
                "driver_number": "PTD-001",
                "cdl_number": "CDL-PT-001",
                "effective_from_at": "2020-01-01T00:00:00Z",
            },
        },
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 201, f"PAYTEST driver seed failed: {resp.text}"
    return resp.json()["driver"]["driver_id"]


@pytest_asyncio.fixture(scope="session")
async def paytest_driver_assignment_id(
    session_client: httpx.AsyncClient, auth_token: str, paytest_driver_id: int,
) -> int:
    """The DriverAssignmentID (PAYTEST home-Branch tenure) of ``paytest_driver_id``.

    Payroll provenance (draft lines, entries, rates) is keyed by this id.
    """
    return await _current_driver_assignment_id(session_client, auth_token, paytest_driver_id)


@pytest_asyncio.fixture
async def owned_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
) -> int:
    """Create and remove a test-owned Driver used by isolated rule scenarios.

    Yields the permanent driver_id; ``owned_driver_assignment_id`` yields its assignment.
    """

    marker = uuid4().hex
    created = await session_client.post(
        "/workforce/employees",
        json={
            "branch_id": paytest_branch_id,
            "full_name": f"Owned rule scenario {marker}",
            "employee_key": f"OWN-EMP-{marker[:8]}",
            "hire_date": "2020-01-01",
            "driver": {
                "driver_number": f"OWN-{marker[:10]}",
                "effective_from_at": "2020-01-01T00:00:00Z",
            },
        },
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert created.status_code == 201, f"Owned Driver create failed: {created.text}"
    body = created.json()
    driver_id = body["driver"]["driver_id"]
    employee_id = body["employee_id"]
    try:
        yield driver_id
    finally:
        dependents = (await direct_db.execute(
            text("""
                SELECT
                    (SELECT COUNT(*) FROM payroll.payrollfinallines WHERE driverid = :did) AS final_lines,
                    (SELECT COUNT(*) FROM payroll.payrolldraftlines dl
                     JOIN core.driverassignments a ON a.driverassignmentid = dl.driverassignmentid
                     WHERE a.driverid = :did) AS draft_lines,
                    (SELECT COUNT(*) FROM payroll.payrollbonusevents b
                     JOIN core.driverassignments a ON a.driverassignmentid = b.driverassignmentid
                     WHERE a.driverid = :did) AS bonuses,
                    (SELECT COUNT(*) FROM payroll.payrollperioddriverdayentrystate s
                     JOIN core.driverassignments a ON a.driverassignmentid = s.driverassignmentid
                     WHERE a.driverid = :did) AS day_state
            """), {"did": driver_id},
        )).mappings().one()
        assert all(value == 0 for value in dependents.values()), (
            f"Owned Driver {driver_id} has unexpected non-rule dependents: {dict(dependents)}"
        )
        await delete_driver_and_residue(direct_db, driver_id, employee_id)


@pytest_asyncio.fixture
async def owned_driver_assignment_id(
    session_client: httpx.AsyncClient, auth_token: str, owned_driver_id: int,
) -> int:
    """The DriverAssignmentID of the test-owned Driver."""
    return await _current_driver_assignment_id(session_client, auth_token, owned_driver_id)


@pytest_asyncio.fixture
async def owned_branch_id(direct_db) -> int:
    """Create a function-owned branch and require its business graph to be removed."""

    marker = uuid4().hex
    branch_id = (await direct_db.execute(
        text("""
            INSERT INTO core.branches
                (companyid, branchcode, branchname, status, isdefault, timezoneid)
            VALUES (1, :code, :name, 'Active', FALSE, 'UTC')
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
                    (SELECT COUNT(*) FROM core.driverassignments WHERE branchid = :bid) AS driver_assignments,
                    (SELECT COUNT(*) FROM payroll.branchpaydefinitionconfig WHERE branchid = :bid) AS item_config,
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
    """Create one driver on the HQ branch at session start; return its permanent driver_id."""
    resp = await session_client.post(
        "/workforce/employees",
        json={
            "branch_id":      hq_branch_id,
            "full_name":      "HQ Driver",
            "preferred_name": "HQD",
            "employee_key":   "HQD-EMP-001",
            "email":          "hqd1@example.com",
            "hire_date":      "2020-01-01",
            "driver": {
                "driver_number": "HQD-001",
                "cdl_number": "CDL-HQ-001",
                "effective_from_at": "2020-01-01T00:00:00Z",
            },
        },
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 201, f"HQ driver seed failed: {resp.text}"
    return resp.json()["driver"]["driver_id"]


@pytest_asyncio.fixture(scope="session")
async def hq_driver_assignment_id(
    session_client: httpx.AsyncClient, auth_token: str, hq_driver_id: int,
) -> int:
    """The DriverAssignmentID (HQ home-Branch tenure) of ``hq_driver_id``."""
    return await _current_driver_assignment_id(session_client, auth_token, hq_driver_id)


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


@pytest_asyncio.fixture
async def direct_db(test_engine, pay_definition_ownership):
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
