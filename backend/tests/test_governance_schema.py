"""Current governance and provenance schema objects on a fresh Alembic schema."""
from __future__ import annotations

import psycopg2
import pytest

from tests.schema_helpers import _schema_database, _version

GOVERNANCE_TABLES = {
    "paydefinitionrequests", "paydefinitionrequestevents", "paydefinitionprovenance",
}


@pytest.fixture(scope="module")
def current_database(schema_databases):
    with _schema_database(schema_databases, "governance_schema") as database:
        yield database


def test_fresh_schema_has_governance_tables_and_guards(current_database):
    _, dsn = current_database
    assert _version(dsn) == "0094"
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        cursor.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'payroll' AND table_name = ANY(%s)
        """, (sorted(GOVERNANCE_TABLES),))
        assert {row[0] for row in cursor.fetchall()} == GOVERNANCE_TABLES
        cursor.execute("""
            SELECT t.tgname FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'payroll' AND NOT t.tgisinternal AND c.relname = ANY(%s)
        """, (sorted(GOVERNANCE_TABLES),))
        assert {row[0] for row in cursor.fetchall()} == {
            "trg_paydefinitionrequests_guard",
            "trg_paydefinitionrequestevents_appendonly",
            "trg_paydefinitionprovenance_immutable",
            "trg_paydefinitionprovenance_requestlink",
        }
