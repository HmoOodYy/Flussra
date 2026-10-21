"""Migration 0089: the immutable calculation evidence cutover."""
from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql

from app.db.schema_guard import _check_calculation_evidence_v2

_ROOT = Path(__file__).resolve().parents[2]


def _alembic(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "alembic", *args],
        cwd=_ROOT, env=env, capture_output=True, text=True, check=False,
    )


@contextmanager
def _disposable_database(pg_instance, prefix: str):
    dsn = pg_instance.dsn()
    name = f"{prefix}_{uuid4().hex[:12]}"
    admin = psycopg2.connect(**dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    env = os.environ.copy()
    env["DATABASE_URL"] = f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:{dsn['port']}/{name}"
    env["SECRET_KEY"] = "calculation-evidence-disposable-migration-test"
    try:
        yield env, {**dsn, "database": name}
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def pre_0089_database(pg_instance):
    """A disposable database at 0088, the last schema with the legacy evidence shape."""
    with _disposable_database(pg_instance, "ev89pre") as (env, dsn):
        assert _alembic(env, "upgrade", "0088").returncode == 0
        yield env, dsn


@pytest.fixture(scope="module")
def evidence_database(pg_instance):
    """A disposable database at 0089, the schema this migration produces."""
    with _disposable_database(pg_instance, "ev89head") as (env, dsn):
        result = _alembic(env, "upgrade", "0089")
        assert result.returncode == 0, result.stderr
        yield env, dsn


def _seed_period(cursor, *, status: str) -> tuple[int, int, int, int]:
    code = "EV" + uuid4().hex[:8]
    cursor.execute("""
        INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended, currencycode)
        VALUES (%s, %s, %s, 'Active', FALSE, 'USD') RETURNING companyid
    """, (code, code, code))
    company_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault, timezoneid)
        VALUES (%s, 'B0', 'Branch 0', 'Active', TRUE, 'UTC') RETURNING branchid
    """, (company_id,))
    branch_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO sec.users (companyid, username, displayname) VALUES (%s, %s, 'ev89') RETURNING userid
    """, (company_id, "ev_" + uuid4().hex[:10]))
    user_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (%s, %s, %s, %s, 'ev89 period', 'Week', DATE '2040-01-01', DATE '2040-01-07')
        RETURNING payrollperiodid
    """, (company_id, branch_id, status, "EV-" + uuid4().hex[:10]))
    return company_id, branch_id, cursor.fetchone()[0], user_id


def _legacy_snapshot(cursor, company_id: int, branch_id: int, period_id: int, user_id: int) -> None:
    cursor.execute("""
        INSERT INTO payroll.payrollcalculationsnapshots
            (companyid, branchid, payrollperiodid, revisionnumber, calculationversion,
             sourceconfighash, snapshothash, createdbyuserid, totalexpectedpay,
             currencycode, currencyminorunitdigits)
        VALUES (%s, %s, %s, 1, 'payroll-calculation-v1', %s, %s, %s, 0, 'USD', 2)
    """, (company_id, branch_id, period_id, "a" * 64, "b" * 64, user_id))


def _version(dsn) -> str:
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT version_num FROM public.alembic_version")
        return cursor.fetchone()[0]


def _columns(dsn, table: str) -> set[str]:
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        cursor.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'payroll' AND table_name = %s
        """, (table,))
        return {row[0] for row in cursor.fetchall()}


@pytest.mark.parametrize("kind", ["snapshot", "finalized_period"])
def test_0089_refuses_pre_existing_evidence_before_any_ddl(pre_0089_database, kind):
    env, dsn = pre_0089_database
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        if kind == "snapshot":
            company_id, branch_id, period_id, user_id = _seed_period(cursor, status="Cancelled")
            _legacy_snapshot(cursor, company_id, branch_id, period_id, user_id)
        else:
            _seed_period(cursor, status="Locked")

    result = _alembic(env, "upgrade", "0089")

    assert result.returncode != 0
    assert "IMMUTABLE_EVIDENCE_REBUILD_REQUIRED" in result.stderr + result.stdout
    assert _version(dsn) == "0088"
    # The refusal ran before any structural statement.
    assert "periodstartdate" not in _columns(dsn, "payrollcalculationsnapshots")
    assert "payrollcalculationsnapshotcomponents" not in _tables(dsn)


def _tables(dsn) -> set[str]:
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'payroll'")
        return {row[0] for row in cursor.fetchall()}


def test_0089_applies_over_periods_that_carry_no_evidence(pre_0089_database):
    env, dsn = pre_0089_database
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        _seed_period(cursor, status="Open")
    result = _alembic(env, "upgrade", "0089")
    assert result.returncode == 0, result.stderr
    assert _version(dsn) == "0089"


def test_0089_is_irreversible(evidence_database):
    env, _ = evidence_database
    result = _alembic(env, "downgrade", "0088")
    assert result.returncode != 0
    assert "irreversible" in (result.stderr + result.stdout)


def test_fresh_upgrade_reaches_the_immutable_evidence_shape(evidence_database):
    _, dsn = evidence_database
    assert _version(dsn) == "0089"
    assert {"payrollcalculationsnapshotcomponents", "payrollcalculationsnapshotminmaxevaluations",
            "payrollcalculationsnapshotminmaxrules"} <= _tables(dsn)
    assert {"periodstartdate", "periodenddate", "payableroundingversion", "minmaxprorationversion",
            "scheduledworkdates", "warnings"} <= _columns(dsn, "payrollcalculationsnapshots")
    assert "employeeid" in _columns(dsn, "payrollcalculationdrivertotals")
    assert {"driverratescheduleid", "payrollcalculationsnapshotcomponentid",
            "payrollperioddefinitionid"} <= _columns(dsn, "payrollcalculationsnapshotlines")
    assert "payitemid" not in _columns(dsn, "payrollcalculationsnapshotlines")
    assert {"driverpayruleid", "payitemid", "ruletypesnapshot"}.isdisjoint(
        _columns(dsn, "payrollcalculationsnapshotusedratedefinitions"))
    assert {"payrollcalculationsnapshotid", "payrollcalculationsnapshotcomponentid", "employeeid",
            "componentkind"} <= _columns(dsn, "payrollfinallines")

    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        assert _check_calculation_evidence_v2(cursor) == []
        cursor.execute("""
            SELECT count(*) FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            WHERE t.tgname LIKE 'trg_payrollcalculation%immutable' AND NOT t.tgisinternal
        """)
        assert cursor.fetchone()[0] == 9
