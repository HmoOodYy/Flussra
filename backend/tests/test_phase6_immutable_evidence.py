"""Phase 6 immutable workflow and used-rate evidence foundation tests."""
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

import app.payroll.service as payroll_service
from app.payroll.schemas import PeriodStatusChange
from app.payroll.service import (
    _CalculationPacketDriverTotal,
    _CalculationPacketLine,
    _capture_calculation_snapshot,
    _LiveCalculationPacket,
    change_period_status,
    finalize_period,
)
from app.review.schemas import ReviewDecide
from app.review.service import decide_review_item


@pytest_asyncio.fixture
async def foundation_db(test_database_url):
    """Seed mutable inputs and rollback all append-only evidence after each test."""
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
                WHERE c.companycode = 'DEMO' AND b.branchcode = 'HQ'
                  AND u.username = 'admin'
            """))).mappings().one()
            ids.update({
                "company_id": int(tenant["companyid"]),
                "branch_id": int(tenant["branchid"]),
                "user_id": int(tenant["userid"]),
            })
            employee_id = (await seed.execute(text("""
                INSERT INTO core.employees
                    (companyid, branchid, fullname, employeetype, employmentstatus,
                     createdbyuserid)
                VALUES (:cid, :bid, :name, 'Driver', 'Active', :uid)
                RETURNING employeeid
            """), {
                "cid": ids["company_id"], "bid": ids["branch_id"],
                "name": f"Phase 6 Evidence Employee {marker}", "uid": ids["user_id"],
            })).scalar_one()
            driver_id = (await seed.execute(text("""
                INSERT INTO core.drivers
                    (companyid, branchid, employeeid, drivercode, driverstatus)
                VALUES (:cid, :bid, :employee_id, :code, 'Active')
                RETURNING driverid
            """), {
                "cid": ids["company_id"], "bid": ids["branch_id"],
                "employee_id": employee_id, "code": f"P6E-{marker[:20]}",
            })).scalar_one()
            period_id = (await seed.execute(text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype,
                     startdate, enddate)
                VALUES (:cid, :bid, 'Open', :code, :name, 'Week', :start, :end)
                RETURNING payrollperiodid
            """), {
                "cid": ids["company_id"], "bid": ids["branch_id"],
                "code": f"P6E-{marker}", "name": f"P6 Evidence {marker}",
                "start": date(2091, 1, 1), "end": date(2091, 1, 7),
            })).scalar_one()
            line_id = (await seed.execute(text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverid, workdate, linetype,
                     quantity, sourcetype, sourceid, status, needsmanagerreview, linescope)
                VALUES (:cid, :bid, :pid, :did, :work_date, 'DailyNote', 1,
                        'User', :source_id, 'Active', FALSE, 'Daily')
                RETURNING draftlineid
            """), {
                "cid": ids["company_id"], "bid": ids["branch_id"], "pid": period_id,
                "did": driver_id, "work_date": date(2091, 1, 1),
                "source_id": f"P6E:{marker}",
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
                VALUES (:cid, :bid, :did, :rate_type_id, 10.0000, :effective_from,
                        'Approved', :uid)
                RETURNING driverrateid
            """), {
                "cid": ids["company_id"], "bid": ids["branch_id"],
                "did": driver_id, "rate_type_id": rate_type_id,
                "effective_from": date(2091, 1, 1), "uid": ids["user_id"],
            })).scalar_one()
            unused_rate_id = (await seed.execute(text("""
                INSERT INTO payroll.driverrates
                    (companyid, branchid, driverid, ratetypeid, amount, effectivefrom,
                     status, createdbyuserid)
                VALUES (:cid, :bid, :did, :rate_type_id, 99.0000, :effective_from,
                        'Voided', :uid)
                RETURNING driverrateid
            """), {
                "cid": ids["company_id"], "bid": ids["branch_id"],
                "did": driver_id, "rate_type_id": rate_type_id,
                "effective_from": date(2091, 1, 1), "uid": ids["user_id"],
            })).scalar_one()
            pay_rule_id = (await seed.execute(text("""
                INSERT INTO payroll.driverpayrules
                    (companyid, branchid, driverid, ruletype, amount, effectivefrom,
                     status, createdbyuserid)
                VALUES (:cid, :bid, :did, 'MinimumPay', 5.0000, :effective_from,
                        'Active', :uid)
                RETURNING driverpayruleid
            """), {
                "cid": ids["company_id"], "bid": ids["branch_id"],
                "did": driver_id, "effective_from": date(2091, 1, 1),
                "uid": ids["user_id"],
            })).scalar_one()
            ids.update({
                "employee_id": int(employee_id), "driver_id": int(driver_id),
                "period_id": int(period_id), "line_id": int(line_id),
                "rate_type_id": rate_type_id, "driver_rate_id": int(driver_rate_id),
                "unused_rate_id": int(unused_rate_id), "pay_rule_id": int(pay_rule_id),
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
                "DELETE FROM payroll.driverpayrules WHERE driverpayruleid = :id"
            ), {"id": ids["pay_rule_id"]})
            await cleanup.execute(text(
                "DELETE FROM payroll.driverrates WHERE driverrateid IN (:used, :unused)"
            ), {"used": ids["driver_rate_id"], "unused": ids["unused_rate_id"]})
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
    finally:
        await engine.dispose()


