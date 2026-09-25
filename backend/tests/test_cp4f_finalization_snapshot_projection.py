"""CP-4F finalization projects only the approved immutable snapshot packet."""
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

import app.payroll.finalization as payroll_finalization
import app.payroll.service as payroll_service
from app.payroll.service import (
    _CalculationPacketDriverTotal,
    _CalculationPacketLine,
    _capture_calculation_snapshot,
    _LiveCalculationPacket,
    finalize_period,
    get_finalization_preview,
)


@pytest_asyncio.fixture
async def cp4f_db(test_database_url):
    """Commit only mutable setup; rollback the immutable submitted history."""
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
            ids.update({key: int(tenant[key]) for key in ("companyid", "branchid", "userid")})
            ids = {"company_id": ids["companyid"], "branch_id": ids["branchid"], "user_id": ids["userid"]}
            employee_id = (await seed.execute(text("""
                INSERT INTO core.employees
                    (companyid, branchid, fullname, employeetype, employmentstatus, createdbyuserid)
                VALUES (:company_id, :branch_id, :name, 'Driver', 'Active', :user_id)
                RETURNING employeeid
            """), {**ids, "name": f"CP4F Employee {marker}"})).scalar_one()
            driver_id = (await seed.execute(text("""
                INSERT INTO core.drivers (companyid, branchid, employeeid, drivercode, driverstatus)
                VALUES (:company_id, :branch_id, :employee_id, :code, 'Active')
                RETURNING driverid
            """), {**ids, "employee_id": employee_id, "code": f"CP4F-{marker[:20]}"})).scalar_one()
            period_id = (await seed.execute(text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (:company_id, :branch_id, 'Open', :code, :name, 'Week', :start, :end)
                RETURNING payrollperiodid
            """), {**ids, "code": f"CP4F-{marker}", "name": f"CP4F {marker}", "start": date(2089, 1, 1), "end": date(2089, 1, 7)})).scalar_one()
            line_id = (await seed.execute(text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverid, workdate, linetype,
                     quantity, sourcetype, sourceid, status, needsmanagerreview, linescope)
                VALUES (:company_id, :branch_id, :period_id, :driver_id, :work_date,
                        'DailyNote', 1, 'User', :source_id, 'Active', FALSE, 'Daily')
                RETURNING draftlineid
            """), {**ids, "period_id": period_id, "driver_id": driver_id, "work_date": date(2089, 1, 1), "source_id": f"CP4F:{marker}"})).scalar_one()
            minimum_rule_id = (await seed.execute(text("""
                INSERT INTO payroll.driverpayrules
                    (companyid, branchid, driverid, ruletype, amount, effectivefrom,
                     status, createdbyuserid)
                VALUES (:company_id, :branch_id, :driver_id, 'MinimumPay', 2.0000,
                        :effective_from, 'Active', :user_id)
                RETURNING driverpayruleid
            """), {
                **ids, "driver_id": driver_id, "effective_from": date(2089, 1, 1),
            })).scalar_one()
            maximum_rule_id = (await seed.execute(text("""
                INSERT INTO payroll.driverpayrules
                    (companyid, branchid, driverid, ruletype, amount, effectivefrom,
                     status, createdbyuserid)
                VALUES (:company_id, :branch_id, :driver_id, 'MaximumPay', 1.0000,
                        :effective_from, 'Active', :user_id)
                RETURNING driverpayruleid
            """), {
                **ids, "driver_id": driver_id, "effective_from": date(2089, 1, 1),
            })).scalar_one()
            ids.update({
                "employee_id": int(employee_id), "driver_id": int(driver_id),
                "period_id": int(period_id), "line_id": int(line_id),
                "minimum_rule_id": int(minimum_rule_id),
                "maximum_rule_id": int(maximum_rule_id),
            })
        async with engine.connect() as conn:
            outer = await conn.begin()
            try:
                yield SimpleNamespace(conn=conn, **ids)
            finally:
                if outer.is_active:
                    await outer.rollback()
        async with engine.begin() as cleanup:
            await cleanup.execute(text("""
                DELETE FROM payroll.driverpayrules
                WHERE driverpayruleid IN (:minimum_rule_id, :maximum_rule_id)
            """), ids)
            await cleanup.execute(text("DELETE FROM payroll.payrolldraftlines WHERE draftlineid = :id"), {"id": ids["line_id"]})
            await cleanup.execute(text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :id"), {"id": ids["period_id"]})
            await cleanup.execute(text("DELETE FROM core.drivers WHERE driverid = :id"), {"id": ids["driver_id"]})
            await cleanup.execute(text("DELETE FROM core.employees WHERE employeeid = :id"), {"id": ids["employee_id"]})
    finally:
        await engine.dispose()


def _packet(db) -> _LiveCalculationPacket:
    line = _CalculationPacketLine(
        source_type="DraftLine", source_id=str(db.line_id), line_type="DailyNote", line_scope="Daily",
        work_date=date(2089, 1, 1), driver_id=db.driver_id, quantity=Decimal("1.0000"),
        resolved_rate_amount=None, calculated_amount=Decimal("25.0000"), needs_manager_review=False,
        blocker_reason=None, source_evidence={"DraftLineID": db.line_id, "Notes": "Frozen test packet"},
    )
    total = _CalculationPacketDriverTotal(
        driver_id=db.driver_id, driver_code="CP4F", driver_name="CP4F Driver",
        daily_pay=Decimal("25.0000"), status_pay=Decimal("0"), period_pay=Decimal("0"),
        minimum_adjustment=Decimal("0"), maximum_adjustment=Decimal("0"), bonus_total=Decimal("0"),
        expected_pay=Decimal("25.0000"), needs_manager_review=False, blockers=[], lines=[line],
    )
    return _LiveCalculationPacket(
        payroll_period_id=db.period_id, company_id=db.company_id, branch_id=db.branch_id,
        status="Open", blockers=[], warnings=[], drivers=[total], total_expected_pay=Decimal("25.0000"),
    )


def _all_line_types_packet(db) -> _LiveCalculationPacket:
    """A persisted packet covering CP-4D's financial taxonomy without live reads."""
    lines = [
        _CalculationPacketLine("DraftLine", str(db.line_id), "DailyNote", "Daily", date(2089, 1, 1), db.driver_id, Decimal("1"), None, Decimal("25"), False, None, source_evidence={"DraftLineID": db.line_id}),
        _CalculationPacketLine("StatusEntryState", "501", "STATUS_PAY", "Daily", date(2089, 1, 2), db.driver_id, Decimal("3"), Decimal("6"), Decimal("18"), False, None, source_evidence={"StatusKeyID": 101}),
        _CalculationPacketLine("Manual", "period-pay:701", "Adjustment", "Period", None, db.driver_id, None, None, Decimal("10"), False, None, source_evidence={"EnteredAmount": Decimal("10")}),
        _CalculationPacketLine("System", "801", "SYS_MIN_TOPUP", "Period", None, db.driver_id, Decimal("1"), None, Decimal("2"), False, None, source_evidence={"DriverPayRuleID": db.minimum_rule_id}),
        _CalculationPacketLine("System", "802", "SYS_MAX_CAP", "Period", None, db.driver_id, Decimal("1"), None, Decimal("-1"), False, None, source_evidence={"DriverPayRuleID": db.maximum_rule_id}),
        _CalculationPacketLine("BonusEvent", "901", "BONUS", "Period", None, db.driver_id, None, None, Decimal("5"), False, None, source_evidence={"PayrollBonusEventID": 901, "Status": "Active"}),
    ]
    total = _CalculationPacketDriverTotal(
        driver_id=db.driver_id, driver_code="CP4F", driver_name="CP4F Driver",
        daily_pay=Decimal("25"), status_pay=Decimal("18"), period_pay=Decimal("10"),
        minimum_adjustment=Decimal("2"), maximum_adjustment=Decimal("-1"), bonus_total=Decimal("5"),
        expected_pay=Decimal("59"), needs_manager_review=False, blockers=[], lines=lines,
    )
    return _LiveCalculationPacket(
        payroll_period_id=db.period_id, company_id=db.company_id, branch_id=db.branch_id,
        status="Open", blockers=[], warnings=[], drivers=[total], total_expected_pay=Decimal("59"),
    )


async def _period_for_capture(db):
    row = (await db.conn.execute(text("""
        SELECT payrollperiodid, branchid, periodcode, periodtype, startdate, enddate
        FROM payroll.payrollperiods WHERE payrollperiodid = :period_id
    """), {"period_id": db.period_id})).mappings().one()
    return SimpleNamespace(
        payroll_period_id=row["payrollperiodid"], branch_id=row["branchid"],
        period_code=row["periodcode"], period_type=row["periodtype"],
        start_date=row["startdate"], end_date=row["enddate"],
    )


async def _approved_snapshot(db, packet: _LiveCalculationPacket | None = None) -> tuple[int, int]:
    snapshot_id = await _capture_calculation_snapshot(
        period=await _period_for_capture(db), company_id=db.company_id, user_id=db.user_id,
        packet=_packet(db) if packet is None else packet, db=db.conn, context="Submit",
    )
    review_id = (await db.conn.execute(text("""
        INSERT INTO review.managerreviewitems
            (companyid, branchid, requestedbyuserid, requesttype, entityschema, entityname,
             entityid, title, priority, status, payrollcalculationsnapshotid)
        VALUES (:company_id, :branch_id, :user_id, 'PeriodApproval', 'payroll', 'PayrollPeriods',
                :period_id, 'CP4F approved packet', 'Normal', 'Approved', :snapshot_id)
        RETURNING reviewitemid
    """), {**db.__dict__, "period_id": str(db.period_id), "snapshot_id": snapshot_id})).scalar_one()
    await db.conn.execute(text("UPDATE payroll.payrollperiods SET status = 'Approved' WHERE payrollperiodid = :period_id"), {"period_id": db.period_id})
    return int(review_id), int(snapshot_id)


@pytest.fixture
def no_access_checks(monkeypatch):
    async def allowed(*_args, **_kwargs):
        return None
    # Stage B4-19: _check_permission and _get_oda_own_driver_id are called
    # by finalize_period / get_finalization_preview, which now live in
    # app.payroll.finalization and resolve both as bare names through that
    # module's own globals — patching app.payroll.service no longer
    # intercepts them.
    monkeypatch.setattr(payroll_finalization, "_check_permission", allowed)
    monkeypatch.setattr(payroll_finalization, "_get_oda_own_driver_id", allowed)


@pytest.mark.asyncio
async def test_finalize_projects_exact_approved_snapshot_and_audits_provenance(cp4f_db, no_access_checks):
    review_id, snapshot_id = await _approved_snapshot(cp4f_db)
    result = await finalize_period(cp4f_db.period_id, cp4f_db.company_id, cp4f_db.user_id, cp4f_db.conn)
    assert result.status == "Locked"
    final = (await cp4f_db.conn.execute(text("""
        SELECT draftlineid, finalamount, sourcetype, sourceid, sourcesnapshot
        FROM payroll.payrollfinallines WHERE payrollperiodid = :period_id
    """), {"period_id": cp4f_db.period_id})).mappings().one()
    assert final["draftlineid"] == cp4f_db.line_id
    assert final["finalamount"] == Decimal("25.0000")
    assert final["sourcetype"] == "DraftLine"
    provenance = final["sourcesnapshot"]
    assert provenance["payroll_calculation_snapshot_id"] == snapshot_id
    audit = (await cp4f_db.conn.execute(text("""
        SELECT newvaluejson FROM audit.auditlog
        WHERE actioncode = 'PAYROLL_FINALIZED' AND entityid = :period_id
        ORDER BY auditid DESC LIMIT 1
    """), {"period_id": str(cp4f_db.period_id)})).scalar_one()
    assert json.loads(audit)["approved_review_item_id"] == review_id


@pytest.mark.asyncio
async def test_preview_and_finalize_ignore_live_financial_helpers_and_draft_drift(cp4f_db, no_access_checks, monkeypatch):
    _, snapshot_id = await _approved_snapshot(cp4f_db)
    await cp4f_db.conn.execute(text("UPDATE payroll.payrolldraftlines SET calculatedamount = 9999, needsmanagerreview = TRUE WHERE draftlineid = :id"), {"id": cp4f_db.line_id})
    async def unexpected(*_args, **_kwargs):
        raise AssertionError("CP-4F must not use live financial calculation")
    for helper in ("_build_live_calculation_packet", "_validate_period_can_finalize"):
        monkeypatch.setattr(payroll_service, helper, unexpected)
    preview = await get_finalization_preview(cp4f_db.period_id, cp4f_db.company_id, cp4f_db.user_id, cp4f_db.conn)
    assert preview.total_final_gross == Decimal("25.0000")
    assert preview.lines[0].source_key.startswith("snapshot-line:")
    assert preview.lines[0].draft_line_id == cp4f_db.line_id
    await finalize_period(cp4f_db.period_id, cp4f_db.company_id, cp4f_db.user_id, cp4f_db.conn)
    final_amount = (await cp4f_db.conn.execute(text("SELECT finalamount FROM payroll.payrollfinallines WHERE payrollperiodid = :id"), {"id": cp4f_db.period_id})).scalar_one()
    assert final_amount == Decimal("25.0000")
    assert (await cp4f_db.conn.execute(text("SELECT COUNT(*) FROM payroll.payrollcalculationsnapshots WHERE payrollcalculationsnapshotid = :id"), {"id": snapshot_id})).scalar_one() == 1


@pytest.mark.asyncio
async def test_legacy_approved_without_snapshot_fails_closed(cp4f_db, no_access_checks):
    await cp4f_db.conn.execute(text("""
        INSERT INTO review.managerreviewitems
            (companyid, branchid, requestedbyuserid, requesttype, entityschema, entityname,
             entityid, title, priority, status)
        VALUES (:company_id, :branch_id, :user_id, 'PeriodApproval', 'payroll', 'PayrollPeriods',
                :period_id, 'Legacy approved', 'Normal', 'Approved')
    """), {**cp4f_db.__dict__, "period_id": str(cp4f_db.period_id)})
    await cp4f_db.conn.execute(text("UPDATE payroll.payrollperiods SET status = 'Approved' WHERE payrollperiodid = :period_id"), {"period_id": cp4f_db.period_id})
    with pytest.raises(HTTPException, match="SNAPSHOT_REQUIRED_FOR_FINALIZATION"):
        await finalize_period(cp4f_db.period_id, cp4f_db.company_id, cp4f_db.user_id, cp4f_db.conn)
    assert (await cp4f_db.conn.execute(text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :id"), {"id": cp4f_db.period_id})).scalar_one() == "Approved"


@pytest.mark.asyncio
async def test_projection_failure_rolls_back_locked_transition(cp4f_db, no_access_checks, monkeypatch):
    await _approved_snapshot(cp4f_db)
    async def fail_projection(**_kwargs):
        raise RuntimeError("projection failed")
    # Stage B4-19: _project_approved_snapshot_final_lines's real
    # implementation now lives in app.payroll.finalization, and
    # finalize_period (also in finalization) resolves it as a bare name
    # through that module's own globals — patching app.payroll.service's
    # compatibility re-export no longer intercepts it.
    monkeypatch.setattr(payroll_finalization, "_project_approved_snapshot_final_lines", fail_projection)
    with pytest.raises(RuntimeError, match="projection failed"):
        async with cp4f_db.conn.begin_nested():
            await finalize_period(cp4f_db.period_id, cp4f_db.company_id, cp4f_db.user_id, cp4f_db.conn)
    assert (await cp4f_db.conn.execute(text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :id"), {"id": cp4f_db.period_id})).scalar_one() == "Approved"
    assert (await cp4f_db.conn.execute(text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :id"), {"id": cp4f_db.period_id})).scalar_one() == 0


@pytest.mark.asyncio
async def test_projection_preserves_all_snapshot_line_classes_and_never_invents_draft_ids(cp4f_db, no_access_checks):
    _, snapshot_id = await _approved_snapshot(cp4f_db, _all_line_types_packet(cp4f_db))
    preview = await get_finalization_preview(cp4f_db.period_id, cp4f_db.company_id, cp4f_db.user_id, cp4f_db.conn)
    assert preview.total_final_gross == Decimal("59.0000")
    status_preview = next(line for line in preview.lines if line.line_type == "STATUS_PAY")
    system_preview = next(line for line in preview.lines if line.line_type == "SYS_MIN_TOPUP")
    assert status_preview.draft_line_id is None
    assert system_preview.draft_line_id is None
    assert status_preview.source_key.startswith("snapshot-line:")
    await finalize_period(cp4f_db.period_id, cp4f_db.company_id, cp4f_db.user_id, cp4f_db.conn)
    lines = (await cp4f_db.conn.execute(text("""
        SELECT linetype, draftlineid, finalamount, sourcetype, sourcesnapshot
        FROM payroll.payrollfinallines WHERE payrollperiodid = :period_id ORDER BY linetype
    """), {"period_id": cp4f_db.period_id})).mappings().all()
    assert {row["linetype"] for row in lines} == {"DailyNote", "STATUS_PAY", "Adjustment", "BONUS", "SYS_MIN_TOPUP", "SYS_MAX_CAP"}
    assert next(row for row in lines if row["linetype"] == "STATUS_PAY")["draftlineid"] is None
    assert next(row for row in lines if row["linetype"] == "SYS_MIN_TOPUP")["draftlineid"] is None
    assert sum((Decimal(str(row["finalamount"])) for row in lines), Decimal("0")) == Decimal("59.0000")
    assert all(row["sourcesnapshot"]["payroll_calculation_snapshot_id"] == snapshot_id for row in lines)


@pytest.mark.asyncio
async def test_finalization_refuses_duplicate_approved_review_authority(cp4f_db, no_access_checks):
    _, snapshot_id = await _approved_snapshot(cp4f_db)
    await cp4f_db.conn.execute(text("""
        INSERT INTO review.managerreviewitems
            (companyid, branchid, requestedbyuserid, requesttype, entityschema, entityname,
             entityid, title, priority, status)
        VALUES (:company_id, :branch_id, :user_id, 'PeriodApproval', 'payroll', 'PayrollPeriods',
                :period_id, 'Corrupt second approved review', 'Normal', 'Approved')
    """), {**cp4f_db.__dict__, "period_id": str(cp4f_db.period_id)})
    with pytest.raises(HTTPException, match="APPROVED_SNAPSHOT_INTEGRITY_ERROR"):
        await finalize_period(cp4f_db.period_id, cp4f_db.company_id, cp4f_db.user_id, cp4f_db.conn)
    assert (await cp4f_db.conn.execute(text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :id"), {"id": cp4f_db.period_id})).scalar_one() == 0
    assert snapshot_id > 0


@pytest.mark.asyncio
async def test_finalization_rejects_review_item_linked_to_a_snapshot_for_another_period(cp4f_db, no_access_checks):
    snapshot_id = await _capture_calculation_snapshot(
        period=await _period_for_capture(cp4f_db), company_id=cp4f_db.company_id,
        user_id=cp4f_db.user_id, packet=_packet(cp4f_db), db=cp4f_db.conn, context="Submit",
    )
    other_period = (await cp4f_db.conn.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (:company_id, :branch_id, 'Approved', :code, 'CP4F mismatched review', 'Week', :start, :end)
        RETURNING payrollperiodid
    """), {**cp4f_db.__dict__, "code": f"CP4F-MISMATCH-{uuid4().hex}", "start": date(2089, 2, 1), "end": date(2089, 2, 7)})).scalar_one()
    await cp4f_db.conn.execute(text("""
        INSERT INTO review.managerreviewitems
            (companyid, branchid, requestedbyuserid, requesttype, entityschema, entityname,
             entityid, title, priority, status, payrollcalculationsnapshotid)
        VALUES (:company_id, :branch_id, :user_id, 'PeriodApproval', 'payroll', 'PayrollPeriods',
                :period_id, 'Wrong snapshot period', 'Normal', 'Approved', :snapshot_id)
    """), {**cp4f_db.__dict__, "period_id": str(other_period), "snapshot_id": snapshot_id})
    with pytest.raises(HTTPException, match="APPROVED_SNAPSHOT_INTEGRITY_ERROR"):
        await finalize_period(int(other_period), cp4f_db.company_id, cp4f_db.user_id, cp4f_db.conn)


@pytest.mark.asyncio
async def test_finalize_retry_cannot_create_duplicate_final_lines(cp4f_db, no_access_checks):
    await _approved_snapshot(cp4f_db)
    await finalize_period(cp4f_db.period_id, cp4f_db.company_id, cp4f_db.user_id, cp4f_db.conn)
    with pytest.raises(HTTPException, match="Only Approved periods"):
        await finalize_period(cp4f_db.period_id, cp4f_db.company_id, cp4f_db.user_id, cp4f_db.conn)
    assert (await cp4f_db.conn.execute(text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :id"), {"id": cp4f_db.period_id})).scalar_one() == 1
