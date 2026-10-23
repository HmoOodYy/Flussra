"""Boundary changes honour the zone a period's roster was captured in.

A roster row keeps ``BranchTimeZoneIdSnapshot`` even after the Branch zone is edited, and
payroll authority (lifecycle eligibility, the writers' guards) judges every WorkDate with
that frozen zone. Transfer complete/cancel/reschedule and termination therefore must

* discover (and lock, and state-check) a period when its WorkDate range overlaps the
  window under ANY governing zone: each snapshot zone of the period plus the current one;
* verify coverage of daily facts and Active Bonus events with the frozen zone;
* re-capture a row under the zone it was captured with.

Scenario: a one-day period captured while the Branch is UTC; the Branch zone is then edited.
"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.payroll import bonus as bonus_module
from app.payroll import eligibility as eligibility_module
from app.payroll.eligibility import _lifecycle_eligible_assignment_ids
from tests.builders.transfer_scenarios import (
    approved_transfer,
    auth,
    branch_today,
    local_boundary,
    make_branch,
    make_driver,
    open_period,
    scheduled_transfer,
)
from tests.target_seed import seed_period_definition, seed_target_line
from tests.test_transfer_boundary_writer_concurrency import _bonus, _bonus_count, pause_after

pytestmark = pytest.mark.asyncio


async def _scenario(client, token, db, *, status="Open", zone_after="America/Los_Angeles",
                    effective_offset_hours=6):
    """Period day P captured in UTC; transfer at P + offset; Branch zone then edited."""
    source_branch, target_branch = await make_branch(db), await make_branch(db)
    driver = await make_driver(client, token, source_branch)
    today = await branch_today(db, source_branch)
    day = today + timedelta(days=8)
    midnight = await local_boundary(db, source_branch, day)  # UTC midnight of the period day
    period_id = await open_period(db, source_branch, day, day, status=status)
    transfer = await scheduled_transfer(
        client, token, driver["driver_id"], target_branch,
        midnight + timedelta(hours=effective_offset_hours))
    await db.execute(text("UPDATE core.branches SET timezoneid = :tz WHERE branchid = :id"),
                     {"tz": zone_after, "id": source_branch})
    await db.commit()
    return {"source_branch": source_branch, "target_branch": target_branch, "driver": driver,
            "day": day, "midnight": midnight, "period_id": period_id, "transfer": transfer}


async def _reschedule(client, token, request_id, instant):
    return await client.post(
        f"/driver-transfers/{request_id}/reschedule",
        json={"effective_at": instant.isoformat()}, headers=auth(token))


async def test_reschedule_waits_for_a_writer_in_a_period_whose_frozen_zone_differs_from_the_current_one(
    client, auth_token, direct_db, monkeypatch,
):
    """The reviewer's race: HEAD found no overlap in the current zone and did not wait."""
    s = await _scenario(client, auth_token, direct_db)
    gate = pause_after(monkeypatch, bonus_module, "_assert_driver_eligible_for_period_via_snapshot")
    bonus = asyncio.create_task(
        _bonus(client, auth_token, s["period_id"], s["driver"]["assignment_id"]))
    await asyncio.wait_for(gate.reached.wait(), timeout=15)

    earlier = s["midnight"] - timedelta(hours=1)  # the previous day, 23:00 UTC
    reschedule = asyncio.create_task(_reschedule(client, auth_token, s["transfer"]["request_id"], earlier))
    await asyncio.sleep(1.0)
    assert not reschedule.done(), "the reschedule must wait for the period lock the writer holds"

    gate.release.set()
    assert (await asyncio.wait_for(bonus, timeout=15)).status_code == 201
    refused = await asyncio.wait_for(reschedule, timeout=15)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "DRIVER_ASSIGNMENT_PAYROLL_FACTS_UNCOVERED"
    assert await _bonus_count(direct_db, s["period_id"], s["driver"]["assignment_id"]) == 1
    moved = (await direct_db.execute(text(
        "SELECT EffectiveToAt FROM core.DriverAssignments WHERE DriverAssignmentID = :id"
    ), {"id": s["transfer"]["source_id"]})).scalar_one()
    assert moved == s["transfer"]["effective_at"]


