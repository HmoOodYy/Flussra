"""Migration 0085: the target period definition runtime foundation."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import errors, sql

from tests.p3b_fixtures import make_pay_definition, make_rate_definition

_ROOT = Path(__file__).resolve().parents[2]

ALL_STATUSES = ["Draft", "Open", "InReview", "Returned", "Approved", "Locked", "Archived", "Cancelled"]


def _alembic(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "alembic", *args],
        cwd=_ROOT, env=env, capture_output=True, text=True, check=False,
    )


@pytest.fixture
def p4b_database(pg_instance):
    dsn = pg_instance.dsn()
    name = "p4bm_" + uuid4().hex[:12]
    admin = psycopg2.connect(**dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    env = os.environ.copy()
    env["DATABASE_URL"] = f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:{dsn['port']}/{name}"
    env["SECRET_KEY"] = "p4b-disposable-migration-test"
    try:
        yield env, {**dsn, "database": name}
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def _company(cur, code: str) -> tuple[int, int]:
    cur.execute("""
        INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended,
                                    timezonename, currencycode)
        VALUES (%s, %s, %s, 'Active', FALSE, 'UTC', 'USD') RETURNING companyid
    """, (code, code, code))
    company_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (%s, %s, %s, 'Active', TRUE) RETURNING branchid
    """, (company_id, code, code))
    return company_id, cur.fetchone()[0]


def _branch(cur, company_id: int, code: str) -> int:
    cur.execute("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (%s, %s, %s, 'Active', FALSE) RETURNING branchid
    """, (company_id, code, code))
    return cur.fetchone()[0]


def _period(cur, company_id: int, branch_id: int, status: str = "Open",
            start: str = "2030-01-07", end: str = "2030-01-13") -> int:
    pointer = None
    if status == "Returned":
        cur.execute("""
            INSERT INTO review.managerreviewitems
                (companyid, branchid, requesttype, title, status, priority)
            VALUES (%s, %s, 'PeriodApproval', 'p4b migration', 'Pending', 'Normal')
            RETURNING reviewitemid
        """, (company_id, branch_id))
        pointer = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate,
             currentreturnreviewitemid)
        VALUES (%s, %s, %s, %s, %s, 'Week', %s, %s, %s) RETURNING payrollperiodid
    """, (company_id, branch_id, status, "P-" + uuid4().hex[:8], "p4b " + status, start, end,
          pointer))
    return cur.fetchone()[0]


def _definition_graph(cur, company_id: int, branch_id: int, *, method: str = "PerUnit") -> dict:
    """A PayDefinition with its RateDefinition and an active BranchPayItemConfig version."""
    pay_definition_id = make_pay_definition(cur, company_id, method=method)
    rate_definition_id = make_rate_definition(cur, pay_definition_id)
    cur.execute("""
        INSERT INTO payroll.branchpayitemconfig
            (companyid, branchid, paydefinitionid, isactive, effectivefrom)
        VALUES (%s, %s, %s, TRUE, '2020-01-01') RETURNING configid
    """, (company_id, branch_id, pay_definition_id))
    return {"pay_definition_id": pay_definition_id, "rate_definition_id": rate_definition_id,
            "config_id": cur.fetchone()[0]}


_PPD_INSERT = """
    INSERT INTO payroll.payrollperioddefinitions
        (payrollperiodid, companyid, branchid, paydefinitionid, ratedefinitionid,
         definitioncodesnapshot, definitionnamesnapshot, inputtypesnapshot, unitsnapshot,
         calculationmethodsnapshot, calculationmethodversionsnapshot, rateshapesnapshot,
         definitionstatusatsnapshot, isactiveinperiod, sortorder, sourcebranchconfigid,
         sourcebranchconfigeffectivefrom)
    VALUES (%s, %s, %s, %s, %s, 'ITEM_ALPHA', 'Item alpha', 'Decimal', 'unit',
            'PerUnit', 1, 'Scalar', 'Active', TRUE, 0, %s, '2020-01-01')
    RETURNING payrollperioddefinitionid
"""


def _ppd(cur, period_id, company_id, branch_id, graph) -> int:
    cur.execute(_PPD_INSERT, (period_id, company_id, branch_id, graph["pay_definition_id"],
                              graph["rate_definition_id"], graph["config_id"]))
    return cur.fetchone()[0]


