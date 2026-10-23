"""Focused P3a currency authority, immutable evidence and read integrity proofs."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.company_currency import get_company_currency
from app.payroll import finalized_library_read_model, report_read_model
from app.settings import audit as settings_audit
from app.settings import company
from app.settings import security as settings_security
from app.settings.schemas import CompanyUpdate
from tests.builders.final_evidence import insert_final_line, seed_snapshot_component
from tests.builders.pay_rules import insert_pay_rule
from tests.currency_test_helpers import create_rate_requirement, insert_approved_schedule
from tests.db_state import replica_replication_role
from tests.target_seed import seed_period_definition, seed_target_line


@pytest.mark.asyncio
@pytest.mark.parametrize(("state_kind", "state", "cleanup_sql"), [
    ("schedule", "Approved", "UPDATE payroll.DriverRateSchedules SET Status='Voided', VoidedByUserID=:uid, VoidedAtUtc=NOW(), VoidReason='retired' WHERE DriverRateScheduleID=:id"),
    ("schedule", "Superseded", "UPDATE payroll.DriverRateSchedules SET Status='Voided', VoidedByUserID=:uid, VoidedAtUtc=NOW(), VoidReason='retired' WHERE DriverRateScheduleID=:id"),
    ("schedule", "Voided", None),
    ("bonus", "Active", "UPDATE payroll.payrollbonusevents SET status='Voided', voidedatutc=NOW(), voidedbyuserid=:uid, voidreason='cleanup' WHERE payrollbonuseventid=:id"),
    ("bonus", "Voided", None),
    ("pay_rule", "Active", "UPDATE payroll.driverpayrules SET status='Voided' WHERE driverpayruleid=:id"),
    ("pay_rule", "Ended", "UPDATE payroll.driverpayrules SET status='Voided' WHERE driverpayruleid=:id"),
    ("pay_rule", "Voided", None),
    ("snapshot", "Frozen", None),
    ("final_line", "Frozen", None),
])
async def test_each_durable_monetary_state_locks_company_currency(
    test_engine, monkeypatch, state_kind, state, cleanup_sql,
):
    async def allow(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings_security, "ensure_company_admin", allow)
    monkeypatch.setattr(company, "check_branch_access", allow)
    monkeypatch.setattr(settings_audit, "write_settings_audit", allow)

    async with test_engine.connect() as db:
        tx = await db.begin()
        try:
            marker = uuid4().hex[:16]
            company_id = int((await db.execute(text("""
                INSERT INTO core.companies(companycode, companyname)
                VALUES (:code, 'P3a acceptance') RETURNING companyid
            """), {"code": f"P3AM_{marker}"})).scalar_one())
            branch_id = int((await db.execute(text("""
                INSERT INTO core.branches(companyid, branchcode, branchname, status, isdefault, timezoneid)
                VALUES (:cid, :code, 'P3a acceptance', 'Active', TRUE, 'UTC') RETURNING branchid
            """), {"cid": company_id, "code": f"P3AB_{marker}"})).scalar_one())
            employee_id = int((await db.execute(text("""
                INSERT INTO core.employees(companyid, branchid, fullname, employmentstatus, createdbyuserid)
                VALUES (:cid, :bid, 'P3a acceptance', 'Active', 1) RETURNING employeeid
            """), {"cid": company_id, "bid": branch_id})).scalar_one())
            driver_assignment_id = int((await db.execute(text("""
                WITH d AS (
                    INSERT INTO core.drivers(companyid, employeeid, drivernumber, driverstatus)
                    VALUES (:cid, :eid, :code, 'Active') RETURNING driverid
                )
                INSERT INTO core.driverassignments(companyid, branchid, employeeid, driverid, effectivefromat)
                SELECT :cid, :bid, :eid, d.driverid, NOW() - INTERVAL '1 day' FROM d
                RETURNING driverassignmentid
            """), {"cid": company_id, "bid": branch_id, "eid": employee_id, "code": f"P3AD_{marker}"})).scalar_one())
            period_id = int((await db.execute(text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (:cid, :bid, 'Open', :code, 'P3a acceptance', 'Week', DATE '2099-01-01', DATE '2099-01-07')
                RETURNING payrollperiodid
            """), {"cid": company_id, "bid": branch_id, "code": f"P3AP_{marker}"})).scalar_one())
            await company.update_company_profile(
                company_id, 1, CompanyUpdate(company_name="P3a acceptance", currency_code="USD"), db,
            )

            params = {"cid": company_id, "bid": branch_id, "did": driver_assignment_id,
                      "eid": employee_id, "pid": period_id,
                      "uid": 1, "state": state,
                      "hash": "a" * 64, "code": f"P3A_{marker}"}
            if state_kind == "schedule":
                requirement = await create_rate_requirement(db, company_id)
                row_id = await insert_approved_schedule(db, {**params, **requirement, "amount": "10"})
                if state == "Superseded":
                    await db.execute(text("""
                        UPDATE payroll.DriverRateSchedules SET Status = 'Superseded',
                            EffectiveTo = DATE '2099-01-07' WHERE DriverRateScheduleID = :id
                    """), {"id": row_id})
                elif state == "Voided":
                    await db.execute(text("""
                        UPDATE payroll.DriverRateSchedules SET Status = 'Voided',
                            VoidedByUserID = 1, VoidedAtUtc = NOW(), VoidReason = 'retired'
                        WHERE DriverRateScheduleID = :id
                    """), {"id": row_id})
            elif state_kind == "bonus":
                row_id = int((await db.execute(text("""
                    INSERT INTO payroll.payrollbonusevents
                        (companyid, branchid, payrollperiodid, driverassignmentid, amount, reason, status,
                         createdbyuserid, voidedbyuserid, voidedatutc, voidreason)
                    VALUES (:cid,:bid,:pid,:did,10,'P3a',CAST(:state AS VARCHAR(30)),1,
                            CASE WHEN CAST(:state AS TEXT)='Voided' THEN 1 END,
                            CASE WHEN CAST(:state AS TEXT)='Voided' THEN NOW() END,
                            CASE WHEN CAST(:state AS TEXT)='Voided' THEN 'cleanup' END)
                    RETURNING payrollbonuseventid
                """), params)).scalar_one())
            elif state_kind == "pay_rule":
                # A pay rule belongs to the permanent Driver and records a calendar source.
                pay_rule_driver_id = int((await db.execute(text(
                    "SELECT driverid FROM core.driverassignments WHERE driverassignmentid = :did"),
                    params)).scalar_one())
                row_id = await insert_pay_rule(
                    db, company_id=company_id, driver_id=pay_rule_driver_id, branch_id=branch_id,
                    amount=10, start="2099-01-01",
                    end="2099-01-07" if state == "Ended" else None, status=state)
            elif state_kind == "snapshot":
                row_id = (await seed_snapshot_component(
                    db, period_id=params["pid"], driver_assignment_id=params["did"])).snapshot_id
            elif state_kind == "final_line":
                chain = await seed_snapshot_component(
                    db, period_id=params["pid"], driver_assignment_id=params["did"])
                row_id = await insert_final_line(db, chain)

            with pytest.raises(HTTPException) as blocked:
                await company.update_company_profile(
                    company_id, 1, CompanyUpdate(company_name="P3a acceptance", currency_code="EUR"), db,
                )
            assert blocked.value.detail["code"] == "COMPANY_CURRENCY_CHANGE_BLOCKED"
            if cleanup_sql is not None:
                await db.execute(text(cleanup_sql), {"id": row_id, "uid": 1})
                with pytest.raises(HTTPException) as still_blocked:
                    await company.update_company_profile(
                        company_id, 1, CompanyUpdate(company_name="P3a acceptance", currency_code="EUR"), db,
                    )
                assert still_blocked.value.detail["code"] == "COMPANY_CURRENCY_CHANGE_BLOCKED"
        finally:
            await tx.rollback()


