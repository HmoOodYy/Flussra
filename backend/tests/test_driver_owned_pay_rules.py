"""Driver-owned pay rules (Min/Max) through the real application.

A pay rule belongs to the permanent Driver and records the home pay calendar captured from
the Payroll Setup of the Branch of its context tenure. These tests build real Drivers with
Workforce, move them with permanent and Scheduled transfers, and drive the pay-rule API as
company-wide admins and Branch-scoped managers. Calendar compatibility is exercised through
the live calculation preview of periods in the Branches the Driver works for.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.builders.pay_rules import DEFAULT_ANCHOR, ensure_branch_pay_calendar
from tests.builders.transfer_scenarios import (
    approved_transfer,
    auth,
    branch_today,
    db_now,
    make_driver,
    open_period,
    scheduled_transfer,
)
from tests.target_seed import seed_approved_rate, seed_period_definition, seed_target_line
from tests.test_driver_owned_rate_schedules import World, _transfer, _world
from tests.test_ended_assignment_write_guard import _expect

pytestmark = pytest.mark.asyncio

RULES = "/payroll/driver-pay-rules"


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

async def _calendars(db, w: World, *, old=None, new=None, other=None) -> dict:
    """Give the world's Branches published Payroll Setups. Each argument is the keyword
    arguments of ``ensure_branch_pay_calendar`` (default weekly, Monday anchor)."""
    out = {}
    for name, branch_id, kwargs in (("old", w.old_branch, old), ("new", w.new_branch, new),
                                    ("other", w.other_branch, other)):
        out[name] = await ensure_branch_pay_calendar(db, 1, branch_id, **(kwargs or {}))
    return out


async def _create(client, token, w: World, start, end=None, rule_type="MinimumPay", amount="100", **extra):
    return await client.post(RULES, json={
        "driver_id": w.driver_id, "rule_type": rule_type, "amount": amount,
        "effective_from": str(start), "effective_to": None if end is None else str(end),
        **extra}, headers=auth(token))


async def _make(client, token, w: World, start, end=None, rule_type="MinimumPay", amount="100") -> dict:
    response = await _expect(await _create(client, token, w, start, end, rule_type, amount), 201)
    return response.json()


def _code(response) -> str:
    return response.json()["detail"]["code"]


async def _rules(client, token, driver_id, **params):
    return await client.get(f"/payroll/drivers/{driver_id}/pay-rules", params=params, headers=auth(token))


def _next_monday(after: date) -> date:
    return after + timedelta(days=(7 - after.weekday()) % 7 or 7)


async def _preview_row(client, db, token, w: World, *, branch_id, assignment_id, start, end,
                       normal="50", rate_from="2026-01-01") -> dict:
    """The Driver's row of the live calculation preview for a period of ``branch_id``."""
    period_id = await open_period(db, branch_id, start, end)
    definition = await seed_period_definition(db, period_id=period_id, branch_id=branch_id)
    await seed_approved_rate(
        db, definition=definition, driver_assignment_id=assignment_id, amount=normal,
        effective_from=rate_from)
    await seed_target_line(
        db, period_id=period_id, branch_id=branch_id, driver_assignment_id=assignment_id,
        definition=definition, quantity=1, work_date=start)
    return await _preview_of(client, token, period_id, assignment_id)


async def _preview_of(client, token, period_id, assignment_id) -> dict:
    response = await client.get(f"/payroll/periods/{period_id}/calculation-preview", headers=auth(token))
    assert response.status_code == 200, response.text
    return next(d for d in response.json()["drivers"] if d["driver_assignment_id"] == assignment_id)


