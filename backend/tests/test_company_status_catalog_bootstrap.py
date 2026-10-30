"""Current company status catalog shape and guard on a fresh Alembic schema."""
from __future__ import annotations

import psycopg2
import pytest

from app.db.schema_guard import _check_calculation_evidence, _check_company_status_catalog
from tests.schema_helpers import _columns, _schema_database, _tables, _version


@pytest.fixture(scope="module")
def head_database(schema_databases):
    with _schema_database(schema_databases, "status_catalog") as database:
        yield database



def test_fresh_schema_has_the_company_status_catalog_shape(head_database):
    _, dsn = head_database
    assert _version(dsn) == "0096"
    assert {"payrollstatuskeyaliases", "payrollstatuskeybranchlimits"} <= _tables(dsn)
    assert {"companyid", "normalizedkeyname", "allowancecategory"} <= _columns(dsn, "payrollstatuskeys")
    assert {"branchid", "limitusesperperiodenabled", "limitusesperperiod", "limitusesperdriver",
            "limitusesacrossdrivers", "limitusesperday"}.isdisjoint(_columns(dsn, "payrollstatuskeys"))
    assert {"branchid", "ratetypeid"}.isdisjoint(_columns(dsn, "statusratecolumns"))
    assert "ownerbranchid" not in _columns(dsn, "ratedefinitions")
    assert {"paydefinitionid", "statusratecolumnid"} <= _columns(dsn, "driverrateschedules")
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        assert _check_company_status_catalog(cursor) == []
        assert _check_calculation_evidence(cursor) == []
        cursor.execute("""
            SELECT is_nullable FROM information_schema.columns
            WHERE table_schema = 'payroll' AND table_name = 'driverrateschedules'
              AND column_name = 'paydefinitionid'
        """)
        assert cursor.fetchone() == ("YES",)
        cursor.execute("""
            SELECT is_nullable FROM information_schema.columns
            WHERE table_schema = 'payroll' AND table_name = 'payrollstatuskeys'
              AND column_name = 'allowancecategory'
        """)
        assert cursor.fetchone() == ("NO",)