async def _period_for_snapshot(db: SimpleNamespace) -> SimpleNamespace:
    row = (await db.conn.execute(text("""
        SELECT payrollperiodid, branchid, periodcode, periodtype, startdate, enddate
        FROM payroll.payrollperiods
        WHERE payrollperiodid = :period_id
    """), {"period_id": db.period_id})).mappings().one()
    return SimpleNamespace(
        payroll_period_id=int(row["payrollperiodid"]),
        branch_id=int(row["branchid"]),
        period_code=str(row["periodcode"]),
        period_type=str(row["periodtype"]),
        start_date=row["startdate"],
        end_date=row["enddate"],
    )


def _rate_packet(db: SimpleNamespace, rate_amount: Decimal) -> _LiveCalculationPacket:
    rate_line = _CalculationPacketLine(
        source_type="DraftLine",
        source_id=str(db.line_id),
        line_type="Hours",
        line_scope="Daily",
        work_date=date(2091, 1, 1),
        driver_id=db.driver_id,
        quantity=Decimal("2.0000"),
        resolved_rate_amount=rate_amount,
        calculated_amount=rate_amount * Decimal("2"),
        needs_manager_review=False,
        blocker_reason=None,
        rate_type_id=db.rate_type_id,
        driver_rate_id=db.driver_rate_id,
        source_evidence={"DraftLineID": db.line_id, "RateBehavior": "PerUnit"},
    )
    rule_line = _CalculationPacketLine(
        source_type="System",
        source_id=f"minimum:{db.pay_rule_id}",
        line_type="SYS_MIN_TOPUP",
        line_scope="Period",
        work_date=None,
        driver_id=db.driver_id,
        quantity=None,
        resolved_rate_amount=None,
        calculated_amount=Decimal("5.0000"),
        needs_manager_review=False,
        blocker_reason=None,
        source_evidence={"DriverPayRuleID": db.pay_rule_id, "RuleType": "MinimumPay"},
    )
    daily_pay = rate_amount * Decimal("2")
    expected_pay = daily_pay + Decimal("5.0000")
    driver = _CalculationPacketDriverTotal(
        driver_id=db.driver_id,
        driver_code="P6E",
        driver_name="Phase 6 Evidence Driver",
        daily_pay=daily_pay,
        status_pay=Decimal("0"),
        period_pay=Decimal("0"),
        minimum_adjustment=Decimal("5.0000"),
        maximum_adjustment=Decimal("0"),
        bonus_total=Decimal("0"),
        expected_pay=expected_pay,
        needs_manager_review=False,
        blockers=[],
        lines=[rate_line, rule_line],
    )
    return _LiveCalculationPacket(
        payroll_period_id=db.period_id,
        company_id=db.company_id,
        branch_id=db.branch_id,
        status="Open",
        blockers=[],
        warnings=[],
        drivers=[driver],
        total_expected_pay=expected_pay,
    )