# ---------------------------------------------------------------------------
# Calendar capture (E3): server-side, from the context Branch's Payroll Setup
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs, frequency, interval", [
    ({}, "Week", None),
    ({"frequency": "Biweek"}, "Biweek", None),
    ({"frequency": "Month", "anchor": date(2000, 1, 1)}, "Month", None),
    ({"frequency": "Custom", "interval": 10}, "Custom", 10),
], ids=["week", "biweek", "month", "custom"])
async def test_the_calendar_is_captured_from_the_context_branchs_setup(
    client, auth_token, direct_db, kwargs, frequency, interval,
):
    w = await _world(client, auth_token, direct_db)
    setups = await _calendars(direct_db, w, old=kwargs)
    rule = await _make(client, w.old_manager, w, "2026-02-01")
    assert rule["calendar_frequency"] == frequency
    assert rule["calendar_custom_interval_days"] == interval
    assert rule["calendar_anchor_start_date"] == str(kwargs.get("anchor", DEFAULT_ANCHOR))
    assert rule["calendar_source_branch_id"] == w.old_branch
    assert rule["calendar_source_payroll_setup_version_id"] == setups["old"]["version_id"]
    assert rule["driver_id"] == w.driver_id
    assert "driver_assignment_id" not in rule and "branch_id" not in rule


async def test_no_payroll_setup_in_the_context_branch_is_a_clear_422(client, auth_token, direct_db):
    w = await _world(client, auth_token, direct_db)  # the old Branch has no Payroll Setup
    refused = await _create(client, auth_token, w, "2026-02-01")
    assert refused.status_code == 422, refused.text
    assert _code(refused) == "PAY_RULE_CALENDAR_UNAVAILABLE"
    assert (await direct_db.execute(text(
        "SELECT count(*) FROM payroll.driverpayrules WHERE driverid = :d"), {"d": w.driver_id})
    ).scalar_one() == 0
    # A setup that only starts later does not govern the earlier start date either.
    await ensure_branch_pay_calendar(direct_db, 1, w.old_branch, from_date=date(2027, 1, 4),
                                     anchor=date(2027, 1, 4))
    assert _code(await _create(client, auth_token, w, "2026-02-01")) == "PAY_RULE_CALENDAR_UNAVAILABLE"
    assert (await _create(client, auth_token, w, "2027-02-01")).status_code == 201


async def test_the_calendar_comes_from_the_tenure_of_the_start_date_not_the_current_branch(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w, old={"frequency": "Week"}, new={"frequency": "Biweek"})
    await _transfer(client, w)
    today = await branch_today(direct_db, w.new_branch)
    # The transfer day belongs to the destination: its biweekly calendar is captured.
    rule = await _make(client, w.new_manager, w, today)
    assert rule["calendar_frequency"] == "Biweek"
    assert rule["calendar_source_branch_id"] == w.new_branch


@pytest.mark.parametrize("extra", [
    {"calendar_frequency": "Week"},
    {"calendar_anchor_start_date": "2000-01-03"},
    {"calendar_custom_interval_days": 7},
    {"calendar_source_branch_id": 1},
    {"calendar_source_payroll_setup_version_id": 1},
    {"branch_id": 1},
    {"driver_assignment_id": 1},
])
async def test_no_client_supplied_calendar_or_ownership_field_is_accepted(
    client, auth_token, direct_db, extra,
):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w)
    refused = await _create(client, auth_token, w, "2026-02-01", **extra)
    assert refused.status_code == 422, refused.text
    assert (await _create(client, auth_token, w, "2026-02-01")).status_code == 201


# ---------------------------------------------------------------------------
# A permanent transfer continues the rule; compatible calendars select it everywhere
# ---------------------------------------------------------------------------

async def test_a_rule_follows_a_permanent_transfer_and_is_selected_for_both_tenures_periods(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w)  # both Branches weekly, Monday grid
    rule = await _make(client, w.old_manager, w, "2026-01-01", amount="300")

    old_row = await _preview_row(
        client, direct_db, auth_token, w, branch_id=w.old_branch, assignment_id=w.source,
        start=date(2026, 3, 2), end=date(2026, 3, 8))
    destination = await _transfer(client, w)
    today = await branch_today(direct_db, w.new_branch)
    start = _next_monday(today + timedelta(days=1))
    new_row = await _preview_row(
        client, direct_db, auth_token, w, branch_id=w.new_branch, assignment_id=destination,
        start=start, end=start + timedelta(days=6), rate_from=str(today))

    for row in (old_row, new_row):
        assert row["blockers"] == [], row
        assert Decimal(row["minimum_adjustment"]) == Decimal("250")  # 300 - 50 of normal pay
        sys_lines = [line for line in row["lines"] if line["line_type"] == "SYS_MIN_TOPUP"]
        assert [line["source_id"] for line in sys_lines] == [str(rule["driver_pay_rule_id"])]
    # Nothing was created, moved or copied by the transfer.
    assert (await direct_db.execute(text(
        "SELECT count(*) FROM payroll.driverpayrules WHERE driverid = :d"), {"d": w.driver_id})
    ).scalar_one() == 1