async def test_a_writer_that_waits_for_a_reschedule_under_a_frozen_zone_is_rejected(
    client, auth_token, direct_db, monkeypatch,
):
    s = await _scenario(client, auth_token, direct_db)
    gate = pause_after(monkeypatch, eligibility_module, "recapture_assignment_eligibility")
    reschedule = asyncio.create_task(_reschedule(
        client, auth_token, s["transfer"]["request_id"], s["midnight"] - timedelta(hours=1)))
    await asyncio.wait_for(gate.reached.wait(), timeout=15)

    bonus = asyncio.create_task(
        _bonus(client, auth_token, s["period_id"], s["driver"]["assignment_id"]))
    await asyncio.sleep(1.0)
    assert not bonus.done(), "the writer must wait for the period lock the reschedule holds"

    gate.release.set()
    assert (await asyncio.wait_for(reschedule, timeout=15)).status_code == 200
    rejected = await asyncio.wait_for(bonus, timeout=15)
    assert rejected.status_code == 422, rejected.text
    assert await _bonus_count(direct_db, s["period_id"], s["driver"]["assignment_id"]) == 0


async def test_daily_facts_are_judged_with_the_frozen_zone_not_the_current_one(
    client, auth_token, direct_db,
):
    """East of UTC the current-zone day overlaps the shortened window; the frozen UTC day does not."""
    s = await _scenario(client, auth_token, direct_db, zone_after="Pacific/Kiritimati")
    definition = await seed_period_definition(
        direct_db, period_id=s["period_id"], branch_id=s["source_branch"])
    await seed_target_line(
        direct_db, period_id=s["period_id"], branch_id=s["source_branch"],
        driver_assignment_id=s["driver"]["assignment_id"], definition=definition,
        quantity=1, work_date=s["day"])
    await direct_db.commit()

    # Window now ends the previous day 12:00Z: UTC day P is uncovered; the Kiritimati day
    # (which began the previous day 10:00Z) would still overlap it.
    refused = await _reschedule(
        client, auth_token, s["transfer"]["request_id"], s["midnight"] - timedelta(hours=12))
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "DRIVER_ASSIGNMENT_PAYROLL_FACTS_UNCOVERED"

    # Ending after the frozen day starts keeps the fact covered, whatever the current zone says.
    accepted = await _reschedule(
        client, auth_token, s["transfer"]["request_id"], s["midnight"] + timedelta(hours=3))
    assert accepted.status_code == 200, accepted.text


async def test_recapture_keeps_the_zone_the_row_was_captured_in(
    client, auth_token, direct_db,
):
    s = await _scenario(client, auth_token, direct_db)
    row = (await direct_db.execute(text("""
        SELECT BranchTimeZoneIdSnapshot, EligibleWorkDates FROM payroll.PayrollPeriodDriverEligibility
        WHERE PayrollPeriodID = :period_id AND DriverAssignmentID = :aid
    """), {"period_id": s["period_id"], "aid": s["driver"]["assignment_id"]})).mappings().one()
    assert row["branchtimezoneidsnapshot"] == "UTC" and row["eligibleworkdates"] == [s["day"]]

    moved = await _reschedule(
        client, auth_token, s["transfer"]["request_id"], s["midnight"] + timedelta(hours=12))
    assert moved.status_code == 200, moved.text
    after = (await direct_db.execute(text("""
        SELECT BranchTimeZoneIdSnapshot, EligibleWorkDates FROM payroll.PayrollPeriodDriverEligibility
        WHERE PayrollPeriodID = :period_id AND DriverAssignmentID = :aid
    """), {"period_id": s["period_id"], "aid": s["driver"]["assignment_id"]})).mappings().one()
    assert after["branchtimezoneidsnapshot"] == "UTC", "re-capture must not rewrite the captured zone"
    assert after["eligibleworkdates"] == [s["day"]]


