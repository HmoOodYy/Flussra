"""Migration 0090: the Company Status catalog cutover."""
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

from app.db.schema_guard import _check_calculation_evidence, _check_company_status_catalog
from tests.db_state import replica_replication_role_sync

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
    env["SECRET_KEY"] = "company-status-catalog-disposable-migration-test"
    try:
        yield env, {**dsn, "database": name}
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def pre_0090_database(pg_instance):
    """A disposable database at 0089: Branch-owned Status keys and rate columns."""
    with _disposable_database(pg_instance, "st90pre") as (env, dsn):
        assert _alembic(env, "upgrade", "0089").returncode == 0
        yield env, dsn


@pytest.fixture(scope="module")
def head_database(pg_instance):
    with _disposable_database(pg_instance, "st90head") as (env, dsn):
        result = _alembic(env, "upgrade", "head")
        assert result.returncode == 0, result.stderr
        yield env, dsn


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


def _tables(dsn) -> set[str]:
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'payroll'")
        return {row[0] for row in cursor.fetchall()}


def _seed_world(cursor) -> dict:
    """A Company with two Branches, a user, a driver assignment and an Open period (0089 shape)."""
    code = "ST" + uuid4().hex[:8]
    cursor.execute("""
        INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended, currencycode)
        VALUES (%s, %s, %s, 'Active', FALSE, 'USD') RETURNING companyid
    """, (code, code, code))
    company_id = cursor.fetchone()[0]
    branches = []
    for index in range(2):
        cursor.execute("""
            INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault, timezoneid)
            VALUES (%s, %s, %s, 'Active', %s, 'UTC') RETURNING branchid
        """, (company_id, f"B{index}", f"Branch {index}", index == 0))
        branches.append(cursor.fetchone()[0])
    cursor.execute("""
        INSERT INTO sec.users (companyid, username, displayname) VALUES (%s, %s, 'st90') RETURNING userid
    """, (company_id, "st_" + uuid4().hex[:10]))
    user_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (%s, %s, 'Open', %s, 'st90 period', 'Week', DATE '2040-01-01', DATE '2040-01-07')
        RETURNING payrollperiodid
    """, (company_id, branches[0], "ST-" + uuid4().hex[:10]))
    period_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO core.employees (companyid, branchid, fullname, employeetype, employmentstatus)
        VALUES (%s, %s, 'St90 Driver', 'Driver', 'Active') RETURNING employeeid
    """, (company_id, branches[0]))
    employee_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO core.drivers (companyid, employeeid, drivernumber, driverstatus)
        VALUES (%s, %s, %s, 'Active') RETURNING driverid
    """, (company_id, employee_id, "D" + uuid4().hex[:8]))
    driver_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO core.driverassignments
            (companyid, branchid, employeeid, driverid, effectivefromat)
        VALUES (%s, %s, %s, %s, TIMESTAMPTZ '2000-01-01 00:00:00+00')
        RETURNING driverassignmentid
    """, (company_id, branches[0], employee_id, driver_id))
    return {"company": company_id, "branches": branches, "user": user_id,
            "period": period_id, "assignment": cursor.fetchone()[0]}


def _seed_branch_status_key(cursor, world: dict) -> int:
    cursor.execute("""
        INSERT INTO payroll.payrollstatuskeys
            (companyid, branchid, statuscode, normalizedstatuscode, keyname)
        VALUES (%s, %s, 'SK_OLD', 'SK_OLD', 'Old Branch Status') RETURNING statuskeyid
    """, (world["company"], world["branches"][0]))
    return cursor.fetchone()[0]


