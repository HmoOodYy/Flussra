"""Migration 0087 and the database-level durable Driver contract.

A permanent ``core.Drivers`` row is the person-driver identity; every branch
tenure is a ``core.DriverAssignments`` row with a half-open instant window.
These tests exercise the schema only, directly against a migrated database.
"""
from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import errors, sql

_ROOT = Path(__file__).resolve().parents[2]

PAST_START = "2020-01-01 00:00+00"
PAST_MID = "2020-06-01 00:00+00"
PAST_END = "2020-09-01 00:00+00"
FUTURE_START = "2099-01-01 00:00+00"
FUTURE_LATER = "2099-02-01 00:00+00"


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
    env["SECRET_KEY"] = "durable-driver-disposable-migration-test"
    try:
        yield env, {**dsn, "database": name}
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def pre_0087_database(pg_instance):
    """A disposable database upgraded to 0086, the last profile-based schema."""
    with _disposable_database(pg_instance, "dd87pre") as (env, dsn):
        assert _alembic(env, "upgrade", "0086").returncode == 0
        yield env, dsn


@pytest.fixture(scope="module")
def head_database(pg_instance):
    with _disposable_database(pg_instance, "dd87head") as (env, dsn):
        result = _alembic(env, "upgrade", "head")
        assert result.returncode == 0, result.stderr
        yield env, dsn


@pytest.fixture
def cur(head_database):
    """A cursor in a transaction that is always rolled back."""
    _, dsn = head_database
    conn = psycopg2.connect(**dsn)
    try:
        with conn.cursor() as cursor:
            yield cursor
    finally:
        conn.rollback()
        conn.close()


@contextmanager
def rejected(cursor, exc_type):
    """Expect the statements inside to fail, and keep the transaction usable."""
    cursor.execute("SAVEPOINT expect_failure")
    with pytest.raises(exc_type) as info:
        yield info
    cursor.execute("ROLLBACK TO SAVEPOINT expect_failure")


def _set_op(cursor, op: str) -> None:
    cursor.execute("SELECT set_config('flussra.workforce_op', %s, TRUE)", (op,))


def _clear_op(cursor) -> None:
    cursor.execute("SELECT set_config('flussra.workforce_op', '', TRUE)")


# ---------------------------------------------------------------------------
# Seed helpers (head schema)
# ---------------------------------------------------------------------------

def _company(cursor, *, branches: int = 2) -> tuple[int, list[int]]:
    code = "DD" + uuid4().hex[:8]
    cursor.execute("""
        INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended, currencycode)
        VALUES (%s, %s, %s, 'Active', FALSE, 'USD') RETURNING companyid
    """, (code, code, code))
    company_id = cursor.fetchone()[0]
    branch_ids = []
    for index in range(branches):
        cursor.execute("""
            INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault, timezoneid)
            VALUES (%s, %s, %s, 'Active', %s, 'UTC') RETURNING branchid
        """, (company_id, f"B{index}", f"Branch {index}", index == 0))
        branch_ids.append(cursor.fetchone()[0])
    return company_id, branch_ids


def _employee(cursor, company_id: int, branch_id: int) -> int:
    cursor.execute("""
        INSERT INTO core.employees (companyid, branchid, fullname, employeetype, employmentstatus)
        VALUES (%s, %s, 'Durable Driver Test', 'Driver', 'Active') RETURNING employeeid
    """, (company_id, branch_id))
    return cursor.fetchone()[0]


def _driver(cursor, company_id: int, employee_id: int, *, number: str | None = None,
            status: str = "Active") -> int:
    cursor.execute("""
        INSERT INTO core.drivers (companyid, employeeid, drivernumber, driverstatus)
        VALUES (%s, %s, %s, %s) RETURNING driverid
    """, (company_id, employee_id, number, status))
    return cursor.fetchone()[0]


def _assignment(cursor, company_id: int, branch_id: int, employee_id: int, driver_id: int,
                start: str, end: str | None = None, reason: str | None = None) -> int:
    cursor.execute("""
        INSERT INTO core.driverassignments
            (companyid, branchid, employeeid, driverid, effectivefromat, effectivetoat, endreason)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING driverassignmentid
    """, (company_id, branch_id, employee_id, driver_id, start, end, reason))
    return cursor.fetchone()[0]


def _person(cursor, *, branches: int = 2) -> dict:
    """A company with one Employee that has a Driver, and the branches to place it in."""
    company_id, branch_ids = _company(cursor, branches=branches)
    employee_id = _employee(cursor, company_id, branch_ids[0])
    driver_id = _driver(cursor, company_id, employee_id)
    return {"company": company_id, "branches": branch_ids,
            "employee": employee_id, "driver": driver_id}


def _settle_exclusion(cursor) -> None:
    """Run the deferred exclusion check now, then restore the deferred default."""
    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    cursor.execute("SET CONSTRAINTS ALL DEFERRED")


# ---------------------------------------------------------------------------
# Preflight and fresh upgrade
# ---------------------------------------------------------------------------

