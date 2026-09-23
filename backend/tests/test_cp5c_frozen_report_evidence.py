"""CP-5C immutable frozen Status/Bonus report-evidence tests."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

import app.payroll.period_lifecycle as period_lifecycle
from app.payroll.schemas import PeriodStatusChange
from app.payroll.service import (
    _build_live_calculation_packet,
    _CalculationPacketDriverTotal,
    _CalculationPacketLine,
    _capture_calculation_snapshot,
    _LiveCalculationPacket,
    _set_submit_transaction_isolation,
    change_period_status,
    get_period_by_id,
    resubmit_period,
)
from app.payroll.snapshot_hash import (
    CURRENT_REPORT_EVIDENCE_VERSION,
    calculate_report_evidence_hash,
)


@pytest_asyncio.fixture
async def evidence_db(test_database_url):
    """Seed live sources and keep generated immutable rows inside a rollback."""
    marker = uuid4().hex
    engine = create_async_engine(test_database_url, echo=False)
    ids: dict[str, int] = {}
    try:
        async with engine.begin() as seed:
            tenant = (await seed.execute(text("""
                SELECT c.companyid, u.userid
                FROM core.companies c
                JOIN sec.users u ON u.companyid = c.companyid
                WHERE c.companycode = 'DEMO'
                  AND u.username = 'admin'
            """))).mappings().one()
            ids.update({
                "company_id": int(tenant["companyid"]),
                "user_id": int(tenant["userid"]),
            })
            branch_id = (await seed.execute(text("""
                INSERT INTO core.branches
                    (companyid, branchcode, branchname, status, isdefault)
                VALUES (:cid, :code, :name, 'Active', FALSE)
                RETURNING branchid
            """), {
                "cid": ids["company_id"],
                "code": f"CP5C-{marker[:16]}",
                "name": f"CP5C isolated {marker[:12]}",
            })).scalar_one()
            await seed.execute(text("""
                INSERT INTO payroll.branchpayrollsettings
                    (companyid, branchid, payrollfrequency, anchorstartdate, isactive)
                VALUES (:cid, :bid, 'Week', '2089-01-01', TRUE)
            """), {"cid": ids["company_id"], "bid": branch_id})
            ids["branch_id"] = int(branch_id)
            employee_id = (await seed.execute(text("""
                INSERT INTO core.employees
                    (companyid, branchid, fullname, employeetype, employmentstatus,
                     createdbyuserid)
                VALUES (:cid, :bid, :name, 'Driver', 'Active', :uid)
                RETURNING employeeid
            """), {
                "cid": ids["company_id"], "bid": ids["branch_id"],
                "name": f"CP5C Evidence Employee {marker}", "uid": ids["user_id"],
            })).scalar_one()
            driver_id = (await seed.execute(text("""
                INSERT INTO core.drivers
                    (companyid, branchid, employeeid, drivercode, driverstatus)
                VALUES (:cid, :bid, :employee_id, :code, 'Active')
                RETURNING driverid
            """), {
                "cid": ids["company_id"], "bid": ids["branch_id"],
                "employee_id": employee_id, "code": f"CP5C-{marker[:20]}",
            })).scalar_one()
            period_id = (await seed.execute(text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype,
                     startdate, enddate)
                VALUES (:cid, :bid, 'Open', :code, :name, 'Week', :start, :end)
                RETURNING payrollperiodid
            """), {
                "cid": ids["company_id"], "bid": ids["branch_id"],
                "code": f"CP5C-{marker}", "name": f"CP5C {marker}",
                "start": date(2089, 1, 1), "end": date(2089, 1, 7),
            })).scalar_one()
            line_id = (await seed.execute(text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverid, workdate, linetype,
                     quantity, sourcetype, sourceid, status, needsmanagerreview, linescope)
                VALUES (:cid, :bid, :pid, :did, :work_date, 'DailyNote', 1,
                        'User', :source_id, 'Active', FALSE, 'Daily')
                RETURNING draftlineid
            """), {
                "cid": ids["company_id"], "bid": ids["branch_id"],
                "pid": period_id, "did": driver_id, "work_date": date(2089, 1, 1),
                "source_id": f"CP5C:{marker}",
            })).scalar_one()
            status_key_id = (await seed.execute(text("""
                INSERT INTO payroll.payrollstatuskeys
                    (companyid, branchid, statuscode, normalizedstatuscode, keyname,
                     isoffreason, hoursvalue, isactive, displayorder)
                VALUES (:cid, :bid, :code, :norm, :name, TRUE, 0, TRUE, 99)
                RETURNING statuskeyid
            """), {
                "cid": ids["company_id"], "bid": ids["branch_id"],
                "code": f"CP5C_OFF_{marker[:12]}",
                "norm": f"CP5C_OFF_{marker[:12]}".upper(),
                "name": f"Frozen Off {marker[:8]}",
            })).scalar_one()
            entry_state_id = (await seed.execute(text("""
                INSERT INTO payroll.payrollperioddriverdayentrystate
                    (companyid, branchid, payrollperiodid, workdate, driverid, statuskeyid,
                     createdbyuserid)
                VALUES (:cid, :bid, :pid, :work_date, :did, :status_key_id, :uid)
                RETURNING payrollperioddriverdayentrystateid
            """), {
                "cid": ids["company_id"], "bid": ids["branch_id"], "pid": period_id,
                "work_date": date(2089, 1, 2), "did": driver_id,
                "status_key_id": status_key_id, "uid": ids["user_id"],
            })).scalar_one()
            bonus_event_id = (await seed.execute(text("""
                INSERT INTO payroll.payrollbonusevents
                    (companyid, branchid, payrollperiodid, driverid, amount, reason,
                     notes, status, createdbyuserid, createdatutc, datarevision)
                VALUES (:cid, :bid, :pid, :did, 12.5000, 'On-time', 'Initial note',
                        'Active', :uid, '2089-01-02T10:00:00Z', 7)
                RETURNING payrollbonuseventid
            """), {
                "cid": ids["company_id"], "bid": ids["branch_id"], "pid": period_id,
                "did": driver_id, "uid": ids["user_id"],
            })).scalar_one()
            ids.update({
                "employee_id": int(employee_id), "driver_id": int(driver_id),
                "period_id": int(period_id), "line_id": int(line_id),
                "status_key_id": int(status_key_id), "entry_state_id": int(entry_state_id),
                "bonus_event_id": int(bonus_event_id),
                "database_url": test_database_url,
            })

        async with engine.connect() as conn:
            outer = await conn.begin()
            try:
                yield SimpleNamespace(conn=conn, **ids)
            finally:
                if outer.is_active:
                    await outer.rollback()
        async with engine.begin() as cleanup:
            await cleanup.execute(text(
                "DELETE FROM payroll.payrollbonusevents WHERE payrollbonuseventid = :id"
            ), {"id": ids["bonus_event_id"]})
            await cleanup.execute(text(
                "DELETE FROM payroll.payrollperioddriverdayentrystate "
                "WHERE payrollperioddriverdayentrystateid = :id"
            ), {"id": ids["entry_state_id"]})
            await cleanup.execute(text(
                "DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"
            ), {"id": ids["status_key_id"]})
            await cleanup.execute(text(
                "DELETE FROM payroll.payrolldraftlines WHERE draftlineid = :id"
            ), {"id": ids["line_id"]})
            await cleanup.execute(text(
                "DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :id"
            ), {"id": ids["period_id"]})
            await cleanup.execute(text("DELETE FROM core.drivers WHERE driverid = :id"), {
                "id": ids["driver_id"],
            })
            await cleanup.execute(text("DELETE FROM core.employees WHERE employeeid = :id"), {
                "id": ids["employee_id"],
            })
            await cleanup.execute(text(
                "DELETE FROM payroll.branchpayrollsettings WHERE branchid = :bid"
            ), {"bid": ids["branch_id"]})
            await cleanup.execute(text("DELETE FROM core.branches WHERE branchid = :bid"), {
                "bid": ids["branch_id"],
            })
    finally:
        await engine.dispose()


async def _period(db: SimpleNamespace):
    return await get_period_by_id(db.company_id, db.user_id, db.period_id, db.conn)


def _direct_packet(db: SimpleNamespace, *, include_bonus: bool = True) -> _LiveCalculationPacket:
    daily = _CalculationPacketLine(
        source_type="DraftLine",
        source_id=str(db.line_id),
        line_type="Hours",
        line_scope="Daily",
        work_date=date(2089, 1, 1),
        driver_id=db.driver_id,
        quantity=Decimal("2.0000"),
        resolved_rate_amount=Decimal("10.0000"),
        calculated_amount=Decimal("20.0000"),
        needs_manager_review=False,
        blocker_reason=None,
        source_evidence={"DraftLineID": db.line_id},
    )
    lines = [daily]
    bonus_total = Decimal("0")
    if include_bonus:
        lines.append(_CalculationPacketLine(
            source_type="BonusEvent",
            source_id=str(db.bonus_event_id),
            line_type="BONUS",
            line_scope="Period",
            work_date=None,
            driver_id=db.driver_id,
            quantity=None,
            resolved_rate_amount=None,
            calculated_amount=Decimal("12.5000"),
            needs_manager_review=False,
            blocker_reason=None,
            bonus_event_id=db.bonus_event_id,
            source_evidence={"PayrollBonusEventID": db.bonus_event_id},
        ))
        bonus_total = Decimal("12.5000")
    total = Decimal("20.0000") + bonus_total
    driver = _CalculationPacketDriverTotal(
        driver_id=db.driver_id,
        driver_code="CP5C",
        driver_name="CP5C Evidence Driver",
        daily_pay=Decimal("20.0000"),
        status_pay=Decimal("0"),
        period_pay=Decimal("0"),
        minimum_adjustment=Decimal("0"),
        maximum_adjustment=Decimal("0"),
        bonus_total=bonus_total,
        expected_pay=total,
        needs_manager_review=False,
        blockers=[],
        lines=lines,
    )
    return _LiveCalculationPacket(
        payroll_period_id=db.period_id,
        company_id=db.company_id,
        branch_id=db.branch_id,
        status="Open",
        blockers=[],
        warnings=[],
        drivers=[driver],
        total_expected_pay=total,
    )


async def _evidence_rows(db: SimpleNamespace, snapshot_id: int) -> tuple[dict, dict, dict]:
    header = (await db.conn.execute(text("""
        SELECT reportevidenceversion, reportevidencehash, sourceconfighash, snapshothash
        FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid = :snapshot_id
    """), {"snapshot_id": snapshot_id})).mappings().one()
    status_row = (await db.conn.execute(text("""
        SELECT payrollperioddriverdayentrystateid, statuskeyid, statuscodesnapshot,
               statuslabelsnapshot, statusisoffreasonsnapshot
        FROM payroll.payrollcalculationsnapshotstatusentries
        WHERE payrollcalculationsnapshotid = :snapshot_id
    """), {"snapshot_id": snapshot_id})).mappings().one()
    bonus_row = (await db.conn.execute(text("""
        SELECT payrollbonuseventid, driverid, amount, reason, notes, datarevision,
               createdbyuserid, creatordisplaynamesnapshot, createdatutc
        FROM payroll.payrollcalculationsnapshotbonusevents
        WHERE payrollcalculationsnapshotid = :snapshot_id
    """), {"snapshot_id": snapshot_id})).mappings().one()
    return dict(header), dict(status_row), dict(bonus_row)


@pytest.mark.asyncio
async def test_submit_captures_versioned_status_and_bonus_evidence(evidence_db):
    result = await change_period_status(
        evidence_db.company_id, evidence_db.user_id, evidence_db.period_id,
        PeriodStatusChange(status="InReview"), evidence_db.conn,
    )
    assert result.status == "InReview"
    snapshot_id = int((await evidence_db.conn.execute(text("""
        SELECT payrollcalculationsnapshotid
        FROM review.managerreviewitems
        WHERE entityid = :pid AND requesttype = 'PeriodApproval' AND status = 'Pending'
    """), {"pid": str(evidence_db.period_id)})).scalar_one())
    header, status_row, bonus_row = await _evidence_rows(evidence_db, snapshot_id)
    assert header["reportevidenceversion"] == CURRENT_REPORT_EVIDENCE_VERSION
    assert len(str(header["reportevidencehash"])) == 64
    assert status_row["payrollperioddriverdayentrystateid"] == evidence_db.entry_state_id
    assert status_row["statuskeyid"] == evidence_db.status_key_id
    assert status_row["statusisoffreasonsnapshot"] is True
    assert bonus_row["payrollbonuseventid"] == evidence_db.bonus_event_id
    assert Decimal(str(bonus_row["amount"])) == Decimal("12.5000")
    assert bonus_row["reason"] == "On-time"
    assert bonus_row["notes"] == "Initial note"
    assert bonus_row["datarevision"] == 7
    assert bonus_row["createdbyuserid"] == evidence_db.user_id
    assert bonus_row["creatordisplaynamesnapshot"] == "Admin User"


@pytest.mark.asyncio
async def test_report_evidence_remains_frozen_after_live_source_drift(evidence_db):
    await change_period_status(
        evidence_db.company_id, evidence_db.user_id, evidence_db.period_id,
        PeriodStatusChange(status="InReview"), evidence_db.conn,
    )
    snapshot_id = int((await evidence_db.conn.execute(text("""
        SELECT payrollcalculationsnapshotid FROM review.managerreviewitems
        WHERE entityid = :pid AND status = 'Pending'
    """), {"pid": str(evidence_db.period_id)})).scalar_one())
    before = await _evidence_rows(evidence_db, snapshot_id)
    await evidence_db.conn.execute(text("""
        UPDATE payroll.payrollstatuskeys
        SET keyname = 'Changed live label', isactive = FALSE
        WHERE statuskeyid = :status_key_id
    """), {"status_key_id": evidence_db.status_key_id})
    await evidence_db.conn.execute(text("""
        UPDATE payroll.payrollbonusevents
        SET amount = 99.9900, reason = 'Changed', notes = 'Changed live source',
            datarevision = datarevision + 1
        WHERE payrollbonuseventid = :bonus_event_id
    """), {"bonus_event_id": evidence_db.bonus_event_id})
    await evidence_db.conn.execute(text(
        "UPDATE sec.users SET displayname = 'Changed creator' WHERE userid = :uid"
    ), {"uid": evidence_db.user_id})
    after = await _evidence_rows(evidence_db, snapshot_id)
    assert after == before


@pytest.mark.asyncio
async def test_resubmit_captures_an_independent_evidence_revision(evidence_db, monkeypatch):
    await change_period_status(
        evidence_db.company_id, evidence_db.user_id, evidence_db.period_id,
        PeriodStatusChange(status="InReview"), evidence_db.conn,
    )
    review = (await evidence_db.conn.execute(text("""
        SELECT reviewitemid, payrollcalculationsnapshotid
        FROM review.managerreviewitems
        WHERE entityid = :pid AND status = 'Pending'
    """), {"pid": str(evidence_db.period_id)})).mappings().one()
    first_snapshot_id = int(review["payrollcalculationsnapshotid"])
    first_evidence = await _evidence_rows(evidence_db, first_snapshot_id)
    await evidence_db.conn.execute(text("""
        UPDATE review.managerreviewitems SET status = 'Rejected'
        WHERE reviewitemid = :review_id
    """), {"review_id": review["reviewitemid"]})
    await evidence_db.conn.execute(text("""
        UPDATE payroll.payrollperiods
        SET status = 'Returned', currentreturnreviewitemid = :review_id
        WHERE payrollperiodid = :period_id
    """), {"review_id": review["reviewitemid"], "period_id": evidence_db.period_id})
    await evidence_db.conn.execute(text("""
        UPDATE payroll.payrollstatuskeys SET keyname = 'Corrected frozen label'
        WHERE statuskeyid = :status_key_id
    """), {"status_key_id": evidence_db.status_key_id})
    await evidence_db.conn.execute(text("""
        UPDATE payroll.payrollbonusevents
        SET amount = 15.0000, notes = 'Corrected source', datarevision = datarevision + 1
        WHERE payrollbonuseventid = :bonus_event_id
    """), {"bonus_event_id": evidence_db.bonus_event_id})

    async def _no_op_isolation(_db):
        return None

    # Stage B4-18: _set_submit_transaction_isolation's real implementation
    # now lives in app.payroll.period_lifecycle, and resubmit_period (also
    # in period_lifecycle) resolves it as a bare name through that module's
    # own globals — patching app.payroll.service's compatibility re-export
    # no longer intercepts it.
    monkeypatch.setattr(period_lifecycle, "_set_submit_transaction_isolation", _no_op_isolation)
    result = await resubmit_period(
        evidence_db.company_id, evidence_db.user_id, evidence_db.period_id, evidence_db.conn,
    )
    assert result.status == "InReview"
    snapshots = (await evidence_db.conn.execute(text("""
        SELECT payrollcalculationsnapshotid
        FROM payroll.payrollcalculationsnapshots
        WHERE payrollperiodid = :pid ORDER BY revisionnumber
    """), {"pid": evidence_db.period_id})).scalars().all()
    assert len(snapshots) == 2
    assert await _evidence_rows(evidence_db, int(snapshots[0])) == first_evidence
    _, second_status, second_bonus = await _evidence_rows(evidence_db, int(snapshots[1]))
    assert second_status["statuslabelsnapshot"] == "Corrected frozen label"
    assert Decimal(str(second_bonus["amount"])) == Decimal("15.0000")
    assert second_bonus["notes"] == "Corrected source"
    assert second_bonus["datarevision"] == 8


@pytest.mark.asyncio
async def test_zero_evidence_uses_versioned_hash_marker(evidence_db):
    await evidence_db.conn.execute(text(
        "DELETE FROM payroll.payrollperioddriverdayentrystate WHERE payrollperiodid = :pid"
    ), {"pid": evidence_db.period_id})
    await evidence_db.conn.execute(text(
        "DELETE FROM payroll.payrollbonusevents WHERE payrollperiodid = :pid"
    ), {"pid": evidence_db.period_id})
    snapshot_id = await _capture_calculation_snapshot(
        period=await _period(evidence_db), company_id=evidence_db.company_id,
        user_id=evidence_db.user_id, packet=_direct_packet(evidence_db, include_bonus=False),
        db=evidence_db.conn, context="Submit",
    )
    header = (await evidence_db.conn.execute(text("""
        SELECT reportevidenceversion, reportevidencehash
        FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid = :snapshot_id
    """), {"snapshot_id": snapshot_id})).mappings().one()
    assert header["reportevidenceversion"] == CURRENT_REPORT_EVIDENCE_VERSION
    assert header["reportevidencehash"] == calculate_report_evidence_hash(
        status_entries=[], bonus_events=[],
    )
    assert (await evidence_db.conn.execute(text("""
        SELECT COUNT(*) FROM payroll.payrollcalculationsnapshotstatusentries
        WHERE payrollcalculationsnapshotid = :snapshot_id
    """), {"snapshot_id": snapshot_id})).scalar_one() == 0
    assert (await evidence_db.conn.execute(text("""
        SELECT COUNT(*) FROM payroll.payrollcalculationsnapshotbonusevents
        WHERE payrollcalculationsnapshotid = :snapshot_id
    """), {"snapshot_id": snapshot_id})).scalar_one() == 0


def test_report_evidence_hash_is_order_independent_and_content_sensitive():
    statuses = [{
        "DriverID": 2, "WorkDate": date(2089, 1, 2),
        "PayrollPeriodDriverDayEntryStateID": 12, "StatusKeyID": 5,
        "StatusCodeSnapshot": "OFF", "StatusLabelSnapshot": "Off day",
        "StatusIsOffReasonSnapshot": True,
    }, {
        "DriverID": 1, "WorkDate": date(2089, 1, 1),
        "PayrollPeriodDriverDayEntryStateID": 11, "StatusKeyID": 4,
        "StatusCodeSnapshot": "PTO", "StatusLabelSnapshot": "PTO day",
        "StatusIsOffReasonSnapshot": False,
    }]
    bonuses = [{
        "PayrollBonusEventID": 22, "DriverID": 2, "Amount": Decimal("2.5000"),
        "Reason": "R", "Notes": None, "DataRevision": 1,
        "CreatedByUserID": 1, "CreatorDisplayNameSnapshot": "A",
        "CreatedAtUtc": "2089-01-02T10:00:00Z",
    }]
    first = calculate_report_evidence_hash(status_entries=statuses, bonus_events=bonuses)
    assert first == calculate_report_evidence_hash(
        status_entries=list(reversed(statuses)), bonus_events=list(reversed(bonuses)),
    )
    assert first != calculate_report_evidence_hash(
        status_entries=[{**statuses[0], "StatusLabelSnapshot": "Changed"}, statuses[1]],
        bonus_events=bonuses,
    )
    assert first != calculate_report_evidence_hash(
        status_entries=statuses,
        bonus_events=[{**bonuses[0], "Notes": "Changed"}],
    )


@pytest.mark.asyncio
async def test_evidence_rows_are_immutable_and_scope_bound(evidence_db):
    snapshot_id = await _capture_calculation_snapshot(
        period=await _period(evidence_db), company_id=evidence_db.company_id,
        user_id=evidence_db.user_id, packet=_direct_packet(evidence_db),
        db=evidence_db.conn, context="Submit",
    )
    with pytest.raises(IntegrityError):
        async with evidence_db.conn.begin_nested():
            await evidence_db.conn.execute(text("""
                UPDATE payroll.payrollcalculationsnapshotstatusentries
                SET statuslabelsnapshot = 'Mutated'
                WHERE payrollcalculationsnapshotid = :snapshot_id
            """), {"snapshot_id": snapshot_id})
    with pytest.raises(IntegrityError):
        async with evidence_db.conn.begin_nested():
            await evidence_db.conn.execute(text("""
                DELETE FROM payroll.payrollcalculationsnapshotbonusevents
                WHERE payrollcalculationsnapshotid = :snapshot_id
            """), {"snapshot_id": snapshot_id})
    with pytest.raises(IntegrityError):
        async with evidence_db.conn.begin_nested():
            await evidence_db.conn.execute(text("""
                INSERT INTO payroll.payrollcalculationsnapshotstatusentries
                    (payrollcalculationsnapshotid, companyid, branchid, payrollperiodid,
                     driverid, workdate, payrollperioddriverdayentrystateid, statuskeyid,
                     statuscodesnapshot, statuslabelsnapshot, statusisoffreasonsnapshot)
                VALUES (:snapshot_id, :cid, :bid, :wrong_period_id, :driver_id,
                        '2089-01-03', :entry_state_id, :status_key_id, 'OFF', 'Off', TRUE)
            """), {
                "snapshot_id": snapshot_id, "cid": evidence_db.company_id,
                "bid": evidence_db.branch_id, "wrong_period_id": evidence_db.period_id + 999999,
                "driver_id": evidence_db.driver_id, "entry_state_id": evidence_db.entry_state_id,
                "status_key_id": evidence_db.status_key_id,
            })


@pytest.mark.asyncio
async def test_repeatable_read_keeps_packet_and_evidence_on_one_source_view(evidence_db):
    period = await _period(evidence_db)
    snapshot_engine = create_async_engine(evidence_db.database_url, echo=False)
    mutator_engine = create_async_engine(evidence_db.database_url, echo=False)
    try:
        async with snapshot_engine.connect() as snapshot_conn:
            snapshot_tx = await snapshot_conn.begin()
            try:
                await _set_submit_transaction_isolation(snapshot_conn)
                packet = await _build_live_calculation_packet(
                    period, evidence_db.company_id, snapshot_conn,
                )
                async with mutator_engine.begin() as mutator_conn:
                    await mutator_conn.execute(text("""
                        UPDATE payroll.payrollstatuskeys
                        SET keyname = 'Concurrent live label'
                        WHERE statuskeyid = :status_key_id
                    """), {"status_key_id": evidence_db.status_key_id})
                    await mutator_conn.execute(text("""
                        UPDATE payroll.payrollbonusevents
                        SET amount = 99.9900, datarevision = datarevision + 1
                        WHERE payrollbonuseventid = :bonus_event_id
                    """), {"bonus_event_id": evidence_db.bonus_event_id})

                snapshot_id = await _capture_calculation_snapshot(
                    period=period, company_id=evidence_db.company_id,
                    user_id=evidence_db.user_id, packet=packet, db=snapshot_conn,
                    context="Submit",
                )
                _, status_row, bonus_row = await _evidence_rows(
                    SimpleNamespace(conn=snapshot_conn), snapshot_id,
                )
                assert status_row["statuslabelsnapshot"].startswith("Frozen Off")
                assert Decimal(str(bonus_row["amount"])) == Decimal("12.5000")
                assert bonus_row["datarevision"] == 7
            finally:
                if snapshot_tx.is_active:
                    await snapshot_tx.rollback()
    finally:
        await snapshot_engine.dispose()
        await mutator_engine.dispose()


@pytest.mark.asyncio
async def test_legacy_snapshot_marker_remains_explicitly_nullable(evidence_db):
    period = await _period(evidence_db)
    legacy_id = int((await evidence_db.conn.execute(text("""
        INSERT INTO payroll.payrollcalculationsnapshots
            (companyid, branchid, payrollperiodid, revisionnumber, calculationversion,
             sourceconfighash, snapshothash, createdbyuserid, totalexpectedpay)
        VALUES (:cid, :bid, :pid, 1, 'legacy', :source_hash, :snapshot_hash, :uid, 0)
        RETURNING payrollcalculationsnapshotid
    """), {
        "cid": evidence_db.company_id, "bid": evidence_db.branch_id,
        "pid": period.payroll_period_id,
        "source_hash": "0" * 64, "snapshot_hash": "1" * 64,
        "uid": evidence_db.user_id,
    })).scalar_one())
    marker = (await evidence_db.conn.execute(text("""
        SELECT reportevidenceversion, reportevidencehash
        FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid = :snapshot_id
    """), {"snapshot_id": legacy_id})).mappings().one()
    assert marker["reportevidenceversion"] is None
    assert marker["reportevidencehash"] is None
