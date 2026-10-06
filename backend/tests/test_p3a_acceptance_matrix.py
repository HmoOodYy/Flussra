"""Focused P3a currency authority, immutable evidence and read integrity proofs."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.company_currency import get_company_currency
from app.payroll import finalized_library_read_model, report_read_model, status_payment_sync
from app.payroll import rates as rates_service
from app.payroll.schemas import DriverRateCreate
from app.settings import service as settings_service
from app.settings.schemas import CompanyUpdate


@pytest.mark.asyncio
@pytest.mark.parametrize(("state_kind", "state", "cleanup_sql"), [
    ("driver_rate", "PendingApproval", "UPDATE payroll.driverrates SET status='Voided' WHERE driverrateid=:id"),
    ("driver_rate", "Approved", "UPDATE payroll.driverrates SET status='Voided' WHERE driverrateid=:id"),
    ("driver_rate", "Superseded", "UPDATE payroll.driverrates SET status='Voided' WHERE driverrateid=:id"),
    ("driver_rate", "Voided", None),
    ("driver_rate_tier", "PendingApproval", "UPDATE payroll.driverrates SET status='Voided' WHERE driverrateid=:id"),
    ("bonus", "Active", "UPDATE payroll.payrollbonusevents SET status='Voided', voidedatutc=NOW(), voidedbyuserid=:uid, voidreason='cleanup' WHERE payrollbonuseventid=:id"),
    ("bonus", "Voided", None),
    ("pay_rule", "Active", "UPDATE payroll.driverpayrules SET status='Voided' WHERE driverpayruleid=:id"),
    ("pay_rule", "Ended", "UPDATE payroll.driverpayrules SET status='Voided' WHERE driverpayruleid=:id"),
    ("pay_rule", "Voided", None),
    ("draft_rate", "Active", "UPDATE payroll.payrolldraftlines SET status='Void' WHERE draftlineid=:id"),
    ("draft_calculated", "Active", "UPDATE payroll.payrolldraftlines SET status='Void' WHERE draftlineid=:id"),
    ("draft_voided", "Void", None),
    ("snapshot", "Frozen", None),
    ("final_line", "Frozen", None),
])
async def test_each_durable_monetary_state_locks_company_currency(
    test_engine, monkeypatch, state_kind, state, cleanup_sql,
):
    async def allow(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings_service, "_ensure_company_admin", allow)
    monkeypatch.setattr(settings_service, "_check_branch_access", allow)
    monkeypatch.setattr(settings_service, "_write_settings_audit", allow)

    async with test_engine.connect() as db:
        tx = await db.begin()
        try:
            marker = uuid4().hex[:16]
            company_id = int((await db.execute(text("""
                INSERT INTO core.companies(companycode, companyname)
                VALUES (:code, 'P3a acceptance') RETURNING companyid
            """), {"code": f"P3AM_{marker}"})).scalar_one())
            branch_id = int((await db.execute(text("""
                INSERT INTO core.branches(companyid, branchcode, branchname, status, isdefault)
                VALUES (:cid, :code, 'P3a acceptance', 'Active', TRUE) RETURNING branchid
            """), {"cid": company_id, "code": f"P3AB_{marker}"})).scalar_one())
            employee_id = int((await db.execute(text("""
                INSERT INTO core.employees(companyid, branchid, fullname, employeetype, employmentstatus, createdbyuserid)
                VALUES (:cid, :bid, 'P3a acceptance', 'Driver', 'Active', 1) RETURNING employeeid
            """), {"cid": company_id, "bid": branch_id})).scalar_one())
            driver_id = int((await db.execute(text("""
                INSERT INTO core.drivers(companyid, branchid, employeeid, drivercode, driverstatus)
                VALUES (:cid, :bid, :eid, :code, 'Active') RETURNING driverid
            """), {"cid": company_id, "bid": branch_id, "eid": employee_id, "code": f"P3AD_{marker}"})).scalar_one())
            period_id = int((await db.execute(text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (:cid, :bid, 'Open', :code, 'P3a acceptance', 'Week', DATE '2099-01-01', DATE '2099-01-07')
                RETURNING payrollperiodid
            """), {"cid": company_id, "bid": branch_id, "code": f"P3AP_{marker}"})).scalar_one())
            rate_type_id = int((await db.execute(text("SELECT ratetypeid FROM payroll.ratetypes ORDER BY ratetypeid LIMIT 1"))).scalar_one())
            await settings_service.update_company_profile(
                company_id, 1, CompanyUpdate(company_name="P3a acceptance", currency_code="USD"), db,
            )

            params = {"cid": company_id, "bid": branch_id, "did": driver_id,
                      "eid": employee_id, "pid": period_id,
                      "rid": rate_type_id, "uid": 1, "state": state,
                      "hash": "a" * 64, "code": f"P3A_{marker}"}
            if state_kind in {"driver_rate", "driver_rate_tier"}:
                row_id = int((await db.execute(text("""
                    INSERT INTO payroll.driverrates
                        (companyid, branchid, driverid, ratetypeid, amount, effectivefrom, status,
                         createdbyuserid, approvedbyuserid, approvedatutc)
                    VALUES (:cid,:bid,:did,:rid,10,DATE '2099-01-01',:state,1,1,NOW())
                    RETURNING driverrateid
                """), params)).scalar_one())
                if state_kind == "driver_rate_tier":
                    await db.execute(text("""
                        INSERT INTO payroll.driverratetiers(driverrateid, tiersequence, fromunit, tounit, tieramount)
                        VALUES (:id, 1, 0, 10, 5)
                    """), {"id": row_id})
            elif state_kind == "bonus":
                row_id = int((await db.execute(text("""
                    INSERT INTO payroll.payrollbonusevents
                        (companyid, branchid, payrollperiodid, driverid, amount, reason, status,
                         createdbyuserid, voidedbyuserid, voidedatutc, voidreason)
                    VALUES (:cid,:bid,:pid,:did,10,'P3a',CAST(:state AS VARCHAR(30)),1,
                            CASE WHEN CAST(:state AS TEXT)='Voided' THEN 1 END,
                            CASE WHEN CAST(:state AS TEXT)='Voided' THEN NOW() END,
                            CASE WHEN CAST(:state AS TEXT)='Voided' THEN 'cleanup' END)
                    RETURNING payrollbonuseventid
                """), params)).scalar_one())
            elif state_kind == "pay_rule":
                row_id = int((await db.execute(text("""
                    INSERT INTO payroll.driverpayrules
                        (companyid, branchid, driverid, ruletype, amount, effectivefrom, effectiveto, status)
                    VALUES (:cid,:bid,:did,'MinimumPay',10,DATE '2099-01-01',
                            CASE WHEN CAST(:state AS TEXT)='Ended' THEN DATE '2099-01-07' END,CAST(:state AS VARCHAR(30)))
                    RETURNING driverpayruleid
                """), params)).scalar_one())
            elif state_kind in {"draft_rate", "draft_calculated", "draft_voided"}:
                amount = "rateamount" if state_kind == "draft_rate" else "calculatedamount"
                status = "Void" if state_kind == "draft_voided" else "Active"
                row_id = int((await db.execute(text(f"""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverid, workdate, linetype,
                         {amount}, sourcetype, sourceid, status)
                    VALUES (:cid,:bid,:pid,:did,DATE '2099-01-01','OTHER',10,'P3a',:code,:status)
                    RETURNING draftlineid
                """), {**params, "status": status})).scalar_one())
            elif state_kind == "snapshot":
                row_id = int((await db.execute(text("""
                    INSERT INTO payroll.payrollcalculationsnapshots
                        (companyid, branchid, payrollperiodid, revisionnumber, calculationversion,
                         sourceconfighash, snapshothash, createdbyuserid, totalexpectedpay,
                         currencycode, currencyminorunitdigits)
                    VALUES (:cid,:bid,:pid,1,'payroll-calculation-v1',:hash,:hash,1,10,'USD',2)
                    RETURNING payrollcalculationsnapshotid
                """), params)).scalar_one())
            elif state_kind == "final_line":
                await db.execute(text("SELECT set_config('app.allow_payroll_final_line_insert','true',true)"))
                row_id = int((await db.execute(text("""
                    INSERT INTO payroll.payrollfinallines
                        (companyid, branchid, payrollperiodid, driverid, linetype, linescope,
                         finalamount, sourcetype, approvedbyuserid, approvedatutc, currencycode,
                         currencyminorunitdigits)
                    VALUES (:cid,:bid,:pid,:did,'OTHER','Period',10,'P3a',1,NOW(),'USD',2)
                    RETURNING finallineid
                """), params)).scalar_one())

            with pytest.raises(HTTPException) as blocked:
                await settings_service.update_company_profile(
                    company_id, 1, CompanyUpdate(company_name="P3a acceptance", currency_code="EUR"), db,
                )
            assert blocked.value.detail["code"] == "COMPANY_CURRENCY_CHANGE_BLOCKED"
            if cleanup_sql is not None:
                await db.execute(text(cleanup_sql), {"id": row_id, "uid": 1})
                with pytest.raises(HTTPException) as still_blocked:
                    await settings_service.update_company_profile(
                        company_id, 1, CompanyUpdate(company_name="P3a acceptance", currency_code="EUR"), db,
                    )
                assert still_blocked.value.detail["code"] == "COMPANY_CURRENCY_CHANGE_BLOCKED"
        finally:
            await tx.rollback()


