from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.builders.workforce_db import end_driver_assignment, insert_driver_assignment


async def _insert_permanent_driver(db, company_id: int, employee_id: int, number: str) -> int:
    return (await db.execute(text("""
        INSERT INTO core.drivers (companyid, employeeid, drivernumber, driverstatus)
        VALUES (:cid, :eid, :number, 'Active')
        RETURNING driverid
    """), {"cid": company_id, "eid": employee_id, "number": number})).scalar_one()


async def test_head_timezone_and_temporal_catalog(db_conn: AsyncConnection):
    branch_timezone = await db_conn.execute(text("""
        SELECT data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'core' AND table_name = 'branches' AND column_name = 'timezoneid'
    """))
    assert branch_timezone.one() == ("character varying", "NO", None)

    company_timezone = await db_conn.execute(text("""
        SELECT count(*)
        FROM information_schema.columns
        WHERE table_schema = 'core' AND table_name = 'companies' AND column_name = 'timezonename'
    """))
    assert company_timezone.scalar_one() == 0

    naive_timestamps = await db_conn.execute(text("""
        SELECT count(*)
        FROM information_schema.columns
        WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
          AND data_type = 'timestamp without time zone'
    """))
    assert naive_timestamps.scalar_one() == 0

    temporal_contract = await db_conn.execute(text("""
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE (table_schema = 'core' AND table_name = 'driverassignments'
               AND column_name IN ('effectivefromat', 'effectivetoat'))
           OR (table_schema = 'core' AND table_name = 'drivertransferrequests'
               AND column_name = 'effectiveat')
           OR (table_schema = 'core' AND table_name = 'employees'
               AND column_name IN ('hiredate', 'terminationdate'))
           OR (table_schema = 'payroll' AND table_name = 'payrolldraftlines'
               AND column_name = 'workdate')
           OR (table_schema = 'payroll' AND table_name = 'payrollperiods'
               AND column_name IN ('startdate', 'enddate'))
           OR (table_schema = 'payroll' AND table_name = 'driverrateassignments'
               AND column_name IN ('effectivefrom', 'effectiveto'))
        ORDER BY table_name, column_name
    """))
    observed = {(row["table_name"], row["column_name"]): row["data_type"]
                for row in temporal_contract.mappings().all()}
    assert observed[("driverassignments", "effectivefromat")] == "timestamp with time zone"
    assert observed[("driverassignments", "effectivetoat")] == "timestamp with time zone"
    assert observed[("drivertransferrequests", "effectiveat")] == "timestamp with time zone"
    assert observed[("employees", "hiredate")] == "date"
    assert observed[("employees", "terminationdate")] == "date"
    assert observed[("payrolldraftlines", "workdate")] == "date"
    assert observed[("payrollperiods", "startdate")] == "date"
    assert observed[("payrollperiods", "enddate")] == "date"
    assert observed[("driverrateassignments", "effectivefrom")] == "date"
    assert observed[("driverrateassignments", "effectiveto")] == "date"


