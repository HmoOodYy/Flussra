"""Focused CP-5C report read-model and public-route authority contracts."""
import itertools
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.payroll import report_read_model
from app.payroll.reporting import ReportAuthorityKind
from app.payroll.service import (
    _CalculationPacketDriverTotal,
    _CalculationPacketLine,
    _capture_calculation_snapshot,
    _create_period_pay_item_rows,
    _LiveCalculationPacket,
    finalize_period,
)

_SECURITY_COUNTER = itertools.count(1)
_REPORT_PATHS = ("drivers", "period-work", "period-pay", "mixed")


@pytest_asyncio.fixture(scope="session")
async def paytest_branch_id(session_db_conn) -> int:
    """Keep CP5C period/report fixtures isolated from shared workflow slots."""
    row = (await session_db_conn.execute(text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (1, :code, 'CP5C isolated', 'Active', FALSE)
        RETURNING branchid
    """), {"code": f"CP5C_{uuid4().hex}"})).scalar_one()
    return int(row)


@pytest_asyncio.fixture(scope="session")
async def paytest_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> int:
    """Create the CP5C driver on its isolated branch."""
    response = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": paytest_branch_id,
            "full_name": "CP5C Isolated Driver",
            "driver_code": f"CP5C-D-{uuid4().hex[:10]}",
        },
        headers=_auth(auth_token),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["driver_id"])


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_cp5c_workflow_slots(direct_db):
    """Keep this route suite from reserving a workflow slot for later modules."""
    yield
    await direct_db.execute(text("""
        UPDATE payroll.payrollperiods
        SET status = 'Cancelled', currentreturnreviewitemid = NULL
        WHERE companyid = 1 AND periodcode LIKE 'CP5C-%'
          AND status IN ('Draft', 'Open', 'InReview', 'Returned', 'Approved')
    """))
    await direct_db.commit()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _insert_http_period(
    direct_db, branch_id: int, status: str, *, start_date: date = date(2099, 1, 1),
    end_date: date = date(2099, 1, 7),
) -> int:
    marker = uuid4().hex
    # This focused file commits source setup so the ASGI application can read
    # it through its own connection. Retire only previous rows owned by this
    # file before claiming a Draft workflow slot again.
    await direct_db.execute(text("""
        UPDATE payroll.payrollperiods
        SET status = 'Cancelled', currentreturnreviewitemid = NULL
        WHERE companyid = 1 AND branchid = :branch_id
          AND periodcode LIKE 'CP5C-%'
          AND status IN ('Draft', 'Open', 'InReview', 'Returned', 'Approved')
    """), {"branch_id": branch_id})
    return_review_id = None
    if status == "Returned":
        return_review_id = (await direct_db.execute(text("""
            INSERT INTO review.managerreviewitems
                (companyid, branchid, requestedbyuserid, requesttype, entityschema, entityname,
                 entityid, title, priority, status, finaldecisionbyuserid, finaldecisionatutc)
            VALUES (1, :branch_id, 1, 'PeriodApproval', 'payroll', 'PayrollPeriods', '0',
                    :title, 'Normal', 'Rejected', 1, NOW())
            RETURNING reviewitemid
        """), {"branch_id": branch_id, "title": f"CP5C return {marker}"})).scalar_one()
    row = (await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate,
             currentreturnreviewitemid)
        VALUES (1, :branch_id, :status, :code, :name, 'Week', :start_date, :end_date,
                :return_review_id)
        RETURNING payrollperiodid
    """), {
        "branch_id": branch_id, "status": status, "code": f"CP5C-{marker}",
        "name": f"CP5C HTTP {marker}", "start_date": start_date,
        "end_date": end_date, "return_review_id": return_review_id,
    })).scalar_one()
    if return_review_id is not None:
        await direct_db.execute(text("""
            UPDATE review.managerreviewitems SET entityid = :period_id
            WHERE reviewitemid = :review_id
        """), {"period_id": str(row), "review_id": return_review_id})
    # Current-architecture periods always carry a frozen Pay Item layout
    # snapshot (CP-2C); give every HTTP-shaped test period one too, so Daily
    # financial lines resolve to a PayItemID that belongs to the period's
    # reportable Daily layout.
    await _create_period_pay_item_rows(
        period_id=int(row), company_id=1, branch_id=branch_id, start_date=start_date, db=direct_db,
    )
    await direct_db.commit()
    return int(row)


async def _insert_http_work(
    direct_db, period_id: int, branch_id: int, driver_id: int, line_type: str = "DailyNote",
) -> None:
    await direct_db.execute(text("""
        INSERT INTO payroll.payrolldraftlines
            (companyid, branchid, payrollperiodid, driverid, workdate, linetype, linescope,
             quantity, sourcetype, status, needsmanagerreview, addedbyuserid)
        VALUES (1, :branch_id, :period_id, :driver_id, '2099-01-01', :line_type, 'Daily',
                1, 'Manual', 'Active', FALSE, 1)
    """), {"branch_id": branch_id, "period_id": period_id, "driver_id": driver_id, "line_type": line_type})
    await direct_db.commit()


async def _report(client: httpx.AsyncClient, token: str, period_id: int, name: str) -> httpx.Response:
    return await client.get(f"/payroll/periods/{period_id}/reports/{name}", headers=_auth(token))


async def _seed_submitted_snapshot(
    direct_db, branch_id: int, driver_id: int, *, include_evidence: bool = True,
    include_multiple_statuses: bool = False,
    system_adjustments: tuple[tuple[str, Decimal], ...] = (),
    daily_pay_item_id: int | None = None,
) -> tuple[int, int, int, int | None, int | None]:
    """Capture a real CP-4D snapshot/evidence packet for route-level reads."""
    period_id = await _insert_http_period(direct_db, branch_id, "Open")
    # A real Daily Pay Item code (not a pseudo line type), so the raw
    # DraftLine row remains valid once the period returns to LIVE authority
    # (Returned) and PayItemID must resolve to the frozen Daily layout.
    await _insert_http_work(direct_db, period_id, branch_id, driver_id, line_type="HOURS")
    hours_pay_item_id = int((await direct_db.execute(text("""
        SELECT payitemid FROM payroll.payrollperiodpayitems
        WHERE payrollperiodid = :period_id AND payitemcode = 'HOURS'
    """), {"period_id": period_id})).scalar_one())
    if daily_pay_item_id is None:
        daily_pay_item_id = hours_pay_item_id
    marker = uuid4().hex[:12]
    status_key_id = None
    bonus_id = None
    if include_evidence:
        status_key_id = (await direct_db.execute(text("""
        INSERT INTO payroll.payrollstatuskeys
            (companyid, branchid, statuscode, normalizedstatuscode, keyname,
             hoursvalue, isoffreason, isactive, displayorder)
        VALUES (1, :branch_id, :code, :normalized_code, :label, 0, TRUE, TRUE, 999)
        RETURNING statuskeyid
    """), {"branch_id": branch_id, "code": f"CP5C_{marker}",
              "normalized_code": f"CP5C_{marker}".upper(), "label": f"Frozen {marker}"})).scalar_one()
        await direct_db.execute(text("""
            INSERT INTO payroll.payrollperioddriverdayentrystate
            (companyid, branchid, payrollperiodid, workdate, driverid, statuskeyid, createdbyuserid)
        VALUES (1, :branch_id, :period_id, '2099-01-02', :driver_id, :status_key_id, 1)
        """), {"branch_id": branch_id, "period_id": period_id, "driver_id": driver_id, "status_key_id": status_key_id})
        if include_multiple_statuses:
            second_status_key_id = (await direct_db.execute(text("""
                INSERT INTO payroll.payrollstatuskeys
                    (companyid, branchid, statuscode, normalizedstatuscode, keyname,
                     hoursvalue, isoffreason, isactive, displayorder)
                VALUES (1, :branch_id, :code, :code, 'Frozen non-off status', 0, FALSE, TRUE, 1000)
                RETURNING statuskeyid
            """), {"branch_id": branch_id, "code": f"CP5C_NONOFF_{marker}".upper()})).scalar_one()
            await direct_db.execute(text("""
                INSERT INTO payroll.payrollperioddriverdayentrystate
                    (companyid, branchid, payrollperiodid, workdate, driverid, statuskeyid, createdbyuserid)
                VALUES (1, :branch_id, :period_id, '2099-01-03', :driver_id, :status_key_id, 1)
            """), {"branch_id": branch_id, "period_id": period_id,
                   "driver_id": driver_id, "status_key_id": second_status_key_id})
        bonus_id = (await direct_db.execute(text("""
        INSERT INTO payroll.payrollbonusevents
            (companyid, branchid, payrollperiodid, driverid, amount, reason, notes, status,
             createdbyuserid, createdatutc, datarevision)
        VALUES (1, :branch_id, :period_id, :driver_id, 4.0000, 'CP5C', 'Frozen bonus',
                'Active', 1, NOW(), 1)
        RETURNING payrollbonuseventid
        """), {"branch_id": branch_id, "period_id": period_id, "driver_id": driver_id})).scalar_one()
    period = SimpleNamespace(
        payroll_period_id=period_id, branch_id=branch_id, period_code=f"CP5C-{marker}",
        period_type="Week", start_date=date(2099, 1, 1), end_date=date(2099, 1, 7),
    )
    daily = _CalculationPacketLine(
        source_type="DraftLine", source_id=f"CP5C:{marker}", line_type="DailyNote", line_scope="Daily",
        work_date=date(2099, 1, 1), driver_id=driver_id, quantity=Decimal("1"),
        resolved_rate_amount=None, calculated_amount=Decimal("16"), needs_manager_review=False,
        blocker_reason=None, pay_item_id=daily_pay_item_id, source_evidence={"CP5C": marker},
    )
    lines = [daily]
    bonus_total = Decimal("0")
    if bonus_id is not None:
        lines.append(_CalculationPacketLine(
            source_type="BonusEvent", source_id=str(bonus_id), line_type="BONUS", line_scope="Period",
            work_date=None, driver_id=driver_id, quantity=None, resolved_rate_amount=None,
            calculated_amount=Decimal("4"), needs_manager_review=False, blocker_reason=None,
            bonus_event_id=int(bonus_id), source_evidence={"PayrollBonusEventID": int(bonus_id)},
        ))
        bonus_total = Decimal("4")
    minimum_adjustment = Decimal("0")
    maximum_adjustment = Decimal("0")
    for line_type, amount in system_adjustments:
        lines.append(_CalculationPacketLine(
            source_type="System", source_id=f"{line_type}:{marker}", line_type=line_type,
            line_scope="Period", work_date=None, driver_id=driver_id, quantity=Decimal("1"),
            resolved_rate_amount=None, calculated_amount=amount, needs_manager_review=False,
            blocker_reason=None, source_evidence={"CP5C": marker},
        ))
        if line_type == "SYS_MIN_TOPUP":
            minimum_adjustment += amount
        elif line_type == "SYS_MAX_CAP":
            maximum_adjustment += amount
    packet = _LiveCalculationPacket(
        payroll_period_id=period_id, company_id=1, branch_id=branch_id, status="Open", blockers=[], warnings=[],
        drivers=[_CalculationPacketDriverTotal(
            driver_id=driver_id, driver_code="CP5C", driver_name="CP5C Frozen Driver",
            daily_pay=Decimal("16"), status_pay=Decimal("0"), period_pay=Decimal("0"),
            minimum_adjustment=minimum_adjustment, maximum_adjustment=maximum_adjustment, bonus_total=bonus_total,
            expected_pay=Decimal("16") + bonus_total + minimum_adjustment + maximum_adjustment,
            needs_manager_review=False, blockers=[], lines=lines,
        )], total_expected_pay=Decimal("16") + bonus_total + minimum_adjustment + maximum_adjustment,
    )
    snapshot_id = await _capture_calculation_snapshot(
        period=period, company_id=1, user_id=1, packet=packet, db=direct_db, context="Submit",
    )
    review_id = (await direct_db.execute(text("""
        INSERT INTO review.managerreviewitems
            (companyid, branchid, requestedbyuserid, requesttype, entityschema, entityname, entityid,
             title, priority, status, payrollcalculationsnapshotid)
        VALUES (1, :branch_id, 1, 'PeriodApproval', 'payroll', 'PayrollPeriods', :period_id,
                :title, 'Normal', 'Pending', :snapshot_id)
        RETURNING reviewitemid
    """), {"branch_id": branch_id, "period_id": str(period_id), "title": f"CP5C {marker}",
              "snapshot_id": snapshot_id})).scalar_one()
    await direct_db.execute(text("UPDATE payroll.payrollperiods SET status = 'InReview' WHERE payrollperiodid = :period_id"), {"period_id": period_id})
    await direct_db.commit()
    return (
        period_id,
        int(snapshot_id),
        int(review_id),
        None if status_key_id is None else int(status_key_id),
        None if bonus_id is None else int(bonus_id),
    )


