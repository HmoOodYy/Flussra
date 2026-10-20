"""Assignment-window changes and the finalized work-date view over immutable evidence.

A final line projects an official component and carries no date of its own. The dated source
behind it is a line of the frozen snapshot, exposed through ``vw_FinalizedWorkDates``, which the
hire-date, termination and assignment-coverage guards read. A window change under a Locked period
is refused outright and moves nothing; the view must keep naming each finalized work date. The
hire-date, termination and Status DriverRate refusals are asserted next to those features
(``test_workforce_employees``, ``test_p1c_transfer_termination_cutover`` and
``test_p4b_final_ledger_integrity``).
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from tests.builders.final_evidence import discard_chain, insert_final_line, seed_snapshot_component
from tests.builders.transfer_scenarios import (
    auth,
    branch_today,
    local_boundary,
    make_branch,
    make_driver,
    open_period,
    scheduled_transfer,
)

pytestmark = pytest.mark.asyncio


async def _finalized_day(client, token, db):
    """A Driver with an Approved transfer window and a Locked period whose finalized Status
    component is dated on ``day``; returns the scenario and the seeded chain."""
    source_branch, target_branch = await make_branch(db), await make_branch(db)
    driver = await make_driver(client, token, source_branch)
    today = await branch_today(db, source_branch)
    day = today + timedelta(days=8)
    midnight = await local_boundary(db, source_branch, day)
    period_id = await open_period(db, source_branch, day, day, status="Open")
    transfer = await scheduled_transfer(
        client, token, driver["driver_id"], target_branch, midnight + timedelta(hours=6))
    await db.execute(text(
        "UPDATE payroll.payrollperiods SET status = 'Locked' WHERE payrollperiodid = :p"),
        {"p": period_id})
    await db.commit()
    chain = await seed_snapshot_component(
        db, period_id=period_id, driver_assignment_id=driver["assignment_id"], work_date=day)
    await insert_final_line(db, chain)
    return {"driver": driver, "transfer": transfer, "midnight": midnight, "day": day,
            "period_id": period_id}, chain


async def test_the_assignment_window_cannot_be_changed_under_a_period_with_finalized_work(
    client, auth_token, direct_db,
):
    scenario, chain = await _finalized_day(client, auth_token, direct_db)
    try:
        refused = await client.post(
            f"/driver-transfers/{scenario['transfer']['request_id']}/reschedule",
            json={"effective_at": (scenario["midnight"] - timedelta(hours=12)).isoformat()},
            headers=auth(auth_token))
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["code"] == "DRIVER_ASSIGNMENT_PERIOD_NOT_EDITABLE"
        window_end = (await direct_db.execute(text(
            "SELECT EffectiveToAt FROM core.DriverAssignments WHERE DriverAssignmentID = :id"
        ), {"id": scenario["transfer"]["source_id"]})).scalar_one()
        assert window_end == scenario["transfer"]["effective_at"]        # nothing moved
    finally:
        await discard_chain(direct_db, chain)


async def test_the_finalized_work_date_view_exposes_each_dated_component_source(
    client, auth_token, direct_db,
):
    scenario, chain = await _finalized_day(client, auth_token, direct_db)
    try:
        rows = (await direct_db.execute(text("""
            SELECT payrollperiodid, driverassignmentid, workdate
            FROM payroll.vw_finalizedworkdates WHERE payrollperiodid = :p
        """), {"p": scenario["period_id"]})).all()
        assert rows == [(scenario["period_id"], scenario["driver"]["assignment_id"],
                         scenario["day"])]
    finally:
        await discard_chain(direct_db, chain)