def _seed_0086_company(cursor, code: str) -> tuple[int, int]:
    cursor.execute("""
        INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended, currencycode)
        VALUES (%s, %s, %s, 'Active', FALSE, 'USD') RETURNING companyid
    """, (code, code, code))
    company_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault, timezoneid)
        VALUES (%s, %s, %s, 'Active', TRUE, 'UTC') RETURNING branchid
    """, (company_id, code, code))
    return company_id, cursor.fetchone()[0]


@pytest.mark.parametrize("dirty_table", ["driver", "transfer", "period"])
def test_0087_refuses_existing_driver_transfer_or_payroll_rows_atomically(
    pre_0087_database, dirty_table,
):
    env, dsn = pre_0087_database
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        company_id, branch_id = _seed_0086_company(cursor, "DD87" + dirty_table[:3].upper())
        if dirty_table == "period":
            cursor.execute("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (%s, %s, 'Open', 'P87', 'dd87', 'Week', '2030-01-07', '2030-01-13')
            """, (company_id, branch_id))
        else:
            cursor.execute("""
                INSERT INTO core.employees (companyid, branchid, fullname, employeetype, employmentstatus)
                VALUES (%s, %s, 'DD87 Driver', 'Driver', 'Active') RETURNING employeeid
            """, (company_id, branch_id))
            employee_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO core.drivers (companyid, branchid, employeeid, drivercode, driverstatus)
                VALUES (%s, %s, %s, 'D87', 'Active') RETURNING driverid
            """, (company_id, branch_id, employee_id))
            driver_id = cursor.fetchone()[0]
            if dirty_table == "transfer":
                cursor.execute("""
                    INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault, timezoneid)
                    VALUES (%s, 'DD87T', 'target', 'Active', FALSE, 'UTC') RETURNING branchid
                """, (company_id,))
                target_branch_id = cursor.fetchone()[0]
                cursor.execute("""
                    INSERT INTO sec.users (companyid, username, displayname)
                    VALUES (%s, %s, 'dd87') RETURNING userid
                """, (company_id, "dd87_" + uuid4().hex[:8]))
                user_id = cursor.fetchone()[0]
                cursor.execute("""
                    INSERT INTO core.drivertransferrequests
                        (companyid, driverid, sourcebranchid, targetbranchid,
                         requestedbyuserid, initiatedby, status)
                    VALUES (%s, %s, %s, %s, %s, 'SourceBranch', 'PendingSourceApproval')
                """, (company_id, driver_id, branch_id, target_branch_id, user_id))

    result = _alembic(env, "upgrade", "head")
    assert result.returncode != 0
    assert "DURABLE_DRIVER_REBUILD_REQUIRED" in (result.stderr + result.stdout)
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT version_num FROM public.alembic_version")
        assert cursor.fetchone() == ("0086",)
        cursor.execute("SELECT to_regclass('core.driverassignments')")
        assert cursor.fetchone()[0] is None
        cursor.execute("SELECT to_regclass('core.driveraliases')")
        assert cursor.fetchone()[0] == "core.driveraliases"
        cursor.execute("""
            SELECT count(*) FROM information_schema.columns
            WHERE table_schema = 'core' AND table_name = 'drivers' AND column_name = 'drivercode'
        """)
        assert cursor.fetchone() == (1,)


def test_0087_applies_over_clean_rosters_without_drivers(pre_0087_database):
    env, dsn = pre_0087_database
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        company_id, branch_id = _seed_0086_company(cursor, "DD87CLEAN")
        cursor.execute("""
            INSERT INTO core.employees (companyid, branchid, fullname, employeetype, employmentstatus)
            VALUES (%s, %s, 'Office Staff', 'OfficeStaff', 'Active')
        """, (company_id, branch_id))
    assert _alembic(env, "upgrade", "head").returncode == 0
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT version_num FROM public.alembic_version")
        assert cursor.fetchone() == ("0087",)
        cursor.execute("SELECT count(*) FROM core.employees WHERE companyid = %s", (company_id,))
        assert cursor.fetchone() == (1,)


def test_0087_is_irreversible(head_database):
    env, _ = head_database
    result = _alembic(env, "downgrade", "0086")
    assert result.returncode != 0
    assert "irreversible" in (result.stderr + result.stdout)


def test_fresh_upgrade_reaches_0087_with_the_durable_driver_shape(head_database, cur):
    cur.execute("SELECT version_num FROM public.alembic_version")
    assert cur.fetchone() == ("0087",)

    def columns(table: str, schema: str = "core") -> set[str]:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
        """, (schema, table))
        return {row[0] for row in cur.fetchall()}

    assert columns("drivers") == {
        "driverid", "companyid", "employeeid", "drivernumber", "cdlnumber",
        "driverstatus", "createdatutc", "updatedatutc",
    }
    assigned = columns("driverassignments")
    assert {"driverassignmentid", "companyid", "branchid", "driverid", "employeeid",
            "effectivefromat", "effectivetoat", "endreason",
            "transferredfromassignmentid", "transferredtoassignmentid"} <= assigned
    assert not assigned & {"drivercode", "cdlnumber", "externaldriverid", "driverstatus",
                           "transferredfromdriverid", "transferredtodriverid"}
    dtr = columns("drivertransferrequests")
    assert {"driverid", "sourcedriverassignmentid", "destinationdriverassignmentid",
            "scheduledatutc", "scheduledbyuserid", "effectiveat"} <= dtr
    assert "newdriverid" not in dtr
    eligibility = columns("payrollperioddrivereligibility", "payroll")
    assert {"driverassignmentid", "drivernumbersnapshot", "driverstatussnapshot",
            "endreasonsnapshot", "transferredfromassignmentidsnapshot",
            "transferredtoassignmentidsnapshot"} <= eligibility
    assert not eligibility & {"driverid", "drivercodesnapshot",
                              "transferredfromdriveridsnapshot", "transferredtodriveridsnapshot"}

    cur.execute("SELECT to_regclass('core.driveraliases')")
    assert cur.fetchone()[0] is None
    for view in ("vw_activedrivers", "vw_homesummary", "vw_payrolldraftsummary"):
        cur.execute("SELECT to_regclass(%s)", (f"app.{view}",))
        assert cur.fetchone()[0] is None
    cur.execute("SELECT to_regclass('app.vw_payrollperiodlist')")
    assert cur.fetchone()[0] is not None
    assert {"draftdrivers", "finaldrivercount", "finalgross"} <= columns("vw_payrollperiodlist", "app")
    for retired in ("core.fn_effectivedriverprofile(integer,integer,timestamptz)",
                    "core.fn_drivercoversworkdate(integer,integer,integer,date)"):
        cur.execute("SELECT to_regprocedure(%s)", (retired,))
        assert cur.fetchone()[0] is None
    for current in ("core.fn_effectivedriverassignment(integer,integer,timestamptz)",
                    "core.fn_driverassignmentcoversworkdate(integer,integer,integer,date)",
                    "core.fn_drivertransferstatus(text,timestamptz,timestamptz)"):
        cur.execute("SELECT to_regprocedure(%s)", (current,))
        assert cur.fetchone()[0] is not None


