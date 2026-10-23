"""Replace the development database with a fresh one holding the canonical demo, or verify it.

A Flussra development database is disposable infrastructure: the whole database is the reset
boundary, and nothing created inside it survives a reset. The repository's migrations and the
canonical demo seed are the only authorities it is rebuilt from:

    DROP DATABASE -> CREATE DATABASE ... TEMPLATE template0 -> grant the application role
    -> alembic upgrade head -> assert the single script head -> startup schema guard
    -> canonical seed -> read-only verification

Roles. The database lifecycle (DROP and CREATE DATABASE) runs on an explicitly supplied
lifecycle connection, ``--admin-url``, to a control database on the same cluster; that
credential is never part of the application configuration. Its role must be a superuser, or
have CREATEDB and own the existing database. It owns the recreated database. The application
role runs the migrations and the application; it receives CONNECT, TEMPORARY and CREATE on the
database and USAGE and CREATE on schema ``public`` (the migrations create Flussra's schemas, two
trusted extensions and ``public.alembic_version``) and owns every Flussra object, but it cannot
drop or create databases.

Identity. The target database and the application role are what SQLAlchemy's asyncpg dialect
would pass to the driver for ``DATABASE_URL``, query arguments included (``?database=``,
``?user=``, ``?host=`` ...), exactly as the FastAPI engine, Alembic and the seed connect. Every
connection the reset opens goes through that same dialect, starts with the search path
``pg_catalog, pg_temp`` (overriding database and role settings), and names every system catalog
and function with ``pg_catalog``, so objects created in other schemas never take part.

Before anything is dropped, all of these must hold:

1. ``ENVIRONMENT`` is explicitly configured as ``development`` (the application default does
   not count);
2. ``--confirm`` repeats the effective target database name;
3. ``--admin-url`` connects to another database (the control database), as a role able to drop
   and recreate the target;
4. the application connection reports the effective database and role it was resolved to;
5. the application connection (to the target, or to the control database when the target does
   not exist) and the lifecycle connection report the same
   ``pg_catalog.pg_control_system().system_identifier``, so the reset can only replace a database
   of the cluster the application uses; host names and network location play no part;
6. an existing target is owned by the lifecycle role or the application role;
7. no other session is connected to the target. The reset never terminates sessions and never
   uses ``DROP DATABASE ... FORCE``; PostgreSQL's own DROP refuses too if one connects meanwhile.

DROP and CREATE are not one transaction. If anything fails after the DROP, the reset says so:
the disposable database is then absent or incomplete, and running the reset again (which also
works when the database does not exist) rebuilds it.

Usage (from the backend directory; keep the lifecycle password in PGPASSWORD or a pgpass file):

    .\\.venv\\Scripts\\python.exe scripts\\reset_dev_database.py \\
        --admin-url postgresql://postgres@localhost:5432/postgres --confirm payroll_v3
    .\\.venv\\Scripts\\python.exe scripts\\reset_dev_database.py --verify-only

``--verify-only`` changes nothing: it runs the schema guard and the canonical verification.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402
from psycopg2.extensions import make_dsn  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.db.schema_guard import _get_script_head, run_schema_guard  # noqa: E402
from scripts.canonical_demo import seed_canonical_demo, verify_canonical_demo  # noqa: E402

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEVELOPMENT = "development"
TRUSTED_SEARCH_PATH = "pg_catalog, pg_temp"
_INVALID_CATALOG_NAME = "3D000"  # SQLSTATE: the database does not exist


class ResetError(RuntimeError):
    """The reset stopped; the message names the step and what state it left."""


@dataclass(frozen=True)
class Target:
    database: str
    application_role: str
    application_url: str


def _asyncpg_url(database_url: str) -> str:
    """The URL with the asyncpg driver the application uses (a plain postgresql:// gets it)."""
    url = make_url(database_url)
    if url.drivername in ("postgresql", "postgres"):
        url = url.set(drivername="postgresql+asyncpg")
    return url.render_as_string(hide_password=False)


def effective_parameters(database_url: str) -> dict:
    """What SQLAlchemy's asyncpg dialect passes to the driver for this URL.

    The application's engine (``app.db.session.create_engine``), Alembic and the seed connect
    through ``create_async_engine`` without connect arguments, so this is where they connect.
    """
    engine = create_async_engine(_asyncpg_url(database_url))
    try:
        return engine.dialect.create_connect_args(engine.url)[1]
    finally:
        engine.sync_engine.dispose()


@asynccontextmanager
async def trusted_connection(database_url: str, *, database: str | None = None
                            ) -> AsyncIterator[asyncpg.Connection]:
    """A driver connection made by the dialect, with only ``pg_catalog`` trusted for names.

    ``database`` replaces the effective database and keeps every other routing argument.
    """
    connect_args: dict = {"server_settings": {"search_path": TRUSTED_SEARCH_PATH}}
    if database is not None:
        connect_args["database"] = database
    engine = create_async_engine(_asyncpg_url(database_url), poolclass=NullPool,
                                 connect_args=connect_args)
    try:
        async with engine.connect() as connection:
            driver = (await connection.get_raw_connection()).driver_connection
            await driver.execute(f"SET search_path = {TRUSTED_SEARCH_PATH}")
            yield driver
    finally:
        await engine.dispose()


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _database_missing(exc: BaseException) -> bool:
    return getattr(getattr(exc, "orig", None), "sqlstate", None) == _INVALID_CATALOG_NAME


def explicit_development(settings) -> bool:
    """Whether ENVIRONMENT was explicitly configured as development, not merely defaulted."""
    return ("ENVIRONMENT" in settings.model_fields_set
            and settings.ENVIRONMENT.strip().lower() == DEVELOPMENT)


def configured_target(database_url: str, confirm: str) -> Target:
    """The database and role the application connects as, confirmed by name."""
    parameters = effective_parameters(database_url)
    database, role = parameters.get("database"), parameters.get("user")
    if not isinstance(database, str) or not database or not isinstance(role, str) or not role:
        raise ResetError("DATABASE_URL must name the development database and the role the "
                         "application connects as.")
    if confirm != database:
        raise ResetError(f"Refusing to reset: --confirm names '{confirm}', but DATABASE_URL "
                         f"connects to '{database}'.")
    return Target(database, role, database_url)


_IDENTITY = """
    SELECT pg_catalog.current_database() AS database, session_user AS role,
           (SELECT system_identifier FROM pg_catalog.pg_control_system()) AS cluster
"""


async def _application_identity(target: Target, control_database: str) -> tuple[int, bool]:
    """The application's cluster identifier, and whether the target exists, as it connects."""
    try:
        async with trusted_connection(target.application_url) as conn:
            seen = await conn.fetchrow(_IDENTITY)
        exists = True
    except DBAPIError as exc:
        if not _database_missing(exc):
            raise
        async with trusted_connection(target.application_url, database=control_database) as conn:
            seen = await conn.fetchrow(_IDENTITY)
        exists = False
    expected = target.database if exists else control_database
    if (seen["database"], seen["role"]) != (expected, target.application_role):
        raise ResetError(
            f"Refusing to reset: the application connection reached database "
            f"'{seen['database']}' as '{seen['role']}', not '{expected}' as "
            f"'{target.application_role}'.")
    return seen["cluster"], exists


async def authorize(admin: asyncpg.Connection, target: Target) -> bool:
    """Every lifecycle check that does not depend on the runtime. Returns whether it exists."""
    lifecycle = await admin.fetchrow(_IDENTITY)
    if lifecycle["database"] == target.database:
        raise ResetError("--admin-url must connect to another database on the same cluster "
                         f"(for example postgres), not to '{target.database}' itself.")
    privileges = await admin.fetchrow("""
        SELECT rolsuper, rolcreatedb FROM pg_catalog.pg_roles WHERE rolname = current_user
    """)
    if not (privileges["rolsuper"] or privileges["rolcreatedb"]):
        raise ResetError(f"Refusing to reset: the lifecycle role '{lifecycle['role']}' cannot "
                         "create databases (it needs SUPERUSER or CREATEDB).")
    try:
        application_cluster, application_sees_target = await _application_identity(
            target, lifecycle["database"])
    except (OSError, asyncpg.PostgresError, DBAPIError) as exc:
        raise ResetError(f"Refusing to reset: the application credential cannot reach its "
                         f"cluster: {exc}") from exc
    if application_cluster != lifecycle["cluster"]:
        raise ResetError(
            f"Refusing to reset: --admin-url reaches PostgreSQL cluster {lifecycle['cluster']}, "
            f"but DATABASE_URL reaches cluster {application_cluster}.")
    owner = await admin.fetchval("""
        SELECT pg_catalog.pg_get_userbyid(datdba) FROM pg_catalog.pg_database WHERE datname = $1
    """, target.database)
    if (owner is not None) != application_sees_target:
        raise ResetError(f"Refusing to reset: the application and lifecycle connections disagree "
                         f"about whether '{target.database}' exists.")
    if owner is None:
        return False
    if owner not in (lifecycle["role"], target.application_role):
        raise ResetError(f"Refusing to reset: '{target.database}' is owned by '{owner}', which is "
                         "neither the lifecycle role nor the application role.")
    if owner != lifecycle["role"] and not privileges["rolsuper"]:
        raise ResetError(f"Refusing to reset: only a superuser or the owner can drop "
                         f"'{target.database}'; it is owned by '{owner}'.")
    sessions = await admin.fetch("""
        SELECT pid, usename, application_name, client_addr::pg_catalog.text AS client
        FROM pg_catalog.pg_stat_activity
        WHERE datname = $1 AND pid <> pg_catalog.pg_backend_pid()
        ORDER BY pid
    """, target.database)
    if sessions:
        raise ResetError(
            f"Refusing to reset: other sessions are connected to '{target.database}'; close them "
            "(for example, stop the backend) and retry:\n  - " + "\n  - ".join(
                f"pid {s['pid']} ({s['usename'] or 'system'}, "
                f"{s['application_name'] or 'no application'}, {s['client'] or 'local'})"
                for s in sessions))
    return True


async def drop_database(admin: asyncpg.Connection, target: Target) -> None:
    try:
        await admin.execute(f"DROP DATABASE {_quote(target.database)}")
    except asyncpg.PostgresError as exc:
        raise ResetError(f"Could not drop '{target.database}'; it is unchanged: {exc}") from exc


async def create_database(admin: asyncpg.Connection, admin_url: str, target: Target) -> None:
    """A pristine database from template0 that the application role can migrate and use."""
    name, role = _quote(target.database), _quote(target.application_role)
    await admin.execute(f"CREATE DATABASE {name} TEMPLATE template0 ENCODING 'UTF8'")
    if target.application_role == await admin.fetchval("SELECT current_user"):
        return
    await admin.execute(f"GRANT CONNECT, TEMPORARY, CREATE ON DATABASE {name} TO {role}")
    async with trusted_connection(admin_url, database=target.database) as fresh:
        await fresh.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {role}")


def upgrade_to_head(database_url: str) -> str:
    """Run ``alembic upgrade head``; return the one script head the database is now at."""
    env = {**os.environ, "DATABASE_URL": database_url}
    heads = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"], cwd=REPOSITORY_ROOT, env=env,
        capture_output=True, text=True, check=False)
    found = [line.split()[0] for line in heads.stdout.splitlines() if line.strip().endswith("(head)")]
    if heads.returncode != 0 or len(found) != 1 or found[0] != _get_script_head():
        raise ResetError(f"Expected exactly one Alembic head; found {found}: {heads.stderr}")
    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPOSITORY_ROOT, env=env,
        capture_output=True, text=True, check=False)
    if upgrade.returncode != 0:
        raise ResetError(f"alembic upgrade head failed:\n{upgrade.stdout}\n{upgrade.stderr}")
    version = asyncio.run(_database_version(database_url))
    if version != found[0]:
        raise ResetError(f"The database is at {version}, not the script head {found[0]}.")
    return version


