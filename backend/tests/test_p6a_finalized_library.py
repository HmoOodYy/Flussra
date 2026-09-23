"""P6A finalized-library HTTP contracts over immutable FinalLines authority."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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


@pytest_asyncio.fixture(scope="session")
async def paytest_branch_id(session_db_conn) -> int:
    row = (await session_db_conn.execute(
        text("""
            INSERT INTO core.branches
                (companyid, branchcode, branchname, status, isdefault)
            VALUES (1, :code, :name, 'Active', FALSE)
            RETURNING branchid
        """), {"code": f"P6A_{uuid4().hex[:10]}", "name": "P6A isolated"},
    )).mappings().one()
    await session_db_conn.commit()
    return int(row["branchid"])


@pytest_asyncio.fixture(scope="session")
async def paytest_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> int:
    """Create a driver whose company/branch key matches P6A legacy lines."""
    response = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": paytest_branch_id,
            "full_name": "P6A Isolated Driver",
            "driver_code": f"P6A-D-{uuid4().hex[:10]}",
        },
        headers=_auth(auth_token),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["driver_id"])


async def _seed_finalized_period(
    direct_db, test_database_url: str, branch_id: int, *, finalize: bool = True,
) -> tuple[int, int, int, int]:
    """Create a real CP-4D/CP-4F finalized packet for P6A's public routes."""
    marker = uuid4().hex
    employee_id = int((await direct_db.execute(text("""
        INSERT INTO core.employees
            (companyid, branchid, fullname, employeetype, employmentstatus, createdbyuserid)
        VALUES (1, :branch_id, :name, 'Driver', 'Active', 1)
        RETURNING employeeid
    """), {"branch_id": branch_id, "name": f"P6A employee {marker}"})).scalar_one())
    driver_id = int((await direct_db.execute(text("""
        INSERT INTO core.drivers (companyid, branchid, employeeid, drivercode, driverstatus)
        VALUES (1, :branch_id, :employee_id, :code, 'Active')
        RETURNING driverid
    """), {
        "branch_id": branch_id, "employee_id": employee_id, "code": f"P6A-{marker[:20]}",
    })).scalar_one())
    period_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (1, :branch_id, 'Open', :code, :name, 'Week', '2098-01-01', '2098-01-07')
        RETURNING payrollperiodid
    """), {
        "branch_id": branch_id, "code": f"P6A-{marker}", "name": f"P6A {marker}",
    })).scalar_one())
    await _create_period_pay_item_rows(
        period_id, 1, branch_id, date(2098, 1, 1), direct_db,
    )
    hours_pay_item_id = int((await direct_db.execute(text("""
        SELECT payitemid FROM payroll.payrollperiodpayitems
        WHERE payrollperiodid = :period_id AND payitemcode = 'HOURS'
    """), {"period_id": period_id})).scalar_one())
    await direct_db.execute(text("""
        INSERT INTO payroll.payrolldraftlines
            (companyid, branchid, payrollperiodid, driverid, workdate, linetype, linescope,
             quantity, sourcetype, status, needsmanagerreview, addedbyuserid)
        VALUES (1, :branch_id, :period_id, :driver_id, '2098-01-01', 'HOURS', 'Daily',
                2.0000, 'Manual', 'Active', FALSE, 1)
    """), {"branch_id": branch_id, "period_id": period_id, "driver_id": driver_id})
    rate_type_id = int((await direct_db.execute(text("""
        SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = 'HOURLY'
    """))).scalar_one())
    rate_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.driverrates
            (companyid, branchid, driverid, ratetypeid, amount, effectivefrom, status, createdbyuserid)
        VALUES (1, :branch_id, :driver_id, :rate_type_id, 10.0000, '2098-01-01', 'Approved', 1)
        RETURNING driverrateid
    """), {
        "branch_id": branch_id, "driver_id": driver_id, "rate_type_id": rate_type_id,
    })).scalar_one())
    period = SimpleNamespace(
        payroll_period_id=period_id, branch_id=branch_id, period_code=f"P6A-{marker}",
        period_type="Week", start_date=date(2098, 1, 1), end_date=date(2098, 1, 7),
    )
    line = _CalculationPacketLine(
        source_type="DraftLine", source_id=f"p6a:{marker}", line_type="HOURS",
        line_scope="Daily", work_date=date(2098, 1, 1), driver_id=driver_id,
        quantity=Decimal("2"), resolved_rate_amount=Decimal("10"),
        calculated_amount=Decimal("20"), needs_manager_review=False, blocker_reason=None,
        pay_item_id=hours_pay_item_id, driver_rate_id=rate_id,
        source_evidence={"RateBehavior": "PerUnit"},
    )
    packet = _LiveCalculationPacket(
        payroll_period_id=period_id, company_id=1, branch_id=branch_id, status="Open",
        blockers=[], warnings=[], total_expected_pay=Decimal("20"),
        drivers=[_CalculationPacketDriverTotal(
            driver_id=driver_id, driver_code="P6A-FROZEN", driver_name="P6A Frozen Driver",
            daily_pay=Decimal("20"), status_pay=Decimal("0"), period_pay=Decimal("0"),
            minimum_adjustment=Decimal("0"), maximum_adjustment=Decimal("0"),
            bonus_total=Decimal("0"), expected_pay=Decimal("20"), needs_manager_review=False,
            blockers=[], lines=[line],
        )],
    )
    snapshot_id = int(await _capture_calculation_snapshot(
        period=period, company_id=1, user_id=1, packet=packet, db=direct_db, context="Submit",
    ))
    review_id = int((await direct_db.execute(text("""
        INSERT INTO review.managerreviewitems
            (companyid, branchid, requestedbyuserid, requesttype, entityschema, entityname,
             entityid, title, priority, status, payrollcalculationsnapshotid)
        VALUES (1, :branch_id, 1, 'PeriodApproval', 'payroll', 'PayrollPeriods', :period_id,
                'P6A packet', 'Normal', :review_status, :snapshot_id)
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
    return period_id, snapshot_id, rate_id, review_id


async def _scoped_permission_token(
    client: httpx.AsyncClient, admin_token: str, branch_id: int, permission_codes: list[str],
    *, return_user_id: bool = False,
) -> str | tuple[str, int]:
    marker = uuid4().hex
    role = await client.post(
        "/admin/company-roles", json={"role_name": f"P6A {marker}"}, headers=_auth(admin_token),
    )
    assert role.status_code == 201, role.text
    role_id = role.json()["company_role_id"]
    assigned = await client.put(
        f"/admin/company-roles/{role_id}/permissions",
        json={"permission_codes": permission_codes}, headers=_auth(admin_token),
    )
    assert assigned.status_code == 200, assigned.text
    username = f"p6a_{marker[:20]}"
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
    access_token = login.json()["access_token"]
    if return_user_id:
        return access_token, int(user.json()["user_id"])
    return access_token


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
async def test_overview_and_all_reports_use_final_lines_and_exact_snapshot(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    period_id, snapshot_id, rate_id, _ = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id,
    )
    # The existing finalized-rate guard rejects mutation; P6A also does not join this table.
    with pytest.raises(IntegrityError):
        await direct_db.execute(text("UPDATE payroll.driverrates SET amount = 99.0000 WHERE driverrateid = :id"), {"id": rate_id})
    overview = await session_client.get(f"/payroll/finalized/{period_id}/overview", headers=_auth(auth_token))
    assert overview.status_code == 200, overview.text
    overview_body = overview.json()
    assert Decimal(overview_body["financial_summary"]["total_pay"]) == Decimal("20")
    assert overview_body["snapshot_provenance"]["snapshot_id"] == snapshot_id
    assert overview_body["section_availability"]["financials"]["state"] == "AVAILABLE"
    assert overview_body["section_availability"]["rates_used"]["state"] == "AVAILABLE"
    assert "drivers" not in overview_body
    assert "columns" not in overview_body
    for view in ("drivers", "period-work", "period-pay", "mixed"):
        response = await session_client.get(
            f"/payroll/finalized/{period_id}/reports/{view}", headers=_auth(auth_token),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["metadata"]["authority_kind"] == "FINAL_LINES"
        assert body["metadata"]["snapshot_id"] == snapshot_id
        assert Decimal(body["pay_totals"]["total_pay"]) == Decimal("20")
        assert body["drivers"][0]["driver_name"] == "P6A Frozen Driver"
        assert body["metadata"]["report_evidence_available"] is True
        assert body["metadata"]["section_availability"]["report_evidence"]["state"] == "AVAILABLE"
        assert body["drivers"][0]["work"]["status_entries"] == []
        assert body["drivers"][0]["bonus_events"] == []
    archived = await session_client.patch(
        f"/payroll/periods/{period_id}/status", json={"status": "Archived"}, headers=_auth(auth_token),
    )
    assert archived.status_code == 200, archived.text
    archived_report = await session_client.get(
        f"/payroll/finalized/{period_id}/reports/mixed", headers=_auth(auth_token),
    )
    assert archived_report.status_code == 200, archived_report.text
    assert Decimal(archived_report.json()["pay_totals"]["total_pay"]) == Decimal("20")


@pytest.mark.asyncio
async def test_finalized_routes_enforce_ledger_view_and_lifecycle_scope(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    period_id, _, _, _ = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id,
    )
    ledger_token = await _scoped_permission_token(
        session_client, auth_token, paytest_branch_id, ["ledger.view"],
    )
    audit_token = await _scoped_permission_token(
        session_client, auth_token, paytest_branch_id, ["ledger.view", "ledger.audit.view"],
    )
    payroll_only_token = await _scoped_permission_token(
        session_client, auth_token, paytest_branch_id, ["payroll.view"],
    )
    audit_only_token = await _scoped_permission_token(
        session_client, auth_token, paytest_branch_id, ["ledger.audit.view"],
    )
    allowed = await session_client.get(
        f"/payroll/finalized/{period_id}/overview", headers=_auth(ledger_token),
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["section_availability"]["audit"] == {
        "state": "UNAVAILABLE", "reason_code": "AUDIT_PERMISSION_REQUIRED",
    }
    audit_allowed = await session_client.get(
        f"/payroll/finalized/{period_id}/overview", headers=_auth(audit_token),
    )
    assert audit_allowed.status_code == 200, audit_allowed.text
    assert audit_allowed.json()["section_availability"]["audit"] == {
        "state": "AVAILABLE", "reason_code": None,
    }
    for token in (payroll_only_token, audit_only_token):
        denied = await session_client.get(
            f"/payroll/finalized/{period_id}/overview", headers=_auth(token),
        )
        assert denied.status_code == 403
    non_finalized_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (1, :branch_id, 'Approved', :code, 'P6A non-finalized', 'Week', '2098-02-01', '2098-02-07')
        RETURNING payrollperiodid
    """), {"branch_id": paytest_branch_id, "code": f"P6A-APPROVED-{uuid4().hex}"})).scalar_one())
    unavailable = await session_client.get(
        f"/payroll/finalized/{non_finalized_id}/overview", headers=_auth(auth_token),
    )
    assert unavailable.status_code == 422


