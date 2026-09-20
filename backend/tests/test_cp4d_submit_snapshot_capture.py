"""CP-4D immutable submit/resubmit snapshot capture tests.

Snapshot rows are created only inside an outer transaction and removed by
rollback. No immutable trigger bypass or snapshot DELETE is used.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

import app.payroll.period_calculation as period_calculation
import app.payroll.service as payroll_service
from app.payroll.schemas import PeriodStatusChange
from app.payroll.service import (
    _CalculationPacketDriverTotal,
    _CalculationPacketLine,
    _LiveCalculationPacket,
    _capture_calculation_snapshot,
    change_period_status,
    get_calculation_preview,
    resubmit_period,
)
from app.payroll.snapshot_hash import (
    CURRENT_PAYROLL_CALCULATION_VERSION,
    calculate_snapshot_hash,
)


@pytest_asyncio.fixture
async def cp4d_db(test_database_url):
    """Seed mutable source rows, then isolate all immutable rows by rollback."""
    marker = uuid4().hex
    engine = create_async_engine(test_database_url, echo=False)
    seed_ids: dict[str, int] = {}
    try:
        async with engine.begin() as seed:
            tenant = (await seed.execute(text("""
                SELECT c.companyid, b.branchid, u.userid
                FROM core.companies c
                JOIN core.branches b ON b.companyid = c.companyid
                JOIN sec.users u ON u.companyid = c.companyid
                WHERE c.companycode = 'DEMO' AND b.branchcode = 'HQ'
                  AND u.username = 'admin'
            """))).mappings().one()
            seed_ids.update({
                "company_id": int(tenant["companyid"]),
                "branch_id": int(tenant["branchid"]),
                "user_id": int(tenant["userid"]),
            })
            employee_id = (await seed.execute(text("""
                INSERT INTO core.employees
                    (companyid, branchid, fullname, employeetype, employmentstatus, createdbyuserid)
                VALUES (:cid, :bid, :name, 'Driver', 'Active', :uid)
                RETURNING employeeid
            """), {
                "cid": seed_ids["company_id"], "bid": seed_ids["branch_id"],
                "name": f"CP4D Employee {marker}", "uid": seed_ids["user_id"],
            })).scalar_one()
            driver_id = (await seed.execute(text("""
                INSERT INTO core.drivers (companyid, branchid, employeeid, drivercode, driverstatus)
                VALUES (:cid, :bid, :employee_id, :code, 'Active')
                RETURNING driverid
            """), {
                "cid": seed_ids["company_id"], "bid": seed_ids["branch_id"],
                "employee_id": employee_id, "code": f"CP4D-{marker[:20]}",
            })).scalar_one()
            period_id = (await seed.execute(text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (:cid, :bid, 'Open', :code, :name, 'Week', :start, :end)
                RETURNING payrollperiodid
            """), {
                "cid": seed_ids["company_id"], "bid": seed_ids["branch_id"],
                "code": f"CP4D-{marker}", "name": f"CP4D {marker}",
                "start": date(2088, 1, 1), "end": date(2088, 1, 7),
            })).scalar_one()
            line_id = (await seed.execute(text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverid, workdate, linetype,
                     quantity, sourcetype, sourceid, status, needsmanagerreview, linescope)
                VALUES (:cid, :bid, :pid, :did, :work_date, 'DailyNote', 1,
                        'User', :source_id, 'Active', FALSE, 'Daily')
                RETURNING draftlineid
            """), {
                "cid": seed_ids["company_id"], "bid": seed_ids["branch_id"],
                "pid": period_id, "did": driver_id, "work_date": date(2088, 1, 1),
                "source_id": f"CP4D:{marker}",
            })).scalar_one()
            rate_type_id = int((await seed.execute(text("""
                SELECT ratetypeid FROM payroll.ratetypes
                WHERE isactive = TRUE
                ORDER BY ratetypeid
                LIMIT 1
            """))).scalar_one())
            driver_rate_id = (await seed.execute(text("""
                INSERT INTO payroll.driverrates
                    (companyid, branchid, driverid, ratetypeid, amount, effectivefrom,
                     status, createdbyuserid)
                VALUES (:cid, :bid, :did, :rate_type_id, 12.5000, :effective_from,
                        'Approved', :uid)
                RETURNING driverrateid
            """), {
                "cid": seed_ids["company_id"], "bid": seed_ids["branch_id"],
                "did": driver_id, "rate_type_id": rate_type_id,
                "effective_from": date(2088, 1, 1), "uid": seed_ids["user_id"],
            })).scalar_one()
            minimum_rule_id = (await seed.execute(text("""
                INSERT INTO payroll.driverpayrules
                    (companyid, branchid, driverid, ruletype, amount, effectivefrom,
                     status, createdbyuserid)
                VALUES (:cid, :bid, :did, 'MinimumPay', 2.0000, :effective_from,
                        'Active', :uid)
                RETURNING driverpayruleid
            """), {
                "cid": seed_ids["company_id"], "bid": seed_ids["branch_id"],
                "did": driver_id, "effective_from": date(2088, 1, 1),
                "uid": seed_ids["user_id"],
            })).scalar_one()
            maximum_rule_id = (await seed.execute(text("""
                INSERT INTO payroll.driverpayrules
                    (companyid, branchid, driverid, ruletype, amount, effectivefrom,
                     status, createdbyuserid)
                VALUES (:cid, :bid, :did, 'MaximumPay', 1.0000, :effective_from,
                        'Active', :uid)
                RETURNING driverpayruleid
            """), {
                "cid": seed_ids["company_id"], "bid": seed_ids["branch_id"],
                "did": driver_id, "effective_from": date(2088, 1, 1),
                "uid": seed_ids["user_id"],
            })).scalar_one()
            seed_ids.update({
                "employee_id": int(employee_id), "driver_id": int(driver_id),
                "period_id": int(period_id), "line_id": int(line_id),
                "rate_type_id": rate_type_id, "driver_rate_id": int(driver_rate_id),
                "minimum_rule_id": int(minimum_rule_id),
                "maximum_rule_id": int(maximum_rule_id),
            })

        async with engine.connect() as conn:
            outer = await conn.begin()
            try:
                yield SimpleNamespace(conn=conn, **seed_ids)
            finally:
                if outer.is_active:
                    await outer.rollback()
        async with engine.begin() as cleanup:
            await cleanup.execute(text("""
                DELETE FROM payroll.driverpayrules
                WHERE driverpayruleid IN (:minimum_rule_id, :maximum_rule_id)
            """), seed_ids)
            await cleanup.execute(text("""
                DELETE FROM payroll.driverrates WHERE driverrateid = :driver_rate_id
            """), seed_ids)
            await cleanup.execute(text("DELETE FROM payroll.payrolldraftlines WHERE draftlineid = :id"), {"id": seed_ids["line_id"]})
            await cleanup.execute(text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :id"), {"id": seed_ids["period_id"]})
            await cleanup.execute(text("DELETE FROM core.drivers WHERE driverid = :id"), {"id": seed_ids["driver_id"]})
            await cleanup.execute(text("DELETE FROM core.employees WHERE employeeid = :id"), {"id": seed_ids["employee_id"]})
    finally:
        await engine.dispose()


async def _period(db: SimpleNamespace):
    from app.payroll.service import get_period_by_id
    return await get_period_by_id(db.company_id, db.user_id, db.period_id, db.conn)


def _packet(db: SimpleNamespace) -> _LiveCalculationPacket:
    line = _CalculationPacketLine(
        source_type="DraftLine",
        source_id=str(db.line_id),
        line_type="Hours",
        line_scope="Daily",
        work_date=date(2088, 1, 1),
        driver_id=db.driver_id,
        quantity=Decimal("2.0000"),
        resolved_rate_amount=Decimal("12.5000"),
        calculated_amount=Decimal("25.0000"),
        needs_manager_review=False,
        blocker_reason=None,
        rate_type_id=db.rate_type_id,
        driver_rate_id=db.driver_rate_id,
        source_evidence={"DraftLineID": db.line_id, "PerUnitCalculationVersion": "cp4a-per-unit-v1"},
    )
    total = _CalculationPacketDriverTotal(
        driver_id=db.driver_id,
        driver_code="CP4D",
        driver_name="CP4D Driver",
        daily_pay=Decimal("25.0000"),
        status_pay=Decimal("0"),
        period_pay=Decimal("0"),
        minimum_adjustment=Decimal("0"),
        maximum_adjustment=Decimal("0"),
        bonus_total=Decimal("0"),
        expected_pay=Decimal("25.0000"),
        needs_manager_review=False,
        blockers=[],
        lines=[line],
    )
    return _LiveCalculationPacket(
        payroll_period_id=db.period_id,
        company_id=db.company_id,
        branch_id=db.branch_id,
        status="Open",
        blockers=[],
        warnings=[],
        drivers=[total],
        total_expected_pay=Decimal("25.0000"),
    )


def _semantic_packet(db: SimpleNamespace) -> _LiveCalculationPacket:
    """A complete persistence-grade packet covering every CP-4D line family."""
    daily = _CalculationPacketLine(
        source_type="User",
        source_id=f"daily:{db.line_id}",
        snapshot_source_type="DraftLine",
        snapshot_source_id=str(db.line_id),
        line_type="HOURS",
        line_scope="Daily",
        work_date=date(2088, 1, 1),
        driver_id=db.driver_id,
        quantity=Decimal("2.0000"),
        resolved_rate_amount=Decimal("12.5000"),
        calculated_amount=Decimal("25.0000"),
        snapshot_calculated_amount=Decimal("25.0000"),
        needs_manager_review=False,
        blocker_reason=None,
        source_evidence={
            "DraftLineID": db.line_id,
            "RateBehavior": "PerUnit",
            "PerUnitCalculationVersion": "cp4a-per-unit-v1",
            "EffectiveRateDate": date(2088, 1, 1),
        },
    )
    status = _CalculationPacketLine(
        source_type="StatusEntryState",
        source_id="STATUS_LIVE:1:2088-01-02:101",
        snapshot_source_type="StatusEntryState",
        snapshot_source_id="501",
        line_type="STATUS_PAY",
        line_scope="Daily",
        work_date=date(2088, 1, 2),
        driver_id=db.driver_id,
        quantity=Decimal("3.0000"),
        resolved_rate_amount=Decimal("6.0000"),
        calculated_amount=Decimal("18.0000"),
        needs_manager_review=False,
        blocker_reason=None,
        source_evidence={
            "PayrollPeriodDriverDayEntryStateID": 501,
            "StatusKeyID": 101,
            "StatusRateColumnID": 201,
            "HoursValue": Decimal("3.0000"),
            "ResolvedDriverRateID": 301,
        },
    )
    period_pay = _CalculationPacketLine(
        source_type="Manual",
        source_id="period-pay:701",
        snapshot_source_type="DraftLine",
        snapshot_source_id="701",
        line_type="Adjustment",
        line_scope="Period",
        work_date=None,
        driver_id=db.driver_id,
        quantity=None,
        resolved_rate_amount=None,
        calculated_amount=Decimal("10.0000"),
        needs_manager_review=False,
        blocker_reason=None,
        source_evidence={"DraftLineID": 701, "EnteredAmount": Decimal("10.0000")},
    )
    minimum = _CalculationPacketLine(
        source_type="System",
        source_id="801",
        line_type="SYS_MIN_TOPUP",
        line_scope="Period",
        work_date=None,
        driver_id=db.driver_id,
        quantity=Decimal("1.0000"),
        resolved_rate_amount=None,
        calculated_amount=Decimal("2.0000"),
        needs_manager_review=False,
        blocker_reason=None,
        source_evidence={"DriverPayRuleID": db.minimum_rule_id, "RuleType": "MinimumPay"},
    )
    maximum = _CalculationPacketLine(
        source_type="System",
        source_id="802",
        line_type="SYS_MAX_CAP",
        line_scope="Period",
        work_date=None,
        driver_id=db.driver_id,
        quantity=Decimal("1.0000"),
        resolved_rate_amount=None,
        calculated_amount=Decimal("-1.0000"),
        needs_manager_review=False,
        blocker_reason=None,
        source_evidence={"DriverPayRuleID": db.maximum_rule_id, "RuleType": "MaximumPay"},
    )
    bonus = _CalculationPacketLine(
        source_type="BonusEvent",
        source_id="901",
        line_type="BONUS",
        line_scope="Period",
        work_date=None,
        driver_id=db.driver_id,
        quantity=None,
        resolved_rate_amount=None,
        calculated_amount=Decimal("5.0000"),
        needs_manager_review=False,
        blocker_reason=None,
        source_evidence={"PayrollBonusEventID": 901, "Status": "Active", "Amount": Decimal("5.0000")},
    )
    total = _CalculationPacketDriverTotal(
        driver_id=db.driver_id,
        driver_code="CP4D",
        driver_name="CP4D Driver",
        daily_pay=Decimal("25.0000"),
        status_pay=Decimal("18.0000"),
        period_pay=Decimal("10.0000"),
        minimum_adjustment=Decimal("2.0000"),
        maximum_adjustment=Decimal("-1.0000"),
        bonus_total=Decimal("5.0000"),
        expected_pay=Decimal("59.0000"),
        needs_manager_review=False,
        blockers=[],
        lines=[daily, status, period_pay, minimum, maximum, bonus],
    )
    return _LiveCalculationPacket(
        payroll_period_id=db.period_id,
        company_id=db.company_id,
        branch_id=db.branch_id,
        status="Open",
        blockers=[],
        warnings=[],
        drivers=[total],
        total_expected_pay=Decimal("59.0000"),
    )


async def _snapshot_count(db: SimpleNamespace) -> int:
    return int((await db.conn.execute(text("""
        SELECT COUNT(*) FROM payroll.payrollcalculationsnapshots WHERE payrollperiodid = :pid
    """), {"pid": db.period_id})).scalar_one())


async def _mark_submission_returned(db: SimpleNamespace) -> tuple[int, int, str]:
    """Create the historical review state required by a real Resubmit call."""
    review = (await db.conn.execute(text("""
        SELECT reviewitemid, payrollcalculationsnapshotid
        FROM review.managerreviewitems
        WHERE entityid = :pid AND requesttype = 'PeriodApproval' AND status = 'Pending'
    """), {"pid": str(db.period_id)})).mappings().one()
    old_hash = (await db.conn.execute(text("""
        SELECT snapshothash FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid = :snapshot_id
    """), {"snapshot_id": review["payrollcalculationsnapshotid"]})).scalar_one()
    await db.conn.execute(text("""
        UPDATE review.managerreviewitems SET status = 'Rejected' WHERE reviewitemid = :review_id
    """), {"review_id": review["reviewitemid"]})
    await db.conn.execute(text("""
        UPDATE payroll.payrollperiods
        SET status = 'Returned', currentreturnreviewitemid = :review_id
        WHERE payrollperiodid = :period_id
    """), {"review_id": review["reviewitemid"], "period_id": db.period_id})
    return int(review["reviewitemid"]), int(review["payrollcalculationsnapshotid"]), str(old_hash)


async def _persisted_hash_totals(
    conn: AsyncConnection,
    snapshot_id: int,
) -> list[dict[str, object]]:
    """Rebuild the hash projection only from persisted immutable rows."""
    totals = (await conn.execute(text("""
        SELECT payrollcalculationdrivertotalid, driverid, drivercodesnapshot,
               drivernamesnapshot, dailypay, statuspay, periodpay,
               minimumadjustment, maximumadjustment, bonustotal, expectedpay
        FROM payroll.payrollcalculationdrivertotals
        WHERE payrollcalculationsnapshotid = :snapshot_id
        ORDER BY driverid
    """), {"snapshot_id": snapshot_id})).mappings().all()
    result: list[dict[str, object]] = []
    for total in totals:
        lines = (await conn.execute(text("""
            SELECT sourcetype, sourceid, linetype, linescope, workdate,
                   payitemid, ratetypeid, driverrateid, bonuseventid, quantity,
                   resolvedrateamount, calculatedamount, sourceevidencejsonb
            FROM payroll.payrollcalculationsnapshotlines
            WHERE payrollcalculationdrivertotalid = :driver_total_id
        """), {"driver_total_id": total["payrollcalculationdrivertotalid"]})).mappings().all()
        result.append({
            "DriverID": total["driverid"],
            "DriverCodeSnapshot": total["drivercodesnapshot"],
            "DriverNameSnapshot": total["drivernamesnapshot"],
            "DailyPay": total["dailypay"],
            "StatusPay": total["statuspay"],
            "PeriodPay": total["periodpay"],
            "MinimumAdjustment": total["minimumadjustment"],
            "MaximumAdjustment": total["maximumadjustment"],
            "BonusTotal": total["bonustotal"],
            "ExpectedPay": total["expectedpay"],
            "Lines": [
                {
                    "SourceType": line["sourcetype"],
                    "SourceID": line["sourceid"],
                    "LineType": line["linetype"],
                    "LineScope": line["linescope"],
                    "WorkDate": line["workdate"],
                    "PayItemID": line["payitemid"],
                    "RateTypeID": line["ratetypeid"],
                    "DriverRateID": line["driverrateid"],
                    "BonusEventID": line["bonuseventid"],
                    "Quantity": line["quantity"],
                    "ResolvedRateAmount": line["resolvedrateamount"],
                    "CalculatedAmount": line["calculatedamount"],
                    "SourceEvidenceJSONB": line["sourceevidencejsonb"],
                }
                for line in lines
            ],
        })
    return result


@pytest.mark.asyncio
async def test_submit_valid_zero_creates_snapshot_and_linked_review_item(cp4d_db):
    result = await change_period_status(
        cp4d_db.company_id,
        cp4d_db.user_id,
        cp4d_db.period_id,
        PeriodStatusChange(status="InReview"),
        cp4d_db.conn,
    )
    assert result.status == "InReview"
    snapshot = (await cp4d_db.conn.execute(text("""
        SELECT revisionnumber, totalexpectedpay, calculationversion
        FROM payroll.payrollcalculationsnapshots WHERE payrollperiodid = :pid
    """), {"pid": cp4d_db.period_id})).mappings().one()
    assert snapshot["revisionnumber"] == 1
    assert snapshot["totalexpectedpay"] == Decimal("0.0000")
    assert snapshot["calculationversion"] == CURRENT_PAYROLL_CALCULATION_VERSION
    linked = (await cp4d_db.conn.execute(text("""
        SELECT payrollcalculationsnapshotid FROM review.managerreviewitems
        WHERE entityid = :pid AND requesttype = 'PeriodApproval' AND status = 'Pending'
    """), {"pid": str(cp4d_db.period_id)})).scalar_one()
    assert linked is not None


@pytest.mark.asyncio
async def test_capture_persists_hash_reconciling_driver_total_and_line(cp4d_db):
    period = await _period(cp4d_db)
    snapshot_id = await _capture_calculation_snapshot(
        period=period, company_id=cp4d_db.company_id, user_id=cp4d_db.user_id,
        packet=_packet(cp4d_db), db=cp4d_db.conn, context="Submit",
    )
    header = (await cp4d_db.conn.execute(text("""
        SELECT sourceconfighash, snapshothash, totalexpectedpay
        FROM payroll.payrollcalculationsnapshots WHERE payrollcalculationsnapshotid = :id
    """), {"id": snapshot_id})).mappings().one()
    total = (await cp4d_db.conn.execute(text("""
        SELECT expectedpay FROM payroll.payrollcalculationdrivertotals
        WHERE payrollcalculationsnapshotid = :id
    """), {"id": snapshot_id})).scalar_one()
    amount = (await cp4d_db.conn.execute(text("""
        SELECT calculatedamount FROM payroll.payrollcalculationsnapshotlines sl
        JOIN payroll.payrollcalculationdrivertotals dt
          ON dt.payrollcalculationdrivertotalid = sl.payrollcalculationdrivertotalid
        WHERE dt.payrollcalculationsnapshotid = :id
    """), {"id": snapshot_id})).scalar_one()
    assert header["sourceconfighash"] != header["snapshothash"]
    assert header["totalexpectedpay"] == total == amount == Decimal("25.0000")


@pytest.mark.asyncio
async def test_persisted_snapshot_rows_reconstruct_the_stored_hash(cp4d_db):
    period = await _period(cp4d_db)
    snapshot_id = await _capture_calculation_snapshot(
        period=period, company_id=cp4d_db.company_id, user_id=cp4d_db.user_id,
        packet=_packet(cp4d_db), db=cp4d_db.conn, context="Submit",
    )
    header = (await cp4d_db.conn.execute(text("""
        SELECT revisionnumber, calculationversion, sourceconfighash, snapshothash
        FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid = :snapshot_id
    """), {"snapshot_id": snapshot_id})).mappings().one()
    rebuilt = calculate_snapshot_hash(
        company_id=cp4d_db.company_id,
        branch_id=cp4d_db.branch_id,
        payroll_period_id=cp4d_db.period_id,
        revision_number=header["revisionnumber"],
        calculation_version=header["calculationversion"],
        source_config_hash=header["sourceconfighash"],
        driver_totals=await _persisted_hash_totals(cp4d_db.conn, snapshot_id),
    )
    assert rebuilt == header["snapshothash"]


@pytest.mark.asyncio
async def test_capture_preserves_source_evidence_and_audits_snapshot(cp4d_db):
    period = await _period(cp4d_db)
    snapshot_id = await _capture_calculation_snapshot(
        period=period, company_id=cp4d_db.company_id, user_id=cp4d_db.user_id,
        packet=_packet(cp4d_db), db=cp4d_db.conn, context="Submit",
    )
    evidence = (await cp4d_db.conn.execute(text("""
        SELECT sourceevidencejsonb
        FROM payroll.payrollcalculationsnapshotlines line
        JOIN payroll.payrollcalculationdrivertotals total
          ON total.payrollcalculationdrivertotalid = line.payrollcalculationdrivertotalid
        WHERE total.payrollcalculationsnapshotid = :snapshot_id
    """), {"snapshot_id": snapshot_id})).scalar_one()
    assert evidence == {
        "DraftLineID": cp4d_db.line_id,
        "PerUnitCalculationVersion": "cp4a-per-unit-v1",
    }
    audit_count = (await cp4d_db.conn.execute(text("""
        SELECT COUNT(*)
        FROM audit.auditlog
        WHERE companyid = :company_id
          AND branchid = :branch_id
          AND actioncode = 'CALCULATION_SNAPSHOT_CAPTURED'
          AND entityname = 'PayrollCalculationSnapshots'
          AND entityid = :snapshot_id
    """), {
        "company_id": cp4d_db.company_id,
        "branch_id": cp4d_db.branch_id,
        "snapshot_id": str(snapshot_id),
    })).scalar_one()
    assert audit_count == 1


@pytest.mark.asyncio
async def test_capture_freezes_legacy_fallback_amount_without_changing_preview_shape(cp4d_db):
    """CP-4D stores the fallback amount that CP-4B intentionally leaves null."""
    period = await _period(cp4d_db)
    original = _packet(cp4d_db)
    fallback_line = replace(
        original.drivers[0].lines[0],
        calculated_amount=None,
        snapshot_calculated_amount=Decimal("25.0000"),
    )
    fallback_packet = replace(
        original,
        drivers=[replace(original.drivers[0], lines=[fallback_line])],
    )

    snapshot_id = await _capture_calculation_snapshot(
        period=period, company_id=cp4d_db.company_id, user_id=cp4d_db.user_id,
        packet=fallback_packet, db=cp4d_db.conn, context="Submit",
    )
    amount = (await cp4d_db.conn.execute(text("""
        SELECT line.calculatedamount
        FROM payroll.payrollcalculationsnapshotlines line
        JOIN payroll.payrollcalculationdrivertotals total
          ON total.payrollcalculationdrivertotalid = line.payrollcalculationdrivertotalid
        WHERE total.payrollcalculationsnapshotid = :snapshot_id
    """), {"snapshot_id": snapshot_id})).scalar_one()
    assert amount == Decimal("25.0000")


@pytest.mark.asyncio
async def test_capture_allocates_per_period_revisions_without_surrogate_hash_input(cp4d_db):
    period = await _period(cp4d_db)
    packet = _packet(cp4d_db)
    first = await _capture_calculation_snapshot(
        period=period, company_id=cp4d_db.company_id, user_id=cp4d_db.user_id,
        packet=packet, db=cp4d_db.conn, context="Submit",
    )
    second = await _capture_calculation_snapshot(
        period=period, company_id=cp4d_db.company_id, user_id=cp4d_db.user_id,
        packet=packet, db=cp4d_db.conn, context="Resubmit",
    )
    rows = (await cp4d_db.conn.execute(text("""
        SELECT revisionnumber, snapshothash FROM payroll.payrollcalculationsnapshots
        WHERE payrollperiodid = :pid ORDER BY revisionnumber
    """), {"pid": cp4d_db.period_id})).mappings().all()
    assert first != second
    assert [row["revisionnumber"] for row in rows] == [1, 2]
    assert rows[0]["snapshothash"] != rows[1]["snapshothash"]


@pytest.mark.asyncio
async def test_capture_failure_after_header_insert_rolls_back_all_snapshot_rows(cp4d_db):
    """A child/evidence scope failure cannot leave an immutable orphan header behind."""
    period = await _period(cp4d_db)
    original = _packet(cp4d_db)
    impossible_driver = replace(original.drivers[0], driver_id=999_999_999)
    invalid_packet = replace(original, drivers=[impossible_driver])

    with pytest.raises((HTTPException, IntegrityError)):
        async with cp4d_db.conn.begin_nested():
            await _capture_calculation_snapshot(
                period=period, company_id=cp4d_db.company_id, user_id=cp4d_db.user_id,
                packet=invalid_packet, db=cp4d_db.conn, context="Submit",
            )

    header_count = (await cp4d_db.conn.execute(text("""
        SELECT COUNT(*) FROM payroll.payrollcalculationsnapshots WHERE payrollperiodid = :pid
    """), {"pid": cp4d_db.period_id})).scalar_one()
    total_count = (await cp4d_db.conn.execute(text("""
        SELECT COUNT(*)
        FROM payroll.payrollcalculationdrivertotals total
        JOIN payroll.payrollcalculationsnapshots snapshot
          ON snapshot.payrollcalculationsnapshotid = total.payrollcalculationsnapshotid
        WHERE snapshot.payrollperiodid = :pid
    """), {"pid": cp4d_db.period_id})).scalar_one()
    assert header_count == total_count == 0


@pytest.mark.asyncio
async def test_blocked_packet_creates_no_snapshot(cp4d_db):
    period = await _period(cp4d_db)
    blocked = _LiveCalculationPacket(
        **{**_packet(cp4d_db).__dict__, "blockers": ["missing approved rate"]}
    )
    with pytest.raises(Exception, match="incomplete calculation packet"):
        await _capture_calculation_snapshot(
            period=period, company_id=cp4d_db.company_id, user_id=cp4d_db.user_id,
            packet=blocked, db=cp4d_db.conn, context="Submit",
        )
    count = (await cp4d_db.conn.execute(text("""
        SELECT COUNT(*) FROM payroll.payrollcalculationsnapshots WHERE payrollperiodid = :pid
    """), {"pid": cp4d_db.period_id})).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_unresolved_packet_line_creates_no_snapshot(cp4d_db):
    period = await _period(cp4d_db)
    original = _packet(cp4d_db)
    unresolved_line = replace(original.drivers[0].lines[0], calculated_amount=None)
    unresolved_driver = replace(original.drivers[0], lines=[unresolved_line])
    unresolved = replace(original, drivers=[unresolved_driver])
    with pytest.raises(Exception, match="authoritative calculation line is unresolved"):
        await _capture_calculation_snapshot(
            period=period, company_id=cp4d_db.company_id, user_id=cp4d_db.user_id,
            packet=unresolved, db=cp4d_db.conn, context="Submit",
        )
    count = (await cp4d_db.conn.execute(text("""
        SELECT COUNT(*) FROM payroll.payrollcalculationsnapshots WHERE payrollperiodid = :pid
    """), {"pid": cp4d_db.period_id})).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_live_preview_does_not_capture_a_snapshot(cp4d_db):
    preview = await get_calculation_preview(
        cp4d_db.period_id,
        cp4d_db.company_id,
        cp4d_db.user_id,
        cp4d_db.conn,
    )
    assert preview.status == "Open"
    count = (await cp4d_db.conn.execute(text("""
        SELECT COUNT(*) FROM payroll.payrollcalculationsnapshots WHERE payrollperiodid = :pid
    """), {"pid": cp4d_db.period_id})).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_submit_is_idempotent_after_committed_inreview_transition(cp4d_db):
    result = await change_period_status(
        cp4d_db.company_id, cp4d_db.user_id, cp4d_db.period_id,
        PeriodStatusChange(status="InReview"), cp4d_db.conn,
    )
    assert result.status == "InReview"
    with pytest.raises(HTTPException, match="cannot be transitioned"):
        await change_period_status(
            cp4d_db.company_id, cp4d_db.user_id, cp4d_db.period_id,
            PeriodStatusChange(status="InReview"), cp4d_db.conn,
        )
    assert await _snapshot_count(cp4d_db) == 1


@pytest.mark.asyncio
async def test_returned_resubmit_creates_next_snapshot_and_preserves_old_review_link(cp4d_db, monkeypatch):
    """The submitted packet remains immutable while a Returned period gets revision two."""
    await change_period_status(
        cp4d_db.company_id, cp4d_db.user_id, cp4d_db.period_id,
        PeriodStatusChange(status="InReview"), cp4d_db.conn,
    )
    old_review_id, old_snapshot_id, old_hash = await _mark_submission_returned(cp4d_db)

    # A production resubmission is a new request/transaction.  This focused
    # rollback fixture intentionally keeps both workflow states in one outer
    # transaction, so avoid issuing a second SET TRANSACTION after setup SQL.
    async def _no_op_isolation(_db):
        return None

    monkeypatch.setattr(payroll_service, "_set_submit_transaction_isolation", _no_op_isolation)
    result = await resubmit_period(
        cp4d_db.company_id, cp4d_db.user_id, cp4d_db.period_id, cp4d_db.conn,
    )
    assert result.status == "InReview"

    snapshots = (await cp4d_db.conn.execute(text("""
        SELECT payrollcalculationsnapshotid, revisionnumber, snapshothash
        FROM payroll.payrollcalculationsnapshots
        WHERE payrollperiodid = :pid ORDER BY revisionnumber
    """), {"pid": cp4d_db.period_id})).mappings().all()
    assert [row["revisionnumber"] for row in snapshots] == [1, 2]
    assert snapshots[0]["payrollcalculationsnapshotid"] == old_snapshot_id
    assert snapshots[0]["snapshothash"] == old_hash

    reviews = (await cp4d_db.conn.execute(text("""
        SELECT reviewitemid, status, payrollcalculationsnapshotid
        FROM review.managerreviewitems
        WHERE entityid = :pid AND requesttype = 'PeriodApproval'
        ORDER BY reviewitemid
    """), {"pid": str(cp4d_db.period_id)})).mappings().all()
    old_review = next(row for row in reviews if row["reviewitemid"] == old_review_id)
    new_review = next(row for row in reviews if row["status"] == "Pending")
    assert old_review["payrollcalculationsnapshotid"] == old_snapshot_id
    assert new_review["payrollcalculationsnapshotid"] == snapshots[1]["payrollcalculationsnapshotid"]


@pytest.mark.asyncio
async def test_legacy_returned_period_captures_its_first_snapshot_without_backfill(cp4d_db, monkeypatch):
    legacy_review_id = (await cp4d_db.conn.execute(text("""
        INSERT INTO review.managerreviewitems
            (companyid, branchid, requestedbyuserid, requesttype, entityschema,
             entityname, entityid, title, description, priority, status)
        VALUES
            (:company_id, :branch_id, :user_id, 'PeriodApproval', 'payroll',
             'PayrollPeriods', :period_id, 'Legacy Returned period',
             'Historical review item without a calculation snapshot.', 'Normal', 'Rejected')
        RETURNING reviewitemid
    """), {
        "company_id": cp4d_db.company_id,
        "branch_id": cp4d_db.branch_id,
        "user_id": cp4d_db.user_id,
        "period_id": str(cp4d_db.period_id),
    })).scalar_one()
    await cp4d_db.conn.execute(text("""
        UPDATE payroll.payrollperiods
        SET status = 'Returned', currentreturnreviewitemid = :review_id
        WHERE payrollperiodid = :period_id
    """), {"period_id": cp4d_db.period_id, "review_id": legacy_review_id})

    async def _no_op_isolation(_db):
        return None

    monkeypatch.setattr(payroll_service, "_set_submit_transaction_isolation", _no_op_isolation)
    result = await resubmit_period(
        cp4d_db.company_id, cp4d_db.user_id, cp4d_db.period_id, cp4d_db.conn,
    )
    assert result.status == "InReview"
    revisions = (await cp4d_db.conn.execute(text("""
        SELECT revisionnumber FROM payroll.payrollcalculationsnapshots
        WHERE payrollperiodid = :pid
    """), {"pid": cp4d_db.period_id})).scalars().all()
    assert revisions == [1]


@pytest.mark.asyncio
async def test_capture_persists_daily_status_period_bonus_and_system_evidence(cp4d_db):
    period = await _period(cp4d_db)
    snapshot_id = await _capture_calculation_snapshot(
        period=period, company_id=cp4d_db.company_id, user_id=cp4d_db.user_id,
        packet=_semantic_packet(cp4d_db), db=cp4d_db.conn, context="Submit",
    )
    header = (await cp4d_db.conn.execute(text("""
        SELECT totalexpectedpay FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid = :snapshot_id
    """), {"snapshot_id": snapshot_id})).scalar_one()
    total = (await cp4d_db.conn.execute(text("""
        SELECT dailypay + statuspay + periodpay + minimumadjustment + maximumadjustment + bonustotal
        FROM payroll.payrollcalculationdrivertotals
        WHERE payrollcalculationsnapshotid = :snapshot_id
    """), {"snapshot_id": snapshot_id})).scalar_one()
    lines = (await cp4d_db.conn.execute(text("""
        SELECT line.linetype, line.sourcetype, line.sourceevidencejsonb, line.calculatedamount
        FROM payroll.payrollcalculationsnapshotlines line
        JOIN payroll.payrollcalculationdrivertotals total
          ON total.payrollcalculationdrivertotalid = line.payrollcalculationdrivertotalid
        WHERE total.payrollcalculationsnapshotid = :snapshot_id
        ORDER BY line.linetype
    """), {"snapshot_id": snapshot_id})).mappings().all()
    line_map = {row["linetype"]: row for row in lines}
    assert header == total == Decimal("59.0000")
    assert set(line_map) == {"HOURS", "STATUS_PAY", "Adjustment", "BONUS", "SYS_MIN_TOPUP", "SYS_MAX_CAP"}
    assert line_map["HOURS"]["sourceevidencejsonb"]["PerUnitCalculationVersion"] == "cp4a-per-unit-v1"
    assert line_map["STATUS_PAY"]["sourcetype"] == "StatusEntryState"
    assert line_map["STATUS_PAY"]["sourceevidencejsonb"]["StatusKeyID"] == 101
    assert Decimal(line_map["Adjustment"]["sourceevidencejsonb"]["EnteredAmount"]) == Decimal("10.0000")
    assert line_map["BONUS"]["sourceevidencejsonb"]["Status"] == "Active"
    assert line_map["SYS_MIN_TOPUP"]["calculatedamount"] == Decimal("2.0000")
    assert line_map["SYS_MAX_CAP"]["calculatedamount"] == Decimal("-1.0000")


@pytest.mark.asyncio
async def test_source_config_and_snapshot_hash_change_when_captured_evidence_changes(cp4d_db):
    period = await _period(cp4d_db)
    original = _semantic_packet(cp4d_db)
    first = await _capture_calculation_snapshot(
        period=period, company_id=cp4d_db.company_id, user_id=cp4d_db.user_id,
        packet=original, db=cp4d_db.conn, context="Submit",
    )
    changed_daily = replace(
        original.drivers[0].lines[0],
        source_evidence={**original.drivers[0].lines[0].source_evidence, "EffectiveRateDate": date(2088, 1, 2)},
    )
    changed = replace(original, drivers=[replace(original.drivers[0], lines=[changed_daily, *original.drivers[0].lines[1:]])])
    second = await _capture_calculation_snapshot(
        period=period, company_id=cp4d_db.company_id, user_id=cp4d_db.user_id,
        packet=changed, db=cp4d_db.conn, context="Resubmit",
    )
    hashes = (await cp4d_db.conn.execute(text("""
        SELECT payrollcalculationsnapshotid, sourceconfighash, snapshothash
        FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid IN (:first, :second)
        ORDER BY payrollcalculationsnapshotid
    """), {"first": first, "second": second})).mappings().all()
    assert hashes[0]["sourceconfighash"] != hashes[1]["sourceconfighash"]
    assert hashes[0]["snapshothash"] != hashes[1]["snapshothash"]


@pytest.mark.asyncio
async def test_snapshot_rows_reject_update_and_delete_without_trigger_bypass(cp4d_db):
    period = await _period(cp4d_db)
    snapshot_id = await _capture_calculation_snapshot(
        period=period, company_id=cp4d_db.company_id, user_id=cp4d_db.user_id,
        packet=_packet(cp4d_db), db=cp4d_db.conn, context="Submit",
    )
    for statement in (
        "UPDATE payroll.payrollcalculationsnapshots SET totalexpectedpay = 1 WHERE payrollcalculationsnapshotid = :snapshot_id",
        "DELETE FROM payroll.payrollcalculationsnapshots WHERE payrollcalculationsnapshotid = :snapshot_id",
    ):
        with pytest.raises(IntegrityError):
            async with cp4d_db.conn.begin_nested():
                await cp4d_db.conn.execute(text(statement), {"snapshot_id": snapshot_id})
    assert await _snapshot_count(cp4d_db) == 1


@pytest.mark.asyncio
async def test_submit_rolls_back_snapshot_review_and_status_when_status_audit_fails(cp4d_db, monkeypatch):
    async def _raise_after_status_update(_db, **_kwargs):
        raise RuntimeError("injected period-status audit failure")

    async def _no_op_isolation(_db):
        return None

    monkeypatch.setattr(payroll_service, "_write_period_status_audit", _raise_after_status_update)
    monkeypatch.setattr(payroll_service, "_set_submit_transaction_isolation", _no_op_isolation)
    with pytest.raises(RuntimeError, match="injected period-status audit failure"):
        async with cp4d_db.conn.begin_nested():
            await change_period_status(
                cp4d_db.company_id, cp4d_db.user_id, cp4d_db.period_id,
                PeriodStatusChange(status="InReview"), cp4d_db.conn,
            )
    assert await _snapshot_count(cp4d_db) == 0
    status = (await cp4d_db.conn.execute(text("""
        SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid
    """), {"pid": cp4d_db.period_id})).scalar_one()
    pending = (await cp4d_db.conn.execute(text("""
        SELECT COUNT(*) FROM review.managerreviewitems
        WHERE entityid = :pid AND requesttype = 'PeriodApproval' AND status = 'Pending'
    """), {"pid": str(cp4d_db.period_id)})).scalar_one()
    assert status == "Open"
    assert pending == 0


@pytest.mark.asyncio
async def test_submit_structural_blocker_leaves_no_snapshot_or_transition(cp4d_db, monkeypatch):
    async def _blocker(*_args, **_kwargs):
        return ["duplicate active Daily draft lines"]

    async def _no_op_isolation(_db):
        return None

    # Stage B4-17: _validate_period_can_finalize now lives in
    # app.payroll.period_calculation, and _build_live_calculation_packet
    # (also in period_calculation) resolves it as a bare name through that
    # module's own globals — patching app.payroll.service's compatibility
    # re-export no longer intercepts it. _set_submit_transaction_isolation
    # is unaffected: it is Lifecycle-owned and stays in app.payroll.service.
    monkeypatch.setattr(period_calculation, "_validate_period_can_finalize", _blocker)
    monkeypatch.setattr(payroll_service, "_set_submit_transaction_isolation", _no_op_isolation)
    with pytest.raises(HTTPException, match="incomplete calculation packet"):
        await change_period_status(
            cp4d_db.company_id, cp4d_db.user_id, cp4d_db.period_id,
            PeriodStatusChange(status="InReview"), cp4d_db.conn,
        )
    assert await _snapshot_count(cp4d_db) == 0
    status = (await cp4d_db.conn.execute(text("""
        SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid
    """), {"pid": cp4d_db.period_id})).scalar_one()
    assert status == "Open"


@pytest.mark.asyncio
async def test_submit_needs_manager_review_guard_leaves_no_snapshot(cp4d_db, monkeypatch):
    await cp4d_db.conn.execute(text("""
        UPDATE payroll.payrolldraftlines SET needsmanagerreview = TRUE
        WHERE draftlineid = :line_id
    """), {"line_id": cp4d_db.line_id})

    async def _no_op_isolation(_db):
        return None

    monkeypatch.setattr(payroll_service, "_set_submit_transaction_isolation", _no_op_isolation)
    with pytest.raises(HTTPException, match="require manager review"):
        await change_period_status(
            cp4d_db.company_id, cp4d_db.user_id, cp4d_db.period_id,
            PeriodStatusChange(status="InReview"), cp4d_db.conn,
        )
    assert await _snapshot_count(cp4d_db) == 0


@pytest.mark.asyncio
async def test_submit_unresolved_period_amount_guard_leaves_no_snapshot(cp4d_db, monkeypatch):
    await cp4d_db.conn.execute(text("""
        UPDATE payroll.payrolldraftlines
        SET linetype = 'Adjustment', linescope = 'Period', calculatedamount = NULL,
            needsmanagerreview = FALSE
        WHERE draftlineid = :line_id
    """), {"line_id": cp4d_db.line_id})

    async def _no_op_isolation(_db):
        return None

    monkeypatch.setattr(payroll_service, "_set_submit_transaction_isolation", _no_op_isolation)
    with pytest.raises(HTTPException, match="no resolved calculation amount"):
        await change_period_status(
            cp4d_db.company_id, cp4d_db.user_id, cp4d_db.period_id,
            PeriodStatusChange(status="InReview"), cp4d_db.conn,
        )
    assert await _snapshot_count(cp4d_db) == 0


@pytest.mark.asyncio
async def test_submit_duplicate_pending_review_guard_leaves_no_snapshot(cp4d_db, monkeypatch):
    await cp4d_db.conn.execute(text("""
        INSERT INTO review.managerreviewitems
            (companyid, branchid, requestedbyuserid, requesttype, entityschema,
             entityname, entityid, title, description, priority, status)
        VALUES
            (:company_id, :branch_id, :user_id, 'PeriodApproval', 'payroll',
             'PayrollPeriods', :period_id, 'Existing pending review',
             'Blocks a duplicate submit.', 'Normal', 'Pending')
    """), {
        "company_id": cp4d_db.company_id,
        "branch_id": cp4d_db.branch_id,
        "user_id": cp4d_db.user_id,
        "period_id": str(cp4d_db.period_id),
    })

    async def _no_op_isolation(_db):
        return None

    monkeypatch.setattr(payroll_service, "_set_submit_transaction_isolation", _no_op_isolation)
    with pytest.raises(HTTPException, match="Pending review item already exists"):
        await change_period_status(
            cp4d_db.company_id, cp4d_db.user_id, cp4d_db.period_id,
            PeriodStatusChange(status="InReview"), cp4d_db.conn,
        )
    assert await _snapshot_count(cp4d_db) == 0


@pytest.mark.parametrize("sqlstate", ["40001", "40P01"])
def test_only_serialization_and_deadlock_sqlstates_are_retryable(sqlstate):
    assert payroll_service._is_retryable_transaction_failure(SimpleNamespace(orig=SimpleNamespace(sqlstate=sqlstate)))


@pytest.mark.parametrize("sqlstate", [None, "23505", "XX000"])
def test_unrelated_database_sqlstates_are_not_retryable(sqlstate):
    assert not payroll_service._is_retryable_transaction_failure(SimpleNamespace(orig=SimpleNamespace(sqlstate=sqlstate)))


@pytest.mark.asyncio
async def test_submit_isolation_helper_executes_the_exact_postgresql_command():
    executed: list[str] = []

    class _RecordingConnection:
        async def execute(self, statement):
            executed.append(str(statement))

    await payroll_service._set_submit_transaction_isolation(_RecordingConnection())
    assert executed == ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"]
