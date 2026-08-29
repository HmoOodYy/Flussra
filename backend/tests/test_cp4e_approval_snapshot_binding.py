"""CP-4E review decisions bind to CP-4D's immutable submitted packet."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import app.payroll.service as payroll_service
from app.payroll.service import (
    _CalculationPacketDriverTotal,
    _CalculationPacketLine,
    _capture_calculation_snapshot,
    _LiveCalculationPacket,
    resubmit_period,
)
from app.review.schemas import ReviewDecide
from app.review.service import decide_review_item, get_review_item_payroll_snapshot


@pytest_asyncio.fixture
async def cp4e_db(test_database_url):
    """Create mutable setup rows, then rollback all immutable test history."""
    marker = uuid4().hex
    engine = create_async_engine(test_database_url, echo=False)
    ids: dict[str, int] = {}
    try:
        async with engine.begin() as seed:
            tenant = (await seed.execute(text("""
                SELECT c.companyid, b.branchid, u.userid
                FROM core.companies c
                JOIN core.branches b ON b.companyid = c.companyid
                JOIN sec.users u ON u.companyid = c.companyid
                WHERE c.companycode = 'DEMO' AND b.branchcode = 'HQ' AND u.username = 'admin'
            """))).mappings().one()
            ids.update({
                "company_id": int(tenant["companyid"]),
                "branch_id": int(tenant["branchid"]),
                "user_id": int(tenant["userid"]),
            })
            employee_id = (await seed.execute(text("""
                INSERT INTO core.employees
                    (companyid, branchid, fullname, employeetype, employmentstatus, createdbyuserid)
                VALUES (:company_id, :branch_id, :name, 'Driver', 'Active', :user_id)
                RETURNING employeeid
            """), {**ids, "name": f"CP4E Employee {marker}"})).scalar_one()
            driver_id = (await seed.execute(text("""
                INSERT INTO core.drivers (companyid, branchid, employeeid, drivercode, driverstatus)
                VALUES (:company_id, :branch_id, :employee_id, :code, 'Active')
                RETURNING driverid
            """), {**ids, "employee_id": employee_id, "code": f"CP4E-{marker[:20]}"})).scalar_one()
            period_id = (await seed.execute(text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (:company_id, :branch_id, 'Open', :code, :name, 'Week', :start, :end)
                RETURNING payrollperiodid
            """), {
                **ids, "code": f"CP4E-{marker}", "name": f"CP4E {marker}",
                "start": date(2088, 2, 1), "end": date(2088, 2, 7),
            })).scalar_one()
            line_id = (await seed.execute(text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverid, workdate, linetype,
                     quantity, sourcetype, sourceid, status, needsmanagerreview, linescope)
                VALUES (:company_id, :branch_id, :period_id, :driver_id, :work_date,
                        'DailyNote', 1, 'User', :source_id, 'Active', FALSE, 'Daily')
                RETURNING draftlineid
            """), {
                **ids, "period_id": period_id, "driver_id": driver_id,
                "work_date": date(2088, 2, 1), "source_id": f"CP4E:{marker}",
            })).scalar_one()
            ids.update({
                "employee_id": int(employee_id), "driver_id": int(driver_id),
                "period_id": int(period_id), "line_id": int(line_id),
            })

        async with engine.connect() as conn:
            outer = await conn.begin()
            try:
                yield SimpleNamespace(conn=conn, **ids)
            finally:
                if outer.is_active:
                    await outer.rollback()
        async with engine.begin() as cleanup:
            await cleanup.execute(text("DELETE FROM payroll.payrolldraftlines WHERE draftlineid = :id"), {"id": ids["line_id"]})
            await cleanup.execute(text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :id"), {"id": ids["period_id"]})
            await cleanup.execute(text("DELETE FROM core.drivers WHERE driverid = :id"), {"id": ids["driver_id"]})
            await cleanup.execute(text("DELETE FROM core.employees WHERE employeeid = :id"), {"id": ids["employee_id"]})
    finally:
        await engine.dispose()


def _packet(db) -> _LiveCalculationPacket:
    line = _CalculationPacketLine(
        source_type="DraftLine", source_id=str(db.line_id), line_type="Hours", line_scope="Daily",
        work_date=date(2088, 2, 1), driver_id=db.driver_id, quantity=Decimal("2.0000"),
        resolved_rate_amount=Decimal("12.5000"), calculated_amount=Decimal("25.0000"),
        needs_manager_review=False, blocker_reason=None,
        source_evidence={"DraftLineID": db.line_id, "PerUnitCalculationVersion": "cp4a-per-unit-v1"},
    )
    driver = _CalculationPacketDriverTotal(
        driver_id=db.driver_id, driver_code="CP4E", driver_name="CP4E Driver",
        daily_pay=Decimal("25.0000"), status_pay=Decimal("0"), period_pay=Decimal("0"),
        minimum_adjustment=Decimal("0"), maximum_adjustment=Decimal("0"), bonus_total=Decimal("0"),
        expected_pay=Decimal("25.0000"), needs_manager_review=False, blockers=[], lines=[line],
    )
    return _LiveCalculationPacket(
        payroll_period_id=db.period_id, company_id=db.company_id, branch_id=db.branch_id,
        status="Open", blockers=[], warnings=[], drivers=[driver], total_expected_pay=Decimal("25.0000"),
    )


async def _period_for_capture(db, period_id: int):
    row = (await db.conn.execute(text("""
        SELECT payrollperiodid, branchid, periodcode, periodtype, startdate, enddate
        FROM payroll.payrollperiods
        WHERE payrollperiodid = :period_id
    """), {"period_id": period_id})).mappings().one()
    return SimpleNamespace(
        payroll_period_id=row["payrollperiodid"],
        branch_id=row["branchid"],
        period_code=row["periodcode"],
        period_type=row["periodtype"],
        start_date=row["startdate"],
        end_date=row["enddate"],
    )


async def _submitted_review(db, *, period_id: int | None = None, snapshot_id: int | None = None) -> tuple[int, int]:
    period_id = db.period_id if period_id is None else period_id
    if snapshot_id is None:
        period = await _period_for_capture(db, period_id)
        snapshot_id = await _capture_calculation_snapshot(
            period=period, company_id=db.company_id, user_id=db.user_id,
            packet=_packet(db), db=db.conn, context="Submit",
        )
    review_id = (await db.conn.execute(text("""
        INSERT INTO review.managerreviewitems
            (companyid, branchid, requestedbyuserid, requesttype, entityschema, entityname,
             entityid, title, description, priority, status, payrollcalculationsnapshotid)
        VALUES (:company_id, :branch_id, :user_id, 'PeriodApproval', 'payroll', 'PayrollPeriods',
                :period_id, 'CP4E submitted packet', 'Immutable packet review.', 'Normal', 'Pending', :snapshot_id)
        RETURNING reviewitemid
    """), {**db.__dict__, "period_id": str(period_id), "snapshot_id": snapshot_id})).scalar_one()
    await db.conn.execute(text("""
        UPDATE payroll.payrollperiods SET status = 'InReview'
        WHERE payrollperiodid = :period_id
    """), {"period_id": period_id})
    return int(review_id), int(snapshot_id)


async def _legacy_pending_review(db) -> int:
    review_id = (await db.conn.execute(text("""
        INSERT INTO review.managerreviewitems
            (companyid, branchid, requestedbyuserid, requesttype, entityschema, entityname,
             entityid, title, description, priority, status)
        VALUES (:company_id, :branch_id, :user_id, 'PeriodApproval', 'payroll', 'PayrollPeriods',
                :period_id, 'Legacy review', 'No snapshot.', 'Normal', 'Pending')
        RETURNING reviewitemid
    """), {**db.__dict__, "period_id": str(db.period_id)})).scalar_one()
    await db.conn.execute(text("UPDATE payroll.payrollperiods SET status = 'InReview' WHERE payrollperiodid = :period_id"), {"period_id": db.period_id})
    return int(review_id)


@pytest.mark.asyncio
async def test_snapshot_linked_pending_approval_keeps_exact_snapshot_and_creates_no_final_lines(cp4e_db):
    review_id, snapshot_id = await _submitted_review(cp4e_db)
    result = await decide_review_item(review_id, cp4e_db.company_id, cp4e_db.user_id, ReviewDecide(decision="Approved"), cp4e_db.conn)
    assert result.status == "Approved"
    row = (await cp4e_db.conn.execute(text("""
        SELECT status, payrollcalculationsnapshotid FROM review.managerreviewitems WHERE reviewitemid = :review_id
    """), {"review_id": review_id})).mappings().one()
    assert row["payrollcalculationsnapshotid"] == snapshot_id
    assert (await cp4e_db.conn.execute(text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :period_id"), {"period_id": cp4e_db.period_id})).scalar_one() == "Approved"
    assert (await cp4e_db.conn.execute(text("SELECT COUNT(*) FROM payroll.payrollcalculationsnapshots WHERE payrollperiodid = :period_id"), {"period_id": cp4e_db.period_id})).scalar_one() == 1
    assert (await cp4e_db.conn.execute(text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :period_id"), {"period_id": cp4e_db.period_id})).scalar_one() == 0
    audit = (await cp4e_db.conn.execute(text("""
        SELECT newvaluejson FROM audit.auditlog
        WHERE entityschema = 'review' AND entityname = 'ManagerReviewItems'
          AND entityid = :review_id AND actioncode = 'REVIEW_ITEM_DECIDED'
        ORDER BY auditid DESC LIMIT 1
    """), {"review_id": str(review_id)})).scalar_one()
    audit_value = json.loads(audit)
    assert audit_value["payroll_calculation_snapshot_id"] == snapshot_id
    assert audit_value["revision_number"] == 1


@pytest.mark.asyncio
async def test_snapshot_read_and_approval_ignore_live_draft_line_drift(cp4e_db, monkeypatch):
    review_id, snapshot_id = await _submitted_review(cp4e_db)
    await cp4e_db.conn.execute(text("""
        UPDATE payroll.payrolldraftlines SET needsmanagerreview = TRUE, calculatedamount = 9999
        WHERE draftlineid = :line_id
    """), {"line_id": cp4e_db.line_id})
    packet = await get_review_item_payroll_snapshot(review_id, cp4e_db.company_id, cp4e_db.user_id, cp4e_db.conn)
    assert packet.total_expected_pay == Decimal("25.0000")
    assert packet.driver_totals[0].expected_pay == Decimal("25.0000")
    assert packet.lines[0].calculated_amount == Decimal("25.0000")

    async def _unexpected_live_calculation(*_args, **_kwargs):
        raise AssertionError("approval must not rebuild live payroll calculations")

    monkeypatch.setattr(payroll_service, "_build_live_calculation_packet", _unexpected_live_calculation)
    await decide_review_item(review_id, cp4e_db.company_id, cp4e_db.user_id, ReviewDecide(decision="Approved"), cp4e_db.conn)
    assert (await cp4e_db.conn.execute(text("SELECT payrollcalculationsnapshotid FROM review.managerreviewitems WHERE reviewitemid = :review_id"), {"review_id": review_id})).scalar_one() == snapshot_id


@pytest.mark.asyncio
async def test_snapshot_read_returns_only_review_item_linked_packet(cp4e_db):
    review_id, snapshot_id = await _submitted_review(cp4e_db)
    packet = await get_review_item_payroll_snapshot(review_id, cp4e_db.company_id, cp4e_db.user_id, cp4e_db.conn)
    assert packet.review_item_id == review_id
    assert packet.payroll_period_id == cp4e_db.period_id
    assert packet.revision_number == 1
    assert packet.total_expected_pay == Decimal("25.0000")
    assert [line.driver_id for line in packet.lines] == [cp4e_db.driver_id]
    assert snapshot_id > 0


@pytest.mark.asyncio
async def test_snapshot_read_cannot_cross_company_review_item_scope(cp4e_db):
    review_id, _ = await _submitted_review(cp4e_db)
    with pytest.raises(HTTPException, match="not found"):
        await get_review_item_payroll_snapshot(
            review_id,
            cp4e_db.company_id + 999,
            cp4e_db.user_id,
            cp4e_db.conn,
        )


@pytest.mark.asyncio
async def test_same_tenant_wrong_period_snapshot_link_fails_closed(cp4e_db):
    snapshot_id = await _capture_calculation_snapshot(
        period=await _period_for_capture(cp4e_db, cp4e_db.period_id),
        company_id=cp4e_db.company_id, user_id=cp4e_db.user_id,
        packet=_packet(cp4e_db), db=cp4e_db.conn, context="Submit",
    )
    other_period = (await cp4e_db.conn.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (:company_id, :branch_id, 'InReview', 'CP4E-other', 'Other', 'Week', :start, :end)
        RETURNING payrollperiodid
    """), {**cp4e_db.__dict__, "start": date(2088, 3, 1), "end": date(2088, 3, 7)})).scalar_one()
    review_id = (await cp4e_db.conn.execute(text("""
        INSERT INTO review.managerreviewitems
            (companyid, branchid, requestedbyuserid, requesttype, entityschema, entityname,
             entityid, title, priority, status, payrollcalculationsnapshotid)
        VALUES (:company_id, :branch_id, :user_id, 'PeriodApproval', 'payroll', 'PayrollPeriods',
                :period_id, 'Wrong packet', 'Normal', 'Pending', :snapshot_id)
        RETURNING reviewitemid
    """), {**cp4e_db.__dict__, "period_id": str(other_period), "snapshot_id": snapshot_id})).scalar_one()
    with pytest.raises(HTTPException, match="does not belong"):
        await decide_review_item(int(review_id), cp4e_db.company_id, cp4e_db.user_id, ReviewDecide(decision="Approved"), cp4e_db.conn)
    assert (await cp4e_db.conn.execute(text("SELECT status FROM review.managerreviewitems WHERE reviewitemid = :review_id"), {"review_id": review_id})).scalar_one() == "Pending"