@pytest.mark.asyncio
async def test_finalized_routes_deny_driver_and_own_driver_data_roles_even_with_ledger_view(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    period_id, _, _, _ = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id,
    )
    for scope_type in ("SpecificBranch", "OwnDriverDataOnly"):
        token = await _ledger_token_with_driver_scope(
            session_client, auth_token, paytest_branch_id, scope_type,
        )
        response = await session_client.get(
            f"/payroll/finalized/{period_id}/overview", headers=_auth(token),
        )
        assert response.status_code == 403, response.text
        assert str(period_id) not in response.text


@pytest.mark.asyncio
async def test_finalized_routes_do_not_leak_foreign_branch_or_company_periods(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    hq_branch_id: int, direct_db, test_database_url: str,
):
    paytest_period_id, _, _, _ = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id,
    )
    hq_limited_token = await _scoped_permission_token(
        session_client, auth_token, hq_branch_id, ["ledger.view"],
    )
    foreign_branch = await session_client.get(
        f"/payroll/finalized/{paytest_period_id}/reports/drivers", headers=_auth(hq_limited_token),
    )
    assert foreign_branch.status_code == 403, foreign_branch.text
    assert str(paytest_period_id) not in foreign_branch.text

    marker = uuid4().hex[:12]
    company_id = int((await direct_db.execute(text("""
        INSERT INTO core.companies
            (companycode, companyname, legalname, status, issuspended, timezonename)
        VALUES (:code, :name, :name, 'Active', FALSE, 'UTC')
        RETURNING companyid
    """), {"code": f"P6A-{marker}", "name": f"P6A foreign {marker}"})).scalar_one())
    branch_id = int((await direct_db.execute(text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (:company_id, :code, :name, 'Active', TRUE)
        RETURNING branchid
    """), {
        "company_id": company_id, "code": f"P6A-{marker}", "name": f"P6A foreign {marker}",
    })).scalar_one())
    foreign_period_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (:company_id, :branch_id, 'Locked', :code, :name, 'Week', '2098-03-01', '2098-03-07')
        RETURNING payrollperiodid
    """), {
        "company_id": company_id, "branch_id": branch_id,
        "code": f"P6A-{marker}", "name": f"P6A foreign {marker}",
    })).scalar_one())
    await direct_db.commit()
    try:
        foreign_company = await session_client.get(
            f"/payroll/finalized/{foreign_period_id}/overview", headers=_auth(auth_token),
        )
        assert foreign_company.status_code == 404, foreign_company.text
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
async def test_finalized_reports_keep_period_pay_item_metadata_after_catalog_mutation(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    period_id, _, _, _ = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id,
    )
    snapshot = (await direct_db.execute(text("""
        SELECT payitemid, payitemcode, COALESCE(displaylabel, payitemname) AS label, unit
        FROM payroll.payrollperiodpayitems
        WHERE payrollperiodid = :period_id AND appearsinreports = TRUE
        ORDER BY sortorder, payitemcode, payitemid
        LIMIT 1
    """), {"period_id": period_id})).mappings().one()
    await direct_db.execute(text("""
        UPDATE payroll.payitems SET payitemname = 'Mutable P6A catalog name', status = 'Retired'
        WHERE payitemid = :pay_item_id
    """), {"pay_item_id": snapshot["payitemid"]})
    await direct_db.commit()
    try:
        response = await session_client.get(
            f"/payroll/finalized/{period_id}/reports/drivers", headers=_auth(auth_token),
        )
        assert response.status_code == 200, response.text
        column = next(
            column for column in response.json()["columns"] if column["pay_item_id"] == snapshot["payitemid"]
        )
        assert column["label"] == snapshot["label"]
        assert column["code"] == snapshot["payitemcode"]
        assert column["unit"] == snapshot["unit"]
    finally:
        await direct_db.execute(text("""
            UPDATE payroll.payitems AS pi
            SET payitemname = p.payitemname, status = p.payitemstatusatsnapshot
            FROM payroll.payrollperiodpayitems AS p
            WHERE pi.payitemid = p.payitemid
              AND p.payrollperiodid = :period_id
              AND p.payitemid = :pay_item_id
        """), {"period_id": period_id, "pay_item_id": snapshot["payitemid"]})
        await direct_db.commit()


