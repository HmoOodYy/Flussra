"""The development database is replaced whole, and only when every lifecycle check holds.

The module owns a PostgreSQL cluster: a superuser lifecycle role, and an application role
without SUPERUSER or CREATEDB, the shape of a real development setup. The reset tool runs as a
developer runs it; the shared test cluster stands in for a different server.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import psycopg2
import pytest
from psycopg2 import sql

from app.db.schema_guard import run_schema_guard
from scripts.canonical_demo import PAYROLL_HISTORY_TABLES
from tests.conftest import _stop_test_postgresql_safely
from tests.postgresql_compat import create_test_postgresql

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESET_SCRIPT = os.path.join(BACKEND, "scripts", "reset_dev_database.py")
APP_ROLE, TARGET = "flussra_app", "flussra_dev"


@pytest.fixture(scope="module")
def server():
    cluster = create_test_postgresql()
    try:
        dsn = cluster.dsn()
        _run({**dsn, "database": "postgres"},
             f"CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
        base = f"{dsn['host']}:{dsn['port']}"
        yield {
            "dsn": dsn,
            "admin_url": f"postgresql://{dsn['user']}@{base}/postgres",
            "app_url": f"postgresql+asyncpg://{APP_ROLE}@{base}/{TARGET}",
            "app_dsn": {**dsn, "user": APP_ROLE, "database": TARGET},
            "target_dsn": {**dsn, "database": TARGET},
        }
    finally:
        _stop_test_postgresql_safely(cluster)


def _run(dsn, statement, params=None):
    conn = psycopg2.connect(**dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cursor:
            cursor.execute(statement, params)
            return cursor.fetchall() if cursor.description else None
    finally:
        conn.close()


def _database(server, name=TARGET):
    rows = _run({**server["dsn"], "database": "postgres"}, """
        SELECT oid, pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s""", (name,))
    return rows[0] if rows else None


def _env(server, **overrides):
    env = {**os.environ, "DATABASE_URL": server["app_url"], "SECRET_KEY": "lifecycle-tests",
           "ENVIRONMENT": "development"}
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _reset(server, tmp_path, *args, **env):
    return subprocess.run([sys.executable, RESET_SCRIPT, *args], cwd=tmp_path,
                          env=_env(server, **env), capture_output=True, text=True, check=False)


def _reset_with_failure(server, tmp_path, patch):
    """Run the reset in a fresh process with one step replaced by a failure."""
    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {BACKEND!r})
        from scripts import reset_dev_database as tool
        {patch}
        try:
            tool.reset_and_reseed({server['app_url']!r}, admin_url={server['admin_url']!r},
                                  confirm={TARGET!r})
        except tool.ResetError as exc:
            print(exc, file=sys.stderr)
            sys.exit(1)
    """)
    return subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=_env(server),
                          capture_output=True, text=True, check=False)


def _assert_canonical(server):
    assert _run(server["app_dsn"], "SELECT version_num FROM public.alembic_version") == [("0093",)]
    run_schema_guard(server["app_url"], is_dev=True)
    for table in PAYROLL_HISTORY_TABLES:
        assert _run(server["app_dsn"], f"SELECT count(*) FROM {table}") == [(0,)], table
    assert _run(server["app_dsn"], "SELECT companycode, currencycode FROM core.companies") == [
        ("DEMO", "USD")]


@pytest.fixture(scope="module")
def rebuilt(server, tmp_path_factory):
    """The target did not exist: the reset creates and rebuilds it from repository authorities."""
    assert _database(server) is None
    result = _reset(server, tmp_path_factory.mktemp("rebuilt"),
                    "--admin-url", server["admin_url"], "--confirm", TARGET)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "canonical demo verified" in result.stdout
    return server


# ---------------------------------------------------------------------------
# Recreation and the privilege model
# ---------------------------------------------------------------------------

