"""RP-1 report financial authority resolution is lifecycle- and link-driven."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.payroll.reporting import (
    ReportAuthorityKind,
    resolve_report_financial_authority,
)
from app.payroll.service import (
    _CalculationPacketDriverTotal,
    _CalculationPacketLine,
    _capture_calculation_snapshot,
    _LiveCalculationPacket,
)


@pytest_asyncio.fixture
async def report_authority_db(test_database_url):
    """Commit mutable identity setup; rollback every snapshot-backed period."""
    marker = uuid4().hex
    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.begin() as seed:
            tenant = (await seed.execute(text("""
                SELECT c.companyid, paytest.branchid AS other_branch_id, u.userid
                FROM core.companies c
                JOIN core.branches paytest ON paytest.companyid = c.companyid AND paytest.branchcode = 'PAYTEST'
                JOIN sec.users u ON u.companyid = c.companyid AND u.username = 'admin'
                WHERE c.companycode = 'DEMO'
            """))).mappings().one()
            isolated_branch_id = (await seed.execute(text("""
                INSERT INTO core.branches
                    (companyid, branchcode, branchname, status, isdefault)
                VALUES (:company_id, :branch_code, :branch_name, 'Active', FALSE)
                RETURNING branchid
            """), {
                "company_id": int(tenant["companyid"]),
                "branch_code": f"RP1_{marker}",
                "branch_name": f"RP1 isolated {marker}",
            })).scalar_one()
            ids = {
                "company_id": int(tenant["companyid"]),
                "branch_id": int(isolated_branch_id),
                "other_branch_id": int(tenant["other_branch_id"]),
                "user_id": int(tenant["userid"]),
            }
            employee_id = (await seed.execute(text("""
                INSERT INTO core.employees
                    (companyid, branchid, fullname, employeetype, employmentstatus, createdbyuserid)
                VALUES (:company_id, :branch_id, :name, 'Driver', 'Active', :user_id)
                RETURNING employeeid
            """), {**ids, "name": f"RP1 Employee {marker}"})).scalar_one()
            driver_id = (await seed.execute(text("""
                INSERT INTO core.drivers (companyid, branchid, employeeid, drivercode, driverstatus)
                VALUES (:company_id, :branch_id, :employee_id, :code, 'Active')
                RETURNING driverid
            """), {**ids, "employee_id": employee_id, "code": f"RP1-{marker[:20]}"})).scalar_one()
            ids["employee_id"] = int(employee_id)
            ids["driver_id"] = int(driver_id)

        async with engine.connect() as conn:
            outer = await conn.begin()
            try:
                yield SimpleNamespace(conn=conn, marker=marker, **ids)
            finally:
                if outer.is_active:
                    await outer.rollback()

        async with engine.begin() as cleanup:
            await cleanup.execute(text("DELETE FROM core.drivers WHERE driverid = :driver_id"), ids)
            await cleanup.execute(text("DELETE FROM core.employees WHERE employeeid = :employee_id"), ids)
            await cleanup.execute(text("DELETE FROM core.branches WHERE branchid = :branch_id"), ids)
    finally:
        await engine.dispose()


async def _period(db, status: str) -> int:
    initial_status = "Open" if status == "Returned" else status
    period_id = int((await db.conn.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (:company_id, :branch_id, :status, :code, :name, 'Week', :start, :end)
        RETURNING payrollperiodid
    """), {
        **db.__dict__,
        "status": initial_status,
        "code": f"RP1-{status}-{uuid4().hex}",
        "name": f"RP1 {status}",
        "start": date(2090, 1, 1),
        "end": date(2090, 1, 7),
    })).scalar_one())
    if status == "Returned":
        review_id = await _review_item(
            db, period_id=period_id, status="EditRequested", snapshot_id=None,
        )
        await db.conn.execute(text("""
            UPDATE payroll.payrollperiods
            SET status = 'Returned', currentreturnreviewitemid = :review_id
            WHERE payrollperiodid = :period_id
        """), {"period_id": period_id, "review_id": review_id})
    return period_id


