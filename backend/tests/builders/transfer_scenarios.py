"""Scenario builders for transfer lifecycle, termination authority and boundary races.

Each scenario owns its Branches so period and roster state never leaks between tests.
"""

from datetime import date, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def sfx() -> str:
    return uuid4().hex[:10]


async def make_branch(db, tz: str = "UTC") -> int:
    marker = uuid4().hex
    return int((await db.execute(text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault, timezoneid)
        VALUES (1, :code, :name, 'Active', FALSE, :tz)
        RETURNING branchid
    """), {"code": f"TS_{marker[:12]}", "name": f"Scenario branch {marker[:8]}", "tz": tz})).scalar_one())


async def branch_today(db, branch_id: int) -> date:
    return (await db.execute(
        text("SELECT core.fn_BranchToday(1, :branch_id)"), {"branch_id": branch_id},
    )).scalar_one()


async def local_boundary(db, branch_id: int, day: date) -> datetime:
    return (await db.execute(text("""
        SELECT core.fn_LocalDateBoundary(b.TimeZoneId, CAST(:day AS DATE))
        FROM core.Branches b WHERE b.CompanyID = 1 AND b.BranchID = :branch_id
    """), {"branch_id": branch_id, "day": day})).scalar_one()


async def db_now(db) -> datetime:
    return (await db.execute(text("SELECT clock_timestamp()"))).scalar_one()


async def make_driver(client, token: str, branch_id: int) -> dict:
    response = await client.post(
        "/workforce/employees",
        json={
            "branch_id": branch_id,
            "full_name": f"Scenario {sfx()}",
            "hire_date": "2020-01-01",
            "driver": {"driver_number": f"SC-{sfx()}", "effective_from_at": "2020-01-01T00:00:00Z"},
        },
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {
        "employee_id": body["employee_id"],
        "driver_id": body["driver"]["driver_id"],
        "assignment_id": body["current_or_pending_assignment"]["driver_assignment_id"],
    }


async def approved_transfer(client, token: str, driver_id: int, target_branch_id: int,
                            effective_at: datetime | None) -> int:
    payload = {"driver_id": driver_id, "target_branch_id": target_branch_id,
               "initiated_by": "SourceBranch", "reason": "scenario"}
    if effective_at is not None:
        payload["effective_at"] = effective_at.isoformat()
    created = await client.post("/driver-transfers", json=payload, headers=auth(token))
    assert created.status_code == 201, created.text
    request_id = created.json()["transfer_request_id"]
    decided = await client.post(
        f"/driver-transfers/{request_id}/decide-target",
        json={"decision": "Approved"}, headers=auth(token),
    )
    assert decided.status_code == 200, decided.text
    return request_id


async def scheduled_transfer(client, token: str, driver_id: int, target_branch_id: int,
                             effective_at: datetime) -> dict:
    request_id = await approved_transfer(client, token, driver_id, target_branch_id, effective_at)
    completed = await client.post(
        f"/driver-transfers/{request_id}/complete", json={}, headers=auth(token),
    )
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["status"] == "Scheduled", body
    return {
        "request_id": request_id,
        "source_id": body["source_driver_assignment_id"],
        "destination_id": body["destination_driver_assignment_id"],
        "effective_at": effective_at,
    }


async def open_period(
    db, branch_id: int, start: date, end: date, status: str = "Open", *, days: bool = False,
) -> int:
    """Insert a period, capture its roster (marker + rows) and set the requested status.

    ``days=True`` also freezes the Branch's Payroll Setup day calendar into the period (the
    Branch needs a published Setup in force), as the canonical creator does.
    """
    from app.payroll.eligibility import _create_period_driver_eligibility_rows
    from tests.builders.pay_rules import freeze_period_days

    period_id = int((await db.execute(text("""
        INSERT INTO payroll.PayrollPeriods
            (CompanyID, BranchID, Status, PeriodCode, PeriodName, PeriodType, StartDate, EndDate)
        VALUES (1, :branch_id, 'Open', :code, 'Scenario period', 'Custom', :start, :end)
        RETURNING PayrollPeriodID
    """), {"branch_id": branch_id, "code": f"SC-{sfx()}", "start": start, "end": end})).scalar_one())
    await db.commit()
    if days:
        await freeze_period_days(db, period_id)
        await db.commit()
    await _create_period_driver_eligibility_rows(
        period_id, 1, branch_id, db, snapshot_source="Generated", freeze=True,
    )
    if status != "Open":
        await db.execute(text(
            "UPDATE payroll.PayrollPeriods SET Status = :status WHERE PayrollPeriodID = :id"
        ), {"status": status, "id": period_id})
    await db.commit()
    return period_id


def days(base: date, count: int) -> date:
    return base + timedelta(days=count)