def test_an_absent_database_is_created_and_rebuilt_with_the_least_privilege_model(rebuilt):
    server = rebuilt
    _assert_canonical(server)
    lifecycle = server["dsn"]["user"]
    assert _database(server)[1] == lifecycle  # the lifecycle role owns the database
    privileges = _run(server["target_dsn"], """
        SELECT has_database_privilege(%(app)s, current_database(), 'CONNECT'),
               has_database_privilege(%(app)s, current_database(), 'CREATE'),
               has_database_privilege(%(app)s, current_database(), 'TEMPORARY'),
               has_schema_privilege(%(app)s, 'public', 'CREATE'),
               (SELECT rolsuper OR rolcreatedb FROM pg_roles WHERE rolname = %(app)s),
               (SELECT count(*) FROM pg_namespace n
                WHERE nspname IN ('app', 'audit', 'core', 'import', 'integration', 'payroll',
                                  'review', 'sec')
                  AND pg_get_userbyid(nspowner) = %(app)s)
    """, {"app": APP_ROLE})
    assert privileges == [(True, True, True, True, False, 8)]
    # The application credential alone holds no database lifecycle power.
    with pytest.raises(psycopg2.Error, match="must be owner of database"):
        _run({**server["dsn"], "user": APP_ROLE, "database": "postgres"},
             sql.SQL("DROP DATABASE {}").format(sql.Identifier(TARGET)))
    with pytest.raises(psycopg2.Error, match="permission denied to create database"):
        _run({**server["dsn"], "user": APP_ROLE, "database": "postgres"}, "CREATE DATABASE app_made")


# ---------------------------------------------------------------------------
# Every check before DROP
# ---------------------------------------------------------------------------

def _other_server_url(pg_instance):
    dsn = pg_instance.dsn()
    return f"postgresql://{dsn['user']}@{dsn['host']}:{dsn['port']}/postgres"


@pytest.mark.parametrize("args, env, message", [
    (("--confirm", TARGET, "--admin-url", "{admin}"), {"ENVIRONMENT": None},
     "ENVIRONMENT=development to be set explicitly"),
    (("--confirm", TARGET, "--admin-url", "{admin}"), {"ENVIRONMENT": "production"},
     "ENVIRONMENT=development to be set explicitly"),
    (("--confirm", TARGET), {}, "requires --admin-url"),
    (("--confirm", "flussra_dev_typo", "--admin-url", "{admin}"), {},
     "--confirm names 'flussra_dev_typo', but DATABASE_URL configures 'flussra_dev'"),
    (("--confirm", "postgres", "--admin-url", "{admin}"), {},
     "--confirm names 'postgres', but DATABASE_URL configures 'flussra_dev'"),
    (("--confirm", TARGET, "--admin-url", "{admin_on_target}"), {},
     "must connect to another database"),
    (("--confirm", TARGET, "--admin-url", "{application}"), {},
     "lifecycle role 'flussra_app' cannot create databases"),
    (("--confirm", TARGET, "--admin-url", "{unreachable}"), {},
     "Could not open the lifecycle connection; nothing was changed"),
], ids=["defaulted-environment", "production-environment", "no-lifecycle-credential",
        "wrong-confirmation", "confirmation-of-another-database", "control-is-target",
        "application-credential-as-lifecycle", "lifecycle-connection-failure"])