# ---------------------------------------------------------------------------
# Authority and visibility (D1, D2, D3); a rule has no Pending state (D4 is schedules only)
# ---------------------------------------------------------------------------

async def test_the_transfer_day_context_is_the_destination_and_ended_tenures_are_refused(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w)
    await _transfer(client, w)
    today = await branch_today(direct_db, w.new_branch)

    assert (await _create(client, w.old_manager, w, today)).status_code == 403      # D1
    assert (await _create(client, w.new_manager, w, today)).status_code == 201
    for token, expected in ((auth_token, 409), (w.old_manager, 409), (w.new_manager, 403)):  # D2
        response = await _create(client, token, w, "2026-03-01", rule_type="MaximumPay")
        assert response.status_code == expected, (token, response.text)
    historical = await _create(client, auth_token, w, "2026-03-01", rule_type="MaximumPay")
    assert _code(historical) == "DRIVER_COMPENSATION_HISTORICAL"


async def test_backdating_inside_the_current_tenure_is_allowed(client, auth_token, direct_db):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w)
    assert (await _create(client, w.old_manager, w, "2026-01-05")).status_code == 201


async def test_a_scheduled_transfers_destination_has_authority_from_its_effective_day(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w)
    await scheduled_transfer(client, auth_token, w.driver_id, w.new_branch,
                             await db_now(direct_db) + timedelta(days=3))
    today = await branch_today(direct_db, w.old_branch)
    transfer_day = today + timedelta(days=3)
    assert (await _create(client, w.new_manager, w, transfer_day)).status_code == 201
    assert (await _create(client, w.old_manager, w, transfer_day, rule_type="MaximumPay")).status_code == 403
    assert (await _create(client, w.new_manager, w, today, rule_type="MaximumPay")).status_code == 403
    assert (await _create(client, w.old_manager, w, today, rule_type="MaximumPay")).status_code == 201


async def test_a_transfer_that_is_not_yet_scheduled_grants_the_target_branch_nothing(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w)
    await approved_transfer(client, auth_token, w.driver_id, w.new_branch,
                            await db_now(direct_db) + timedelta(days=3))  # Approved only
    transfer_day = await branch_today(direct_db, w.new_branch) + timedelta(days=3)
    assert (await _create(client, w.new_manager, w, transfer_day)).status_code == 403
    assert (await _create(client, w.old_manager, w, transfer_day)).status_code == 201


async def test_visibility_follows_the_drivers_tenures(client, auth_token, direct_db):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w)
    in_old = await _make(client, w.old_manager, w, "2026-01-01", "2026-06-30")
    await _transfer(client, w)
    tomorrow = await branch_today(direct_db, w.new_branch) + timedelta(days=1)
    in_new = await _make(client, w.new_manager, w, tomorrow, rule_type="MaximumPay", amount="900")

    def ids(response):
        assert response.status_code == 200, response.text
        return {row["driver_pay_rule_id"] for row in response.json()}

    # The past Branch sees only rules overlapping its tenure, read-only.
    assert ids(await _rules(client, w.old_manager, w.driver_id)) == {in_old["driver_pay_rule_id"]}
    assert (await client.get(f"{RULES}/{in_old['driver_pay_rule_id']}", headers=auth(w.old_manager))
            ).status_code == 200
    assert (await client.get(f"{RULES}/{in_new['driver_pay_rule_id']}", headers=auth(w.old_manager))
            ).status_code == 404
    refused = await client.post(
        f"{RULES}/{in_old['driver_pay_rule_id']}/void", headers=auth(w.old_manager))
    assert refused.status_code == 409 and _code(refused) == "DRIVER_COMPENSATION_HISTORICAL"
    # The current authority Branch and company-wide readers see everything.
    both = {in_old["driver_pay_rule_id"], in_new["driver_pay_rule_id"]}
    assert ids(await _rules(client, w.new_manager, w.driver_id)) == both
    assert ids(await _rules(client, auth_token, w.driver_id)) == both
    # An unrelated Branch sees nothing.
    assert (await _rules(client, w.other_manager, w.driver_id)).status_code == 403
    assert (await client.get(f"{RULES}/{in_old['driver_pay_rule_id']}", headers=auth(w.other_manager))
            ).status_code == 403


