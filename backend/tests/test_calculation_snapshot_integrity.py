"""Immutability, tamper detection and database-level integrity of the calculation evidence."""
from __future__ import annotations

from decimal import Decimal

import psycopg2
import pytest
from sqlalchemy import text

from app.payroll.snapshot_verification import SnapshotEvidenceError, verify_snapshot
from tests.calculation_snapshot_helpers import (
    add_bonus,
    add_rule,
    capture,
    enter,
    open_period,
    rows,
    status_pay_setup,
)
from tests.compensation_api_fixtures import (  # noqa: F401 - register fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import (  # noqa: F401 - register fixtures
    p3b_cursor,
    p3b_database,
)
from tests.db_state import TriggerIdentity, suspended_test_triggers
from tests.payroll_runtime_fixtures import build_payroll_tenant, query

pytestmark = pytest.mark.asyncio
D = Decimal

# (table, primary key) of every table holding evidence of a calculation snapshot.
EVIDENCE_TABLES = [
    ("payrollcalculationsnapshots", "payrollcalculationsnapshotid"),
    ("payrollcalculationdrivertotals", "payrollcalculationdrivertotalid"),
    ("payrollcalculationsnapshotcomponents", "payrollcalculationsnapshotcomponentid"),
    ("payrollcalculationsnapshotminmaxevaluations", "payrollcalculationsnapshotminmaxevaluationid"),
    ("payrollcalculationsnapshotminmaxrules", "payrollcalculationsnapshotminmaxruleid"),
    ("payrollcalculationsnapshotlines", "payrollcalculationsnapshotlineid"),
    ("payrollcalculationsnapshotusedratedefinitions", "payrollcalculationsnapshotusedratedefinitionid"),
    ("payrollcalculationsnapshotstatusentries", "payrollcalculationsnapshotstatusentryid"),
    ("payrollcalculationsnapshotbonusevents", "payrollcalculationsnapshotbonuseventid"),
]


@pytest.fixture(name="tenant")
def usd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _full_snapshot(client, engine, tenant):
    """A snapshot with rows in every evidence table: ordinary, Status and Bonus sources and an
    applied minimum."""
    status_pay_setup(tenant, "0.5000")
    period, ppd, _ = await open_period(client, engine, tenant, hire_offset=3, rate="1.0000")
    await enter(client, tenant, period, 3, ppd=ppd, quantity="2")
    await enter(client, tenant, period, 4, status_key="PAID")
    await enter(client, tenant, period, 5, status_key="PAID")
    await add_rule(client, tenant, period, "MinimumPay", "70")
    await add_bonus(client, tenant, period, "1.00")
    return period, await capture(engine, tenant, period)


async def test_every_evidence_table_refuses_update_and_delete(
    p3c_client, p3c_engine, tenant,
):
    _, frozen = await _full_snapshot(p3c_client, p3c_engine, tenant)
    counts = {}
    for table, key in EVIDENCE_TABLES:
        counts[table] = query(tenant, f"SELECT count(*) FROM payroll.{table}")[0][0]
        assert counts[table] > 0, f"{table} has no evidence row to protect"
        for statement in (
            f"UPDATE payroll.{table} SET {key} = {key}",
            f"DELETE FROM payroll.{table}",
        ):
            with pytest.raises(psycopg2.Error) as refused:
                query(tenant, statement)
            assert "payroll_calculation_snapshot_immutable" in str(refused.value), (table, statement)
    for table, _ in EVIDENCE_TABLES:
        assert query(tenant, f"SELECT count(*) FROM payroll.{table}")[0][0] == counts[table]
    assert frozen.snapshot_id


