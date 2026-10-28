"""Current database-level Driver-owned compensation contract.

Ordinary rate schedules and Min/Max pay rules belong to the permanent
``core.Drivers`` row: they carry no DriverAssignmentID and no BranchID, so a permanent
transfer (one Driver, several home DriverAssignments) continues them without any copy.
These tests exercise the current schema on an isolated fresh Alembic database.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import errors, sql

from tests.schema_helpers import _schema_database

PAST_START = "2020-01-01 00:00+00"
PAST_MID = "2020-06-01 00:00+00"
PAST_END = "2020-09-01 00:00+00"
FUTURE_START = "2099-01-01 00:00+00"


@pytest.fixture(scope="module")
def head_database(schema_databases):
    with _schema_database(schema_databases, "driver_compensation") as database:
        yield database


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
def rejected(cursor, exc_type=psycopg2.Error):
    """Expect the statements inside to fail, and keep the transaction usable."""
    cursor.execute("SAVEPOINT expect_failure")
    with pytest.raises(exc_type) as info:
        yield info
    cursor.execute("ROLLBACK TO SAVEPOINT expect_failure")


@contextmanager
def refused_as_historical(cursor):
    """The Driver compensation context trigger: SQLSTATE FL001, DRIVER_COMPENSATION_HISTORICAL."""
    with rejected(cursor) as info:
        yield info
    error = info.value
    assert error.pgcode == "FL001", error.pgcode
    assert "DRIVER_COMPENSATION_HISTORICAL" in str(error)


def _set_op(cursor, op: str) -> None:
    cursor.execute("SELECT set_config('flussra.workforce_op', %s, TRUE)", (op,))


# ---------------------------------------------------------------------------
# Seed helpers (head schema)
# ---------------------------------------------------------------------------


def _company(cursor, *, zones: tuple[str, ...] = ("UTC", "UTC")) -> tuple[int, list[int]]:
    code = "DC" + uuid4().hex[:8]
    cursor.execute("""
        INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended, currencycode)
        VALUES (%s, %s, %s, 'Active', FALSE, 'USD') RETURNING companyid
    """, (code, code, code))
    company_id = cursor.fetchone()[0]
    branch_ids = []
    for index, zone in enumerate(zones):
        cursor.execute("""
            INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault, timezoneid)
            VALUES (%s, %s, %s, 'Active', %s, %s) RETURNING branchid
        """, (company_id, f"B{index}", f"Branch {index}", index == 0, zone))
        branch_ids.append(cursor.fetchone()[0])
    return company_id, branch_ids


def _user(cursor, company_id: int) -> int:
    cursor.execute("""
        INSERT INTO sec.users (companyid, username, displayname) VALUES (%s, %s, 'dc88') RETURNING userid
    """, (company_id, "dc_" + uuid4().hex[:10]))
    return cursor.fetchone()[0]


def _driver_row(cursor, company_id: int, branch_id: int) -> tuple[int, int]:
    """A permanent Driver (and its Employee); no assignment yet."""
    cursor.execute("""
        INSERT INTO core.employees (companyid, branchid, fullname, employmentstatus)
        VALUES (%s, %s, 'DC88 Driver', 'Active') RETURNING employeeid
    """, (company_id, branch_id))
    employee_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO core.drivers (companyid, employeeid, drivernumber, driverstatus)
        VALUES (%s, %s, %s, 'Active') RETURNING driverid
    """, (company_id, employee_id, "D" + uuid4().hex[:8]))
    return cursor.fetchone()[0], employee_id


def _assignment(cursor, world: dict, branch_id: int, start: str, end: str | None = None,
                reason: str | None = None) -> int:
    cursor.execute("""
        INSERT INTO core.driverassignments
            (companyid, branchid, employeeid, driverid, effectivefromat, effectivetoat, endreason)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING driverassignmentid
    """, (world["company"], branch_id, world["employee"], world["driver"], start, end, reason))
    return cursor.fetchone()[0]


def _world(cursor, *, zones: tuple[str, ...] = ("UTC", "UTC"), assignment: bool = True) -> dict:
    """A company, one Driver and (by default) one open home assignment in the first Branch."""
    company_id, branches = _company(cursor, zones=zones)
    driver_id, employee_id = _driver_row(cursor, company_id, branches[0])
    world = {"company": company_id, "branches": branches, "driver": driver_id,
             "employee": employee_id, "user": _user(cursor, company_id)}
    if assignment:
        world["assignment"] = _assignment(cursor, world, branches[0], PAST_START)
    _definitions(cursor, world)
    _calendar_source(cursor, world)
    return world


def _definitions(cursor, world: dict) -> None:
    """Two PayDefinition-owned Scalar RateDefinitions and the Company's Status-owned one."""
    company_id = world["company"]
    world["rate_definitions"] = []
    for _ in range(2):
        code = "PD" + uuid4().hex[:8]
        cursor.execute("""
            INSERT INTO payroll.paydefinitions
                (companyid, definitioncode, definitionname, inputtype, calculationmethod)
            VALUES (%s, %s, %s, 'Decimal', 'PerUnit') RETURNING paydefinitionid
        """, (company_id, code, code))
        pay_definition_id = cursor.fetchone()[0]
        cursor.execute("""
            INSERT INTO payroll.ratedefinitions (companyid, paydefinitionid, shape)
            VALUES (%s, %s, 'Scalar') RETURNING ratedefinitionid
        """, (company_id, pay_definition_id))
        rate_definition_id = cursor.fetchone()[0]
        cursor.execute("""
            INSERT INTO payroll.ratecomponentdefinitions (ratedefinitionid, shape, sequenceno)
            VALUES (%s, 'Scalar', 1) RETURNING ratecomponentdefinitionid
        """, (rate_definition_id,))
        world["rate_definitions"].append(
            {"pay": pay_definition_id, "rate": rate_definition_id,
             "component": cursor.fetchone()[0]})
    # The Company's default Status rate column owns the Status RateDefinition.
    cursor.execute("SELECT payroll.fn_EnsureDefaultStatusRateColumn(%s)", (company_id,))
    column_id = cursor.fetchone()[0]
    cursor.execute("""
        SELECT rd.ratedefinitionid, c.ratecomponentdefinitionid
        FROM payroll.ratedefinitions rd
        JOIN payroll.ratecomponentdefinitions c ON c.ratedefinitionid = rd.ratedefinitionid
        WHERE rd.statusratecolumnid = %s
    """, (column_id,))
    world["status_rate_definition"], world["status_component"] = cursor.fetchone()
    world["status_column"] = column_id