@pytest.mark.asyncio
async def test_unconfigured_company_with_durable_state_fails_closed(test_engine, monkeypatch):
    async def allow(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings_service, "_check_branch_access", allow)
    async with test_engine.connect() as db:
        tx = await db.begin()
        try:
            marker = uuid4().hex[:16]
            cid = int((await db.execute(text("INSERT INTO core.companies(companycode,companyname) VALUES (:c,'P3a invalid') RETURNING companyid"), {"c": f"P3AI_{marker}"})).scalar_one())
            bid = int((await db.execute(text("INSERT INTO core.branches(companyid,branchcode,branchname,status,isdefault) VALUES (:c,:b,'P3a invalid','Active',TRUE) RETURNING branchid"), {"c": cid, "b": f"P3AIB_{marker}"})).scalar_one())
            eid = int((await db.execute(text("INSERT INTO core.employees(companyid,branchid,fullname,employeetype,employmentstatus,createdbyuserid) VALUES (:c,:b,'P3a invalid','Driver','Active',1) RETURNING employeeid"), {"c": cid, "b": bid})).scalar_one())
            did = int((await db.execute(text("INSERT INTO core.drivers(companyid,branchid,employeeid,drivercode,driverstatus) VALUES (:c,:b,:e,:d,'Active') RETURNING driverid"), {"c": cid, "b": bid, "e": eid, "d": f"P3AID_{marker}"})).scalar_one())
            period_id = int((await db.execute(text("INSERT INTO payroll.payrollperiods(companyid,branchid,status,periodcode,periodname,periodtype,startdate,enddate) VALUES (:c,:b,'Open',:p,'P3a invalid','Week',DATE '2099-01-01',DATE '2099-01-07') RETURNING payrollperiodid"), {"c": cid, "b": bid, "p": f"P3AIP_{marker}"})).scalar_one())
            await db.execute(text("INSERT INTO payroll.payrolldraftlines(companyid,branchid,payrollperiodid,driverid,workdate,linetype,rateamount,sourcetype,sourceid) VALUES (:c,:b,:p,:d,DATE '2099-01-01','OTHER',10,'P3a',:s)"), {"c":cid,"b":bid,"p":period_id,"d":did,"s":marker})
            with pytest.raises(HTTPException) as profile_error:
                await settings_service.get_company_profile(cid, 1, db)
            assert profile_error.value.detail["code"] == "COMPANY_CURRENCY_INVARIANT_VIOLATION"
            with pytest.raises(HTTPException) as read_error:
                await get_company_currency(cid, db)
            assert read_error.value.detail["code"] == "COMPANY_CURRENCY_INVARIANT_VIOLATION"
        finally:
            await tx.rollback()


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


