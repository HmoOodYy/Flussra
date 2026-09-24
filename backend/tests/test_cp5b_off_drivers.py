"""Focused CP-5B coverage for official Fully-Off and selected-day contracts."""
import datetime
import itertools
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncConnection

_COMPANY_ID = 1
_BASE_DATE = datetime.date(2097, 1, 1)
_COUNTER = itertools.count(1)


@pytest_asyncio.fixture(scope="session")
async def paytest_branch_id(session_db_conn) -> int:
    """Keep CP5B workflow slots isolated from the shared PAYTEST branch."""
    row = (await session_db_conn.execute(_text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (1, :code, 'CP5B isolated', 'Active', FALSE)
        RETURNING branchid
    """), {"code": f"CP5B_{uuid4().hex}"})).scalar_one()
    return int(row)


@pytest_asyncio.fixture(scope="session")
async def paytest_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> int:
    """Create the CP5B driver on its isolated branch."""
    response = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": paytest_branch_id,
            "full_name": "CP5B Isolated Driver",
            "driver_code": f"CP5B-D-{uuid4().hex[:10]}",
        },
        headers=_auth(auth_token),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["driver_id"])


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
        # Stage B3 Unit 8C-5: some CP5B tests now drive a real
        # Submit/Approve/Finalize flow, or directly construct a calculation
        # snapshot, to exercise the immutable-evidence Off Drivers path.
        # CP-4C/CP-4D/CP-5C/Phase6/P6D (migrations 0061-0065) added several
        # ON DELETE RESTRICT children of PayrollPeriods and
        # PayrollCalculationSnapshots -- each protected by its own immutable
        # BEFORE UPDATE OR DELETE trigger, plus a BEFORE DELETE guard
        # directly on PayrollPeriods. Trigger names verified against actual
        # migration source (0035/0061/0063/0064/0065). Never DISABLE TRIGGER
        # ALL (it also suspends unrelated ON DELETE CASCADE elsewhere) --
        # disable each by exact name, delete leaf-to-root, always re-enable.
        # Mirrors the proven approach in
        # test_cp2d_canonical_entry_state.py's _clean_branch.
        guards = [
            ("payroll.payrollfinallines", "trg_final_line_immutable"),
            ("payroll.payrollcalculationsnapshotlines", "trg_PayrollCalculationSnapshotLines_Immutable"),
            ("payroll.payrollperiodauditevidencesnapshotevents", "trg_PayrollPeriodAuditEvidenceSnapshotEvents_Immutable"),
            ("payroll.payrollperiodauditevidenceevents", "trg_PayrollPeriodAuditEvidenceEvents_Immutable"),
            ("payroll.payrollperiodauditevidencecoverage", "trg_PayrollPeriodAuditEvidenceCoverage_Immutable"),
            ("payroll.payrollcalculationsnapshotusedratedefinitions", "trg_PayrollCalculationSnapshotUsedRateDefinitions_Immutable"),
            ("payroll.payrollcalculationdrivertotals", "trg_PayrollCalculationDriverTotals_Immutable"),
            ("payroll.payrollcalculationsnapshotstatusentries", "trg_PayrollCalculationSnapshotStatusEntries_Immutable"),
            ("payroll.payrollcalculationsnapshotbonusevents", "trg_PayrollCalculationSnapshotBonusEvents_Immutable"),
            ("payroll.payrollperiodworkflowactionevidence", "trg_PayrollPeriodWorkflowActionEvidence_Immutable"),
            ("payroll.payrollcalculationsnapshots", "trg_PayrollCalculationSnapshots_Immutable"),
            ("payroll.payrollperiods", "trg_PayrollPeriods_AuditEvidenceDelete"),
        ]
        for table, trigger in guards:
            await db.execute(_text(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}"))
        try:
            snapshot_subq = (
                "(SELECT payrollcalculationsnapshotid FROM payroll.payrollcalculationsnapshots "
                "WHERE payrollperiodid = ANY(:ids))"
            )
            drivertotal_subq = (
                "(SELECT payrollcalculationdrivertotalid FROM payroll.payrollcalculationdrivertotals "
                f"WHERE payrollcalculationsnapshotid IN {snapshot_subq})"
            )
            # ManagerReviewItems has no real FK to PayrollPeriods -- the link
            # is the polymorphic (EntitySchema, EntityName, EntityID)
            # convention this module's own Submit/Approve calls (and the
            # legacy/unavailable tests' direct inserts) create.
            review_items_subq = (
                "(SELECT reviewitemid FROM review.managerreviewitems "
                "WHERE entityschema = 'payroll' AND entityname = 'PayrollPeriods' "
                "AND entityid IN (SELECT payrollperiodid::text FROM payroll.payrollperiods "
                "WHERE payrollperiodid = ANY(:ids)))"
            )
            for stmt in (
                f"DELETE FROM payroll.payrollcalculationsnapshotlines "
                f"WHERE payrollcalculationdrivertotalid IN {drivertotal_subq}",

                "DELETE FROM payroll.payrollperiodauditevidencesnapshotevents "
                "WHERE payrollperiodid = ANY(:ids)",

                "DELETE FROM payroll.payrollperiodauditevidenceevents "
                "WHERE payrollperiodid = ANY(:ids)",

                "DELETE FROM payroll.payrollperiodauditevidencecoverage "
                "WHERE payrollperiodid = ANY(:ids)",

                "DELETE FROM payroll.payrollcalculationsnapshotusedratedefinitions "
                "WHERE payrollperiodid = ANY(:ids)",

                f"DELETE FROM payroll.payrollcalculationdrivertotals "
                f"WHERE payrollcalculationsnapshotid IN {snapshot_subq}",

                "DELETE FROM payroll.payrollcalculationsnapshotstatusentries "
                "WHERE payrollperiodid = ANY(:ids)",

                "DELETE FROM payroll.payrollcalculationsnapshotbonusevents "
                "WHERE payrollperiodid = ANY(:ids)",

                "DELETE FROM payroll.payrollperiodworkflowactionevidence "
                "WHERE payrollperiodid = ANY(:ids)",

                "UPDATE payroll.payrollperiods SET currentreturnreviewitemid = NULL "
                "WHERE payrollperiodid = ANY(:ids)",

                f"DELETE FROM review.managerreviewdecisions WHERE reviewitemid IN {review_items_subq}",

                f"DELETE FROM review.managerreviewitems WHERE reviewitemid IN {review_items_subq}",

                "DELETE FROM payroll.payrollcalculationsnapshots WHERE payrollperiodid = ANY(:ids)",

                "DELETE FROM payroll.payrollfinallines WHERE payrollperiodid = ANY(:ids)",

                "DELETE FROM payroll.payrollbonusevents WHERE payrollperiodid = ANY(:ids)",

                "DELETE FROM payroll.payrolldraftlines WHERE payrollperiodid = ANY(:ids)",

                "DELETE FROM payroll.payrollperioddriverdayentrystate WHERE payrollperiodid = ANY(:ids)",

                "DELETE FROM payroll.payrollperioddrivereligibility WHERE payrollperiodid = ANY(:ids)",

                "DELETE FROM payroll.payrollperiodeligibilitysnapshots WHERE payrollperiodid = ANY(:ids)",

                "DELETE FROM payroll.payrollperiods WHERE payrollperiodid = ANY(:ids)",
            ):
                await db.execute(_text(stmt), {"ids": period_ids})
        finally:
            for table, trigger in reversed(guards):
                await db.execute(_text(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}"))
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


# ---------------------------------------------------------------------------
# Stage B3 Unit 8C-5: real Submit -> Approve -> Finalize flow, and direct
# construction of legacy/unavailable snapshot scenarios (same techniques
# proven in test_cp2d_canonical_entry_state.py's E10/E25-E28).
# ---------------------------------------------------------------------------

async def _save_day_grid_status(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    work_date: datetime.date,
    status_key_code: str,
) -> None:
    resp = await client.post(
        f"/payroll/periods/{period_id}/day-grid",
        headers=_auth(token),
        json={"work_date": work_date.isoformat(),
              "rows": [{"driver_id": driver_id, "values": {}, "status_key": status_key_code}]},
    )
    assert resp.status_code == 200, resp.text


async def _submit_approve_finalize(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    work_date: datetime.date,
) -> None:
    """Drive a period Open -> InReview -> Approved -> Locked through the
    real HTTP workflow, capturing immutable Status evidence at Submit."""
    headers = _auth(token)
    lines_resp = await client.get(
        f"/payroll/periods/{period_id}/lines", headers=headers, params={"status": "Active"},
    )
    if lines_resp.status_code == 200 and len(lines_resp.json()) == 0:
        await client.post(
            f"/payroll/periods/{period_id}/lines",
            headers=headers,
            json={"driver_id": driver_id, "work_date": work_date.isoformat(),
                  "line_type": "DailyNote", "notes": "filler"},
        )
    submit = await client.patch(
        f"/payroll/periods/{period_id}/status", headers=headers, json={"status": "InReview"},
    )
    assert submit.status_code == 200, f"InReview failed: {submit.text}"
    review_resp = await client.get("/review/items", headers=headers)
    assert review_resp.status_code == 200
    item = next(
        (i for i in review_resp.json()
         if i.get("entity_name") == "PayrollPeriods"
         and i.get("entity_id") == str(period_id)
         and i.get("status") == "Pending"),
        None,
    )
    assert item is not None, f"No pending review item for period {period_id}"
    decide = await client.post(
        f"/review/items/{item['review_item_id']}/decide",
        headers=headers,
        json={"decision": "Approved"},
    )
    assert decide.status_code == 200, f"Approval failed: {decide.text}"
    finalize = await client.post(f"/payroll/periods/{period_id}/finalize", headers=headers)
    assert finalize.status_code == 200, f"Finalize failed: {finalize.text}"


async def _insert_approved_review_item(
    db: AsyncConnection, company_id: int, branch_id: int, period_id: int, snapshot_id: int,
) -> int:
    """Bind a snapshot to a period the way an Approved PeriodApproval review
    item does -- the exact selector app.payroll.service._load_approved_snapshot_packet
    (and therefore finalize_period) trusts, and the one
    status_evidence.resolve_finalized_snapshot mirrors for Locked/Archived
    Off Drivers reads (Stage B3 Unit 8C-5, same rule as Day Grid Unit 8C-3)."""
    row = (await db.execute(
        _text("""
            INSERT INTO review.managerreviewitems
                (companyid, branchid, requesttype, entityschema, entityname, entityid,
                 title, status, payrollcalculationsnapshotid)
            VALUES (:cid, :bid, 'PeriodApproval', 'payroll', 'PayrollPeriods', :pid_text,
                    'Test review item', 'Approved', :sid)
            RETURNING reviewitemid
        """),
        {"cid": company_id, "bid": branch_id, "pid_text": str(period_id), "sid": snapshot_id},
    )).mappings().first()
    await db.commit()
    return row["reviewitemid"]


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

    async def test_selected_day_uses_immutable_status_evidence_ignoring_statuskey_drift(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """Stage B3 Unit 8C-5 drift regression (real flow, not manually-set
        FinalizedAtUtc): Submit -> Approve -> Finalize captures immutable
        Status evidence; mutating the CURRENT StatusKey afterward (label AND
        IsOffReason) must not change the historical selected-day Off Drivers
        result for the Locked period."""
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "IMMUT")
        days = await _period_dates(direct_db, period_id)
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        off_code = f"CP5B_IMMUT_{next(_COUNTER)}"
        status_key_id = await _status_key(direct_db, paytest_branch_id, off_code, True)

        await _save_day_grid_status(
            session_client, auth_token, period_id, paytest_driver_id, days[0], off_code,
        )
        await _submit_approve_finalize(
            session_client, auth_token, period_id, paytest_driver_id, days[0],
        )

        # Drift the CURRENT StatusKey after Locking -- label AND IsOffReason.
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrollstatuskeys
                SET keyname = 'Mutated', isoffreason = FALSE
                WHERE statuskeyid = :status_key_id
            """),
            {"status_key_id": status_key_id},
        )
        await direct_db.commit()

        selected = await _selected(session_client, auth_token, period_id, days[0])
        assert selected.status_code == 200, selected.text
        body = selected.json()
        assert body["total_count"] == 1
        row = body["drivers"][0]
        assert row["driver_id"] == paytest_driver_id
        assert row["status_code"] == off_code
        assert row["status_label"] == f"{off_code} label", (
            "Must show the ORIGINAL captured label, not the drifted 'Mutated'"
        )
        assert row["is_off_reason"] is True, (
            "Must show the ORIGINAL captured is_off_reason, not the drifted False"
        )
        assert body.get("status_evidence") == {"state": "AVAILABLE", "reason_code": None}

    async def test_summary_fully_off_uses_immutable_status_evidence_ignoring_statuskey_drift(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """Same drift regression for the Fully-Off KPI resolver shared with
        the Current Payroll hub (resolve_fully_off_drivers)."""
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "IMMUTFULL")
        days = await _period_dates(direct_db, period_id)
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        off_code = f"CP5B_IMMUTFULL_{next(_COUNTER)}"
        status_key_id = await _status_key(direct_db, paytest_branch_id, off_code, True)

        for work_date in days:
            await _save_day_grid_status(
                session_client, auth_token, period_id, paytest_driver_id, work_date, off_code,
            )
        await _submit_approve_finalize(
            session_client, auth_token, period_id, paytest_driver_id, days[0],
        )

        await direct_db.execute(
            _text("""
                UPDATE payroll.payrollstatuskeys
                SET keyname = 'Mutated', isoffreason = FALSE
                WHERE statuskeyid = :status_key_id
            """),
            {"status_key_id": status_key_id},
        )
        await direct_db.commit()

        summary = await _summary(session_client, auth_token, period_id)
        assert summary.status_code == 200, summary.text
        body = summary.json()
        assert body["total_fully_off_drivers"] == 1
        assert body["fully_off_drivers"][0]["driver_id"] == paytest_driver_id
        assert body.get("status_evidence") == {"state": "AVAILABLE", "reason_code": None}

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

    # ------------------------------------------------------------------ #
    # Stage B3 Unit 8C-5: legacy / unavailable / empty coverage, mirroring
    # test_cp2d_canonical_entry_state.py's E26/E27/E28 for Day Grid.
    # ------------------------------------------------------------------ #

    async def test_locked_period_legacy_snapshot_status_evidence_unavailable(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """A Locked period whose authoritative snapshot predates CP-5C
        (ReportEvidenceVersion IS NULL) must report status_evidence as
        UNAVAILABLE/LEGACY_NOT_CAPTURED for both Off Drivers reads, with no
        row fabricated from mutable PayrollStatusKeys or legacy DraftLines."""
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "LEGACYSNAP", status="Locked")
        days = await _period_dates(direct_db, period_id)
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        snapshot_id = int((await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollcalculationsnapshots
                    (companyid, branchid, payrollperiodid, revisionnumber, calculationversion,
                     sourceconfighash, snapshothash, createdbyuserid, totalexpectedpay)
                VALUES (:cid, :bid, :pid, 1, 'legacy', :source_hash, :snapshot_hash, 1, 0)
                RETURNING payrollcalculationsnapshotid
            """),
            {
                "cid": _COMPANY_ID, "bid": paytest_branch_id, "pid": period_id,
                "source_hash": "0" * 64, "snapshot_hash": "1" * 64,
            },
        )).scalar_one())
        await _insert_approved_review_item(
            direct_db, _COMPANY_ID, paytest_branch_id, period_id, snapshot_id,
        )
        await direct_db.execute(
            _text("SELECT set_config('app.allow_payroll_final_line_insert', 'true', false)")
        )
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollfinallines
                    (companyid, branchid, payrollperiodid, driverid, workdate, linetype,
                     linescope, quantity, finalamount, sourcetype, approvedbyuserid,
                     approvedatutc, lockedatutc, sourcesnapshot)
                VALUES (:cid, :bid, :pid, :did, :wdate, 'HOURS', 'Daily', 1, 12.0000,
                        'DraftLine', 1, NOW(), NOW(), CAST(:snap AS JSONB))
            """),
            {
                "cid": _COMPANY_ID, "bid": paytest_branch_id, "pid": period_id, "did": paytest_driver_id,
                "wdate": days[0],
                "snap": (
                    '{"payroll_calculation_snapshot_id": %d, "revision_number": 1, '
                    '"snapshot_hash": "%s"}' % (snapshot_id, "1" * 64)
                ),
            },
        )
        await direct_db.commit()

        selected = await _selected(session_client, auth_token, period_id, days[0])
        assert selected.status_code == 200, selected.text
        sel_body = selected.json()
        assert sel_body["total_count"] == 0
        assert sel_body.get("status_evidence") == {
            "state": "UNAVAILABLE", "reason_code": "LEGACY_NOT_CAPTURED",
        }

        summary = await _summary(session_client, auth_token, period_id)
        assert summary.status_code == 200, summary.text
        sum_body = summary.json()
        assert sum_body["total_fully_off_drivers"] == 0
        assert sum_body.get("status_evidence") == {
            "state": "UNAVAILABLE", "reason_code": "LEGACY_NOT_CAPTURED",
        }

    async def test_locked_period_captured_snapshot_zero_status_rows_is_empty(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """A Locked period whose authoritative snapshot IS versioned
        (captured) but has zero PayrollCalculationSnapshotStatusEntries rows
        must report status_evidence as EMPTY -- a positive historical fact
        (nobody had an off/PTO Status), not UNAVAILABLE, and with no
        fabricated off-driver rows."""
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "EMPTYSNAP", status="Locked")
        days = await _period_dates(direct_db, period_id)
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        snapshot_id = int((await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollcalculationsnapshots
                    (companyid, branchid, payrollperiodid, revisionnumber, calculationversion,
                     sourceconfighash, snapshothash, createdbyuserid, totalexpectedpay,
                     reportevidenceversion, reportevidencehash)
                VALUES (:cid, :bid, :pid, 1, 'legacy', :source_hash, :snapshot_hash, 1, 0,
                        1, :evidence_hash)
                RETURNING payrollcalculationsnapshotid
            """),
            {
                "cid": _COMPANY_ID, "bid": paytest_branch_id, "pid": period_id,
                "source_hash": "0" * 64, "snapshot_hash": "2" * 64,
                "evidence_hash": "3" * 64,
            },
        )).scalar_one())
        await _insert_approved_review_item(
            direct_db, _COMPANY_ID, paytest_branch_id, period_id, snapshot_id,
        )
        await direct_db.execute(
            _text("SELECT set_config('app.allow_payroll_final_line_insert', 'true', false)")
        )
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollfinallines
                    (companyid, branchid, payrollperiodid, driverid, workdate, linetype,
                     linescope, quantity, finalamount, sourcetype, approvedbyuserid,
                     approvedatutc, lockedatutc, sourcesnapshot)
                VALUES (:cid, :bid, :pid, :did, :wdate, 'HOURS', 'Daily', 1, 12.0000,
                        'DraftLine', 1, NOW(), NOW(), CAST(:snap AS JSONB))
            """),
            {
                "cid": _COMPANY_ID, "bid": paytest_branch_id, "pid": period_id, "did": paytest_driver_id,
                "wdate": days[0],
                "snap": (
                    '{"payroll_calculation_snapshot_id": %d, "revision_number": 1, '
                    '"snapshot_hash": "%s"}' % (snapshot_id, "2" * 64)
                ),
            },
        )
        await direct_db.commit()

        selected = await _selected(session_client, auth_token, period_id, days[0])
        assert selected.status_code == 200, selected.text
        sel_body = selected.json()
        assert sel_body["total_count"] == 0
        assert sel_body.get("status_evidence") == {"state": "EMPTY", "reason_code": None}

        summary = await _summary(session_client, auth_token, period_id)
        assert summary.status_code == 200, summary.text
        sum_body = summary.json()
        assert sum_body["total_fully_off_drivers"] == 0
        assert sum_body.get("status_evidence") == {"state": "EMPTY", "reason_code": None}

    async def test_locked_period_missing_snapshot_provenance_is_unavailable(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """A Locked period with no Approved PeriodApproval review item bound
        to a snapshot must report status_evidence as UNAVAILABLE/
        PROVENANCE_UNAVAILABLE -- never silently falling back to another
        snapshot or to mutable current state."""
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "NOPROV", status="Locked")
        days = await _period_dates(direct_db, period_id)
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        await direct_db.execute(
            _text("SELECT set_config('app.allow_payroll_final_line_insert', 'true', false)")
        )
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollfinallines
                    (companyid, branchid, payrollperiodid, driverid, workdate, linetype,
                     linescope, quantity, finalamount, sourcetype, approvedbyuserid,
                     approvedatutc, lockedatutc)
                VALUES (:cid, :bid, :pid, :did, :wdate, 'HOURS', 'Daily', 1, 12.0000,
                        'DraftLine', 1, NOW(), NOW())
            """),
            {
                "cid": _COMPANY_ID, "bid": paytest_branch_id, "pid": period_id, "did": paytest_driver_id,
                "wdate": days[0],
            },
        )
        await direct_db.commit()

        selected = await _selected(session_client, auth_token, period_id, days[0])
        assert selected.status_code == 200, selected.text
        sel_body = selected.json()
        assert sel_body["total_count"] == 0
        assert sel_body.get("status_evidence") == {
            "state": "UNAVAILABLE", "reason_code": "PROVENANCE_UNAVAILABLE",
        }

        summary = await _summary(session_client, auth_token, period_id)
        assert summary.status_code == 200, summary.text
        sum_body = summary.json()
        assert sum_body["total_fully_off_drivers"] == 0
        assert sum_body.get("status_evidence") == {
            "state": "UNAVAILABLE", "reason_code": "PROVENANCE_UNAVAILABLE",
        }

    async def test_locked_period_zero_final_lines_still_resolves_via_review_item_binding(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """A period whose only entries are Status (no billable pay-item
        line) finalizes with zero FinalLines -- Off Drivers must still
        resolve the immutable Status evidence correctly through the
        Approved review-item binding, not treat empty FinalLines as
        unavailable provenance."""
        await _clean(direct_db, paytest_branch_id)
        period_id = await _insert_period(direct_db, paytest_branch_id, "ZEROFL")
        days = await _period_dates(direct_db, period_id)
        await _insert_eligibility(direct_db, period_id, paytest_branch_id, paytest_driver_id)
        off_code = f"CP5B_ZEROFL_{next(_COUNTER)}"
        await _status_key(direct_db, paytest_branch_id, off_code, True)

        await _save_day_grid_status(
            session_client, auth_token, period_id, paytest_driver_id, days[0], off_code,
        )
        await _submit_approve_finalize(
            session_client, auth_token, period_id, paytest_driver_id, days[0],
        )

        final_lines = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :pid"),
            {"pid": period_id},
        )).scalar_one()
        assert final_lines == 0, (
            "A Status-only day must finalize with zero FinalLines for this test to be meaningful"
        )

        selected = await _selected(session_client, auth_token, period_id, days[0])
        assert selected.status_code == 200, selected.text
        sel_body = selected.json()
        assert sel_body["total_count"] == 1
        assert sel_body["drivers"][0]["status_code"] == off_code
        assert sel_body.get("status_evidence") == {"state": "AVAILABLE", "reason_code": None}