async def _snapshot(db, period_id: int, amount: Decimal) -> int:
    period = (await db.conn.execute(text("""
        SELECT payrollperiodid, branchid, periodcode, periodtype, startdate, enddate
        FROM payroll.payrollperiods WHERE payrollperiodid = :period_id
    """), {"period_id": period_id})).mappings().one()
    line = _CalculationPacketLine(
        source_type="DraftLine", source_id=f"rp1:{period_id}:{amount}", line_type="DailyNote",
        line_scope="Daily", work_date=date(2090, 1, 1), driver_id=db.driver_id,
        quantity=Decimal("1.0000"), resolved_rate_amount=None, calculated_amount=amount,
        needs_manager_review=False, blocker_reason=None, source_evidence={"RP1": True},
    )
    total = _CalculationPacketDriverTotal(
        driver_id=db.driver_id, driver_code="RP1", driver_name="RP1 Driver",
        daily_pay=amount, status_pay=Decimal("0"), period_pay=Decimal("0"),
        minimum_adjustment=Decimal("0"), maximum_adjustment=Decimal("0"), bonus_total=Decimal("0"),
        expected_pay=amount, needs_manager_review=False, blockers=[], lines=[line],
    )
    packet = _LiveCalculationPacket(
        payroll_period_id=period_id, company_id=db.company_id, branch_id=db.branch_id,
        status="Open", blockers=[], warnings=[], drivers=[total], total_expected_pay=amount,
    )
    return await _capture_calculation_snapshot(
        period=SimpleNamespace(
            payroll_period_id=period["payrollperiodid"], branch_id=period["branchid"],
            period_code=period["periodcode"], period_type=period["periodtype"],
            start_date=period["startdate"], end_date=period["enddate"],
        ),
        company_id=db.company_id,
        user_id=db.user_id,
        packet=packet,
        db=db.conn,
        context="RP-1 test",
    )


async def _review_item(db, *, period_id: int, status: str, snapshot_id: int | None) -> int:
    return int((await db.conn.execute(text("""
        INSERT INTO review.managerreviewitems
            (companyid, branchid, requestedbyuserid, requesttype, entityschema, entityname,
             entityid, title, priority, status, payrollcalculationsnapshotid)
        VALUES (:company_id, :branch_id, :user_id, 'PeriodApproval', 'payroll', 'PayrollPeriods',
                :period_id, :title, 'Normal', :status, :snapshot_id)
        RETURNING reviewitemid
    """), {
        **db.__dict__,
        "period_id": str(period_id),
        "status": status,
        "snapshot_id": snapshot_id,
        "title": f"RP1 {status} review",
    })).scalar_one())


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "expected"), [
    ("Draft", ReportAuthorityKind.SOURCE_ONLY),
    ("Open", ReportAuthorityKind.LIVE),
    ("Returned", ReportAuthorityKind.LIVE),
    ("Locked", ReportAuthorityKind.FINAL_LINES),
    ("Archived", ReportAuthorityKind.FINAL_LINES),
    ("Cancelled", ReportAuthorityKind.UNAVAILABLE),
])
async def test_non_snapshot_lifecycle_authority(report_authority_db, status, expected):
    period_id = await _period(report_authority_db, status)
    authority = await resolve_report_financial_authority(
        db=report_authority_db.conn,
        period_id=period_id,
        company_id=report_authority_db.company_id,
        branch_id=report_authority_db.branch_id,
    )
    assert authority.authority_kind is expected
    assert authority.snapshot_id is None
    assert authority.review_item_id is None


@pytest.mark.asyncio
async def test_inreview_uses_exact_pending_review_snapshot_not_latest(report_authority_db):
    period_id = await _period(report_authority_db, "InReview")
    linked_snapshot_id = await _snapshot(report_authority_db, period_id, Decimal("10"))
    newer_snapshot_id = await _snapshot(report_authority_db, period_id, Decimal("20"))
    review_id = await _review_item(
        report_authority_db, period_id=period_id, status="Pending", snapshot_id=linked_snapshot_id,
    )
    authority = await resolve_report_financial_authority(
        db=report_authority_db.conn, period_id=period_id,
        company_id=report_authority_db.company_id, branch_id=report_authority_db.branch_id,
    )
    assert authority.authority_kind is ReportAuthorityKind.SUBMITTED_SNAPSHOT
    assert authority.review_item_id == review_id
    assert authority.snapshot_id == linked_snapshot_id
    assert authority.snapshot_id != newer_snapshot_id
    assert authority.revision_number == 1