async def _seed_legacy_snapshot(direct_db, branch_id: int, driver_id: int) -> tuple[int, int]:
    """Create a pre-evidence immutable packet with live evidence deliberately present."""
    period_id = await _insert_http_period(direct_db, branch_id, "Open")
    await _insert_http_work(direct_db, period_id, branch_id, driver_id)
    hours_pay_item_id = int((await direct_db.execute(text("""
        SELECT payitemid FROM payroll.payrollperiodpayitems
        WHERE payrollperiodid = :period_id AND payitemcode = 'HOURS'
    """), {"period_id": period_id})).scalar_one())
    marker = uuid4().hex[:12]
    await direct_db.execute(text("""
        INSERT INTO payroll.payrollstatuskeys
            (companyid, branchid, statuscode, normalizedstatuscode, keyname,
             hoursvalue, isoffreason, isactive, displayorder)
        VALUES (1, :branch_id, :code, :code, 'Mutable legacy status', 0, TRUE, TRUE, 999)
    """), {"branch_id": branch_id, "code": f"LEGACY_{marker}".upper()})
    await direct_db.execute(text("""
        INSERT INTO payroll.payrollbonusevents
            (companyid, branchid, payrollperiodid, driverid, amount, reason, notes, status,
             createdbyuserid, createdatutc, datarevision)
        VALUES (1, :branch_id, :period_id, :driver_id, 99.0000, 'Mutable legacy bonus',
                'must not leak', 'Active', 1, NOW(), 1)
    """), {"branch_id": branch_id, "period_id": period_id, "driver_id": driver_id})
    snapshot_id = (await direct_db.execute(text("""
        INSERT INTO payroll.payrollcalculationsnapshots
            (companyid, branchid, payrollperiodid, revisionnumber, calculationversion,
             sourceconfighash, snapshothash, createdbyuserid, totalexpectedpay)
        VALUES (1, :branch_id, :period_id, 1, 'legacy-cp5c', :source_hash, :snapshot_hash, 1, 16.0000)
        RETURNING payrollcalculationsnapshotid
    """), {"branch_id": branch_id, "period_id": period_id,
           "source_hash": "0" * 64, "snapshot_hash": "1" * 64})).scalar_one()
    total_id = (await direct_db.execute(text("""
        INSERT INTO payroll.payrollcalculationdrivertotals
            (payrollcalculationsnapshotid, companyid, branchid, driverid, drivercodesnapshot,
             drivernamesnapshot, dailypay, statuspay, periodpay, minimumadjustment,
             maximumadjustment, bonustotal, expectedpay)
        VALUES (:snapshot_id, 1, :branch_id, :driver_id, 'LEGACY', 'Legacy Driver',
                16.0000, 0, 0, 0, 0, 0, 16.0000)
        RETURNING payrollcalculationdrivertotalid
    """), {"snapshot_id": snapshot_id, "branch_id": branch_id, "driver_id": driver_id})).scalar_one()
    await direct_db.execute(text("""
        INSERT INTO payroll.payrollcalculationsnapshotlines
            (payrollcalculationdrivertotalid, sourcetype, sourceid, linetype, linescope,
             workdate, payitemid, quantity, calculatedamount, sourceevidencejsonb)
        VALUES (:total_id, 'DraftLine', 'legacy-cp5c', 'DailyNote', 'Daily', '2099-01-01',
                :pay_item_id, 1.0000, 16.0000, '{}'::jsonb)
    """), {"total_id": total_id, "pay_item_id": hours_pay_item_id})
    await direct_db.execute(text("""
        INSERT INTO review.managerreviewitems
            (companyid, branchid, requestedbyuserid, requesttype, entityschema, entityname,
             entityid, title, priority, status, payrollcalculationsnapshotid)
        VALUES (1, :branch_id, 1, 'PeriodApproval', 'payroll', 'PayrollPeriods', :period_id,
                'Legacy CP5C packet', 'Normal', 'Pending', :snapshot_id)
    """), {"branch_id": branch_id, "period_id": str(period_id), "snapshot_id": snapshot_id})
    await direct_db.execute(text("UPDATE payroll.payrollperiods SET status = 'InReview' WHERE payrollperiodid = :period_id"), {"period_id": period_id})
    await direct_db.commit()
    return period_id, int(snapshot_id)


async def _create_user(client: httpx.AsyncClient, admin_token: str, username: str) -> dict:
    response = await client.post("/admin/users", json={
        "username": username, "display_name": username, "password": "TestPass1234!",
        "is_active": True, "can_login": True, "must_change_password": False,
    }, headers=_auth(admin_token))
    assert response.status_code == 201, response.text
    return response.json()


async def _login(client: httpx.AsyncClient, username: str) -> str:
    response = await client.post("/auth/login", json={
        "username": username, "password": "TestPass1234!", "company_code": "DEMO",
    })
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


async def _reports_role_token(
    client: httpx.AsyncClient, admin_token: str, branch_id: int, *, include_reports_view: bool,
) -> str:
    suffix = next(_SECURITY_COUNTER)
    role = await client.post("/admin/company-roles", json={"role_name": f"CP5C reports {suffix}"}, headers=_auth(admin_token))
    assert role.status_code == 201, role.text
    role_id = role.json()["company_role_id"]
    permissions = ["reports.view"] if include_reports_view else []
    assigned = await client.put(
        f"/admin/company-roles/{role_id}/permissions", json={"permission_codes": permissions}, headers=_auth(admin_token),
    )
    assert assigned.status_code == 200, assigned.text
    username = f"cp5c_reports_{suffix:04d}"
    user = await _create_user(client, admin_token, username)
    assignment = await client.post(
        f"/admin/users/{user['user_id']}/company-role-assignments",
        json={"company_role_id": role_id, "scope_type": "SpecificBranch", "branch_id": branch_id}, headers=_auth(admin_token),
    )
    assert assignment.status_code == 201, assignment.text
    return await _login(client, username)


