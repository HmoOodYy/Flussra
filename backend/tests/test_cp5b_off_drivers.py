"""Focused CP-5B coverage for official Fully-Off and selected-day contracts."""
import datetime
import itertools
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncConnection

_COMPANY_ID = 1
_BASE_DATE = datetime.date(2097, 1, 1)
_COUNTER = itertools.count(1)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _clean(db: AsyncConnection, branch_id: int) -> None:
    period_ids = (await db.execute(
        _text("""
            SELECT payrollperiodid FROM payroll.payrollperiods
            WHERE branchid = :branch_id AND periodcode LIKE 'CP5B-%'
        """),
        {"branch_id": branch_id},
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
        _text("DELETE FROM payroll.payrollstatuskeys WHERE branchid = :branch_id AND statuscode LIKE 'CP5B_%'"),
        {"branch_id": branch_id},
    )
    await db.commit()


async def _insert_period(db: AsyncConnection, branch_id: int, suffix: str, status: str = "Open") -> int:
    offset = next(_COUNTER) * 14
    start = _BASE_DATE + datetime.timedelta(days=offset)
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
            "code": f"CP5B-{suffix}-{offset}",
            "name": f"CP5B {suffix}",
            "start_date": start,
            "end_date": start + datetime.timedelta(days=6),
        },
    )).mappings().one()
    await db.commit()
    return int(row["payrollperiodid"])


async def _period_dates(db: AsyncConnection, period_id: int) -> list[datetime.date]:
    row = (await db.execute(
        _text("SELECT startdate, enddate FROM payroll.payrollperiods WHERE payrollperiodid = :period_id"),
        {"period_id": period_id},
    )).mappings().one()
    return [
        row["startdate"] + datetime.timedelta(days=offset)
        for offset in range((row["enddate"] - row["startdate"]).days + 1)
    ]