async def _pending_review(db: SimpleNamespace) -> dict:
    return dict((await db.conn.execute(text("""
        SELECT reviewitemid, payrollcalculationsnapshotid
        FROM review.managerreviewitems
        WHERE entityid = :period_id
          AND requesttype = 'PeriodApproval'
          AND status = 'Pending'
        ORDER BY reviewitemid DESC
        LIMIT 1
    """), {"period_id": str(db.period_id)})).mappings().one())


@pytest.mark.asyncio
async def test_workflow_actions_capture_frozen_participants_at_authoritative_transitions(
    foundation_db, monkeypatch,
):
    """Submit, return/resubmit, approve, and finalize each write immutable evidence."""
    await change_period_status(
        foundation_db.company_id, foundation_db.user_id, foundation_db.period_id,
        PeriodStatusChange(status="InReview"), foundation_db.conn,
    )
    first = await _pending_review(foundation_db)
    await decide_review_item(
        int(first["reviewitemid"]), foundation_db.company_id, foundation_db.user_id,
        ReviewDecide(decision="EditRequested", decision_reason="Correct source"),
        foundation_db.conn,
    )

    async def _no_op_isolation(_db):
        return None

    monkeypatch.setattr(payroll_service, "_set_submit_transaction_isolation", _no_op_isolation)
    await payroll_service.resubmit_period(
        foundation_db.company_id, foundation_db.user_id, foundation_db.period_id,
        foundation_db.conn,
    )
    second = await _pending_review(foundation_db)
    await decide_review_item(
        int(second["reviewitemid"]), foundation_db.company_id, foundation_db.user_id,
        ReviewDecide(decision="Approved"), foundation_db.conn,
    )
    await finalize_period(
        foundation_db.period_id, foundation_db.company_id, foundation_db.user_id,
        foundation_db.conn,
    )

    rows = (await foundation_db.conn.execute(text("""
        SELECT actioncode, payrollcalculationsnapshotid, reviewitemid,
               actordisplaynamesnapshot, responsibilitycontextsnapshot
        FROM payroll.payrollperiodworkflowactionevidence
        WHERE payrollperiodid = :period_id
        ORDER BY payrollperiodworkflowactionevidenceid
    """), {"period_id": foundation_db.period_id})).mappings().all()
    assert [row["actioncode"] for row in rows] == [
        "SUBMITTED", "REVIEW_EDIT_REQUESTED", "RESUBMITTED",
        "REVIEW_APPROVED", "FINALIZED",
    ]
    assert int(rows[0]["payrollcalculationsnapshotid"]) == int(first["payrollcalculationsnapshotid"])
    assert int(rows[1]["payrollcalculationsnapshotid"]) == int(first["payrollcalculationsnapshotid"])
    assert int(rows[2]["payrollcalculationsnapshotid"]) == int(second["payrollcalculationsnapshotid"])
    assert int(rows[3]["payrollcalculationsnapshotid"]) == int(second["payrollcalculationsnapshotid"])
    assert int(rows[4]["payrollcalculationsnapshotid"]) == int(second["payrollcalculationsnapshotid"])
    assert all(row["actordisplaynamesnapshot"] == "Admin User" for row in rows)
    assert all(row["responsibilitycontextsnapshot"]["roles"] for row in rows)

    await foundation_db.conn.execute(text("""
        UPDATE sec.users SET displayname = 'Changed current actor'
        WHERE userid = :user_id
    """), {"user_id": foundation_db.user_id})
    await foundation_db.conn.execute(text("""
        UPDATE sec.companyroles SET rolename = 'Changed current responsibility'
        WHERE companyroleid IN (
            SELECT companyroleid FROM sec.userbranchroles
            WHERE userid = :user_id AND companyroleid IS NOT NULL
        )
    """), {"user_id": foundation_db.user_id})
    frozen = (await foundation_db.conn.execute(text("""
        SELECT actordisplaynamesnapshot, responsibilitycontextsnapshot
        FROM payroll.payrollperiodworkflowactionevidence
        WHERE payrollperiodid = :period_id
        ORDER BY payrollperiodworkflowactionevidenceid
    """), {"period_id": foundation_db.period_id})).mappings().all()
    assert [row["actordisplaynamesnapshot"] for row in frozen] == ["Admin User"] * 5
    assert all(
        role["role_name"] != "Changed current responsibility"
        for row in frozen for role in row["responsibilitycontextsnapshot"]["roles"]
    )

    with pytest.raises(IntegrityError):
        async with foundation_db.conn.begin_nested():
            await foundation_db.conn.execute(text("""
                UPDATE payroll.payrollperiodworkflowactionevidence
                SET actordisplaynamesnapshot = 'Mutated'
                WHERE payrollperiodid = :period_id
            """), {"period_id": foundation_db.period_id})


