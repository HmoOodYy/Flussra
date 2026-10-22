"""The shared disposable-database fixtures are released with their test module.

Every test module imports ``p3b_dsn``, ``p3c_engine`` and ``p3c_app``, and pytest registers an
imported fixture once per importing module, so each module has its own instance. Declared with
session scope, each of those instances kept its database, administrative connection and engine
pool alive until the process ended, so the live PostgreSQL backends grew with every module and a
full run reached the server's connection limit. Module scope releases them when their module is
done; this guard keeps it that way.
"""
from __future__ import annotations

import pytest

from tests.compensation_api_fixtures import (  # noqa: F401 - register fixtures
    p3c_application,
    p3c_database_engine,
)
from tests.compensation_database_fixtures import p3b_database  # noqa: F401 - register fixtures

pytestmark = pytest.mark.asyncio


async def test_shared_database_fixtures_have_module_scope(request, p3c_app):
    definitions = request.node._fixtureinfo.name2fixturedefs
    assert {name: definitions[name][-1].scope for name in ("p3b_dsn", "p3c_engine", "p3c_app")} == {
        "p3b_dsn": "module", "p3c_engine": "module", "p3c_app": "module"}


async def test_the_module_database_is_open_for_use_and_holds_no_administrative_connection(
    p3b_dsn,
):
    """The database exists for the module, and creating it left no idle connection of its own."""
    import psycopg2

    conn = psycopg2.connect(client_encoding="utf-8", **p3b_dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            assert cur.fetchone()[0] == p3b_dsn["database"]
            # An idle backend keeps the text of its last statement, so an administrative
            # connection left open after creating the database would show up here.
            cur.execute("""
                SELECT count(*) FROM pg_stat_activity
                WHERE pid <> pg_backend_pid() AND state = 'idle'
                  AND query LIKE 'CREATE DATABASE%'
            """)
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()