async def test_branch_workdate_ranges_use_local_calendar_boundaries(db_conn: AsyncConnection):
    rows = (await db_conn.execute(text("""
        SELECT
          upper(core.fn_WorkDateRange('America/New_York', DATE '2026-03-08', DATE '2026-03-08'))
            - lower(core.fn_WorkDateRange('America/New_York', DATE '2026-03-08', DATE '2026-03-08')) AS spring,
          upper(core.fn_WorkDateRange('America/New_York', DATE '2026-11-01', DATE '2026-11-01'))
            - lower(core.fn_WorkDateRange('America/New_York', DATE '2026-11-01', DATE '2026-11-01')) AS fall,
          upper(core.fn_WorkDateRange('America/Phoenix', DATE '2026-03-08', DATE '2026-03-08'))
            - lower(core.fn_WorkDateRange('America/Phoenix', DATE '2026-03-08', DATE '2026-03-08')) AS phoenix,
          isempty(core.fn_WorkDateRange('Pacific/Apia', DATE '2011-12-30', DATE '2011-12-30')) AS apia_empty,
          lower(core.fn_WorkDateRange('America/Havana', DATE '2026-11-01', DATE '2026-11-01')) AS havana_start,
          upper(core.fn_WorkDateRange('America/Havana', DATE '2026-11-01', DATE '2026-11-01')) AS havana_end
    """))).mappings().one()
    assert rows["spring"].total_seconds() == 23 * 60 * 60
    assert rows["fall"].total_seconds() == 25 * 60 * 60
    assert rows["phoenix"].total_seconds() == 24 * 60 * 60
    assert rows["apia_empty"] is True
    assert rows["havana_start"].isoformat() == "2026-11-01T04:00:00+00:00"
    assert rows["havana_end"].isoformat() == "2026-11-02T05:00:00+00:00"

    occurrences = (await db_conn.execute(text("""
        SELECT count(*)
        FROM (VALUES
          (TIMESTAMPTZ '2026-11-01 04:30:00+00'),
          (TIMESTAMPTZ '2026-11-01 05:30:00+00')
        ) instants(at)
        WHERE instants.at <@ core.fn_WorkDateRange(
          'America/Havana', DATE '2026-11-01', DATE '2026-11-01'
        )
    """))).scalar_one()
    assert occurrences == 2

    before = (await db_conn.execute(text("""
        SELECT lower(core.fn_WorkDateRange('America/New_York', DATE '2026-03-08', DATE '2026-03-08'))
    """))).scalar_one()
    await db_conn.execute(text("SET LOCAL TIME ZONE 'Asia/Tokyo'"))
    after = (await db_conn.execute(text("""
        SELECT lower(core.fn_WorkDateRange('America/New_York', DATE '2026-03-08', DATE '2026-03-08'))
    """))).scalar_one()
    assert before == after


async def test_supported_timezone_catalog_rejects_nonregional_values(db_conn: AsyncConnection):
    accepted = (await db_conn.execute(text("""
        SELECT core.fn_IsSupportedTimeZone('UTC')
           AND core.fn_IsSupportedTimeZone('America/Phoenix')
           AND core.fn_IsSupportedTimeZone('Pacific/Apia')
    """))).scalar_one()
    rejected = (await db_conn.execute(text("""
        SELECT NOT core.fn_IsSupportedTimeZone(NULL)
           AND NOT core.fn_IsSupportedTimeZone('EST')
           AND NOT core.fn_IsSupportedTimeZone('+05:00')
           AND NOT core.fn_IsSupportedTimeZone('Etc/GMT+5')
    """))).scalar_one()
    assert accepted is True
    assert rejected is True


async def test_one_instant_resolves_five_branch_dates_and_branch_lookup_is_tenant_scoped(
    db_conn: AsyncConnection, hq_branch_id: int,
):
    instant = datetime(2026, 10, 18, 4, 30, tzinfo=UTC)
    dates = (await db_conn.execute(text("""
        SELECT core.fn_BusinessDate('America/New_York', CAST(:at AS TIMESTAMPTZ)) AS ny,
               core.fn_BusinessDate('America/Chicago', CAST(:at AS TIMESTAMPTZ)) AS chicago,
               core.fn_BusinessDate('America/Denver', CAST(:at AS TIMESTAMPTZ)) AS denver,
               core.fn_BusinessDate('America/Los_Angeles', CAST(:at AS TIMESTAMPTZ)) AS los_angeles,
               core.fn_BusinessDate('America/Phoenix', CAST(:at AS TIMESTAMPTZ)) AS phoenix
    """), {"at": instant})).mappings().one()
    assert dates["ny"].isoformat() == "2026-10-18"
    assert {dates[name].isoformat() for name in ("chicago", "denver", "los_angeles", "phoenix")} == {
        "2026-10-17"
    }

    company_id = (await db_conn.execute(text(
        "SELECT companyid FROM core.branches WHERE branchid = :bid"
    ), {"bid": hq_branch_id})).scalar_one()
    scoped = (await db_conn.execute(text("""
        SELECT core.fn_BranchToday(:cid, :bid, CAST(:at AS TIMESTAMPTZ)) AS right_tenant,
               core.fn_BranchToday(:wrong_cid, :bid, CAST(:at AS TIMESTAMPTZ)) AS wrong_tenant,
               core.fn_BranchToday(:cid, -1, CAST(:at AS TIMESTAMPTZ)) AS missing_branch
    """), {"cid": company_id, "wrong_cid": company_id + 999999,
          "bid": hq_branch_id, "at": instant})).mappings().one()
    assert scoped["right_tenant"] is not None
    assert scoped["wrong_tenant"] is None
    assert scoped["missing_branch"] is None