@pytest.mark.asyncio
async def test_snapshot_used_rate_rule_evidence_is_used_only_frozen_and_revision_isolated(
    foundation_db,
):
    first_snapshot_id = await _capture_calculation_snapshot(
        period=await _period_for_snapshot(foundation_db),
        company_id=foundation_db.company_id,
        user_id=foundation_db.user_id,
        packet=_rate_packet(foundation_db, Decimal("10.0000")),
        db=foundation_db.conn,
        context="Submit",
    )
    first_definitions = (await foundation_db.conn.execute(text("""
        SELECT evidencekind, driverrateid, driverpayruleid, rateamountsnapshot,
               ruleamountsnapshot, ratetypecodesnapshot, definitionfingerprint
        FROM payroll.payrollcalculationsnapshotusedratedefinitions
        WHERE payrollcalculationsnapshotid = :snapshot_id
        ORDER BY evidencekind
    """), {"snapshot_id": first_snapshot_id})).mappings().all()
    assert len(first_definitions) == 2
    rate_definition = next(row for row in first_definitions if row["evidencekind"] == "DriverRate")
    rule_definition = next(row for row in first_definitions if row["evidencekind"] == "DriverPayRule")
    assert int(rate_definition["driverrateid"]) == foundation_db.driver_rate_id
    assert Decimal(str(rate_definition["rateamountsnapshot"])) == Decimal("10.0000")
    assert int(rule_definition["driverpayruleid"]) == foundation_db.pay_rule_id
    assert Decimal(str(rule_definition["ruleamountsnapshot"])) == Decimal("5.0000")
    assert not any(
        row["driverrateid"] == foundation_db.unused_rate_id
        for row in first_definitions
    )
    assert (await foundation_db.conn.execute(text("""
        SELECT COUNT(*)
        FROM payroll.payrollcalculationsnapshotlines l
        JOIN payroll.payrollcalculationdrivertotals t
          ON t.payrollcalculationdrivertotalid = l.payrollcalculationdrivertotalid
        WHERE t.payrollcalculationsnapshotid = :snapshot_id
          AND l.usedratedefinitionid IS NOT NULL
    """), {"snapshot_id": first_snapshot_id})).scalar_one() == 2

    await foundation_db.conn.execute(text("""
        UPDATE payroll.driverrates SET amount = 12.0000
        WHERE driverrateid = :driver_rate_id
    """), {"driver_rate_id": foundation_db.driver_rate_id})
    second_snapshot_id = await _capture_calculation_snapshot(
        period=await _period_for_snapshot(foundation_db),
        company_id=foundation_db.company_id,
        user_id=foundation_db.user_id,
        packet=_rate_packet(foundation_db, Decimal("12.0000")),
        db=foundation_db.conn,
        context="Resubmit",
    )
    amounts = (await foundation_db.conn.execute(text("""
        SELECT s.revisionnumber, d.rateamountsnapshot
        FROM payroll.payrollcalculationsnapshots s
        JOIN payroll.payrollcalculationsnapshotusedratedefinitions d
          ON d.payrollcalculationsnapshotid = s.payrollcalculationsnapshotid
        WHERE s.payrollperiodid = :period_id AND d.evidencekind = 'DriverRate'
        ORDER BY s.revisionnumber
    """), {"period_id": foundation_db.period_id})).all()
    assert [(int(revision), Decimal(str(amount))) for revision, amount in amounts] == [
        (1, Decimal("10.0000")), (2, Decimal("12.0000")),
    ]
    assert first_snapshot_id != second_snapshot_id

    with pytest.raises(IntegrityError):
        async with foundation_db.conn.begin_nested():
            await foundation_db.conn.execute(text("""
                UPDATE payroll.payrollcalculationsnapshotusedratedefinitions
                SET rateamountsnapshot = 99.0000
                WHERE payrollcalculationsnapshotid = :snapshot_id
            """), {"snapshot_id": first_snapshot_id})


