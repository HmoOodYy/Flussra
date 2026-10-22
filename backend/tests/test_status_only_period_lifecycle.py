"""A canonical Status selection is period content for Submit and Resubmit.

The canonical day-entry selection is the Status authority; the legacy DailyStatus duplicate row is
never a lifecycle prerequisite. A period whose only content is a canonical Status submits and
resubmits whether or not the duplicate exists, a period with nothing in it is still refused, and
a Status that exists only as a legacy duplicate (no canonical selection) stays refused by the
canonicalization blocker.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.calculation_snapshot_helpers import (
    allow_self_approval,
    day,
    decide,
    enter,
    open_period,
    pending_review_item,
    period_status,
    rows,
    snapshot_ids,
    status_pay_setup,
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
from tests.db_state import replica_replication_role
from tests.payroll_runtime_fixtures import build_payroll_tenant

pytestmark = pytest.mark.asyncio
D = Decimal


@pytest.fixture(name="tenant")
def usd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _status_only_period(client, engine, tenant) -> dict:
    """An Open period whose only content is one paid Status day (1 hour x 25)."""
    status_pay_setup(tenant, "25")
    period, _, _ = await open_period(client, engine, tenant)
    await enter(client, tenant, period, 0, status_key="PAID")
    return period


async def _void_legacy_duplicate(engine, period) -> None:
    """Remove the legacy DailyStatus duplicate the Day Grid wrote beside the selection."""
    async with engine.begin() as conn:
        async with replica_replication_role(conn):
            result = await conn.execute(text("""
                UPDATE payroll.payrolldraftlines SET status = 'Void'
                WHERE payrollperiodid = :pid AND linetype = 'DailyStatus' AND status <> 'Void'
            """), {"pid": period["payroll_period_id"]})
            assert result.rowcount == 1


def _live_content(tenant, period) -> tuple[int, int]:
    """(non-void draft lines, non-void canonical Status selections)."""
    ((lines,),) = rows(tenant, """
        SELECT count(*) FROM payroll.payrolldraftlines
        WHERE payrollperiodid = %s AND status <> 'Void'
    """, period["payroll_period_id"])
    ((selections,),) = rows(tenant, """
        SELECT count(*) FROM payroll.payrollperioddriverdayentrystate
        WHERE payrollperiodid = %s AND NOT isvoided AND statuskeyid IS NOT NULL
    """, period["payroll_period_id"])
    return lines, selections


async def _resubmit(client, tenant, period, *, expect: int = 200):
    response = await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/resubmissions", json={},
        headers=tenant.admin)
    assert response.status_code == expect, response.text
    return response


async def test_a_status_only_period_submits_without_a_legacy_duplicate(
    p3c_client, p3c_engine, tenant,
):
    period = await _status_only_period(p3c_client, p3c_engine, tenant)
    await _void_legacy_duplicate(p3c_engine, period)
    assert _live_content(tenant, period) == (0, 1)       # no source line at all, one selection
    allow_self_approval(tenant)

    await submit(p3c_client, tenant, period)

    assert period_status(tenant, period) == "InReview"
    (snapshot_id,) = snapshot_ids(tenant, period)
    assert rows(tenant, """
        SELECT c.officialamount FROM payroll.payrollcalculationsnapshotcomponents c
        WHERE c.payrollcalculationsnapshotid = %s AND c.componentkind = 'STATUS'
    """, snapshot_id) == [(D("25.00"),)]


async def test_a_status_only_period_resubmits_without_a_legacy_duplicate(
    p3c_client, p3c_engine, tenant,
):
    period = await _status_only_period(p3c_client, p3c_engine, tenant)
    await _void_legacy_duplicate(p3c_engine, period)
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    await decide(p3c_client, tenant, await pending_review_item(tenant, period), "EditRequested")
    assert period_status(tenant, period) == "Returned"
    assert _live_content(tenant, period) == (0, 1)

    await _resubmit(p3c_client, tenant, period)

    assert period_status(tenant, period) == "InReview"
    assert len(snapshot_ids(tenant, period)) == 2


async def test_a_period_with_nothing_in_it_is_still_refused_at_submit(
    p3c_client, p3c_engine, tenant,
):
    period, _, _ = await open_period(p3c_client, p3c_engine, tenant)
    assert _live_content(tenant, period) == (0, 0)

    response = await submit(p3c_client, tenant, period, expect=422)

    assert "empty period" in response.text
    assert period_status(tenant, period) == "Open"
    assert snapshot_ids(tenant, period) == []


async def test_a_period_whose_only_status_was_cleared_is_refused_at_resubmit(
    p3c_client, p3c_engine, tenant,
):
    period = await _status_only_period(p3c_client, p3c_engine, tenant)
    allow_self_approval(tenant)
    await submit(p3c_client, tenant, period)
    await decide(p3c_client, tenant, await pending_review_item(tenant, period), "EditRequested")
    await enter(p3c_client, tenant, period, 0, status_key="")       # the Status is cleared
    assert _live_content(tenant, period) == (0, 0)

    response = await _resubmit(p3c_client, tenant, period, expect=422)

    assert "empty period" in response.text
    assert period_status(tenant, period) == "Returned"
    assert len(snapshot_ids(tenant, period)) == 1


async def test_a_legacy_only_status_is_refused_by_the_canonicalization_blocker(
    p3c_client, p3c_engine, tenant,
):
    """A DailyStatus row with no canonical selection is not valid Status data: it makes the
    period non-empty but is refused by the intended blocker, not accepted as content."""
    status_pay_setup(tenant, "25")
    period, _, _ = await open_period(p3c_client, p3c_engine, tenant)
    async with p3c_engine.begin() as conn:
        async with replica_replication_role(conn):
            await conn.execute(text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverassignmentid, workdate,
                     linetype, quantity, sourcetype, status, needsmanagerreview, notes, addedbyuserid)
                VALUES (:cid, :bid, :pid, :did, :dt, 'DailyStatus', 0, 'Manual', 'Active', FALSE,
                        'PAID', :uid)
            """), {"cid": tenant.company_id, "bid": tenant.branch_a,
                   "pid": period["payroll_period_id"], "did": tenant.driver_a,
                   "dt": date.fromisoformat(day(period, 0)), "uid": tenant.owner})
    assert _live_content(tenant, period) == (1, 0)

    response = await submit(p3c_client, tenant, period, expect=422)

    assert "LEGACY_STATUS_NOT_CANONICAL" in response.text
    assert "empty period" not in response.text
    assert period_status(tenant, period) == "Open"
    assert snapshot_ids(tenant, period) == []