async def _database_version(database_url: str) -> str:
    async with trusted_connection(database_url) as conn:
        return await conn.fetchval("SELECT version_num FROM public.alembic_version")


def _libpq_dsn(database_url: str) -> str:
    """The effective connection as a libpq DSN, for the psycopg2-based schema guard."""
    parameters = effective_parameters(database_url)
    keywords = {"host": parameters.get("host"), "port": parameters.get("port"),
                "user": parameters.get("user"), "password": parameters.get("password"),
                "dbname": parameters.get("database")}
    return make_dsn(**{key: ",".join(map(str, value)) if isinstance(value, list) else value
                       for key, value in keywords.items() if value is not None})


def check_schema(database_url: str) -> None:
    """The application's own startup guard, in development mode (head and every invariant)."""
    run_schema_guard(_libpq_dsn(database_url), is_dev=True)


def verify(database_url: str) -> None:
    check_schema(database_url)
    problems = asyncio.run(verify_canonical_demo(database_url))
    if problems:
        raise ResetError("The database is not the canonical demo:\n  - " + "\n  - ".join(problems))


async def _replace_database(admin_url: str, target: Target) -> None:
    try:
        async with trusted_connection(admin_url) as admin:
            exists = await authorize(admin, target)
            if exists:
                await drop_database(admin, target)
                print(f"[reset] dropped database {target.database}")
            try:
                await create_database(admin, admin_url, target)
            except (OSError, asyncpg.PostgresError, DBAPIError) as exc:
                raise ResetError(
                    f"'{target.database}' was dropped or absent and could not be recreated, so it "
                    f"is now absent or incomplete. Fix the cause and run the reset again: {exc}"
                ) from exc
            print(f"[reset] created database {target.database} from template0 for "
                  f"{target.application_role}")
    except (OSError, asyncpg.PostgresError, DBAPIError) as exc:
        raise ResetError(f"Could not open the lifecycle connection; nothing was changed: {exc}") from exc


