"""DD3: a permanent transfer inside a payroll period splits one guarantee, never doubles it.

The Driver's Minimum is selected by DriverID at each period start. Each Branch period
evaluates its own DriverAssignment on that assignment's frozen eligible days, so on
compatible calendars the two prorated guarantees add up to one full amount (to rounding);
an incompatible calendar still blocks (MINMAX_CALENDAR_MISMATCH, implemented by Durable
Driver Stage 2) and the proration never rescales it.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.builders.transfer_scenarios import (
    approved_transfer,
    auth,
    branch_today,
    local_boundary,
    open_period,
)
from tests.test_driver_owned_pay_rules import _calendars, _make, _preview_of
from tests.test_driver_owned_rate_schedules import _transfer, _world

pytestmark = pytest.mark.asyncio

D = Decimal


async def _bonus(db, period_id: int, branch_id: int, assignment_id: int, amount: str = "1.00") -> None:
    """A financial source of its own, so the assignment is part of the packet."""
    await db.execute(text("""
        INSERT INTO payroll.payrollbonusevents
            (companyid, branchid, payrollperiodid, driverassignmentid, amount, status,
             createdbyuserid, createdatutc, datarevision)
        VALUES (1, :branch_id, :period_id, :assignment_id, :amount, 'Active', 1, NOW(), 1)
    """), {"branch_id": branch_id, "period_id": period_id, "assignment_id": assignment_id,
           "amount": D(amount)})
    await db.commit()


async def _eligible_days(db, period_id: int, assignment_id: int) -> set[date]:
    row = (await db.execute(text("""
        SELECT eligibleworkdates FROM payroll.payrollperioddrivereligibility
        WHERE payrollperiodid = :period_id AND driverassignmentid = :assignment_id
    """), {"period_id": period_id, "assignment_id": assignment_id})).scalar_one()
    return set(row or [])


async def _scheduled(client, token, direct_db, w, cut_day: date) -> int:
    """A permanent transfer effective at the Branch-local start of ``cut_day``; returns the
    destination assignment."""
    request_id = await approved_transfer(
        client, token, w.driver_id, w.new_branch, await local_boundary(direct_db, w.new_branch, cut_day))
    completed = await client.post(
        f"/driver-transfers/{request_id}/complete", json={}, headers=auth(token))
    assert completed.status_code == 200, completed.text
    return completed.json()["destination_driver_assignment_id"]


def _next_thursday(today: date) -> date:
    return today + timedelta(days=(3 - today.weekday()) % 7 or 7)


async def test_two_branch_periods_share_one_minimum_across_a_permanent_transfer(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w)                          # both Branches weekly, Monday grid
    await _make(client, w.old_manager, w, "2026-01-01", amount="100")
    cut_day = _next_thursday(await branch_today(direct_db, w.old_branch))
    destination = await _scheduled(client, auth_token, direct_db, w, cut_day)

    start = cut_day - timedelta(days=3)                     # the Monday of the transfer week
    end = start + timedelta(days=6)
    old_period = await open_period(direct_db, w.old_branch, start, end, days=True)
    new_period = await open_period(direct_db, w.new_branch, start, end, days=True)
    await _bonus(direct_db, old_period, w.old_branch, w.source)
    await _bonus(direct_db, new_period, w.new_branch, destination)

    old_days = await _eligible_days(direct_db, old_period, w.source)
    new_days = await _eligible_days(direct_db, new_period, destination)
    assert len(old_days) == 3 and len(new_days) == 4        # Monday .. Wednesday, Thursday .. Sunday
    assert old_days.isdisjoint(new_days) and len(old_days | new_days) == 7

    old_row = await _preview_of(client, auth_token, old_period, w.source)
    new_row = await _preview_of(client, auth_token, new_period, destination)
    for row, numerator in ((old_row, 3), (new_row, 4)):
        assert row["blockers"] == [], row["blockers"]
        assert (row["minmax"]["status"], row["minmax"]["numerator"], row["minmax"]["denominator"]) == (
            "APPLIED", numerator, 7)
    assert D(old_row["minimum_adjustment"]) == D("42.86")   # 100 x 3/7
    assert D(new_row["minimum_adjustment"]) == D("57.14")   # 100 x 4/7
    # Both periods used the one Driver rule (nothing was copied), and together they pay one Minimum.
    assert old_row["minmax"]["minimum_rule_id"] == new_row["minmax"]["minimum_rule_id"]
    assert D(old_row["minimum_adjustment"]) + D(new_row["minimum_adjustment"]) == D("100.00")


async def test_a_transfer_day_shared_by_two_tenures_counts_only_for_its_owner(
    client, auth_token, direct_db,
):
    """An immediate transfer happens inside a Branch-local day, so that day is in the frozen
    EligibleWorkDates of both assignments. It counts only for the assignment that owns the
    date under rule D1 (the latest-starting tenure overlapping it: the destination), so the
    two guarantees add up to exactly one full set of scheduled days."""
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w)
    await _make(client, w.old_manager, w, "2026-01-01", amount="700")
    destination = await _transfer(client, w)

    today = await branch_today(direct_db, w.old_branch)
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    old_period = await open_period(direct_db, w.old_branch, start, end, days=True)
    new_period = await open_period(direct_db, w.new_branch, start, end, days=True)
    await _bonus(direct_db, old_period, w.old_branch, w.source)
    await _bonus(direct_db, new_period, w.new_branch, destination)

    old_days = await _eligible_days(direct_db, old_period, w.source)
    new_days = await _eligible_days(direct_db, new_period, destination)
    assert old_days & new_days == {today}                   # frozen eligibility overlaps ...
    old_row = await _preview_of(client, auth_token, old_period, w.source)
    new_row = await _preview_of(client, auth_token, new_period, destination)
    # ... but the transfer day is the destination's: Monday .. day before vs the rest.
    assert old_row["minmax"]["numerator"] == today.weekday()
    assert new_row["minmax"]["numerator"] == 7 - today.weekday()
    assert old_row["minmax"]["numerator"] + new_row["minmax"]["numerator"] == 7
    assert D(old_row["minmax"]["denominator"]) == D(new_row["minmax"]["denominator"]) == 7
    assert D(old_row["minimum_adjustment"]) + D(new_row["minimum_adjustment"]) == D(700)


async def test_an_incompatible_destination_calendar_still_blocks_with_proration_in_play(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w, new={"frequency": "Biweek"})
    await _make(client, w.old_manager, w, "2026-01-01", amount="100")
    destination = await _transfer(client, w)

    today = await branch_today(direct_db, w.new_branch)
    start = today - timedelta(days=today.weekday())
    # A weekly rule against a two-week period of the destination Branch's own calendar.
    period = await open_period(direct_db, w.new_branch, start, start + timedelta(days=13), days=True)
    await _bonus(direct_db, period, w.new_branch, destination)
    row = await _preview_of(client, auth_token, period, destination)
    assert any(b.startswith("MINMAX_CALENDAR_MISMATCH") for b in row["blockers"]), row["blockers"]
    assert row["minmax"]["status"] == "CALENDAR_MISMATCH"
    assert D(row["minimum_adjustment"]) == 0
