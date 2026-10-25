"""Shared PostgreSQL fixtures and builders for the P3b target compensation tests.

These tests use independently cloned current-schema databases. Every builder uses unique data,
so tests never need cleanup (approved schedules are intentionally undeletable).
Rate schedules and pay rules are owned by the permanent Driver; ``make_driver`` still
returns the home DriverAssignment id that status rates and payroll rows are keyed by.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import psycopg2
import pytest

TIERS = [(1, 1), (2, 2), (3, None)]


def _seed_user(target: dict[str, Any]) -> None:
    bootstrap = psycopg2.connect(client_encoding="utf-8", **target)
    try:
        bootstrap.autocommit = True
        with bootstrap.cursor() as cur:
            cur.execute("""
                INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended)
                VALUES ('P3BUSERCO', 'P3b users', 'P3b users', 'Active', FALSE)
                RETURNING companyid
            """)
            cur.execute("""
                INSERT INTO sec.users (companyid, username, displayname)
                VALUES (%s, 'p3b_user', 'P3b User')
            """, (cur.fetchone()[0],))
    finally:
        bootstrap.close()


# Module scope is the lifetime this fixture has always had in effect: every test module imports
# it, and pytest registers an imported fixture once per importing module, so each module gets its
# own database whatever scope is declared. Declared "session", each of those per-module instances
# (a database, an administrative connection and, for the engine fixtures built on it, a pool)
# stayed alive until the process ended, so the live PostgreSQL backends grew with every module and
# a full run reached the server's connection limit. At module scope an instance is torn down, and
# its database dropped, when its module is done. No connection is held for its own sake: the
# administrative connections that create and drop the database are opened only for that.
@pytest.fixture(scope="module", name="p3b_dsn")
def p3b_database(schema_databases):
    with schema_databases.database("compensation") as target:
        _seed_user(target)
        yield target


@pytest.fixture(name="cur")
def p3b_cursor(p3b_dsn):
    """Autocommit cursor: each statement is its own transaction."""
    conn = psycopg2.connect(client_encoding="utf-8", **p3b_dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cursor:
            yield cursor
    finally:
        conn.close()


def connect(dsn: dict[str, Any], *, autocommit: bool = False):
    conn = psycopg2.connect(client_encoding="utf-8", **dsn)
    conn.autocommit = autocommit
    return conn


def user_id(cur) -> int:
    cur.execute("SELECT userid FROM sec.users WHERE username = 'p3b_user'")
    return cur.fetchone()[0]


def unique(prefix: str) -> str:
    return f"{prefix}{uuid4().hex[:10]}"


def make_company(cur, currency: str | None = "USD") -> int:
    code = unique("P3B")
    cur.execute("""
    INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended,
                                currencycode)
        VALUES (%s, %s, %s, 'Active', FALSE, %s) RETURNING companyid
    """, (code, code, code, currency))
    company_id = cur.fetchone()[0]
    # A Company starts with its default Status rate column and the rate definition it owns.
    cur.execute("SELECT payroll.fn_EnsureDefaultStatusRateColumn(%s)", (company_id,))
    return company_id


def make_branch(cur, company_id: int, *, is_default: bool = False) -> int:
    code = unique("B")
    cur.execute("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault, timezoneid)
        VALUES (%s, %s, %s, 'Active', %s, 'UTC') RETURNING branchid
    """, (company_id, code, code, is_default))
    return cur.fetchone()[0]


def make_driver_pair(cur, company_id: int, branch_id: int) -> tuple[int, int]:
    """A permanent Driver with one open home assignment: (DriverID, DriverAssignmentID)."""
    cur.execute("""
        INSERT INTO core.employees (companyid, branchid, fullname, employmentstatus)
        VALUES (%s, %s, 'P3b Driver', 'Active') RETURNING employeeid
    """, (company_id, branch_id))
    employee_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO core.drivers (companyid, employeeid, drivernumber, driverstatus)
        VALUES (%s, %s, %s, 'Active') RETURNING driverid
    """, (company_id, employee_id, unique("D")))
    driver_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO core.driverassignments
            (companyid, branchid, employeeid, driverid, effectivefromat)
        VALUES (%s, %s, %s, %s, TIMESTAMPTZ '2000-01-01 00:00:00+00')
        RETURNING driverassignmentid
    """, (company_id, branch_id, employee_id, driver_id))
    return driver_id, cur.fetchone()[0]


