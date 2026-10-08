"""Source-mutation status guards and workflow-slot invariants that survive P4b.

Ports the still-live contracts of the retired CP-0A / CP-1A / CP-2F suites to the target
period model: which period statuses accept source writes, what a rejected write leaves
behind, the reserved direct transitions, the Returned-period review decisions and the
database workflow invariants. Obsolete PayItem authority and submit-driven success paths
(P4c) are deliberately not covered here.
"""
from __future__ import annotations

from datetime import date, timedelta

import psycopg2
import pytest

from tests.p3b_fixtures import p3b_cursor, p3b_database  # noqa: F401 - register fixtures
from tests.p3c_fixtures import (  # noqa: F401 - register fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.p4b_fixtures import (
    add_inreview_item,
    assign_weekly_setup,
    build_payroll_tenant,
    create_period,
    force_status,
    get_grid,
    make_user,
    open_period_with_grid,
    query,
    rated_definition,
)

pytestmark = pytest.mark.asyncio

REJECTING = ["InReview", "Approved", "Locked", "Archived", "Cancelled"]
ACCEPTING = ["Open", "Returned"]


@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


def _lines(period) -> str:
    return f"/payroll/periods/{period['payroll_period_id']}/lines"


def _body(tenant, ppd, work_date, quantity="5") -> dict:
    return {"driver_id": tenant.driver_a, "work_date": work_date,
            "payroll_period_definition_id": ppd, "quantity": quantity}


def _audit_count(tenant, period_id) -> int:
    return query(tenant, """
        SELECT count(*) FROM audit.auditlog
        WHERE entityname = 'PayrollDraftLines'
          AND entityid IN (SELECT draftlineid::text FROM payroll.payrolldraftlines
                           WHERE payrollperiodid = %s)""", (period_id,))[0][0]


def _line_state(tenant, period_id):
    return query(tenant, """
        SELECT draftlineid, quantity, status FROM payroll.payrolldraftlines
        WHERE payrollperiodid = %s ORDER BY draftlineid""", (period_id,))


async def _source_line(client, tenant, period, ppd, start):
    created = await client.post(_lines(period), json=_body(tenant, ppd, start),
                                headers=tenant.admin)
    assert created.status_code == 201, created.text
    return created.json()["draft_line_id"]


# ---------------------------------------------------------------------------
# Which statuses accept ordinary source entry
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", REJECTING)
async def test_source_writes_are_rejected_outside_the_editable_statuses_and_leave_nothing(
    p3c_client, p3c_engine, tenant, status,
):
    period, grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]
    ppd = grid["columns"][0]["payroll_period_definition_id"]
    start = period["start_date"]
    line_id = await _source_line(p3c_client, tenant, period, ppd, start)
    force_status(tenant, period_id, status)
    before_state, before_audit = _line_state(tenant, period_id), _audit_count(tenant, period_id)

    other_day = (date.fromisoformat(start) + timedelta(days=1)).isoformat()
    add = await p3c_client.post(_lines(period), json=_body(tenant, ppd, other_day),
                                headers=tenant.admin)
    update = await p3c_client.patch(f"{_lines(period)}/{line_id}", json={"quantity": "9"},
                                    headers=tenant.admin)
    void = await p3c_client.delete(f"{_lines(period)}/{line_id}", headers=tenant.admin)
    grid_save = await p3c_client.post(
        f"/payroll/periods/{period_id}/day-grid",
        json={"work_date": other_day, "rows": [
            {"driver_id": tenant.driver_a, "values": {str(ppd): "3"}}]},
        headers=tenant.admin)
    for response in (add, update, void, grid_save):
        assert response.status_code in (403, 409, 422), (status, response.status_code, response.text)

    # A rejected write leaves the rows untouched and writes no audit.
    assert _line_state(tenant, period_id) == before_state
    assert _audit_count(tenant, period_id) == before_audit