# Each statement alters one persisted fact of the snapshot without touching its stored hash.
TAMPERING = [
    ("header total", "UPDATE payroll.payrollcalculationsnapshots SET totalexpectedpay = totalexpectedpay + 1 "
                     "WHERE payrollcalculationsnapshotid = {snapshot}"),
    ("header scheduled days", "UPDATE payroll.payrollcalculationsnapshots SET scheduledworkdates = '{{}}' "
                              "WHERE payrollcalculationsnapshotid = {snapshot}"),
    ("header warnings", "UPDATE payroll.payrollcalculationsnapshots SET warnings = ARRAY['x'] "
                        "WHERE payrollcalculationsnapshotid = {snapshot}"),
    ("header rounding version", "UPDATE payroll.payrollcalculationsnapshots SET payableroundingversion = 'x' "
                                "WHERE payrollcalculationsnapshotid = {snapshot}"),
    ("driver name", "UPDATE payroll.payrollcalculationdrivertotals SET drivernamesnapshot = 'Mallory' "
                    "WHERE payrollcalculationsnapshotid = {snapshot}"),
    ("component official amount", "UPDATE payroll.payrollcalculationsnapshotcomponents "
                                  "SET officialamount = officialamount + 1 "
                                  "WHERE payrollcalculationsnapshotid = {snapshot} AND componentkind = 'ORDINARY'"),
    ("component definition label", "UPDATE payroll.payrollcalculationsnapshotcomponents "
                                   "SET definitionnamesnapshot = 'Other' "
                                   "WHERE payrollcalculationsnapshotid = {snapshot} AND componentkind = 'ORDINARY'"),
    ("line resolved rate", "UPDATE payroll.payrollcalculationsnapshotlines SET resolvedrateamount = resolvedrateamount + 1 "
                           "WHERE sourcetype = 'DraftLine' AND payrollcalculationdrivertotalid IN "
                           "(SELECT payrollcalculationdrivertotalid FROM payroll.payrollcalculationdrivertotals "
                           "WHERE payrollcalculationsnapshotid = {snapshot})"),
    ("line calculation version", "UPDATE payroll.payrollcalculationsnapshotlines SET calculationversion = 'other' "
                               "WHERE sourcetype = 'DraftLine' AND payrollcalculationdrivertotalid IN "
                               "(SELECT payrollcalculationdrivertotalid FROM payroll.payrollcalculationdrivertotals "
                               "WHERE payrollcalculationsnapshotid = {snapshot})"),
    ("evaluation bonus exclusion", "UPDATE payroll.payrollcalculationsnapshotminmaxevaluations "
                                   "SET excludedbonusamount = excludedbonusamount + 1 "
                                   "WHERE payrollcalculationsnapshotid = {snapshot}"),
    ("rule prorated amount", "UPDATE payroll.payrollcalculationsnapshotminmaxrules SET proratedamount = proratedamount + 1 "
                             "WHERE payrollcalculationsnapshotid = {snapshot}"),
    ("rule calendar", "UPDATE payroll.payrollcalculationsnapshotminmaxrules SET calendaranchorstartdate = "
                      "calendaranchorstartdate + 7 WHERE payrollcalculationsnapshotid = {snapshot}"),
]

IMMUTABILITY_TRIGGERS = [
    TriggerIdentity("payroll", table, f"trg_{table}_immutable") for table, _ in EVIDENCE_TABLES
]


async def test_tampering_with_any_persisted_fact_makes_the_hash_verification_fail_closed(
    p3c_client, p3c_engine, tenant,
):
    _, frozen = await _full_snapshot(p3c_client, p3c_engine, tenant)
    async with p3c_engine.connect() as conn:
        outer = await conn.begin()
        try:
            await verify_snapshot(conn, snapshot_id=frozen.snapshot_id)      # intact
            async with suspended_test_triggers(conn, IMMUTABILITY_TRIGGERS):
                for label, statement in TAMPERING:
                    savepoint = await conn.begin_nested()
                    result = await conn.execute(text(statement.format(snapshot=frozen.snapshot_id)))
                    assert result.rowcount >= 1, label
                    with pytest.raises(SnapshotEvidenceError) as failed:
                        await verify_snapshot(conn, snapshot_id=frozen.snapshot_id)
                    assert failed.value.code == "SNAPSHOT_HASH_MISMATCH", label
                    assert failed.value.http().status_code == 422
                    await savepoint.rollback()
                await verify_snapshot(conn, snapshot_id=frozen.snapshot_id)  # restored
        finally:
            await outer.rollback()