@pytest.mark.asyncio
async def test_approved_uses_exact_approved_review_snapshot_not_latest(report_authority_db):
    period_id = await _period(report_authority_db, "Approved")
    linked_snapshot_id = await _snapshot(report_authority_db, period_id, Decimal("10"))
    newer_snapshot_id = await _snapshot(report_authority_db, period_id, Decimal("20"))
    review_id = await _review_item(
        report_authority_db, period_id=period_id, status="Approved", snapshot_id=linked_snapshot_id,
    )
    authority = await resolve_report_financial_authority(
        db=report_authority_db.conn, period_id=period_id,
        company_id=report_authority_db.company_id, branch_id=report_authority_db.branch_id,
    )
    assert authority.authority_kind is ReportAuthorityKind.APPROVED_SNAPSHOT
    assert authority.review_item_id == review_id
    assert authority.snapshot_id == linked_snapshot_id
    assert authority.snapshot_id != newer_snapshot_id
    assert authority.revision_number == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("period_status", "review_status"), [
    ("InReview", "Pending"),
    ("Approved", "Approved"),
])
async def test_snapshot_states_fail_closed_when_review_link_is_legacy_null(report_authority_db, period_status, review_status):
    period_id = await _period(report_authority_db, period_status)
    await _review_item(report_authority_db, period_id=period_id, status=review_status, snapshot_id=None)
    with pytest.raises(HTTPException, match="SNAPSHOT_REQUIRED_FOR_REPORTING"):
        await resolve_report_financial_authority(
            db=report_authority_db.conn, period_id=period_id,
            company_id=report_authority_db.company_id, branch_id=report_authority_db.branch_id,
        )


@pytest.mark.asyncio
async def test_wrong_period_snapshot_link_fails_closed(report_authority_db):
    source_period_id = await _period(report_authority_db, "Open")
    snapshot_id = await _snapshot(report_authority_db, source_period_id, Decimal("10"))
    target_period_id = await _period(report_authority_db, "InReview")
    await _review_item(report_authority_db, period_id=target_period_id, status="Pending", snapshot_id=snapshot_id)
    with pytest.raises(HTTPException, match="REPORT_FINANCIAL_AUTHORITY_INTEGRITY_ERROR"):
        await resolve_report_financial_authority(
            db=report_authority_db.conn, period_id=target_period_id,
            company_id=report_authority_db.company_id, branch_id=report_authority_db.branch_id,
        )


@pytest.mark.asyncio
async def test_ambiguous_approved_review_authority_fails_closed(report_authority_db):
    period_id = await _period(report_authority_db, "Approved")
    first_snapshot_id = await _snapshot(report_authority_db, period_id, Decimal("10"))
    second_snapshot_id = await _snapshot(report_authority_db, period_id, Decimal("20"))
    await _review_item(report_authority_db, period_id=period_id, status="Approved", snapshot_id=first_snapshot_id)
    await _review_item(report_authority_db, period_id=period_id, status="Approved", snapshot_id=second_snapshot_id)
    with pytest.raises(HTTPException, match="REPORT_FINANCIAL_AUTHORITY_INTEGRITY_ERROR"):
        await resolve_report_financial_authority(
            db=report_authority_db.conn, period_id=period_id,
            company_id=report_authority_db.company_id, branch_id=report_authority_db.branch_id,
        )


@pytest.mark.asyncio
async def test_scope_mismatch_never_resolves_another_branch_period(report_authority_db):
    period_id = await _period(report_authority_db, "Open")
    with pytest.raises(HTTPException, match="REPORT_FINANCIAL_AUTHORITY_UNAVAILABLE"):
        await resolve_report_financial_authority(
            db=report_authority_db.conn, period_id=period_id,
            company_id=report_authority_db.company_id, branch_id=report_authority_db.other_branch_id,
        )
    with pytest.raises(HTTPException, match="REPORT_FINANCIAL_AUTHORITY_UNAVAILABLE"):
        await resolve_report_financial_authority(
            db=report_authority_db.conn, period_id=period_id,
            company_id=report_authority_db.company_id + 1000, branch_id=report_authority_db.branch_id,
        )