@pytest.mark.parametrize("status", ACCEPTING)
async def test_open_and_returned_periods_accept_source_add_update_and_void(
    p3c_client, p3c_engine, tenant, status,
):
    period, grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]
    ppd = grid["columns"][0]["payroll_period_definition_id"]
    start = period["start_date"]
    if status != "Open":
        force_status(tenant, period_id, status)
    line_id = await _source_line(p3c_client, tenant, period, ppd, start)
    assert (await p3c_client.patch(f"{_lines(period)}/{line_id}", json={"quantity": "7"},
                                   headers=tenant.admin)).status_code == 200
    assert (await p3c_client.delete(f"{_lines(period)}/{line_id}",
                                    headers=tenant.admin)).status_code in (200, 204)
    assert _line_state(tenant, period_id)[0][2] == "Void"


async def test_a_returned_period_stays_editable_while_an_inreview_period_is_frozen(
    p3c_client, p3c_engine, tenant,
):
    period, grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    ppd = grid["columns"][0]["payroll_period_definition_id"]
    force_status(tenant, period["payroll_period_id"], "InReview")
    frozen = await p3c_client.post(
        _lines(period), json=_body(tenant, ppd, period["start_date"]), headers=tenant.admin)
    assert frozen.status_code in (409, 422)
    force_status(tenant, period["payroll_period_id"], "Returned")
    editable = await p3c_client.post(
        _lines(period), json=_body(tenant, ppd, period["start_date"]), headers=tenant.admin)
    assert editable.status_code == 201


# ---------------------------------------------------------------------------
# Prepared (Draft) periods are operational-only
# ---------------------------------------------------------------------------

async def _draft_period(client, engine, tenant) -> tuple[dict, int]:
    await rated_definition(client, tenant, rate="25")
    await assign_weekly_setup(engine, tenant, tenant.branch_a)
    await create_period(client, tenant, tenant.branch_a)             # the Open slot
    draft = await create_period(client, tenant, tenant.branch_a, mode="PREPARED_CREATION")
    assert draft["status"] == "Draft"
    grid = await get_grid(client, tenant, draft)
    return draft, grid["columns"][0]["payroll_period_definition_id"]


async def test_a_prepared_period_takes_source_but_never_stores_or_shows_money(
    p3c_client, p3c_engine, tenant,
):
    draft, ppd = await _draft_period(p3c_client, p3c_engine, tenant)
    period_id = draft["payroll_period_id"]
    created = await p3c_client.post(
        _lines(draft), json=_body(tenant, ppd, draft["start_date"], "8"), headers=tenant.admin)
    assert created.status_code == 201, created.text
    line = created.json()
    assert line["rate_amount"] is None and line["calculated_amount"] is None
    assert line["needs_manager_review"] is False
    stored = query(tenant, "SELECT rateamount, calculatedamount, needsmanagerreview "
                           "FROM payroll.payrolldraftlines WHERE payrollperiodid = %s",
                   (period_id,))
    assert stored == [(None, None, False)]
    listed = (await p3c_client.get(_lines(draft), headers=tenant.admin)).json()
    assert [(row["rate_amount"], row["calculated_amount"]) for row in listed] == [(None, None)]
    update = await p3c_client.patch(
        f"{_lines(draft)}/{line['draft_line_id']}", json={"quantity": "3"}, headers=tenant.admin)
    assert update.status_code == 200 and update.json()["calculated_amount"] is None
    assert (await p3c_client.delete(f"{_lines(draft)}/{line['draft_line_id']}",
                                    headers=tenant.admin)).status_code in (200, 204)


async def test_a_prepared_period_blocks_every_financial_surface(p3c_client, p3c_engine, tenant):
    draft, ppd = await _draft_period(p3c_client, p3c_engine, tenant)
    period_id = draft["payroll_period_id"]
    admin = tenant.admin

    grid = await get_grid(p3c_client, tenant, draft)
    assert grid["summary"]["gross_total"] is None
    assert grid["summary"]["financials_available"] is False

    submit = await p3c_client.patch(
        f"/payroll/periods/{period_id}/status", json={"status": "InReview"}, headers=admin)
    assert submit.status_code == 422                                  # invalid transition
    for url in (f"/payroll/periods/{period_id}/finalization-preview",):
        assert (await p3c_client.get(url, headers=admin)).status_code == 422
    assert (await p3c_client.post(
        f"/payroll/periods/{period_id}/finalize", headers=admin)).status_code == 422
    assert (await p3c_client.get(
        f"/payroll/periods/{period_id}/lines/summary", headers=admin)).status_code == 422
    assert (await p3c_client.get(
        f"/payroll/periods/{period_id}/eligible-drivers", headers=admin)).status_code == 422
    assert (await p3c_client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": tenant.driver_a, "amount": "10"}, headers=admin
    )).status_code in (409, 422)