async def test_verification_is_scoped_to_the_snapshot_company_branch_and_period(
    p3c_client, p3c_engine, tenant,
):
    period, frozen = await _full_snapshot(p3c_client, p3c_engine, tenant)
    async with p3c_engine.connect() as conn:
        await verify_snapshot(
            conn, snapshot_id=frozen.snapshot_id, company_id=tenant.company_id,
            branch_id=tenant.branch_a, period_id=period["payroll_period_id"])
        for scope in (
            {"company_id": tenant.company_id + 1},
            {"branch_id": tenant.branch_b},
            {"period_id": period["payroll_period_id"] + 1},
        ):
            with pytest.raises(SnapshotEvidenceError) as refused:
                await verify_snapshot(conn, snapshot_id=frozen.snapshot_id, **scope)
            assert refused.value.code == "SNAPSHOT_NOT_FOUND"
        with pytest.raises(SnapshotEvidenceError) as missing:
            await verify_snapshot(conn, snapshot_id=frozen.snapshot_id + 100000)
        assert missing.value.code == "SNAPSHOT_NOT_FOUND"


# ---------------------------------------------------------------------------
# Database-level constraints
# ---------------------------------------------------------------------------

async def test_a_component_is_unique_per_driver_total_kind_and_identity_and_must_carry_its_identity(
    p3c_client, p3c_engine, tenant,
):
    _, frozen = await _full_snapshot(p3c_client, p3c_engine, tenant)
    ordinary = rows(tenant, """
        SELECT payrollcalculationsnapshotid, companyid, branchid, payrollperiodid,
               payrollcalculationdrivertotalid, payrollperioddefinitionid, paydefinitionid,
               ratedefinitionid
        FROM payroll.payrollcalculationsnapshotcomponents
        WHERE payrollcalculationsnapshotid = %s AND componentkind = 'ORDINARY'
    """, frozen.snapshot_id)[0]
    base = ("INSERT INTO payroll.payrollcalculationsnapshotcomponents "
            "(payrollcalculationsnapshotid, companyid, branchid, payrollperiodid, "
            "payrollcalculationdrivertotalid, componentkind, payrollperioddefinitionid, "
            "paydefinitionid, ratedefinitionid, calculationmethod, calculationmethodversion, "
            "definitioncodesnapshot, definitionnamesnapshot, preroundingamount, officialamount, "
            "sourcecount, unresolvedlinecount) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, 'PerUnit', 1, 'X', 'X', 0, 0, 1, 0)")
    # The same driver total and definition cannot be an ordinary component twice.
    with pytest.raises(psycopg2.errors.UniqueViolation):
        query(tenant, base, (*ordinary[:5], "ORDINARY", *ordinary[5:]))
    # A Bonus component carries no definition identity.
    with pytest.raises(psycopg2.errors.CheckViolation):
        query(tenant, base, (*ordinary[:5], "BONUS", *ordinary[5:]))
    # An unresolved line can never be frozen.
    with pytest.raises(psycopg2.errors.CheckViolation):
        query(tenant, """
            INSERT INTO payroll.payrollcalculationsnapshotcomponents
                (payrollcalculationsnapshotid, companyid, branchid, payrollperiodid,
                 payrollcalculationdrivertotalid, componentkind, preroundingamount, officialamount,
                 sourcecount, unresolvedlinecount)
            VALUES (%s, %s, %s, %s, %s, 'BONUS', 1, 1, 1, 1)
        """, ordinary[:5])