def _draft_line(cur, company_id, branch_id, period_id, ppd_id, driver_id) -> int:
    cur.execute("""
        INSERT INTO payroll.payrolldraftlines
            (companyid, branchid, payrollperiodid, driverid, workdate, payrollperioddefinitionid,
             quantity, sourcetype)
        VALUES (%s, %s, %s, %s, '2030-01-08', %s, 5, 'Manual') RETURNING draftlineid
    """, (company_id, branch_id, period_id, driver_id, ppd_id))
    return cur.fetchone()[0]


def _driver(cur, company_id: int, branch_id: int) -> int:
    cur.execute("""
        INSERT INTO core.employees (companyid, branchid, fullname, employeetype, employmentstatus)
        VALUES (%s, %s, 'P4b Driver', 'Driver', 'Active') RETURNING employeeid
    """, (company_id, branch_id))
    employee_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO core.drivers (companyid, branchid, employeeid, drivercode, driverstatus)
        VALUES (%s, %s, %s, %s, 'Active') RETURNING driverid
    """, (company_id, branch_id, employee_id, "D" + uuid4().hex[:8]))
    return cur.fetchone()[0]


def _tables(cur) -> set[str]:
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'payroll'
          AND table_name IN ('payrollperiodpayitems', 'payrollperioddefinitions')
    """)
    return {row[0] for row in cur.fetchall()}


