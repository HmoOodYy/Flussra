"""Who may reset a development database, and what a reset may destroy.

Each scenario runs against an invocation-owned database owned by a role without SUPERUSER or
CREATEDB (the shape of a real development database), upgraded to the head and provisioned by a
separate administrator. A refused reset must leave every Flussra schema and every external
object in place.
"""
from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from uuid import uuid4

import psycopg2
import pytest
import pytest_asyncio
from psycopg2 import sql

from app.config import Settings
from scripts.canonical_demo import PAYROLL_HISTORY_TABLES
from scripts.provision_dev_database import provision
from scripts.reset_dev_database import (
    ENVIRONMENT_SETTING,
    RESET_CAPABILITY_SETTING,
    RESET_SCHEMAS,
    ResetError,
    boundary_violations,
    drop_application_objects,
    explicit_development,
    reset_refusal,
)
from tests.test_company_status_catalog_migration import _alembic

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESET_SCRIPT = os.path.join(BACKEND, "scripts", "reset_dev_database.py")


@pytest_asyncio.fixture(scope="module")
async def owned(pg_instance):
    """A head database owned by a non-superuser, provisioned by the administrator."""
    admin_dsn = pg_instance.dsn()
    suffix = uuid4().hex[:10]
    role, name = f"reset_owner_{suffix}", f"reset_safety_{suffix}"
    admin = psycopg2.connect(**admin_dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
                       .format(sql.Identifier(role)))
        cursor.execute(sql.SQL("CREATE DATABASE {} OWNER {}")
                       .format(sql.Identifier(name), sql.Identifier(role)))
    host, port = admin_dsn["host"], admin_dsn["port"]
    owner_url = f"postgresql+asyncpg://{role}@{host}:{port}/{name}"
    admin_url = f"postgresql://{admin_dsn['user']}@{host}:{port}/{name}"
    env = {**os.environ, "DATABASE_URL": owner_url, "SECRET_KEY": "development-reset-safety"}
    try:
        upgrade = _alembic(env, "upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stderr
        await provision(admin_url, confirm=name)
        yield {"name": name, "role": role, "owner_url": owner_url, "admin_url": admin_url,
               "env": env, "owner_dsn": {**admin_dsn, "user": role, "database": name},
               "admin_dsn": {**admin_dsn, "database": name}}
    finally:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
            cursor.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        admin.close()


def _run(dsn, statement, params=None):
    conn = psycopg2.connect(**dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cursor:
            cursor.execute(statement, params)
            return cursor.fetchall() if cursor.description else None
    finally:
        conn.close()


def _state(owned) -> dict:
    """What a refused reset must leave untouched."""
    return {
        "schemas": sorted(r[0] for r in _run(owned["owner_dsn"], """
            SELECT nspname FROM pg_namespace WHERE nspname = ANY(%s)""", (list(RESET_SCHEMAS),))),
        "relations": _run(owned["owner_dsn"], """
            SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = ANY(%s)""", (list(RESET_SCHEMAS),))[0][0],
        "version": _run(owned["owner_dsn"], "SELECT version_num FROM public.alembic_version")[0][0],
    }


@contextmanager
def _database_setting(owned, name, value):
    """Temporarily store (or with None, remove) one database-level setting as the administrator."""
    database = sql.Identifier(owned["name"])
    if value is None:
        _run(owned["admin_dsn"], sql.SQL("ALTER DATABASE {} RESET {}").format(database, sql.SQL(name)))
    else:
        _run(owned["admin_dsn"], sql.SQL("ALTER DATABASE {} SET {} = {}").format(
            database, sql.SQL(name), sql.Literal(value)))
    try:
        yield
    finally:
        restored = "development" if name == ENVIRONMENT_SETTING else "on"
        _run(owned["admin_dsn"], sql.SQL("ALTER DATABASE {} SET {} = {}").format(
            database, sql.SQL(name), sql.Literal(restored)))


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

_MARKED = {ENVIRONMENT_SETTING: "development", RESET_CAPABILITY_SETTING: "on"}


@pytest.mark.parametrize("settings, confirm, allowed", [
    (_MARKED, "dev", True),
    ({RESET_CAPABILITY_SETTING: "on"}, "dev", False),
    ({**_MARKED, ENVIRONMENT_SETTING: "production"}, "dev", False),
    ({**_MARKED, ENVIRONMENT_SETTING: "Development "}, "dev", False),
    ({ENVIRONMENT_SETTING: "development"}, "dev", False),
    ({**_MARKED, RESET_CAPABILITY_SETTING: "off"}, "dev", False),
    ({**_MARKED, RESET_CAPABILITY_SETTING: "true"}, "dev", False),
    (_MARKED, "other", False),
], ids=["marked", "environment-missing", "environment-production", "environment-malformed",
        "capability-missing", "capability-off", "capability-malformed", "wrong-confirmation"])
def test_reset_needs_the_database_identity_capability_and_confirmation(settings, confirm, allowed):
    # The decision has no input for host or network location: a remote database that carries
    # the settings is allowed, and a local one without them is refused.
    refusal = reset_refusal(database="dev", confirm=confirm, settings=settings)
    assert (refusal is None) is allowed, refusal


@pytest.mark.parametrize("environment, explicit", [
    (None, False), ("development", True), ("DEVELOPMENT", True), ("production", False),
    ("staging", False),
])
def test_only_an_explicitly_configured_development_runtime_counts(monkeypatch, environment, explicit):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused@127.0.0.1:1/unused")
    monkeypatch.setenv("SECRET_KEY", "unused")
    if environment is None:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("ENVIRONMENT", environment)
    settings = Settings(_env_file=None)
    if environment is None:
        assert settings.ENVIRONMENT == "development"  # the application default
    assert explicit_development(settings) is explicit


@pytest.mark.parametrize("environment", [None, "production"], ids=["defaulted", "production"])
def test_the_reset_command_refuses_a_runtime_that_is_not_explicitly_development(
    owned, tmp_path, environment,
):
    before = _state(owned)
    env = {k: v for k, v in owned["env"].items() if k != "ENVIRONMENT"}
    if environment:
        env["ENVIRONMENT"] = environment
    result = subprocess.run([sys.executable, RESET_SCRIPT, "--confirm", owned["name"]],
                            cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "ENVIRONMENT=development to be set explicitly" in result.stderr
    assert _state(owned) == before


@pytest.mark.parametrize("setting, value, reason", [
    (ENVIRONMENT_SETTING, None, "flussra.environment is None"),
    (ENVIRONMENT_SETTING, "production", "flussra.environment is 'production'"),
    (RESET_CAPABILITY_SETTING, None, "flussra.reset_allowed is None"),
    (RESET_CAPABILITY_SETTING, "off", "flussra.reset_allowed is 'off'"),
], ids=["unmarked-local-database", "marked-production", "capability-missing", "capability-off"])
async def test_a_database_that_does_not_allow_reset_is_left_unchanged(owned, setting, value, reason):
    before = _state(owned)
    with _database_setting(owned, setting, value):
        with pytest.raises(ResetError, match=reason):
            await drop_application_objects(owned["owner_url"], confirm=owned["name"])
    assert _state(owned) == before


async def test_a_wrong_database_confirmation_is_refused(owned):
    before = _state(owned)
    with pytest.raises(ResetError, match="--confirm names 'payroll_v3'"):
        await drop_application_objects(owned["owner_url"], confirm="payroll_v3")
    assert _state(owned) == before


async def test_the_application_role_cannot_provision_its_own_database(owned):
    with pytest.raises(ResetError, match="administrator"):
        await provision(owned["owner_url"], confirm=owned["name"])


async def test_provisioning_refuses_a_database_marked_as_another_environment(owned):
    with _database_setting(owned, ENVIRONMENT_SETTING, "production"):
        with pytest.raises(ResetError, match="marked flussra.environment = 'production'"):
            await provision(owned["admin_url"], confirm=owned["name"])
        assert _run(owned["admin_dsn"], """
            SELECT setconfig FROM pg_db_role_setting
            WHERE setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database())
              AND setrole = 0""")[0][0].count("flussra.environment=production") == 1


# ---------------------------------------------------------------------------
# Dependency boundary
# ---------------------------------------------------------------------------

def test_the_reset_schemas_are_exactly_the_schemas_a_fresh_upgrade_creates(owned):
    created = {r[0] for r in _run(owned["owner_dsn"], """
        SELECT nspname FROM pg_namespace
        WHERE nspname NOT IN ('public', 'information_schema') AND nspname NOT LIKE 'pg\\_%%'""")}
    assert created == set(RESET_SCHEMAS)


async def test_the_reviewer_external_domain_on_a_flussra_enum_blocks_the_reset(owned):
    _run(owned["owner_dsn"], "CREATE TYPE core.app_kind AS ENUM ('one', 'two')")
    _run(owned["owner_dsn"], "CREATE DOMAIN public.unrelated_state AS core.app_kind")
    try:
        before = _state(owned)
        with pytest.raises(ResetError, match=r"type public\.unrelated_state") as refused:
            await drop_application_objects(owned["owner_url"], confirm=owned["name"])
        assert "outside them" in str(refused.value)
        assert _state(owned) == before
        assert _run(owned["owner_dsn"], "SELECT to_regtype('public.unrelated_state')")[0][0]
        assert _run(owned["owner_dsn"], "SELECT to_regtype('core.app_kind')")[0][0]
    finally:
        _run(owned["owner_dsn"], "DROP DOMAIN public.unrelated_state")
        _run(owned["owner_dsn"], "DROP TYPE core.app_kind")


@pytest.mark.parametrize("create, expected, drop", [
    ("CREATE VIEW public.company_names AS SELECT companyname FROM core.companies",
     "view public.company_names", "DROP VIEW public.company_names"),
    ("CREATE TABLE public.notes (company INTEGER REFERENCES core.companies (companyid))",
     "on public.notes", "DROP TABLE public.notes"),
    ("CREATE TABLE public.users_copy (id INTEGER, u sec.users)",
     "table column public.users_copy.u", "DROP TABLE public.users_copy"),
    ("CREATE SCHEMA scratch; CREATE VIEW scratch.logins AS SELECT username FROM sec.users",
     "view scratch.logins", "DROP SCHEMA scratch CASCADE"),
    ("CREATE TABLE public.audited (id INTEGER); CREATE TRIGGER audited_guard BEFORE DELETE "
     "ON public.audited FOR EACH ROW EXECUTE FUNCTION audit.fn_guard_audit_log_immutable()",
     "trigger audited_guard on public.audited", "DROP TABLE public.audited"),
    ("CREATE FUNCTION core.fold_label(TEXT) RETURNS TEXT IMMUTABLE LANGUAGE sql "
     "AS 'SELECT lower($1)'; CREATE TABLE public.labels (v TEXT); "
     "CREATE INDEX labels_folded ON public.labels (core.fold_label(v))",
     "index public.labels_folded",
     "DROP TABLE public.labels; DROP FUNCTION core.fold_label(TEXT)"),
], ids=["view", "foreign-key", "column-type", "other-schema-view", "trigger", "expression-index"])
async def test_any_external_dependent_of_a_flussra_object_blocks_the_reset(owned, create, expected, drop):
    _run(owned["owner_dsn"], create)
    try:
        before = _state(owned)
        with pytest.raises(ResetError) as refused:
            await drop_application_objects(owned["owner_url"], confirm=owned["name"])
        assert expected in str(refused.value)
        assert _state(owned) == before
    finally:
        _run(owned["owner_dsn"], drop)


async def test_dependencies_inside_the_flussra_schemas_do_not_block_the_reset(owned):
    import asyncpg

    _run(owned["owner_dsn"], "CREATE VIEW payroll.company_users AS "
                             "SELECT c.companyname, u.username FROM core.companies c "
                             "JOIN sec.users u USING (companyid)")
    try:
        conn = await asyncpg.connect(owned["owner_url"].replace("+asyncpg", ""))
        try:
            assert await boundary_violations(conn) == []
        finally:
            await conn.close()
    finally:
        _run(owned["owner_dsn"], "DROP VIEW payroll.company_users")


async def test_a_failure_while_dropping_rolls_the_whole_reset_back(owned):
    """The last schema cannot be dropped by the owner, so the earlier drops must not survive."""
    _run(owned["admin_dsn"], sql.SQL("ALTER SCHEMA sec OWNER TO {}").format(
        sql.Identifier(owned["admin_dsn"]["user"])))
    try:
        before = _state(owned)
        with pytest.raises(Exception, match="must be owner of schema sec"):
            await drop_application_objects(owned["owner_url"], confirm=owned["name"])
        assert _state(owned) == before
    finally:
        _run(owned["admin_dsn"], sql.SQL("ALTER SCHEMA sec OWNER TO {}").format(
            sql.Identifier(owned["role"])))


# ---------------------------------------------------------------------------
# The authorized reset (runs last: it replaces the database contents)
# ---------------------------------------------------------------------------

def test_an_authorized_reset_restores_the_canonical_demo_and_keeps_its_capability(owned, tmp_path):
    _run(owned["owner_dsn"], "CREATE TABLE public.unrelated_keep (id INTEGER)")
    env = {**owned["env"], "ENVIRONMENT": "development"}
    result = subprocess.run([sys.executable, RESET_SCRIPT, "--confirm", owned["name"]],
                            cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "canonical demo verified" in result.stdout
    assert _run(owned["owner_dsn"], "SELECT version_num FROM public.alembic_version")[0][0] == "0093"
    for table in PAYROLL_HISTORY_TABLES:
        assert _run(owned["owner_dsn"], f"SELECT count(*) FROM {table}")[0][0] == 0, table
    # Objects that do not depend on Flussra are not part of the reset.
    assert _run(owned["owner_dsn"], "SELECT to_regclass('public.unrelated_keep')")[0][0]
    # The capability lives in the database catalog, so the next reset is still authorized.
    stored = dict(entry.split("=", 1) for entry in _run(owned["owner_dsn"], """
        SELECT setconfig FROM pg_db_role_setting
        WHERE setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database())
          AND setrole = 0""")[0][0])
    assert reset_refusal(database=owned["name"], confirm=owned["name"], settings=stored) is None
    _run(owned["owner_dsn"], "DROP TABLE public.unrelated_keep")