def _seed_blocking_row(cursor, world: dict, kind: str) -> None:
    """One of the rows the Branch-owned model cannot be reinterpreted from. Evidence rows are
    written with foreign keys suspended (a real one needs a whole frozen calculation)."""
    if kind == "status_key":
        _seed_branch_status_key(cursor, world)
    elif kind == "status_selection":
        key_id = _seed_branch_status_key(cursor, world)
        cursor.execute("""
            INSERT INTO payroll.payrollperioddriverdayentrystate
                (companyid, branchid, payrollperiodid, workdate, driverassignmentid,
                 statuskeyid, isvoided, createdbyuserid)
            VALUES (%s, %s, %s, DATE '2040-01-02', %s, %s, FALSE, %s)
        """, (world["company"], world["branches"][0], world["period"],
              world["assignment"], key_id, world["user"]))
    elif kind == "snapshot":
        cursor.execute("""
            INSERT INTO payroll.payrollcalculationsnapshots
                (companyid, branchid, payrollperiodid, revisionnumber, calculationversion,
                 sourceconfighash, snapshothash, createdbyuserid, totalexpectedpay,
                 currencycode, currencyminorunitdigits, periodstartdate, periodenddate,
                 payableroundingversion, minmaxprorationversion, scheduledworkdates, warnings)
            VALUES (%s, %s, %s, 1, 'payroll-calculation-v2', %s, %s, %s, 0, 'USD', 2,
                    DATE '2040-01-01', DATE '2040-01-07', 'payable-rounding-v1',
                    'minmax-proration-v1', '{}', '{}')
        """, (world["company"], world["branches"][0], world["period"],
              "a" * 64, "b" * 64, world["user"]))
    elif kind == "final_line":
        with replica_replication_role_sync(cursor):
            cursor.execute("""
                INSERT INTO payroll.payrollfinallines
                    (companyid, branchid, payrollperiodid, driverassignmentid, linetype, quantity,
                     finalamount, sourcetype, sourceid, approvedatutc, linescope, sourcesnapshot,
                     currencycode, currencyminorunitdigits, driverid, payrollcalculationsnapshotid,
                     payrollcalculationsnapshotcomponentid, employeeid, componentkind)
                VALUES (%s, %s, %s, %s, 'BONUS', 1, 10, 'SnapshotComponent', '1', NOW(), 'Period',
                        '{}', 'USD', 2, 1, 1, 1, 1, 'BONUS')
            """, (world["company"], world["branches"][0], world["period"], world["assignment"]))
    elif kind == "status_payment_line":
        cursor.execute("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverassignmentid, workdate, quantity,
                 sourcetype, sourceid, linetype)
            VALUES (%s, %s, %s, %s, DATE '2040-01-02', 8, 'System', 'STATUS_PAYMENT:1:1:1',
                    'STATUS_PAY')
        """, (world["company"], world["branches"][0], world["period"], world["assignment"]))
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize(
    "kind", ["status_key", "status_selection", "snapshot", "final_line", "status_payment_line"])
def test_0090_refuses_the_branch_owned_status_model_before_any_ddl(pre_0090_database, kind):
    env, dsn = pre_0090_database
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        _seed_blocking_row(cursor, _seed_world(cursor), kind)

    result = _alembic(env, "upgrade", "head")

    assert result.returncode != 0
    assert "STATUS_MODEL_REBUILD_REQUIRED" in result.stderr + result.stdout
    assert _version(dsn) == "0089"
    # The refusal ran before any structural statement.
    assert "branchid" in _columns(dsn, "statusratecolumns")
    assert "payrollstatuskeyaliases" not in _tables(dsn)
    assert "payrollstatuskeybranchlimits" not in _tables(dsn)


def test_0090_replaces_the_branch_default_columns_with_one_default_per_company(pre_0090_database):
    env, dsn = pre_0090_database
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        first = _seed_world(cursor)
        second = _seed_world(cursor)
        # A note-only selection never referenced a Status, so it does not block the cutover.
        cursor.execute("""
            INSERT INTO payroll.payrollperioddriverdayentrystate
                (companyid, branchid, payrollperiodid, workdate, driverassignmentid,
                 notetext, isvoided, createdbyuserid)
            VALUES (%s, %s, %s, DATE '2040-01-02', %s, 'note only', FALSE, %s)
        """, (first["company"], first["branches"][0], first["period"],
              first["assignment"], first["user"]))
        # Each Branch had its own default Status rate column, one of them with a Status-owned
        # rate definition.
        cursor.execute("SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = 'STATUS_PAY'")
        rate_type_id = cursor.fetchone()[0]
        for world in (first, second):
            for branch_id in world["branches"]:
                cursor.execute("""
                    INSERT INTO payroll.statusratecolumns
                        (companyid, branchid, ratetypeid, columnname, normalizedcolumnname, isdefault)
                    VALUES (%s, %s, %s, 'Status Pay', 'STATUS PAY', TRUE) RETURNING statusratecolumnid
                """, (world["company"], branch_id, rate_type_id))
                column_id = cursor.fetchone()[0]
                if world is first and branch_id == first["branches"][0]:
                    first_column_id = column_id
        cursor.execute("""
            INSERT INTO payroll.ratedefinitions (companyid, statusratecolumnid, ownerbranchid, shape)
            VALUES (%s, %s, %s, 'Scalar') RETURNING ratedefinitionid
        """, (first["company"], first_column_id, first["branches"][0]))

    result = _alembic(env, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    assert _version(dsn) == "0092"
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        for world in (first, second):
            cursor.execute("""
                SELECT c.columnname, c.isdefault, c.isactive, rd.shape, rd.structurelockedatutc IS NOT NULL,
                       (SELECT count(*) FROM payroll.ratecomponentdefinitions x
                         WHERE x.ratedefinitionid = rd.ratedefinitionid)
                FROM payroll.statusratecolumns c
                JOIN payroll.ratedefinitions rd ON rd.statusratecolumnid = c.statusratecolumnid
                WHERE c.companyid = %s
            """, (world["company"],))
            assert cursor.fetchall() == [("Status Pay", True, True, "Scalar", True, 1)]


def test_0090_is_irreversible(head_database):
    env, _ = head_database
    result = _alembic(env, "downgrade", "0089")
    assert result.returncode != 0
    assert "irreversible" in (result.stderr + result.stdout)


def test_fresh_upgrade_reaches_the_company_status_catalog_shape(head_database):
    _, dsn = head_database
    assert _version(dsn) == "0092"
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