@pytest.mark.asyncio
async def test_finalized_routes_use_the_exact_resubmitted_snapshot_revision(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db, test_database_url: str,
):
    period_id, first_snapshot_id, _, review_id = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id, finalize=False,
    )
    returned = await session_client.post(
        f"/review/items/{review_id}/decide",
        json={"decision": "EditRequested", "decision_reason": "Correct source"},
        headers=_auth(auth_token),
    )
    assert returned.status_code == 200, returned.text
    lines = await session_client.get(f"/payroll/periods/{period_id}/lines", headers=_auth(auth_token))
    assert lines.status_code == 200, lines.text
    corrected = await session_client.patch(
        f"/payroll/periods/{period_id}/lines/{lines.json()[0]['draft_line_id']}",
        json={"quantity": "3.0000"}, headers=_auth(auth_token),
    )
    assert corrected.status_code == 200, corrected.text
    resubmitted = await session_client.post(
        f"/payroll/periods/{period_id}/resubmissions", headers=_auth(auth_token),
    )
    assert resubmitted.status_code == 200, resubmitted.text
    second_review = (await direct_db.execute(text("""
        SELECT reviewitemid, payrollcalculationsnapshotid
        FROM review.managerreviewitems
        WHERE entityschema = 'payroll' AND entityname = 'PayrollPeriods'
          AND entityid = :period_id AND status = 'Pending'
        ORDER BY reviewitemid DESC
        LIMIT 1
    """), {"period_id": str(period_id)})).mappings().one()
    second_snapshot_id = int(second_review["payrollcalculationsnapshotid"])
    assert second_snapshot_id != first_snapshot_id
    approved = await session_client.post(
        f"/review/items/{second_review['reviewitemid']}/decide",
        json={"decision": "Approved", "decision_reason": "Approved corrected source"},
        headers=_auth(auth_token),
    )
    assert approved.status_code == 200, approved.text
    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.begin() as finalize_db:
            result = await finalize_period(period_id, 1, 1, finalize_db)
            assert result.status == "Locked"
    finally:
        await engine.dispose()
    overview = await session_client.get(
        f"/payroll/finalized/{period_id}/overview", headers=_auth(auth_token),
    )
    assert overview.status_code == 200, overview.text
    provenance = overview.json()["snapshot_provenance"]
    assert provenance["snapshot_id"] == second_snapshot_id
    assert provenance["revision_number"] == 2
    second_snapshot_hash = (await direct_db.execute(text("""
        SELECT snapshothash FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid = :snapshot_id
    """), {"snapshot_id": second_snapshot_id})).scalar_one()
    assert provenance["snapshot_hash"] == second_snapshot_hash
    report = await session_client.get(
        f"/payroll/finalized/{period_id}/reports/period-pay", headers=_auth(auth_token),
    )
    assert report.status_code == 200, report.text
    assert report.json()["metadata"]["snapshot_id"] == second_snapshot_id
    final_total = (await direct_db.execute(text("""
        SELECT COALESCE(SUM(finalamount), 0)
        FROM payroll.payrollfinallines
        WHERE payrollperiodid = :period_id
    """), {"period_id": period_id})).scalar_one()
    assert Decimal(report.json()["pay_totals"]["total_pay"]) == Decimal(final_total)
    assert Decimal(final_total) != Decimal("20")