def _columns(cur, table: str) -> set[str]:
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'payroll' AND table_name = %s
    """, (table,))
    return {row[0] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Preflight and fresh upgrade
# ---------------------------------------------------------------------------

def test_fresh_database_upgrades_to_the_period_definition_model(p4b_database):
    env, dsn = p4b_database
    assert _alembic(env, "upgrade", "head").returncode == 0
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM public.alembic_version")
        assert cur.fetchone() == ("0085",)
        assert _tables(cur) == {"payrollperioddefinitions"}
        columns = _columns(cur, "payrollperioddefinitions")
        assert {"payrollperioddefinitionid", "paydefinitionid", "ratedefinitionid",
                "definitioncodesnapshot", "calculationmethodsnapshot",
                "calculationmethodversionsnapshot", "rateshapesnapshot", "isactiveinperiod",
                "sourcebranchconfigid", "sourcebranchconfigeffectivefrom",
                "sourcebranchconfigeffectiveto"} <= columns
        assert not columns & {"payitemid", "payitemcode", "itemscope", "ratebehavior"}
        draft_columns = _columns(cur, "payrolldraftlines")
        assert "payrollperioddefinitionid" in draft_columns
        cur.execute("SELECT is_nullable FROM information_schema.columns WHERE table_schema = "
                    "'payroll' AND table_name = 'payrolldraftlines' AND column_name = 'linetype'")
        assert cur.fetchone() == ("YES",)


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_any_existing_period_blocks_the_cutover_and_leaves_0084_untouched(p4b_database, status):
    env, dsn = p4b_database
    assert _alembic(env, "upgrade", "0084").returncode == 0
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        company_id, branch_id = _company(cur, "P4BA")
        _period(cur, company_id, branch_id, status)
        cur.execute("SELECT count(*) FROM payroll.payrollperiodpayitems")
        legacy_rows = cur.fetchone()[0]
        before_columns = _columns(cur, "payrollperiodpayitems")
        conn.commit()

    result = _alembic(env, "upgrade", "0085")
    assert result.returncode != 0
    assert "P4B_PRE_CUTOVER_PERIODS_REQUIRE_REBUILD" in result.stderr + result.stdout

    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM public.alembic_version")
        assert cur.fetchone() == ("0084",)
        assert _tables(cur) == {"payrollperiodpayitems"}
        assert _columns(cur, "payrollperiodpayitems") == before_columns
        assert "payrollperioddefinitionid" not in _columns(cur, "payrolldraftlines")
        cur.execute("SELECT count(*) FROM payroll.payrollperiodpayitems")
        assert cur.fetchone() == (legacy_rows,)
        cur.execute("SELECT count(*) FROM payroll.payrollperiods WHERE status = %s", (status,))
        assert cur.fetchone() == (1,)


def test_0085_is_irreversible(p4b_database):
    env, _ = p4b_database
    assert _alembic(env, "upgrade", "0085").returncode == 0
    result = _alembic(env, "downgrade", "0084")
    assert result.returncode != 0
    assert "irreversible" in (result.stderr + result.stdout).lower()


# ---------------------------------------------------------------------------
# Period definition integrity
# ---------------------------------------------------------------------------

def test_period_definition_scope_and_snapshot_constraints(p4b_database):
    env, dsn = p4b_database
    assert _alembic(env, "upgrade", "head").returncode == 0
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        company_a, branch_a = _company(cur, "P4BC")
        company_b, branch_b = _company(cur, "P4BD")
        period_a = _period(cur, company_a, branch_a)
        graph_a = _definition_graph(cur, company_a, branch_a)
        graph_b = _definition_graph(cur, company_b, branch_b)
        other_branch = _branch(cur, company_a, "P4BE")
        graph_other_branch = _definition_graph(cur, company_a, other_branch)
        conn.commit()

        ppd_id = _ppd(cur, period_a, company_a, branch_a, graph_a)
        conn.commit()

        def rejected(error, action) -> None:
            with pytest.raises(error):
                action()
            conn.rollback()

        # One row per PayDefinition per period.
        rejected(errors.UniqueViolation, lambda: _ppd(cur, period_a, company_a, branch_a, graph_a))

        def fresh() -> dict:
            graph = _definition_graph(cur, company_a, branch_a)
            conn.commit()
            return graph

        # A PayDefinition of another Company, a period of another Company or Branch.
        rejected(errors.ForeignKeyViolation,
                 lambda: _ppd(cur, period_a, company_a, branch_a, graph_b))
        wrong_company = fresh()
        rejected(errors.ForeignKeyViolation,
                 lambda: _ppd(cur, period_a, company_b, branch_a, wrong_company))
        wrong_branch = fresh()
        rejected(errors.ForeignKeyViolation,
                 lambda: _ppd(cur, period_a, company_a, other_branch, wrong_branch))
        # The Branch configuration version must belong to that Branch and PayDefinition.
        wrong_config = fresh()
        rejected(errors.ForeignKeyViolation,
                 lambda: _ppd(cur, period_a, company_a, branch_a,
                              {**wrong_config, "config_id": graph_other_branch["config_id"]}))
        # A RateDefinition must belong to the same PayDefinition.
        wrong_rate = fresh()
        rejected(errors.ForeignKeyViolation,
                 lambda: _ppd(cur, period_a, company_a, branch_a,
                              {**wrong_rate, "rate_definition_id": graph_a["rate_definition_id"]}))
        other_graph = fresh()
        # Snapshot contract.
        with pytest.raises(errors.CheckViolation):
            cur.execute(_PPD_INSERT.replace("'PerUnit', 1, 'Scalar'", "'PerUnit', 1, 'OrdinalTierSchedule'"),
                        (period_a, company_a, branch_a, other_graph["pay_definition_id"],
                         other_graph["rate_definition_id"], other_graph["config_id"]))
        conn.rollback()

        # A snapshot is immutable.
        with pytest.raises(errors.CheckViolation):
            cur.execute("UPDATE payroll.payrollperioddefinitions SET isactiveinperiod = FALSE "
                        "WHERE payrollperioddefinitionid = %s", (ppd_id,))
        conn.rollback()


def test_draft_lines_reference_only_their_own_period_definition(p4b_database):
    env, dsn = p4b_database
    assert _alembic(env, "upgrade", "head").returncode == 0
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        company_a, branch_a = _company(cur, "P4BF")
        company_b, branch_b = _company(cur, "P4BG")
        branch_a2 = _branch(cur, company_a, "P4BH")
        driver_a = _driver(cur, company_a, branch_a)
        driver_a2 = _driver(cur, company_a, branch_a2)
        period_a = _period(cur, company_a, branch_a)
        period_a_other = _period(cur, company_a, branch_a, "Draft", "2030-01-14", "2030-01-20")
        period_a2 = _period(cur, company_a, branch_a2)
        graph_a = _definition_graph(cur, company_a, branch_a)
        graph_a2 = _definition_graph(cur, company_a, branch_a2)
        ppd_a = _ppd(cur, period_a, company_a, branch_a, graph_a)
        ppd_a2 = _ppd(cur, period_a2, company_a, branch_a2, graph_a2)
        conn.commit()

        _draft_line(cur, company_a, branch_a, period_a, ppd_a, driver_a)
        conn.commit()

        def rejected(error, action) -> None:
            with pytest.raises(error):
                action()
            conn.rollback()

        # Another period, another Branch's definition, another Company's scope.
        rejected(errors.ForeignKeyViolation,
                 lambda: _draft_line(cur, company_a, branch_a, period_a_other, ppd_a, driver_a))
        rejected(errors.ForeignKeyViolation,
                 lambda: _draft_line(cur, company_a, branch_a, period_a, ppd_a2, driver_a))
        rejected(errors.ForeignKeyViolation,
                 lambda: _draft_line(cur, company_b, branch_a, period_a, ppd_a, driver_a))
        # One active line per (driver, date, period definition).
        rejected(errors.UniqueViolation,
                 lambda: _draft_line(cur, company_a, branch_a, period_a, ppd_a, driver_a))
        assert driver_a2 != driver_a


def test_a_draft_line_is_either_a_target_source_fact_or_a_compatibility_row(p4b_database):
    env, dsn = p4b_database
    assert _alembic(env, "upgrade", "head").returncode == 0
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        company_id, branch_id = _company(cur, "P4BI")
        driver_id = _driver(cur, company_id, branch_id)
        period_id = _period(cur, company_id, branch_id)
        graph = _definition_graph(cur, company_id, branch_id)
        ppd_id = _ppd(cur, period_id, company_id, branch_id, graph)
        conn.commit()

        insert = """
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid, workdate,
                 payrollperioddefinitionid, linetype, quantity, rateamount, calculatedamount,
                 needsmanagerreview, sourcetype)
            VALUES (%s, %s, %s, %s, '2030-01-08', %s, %s, 1, %s, %s, %s, %s)
        """

        def rejected(*params) -> None:
            with pytest.raises(errors.CheckViolation):
                cur.execute(insert, (company_id, branch_id, period_id, driver_id, *params))
            conn.rollback()

        # A target row has no LineType and stores no money.
        rejected(ppd_id, "HOURS", None, None, False, "Manual")
        rejected(ppd_id, None, 9999, None, False, "Manual")
        rejected(ppd_id, None, None, 3, False, "Manual")
        rejected(ppd_id, None, None, None, True, "Manual")
        rejected(ppd_id, None, None, None, False, "System")
        # A row with neither identity is ambiguous.
        rejected(None, None, None, None, False, "Manual")
        # The temporary compatibility row keeps its LineType and may keep stored money.
        cur.execute(insert, (company_id, branch_id, period_id, driver_id, None, "STATUS_PAY",
                             5, 10, False, "System"))
        conn.commit()


def test_zero_pay_definitions_needs_no_sentinel_row(p4b_database):
    env, dsn = p4b_database
    assert _alembic(env, "upgrade", "head").returncode == 0
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        company_id, branch_id = _company(cur, "P4BJ")
        period_id = _period(cur, company_id, branch_id)
        conn.commit()
        cur.execute("SELECT count(*) FROM payroll.payrollperioddefinitions "
                    "WHERE payrollperiodid = %s", (period_id,))
        assert cur.fetchone() == (0,)
        cur.execute("SELECT count(*) FROM payroll.paydefinitions WHERE companyid = %s",
                    (company_id,))
        assert cur.fetchone() == (0,)


def test_legacy_pay_item_identity_cannot_be_period_definition_authority(p4b_database):
    env, dsn = p4b_database
    assert _alembic(env, "upgrade", "head").returncode == 0
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        columns = _columns(cur, "payrollperioddefinitions")
        assert "payitemid" not in columns and "payitemcode" not in columns
        cur.execute("""
            SELECT count(*) FROM information_schema.table_constraints tc
            JOIN information_schema.constraint_column_usage ccu USING (constraint_name)
            WHERE tc.table_schema = 'payroll' AND tc.table_name = 'payrollperioddefinitions'
              AND tc.constraint_type = 'FOREIGN KEY' AND ccu.table_name = 'payitems'
        """)
        assert cur.fetchone() == (0,)