def reset_and_reseed(database_url: str, *, admin_url: str, confirm: str) -> str:
    """Replace the configured database with the canonical demo. Returns its Alembic version."""
    target = configured_target(database_url, confirm)
    asyncio.run(_replace_database(admin_url, target))
    try:
        version = upgrade_to_head(database_url)
        print(f"[reset] upgraded to the single Alembic head {version}")
        check_schema(database_url)
        print("[reset] schema guard passed")
        summary = asyncio.run(seed_canonical_demo(database_url))
        print(f"[reset] seeded the canonical demo (Company {summary.company_id}, "
              f"Payroll Setup anchor {summary.payroll_setup_anchor})")
        verify(database_url)
    except Exception as exc:
        raise ResetError(
            f"'{target.database}' was recreated, but rebuilding it failed, so the canonical "
            f"reset did not complete. Fix the cause and run the reset again: {exc}") from exc
    print("[reset] canonical demo verified")
    return version


def main() -> int:
    from app.config import get_settings

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--confirm", metavar="DATABASE",
                        help="replace the database DATABASE_URL connects to, named again here")
    action.add_argument("--verify-only", action="store_true",
                        help="change nothing; verify the configured database is the canonical demo")
    parser.add_argument("--admin-url", metavar="URL",
                        help="lifecycle connection to a control database on the same cluster, as "
                             "a role that can drop and create the development database")
    args = parser.parse_args()
    settings = get_settings()
    try:
        if args.verify_only:
            verify(settings.DATABASE_URL)
            print(f"[verify] version {asyncio.run(_database_version(settings.DATABASE_URL))}: "
                  "schema guard passed and the canonical demo is intact")
            return 0
        if not explicit_development(settings):
            raise ResetError("Resetting requires ENVIRONMENT=development to be set explicitly "
                             "in the environment or .env; the default does not count.")
        if not args.admin_url:
            raise ResetError("Resetting requires --admin-url, the lifecycle connection; the "
                             "application's DATABASE_URL credential is never used to drop or "
                             "create databases.")
        reset_and_reseed(settings.DATABASE_URL, admin_url=args.admin_url, confirm=args.confirm)
        return 0
    except ResetError as exc:
        print(f"[reset] FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