def make_driver(cur, company_id: int, branch_id: int) -> int:
    """The home DriverAssignment id of a new permanent Driver."""
    return make_driver_pair(cur, company_id, branch_id)[1]


def make_pay_definition(
    cur, company_id: int, *, code: str | None = None, name: str | None = None,
    method: str = "PerUnit", input_type: str | None = None,
) -> int:
    if input_type is None:
        input_type = "WholeNumber" if method == "OrdinalTier" else "Decimal"
    code = code or unique("PD")
    cur.execute("""
        INSERT INTO payroll.paydefinitions
            (companyid, definitioncode, definitionname, inputtype, calculationmethod)
        VALUES (%s, %s, %s, %s, %s) RETURNING paydefinitionid
    """, (company_id, code, name or code, input_type, method))
    return cur.fetchone()[0]


def make_rate_definition(cur, pay_definition_id: int) -> int:
    cur.execute("""
        INSERT INTO payroll.ratedefinitions (companyid, paydefinitionid, shape)
        SELECT pd.companyid, pd.paydefinitionid,
               payroll.fn_shapeforcalculationmethod(pd.calculationmethod)
        FROM payroll.paydefinitions pd WHERE pd.paydefinitionid = %s
        RETURNING ratedefinitionid
    """, (pay_definition_id,))
    return cur.fetchone()[0]


def add_scalar_component(cur, rate_definition_id: int) -> int:
    cur.execute("""
        INSERT INTO payroll.ratecomponentdefinitions (ratedefinitionid, shape, sequenceno)
        VALUES (%s, 'Scalar', 1) RETURNING ratecomponentdefinitionid
    """, (rate_definition_id,))
    return cur.fetchone()[0]


def add_tiers(cur, rate_definition_id: int, tiers=TIERS) -> list[int]:
    ids = []
    for sequence_no, (ordinal_from, ordinal_to) in enumerate(tiers, start=1):
        cur.execute("""
            INSERT INTO payroll.ratecomponentdefinitions
                (ratedefinitionid, shape, sequenceno, ordinalfrom, ordinalto)
            VALUES (%s, 'OrdinalTierSchedule', %s, %s, %s) RETURNING ratecomponentdefinitionid
        """, (rate_definition_id, sequence_no, ordinal_from, ordinal_to))
        ids.append(cur.fetchone()[0])
    return ids


class Scalar:
    """A Company with one Branch, one or more Drivers and a PerUnit definition."""

    def __init__(self, cur, *, currency: str | None = "USD", driver_count: int = 1):
        self.company_id = make_company(cur, currency)
        self.branch_id = make_branch(cur, self.company_id)
        pairs = [make_driver_pair(cur, self.company_id, self.branch_id)
                 for _ in range(driver_count)]
        self.driver_ids = [pair[0] for pair in pairs]
        self.driver_id = self.driver_ids[0]
        self.driver_assignment_ids = [pair[1] for pair in pairs]
        self.driver_assignment_id = self.driver_assignment_ids[0]
        self.pay_definition_id = make_pay_definition(cur, self.company_id)
        self.rate_definition_id = make_rate_definition(cur, self.pay_definition_id)
        self.component_id = add_scalar_component(cur, self.rate_definition_id)


class Ordinal:
    """A Company with one Branch, one Driver and an OrdinalTier definition."""

    def __init__(self, cur, *, currency: str | None = "USD", tiers=TIERS):
        self.company_id = make_company(cur, currency)
        self.branch_id = make_branch(cur, self.company_id)
        self.driver_id, self.driver_assignment_id = make_driver_pair(
            cur, self.company_id, self.branch_id)
        self.pay_definition_id = make_pay_definition(cur, self.company_id, method="OrdinalTier")
        self.rate_definition_id = make_rate_definition(cur, self.pay_definition_id)
        self.component_ids = add_tiers(cur, self.rate_definition_id, tiers)


