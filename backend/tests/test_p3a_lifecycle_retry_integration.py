"""Route-equivalent Submit/Resubmit whole-transaction retries against PostgreSQL."""
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.db.transaction_retry import run_retryable_transaction
from app.payroll import period_lifecycle
from app.payroll.schemas import PeriodStatusChange


async def _seed_tenant(engine, status, currency_code="USD"):
    marker = uuid4().hex[:14]
    async with engine.begin() as db:
        cid = (await db.execute(text("""
            INSERT INTO core.companies(companycode, companyname, currencycode)
            VALUES (:code, 'P3a retry tenant', :currency_code) RETURNING companyid
        """), {"code": "P3AR_" + marker, "currency_code": currency_code})).scalar_one()
        bid = (await db.execute(text("""
            INSERT INTO core.branches(companyid, branchcode, branchname)
            VALUES (:cid, :code, 'P3a retry branch') RETURNING branchid
        """), {"cid": cid, "code": "P3AR_" + marker})).scalar_one()
        company_role = (await db.execute(text("""
            INSERT INTO sec.companyroles
                (companyid, rolecode, rolename, rolelevel, isdefault, isprotected, iscustom, isactive)
            VALUES (:cid, 'COMPANY_OWNER', 'Company Owner', 100, TRUE, TRUE, FALSE, TRUE)
            RETURNING companyroleid
        """), {"cid": cid})).scalar_one()
        await db.execute(text("""
            INSERT INTO sec.companyrolepermissions(companyroleid, permissioncode)
            SELECT :rid, permissioncode FROM sec.permissions
        """), {"rid": company_role})
        uid = (await db.execute(text("""
            INSERT INTO sec.users(companyid, username, displayname, passwordhash, isactive, canlogin)
            SELECT :cid, :name, 'P3a Test', passwordhash, TRUE, TRUE
            FROM sec.users WHERE companyid=1 AND username='admin'
            RETURNING userid
        """), {"cid": cid, "name": "p3a_" + marker})).scalar_one()
        await db.execute(text("""
            INSERT INTO sec.userbranchroles(userid, companyid, branchid, roleid, companyroleid, scopetype, isactive)
            SELECT :uid, :cid, NULL, roleid, :company_role, 'AllCompanyBranches', TRUE
            FROM sec.roles WHERE rolecode='PAYROLL_ADMIN'
        """), {"uid": uid, "cid": cid, "company_role": company_role})
        eid = (await db.execute(text("""
            INSERT INTO core.employees(companyid, branchid, fullname, employeetype, employmentstatus, createdbyuserid)
            VALUES (:cid, :bid, 'P3a Driver', 'Driver', 'Active', :uid) RETURNING employeeid
        """), {"cid": cid, "bid": bid, "uid": uid})).scalar_one()
        did = (await db.execute(text("""
            INSERT INTO core.drivers(companyid, branchid, employeeid, drivercode, driverstatus)
            VALUES (:cid, :bid, :eid, :code, 'Active') RETURNING driverid
        """), {"cid": cid, "bid": bid, "eid": eid, "code": "P3AR_" + marker})).scalar_one()
        pid = (await db.execute(text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (:cid, :bid, 'Open', :code, 'P3a retry period', 'Week',
                    DATE '2099-01-01', DATE '2099-01-07')
            RETURNING payrollperiodid
        """), {"cid": cid, "bid": bid, "code": "P3AR_" + marker})).scalar_one()
        if status == "Returned":
            # A schema-valid, pre-monetary Returned state lets the race exercise
            # Resubmit while Company currency is still legally changeable.
            review_id = (await db.execute(text("""
                INSERT INTO review.managerreviewitems
                    (companyid, branchid, requestedbyuserid, requesttype,
                     entityschema, entityname, entityid, title, description,
                     priority, status, finaldecisionbyuserid,
                     finaldecisionatutc, finaldecisionreason)
                VALUES (:cid, :bid, :uid, 'PeriodApproval',
                        'payroll', 'PayrollPeriods', :eid, 'P3a return',
                        'Pre-monetary return', 'Normal', 'Rejected',
                        :uid, NOW(), 'Test setup')
                RETURNING reviewitemid
            """), {"cid": cid, "bid": bid, "uid": uid, "eid": str(pid)})).scalar_one()
            await db.execute(text("""
                UPDATE payroll.payrollperiods
                SET status='Returned', currentreturnreviewitemid=:rid
                WHERE payrollperiodid=:pid
            """), {"rid": review_id, "pid": pid})
        await db.execute(text("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid, workdate, linetype,
                 quantity, sourcetype, sourceid, status, needsmanagerreview, linescope)
            VALUES (:cid, :bid, :pid, :did, DATE '2099-01-01', 'DailyNote',
                    1, 'User', :source, 'Active', FALSE, 'Daily')
        """), {"cid": cid, "bid": bid, "pid": pid, "did": did, "source": marker})
    return cid, bid, uid, pid


