"""G0.4B migration safety for retiring the legacy Period source architecture."""
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
def g0_4b_database(pg_instance):
    dsn = pg_instance.dsn()
    name = "g04b_" + uuid4().hex[:12]
    admin = psycopg2.connect(**dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    env = os.environ.copy()
    env["DATABASE_URL"] = (
        f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:{dsn['port']}/{name}"
    )
    env["SECRET_KEY"] = "g0-4b-disposable-migration-test"
    check_dsn = {**dsn, "database": name}
    try:
        result = _alembic(env, "upgrade", "0077")
        assert result.returncode == 0, result.stderr
        yield env, check_dsn
    finally:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def _seed_legacy_period_state(conn) -> dict[str, int]:
    """Legacy residue: a Period DraftLine, a Daily line, a Bonus bridge row, and a custom Period PayItem."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended,
                                        timezonename, currencycode)
            VALUES ('G04B', 'G04B', 'G04B', 'Active', FALSE, 'UTC', 'USD') RETURNING companyid
        """)
        company_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
            VALUES (%s, 'G04B', 'G04B', 'Active', TRUE) RETURNING branchid
        """, (company_id,))
        branch_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO core.employees (companyid, branchid, fullname, employeetype, employmentstatus)
            VALUES (%s, %s, 'G04B Driver', 'Driver', 'Active') RETURNING employeeid
        """, (company_id, branch_id))
        employee_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO core.drivers (companyid, branchid, employeeid, drivercode, driverstatus)
            VALUES (%s, %s, %s, 'G04B-D', 'Active') RETURNING driverid
        """, (company_id, branch_id, employee_id))
        driver_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (%s, %s, 'Open', 'G04B-P', 'G04B', 'Week', '2098-04-01', '2098-04-07')
            RETURNING payrollperiodid
        """, (company_id, branch_id))
        period_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO payroll.payitems
                (companyid, payitemcode, payitemname, category, datatype, itemscope,
                 ratebehavior, status, issystemstandard)
            VALUES (%s, 'G04B_PERIOD', 'Custom Period', 'Custom', 'Currency', 'Period',
                    'EnteredAmount', 'Active', FALSE) RETURNING payitemid
        """, (company_id,))
        custom_period_item_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid, workdate, linetype, linescope,
                 quantity, calculatedamount, sourcetype, status, needsmanagerreview)
            VALUES (%s, %s, %s, %s, NULL, 'BONUS', 'Period', 1, 50, 'Manual', 'Active', FALSE)
            RETURNING draftlineid
        """, (company_id, branch_id, period_id, driver_id))
        period_line_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid, workdate, linetype, linescope,
                 quantity, calculatedamount, sourcetype, status, needsmanagerreview)
            VALUES (%s, %s, %s, %s, '2098-04-02', 'HOURS', 'Daily', 2, 20, 'Manual', 'Active', FALSE)
            RETURNING draftlineid
        """, (company_id, branch_id, period_id, driver_id))
        daily_line_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO payroll.payrollbonusevents
                (companyid, branchid, payrollperiodid, driverid, amount, status, sourcedraftlineid)
            VALUES (%s, %s, %s, %s, 50, 'Active', %s) RETURNING payrollbonuseventid
        """, (company_id, branch_id, period_id, driver_id, period_line_id))
        bonus_event_id = cur.fetchone()[0]
    conn.commit()
    return {
        "company_id": company_id, "branch_id": branch_id, "period_id": period_id,
        "driver_id": driver_id, "period_line_id": period_line_id,
        "daily_line_id": daily_line_id, "bonus_event_id": bonus_event_id,
        "custom_period_item_id": custom_period_item_id,
    }


def test_0078_removes_legacy_rows_and_keeps_canonical_state(g0_4b_database):
    env, dsn = g0_4b_database
    with psycopg2.connect(**dsn) as conn:
        ids = _seed_legacy_period_state(conn)

    upgraded = _alembic(env, "upgrade", "0078")
    assert upgraded.returncode == 0, upgraded.stderr

    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT draftlineid FROM payroll.payrolldraftlines ORDER BY draftlineid")
        assert cur.fetchall() == [(ids["daily_line_id"],)]
        cur.execute("SELECT payrollbonuseventid, amount FROM payroll.payrollbonusevents")
        assert cur.fetchall() == [(ids["bonus_event_id"], 50)]
        cur.execute("""
            SELECT payitemcode FROM payroll.payitems
            WHERE itemscope = 'Period' ORDER BY payitemcode
        """)
        assert cur.fetchall() == [("SYS_MAX_CAP",), ("SYS_MIN_TOPUP",)]
        cur.execute("""
            SELECT DISTINCT ratebehavior FROM payroll.payitems
            WHERE payitemcode IN ('SYS_MIN_TOPUP', 'SYS_MAX_CAP')
        """)
        assert cur.fetchall() == [("Calculated",)]
        cur.execute("SELECT version_num FROM public.alembic_version")
        assert cur.fetchone() == ("0078",)


def test_0078_daily_business_key_stays_unique_and_workdate_required(g0_4b_database):
    env, dsn = g0_4b_database
    with psycopg2.connect(**dsn) as conn:
        ids = _seed_legacy_period_state(conn)
    assert _alembic(env, "upgrade", "0078").returncode == 0

    insert_daily = """
        INSERT INTO payroll.payrolldraftlines
            (companyid, branchid, payrollperiodid, driverid, workdate, linetype,
             quantity, sourcetype, status, needsmanagerreview)
        VALUES (%s, %s, %s, %s, '2098-04-02', 'HOURS', 1, 'Manual', 'Active', FALSE)
    """
    args = (ids["company_id"], ids["branch_id"], ids["period_id"], ids["driver_id"])
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(insert_daily, args)
        conn.rollback()
        with pytest.raises(psycopg2.errors.NotNullViolation):
            cur.execute("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverid, workdate, linetype,
                     quantity, sourcetype, status, needsmanagerreview)
                VALUES (%s, %s, %s, %s, NULL, 'MILES', 1, 'Manual', 'Active', FALSE)
            """, args)


def test_0078_fails_closed_when_final_ledger_depends_on_legacy_period_line(g0_4b_database):
    env, dsn = g0_4b_database
    with psycopg2.connect(**dsn) as conn:
        ids = _seed_legacy_period_state(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.allow_payroll_final_line_insert', 'true', true)")
            cur.execute("""
                INSERT INTO payroll.payrollfinallines
                    (companyid, branchid, payrollperiodid, draftlineid, driverid, linetype,
                     quantity, finalamount, sourcetype, approvedatutc, linescope,
                     currencycode, currencyminorunitdigits)
                VALUES (%s, %s, %s, %s, %s, 'BONUS', 1, 50, 'DraftLine', NOW(), 'Period', 'USD', 2)
            """, (ids["company_id"], ids["branch_id"], ids["period_id"],
                  ids["period_line_id"], ids["driver_id"]))
        conn.commit()

    refused = _alembic(env, "upgrade", "0078")
    assert refused.returncode != 0
    assert "G04B_BLOCKED_FINAL_LEDGER_DEPENDENCY" in refused.stderr

    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM public.alembic_version")
        assert cur.fetchone() == ("0077",)
        cur.execute("SELECT COUNT(*) FROM payroll.payrolldraftlines")
        assert cur.fetchone() == (2,)
        cur.execute("SELECT COUNT(*) FROM payroll.payitems WHERE payitemcode = 'BONUS'")
        assert cur.fetchone() == (1,)