@pytest.mark.asyncio
async def test_unauthorized_rate_request_cannot_probe_company_currency(monkeypatch):
    currency_gate_called = False

    async def deny_driver_subject(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail="DRIVER/Self cannot manage rates")

    async def currency_probe(*_args, **_kwargs):
        nonlocal currency_gate_called
        currency_gate_called = True
        raise HTTPException(status_code=422, detail={"code": "COMPANY_CURRENCY_REQUIRED"})

    monkeypatch.setattr(rates_service, "_require_non_driver_rate_subject", deny_driver_subject)
    monkeypatch.setattr(rates_service, "lock_and_get_company_currency_for_monetary_write", currency_probe)
    request = DriverRateCreate(
        driver_id=1, rate_type_id=1, amount=10, effective_from=date(2099, 1, 1),
    )
    with pytest.raises(HTTPException) as denied:
        await rates_service.create_rate(1, 2, request, db=object())
    assert denied.value.status_code == 403
    assert "COMPANY_CURRENCY_REQUIRED" not in str(denied.value.detail)
    assert currency_gate_called is False


class _FirstMappingResult:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class _StatusPaymentWithoutCurrencyDB:
    def __init__(self, existing):
        self.existing = existing
        self.writes = []

    async def execute(self, statement, _params):
        query = str(statement)
        if "SELECT payrollperioddriverdayentrystateid" in query:
            return _FirstMappingResult({"payrollperioddriverdayentrystateid": 7})
        if "FROM   payroll.payrollstatuskeys sk" in query:
            return _FirstMappingResult({
                "statuskeyid": 3, "statuscode": "ON_TIME", "keyname": "On time",
                "hoursvalue": 1, "statusratecolumnid": 4, "src_col_name": "StatusPay",
                "ratetypeid": 5, "ratecode": "STATUS_PAY",
            })
        if "SELECT draftlineid, sourceid" in query:
            return _FirstMappingResult(self.existing)
        self.writes.append(query)
        return _FirstMappingResult(None)


@pytest.mark.asyncio
async def test_status_payment_without_currency_keeps_source_and_clears_stale_money():
    db = _StatusPaymentWithoutCurrencyDB({"draftlineid": 31, "sourceid": "STATUS_PAYMENT:7:3:4"})
    await status_payment_sync._sync_status_payment_for_entry_state(
        company_id=1, branch_id=2, period_id=3, driver_id=4,
        work_date=date(2099, 1, 1), status_key_id=3, user_id=5, db=db,
        currency=None,
    )
    assert len(db.writes) == 1
    assert "UPDATE payroll.payrolldraftlines SET status = 'Void'" in db.writes[0]
    assert not any("INSERT INTO payroll.payrolldraftlines" in query for query in db.writes)

    fresh = _StatusPaymentWithoutCurrencyDB(None)
    await status_payment_sync._sync_status_payment_for_entry_state(
        company_id=1, branch_id=2, period_id=3, driver_id=4,
        work_date=date(2099, 1, 1), status_key_id=3, user_id=5, db=fresh,
        currency=None,
    )
    assert fresh.writes == []