@pytest.mark.asyncio
async def test_legacy_final_lines_keep_money_but_report_evidence_is_unavailable(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    paytest_driver_id: int, direct_db,
):
    marker = uuid4().hex
    period_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (1, :branch_id, 'Locked', :code, 'P6A legacy', 'Week', '2077-01-01', '2077-01-07')
        RETURNING payrollperiodid
    """), {"branch_id": paytest_branch_id, "code": f"P6A-LEGACY-{marker}"})).scalar_one())
    # This period deliberately has no originating calculation-snapshot
    # provenance (pre-CP-4D FinalLines). It still gets a current-architecture
    # Daily Pay Item layout, distinct from snapshot provenance, so its
    # FinalLine can carry a valid PayItemID.
    await _create_period_pay_item_rows(
        period_id, 1, paytest_branch_id, date(2077, 1, 1), direct_db,
    )
    hours_pay_item_id = int((await direct_db.execute(text("""
        SELECT payitemid FROM payroll.payrollperiodpayitems
        WHERE payrollperiodid = :period_id AND payitemcode = 'HOURS'
    """), {"period_id": period_id})).scalar_one())
    await direct_db.execute(text("SELECT set_config('app.allow_payroll_final_line_insert', 'true', false)"))
    await direct_db.execute(text("""
        INSERT INTO payroll.payrollfinallines
            (companyid, branchid, payrollperiodid, driverid, workdate, linetype, linescope,
             payitemid, quantity, finalamount, sourcetype, approvedbyuserid, approvedatutc, lockedatutc)
        VALUES (1, :branch_id, :period_id, :driver_id, '2077-01-01', 'HOURS', 'Daily',
                :pay_item_id, 1, 12.0000, 'DraftLine', 1, NOW(), NOW())
    """), {"branch_id": paytest_branch_id, "period_id": period_id, "driver_id": paytest_driver_id,
           "pay_item_id": hours_pay_item_id})
    overview = await session_client.get(f"/payroll/finalized/{period_id}/overview", headers=_auth(auth_token))
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert Decimal(body["financial_summary"]["total_pay"]) == Decimal("12")
    assert body["section_availability"]["financials"]["state"] == "AVAILABLE"
    assert body["section_availability"]["snapshot_provenance"]["state"] == "UNAVAILABLE"
    report = await session_client.get(
        f"/payroll/finalized/{period_id}/reports/period-pay", headers=_auth(auth_token),
    )
    assert report.status_code == 200, report.text
    assert report.json()["metadata"]["report_evidence_available"] is False
    assert report.json()["metadata"]["section_availability"]["report_evidence"]["state"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_finalized_period_discovery_returns_only_minimal_locked_archived_items(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
    test_database_url: str,
):
    period_id, _, _, _ = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id,
    )
    await direct_db.execute(text("""
        UPDATE payroll.payrollperiods
        SET status = 'Archived'
        WHERE payrollperiodid = :period_id
    """), {"period_id": period_id})
    non_finalized_ids = []
    for status, start_date in (("Draft", date(2098, 4, 1)), ("Cancelled", date(2098, 5, 1))):
        non_finalized_ids.append(int((await direct_db.execute(text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype,
                 startdate, enddate)
                VALUES (1, :branch_id, :status, :code, :name, 'Week', :start_date,
                        :end_date)
            RETURNING payrollperiodid
        """), {
            "branch_id": paytest_branch_id,
            "status": status,
                "code": f"P6A-DISCOVERY-{uuid4().hex}",
                "name": f"P6A discovery {status}",
                "start_date": start_date,
                "end_date": start_date.replace(day=start_date.day + 6),
        })).scalar_one()))
    await direct_db.commit()

    response = await session_client.get(
        "/payroll/finalized", headers=_auth(auth_token),
    )
    assert response.status_code == 200, response.text
    items = response.json()
    target = next(item for item in items if item["period_id"] == period_id)
    assert target["period_status"] == "Archived"
    assert {item["period_status"] for item in items} <= {"Locked", "Archived"}
    assert not any(item["period_id"] in non_finalized_ids for item in items)
    assert set(target) == {
        "period_id", "period_code", "period_name", "period_status", "period_type",
        "branch_id", "branch_name", "start_date", "end_date", "pay_date",
        "finalized_at_utc",
    }

    locked_only = await session_client.get(
        "/payroll/finalized?status=Locked", headers=_auth(auth_token),
    )
    assert locked_only.status_code == 200, locked_only.text
    assert all(item["period_status"] == "Locked" for item in locked_only.json())
    archived_only = await session_client.get(
        "/payroll/finalized?status=Archived", headers=_auth(auth_token),
    )
    assert archived_only.status_code == 200, archived_only.text
    assert any(item["period_id"] == period_id for item in archived_only.json())
    invalid_status = await session_client.get(
        "/payroll/finalized?status=Open", headers=_auth(auth_token),
    )
    assert invalid_status.status_code == 422, invalid_status.text


