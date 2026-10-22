"""Reads of a submitted, approved or finalized period come from frozen evidence only.

InReview and Approved periods read the exact snapshot bound to their review item; Locked and
Archived periods read the one snapshot their FinalLines project. Neither ever selects a
"latest" snapshot, and a snapshot that does not verify is refused rather than rebuilt from live
data.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from app.payroll import snapshot_hash
from tests.calculation_snapshot_helpers import (
    allow_self_approval,
    bound_snapshot_id,
    capture,
    decide,
    finalize,
    mutate_live_sources,
    pending_review_item,
    snapshot_ids,
    standard_scenario,
    submit,
    submit_and_approve,
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
from tests.payroll_runtime_fixtures import (
    build_payroll_tenant,
    create_period,
    make_user,
    query,
)

pytestmark = pytest.mark.asyncio
D = Decimal

LIVE_REPORT_VIEWS = ("drivers", "period-work", "period-pay", "mixed")
FINALIZED_VIEWS = ("overview", "reports/drivers", "reports/period-work", "reports/period-pay",
                   "reports/mixed", "off-drivers", "rates-used", "audit")


@pytest.fixture(name="tenant")
def usd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


def _stable(payload):
    """A response without its generation time, comparable across two reads."""
    if isinstance(payload, dict):
        return {k: _stable(v) for k, v in payload.items() if k != "generated_at_utc"}
    if isinstance(payload, list):
        return [_stable(item) for item in payload]
    return payload


async def _live_reports(client, tenant, period) -> dict:
    pid = period["payroll_period_id"]
    result = {}
    for view in LIVE_REPORT_VIEWS:
        response = await client.get(f"/payroll/periods/{pid}/reports/{view}", headers=tenant.admin)
        assert response.status_code == 200, (view, response.text)
        result[view] = _stable(response.json())
    return result


async def _finalized_reads(client, tenant, period) -> dict:
    pid = period["payroll_period_id"]
    result = {}
    for view in FINALIZED_VIEWS:
        response = await client.get(f"/payroll/finalized/{pid}/{view}", headers=tenant.admin)
        assert response.status_code == 200, (view, response.text)
        result[view] = _stable(response.json())
    return result


# ---------------------------------------------------------------------------
# Frozen money and labels
# ---------------------------------------------------------------------------

async def test_reports_of_a_submitted_period_stay_frozen_when_live_sources_change(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    before = await _live_reports(p3c_client, tenant, period)
    (driver,) = before["drivers"]["drivers"]
    assert D(driver["pay"]["total_pay"]) == D("42.00")
    assert driver["driver_name"] == "P3b Driver"
    assert before["drivers"]["columns"][0]["label"] == "Haul"

    await mutate_live_sources(p3c_engine, tenant, period)

    assert await _live_reports(p3c_client, tenant, period) == before


async def test_reports_of_an_approved_period_stay_frozen_when_live_sources_change(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    await submit_and_approve(p3c_client, tenant, period)
    before = await _live_reports(p3c_client, tenant, period)

    await mutate_live_sources(p3c_engine, tenant, period)

    after = await _live_reports(p3c_client, tenant, period)
    assert after == before
    (driver,) = after["drivers"]["drivers"]
    assert (D(driver["pay"]["daily_pay"]), D(driver["pay"]["minimum_adjustment"]),
            D(driver["pay"]["bonus_total"]), D(driver["pay"]["total_pay"])) == (
        D("0.67"), D("39.33"), D("2.00"), D("42.00"))
    assert after["drivers"]["metadata"]["authority_kind"] == "APPROVED_SNAPSHOT"


async def test_the_finalized_library_stays_frozen_when_live_sources_change(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    await submit_and_approve(p3c_client, tenant, period)
    await finalize(p3c_client, tenant, period)
    before = await _finalized_reads(p3c_client, tenant, period)
    assert before["overview"]["financial_summary"]["total_pay"] == "42.0000"
    assert before["reports/drivers"]["metadata"]["authority_kind"] == "FINAL_LINES"
    assert {row["evidence_kind"] for row in before["rates-used"]["used_rate_definitions"]} == {
        "DriverRateSchedule", "DriverPayRule"}

    await mutate_live_sources(p3c_engine, tenant, period)

    assert await _finalized_reads(p3c_client, tenant, period) == before


async def test_a_finalized_period_is_read_through_the_snapshot_its_final_lines_name(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    item_id = await submit_and_approve(p3c_client, tenant, period)
    bound = bound_snapshot_id(tenant, item_id)
    await finalize(p3c_client, tenant, period)
    # A later revision with other money is written; it is never selected.
    query(tenant, "UPDATE payroll.payrolldraftlines SET quantity = 100 WHERE payrollperiodid = %s",
          (period["payroll_period_id"],))
    newer = (await capture(p3c_engine, tenant, period)).snapshot_id
    assert newer != bound and snapshot_ids(tenant, period) == [bound, newer]

    overview = (await p3c_client.get(
        f"/payroll/finalized/{period['payroll_period_id']}/overview", headers=tenant.admin)).json()
    report = (await p3c_client.get(
        f"/payroll/finalized/{period['payroll_period_id']}/reports/drivers",
        headers=tenant.admin)).json()

    assert overview["snapshot_provenance"]["snapshot_id"] == bound
    assert report["metadata"]["snapshot_id"] == bound
    assert report["drivers"][0]["pay"]["total_pay"] == "42.0000"


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------

async def _tamper_bonus_component(engine, snapshot_id: int) -> None:
    async with engine.begin() as conn:
        async with suspended_test_triggers(conn, [TriggerIdentity(
                "payroll", "payrollcalculationsnapshotcomponents",
                "trg_payrollcalculationsnapshotcomponents_immutable")]):
            await conn.execute(text(
                "UPDATE payroll.payrollcalculationsnapshotcomponents SET officialamount = "
                "officialamount + 1 WHERE payrollcalculationsnapshotid = :s AND "
                "componentkind = 'BONUS'"), {"s": snapshot_id})


async def test_a_submitted_report_fails_closed_when_the_bound_snapshot_does_not_verify(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    (snapshot_id,) = snapshot_ids(tenant, period)
    await _tamper_bonus_component(p3c_engine, snapshot_id)

    for view in LIVE_REPORT_VIEWS:
        response = await p3c_client.get(
            f"/payroll/periods/{period['payroll_period_id']}/reports/{view}", headers=tenant.admin)
        # Refused, never rebuilt from live data.
        assert response.status_code == 422, (view, response.text)
        assert response.json()["detail"].startswith("SNAPSHOT_HASH_MISMATCH")


async def test_a_finalized_read_fails_closed_when_the_snapshot_or_final_lines_do_not_verify(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    await submit_and_approve(p3c_client, tenant, period)
    await finalize(p3c_client, tenant, period)
    (snapshot_id,) = snapshot_ids(tenant, period)
    await _tamper_bonus_component(p3c_engine, snapshot_id)

    for view in FINALIZED_VIEWS:
        response = await p3c_client.get(
            f"/payroll/finalized/{period['payroll_period_id']}/{view}", headers=tenant.admin)
        assert response.status_code == 422, (view, response.status_code, response.text[:300])
        assert response.json()["detail"].startswith("SNAPSHOT_HASH_MISMATCH")


async def test_an_unsupported_calculation_version_is_refused_by_every_frozen_read(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    await submit_and_approve(p3c_client, tenant, period)
    pid = period["payroll_period_id"]
    monkeypatch.setattr(
        snapshot_hash, "CURRENT_PAYROLL_CALCULATION_VERSION", "payroll-calculation-v5")

    for view in LIVE_REPORT_VIEWS:
        response = await p3c_client.get(f"/payroll/periods/{pid}/reports/{view}", headers=tenant.admin)
        assert response.status_code == 422, (view, response.text)
        assert response.json()["detail"].startswith("UNSUPPORTED_CALCULATION_VERSION")
    monkeypatch.undo()
    await finalize(p3c_client, tenant, period)
    monkeypatch.setattr(
        snapshot_hash, "CURRENT_PAYROLL_CALCULATION_VERSION", "payroll-calculation-v5")
    for view in ("overview", "reports/drivers", "rates-used", "audit"):
        response = await p3c_client.get(f"/payroll/finalized/{pid}/{view}", headers=tenant.admin)
        assert response.status_code == 422, (view, response.status_code, response.text[:300])
        assert response.json()["detail"].startswith("UNSUPPORTED_CALCULATION_VERSION")


async def test_a_review_item_bound_to_another_periods_snapshot_is_never_read(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    allow_self_approval(tenant)
    draft = await create_period(p3c_client, tenant, tenant.branch_a, mode="PREPARED_CREATION")
    await submit(p3c_client, tenant, period)                 # promotes the adjacent draft to Open
    foreign = (await capture(p3c_engine, tenant, draft)).snapshot_id
    item_id = await pending_review_item(tenant, period)
    async with p3c_engine.begin() as conn:
        async with replica_replication_role(conn):
            await conn.execute(text(
                "UPDATE review.managerreviewitems SET payrollcalculationsnapshotid = :s "
                "WHERE reviewitemid = :i"), {"s": foreign, "i": item_id})

    for view in LIVE_REPORT_VIEWS:
        response = await p3c_client.get(
            f"/payroll/periods/{period['payroll_period_id']}/reports/{view}", headers=tenant.admin)
        assert response.status_code == 422, (view, response.text)
    refused = await p3c_client.post(
        f"/review/items/{item_id}/decide",
        json={"decision": "Approved", "decision_reason": "reviewed"}, headers=tenant.admin)
    assert refused.status_code == 422, refused.text


async def test_final_lines_naming_two_snapshots_make_the_finalized_reads_unavailable(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    await submit_and_approve(p3c_client, tenant, period)
    await finalize(p3c_client, tenant, period)
    other = (await capture(p3c_engine, tenant, period)).snapshot_id
    async with p3c_engine.begin() as conn:
        async with replica_replication_role(conn):
            await conn.execute(text(
                "UPDATE payroll.payrollfinallines SET payrollcalculationsnapshotid = :s "
                "WHERE finallineid = (SELECT min(finallineid) FROM "
                "payroll.payrollfinallines WHERE payrollperiodid = :p)"),
                {"s": other, "p": period["payroll_period_id"]})

    response = await p3c_client.get(
        f"/payroll/finalized/{period['payroll_period_id']}/overview", headers=tenant.admin)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["snapshot_provenance"]["snapshot_id"] is None
    for section in ("snapshot_provenance", "report_evidence", "off_status", "rates_used"):
        assert body["section_availability"][section] == {
            "state": "UNAVAILABLE", "reason_code": "PROVENANCE_UNAVAILABLE"}


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

async def test_frozen_reads_are_denied_to_users_without_branch_access(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    pid = period["payroll_period_id"]
    other_branch = make_user(
        tenant, ["payroll.view", "ledger.view", "ledger.audit.view"],
        scope="SpecificBranch", branch_id=tenant.branch_b)
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    review_item = await pending_review_item(tenant, period)
    await decide(p3c_client, tenant, review_item)
    await finalize(p3c_client, tenant, period)

    paths = [f"/payroll/periods/{pid}/reports/drivers",
             *[f"/payroll/finalized/{pid}/{view}" for view in FINALIZED_VIEWS]]
    for path in paths:
        response = await p3c_client.get(path, headers=other_branch)
        assert response.status_code in (403, 404), (path, response.status_code)
        assert "42.00" not in response.text


async def test_the_snapshot_packet_of_a_review_item_is_scoped_to_the_reviewers_branch(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    item_id = await pending_review_item(tenant, period)
    other_branch = make_user(
        tenant, ["review.view", "review.decide"], scope="SpecificBranch", branch_id=tenant.branch_b)

    own = await p3c_client.get(f"/review/items/{item_id}/payroll-snapshot", headers=tenant.admin)
    assert own.status_code == 200, own.text
    packet = own.json()
    assert packet["snapshot_id"] == bound_snapshot_id(tenant, item_id)
    assert D(packet["total_expected_pay"]) == D("42.00")
    assert packet["period_start_date"] == period["start_date"]
    (driver,) = packet["driver_totals"]
    assert D(driver["expected_pay"]) == D("42.00") and driver["driver_name_snapshot"]
    assert {c["component_kind"] for c in driver["components"]} == {
        "ORDINARY", "BONUS", "MINIMUM_ADJUSTMENT"}
    assert driver["min_max"]["status"] == "APPLIED"
    (rule,) = driver["min_max"]["rules"]
    assert rule["rule_type"] == "MinimumPay" and D(rule["amount"]) == D("70")

    denied = await p3c_client.get(
        f"/review/items/{item_id}/payroll-snapshot", headers=other_branch)
    assert denied.status_code in (403, 404)
