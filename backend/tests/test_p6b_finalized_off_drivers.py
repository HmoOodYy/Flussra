"""P6B finalized Off/Status route coverage over immutable snapshot evidence."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.company_currency import CompanyCurrency
from app.payroll.service import (
    _CalculationPacketDriverTotal,
    _CalculationPacketLine,
    _capture_calculation_snapshot,
    _create_period_pay_item_rows,
    _LiveCalculationPacket,
    finalize_period,
)
from tests.access_test_helpers import create_provisioned_test_user
from tests.builders.access import create_user_with_role_token, get_company_role_id
from tests.db_state import allow_final_line_insert


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture(scope="session")
async def paytest_branch_id(session_db_conn) -> int:
    row = (await session_db_conn.execute(
        text("""
            INSERT INTO core.branches
                (companyid, branchcode, branchname, status, isdefault)
            VALUES (1, :code, :name, 'Active', FALSE)
            RETURNING branchid
        """), {"code": f"P6B_{uuid4().hex[:10]}", "name": "P6B isolated"},
    )).mappings().one()
    await session_db_conn.commit()
    return int(row["branchid"])


@pytest_asyncio.fixture(scope="session")
async def paytest_driver_id(session_client, auth_token, paytest_branch_id) -> int:
    response = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": paytest_branch_id,
            "full_name": f"P6B Driver {uuid4().hex[:8]}",
            "driver_code": f"P6B-{uuid4().hex[:10]}",
        },
        headers=_auth(auth_token),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["driver_id"])


async def _scoped_permission_token(
    client: httpx.AsyncClient, admin_token: str, branch_id: int, permission_codes: list[str],
    *, return_user_id: bool = False,
) -> str | tuple[str, int]:
    marker = uuid4().hex
    role = await client.post(
        "/admin/company-roles", json={"role_name": f"P6B {marker}"}, headers=_auth(admin_token),
    )
    assert role.status_code == 201, role.text
    role_id = role.json()["company_role_id"]
    assigned = await client.put(
        f"/admin/company-roles/{role_id}/permissions",
        json={"permission_codes": permission_codes}, headers=_auth(admin_token),
    )
    assert assigned.status_code == 200, assigned.text
    username = f"p6b_{marker[:20]}"
    user = await create_provisioned_test_user(
        client, admin_token, username, role_id, scope_type="SpecificBranch",
        branch_id=branch_id, password="TestPass1234!",
    )
    login = await client.post("/auth/login", json={
        "username": username, "password": "TestPass1234!", "company_code": "DEMO",
    })
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    if return_user_id:
        return token, int(user["user_id"])
    return token


async def _ledger_token_with_driver_scope(
    client: httpx.AsyncClient, admin_token: str, branch_id: int, scope_type: str,
) -> str:
    token = await create_user_with_role_token(
        client, admin_token, f"p6b_self_{uuid4().hex[:12]}",
        await get_company_role_id(client, admin_token, "DRIVER"),
        scope_type="Self", driver_branch_id=branch_id,
    )
    user = await client.get("/auth/me", headers=_auth(token))
    assert user.status_code == 200, user.text
    override = await client.put(
        f"/admin/users/{user.json()['user_id']}/permission-overrides",
        headers=_auth(admin_token), json={"permission_codes": ["ledger.view"]},
    )
    assert override.status_code == 422, override.text
    assert "cannot receive generic permission overrides" in override.json()["detail"]
    return token


async def _canonical_authority(
    direct_db, branch_id: int, start_date: date, *, company_id: int = 1,
) -> dict[str, int | str]:
    """Create or reuse one canonical Payroll Setup authority for the branch."""
    existing = (await direct_db.execute(text("""
        SELECT a.branchpayrollsetupassignmentid AS assignment_id,
               v.payrollsetupversionid AS version_id,
               s.payrollsetupid AS setup_id, s.setupcode AS setup_code
        FROM payroll.branchpayrollsetupassignments a
        JOIN payroll.payrollsetups s ON s.payrollsetupid = a.payrollsetupid
        JOIN payroll.payrollsetupversions v
          ON v.payrollsetupid = s.payrollsetupid AND v.companyid = s.companyid
        WHERE a.companyid = :company_id AND a.branchid = :branch_id
          AND a.effectivefromdate = :start_date
          AND v.lifecyclestate = 'Published'
        ORDER BY a.branchpayrollsetupassignmentid
        LIMIT 1
    """), {
        "company_id": company_id, "branch_id": branch_id, "start_date": start_date,
    })).mappings().first()
    if existing is not None:
        return dict(existing) | {"config_hash": "b" * 64}

    setup_code = f"P6B_{company_id}_{branch_id}_{start_date:%Y%m%d}"
    setup_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollsetups
            (companyid, setupcode, setupname, status, createdbyuserid)
        VALUES (:company_id, :setup_code, :setup_name, 'Active', 1)
        RETURNING payrollsetupid
    """), {
        "company_id": company_id, "setup_code": setup_code,
        "setup_name": f"P6B {company_id}/{branch_id}",
    })).scalar_one())
    version_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollsetupversions
            (companyid, payrollsetupid, lifecyclestate, versionnumber,
             effectivefromdate, payrollfrequency, anchorstartdate,
             normaldaysoffmask, confighash, publishedbyuserid, publishedatutc)
        VALUES (:company_id, :setup_id, 'Published', 1, :start_date, 'Week', :start_date,
                0, :config_hash, 1, NOW())
        RETURNING payrollsetupversionid
    """), {
        "company_id": company_id, "setup_id": setup_id,
        "start_date": start_date, "config_hash": "b" * 64,
    })).scalar_one())
    assignment_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.branchpayrollsetupassignments
            (companyid, branchid, payrollsetupid, effectivefromdate)
        VALUES (:company_id, :branch_id, :setup_id, :start_date)
        RETURNING branchpayrollsetupassignmentid
    """), {
        "company_id": company_id, "branch_id": branch_id,
        "setup_id": setup_id, "start_date": start_date,
    })).scalar_one())
    await direct_db.commit()
    return {
        "assignment_id": assignment_id, "version_id": version_id,
        "setup_id": setup_id, "setup_code": setup_code, "config_hash": "b" * 64,
    }