@pytest.mark.asyncio
async def test_evidence_schema_seeds_permissions_and_rejects_cross_snapshot_line_links(
    foundation_db,
):
    assert (await foundation_db.conn.execute(text("""
        SELECT array_agg(permissioncode ORDER BY permissioncode)
        FROM sec.permissions
        WHERE permissioncode IN ('ledger.audit.view', 'ledger.view')
    """))).scalar_one() == ["ledger.audit.view", "ledger.view"]
    assert (await foundation_db.conn.execute(text("""
        SELECT to_regclass('payroll.payrollperiodworkflowactionevidence'),
               to_regclass('payroll.payrollcalculationsnapshotusedratedefinitions')
    """))).one() == (
        "payroll.payrollperiodworkflowactionevidence",
        "payroll.payrollcalculationsnapshotusedratedefinitions",
    )

    first_snapshot_id = await _capture_calculation_snapshot(
        period=await _period_for_snapshot(foundation_db),
        company_id=foundation_db.company_id,
        user_id=foundation_db.user_id,
        packet=_rate_packet(foundation_db, Decimal("10.0000")),
        db=foundation_db.conn,
        context="Submit",
    )
    second_snapshot_id = await _capture_calculation_snapshot(
        period=await _period_for_snapshot(foundation_db),
        company_id=foundation_db.company_id,
        user_id=foundation_db.user_id,
        packet=_rate_packet(foundation_db, Decimal("10.0000")),
        db=foundation_db.conn,
        context="Resubmit",
    )
    definition_id = int((await foundation_db.conn.execute(text("""
        SELECT payrollcalculationsnapshotusedratedefinitionid
        FROM payroll.payrollcalculationsnapshotusedratedefinitions
        WHERE payrollcalculationsnapshotid = :snapshot_id
        ORDER BY payrollcalculationsnapshotusedratedefinitionid
        LIMIT 1
    """), {"snapshot_id": second_snapshot_id})).scalar_one())
    driver_total_id = int((await foundation_db.conn.execute(text("""
        SELECT payrollcalculationdrivertotalid
        FROM payroll.payrollcalculationdrivertotals
        WHERE payrollcalculationsnapshotid = :snapshot_id
        ORDER BY payrollcalculationdrivertotalid
        LIMIT 1
    """), {"snapshot_id": first_snapshot_id})).scalar_one())
    with pytest.raises(IntegrityError):
        async with foundation_db.conn.begin_nested():
            await foundation_db.conn.execute(text("""
                UPDATE payroll.payrollcalculationsnapshotlines
                SET calculatedamount = 0
                WHERE payrollcalculationdrivertotalid = :driver_total_id
            """), {"driver_total_id": driver_total_id})

    with pytest.raises(IntegrityError):
        async with foundation_db.conn.begin_nested():
            await foundation_db.conn.execute(text("""
                INSERT INTO payroll.payrollcalculationsnapshotlines
                    (payrollcalculationdrivertotalid, sourcetype, linetype,
                     calculatedamount, sourceevidencejsonb, usedratedefinitionid)
                VALUES
                    (:driver_total_id, 'System', 'TEST_SCOPE', 0, '{}'::jsonb,
                     :definition_id)
            """), {
                "driver_total_id": driver_total_id,
                "definition_id": definition_id,
            })

    with pytest.raises(IntegrityError):
        async with foundation_db.conn.begin_nested():
            await foundation_db.conn.execute(text("""
                INSERT INTO payroll.payrollperiodworkflowactionevidence
                    (companyid, branchid, payrollperiodid, payrollcalculationsnapshotid,
                     actioncode, actoruserid, actordisplaynamesnapshot,
                     requiredpermissioncode, responsibilitycontextsnapshot)
                VALUES
                    (:company_id, :wrong_branch_id, :period_id, :snapshot_id,
                     'SUBMITTED', :user_id, 'Wrong scope', 'payroll.entry', '{}'::jsonb)
            """), {
                "company_id": foundation_db.company_id,
                "wrong_branch_id": foundation_db.branch_id + 999999,
                "period_id": foundation_db.period_id,
                "snapshot_id": first_snapshot_id,
                "user_id": foundation_db.user_id,
            })