@pytest.mark.asyncio
@pytest.mark.parametrize("operation_name,status", [("submit", "Open"), ("resubmit", "Returned")])
async def test_lifecycle_change_first_retries_and_freezes_new_currency(
    test_engine, monkeypatch, operation_name, status,
):
    cid, bid, uid, pid = await _seed_tenant(test_engine, status)
    change_held = asyncio.Event()
    first_gate = asyncio.Event()
    changed = asyncio.Event()
    gate_count = 0
    real_gate = period_lifecycle.lock_and_get_company_currency_for_monetary_write

    async def gated(company_id, db):
        nonlocal gate_count
        gate_count += 1
        if gate_count == 1:
            first_gate.set()
            await asyncio.wait_for(changed.wait(), 5)
        return await real_gate(company_id, db)

    monkeypatch.setattr(period_lifecycle, "lock_and_get_company_currency_for_monetary_write", gated)

    async def change():
        async with test_engine.begin() as db:
            await db.execute(text("UPDATE core.companies SET currencycode='EUR' WHERE companyid=:cid"), {"cid": cid})
            change_held.set()
            await asyncio.wait_for(first_gate.wait(), 5)
        changed.set()

    async def operation(db):
        if operation_name == "submit":
            return await period_lifecycle.change_period_status(
                cid, uid, pid, PeriodStatusChange(status="InReview"), db,
            )
        return await period_lifecycle.resubmit_period(cid, uid, pid, db)

    async def no_delay(_seconds):
        return None

    task = asyncio.create_task(change())
    await asyncio.wait_for(change_held.wait(), 5)
    result = await asyncio.wait_for(
        run_retryable_transaction(test_engine, operation, operation_name=operation_name, sleep=no_delay),
        15,
    )
    await task
    assert result.status == "InReview"
    assert gate_count == 2
    async with test_engine.connect() as db:
        snapshots = (await db.execute(text("""
            SELECT currencycode, currencyminorunitdigits, revisionnumber
            FROM payroll.payrollcalculationsnapshots WHERE payrollperiodid=:pid
        """), {"pid": pid})).mappings().all()
        assert [(r["currencycode"], r["currencyminorunitdigits"], r["revisionnumber"]) for r in snapshots] == [("EUR", 2, 1)]
        review_count = (await db.execute(text("""
            SELECT COUNT(*) FROM review.managerreviewitems
            WHERE companyid=:cid AND entityid=:entity AND requesttype='PeriodApproval'
              AND status='Pending'
        """), {"cid": cid, "entity": str(pid)})).scalar_one()
        assert review_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["Open", "Returned"])
async def test_submit_and_resubmit_require_company_currency(test_engine, status):
    cid, _, uid, pid = await _seed_tenant(test_engine, status, currency_code=None)
    async with test_engine.begin() as db:
        with pytest.raises(HTTPException) as error:
            if status == "Open":
                await period_lifecycle.change_period_status(
                    cid, uid, pid, PeriodStatusChange(status="InReview"), db,
                )
            else:
                await period_lifecycle.resubmit_period(cid, uid, pid, db)
        assert error.value.detail["code"] == "COMPANY_CURRENCY_REQUIRED"
        assert (await db.execute(text(
            "SELECT COUNT(*) FROM payroll.payrollcalculationsnapshots WHERE payrollperiodid=:pid"
        ), {"pid": pid})).scalar_one() == 0
        expected = status
        assert (await db.execute(text(
            "SELECT status FROM payroll.payrollperiods WHERE payrollperiodid=:pid"
        ), {"pid": pid})).scalar_one() == expected