def _calendar_source(cursor, world: dict, *, company_id: int | None = None) -> dict:
    """A published Payroll Setup version: the provenance a pay rule's calendar is captured from."""
    company_id = company_id or world["company"]
    user_id = world["user"] if company_id == world["company"] else _user(cursor, company_id)
    cursor.execute("""
        INSERT INTO payroll.payrollsetups (companyid, setupcode, setupname, status)
        VALUES (%s, %s, 'dc88', 'Active') RETURNING payrollsetupid
    """, (company_id, "DC" + uuid4().hex[:10]))
    setup_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO payroll.payrollsetupversions
            (companyid, payrollsetupid, lifecyclestate, versionnumber, effectivefromdate,
             payrollfrequency, anchorstartdate, normaldaysoffmask, confighash,
             publishedbyuserid, publishedatutc)
        VALUES (%s, %s, 'Published', 1, DATE '2020-01-06', 'Week', DATE '2020-01-06', 0, %s, %s, NOW())
        RETURNING payrollsetupversionid
    """, (company_id, setup_id, "c" * 64, user_id))
    version_id = cursor.fetchone()[0]
    source = {"version": version_id}
    if company_id == world["company"]:
        world["calendar"] = source
    return source


def _pending(cursor, world: dict, *, definition: int = 0, start: str = "2021-01-01",
             end: str | None = None, driver: int | None = None, value: str | None = "25") -> int:
    """A Pending schedule for the Driver, with its (single) value set unless value is None."""
    rate = world["rate_definitions"][definition]
    cursor.execute("""
        INSERT INTO payroll.driverrateschedules
            (companyid, driverid, paydefinitionid, ratedefinitionid, effectivefrom, effectiveto)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING driverratescheduleid
    """, (world["company"], driver or world["driver"], rate["pay"], rate["rate"], start, end))
    schedule_id = cursor.fetchone()[0]
    if value is not None:
        cursor.execute("""
            INSERT INTO payroll.driverrateschedulevalues
                (driverratescheduleid, ratedefinitionid, ratecomponentdefinitionid, amount)
            VALUES (%s, %s, %s, %s)
        """, (schedule_id, rate["rate"], rate["component"], value))
    return schedule_id


def _approve(cursor, world: dict, schedule_id: int) -> None:
    cursor.execute("""
        UPDATE payroll.driverrateschedules
           SET status = 'Approved', approvedbyuserid = %s, approvedatutc = NOW()
         WHERE driverratescheduleid = %s
    """, (world["user"], schedule_id))


def _supersede(cursor, schedule_id: int, end: str) -> None:
    cursor.execute("""
        UPDATE payroll.driverrateschedules SET status = 'Superseded', effectiveto = %s
         WHERE driverratescheduleid = %s
    """, (end, schedule_id))


def _void_schedule(cursor, world: dict, schedule_id: int) -> None:
    cursor.execute("""
        UPDATE payroll.driverrateschedules
           SET status = 'Voided', voidedbyuserid = %s, voidedatutc = NOW(), voidreason = 'test'
         WHERE driverratescheduleid = %s
    """, (world["user"], schedule_id))


def _pay_rule(cursor, world: dict, *, rule_type: str = "MinimumPay", start: str = "2021-01-01",
              end: str | None = None, status: str = "Active", driver: int | None = None,
              frequency: str = "Week", interval: int | None = None,
              branch: int | None = None, version: int | None = None) -> int:
    cursor.execute("""
        INSERT INTO payroll.driverpayrules
            (companyid, driverid, ruletype, amount, effectivefrom, effectiveto, status,
             calendarfrequency, calendaranchorstartdate, calendarcustomintervaldays,
             calendarsourcebranchid, calendarsourcepayrollsetupversionid)
        VALUES (%s, %s, %s, 100, %s, %s, %s, %s, DATE '2020-01-06', %s, %s, %s)
        RETURNING driverpayruleid
    """, (world["company"], driver or world["driver"], rule_type, start, end, status,
          frequency, interval, branch or world["branches"][0],
          version or world["calendar"]["version"]))
    return cursor.fetchone()[0]


def _end_open_assignment_in_the_past(cursor, assignment_id: int, at: str = PAST_END) -> None:
    _set_op(cursor, "terminate")
    cursor.execute("""
        UPDATE core.driverassignments SET effectivetoat = %s, endreason = 'Terminated'
        WHERE driverassignmentid = %s
    """, (at, assignment_id))
    _set_op(cursor, "")


def _finalized_period(cursor, world: dict, branch_id: int, assignment_id: int, *, status: str,
                      start: str, end: str) -> int:
    cursor.execute("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (%s, %s, 'Open', %s, 'dc88', 'Week', %s, %s) RETURNING payrollperiodid
    """, (world["company"], branch_id, "P" + uuid4().hex[:8], start, end))
    period_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO payroll.payrollperioddrivereligibility
            (companyid, branchid, payrollperiodid, driverassignmentid, eligibilityreasoncode,
             snapshotsource, drivereffectivefromatsnapshot, branchtimezoneidsnapshot, eligibleworkdates,
             driverstatussnapshot, endreasonsnapshot)
        VALUES (%s, %s, %s, %s, 'Active', 'Generated', %s, 'UTC', '{}', 'Active', NULL)
    """, (world["company"], branch_id, period_id, assignment_id, PAST_START))
    if status != "Open":
        cursor.execute("UPDATE payroll.payrollperiods SET status = %s WHERE payrollperiodid = %s",
                       (status, period_id))
    return period_id


# ---------------------------------------------------------------------------
# Preflight, irreversibility and fresh upgrade
# ---------------------------------------------------------------------------


def _seed_0087_rows(cursor, kind: str) -> None:
    """The assignment-owned compensation state 0088 refuses to reinterpret."""
    code = "DC88" + kind[:4].upper() + uuid4().hex[:4]
    cursor.execute("""
        INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended, currencycode)
        VALUES (%s, %s, %s, 'Active', FALSE, 'USD') RETURNING companyid
    """, (code, code, code))
    company_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault, timezoneid)
        VALUES (%s, 'B0', 'b0', 'Active', TRUE, 'UTC') RETURNING branchid
    """, (company_id,))
    branch_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO core.employees (companyid, branchid, fullname, employmentstatus)
        VALUES (%s, %s, 'DC88', 'Active') RETURNING employeeid
    """, (company_id, branch_id))
    employee_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO core.drivers (companyid, employeeid, drivernumber, driverstatus)
        VALUES (%s, %s, 'D88', 'Active') RETURNING driverid
    """, (company_id, employee_id))
    driver_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO core.driverassignments (companyid, branchid, employeeid, driverid, effectivefromat)
        VALUES (%s, %s, %s, %s, TIMESTAMPTZ '2020-01-01 00:00+00') RETURNING driverassignmentid
    """, (company_id, branch_id, employee_id, driver_id))
    assignment_id = cursor.fetchone()[0]
    if kind == "pay_rule":
        cursor.execute("""
            INSERT INTO payroll.driverpayrules
                (companyid, branchid, driverassignmentid, ruletype, amount, effectivefrom)
            VALUES (%s, %s, %s, 'MinimumPay', 100, DATE '2021-01-01')
        """, (company_id, branch_id, assignment_id))
        return
    cursor.execute("""
        INSERT INTO payroll.paydefinitions
            (companyid, definitioncode, definitionname, inputtype, calculationmethod)
        VALUES (%s, 'PD88', 'pd88', 'Decimal', 'PerUnit') RETURNING paydefinitionid
    """, (company_id,))
    pay_definition_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO payroll.ratedefinitions (companyid, paydefinitionid, shape)
        VALUES (%s, %s, 'Scalar') RETURNING ratedefinitionid
    """, (company_id, pay_definition_id))
    rate_definition_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO payroll.ratecomponentdefinitions (ratedefinitionid, shape, sequenceno)
        VALUES (%s, 'Scalar', 1) RETURNING ratecomponentdefinitionid
    """, (rate_definition_id,))
    component_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO payroll.driverrateassignments
            (companyid, branchid, driverassignmentid, ratedefinitionid, effectivefrom)
        VALUES (%s, %s, %s, %s, DATE '2021-01-01') RETURNING driverrateassignmentid
    """, (company_id, branch_id, assignment_id, rate_definition_id))
    schedule_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO payroll.driverratevalues
            (driverrateassignmentid, ratedefinitionid, ratecomponentdefinitionid, amount)
        VALUES (%s, %s, %s, 25)
    """, (schedule_id, rate_definition_id, component_id))


def test_fresh_upgrade_reaches_head_with_the_driver_owned_shape(head_database, cur):
    cur.execute("SELECT version_num FROM public.alembic_version")
    assert cur.fetchone() == ("0097",)

    def columns(table: str) -> set[str]:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'payroll' AND table_name = %s
        """, (table,))
        return {row[0] for row in cur.fetchall()}

    assert columns("driverrateschedules") == {
        "driverratescheduleid", "companyid", "driverid", "paydefinitionid", "statusratecolumnid",
        "ratedefinitionid", "effectivefrom", "effectiveto", "status", "createdbyuserid",
        "createdatutc",
        "updatedbyuserid", "updatedatutc", "approvedbyuserid", "approvedatutc",
        "voidedbyuserid", "voidedatutc", "voidreason", "notes",
    }
    assert columns("driverrateschedulevalues") == {
        "driverrateschedulevalueid", "driverratescheduleid", "ratedefinitionid",
        "ratecomponentdefinitionid", "amount",
    }
    assert columns("driverpayrules") == {
        "driverpayruleid", "companyid", "driverid", "ruletype", "amount", "effectivefrom",
        "effectiveto", "status", "createdbyuserid", "createdatutc", "updatedbyuserid",
        "updatedatutc", "notes", "calendarfrequency", "calendaranchorstartdate",
        "calendarcustomintervaldays", "calendarsourcebranchid",
        "calendarsourcepayrollsetupversionid",
    }
    for retired in ("driverrateassignments", "driverratevalues", "driverrates", "driverratetiers"):
        cur.execute("SELECT to_regclass(%s)", (f"payroll.{retired}",))
        assert cur.fetchone()[0] is None