def add_driver(cur, fx) -> int:
    """Another permanent Driver of the fixture's Company; returns its DriverID."""
    return make_driver_pair(cur, fx.company_id, fx.branch_id)[0]


def make_pending(
    cur, fx, *, driver_id: int | None = None, effective_from: str = "2026-01-01",
    effective_to: str | None = None, rate_definition_id: int | None = None,
) -> int:
    """A Pending schedule for a Driver; PayDefinitionID comes from the RateDefinition."""
    cur.execute("""
        INSERT INTO payroll.driverrateschedules
            (companyid, driverid, paydefinitionid, ratedefinitionid, effectivefrom, effectiveto)
        SELECT %s, %s, rd.paydefinitionid, rd.ratedefinitionid, %s, %s
        FROM payroll.ratedefinitions rd WHERE rd.ratedefinitionid = %s
        RETURNING driverratescheduleid
    """, (fx.company_id, driver_id or fx.driver_id, effective_from, effective_to,
          rate_definition_id or fx.rate_definition_id))
    return cur.fetchone()[0]


def set_value(cur, schedule_id: int, rate_definition_id: int, component_id: int, amount) -> None:
    cur.execute("""
        INSERT INTO payroll.driverrateschedulevalues
            (driverratescheduleid, ratedefinitionid, ratecomponentdefinitionid, amount)
        VALUES (%s, %s, %s, %s)
    """, (schedule_id, rate_definition_id, component_id, amount))


def approve(cur, schedule_id: int, *, approver: int | None = None) -> None:
    cur.execute("""
        UPDATE payroll.driverrateschedules
           SET status = 'Approved', approvedbyuserid = %s, approvedatutc = NOW()
         WHERE driverratescheduleid = %s
    """, (approver or user_id(cur), schedule_id))


def supersede(cur, schedule_id: int, effective_to: str) -> None:
    cur.execute("""
        UPDATE payroll.driverrateschedules
           SET status = 'Superseded', effectiveto = %s
         WHERE driverratescheduleid = %s
    """, (effective_to, schedule_id))


def approved_scalar(cur, fx, amount="25", **kwargs) -> int:
    schedule_id = make_pending(cur, fx, **kwargs)
    set_value(cur, schedule_id, fx.rate_definition_id, fx.component_id, amount)
    approve(cur, schedule_id)
    return schedule_id


def status_of(cur, schedule_id: int) -> str:
    cur.execute("SELECT status FROM payroll.driverrateschedules WHERE driverratescheduleid = %s",
                (schedule_id,))
    return cur.fetchone()[0]


def structure_locked_at(cur, rate_definition_id: int):
    cur.execute("SELECT structurelockedatutc FROM payroll.ratedefinitions WHERE ratedefinitionid = %s",
                (rate_definition_id,))
    return cur.fetchone()[0]


class Blocked:
    """Run a callable on its own connection and expose whether it is waiting on a lock."""

    def __init__(self, dsn: dict[str, Any], fn: Callable[[Any], None]):
        self.conn = connect(dsn)
        self.error: BaseException | None = None
        self.done = threading.Event()
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            self.pid = cursor.fetchone()[0]

        def target():
            try:
                with self.conn.cursor() as cursor:
                    fn(cursor)
                self.conn.commit()
            except BaseException as exc:  # noqa: BLE001 - surfaced to the test
                self.error = exc
                self.conn.rollback()
            finally:
                self.done.set()

        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()

    def wait_for_lock(self, observer_cur, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            observer_cur.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (self.pid,)
            )
            row = observer_cur.fetchone()
            if row and row[0] == "Lock":
                return
            if self.done.is_set():
                raise AssertionError("statement finished instead of waiting on a lock")
            time.sleep(0.02)
        raise AssertionError("statement never waited on a lock")

    def finish(self, timeout: float = 10.0) -> BaseException | None:
        assert self.done.wait(timeout), "blocked statement did not finish"
        self.thread.join(timeout)
        self.conn.close()
        return self.error
