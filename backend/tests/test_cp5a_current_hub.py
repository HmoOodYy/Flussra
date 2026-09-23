"""Focused CP-5A coverage for the backend-owned Current Payroll Hub."""
import datetime
import itertools
import uuid
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll import current_hub

_COMPANY_ID = 1
_BASE_DATE = datetime.date(2096, 1, 1)
_SECURITY_COUNTER = itertools.count(1)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture(scope="session")
async def paytest_branch_id(session_db_conn) -> int:
    """Use a suite-owned branch so retained evidence cannot affect PAYTEST."""
    row = (await session_db_conn.execute(
        _text("""
            INSERT INTO core.branches
                (companyid, branchcode, branchname, status, isdefault)
            VALUES (1, :code, :name, 'Active', FALSE)
            RETURNING branchid
        """),
        {"code": (code := f"CP5A_{uuid.uuid4().hex[:10]}"), "name": code},
    )).mappings().first()
    await session_db_conn.commit()
    assert row is not None
    return row["branchid"]


@pytest_asyncio.fixture(scope="session")
async def paytest_driver_id(session_client, auth_token, paytest_branch_id) -> int:
    response = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": paytest_branch_id,
            "full_name": f"CP5A Driver {uuid.uuid4().hex[:8]}",
            "driver_code": f"CP5A-{uuid.uuid4().hex[:10]}",
        },
        headers=_auth(auth_token),
    )
    assert response.status_code == 201, response.text
    return response.json()["driver_id"]


async def _clean(db: AsyncConnection, branch_id: int) -> None:
    period_ids = (await db.execute(
        _text("""
            SELECT payrollperiodid
            FROM payroll.payrollperiods
            WHERE branchid = :branch_id AND startdate >= :start_date
        """),
        {"branch_id": branch_id, "start_date": _BASE_DATE},
    )).scalars().all()
    if period_ids:
        await db.execute(
            _text("DELETE FROM payroll.payrollbonusevents WHERE payrollperiodid = ANY(:ids)"),
            {"ids": period_ids},
        )
        await db.execute(
            _text("DELETE FROM payroll.payrolldraftlines WHERE payrollperiodid = ANY(:ids)"),
            {"ids": period_ids},
        )
        await db.execute(
            _text("DELETE FROM payroll.payrollperioddriverdayentrystate WHERE payrollperiodid = ANY(:ids)"),
            {"ids": period_ids},
        )
        await db.execute(
            _text("DELETE FROM payroll.payrollperioddrivereligibility WHERE payrollperiodid = ANY(:ids)"),
            {"ids": period_ids},
        )
        await db.execute(
            _text("DELETE FROM payroll.payrollperiodeligibilitysnapshots WHERE payrollperiodid = ANY(:ids)"),
            {"ids": period_ids},
        )
        await db.execute(
            _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = ANY(:ids)"),
            {"ids": period_ids},
        )
    await db.execute(
        _text("DELETE FROM review.managerreviewitems WHERE branchid = :branch_id AND title LIKE 'CP5A %'"),
        {"branch_id": branch_id},
    )
    await db.execute(
        _text("DELETE FROM payroll.payrollstatuskeys WHERE branchid = :branch_id AND statuscode = 'CP5A_STATUS'"),
        {"branch_id": branch_id},
    )
    await db.commit()


async def _insert_period(
    db: AsyncConnection,
    branch_id: int,
    status: str,
    suffix: str,
) -> int:
    start = _BASE_DATE + datetime.timedelta(days=len(suffix) * 7)
    row = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (:company_id, :branch_id, :status, :code, :name, 'Week', :start_date, :end_date)
            RETURNING payrollperiodid
        """),
        {
            "company_id": _COMPANY_ID,
            "branch_id": branch_id,
            "status": status,
            "code": f"CP5A-{suffix}",
            "name": f"CP5A {suffix}",
            "start_date": start,
            "end_date": start + datetime.timedelta(days=6),
        },
    )).mappings().one()
    await db.commit()
    return int(row["payrollperiodid"])


async def _insert_eligibility(
    db: AsyncConnection,
    period_id: int,
    branch_id: int,
    driver_id: int,
    effective_from: datetime.date | None = None,
    effective_to: datetime.date | None = None,
) -> None:
    await db.execute(
        _text("""
            INSERT INTO payroll.payrollperioddrivereligibility
                (companyid, branchid, payrollperiodid, driverid, iseligibleforperiod,
                 eligibilityreasoncode, snapshotsource, drivereffectivefromsnapshot,
                 drivereffectivetosnapshot)
            VALUES (:company_id, :branch_id, :period_id, :driver_id, TRUE, 'Active', 'Generated',
                    :effective_from, :effective_to)
        """),
        {
            "company_id": _COMPANY_ID,
            "branch_id": branch_id,
            "period_id": period_id,
            "driver_id": driver_id,
            "effective_from": effective_from,
            "effective_to": effective_to,
        },
    )
    await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiodeligibilitysnapshots
                (payrollperiodid, companyid, branchid, snapshotsource)
            VALUES (:period_id, :company_id, :branch_id, 'Generated')
        """),
        {"period_id": period_id, "company_id": _COMPANY_ID, "branch_id": branch_id},
    )
    await db.commit()


