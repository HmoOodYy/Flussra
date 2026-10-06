"""G0.4C migration safety for the stable payroll calculation contract."""
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
_HASH = "a" * 64


def _alembic(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "alembic", *args],
        cwd=_ROOT, env=env, capture_output=True, text=True, check=False,
    )


@pytest.fixture
def g0_4c_database(pg_instance):
    dsn = pg_instance.dsn()
    name = "g04c_" + uuid4().hex[:12]
    admin = psycopg2.connect(**dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    env = os.environ.copy()
    env["DATABASE_URL"] = (
        f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:{dsn['port']}/{name}"
    )
    env["SECRET_KEY"] = "g0-4c-disposable-migration-test"
    check_dsn = {**dsn, "database": name}
    try:
        result = _alembic(env, "upgrade", "0078")
        assert result.returncode == 0, result.stderr
        yield env, check_dsn
    finally:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def _seed_period(cur) -> dict[str, int]:
    cur.execute("""
        INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended,
                                    timezonename, currencycode)
        VALUES ('G04C', 'G04C', 'G04C', 'Active', FALSE, 'UTC', 'USD') RETURNING companyid
    """)
    company_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (%s, 'G04C', 'G04C', 'Active', TRUE) RETURNING branchid
    """, (company_id,))
    branch_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO core.employees (companyid, branchid, fullname, employeetype, employmentstatus)
        VALUES (%s, %s, 'G04C Driver', 'Driver', 'Active') RETURNING employeeid
    """, (company_id, branch_id))
    employee_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO core.drivers (companyid, branchid, employeeid, drivercode, driverstatus)
        VALUES (%s, %s, %s, 'G04C-D', 'Active') RETURNING driverid
    """, (company_id, branch_id, employee_id))
    driver_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (%s, %s, 'Open', 'G04C-P', 'G04C', 'Week', '2098-05-01', '2098-05-07')
        RETURNING payrollperiodid
    """, (company_id, branch_id))
    period_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO sec.users (companyid, username, displayname, passwordhash, isactive, canlogin)
        VALUES (%s, 'g04c', 'G04C', 'x', TRUE, FALSE) RETURNING userid
    """, (company_id,))
    user_id = cur.fetchone()[0]
    return {"company_id": company_id, "branch_id": branch_id, "driver_id": driver_id,
            "period_id": period_id, "user_id": user_id}


def _insert_snapshot(cur, ids: dict[str, int], version: str) -> int:
    cur.execute("""
        INSERT INTO payroll.payrollcalculationsnapshots
            (companyid, branchid, payrollperiodid, revisionnumber, calculationversion,
             sourceconfighash, snapshothash, createdbyuserid, totalexpectedpay,
             currencycode, currencyminorunitdigits)
        VALUES (%s, %s, %s, 1, %s, %s, %s, %s, 49, 'USD', 2)
        RETURNING payrollcalculationsnapshotid
    """, (ids["company_id"], ids["branch_id"], ids["period_id"], version,
          _HASH, _HASH, ids["user_id"]))
    return cur.fetchone()[0]


def test_0079_clean_upgrade_accepts_canonical_calculation_snapshot(g0_4c_database):
    env, dsn = g0_4c_database
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        ids = _seed_period(cur)
    upgraded = _alembic(env, "upgrade", "0079")
    assert upgraded.returncode == 0, upgraded.stderr

    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        snapshot_id = _insert_snapshot(cur, ids, "payroll-calculation-v1")
        cur.execute("""
            INSERT INTO payroll.payrollcalculationdrivertotals
                (payrollcalculationsnapshotid, companyid, branchid, driverid,
                 dailypay, statuspay, minimumadjustment, maximumadjustment,
                 bonustotal, expectedpay)
            VALUES (%s, %s, %s, %s, 25, 18, 2, -1, 5, 49)
            RETURNING expectedpay
        """, (snapshot_id, ids["company_id"], ids["branch_id"], ids["driver_id"]))
        assert cur.fetchone() == (49,)
        cur.execute("SELECT version_num FROM public.alembic_version")
        assert cur.fetchone() == ("0079",)


def test_0079_snapshot_rejects_unsupported_calculation_version(g0_4c_database):
    env, dsn = g0_4c_database
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        ids = _seed_period(cur)
    assert _alembic(env, "upgrade", "0079").returncode == 0

    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            _insert_snapshot(cur, ids, "current-payroll-v1")


def test_0079_refuses_to_reinterpret_existing_calculation_snapshot(g0_4c_database):
    env, dsn = g0_4c_database
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        ids = _seed_period(cur)
        _insert_snapshot(cur, ids, "current-payroll-v1")

    refused = _alembic(env, "upgrade", "0079")
    assert refused.returncode != 0
    assert "G04C_BLOCKED_RETIRED_CALCULATION_CONTRACT" in refused.stderr

    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM public.alembic_version")
        assert cur.fetchone() == ("0078",)
        cur.execute("SELECT calculationversion FROM payroll.payrollcalculationsnapshots")
        assert cur.fetchall() == [("current-payroll-v1",)]