def test_no_object_keeps_a_retired_assignment_name_or_foreign_key(cur):
    # No constraint, index, sequence, trigger, function or NOT NULL name is left on the
    # old assignment names, and nothing in compensation references a DriverAssignment.
    cur.execute(r"""
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'payroll' AND c.relname ~ '(driverrateassignment|driverratevalue)'
    """)
    assert cur.fetchall() == []
    cur.execute(r"""
        SELECT conrelid::regclass::TEXT, conname FROM pg_constraint
        WHERE conrelid IN ('payroll.driverrateschedules'::regclass,
                           'payroll.driverrateschedulevalues'::regclass,
                           'payroll.driverpayrules'::regclass)
          AND conname ~ '(assignment|driverratevalue)'
    """)
    assert cur.fetchall() == []
    cur.execute(r"""
        SELECT tgrelid::regclass::TEXT, tgname FROM pg_trigger
        WHERE NOT tgisinternal AND (tgname ~ '(driverrateassignment|driverratevalue)')
    """)
    assert cur.fetchall() == []
    cur.execute(r"""
        SELECT n.nspname || '.' || p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND (p.proname ~ '(driverrateassignment|driverratevalue)'
               OR p.prosrc ~* '(driverrateassignment|driverratevalue|RATE_ASSIGNMENT|PENDING_ASSIGNMENT)')
    """)
    assert cur.fetchall() == []
    cur.execute("""
        SELECT conrelid::regclass::TEXT, conname FROM pg_constraint
        WHERE contype = 'f'
          AND conrelid IN ('payroll.driverrateschedules'::regclass,
                           'payroll.driverrateschedulevalues'::regclass,
                           'payroll.driverpayrules'::regclass)
          AND confrelid = 'core.driverassignments'::regclass
    """)
    assert cur.fetchall() == []
    cur.execute("""
        SELECT conrelid::regclass::TEXT, conname FROM pg_constraint
        WHERE contype = 'n'
          AND conrelid IN ('payroll.driverrateschedules'::regclass,
                           'payroll.driverrateschedulevalues'::regclass,
                           'payroll.driverpayrules'::regclass)
          AND conname !~ '^(driverrateschedules|driverrateschedulevalues|driverpayrules)_[a-z]+_not_null$'
    """)
    assert cur.fetchall() == []
    cur.execute("""
        SELECT s.relname FROM pg_class s JOIN pg_namespace n ON n.oid = s.relnamespace
        WHERE n.nspname = 'payroll' AND s.relkind = 'S'
          AND s.relname IN ('driverrateschedules_driverratescheduleid_seq',
                            'driverrateschedulevalues_driverrateschedulevalueid_seq')
    """)
    assert len(cur.fetchall()) == 2


def test_driver_owned_context_guards_remain_without_legacy_assignment_guards(cur):
    def triggers(name: str) -> set[str]:
        cur.execute("""
            SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE NOT t.tgisinternal AND n.nspname = 'payroll' AND t.tgname = %s
        """, (name,))
        return {row[0] for row in cur.fetchall()}

    assert triggers("trg_compensationassignmentwritable") == set()
    assert triggers("trg_drivercompensationcontextwritable") == {"driverrateschedules", "driverpayrules"}
    cur.execute("SELECT to_regprocedure('core.trg_compensationassignmentwritable()')")
    assert cur.fetchone()[0] is None


# ---------------------------------------------------------------------------
# Ownership: the permanent Driver
# ---------------------------------------------------------------------------


def test_a_schedule_belongs_to_a_driver_of_its_own_company_and_pay_definition(cur):
    w = _world(cur)
    other = _world(cur)
    rate = w["rate_definitions"][0]
    insert = """
        INSERT INTO payroll.driverrateschedules
            (companyid, driverid, paydefinitionid, ratedefinitionid, effectivefrom)
        VALUES (%s, %s, %s, %s, DATE '2021-01-01')
    """
    with rejected(cur, errors.ForeignKeyViolation):  # Driver of another company
        cur.execute(insert, (w["company"], other["driver"], rate["pay"], rate["rate"]))
    with rejected(cur, errors.ForeignKeyViolation):  # company differs from Driver and RateDefinition
        cur.execute(insert, (other["company"], w["driver"], rate["pay"], rate["rate"]))
    with rejected(cur, errors.ForeignKeyViolation):  # PayDefinition is not the RateDefinition's own
        cur.execute(insert, (w["company"], w["driver"], w["rate_definitions"][1]["pay"], rate["rate"]))
    with rejected(cur, errors.CheckViolation):  # an owner is required
        cur.execute("""
            INSERT INTO payroll.driverrateschedules
                (companyid, driverid, ratedefinitionid, effectivefrom)
            VALUES (%s, %s, %s, DATE '2021-01-01')
        """, (w["company"], w["driver"], rate["rate"]))
    with rejected(cur, errors.NotNullViolation):
        cur.execute("""
            INSERT INTO payroll.driverrateschedules
                (companyid, paydefinitionid, ratedefinitionid, effectivefrom)
            VALUES (%s, %s, %s, DATE '2021-01-01')
        """, (w["company"], rate["pay"], rate["rate"]))
    _pending(cur, w)