@pytest.mark.asyncio
async def test_unconfigured_company_with_durable_state_fails_closed(test_engine, monkeypatch):
    async def allow(*_args, **_kwargs):
        return None

    monkeypatch.setattr(company, "check_branch_access", allow)
    async with test_engine.connect() as db:
        tx = await db.begin()
        try:
            marker = uuid4().hex[:16]
            cid = int((await db.execute(text("INSERT INTO core.companies(companycode,companyname,currencycode) VALUES (:c,'P3a invalid','USD') RETURNING companyid"), {"c": f"P3AI_{marker}"})).scalar_one())
            bid = int((await db.execute(text("INSERT INTO core.branches(companyid,branchcode,branchname,status,isdefault, timezoneid) VALUES (:c,:b,'P3a invalid','Active',TRUE, 'UTC') RETURNING branchid"), {"c": cid, "b": f"P3AIB_{marker}"})).scalar_one())
            eid = int((await db.execute(text("INSERT INTO core.employees(companyid, branchid, fullname, employmentstatus, createdbyuserid) VALUES (:c, :b, 'P3a invalid', 'Active', 1) RETURNING employeeid"), {"c": cid, "b": bid})).scalar_one())
            did = int((await db.execute(text("WITH x AS (INSERT INTO core.drivers(companyid,employeeid,drivernumber,driverstatus) VALUES (:c,:e,:d,'Active') RETURNING driverid) INSERT INTO core.driverassignments(companyid,branchid,employeeid,driverid,effectivefromat) SELECT :c,:b,:e,x.driverid,NOW() - INTERVAL '1 day' FROM x RETURNING driverassignmentid"), {"c": cid, "b": bid, "e": eid, "d": f"P3AID_{marker}"})).scalar_one())
            requirement = await create_rate_requirement(db, cid)
            await insert_approved_schedule(db, {"cid": cid, "did": did, **requirement, "amount": "10"})
            # Simulate corrupt persisted authority after a valid approval; normal writes reject this state.
            async with replica_replication_role(db):
                await db.execute(text("UPDATE core.Companies SET CurrencyCode = NULL WHERE CompanyID = :cid"), {"cid": cid})
            with pytest.raises(HTTPException) as profile_error:
                await company.get_company_profile(cid, 1, db)
            assert profile_error.value.detail["code"] == "COMPANY_CURRENCY_INVARIANT_VIOLATION"
            with pytest.raises(HTTPException) as read_error:
                await get_company_currency(cid, db)
            assert read_error.value.detail["code"] == "COMPANY_CURRENCY_INVARIANT_VIOLATION"
        finally:
            await tx.rollback()


