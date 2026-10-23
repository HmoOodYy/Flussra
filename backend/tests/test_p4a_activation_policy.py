"""One effective-date policy for target Branch applicability, including request approval."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.compensation.branch_config import MUTABLE_PERIOD_STATUSES
from tests.compensation_api_fixtures import (  # noqa: F401 - register fixtures
    create_definition,
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
    p3c_tenant,
)
from tests.compensation_database_fixtures import (  # noqa: F401 - register fixtures
    p3b_cursor,
    p3b_database,
)
from tests.test_p3c_pay_definition_governance import _decide, _submitted

pytestmark = pytest.mark.asyncio


def _branch_today(cur, tenant, branch_id: int):
    cur.execute("SELECT core.fn_BranchToday(%s, %s)", (tenant.company_id, branch_id))
    return cur.fetchone()[0]


def _period_around_today(cur, tenant, status: str, *, ends_in_days: int = 3):
    today = _branch_today(cur, tenant, tenant.branch_a)
    pointer = None
    if status == "Returned":
        cur.execute("""
            INSERT INTO review.managerreviewitems
                (companyid, branchid, requesttype, title, status, priority)
            VALUES (%s, %s, 'PeriodApproval', 'activation policy', 'Pending', 'Normal')
            RETURNING reviewitemid
        """, (tenant.company_id, tenant.branch_a))
        pointer = cur.fetchone()[0]
    end = today + timedelta(days=ends_in_days)
    cur.execute("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate,
             currentreturnreviewitemid)
        VALUES (%s, %s, %s, 'ACT-POLICY', 'Activation policy', 'Week', %s, %s, %s)
    """, (tenant.company_id, tenant.branch_a, status, today - timedelta(days=1), end, pointer))
    return today, end


def _config_start(cur, tenant, request_branch_id: int) -> object:
    cur.execute("""
        SELECT effectivefrom FROM payroll.branchpaydefinitionconfig
        WHERE companyid = %s AND branchid = %s AND isactive
    """, (tenant.company_id, request_branch_id))
    return cur.fetchone()[0]


def test_the_mutable_period_vocabulary_is_the_canonical_five():
    assert MUTABLE_PERIOD_STATUSES == ("Draft", "Open", "InReview", "Returned", "Approved")


async def test_request_approval_without_a_period_activates_on_branch_today(
    p3c_client, tenant, cur,
):
    submitted = await _submitted(p3c_client, tenant)
    assert (await _decide(p3c_client, tenant, submitted, "Approve")).status_code == 200
    assert _config_start(cur, tenant, tenant.branch_a) == _branch_today(cur, tenant, tenant.branch_a)


async def test_bulk_default_activation_uses_each_branch_business_date(
    p3c_client, tenant, cur,
):
    from tests.test_p3c_pay_definition_governance import create_definition

    cur.execute("SELECT TimeZoneId FROM core.Branches WHERE BranchID = %s", (tenant.branch_a,))
    prior_a = cur.fetchone()[0]
    cur.execute("SELECT TimeZoneId FROM core.Branches WHERE BranchID = %s", (tenant.branch_b,))
    prior_b = cur.fetchone()[0]
    try:
        cur.execute("UPDATE core.Branches SET TimeZoneId = 'Pacific/Kiritimati' WHERE BranchID = %s", (tenant.branch_a,))
        cur.execute("UPDATE core.Branches SET TimeZoneId = 'Pacific/Honolulu' WHERE BranchID = %s", (tenant.branch_b,))
        today_a = _branch_today(cur, tenant, tenant.branch_a)
        today_b = _branch_today(cur, tenant, tenant.branch_b)
        assert (today_a - today_b).days == 1

        definition = await create_definition(p3c_client, tenant)
        response = await p3c_client.patch(
            f"/compensation/pay-definitions/{definition['pay_definition_id']}/branch-config",
            json={"target": "SelectedBranches", "branch_ids": [tenant.branch_a, tenant.branch_b],
                  "is_active": True},
            headers=tenant.admin,
        )
        assert response.status_code == 200, response.text
        effective_dates = {
            row["branch_id"]: row["effective_from"]
            for row in response.json()["results"]
        }
        assert str(effective_dates[tenant.branch_a]) == today_a.isoformat()
        assert str(effective_dates[tenant.branch_b]) == today_b.isoformat()
    finally:
        cur.execute("UPDATE core.Branches SET TimeZoneId = %s WHERE BranchID = %s", (prior_a, tenant.branch_a))
        cur.execute("UPDATE core.Branches SET TimeZoneId = %s WHERE BranchID = %s", (prior_b, tenant.branch_b))


@pytest.mark.parametrize("status", ["Draft", "Open", "InReview", "Returned", "Approved"])
async def test_request_approval_inside_a_mutable_period_activates_after_it_ends(
    p3c_client, tenant, cur, status,
):
    _, end = _period_around_today(cur, tenant, status)
    submitted = await _submitted(p3c_client, tenant)
    assert (await _decide(p3c_client, tenant, submitted, "Approve")).status_code == 200
    assert _config_start(cur, tenant, tenant.branch_a) == end + timedelta(days=1)


@pytest.mark.parametrize("status", ["Archived", "Cancelled"])
async def test_a_terminal_period_does_not_move_the_activation_date(
    p3c_client, tenant, cur, status,
):
    today, _ = _period_around_today(cur, tenant, status)
    submitted = await _submitted(p3c_client, tenant)
    assert (await _decide(p3c_client, tenant, submitted, "Approve")).status_code == 200
    assert _config_start(cur, tenant, tenant.branch_a) == today


@pytest.mark.parametrize("status", ["Draft", "Returned"])
async def test_an_explicit_date_inside_a_mutable_period_stays_rejected(
    p3c_client, tenant, cur, status,
):
    definition = await create_definition(p3c_client, tenant)
    today, end = _period_around_today(cur, tenant, status)
    url = f"/compensation/branches/{tenant.branch_a}/pay-definitions/{definition['pay_definition_id']}"
    rejected = await p3c_client.patch(
        url, json={"is_active": True, "effective_from": today.isoformat()}, headers=tenant.admin)
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "OPEN_PERIOD_BOUNDARY"
    accepted = await p3c_client.patch(
        url, json={"is_active": True, "effective_from": (end + timedelta(days=1)).isoformat()},
        headers=tenant.admin)
    assert accepted.status_code == 200