async def _insert_canonical_period_without_day_snapshot(
    direct_db, branch_id: int, start_date: date, *, status: str, code: str, name: str,
    company_id: int = 1,
) -> int:
    authority = await _canonical_authority(
        direct_db, branch_id, start_date, company_id=company_id,
    )
    end_date = start_date + timedelta(days=6)
    return int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype,
             startdate, enddate, branchpayrollsetupassignmentid, payrollsetupversionid,
             frozenpayrollsetupid, frozenpayrollsetupcode, frozenpayrollsetupversionnumber,
             frozenpayrollfrequency, frozenanchorstartdate, frozennormaldaysoffmask,
             scheduleconfighash)
        VALUES (:company_id, :branch_id, :status, :code, :name, 'Week', :start_date, :end_date,
                :assignment_id, :version_id, :setup_id, :setup_code, 1, 'Week',
                :start_date, 0, :config_hash)
        RETURNING payrollperiodid
    """), {
        "company_id": company_id, "branch_id": branch_id, "status": status,
        "code": code, "name": name,
        "start_date": start_date, "end_date": end_date, **authority,
    })).scalar_one())


async def _seed_finalized_off_period(
    direct_db, test_database_url: str, branch_id: int, *, status_days: int = 7,
    normal_work: bool = False, resubmittable: bool = False, finalize: bool = True,
    non_work_financial_sources: tuple[str, ...] = (),
) -> dict[str, int | date]:
    marker = uuid4().hex
    start_date = date(2096, 1, 1)
    end_date = start_date + timedelta(days=6)
    employee_id = int((await direct_db.execute(text("""
        INSERT INTO core.employees
            (companyid, branchid, fullname, employeetype, employmentstatus, createdbyuserid)
        VALUES (1, :branch_id, :name, 'Driver', 'Active', 1)
        RETURNING employeeid
    """), {"branch_id": branch_id, "name": f"P6B employee {marker}"})).scalar_one())
    driver_id = int((await direct_db.execute(text("""
        INSERT INTO core.drivers (companyid, branchid, employeeid, drivercode, driverstatus)
        VALUES (1, :branch_id, :employee_id, :code, 'Active')
        RETURNING driverid
    """), {
        "branch_id": branch_id, "employee_id": employee_id, "code": f"P6B-{marker[:20]}",
    })).scalar_one())
    authority = await _canonical_authority(direct_db, branch_id, start_date)
    period_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype,
             startdate, enddate, branchpayrollsetupassignmentid, payrollsetupversionid,
             frozenpayrollsetupid, frozenpayrollsetupcode, frozenpayrollsetupversionnumber,
             frozenpayrollfrequency, frozenanchorstartdate, frozennormaldaysoffmask,
             scheduleconfighash)
        VALUES (1, :branch_id, 'Open', :code, :name, 'Week', :start_date, :end_date,
                :assignment_id, :version_id, :setup_id, :setup_code, 1, 'Week',
                :start_date, 0, :config_hash)
        RETURNING payrollperiodid
    """), {
        "branch_id": branch_id, **authority,
        "code": f"P6B-{marker}", "name": f"P6B {marker}",
        "start_date": start_date, "end_date": end_date,
    })).scalar_one())
    days = [start_date + timedelta(days=offset) for offset in range(7)]
    for work_date in days:
        await direct_db.execute(text("""
            INSERT INTO payroll.payrollperioddays
                (payrollperiodid, companyid, branchid,
                 branchpayrollsetupassignmentid, payrollsetupversionid, workdate, dayofweek,
                 isdefaultworkday, isconfiguredoffday, isaddedworkday)
            VALUES (:period_id, 1, :branch_id, :assignment_id, :version_id, :work_date, :day_of_week,
                    TRUE, FALSE, FALSE)
        """), {
            "period_id": period_id, "branch_id": branch_id,
            "assignment_id": authority["assignment_id"], "version_id": authority["version_id"],
            "work_date": work_date,
            "day_of_week": (work_date.weekday() + 1) % 7,
        })
    await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiodeligibilitysnapshots
            (payrollperiodid, companyid, branchid, snapshotsource)
        VALUES (:period_id, 1, :branch_id, 'Generated')
    """), {"period_id": period_id, "branch_id": branch_id})
    await direct_db.execute(text("""
        INSERT INTO payroll.payrollperioddrivereligibility
            (companyid, branchid, payrollperiodid, driverid, drivercodesnapshot, drivernamesnapshot,
             iseligibleforperiod, eligibilityreasoncode, snapshotsource)
        VALUES (1, :branch_id, :period_id, :driver_id, 'P6B-FROZEN', 'P6B Frozen Driver',
                TRUE, 'Active', 'Generated')
    """), {"branch_id": branch_id, "period_id": period_id, "driver_id": driver_id})
    await _create_period_pay_item_rows(period_id, 1, branch_id, start_date, direct_db)
    hours_pay_item_id = int((await direct_db.execute(text("""
        SELECT payitemid FROM payroll.payrollperiodpayitems
        WHERE payrollperiodid = :period_id AND payitemcode = 'HOURS'
        LIMIT 1
    """), {"period_id": period_id})).scalar_one())
    status_key_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollstatuskeys
            (companyid, branchid, statuscode, normalizedstatuscode, keyname,
             hoursvalue, isoffreason, isactive, displayorder)
        VALUES (1, :branch_id, :code, :code, 'Frozen P6B Off', 0, TRUE, TRUE, 999)
        RETURNING statuskeyid
    """), {"branch_id": branch_id, "code": f"P6B_OFF_{marker[:12].upper()}"})).scalar_one())
    for work_date in days[:status_days]:
        await direct_db.execute(text("""
            INSERT INTO payroll.payrollperioddriverdayentrystate
                (companyid, branchid, payrollperiodid, workdate, driverid, statuskeyid, notetext,
                 isvoided, createdbyuserid, updatedbyuserid, createdatutc, updatedatutc)
            VALUES (1, :branch_id, :period_id, :work_date, :driver_id, :status_key_id,
                    'Frozen P6B note', FALSE, 1, 1, NOW(), NOW())
        """), {
            "branch_id": branch_id, "period_id": period_id, "work_date": work_date,
            "driver_id": driver_id, "status_key_id": status_key_id,
        })
    lines = [_CalculationPacketLine(
        source_type="System", source_id=f"p6b-system:{marker}", line_type="SYS_MIN_TOPUP",
        line_scope="Period", work_date=None, driver_id=driver_id, quantity=Decimal("1"),
        resolved_rate_amount=None, calculated_amount=Decimal("1"), needs_manager_review=False,
        blocker_reason=None, source_evidence={"P6B": marker},
    )]
    if normal_work:
        lines.append(_CalculationPacketLine(
            source_type="DraftLine", source_id=f"p6b-work:{marker}", line_type="HOURS",
            line_scope="Daily", work_date=days[0], driver_id=driver_id, quantity=Decimal("1"),
            resolved_rate_amount=None, calculated_amount=Decimal("0"), needs_manager_review=False,
            blocker_reason=None, pay_item_id=hours_pay_item_id, source_evidence={"P6B": marker},
        ))
    for source_type in non_work_financial_sources:
        lines.append(_CalculationPacketLine(
            source_type=source_type, source_id=f"p6b-{source_type.lower()}:{marker}",
            line_type="BONUS" if source_type == "BonusEvent" else "STATUS_PAYMENT",
            line_scope="Period" if source_type == "BonusEvent" else "Daily",
            work_date=None if source_type == "BonusEvent" else days[0], driver_id=driver_id,
            quantity=Decimal("1"), resolved_rate_amount=None, calculated_amount=Decimal("1"),
            needs_manager_review=False, blocker_reason=None, source_evidence={"P6B": marker},
        ))
    if resubmittable:
        await direct_db.execute(text("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid, workdate, linetype, quantity, sourcetype, status, needsmanagerreview, addedbyuserid)
            VALUES (1, :branch_id, :period_id, :driver_id, :work_date, 'HOURS', 1.0000, 'Manual', 'Active', FALSE, 1)
        """), {
            "branch_id": branch_id, "period_id": period_id, "driver_id": driver_id,
            "work_date": days[0],
        })
        rate_type_id = int((await direct_db.execute(text("""
            SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = 'HOURLY'
        """))).scalar_one())
        await direct_db.execute(text("""
            INSERT INTO payroll.driverrates
                (companyid, branchid, driverid, ratetypeid, amount, effectivefrom, status, createdbyuserid)
            VALUES (1, :branch_id, :driver_id, :rate_type_id, 10.0000, :effective_from, 'Approved', 1)
        """), {
            "branch_id": branch_id, "driver_id": driver_id, "rate_type_id": rate_type_id,
            "effective_from": start_date,
        })
    expected_pay = sum((line.calculated_amount or Decimal("0") for line in lines), Decimal("0"))
    bonus_total = sum(
        (line.calculated_amount or Decimal("0") for line in lines if line.source_type == "BonusEvent"),
        Decimal("0"),
    )
    status_pay = sum(
        (line.calculated_amount or Decimal("0") for line in lines if line.source_type == "StatusEntryState"),
        Decimal("0"),
    )
    period = SimpleNamespace(
        payroll_period_id=period_id, branch_id=branch_id, period_code=f"P6B-{marker}",
        period_type="Week", start_date=start_date, end_date=end_date,
    )
    packet = _LiveCalculationPacket(
        payroll_period_id=period_id, company_id=1, branch_id=branch_id, status="Open",
        blockers=[], warnings=[], total_expected_pay=expected_pay,
        drivers=[_CalculationPacketDriverTotal(
            driver_id=driver_id, driver_code="P6B-FROZEN", driver_name="P6B Frozen Driver",
            daily_pay=Decimal("0"), status_pay=status_pay,
            minimum_adjustment=Decimal("1"), maximum_adjustment=Decimal("0"),
            bonus_total=bonus_total, expected_pay=expected_pay, needs_manager_review=False,
            blockers=[], lines=lines,
        )],
    )
    snapshot_id = int(await _capture_calculation_snapshot(currency=CompanyCurrency("USD", 2),
        period=period, company_id=1, user_id=1, packet=packet, db=direct_db, context="Submit",
    ))
    review_id = int((await direct_db.execute(text("""
        INSERT INTO review.managerreviewitems
            (companyid, branchid, requestedbyuserid, requesttype, entityschema, entityname,
             entityid, title, priority, status, payrollcalculationsnapshotid)
        VALUES (1, :branch_id, 1, 'PeriodApproval', 'payroll', 'PayrollPeriods', :period_id,
                'P6B approved packet', 'Normal', :review_status, :snapshot_id)
        RETURNING reviewitemid
    """), {
        "branch_id": branch_id, "period_id": str(period_id), "snapshot_id": snapshot_id,
        "review_status": "Approved" if finalize else "Pending",
    })).scalar_one())
    await direct_db.execute(text("""
        UPDATE payroll.payrollperiods SET status = :status WHERE payrollperiodid = :period_id
    """), {"period_id": period_id, "status": "Approved" if finalize else "InReview"})
    await direct_db.commit()
    if finalize:
        engine = create_async_engine(test_database_url, echo=False)
        try:
            async with engine.begin() as finalize_db:
                result = await finalize_period(period_id, 1, 1, finalize_db)
                assert result.status == "Locked"
        finally:
            await engine.dispose()
    return {
        "period_id": period_id, "snapshot_id": snapshot_id, "driver_id": driver_id,
        "employee_id": employee_id, "status_key_id": status_key_id, "start_date": start_date,
        "review_id": review_id,
    }


@pytest.mark.asyncio
async def test_finalized_off_drivers_use_frozen_status_calendar_and_eligibility(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    seed = await _seed_finalized_off_period(direct_db, test_database_url, paytest_branch_id)
    response = await session_client.get(
        f"/payroll/finalized/{seed['period_id']}/off-drivers", headers=_auth(auth_token),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["metadata"]["snapshot_id"] == seed["snapshot_id"]
    assert payload["metadata"]["section_availability"]["off_drivers"]["state"] == "AVAILABLE"
    assert payload["total_fully_off_drivers"] == 1
    assert payload["fully_off_drivers"][0]["eligible_scheduled_day_count"] == 7
    assert payload["status_entries"][0]["status_label"] == "Frozen P6B Off"
    assert payload["status_entries"][0]["driver_name"] == "P6B Frozen Driver"
    await direct_db.execute(text("""
        UPDATE payroll.payrollstatuskeys SET keyname = 'Mutable P6B label', isoffreason = FALSE
        WHERE statuskeyid = :status_key_id
    """), {"status_key_id": seed["status_key_id"]})
    await direct_db.execute(text("""
        UPDATE core.employees SET employmentstatus = 'Inactive' WHERE employeeid = :employee_id
    """), {"employee_id": seed["employee_id"]})
    await direct_db.execute(text("""
        UPDATE core.drivers SET driverstatus = 'Inactive' WHERE driverid = :driver_id
    """), {"driver_id": seed["driver_id"]})
    await direct_db.commit()
    frozen = await session_client.get(
        f"/payroll/finalized/{seed['period_id']}/off-drivers", headers=_auth(auth_token),
    )
    assert frozen.status_code == 200, frozen.text
    assert frozen.json()["total_fully_off_drivers"] == 1
    assert frozen.json()["status_entries"][0]["status_label"] == "Frozen P6B Off"
    assert frozen.json()["status_entries"][0]["driver_name"] == "P6B Frozen Driver"
    archived = await session_client.patch(
        f"/payroll/periods/{seed['period_id']}/status", json={"status": "Archived"}, headers=_auth(auth_token),
    )
    assert archived.status_code == 200, archived.text
    assert (await session_client.get(
        f"/payroll/finalized/{seed['period_id']}/off-drivers", headers=_auth(auth_token),
    )).status_code == 200


@pytest.mark.asyncio
async def test_finalized_off_drivers_preserve_missing_status_and_normal_work_disqualification(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    missing_status = await _seed_finalized_off_period(
        direct_db, test_database_url, paytest_branch_id, status_days=6,
    )
    normal_work = await _seed_finalized_off_period(
        direct_db, test_database_url, paytest_branch_id, normal_work=True,
    )
    for seed in (missing_status, normal_work):
        response = await session_client.get(
            f"/payroll/finalized/{seed['period_id']}/off-drivers", headers=_auth(auth_token),
        )
        assert response.status_code == 200, response.text
        assert response.json()["total_fully_off_drivers"] == 0
        assert response.json()["metadata"]["section_availability"]["off_drivers"]["state"] == "EMPTY"


@pytest.mark.asyncio
async def test_finalized_off_drivers_do_not_treat_bonus_or_status_payment_as_normal_work(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    seed = await _seed_finalized_off_period(
        direct_db, test_database_url, paytest_branch_id,
        non_work_financial_sources=("BonusEvent", "StatusEntryState"),
    )
    response = await session_client.get(
        f"/payroll/finalized/{seed['period_id']}/off-drivers", headers=_auth(auth_token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["total_fully_off_drivers"] == 1


@pytest.mark.asyncio
async def test_finalized_off_drivers_distinguish_zero_evidence_from_snapshot_unavailable(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    paytest_driver_id: int, direct_db, test_database_url: str,
):
    zero_evidence = await _seed_finalized_off_period(
        direct_db, test_database_url, paytest_branch_id, status_days=0,
    )
    zero_response = await session_client.get(
        f"/payroll/finalized/{zero_evidence['period_id']}/off-drivers", headers=_auth(auth_token),
    )
    assert zero_response.status_code == 200, zero_response.text
    assert zero_response.json()["metadata"]["report_evidence_available"] is True
    assert zero_response.json()["metadata"]["section_availability"]["status_evidence"]["state"] == "EMPTY"

    marker = uuid4().hex
    snapshot_unavailable_period_id = await _insert_canonical_period_without_day_snapshot(
        direct_db, paytest_branch_id, date(2096, 1, 1), status="Locked",
        code=f"P6B-NOSNAPSHOT-{marker}", name="P6B no snapshot",
    )
    async with allow_final_line_insert(direct_db):
        await direct_db.execute(text("""
            INSERT INTO payroll.payrollfinallines
                (companyid, branchid, payrollperiodid, driverid, workdate, linetype, linescope,
                 quantity, finalamount, sourcetype, approvedbyuserid, approvedatutc, lockedatutc, CurrencyCode, CurrencyMinorUnitDigits)
            VALUES (1, :branch_id, :period_id, :driver_id, '2076-01-01', 'HOURS', 'Daily', 1,
                    12.0000, 'DraftLine', 1, NOW(), NOW(), 'USD', 2)
        """), {
                "branch_id": paytest_branch_id, "period_id": snapshot_unavailable_period_id,
            "driver_id": paytest_driver_id,
        })
    await direct_db.commit()
    legacy_response = await session_client.get(
        f"/payroll/finalized/{snapshot_unavailable_period_id}/off-drivers",
        headers=_auth(auth_token),
    )
    assert legacy_response.status_code == 200, legacy_response.text
    assert legacy_response.json()["metadata"]["report_evidence_available"] is False
    assert legacy_response.json()["metadata"]["section_availability"]["status_evidence"]["state"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_finalized_off_drivers_enforce_ledger_role_branch_and_company_scope(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    hq_branch_id: int, direct_db, test_database_url: str,
):
    seed = await _seed_finalized_off_period(direct_db, test_database_url, paytest_branch_id)
    ledger_token = await _scoped_permission_token(
        session_client, auth_token, paytest_branch_id, ["ledger.view"],
    )
    assert (await session_client.get(
        f"/payroll/finalized/{seed['period_id']}/off-drivers", headers=_auth(ledger_token),
    )).status_code == 200
    for permissions in ([], ["payroll.view"], ["ledger.audit.view"]):
        token = await _scoped_permission_token(
            session_client, auth_token, paytest_branch_id, permissions,
        )
        denied = await session_client.get(
            f"/payroll/finalized/{seed['period_id']}/off-drivers", headers=_auth(token),
        )
        assert denied.status_code == 403
    for scope_type in ("Self",):
        token = await _ledger_token_with_driver_scope(
            session_client, auth_token, paytest_branch_id, scope_type,
        )
        denied = await session_client.get(
            f"/payroll/finalized/{seed['period_id']}/off-drivers", headers=_auth(token),
        )
        assert denied.status_code == 403
        assert str(seed["period_id"]) not in denied.text
    branch_limited = await _scoped_permission_token(
        session_client, auth_token, hq_branch_id, ["ledger.view"],
    )
    foreign_branch = await session_client.get(
        f"/payroll/finalized/{seed['period_id']}/off-drivers", headers=_auth(branch_limited),
    )
    assert foreign_branch.status_code == 403
    assert str(seed["period_id"]) not in foreign_branch.text
    non_finalized_id = await _insert_canonical_period_without_day_snapshot(
        direct_db, paytest_branch_id, date(2096, 1, 1), status="Approved",
        code=f"P6B-APPROVED-{uuid4().hex}", name="P6B non-finalized",
    )
    await direct_db.commit()
    non_finalized = await session_client.get(
        f"/payroll/finalized/{non_finalized_id}/off-drivers", headers=_auth(ledger_token),
    )
    assert non_finalized.status_code == 422

    marker = uuid4().hex[:12]
    company_id = int((await direct_db.execute(text("""
        INSERT INTO core.companies
            (companycode, companyname, legalname, status, issuspended, timezonename)
        VALUES (:code, :name, :name, 'Active', FALSE, 'UTC')
        RETURNING companyid
    """), {"code": f"P6B-{marker}", "name": f"P6B foreign {marker}"})).scalar_one())
    branch_id = int((await direct_db.execute(text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (:company_id, :code, :name, 'Active', TRUE)
        RETURNING branchid
    """), {
        "company_id": company_id, "code": f"P6B-{marker}", "name": f"P6B foreign {marker}",
    })).scalar_one())
    foreign_period_id = await _insert_canonical_period_without_day_snapshot(
        direct_db, branch_id, date(2076, 2, 1), status="Locked",
        code=f"P6B-{marker}", name=f"P6B foreign {marker}", company_id=company_id,
    )
    await direct_db.commit()
    try:
        foreign_company = await session_client.get(
            f"/payroll/finalized/{foreign_period_id}/off-drivers", headers=_auth(auth_token),
        )
        assert foreign_company.status_code == 404
        assert str(foreign_period_id) not in foreign_company.text
    finally:
        await direct_db.execute(text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :period_id"), {
            "period_id": foreign_period_id,
        })
        await direct_db.commit()


