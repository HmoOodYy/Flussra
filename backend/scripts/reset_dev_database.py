"""Destructively reset a development database to the canonical demo, or verify it.

Flussra is pre-production: development data is disposable and is never converted. The reset
contract is

    authorize -> drop the Flussra schemas -> alembic upgrade head -> assert the single script
    head -> runtime schema guard -> canonical seed -> read-only verification

Authorization needs four independent facts, and network location is not one of them:

1. the runtime environment is explicitly configured as ``development`` (``ENVIRONMENT`` set in
   the process environment or ``.env``; the application default does not count);
2. the connected database says it is a Flussra development database
   (database setting ``flussra.environment = development``);
3. the connected database separately allows destructive reset
   (database setting ``flussra.reset_allowed = on``);
4. ``--confirm`` repeats the name of the database actually connected to.

The two database settings are stored by an administrator with
``scripts/provision_dev_database.py``; they live in the database catalog, survive every reset,
and the application role cannot set them itself. A missing, malformed or different value
refuses the reset.

The destructive phase runs in an exclusive window. The reset takes a per-database advisory
lock (so two reset commands never overlap), then closes the database to new connections with
``ALTER DATABASE ... WITH ALLOW_CONNECTIONS false``, issued from a control session in another
database of the same server (``--control-database``, default ``postgres``) because PostgreSQL
does not let a session close its own database. It refuses, and reopens the database, if any
other session is connected or starts connecting within a short settle period; it never
terminates other sessions. Only then does it open the destructive transaction.

The reset boundary is the set of Flussra application schemas (``RESET_SCHEMAS``) plus
``public.alembic_version``. Inside that transaction the reset locks the Flussra relations,
checks again that it is alone, and walks PostgreSQL's dependency graph from those schemas; if
dropping them would also drop any object outside them (a view, a column, a domain, a
constraint, ...), nothing is dropped and the objects are reported. ``public.alembic_version``
is dropped without CASCADE, and extensions are kept because the migrations create them only if
absent. The previous connection setting is restored on success and on every handled failure,
before the upgrade and seed open new connections. If the process is killed inside the window,
the database stays closed (fail safe); the owner or a superuser reopens it from another
database with ``ALTER DATABASE <database> WITH ALLOW_CONNECTIONS true``. The database itself,
its owner and its privileges are kept, so its owner can reset it without CREATEDB.

Usage (from the backend directory):

    .\\.venv\\Scripts\\python.exe scripts\\reset_dev_database.py --confirm <database name>
    .\\.venv\\Scripts\\python.exe scripts\\reset_dev_database.py --verify-only

``--verify-only`` changes nothing: it runs the schema guard and the canonical verification.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402

from app.db.schema_guard import _get_script_head, run_schema_guard  # noqa: E402
from scripts.canonical_demo import seed_canonical_demo, verify_canonical_demo  # noqa: E402

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# Every schema the migration chain creates; tests keep this equal to a fresh upgrade.
RESET_SCHEMAS = ("app", "audit", "core", "import", "integration", "payroll", "review", "sec")
ENVIRONMENT_SETTING = "flussra.environment"
RESET_CAPABILITY_SETTING = "flussra.reset_allowed"
DEVELOPMENT = "development"
RESET_ALLOWED = "on"
# The reset closes the target database from a session in this database of the same server.
CONTROL_DATABASE = "postgres"
SETTLE_SECONDS = 1.0
RESET_LOCK_CLASS = 0x464C5253  # advisory lock class for resets; the object is the database OID


class ResetError(RuntimeError):
    """The reset stopped; the message names the step."""


def _plain_dsn(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def explicit_development(settings) -> bool:
    """Whether ENVIRONMENT was explicitly configured as development, not merely defaulted."""
    return ("ENVIRONMENT" in settings.model_fields_set
            and settings.ENVIRONMENT.strip().lower() == DEVELOPMENT)


async def database_settings(conn: asyncpg.Connection) -> dict[str, str]:
    """The database-level settings stored for the connected database (not session values)."""
    entries = await conn.fetchval("""
        SELECT setconfig FROM pg_db_role_setting
        WHERE setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database())
          AND setrole = 0
    """)
    return dict(entry.split("=", 1) for entry in entries or [] if "=" in entry)


def reset_refusal(*, database: str, confirm: str, settings: Mapping[str, str]) -> str | None:
    """Why the connected database may not be reset, or None when it may."""
    if confirm != database:
        return f"Refusing to reset '{database}': --confirm names '{confirm}'."
    environment = settings.get(ENVIRONMENT_SETTING)
    if environment != DEVELOPMENT:
        return (f"Refusing to reset '{database}': its database setting {ENVIRONMENT_SETTING} is "
                f"{environment!r}, not '{DEVELOPMENT}'. An administrator provisions a development "
                "database with scripts/provision_dev_database.py.")
    capability = settings.get(RESET_CAPABILITY_SETTING)
    if capability != RESET_ALLOWED:
        return (f"Refusing to reset '{database}': its database setting {RESET_CAPABILITY_SETTING} "
                f"is {capability!r}, not '{RESET_ALLOWED}'.")
    return None


async def boundary_violations(conn: asyncpg.Connection, schemas=RESET_SCHEMAS) -> list[str]:
    """Objects outside ``schemas`` that dropping them with CASCADE would also drop.

    The cascade closure follows every pg_depend edge from the schemas to their dependents, and
    from an internal part to the object it belongs to. An object of the closure is inside the
    boundary when it lives in one of the schemas or is one of them, or when it is a part of
    objects that are all inside: an internal dependency (a TOAST table, a row or array type, a
    view's rule) or, for an object without a user schema of its own (a trigger, rule or default,
    or a TOAST index in PostgreSQL's ``pg_*`` schemas), an auto dependency. A part of an object
    outside the closure is outside. Anything else is outside, whatever its kind.
    """
    rows = await conn.fetch("""
        WITH RECURSIVE
        closure(classid, objid, objsubid) AS (
            SELECT 'pg_namespace'::regclass::oid, oid, 0
            FROM pg_namespace WHERE nspname = ANY($1::text[])
            UNION
            -- Downward: every dependent is dropped. Upward: dropping an internal part of an
            -- object (a view's rule) drops the object itself.
            SELECT CASE WHEN down THEN d.classid ELSE d.refclassid END,
                   CASE WHEN down THEN d.objid ELSE d.refobjid END,
                   CASE WHEN down THEN d.objsubid ELSE d.refobjsubid END
            FROM closure c
            JOIN pg_depend d
              ON (d.refclassid = c.classid AND d.refobjid = c.objid
                  AND (c.objsubid = 0 OR d.refobjsubid = c.objsubid))
              OR (d.deptype = 'i' AND d.classid = c.classid AND d.objid = c.objid
                  AND d.objsubid = c.objsubid)
            CROSS JOIN LATERAL (SELECT d.refclassid = c.classid AND d.refobjid = c.objid
                                       AND (c.objsubid = 0 OR d.refobjsubid = c.objsubid) AS down) e
            WHERE d.deptype <> 'p'
        )
        SELECT c.classid, c.objid, c.objsubid, o.type, o.schema, o.identity,
               (c.classid = 'pg_namespace'::regclass::oid
                AND c.objid IN (SELECT oid FROM pg_namespace WHERE nspname = ANY($1::text[])))
                   AS is_root,
               COALESCE(array_agg(ARRAY[p.refclassid, p.refobjid, p.refobjsubid]::oid[]
                                  ORDER BY p.refclassid, p.refobjid, p.refobjsubid)
                        FILTER (WHERE p.deptype = 'i'
                                   OR (p.deptype = 'a'
                                       AND (o.schema IS NULL OR o.schema LIKE 'pg\\_%'))),
                        '{}') AS parents
        FROM closure c
        CROSS JOIN LATERAL pg_identify_object(c.classid, c.objid, c.objsubid) o
        LEFT JOIN pg_depend p ON p.classid = c.classid AND p.objid = c.objid
                             AND p.objsubid = c.objsubid AND p.deptype IN ('i', 'a')
        GROUP BY c.classid, c.objid, c.objsubid, o.type, o.schema, o.identity
    """, list(schemas))
    names = set(schemas) | {f'"{name}"' for name in schemas}
    nodes = {(r["classid"], r["objid"], r["objsubid"]): r for r in rows}
    verdicts: dict[tuple, bool] = {}

    def inside(key: tuple) -> bool:
        if key in verdicts:
            return verdicts[key]
        verdicts[key] = False  # a cycle never proves membership
        node = nodes.get(key)
        if node is None:
            return False  # a part of an object that is not dropped
        if node["is_root"] or node["schema"] in names:
            verdicts[key] = True
        else:
            parents = [tuple(int(v) for v in parent) for parent in node["parents"]]
            # Whole objects contain their columns: a column part refers to its relation.
            verdicts[key] = bool(parents) and all(
                inside(p) or inside((p[0], p[1], 0)) for p in parents)
        return verdicts[key]

    return sorted({f"{r['type']} {r['identity']}" for key, r in nodes.items() if not inside(key)})


def recovery_command(database: str) -> str:
    return (f'ALTER DATABASE "{database}" WITH ALLOW_CONNECTIONS true  '
            "(run by the database owner or a superuser while connected to another database, "
            "such as postgres)")


async def competing_sessions(conn: asyncpg.Connection, database_oid: int) -> list[str]:
    """Other sessions that can run DDL in the database, including connections still starting.

    A session counts when it runs as a role (system workers such as autovacuum have none) and
    is connected to the database, or holds the database lock taken while a connection starts.
    """
    rows = await conn.fetch("""
        SELECT pid, usename, application_name, client_addr::text AS client
        FROM pg_stat_activity
        WHERE datid = $1 AND pid <> pg_backend_pid() AND usename IS NOT NULL
        UNION
        SELECT l.pid, NULL, NULL, NULL
        FROM pg_locks l
        WHERE l.locktype = 'object' AND l.classid = 'pg_database'::regclass
          AND l.objid = $1 AND l.pid <> pg_backend_pid()
        ORDER BY 1
    """, database_oid)
    return [f"pid {r['pid']}" + (f" ({r['usename']}, {r['application_name'] or 'no application'}"
                                 f", {r['client'] or 'local'})" if r["usename"] else " (starting)")
            for r in rows]


async def _await_exclusive(conn: asyncpg.Connection, database_oid: int, settle: float) -> None:
    """Refuse unless no other session is or becomes connected while new connections are closed.

    The settle period lets a connection that passed PostgreSQL's connection check just before
    the database was closed become visible, so it is refused rather than missed.
    """
    deadline = asyncio.get_running_loop().time() + settle
    while True:
        others = await competing_sessions(conn, database_oid)
        if others:
            raise ResetError(
                "Refusing to reset: other sessions are connected to the database; close them "
                "(for example, stop the backend) and retry:\n  - " + "\n  - ".join(others))
        if asyncio.get_running_loop().time() >= deadline:
            return
        await asyncio.sleep(0.1)


async def drop_application_objects(
    database_url: str, *, confirm: str, control_database: str = CONTROL_DATABASE,
    settle: float = SETTLE_SECONDS, after_isolation=None,
) -> list[str]:
    """Authorize on the database side, then drop the Flussra schemas in an exclusive window.

    The caller has already established explicit runtime development intent. Once authorized,
    the reset takes the reset lock, closes the database to new connections from a control
    session in ``control_database`` (PostgreSQL does not let a session close its own database),
    proves no other session is connected, and only then, in one transaction, locks the Flussra
    relations, checks the dependency boundary again and drops. The previous connection setting
    is restored on every handled path before this returns. ``after_isolation`` is an
    observation point that runs once the database is exclusive, before the transaction.
    """
    try:
        conn = await asyncpg.connect(_plain_dsn(database_url))
    except asyncpg.PostgresError as exc:
        if "not currently accepting connections" in str(exc):
            raise ResetError(
                f"The database is closed to new connections, either by a reset in progress or "
                f"by an interrupted one. If no reset is running, reopen it with: "
                f"{recovery_command(confirm)}") from exc
        raise
    try:
        database = await conn.fetchval("SELECT current_database()")
        refusal = reset_refusal(database=database, confirm=confirm,
                                settings=await database_settings(conn))
        if refusal:
            raise ResetError(refusal)
        database_oid, allowed_before = await conn.fetchrow(
            "SELECT oid, datallowconn FROM pg_database WHERE datname = current_database()")
        control = await asyncpg.connect(_plain_dsn(database_url), database=control_database)
        try:
            # Serializes cooperating reset commands only; the isolation below is what keeps
            # every other session out.
            if not await control.fetchval("SELECT pg_try_advisory_lock($1::bigint)",
                                          (RESET_LOCK_CLASS << 32) | database_oid):
                raise ResetError(f"Refusing to reset '{database}': another reset of it is running.")
            quoted = await conn.fetchval("SELECT quote_ident(current_database())")
            await control.execute(f"ALTER DATABASE {quoted} WITH ALLOW_CONNECTIONS false")
            try:
                await _await_exclusive(conn, database_oid, settle)
                if after_isolation is not None:
                    await after_isolation(conn)
                async with conn.transaction():
                    await conn.execute("SET LOCAL lock_timeout = '30s'")
                    relations = await conn.fetch("""
                        SELECT format('%I.%I', n.nspname, c.relname) AS name
                        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = ANY($1::text[]) AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                        ORDER BY 1
                    """, list(RESET_SCHEMAS))
                    if relations:
                        await conn.execute("LOCK TABLE " + ", ".join(r["name"] for r in relations)
                                           + " IN ACCESS EXCLUSIVE MODE")
                    await _await_exclusive(conn, database_oid, 0)
                    outside = await boundary_violations(conn)
                    if outside:
                        raise ResetError(
                            "Refusing to reset: dropping the Flussra schemas would also drop these "
                            "objects outside them:\n  - " + "\n  - ".join(outside))
                    present = [r["nspname"] for r in await conn.fetch(
                        "SELECT nspname FROM pg_namespace WHERE nspname = ANY($1::text[]) ORDER BY 1",
                        list(RESET_SCHEMAS))]
                    for schema in present:
                        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
                    await conn.execute("DROP TABLE IF EXISTS public.alembic_version RESTRICT")
            finally:
                try:
                    await control.execute(
                        f"ALTER DATABASE {quoted} WITH ALLOW_CONNECTIONS "
                        f"{'true' if allowed_before else 'false'}")
                except Exception as exc:
                    raise ResetError(f"Could not reopen '{database}' to new connections ({exc}). "
                                     f"Reopen it with: {recovery_command(database)}") from exc
        finally:
            await control.close()
        return [f"schema {s}" for s in present]
    finally:
        await conn.close()


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


def reset_and_reseed(database_url: str, *, confirm: str,
                     control_database: str = CONTROL_DATABASE) -> str:
    """Reset ``database_url`` to the canonical demo. Returns the Alembic version it is at.

    The database is open to new connections again before the upgrade and seed run.
    """
    dropped = asyncio.run(drop_application_objects(
        database_url, confirm=confirm, control_database=control_database))
    print(f"[reset] dropped {', '.join(dropped) or 'nothing'}")
    version = upgrade_to_head(database_url)
    print(f"[reset] upgraded to the single Alembic head {version}")
    check_schema(database_url)
    print("[reset] schema guard passed")
    summary = asyncio.run(seed_canonical_demo(database_url))
    print(f"[reset] seeded the canonical demo (Company {summary.company_id}, "
          f"Payroll Setup anchor {summary.payroll_setup_anchor})")
    verify(database_url)
    print("[reset] canonical demo verified")
    return version


def main() -> int:
    from app.config import get_settings

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--confirm", metavar="DATABASE",
                        help="reset the configured database, whose name must be repeated here")
    action.add_argument("--verify-only", action="store_true",
                        help="change nothing; verify the configured database is the canonical demo")
    parser.add_argument("--control-database", default=CONTROL_DATABASE, metavar="DATABASE",
                        help="another database on the same server, used to close and reopen the "
                             f"reset database to new connections (default {CONTROL_DATABASE})")
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
        reset_and_reseed(settings.DATABASE_URL, confirm=args.confirm,
                         control_database=args.control_database)
        return 0
    except ResetError as exc:
        print(f"[reset] FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