@pytest.mark.asyncio
async def test_finalized_period_discovery_requires_ledger_scope_and_denies_driver_oda(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    hq_branch_id: int,
    direct_db,
    test_database_url: str,
):
    period_id, _, _, _ = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id,
    )
    ledger_token = await _scoped_permission_token(
        session_client, auth_token, paytest_branch_id, ["ledger.view"],
    )
    ledger_response = await session_client.get("/payroll/finalized", headers=_auth(ledger_token))
    assert ledger_response.status_code == 200, ledger_response.text
    assert any(item["period_id"] == period_id for item in ledger_response.json())
    permission_tokens = [
        await _scoped_permission_token(session_client, auth_token, paytest_branch_id, ["payroll.view"]),
        await _scoped_permission_token(session_client, auth_token, paytest_branch_id, ["ledger.audit.view"]),
        await _scoped_permission_token(session_client, auth_token, paytest_branch_id, []),
    ]
    for token in permission_tokens:
        response = await session_client.get("/payroll/finalized", headers=_auth(token))
        assert response.status_code == 403, response.text

    for scope_type in ("SpecificBranch", "OwnDriverDataOnly"):
        token = await _ledger_token_with_driver_scope(
            session_client, auth_token, paytest_branch_id, scope_type,
        )
        response = await session_client.get("/payroll/finalized", headers=_auth(token))
        assert response.status_code == 403, response.text
        assert str(period_id) not in response.text

    hq_token = await _scoped_permission_token(
        session_client, auth_token, hq_branch_id, ["ledger.view"],
    )
    inaccessible = await session_client.get(
        f"/payroll/finalized?branch_id={paytest_branch_id}", headers=_auth(hq_token),
    )
    assert inaccessible.status_code == 403, inaccessible.text
    scoped_empty = await session_client.get("/payroll/finalized", headers=_auth(hq_token))
    assert scoped_empty.status_code == 200, scoped_empty.text
    assert all(item["branch_id"] == hq_branch_id for item in scoped_empty.json())
    assert all(item["period_id"] != period_id for item in scoped_empty.json())