def test_every_failed_check_leaves_the_database_untouched(rebuilt, tmp_path, args, env, message):
    server = rebuilt
    dsn = server["dsn"]
    urls = {
        "admin": server["admin_url"],
        "admin_on_target": f"postgresql://{dsn['user']}@{dsn['host']}:{dsn['port']}/{TARGET}",
        "application": f"postgresql://{APP_ROLE}@{dsn['host']}:{dsn['port']}/postgres",
        "unreachable": f"postgresql://{dsn['user']}@{dsn['host']}:1/postgres",
    }
    before = _database(server)
    result = _reset(server, tmp_path, *[a.format(**urls) for a in args], **env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stderr
    assert _database(server) == before
    assert _database(server, "postgres") is not None


def test_a_lifecycle_connection_to_another_server_is_refused(rebuilt, pg_instance, tmp_path):
    """Identity, not host name: the other server even has a role of the same name."""
    other = {**pg_instance.dsn(), "database": "postgres"}
    _run(other, f"CREATE ROLE {APP_ROLE} LOGIN")
    try:
        before = _database(rebuilt)
        result = _reset(rebuilt, tmp_path, "--admin-url", _other_server_url(pg_instance),
                        "--confirm", TARGET)
        assert result.returncode == 1
        assert "reach different PostgreSQL servers" in result.stderr
        assert _database(rebuilt) == before
    finally:
        _run(other, f"DROP ROLE {APP_ROLE}")


def test_a_database_owned_by_another_role_is_refused(rebuilt, tmp_path):
    admin = {**rebuilt["dsn"], "database": "postgres"}
    _run(admin, "CREATE ROLE stranger NOLOGIN")
    _run(admin, "CREATE DATABASE strangers_db OWNER stranger")
    try:
        before = _database(rebuilt, "strangers_db")
        url = rebuilt["app_url"].rsplit("/", 1)[0] + "/strangers_db"
        result = _reset(rebuilt, tmp_path, "--admin-url", rebuilt["admin_url"],
                        "--confirm", "strangers_db", DATABASE_URL=url)
        assert result.returncode == 1
        assert "owned by 'stranger'" in result.stderr
        assert _database(rebuilt, "strangers_db") == before
    finally:
        _run(admin, "DROP DATABASE strangers_db")
        _run(admin, "DROP ROLE stranger")


# ---------------------------------------------------------------------------
# Active sessions, and nothing inside the old database survives
# ---------------------------------------------------------------------------

def test_an_open_session_refuses_the_reset_until_it_closes_and_nothing_old_survives(rebuilt, tmp_path):
    server = rebuilt
    # Arbitrary contents of the disposable database, including the shapes that once made a
    # partial reset unsafe: custom schemas, public objects, catalog look-alikes and an external
    # domain over a Flussra type.
    for statement in (
        "CREATE SCHEMA scratch",
        "CREATE TABLE scratch.notes (body TEXT)",
        "CREATE TABLE public.leftover (id INTEGER)",
        "CREATE TABLE public.pg_class (oid INTEGER)",
        "CREATE TYPE core.app_kind AS ENUM ('one')",
        "CREATE DOMAIN public.unrelated_state AS core.app_kind",
        "CREATE VIEW public.company_names AS SELECT companyname FROM core.companies",
    ):
        _run(server["app_dsn"], statement)
    # Anything added to template1 must not reach the new database: it is built from template0.
    _run({**server["dsn"], "database": "template1"}, "CREATE TABLE public.from_template1 (id INTEGER)")
    try:
        before = _database(server)
        other = psycopg2.connect(**server["app_dsn"])
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                pid = cursor.fetchone()[0]
            refused = _reset(server, tmp_path, "--admin-url", server["admin_url"], "--confirm", TARGET)
            assert refused.returncode == 1
            assert "other sessions are connected" in refused.stderr and f"pid {pid}" in refused.stderr
            with other.cursor() as cursor:  # never terminated
                cursor.execute("SELECT 1")
                assert cursor.fetchone() == (1,)
            assert _database(server) == before
        finally:
            other.close()

        result = _reset(server, tmp_path, "--admin-url", server["admin_url"], "--confirm", TARGET)
        assert result.returncode == 0, result.stdout + result.stderr
        assert _database(server)[0] != before[0]  # a different database
        _assert_canonical(server)
        assert _run(server["app_dsn"], """
            SELECT to_regnamespace('scratch'), to_regclass('public.leftover'),
                   to_regclass('public.pg_class'), to_regtype('core.app_kind'),
                   to_regtype('public.unrelated_state'), to_regclass('public.company_names'),
                   to_regclass('public.from_template1')
        """) == [(None,) * 7]
    finally:
        _run({**server["dsn"], "database": "template1"}, "DROP TABLE public.from_template1")


# ---------------------------------------------------------------------------
# Failures after DROP: reported, and repaired by running the reset again
# ---------------------------------------------------------------------------

def test_a_failed_create_reports_an_absent_database_and_the_next_run_recreates_it(rebuilt, tmp_path):
    failed = _reset_with_failure(rebuilt, tmp_path, """
        async def create_database(*args):
            raise OSError("simulated CREATE DATABASE failure")
        tool.create_database = create_database
    """)
    assert failed.returncode == 1
    assert "now absent or incomplete" in failed.stderr and "run the reset again" in failed.stderr
    assert _database(rebuilt) is None
    retried = _reset(rebuilt, tmp_path, "--admin-url", rebuilt["admin_url"], "--confirm", TARGET)
    assert retried.returncode == 0, retried.stdout + retried.stderr
    _assert_canonical(rebuilt)


def test_a_failed_rebuild_is_reported_and_never_claims_a_canonical_reset(rebuilt, tmp_path):
    failed = _reset_with_failure(rebuilt, tmp_path, """
        def upgrade_to_head(url):
            raise tool.ResetError("simulated migration failure")
        tool.upgrade_to_head = upgrade_to_head
    """)
    assert failed.returncode == 1
    assert "was recreated, but rebuilding it failed" in failed.stderr
    assert "simulated migration failure" in failed.stderr
    assert "canonical demo verified" not in failed.stdout
    assert _database(rebuilt) is not None
    assert _run(rebuilt["app_dsn"], "SELECT to_regclass('public.alembic_version')") == [(None,)]
    retried = _reset(rebuilt, tmp_path, "--admin-url", rebuilt["admin_url"], "--confirm", TARGET)
    assert retried.returncode == 0, retried.stdout + retried.stderr
    _assert_canonical(rebuilt)