async def _insert_returned_period(db: AsyncConnection, branch_id: int) -> int:
    review_item = (await db.execute(
        _text("""
            INSERT INTO review.managerreviewitems
                (companyid, branchid, requestedbyuserid, requesttype, entityschema,
                 entityname, entityid, title, description, priority, status,
                 finaldecisionbyuserid, finaldecisionatutc, finaldecisionreason)
            VALUES (:company_id, :branch_id, 1, 'PeriodApproval', 'payroll',
                    'PayrollPeriods', '0', 'CP5A Returned review', 'CP5A return', 'Normal',
                    'Rejected', 1, NOW(), 'CP5A test')
            RETURNING reviewitemid
        """),
        {"company_id": _COMPANY_ID, "branch_id": branch_id},
    )).mappings().one()
    start = _BASE_DATE + datetime.timedelta(days=84)
    period = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype,
                 startdate, enddate, currentreturnreviewitemid)
            VALUES (:company_id, :branch_id, 'Returned', 'CP5A-RETURNED', 'CP5A Returned',
                    'Week', :start_date, :end_date, :review_item_id)
            RETURNING payrollperiodid
        """),
        {
            "company_id": _COMPANY_ID,
            "branch_id": branch_id,
            "start_date": start,
            "end_date": start + datetime.timedelta(days=6),
            "review_item_id": review_item["reviewitemid"],
        },
    )).mappings().one()
    await db.commit()
    return int(period["payrollperiodid"])


async def _insert_daily_line(
    db: AsyncConnection,
    period_id: int,
    branch_id: int,
    driver_id: int,
    line_type: str,
    quantity: Decimal,
    source_type: str = "Manual",
    work_date: datetime.date | None = None,
) -> None:
    if work_date is None:
        work_date = (await db.execute(
            _text("SELECT startdate FROM payroll.payrollperiods WHERE payrollperiodid = :period_id"),
            {"period_id": period_id},
        )).scalar_one()
    await db.execute(
        _text("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid, workdate,
                 linetype, linescope, quantity, sourcetype, status, needsmanagerreview, addedbyuserid)
            VALUES (:company_id, :branch_id, :period_id, :driver_id, :work_date,
                    :line_type, 'Daily', :quantity, :source_type, 'Active', FALSE, 1)
        """),
        {
            "company_id": _COMPANY_ID,
            "branch_id": branch_id,
            "period_id": period_id,
            "driver_id": driver_id,
            "work_date": work_date,
            "line_type": line_type,
            "quantity": quantity,
            "source_type": source_type,
        },
    )
    await db.commit()