# ---------------------------------------------------------------------------
# The canonical finalized guard, PeriodStart mode
# ---------------------------------------------------------------------------

async def test_a_finalized_period_start_of_any_tenure_inside_the_footprint_blocks_create_end_and_void(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w)
    rule = await _make(client, w.old_manager, w, "2026-04-01")
    period = await open_period(
        direct_db, w.old_branch, date(2026, 5, 4), date(2026, 5, 10), "Locked")
    rule_url = f"{RULES}/{rule['driver_pay_rule_id']}"

    # end: the footprint [newTo + 1, oldTo]. Keeping the finalized start covered is allowed.
    blocked = await client.post(f"{rule_url}/end", json={"effective_to": "2026-04-30"},
                                headers=auth(w.old_manager))
    assert blocked.status_code == 422, blocked.text
    assert _code(blocked) == "DRIVER_COMPENSATION_IN_FINALIZED_PERIOD"
    assert blocked.json()["detail"]["payroll_period_ids"] == [period]
    kept = await client.post(f"{rule_url}/end", json={"effective_to": "2026-06-30"},
                             headers=auth(w.old_manager))
    assert kept.status_code == 200, kept.text

    # void: the rule's whole window contains the finalized start.
    voided = await client.post(f"{rule_url}/void", headers=auth(w.old_manager))
    assert voided.status_code == 422 and _code(voided) == "DRIVER_COMPENSATION_IN_FINALIZED_PERIOD"
    # create: a new rule whose footprint contains the start.
    created = await _create(client, w.old_manager, w, "2026-05-01", "2026-05-31", rule_type="MaximumPay")
    assert created.status_code == 422 and _code(created) == "DRIVER_COMPENSATION_IN_FINALIZED_PERIOD"
    # ... while one that does not is fine, and the period is Archived-equivalent below.
    assert (await _create(client, w.old_manager, w, "2026-05-05", "2026-05-31", rule_type="MaximumPay")
            ).status_code == 201


async def test_a_finalized_period_in_another_branch_of_the_driver_blocks_across_branches(
    client, auth_token, direct_db,
):
    """A Scheduled destination already has a Locked period rostering it; the source Branch's
    manager cannot create a rule whose footprint reaches that period's start."""
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w)
    await scheduled_transfer(client, auth_token, w.driver_id, w.new_branch,
                             await db_now(direct_db) + timedelta(days=3))
    today = await branch_today(direct_db, w.old_branch)
    start = _next_monday(today + timedelta(days=5))
    period = await open_period(direct_db, w.new_branch, start, start + timedelta(days=6), "Locked")
    refused = await _create(client, w.old_manager, w, today)
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["payroll_period_ids"] == [period]
    # A rule that ends before that period starts is not touched by it.
    assert (await _create(client, w.old_manager, w, today, today + timedelta(days=2))
            ).status_code == 201


async def test_an_unrelated_drivers_finalized_period_does_not_block(client, auth_token, direct_db):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w)
    other = await make_driver(client, auth_token, w.other_branch)  # noqa: F841 - rostered
    today = await branch_today(direct_db, w.other_branch)
    start = _next_monday(today)
    await open_period(direct_db, w.other_branch, start, start + timedelta(days=6), "Locked")
    assert (await _create(client, w.old_manager, w, today)).status_code == 201