def test_driver_reference_columns_are_classified_with_no_unvalidated_driver_foreign_keys(cur):
    # Assignment-classified tables carry DriverAssignmentID and never a bare DriverID.
    assignment_tables = [
        "payrolldraftlines", "payrollperioddriverdayentrystate", "payrollperioddrivereligibility",
        "payrollbonusevents", "payrollfinallines", "payrollcalculationdrivertotals",
        "payrollcalculationsnapshotbonusevents", "payrollcalculationsnapshotstatusentries",
        "payrollcalculationsnapshotusedratedefinitions", "payrollperiodauditevidenceevents",
        "driverrates", "driverrateassignments", "driverpayrules",
    ]
    with_driver_id = {
        "payrollfinallines", "payrollcalculationdrivertotals",
        "payrollcalculationsnapshotbonusevents", "payrollcalculationsnapshotstatusentries",
        "payrollcalculationsnapshotusedratedefinitions", "payrollperiodauditevidenceevents",
    }
    for table in assignment_tables:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'payroll' AND table_name = %s
              AND column_name IN ('driverid', 'driverassignmentid')
        """, (table,))
        found = {row[0] for row in cur.fetchall()}
        expected = {"driverassignmentid"} | ({"driverid"} if table in with_driver_id else set())
        assert found == expected, table
    cur.execute("""
        SELECT conrelid::regclass::TEXT, conname FROM pg_constraint
        WHERE contype = 'f' AND NOT convalidated
          AND confrelid IN ('core.drivers'::regclass, 'core.driverassignments'::regclass)
    """)
    assert cur.fetchall() == []
    cur.execute("""
        SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
        WHERE contype = 'f' AND confrelid = 'core.drivers'::regclass
          AND conrelid = 'import.importrows'::regclass
    """)
    rows = cur.fetchall()
    assert len(rows) == 1 and "(matcheddriverid)" in rows[0][1] and "core.drivers(driverid)" in rows[0][1]


def test_no_function_or_view_still_reads_the_profile_meaning_of_driverid(cur):
    cur.execute(r"""
        SELECT n.nspname || '.' || p.proname
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND p.proname NOT IN ('fn_effectivedriverassignment', 'trg_driverassignments_identityimmutable')
          AND p.prosrc ~* '(\mdriverid\M|driver_id|fn_effectivedriverprofile|fn_drivercoversworkdate|drivercode)'
    """)
    assert cur.fetchall() == []
    cur.execute(r"""
        SELECT schemaname || '.' || viewname FROM pg_views
        WHERE schemaname NOT IN ('pg_catalog', 'information_schema') AND definition ~* '\mdriverid\M'
    """)
    assert cur.fetchall() == []


# ---------------------------------------------------------------------------
# core.Drivers
# ---------------------------------------------------------------------------

def test_an_employee_has_at_most_one_driver(cur):
    person = _person(cur)
    with rejected(cur, errors.UniqueViolation):
        _driver(cur, person["company"], person["employee"])


def test_driver_number_is_unique_per_company_when_present(cur):
    company_id, branches = _company(cur)
    first = _employee(cur, company_id, branches[0])
    second = _employee(cur, company_id, branches[0])
    third = _employee(cur, company_id, branches[0])
    _driver(cur, company_id, first, number="D-100")
    with rejected(cur, errors.UniqueViolation):
        _driver(cur, company_id, second, number="D-100")
    _driver(cur, company_id, second, number=None)
    _driver(cur, company_id, third, number=None)
    other_company, other_branches = _company(cur)
    other_employee = _employee(cur, other_company, other_branches[0])
    _driver(cur, other_company, other_employee, number="D-100")


def test_driver_status_is_a_work_state_not_an_assignment_reason(cur):
    company_id, branches = _company(cur)
    for index, status in enumerate(("Active", "Inactive", "OnLeave")):
        _driver(cur, company_id, _employee(cur, company_id, branches[0]), status=status)
    for bad in ("Transferred", "Terminated"):
        with rejected(cur, errors.CheckViolation):
            _driver(cur, company_id, _employee(cur, company_id, branches[0]), status=bad)


def test_a_driver_must_belong_to_its_employees_company(cur):
    company_id, branches = _company(cur)
    other_company, _ = _company(cur)
    employee_id = _employee(cur, company_id, branches[0])
    with rejected(cur, errors.ForeignKeyViolation):
        _driver(cur, other_company, employee_id)


def test_driver_company_and_employee_are_immutable_but_number_and_cdl_edit(cur):
    person = _person(cur)
    other_employee = _employee(cur, person["company"], person["branches"][0])
    with rejected(cur, errors.RaiseException):
        cur.execute("UPDATE core.drivers SET employeeid = %s WHERE driverid = %s",
                    (other_employee, person["driver"]))
    other_company, _ = _company(cur)
    with rejected(cur, errors.RaiseException):
        cur.execute("UPDATE core.drivers SET companyid = %s WHERE driverid = %s",
                    (other_company, person["driver"]))
    cur.execute("""
        UPDATE core.drivers SET drivernumber = 'D-7', cdlnumber = 'CDL-7', driverstatus = 'OnLeave'
        WHERE driverid = %s RETURNING drivernumber, cdlnumber, driverstatus
    """, (person["driver"],))
    assert cur.fetchone() == ("D-7", "CDL-7", "OnLeave")


# ---------------------------------------------------------------------------
# Assignment windows
# ---------------------------------------------------------------------------

def test_assignment_windows_of_one_driver_cannot_overlap_but_adjacent_half_open_windows_can(cur):
    p = _person(cur)
    company, branch_a, branch_b = p["company"], p["branches"][0], p["branches"][1]
    _assignment(cur, company, branch_a, p["employee"], p["driver"], PAST_START, PAST_MID, "Transferred")
    # Adjacent: the next window starts exactly where the previous one ends.
    _assignment(cur, company, branch_b, p["employee"], p["driver"], PAST_MID)
    _settle_exclusion(cur)
    with rejected(cur, errors.ExclusionViolation):
        _assignment(cur, company, branch_a, p["employee"], p["driver"], PAST_START, "2020-03-01 00:00+00", "Transferred")
        _settle_exclusion(cur)


def test_overlap_is_checked_per_driver_not_per_branch_or_employee_text(cur):
    first = _person(cur)
    second_employee = _employee(cur, first["company"], first["branches"][0])
    second_driver = _driver(cur, first["company"], second_employee)
    _assignment(cur, first["company"], first["branches"][0], first["employee"], first["driver"], PAST_START)
    _assignment(cur, first["company"], first["branches"][0], second_employee, second_driver, PAST_START)
    _settle_exclusion(cur)


def test_an_open_assignment_blocks_any_later_overlapping_window(cur):
    p = _person(cur)
    _assignment(cur, p["company"], p["branches"][0], p["employee"], p["driver"], PAST_START)
    with rejected(cur, errors.ExclusionViolation):
        _assignment(cur, p["company"], p["branches"][1], p["employee"], p["driver"], PAST_MID)
        _settle_exclusion(cur)


@pytest.mark.parametrize("end, reason", [(PAST_MID, None), (None, "Transferred"), (None, "Terminated")])
def test_end_reason_and_effective_to_are_set_together(cur, end, reason):
    p = _person(cur)
    with rejected(cur, errors.CheckViolation):
        _assignment(cur, p["company"], p["branches"][0], p["employee"], p["driver"], PAST_START, end, reason)


def test_end_reason_is_a_closed_set(cur):
    p = _person(cur)
    with rejected(cur, errors.CheckViolation):
        _assignment(cur, p["company"], p["branches"][0], p["employee"], p["driver"],
                    PAST_START, PAST_MID, "Resigned")


def test_an_empty_window_is_allowed_only_when_terminated(cur):
    p = _person(cur)
    with rejected(cur, errors.CheckViolation):
        _assignment(cur, p["company"], p["branches"][0], p["employee"], p["driver"],
                    FUTURE_START, FUTURE_START, "Transferred")
    with rejected(cur, errors.CheckViolation):
        _assignment(cur, p["company"], p["branches"][0], p["employee"], p["driver"],
                    PAST_MID, PAST_START, "Terminated")
    _assignment(cur, p["company"], p["branches"][0], p["employee"], p["driver"],
                FUTURE_START, FUTURE_START, "Terminated")
    _settle_exclusion(cur)


def test_an_assignment_ties_driver_employee_and_company_together(cur):
    p = _person(cur)
    other_employee = _employee(cur, p["company"], p["branches"][0])
    with rejected(cur, errors.ForeignKeyViolation):
        _assignment(cur, p["company"], p["branches"][0], other_employee, p["driver"], PAST_START)
    other_company, other_branches = _company(cur)
    with rejected(cur, errors.ForeignKeyViolation):
        _assignment(cur, other_company, other_branches[0], p["employee"], p["driver"], PAST_START)
    with rejected(cur, errors.ForeignKeyViolation):
        _assignment(cur, p["company"], other_branches[0], p["employee"], p["driver"], PAST_START)


def test_lineage_cannot_cross_drivers(cur):
    first = _person(cur)
    source = _assignment(cur, first["company"], first["branches"][0], first["employee"], first["driver"],
                         PAST_START, PAST_MID, "Transferred")
    other_employee = _employee(cur, first["company"], first["branches"][0])
    other_driver = _driver(cur, first["company"], other_employee)
    with rejected(cur, errors.ForeignKeyViolation):
        cur.execute("""
            INSERT INTO core.driverassignments
                (companyid, branchid, employeeid, driverid, effectivefromat, transferredfromassignmentid)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (first["company"], first["branches"][1], other_employee, other_driver, PAST_MID, source))
    cur.execute("""
        INSERT INTO core.driverassignments
            (companyid, branchid, employeeid, driverid, effectivefromat, transferredfromassignmentid)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING driverassignmentid
    """, (first["company"], first["branches"][1], first["employee"], first["driver"], PAST_MID, source))
    assert cur.fetchone()[0] is not None


