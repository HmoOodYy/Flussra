"""P6C finalized rate/rule and Bonus evidence HTTP contracts."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.payroll.service import (
    _CalculationPacketDriverTotal,
    _CalculationPacketLine,
    _capture_calculation_snapshot,
    _create_period_pay_item_rows,
    _LiveCalculationPacket,
    finalize_period,
)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_finalized_rates_period(
    direct_db, test_database_url: str, branch_id: int, *, include_bonus: bool = True,
) -> dict[str, int | str]:
    """Build a finalized packet with one used rate and an optional frozen Bonus event."""
    marker = uuid4().hex
    await direct_db.execute(text("""
        UPDATE payroll.payrollperiods
        SET status = 'Cancelled', currentreturnreviewitemid = NULL
        WHERE companyid = 1 AND branchid = :branch_id AND periodcode LIKE 'P6C-%'
          AND status IN ('Draft', 'Open', 'InReview', 'Returned', 'Approved')
    """), {"branch_id": branch_id})
    employee_id = int((await direct_db.execute(text("""
        INSERT INTO core.employees
            (companyid, branchid, fullname, employeetype, employmentstatus, createdbyuserid)
        VALUES (1, :branch_id, :name, 'Driver', 'Active', 1)
        RETURNING employeeid
    """), {"branch_id": branch_id, "name": f"P6C employee {marker}"})).scalar_one())
    driver_id = int((await direct_db.execute(text("""
        INSERT INTO core.drivers (companyid, branchid, employeeid, drivercode, driverstatus)
        VALUES (1, :branch_id, :employee_id, :code, 'Active')
        RETURNING driverid
    """), {
        "branch_id": branch_id, "employee_id": employee_id, "code": f"P6C-{marker[:20]}",
    })).scalar_one())
    period_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (1, :branch_id, 'Open', :code, :name, 'Week', '2097-01-01', '2097-01-07')
        RETURNING payrollperiodid
    """), {
        "branch_id": branch_id, "code": f"P6C-{marker}", "name": f"P6C {marker}",
    })).scalar_one())
    await _create_period_pay_item_rows(period_id, 1, branch_id, date(2097, 1, 1), direct_db)
    pay_item_id = int((await direct_db.execute(text("""
        SELECT payitemid
        FROM payroll.payrollperiodpayitems
        WHERE payrollperiodid = :period_id AND payitemcode = 'HOURS'
        LIMIT 1
    """), {"period_id": period_id})).scalar_one())
    rate_type_id = int((await direct_db.execute(text("""
        SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = 'HOURLY'
    """))).scalar_one())
    rate_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.driverrates
            (companyid, branchid, driverid, ratetypeid, amount, effectivefrom, status, createdbyuserid)
        VALUES (1, :branch_id, :driver_id, :rate_type_id, 10.0000, '2097-01-01', 'Approved', 1)
        RETURNING driverrateid
    """), {
        "branch_id": branch_id, "driver_id": driver_id, "rate_type_id": rate_type_id,
    })).scalar_one())
    unused_rate_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.driverrates
            (companyid, branchid, driverid, ratetypeid, amount, effectivefrom, status, createdbyuserid)
        VALUES (1, :branch_id, :driver_id, :rate_type_id, 99.0000, '2097-01-01', 'Voided', 1)
        RETURNING driverrateid
    """), {
        "branch_id": branch_id, "driver_id": driver_id, "rate_type_id": rate_type_id,
    })).scalar_one())
    pay_rule_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.driverpayrules
            (companyid, branchid, driverid, ruletype, amount, effectivefrom, status, createdbyuserid)
        VALUES (1, :branch_id, :driver_id, 'MinimumPay', 1.0000, '2097-01-01', 'Active', 1)
        RETURNING driverpayruleid
    """), {"branch_id": branch_id, "driver_id": driver_id})).scalar_one())
    bonus_id: int | None = None
    if include_bonus:
        bonus_id = int((await direct_db.execute(text("""
            INSERT INTO payroll.payrollbonusevents
                (companyid, branchid, payrollperiodid, driverid, amount, reason, notes, status,
                 createdbyuserid, createdatutc, datarevision)
            VALUES (1, :branch_id, :period_id, :driver_id, 4.0000, 'P6C frozen reason',
                    'P6C frozen notes', 'Active', 1, NOW(), 7)
            RETURNING payrollbonuseventid
        """), {
            "branch_id": branch_id, "period_id": period_id, "driver_id": driver_id,
        })).scalar_one())
    period = SimpleNamespace(
        payroll_period_id=period_id, branch_id=branch_id, period_code=f"P6C-{marker}",
        period_type="Week", start_date=date(2097, 1, 1), end_date=date(2097, 1, 7),
    )
    lines = [_CalculationPacketLine(
        source_type="DraftLine", source_id=f"P6C:{marker}", line_type="HOURS", line_scope="Daily",
        work_date=date(2097, 1, 1), driver_id=driver_id, quantity=Decimal("2"),
        resolved_rate_amount=Decimal("10"), calculated_amount=Decimal("20"),
        needs_manager_review=False, blocker_reason=None, driver_rate_id=rate_id,
        pay_item_id=pay_item_id, source_evidence={"RateBehavior": "PerUnit"},
    ), _CalculationPacketLine(
        source_type="System", source_id=f"P6C-rule:{marker}", line_type="SYS_MIN_TOPUP",
        line_scope="Period", work_date=None, driver_id=driver_id, quantity=None,
        resolved_rate_amount=None, calculated_amount=Decimal("1"), needs_manager_review=False,
        blocker_reason=None, source_evidence={"DriverPayRuleID": pay_rule_id},
    )]
    if bonus_id is not None:
        lines.append(_CalculationPacketLine(
            source_type="BonusEvent", source_id=str(bonus_id), line_type="BONUS", line_scope="Period",
            work_date=None, driver_id=driver_id, quantity=None, resolved_rate_amount=None,
            calculated_amount=Decimal("4"), needs_manager_review=False, blocker_reason=None,
            bonus_event_id=bonus_id, source_evidence={"PayrollBonusEventID": bonus_id},
        ))
    total = sum((line.calculated_amount or Decimal("0") for line in lines), Decimal("0"))
    packet = _LiveCalculationPacket(
        payroll_period_id=period_id, company_id=1, branch_id=branch_id, status="Open",
        blockers=[], warnings=[], total_expected_pay=total,
        drivers=[_CalculationPacketDriverTotal(
            driver_id=driver_id, driver_code="P6C-FROZEN", driver_name="P6C Frozen Driver",
            daily_pay=Decimal("20"), status_pay=Decimal("0"), period_pay=Decimal("0"),
            minimum_adjustment=Decimal("1"), maximum_adjustment=Decimal("0"),
            bonus_total=Decimal("4") if bonus_id is not None else Decimal("0"),
            expected_pay=total, needs_manager_review=False, blockers=[], lines=lines,
        )],
    )
    snapshot_id = int(await _capture_calculation_snapshot(
        period=period, company_id=1, user_id=1, packet=packet, db=direct_db, context="Submit",
    ))
    await direct_db.execute(text("""
        INSERT INTO review.managerreviewitems
            (companyid, branchid, requestedbyuserid, requesttype, entityschema, entityname,
             entityid, title, priority, status, payrollcalculationsnapshotid)
        VALUES (1, :branch_id, 1, 'PeriodApproval', 'payroll', 'PayrollPeriods', :period_id,
                'P6C finalized packet', 'Normal', 'Approved', :snapshot_id)
    """), {"branch_id": branch_id, "period_id": str(period_id), "snapshot_id": snapshot_id})
    await direct_db.execute(text("""
        UPDATE payroll.payrollperiods SET status = 'Approved' WHERE payrollperiodid = :period_id
    """), {"period_id": period_id})
    await direct_db.commit()
    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.begin() as finalize_db:
            result = await finalize_period(period_id, 1, 1, finalize_db)
            assert result.status == "Locked"
    finally:
        await engine.dispose()
    return {
        "period_id": period_id, "snapshot_id": snapshot_id, "driver_id": driver_id,
        "rate_id": rate_id, "unused_rate_id": unused_rate_id, "pay_rule_id": pay_rule_id,
        "bonus_id": bonus_id or 0,
    }


async def _scoped_permission_token(
    client: httpx.AsyncClient, admin_token: str, branch_id: int, permission_codes: list[str],
    *, return_user_id: bool = False,
) -> str | tuple[str, int]:
    marker = uuid4().hex
    role = await client.post(
        "/admin/company-roles", json={"role_name": f"P6C {marker}"}, headers=_auth(admin_token),
    )
    assert role.status_code == 201, role.text
    role_id = role.json()["company_role_id"]
    assigned = await client.put(
        f"/admin/company-roles/{role_id}/permissions",
        json={"permission_codes": permission_codes}, headers=_auth(admin_token),
    )
    assert assigned.status_code == 200, assigned.text
    username = f"p6c_{marker[:20]}"
    user = await client.post("/admin/users", json={
        "username": username, "display_name": username, "password": "TestPass1234!",
        "is_active": True, "can_login": True, "must_change_password": False,
    }, headers=_auth(admin_token))
    assert user.status_code == 201, user.text
    assignment = await client.post(
        f"/admin/users/{user.json()['user_id']}/company-role-assignments",
        json={"company_role_id": role_id, "scope_type": "SpecificBranch", "branch_id": branch_id},
        headers=_auth(admin_token),
    )
    assert assignment.status_code == 201, assignment.text
    login = await client.post("/auth/login", json={
        "username": username, "password": "TestPass1234!", "company_code": "DEMO",
    })
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    return (token, int(user.json()["user_id"])) if return_user_id else token


async def _ledger_token_with_driver_scope(
    client: httpx.AsyncClient, admin_token: str, branch_id: int, scope_type: str,
) -> str:
    token, user_id = await _scoped_permission_token(
        client, admin_token, branch_id, ["ledger.view"], return_user_id=True,
    )
    roles = await client.get("/admin/company-roles", headers=_auth(admin_token))
    assert roles.status_code == 200, roles.text
    driver_role_id = next(role["company_role_id"] for role in roles.json() if role["role_code"] == "DRIVER")
    assignment = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json={"company_role_id": driver_role_id, "scope_type": scope_type, "branch_id": branch_id},
        headers=_auth(admin_token),
    )
    assert assignment.status_code == 201, assignment.text
    return token


@pytest.mark.asyncio
async def test_rates_used_route_returns_only_linked_frozen_rate_and_bonus_evidence(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    seed = await _seed_finalized_rates_period(direct_db, test_database_url, paytest_branch_id)
    response = await session_client.get(
        f"/payroll/finalized/{seed['period_id']}/rates-used", headers=_auth(auth_token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metadata"]["snapshot_id"] == seed["snapshot_id"]
    assert body["metadata"]["rate_evidence_available"] is True
    assert body["metadata"]["section_availability"]["rates_rules"]["state"] == "AVAILABLE"
    assert len(body["used_rate_definitions"]) == 2
    definition = next(
        item for item in body["used_rate_definitions"] if item["evidence_kind"] == "DriverRate"
    )
    assert definition["driver_rate_id"] == seed["rate_id"]
    assert definition["driver_rate_id"] != seed["unused_rate_id"]
    assert Decimal(definition["rate_amount"]) == Decimal("10")
    assert definition["pay_item_code"] == "HOURS"
    assert definition["snapshot_line_ids"]
    assert definition["line_use_count"] == len(definition["snapshot_line_ids"])
    rule = next(
        item for item in body["used_rate_definitions"] if item["evidence_kind"] == "DriverPayRule"
    )
    assert rule["driver_pay_rule_id"] == seed["pay_rule_id"]
    assert rule["rule_type"] == "MinimumPay"
    assert Decimal(rule["rule_amount"]) == Decimal("1")
    assert len(body["bonus_events"]) == 1
    bonus = body["bonus_events"][0]
    assert bonus["bonus_event_id"] == seed["bonus_id"]
    assert Decimal(bonus["amount"]) == Decimal("4")
    assert bonus["reason"] == "P6C frozen reason"
    assert bonus["notes"] == "P6C frozen notes"
    assert bonus["creator_user_id"] == 1
    assert bonus["creator_display_name"]
    assert bonus["created_at_utc"]
    archived = await session_client.patch(
        f"/payroll/periods/{seed['period_id']}/status", json={"status": "Archived"}, headers=_auth(auth_token),
    )
    assert archived.status_code == 200, archived.text
    archived_response = await session_client.get(
        f"/payroll/finalized/{seed['period_id']}/rates-used", headers=_auth(auth_token),
    )
    assert archived_response.status_code == 200, archived_response.text


@pytest.mark.asyncio
async def test_rates_used_route_is_ledger_only_finalized_and_scope_protected(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    hq_branch_id: int, direct_db, test_database_url: str,
):
    seed = await _seed_finalized_rates_period(direct_db, test_database_url, paytest_branch_id)
    ledger_token = await _scoped_permission_token(
        session_client, auth_token, paytest_branch_id, ["ledger.view"],
    )
    assert (await session_client.get(
        f"/payroll/finalized/{seed['period_id']}/rates-used", headers=_auth(ledger_token),
    )).status_code == 200
    for permissions in ([], ["payroll.view"], ["ledger.audit.view"]):
        token = await _scoped_permission_token(session_client, auth_token, paytest_branch_id, permissions)
        denied = await session_client.get(
            f"/payroll/finalized/{seed['period_id']}/rates-used", headers=_auth(token),
        )
        assert denied.status_code == 403
        assert str(seed["period_id"]) not in denied.text
    hq_token = await _scoped_permission_token(session_client, auth_token, hq_branch_id, ["ledger.view"])
    foreign_branch = await session_client.get(
        f"/payroll/finalized/{seed['period_id']}/rates-used", headers=_auth(hq_token),
    )
    assert foreign_branch.status_code == 403
    assert str(seed["period_id"]) not in foreign_branch.text
    for scope_type in ("SpecificBranch", "OwnDriverDataOnly"):
        driver_token = await _ledger_token_with_driver_scope(
            session_client, auth_token, paytest_branch_id, scope_type,
        )
        denied = await session_client.get(
            f"/payroll/finalized/{seed['period_id']}/rates-used", headers=_auth(driver_token),
        )
        assert denied.status_code == 403
        assert str(seed["period_id"]) not in denied.text
    non_finalized_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (1, :branch_id, 'Approved', :code, 'P6C pending', 'Week', '2097-02-01', '2097-02-07')
        RETURNING payrollperiodid
    """), {"branch_id": paytest_branch_id, "code": f"P6C-PENDING-{uuid4().hex}"})).scalar_one())
    await direct_db.commit()
    unavailable = await session_client.get(
        f"/payroll/finalized/{non_finalized_id}/rates-used", headers=_auth(ledger_token),
    )
    assert unavailable.status_code == 422
    marker = uuid4().hex[:12]
    company_id = int((await direct_db.execute(text("""
        INSERT INTO core.companies
            (companycode, companyname, legalname, status, issuspended, timezonename)
        VALUES (:code, :name, :name, 'Active', FALSE, 'UTC')
        RETURNING companyid
    """), {"code": f"P6C-{marker}", "name": f"P6C foreign {marker}"})).scalar_one())
    branch_id = int((await direct_db.execute(text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (:company_id, :code, :name, 'Active', TRUE)
        RETURNING branchid
    """), {
        "company_id": company_id, "code": f"P6C-{marker}", "name": f"P6C foreign {marker}",
    })).scalar_one())
    foreign_period_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (:company_id, :branch_id, 'Locked', :code, :name, 'Week', '2097-03-01', '2097-03-07')
        RETURNING payrollperiodid
    """), {
        "company_id": company_id, "branch_id": branch_id,
        "code": f"P6C-{marker}", "name": f"P6C foreign {marker}",
    })).scalar_one())
    await direct_db.commit()
    try:
        foreign_company = await session_client.get(
            f"/payroll/finalized/{foreign_period_id}/rates-used", headers=_auth(auth_token),
        )
        assert foreign_company.status_code == 404
        assert str(foreign_period_id) not in foreign_company.text
    finally:
        await direct_db.execute(text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :period_id"), {
            "period_id": foreign_period_id,
        })
        await direct_db.execute(text("DELETE FROM core.branches WHERE branchid = :branch_id"), {
            "branch_id": branch_id,
        })
        await direct_db.execute(text("DELETE FROM core.companies WHERE companyid = :company_id"), {
            "company_id": company_id,
        })
        await direct_db.commit()


