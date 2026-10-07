"""P3b migration 0082: fresh schema creation, upgrade from 0081 and reversibility."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import errors, sql

_ROOT = Path(__file__).resolve().parents[2]

TARGET_TABLES = {
    "paydefinitions", "ratedefinitions", "ratecomponentdefinitions",
    "driverrateassignments", "driverratevalues",
}


def _alembic(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "alembic", *args],
        cwd=_ROOT, env=env, capture_output=True, text=True, check=False,
    )


@pytest.fixture
def p3b_migration_database(pg_instance):
    dsn = pg_instance.dsn()
    name = "p3bm_" + uuid4().hex[:12]
    admin = psycopg2.connect(**dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    env = os.environ.copy()
    env["DATABASE_URL"] = (
        f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:{dsn['port']}/{name}"
    )
    env["SECRET_KEY"] = "p3b-disposable-migration-test"
    try:
        yield env, {**dsn, "database": name}
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
        admin.close()


def _target_tables(cur) -> set[str]:
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'payroll' AND table_name = ANY(%s)
    """, (sorted(TARGET_TABLES),))
    return {row[0] for row in cur.fetchall()}


def test_alembic_has_a_single_head():
    result = subprocess.run(
        [sys.executable, "-B", "-m", "alembic", "heads"],
        cwd=_ROOT, capture_output=True, text=True, check=False,
        env={**os.environ, "DATABASE_URL": "postgresql+asyncpg://x:x@127.0.0.1:1/x",
             "SECRET_KEY": "p3b-heads-only"},
    )
    assert result.returncode == 0, result.stderr
    heads = [line for line in result.stdout.splitlines() if "(head)" in line]
    assert len(heads) == 1 and heads[0].startswith("0083"), result.stdout


def test_0081_with_legacy_state_upgrades_to_0082_without_touching_it(p3b_migration_database):
    env, dsn = p3b_migration_database
    assert _alembic(env, "upgrade", "0081").returncode == 0

    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        assert _target_tables(cur) == set()
        cur.execute("""
            INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended,
                                        timezonename, currencycode)
            VALUES ('P3BM', 'P3BM', 'P3BM', 'Active', FALSE, 'UTC', 'USD') RETURNING companyid
        """)
        company_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
            VALUES (%s, 'P3BM', 'P3BM', 'Active', TRUE) RETURNING branchid
        """, (company_id,))
        branch_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO core.employees (companyid, branchid, fullname, employeetype, employmentstatus)
            VALUES (%s, %s, 'P3BM', 'Driver', 'Active') RETURNING employeeid
        """, (company_id, branch_id))
        employee_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO core.drivers (companyid, branchid, employeeid, drivercode, driverstatus)
            VALUES (%s, %s, %s, 'P3BM', 'Active') RETURNING driverid
        """, (company_id, branch_id, employee_id))
        driver_id = cur.fetchone()[0]
        cur.execute("SELECT MIN(ratetypeid) FROM payroll.ratetypes")
        rate_type_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO payroll.driverrates
                (companyid, branchid, driverid, ratetypeid, amount, effectivefrom, status)
            VALUES (%s, %s, %s, %s, 10, DATE '2099-01-01', 'PendingApproval')
            RETURNING driverrateid
        """, (company_id, branch_id, driver_id, rate_type_id))
        legacy_rate_id = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM payroll.payitems")
        pay_items_before = cur.fetchone()[0]
        conn.commit()

    upgraded = _alembic(env, "upgrade", "0082")
    assert upgraded.returncode == 0, upgraded.stderr

    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM public.alembic_version")
        assert cur.fetchone() == ("0082",)
        assert _target_tables(cur) == TARGET_TABLES
        for table in TARGET_TABLES:
            cur.execute(sql.SQL("SELECT count(*) FROM payroll.{}").format(sql.Identifier(table)))
            assert cur.fetchone()[0] == 0
        cur.execute("SELECT amount FROM payroll.driverrates WHERE driverrateid = %s",
                    (legacy_rate_id,))
        assert cur.fetchone()[0] == 10
        cur.execute("SELECT count(*) FROM payroll.payitems")
        assert cur.fetchone()[0] == pay_items_before
        cur.execute("SELECT core.fn_company_has_durable_monetary_state(%s)", (company_id,))
        assert cur.fetchone() == (True,)
        with pytest.raises(errors.CheckViolation, match="COMPANY_CURRENCY_CHANGE_BLOCKED"):
            cur.execute("UPDATE core.companies SET currencycode = 'EUR' WHERE companyid = %s",
                        (company_id,))
        conn.rollback()


def test_fresh_database_upgrades_to_head_with_the_target_schema(p3b_migration_database):
    env, dsn = p3b_migration_database
    upgraded = _alembic(env, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM public.alembic_version")
        assert cur.fetchone() == ("0083",)
        assert _target_tables(cur) == TARGET_TABLES
        cur.execute("""
            SELECT count(*) FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'payroll' AND NOT t.tgisinternal
              AND c.relname = ANY(%s)
        """, (sorted(TARGET_TABLES),))
        assert cur.fetchone()[0] >= 9


def test_downgrade_removes_the_empty_target_schema_and_refuses_when_rows_exist(
    p3b_migration_database,
):
    env, dsn = p3b_migration_database
    assert _alembic(env, "upgrade", "head").returncode == 0

    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended,
                                        timezonename)
            VALUES ('P3BD', 'P3BD', 'P3BD', 'Active', FALSE, 'UTC') RETURNING companyid
        """)
        company_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO payroll.paydefinitions
                (companyid, definitioncode, definitionname, inputtype, calculationmethod)
            VALUES (%s, 'X', 'X', 'Decimal', 'PerUnit')
        """, (company_id,))
        conn.commit()

    refused = _alembic(env, "downgrade", "0081")
    assert refused.returncode != 0
    assert "Downgrade of 0082 refused" in refused.stderr

    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM payroll.paydefinitions")
        conn.commit()

    downgraded = _alembic(env, "downgrade", "0081")
    assert downgraded.returncode == 0, downgraded.stderr
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        assert _target_tables(cur) == set()
        cur.execute("SELECT version_num FROM public.alembic_version")
        assert cur.fetchone() == ("0081",)

    assert _alembic(env, "upgrade", "head").returncode == 0