def test_assignment_identity_columns_are_immutable(cur):
    p = _person(cur)
    assignment = _assignment(cur, p["company"], p["branches"][0], p["employee"], p["driver"], PAST_START)
    other_employee = _employee(cur, p["company"], p["branches"][0])
    other_driver = _driver(cur, p["company"], other_employee)
    other_company, _ = _company(cur)
    for column, value in (("branchid", p["branches"][1]), ("driverid", other_driver),
                          ("employeeid", other_employee), ("companyid", other_company)):
        with rejected(cur, errors.RaiseException):
            cur.execute(
                sql.SQL("UPDATE core.driverassignments SET {} = %s WHERE driverassignmentid = %s")
                .format(sql.Identifier(column)),
                (value, assignment),
            )


# ---------------------------------------------------------------------------
# History protection
# ---------------------------------------------------------------------------

def test_an_ended_assignment_window_reason_and_lineage_are_immutable(cur):
    p = _person(cur)
    ended = _assignment(cur, p["company"], p["branches"][0], p["employee"], p["driver"],
                        PAST_START, PAST_MID, "Transferred")
    successor = _assignment(cur, p["company"], p["branches"][1], p["employee"], p["driver"], PAST_MID)
    changes = [
        "effectivetoat = '2020-07-01 00:00+00'",
        "effectivefromat = '2019-12-01 00:00+00'",
        "endreason = 'Terminated'",
        f"transferredtoassignmentid = {successor}",
        "effectivetoat = NULL, endreason = NULL",
    ]
    for op in ("", "transfer_cancel", "transfer_reschedule", "terminate"):
        for change in changes:
            _set_op(cur, op) if op else _clear_op(cur)
            with rejected(cur, errors.RaiseException):
                cur.execute(sql.SQL("UPDATE core.driverassignments SET {} WHERE driverassignmentid = %s")
                            .format(sql.SQL(change)), (ended,))


