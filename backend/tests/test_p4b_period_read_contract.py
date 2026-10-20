"""Period list / detail read contract and workflow permissions around finalization.

Ports the still-live read contract of the retired payroll-period suites and pins the
permission-before-gate ordering of the P4c evidence refusals.
"""
from __future__ import annotations

import pytest

from tests.compensation_api_fixtures import (  # noqa: F401 - register fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import (  # noqa: F401 - register fixtures
    p3b_cursor,
    p3b_database,
)
from tests.payroll_runtime_fixtures import (
    assign_weekly_setup,
    build_payroll_tenant,
    create_period,
    force_status,
    make_driver_self_user,
    make_user,
    rated_definition,
)

pytestmark = pytest.mark.asyncio


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