@pytest.mark.asyncio
async def test_finalized_period_discovery_does_not_leak_foreign_company_periods(
    session_client: httpx.AsyncClient,
    auth_token: str,
    direct_db,
):
    marker = uuid4().hex[:12]
    company_id = int((await direct_db.execute(text("""
        INSERT INTO core.companies
            (companycode, companyname, legalname, status, issuspended, timezonename)
        VALUES (:code, :name, :name, 'Active', FALSE, 'UTC')
        RETURNING companyid
    """), {"code": f"P6A-F-{marker}", "name": f"P6A foreign {marker}"})).scalar_one())
    branch_id = int((await direct_db.execute(text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (:company_id, :code, :name, 'Active', TRUE)
        RETURNING branchid
    """), {
        "company_id": company_id,
        "code": f"P6A-F-{marker}",
        "name": f"P6A foreign {marker}",
    })).scalar_one())
    period_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (:company_id, :branch_id, 'Locked', :code, :name, 'Week', '2098-06-01', '2098-06-07')
        RETURNING payrollperiodid
    """), {
        "company_id": company_id,
        "branch_id": branch_id,
        "code": f"P6A-F-{marker}",
        "name": f"P6A foreign {marker}",
    })).scalar_one())
    await direct_db.commit()
    try:
        response = await session_client.get("/payroll/finalized", headers=_auth(auth_token))
        assert response.status_code == 200, response.text
        assert all(item["period_id"] != period_id for item in response.json())
    finally:
        await direct_db.execute(text(
            "DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :period_id"
        ), {"period_id": period_id})
        await direct_db.execute(text("DELETE FROM core.branches WHERE branchid = :branch_id"), {
            "branch_id": branch_id,
        })
        await direct_db.execute(text("DELETE FROM core.companies WHERE companyid = :company_id"), {
            "company_id": company_id,
        })
        await direct_db.commit()