def test_noop_updates_of_historical_columns_are_allowed(cur):
    p = _person(cur)
    ended = _assignment(cur, p["company"], p["branches"][0], p["employee"], p["driver"],
                        PAST_START, PAST_MID, "Transferred")
    cur.execute("""
        UPDATE core.driverassignments SET effectivetoat = effectivetoat, endreason = endreason,
               updatedatutc = NOW()
        WHERE driverassignmentid = %s
    """, (ended,))
    assert cur.rowcount == 1


def test_termination_may_cap_a_transferred_assignment_only_at_the_branch_local_cutoff(cur):
    p = _person(cur)
    source = _assignment(cur, p["company"], p["branches"][0], p["employee"], p["driver"],
                         PAST_START, PAST_END, "Transferred")
    destination = _assignment(cur, p["company"], p["branches"][1], p["employee"], p["driver"], PAST_END)
    cur.execute("""
        UPDATE core.employees SET employmentstatus = 'Terminated', terminationdate = DATE '2020-06-10'
        WHERE employeeid = %s
    """, (p["employee"],))

    update = """
        UPDATE core.driverassignments SET effectivetoat = %s, endreason = 'Terminated'
        WHERE driverassignmentid = %s
    """
    _clear_op(cur)
    with rejected(cur, errors.RaiseException):
        cur.execute(update, ("2020-06-11 00:00+00", source))
    _set_op(cur, "terminate")
    with rejected(cur, errors.RaiseException):
        cur.execute(update, ("2020-06-12 00:00+00", source))
    cur.execute(update, ("2020-06-11 00:00+00", source))
    assert cur.rowcount == 1
    # The destination started after the cutoff and is closed empty.
    cur.execute("""
        UPDATE core.driverassignments SET effectivetoat = effectivefromat, endreason = 'Terminated'
        WHERE driverassignmentid = %s
    """, (destination,))
    assert cur.rowcount == 1
    _settle_exclusion(cur)


def test_a_future_boundary_changes_only_under_a_named_workforce_operation(cur):
    p = _person(cur)
    source = _assignment(cur, p["company"], p["branches"][0], p["employee"], p["driver"],
                         PAST_START, FUTURE_START, "Transferred")
    destination = _assignment(cur, p["company"], p["branches"][1], p["employee"], p["driver"], FUTURE_START)
    move_source = "UPDATE core.driverassignments SET effectivetoat = %s WHERE driverassignmentid = %s"
    move_destination = "UPDATE core.driverassignments SET effectivefromat = %s WHERE driverassignmentid = %s"

    _clear_op(cur)
    with rejected(cur, errors.RaiseException):
        cur.execute(move_source, (FUTURE_LATER, source))
    with rejected(cur, errors.RaiseException):
        cur.execute(move_destination, (FUTURE_LATER, destination))
    _set_op(cur, "some_other_operation")
    with rejected(cur, errors.RaiseException):
        cur.execute(move_source, (FUTURE_LATER, source))

    _set_op(cur, "transfer_reschedule")
    cur.execute(move_source, (FUTURE_LATER, source))
    cur.execute(move_destination, (FUTURE_LATER, destination))
    _settle_exclusion(cur)
    cur.execute("SELECT effectivetoat = %s::timestamptz FROM core.driverassignments "
                "WHERE driverassignmentid = %s", (FUTURE_LATER, source))
    assert cur.fetchone() == (True,)
    cur.execute("SELECT effectivefromat = %s::timestamptz FROM core.driverassignments "
                "WHERE driverassignmentid = %s", (FUTURE_LATER, destination))
    assert cur.fetchone() == (True,)


def test_closing_an_open_effective_assignment_needs_no_operation(cur):
    p = _person(cur)
    source = _assignment(cur, p["company"], p["branches"][0], p["employee"], p["driver"], PAST_START)
    _clear_op(cur)
    cur.execute("""
        UPDATE core.driverassignments SET effectivetoat = %s, endreason = 'Transferred'
        WHERE driverassignmentid = %s
    """, (FUTURE_START, source))
    assert cur.rowcount == 1


