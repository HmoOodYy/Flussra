"""Destructively reset a development database to the canonical demo, or verify it.

Flussra is pre-production: development data is disposable and is never converted. The reset
contract is

    drop every application object -> alembic upgrade head -> assert the single script head
    -> runtime schema guard -> canonical seed -> read-only verification

Dropping covers every non-system schema and extension the connecting role owns, plus
``public.alembic_version``; the database itself, its owner and its privileges are kept, so a
database owner without CREATEDB can reset its own database. Anything else the role owns in
``public`` stops the reset before anything is dropped.

Usage (from the backend directory, with ENVIRONMENT=development in .env):

    .\\.venv\\Scripts\\python.exe scripts\\reset_dev_database.py --confirm <database name>
    .\\.venv\\Scripts\\python.exe scripts\\reset_dev_database.py --verify-only

``--confirm`` must repeat the name of the configured database. ``--verify-only`` changes
nothing: it runs the schema guard and the canonical verification.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402

from app.db.schema_guard import _get_script_head, run_schema_guard  # noqa: E402
from scripts.canonical_demo import seed_canonical_demo, verify_canonical_demo  # noqa: E402

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class ResetError(RuntimeError):
    """The reset stopped; the message names the step."""


def _plain_dsn(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def drop_application_objects(database_url: str, *, confirm: str) -> list[str]:
    """Drop every schema and extension the role owns, in one transaction. Returns what went."""
    conn = await asyncpg.connect(_plain_dsn(database_url))
    try:
        database = await conn.fetchval("SELECT current_database()")
        if confirm != database:
            raise ResetError(f"Refusing to reset '{database}': --confirm names '{confirm}'.")
        schemas = [r["nspname"] for r in await conn.fetch("""
            SELECT nspname FROM pg_namespace
            WHERE nspowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)
              AND nspname NOT IN ('public', 'information_schema')
              AND nspname NOT LIKE 'pg\\_%'
            ORDER BY nspname
        """)]
        extensions = [r["extname"] for r in await conn.fetch("""
            SELECT extname FROM pg_extension
            WHERE extowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)
              AND extname <> 'plpgsql'
            ORDER BY extname
        """)]
        unknown = [r["name"] for r in await conn.fetch("""
            SELECT c.relname AS name FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)
              AND c.relname NOT IN ('alembic_version', 'alembic_version_pkc')
              AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                              AND d.objid = c.oid AND d.deptype = 'e')
            UNION ALL
            SELECT p.proname FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public' AND p.proowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)
              AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass
                              AND d.objid = p.oid AND d.deptype = 'e')
        """)]
        if unknown:
            raise ResetError(f"Refusing to reset: unexpected objects in public: {sorted(unknown)}")
        async with conn.transaction():
            for schema in schemas:
                await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
            for extension in extensions:
                await conn.execute(f'DROP EXTENSION "{extension}" CASCADE')
            await conn.execute("DROP TABLE IF EXISTS public.alembic_version")
        return [f"schema {s}" for s in schemas] + [f"extension {e}" for e in extensions]
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


def reset_and_reseed(database_url: str, *, confirm: str) -> str:
    """Reset ``database_url`` to the canonical demo. Returns the Alembic version it is at."""
    dropped = asyncio.run(drop_application_objects(database_url, confirm=confirm))
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
    args = parser.parse_args()
    settings = get_settings()
    try:
        if args.verify_only:
            verify(settings.DATABASE_URL)
            print(f"[verify] version {asyncio.run(_database_version(settings.DATABASE_URL))}: "
                  "schema guard passed and the canonical demo is intact")
            return 0
        if not settings.is_dev:
            raise ResetError("Resetting is allowed only with ENVIRONMENT=development.")
        reset_and_reseed(settings.DATABASE_URL, confirm=args.confirm)
        return 0
    except ResetError as exc:
        print(f"[reset] FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