async def test_captured_workdate_zone_survives_branch_zone_edit_for_live_eligibility(
    db_conn: AsyncConnection, hq_branch_id: int,
):
    from app.payroll.eligibility import (
        _assert_driver_eligible_for_workdate_via_snapshot,
        _create_period_driver_eligibility_rows,
    )

    company_id = (await db_conn.execute(text(
        "SELECT companyid FROM core.branches WHERE branchid = :bid"
    ), {"bid": hq_branch_id})).scalar_one()
    branch_id = (await db_conn.execute(text("""
        INSERT INTO core.branches
            (companyid, branchcode, branchname, status, isdefault, timezoneid)
        VALUES (:cid, :code, :name, 'Active', FALSE, 'America/New_York')
        RETURNING branchid
    """), {"cid": company_id, "code": f"TZF-{uuid4().hex[:8]}",
          "name": f"Frozen zone branch {uuid4().hex[:8]}"})).scalar_one()
    work_date = datetime(2026, 10, 18).date()
    suffix = uuid4().hex[:10]
    employee_id = (await db_conn.execute(text("""
        INSERT INTO core.employees
            (companyid, branchid, fullname, employeetype, employmentstatus, hiredate)
        VALUES (:cid, :bid, :name, 'Driver', 'Active', :hire_date)
        RETURNING employeeid
    """), {"cid": company_id, "bid": branch_id,
          "name": f"Frozen zone {suffix}", "hire_date": work_date})).scalar_one()
    permanent_driver_id = await _insert_permanent_driver(
        db_conn, company_id, employee_id, f"TZF-{suffix}",
    )
    # The assignment window is entirely in the Branch-local 2026-10-18 day for New York.
    driver_id = await insert_driver_assignment(
        db_conn, company_id=company_id, branch_id=branch_id, employee_id=employee_id,
        driver_id=permanent_driver_id,
        effective_from_at="2026-10-18T04:30:00+00:00", effective_to_at="2026-10-18T05:00:00+00:00",
        end_reason="Terminated",
    )
    period_id = (await db_conn.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, periodcode, periodname, periodtype, startdate, enddate, status)
        VALUES (:cid, :bid, :code, 'Frozen timezone test', 'Custom', :date, :date, 'Cancelled')
        RETURNING payrollperiodid
    """), {"cid": company_id, "bid": branch_id,
          "code": f"TZF-{suffix}", "date": work_date})).scalar_one()
    try:
        await db_conn.execute(text("""
            INSERT INTO payroll.payrollperiodeligibilitysnapshots
                (payrollperiodid, companyid, branchid, snapshotsource)
            VALUES (:pid, :cid, :bid, 'Generated')
        """), {"pid": period_id, "cid": company_id, "bid": branch_id})
        await _create_period_driver_eligibility_rows(
            period_id, company_id, branch_id, db_conn,
            snapshot_source="Generated", freeze=True,
        )
        captured = (await db_conn.execute(text("""
            SELECT branchtimezoneidsnapshot, eligibleworkdates,
                   drivereffectivefromatsnapshot, drivereffectivetoatsnapshot
            FROM payroll.payrollperioddrivereligibility
            WHERE payrollperiodid = :pid AND driverassignmentid = :did
        """), {"pid": period_id, "did": driver_id})).one()
        assert captured == (
            "America/New_York", [work_date],
            datetime(2026, 10, 18, 4, 30, tzinfo=UTC),
            datetime(2026, 10, 18, 5, 0, tzinfo=UTC),
        )
        # This instant window intersects New York's local day but not Los Angeles's.
        assert (await db_conn.execute(text("""
            SELECT NOT (core.fn_DriverEffectiveRange(
                TIMESTAMPTZ '2026-10-18 04:30:00+00', TIMESTAMPTZ '2026-10-18 05:00:00+00'
            ) && core.fn_WorkDateRange('America/Los_Angeles', :work_date, :work_date))
        """), {"work_date": work_date})).scalar_one() is True
        await db_conn.execute(text(
            "UPDATE core.branches SET timezoneid = 'America/Los_Angeles' WHERE branchid = :bid"
        ), {"bid": branch_id})
        await _assert_driver_eligible_for_workdate_via_snapshot(
            company_id, branch_id, period_id, driver_id, work_date, db_conn,
            allow_existing_source_rescue=False,
        )
        snapshot = (await db_conn.execute(text("""
            SELECT branchtimezoneidsnapshot, eligibleworkdates
            FROM payroll.payrollperioddrivereligibility
            WHERE payrollperiodid = :pid AND driverassignmentid = :did
        """), {"pid": period_id, "did": driver_id})).one()
        assert snapshot == ("America/New_York", [work_date])
    finally:
        await db_conn.execute(text(
            "UPDATE core.branches SET timezoneid = 'America/New_York' WHERE branchid = :bid"
        ), {"bid": branch_id})


async def test_midday_transfer_overlap_allows_each_branch_local_workdate(
    db_conn: AsyncConnection, hq_branch_id: int,
):
    company_id = (await db_conn.execute(text(
        "SELECT companyid FROM core.branches WHERE branchid = :bid"
    ), {"bid": hq_branch_id})).scalar_one()
    suffix = uuid4().hex[:10]
    source_branch_id = (await db_conn.execute(text("""
        INSERT INTO core.branches
            (companyid, branchcode, branchname, status, isdefault, timezoneid)
        VALUES (:cid, :code, :name, 'Active', FALSE, 'America/New_York')
        RETURNING branchid
    """), {"cid": company_id, "code": f"TZN-{suffix}",
          "name": f"New York overlap {suffix}"})).scalar_one()
    target_branch_id = (await db_conn.execute(text("""
        INSERT INTO core.branches
            (companyid, branchcode, branchname, status, isdefault, timezoneid)
        VALUES (:cid, :code, :name, 'Active', FALSE, 'America/Los_Angeles')
        RETURNING branchid
    """), {"cid": company_id, "code": f"TZ-{suffix}",
          "name": f"Los Angeles overlap {suffix}"})).scalar_one()
    employee_id = (await db_conn.execute(text("""
        INSERT INTO core.employees
            (companyid, branchid, fullname, employeetype, employmentstatus, hiredate)
        VALUES (:cid, :bid, :name, 'Driver', 'Active', DATE '2020-01-01')
        RETURNING employeeid
    """), {"cid": company_id, "bid": source_branch_id,
          "name": f"Midday transfer {suffix}"})).scalar_one()
    permanent_driver_id = await _insert_permanent_driver(
        db_conn, company_id, employee_id, f"TZ-{suffix}",
    )
    # One permanent Driver, two consecutive home-Branch assignments sharing the boundary instant.
    source_id = await insert_driver_assignment(
        db_conn, company_id=company_id, branch_id=source_branch_id, employee_id=employee_id,
        driver_id=permanent_driver_id,
        effective_from_at="2026-11-17T05:00:00+00:00", effective_to_at="2026-11-17T08:30:00+00:00",
        end_reason="Transferred",
    )
    destination_id = await insert_driver_assignment(
        db_conn, company_id=company_id, branch_id=target_branch_id, employee_id=employee_id,
        driver_id=permanent_driver_id, effective_from_at="2026-11-17T08:30:00+00:00",
        transferred_from_assignment_id=source_id,
    )
    await end_driver_assignment(
        db_conn, source_id, ended_at="2026-11-17T08:30:00+00:00", end_reason="Transferred",
        transferred_to_assignment_id=destination_id,
    )
    overlap = (await db_conn.execute(text("""
        SELECT core.fn_DriverAssignmentCoversWorkDate(:cid, :source, :source_branch, DATE '2026-11-17') AS source_day,
               core.fn_DriverAssignmentCoversWorkDate(:cid, :destination, :target_branch, DATE '2026-11-17') AS target_day
    """), {"cid": company_id, "source": source_id, "source_branch": source_branch_id,
          "destination": destination_id, "target_branch": target_branch_id})).mappings().one()
    assert overlap["source_day"] is True
    assert overlap["target_day"] is True