def test_a_status_owned_rate_definition_is_scheduled_by_its_status_rate_column(cur):
    w = _world(cur)
    status_rate = w["status_rate_definition"]
    cur.execute("SELECT paydefinitionid FROM payroll.ratedefinitions WHERE ratedefinitionid = %s",
                (status_rate,))
    assert cur.fetchone() == (None,)
    insert = """
        INSERT INTO payroll.driverrateschedules
            (companyid, driverid, paydefinitionid, statusratecolumnid, ratedefinitionid,
             effectivefrom)
        VALUES (%s, %s, %s, %s, %s, DATE '2021-01-01')
    """
    # A PayDefinition cannot own a schedule of a Status-owned definition.
    for pay_definition in (w["rate_definitions"][0]["pay"], w["rate_definitions"][1]["pay"]):
        with rejected(cur, errors.ForeignKeyViolation):
            cur.execute(insert, (w["company"], w["driver"], pay_definition, None, status_rate))
    # Neither owner, or both owners, are refused.
    with rejected(cur, errors.CheckViolation):
        cur.execute(insert, (w["company"], w["driver"], None, None, status_rate))
    with rejected(cur, errors.CheckViolation):
        cur.execute(insert, (w["company"], w["driver"], w["rate_definitions"][0]["pay"],
                             w["status_column"], status_rate))
    cur.execute(insert, (w["company"], w["driver"], None, w["status_column"], status_rate))


def _transferred_driver(cursor) -> dict:
    """One Driver with an ended source assignment and a current destination in another Branch."""
    w = _world(cursor, assignment=False)
    w["source"] = _assignment(cursor, w, w["branches"][0], PAST_START, PAST_END, "Transferred")
    w["destination"] = _assignment(cursor, w, w["branches"][1], PAST_END)
    return w


def test_uniqueness_and_overlap_are_per_driver_across_its_assignments(cur):
    w = _transferred_driver(cur)
    # One Pending and one Approved per (Driver, RateDefinition), whatever assignment it was
    # authored under: the key is the Driver.
    first = _pending(cur, w, start="2021-01-01")
    with rejected(cur, errors.UniqueViolation):
        _pending(cur, w, start="2022-01-01")
    _approve(cur, w, first)
    second = _pending(cur, w, start="2022-01-01", end="2022-12-31")  # a Pending beside the Approved
    _supersede(cur, first, "2021-12-31")
    _approve(cur, w, second)
    third = _pending(cur, w, start="2023-01-01", end="2023-12-31")
    with rejected(cur, errors.UniqueViolation):  # a second Approved for the same pair, no overlap
        _approve(cur, w, third)
    _supersede(cur, second, "2022-12-31")
    _approve(cur, w, third)
    # No overlapping Approved/Superseded windows for the pair.
    early = _pending(cur, w, definition=1, start="2021-01-01")
    _approve(cur, w, early)
    _supersede(cur, early, "2021-06-30")
    overlapping = _pending(cur, w, definition=1, start="2021-06-01")
    with rejected(cur, errors.ExclusionViolation):
        _approve(cur, w, overlapping)
    # A different RateDefinition, and a different Driver, are independent.
    other_driver, other_employee = _driver_row(cur, w["company"], w["branches"][1])
    cur.execute("""
        INSERT INTO core.driverassignments (companyid, branchid, employeeid, driverid, effectivefromat)
        VALUES (%s, %s, %s, %s, %s)
    """, (w["company"], w["branches"][1], other_employee, other_driver, PAST_START))
    _approve(cur, w, _pending(cur, w, start="2021-01-01", driver=other_driver))


def test_a_pay_rule_window_and_open_end_are_unique_per_driver_and_rule_type(cur):
    w = _transferred_driver(cur)
    first = _pay_rule(cur, w, start="2021-01-01", end="2021-12-31")
    with rejected(cur, errors.ExclusionViolation):
        _pay_rule(cur, w, start="2021-12-31")
    _pay_rule(cur, w, start="2022-01-01")
    with rejected(cur, errors.ExclusionViolation):  # also an overlapping closed window
        _pay_rule(cur, w, start="2023-01-01", end="2023-12-31")
    _pay_rule(cur, w, rule_type="MaximumPay", start="2021-01-01")  # other rule type
    cur.execute("UPDATE payroll.driverpayrules SET status = 'Voided' WHERE driverpayruleid = %s", (first,))
    _pay_rule(cur, w, start="2021-01-01", end="2021-12-31")  # a Voided rule frees its window
    open_driver, open_employee = _driver_row(cur, w["company"], w["branches"][1])
    cur.execute("""
        INSERT INTO core.driverassignments (companyid, branchid, employeeid, driverid, effectivefromat)
        VALUES (%s, %s, %s, %s, %s)
    """, (w["company"], w["branches"][1], open_employee, open_driver, PAST_START))
    _pay_rule(cur, w, start="2021-01-01", driver=open_driver)  # another Driver


# ---------------------------------------------------------------------------
# Schedule triggers, re-pointed
# ---------------------------------------------------------------------------


def test_schedule_lifecycle_guards_hold_on_the_driver_key(cur):
    w = _world(cur)
    other = _driver_row(cur, w["company"], w["branches"][0])[0]
    pending = _pending(cur, w)
    with rejected(cur, errors.CheckViolation) as info:
        cur.execute("UPDATE payroll.driverrateschedules SET driverid = %s WHERE driverratescheduleid = %s",
                    (other, pending))
    assert "RATE_SCHEDULE_IDENTITY_IMMUTABLE" in str(info.value)
    with rejected(cur, errors.CheckViolation) as info:
        cur.execute("UPDATE payroll.driverrateschedules SET paydefinitionid = %s "
                    "WHERE driverratescheduleid = %s", (w["rate_definitions"][1]["pay"], pending))
    assert "RATE_SCHEDULE_IDENTITY_IMMUTABLE" in str(info.value)
    rate = w["rate_definitions"][0]
    with rejected(cur, errors.CheckViolation) as info:  # must start Pending
        cur.execute("""
            INSERT INTO payroll.driverrateschedules
                (companyid, driverid, paydefinitionid, ratedefinitionid, effectivefrom, status,
                 approvedbyuserid, approvedatutc)
            VALUES (%s, %s, %s, %s, DATE '2024-01-01', 'Approved', %s, NOW())
        """, (w["company"], w["driver"], rate["pay"], rate["rate"], w["user"]))
    assert "RATE_SCHEDULE_MUST_START_PENDING" in str(info.value)
    incomplete = _pending(cur, w, definition=1, value=None)
    with rejected(cur, errors.CheckViolation) as info:
        _approve(cur, w, incomplete)
    assert "RATE_SCHEDULE_INCOMPLETE" in str(info.value)
    _approve(cur, w, pending)
    with rejected(cur, errors.CheckViolation) as info:
        cur.execute("UPDATE payroll.driverrateschedulevalues SET amount = 1 "
                    "WHERE driverratescheduleid = %s", (pending,))
    assert "RATE_VALUES_IMMUTABLE_AFTER_APPROVAL" in str(info.value)
    with rejected(cur, errors.CheckViolation) as info:
        cur.execute("DELETE FROM payroll.driverrateschedules WHERE driverratescheduleid = %s", (pending,))
    assert "RATE_SCHEDULE_DELETE_BLOCKED" in str(info.value)
    with rejected(cur, errors.CheckViolation) as info:
        cur.execute("UPDATE payroll.driverrateschedules SET effectivefrom = DATE '2021-02-01' "
                    "WHERE driverratescheduleid = %s", (pending,))
    assert "RATE_SCHEDULE_AUTHORITATIVE_IMMUTABLE" in str(info.value)
    with rejected(cur, errors.CheckViolation) as info:
        cur.execute("UPDATE payroll.driverrateschedules SET status = 'Pending' "
                    "WHERE driverratescheduleid = %s", (pending,))
    assert "RATE_SCHEDULE_INVALID_TRANSITION" in str(info.value)
    # The structure lock is set by the first approval and a Pending schedule blocks edits.
    cur.execute("SELECT structurelockedatutc FROM payroll.ratedefinitions WHERE ratedefinitionid = %s",
                (rate["rate"],))
    assert cur.fetchone()[0] is not None