def test_only_transfer_cancel_deletes_and_only_a_never_effective_assignment(cur):
    p = _person(cur)
    source = _assignment(cur, p["company"], p["branches"][0], p["employee"], p["driver"],
                         PAST_START, FUTURE_START, "Transferred")
    destination = _assignment(cur, p["company"], p["branches"][1], p["employee"], p["driver"], FUTURE_START)
    delete = "DELETE FROM core.driverassignments WHERE driverassignmentid = %s"

    for op in ("", "transfer_reschedule", "terminate"):
        _set_op(cur, op) if op else _clear_op(cur)
        with rejected(cur, errors.RaiseException):
            cur.execute(delete, (destination,))

    _set_op(cur, "transfer_cancel")
    with rejected(cur, errors.RaiseException):
        cur.execute(delete, (source,))  # already effective: history, never deletable
    cur.execute(delete, (destination,))
    assert cur.rowcount == 1


def test_a_referenced_destination_cannot_be_deleted_until_the_source_lineage_is_cleared(cur):
    p = _person(cur)
    source = _assignment(cur, p["company"], p["branches"][0], p["employee"], p["driver"], PAST_START)
    cur.execute("""
        INSERT INTO core.driverassignments
            (companyid, branchid, employeeid, driverid, effectivefromat, transferredfromassignmentid)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING driverassignmentid
    """, (p["company"], p["branches"][1], p["employee"], p["driver"], FUTURE_START, source))
    destination = cur.fetchone()[0]
    cur.execute("""
        UPDATE core.driverassignments
        SET effectivetoat = %s, endreason = 'Transferred', transferredtoassignmentid = %s
        WHERE driverassignmentid = %s
    """, (FUTURE_START, destination, source))
    _settle_exclusion(cur)

    _set_op(cur, "transfer_cancel")
    with rejected(cur, errors.ForeignKeyViolation):
        cur.execute("DELETE FROM core.driverassignments WHERE driverassignmentid = %s", (destination,))
    # Cancel: reopen the source first, then remove the never-effective destination.
    cur.execute("""
        UPDATE core.driverassignments
        SET effectivetoat = NULL, endreason = NULL, transferredtoassignmentid = NULL
        WHERE driverassignmentid = %s
    """, (source,))
    cur.execute("DELETE FROM core.driverassignments WHERE driverassignmentid = %s", (destination,))
    assert cur.rowcount == 1
    _settle_exclusion(cur)


def test_a_driver_with_assignments_cannot_be_deleted(cur):
    p = _person(cur)
    _assignment(cur, p["company"], p["branches"][0], p["employee"], p["driver"], PAST_START)
    with rejected(cur, errors.ForeignKeyViolation):
        cur.execute("DELETE FROM core.drivers WHERE driverid = %s", (p["driver"],))


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------

def test_effective_driver_assignment_switches_exactly_at_the_boundary_instant(cur):
    p = _person(cur)
    first = _assignment(cur, p["company"], p["branches"][0], p["employee"], p["driver"],
                        PAST_START, PAST_MID, "Transferred")
    second = _assignment(cur, p["company"], p["branches"][1], p["employee"], p["driver"], PAST_MID)
    _settle_exclusion(cur)

    def effective(at: str | None, company: int | None = None):
        cur.execute("SELECT core.fn_EffectiveDriverAssignment(%s, %s, %s::timestamptz)",
                    (company or p["company"], p["driver"], at))
        return cur.fetchone()[0]

    assert effective("2019-12-31 23:59:59.999999+00") is None
    assert effective(PAST_START) == first
    assert effective("2020-05-31 23:59:59.999999+00") == first
    assert effective(PAST_MID) == second
    assert effective("2030-01-01 00:00+00") == second
    other_company, _ = _company(cur)
    assert effective("2030-01-01 00:00+00", other_company) is None
    cur.execute("SELECT core.fn_EffectiveDriverAssignment(%s, %s)", (p["company"], p["driver"]))
    assert cur.fetchone()[0] == second
    with rejected(cur, errors.RaiseException):
        cur.execute("SELECT core.fn_EffectiveDriverAssignment(%s, %s, NULL)", (p["company"], p["driver"]))


def test_an_empty_terminated_assignment_is_never_effective(cur):
    p = _person(cur)
    _assignment(cur, p["company"], p["branches"][0], p["employee"], p["driver"],
                PAST_START, PAST_START, "Terminated")
    _settle_exclusion(cur)
    cur.execute("SELECT core.fn_EffectiveDriverAssignment(%s, %s, %s::timestamptz)",
                (p["company"], p["driver"], PAST_START))
    assert cur.fetchone()[0] is None


def test_assignment_covers_a_work_date_by_branch_local_day_overlap(cur):
    p = _person(cur)
    first = _assignment(cur, p["company"], p["branches"][0], p["employee"], p["driver"],
                        PAST_START, "2020-06-01 12:00+00", "Transferred")
    second = _assignment(cur, p["company"], p["branches"][1], p["employee"], p["driver"], "2020-06-01 12:00+00")
    _settle_exclusion(cur)

    def covers(assignment: int, branch: int, work_date: str) -> bool:
        cur.execute("SELECT core.fn_DriverAssignmentCoversWorkDate(%s, %s, %s, %s::date)",
                    (p["company"], assignment, branch, work_date))
        return cur.fetchone()[0]

    # A midday transfer leaves both assignments covering the transfer day in their own Branch.
    assert covers(first, p["branches"][0], "2020-06-01")
    assert covers(second, p["branches"][1], "2020-06-01")
    assert not covers(first, p["branches"][0], "2020-06-02")
    assert not covers(second, p["branches"][1], "2020-05-31")
    assert not covers(first, p["branches"][1], "2020-06-01")  # wrong Branch for that assignment