async def test_workflow_capabilities_of_a_prepared_period(p3c_client, p3c_engine, tenant):
    draft, _ = await _draft_period(p3c_client, p3c_engine, tenant)
    hub = (await p3c_client.get("/payroll/current", headers=tenant.admin)).json()
    branch = next(b for b in hub["branches"] if b["branch_id"] == tenant.branch_a)
    capability = branch["capabilities"]["periods"][str(draft["payroll_period_id"])]
    assert capability["can_open_day_grid"]["allowed"] is True
    assert capability["can_enter_source"]["allowed"] is True
    assert capability["can_submit_for_review"]["allowed"] is False
    viewer = make_user(tenant, ["payroll.view"])
    hub = (await p3c_client.get("/payroll/current", headers=viewer)).json()
    branch = next(b for b in hub["branches"] if b["branch_id"] == tenant.branch_a)
    assert branch["capabilities"]["periods"][str(draft["payroll_period_id"])][
        "can_enter_source"]["allowed"] is False


# ---------------------------------------------------------------------------
# Reserved direct transitions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target", ["Returned", "Approved", "Locked"])
async def test_reserved_statuses_cannot_be_reached_by_a_direct_patch(
    p3c_client, p3c_engine, tenant, target,
):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    response = await p3c_client.patch(
        f"/payroll/periods/{period['payroll_period_id']}/status", json={"status": target},
        headers=tenant.admin)
    assert response.status_code == 422, response.text
    assert query(tenant, "SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = %s",
                 (period["payroll_period_id"],)) == [("Open",)]


@pytest.mark.parametrize("status", ["InReview", "Returned"])
async def test_inreview_and_returned_periods_cannot_be_cancelled_directly(
    p3c_client, p3c_engine, tenant, status,
):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    force_status(tenant, period["payroll_period_id"], status)
    response = await p3c_client.patch(
        f"/payroll/periods/{period['payroll_period_id']}/status", json={"status": "Cancelled"},
        headers=tenant.admin)
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# Returned lifecycle: review decisions that remain available without evidence
# ---------------------------------------------------------------------------

async def _inreview_with_item(client, engine, tenant) -> tuple[dict, int]:
    period, _ = await open_period_with_grid(client, engine, tenant)
    force_status(tenant, period["payroll_period_id"], "InReview")
    return period, add_inreview_item(tenant, period["payroll_period_id"], requested_by=tenant.owner)


@pytest.mark.parametrize("decision", ["Rejected", "EditRequested"])
async def test_a_return_decision_needs_a_reason_and_returns_the_period(
    p3c_client, p3c_engine, tenant, decision,
):
    period, item = await _inreview_with_item(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]
    for body in ({"decision": decision}, {"decision": decision, "decision_reason": "   "}):
        blank = await p3c_client.post(f"/review/items/{item}/decide", json=body,
                                      headers=tenant.admin)
        assert blank.status_code == 422, blank.text
    assert query(tenant, "SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = %s",
                 (period_id,)) == [("InReview",)]

    decided = await p3c_client.post(
        f"/review/items/{item}/decide",
        json={"decision": decision, "decision_reason": "Please correct the quantities."},
        headers=tenant.admin)
    assert decided.status_code == 200, decided.text
    status, pointer = query(
        tenant, "SELECT status, currentreturnreviewitemid FROM payroll.payrollperiods "
                "WHERE payrollperiodid = %s", (period_id,))[0]
    assert status == "Returned" and pointer == item
    # The Returned period is visible and editable again.
    assert (await p3c_client.get(f"/payroll/periods/{period_id}",
                                 headers=tenant.admin)).json()["status"] == "Returned"