async def _insert_eligibility(
    db: AsyncConnection,
    period_id: int,
    branch_id: int,
    driver_id: int,
    *,
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
            ON CONFLICT (payrollperiodid) DO NOTHING
        """),
        {"period_id": period_id, "company_id": _COMPANY_ID, "branch_id": branch_id},
    )
    await db.commit()


async def _status_key(db: AsyncConnection, branch_id: int, code: str, is_off: bool) -> int:
    return int((await db.execute(
        _text("""
            INSERT INTO payroll.payrollstatuskeys
                (companyid, branchid, statuscode, normalizedstatuscode, keyname,
                 hoursvalue, isoffreason, isactive, displayorder)
            VALUES (:company_id, :branch_id, :code, :code, :name, 0, :is_off, TRUE, 990)
            RETURNING statuskeyid
        """),
        {
            "company_id": _COMPANY_ID,
            "branch_id": branch_id,
            "code": code,
            "name": f"{code} label",
            "is_off": is_off,
        },
    )).scalar_one())


async def _set_status(
    db: AsyncConnection,
    period_id: int,
    branch_id: int,
    driver_id: int,
    work_date: datetime.date,
    status_key_id: int,
    note: str | None = None,
) -> None:
    await db.execute(
        _text("""
            INSERT INTO payroll.payrollperioddriverdayentrystate
                (companyid, branchid, payrollperiodid, workdate, driverid, statuskeyid, notetext,
                 isvoided, createdbyuserid, updatedbyuserid, createdatutc, updatedatutc)
            VALUES (:company_id, :branch_id, :period_id, :work_date, :driver_id, :status_key_id, :note,
                    FALSE, 1, 1, NOW(), NOW())
        """),
        {
            "company_id": _COMPANY_ID,
            "branch_id": branch_id,
            "period_id": period_id,
            "work_date": work_date,
            "driver_id": driver_id,
            "status_key_id": status_key_id,
            "note": note,
        },
    )
    await db.commit()


async def _set_off_for_days(
    db: AsyncConnection,
    period_id: int,
    branch_id: int,
    driver_id: int,
    days: list[datetime.date],
) -> int:
    status_key_id = await _status_key(db, branch_id, f"CP5B_OFF_{next(_COUNTER)}", True)
    for work_date in days:
        await _set_status(db, period_id, branch_id, driver_id, work_date, status_key_id)
    return status_key_id


async def _insert_daily_line(
    db: AsyncConnection,
    period_id: int,
    branch_id: int,
    driver_id: int,
    work_date: datetime.date,
    line_type: str,
    quantity: Decimal = Decimal("1"),
    source_type: str = "Manual",
) -> None:
    await db.execute(
        _text("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid, workdate, linetype, linescope,
                 quantity, sourcetype, status, needsmanagerreview, addedbyuserid)
            VALUES (:company_id, :branch_id, :period_id, :driver_id, :work_date, :line_type, 'Daily',
                    :quantity, :source_type, 'Active', FALSE, 1)
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


async def _calendar_with_configured_off_day(
    db: AsyncConnection,
    period_id: int,
    branch_id: int,
    configured_off: datetime.date | set[datetime.date],
    *,
    added: bool = False,
) -> list[datetime.date]:
    configured_off_dates = (
        configured_off if isinstance(configured_off, set) else {configured_off}
    )
    schedule_version_id = (await db.execute(
        _text("""
            SELECT scheduleversionid FROM payroll.payrollscheduleversions
            WHERE companyid = :company_id AND branchid = :branch_id
            ORDER BY scheduleversionid DESC LIMIT 1
        """),
        {"company_id": _COMPANY_ID, "branch_id": branch_id},
    )).scalar_one_or_none()
    if schedule_version_id is None:
        schedule_version_id = (await db.execute(
            _text("""
                INSERT INTO payroll.payrollscheduleversions
                    (companyid, branchid, versionnumber, payrollfrequency, anchorstartdate,
                     normaldaysoffmask, sourceaction)
                VALUES (:company_id, :branch_id, 1, 'Week', :anchor_start_date, 0, 'CP5B_TEST')
                RETURNING scheduleversionid
            """),
            {
                "company_id": _COMPANY_ID,
                "branch_id": branch_id,
                "anchor_start_date": _BASE_DATE,
            },
        )).scalar_one()
    await db.execute(
        _text("UPDATE payroll.payrollperiods SET scheduleversionid = :schedule_version_id WHERE payrollperiodid = :period_id"),
        {"schedule_version_id": schedule_version_id, "period_id": period_id},
    )
    days = await _period_dates(db, period_id)
    for work_date in days:
        is_configured_off = work_date in configured_off_dates
        await db.execute(
            _text("""
                INSERT INTO payroll.payrollperioddays
                    (payrollperiodid, companyid, branchid, scheduleversionid, workdate, dayofweek,
                     isdefaultworkday, isconfiguredoffday, isaddedworkday)
                VALUES (:period_id, :company_id, :branch_id, :schedule_version_id, :work_date,
                        :day_of_week, :is_default_work_day, :is_configured_off_day, :is_added_work_day)
            """),
            {
                "period_id": period_id,
                "company_id": _COMPANY_ID,
                "branch_id": branch_id,
                "schedule_version_id": schedule_version_id,
                "work_date": work_date,
                "day_of_week": (work_date.weekday() + 1) % 7,
                "is_default_work_day": not is_configured_off,
                "is_configured_off_day": is_configured_off,
                "is_added_work_day": is_configured_off and added,
            },
        )
    await db.commit()
    return days


async def _create_driver(
    client: httpx.AsyncClient, token: str, branch_id: int,
) -> int:
    suffix = next(_COUNTER)
    response = await client.post(
        "/core/drivers",
        json={
            "branch_id": branch_id,
            "full_name": f"CP5B Driver {suffix}",
            "preferred_name": f"CP5B {suffix}",
            "driver_code": f"CP5B-{suffix}",
            "cdl_number": f"CP5B-CDL-{suffix}",
            "email": f"cp5b-{suffix}@example.com",
        },
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["driver_id"])


async def _summary(client: httpx.AsyncClient, token: str, period_id: int) -> httpx.Response:
    return await client.get(f"/payroll/periods/{period_id}/off-drivers/summary", headers=_auth(token))


async def _selected(
    client: httpx.AsyncClient, token: str, period_id: int, work_date: datetime.date,
) -> httpx.Response:
    return await client.get(
        f"/payroll/periods/{period_id}/off-drivers",
        params={"work_date": work_date.isoformat()},
        headers=_auth(token),
    )


def _hub_branch(payload: dict, branch_id: int) -> dict:
    return next(branch for branch in payload["branches"] if branch["branch_id"] == branch_id)


@pytest.mark.asyncio
class TestOffDrivers:
    async def test_summary_counts_distinct_fully_off_drivers(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "DISTINCT")
        days = await _period_dates(direct_db, period_id)
        second_driver_id = await _create_driver(session_client, auth_token, paytest_branch_id)
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, second_driver_id)
        await _set_off_for_days(direct_db, period_id, paytest_branch_id, paytest_driver_id, days)
        await _set_off_for_days(direct_db, period_id, paytest_branch_id, second_driver_id, days)

        response = await _summary(session_client, auth_token, period_id)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total_fully_off_drivers"] == 2
        assert {row["driver_id"] for row in body["fully_off_drivers"]} == {
            paytest_driver_id, second_driver_id,
        }
        assert {row["eligible_scheduled_day_count"] for row in body["fully_off_drivers"]} == {len(days)}

    async def test_missing_or_non_off_status_disqualifies_fully_off(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "STATUS")
        days = await _period_dates(direct_db, period_id)
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        await _set_off_for_days(direct_db, period_id, paytest_branch_id, paytest_driver_id, days[:-1])
        assert (await _summary(session_client, auth_token, period_id)).json()["total_fully_off_drivers"] == 0

        non_off = await _status_key(direct_db, paytest_branch_id, "CP5B_WORK", False)
        await _set_status(direct_db, period_id, paytest_branch_id, paytest_driver_id, days[-1], non_off)
        response = await _summary(session_client, auth_token, period_id)
        assert response.status_code == 200, response.text
        assert response.json()["total_fully_off_drivers"] == 0

    async def test_normal_work_disqualifies_but_financial_only_sources_do_not(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "WORK")
        days = await _period_dates(direct_db, period_id)
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        await _set_off_for_days(direct_db, period_id, paytest_branch_id, paytest_driver_id, days)
        await _insert_daily_line(direct_db, period_id, paytest_branch_id, paytest_driver_id, days[0], "STATUS_PAYMENT", source_type="System")
        await _insert_daily_line(direct_db, period_id, paytest_branch_id, paytest_driver_id, days[1], "SYS_MIN_TOPUP", source_type="System")
        bonus = await session_client.post(
            f"/payroll/periods/{period_id}/bonuses",
            json={"driver_id": paytest_driver_id, "amount": "20.00", "reason": "CP5B test"},
            headers=_auth(auth_token),
        )
        assert bonus.status_code == 201, bonus.text
        assert (await _summary(session_client, auth_token, period_id)).json()["total_fully_off_drivers"] == 1

        await _insert_daily_line(direct_db, period_id, paytest_branch_id, paytest_driver_id, days[2], "HOURS")
        assert (await _summary(session_client, auth_token, period_id)).json()["total_fully_off_drivers"] == 0

    async def test_mid_period_eligibility_and_configured_off_days_define_denominator(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "WINDOW")
        days = await _calendar_with_configured_off_day(
            direct_db, period_id, paytest_branch_id, (await _period_dates(direct_db, period_id))[3],
        )
        await _insert_eligibility(
            direct_db,
            period_id,
            paytest_branch_id,
            paytest_driver_id,
            effective_from=days[2],
            effective_to=days[-1],
        )
        await _set_off_for_days(
            direct_db,
            period_id,
            paytest_branch_id,
            paytest_driver_id,
            [day for day in days[2:] if day != days[3]],
        )

        response = await _summary(session_client, auth_token, period_id)
        assert response.status_code == 200, response.text
        driver = response.json()["fully_off_drivers"][0]
        assert driver["eligible_scheduled_day_count"] == len(days[2:]) - 1
        assert driver["off_day_count"] == len(days[2:]) - 1

    async def test_added_configured_off_day_becomes_part_of_denominator(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "ADDED")
        days = await _period_dates(direct_db, period_id)
        await _calendar_with_configured_off_day(direct_db, period_id, paytest_branch_id, days[1], added=True)
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        await _set_off_for_days(direct_db, period_id, paytest_branch_id, paytest_driver_id, [day for day in days if day != days[1]])

        assert (await _summary(session_client, auth_token, period_id)).json()["total_fully_off_drivers"] == 0

    async def test_zero_eligible_scheduled_days_never_counts_as_fully_off(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "ZERO")
        days = await _period_dates(direct_db, period_id)
        await _calendar_with_configured_off_day(
            direct_db, period_id, paytest_branch_id, set(days),
        )
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)

        response = await _summary(session_client, auth_token, period_id)
        assert response.status_code == 200, response.text
        assert response.json()["total_fully_off_drivers"] == 0

    async def test_selected_day_is_distinct_from_period_fully_off_and_returns_note(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "SELECTED")
        days = await _period_dates(direct_db, period_id)
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        off_key = await _status_key(direct_db, paytest_branch_id, "CP5B_SELECTED", True)
        await _set_status(direct_db, period_id, paytest_branch_id, paytest_driver_id, days[0], off_key, "Returned for correction")
        await _insert_daily_line(direct_db, period_id, paytest_branch_id, paytest_driver_id, days[1], "HOURS")

        selected = await _selected(session_client, auth_token, period_id, days[0])
        assert selected.status_code == 200, selected.text
        row = selected.json()["drivers"][0]
        assert row["driver_id"] == paytest_driver_id
        assert row["status_key_id"] == off_key
        assert row["status_code"] == "CP5B_SELECTED"
        assert row["is_off_reason"] is True
        assert row["has_note"] is True
        assert row["note"] == "Returned for correction"
        assert (await _summary(session_client, auth_token, period_id)).json()["total_fully_off_drivers"] == 0

    async def test_selected_day_requires_in_period_date_and_canonical_eligible_status(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "DATE")
        days = await _period_dates(direct_db, period_id)
        await _insert_eligibility(
            direct_db, period_id, paytest_branch_id, paytest_driver_id, effective_from=days[1],
        )
        await _set_off_for_days(direct_db, period_id, paytest_branch_id, paytest_driver_id, [days[0], days[1]])

        ineligible = await _selected(session_client, auth_token, period_id, days[0])
        assert ineligible.status_code == 200, ineligible.text
        assert ineligible.json()["total_count"] == 0
        outside = await _selected(session_client, auth_token, period_id, days[-1] + datetime.timedelta(days=1))
        assert outside.status_code == 400, outside.text

    async def test_selected_day_uses_date_eligibility_not_fully_off_calendar_denominator(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "CALENDAR")
        days = await _period_dates(direct_db, period_id)
        await _calendar_with_configured_off_day(direct_db, period_id, paytest_branch_id, days[0])
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        await _set_off_for_days(direct_db, period_id, paytest_branch_id, paytest_driver_id, [days[0]])

        selected = await _selected(session_client, auth_token, period_id, days[0])
        assert selected.status_code == 200, selected.text
        assert selected.json()["total_count"] == 1
        assert (await _summary(session_client, auth_token, period_id)).json()["total_fully_off_drivers"] == 0

    async def test_selected_day_uses_frozen_status_snapshot_when_available(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "FROZEN", status="Locked")
        days = await _period_dates(direct_db, period_id)
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        status_key_id = await _status_key(direct_db, paytest_branch_id, "CP5B_FROZEN", True)
        await _set_status(direct_db, period_id, paytest_branch_id, paytest_driver_id, days[0], status_key_id)
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrollperioddriverdayentrystate
                SET finalizedatutc = NOW(), statuscodesnapshot = 'FROZEN_OFF',
                    statuslabelsnapshot = 'Frozen Off', statusisoffreasonsnapshot = TRUE
                WHERE payrollperiodid = :period_id AND driverid = :driver_id AND workdate = :work_date
            """),
            {"period_id": period_id, "driver_id": paytest_driver_id, "work_date": days[0]},
        )
        await direct_db.execute(
            _text("UPDATE payroll.payrollstatuskeys SET keyname = 'Mutated' WHERE statuskeyid = :status_key_id"),
            {"status_key_id": status_key_id},
        )
        await direct_db.commit()

        selected = await _selected(session_client, auth_token, period_id, days[0])
        assert selected.status_code == 200, selected.text
        row = selected.json()["drivers"][0]
        assert row["status_code"] == "FROZEN_OFF"
        assert row["status_label"] == "Frozen Off"

    async def test_legacy_drivers_off_endpoint_retains_driver_day_row_semantics(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "LEGACY")
        work_date = (await _period_dates(direct_db, period_id))[0]
        off_code = f"CP5B_LEGACY_{next(_COUNTER)}"
        await _status_key(direct_db, paytest_branch_id, off_code, True)
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverid, workdate, linetype, linescope,
                     quantity, sourcetype, status, needsmanagerreview, notes, addedbyuserid)
                VALUES (:company_id, :branch_id, :period_id, :driver_id, :work_date, 'DailyStatus',
                        'Daily', 1, 'Manual', 'Active', FALSE, :status_code, 1)
            """),
            {
                "company_id": _COMPANY_ID,
                "branch_id": paytest_branch_id,
                "period_id": period_id,
                "driver_id": paytest_driver_id,
                "work_date": work_date,
                "status_code": off_code,
            },
        )
        await direct_db.commit()

        response = await session_client.get(
            f"/payroll/periods/{period_id}/drivers-off", headers=_auth(auth_token),
        )
        assert response.status_code == 200, response.text
        assert response.json()["total_count"] == 1
        assert response.json()["entries"][0]["work_date"] == work_date.isoformat()
        assert response.json()["entries"][0]["status_key_code"] == off_code

    async def test_hub_uses_same_distinct_fully_off_resolver_as_summary(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "HUB")
        days = await _period_dates(direct_db, period_id)
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        await _set_off_for_days(direct_db, period_id, paytest_branch_id, paytest_driver_id, days)

        summary = await _summary(session_client, auth_token, period_id)
        hub = await session_client.get(
            "/payroll/current", params={"branch_id": paytest_branch_id}, headers=_auth(auth_token),
        )
        assert summary.status_code == 200, summary.text
        assert hub.status_code == 200, hub.text
        assert _hub_branch(hub.json(), paytest_branch_id)["slots"]["open"]["metrics"]["fully_off_drivers"] == summary.json()["total_fully_off_drivers"]
