"""Money shown for a period comes from the period financial authority, never from live rates.

Open and Returned periods derive money live. InReview, Approved, Locked and Archived periods take
it only from the exact line of the verified frozen snapshot, so replacing a live rate after
submission changes nothing the Day Grid, the source lines and their summary show. A frozen
authority that does not verify withholds the money (never a live fallback) and keeps the entry
data.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.calculation_snapshot_helpers import (
    allow_self_approval,
    day,
    decide,
    enter,
    finalize,
    open_period,
    pending_review_item,
    snapshot_ids,
    submit,
)
from tests.compensation_api_fixtures import (  # noqa: F401 - register fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import (  # noqa: F401 - register fixtures
    p3b_cursor,
    p3b_database,
)
from tests.db_state import TriggerIdentity, replica_replication_role, suspended_test_triggers
from tests.payroll_runtime_fixtures import build_payroll_tenant

pytestmark = pytest.mark.asyncio
D = Decimal


@pytest.fixture(name="tenant")
def usd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _open_period_with_work(client, engine, tenant):
    """Quantity 2 at rate 10: the ordinary pay is 20."""
    period, ppd, _ = await open_period(
        client, engine, tenant, rate="10", definition_code="HAUL", definition_name="Haul")
    await enter(client, tenant, period, 0, ppd=ppd, quantity="2")
    return period, ppd


async def _replace_live_rate(engine, amount: str) -> None:
    async with engine.begin() as conn:
        async with replica_replication_role(conn):
            result = await conn.execute(
                text("UPDATE payroll.driverrateschedulevalues SET amount = :amount "
                     "WHERE amount IS NOT NULL"), {"amount": D(amount)})
            assert result.rowcount >= 1


async def _reads(client, tenant, period, ppd) -> dict:
    """What the Day Grid, the source lines and their summary show for the period."""
    pid = period["payroll_period_id"]
    grid = await client.get(
        f"/payroll/periods/{pid}/day-grid", params={"work_date": day(period, 0)},
        headers=tenant.admin)
    lines = await client.get(f"/payroll/periods/{pid}/lines", headers=tenant.admin)
    summary = await client.get(f"/payroll/periods/{pid}/lines/summary", headers=tenant.admin)
    for response in (grid, lines, summary):
        assert response.status_code == 200, response.text
    grid_body = grid.json()
    (grid_row,) = [r for r in grid_body["rows"] if r["driver_assignment_id"] == tenant.driver_a]
    (line,) = [row for row in lines.json() if row["payroll_period_definition_id"] == ppd]
    (summary_row,) = summary.json()
    return {"grid": grid_body, "grid_value": grid_row["values"][str(ppd)], "line": line,
            "summary": summary_row}


def _money(reads: dict) -> tuple:
    value = lambda x: None if x is None else D(str(x))  # noqa: E731
    return (
        value(reads["grid_value"]["calculated_amount"]), value(reads["grid"]["summary"]["gross_total"]),
        value(reads["line"]["calculated_amount"]), value(reads["summary"]["total_calculated_amount"]),
    )


async def _drive_to(client, tenant, period, state: str) -> None:
    allow_self_approval(tenant)
    await submit(client, tenant, period)
    if state in ("Approved", "Locked"):
        await decide(client, tenant, await pending_review_item(tenant, period))
    if state == "Locked":
        await finalize(client, tenant, period)


@pytest.mark.parametrize("state", ["InReview", "Approved", "Locked"])
async def test_day_grid_lines_and_summary_keep_the_frozen_money_when_the_live_rate_changes(
    p3c_client, p3c_engine, tenant, state,
):
    period, ppd = await _open_period_with_work(p3c_client, p3c_engine, tenant)
    live_before = await _reads(p3c_client, tenant, period, ppd)
    assert _money(live_before) == (D("20"), D("20"), D("20"), D("20"))
    assert live_before["grid"]["summary"]["financial_authority"] == "LIVE"
    await _drive_to(p3c_client, tenant, period, state)
    frozen = await _reads(p3c_client, tenant, period, ppd)
    assert _money(frozen) == (D("20"), D("20"), D("20"), D("20"))

    await _replace_live_rate(p3c_engine, "20")

    after = await _reads(p3c_client, tenant, period, ppd)
    assert _money(after) == (D("20"), D("20"), D("20"), D("20"))
    assert D(after["line"]["rate_amount"]) == D("10")          # the rate frozen with the line
    assert D(after["summary"]["total_quantity"]) == D("2") and after["summary"]["line_count"] == 1
    for reads in (after["grid"]["summary"], after["line"], after["summary"]):
        assert reads["financial_authority"] == "FROZEN" and reads["financial_authority_reason"] is None
    assert after["grid"]["summary"]["financials_available"] is True


async def test_an_open_period_keeps_showing_live_money(p3c_client, p3c_engine, tenant):
    period, ppd = await _open_period_with_work(p3c_client, p3c_engine, tenant)

    await _replace_live_rate(p3c_engine, "20")

    assert _money(await _reads(p3c_client, tenant, period, ppd)) == (
        D("40"), D("40"), D("40"), D("40"))


async def test_a_returned_period_is_live_again_and_the_resubmission_freezes_the_new_money(
    p3c_client, p3c_engine, tenant,
):
    period, ppd = await _open_period_with_work(p3c_client, p3c_engine, tenant)
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    await decide(p3c_client, tenant, await pending_review_item(tenant, period), "EditRequested")

    await _replace_live_rate(p3c_engine, "20")

    returned = await _reads(p3c_client, tenant, period, ppd)
    assert _money(returned) == (D("40"), D("40"), D("40"), D("40"))
    assert returned["grid"]["summary"]["financial_authority"] == "LIVE"
    resubmitted = await p3c_client.post(
        f"/payroll/periods/{period['payroll_period_id']}/resubmissions", json={}, headers=tenant.admin)
    assert resubmitted.status_code == 200, resubmitted.text
    await _replace_live_rate(p3c_engine, "30")
    assert _money(await _reads(p3c_client, tenant, period, ppd)) == (
        D("40"), D("40"), D("40"), D("40"))


async def test_a_void_source_has_no_money_in_a_frozen_period(p3c_client, p3c_engine, tenant):
    period, ppd = await _open_period_with_work(p3c_client, p3c_engine, tenant)
    await enter(p3c_client, tenant, period, 1, ppd=ppd, quantity="3")
    await enter(p3c_client, tenant, period, 1, ppd=ppd, quantity="")        # clears: Void
    await _drive_to(p3c_client, tenant, period, "Approved")

    lines = (await p3c_client.get(
        f"/payroll/periods/{period['payroll_period_id']}/lines", params={"status": "Void"},
        headers=tenant.admin)).json()

    assert lines and all(
        line["calculated_amount"] is None and line["rate_amount"] is None for line in lines)


async def _tamper_ordinary_component(engine, snapshot_id: int) -> None:
    async with engine.begin() as conn:
        async with suspended_test_triggers(conn, [TriggerIdentity(
                "payroll", "payrollcalculationsnapshotcomponents",
                "trg_payrollcalculationsnapshotcomponents_immutable")]):
            await conn.execute(text(
                "UPDATE payroll.payrollcalculationsnapshotcomponents SET officialamount = "
                "officialamount + 1 WHERE payrollcalculationsnapshotid = :s "
                "AND componentkind = 'ORDINARY'"), {"s": snapshot_id})


@pytest.mark.parametrize("state", ["InReview", "Locked"])
async def test_a_frozen_authority_that_does_not_verify_withholds_money_and_keeps_entry_data(
    p3c_client, p3c_engine, tenant, state,
):
    period, ppd = await _open_period_with_work(p3c_client, p3c_engine, tenant)
    await _drive_to(p3c_client, tenant, period, state)
    (snapshot_id,) = snapshot_ids(tenant, period)
    await _tamper_ordinary_component(p3c_engine, snapshot_id)
    await _replace_live_rate(p3c_engine, "20")                 # live money would be 40

    reads = await _reads(p3c_client, tenant, period, ppd)

    assert _money(reads) == (None, None, None, None)
    for withheld in (reads["grid"]["summary"], reads["line"], reads["summary"]):
        assert withheld["financial_authority"] == "UNAVAILABLE"
        assert withheld["financial_authority_reason"] == "SNAPSHOT_HASH_MISMATCH"
    assert reads["grid"]["summary"]["financials_available"] is False
    assert reads["grid_value"]["quantity"] == "2.0000" and reads["grid_value"]["line_id"]
    assert D(reads["line"]["quantity"]) == D("2") and reads["line"]["rate_amount"] is None
    assert D(reads["summary"]["total_quantity"]) == D("2") and reads["summary"]["line_count"] == 1
    assert reads["line"]["needs_manager_review"] is False


async def test_a_draft_period_shows_no_money_and_names_why(p3c_client, p3c_engine, tenant):
    from tests.payroll_runtime_fixtures import create_period

    period, ppd = await _open_period_with_work(p3c_client, p3c_engine, tenant)
    draft = await create_period(p3c_client, tenant, tenant.branch_a, mode="PREPARED_CREATION")

    grid = await p3c_client.get(
        f"/payroll/periods/{draft['payroll_period_id']}/day-grid", headers=tenant.admin)

    assert grid.status_code == 200, grid.text
    summary = grid.json()["summary"]
    assert summary["gross_total"] is None and summary["financials_available"] is False
    assert summary["financial_authority"] == "UNAVAILABLE"
    assert summary["financial_authority_reason"] == "SOURCE_ONLY_PERIOD"
    assert period["payroll_period_id"] != draft["payroll_period_id"] and ppd