def test_discarding_a_pending_schedule_audits_it_without_a_branch(cur):
    w = _world(cur)
    pending = _pending(cur, w, value="12.5")
    cur.execute("SELECT set_config('flussra.actor_user_id', %s, TRUE)", (str(w["user"]),))
    cur.execute("DELETE FROM payroll.driverrateschedules WHERE driverratescheduleid = %s", (pending,))
    cur.execute("""
        SELECT actioncode, entityname, branchid, actoruserid, oldvaluejson::jsonb
        FROM audit.auditlog WHERE entityid = %s AND entityname = 'DriverRateSchedules'
    """, (str(pending),))
    action, entity, branch, actor, old = cur.fetchone()
    assert (action, entity, branch, actor) == (
        "DRIVER_RATE_SCHEDULE_DISCARDED", "DriverRateSchedules", None, w["user"])
    assert old["DriverRateScheduleID"] == pending and old["DriverID"] == w["driver"]
    assert "DriverAssignmentID" not in old and "DriverRateAssignmentID" not in old
    assert [float(row["Amount"]) for row in old["Values"]] == [12.5]
    cur.execute("SELECT count(*) FROM payroll.driverrateschedulevalues WHERE driverratescheduleid = %s",
                (pending,))
    assert cur.fetchone() == (0,)


def test_structure_edits_are_blocked_by_a_pending_schedule_of_any_driver(cur):
    w = _world(cur)
    _pending(cur, w)
    rate = w["rate_definitions"][0]["rate"]
    with rejected(cur, errors.CheckViolation) as info:
        cur.execute("SELECT payroll.fn_AssertRateStructureMutable(%s)", (rate,))
    assert "RATE_STRUCTURE_PENDING_SCHEDULE" in str(info.value)


def test_company_currency_becomes_immutable_once_a_schedule_or_pay_rule_is_authoritative(cur):
    w = _world(cur)
    cur.execute("SELECT core.fn_company_has_durable_monetary_state(%s)", (w["company"],))
    assert cur.fetchone() == (False,)
    _pending(cur, w)
    cur.execute("SELECT core.fn_company_has_durable_monetary_state(%s)", (w["company"],))
    assert cur.fetchone() == (False,)  # a Pending schedule is not durable monetary state
    _pay_rule(cur, w)
    cur.execute("SELECT core.fn_company_has_durable_monetary_state(%s)", (w["company"],))
    assert cur.fetchone() == (True,)
    v = _world(cur)
    _approve(cur, v, _pending(cur, v))
    cur.execute("SELECT core.fn_company_has_durable_monetary_state(%s)", (v["company"],))
    assert cur.fetchone() == (True,)


# ---------------------------------------------------------------------------
# Pay rules: captured calendar and integrity
# ---------------------------------------------------------------------------


def test_pay_rule_calendar_columns_are_required_and_constrained(cur):
    w = _world(cur)
    _pay_rule(cur, w, frequency="Week")
    _pay_rule(cur, w, rule_type="MaximumPay", frequency="Custom", interval=14)
    for frequency in ("Biweek", "Month"):
        cur.execute("DELETE FROM payroll.driverpayrules WHERE companyid = %s", (w["company"],))
        _pay_rule(cur, w, frequency=frequency)
    with rejected(cur, errors.CheckViolation):  # frequency is a closed set
        _pay_rule(cur, w, rule_type="MaximumPay", frequency="Daily")
    with rejected(cur, errors.CheckViolation):  # Custom needs its interval
        _pay_rule(cur, w, rule_type="MaximumPay", frequency="Custom", interval=None)
    with rejected(cur, errors.CheckViolation):  # a fixed frequency has no interval
        _pay_rule(cur, w, rule_type="MaximumPay", frequency="Week", interval=7)
    with rejected(cur, errors.CheckViolation):  # the interval is positive
        _pay_rule(cur, w, rule_type="MaximumPay", frequency="Custom", interval=0)
    for column in ("calendarfrequency", "calendaranchorstartdate", "calendarsourcebranchid",
                   "calendarsourcepayrollsetupversionid", "driverid"):
        cur.execute("""
            SELECT is_nullable FROM information_schema.columns
            WHERE table_schema = 'payroll' AND table_name = 'driverpayrules' AND column_name = %s
        """, (column,))
        assert cur.fetchone() == ("NO",), column
    cur.execute("""
        SELECT is_nullable FROM information_schema.columns
        WHERE table_schema = 'payroll' AND table_name = 'driverpayrules'
          AND column_name = 'calendarcustomintervaldays'
    """)
    assert cur.fetchone() == ("YES",)


def test_pay_rule_calendar_provenance_is_company_consistent(cur):
    w = _world(cur)
    other = _world(cur)
    with rejected(cur, errors.ForeignKeyViolation):  # a Branch of another company
        _pay_rule(cur, w, branch=other["branches"][0])
    with rejected(cur, errors.ForeignKeyViolation):  # a Payroll Setup version of another company
        _pay_rule(cur, w, version=other["calendar"]["version"])
    with rejected(cur, errors.ForeignKeyViolation):  # a Driver of another company
        _pay_rule(cur, w, driver=other["driver"])
    with rejected(cur, errors.ForeignKeyViolation):  # a version that does not exist
        _pay_rule(cur, w, version=2_000_000_000)
    _pay_rule(cur, w)