def test_transfer_status_reads_scheduled_as_completed_from_its_effective_instant(cur):
    effective = "2099-03-01 09:30:00+00"

    def status(stored: str, at: str | None, effective_at: str | None = effective) -> str:
        cur.execute("SELECT core.fn_DriverTransferStatus(%s, %s::timestamptz, %s::timestamptz)",
                    (stored, effective_at, at))
        return cur.fetchone()[0]

    assert status("Scheduled", "2099-03-01 09:29:59.999999+00") == "Scheduled"
    assert status("Scheduled", effective) == "Completed"
    assert status("Scheduled", "2100-01-01 00:00+00") == "Completed"
    assert status("Scheduled", effective, None) == "Scheduled"
    for stored in ("PendingSourceApproval", "PendingTargetApproval", "Returned", "Rejected",
                   "Approved", "Completed", "Cancelled"):
        assert status(stored, "2100-01-01 00:00+00") == stored
    cur.execute("SELECT core.fn_DriverTransferStatus('Scheduled', NOW() - INTERVAL '1 second')")
    assert cur.fetchone()[0] == "Completed"
    cur.execute("SELECT core.fn_DriverTransferStatus('Scheduled', NOW() + INTERVAL '1 day')")
    assert cur.fetchone()[0] == "Scheduled"


# ---------------------------------------------------------------------------
# Transfer requests
# ---------------------------------------------------------------------------

def _request_world(cursor) -> dict:
    p = _person(cursor, branches=3)
    source_branch, target_branch, third_branch = p["branches"]
    source = _assignment(cursor, p["company"], source_branch, p["employee"], p["driver"], PAST_START)
    other_employee = _employee(cursor, p["company"], source_branch)
    other_driver = _driver(cursor, p["company"], other_employee)
    other_assignment = _assignment(cursor, p["company"], source_branch, other_employee, other_driver, PAST_START)
    cursor.execute("""
        INSERT INTO sec.users (companyid, username, displayname) VALUES (%s, %s, 'dd87') RETURNING userid
    """, (p["company"], "dd_" + uuid4().hex[:10]))
    user = cursor.fetchone()[0]
    return {**p, "source_branch": source_branch, "target_branch": target_branch,
            "third_branch": third_branch, "source": source, "other_driver": other_driver,
            "other_assignment": other_assignment, "user": user}


def _request(cursor, w: dict, *, status="PendingSourceApproval", driver=None, source=None,
             source_branch=None, destination=None, target_branch=None, effective_at=None,
             scheduled_at=None) -> int:
    cursor.execute("""
        INSERT INTO core.drivertransferrequests
            (companyid, driverid, sourcedriverassignmentid, sourcebranchid, targetbranchid,
             requestedbyuserid, initiatedby, status, destinationdriverassignmentid, effectiveat,
             scheduledatutc, scheduledbyuserid)
        VALUES (%s, %s, %s, %s, %s, %s, 'SourceBranch', %s, %s, %s, %s, %s) RETURNING transferrequestid
    """, (w["company"], driver or w["driver"], source or w["source"],
          source_branch or w["source_branch"], target_branch or w["target_branch"], w["user"],
          status, destination, effective_at, scheduled_at,
          w["user"] if scheduled_at else None))
    return cursor.fetchone()[0]


def test_a_transfer_request_names_the_driver_and_its_source_assignment_consistently(cur):
    w = _request_world(cur)
    assert _request(cur, w) is not None
    with rejected(cur, errors.ForeignKeyViolation):
        _request(cur, w, source=w["other_assignment"])  # another Driver's assignment
    with rejected(cur, errors.ForeignKeyViolation):
        _request(cur, w, driver=w["other_driver"])  # assignment does not belong to that Driver
    with rejected(cur, errors.ForeignKeyViolation):
        _request(cur, w, source_branch=w["third_branch"])  # not the source assignment's Branch
    other_company, _ = _company(cur)
    with rejected(cur, errors.ForeignKeyViolation):
        cur.execute("""
            INSERT INTO core.drivertransferrequests
                (companyid, driverid, sourcedriverassignmentid, sourcebranchid, targetbranchid,
                 requestedbyuserid, initiatedby, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'SourceBranch', 'PendingSourceApproval')
        """, (other_company, w["driver"], w["source"], w["source_branch"], w["target_branch"], w["user"]))


def test_a_transfer_destination_must_be_the_same_drivers_assignment_in_the_target_branch(cur):
    w = _request_world(cur)
    destination = _assignment(cur, w["company"], w["target_branch"], w["employee"], w["driver"], FUTURE_START)
    cur.execute("UPDATE core.driverassignments SET effectivetoat = %s, endreason = 'Transferred' "
                "WHERE driverassignmentid = %s", (FUTURE_START, w["source"]))
    assert _request(cur, w, status="Approved", destination=destination) is not None
    with rejected(cur, errors.ForeignKeyViolation):
        _request(cur, w, status="Approved", destination=w["other_assignment"])
    with rejected(cur, errors.ForeignKeyViolation):
        _request(cur, w, status="Approved", destination=destination, target_branch=w["third_branch"])