@pytest.mark.asyncio
async def test_ordinary_source_facts_do_not_lock_company_currency(test_engine):
    from tests.currency_test_helpers import create_branch_and_driver

    async with test_engine.connect() as db:
        transaction = await db.begin()
        try:
            code = "SOURCE_" + uuid4().hex[:12]
            cid = int((await db.execute(text("""
                INSERT INTO core.Companies (CompanyCode, CompanyName, CurrencyCode)
                VALUES (:code, :code, 'USD') RETURNING CompanyID
            """), {"code": code})).scalar_one())
            bid, assignment = await create_branch_and_driver(db, cid, code)
            period = int((await db.execute(text("""
                INSERT INTO payroll.PayrollPeriods
                    (CompanyID, BranchID, Status, PeriodCode, PeriodName, PeriodType, StartDate, EndDate)
                VALUES (:cid, :bid, 'Open', :code, :code, 'Week', '2099-01-01', '2099-01-07')
                RETURNING PayrollPeriodID
            """), {"cid": cid, "bid": bid, "code": code})).scalar_one())
            definition = await seed_period_definition(db, company_id=cid, branch_id=bid, period_id=period)
            await seed_target_line(db, company_id=cid, period_id=period, branch_id=bid,
                                   driver_assignment_id=assignment, definition=definition,
                                   quantity="8", work_date="2099-01-01")
            await db.execute(text("UPDATE core.Companies SET CurrencyCode = 'EUR' WHERE CompanyID = :cid"), {"cid": cid})
            currency = await get_company_currency(cid, db)
            assert currency.code == "EUR"
        finally:
            await transaction.rollback()


class _FakeResult:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one(self):
        return self.row


class _FinalCurrencyDB:
    def __init__(self, row):
        self.row = row
        self.queries = 0

    async def execute(self, *_args, **_kwargs):
        self.queries += 1
        return _FakeResult(self.row)


@pytest.mark.asyncio
@pytest.mark.parametrize(("row", "expected_code"), [
    ({"row_count": 0, "pair_count": 0, "currency_code": None, "minor_unit_digits": None}, "SNAPSHOT_CURRENCY_REQUIRED"),
    ({"row_count": 2, "pair_count": 2, "currency_code": "EUR", "minor_unit_digits": 2}, "SNAPSHOT_CURRENCY_MISMATCH"),
])
async def test_report_final_lines_currency_rejects_empty_or_corrupt_authority(row, expected_code):
    db = _FinalCurrencyDB(row)
    with pytest.raises(HTTPException) as error:
        await report_read_model._final_lines_currency(1, 99, db)
    assert error.value.detail.startswith(expected_code)
    assert db.queries == 1


class _CurrencyBatchDB:
    def __init__(self):
        self.queries = []

    async def execute(self, statement, params):
        self.queries.append(str(statement))
        if "FROM payroll.payrollfinallines" in str(statement):
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: [
                {"period_id": 10, "row_count": 1, "pair_count": 1, "currency_code": "USD", "minor_unit_digits": 2},
            ]))
        return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: [
            {"period_id": 11, "row_count": 1, "pair_count": 1, "currency_code": "JPY", "minor_unit_digits": 0},
        ]))


@pytest.mark.asyncio
async def test_finalized_currency_page_resolves_multiple_periods_setwise():
    db = _CurrencyBatchDB()
    currencies = await finalized_library_read_model._approved_currencies([
        {"period_id": 10, "company_id": 1}, {"period_id": 11, "company_id": 1},
    ], db)
    assert currencies[10].code == "USD" and currencies[10].minor_unit_digits == 2
    assert currencies[11].code == "JPY" and currencies[11].minor_unit_digits == 0
    assert len(db.queries) == 2