def test_pay_rule_identity_amount_start_and_calendar_are_immutable(cur):
    w = _world(cur)
    other_driver = _driver_row(cur, w["company"], w["branches"][0])[0]
    rule = _pay_rule(cur, w, frequency="Custom", interval=14)
    other = _calendar_source(cur, w)  # another published version of the same company
    changes = {
        "companyid": None, "driverid": other_driver, "ruletype": "'MaximumPay'", "amount": "101",
        "effectivefrom": "DATE '2021-01-02'", "calendarfrequency": "'Week'",
        "calendaranchorstartdate": "DATE '2020-01-13'", "calendarcustomintervaldays": "7",
        "calendarsourcebranchid": w["branches"][1],
        "calendarsourcepayrollsetupversionid": other["version"],
    }
    for column, value in changes.items():
        if value is None:
            continue
        with rejected(cur, errors.CheckViolation) as info:
            cur.execute(sql.SQL("UPDATE payroll.driverpayrules SET {} = {} WHERE driverpayruleid = %s").format(
                sql.Identifier(column), sql.SQL(str(value))), (rule,))
        assert "DRIVER_PAY_RULE_IMMUTABLE" in str(info.value), column
    # Notes and audit metadata stay editable.
    cur.execute("UPDATE payroll.driverpayrules SET notes = 'note', updatedbyuserid = %s, updatedatutc = NOW() "
                "WHERE driverpayruleid = %s", (w["user"], rule))
    assert cur.rowcount == 1


def test_pay_rule_lifecycle_is_active_to_ended_or_voided_and_ended_to_voided(cur):
    w = _world(cur)

    def move(rule, status, end=None):
        cur.execute("UPDATE payroll.driverpayrules SET status = %s, effectiveto = COALESCE(%s, effectiveto) "
                    "WHERE driverpayruleid = %s", (status, end, rule))

    ended = _pay_rule(cur, w, start="2021-01-01")
    move(ended, "Ended", "2021-12-31")
    cur.execute("SELECT status, effectiveto FROM payroll.driverpayrules WHERE driverpayruleid = %s", (ended,))
    assert cur.fetchone() == ("Ended", date(2021, 12, 31))
    with rejected(cur, errors.CheckViolation) as info:
        move(ended, "Active")
    assert "DRIVER_PAY_RULE_INVALID_TRANSITION" in str(info.value)
    with rejected(cur, errors.CheckViolation) as info:  # an end date moves only with ending
        cur.execute("UPDATE payroll.driverpayrules SET effectiveto = DATE '2022-06-30' "
                    "WHERE driverpayruleid = %s", (ended,))
    assert "DRIVER_PAY_RULE_EFFECTIVE_TO_IMMUTABLE" in str(info.value)
    move(ended, "Voided")  # Ended -> Voided
    with rejected(cur, errors.CheckViolation) as info:
        move(ended, "Active")
    assert "DRIVER_PAY_RULE_INVALID_TRANSITION" in str(info.value)
    with rejected(cur, errors.CheckViolation) as info:
        move(ended, "Ended", "2021-12-31")
    assert "DRIVER_PAY_RULE_INVALID_TRANSITION" in str(info.value)

    active = _pay_rule(cur, w, rule_type="MaximumPay", start="2021-01-01")
    with rejected(cur, errors.CheckViolation) as info:  # open end cannot be set without ending
        cur.execute("UPDATE payroll.driverpayrules SET effectiveto = DATE '2021-12-31' "
                    "WHERE driverpayruleid = %s", (active,))
    assert "DRIVER_PAY_RULE_EFFECTIVE_TO_IMMUTABLE" in str(info.value)
    move(active, "Voided")  # Active -> Voided, window untouched
    cur.execute("UPDATE payroll.driverpayrules SET notes = 'still editable' WHERE driverpayruleid = %s", (active,))
    with rejected(cur, errors.CheckViolation) as info:  # Voided keeps its window
        cur.execute("UPDATE payroll.driverpayrules SET effectiveto = DATE '2021-12-31' "
                    "WHERE driverpayruleid = %s", (active,))
    assert "DRIVER_PAY_RULE_EFFECTIVE_TO_IMMUTABLE" in str(info.value)


# ---------------------------------------------------------------------------
# core.fn_DriverAssignmentForBusinessDate
# ---------------------------------------------------------------------------


def _context(cursor, world: dict, day: str, *, company: int | None = None, driver: int | None = None):
    cursor.execute("SELECT core.fn_DriverAssignmentForBusinessDate(%s, %s, %s::date)",
                   (company or world["company"], driver or world["driver"], day))
    return cursor.fetchone()[0]


def test_context_is_null_before_the_first_assignment_and_for_foreign_or_null_arguments(cur):
    w = _world(cur)
    assert _context(cur, w, "2019-12-31") is None
    assert _context(cur, w, "2020-01-01") == w["assignment"]
    other = _world(cur)
    assert _context(cur, w, "2021-01-01", company=other["company"]) is None
    assert _context(cur, w, "2021-01-01", driver=other["driver"]) is None
    cur.execute("SELECT core.fn_DriverAssignmentForBusinessDate(%s, %s, NULL)", (w["company"], w["driver"]))
    assert cur.fetchone() == (None,)


def test_context_is_the_assignment_covering_a_normal_day(cur):
    w = _transferred_driver(cur)
    assert _context(cur, w, "2020-03-15") == w["source"]
    assert _context(cur, w, "2020-08-31") == w["source"]
    assert _context(cur, w, "2021-03-15") == w["destination"]
    assert _context(cur, w, "2099-01-01") == w["destination"]  # open-ended


def test_context_on_the_transfer_day_is_the_destination(cur):
    # Transfer at midnight: the source no longer overlaps the transfer day at all.
    w = _transferred_driver(cur)
    assert _context(cur, w, "2020-09-01") == w["destination"]
    assert _context(cur, w, "2020-08-31") == w["source"]
    # Transfer at noon: both tenures cover part of the day, and the latest-starting one wins.
    w = _world(cur, assignment=False)
    w["source"] = _assignment(cur, w, w["branches"][0], PAST_START, "2020-09-01 12:00+00", "Transferred")
    w["destination"] = _assignment(cur, w, w["branches"][1], "2020-09-01 12:00+00")
    assert _context(cur, w, "2020-09-01") == w["destination"]
    assert _context(cur, w, "2020-08-31") == w["source"]


def test_context_uses_each_assignments_own_branch_timezone(cur):
    # Source Branch in Tokyo (UTC+9), destination Branch in Los Angeles (UTC-7 in September),
    # transfer at 2020-09-01 06:00Z.
    w = _world(cur, zones=("Asia/Tokyo", "America/Los_Angeles"), assignment=False)
    w["source"] = _assignment(cur, w, w["branches"][0], PAST_START, "2020-09-01 06:00+00", "Transferred")
    w["destination"] = _assignment(cur, w, w["branches"][1], "2020-09-01 06:00+00")
    # Aug 30: only Tokyo's day overlaps the source; Los Angeles' Aug 30 ends before the destination.
    assert _context(cur, w, "2020-08-30") == w["source"]
    # Aug 31 and Sept 1: Los Angeles' local day overlaps the destination window, and the
    # destination starts later, so it is the context even though Tokyo's day also overlaps.
    assert _context(cur, w, "2020-08-31") == w["destination"]
    assert _context(cur, w, "2020-09-01") == w["destination"]
    assert _context(cur, w, "2020-09-02") == w["destination"]
    # Reverse zones: the date is read in each assignment's own Branch, not in a shared clock.
    r = _world(cur, zones=("America/Los_Angeles", "Asia/Tokyo"), assignment=False)
    r["source"] = _assignment(cur, r, r["branches"][0], PAST_START, "2020-09-01 06:00+00", "Transferred")
    r["destination"] = _assignment(cur, r, r["branches"][1], "2020-09-01 06:00+00")
    assert _context(cur, r, "2020-08-31") == r["source"]  # Tokyo's Aug 31 ends before the destination starts
    assert _context(cur, r, "2020-09-01") == r["destination"]


