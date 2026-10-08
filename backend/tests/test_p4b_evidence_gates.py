"""P4c readiness gates: target payroll cannot freeze or finalize money yet."""
from __future__ import annotations

import psycopg2
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
    rated_definition,
)

pytestmark = pytest.mark.asyncio

CODE = "TARGET_PAYROLL_EVIDENCE_NOT_READY"


@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


def _query(tenant, sql: str, params=()):
    conn = psycopg2.connect(client_encoding="utf-8", **tenant.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


def _counts(tenant, period_id: int) -> dict:
    """Everything a refused path must leave untouched."""
    scalar = lambda sql, *p: _query(tenant, sql, p)[0][0]  # noqa: E731
    return {
        "status": scalar("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = %s",
                         period_id),
        "snapshots": scalar("SELECT count(*) FROM payroll.payrollcalculationsnapshots "
                            "WHERE payrollperiodid = %s", period_id),
        "final_lines": scalar("SELECT count(*) FROM payroll.payrollfinallines "
                              "WHERE payrollperiodid = %s", period_id),
        "review_items": scalar(
            "SELECT count(*) FROM review.managerreviewitems "
            "WHERE entityname = 'PayrollPeriods' AND entityid = %s", str(period_id)),
        "lines": scalar("SELECT count(*) FROM payroll.payrolldraftlines "
                        "WHERE payrollperiodid = %s", period_id),
    }


async def _open_period_with_source(client, engine, tenant) -> dict:
    await rated_definition(client, tenant, rate="25")
    await assign_weekly_setup(engine, tenant, tenant.branch_a)
    period = await create_period(client, tenant, tenant.branch_a)
    grid = (await client.get(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid", headers=tenant.admin)).json()
    ppd = grid["columns"][0]["payroll_period_definition_id"]
    saved = await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid",
        json={"work_date": grid["work_date"], "rows": [
            {"driver_id": tenant.driver_a, "values": {str(ppd): "8"}}]},
        headers=tenant.admin)
    assert saved.status_code == 200, saved.text
    return period


def _assert_gate(response) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == CODE


async def test_submit_is_refused_before_any_snapshot_review_item_or_status_change(
    p3c_client, p3c_engine, tenant,
):
    period = await _open_period_with_source(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]
    before = _counts(tenant, period_id)
    assert before["status"] == "Open"

    response = await p3c_client.patch(
        f"/payroll/periods/{period_id}/status", json={"status": "InReview"},
        headers=tenant.admin)
    _assert_gate(response)
    assert _counts(tenant, period_id) == before

    # The live operational surfaces keep working on the refused period.
    preview = await p3c_client.get(
        f"/payroll/periods/{period_id}/calculation-preview", headers=tenant.admin)
    assert preview.status_code == 200
    assert preview.json()["has_blockers"] is False


async def test_workflow_capabilities_match_the_backend_refusal(p3c_client, p3c_engine, tenant):
    period = await _open_period_with_source(p3c_client, p3c_engine, tenant)
    hub = (await p3c_client.get("/payroll/current", headers=tenant.admin)).json()
    branch = next(b for b in hub["branches"] if b["branch_id"] == tenant.branch_a)
    capability = branch["capabilities"]["periods"][str(period["payroll_period_id"])]
    assert capability["can_submit_for_review"]["allowed"] is False
    assert capability["can_submit_for_review"]["reason_code"] == CODE
    # An unrelated refusal keeps its own, more specific reason.
    assert capability["can_resubmit_returned"]["reason_code"] == "PERIOD_NOT_RETURNED"


async def test_resubmit_is_refused_without_touching_the_returned_period(
    p3c_client, p3c_engine, tenant,
):
    period = await _open_period_with_source(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]
    force_status(tenant, period_id, "Returned")
    before = _counts(tenant, period_id)
    assert before["status"] == "Returned"

    _assert_gate(await p3c_client.post(
        f"/payroll/periods/{period_id}/resubmissions", json={}, headers=tenant.admin))
    assert _counts(tenant, period_id) == before

    hub = (await p3c_client.get("/payroll/current", headers=tenant.admin)).json()
    branch = next(b for b in hub["branches"] if b["branch_id"] == tenant.branch_a)
    capability = branch["capabilities"]["periods"][str(period_id)]["can_resubmit_returned"]
    assert capability["allowed"] is False and capability["reason_code"] == CODE


async def test_finalize_and_its_preview_are_refused_without_a_financial_write(
    p3c_client, p3c_engine, tenant,
):
    period = await _open_period_with_source(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]
    force_status(tenant, period_id, "Approved")
    before = _counts(tenant, period_id)
    assert before["status"] == "Approved"

    _assert_gate(await p3c_client.post(
        f"/payroll/periods/{period_id}/finalize", headers=tenant.admin))
    _assert_gate(await p3c_client.get(
        f"/payroll/periods/{period_id}/finalization-preview", headers=tenant.admin))
    assert _counts(tenant, period_id) == before
    assert before["final_lines"] == 0


async def test_a_period_in_the_wrong_status_keeps_its_ordinary_refusal(
    p3c_client, p3c_engine, tenant,
):
    """The gate sits after status and permission checks: it never masks them."""
    period = await _open_period_with_source(p3c_client, p3c_engine, tenant)
    response = await p3c_client.post(
        f"/payroll/periods/{period['payroll_period_id']}/finalize", headers=tenant.admin)
    assert response.status_code == 422
    assert "Only Approved periods" in response.text
