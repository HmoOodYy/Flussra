"""Submit, resubmit and approval freeze and authorize exactly one immutable snapshot.

Real tenants, periods and routes. Submission freezes the live packet into one new revision and
binds it to the Pending PeriodApproval review item; approval authorizes exactly that revision
after it verifies (version, persisted-row hash, evidence arithmetic) and never builds a live
packet or substitutes a newer revision.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.payroll import period_lifecycle, snapshot_hash
from tests.calculation_snapshot_helpers import (
    add_rule,
    allow_self_approval,
    bound_snapshot_id,
    capture,
    decide,
    enter,
    finalization_preview,
    finalize,
    mutate_live_sources,
    open_period,
    pending_review_item,
    period_evidence_counts,
    period_status,
    rows,
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
from tests.db_state import TriggerIdentity, suspended_test_triggers
from tests.payroll_runtime_fixtures import (
    build_payroll_tenant,
    create_period,
    make_driver_self_user,
    make_user,
    query,
)

pytestmark = pytest.mark.asyncio
D = Decimal


@pytest.fixture(name="tenant")
def usd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


def _snapshot_row(tenant, snapshot_id: int) -> dict:
    ((revision, snapshot_hash_value, total, version),) = rows(tenant, """
        SELECT revisionnumber, snapshothash, totalexpectedpay, calculationversion
        FROM payroll.payrollcalculationsnapshots WHERE payrollcalculationsnapshotid = %s
    """, snapshot_id)
    return {"revision": revision, "hash": snapshot_hash_value, "total": total, "version": version}


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------

async def test_submit_freezes_one_snapshot_and_binds_it_to_the_pending_review_item(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    allow_self_approval(tenant)

    response = await submit(p3c_client, tenant, period)

    assert response.json()["status"] == "InReview" and period_status(tenant, period) == "InReview"
    (snapshot_id,) = snapshot_ids(tenant, period)
    item_id = await pending_review_item(tenant, period)
    assert bound_snapshot_id(tenant, item_id) == snapshot_id
    frozen = _snapshot_row(tenant, snapshot_id)
    assert (frozen["revision"], frozen["version"]) == (1, "payroll-calculation-v4")
    assert frozen["total"] == D("42.00")
    # The submission is evidenced: workflow action bound to the snapshot, and an audit entry.
    assert rows(tenant, """
        SELECT actioncode FROM payroll.payrollperiodworkflowactionevidence
        WHERE payrollperiodid = %s AND payrollcalculationsnapshotid = %s
    """, period["payroll_period_id"], snapshot_id) == [("SUBMITTED",)]
    assert rows(tenant, """
        SELECT count(*) FROM audit.auditlog
        WHERE actioncode = 'CALCULATION_SNAPSHOT_CAPTURED' AND entityid = %s
    """, str(snapshot_id)) == [(1,)]


async def test_submit_is_refused_for_a_calculation_blocker_and_writes_nothing(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, _ = await open_period(p3c_client, p3c_engine, tenant, rate=None)
    await enter(p3c_client, tenant, period, 0, ppd=ppd, quantity="3")
    before = period_evidence_counts(tenant, period)

    response = await submit(p3c_client, tenant, period, expect=422)

    assert "incomplete calculation packet" in response.text
    assert period_status(tenant, period) == "Open"
    assert period_evidence_counts(tenant, period) == before == {
        "snapshots": 0, "final_lines": 0, "review_items": 0, "workflow_evidence": 0}


async def test_submit_rolls_back_every_write_when_a_later_step_fails(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    before = period_evidence_counts(tenant, period)

    async def failing_evidence(**_kwargs):
        raise RuntimeError("simulated failure after the snapshot was written")

    monkeypatch.setattr(period_lifecycle, "capture_workflow_action_evidence", failing_evidence)
    with pytest.raises(RuntimeError, match="simulated failure"):
        await submit(p3c_client, tenant, period)

    assert period_status(tenant, period) == "Open"
    assert period_evidence_counts(tenant, period) == before


async def test_concurrent_submissions_of_one_period_create_a_single_revision(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    allow_self_approval(tenant)

    async def attempt():
        return await p3c_client.patch(
            f"/payroll/periods/{period['payroll_period_id']}/status",
            json={"status": "InReview"}, headers=tenant.admin)

    first, second = await asyncio.gather(attempt(), attempt())

    assert sorted([first.status_code, second.status_code])[0] == 200
    assert sorted([first.status_code, second.status_code])[1] in (409, 422)
    assert len(snapshot_ids(tenant, period)) == 1
    counts = period_evidence_counts(tenant, period)
    assert counts["review_items"] == 1 and counts["workflow_evidence"] == 1
    assert period_status(tenant, period) == "InReview"


async def test_submit_promotes_the_adjacent_draft_period_in_the_same_transaction(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    allow_self_approval(tenant)
    draft = await create_period(
        p3c_client, tenant, tenant.branch_a, mode="PREPARED_CREATION")
    assert period_status(tenant, draft) == "Draft"
    assert date.fromisoformat(draft["start_date"]) == (
        date.fromisoformat(period["end_date"]) + timedelta(days=1))

    await submit(p3c_client, tenant, period)

    assert period_status(tenant, period) == "InReview"
    assert period_status(tenant, draft) == "Open"


async def test_a_period_in_the_wrong_status_or_without_permission_is_refused_before_any_evidence(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period, period_id = scenario.period, scenario.period_id
    viewer = make_user(tenant, ["payroll.view"])
    entry_only = make_user(tenant, ["payroll.entry"])
    driver = make_driver_self_user(tenant, ["payroll.view", "payroll.entry", "payroll.finalize"])
    status_url = f"/payroll/periods/{period_id}/status"

    for headers in (viewer, driver):
        refused = await p3c_client.patch(status_url, json={"status": "InReview"}, headers=headers)
        assert refused.status_code == 403, refused.text
    finalize_url = f"/payroll/periods/{period_id}/finalize"
    preview_url = f"/payroll/periods/{period_id}/finalization-preview"
    # An unapproved period is a status problem first.
    assert (await p3c_client.post(finalize_url, headers=tenant.admin)).status_code == 422
    assert (await p3c_client.get(preview_url, headers=tenant.admin)).status_code == 422
    resubmit = f"/payroll/periods/{period_id}/resubmissions"
    assert (await p3c_client.post(resubmit, json={}, headers=tenant.admin)).status_code == 422

    await submit_and_approve(p3c_client, tenant, period)
    finalizer = make_user(tenant, ["payroll.finalize"])
    for headers in (entry_only, viewer, driver):
        assert (await p3c_client.post(finalize_url, headers=headers)).status_code == 403
        assert (await p3c_client.get(preview_url, headers=headers)).status_code == 403
    assert (await p3c_client.get(preview_url, headers=finalizer)).status_code == 200
    assert period_evidence_counts(tenant, period)["final_lines"] == 0


# ---------------------------------------------------------------------------
# Return and resubmit
# ---------------------------------------------------------------------------

async def test_return_then_resubmit_freezes_a_new_revision_and_leaves_the_first_untouched(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    first_item = await pending_review_item(tenant, period)
    (first_snapshot,) = snapshot_ids(tenant, period)
    first_before = _snapshot_row(tenant, first_snapshot)
    await decide(p3c_client, tenant, first_item, "EditRequested")
    assert period_status(tenant, period) == "Returned"

    # Correct the source (a further ordinary day) and resubmit.
    await enter(p3c_client, tenant, period, 5, ppd=scenario.ppd, quantity="1")
    resubmitted = await p3c_client.post(
        f"/payroll/periods/{period['payroll_period_id']}/resubmissions", json={},
        headers=tenant.admin)
    assert resubmitted.status_code == 200, resubmitted.text

    first_snapshot_id, second_snapshot_id = snapshot_ids(tenant, period)
    assert first_snapshot_id == first_snapshot
    second = _snapshot_row(tenant, second_snapshot_id)
    assert second["revision"] == 2 and second["hash"] != first_before["hash"]
    assert _snapshot_row(tenant, first_snapshot) == first_before        # the earlier one untouched
    second_item = await pending_review_item(tenant, period)
    assert second_item != first_item
    assert bound_snapshot_id(tenant, second_item) == second_snapshot_id
    assert bound_snapshot_id(tenant, first_item) == first_snapshot      # the exact relationship
    assert rows(tenant, """
        SELECT actioncode FROM payroll.payrollperiodworkflowactionevidence
        WHERE payrollcalculationsnapshotid = %s
    """, second_snapshot_id) == [("RESUBMITTED",)]


async def test_resubmitting_unchanged_source_freezes_an_identical_financial_hash(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    await decide(p3c_client, tenant, await pending_review_item(tenant, period), "Rejected")
    await p3c_client.post(
        f"/payroll/periods/{period['payroll_period_id']}/resubmissions", json={},
        headers=tenant.admin)

    first, second = snapshot_ids(tenant, period)
    assert _snapshot_row(tenant, first)["hash"] == _snapshot_row(tenant, second)["hash"]
    assert _snapshot_row(tenant, second)["revision"] == 2


# ---------------------------------------------------------------------------
# Approval binding
# ---------------------------------------------------------------------------

async def test_approval_authorizes_exactly_the_bound_revision_never_a_newer_one(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    item_id = await pending_review_item(tenant, period)
    (bound,) = snapshot_ids(tenant, period)
    # A newer revision with different money exists (written outside the review item).
    query(tenant, "UPDATE payroll.payrolldraftlines SET quantity = 100 "
                  "WHERE payrollperiodid = %s", (period["payroll_period_id"],))
    newer = (await capture(p3c_engine, tenant, period)).snapshot_id
    assert newer != bound and _snapshot_row(tenant, newer)["total"] != D("42.00")

    await decide(p3c_client, tenant, item_id)
    await finalize(p3c_client, tenant, period)

    assert rows(tenant, "SELECT DISTINCT payrollcalculationsnapshotid FROM payroll.payrollfinallines "
                        "WHERE payrollperiodid = %s", period["payroll_period_id"]) == [(bound,)]
    assert rows(tenant, "SELECT sum(finalamount) FROM payroll.payrollfinallines "
                        "WHERE payrollperiodid = %s", period["payroll_period_id"]) == [(D("42.00"),)]


async def test_approval_fails_closed_when_the_bound_snapshot_no_longer_verifies(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    item_id = await pending_review_item(tenant, period)
    (snapshot_id,) = snapshot_ids(tenant, period)
    async with p3c_engine.begin() as conn:
        async with suspended_test_triggers(conn, [TriggerIdentity(
                "payroll", "payrollcalculationsnapshotcomponents",
                "trg_payrollcalculationsnapshotcomponents_immutable")]):
            from sqlalchemy import text
            await conn.execute(text(
                "UPDATE payroll.payrollcalculationsnapshotcomponents SET officialamount = "
                "officialamount + 1 WHERE payrollcalculationsnapshotid = :s AND "
                "componentkind = 'BONUS'"), {"s": snapshot_id})

    refused = await decide(p3c_client, tenant, item_id, expect=422)

    assert refused.json()["detail"].startswith("SNAPSHOT_HASH_MISMATCH")
    assert period_status(tenant, period) == "InReview"
    assert rows(tenant, "SELECT status FROM review.managerreviewitems WHERE reviewitemid = %s",
                item_id) == [("Pending",)]
    # Returning it for correction never needs the snapshot to verify.
    await decide(p3c_client, tenant, item_id, "EditRequested")
    assert period_status(tenant, period) == "Returned"


async def test_approval_authorizes_the_bound_revision_even_after_live_sources_changed(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    item_id = await pending_review_item(tenant, period)
    await mutate_live_sources(p3c_engine, tenant, period)

    await decide(p3c_client, tenant, item_id)

    assert period_status(tenant, period) == "Approved"
    preview = (await finalization_preview(p3c_client, tenant, period)).json()
    assert D(str(preview["total_final_gross"])) == D("42.00")


async def test_a_calculation_snapshot_of_another_version_is_never_consumed(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    item_id = await pending_review_item(tenant, period)
    monkeypatch.setattr(snapshot_hash, "CURRENT_PAYROLL_CALCULATION_VERSION", "payroll-calculation-v5")

    refused = await decide(p3c_client, tenant, item_id, expect=422)
    assert refused.json()["detail"].startswith("UNSUPPORTED_CALCULATION_VERSION")
    assert period_status(tenant, period) == "InReview"

    monkeypatch.undo()
    await decide(p3c_client, tenant, item_id)
    monkeypatch.setattr(snapshot_hash, "CURRENT_PAYROLL_CALCULATION_VERSION", "payroll-calculation-v5")
    for response in (await finalization_preview(p3c_client, tenant, period, expect=422),
                     await finalize(p3c_client, tenant, period, expect=422)):
        assert response.json()["detail"].startswith("UNSUPPORTED_CALCULATION_VERSION")
    assert period_status(tenant, period) == "Approved"
    assert period_evidence_counts(tenant, period)["final_lines"] == 0


async def test_resubmission_checks_status_and_permission_before_freezing_anything(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period, period_id = scenario.period, scenario.period_id
    viewer = make_user(tenant, ["payroll.view"])
    entry_only = make_user(tenant, ["payroll.entry"])
    resubmit = f"/payroll/periods/{period_id}/resubmissions"
    assert (await p3c_client.post(resubmit, json={}, headers=tenant.admin)).status_code == 422

    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    await decide(p3c_client, tenant, await pending_review_item(tenant, period), "EditRequested")
    before = period_evidence_counts(tenant, period)
    assert (await p3c_client.post(resubmit, json={}, headers=viewer)).status_code == 403
    assert period_evidence_counts(tenant, period) == before

    assert (await p3c_client.post(resubmit, json={}, headers=entry_only)).status_code == 200
    assert len(snapshot_ids(tenant, period)) == 2


async def test_workflow_capabilities_allow_submission_and_resubmission_of_a_calculable_period(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period, period_id = scenario.period, scenario.period_id

    async def capability(name: str) -> dict:
        hub = (await p3c_client.get("/payroll/current", headers=tenant.admin)).json()
        branch = next(b for b in hub["branches"] if b["branch_id"] == tenant.branch_a)
        return branch["capabilities"]["periods"][str(period_id)][name]

    assert (await capability("can_submit_for_review"))["allowed"] is True
    assert (await capability("can_resubmit_returned"))["reason_code"] == "PERIOD_NOT_RETURNED"
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    await decide(p3c_client, tenant, await pending_review_item(tenant, period), "EditRequested")
    assert (await capability("can_resubmit_returned"))["allowed"] is True


async def test_submit_succeeds_when_a_minimum_prorates_to_zero_in_a_four_digit_currency(
    p3c_client, p3c_engine, p3b_dsn,
):
    clf_tenant = build_payroll_tenant(p3b_dsn, currency="CLF")
    period, ppd, _ = await open_period(
        p3c_client, p3c_engine, clf_tenant, hire_offset=6, rate="0.3333")
    await enter(p3c_client, clf_tenant, period, 6, ppd=ppd, quantity="1")
    await add_rule(p3c_client, clf_tenant, period, "MinimumPay", "0.0001")
    allow_self_approval(clf_tenant)

    await submit(p3c_client, clf_tenant, period)

    (snapshot_id,) = snapshot_ids(clf_tenant, period)
    assert rows(clf_tenant, """
        SELECT proratedamount FROM payroll.payrollcalculationsnapshotminmaxrules
        WHERE payrollcalculationsnapshotid = %s
    """, snapshot_id) == [(D("0.0000"),)]
    await decide(p3c_client, clf_tenant, await pending_review_item(clf_tenant, period))
    await finalize(p3c_client, clf_tenant, period)
    assert rows(clf_tenant, "SELECT sum(finalamount) FROM payroll.payrollfinallines "
                            "WHERE payrollperiodid = %s", period["payroll_period_id"]) == [(D("0.3333"),)]