@pytest.mark.asyncio
async def test_finalized_and_operational_discovery_permissions_remain_separate(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
    test_database_url: str,
):
    period_id, _, _, _ = await _seed_finalized_period(
        direct_db, test_database_url, paytest_branch_id,
    )
    ledger_only = await _scoped_permission_token(
        session_client, auth_token, paytest_branch_id, ["ledger.view"],
    )
    finalized_list = await session_client.get(
        "/payroll/finalized", headers=_auth(ledger_only),
    )
    assert finalized_list.status_code == 200, finalized_list.text
    assert any(item["period_id"] == period_id for item in finalized_list.json())
    overview = await session_client.get(
        f"/payroll/finalized/{period_id}/overview", headers=_auth(ledger_only),
    )
    assert overview.status_code == 200, overview.text
    report = await session_client.get(
        f"/payroll/finalized/{period_id}/reports/drivers", headers=_auth(ledger_only),
    )
    assert report.status_code == 200, report.text

    both = await _scoped_permission_token(
        session_client, auth_token, paytest_branch_id, ["ledger.view", "payroll.view"],
    )
    both_list = await session_client.get("/payroll/finalized", headers=_auth(both))
    assert both_list.status_code == 200, both_list.text
    assert any(item["period_id"] == period_id for item in both_list.json())

    payroll_only = await _scoped_permission_token(
        session_client, auth_token, paytest_branch_id, ["payroll.view"],
    )
    denied = await session_client.get("/payroll/finalized", headers=_auth(payroll_only))
    assert denied.status_code == 403, denied.text
    operational = await session_client.get(
        f"/payroll/periods?status=Locked&branch_id={paytest_branch_id}",
        headers=_auth(payroll_only),
    )
    assert operational.status_code == 200, operational.text
    assert any(item["payroll_period_id"] == period_id for item in operational.json())
    final_lines = await session_client.get(
        f"/payroll/periods/{period_id}/final-lines", headers=_auth(payroll_only),
    )
    assert final_lines.status_code == 200, final_lines.text
    assert final_lines.json()