async def test_a_driver_total_must_reconcile_and_a_rule_adjustment_has_a_fixed_sign(
    p3c_client, p3c_engine, tenant,
):
    _, frozen = await _full_snapshot(p3c_client, p3c_engine, tenant)
    scope = rows(tenant, """
        SELECT payrollcalculationsnapshotid, companyid, branchid, driverassignmentid, driverid,
               employeeid
        FROM payroll.payrollcalculationdrivertotals WHERE payrollcalculationsnapshotid = %s
    """, frozen.snapshot_id)[0]
    other = rows(tenant, """
        SELECT a.driverassignmentid, a.driverid, a.employeeid
        FROM core.driverassignments a WHERE a.driverassignmentid = %s
    """, tenant.driver_a2)[0]
    insert = ("INSERT INTO payroll.payrollcalculationdrivertotals "
              "(payrollcalculationsnapshotid, companyid, branchid, driverassignmentid, driverid, "
              "employeeid, dailypay, statuspay, minimumadjustment, maximumadjustment, bonustotal, "
              "expectedpay) VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, %s, 0, %s)")
    head = (scope[0], scope[1], scope[2], other[0], other[1], other[2])
    with pytest.raises(psycopg2.errors.CheckViolation):          # the parts do not add up
        query(tenant, insert, (*head, 10, 0, 0, 11))
    with pytest.raises(psycopg2.errors.CheckViolation):          # a top-up is never negative
        query(tenant, insert, (*head, 10, -1, 0, 9))
    with pytest.raises(psycopg2.errors.CheckViolation):          # a cap is never positive
        query(tenant, insert, (*head, 10, 0, 1, 11))
    # The Employee must be the Driver's own.
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        query(tenant, insert, (scope[0], scope[1], scope[2], other[0], other[1], scope[5], 1, 0, 0, 1))


async def test_min_max_evidence_must_belong_to_the_evaluated_driver_and_selection_date(
    p3c_client, p3c_engine, tenant,
):
    period, frozen = await _full_snapshot(p3c_client, p3c_engine, tenant)
    evaluation = rows(tenant, """
        SELECT e.payrollcalculationsnapshotminmaxevaluationid, e.payrollcalculationdrivertotalid
        FROM payroll.payrollcalculationsnapshotminmaxevaluations e
        WHERE e.payrollcalculationsnapshotid = %s
    """, frozen.snapshot_id)[0]
    # A pay rule of another Driver cannot be recorded against this Driver's evaluation.
    foreign_rule = query(tenant, """
        INSERT INTO payroll.driverpayrules
            (companyid, driverid, ruletype, amount, effectivefrom, status, calendarfrequency,
             calendaranchorstartdate, calendarsourcebranchid, calendarsourcepayrollsetupversionid)
        SELECT %s, %s, 'MaximumPay', 500, DATE '2000-01-03', 'Active', calendarfrequency,
               calendaranchorstartdate, calendarsourcebranchid, calendarsourcepayrollsetupversionid
        FROM payroll.driverpayrules WHERE driverid = %s AND ruletype = 'MinimumPay'
        RETURNING driverpayruleid
    """, (tenant.company_id, tenant.driver_b_id, tenant.driver_a_id))
    assert foreign_rule is not None
    with pytest.raises(psycopg2.Error) as refused:
        query(tenant, """
            INSERT INTO payroll.payrollcalculationsnapshotminmaxrules
                (payrollcalculationsnapshotminmaxevaluationid, payrollcalculationsnapshotid,
                 companyid, branchid, payrollperiodid, payrollcalculationdrivertotalid,
                 driverpayruleid, ruletype, amount, rulestatus, effectivefrom, proratedamount,
                 calendarfrequency, calendaranchorstartdate, calendarsourcebranchid,
                 calendarsourcepayrollsetupversionid)
            SELECT %s, %s, %s, %s, %s, %s, driverpayruleid, 'MaximumPay', 500, 'Active',
                   DATE '2000-01-03', 400, calendarfrequency, calendaranchorstartdate,
                   calendarsourcebranchid, calendarsourcepayrollsetupversionid
            FROM payroll.driverpayrules WHERE driverpayruleid = %s
        """, (evaluation[0], frozen.snapshot_id, tenant.company_id, tenant.branch_a,
              period["payroll_period_id"], evaluation[1], foreign_rule[0][0]))
    assert "snapshot_minmax_rule_scope" in str(refused.value)


_COPY_SNAPSHOT = """
    INSERT INTO payroll.payrollcalculationsnapshots
        (companyid, branchid, payrollperiodid, revisionnumber, calculationversion,
         sourceconfighash, snapshothash, createdbyuserid, totalexpectedpay,
         currencycode, currencyminorunitdigits, periodstartdate, periodenddate,
         payableroundingversion, minmaxprorationversion, scheduledworkdates, warnings)
    SELECT companyid, branchid, payrollperiodid, %s, calculationversion,
           %s, %s, createdbyuserid, 0, currencycode, currencyminorunitdigits, periodstartdate,
           periodenddate, payableroundingversion, minmaxprorationversion, scheduledworkdates,
           warnings
    FROM payroll.payrollcalculationsnapshots WHERE payrollperiodid = %s LIMIT 1
"""