@pytest.mark.asyncio
async def test_finalized_off_drivers_use_status_evidence_from_exact_resubmitted_snapshot(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    seed = await _seed_finalized_off_period(
        direct_db, test_database_url, paytest_branch_id, status_days=1,
        resubmittable=True, finalize=False,
    )
    returned = await session_client.post(
        f"/review/items/{seed['review_id']}/decide",
        json={"decision": "EditRequested", "decision_reason": "Correct frozen Status source"},
        headers=_auth(auth_token),
    )
    assert returned.status_code == 200, returned.text
    replacement_status_key_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollstatuskeys
            (companyid, branchid, statuscode, normalizedstatuscode, keyname,
             hoursvalue, isoffreason, isactive, displayorder)
        VALUES (1, :branch_id, :code, :code, 'Resubmitted Frozen Off', 0, TRUE, TRUE, 998)
        RETURNING statuskeyid
    """), {
        "branch_id": paytest_branch_id, "code": f"P6B_RESUB_{uuid4().hex[:12].upper()}",
    })).scalar_one())
    await direct_db.execute(text("""
        UPDATE payroll.payrollperioddriverdayentrystate
        SET statuskeyid = :status_key_id, updatedatutc = NOW(), updatedbyuserid = 1
        WHERE payrollperiodid = :period_id AND driverid = :driver_id AND workdate = :work_date
    """), {
        "status_key_id": replacement_status_key_id, "period_id": seed["period_id"],
        "driver_id": seed["driver_id"], "work_date": seed["start_date"],
    })
    await direct_db.commit()
    resubmitted = await session_client.post(
        f"/payroll/periods/{seed['period_id']}/resubmissions", headers=_auth(auth_token),
    )
    assert resubmitted.status_code == 200, resubmitted.text
    review = (await direct_db.execute(text("""
        SELECT reviewitemid, payrollcalculationsnapshotid
        FROM review.managerreviewitems
        WHERE entityschema = 'payroll' AND entityname = 'PayrollPeriods'
          AND entityid = :period_id AND status = 'Pending'
        ORDER BY reviewitemid DESC
        LIMIT 1
    """), {"period_id": str(seed["period_id"])})).mappings().one()
    assert int(review["payrollcalculationsnapshotid"]) != seed["snapshot_id"]
    approved = await session_client.post(
        f"/review/items/{review['reviewitemid']}/decide",
        json={"decision": "Approved", "decision_reason": "Approve corrected Status source"},
        headers=_auth(auth_token),
    )
    assert approved.status_code == 200, approved.text
    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.begin() as finalize_db:
            result = await finalize_period(seed["period_id"], 1, 1, finalize_db)
            assert result.status == "Locked"
    finally:
        await engine.dispose()
    response = await session_client.get(
        f"/payroll/finalized/{seed['period_id']}/off-drivers", headers=_auth(auth_token),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["metadata"]["snapshot_id"] == int(review["payrollcalculationsnapshotid"])
    assert payload["metadata"]["revision_number"] == 2
    assert payload["status_entries"][0]["status_label"] == "Resubmitted Frozen Off"