def test_transfer_request_status_set_and_completion_shape(cur):
    w = _request_world(cur)
    destination = _assignment(cur, w["company"], w["target_branch"], w["employee"], w["driver"], FUTURE_START)
    cur.execute("UPDATE core.driverassignments SET effectivetoat = %s, endreason = 'Transferred' "
                "WHERE driverassignmentid = %s", (FUTURE_START, w["source"]))
    with rejected(cur, errors.CheckViolation):
        _request(cur, w, status="Paused")
    with rejected(cur, errors.CheckViolation):
        _request(cur, w, status="Scheduled", destination=destination, scheduled_at="2026-01-01 00:00+00")
    with rejected(cur, errors.CheckViolation):
        _request(cur, w, status="Scheduled", effective_at=FUTURE_START, scheduled_at="2026-01-01 00:00+00")
    with rejected(cur, errors.CheckViolation):
        _request(cur, w, status="Scheduled", destination=destination, effective_at=FUTURE_START)
    with rejected(cur, errors.CheckViolation):
        _request(cur, w, status="Completed", destination=destination)
    with rejected(cur, errors.CheckViolation):
        _request(cur, w, status="Completed", effective_at=FUTURE_START)
    assert _request(cur, w, status="Scheduled", destination=destination, effective_at=FUTURE_START,
                    scheduled_at="2026-01-01 00:00+00") is not None
    assert _request(cur, w, status="Completed", destination=destination,
                    effective_at=FUTURE_START) is not None
    assert _request(cur, w, status="Cancelled") is not None


# ---------------------------------------------------------------------------
# Evidence keyed by assignment and Driver
# ---------------------------------------------------------------------------

EVIDENCE_TABLES = [
    "payrollfinallines", "payrollcalculationdrivertotals", "payrollcalculationsnapshotbonusevents",
    "payrollcalculationsnapshotstatusentries", "payrollcalculationsnapshotusedratedefinitions",
    "payrollperiodauditevidenceevents",
]


@pytest.mark.parametrize("table", EVIDENCE_TABLES)
def test_evidence_tables_bind_the_assignment_to_its_driver_by_foreign_key(cur, table):
    cur.execute("""
        SELECT pg_get_constraintdef(oid), convalidated
        FROM pg_constraint
        WHERE contype = 'f' AND conrelid = %s::regclass
          AND confrelid = 'core.driverassignments'::regclass
          AND pg_get_constraintdef(oid) LIKE 'FOREIGN KEY (driverassignmentid, driverid)%%'
    """, (f"payroll.{table}",))
    rows = cur.fetchall()
    assert len(rows) == 1 and rows[0][1] is True
    assert "(driverassignmentid, driverid)" in rows[0][0].split("REFERENCES")[1]


def test_audit_evidence_rejects_a_driver_that_does_not_own_the_assignment(cur):
    w = _request_world(cur)
    cur.execute("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (%s, %s, 'Open', 'PDD87', 'dd87', 'Week', '2030-01-07', '2030-01-13') RETURNING payrollperiodid
    """, (w["company"], w["source_branch"]))
    period = cur.fetchone()[0]
    insert = """
        INSERT INTO payroll.payrollperiodauditevidenceevents
            (companyid, branchid, payrollperiodid, evidencedomain, actioncode, sourceentitytype,
             sourceentityid, actoruserid, actordisplaynamesnapshot, responsibilitycontextsnapshot,
             driverassignmentid, driverid)
        VALUES (%s, %s, %s, 'SOURCE', 'SOURCE_CREATED', 'Test', '1', %s, 'dd87', '{}', %s, %s)
    """

    def args(assignment, driver):
        return (w["company"], w["source_branch"], period, w["user"], assignment, driver)

    cur.execute(insert, args(w["source"], w["driver"]))
    cur.execute(insert, args(None, None))  # a period-level event names no Driver at all
    with rejected(cur, errors.ForeignKeyViolation):
        cur.execute(insert, args(w["source"], w["other_driver"]))
    with rejected(cur, errors.CheckViolation):
        cur.execute(insert, args(w["source"], None))
    with rejected(cur, errors.CheckViolation):
        cur.execute(insert, args(None, w["driver"]))


def test_eligibility_ownership_trigger_checks_the_assignment_branch(cur):
    w = _request_world(cur)
    cur.execute("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (%s, %s, 'Open', 'PDD88', 'dd87', 'Week', '2030-01-07', '2030-01-13') RETURNING payrollperiodid
    """, (w["company"], w["source_branch"]))
    period = cur.fetchone()[0]
    insert = """
        INSERT INTO payroll.payrollperioddrivereligibility
            (companyid, branchid, payrollperiodid, driverassignmentid, eligibilityreasoncode,
             snapshotsource, drivereffectivefromatsnapshot, branchtimezoneidsnapshot, eligibleworkdates,
             driverstatussnapshot, endreasonsnapshot)
        VALUES (%s, %s, %s, %s, 'Active', 'Generated', %s, 'UTC', '{}', %s, %s)
    """
    cur.execute(insert, (w["company"], w["source_branch"], period, w["source"], PAST_START, "Active", None))
    target_employee = _employee(cur, w["company"], w["target_branch"])
    target_driver = _driver(cur, w["company"], target_employee)
    other_assignment = _assignment(cur, w["company"], w["target_branch"], target_employee,
                                   target_driver, PAST_START)
    with rejected(cur, errors.CheckViolation):
        cur.execute(insert, (w["company"], w["source_branch"], period, other_assignment, PAST_START, "Active", None))
    with rejected(cur, errors.CheckViolation):
        cur.execute(insert, (w["company"], w["source_branch"], period, w["other_assignment"], PAST_START, "Terminated", None))
    with rejected(cur, errors.CheckViolation):
        cur.execute(insert, (w["company"], w["source_branch"], period, w["other_assignment"], PAST_START, "Active", "Resigned"))
