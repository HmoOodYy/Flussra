"""Replace the development database with a fresh one holding the canonical demo, or verify it.

A Flussra development database is disposable infrastructure: the whole database is the reset
boundary, and nothing created inside it survives a reset. The repository's migrations and the
canonical demo seed are the only authorities it is rebuilt from:

    DROP DATABASE -> CREATE DATABASE ... TEMPLATE template0 -> grant the application role
    -> alembic upgrade head -> assert the single script head -> startup schema guard
    -> canonical seed -> read-only verification

Roles. The database lifecycle (DROP and CREATE DATABASE) runs on an explicitly supplied
lifecycle connection, ``--admin-url``, to a control database on the same server; that
credential is never part of the application configuration. Its role must be a superuser, or
have CREATEDB and own the existing database. It owns the recreated database. The application
role from ``DATABASE_URL`` runs the migrations and the application; it receives CONNECT,
TEMPORARY and CREATE on the database and USAGE and CREATE on schema ``public`` (the migrations
create Flussra's schemas, two trusted extensions and ``public.alembic_version``) and owns every
Flussra object, but it cannot drop or create databases.

Before anything is dropped, all of these must hold:

1. ``ENVIRONMENT`` is explicitly configured as ``development`` (the application default does
   not count);
2. the target is the database named in ``DATABASE_URL``, and ``--confirm`` repeats that name;
3. ``--admin-url`` connects to another database (the control database), as a role able to drop
   and recreate the target;
4. the lifecycle connection and the application credential reach the same running PostgreSQL
   server (same postmaster start time and version), so the reset can only replace the database
   the application uses; network location plays no part;
5. an existing target is owned by the lifecycle role or the application role;
6. no other session is connected to the target. The reset never terminates sessions and never
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
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.db.schema_guard import _get_script_head, run_schema_guard  # noqa: E402
from scripts.canonical_demo import seed_canonical_demo, verify_canonical_demo  # noqa: E402

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEVELOPMENT = "development"


class ResetError(RuntimeError):
    """The reset stopped; the message names the step and what state it left."""


@dataclass(frozen=True)
class Target:
    database: str
    application_role: str
    application_url: str


def _plain_dsn(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def explicit_development(settings) -> bool:
    """Whether ENVIRONMENT was explicitly configured as development, not merely defaulted."""
    return ("ENVIRONMENT" in settings.model_fields_set
            and settings.ENVIRONMENT.strip().lower() == DEVELOPMENT)


def configured_target(database_url: str, confirm: str) -> Target:
    """The database the application is configured to use, confirmed by name."""
    url = make_url(database_url)
    if not url.database or not url.username:
        raise ResetError("DATABASE_URL must name the development database and its role.")
    if confirm != url.database:
        raise ResetError(f"Refusing to reset: --confirm names '{confirm}', but DATABASE_URL "
                         f"configures '{url.database}'.")
    return Target(url.database, url.username, database_url)


_SERVER_IDENTITY = "SELECT pg_postmaster_start_time()::text || ' ' || version()"


async def _application_server_identity(target: Target, control_database: str) -> str:
    """The server the application credential reaches, through the target or the control DB."""
    plain = _plain_dsn(target.application_url)
    try:
        conn = await asyncpg.connect(plain)
    except asyncpg.InvalidCatalogNameError:
        conn = await asyncpg.connect(plain, database=control_database)
    try:
        return await conn.fetchval(_SERVER_IDENTITY)
    finally:
        await conn.close()


async def authorize(admin: asyncpg.Connection, target: Target) -> bool:
    """Every lifecycle check that does not depend on the runtime. Returns whether it exists."""
    control = await admin.fetchval("SELECT current_database()")
    if control == target.database:
        raise ResetError("--admin-url must connect to another database on the same server "
                         f"(for example postgres), not to '{target.database}' itself.")
    lifecycle = await admin.fetchrow("""
        SELECT current_user AS name, rolsuper, rolcreatedb
        FROM pg_roles WHERE rolname = current_user
    """)
    if not (lifecycle["rolsuper"] or lifecycle["rolcreatedb"]):
        raise ResetError(f"Refusing to reset: the lifecycle role '{lifecycle['name']}' cannot "
                         "create databases (it needs SUPERUSER or CREATEDB).")
    if not await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", target.application_role):
        raise ResetError(f"Refusing to reset: the application role '{target.application_role}' "
                         "does not exist on the lifecycle server.")
    try:
        application_server = await _application_server_identity(target, control)
    except (OSError, asyncpg.PostgresError) as exc:
        raise ResetError(f"Refusing to reset: the application credential cannot reach its "
                         f"server: {exc}") from exc
    if application_server != await admin.fetchval(_SERVER_IDENTITY):
        raise ResetError("Refusing to reset: --admin-url and DATABASE_URL reach different "
                         "PostgreSQL servers.")
    owner = await admin.fetchval("""
        SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = $1
    """, target.database)
    if owner is None:
        return False
    if owner not in (lifecycle["name"], target.application_role):
        raise ResetError(f"Refusing to reset: '{target.database}' is owned by '{owner}', which is "
                         "neither the lifecycle role nor the application role.")
    if owner != lifecycle["name"] and not lifecycle["rolsuper"]:
        raise ResetError(f"Refusing to reset: only a superuser or the owner can drop "
                         f"'{target.database}'; it is owned by '{owner}'.")
    sessions = await admin.fetch("""
        SELECT pid, usename, application_name, client_addr::text AS client
        FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()
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
    if role == _quote(await admin.fetchval("SELECT current_user")):
        return
    await admin.execute(f"GRANT CONNECT, TEMPORARY, CREATE ON DATABASE {name} TO {role}")
    fresh = await asyncpg.connect(_plain_dsn(admin_url), database=target.database)
    try:
        await fresh.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {role}")
    finally:
        await fresh.close()


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
    conn = await asyncpg.connect(_plain_dsn(database_url))
    try:
        return await conn.fetchval("SELECT version_num FROM public.alembic_version")
    finally:
        await conn.close()


def check_schema(database_url: str) -> None:
    """The application's own startup guard, in development mode (head and every invariant)."""
    run_schema_guard(_plain_dsn(database_url), is_dev=True)


def verify(database_url: str) -> None:
    check_schema(database_url)
    problems = asyncio.run(verify_canonical_demo(database_url))
    if problems:
        raise ResetError("The database is not the canonical demo:\n  - " + "\n  - ".join(problems))


async def _replace_database(admin_url: str, target: Target) -> None:
    try:
        admin = await asyncpg.connect(_plain_dsn(admin_url))
    except (OSError, asyncpg.PostgresError) as exc:
        raise ResetError(f"Could not open the lifecycle connection; nothing was changed: {exc}") from exc
    try:
        exists = await authorize(admin, target)
        if exists:
            await drop_database(admin, target)
            print(f"[reset] dropped database {target.database}")
        try:
            await create_database(admin, admin_url, target)
        except (OSError, asyncpg.PostgresError) as exc:
            raise ResetError(
                f"'{target.database}' was dropped or absent and could not be recreated, so it is "
                f"now absent or incomplete. Fix the cause and run the reset again: {exc}") from exc
        print(f"[reset] created database {target.database} from template0 for "
              f"{target.application_role}")
    finally:
        await admin.close()


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
                        help="replace the database configured in DATABASE_URL, named again here")
    action.add_argument("--verify-only", action="store_true",
                        help="change nothing; verify the configured database is the canonical demo")
    parser.add_argument("--admin-url", metavar="URL",
                        help="lifecycle connection to a control database on the same server, as "
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