async def _driver_role_token(
    client: httpx.AsyncClient, admin_token: str, branch_id: int, scope: str,
) -> str:
    roles = await client.get("/admin/company-roles", headers=_auth(admin_token))
    assert roles.status_code == 200, roles.text
    driver_role_id = next(role["company_role_id"] for role in roles.json() if role["role_code"] == "DRIVER")
    suffix = next(_SECURITY_COUNTER)
    username = f"cp5c_driver_{suffix:04d}"
    user = await _create_user(client, admin_token, username)
    assignment = await client.post(
        f"/admin/users/{user['user_id']}/company-role-assignments",
        json={"company_role_id": driver_role_id, "scope_type": scope, "branch_id": branch_id}, headers=_auth(admin_token),
    )
    assert assignment.status_code == 201, assignment.text
    return await _login(client, username)


def _period(status: str) -> dict:
    return {
        "payrollperiodid": 9,
        "companyid": 1,
        "branchid": 2,
        "status": status,
        "periodcode": "CP5C-9",
        "periodname": "CP5C period",
        "periodtype": "Week",
        "startdate": None,
        "enddate": None,
        "branchname": "HQ",
    }


async def _columns(*_args):
    return [{"pay_item_id": 1, "code": "HOURS", "label": "Hours", "category": "Work",
             "data_type": "Decimal", "unit": "Hours", "scope": "Daily", "sort_order": 1}]


async def _operational(*_args):
    return ({7: {"driver_id": 7, "driver_code": "D7", "driver_name": "Driver Seven"}}, [
        {"driver_id": 7, "work_date": None, "line_type": "HOURS", "pay_item_id": 1,
         "quantity": Decimal("2"), "line_scope": "Daily"},
    ])


@pytest.mark.asyncio
async def test_prepared_reports_are_operational_only_without_live_financial_builder(monkeypatch):
    authority = SimpleNamespace(
        authority_kind=ReportAuthorityKind.SOURCE_ONLY,
        snapshot_id=None, snapshot_hash=None, revision_number=None,
    )

    async def financial(*_args):
        return [], {}, [], [], False, None, None

    monkeypatch.setattr(report_read_model, "_period_context", lambda **_: _async(_period("Draft")))
    monkeypatch.setattr(report_read_model, "resolve_report_financial_authority", lambda **_: _async(authority))
    monkeypatch.setattr(report_read_model, "_columns", _columns)
    monkeypatch.setattr(report_read_model, "_pay_item_columns", _columns)
    monkeypatch.setattr(report_read_model, "_operational_rows", _operational)
    monkeypatch.setattr(report_read_model, "_financial_packet", financial)

    drivers = await report_read_model.build_report(
        report_type="drivers", period_id=9, company_id=1, user_id=1, db=None,
    )
    assert drivers["metadata"]["financials_available"] is False
    assert drivers["drivers"][0]["pay"] is None
    assert drivers["work_totals"] == {"HOURS": Decimal("2")}
    assert drivers["pay_totals"] is None
    with pytest.raises(HTTPException, match="REPORT_FINANCIALS_UNAVAILABLE"):
        await report_read_model.build_report(
            report_type="period-pay", period_id=9, company_id=1, user_id=1, db=None,
        )


@pytest.mark.asyncio
async def test_frozen_report_uses_the_rp1_selected_snapshot_and_immutable_evidence(monkeypatch):
    authority = SimpleNamespace(
        authority_kind=ReportAuthorityKind.APPROVED_SNAPSHOT,
        snapshot_id=88, snapshot_hash="a" * 64, revision_number=3,
    )
    totals = {7: {"daily_pay": Decimal("16"), "status_pay": Decimal("0"),
                  "period_pay": Decimal("0"), "minimum_adjustment": Decimal("0"),
                  "maximum_adjustment": Decimal("0"), "bonus_total": Decimal("4"),
                  "total_pay": Decimal("20"), "driver_code": "D7", "driver_name": "Frozen"}}

    async def financial(*_args):
        return ([{"driver_id": 7, "source_type": "DraftLine", "line_type": "HOURS",
                  "line_scope": "Daily", "work_date": None, "pay_item_id": 1,
                  "calculated_amount": Decimal("16"), "snapshot_source_type": "DraftLine"}],
                totals, [], [], True, 88, "a" * 64)

    async def evidence(*_args):
        return True, 1, "b" * 64, [{"driver_id": 7, "work_date": None, "status_key_id": 3,
                                      "code": "OFF", "label": "Off", "is_off": True}], [{
            "bonus_event_id": 12, "driver_id": 7, "amount": Decimal("4"), "reason": "Quality",
            "notes": "Frozen", "data_revision": 2, "creator_user_id": 1,
            "creator_display_name": "Original", "created_at_utc": None,
        }]

    monkeypatch.setattr(report_read_model, "_period_context", lambda **_: _async(_period("Approved")))
    monkeypatch.setattr(report_read_model, "resolve_report_financial_authority", lambda **_: _async(authority))
    monkeypatch.setattr(report_read_model, "_columns", _columns)
    monkeypatch.setattr(report_read_model, "_pay_item_columns", _columns)
    monkeypatch.setattr(report_read_model, "_period_has_pay_item_snapshot", lambda *_: _async(True))
    monkeypatch.setattr(report_read_model, "_operational_rows", _operational)
    monkeypatch.setattr(report_read_model, "_financial_packet", financial)
    monkeypatch.setattr(report_read_model, "_snapshot_evidence", evidence)

    report = await report_read_model.build_report(
        report_type="mixed", period_id=9, company_id=1, user_id=1, db=None,
    )
    assert report["metadata"]["snapshot_id"] == 88
    assert report["metadata"]["report_evidence_available"] is True
    assert report["drivers"][0]["pay"]["total_pay"] == Decimal("20")
    assert report["drivers"][0]["bonus_events"][0]["creator_display_name"] == "Original"
    assert report["pay_totals"]["total_pay"] == Decimal("20")


@pytest.mark.asyncio
async def test_cancelled_report_fails_closed(monkeypatch):
    authority = SimpleNamespace(
        authority_kind=ReportAuthorityKind.UNAVAILABLE,
        snapshot_id=None, snapshot_hash=None, revision_number=None,
    )
    monkeypatch.setattr(report_read_model, "_period_context", lambda **_: _async(_period("Cancelled")))
    monkeypatch.setattr(report_read_model, "resolve_report_financial_authority", lambda **_: _async(authority))
    with pytest.raises(HTTPException, match="REPORT_UNAVAILABLE"):
        await report_read_model.build_report(
            report_type="drivers", period_id=9, company_id=1, user_id=1, db=None,
        )


# ---------------------------------------------------------------------------
# Public route coverage. These exercise FastAPI dependencies and PostgreSQL
# access controls rather than replacing them with mocked read-model calls.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prepared_route_matrix_is_operational_only(
    session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db, monkeypatch,
):
    period_id = await _insert_http_period(direct_db, paytest_branch_id, "Draft")
    await _insert_http_work(direct_db, period_id, paytest_branch_id, paytest_driver_id)

    async def live_packet_must_not_run(*_args, **_kwargs):
        raise AssertionError("Prepared reports must not invoke CP-4B live calculation")

    # Stage B4-17: report_read_model's Calculation edge now calls
    # app.payroll.period_calculation._build_live_calculation_packet
    # directly — the old report_read_model.service binding is no longer on
    # the production call path.
    monkeypatch.setattr(report_read_model.period_calculation, "_build_live_calculation_packet", live_packet_must_not_run)
    drivers = await _report(session_client, auth_token, period_id, "drivers")
    work = await _report(session_client, auth_token, period_id, "period-work")
    pay = await _report(session_client, auth_token, period_id, "period-pay")
    mixed = await _report(session_client, auth_token, period_id, "mixed")
    assert drivers.status_code == 200, drivers.text
    assert work.status_code == 200, work.text
    assert pay.status_code == 422 and "REPORT_FINANCIALS_UNAVAILABLE" in pay.text
    assert mixed.status_code == 200, mixed.text
    for response in (drivers, work, mixed):
        assert response.json()["metadata"]["financials_available"] is False
        assert response.json()["metadata"]["unavailable_reason"] == "SOURCE_ONLY_PERIOD"
        assert response.json()["pay_totals"] is None
        # Pay Item column metadata is not financial and remains available
        # even when Prepared has no financial authority; only the amounts do not.
        assert any(c["code"] == "HOURS" for c in response.json()["pay_item_columns"])
        assert response.json()["pay_item_totals"] is None
    assert mixed.json()["drivers"][0]["work"]["daily_rows"]
    assert mixed.json()["drivers"][0]["pay"] is None


@pytest.mark.asyncio
async def test_cancelled_routes_fail_closed_without_source_leak(
    session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
):
    period_id = await _insert_http_period(direct_db, paytest_branch_id, "Cancelled")
    await _insert_http_work(direct_db, period_id, paytest_branch_id, paytest_driver_id)
    for name in _REPORT_PATHS:
        response = await _report(session_client, auth_token, period_id, name)
        assert response.status_code == 422, response.text
        assert "REPORT_UNAVAILABLE" in response.text
        assert str(paytest_driver_id) not in response.text


@pytest.mark.asyncio
async def test_reports_view_allows_routes_without_payroll_view(
    session_client, auth_token, paytest_branch_id, direct_db,
):
    period_id = await _insert_http_period(direct_db, paytest_branch_id, "Draft")
    reports_only = await _reports_role_token(session_client, auth_token, paytest_branch_id, include_reports_view=True)
    response = await _report(session_client, reports_only, period_id, "drivers")
    assert response.status_code == 200, response.text
    assert response.json()["metadata"]["authority_kind"] == "SOURCE_ONLY"