def test_an_empty_terminated_window_never_matches(cur):
    w = _world(cur, assignment=False)
    real = _assignment(cur, w, w["branches"][0], PAST_START, PAST_MID, "Terminated")
    empty = _assignment(cur, w, w["branches"][1], PAST_MID, PAST_MID, "Terminated")
    assert real != empty
    assert _context(cur, w, "2020-05-31") == real
    assert _context(cur, w, "2020-06-01") is None  # the real tenure ended at 00:00Z; the empty one never counts
    assert _context(cur, w, "2020-07-01") is None


def test_a_scheduled_transfers_destination_is_context_only_from_its_effective_day(cur):
    w = _world(cur, assignment=False)
    w["source"] = _assignment(cur, w, w["branches"][0], PAST_START, FUTURE_START, "Transferred")
    w["destination"] = _assignment(cur, w, w["branches"][1], FUTURE_START)
    assert _context(cur, w, "2098-12-31") == w["source"]
    assert _context(cur, w, "2099-01-01") == w["destination"]
    assert _context(cur, w, "2030-01-01") == w["source"]


# ---------------------------------------------------------------------------
# payroll.fn_DriverFinalizedPayrollTouched
# ---------------------------------------------------------------------------


def _touched(cursor, world: dict, footprint: str, mode: str, driver: int | None = None):
    cursor.execute("""
        SELECT payrollperiodid, startdate::TEXT, enddate::TEXT, status
        FROM payroll.fn_DriverFinalizedPayrollTouched(%s, %s, %s::daterange, %s)
        ORDER BY startdate
    """, (world["company"], driver or world["driver"], footprint, mode))
    return cursor.fetchall()


def test_finalized_guard_sees_locked_and_archived_periods_in_any_branch_of_the_driver(cur):
    w = _transferred_driver(cur)
    in_source_branch = _finalized_period(cur, w, w["branches"][0], w["source"], status="Locked",
                                         start="2020-03-02", end="2020-03-08")
    in_destination_branch = _finalized_period(cur, w, w["branches"][1], w["destination"],
                                              status="Archived", start="2021-03-01", end="2021-03-07")
    _finalized_period(cur, w, w["branches"][1], w["destination"], status="Approved",
                      start="2021-04-05", end="2021-04-11")
    _finalized_period(cur, w, w["branches"][1], w["destination"], status="Open",
                      start="2021-05-03", end="2021-05-09")
    everything = "[2000-01-01,2100-01-01)"
    assert [row[0] for row in _touched(cur, w, everything, "WorkDate")] == [in_source_branch, in_destination_branch]
    assert _touched(cur, w, everything, "WorkDate")[1][1:] == ("2021-03-01", "2021-03-07", "Archived")
    # The Driver's other Branch is found from the destination-side footprint alone.
    assert [row[0] for row in _touched(cur, w, "[2020-03-01,2020-03-31]", "WorkDate")] == [in_source_branch]
    assert [row[0] for row in _touched(cur, w, "[2021-01-01,)", "WorkDate")] == [in_destination_branch]
    assert _touched(cur, w, "[2021-06-01,)", "WorkDate") == []


def test_finalized_guard_modes_differ_in_how_a_period_meets_the_footprint(cur):
    w = _transferred_driver(cur)
    period = _finalized_period(cur, w, w["branches"][1], w["destination"], status="Locked",
                               start="2021-03-01", end="2021-03-07")
    mid_period = "[2021-03-04,2021-03-20]"
    assert [row[0] for row in _touched(cur, w, mid_period, "WorkDate")] == [period]  # work dates overlap
    assert _touched(cur, w, mid_period, "PeriodStart") == []  # the period started before the footprint
    assert [row[0] for row in _touched(cur, w, "[2021-03-01,2021-03-20]", "PeriodStart")] == [period]
    assert [row[0] for row in _touched(cur, w, "[2021-02-01,2021-03-01)", "PeriodStart")] == []  # half-open end
    assert [row[0] for row in _touched(cur, w, "[,2021-03-02)", "PeriodStart")] == [period]  # unbounded start
    assert _touched(cur, w, "empty", "WorkDate") == []


def test_finalized_guard_ignores_another_drivers_periods_and_other_companies(cur):
    w = _transferred_driver(cur)
    _finalized_period(cur, w, w["branches"][1], w["destination"], status="Locked",
                      start="2021-03-01", end="2021-03-07")
    other_driver, other_employee = _driver_row(cur, w["company"], w["branches"][1])
    other_assignment = _assignment(cur, {**w, "driver": other_driver, "employee": other_employee},
                                   w["branches"][1], PAST_START)
    assert _touched(cur, w, "[2021-01-01,)", "WorkDate", driver=other_driver) == []
    _finalized_period(cur, w, w["branches"][1], other_assignment, status="Archived",
                      start="2021-07-05", end="2021-07-11")
    assert len(_touched(cur, w, "[2021-01-01,)", "WorkDate")) == 1  # still only the first Driver's period
    assert len(_touched(cur, w, "[2021-01-01,)", "WorkDate", driver=other_driver)) == 1
    foreign = _world(cur)
    assert _touched(cur, foreign, "[2000-01-01,)", "WorkDate", driver=w["driver"]) == []


def test_one_predicate_serves_the_guard_and_the_lock_discovery(cur):
    """The finalized guard is the Locked/Archived subset of the footprint-periods function,
    which lists the periods of ANY status with their Branch (what the writers lock)."""
    w = _transferred_driver(cur)
    approved = _finalized_period(cur, w, w["branches"][0], w["source"], status="Approved",
                                 start="2020-03-02", end="2020-03-08")
    locked = _finalized_period(cur, w, w["branches"][1], w["destination"], status="Locked",
                               start="2021-03-01", end="2021-03-07")
    cur.execute("""
        SELECT payrollperiodid, branchid, status
        FROM payroll.fn_DriverPayrollPeriodsForFootprint(%s, %s, '[2000-01-01,)'::daterange, 'WorkDate')
        ORDER BY startdate
    """, (w["company"], w["driver"]))
    assert cur.fetchall() == [
        (approved, w["branches"][0], "Approved"), (locked, w["branches"][1], "Locked")]
    assert [row[0] for row in _touched(cur, w, "[2000-01-01,)", "WorkDate")] == [locked]
    cur.execute("""
        SELECT payrollperiodid FROM payroll.fn_DriverPayrollPeriodsForFootprint(
            %s, %s, '[2020-03-05,2020-03-05]'::daterange, 'PeriodStart')
    """, (w["company"], w["driver"]))
    assert cur.fetchall() == []  # the period started before the footprint
    for mode in ("Both", None):
        with rejected(cur, errors.InvalidParameterValue):
            cur.execute("SELECT * FROM payroll.fn_DriverPayrollPeriodsForFootprint(%s, %s, "
                        "'[2020-01-01,)'::daterange, %s)", (w["company"], w["driver"], mode))