@pytest.mark.asyncio
async def test_rates_used_keeps_frozen_evidence_after_current_source_mutation_attempt(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    seed = await _seed_finalized_rates_period(direct_db, test_database_url, paytest_branch_id)
    # The canonical mutable Bonus row can change; the route deliberately reads only snapshot evidence.
    async with direct_db.begin():
        await direct_db.execute(text("""
            UPDATE payroll.payrollbonusevents SET reason = 'Mutable replacement', notes = 'Mutable replacement'
            WHERE payrollbonuseventid = :bonus_id
        """), {"bonus_id": seed["bonus_id"]})
    response = await session_client.get(
        f"/payroll/finalized/{seed['period_id']}/rates-used", headers=_auth(auth_token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    frozen_rate = next(
        item for item in body["used_rate_definitions"] if item["evidence_kind"] == "DriverRate"
    )
    assert Decimal(frozen_rate["rate_amount"]) == Decimal("10")
    assert body["bonus_events"][0]["reason"] == "P6C frozen reason"
    assert body["bonus_events"][0]["notes"] == "P6C frozen notes"


@pytest.mark.asyncio
async def test_rates_used_distinguishes_new_empty_bonus_evidence(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    seed = await _seed_finalized_rates_period(
        direct_db, test_database_url, paytest_branch_id, include_bonus=False,
    )
    response = await session_client.get(
        f"/payroll/finalized/{seed['period_id']}/rates-used", headers=_auth(auth_token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metadata"]["report_evidence_available"] is True
    assert body["metadata"]["section_availability"]["bonus_evidence"]["state"] == "EMPTY"
    assert body["bonus_events"] == []
