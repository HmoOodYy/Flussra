"""Current target compensation tables and integrity triggers on a fresh schema."""
from __future__ import annotations

import psycopg2
import pytest

from tests.schema_helpers import _schema_database, _version

TARGET_TABLES = {
    "paydefinitions", "ratedefinitions", "ratecomponentdefinitions",
    "driverrateschedules", "driverrateschedulevalues", "paydefinitionrequests",
    "paydefinitionrequestevents", "paydefinitionprovenance",
}


@pytest.fixture(scope="module")
def current_database(schema_databases):
    with _schema_database(schema_databases, "compensation_bootstrap") as database:
        yield database


def test_fresh_schema_has_target_compensation_and_governance(current_database):
    _, dsn = current_database
    assert _version(dsn) == "0095"
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        for table in TARGET_TABLES:
            cursor.execute("SELECT to_regclass(%s)", (f"payroll.{table}",))
            assert cursor.fetchone()[0] is not None, table
        cursor.execute("""
            SELECT count(*) FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'payroll' AND NOT t.tgisinternal
              AND c.relname = ANY(%s)
        """, (sorted(TARGET_TABLES),))
        assert cursor.fetchone()[0] >= 9