async def test_approval_without_submitted_evidence_fails_closed_and_changes_nothing(
    p3c_client, p3c_engine, tenant,
):
    period, item = await _inreview_with_item(p3c_client, p3c_engine, tenant)
    response = await p3c_client.post(
        f"/review/items/{item}/decide", json={"decision": "Approved", "decision_reason": "ok"},
        headers=tenant.admin)
    assert response.status_code >= 400
    assert query(tenant, "SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = %s",
                 (period["payroll_period_id"],)) == [("InReview",)]
    assert query(tenant, "SELECT status FROM review.managerreviewitems WHERE reviewitemid = %s",
                 (item,)) == [("Pending",)]
    assert query(tenant, "SELECT count(*) FROM payroll.payrollfinallines "
                         "WHERE payrollperiodid = %s", (period["payroll_period_id"],)) == [(0,)]


async def test_period_review_decisions_enforce_permission_and_branch_scope(
    p3c_client, p3c_engine, tenant,
):
    period, item = await _inreview_with_item(p3c_client, p3c_engine, tenant)
    body = {"decision": "EditRequested", "decision_reason": "Please correct."}
    nobody = make_user(tenant, [])
    assert (await p3c_client.post(f"/review/items/{item}/decide", json=body,
                                  headers=nobody)).status_code == 403
    elsewhere = make_user(tenant, ["review.decide", "payroll.view"],
                          scope="SpecificBranch", branch_id=tenant.branch_b)
    assert (await p3c_client.post(f"/review/items/{item}/decide", json=body,
                                  headers=elsewhere)).status_code in (403, 404)
    assert query(tenant, "SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = %s",
                 (period["payroll_period_id"],)) == [("InReview",)]
    listed = await p3c_client.get("/review/items", params={"status": "Pending"},
                                  headers=elsewhere)
    assert item not in [row["review_item_id"] for row in listed.json()]


# ---------------------------------------------------------------------------
# Database workflow invariants
# ---------------------------------------------------------------------------

async def test_at_most_one_returned_and_one_inreview_period_per_branch(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    second = await create_period(p3c_client, tenant, tenant.branch_a, mode="PREPARED_CREATION")
    for status in ("Returned", "InReview"):
        force_status(tenant, period["payroll_period_id"], status)
        with pytest.raises(psycopg2.errors.UniqueViolation):
            force_status(tenant, second["payroll_period_id"], status)
        # restore a status the next round can use
        force_status(tenant, period["payroll_period_id"], "Open")
        force_status(tenant, second["payroll_period_id"], "Draft")


async def test_the_returned_pointer_is_required_exactly_for_returned_periods(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]
    with pytest.raises(psycopg2.errors.CheckViolation):
        query(tenant, "UPDATE payroll.payrollperiods SET status = 'Returned' "
                      "WHERE payrollperiodid = %s", (period_id,))
    with pytest.raises(psycopg2.errors.CheckViolation):
        query(tenant, "UPDATE payroll.payrollperiods SET status = 'Open', "
                      "currentreturnreviewitemid = 1 WHERE payrollperiodid = %s", (period_id,))
    with pytest.raises(psycopg2.errors.CheckViolation):
        query(tenant, "UPDATE payroll.payrollperiods SET status = 'NotAStatus' "
                      "WHERE payrollperiodid = %s", (period_id,))


async def test_the_workflow_slot_indexes_and_pointer_constraints_exist(
    p3c_client, p3c_engine, tenant,
):
    await open_period_with_grid(p3c_client, p3c_engine, tenant)
    indexes = {row[0]: row[1] for row in query(
        tenant, "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'payroll' "
                "AND tablename = 'payrollperiods'")}
    returned = indexes["ux_payrollperiods_onereturnedperbranch"].lower()
    in_review = indexes["ux_payrollperiods_oneinreviewperbranch"].lower()
    assert "unique" in returned and "returned" in returned
    assert "unique" in in_review and "inreview" in in_review
    assert "ux_payrollperiods_oneopenperbranch" in indexes
    constraints = {row[0] for row in query(
        tenant, "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'payroll.payrollperiods'::regclass")}
    assert "ck_payrollperiods_returnedpointerconsistency" in constraints
    pointer = query(tenant, """
        SELECT pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conrelid = 'payroll.payrollperiods'::regclass AND contype = 'f'
          AND pg_get_constraintdef(oid) ILIKE '%%currentreturnreviewitemid%%'""")
    assert pointer, "the Returned pointer must be a composite foreign key"
