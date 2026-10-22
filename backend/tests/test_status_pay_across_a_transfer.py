"""Status compensation follows the permanent Driver across a permanent transfer.

A Driver's Status rate is a schedule of the Company Status rate column, owned by the Driver, not
by an assignment. A permanent transfer therefore copies nothing: the days before it and the days
after it resolve the same schedule by their own WorkDate, whatever the Branch, and a rate change
takes effect by date, not by Branch.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.builders.transfer_scenarios import auth, branch_today, open_period
from tests.test_driver_owned_pay_rules import _calendars, _preview_of
from tests.test_driver_owned_rate_schedules import _author, _transfer, _world
from tests.test_settings_payroll import make_key

pytestmark = pytest.mark.asyncio
D = Decimal


async def _default_status_definition(client, token) -> tuple[int, dict]:
    columns = (await client.get("/settings/status-rate-columns", headers=auth(token))).json()
    column = next(c for c in columns if c["is_default"] and c["is_active"])
    return column["status_rate_column_id"], {
        "rate_definition_id": column["rate_definition_id"],
        "components": [{"rate_component_definition_id": column["rate_component_definition_id"]}],
    }


async def _select(db, period_id, branch_id, assignment_id, work_date, status_key_id) -> None:
    await db.execute(text("""
        INSERT INTO payroll.payrollperioddriverdayentrystate
            (companyid, branchid, payrollperiodid, workdate, driverassignmentid, statuskeyid,
             isvoided, createdbyuserid)
        VALUES (1, :branch_id, :period_id, :work_date, :assignment_id, :key_id, FALSE, 1)
    """), {"branch_id": branch_id, "period_id": period_id, "work_date": work_date,
           "assignment_id": assignment_id, "key_id": status_key_id})
    await db.commit()


def _status_lines(row: dict) -> list[dict]:
    return [line for line in row["lines"] if line["source_type"] == "StatusEntryState"]


async def test_the_same_status_schedule_pays_before_and_after_a_permanent_transfer(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w)
    column_id, definition = await _default_status_definition(client, auth_token)
    key = await make_key(
        client, auth_token, key_name=f"Paid leave {uuid4().hex[:8]}", category="Vacation",
        hours_value="8", is_off_reason=False, status_rate_column_id=column_id)
    today = await branch_today(direct_db, w.old_branch)
    await _author(client, auth_token, w, definition, "2020-01-01", today - timedelta(days=1),
                  amount="25")
    destination = await _transfer(client, w)
    await _author(client, auth_token, w, definition, today, None, amount="40")

    last_week = today - timedelta(days=today.weekday() + 7)
    this_week = today - timedelta(days=today.weekday())
    old_period = await open_period(
        direct_db, w.old_branch, last_week, last_week + timedelta(days=6), days=True)
    new_period = await open_period(
        direct_db, w.new_branch, this_week, this_week + timedelta(days=6), days=True)
    await _select(direct_db, old_period, w.old_branch, w.source, last_week, key["status_key_id"])
    await _select(direct_db, new_period, w.new_branch, destination, today, key["status_key_id"])

    old_row = await _preview_of(client, auth_token, old_period, w.source)
    new_row = await _preview_of(client, auth_token, new_period, destination)
    for row in (old_row, new_row):
        assert row["blockers"] == [], row["blockers"]
    (old_line,) = _status_lines(old_row)
    (new_line,) = _status_lines(new_row)
    # The day before the transfer resolves the schedule effective on its date, the day of the
    # transfer the next one: the rate follows the date, and neither Branch had to be configured.
    assert D(str(old_line["resolved_rate"])) == D("25")
    assert D(str(new_line["resolved_rate"])) == D("40")
    assert D(str(old_row["status_pay"])) == D("200.00")
    assert D(str(new_row["status_pay"])) == D("320.00")

    count = (await direct_db.execute(text("""
        SELECT count(*) FROM payroll.driverrateschedules
        WHERE driverid = :driver AND statusratecolumnid = :column
    """), {"driver": w.driver_id, "column": column_id})).scalar_one()
    assert count == 2                    # authored once each; the transfer copied nothing
    assert (await direct_db.execute(text("""
        SELECT count(DISTINCT driverid) FROM payroll.driverrateschedules
        WHERE driverid IN (:driver) AND statusratecolumnid = :column
    """), {"driver": w.driver_id, "column": column_id})).scalar_one() == 1


async def test_a_driver_without_a_status_rate_is_a_blocker_in_the_new_branch_too(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w)
    column_id, _ = await _default_status_definition(client, auth_token)
    key = await make_key(
        client, auth_token, key_name=f"Unrated leave {uuid4().hex[:8]}", category="Vacation",
        hours_value="8", is_off_reason=False, status_rate_column_id=column_id)
    destination = await _transfer(client, w)
    this_week = (await branch_today(direct_db, w.new_branch))
    this_week -= timedelta(days=this_week.weekday())
    new_period = await open_period(
        direct_db, w.new_branch, this_week, this_week + timedelta(days=6), days=True)
    today = await branch_today(direct_db, w.new_branch)
    await _select(direct_db, new_period, w.new_branch, destination, today, key["status_key_id"])

    row = await _preview_of(client, auth_token, new_period, destination)
    (line,) = _status_lines(row)
    assert line["calculation_status"] == "MissingRate"
    preview = await client.get(
        f"/payroll/periods/{new_period}/calculation-preview", headers=auth(auth_token))
    assert preview.json()["has_blockers"] is True