@pytest.mark.asyncio
async def test_missing_reports_view_is_denied_at_route(
    session_client, auth_token, paytest_branch_id, direct_db,
):
    period_id = await _insert_http_period(direct_db, paytest_branch_id, "Draft")
    no_reports = await _reports_role_token(session_client, auth_token, paytest_branch_id, include_reports_view=False)
    response = await _report(session_client, no_reports, period_id, "drivers")
    assert response.status_code == 403, response.text
    assert "reports.view" in response.text


@pytest.mark.asyncio
async def test_driver_and_oda_roles_are_denied_at_report_routes(
    session_client, auth_token, paytest_branch_id, direct_db,
):
    period_id = await _insert_http_period(direct_db, paytest_branch_id, "Draft")
    for scope in ("SpecificBranch", "OwnDriverDataOnly"):
        token = await _driver_role_token(session_client, auth_token, paytest_branch_id, scope)
        response = await _report(session_client, token, period_id, "drivers")
        assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_reports_route_does_not_leak_inaccessible_branch_metadata(
    session_client, auth_token, paytest_branch_id, hq_branch_id, direct_db,
):
    foreign_branch_period = await _insert_http_period(direct_db, hq_branch_id, "Draft")
    limited_token = await _reports_role_token(
        session_client, auth_token, paytest_branch_id, include_reports_view=True,
    )
    response = await _report(session_client, limited_token, foreign_branch_period, "drivers")
    assert response.status_code == 403, response.text
    assert str(foreign_branch_period) not in response.text


