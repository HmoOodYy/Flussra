"""Period list / detail read contract and workflow permissions around finalization.

Ports the still-live read contract of the retired payroll-period suites and pins the
permission-before-gate ordering of the P4c evidence refusals.
"""
from __future__ import annotations

import pytest

from tests.p3b_fixtures import p3b_cursor, p3b_database  # noqa: F401 - register fixtures
from tests.p3c_fixtures import (  # noqa: F401 - register fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.p4b_fixtures import (
    assign_weekly_setup,
    build_payroll_tenant,
    create_period,
    force_status,
    make_driver_self_user,
    make_user,
    open_period_with_grid,
    rated_definition,
)

pytestmark = pytest.mark.asyncio

CODE = "TARGET_PAYROLL_EVIDENCE_NOT_READY"


@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _two_periods(client, engine, tenant):
    await rated_definition(client, tenant)
    await assign_weekly_setup(engine, tenant, tenant.branch_a)
    await assign_weekly_setup(engine, tenant, tenant.branch_b)
    open_a = await create_period(client, tenant, tenant.branch_a)
    draft_a = await create_period(client, tenant, tenant.branch_a, mode="PREPARED_CREATION")
    open_b = await create_period(client, tenant, tenant.branch_b)
    return open_a, draft_a, open_b


# ---------------------------------------------------------------------------
# Listing and detail
# ---------------------------------------------------------------------------

async def test_the_period_list_filters_pages_and_requires_authentication(
    p3c_client, p3c_engine, tenant,
):
    open_a, draft_a, open_b = await _two_periods(p3c_client, p3c_engine, tenant)
    assert (await p3c_client.get("/payroll/periods")).status_code in (401, 403)

    everything = (await p3c_client.get("/payroll/periods", headers=tenant.admin)).json()
    ids = {p["payroll_period_id"] for p in everything}
    assert ids == {open_a["payroll_period_id"], draft_a["payroll_period_id"],
                   open_b["payroll_period_id"]}
    assert {"payroll_period_id", "branch_id", "status", "start_date", "end_date",
            "period_code", "period_name"} <= set(everything[0])

    drafts = (await p3c_client.get("/payroll/periods", params={"status": "Draft"},
                                   headers=tenant.admin)).json()
    assert [p["payroll_period_id"] for p in drafts] == [draft_a["payroll_period_id"]]
    assert (await p3c_client.get("/payroll/periods", params={"status": "NoSuchStatus"},
                                 headers=tenant.admin)).json() == []
    branch_b = (await p3c_client.get("/payroll/periods", params={"branch_id": tenant.branch_b},
                                     headers=tenant.admin)).json()
    assert [p["payroll_period_id"] for p in branch_b] == [open_b["payroll_period_id"]]

    first_page = (await p3c_client.get("/payroll/periods", params={"limit": 2},
                                       headers=tenant.admin)).json()
    second_page = (await p3c_client.get("/payroll/periods", params={"limit": 2, "offset": 2},
                                        headers=tenant.admin)).json()
    assert len(first_page) == 2 and len(second_page) == 1
    assert {p["payroll_period_id"] for p in first_page + second_page} == ids


async def test_the_period_list_and_detail_respect_branch_scope_and_company(
    p3c_client, p3c_engine, tenant,
):
    open_a, _, open_b = await _two_periods(p3c_client, p3c_engine, tenant)
    scoped = make_user(tenant, ["payroll.view"], scope="SpecificBranch", branch_id=tenant.branch_a)
    visible = {p["payroll_period_id"] for p in (
        await p3c_client.get("/payroll/periods", headers=scoped)).json()}
    assert open_b["payroll_period_id"] not in visible and open_a["payroll_period_id"] in visible
    assert (await p3c_client.get(f"/payroll/periods/{open_b['payroll_period_id']}",
                                 headers=scoped)).status_code in (403, 404)
    foreign = build_payroll_tenant(tenant.dsn).admin
    assert (await p3c_client.get("/payroll/periods", headers=foreign)).json() == []
    assert (await p3c_client.get(f"/payroll/periods/{open_a['payroll_period_id']}",
                                 headers=foreign)).status_code == 404
    assert (await p3c_client.get("/payroll/periods", headers=make_driver_self_user(
        tenant, ["payroll.view"]))).status_code == 403