# ---------------------------------------------------------------------------
# MINMAX_CALENDAR_MISMATCH (E4) and reconciliation
# ---------------------------------------------------------------------------

async def _transferred(client, auth_token, db, *, new=None, rule_amount="400"):
    """A Driver with a weekly (Monday) rule, transferred into a Branch with the given calendar."""
    w = await _world(client, auth_token, db)
    await _calendars(db, w, old={}, new=new)
    rule = await _make(client, w.old_manager, w, "2026-01-01", amount=rule_amount)
    destination = await _transfer(client, w)
    today = await branch_today(db, w.new_branch)
    return w, rule, destination, today


@pytest.mark.parametrize("new_calendar, start_offset, length", [
    ({"frequency": "Biweek"}, 7, 14),                         # Week rule, Biweek period
    ({"frequency": "Week", "anchor": date(2000, 1, 4)}, 7, 7),  # same frequency, misaligned anchor
    ({"frequency": "Month", "anchor": date(2000, 1, 1)}, 0, 0),  # Month period against a Week rule
], ids=["different-frequency", "misaligned-anchor", "month-vs-week"])
async def test_an_incompatible_home_calendar_is_a_payroll_blocker(
    client, auth_token, direct_db, new_calendar, start_offset, length,
):
    w, rule, destination, today = await _transferred(client, auth_token, direct_db, new=new_calendar)
    if new_calendar["frequency"] == "Month":
        start = _first_of_next_month(today)
        end = _first_of_next_month(start) - timedelta(days=1)
    elif new_calendar.get("anchor") == date(2000, 1, 4):
        start = _next_monday(today) + timedelta(days=1)          # a Tuesday: the new grid's start
        end = start + timedelta(days=6)
    else:
        start = _next_monday(today + timedelta(days=1))
        end = start + timedelta(days=13)
    row = await _preview_row(
        client, direct_db, auth_token, w, branch_id=w.new_branch, assignment_id=destination,
        start=start, end=end, rate_from=str(today))
    assert any(b.startswith("MINMAX_CALENDAR_MISMATCH") for b in row["blockers"]), row["blockers"]
    # Never rescaled, never silently dropped: no adjustment line and no duplicate.
    assert Decimal(row["minimum_adjustment"]) == 0 and Decimal(row["maximum_adjustment"]) == 0
    assert [line for line in row["lines"] if line["line_type"] in ("SYS_MIN_TOPUP", "SYS_MAX_CAP")] == []


@pytest.mark.parametrize("new_calendar", [
    {"frequency": "Week", "anchor": date(2000, 1, 10)},   # another anchor generating the same grid
    {"frequency": "Week", "mask": 3},                      # a days-off mask only (D7)
], ids=["same-grid-other-anchor", "days-off-mask-only"])
async def test_a_compatible_calendar_is_not_a_blocker(client, auth_token, direct_db, new_calendar):
    w, rule, destination, today = await _transferred(client, auth_token, direct_db, new=new_calendar)
    start = _next_monday(today + timedelta(days=1))
    row = await _preview_row(
        client, direct_db, auth_token, w, branch_id=w.new_branch, assignment_id=destination,
        start=start, end=start + timedelta(days=6), rate_from=str(today))
    assert row["blockers"] == [], row["blockers"]
    assert Decimal(row["minimum_adjustment"]) == Decimal("350")


