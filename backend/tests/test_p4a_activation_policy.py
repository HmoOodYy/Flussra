"""One effective-date policy for target Branch applicability, including request approval."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.compensation.branch_config import MUTABLE_PERIOD_STATUSES
from tests.p3b_fixtures import p3b_cursor, p3b_database  # noqa: F401 - register fixtures
from tests.p3c_fixtures import (  # noqa: F401 - register fixtures
    create_definition,
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
    p3c_tenant,
)
from tests.test_p3c_pay_definition_governance import _decide, _submitted

pytestmark = pytest.mark.asyncio


def _company_today(cur, tenant):
    cur.execute("SELECT core.fn_CompanyToday(%s)", (tenant.company_id,))
    return cur.fetchone()[0]


def _period_around_today(cur, tenant, status: str, *, ends_in_days: int = 3):
    today = _company_today(cur, tenant)
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
        SELECT effectivefrom FROM payroll.branchpayitemconfig
        WHERE companyid = %s AND branchid = %s AND isactive
    """, (tenant.company_id, request_branch_id))
    return cur.fetchone()[0]


def test_the_mutable_period_vocabulary_is_the_canonical_five():
    assert MUTABLE_PERIOD_STATUSES == ("Draft", "Open", "InReview", "Returned", "Approved")


async def test_request_approval_without_a_period_activates_on_company_today(
    p3c_client, tenant, cur,
):
    submitted = await _submitted(p3c_client, tenant)
    assert (await _decide(p3c_client, tenant, submitted, "Approve")).status_code == 200
    assert _config_start(cur, tenant, tenant.branch_a) == _company_today(cur, tenant)


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