@pytest.mark.asyncio
async def test_reports_route_does_not_leak_foreign_company_period(
    session_client, auth_token, direct_db,
):
    marker = uuid4().hex[:12]
    company_id = (await direct_db.execute(text("""
        INSERT INTO core.companies
            (companycode, companyname, legalname, status, issuspended, timezonename)
        VALUES (:code, :name, :name, 'Active', FALSE, 'UTC')
        RETURNING companyid
    """), {"code": f"CP5C-{marker}", "name": f"CP5C foreign {marker}"})).scalar_one()
    branch_id = (await direct_db.execute(text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (:company_id, :code, :name, 'Active', TRUE)
        RETURNING branchid
    """), {"company_id": company_id, "code": f"CP5C-{marker}", "name": f"CP5C foreign {marker}"})).scalar_one()
    period_id = (await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (:company_id, :branch_id, 'Draft', :code, :name, 'Week', '2099-02-01', '2099-02-07')
        RETURNING payrollperiodid
    """), {"company_id": company_id, "branch_id": branch_id,
           "code": f"CP5C-{marker}", "name": f"CP5C foreign {marker}"})).scalar_one()
    try:
        response = await _report(session_client, auth_token, int(period_id), "drivers")
        assert response.status_code == 404, response.text
        assert str(period_id) not in response.text
    finally:
        await direct_db.execute(text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :period_id"), {"period_id": period_id})
        await direct_db.execute(text("DELETE FROM core.branches WHERE branchid = :branch_id"), {"branch_id": branch_id})
        await direct_db.execute(text("DELETE FROM core.companies WHERE companyid = :company_id"), {"company_id": company_id})
        await direct_db.commit()


@pytest.mark.asyncio
async def test_report_columns_use_frozen_period_pay_item_metadata(
    session_client, auth_token, paytest_branch_id, direct_db,
):
    period_id = await _insert_http_period(direct_db, paytest_branch_id, "Draft")
    snapshot = (await direct_db.execute(text("""
        SELECT payitemid, payitemcode, COALESCE(displaylabel, payitemname) AS label,
               unit, sortorder, appearsinreports
        FROM payroll.payrollperiodpayitems
        WHERE payrollperiodid = :period_id AND appearsinreports = TRUE
        ORDER BY sortorder, payitemcode, payitemid
        LIMIT 1
    """), {"period_id": period_id})).mappings().one()
    await direct_db.execute(text("""
        UPDATE payroll.payitems SET payitemname = 'Mutable CP5C catalog name', status = 'Retired'
        WHERE payitemid = :pay_item_id
    """), {"pay_item_id": snapshot["payitemid"]})
    await direct_db.commit()
    try:
        response = await _report(session_client, auth_token, period_id, "drivers")
        assert response.status_code == 200, response.text
        column = next(c for c in response.json()["columns"] if c["pay_item_id"] == snapshot["payitemid"])
        assert column["label"] == snapshot["label"]
        assert column["code"] == snapshot["payitemcode"]
        assert column["unit"] == snapshot["unit"]
        assert column["sort_order"] == snapshot["sortorder"]
        assert snapshot["appearsinreports"] is True
    finally:
        await direct_db.execute(text("""
            UPDATE payroll.payitems
            SET payitemname = p.payitemname, status = p.payitemstatusatsnapshot
            FROM payroll.payrollperiodpayitems p
            WHERE payroll.payitems.payitemid = p.payitemid
              AND p.payrollperiodid = :period_id
              AND p.payitemid = :pay_item_id
        """), {"period_id": period_id, "pay_item_id": snapshot["payitemid"]})
        await direct_db.commit()


@pytest.mark.asyncio
async def test_inreview_routes_use_linked_snapshot_and_frozen_status_bonus_evidence(
    session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
):
    period_id, snapshot_id, _, status_key_id, bonus_id = await _seed_submitted_snapshot(
        direct_db, paytest_branch_id, paytest_driver_id,
    )
    before = await _report(session_client, auth_token, period_id, "drivers")
    assert before.status_code == 200, before.text
    payload = before.json()
    assert payload["metadata"]["authority_kind"] == "SUBMITTED_SNAPSHOT"
    assert payload["metadata"]["snapshot_id"] == snapshot_id
    assert payload["metadata"]["report_evidence_available"] is True
    assert Decimal(payload["pay_totals"]["total_pay"]) == Decimal("20")
    assert payload["drivers"][0]["work"]["status_entries"][0]["label"].startswith("Frozen")
    assert payload["drivers"][0]["bonus_events"][0]["reason"] == "CP5C"
    frozen_bonus = payload["drivers"][0]["bonus_events"][0]
    assert frozen_bonus["bonus_event_id"] == bonus_id
    assert frozen_bonus["driver_id"] == paytest_driver_id
    assert Decimal(frozen_bonus["amount"]) == Decimal("4")
    assert frozen_bonus["creator_user_id"] == 1
    assert frozen_bonus["creator_display_name"] == "Admin User"
    assert frozen_bonus["created_at_utc"] is not None
    assert frozen_bonus["data_revision"] == 1

    await direct_db.execute(text("UPDATE payroll.payrollstatuskeys SET keyname = 'Mutable label' WHERE statuskeyid = :id"), {"id": status_key_id})
    await direct_db.execute(text("UPDATE payroll.payrollbonusevents SET reason = 'Mutable reason', notes = 'Mutable notes' WHERE payrollbonuseventid = :id"), {"id": bonus_id})
    await direct_db.execute(text("UPDATE sec.users SET displayname = 'Mutable creator' WHERE userid = 1"))
    await direct_db.commit()
    after = await _report(session_client, auth_token, period_id, "mixed")
    assert after.status_code == 200, after.text
    frozen = after.json()["drivers"][0]
    assert frozen["work"]["status_entries"][0]["label"].startswith("Frozen")
    assert frozen["bonus_events"][0]["reason"] == "CP5C"
    assert frozen["bonus_events"][0]["notes"] == "Frozen bonus"
    assert frozen["bonus_events"][0]["creator_user_id"] == 1
    assert frozen["bonus_events"][0]["creator_display_name"] == "Admin User"
    await direct_db.execute(text("UPDATE sec.users SET displayname = 'Admin User' WHERE userid = 1"))
    await direct_db.commit()


@pytest.mark.asyncio
async def test_approved_routes_use_exact_approved_review_snapshot(
    session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
):
    period_id, snapshot_id, review_id, _, _ = await _seed_submitted_snapshot(
        direct_db, paytest_branch_id, paytest_driver_id,
    )
    decision = await session_client.post(
        f"/review/items/{review_id}/decide", json={"decision": "Approved"}, headers=_auth(auth_token),
    )
    assert decision.status_code == 200, decision.text
    for name in ("drivers", "period-pay", "mixed"):
        response = await _report(session_client, auth_token, period_id, name)
        assert response.status_code == 200, response.text
        assert response.json()["metadata"]["authority_kind"] == "APPROVED_SNAPSHOT"
        assert response.json()["metadata"]["snapshot_id"] == snapshot_id
        assert Decimal(response.json()["pay_totals"]["total_pay"]) == Decimal("20")


@pytest.mark.asyncio
async def test_return_decision_switches_report_from_submitted_snapshot_to_live_authority(
    session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
):
    period_id, snapshot_id, review_id, _, _ = await _seed_submitted_snapshot(
        direct_db, paytest_branch_id, paytest_driver_id,
    )
    returned = await session_client.post(
        f"/review/items/{review_id}/decide",
        json={"decision": "EditRequested", "decision_reason": "Correct source"},
        headers=_auth(auth_token),
    )
    assert returned.status_code == 200, returned.text
    report = await _report(session_client, auth_token, period_id, "mixed")
    assert report.status_code == 200, report.text
    payload = report.json()
    assert payload["metadata"]["period_status"] == "Returned"
    assert payload["metadata"]["authority_kind"] == "LIVE"
    assert payload["metadata"]["snapshot_id"] is None
    assert Decimal(payload["pay_totals"]["total_pay"]) != Decimal("20")
    assert snapshot_id > 0


@pytest.mark.asyncio
async def test_resubmit_creates_the_next_report_snapshot_revision(
    session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
):
    period_id, first_snapshot_id, review_id, _, _ = await _seed_submitted_snapshot(
        direct_db, paytest_branch_id, paytest_driver_id,
    )
    # An approved rate is required so the corrected HOURS line resolves
    # cleanly and does not block resubmission with a manager-review flag.
    hourly_rate_type_id = (await direct_db.execute(text("""
        SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = 'HOURLY'
    """))).scalar_one()
    await direct_db.execute(text("""
        INSERT INTO payroll.driverrates
            (companyid, branchid, driverid, ratetypeid, amount, effectivefrom, status, createdbyuserid)
        VALUES (1, :branch_id, :driver_id, :rate_type_id, 5.0000, '2099-01-01', 'Approved', 1)
    """), {"branch_id": paytest_branch_id, "driver_id": paytest_driver_id, "rate_type_id": hourly_rate_type_id})
    await direct_db.commit()
    returned = await session_client.post(
        f"/review/items/{review_id}/decide",
        json={"decision": "EditRequested", "decision_reason": "Correct source"},
        headers=_auth(auth_token),
    )
    assert returned.status_code == 200, returned.text
    lines = await session_client.get(f"/payroll/periods/{period_id}/lines", headers=_auth(auth_token))
    assert lines.status_code == 200, lines.text
    line_id = lines.json()[0]["draft_line_id"]
    corrected = await session_client.patch(
        f"/payroll/periods/{period_id}/lines/{line_id}", json={"quantity": "2.0000"}, headers=_auth(auth_token),
    )
    assert corrected.status_code == 200, corrected.text
    live_preview = await session_client.get(
        f"/payroll/periods/{period_id}/calculation-preview", headers=_auth(auth_token),
    )
    assert live_preview.status_code == 200, live_preview.text
    for name in ("drivers", "period-work", "period-pay", "mixed"):
        report_before_resubmit = await _report(session_client, auth_token, period_id, name)
        assert report_before_resubmit.status_code == 200, report_before_resubmit.text
        payload_before_resubmit = report_before_resubmit.json()
        assert payload_before_resubmit["metadata"]["authority_kind"] == "LIVE"
        if name != "period-pay":
            assert Decimal(payload_before_resubmit["work_totals"]["HOURS"]) == Decimal("2")
        if payload_before_resubmit["pay_totals"] is not None:
            assert Decimal(payload_before_resubmit["pay_totals"]["total_pay"]) == Decimal(live_preview.json()["total_expected_pay"])
    resubmitted = await session_client.post(
        f"/payroll/periods/{period_id}/resubmissions", headers=_auth(auth_token),
    )
    assert resubmitted.status_code == 200, resubmitted.text
    report = await _report(session_client, auth_token, period_id, "drivers")
    assert report.status_code == 200, report.text
    payload = report.json()
    assert payload["metadata"]["authority_kind"] == "SUBMITTED_SNAPSHOT"
    assert payload["metadata"]["snapshot_id"] != first_snapshot_id
    assert payload["metadata"]["revision_number"] == 2


@pytest.mark.asyncio
async def test_zero_evidence_snapshot_is_available_and_empty_over_http(
    session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
):
    period_id, snapshot_id, _, status_key_id, bonus_id = await _seed_submitted_snapshot(
        direct_db, paytest_branch_id, paytest_driver_id, include_evidence=False,
    )
    assert status_key_id is None and bonus_id is None
    response = await _report(session_client, auth_token, period_id, "mixed")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["metadata"]["authority_kind"] == "SUBMITTED_SNAPSHOT"
    assert payload["metadata"]["snapshot_id"] == snapshot_id
    assert payload["metadata"]["report_evidence_available"] is True
    assert payload["metadata"]["report_evidence_version"] == 1
    assert payload["metadata"]["report_evidence_hash"]
    assert payload["drivers"][0]["work"]["status_entries"] == []
    assert payload["drivers"][0]["bonus_events"] == []


@pytest.mark.asyncio
async def test_legacy_snapshot_keeps_money_but_never_falls_back_to_live_evidence(
    session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
):
    period_id, snapshot_id = await _seed_legacy_snapshot(direct_db, paytest_branch_id, paytest_driver_id)
    response = await _report(session_client, auth_token, period_id, "drivers")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["metadata"]["authority_kind"] == "SUBMITTED_SNAPSHOT"
    assert payload["metadata"]["snapshot_id"] == snapshot_id
    assert payload["metadata"]["report_evidence_available"] is False
    assert payload["metadata"]["report_evidence_version"] is None
    assert payload["metadata"]["report_evidence_hash"] is None
    assert Decimal(payload["pay_totals"]["total_pay"]) == Decimal("16")
    assert payload["drivers"][0]["work"]["status_entries"] == []
    assert payload["drivers"][0]["bonus_events"] == []


@pytest.mark.asyncio
async def test_frozen_status_summaries_count_off_and_no_pay_non_off_entries(
    session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
):
    period_id, _, _, _, _ = await _seed_submitted_snapshot(
        direct_db, paytest_branch_id, paytest_driver_id, include_multiple_statuses=True,
    )
    response = await _report(session_client, auth_token, period_id, "period-work")
    assert response.status_code == 200, response.text
    work = response.json()["drivers"][0]["work"]
    assert len(work["status_entries"]) == 2
    summaries = {row["is_off"]: row for row in work["status_summaries"]}
    assert summaries[True]["count"] == 1
    assert summaries[False]["count"] == 1
    assert all(row["code"] != "STATUS_PAY" for row in work["status_entries"])


@pytest.mark.asyncio
async def test_snapshot_reports_project_min_max_adjustments_without_rule_replay(
    session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
):
    period_id, _, _, _, _ = await _seed_submitted_snapshot(
        direct_db,
        paytest_branch_id,
        paytest_driver_id,
        system_adjustments=(("SYS_MIN_TOPUP", Decimal("5")), ("SYS_MAX_CAP", Decimal("-2"))),
    )
    for name in ("drivers", "period-pay", "mixed"):
        response = await _report(session_client, auth_token, period_id, name)
        assert response.status_code == 200, response.text
        pay = response.json()["pay_totals"]
        assert Decimal(pay["minimum_adjustment"]) == Decimal("5")
        assert Decimal(pay["maximum_adjustment"]) == Decimal("-2")
        assert Decimal(pay["bonus_total"]) == Decimal("4")
        assert Decimal(pay["total_pay"]) == Decimal("23")


@pytest.mark.asyncio
async def test_period_pay_report_uses_effective_dated_cp4b_amounts_over_one_rate_math(
    session_client, auth_token, paytest_branch_id, direct_db,
):
    driver = await session_client.post(
        "/core/drivers",
        json={"branch_id": paytest_branch_id, "full_name": f"CP5C rate {uuid4().hex[:10]}"},
        headers=_auth(auth_token),
    )
    assert driver.status_code == 201, driver.text
    driver_id = driver.json()["driver_id"]
    rate_types = await session_client.get("/payroll/rate-types", headers=_auth(auth_token))
    assert rate_types.status_code == 200, rate_types.text
    hourly_rate_type_id = next(row["rate_type_id"] for row in rate_types.json() if row["rate_code"] == "HOURLY")
    for amount, effective_from in (("10.00", "2082-06-21"), ("20.00", "2082-06-25")):
        created = await session_client.post(
            "/payroll/rates",
            json={"driver_id": driver_id, "rate_type_id": hourly_rate_type_id,
                  "amount": amount, "effective_from": effective_from},
            headers=_auth(auth_token),
        )
        assert created.status_code == 201, created.text
        approved = await session_client.post(
            f"/payroll/rates/{created.json()['driver_rate_id']}/approve", headers=_auth(auth_token),
        )
        assert approved.status_code == 200, approved.text
    period_id = await _insert_http_period(
        direct_db, paytest_branch_id, "Open", start_date=date(2082, 6, 21), end_date=date(2082, 6, 27),
    )
    for work_date in ("2082-06-24", "2082-06-25"):
        line = await session_client.post(
            f"/payroll/periods/{period_id}/lines",
            json={"driver_id": driver_id, "work_date": work_date, "line_type": "HOURS", "quantity": "3.0000"},
            headers=_auth(auth_token),
        )
        assert line.status_code == 201, line.text
    preview = await session_client.get(
        f"/payroll/periods/{period_id}/calculation-preview", headers=_auth(auth_token),
    )
    assert preview.status_code == 200, preview.text
    authoritative = Decimal(preview.json()["total_expected_pay"])
    naive_one_rate = Decimal("6") * Decimal("20")
    assert authoritative == Decimal("90")
    assert naive_one_rate != authoritative
    report = await _report(session_client, auth_token, period_id, "period-pay")
    assert report.status_code == 200, report.text
    assert Decimal(report.json()["pay_totals"]["total_pay"]) == authoritative


@pytest.mark.asyncio
async def test_live_cp4b_blockers_propagate_through_report_metadata(
    session_client, auth_token, paytest_branch_id, direct_db,
):
    driver = await session_client.post(
        "/core/drivers",
        json={"branch_id": paytest_branch_id, "full_name": f"CP5C blocker {uuid4().hex[:10]}"},
        headers=_auth(auth_token),
    )
    assert driver.status_code == 201, driver.text
    period_id = await _insert_http_period(direct_db, paytest_branch_id, "Open")
    line = await session_client.post(
        f"/payroll/periods/{period_id}/lines",
        json={"driver_id": driver.json()["driver_id"], "work_date": "2099-01-01",
              "line_type": "HOURS", "quantity": "3.0000"},
        headers=_auth(auth_token),
    )
    assert line.status_code == 201, line.text
    preview = await session_client.get(
        f"/payroll/periods/{period_id}/calculation-preview", headers=_auth(auth_token),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["has_blockers"] is True
    report = await _report(session_client, auth_token, period_id, "mixed")
    assert report.status_code == 200, report.text
    metadata = report.json()["metadata"]
    assert metadata["authority_kind"] == "LIVE"
    assert metadata["blockers"] == preview.json()["blockers"]
    assert metadata["warnings"] == preview.json()["warnings"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ("Open", "Returned"))
async def test_live_report_routes_reconcile_with_cp4b_preview(
    session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db, status,
):
    period_id = await _insert_http_period(direct_db, paytest_branch_id, status)
    await _insert_http_work(direct_db, period_id, paytest_branch_id, paytest_driver_id, line_type="HOURS")
    preview = await session_client.get(
        f"/payroll/periods/{period_id}/calculation-preview", headers=_auth(auth_token),
    )
    assert preview.status_code == 200, preview.text
    expected_total = Decimal(preview.json()["total_expected_pay"])
    drivers = await _report(session_client, auth_token, period_id, "drivers")
    work = await _report(session_client, auth_token, period_id, "period-work")
    pay = await _report(session_client, auth_token, period_id, "period-pay")
    mixed = await _report(session_client, auth_token, period_id, "mixed")
    for response in (drivers, work, pay, mixed):
        assert response.status_code == 200, response.text
    for response in (drivers, pay, mixed):
        assert response.json()["metadata"]["authority_kind"] == "LIVE"
        assert Decimal(response.json()["pay_totals"]["total_pay"]) == expected_total
    assert work.json()["work_totals"] == mixed.json()["work_totals"]
    assert work.json()["drivers"][0]["work"] == mixed.json()["drivers"][0]["work"]


@pytest.mark.asyncio
async def test_locked_route_uses_final_lines_and_originating_snapshot_evidence(
    session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db, test_database_url,
):
    period_id, snapshot_id, review_id, status_key_id, bonus_id = await _seed_submitted_snapshot(
        direct_db, paytest_branch_id, paytest_driver_id,
    )
    await direct_db.execute(text("UPDATE review.managerreviewitems SET status = 'Approved' WHERE reviewitemid = :review_id"), {"review_id": review_id})
    await direct_db.execute(text("UPDATE payroll.payrollperiods SET status = 'Approved' WHERE payrollperiodid = :period_id"), {"period_id": period_id})
    await direct_db.commit()
    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.begin() as finalize_db:
            result = await finalize_period(period_id, 1, 1, finalize_db)
            assert result.status == "Locked"
    finally:
        await engine.dispose()
    await direct_db.execute(text("UPDATE payroll.payrollstatuskeys SET keyname = 'Mutable locked label' WHERE statuskeyid = :id"), {"id": status_key_id})
    await direct_db.execute(text("UPDATE payroll.payrollbonusevents SET reason = 'Mutable locked reason' WHERE payrollbonuseventid = :id"), {"id": bonus_id})
    await direct_db.commit()
    response = await _report(session_client, auth_token, period_id, "drivers")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["metadata"]["authority_kind"] == "FINAL_LINES"
    assert payload["metadata"]["snapshot_id"] == snapshot_id
    assert Decimal(payload["pay_totals"]["total_pay"]) == Decimal("20")
    assert payload["drivers"][0]["work"]["status_entries"][0]["label"].startswith("Frozen")
    assert payload["drivers"][0]["bonus_events"][0]["reason"] == "CP5C"
    archived = await session_client.patch(
        f"/payroll/periods/{period_id}/status", json={"status": "Archived"}, headers=_auth(auth_token),
    )
    assert archived.status_code == 200, archived.text
    archived_report = await _report(session_client, auth_token, period_id, "mixed")
    assert archived_report.status_code == 200, archived_report.text
    archived_payload = archived_report.json()
    assert archived_payload["metadata"]["authority_kind"] == "FINAL_LINES"
    assert archived_payload["metadata"]["snapshot_id"] == snapshot_id
    assert Decimal(archived_payload["pay_totals"]["total_pay"]) == Decimal("20")
    assert archived_payload["drivers"][0]["bonus_events"][0]["reason"] == "CP5C"


async def _async(value):
    return value


# ---------------------------------------------------------------------------
# C1-3A — Period Pay per-Daily-Pay-Item money.
# ---------------------------------------------------------------------------

async def _approve_rate(
    session_client: httpx.AsyncClient, auth_token: str, driver_id: int,
    rate_code: str, amount: str, effective_from: str = "2098-01-01",
) -> None:
    # Deliberately before the shared 2099-01-01..2099-01-07 default period
    # window: other tests in this module leave Locked/Archived residue there
    # (never cancelled, unlike Draft/Open/InReview/Returned/Approved), and
    # rate approval rejects any effective_from inside a finalized period for
    # the same branch. An open-ended earlier effective date still applies.
    rate_types = await session_client.get("/payroll/rate-types", headers=_auth(auth_token))
    assert rate_types.status_code == 200, rate_types.text
    rate_type_id = next(row["rate_type_id"] for row in rate_types.json() if row["rate_code"] == rate_code)
    created = await session_client.post(
        "/payroll/rates",
        json={"driver_id": driver_id, "rate_type_id": rate_type_id,
              "amount": amount, "effective_from": effective_from},
        headers=_auth(auth_token),
    )
    assert created.status_code == 201, created.text
    approved = await session_client.post(
        f"/payroll/rates/{created.json()['driver_rate_id']}/approve", headers=_auth(auth_token),
    )
    assert approved.status_code == 200, approved.text


async def _pay_item_id(direct_db, period_id: int, code: str) -> int:
    return int((await direct_db.execute(text("""
        SELECT payitemid FROM payroll.payrollperiodpayitems
        WHERE payrollperiodid = :period_id AND payitemcode = :code
    """), {"period_id": period_id, "code": code})).scalar_one())


def _pay_gross_reconciles(pay: dict) -> None:
    gross = Decimal(pay["daily_pay"]) + Decimal(pay["status_pay"]) + Decimal(pay["period_pay"])
    assert Decimal(pay["gross_pay"]) == gross
    assert (
        gross + Decimal(pay["minimum_adjustment"]) + Decimal(pay["maximum_adjustment"]) + Decimal(pay["bonus_total"])
        == Decimal(pay["total_pay"])
    )


@pytest.mark.asyncio
async def test_open_period_pay_item_amounts_dense_and_reconcile(
    session_client, auth_token, paytest_branch_id, direct_db,
):
    driver_ids = []
    for _ in range(2):
        created = await session_client.post(
            "/core/drivers",
            json={"branch_id": paytest_branch_id, "full_name": f"CP5C item {uuid4().hex[:8]}"},
            headers=_auth(auth_token),
        )
        assert created.status_code == 201, created.text
        driver_ids.append(created.json()["driver_id"])
    driver_a, driver_b = driver_ids
    await _approve_rate(session_client, auth_token, driver_a, "HOURLY", "10.00")
    await _approve_rate(session_client, auth_token, driver_b, "HOURLY", "10.00")
    await _approve_rate(session_client, auth_token, driver_b, "MILEAGE", "1.00")
    period_id = await _insert_http_period(direct_db, paytest_branch_id, "Open")
    hours_id = await _pay_item_id(direct_db, period_id, "HOURS")
    miles_id = await _pay_item_id(direct_db, period_id, "MILES")
    for driver_id, work_date, line_type, qty in (
        (driver_a, "2099-01-01", "HOURS", "2.0000"),
        (driver_b, "2099-01-01", "HOURS", "3.0000"),
        (driver_b, "2099-01-02", "MILES", "10.0000"),
    ):
        line = await session_client.post(
            f"/payroll/periods/{period_id}/lines",
            json={"driver_id": driver_id, "work_date": work_date, "line_type": line_type, "quantity": qty},
            headers=_auth(auth_token),
        )
        assert line.status_code == 201, line.text
    report = await _report(session_client, auth_token, period_id, "period-pay")
    assert report.status_code == 200, report.text
    payload = report.json()
    column_ids = [c["pay_item_id"] for c in payload["pay_item_columns"]]
    assert hours_id in column_ids
    assert miles_id in column_ids
    by_driver = {d["driver_id"]: d for d in payload["drivers"]}
    a_amounts = {a["pay_item_id"]: Decimal(a["amount"]) for a in by_driver[driver_a]["pay"]["pay_item_amounts"]}
    b_amounts = {a["pay_item_id"]: Decimal(a["amount"]) for a in by_driver[driver_b]["pay"]["pay_item_amounts"]}
    # Dense: every active reportable Daily column is present per driver.
    assert set(a_amounts) == set(column_ids)
    assert set(b_amounts) == set(column_ids)
    # Driver A never touched MILES: present as an explicit zero, not absent.
    assert a_amounts[miles_id] == Decimal("0")
    assert a_amounts[hours_id] == Decimal("20")
    assert b_amounts[hours_id] == Decimal("30")
    assert b_amounts[miles_id] == Decimal("10")
    for driver_id in (driver_a, driver_b):
        amounts = a_amounts if driver_id == driver_a else b_amounts
        pay = by_driver[driver_id]["pay"]
        # Only Daily items exist in this period, so per-item sum == daily_pay.
        assert Decimal(pay["daily_pay"]) == sum(amounts.values(), Decimal("0"))
        _pay_gross_reconciles(pay)
    totals = {t["pay_item_id"]: Decimal(t["amount"]) for t in payload["pay_item_totals"]}
    assert totals[hours_id] == a_amounts[hours_id] + b_amounts[hours_id]
    assert totals[miles_id] == a_amounts[miles_id] + b_amounts[miles_id]
    # Unused active columns (LOADS etc.) still total zero, not absent.
    unused = [c for c in column_ids if c not in (hours_id, miles_id)]
    assert unused, "expected at least one other active Daily column with zero usage"
    for cid in unused:
        assert totals[cid] == Decimal("0")


@pytest.mark.asyncio
async def test_returned_period_pay_reflects_corrected_live_money(
    session_client, auth_token, paytest_branch_id, direct_db,
):
    # A fresh driver (not the shared paytest_driver_id) — other tests in this
    # module may have already given paytest_driver_id an Approved HOURLY rate.
    created = await session_client.post(
        "/core/drivers",
        json={"branch_id": paytest_branch_id, "full_name": f"CP5C returned {uuid4().hex[:8]}"},
        headers=_auth(auth_token),
    )
    assert created.status_code == 201, created.text
    driver_id = created.json()["driver_id"]
    period_id, _, review_id, _, _ = await _seed_submitted_snapshot(
        direct_db, paytest_branch_id, driver_id,
    )
    await _approve_rate(session_client, auth_token, driver_id, "HOURLY", "7.00")
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
        json={"quantity": "4.0000"}, headers=_auth(auth_token),
    )
    assert corrected.status_code == 200, corrected.text
    hours_id = await _pay_item_id(direct_db, period_id, "HOURS")
    report = await _report(session_client, auth_token, period_id, "period-pay")
    assert report.status_code == 200, report.text
    payload = report.json()
    assert payload["metadata"]["authority_kind"] == "LIVE"
    driver = next(d for d in payload["drivers"] if d["driver_id"] == driver_id)
    amounts = {a["pay_item_id"]: Decimal(a["amount"]) for a in driver["pay"]["pay_item_amounts"]}
    # Corrected LIVE money (4 x 7 = 28), never the stale $16 submitted snapshot.
    assert amounts[hours_id] == Decimal("28")
    assert amounts[hours_id] != Decimal("16")
    _pay_gross_reconciles(driver["pay"])


@pytest.mark.asyncio
async def test_live_fallback_amount_reconciles_pay_item_total(
    session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
):
    period_id = await _insert_http_period(direct_db, paytest_branch_id, "Open")
    hours_id = await _pay_item_id(direct_db, period_id, "HOURS")
    # A manager-flagged line with a manual rate override and NULL
    # calculatedamount is excluded from live re-resolution (the manager-NMR
    # guard) — the packet must still use its fallback qty*rate amount for
    # per-item aggregation, not the raw (None) calculated_amount.
    await direct_db.execute(text("""
        INSERT INTO payroll.payrolldraftlines
            (companyid, branchid, payrollperiodid, driverid, workdate, linetype, linescope,
             quantity, rateamount, calculatedamount, sourcetype, status, needsmanagerreview, addedbyuserid)
        VALUES (1, :branch_id, :period_id, :driver_id, '2099-01-01', 'HOURS', 'Daily',
                2.0000, 15.0000, NULL, 'Manual', 'Active', TRUE, 1)
    """), {"branch_id": paytest_branch_id, "period_id": period_id, "driver_id": paytest_driver_id})
    await direct_db.commit()
    report = await _report(session_client, auth_token, period_id, "drivers")
    assert report.status_code == 200, report.text
    payload = report.json()
    driver = next(d for d in payload["drivers"] if d["driver_id"] == paytest_driver_id)
    pay = driver["pay"]
    assert Decimal(pay["daily_pay"]) == Decimal("30")
    amounts = {a["pay_item_id"]: Decimal(a["amount"]) for a in pay["pay_item_amounts"]}
    assert amounts[hours_id] == Decimal("30")
    line = next(row for row in pay["financial_lines"] if row["line_type"] == "HOURS")
    assert Decimal(line["calculated_amount"]) == Decimal("30")


def test_pay_item_amounts_excludes_status_bonus_and_system_lines():
    lines = [
        # Public source_type is the raw LIVE provenance value ("Manual"), not
        # the category — classification must use snapshot_source_type, never
        # source_type, so this line is still correctly aggregated.
        {"driver_id": 1, "line_scope": "Daily", "source_type": "Manual", "snapshot_source_type": "DraftLine",
         "pay_item_id": 10, "calculated_amount": Decimal("50")},
        {"driver_id": 1, "line_scope": "Daily", "source_type": "StatusEntryState", "snapshot_source_type": "StatusEntryState",
         "pay_item_id": None, "calculated_amount": Decimal("99")},
        {"driver_id": 1, "line_scope": "Period", "source_type": "BonusEvent", "snapshot_source_type": "BonusEvent",
         "pay_item_id": None, "calculated_amount": Decimal("5")},
        {"driver_id": 1, "line_scope": "Period", "source_type": "System", "snapshot_source_type": "System",
         "pay_item_id": None, "calculated_amount": Decimal("7")},
    ]
    per_driver, totals = report_read_model._pay_item_amounts(lines, [10])
    assert per_driver[1] == {10: Decimal("50")}
    assert totals == {10: Decimal("50")}


@pytest.mark.asyncio
async def test_snapshot_authority_pay_item_amounts_frozen_against_later_mutation(
    session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
):
    period_id, snapshot_id, review_id, _, _ = await _seed_submitted_snapshot(
        direct_db, paytest_branch_id, paytest_driver_id,
    )
    hours_id = await _pay_item_id(direct_db, period_id, "HOURS")
    # Mutate the underlying (now dormant) DraftLine after the snapshot was
    # captured; InReview/Approved must keep reading the frozen $16, not it.
    await direct_db.execute(text("""
        UPDATE payroll.payrolldraftlines SET calculatedamount = 9999.0000
        WHERE payrollperiodid = :period_id
    """), {"period_id": period_id})
    await direct_db.commit()
    for name in ("drivers", "period-pay", "mixed"):
        response = await _report(session_client, auth_token, period_id, name)
        assert response.status_code == 200, response.text
        driver = next(d for d in response.json()["drivers"] if d["driver_id"] == paytest_driver_id)
        amounts = {a["pay_item_id"]: Decimal(a["amount"]) for a in driver["pay"]["pay_item_amounts"]}
        assert amounts[hours_id] == Decimal("16")
        _pay_gross_reconciles(driver["pay"])
    decision = await session_client.post(
        f"/review/items/{review_id}/decide", json={"decision": "Approved"}, headers=_auth(auth_token),
    )
    assert decision.status_code == 200, decision.text
    approved_report = await _report(session_client, auth_token, period_id, "period-pay")
    assert approved_report.status_code == 200, approved_report.text
    approved_payload = approved_report.json()
    assert approved_payload["metadata"]["authority_kind"] == "APPROVED_SNAPSHOT"
    assert approved_payload["metadata"]["snapshot_id"] == snapshot_id
    approved_driver = next(d for d in approved_payload["drivers"] if d["driver_id"] == paytest_driver_id)
    approved_amounts = {a["pay_item_id"]: Decimal(a["amount"]) for a in approved_driver["pay"]["pay_item_amounts"]}
    assert approved_amounts[hours_id] == Decimal("16")
    _pay_gross_reconciles(approved_driver["pay"])


@pytest.mark.asyncio
async def test_locked_and_archived_status_lines_classified_separately_from_pay_items(
    session_client, auth_token, paytest_branch_id, direct_db,
):
    marker = uuid4().hex[:12]
    employee_id = int((await direct_db.execute(text("""
        INSERT INTO core.employees
            (companyid, branchid, fullname, employeetype, employmentstatus, createdbyuserid)
        VALUES (1, :branch_id, :name, 'Driver', 'Active', 1)
        RETURNING employeeid
    """), {"branch_id": paytest_branch_id, "name": f"CP5C status split {marker}"})).scalar_one())
    driver_id = int((await direct_db.execute(text("""
        INSERT INTO core.drivers (companyid, branchid, employeeid, drivercode, driverstatus)
        VALUES (1, :branch_id, :employee_id, :code, 'Active')
        RETURNING driverid
    """), {"branch_id": paytest_branch_id, "employee_id": employee_id, "code": f"CP5C-{marker[:20]}"})).scalar_one())
    period_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (1, :branch_id, 'Locked', :code, 'CP5C status classification', 'Week', '2097-01-01', '2097-01-07')
        RETURNING payrollperiodid
    """), {"branch_id": paytest_branch_id, "code": f"CP5C-{marker}"})).scalar_one())
    await _create_period_pay_item_rows(
        period_id=period_id, company_id=1, branch_id=paytest_branch_id, start_date=date(2097, 1, 1), db=direct_db,
    )
    hours_id = await _pay_item_id(direct_db, period_id, "HOURS")
    await direct_db.execute(text("SELECT set_config('app.allow_payroll_final_line_insert', 'true', false)"))
    await direct_db.execute(text("""
        INSERT INTO payroll.payrollfinallines
            (companyid, branchid, payrollperiodid, driverid, workdate, linetype, linescope,
             payitemid, quantity, finalamount, sourcetype, approvedbyuserid, approvedatutc, lockedatutc)
        VALUES
            (1, :branch_id, :period_id, :driver_id, '2097-01-01', 'HOURS', 'Daily',
             :hours_id, 2, 20.0000, 'DraftLine', 1, NOW(), NOW()),
            (1, :branch_id, :period_id, :driver_id, '2097-01-02', 'STATUS_PAY', 'Daily',
             NULL, 1, 5.0000, 'StatusEntryState', 1, NOW(), NOW())
    """), {"branch_id": paytest_branch_id, "period_id": period_id, "driver_id": driver_id, "hours_id": hours_id})
    await direct_db.commit()
    for name in ("drivers", "period-pay", "mixed"):
        response = await _report(session_client, auth_token, period_id, name)
        assert response.status_code == 200, response.text
        payload = response.json()
        driver = next(d for d in payload["drivers"] if d["driver_id"] == driver_id)
        pay = driver["pay"]
        assert Decimal(pay["daily_pay"]) == Decimal("20")
        assert Decimal(pay["status_pay"]) == Decimal("5")
        amounts = {a["pay_item_id"]: Decimal(a["amount"]) for a in pay["pay_item_amounts"]}
        assert amounts[hours_id] == Decimal("20")
        assert Decimal("5") not in amounts.values()
        _pay_gross_reconciles(pay)
    archived = await session_client.patch(
        f"/payroll/periods/{period_id}/status", json={"status": "Archived"}, headers=_auth(auth_token),
    )
    assert archived.status_code == 200, archived.text
    archived_report = await _report(session_client, auth_token, period_id, "mixed")
    assert archived_report.status_code == 200, archived_report.text
    archived_driver = next(d for d in archived_report.json()["drivers"] if d["driver_id"] == driver_id)
    assert Decimal(archived_driver["pay"]["status_pay"]) == Decimal("5")
    assert Decimal(archived_driver["pay"]["daily_pay"]) == Decimal("20")


@pytest.mark.asyncio
async def test_current_and_finalized_period_pay_reconcile_for_locked_period(
    session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db, test_database_url,
):
    period_id, _, review_id, _, _ = await _seed_submitted_snapshot(
        direct_db, paytest_branch_id, paytest_driver_id,
    )
    await direct_db.execute(text("UPDATE review.managerreviewitems SET status = 'Approved' WHERE reviewitemid = :id"), {"id": review_id})
    await direct_db.execute(text("UPDATE payroll.payrollperiods SET status = 'Approved' WHERE payrollperiodid = :id"), {"id": period_id})
    await direct_db.commit()
    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.begin() as finalize_db:
            result = await finalize_period(period_id, 1, 1, finalize_db)
            assert result.status == "Locked"
    finally:
        await engine.dispose()
    for view in ("drivers", "period-work", "period-pay", "mixed"):
        current = await _report(session_client, auth_token, period_id, view)
        finalized = await session_client.get(
            f"/payroll/finalized/{period_id}/reports/{view}", headers=_auth(auth_token),
        )
        assert current.status_code == 200, current.text
        assert finalized.status_code == 200, finalized.text
        current_body, finalized_body = current.json(), finalized.json()
        assert (
            sorted(c["pay_item_id"] for c in current_body["pay_item_columns"])
            == sorted(c["pay_item_id"] for c in finalized_body["pay_item_columns"])
        )
        assert current_body["pay_item_totals"] == finalized_body["pay_item_totals"]
        assert current_body["pay_totals"] == finalized_body["pay_totals"]
        current_driver = next(d for d in current_body["drivers"] if d["driver_id"] == paytest_driver_id)
        finalized_driver = next(d for d in finalized_body["drivers"] if d["driver_id"] == paytest_driver_id)
        assert current_driver["pay"]["pay_item_amounts"] == finalized_driver["pay"]["pay_item_amounts"]
        assert current_driver["pay"]["gross_pay"] == finalized_driver["pay"]["gross_pay"]


@pytest.mark.asyncio
async def test_pay_item_columns_frozen_and_scoped_to_active_reportable_daily(
    session_client, auth_token, paytest_branch_id, direct_db,
):
    marker = uuid4().hex[:12]
    custom_pay_item_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payitems
            (companyid, branchid, payitemcode, payitemname, category, datatype, unit, status, sortorder,
             appearsinpayrollentry, appearsinledger, appearsinreports, requiresrate, issystemstandard,
             itemscope, ratebehavior, isdefaultbranchactive)
        VALUES (1, NULL, :code, 'C1-3A Custom Item', 'Custom', 'Decimal', 'Unit', 'Active', 50,
                TRUE, TRUE, TRUE, TRUE, FALSE, 'Daily', 'PerUnit', FALSE)
        RETURNING payitemid
    """), {"code": f"C13A{marker}".upper()})).scalar_one())
    await direct_db.execute(text("""
        INSERT INTO payroll.branchpayitemconfig (companyid, branchid, payitemid, isactive, effectivefrom, createdbyuserid)
        VALUES (1, :branch_id, :pay_item_id, TRUE, '2099-01-01', 1)
    """), {"branch_id": paytest_branch_id, "pay_item_id": custom_pay_item_id})
    await direct_db.commit()
    period_id = await _insert_http_period(direct_db, paytest_branch_id, "Draft")
    report = await _report(session_client, auth_token, period_id, "drivers")
    assert report.status_code == 200, report.text
    columns = report.json()["pay_item_columns"]
    codes = {c["code"] for c in columns}
    assert "HOURS" in codes
    assert f"C13A{marker}".upper() in codes
    assert "BONUS" not in codes
    assert "ADJUSTMENT" not in codes
    assert "SYS_MIN_TOPUP" not in codes
    assert "SYS_MAX_CAP" not in codes
    assert "GUARANTEED_MINIMUM" not in codes
    assert all(c["scope"] == "Daily" for c in columns)
    assert [c["sort_order"] for c in columns] == sorted(c["sort_order"] for c in columns)
    # Mutating the live catalog after the fact must not change the frozen columns.
    await direct_db.execute(text("""
        UPDATE payroll.payitems SET payitemname = 'Mutated after freeze', status = 'Retired'
        WHERE payitemid = :pay_item_id
    """), {"pay_item_id": custom_pay_item_id})
    await direct_db.commit()
    report_after = await _report(session_client, auth_token, period_id, "drivers")
    assert report_after.status_code == 200, report_after.text
    frozen_custom = next(c for c in report_after.json()["pay_item_columns"] if c["pay_item_id"] == custom_pay_item_id)
    assert frozen_custom["label"] == "C1-3A Custom Item"