async def test_reconciliation_is_ending_the_old_rule_and_creating_a_new_one(
    client, auth_token, direct_db,
):
    w, rule, destination, today = await _transferred(
        client, auth_token, direct_db, new={"frequency": "Biweek"})
    start = _next_monday(today + timedelta(days=1))
    if (start - DEFAULT_ANCHOR).days % 14:
        start += timedelta(days=7)  # a period start on the biweekly grid
    period_id = await open_period(direct_db, w.new_branch, start, start + timedelta(days=13))
    definition = await seed_period_definition(direct_db, period_id=period_id, branch_id=w.new_branch)
    await seed_approved_rate(
        direct_db, definition=definition, driver_assignment_id=destination, amount="50",
        effective_from=str(today))
    await seed_target_line(
        direct_db, period_id=period_id, branch_id=w.new_branch, driver_assignment_id=destination,
        definition=definition, quantity=1, work_date=start)
    blocked = await _preview_of(client, auth_token, period_id, destination)
    assert any(b.startswith("MINMAX_CALENDAR_MISMATCH") for b in blocked["blockers"])
    # The rule itself says where reconciliation is needed.
    listed = (await _rules(client, auth_token, w.driver_id)).json()
    assert listed[0]["calendar_reconciliation_needed_from"] == str(today)

    ended = await client.post(
        f"{RULES}/{rule['driver_pay_rule_id']}/end",
        json={"effective_to": str(today - timedelta(days=1))}, headers=auth(w.new_manager))
    assert ended.status_code == 200, ended.text
    fresh = await _make(client, w.new_manager, w, today, amount="500")
    assert fresh["calendar_frequency"] == "Biweek"
    # The same period now selects the new rule, with no blocker.
    row = await _preview_of(client, auth_token, period_id, destination)
    assert row["blockers"] == [], row["blockers"]
    assert Decimal(row["minimum_adjustment"]) == Decimal("450")
    assert [line["source_id"] for line in row["lines"] if line["line_type"] == "SYS_MIN_TOPUP"] == [
        str(fresh["driver_pay_rule_id"])]
    # The reconciled rule no longer asks for reconciliation.
    assert all(r["calendar_reconciliation_needed_from"] is None
               for r in (await _rules(client, auth_token, w.driver_id)).json()
               if r["driver_pay_rule_id"] == fresh["driver_pay_rule_id"])


# ---------------------------------------------------------------------------
# D8: a Scheduled transfer into an incompatible calendar warns and never blocks
# ---------------------------------------------------------------------------

async def test_a_scheduled_transfer_into_an_incompatible_calendar_reports_where_to_reconcile(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w, old={"frequency": "Week"}, new={"frequency": "Biweek"})
    rule = await _make(client, w.old_manager, w, "2026-01-01")
    assert (await _rules(client, auth_token, w.driver_id)).json()[0][
        "calendar_reconciliation_needed_from"] is None

    # Scheduling is allowed: nothing is blocked, ended or rewritten.
    await scheduled_transfer(client, auth_token, w.driver_id, w.new_branch,
                             await db_now(direct_db) + timedelta(days=3))
    transfer_day = await branch_today(direct_db, w.new_branch) + timedelta(days=3)
    for token in (auth_token, w.old_manager):
        row = (await _rules(client, token, w.driver_id)).json()[0]
        assert row["driver_pay_rule_id"] == rule["driver_pay_rule_id"]
        assert row["status"] == "Active"
        assert row["calendar_reconciliation_needed_from"] == str(transfer_day)


async def test_a_scheduled_transfer_into_a_compatible_calendar_needs_no_reconciliation(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    await _calendars(direct_db, w, old={"frequency": "Week"},
                     new={"frequency": "Week", "anchor": date(2000, 1, 10)})
    await _make(client, w.old_manager, w, "2026-01-01")
    await scheduled_transfer(client, auth_token, w.driver_id, w.new_branch,
                             await db_now(direct_db) + timedelta(days=3))
    assert (await _rules(client, auth_token, w.driver_id)).json()[0][
        "calendar_reconciliation_needed_from"] is None


def _first_of_next_month(day: date) -> date:
    return (day.replace(day=1) + timedelta(days=32)).replace(day=1)


async def test_unknown_drivers_and_rules_are_not_found(client, auth_token):
    assert (await _rules(client, auth_token, 2_000_000_000)).status_code == 404
    assert (await client.get(f"{RULES}/2000000000", headers=auth(auth_token))).status_code == 404
    assert (await client.post(f"{RULES}/2000000000/void", headers=auth(auth_token))).status_code == 404