async def test_snapshot_revisions_are_positive_and_unique_per_period_and_hashes_are_sha256_hex(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await _full_snapshot(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]
    good = "a" * 64
    with pytest.raises(psycopg2.errors.UniqueViolation):          # revision 1 already exists
        query(tenant, _COPY_SNAPSHOT, (1, good, good, period_id))
    for revision in (0, -1):
        with pytest.raises(psycopg2.errors.CheckViolation):
            query(tenant, _COPY_SNAPSHOT, (revision, good, good, period_id))
    for bad in ("a" * 63, "A" * 64, "g" * 64):
        for position in (0, 1):
            hashes = [good, good]
            hashes[position] = bad
            with pytest.raises(psycopg2.errors.CheckViolation):
                query(tenant, _COPY_SNAPSHOT, (2, *hashes, period_id))


async def test_a_calculation_snapshot_is_only_valid_under_the_current_calculation_version(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await _full_snapshot(p3c_client, p3c_engine, tenant)
    with pytest.raises(psycopg2.errors.CheckViolation) as refused:
        query(tenant, """
            INSERT INTO payroll.payrollcalculationsnapshots
                (companyid, branchid, payrollperiodid, revisionnumber, calculationversion,
                 sourceconfighash, snapshothash, createdbyuserid, totalexpectedpay,
                 currencycode, currencyminorunitdigits, periodstartdate, periodenddate,
                 payableroundingversion, minmaxprorationversion, scheduledworkdates, warnings)
            SELECT companyid, branchid, payrollperiodid, 99, 'payroll-calculation-v1',
                   sourceconfighash, snapshothash, createdbyuserid, 0, currencycode,
                   currencyminorunitdigits, periodstartdate, periodenddate, payableroundingversion,
                   minmaxprorationversion, scheduledworkdates, warnings
            FROM payroll.payrollcalculationsnapshots WHERE payrollperiodid = %s LIMIT 1
        """, (period["payroll_period_id"],))
    assert "calculationversion" in str(refused.value)
    # Scheduled days must be an ascending set inside the period.
    start = period["start_date"]
    for dates in ("{" + start + "," + start + "}", "{2000-01-01}"):
        with pytest.raises(psycopg2.errors.CheckViolation):
            query(tenant, """
                INSERT INTO payroll.payrollcalculationsnapshots
                    (companyid, branchid, payrollperiodid, revisionnumber, calculationversion,
                     sourceconfighash, snapshothash, createdbyuserid, totalexpectedpay,
                     currencycode, currencyminorunitdigits, periodstartdate, periodenddate,
                     payableroundingversion, minmaxprorationversion, scheduledworkdates, warnings)
                SELECT companyid, branchid, payrollperiodid, 99, calculationversion,
                       sourceconfighash, snapshothash, createdbyuserid, 0, currencycode,
                       currencyminorunitdigits, periodstartdate, periodenddate,
                       payableroundingversion, minmaxprorationversion, %s::date[], warnings
                FROM payroll.payrollcalculationsnapshots WHERE payrollperiodid = %s LIMIT 1
            """, (dates, period["payroll_period_id"]))


async def test_a_final_line_projects_exactly_one_snapshot_component(
    p3c_client, p3c_engine, tenant,
):
    period, frozen = await _full_snapshot(p3c_client, p3c_engine, tenant)
    components = rows(tenant, """
        SELECT c.payrollcalculationsnapshotcomponentid, c.componentkind, c.officialamount,
               c.payrollperioddefinitionid, c.paydefinitionid
        FROM payroll.payrollcalculationsnapshotcomponents c
        WHERE c.payrollcalculationsnapshotid = %s ORDER BY c.payrollcalculationsnapshotcomponentid
    """, frozen.snapshot_id)
    ((driver_id, employee_id, assignment_id),) = rows(tenant, """
        SELECT driverid, employeeid, driverassignmentid
        FROM payroll.payrollcalculationdrivertotals WHERE payrollcalculationsnapshotid = %s
    """, frozen.snapshot_id)
    line_types = {"ORDINARY": "ORDINARY", "STATUS": "STATUS_PAY", "BONUS": "BONUS",
                  "MINIMUM_ADJUSTMENT": "SYS_MIN_TOPUP", "MAXIMUM_ADJUSTMENT": "SYS_MAX_CAP"}

    def insert_final_line(cursor, component, *, amount=None, kind=None, line_type=None):
        cursor.execute("SELECT set_config('app.allow_payroll_final_line_insert', 'true', TRUE)")
        cursor.execute("""
            INSERT INTO payroll.payrollfinallines
                (companyid, branchid, payrollperiodid, driverassignmentid, driverid, employeeid,
                 linetype, linescope, finalamount, sourcetype, sourceid, approvedbyuserid,
                 sourcesnapshot, currencycode, currencyminorunitdigits,
                 payrollcalculationsnapshotid, payrollcalculationsnapshotcomponentid,
                 componentkind, payrollperioddefinitionid, paydefinitionid)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'Period', %s, 'SnapshotComponent', %s, %s,
                    '{}'::jsonb, 'USD', 2, %s, %s, %s, %s, %s)
        """, (tenant.company_id, tenant.branch_a, period["payroll_period_id"], assignment_id,
              driver_id, employee_id, line_type or line_types[kind or component[1]],
              component[2] if amount is None else amount, str(component[0]), tenant.owner,
              frozen.snapshot_id, component[0], kind or component[1], component[3], component[4]))

    conn = psycopg2.connect(client_encoding="utf-8", **tenant.dsn)
    try:
        with conn.cursor() as cursor:
            for component in components:
                insert_final_line(cursor, component)                       # exact projection
            cursor.execute("""
                SELECT sum(finalamount) FROM payroll.payrollfinallines
                WHERE payrollcalculationsnapshotid = %s
            """, (frozen.snapshot_id,))
            assert cursor.fetchone()[0] == sum(c[2] for c in components)
            cursor.execute("SELECT expectedpay FROM payroll.payrollcalculationdrivertotals "
                           "WHERE payrollcalculationsnapshotid = %s", (frozen.snapshot_id,))
            assert cursor.fetchone()[0] == sum(c[2] for c in components)   # the FinalLines sum to ExpectedPay
            for label, kwargs, error in (
                ("one line per component", {}, psycopg2.errors.UniqueViolation),
                ("an unofficial amount", {"amount": components[0][2] + 1}, psycopg2.Error),
                ("another kind", {"kind": "BONUS", "line_type": "BONUS"}, psycopg2.Error),
                ("another line type", {"line_type": "ORDINARY2"}, psycopg2.errors.CheckViolation),
            ):
                cursor.execute("SAVEPOINT probe")
                with pytest.raises(error):
                    insert_final_line(cursor, components[0], **kwargs)
                cursor.execute("ROLLBACK TO SAVEPOINT probe")
            # No final line without the finalization process.
            cursor.execute("SELECT set_config('app.allow_payroll_final_line_insert', 'false', TRUE)")
            cursor.execute("SAVEPOINT probe")
            with pytest.raises(psycopg2.errors.RestrictViolation):
                cursor.execute("""
                    INSERT INTO payroll.payrollfinallines
                        (companyid, branchid, payrollperiodid, driverassignmentid, driverid,
                         employeeid, linetype, linescope, finalamount, sourcetype, sourceid,
                         sourcesnapshot, currencycode, currencyminorunitdigits,
                         payrollcalculationsnapshotid, payrollcalculationsnapshotcomponentid,
                         componentkind)
                    VALUES (%s, %s, %s, %s, %s, %s, 'BONUS', 'Period', 0, 'SnapshotComponent', 'x',
                            '{}'::jsonb, 'USD', 2, %s, %s, 'BONUS')
                """, (tenant.company_id, tenant.branch_a, period["payroll_period_id"],
                      assignment_id, driver_id, employee_id, frozen.snapshot_id, components[-1][0]))
            cursor.execute("ROLLBACK TO SAVEPOINT probe")
    finally:
        conn.rollback()
        conn.close()