async def test_period_detail_returns_the_period_or_404(p3c_client, p3c_engine, tenant):
    open_a, _, _ = await _two_periods(p3c_client, p3c_engine, tenant)
    detail = await p3c_client.get(f"/payroll/periods/{open_a['payroll_period_id']}",
                                  headers=tenant.admin)
    assert detail.status_code == 200
    body = detail.json()
    assert body["payroll_period_id"] == open_a["payroll_period_id"]
    assert body["status"] == "Open" and body["branch_id"] == tenant.branch_a
    assert body["period_name"] and body["period_code"]
    assert (await p3c_client.get("/payroll/periods/987654", headers=tenant.admin)
            ).status_code == 404
    assert (await p3c_client.get(f"/payroll/periods/{open_a['payroll_period_id']}")
            ).status_code in (401, 403)


async def test_the_review_page_discovers_inreview_periods_by_status(
    p3c_client, p3c_engine, tenant,
):
    open_a, draft_a, _ = await _two_periods(p3c_client, p3c_engine, tenant)
    force_status(tenant, open_a["payroll_period_id"], "InReview")
    inreview = (await p3c_client.get("/payroll/periods", params={"status": "InReview"},
                                     headers=tenant.admin)).json()
    assert [p["payroll_period_id"] for p in inreview] == [open_a["payroll_period_id"]]


# ---------------------------------------------------------------------------
# The evidence refusal never masks the ordinary checks
# ---------------------------------------------------------------------------

async def test_permissions_and_status_are_checked_before_the_evidence_refusal(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]
    viewer = make_user(tenant, ["payroll.view"])
    entry_only = make_user(tenant, ["payroll.entry"])
    driver = make_driver_self_user(tenant, ["payroll.view", "payroll.entry", "payroll.finalize"])

    # Submit needs payroll.entry; the gate never answers a caller who lacks it.
    submit = {"status": "InReview"}
    for headers in (viewer, driver):
        response = await p3c_client.patch(f"/payroll/periods/{period_id}/status", json=submit,
                                          headers=headers)
        assert response.status_code == 403, response.text
    gated = await p3c_client.patch(f"/payroll/periods/{period_id}/status", json=submit,
                                   headers=entry_only)
    assert gated.status_code == 409 and gated.json()["detail"]["code"] == CODE

    # An unapproved period is a status problem before anything else.
    finalize = f"/payroll/periods/{period_id}/finalize"
    preview = f"/payroll/periods/{period_id}/finalization-preview"
    assert (await p3c_client.post(finalize, headers=tenant.admin)).status_code == 422
    assert (await p3c_client.get(preview, headers=tenant.admin)).status_code == 422

    force_status(tenant, period_id, "Approved")
    for headers in (entry_only, viewer, driver):
        assert (await p3c_client.post(finalize, headers=headers)).status_code == 403
        assert (await p3c_client.get(preview, headers=headers)).status_code == 403
    finalizer = make_user(tenant, ["payroll.finalize"])
    for response in (await p3c_client.post(finalize, headers=finalizer),
                     await p3c_client.get(preview, headers=finalizer)):
        assert response.status_code == 409 and response.json()["detail"]["code"] == CODE

    # Resubmission has its own state and permission checks first.
    resubmit = f"/payroll/periods/{period_id}/resubmissions"
    assert (await p3c_client.post(resubmit, json={}, headers=tenant.admin)).status_code == 422
    force_status(tenant, period_id, "Returned")
    assert (await p3c_client.post(resubmit, json={}, headers=viewer)).status_code == 403
    assert (await p3c_client.post(resubmit, json={}, headers=entry_only)).status_code == 409
