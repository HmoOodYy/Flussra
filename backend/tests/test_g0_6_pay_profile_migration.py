"""G0.6 migration: retired Pay Profile state no longer locks Company currency."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql

_ROOT = Path(__file__).resolve().parents[2]


def _alembic(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "alembic", *args],
        cwd=_ROOT, env=env, capture_output=True, text=True, check=False,
    )


@pytest.fixture
def g0_6_database(pg_instance):
    dsn = pg_instance.dsn()
    name = "g06_" + uuid4().hex[:12]
    admin = psycopg2.connect(**dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    env = os.environ.copy()
    env["DATABASE_URL"] = (
        f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:{dsn['port']}/{name}"
    )
    env["SECRET_KEY"] = "g0-6-disposable-migration-test"
    check_dsn = {**dsn, "database": name}
    try:
        result = _alembic(env, "upgrade", "0080")
        assert result.returncode == 0, result.stderr
        yield env, check_dsn
    finally:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def _seed_company_with_pay_profile_state(cur) -> dict[str, int]:
    """A USD company whose only durable monetary state is a Pay Profile rate."""
    cur.execute("""
        INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended,
                                    timezonename, currencycode)
        VALUES ('G06', 'G06', 'G06', 'Active', FALSE, 'UTC', 'USD') RETURNING companyid
    """)
    company_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (%s, 'G06', 'G06', 'Active', TRUE) RETURNING branchid
    """, (company_id,))
    branch_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO core.employees (companyid, branchid, fullname, employeetype, employmentstatus)
        VALUES (%s, %s, 'G06 Driver', 'Driver', 'Active') RETURNING employeeid
    """, (company_id, branch_id))
    employee_id = cur.fetchone()[0]
    cur.execute("SELECT MIN(ratetypeid) FROM payroll.ratetypes")
    rate_type_id = cur.fetchone()[0]

    cur.execute("""
        INSERT INTO payroll.payprofiles (companyid, profilecode, profilename, effectivefrom)
        VALUES (%s, 'G06-PROFILE', 'G06 profile', DATE '2099-01-01') RETURNING payprofileid
    """, (company_id,))
    profile_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO payroll.payprofilepayitems (payprofileid, ratetypeid)
        VALUES (%s, %s)
    """, (profile_id, rate_type_id))
    cur.execute("""
        INSERT INTO payroll.payprofilerates (payprofileid, ratetypeid, rateamount, effectivefrom)
        VALUES (%s, %s, 12.5, DATE '2099-01-01')
    """, (profile_id, rate_type_id))
    cur.execute("""
        INSERT INTO payroll.personpayprofileassignments
            (companyid, employeeid, payprofileid, branchid, effectivefrom)
        VALUES (%s, %s, %s, %s, DATE '2099-01-01')
    """, (company_id, employee_id, profile_id, branch_id))
    return {"company_id": company_id, "branch_id": branch_id,
            "employee_id": employee_id, "rate_type_id": rate_type_id}


def test_0081_dead_pay_profile_state_stops_locking_company_currency(g0_6_database):
    env, dsn = g0_6_database
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        ids = _seed_company_with_pay_profile_state(cur)
        conn.commit()
        cur.execute("SELECT core.fn_company_has_durable_monetary_state(%s)", (ids["company_id"],))
        assert cur.fetchone() == (True,)
        with pytest.raises(psycopg2.errors.CheckViolation, match="COMPANY_CURRENCY_CHANGE_BLOCKED"):
            cur.execute("UPDATE core.companies SET currencycode = 'EUR' WHERE companyid = %s",
                        (ids["company_id"],))
        conn.rollback()

    upgraded = _alembic(env, "upgrade", "0081")
    assert upgraded.returncode == 0, upgraded.stderr

    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        # The company survives and the retired profile state no longer locks its currency.
        cur.execute("SELECT core.fn_company_has_durable_monetary_state(%s)", (ids["company_id"],))
        assert cur.fetchone() == (False,)
        cur.execute("UPDATE core.companies SET currencycode = 'EUR' WHERE companyid = %s",
                    (ids["company_id"],))
        cur.execute("SELECT currencycode FROM core.companies WHERE companyid = %s",
                    (ids["company_id"],))
        assert cur.fetchone() == ("EUR",)
        cur.execute("SELECT COUNT(*) FROM core.employees WHERE employeeid = %s", (ids["employee_id"],))
        assert cur.fetchone() == (1,)
        conn.commit()

        # Current monetary state still locks the currency permanently.
        cur.execute("""
            INSERT INTO core.drivers (companyid, branchid, employeeid, drivercode, driverstatus)
            VALUES (%s, %s, %s, 'G06-D', 'Active') RETURNING driverid
        """, (ids["company_id"], ids["branch_id"], ids["employee_id"]))
        driver_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO payroll.driverrates
                (companyid, branchid, driverid, ratetypeid, amount, effectivefrom, status)
            VALUES (%s, %s, %s, %s, 10, DATE '2099-01-01', 'PendingApproval')
        """, (ids["company_id"], ids["branch_id"], driver_id, ids["rate_type_id"]))
        cur.execute("SELECT core.fn_company_has_durable_monetary_state(%s)", (ids["company_id"],))
        assert cur.fetchone() == (True,)
        with pytest.raises(psycopg2.errors.CheckViolation, match="COMPANY_CURRENCY_CHANGE_BLOCKED"):
            cur.execute("UPDATE core.companies SET currencycode = 'USD' WHERE companyid = %s",
                        (ids["company_id"],))
        conn.rollback()

        cur.execute("SELECT version_num FROM public.alembic_version")
        assert cur.fetchone() == ("0081",)