async def _insert_period_line(
    db: AsyncConnection,
    period_id: int,
    branch_id: int,
    driver_id: int,
) -> None:
    await db.execute(
        _text("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid, linetype, linescope,
                 quantity, calculatedamount, sourcetype, status, needsmanagerreview, addedbyuserid)
            VALUES (:company_id, :branch_id, :period_id, :driver_id, 'ADJUSTMENT', 'Period',
                    1, 25, 'Manual', 'Active', FALSE, 1)
        """),
        {
            "company_id": _COMPANY_ID,
            "branch_id": branch_id,
            "period_id": period_id,
            "driver_id": driver_id,
        },
    )
    await db.commit()


async def _insert_canonical_status(
    db: AsyncConnection,
    period_id: int,
    branch_id: int,
    driver_id: int,
) -> None:
    status_key_id = (await db.execute(
        _text("""
            SELECT statuskeyid
            FROM payroll.payrollstatuskeys
            WHERE companyid = :company_id AND branchid = :branch_id AND isactive = TRUE
            ORDER BY statuskeyid
            LIMIT 1
        """),
        {"company_id": _COMPANY_ID, "branch_id": branch_id},
    )).scalar_one_or_none()
    if status_key_id is None:
        status_key_id = (await db.execute(
            _text("""
                INSERT INTO payroll.payrollstatuskeys
                    (companyid, branchid, statuscode, normalizedstatuscode, keyname,
                     hoursvalue, isoffreason, isactive, displayorder)
                VALUES (:company_id, :branch_id, 'CP5A_STATUS', 'CP5A_STATUS', 'CP5A Status',
                        0, TRUE, TRUE, 999)
                RETURNING statuskeyid
            """),
            {"company_id": _COMPANY_ID, "branch_id": branch_id},
        )).scalar_one()
    work_date = (await db.execute(
        _text("SELECT startdate FROM payroll.payrollperiods WHERE payrollperiodid = :period_id"),
        {"period_id": period_id},
    )).scalar_one()
    await db.execute(
        _text("""
            INSERT INTO payroll.payrollperioddriverdayentrystate
                (companyid, branchid, payrollperiodid, workdate, driverid, statuskeyid,
                 isvoided, createdbyuserid, updatedbyuserid, createdatutc, updatedatutc)
            VALUES (:company_id, :branch_id, :period_id, :work_date, :driver_id, :status_key_id,
                    FALSE, 1, 1, NOW(), NOW())
        """),
        {
            "company_id": _COMPANY_ID,
            "branch_id": branch_id,
            "period_id": period_id,
            "work_date": work_date,
            "driver_id": driver_id,
            "status_key_id": status_key_id,
        },
    )
    await db.commit()


def _security_name(prefix: str) -> str:
    return f"cp5a_{prefix}_{next(_SECURITY_COUNTER):04d}"


async def _create_user(client: httpx.AsyncClient, admin_token: str, username: str) -> dict:
    response = await client.post(
        "/admin/users",
        json={
            "username": username,
            "display_name": username,
            "password": "TestPass1234!",
            "is_active": True,
            "can_login": True,
            "must_change_password": False,
        },
        headers=_auth(admin_token),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _login_as(client: httpx.AsyncClient, username: str) -> str:
    response = await client.post(
        "/auth/login",
        json={"username": username, "password": "TestPass1234!", "company_code": "DEMO"},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


async def _driver_role_id(client: httpx.AsyncClient, admin_token: str) -> int:
    response = await client.get("/admin/company-roles", headers=_auth(admin_token))
    assert response.status_code == 200, response.text
    return next(role["company_role_id"] for role in response.json() if role["role_code"] == "DRIVER")


async def _assign_role(
    client: httpx.AsyncClient,
    admin_token: str,
    user_id: int,
    role_id: int,
    scope: str,
    branch_id: int,
) -> None:
    response = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json={"company_role_id": role_id, "scope_type": scope, "branch_id": branch_id},
        headers=_auth(admin_token),
    )
    assert response.status_code == 201, response.text


async def _driver_token(
    client: httpx.AsyncClient,
    admin_token: str,
    branch_id: int,
    scope: str,
) -> str:
    username = _security_name(scope.lower())
    user = await _create_user(client, admin_token, username)
    await _assign_role(
        client,
        admin_token,
        user["user_id"],
        await _driver_role_id(client, admin_token),
        scope,
        branch_id,
    )
    return await _login_as(client, username)


async def _branch_view_token(
    client: httpx.AsyncClient,
    admin_token: str,
    branch_id: int,
) -> str:
    role = await client.post(
        "/admin/company-roles",
        json={"role_name": _security_name("branch_view")},
        headers=_auth(admin_token),
    )
    assert role.status_code == 201, role.text
    role_id = role.json()["company_role_id"]
    permissions = await client.put(
        f"/admin/company-roles/{role_id}/permissions",
        json={"permission_codes": ["payroll.view"]},
        headers=_auth(admin_token),
    )
    assert permissions.status_code == 200, permissions.text
    username = _security_name("branch_view_user")
    user = await _create_user(client, admin_token, username)
    await _assign_role(client, admin_token, user["user_id"], role_id, "SpecificBranch", branch_id)
    return await _login_as(client, username)


async def _hub(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int | None = None,
) -> httpx.Response:
    params = {"branch_id": branch_id} if branch_id is not None else {}
    return await client.get("/payroll/current", params=params, headers=_auth(token))


def _branch(payload: dict, branch_id: int) -> dict:
    return next(branch for branch in payload["branches"] if branch["branch_id"] == branch_id)


@pytest.mark.asyncio
class TestCurrentPayrollHub:
    async def test_no_active_period_returns_empty_slots(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        response = await _hub(session_client, auth_token, paytest_branch_id)
        assert response.status_code == 200, response.text
        slots = _branch(response.json(), paytest_branch_id)["slots"]
        assert slots == {"open": None, "prepared": None, "in_review": None, "returned": None}

    async def test_prepared_has_operational_metrics_but_no_financial_authority(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db, monkeypatch,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "Draft", "PREPARED")
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)

        async def live_packet_must_not_run(*args, **kwargs):
            raise AssertionError("Prepared must not invoke CP-4B live calculation")

        # Stage B4-17: current_hub's Calculation edge now calls
        # app.payroll.period_calculation._build_live_calculation_packet
        # directly — the old current_hub.service binding is no longer on
        # the production call path.
        monkeypatch.setattr(current_hub.period_calculation, "_build_live_calculation_packet", live_packet_must_not_run)
        response = await _hub(session_client, auth_token, paytest_branch_id)
        assert response.status_code == 200, response.text
        prepared = _branch(response.json(), paytest_branch_id)["slots"]["prepared"]
        assert prepared["period_id"] == period_id
        assert prepared["display_status"] == "Prepared"
        assert prepared["financials_available"] is False
        assert prepared["financial_summary"] is None
        assert prepared["metrics"] == {
            "total_eligible_drivers": 1,
            "working_drivers": 0,
            "fully_off_drivers": 0,
        }

    async def test_open_summary_matches_cp4b_live_preview(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "Open", "OPEN")
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        await _insert_daily_line(direct_db, period_id, paytest_branch_id, paytest_driver_id, "HOURS", Decimal("1"))

        preview = await session_client.get(
            f"/payroll/periods/{period_id}/calculation-preview", headers=_auth(auth_token),
        )
        assert preview.status_code == 200, preview.text
        response = await _hub(session_client, auth_token, paytest_branch_id)
        assert response.status_code == 200, response.text
        summary = _branch(response.json(), paytest_branch_id)["slots"]["open"]["financial_summary"]
        assert summary["authority_kind"] == "LIVE"
        assert Decimal(summary["total_expected_pay"]) == Decimal(preview.json()["total_expected_pay"])
        assert summary["blockers"] == preview.json()["blockers"]
        assert summary["warnings"] == preview.json()["warnings"]
        assert Decimal(summary["normal_pay"]) == sum(
            (Decimal(driver["normal_base"]) for driver in preview.json()["drivers"]),
            Decimal("0"),
        )
        assert Decimal(summary["bonus_total"]) == sum(
            (Decimal(driver["bonus_total"]) for driver in preview.json()["drivers"]),
            Decimal("0"),
        )
        assert _branch(response.json(), paytest_branch_id)["slots"]["open"]["metrics"] == {
            "total_eligible_drivers": 1,
            "working_drivers": 1,
            "fully_off_drivers": 0,
        }

    async def test_working_drivers_excludes_status_and_system_sources(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "Open", "WORKING")
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        await _insert_daily_line(direct_db, period_id, paytest_branch_id, paytest_driver_id, "DailyStatus", Decimal("1"))
        await _insert_daily_line(direct_db, period_id, paytest_branch_id, paytest_driver_id, "SYS_MIN_TOPUP", Decimal("1"), "System")

        response = await _hub(session_client, auth_token, paytest_branch_id)
        assert response.status_code == 200, response.text
        metrics = _branch(response.json(), paytest_branch_id)["slots"]["open"]["metrics"]
        assert metrics == {
            "total_eligible_drivers": 1,
            "working_drivers": 0,
            "fully_off_drivers": 0,
        }

    @pytest.mark.parametrize("line_type", ["HOURS", "MILES", "LOADS", "PALLETS"])
    async def test_normal_daily_work_sources_count_once(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db, line_type,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "Open", f"WORK-{line_type}")
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        await _insert_daily_line(direct_db, period_id, paytest_branch_id, paytest_driver_id, line_type, Decimal("1"))
        await _insert_daily_line(
            direct_db,
            period_id,
            paytest_branch_id,
            paytest_driver_id,
            line_type,
            Decimal("2"),
            work_date=_BASE_DATE + datetime.timedelta(days=1),
        )

        response = await _hub(session_client, auth_token, paytest_branch_id)
        assert response.status_code == 200, response.text
        metrics = _branch(response.json(), paytest_branch_id)["slots"]["open"]["metrics"]
        assert metrics == {
            "total_eligible_drivers": 1,
            "working_drivers": 1,
            "fully_off_drivers": 0,
        }

    async def test_inreview_does_not_request_live_financial_authority(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db, monkeypatch,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "InReview", "INREVIEW")
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)

        async def live_packet_must_not_run(*args, **kwargs):
            raise AssertionError("InReview must not invoke CP-4B live calculation")

        # Stage B4-17: current_hub's Calculation edge now calls
        # app.payroll.period_calculation._build_live_calculation_packet
        # directly — the old current_hub.service binding is no longer on
        # the production call path.
        monkeypatch.setattr(current_hub.period_calculation, "_build_live_calculation_packet", live_packet_must_not_run)
        response = await _hub(session_client, auth_token, paytest_branch_id)
        assert response.status_code == 200, response.text
        in_review = _branch(response.json(), paytest_branch_id)["slots"]["in_review"]
        assert in_review["period_id"] == period_id
        assert in_review["financials_available"] is False
        assert in_review["financial_summary"] is None

    async def test_returned_uses_current_live_packet_for_resubmission_work(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_returned_period(direct_db, paytest_branch_id)
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        await _insert_daily_line(direct_db, period_id, paytest_branch_id, paytest_driver_id, "MILES", Decimal("2"))

        preview = await session_client.get(
            f"/payroll/periods/{period_id}/calculation-preview", headers=_auth(auth_token),
        )
        response = await _hub(session_client, auth_token, paytest_branch_id)
        assert preview.status_code == 200, preview.text
        assert response.status_code == 200, response.text
        returned = _branch(response.json(), paytest_branch_id)["slots"]["returned"]
        assert returned["period_id"] == period_id
        assert returned["financials_available"] is True
        assert Decimal(returned["financial_summary"]["total_expected_pay"]) == Decimal(
            preview.json()["total_expected_pay"]
        )
        assert returned["metrics"]["working_drivers"] == 1

    async def test_branch_filter_keeps_current_workflow_compatible(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        hub = await _hub(session_client, auth_token, paytest_branch_id)
        workflow = await session_client.get(
            "/payroll/current-workflow",
            params={"branch_id": paytest_branch_id},
            headers=_auth(auth_token),
        )
        assert hub.status_code == 200, hub.text
        assert workflow.status_code == 200, workflow.text
        assert hub.json()["requested_branch_id"] == paytest_branch_id
        assert workflow.json()["requested_branch_id"] == paytest_branch_id
        assert [b["branch_id"] for b in hub.json()["branches"]] == [paytest_branch_id]

    async def test_out_of_period_daily_work_never_counts_as_working(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "Open", "OUTSIDE")
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        period_start = (await direct_db.execute(
            _text("SELECT startdate FROM payroll.payrollperiods WHERE payrollperiodid = :period_id"),
            {"period_id": period_id},
        )).scalar_one()
        await _insert_daily_line(
            direct_db,
            period_id,
            paytest_branch_id,
            paytest_driver_id,
            "HOURS",
            Decimal("1"),
            work_date=period_start - datetime.timedelta(days=1),
        )

        response = await _hub(session_client, auth_token, paytest_branch_id)
        assert response.status_code == 200, response.text
        assert _branch(response.json(), paytest_branch_id)["slots"]["open"]["metrics"] == {
            "total_eligible_drivers": 1,
            "working_drivers": 0,
            "fully_off_drivers": 0,
        }

    async def test_snapshot_driver_date_window_gates_working_metric(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "Open", "WINDOW")
        period_start = (await direct_db.execute(
            _text("SELECT startdate FROM payroll.payrollperiods WHERE payrollperiodid = :period_id"),
            {"period_id": period_id},
        )).scalar_one()
        valid_date = period_start + datetime.timedelta(days=2)
        await _insert_eligibility(
            direct_db,
            period_id,
            paytest_branch_id,
            paytest_driver_id,
            effective_from=valid_date,
            effective_to=period_start + datetime.timedelta(days=3),
        )
        await _insert_daily_line(
            direct_db,
            period_id,
            paytest_branch_id,
            paytest_driver_id,
            "HOURS",
            Decimal("1"),
            work_date=period_start,
        )

        outside = await _hub(session_client, auth_token, paytest_branch_id)
        assert outside.status_code == 200, outside.text
        assert _branch(outside.json(), paytest_branch_id)["slots"]["open"]["metrics"]["working_drivers"] == 0

        await _insert_daily_line(
            direct_db,
            period_id,
            paytest_branch_id,
            paytest_driver_id,
            "HOURS",
            Decimal("1"),
            work_date=valid_date,
        )
        inside = await _hub(session_client, auth_token, paytest_branch_id)
        assert inside.status_code == 200, inside.text
        assert _branch(inside.json(), paytest_branch_id)["slots"]["open"]["metrics"]["working_drivers"] == 1

    async def test_canonical_status_only_does_not_count_as_working(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "Open", "STATUS")
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        await _insert_canonical_status(direct_db, period_id, paytest_branch_id, paytest_driver_id)

        response = await _hub(session_client, auth_token, paytest_branch_id)
        assert response.status_code == 200, response.text
        assert _branch(response.json(), paytest_branch_id)["slots"]["open"]["metrics"] == {
            "total_eligible_drivers": 1,
            "working_drivers": 0,
            "fully_off_drivers": 0,
        }

    async def test_bonus_and_period_financial_only_do_not_count_as_working(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "Open", "FINANCIAL")
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        bonus = await session_client.post(
            f"/payroll/periods/{period_id}/bonuses",
            json={"driver_id": paytest_driver_id, "amount": "25.00", "reason": "CP-5A test"},
            headers=_auth(auth_token),
        )
        assert bonus.status_code == 201, bonus.text
        await _insert_period_line(direct_db, period_id, paytest_branch_id, paytest_driver_id)

        response = await _hub(session_client, auth_token, paytest_branch_id)
        assert response.status_code == 200, response.text
        assert _branch(response.json(), paytest_branch_id)["slots"]["open"]["metrics"] == {
            "total_eligible_drivers": 1,
            "working_drivers": 0,
            "fully_off_drivers": 0,
        }

    async def test_driver_and_oda_users_cannot_read_hub(
        self, session_client, auth_token, paytest_branch_id,
    ):
        for scope in ("SpecificBranch", "OwnDriverDataOnly"):
            token = await _driver_token(session_client, auth_token, paytest_branch_id, scope)
            response = await _hub(session_client, token, paytest_branch_id)
            assert response.status_code == 403, response.text

    async def test_inaccessible_and_cross_company_branches_never_leak_hub_data(
        self, session_client, auth_token, paytest_branch_id, hq_branch_id, direct_db,
    ):
        branch_token = await _branch_view_token(session_client, auth_token, paytest_branch_id)
        inaccessible = await _hub(session_client, branch_token, hq_branch_id)
        assert inaccessible.status_code == 403, inaccessible.text

        foreign_company_id = (await direct_db.execute(
            _text("""
                INSERT INTO core.companies
                    (companycode, companyname, legalname, status, issuspended, timezonename)
                VALUES ('CP5A_XCO', 'CP5A Cross Company', 'CP5A Cross Company', 'Active', FALSE, 'UTC')
                RETURNING companyid
            """),
        )).scalar_one()
        foreign_branch_id = (await direct_db.execute(
            _text("""
                INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
                VALUES (:company_id, 'CP5A_XCO', 'CP5A Cross Company', 'Active', TRUE)
                RETURNING branchid
            """),
            {"company_id": foreign_company_id},
        )).scalar_one()
        await direct_db.commit()
        try:
            foreign = await _hub(session_client, auth_token, foreign_branch_id)
            assert foreign.status_code in (200, 403), foreign.text
            if foreign.status_code == 200:
                assert foreign.json()["branches"] == []
        finally:
            await direct_db.execute(
                _text("DELETE FROM core.branches WHERE branchid = :branch_id"),
                {"branch_id": foreign_branch_id},
            )
            await direct_db.execute(
                _text("DELETE FROM core.companies WHERE companyid = :company_id"),
                {"company_id": foreign_company_id},
            )
            await direct_db.commit()