def test_finalized_guard_rejects_an_unknown_mode_or_a_null_footprint(cur):
    w = _world(cur)
    for mode in ("Both", "workdate", "", None):
        with rejected(cur, errors.InvalidParameterValue):
            _touched(cur, w, "[2021-01-01,)", mode)
    with rejected(cur, errors.InvalidParameterValue):
        cur.execute("SELECT * FROM payroll.fn_DriverFinalizedPayrollTouched(%s, %s, NULL, 'WorkDate')",
                    (w["company"], w["driver"]))


# ---------------------------------------------------------------------------
# core.trg_DriverCompensationContextWritable
# ---------------------------------------------------------------------------


def test_approve_void_and_end_are_refused_when_the_context_tenure_has_ended(cur):
    w = _world(cur)  # one open assignment from 2020-01-01
    approved = _pending(cur, w, definition=0, start="2020-03-01")
    _approve(cur, w, approved)  # the tenure is current: allowed
    pending = _pending(cur, w, definition=1, start="2020-03-01")
    rule = _pay_rule(cur, w, start="2020-03-01")
    _end_open_assignment_in_the_past(cur, w["assignment"], PAST_END)  # tenure 2020-01-01 .. 2020-09-01

    with refused_as_historical(cur):  # approve from a date inside the ended tenure
        _approve(cur, w, pending)
    with refused_as_historical(cur):  # void
        _void_schedule(cur, w, approved)
    with refused_as_historical(cur):  # supersession tail starts the day after the new end
        _supersede(cur, approved, "2020-03-31")
    with refused_as_historical(cur):  # pay rule: end
        cur.execute("UPDATE payroll.driverpayrules SET status = 'Ended', effectiveto = DATE '2020-04-30' "
                    "WHERE driverpayruleid = %s", (rule,))
    with refused_as_historical(cur):  # pay rule: void
        cur.execute("UPDATE payroll.driverpayrules SET status = 'Voided' WHERE driverpayruleid = %s", (rule,))
    with refused_as_historical(cur):  # pay rule: create from the ended tenure
        _pay_rule(cur, w, rule_type="MaximumPay", start="2020-03-01")
    # No payroll effect: a Pending schedule can be created, edited and discarded for the same dates.
    cur.execute("UPDATE payroll.driverrateschedules SET notes = 'edited' WHERE driverratescheduleid = %s", (pending,))
    cur.execute("UPDATE payroll.driverrateschedulevalues SET amount = 30 WHERE driverratescheduleid = %s", (pending,))
    cur.execute("DELETE FROM payroll.driverrateschedules WHERE driverratescheduleid = %s", (pending,))
    assert cur.rowcount == 1
    created = _pending(cur, w, definition=1, start="2020-04-01")
    cur.execute("DELETE FROM payroll.driverrateschedules WHERE driverratescheduleid = %s", (created,))
    cur.execute("UPDATE payroll.driverpayrules SET notes = 'still editable' WHERE driverpayruleid = %s", (rule,))
    assert cur.rowcount == 1  # a notes-only edit is not a compensation change


def test_the_context_is_the_tenure_of_the_start_date_not_the_current_one(cur):
    w = _transferred_driver(cur)
    in_source = _pending(cur, w, definition=0, start="2020-03-01")
    in_destination = _pending(cur, w, definition=1, start="2021-03-01")
    with refused_as_historical(cur):  # start inside the ended source tenure
        _approve(cur, w, in_source)
    _approve(cur, w, in_destination)  # start inside the current destination tenure
    # A pay rule created earlier in the ended tenure's dates is refused, a later one is allowed.
    with refused_as_historical(cur):
        _pay_rule(cur, w, start="2020-03-01")
    _pay_rule(cur, w, start="2021-03-01")
    rule = _pay_rule(cur, w, rule_type="MaximumPay", start="2021-03-01")
    cur.execute("UPDATE payroll.driverpayrules SET status = 'Ended', effectiveto = DATE '2021-09-30' "
                "WHERE driverpayruleid = %s", (rule,))
    # Supersession by a later schedule starts at the new schedule's date, in the current tenure.
    successor = _pending(cur, w, definition=1, start="2022-01-01")
    _supersede(cur, in_destination, "2021-12-31")
    _approve(cur, w, successor)


def test_pending_and_scheduled_contexts_are_writable_and_a_missing_context_is_refused(cur):
    # A tenure that has not started yet is a context (pending authority).
    future = _world(cur, assignment=False)
    _assignment(cur, future, future["branches"][0], FUTURE_START)
    _approve(cur, future, _pending(cur, future, start="2099-02-01"))
    _pay_rule(cur, future, start="2099-02-01")
    # Before any tenure there is no context at all.
    with refused_as_historical(cur):
        _approve(cur, future, _pending(cur, future, definition=1, start="2098-01-01"))
    with refused_as_historical(cur):
        _pay_rule(cur, future, rule_type="MaximumPay", start="2098-01-01")
    # A Scheduled transfer: the destination (not yet effective) is the context from its day.
    scheduled = _world(cur, assignment=False)
    _assignment(cur, scheduled, scheduled["branches"][0], PAST_START, FUTURE_START, "Transferred")
    _assignment(cur, scheduled, scheduled["branches"][1], FUTURE_START)
    _approve(cur, scheduled, _pending(cur, scheduled, definition=0, start="2099-03-01"))
    _approve(cur, scheduled, _pending(cur, scheduled, definition=1, start="2050-03-01"))  # source, still current
    # A Driver with no assignment at all has no context for any date.
    nobody = _world(cur, assignment=False)
    with refused_as_historical(cur):
        _pay_rule(cur, nobody)


def test_the_decision_uses_the_wall_clock_inside_a_long_transaction(cur):
    """A tenure that ends while the transaction is open refuses the later write."""
    w = _world(cur)
    today = "core.fn_BusinessDate('UTC', now())"
    cur.execute(f"SELECT {today}")
    day = cur.fetchone()[0].isoformat()
    first = _pending(cur, w, definition=0, start=day)
    second = _pending(cur, w, definition=1, start=day)
    cur.execute("""
        UPDATE core.driverassignments
        SET effectivetoat = clock_timestamp() + INTERVAL '1500 milliseconds', endreason = 'Transferred'
        WHERE driverassignmentid = %s
    """, (w["assignment"],))
    _approve(cur, w, first)  # the boundary is still ahead: allowed
    cur.execute("SELECT pg_sleep(2)")
    cur.execute("SELECT clock_timestamp() >= (SELECT effectivetoat FROM core.driverassignments "
                "WHERE driverassignmentid = %s), now() < (SELECT effectivetoat FROM "
                "core.driverassignments WHERE driverassignmentid = %s)", (w["assignment"], w["assignment"]))
    passed_by_clock, before_by_transaction_start = cur.fetchone()
    assert passed_by_clock and before_by_transaction_start
    with refused_as_historical(cur):  # the tenure has ended by the wall clock
        _approve(cur, w, second)
    with refused_as_historical(cur):
        _void_schedule(cur, w, first)
    with refused_as_historical(cur):
        _pay_rule(cur, w, start=day)