@pytest.mark.asyncio
async def test_daily_line_outside_frozen_layout_fails_closed_but_period_work_still_available(
    session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
):
    period_id, _, _, _, _ = await _seed_submitted_snapshot(
        direct_db, paytest_branch_id, paytest_driver_id, daily_pay_item_id=999999999,
    )
    for name in ("drivers", "period-pay", "mixed"):
        response = await _report(session_client, auth_token, period_id, name)
        assert response.status_code == 422, response.text
        assert "REPORT_PAY_ITEM_INTEGRITY_ERROR" in response.text
    # Period Work is operational, not financial, and must stay available even
    # though the per-item financial projection for this same period fails.
    work = await _report(session_client, auth_token, period_id, "period-work")
    assert work.status_code == 200, work.text
    assert work.json()["metadata"]["authority_kind"] == "SUBMITTED_SNAPSHOT"
    assert work.json()["pay_item_totals"] is None
    driver = next(d for d in work.json()["drivers"] if d["driver_id"] == paytest_driver_id)
    assert driver["pay"]["pay_item_amounts"] == []


@pytest.mark.asyncio
async def test_missing_frozen_layout_fails_closed_but_period_work_still_available(
    session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
):
    # A period built without going through real period creation (or
    # _insert_http_period) never gets a PayrollPeriodPayItems layout row at
    # all -- the pre-CP-2C shape. This must fail closed for the financial
    # per-item path, not silently report zero columns.
    marker = uuid4().hex[:12]
    period_id = int((await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (1, :branch_id, 'Open', :code, 'CP5C missing layout', 'Week', '2096-01-01', '2096-01-07')
        RETURNING payrollperiodid
    """), {"branch_id": paytest_branch_id, "code": f"CP5C-{marker}"})).scalar_one())
    await direct_db.execute(text("""
        INSERT INTO payroll.payrolldraftlines
            (companyid, branchid, payrollperiodid, driverid, workdate, linetype, linescope,
             quantity, sourcetype, status, needsmanagerreview, addedbyuserid)
        VALUES (1, :branch_id, :period_id, :driver_id, '2096-01-01', 'HOURS', 'Daily',
                2.0000, 'Manual', 'Active', FALSE, 1)
    """), {"branch_id": paytest_branch_id, "period_id": period_id, "driver_id": paytest_driver_id})
    await direct_db.commit()
    assert not await report_read_model._period_has_pay_item_snapshot(period_id, direct_db)
    for name in ("drivers", "period-pay", "mixed"):
        response = await _report(session_client, auth_token, period_id, name)
        assert response.status_code == 422, response.text
        assert "REPORT_PAY_ITEM_LAYOUT_UNAVAILABLE" in response.text
    # Period Work must not be coupled to the missing financial layout either.
    work = await _report(session_client, auth_token, period_id, "period-work")
    assert work.status_code == 200, work.text
    assert Decimal(work.json()["work_totals"]["HOURS"]) == Decimal("2")
    assert work.json()["pay_item_totals"] is None