@pytest.mark.asyncio
async def test_legacy_pending_item_requires_snapshot_for_approval_but_can_return(cp4e_db):
    review_id = await _legacy_pending_review(cp4e_db)
    with pytest.raises(HTTPException, match="SNAPSHOT_REQUIRED_FOR_APPROVAL"):
        await decide_review_item(review_id, cp4e_db.company_id, cp4e_db.user_id, ReviewDecide(decision="Approved"), cp4e_db.conn)
    assert (await cp4e_db.conn.execute(text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :period_id"), {"period_id": cp4e_db.period_id})).scalar_one() == "InReview"
    await decide_review_item(review_id, cp4e_db.company_id, cp4e_db.user_id, ReviewDecide(decision="EditRequested", decision_reason="Resubmit required"), cp4e_db.conn)
    assert (await cp4e_db.conn.execute(text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :period_id"), {"period_id": cp4e_db.period_id})).scalar_one() == "Returned"


@pytest.mark.asyncio
async def test_legacy_return_then_cp4d_resubmit_creates_revision_one_that_can_approve(cp4e_db, monkeypatch):
    review_id = await _legacy_pending_review(cp4e_db)
    await decide_review_item(review_id, cp4e_db.company_id, cp4e_db.user_id, ReviewDecide(decision="EditRequested", decision_reason="Resubmit required"), cp4e_db.conn)

    async def _no_op_isolation(_db):
        return None

    monkeypatch.setattr(payroll_service, "_set_submit_transaction_isolation", _no_op_isolation)
    result = await resubmit_period(cp4e_db.company_id, cp4e_db.user_id, cp4e_db.period_id, cp4e_db.conn)
    assert result.status == "InReview"
    new_review = (await cp4e_db.conn.execute(text("""
        SELECT reviewitemid, payrollcalculationsnapshotid
        FROM review.managerreviewitems
        WHERE entityid = :period_id AND requesttype = 'PeriodApproval' AND status = 'Pending'
    """), {"period_id": str(cp4e_db.period_id)})).mappings().one()
    revision = (await cp4e_db.conn.execute(text("""
        SELECT revisionnumber FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid = :snapshot_id
    """), {"snapshot_id": new_review["payrollcalculationsnapshotid"]})).scalar_one()
    assert revision == 1
    await decide_review_item(int(new_review["reviewitemid"]), cp4e_db.company_id, cp4e_db.user_id, ReviewDecide(decision="Approved"), cp4e_db.conn)


@pytest.mark.asyncio
async def test_legacy_null_snapshot_cannot_be_read_as_submitted_financial_packet(cp4e_db):
    review_id = await _legacy_pending_review(cp4e_db)
    with pytest.raises(HTTPException, match="SNAPSHOT_REQUIRED_FOR_APPROVAL"):
        await get_review_item_payroll_snapshot(review_id, cp4e_db.company_id, cp4e_db.user_id, cp4e_db.conn)


@pytest.mark.asyncio
async def test_period_not_inreview_cannot_approve_snapshot_linked_item(cp4e_db):
    review_id, _ = await _submitted_review(cp4e_db)
    await cp4e_db.conn.execute(text("UPDATE payroll.payrollperiods SET status = 'Open' WHERE payrollperiodid = :period_id"), {"period_id": cp4e_db.period_id})
    with pytest.raises(HTTPException, match="no longer in InReview"):
        await decide_review_item(review_id, cp4e_db.company_id, cp4e_db.user_id, ReviewDecide(decision="Approved"), cp4e_db.conn)


@pytest.mark.asyncio
async def test_historical_returned_item_cannot_approve_newer_pending_packet(cp4e_db):
    old_review_id, old_snapshot_id = await _submitted_review(cp4e_db)
    await decide_review_item(old_review_id, cp4e_db.company_id, cp4e_db.user_id, ReviewDecide(decision="EditRequested", decision_reason="Correct"), cp4e_db.conn)
    await cp4e_db.conn.execute(text("""
        UPDATE payroll.payrollperiods
        SET status = 'InReview', currentreturnreviewitemid = NULL
        WHERE payrollperiodid = :period_id
    """), {"period_id": cp4e_db.period_id})
    new_snapshot_id = await _capture_calculation_snapshot(
        period=await _period_for_capture(cp4e_db, cp4e_db.period_id), company_id=cp4e_db.company_id,
        user_id=cp4e_db.user_id, packet=_packet(cp4e_db), db=cp4e_db.conn, context="Resubmit",
    )
    new_review_id = (await cp4e_db.conn.execute(text("""
        INSERT INTO review.managerreviewitems
            (companyid, branchid, requestedbyuserid, requesttype, entityschema, entityname,
             entityid, title, priority, status, payrollcalculationsnapshotid)
        VALUES (:company_id, :branch_id, :user_id, 'PeriodApproval', 'payroll', 'PayrollPeriods',
                :period_id, 'Resubmitted', 'Normal', 'Pending', :snapshot_id)
        RETURNING reviewitemid
    """), {**cp4e_db.__dict__, "period_id": str(cp4e_db.period_id), "snapshot_id": new_snapshot_id})).scalar_one()
    with pytest.raises(HTTPException, match="require a Pending"):
        await decide_review_item(old_review_id, cp4e_db.company_id, cp4e_db.user_id, ReviewDecide(decision="Approved"), cp4e_db.conn)
    await decide_review_item(int(new_review_id), cp4e_db.company_id, cp4e_db.user_id, ReviewDecide(decision="Approved"), cp4e_db.conn)
    assert old_snapshot_id != new_snapshot_id


@pytest.mark.asyncio
async def test_snapshot_identity_is_in_approval_and_return_audits(cp4e_db):
    review_id, snapshot_id = await _submitted_review(cp4e_db)
    await decide_review_item(review_id, cp4e_db.company_id, cp4e_db.user_id, ReviewDecide(decision="EditRequested", decision_reason="Correct"), cp4e_db.conn)
    audit = (await cp4e_db.conn.execute(text("""
        SELECT newvaluejson FROM audit.auditlog
        WHERE entityschema = 'review' AND entityname = 'ManagerReviewItems'
          AND entityid = :review_id AND actioncode = 'REVIEW_ITEM_DECIDED'
        ORDER BY auditid DESC LIMIT 1
    """), {"review_id": str(review_id)})).scalar_one()
    audit_value = json.loads(audit)
    assert audit_value["payroll_calculation_snapshot_id"] == snapshot_id
    assert audit_value["revision_number"] == 1
    assert audit_value["payroll_period_id"] == cp4e_db.period_id


@pytest.mark.asyncio
async def test_duplicate_approval_is_rejected_without_second_decision(cp4e_db):
    review_id, _ = await _submitted_review(cp4e_db)
    await decide_review_item(review_id, cp4e_db.company_id, cp4e_db.user_id, ReviewDecide(decision="Approved"), cp4e_db.conn)
    with pytest.raises(HTTPException, match="Cannot record a decision"):
        await decide_review_item(review_id, cp4e_db.company_id, cp4e_db.user_id, ReviewDecide(decision="Approved"), cp4e_db.conn)
    assert (await cp4e_db.conn.execute(text("SELECT COUNT(*) FROM review.managerreviewdecisions WHERE reviewitemid = :review_id"), {"review_id": review_id})).scalar_one() == 1