async def test_a_period_captured_in_utc_is_discovered_by_cancel_and_reschedule_after_the_zone_changes(
    client, auth_token, direct_db,
):
    """The current (Kiritimati) day range ends before the window; the frozen UTC range overlaps it."""
    s = await _scenario(client, auth_token, direct_db, status="InReview",
                        zone_after="Pacific/Kiritimati", effective_offset_hours=18)
    reschedule = await _reschedule(
        client, auth_token, s["transfer"]["request_id"], s["midnight"] + timedelta(hours=20))
    assert reschedule.status_code == 409, reschedule.text
    assert reschedule.json()["detail"]["code"] == "DRIVER_ASSIGNMENT_PERIOD_NOT_EDITABLE"
    assert s["period_id"] in reschedule.json()["detail"]["payroll_period_ids"]

    cancel = await client.post(
        f"/driver-transfers/{s['transfer']['request_id']}/cancel", json={}, headers=auth(auth_token))
    assert cancel.status_code == 409, cancel.text
    assert cancel.json()["detail"]["code"] == "DRIVER_ASSIGNMENT_PERIOD_NOT_EDITABLE"


async def test_complete_waits_for_a_writer_in_a_period_captured_under_another_zone(
    client, auth_token, direct_db, monkeypatch,
):
    source_branch, target_branch = await make_branch(direct_db), await make_branch(direct_db)
    driver = await make_driver(client, auth_token, source_branch)
    today = await branch_today(direct_db, source_branch)
    day = today + timedelta(days=8)
    midnight = await local_boundary(direct_db, source_branch, day)
    period_id = await open_period(direct_db, source_branch, day, day)
    await direct_db.execute(text(
        "UPDATE core.branches SET timezoneid = 'Pacific/Kiritimati' WHERE branchid = :id"
    ), {"id": source_branch})
    await direct_db.commit()
    request_id = await approved_transfer(
        client, auth_token, driver["driver_id"], target_branch, midnight + timedelta(hours=18))

    gate = pause_after(monkeypatch, bonus_module, "_lock_period_for_mutation")
    bonus = asyncio.create_task(_bonus(client, auth_token, period_id, driver["assignment_id"]))
    await asyncio.wait_for(gate.reached.wait(), timeout=15)
    complete = asyncio.create_task(client.post(
        f"/driver-transfers/{request_id}/complete", json={}, headers=auth(auth_token)))
    await asyncio.sleep(1.0)
    assert not complete.done(), "completion must wait for the writer in the frozen-zone period"

    gate.release.set()
    assert (await asyncio.wait_for(bonus, timeout=15)).status_code == 201
    assert (await asyncio.wait_for(complete, timeout=15)).status_code == 200


async def test_the_shared_window_predicate_matches_lifecycle_eligibility_for_every_boundary(
    client, auth_token, direct_db,
):
    """Parity: the coverage helper's predicate and the lifecycle query judge a captured day alike."""
    from app.payroll.eligibility import frozen_day_overlap_sql
    from tests.builders.workforce_db import end_driver_assignment

    branch = await make_branch(direct_db)
    driver = await make_driver(client, auth_token, branch)
    day = (await branch_today(direct_db, branch)) + timedelta(days=8)
    s = {"midnight": await local_boundary(direct_db, branch, day), "source_branch": branch,
         "period_id": await open_period(direct_db, branch, day, day)}
    await direct_db.execute(text(
        "UPDATE core.branches SET timezoneid = 'Pacific/Kiritimati' WHERE branchid = :id"
    ), {"id": branch})
    await direct_db.commit()
    assignment_id = driver["assignment_id"]
    for offset_hours in (-14, -13, -1, 0, 1, 6, 23, 24, 25):
        await end_driver_assignment(
            direct_db, assignment_id, ended_at=s["midnight"] + timedelta(hours=offset_hours),
            end_reason="Terminated",
        )
        lifecycle = await _lifecycle_eligible_assignment_ids(
            s["period_id"], 1, s["source_branch"], None, [assignment_id], direct_db)
        shared = bool((await direct_db.execute(text(f"""
            SELECT EXISTS (
                SELECT 1
                FROM payroll.PayrollPeriodDriverEligibility pde
                JOIN core.DriverAssignments a ON a.DriverAssignmentID = pde.DriverAssignmentID
                CROSS JOIN LATERAL unnest(pde.EligibleWorkDates) AS d(WorkDate)
                WHERE pde.PayrollPeriodID = :period_id AND pde.DriverAssignmentID = :aid
                  AND {frozen_day_overlap_sql("a", "pde.BranchTimeZoneIdSnapshot", "d.WorkDate")}
            )
        """), {"period_id": s["period_id"], "aid": assignment_id})).scalar_one())
        assert (assignment_id in lifecycle) == shared, f"window ends {offset_hours}h from UTC midnight"
