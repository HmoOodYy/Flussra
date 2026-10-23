"""Mark a database as a disposable Flussra development database that may be reset.

This is the only place the reset capability is granted; ``scripts/reset_dev_database.py``
never grants it. It stores two database-level settings in the PostgreSQL catalog:

    flussra.environment   = development   (what the database is)
    flussra.reset_allowed = on            (destructive reset is explicitly allowed)

They survive every application-schema reset. PostgreSQL lets only an administrator (a
superuser, or a role granted SET on these parameters) store them, so the application role cannot
make its own database resettable. The trust decision is the database's identity, never its
network location: a remote or containerized development database can be provisioned, and a
local database that is not provisioned cannot be reset.

Usage (from the backend directory, with ENVIRONMENT=development set explicitly):

    .\\.venv\\Scripts\\python.exe scripts\\provision_dev_database.py \\
        --admin-url postgresql://<administrator>@<host>:<port>/<database> --confirm <database>

``--admin-url`` connects to the target database as the administrator; ``--confirm`` must repeat
that database's name. A database already marked as another environment is refused. To withdraw
the capability, an administrator runs
``ALTER DATABASE <database> SET flussra.reset_allowed = off``.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402

from scripts.reset_dev_database import (  # noqa: E402
    DEVELOPMENT,
    ENVIRONMENT_SETTING,
    RESET_ALLOWED,
    RESET_CAPABILITY_SETTING,
    ResetError,
    _plain_dsn,
    database_settings,
    explicit_development,
)


async def provision(admin_url: str, *, confirm: str) -> str:
    """Store the development identity and reset capability on the connected database."""
    conn = await asyncpg.connect(_plain_dsn(admin_url))
    try:
        database = await conn.fetchval("SELECT current_database()")
        if confirm != database:
            raise ResetError(f"Refusing to provision '{database}': --confirm names '{confirm}'.")
        environment = (await database_settings(conn)).get(ENVIRONMENT_SETTING)
        if environment not in (None, DEVELOPMENT):
            raise ResetError(f"Refusing to provision '{database}': it is marked "
                             f"{ENVIRONMENT_SETTING} = {environment!r}.")
        quoted = await conn.fetchval("SELECT quote_ident(current_database())")
        async with conn.transaction():
            await conn.execute(f"ALTER DATABASE {quoted} SET {ENVIRONMENT_SETTING} = '{DEVELOPMENT}'")
            await conn.execute(
                f"ALTER DATABASE {quoted} SET {RESET_CAPABILITY_SETTING} = '{RESET_ALLOWED}'")
        return database
    except asyncpg.InsufficientPrivilegeError as exc:
        raise ResetError(f"Provisioning needs an administrator connection: {exc}") from exc
    finally:
        await conn.close()


def main() -> int:
    from app.config import get_settings

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--admin-url", required=True,
                        help="administrator connection to the database to provision")
    parser.add_argument("--confirm", required=True, metavar="DATABASE",
                        help="the name of that database, repeated")
    args = parser.parse_args()
    try:
        if not explicit_development(get_settings()):
            raise ResetError("Provisioning requires ENVIRONMENT=development to be set explicitly "
                             "in the environment or .env; the default does not count.")
        database = asyncio.run(provision(args.admin_url, confirm=args.confirm))
        print(f"[provision] '{database}' is a Flussra development database: "
              f"{ENVIRONMENT_SETTING} = {DEVELOPMENT}, {RESET_CAPABILITY_SETTING} = {RESET_ALLOWED}")
        return 0
    except ResetError as exc:
        print(f"[provision] FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
