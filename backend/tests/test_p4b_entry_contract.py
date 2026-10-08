"""Source-line (entry) API contract that survives the P4b target cutover.

Listing and filtering, the live per-driver summary, input validation, driver eligibility
on the write paths and void semantics. Identity, forbidden money inputs and audit are in
test_p4b_source_lines; status guards are in test_p4b_source_mutation_guards.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.p3b_fixtures import p3b_cursor, p3b_database  # noqa: F401 - register fixtures
from tests.p3c_fixtures import (  # noqa: F401 - register fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.p4b_fixtures import (
    add_driver,
    assign_weekly_setup,
    build_payroll_tenant,
    create_period,
    force_status,
    get_grid,
    make_driver_self_user,
    make_user,
    query,
    rated_definition,
    set_employment,
    setup_anchor,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


def _lines(period) -> str:
    return f"/payroll/periods/{period['payroll_period_id']}/lines"


def _day(period, offset=0) -> str:
    return (date.fromisoformat(period["start_date"]) + timedelta(days=offset)).isoformat()


def _body(driver_id, ppd, work_date, quantity="5", **extra) -> dict:
    return {"driver_id": driver_id, "work_date": work_date,
            "payroll_period_definition_id": ppd, "quantity": quantity, **extra}


async def _two_definition_period(client, engine, tenant):
    await rated_definition(client, tenant, rate="10", definition_code="ALPHA",
                           definition_name="Alpha")
    await rated_definition(client, tenant, rate="4", definition_code="BETA",
                           definition_name="Beta")
    await assign_weekly_setup(engine, tenant, tenant.branch_a)
    period = await create_period(client, tenant, tenant.branch_a)
    grid = await get_grid(client, tenant, period)
    ids = {c["definition_code"]: c["payroll_period_definition_id"] for c in grid["columns"]}
    return period, ids


async def _add(client, tenant, period, driver_id, ppd, offset, quantity="5"):
    response = await client.post(
        _lines(period), json=_body(driver_id, ppd, _day(period, offset), quantity),
        headers=tenant.admin)
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Listing, filtering and the live summary
# ---------------------------------------------------------------------------

async def test_listing_requires_authentication_and_a_known_period(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await _two_definition_period(p3c_client, p3c_engine, tenant)
    assert (await p3c_client.get(_lines(period))).status_code in (401, 403)
    assert (await p3c_client.get(
        "/payroll/periods/987654/lines", headers=tenant.admin)).status_code == 404
    assert (await p3c_client.get(_lines(period), headers=tenant.admin)).json() == []
    assert (await p3c_client.get(
        "/payroll/periods/987654/lines/summary", headers=tenant.admin)).status_code == 404


async def test_lines_filter_by_driver_work_date_and_status(p3c_client, p3c_engine, tenant):
    period, ids = await _two_definition_period(p3c_client, p3c_engine, tenant)
    first = await _add(p3c_client, tenant, period, tenant.driver_a, ids["ALPHA"], 0)
    await _add(p3c_client, tenant, period, tenant.driver_a2, ids["ALPHA"], 0)
    await _add(p3c_client, tenant, period, tenant.driver_a, ids["BETA"], 1)
    await p3c_client.delete(f"{_lines(period)}/{first['draft_line_id']}", headers=tenant.admin)

    def listed(**params):
        return p3c_client.get(_lines(period), params=params, headers=tenant.admin)

    assert len((await listed()).json()) == 3
    assert {line["driver_id"] for line in (await listed(driver_id=tenant.driver_a2)).json()} \
        == {tenant.driver_a2}
    assert len((await listed(work_date=_day(period, 1))).json()) == 1
    assert [line["status"] for line in (await listed(status="Void")).json()] == ["Void"]
    assert {line["status"] for line in (await listed(status="Active")).json()} == {"Active"}
    assert (await listed(driver_id=tenant.driver_b)).json() == []


async def test_the_summary_aggregates_live_money_per_driver_and_definition_without_voids(
    p3c_client, p3c_engine, tenant,
):
    period, ids = await _two_definition_period(p3c_client, p3c_engine, tenant)
    await _add(p3c_client, tenant, period, tenant.driver_a, ids["ALPHA"], 0, "2")
    await _add(p3c_client, tenant, period, tenant.driver_a, ids["ALPHA"], 1, "3")
    await _add(p3c_client, tenant, period, tenant.driver_a, ids["BETA"], 0, "5")
    voided = await _add(p3c_client, tenant, period, tenant.driver_a2, ids["ALPHA"], 0, "9")
    await p3c_client.delete(f"{_lines(period)}/{voided['draft_line_id']}", headers=tenant.admin)

    summary = (await p3c_client.get(
        f"{_lines(period)}/summary", headers=tenant.admin)).json()
    by_definition = {
        (row["driver_id"], row["definition_code"]): row for row in summary}
    assert set(by_definition) == {(tenant.driver_a, "ALPHA"), (tenant.driver_a, "BETA")}
    alpha = by_definition[(tenant.driver_a, "ALPHA")]
    assert Decimal(alpha["total_quantity"]) == 5
    assert Decimal(alpha["total_calculated_amount"]) == 50           # 5 x 10
    assert Decimal(by_definition[(tenant.driver_a, "BETA")]["total_calculated_amount"]) == 20
    assert alpha["lines_needing_attention"] == 0


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

async def test_invalid_input_is_rejected_on_create(p3c_client, p3c_engine, tenant):
    period, ids = await _two_definition_period(p3c_client, p3c_engine, tenant)
    ppd = ids["ALPHA"]
    cases = {
        "negative quantity": _body(tenant.driver_a, ppd, _day(period), "-1"),
        "unknown source type": _body(tenant.driver_a, ppd, _day(period), source_type="Bogus"),
        "missing definition": {"driver_id": tenant.driver_a, "work_date": _day(period)},
        "wrong-branch driver": _body(tenant.driver_b, ppd, _day(period)),
        "unknown driver": _body(tenant.driver_b + 5000, ppd, _day(period)),
    }
    for label, body in cases.items():
        response = await p3c_client.post(_lines(period), json=body, headers=tenant.admin)
        assert response.status_code == 422, (label, response.status_code, response.text)
    unknown_period = await p3c_client.post(
        "/payroll/periods/987654/lines", json=_body(tenant.driver_a, ppd, _day(period)),
        headers=tenant.admin)
    assert unknown_period.status_code == 404
    assert query(tenant, "SELECT count(*) FROM payroll.payrolldraftlines "
                         "WHERE payrollperiodid = %s", (period["payroll_period_id"],)) == [(0,)]


async def test_invalid_input_is_rejected_on_update_and_voided_lines_are_immutable(
    p3c_client, p3c_engine, tenant,
):
    period, ids = await _two_definition_period(p3c_client, p3c_engine, tenant)
    line = await _add(p3c_client, tenant, period, tenant.driver_a, ids["ALPHA"], 0)
    url = f"{_lines(period)}/{line['draft_line_id']}"
    assert (await p3c_client.patch(url, json={"quantity": "-2"}, headers=tenant.admin)
            ).status_code == 422
    assert (await p3c_client.patch(url, json={"status": "Nonsense"}, headers=tenant.admin)
            ).status_code == 422
    assert (await p3c_client.patch(f"{_lines(period)}/99999999", json={"quantity": "1"},
                                   headers=tenant.admin)).status_code == 404
    assert (await p3c_client.patch(url, json={"notes": "only notes"}, headers=tenant.admin)
            ).status_code == 200
    assert (await p3c_client.delete(url, headers=tenant.admin)).status_code in (200, 204)
    assert (await p3c_client.patch(url, json={"quantity": "1"}, headers=tenant.admin)
            ).status_code == 422


async def test_void_is_idempotent_and_unknown_lines_are_not_found(
    p3c_client, p3c_engine, tenant,
):
    period, ids = await _two_definition_period(p3c_client, p3c_engine, tenant)
    line = await _add(p3c_client, tenant, period, tenant.driver_a, ids["ALPHA"], 0)
    url = f"{_lines(period)}/{line['draft_line_id']}"
    assert (await p3c_client.delete(url, headers=tenant.admin)).status_code in (200, 204)
    assert (await p3c_client.delete(url, headers=tenant.admin)).status_code in (200, 204)
    assert query(tenant, "SELECT status FROM payroll.payrolldraftlines "
                         "WHERE draftlineid = %s", (line["draft_line_id"],)) == [("Void",)]
    assert (await p3c_client.delete(f"{_lines(period)}/99999999", headers=tenant.admin)
            ).status_code == 404


async def test_a_frozen_period_rejects_void_and_update(p3c_client, p3c_engine, tenant):
    period, ids = await _two_definition_period(p3c_client, p3c_engine, tenant)
    line = await _add(p3c_client, tenant, period, tenant.driver_a, ids["ALPHA"], 0)
    force_status(tenant, period["payroll_period_id"], "Locked")
    url = f"{_lines(period)}/{line['draft_line_id']}"
    assert (await p3c_client.patch(url, json={"quantity": "1"}, headers=tenant.admin)
            ).status_code == 422
    assert (await p3c_client.delete(url, headers=tenant.admin)).status_code == 422
    assert query(tenant, "SELECT status, quantity FROM payroll.payrolldraftlines "
                         "WHERE draftlineid = %s", (line["draft_line_id"],)
                 ) == [("Active", Decimal("5"))]


# ---------------------------------------------------------------------------
# Driver eligibility on the write paths
# ---------------------------------------------------------------------------

async def test_driver_eligibility_is_enforced_by_work_date_on_create(
    p3c_client, p3c_engine, tenant,
):
    start = setup_anchor()
    hired, ended = start + timedelta(days=2), start + timedelta(days=4)
    set_employment(tenant, tenant.driver_a, hire_date=hired, termination_date=ended)
    period, ids = await _two_definition_period(p3c_client, p3c_engine, tenant)
    for offset, accepted in {1: False, 2: True, 4: True, 5: False}.items():
        response = await p3c_client.post(
            _lines(period), json=_body(tenant.driver_a, ids["ALPHA"], _day(period, offset)),
            headers=tenant.admin)
        assert response.status_code == (201 if accepted else 422), (offset, response.text)


async def test_a_retroactively_ended_driver_keeps_editable_source_and_can_always_void(
    p3c_client, p3c_engine, tenant,
):
    period, ids = await _two_definition_period(p3c_client, p3c_engine, tenant)
    line = await _add(p3c_client, tenant, period, tenant.driver_a, ids["ALPHA"], 3)
    set_employment(tenant, tenant.driver_a, termination_date=setup_anchor() + timedelta(days=1),
                   employment_status="Terminated")
    url = f"{_lines(period)}/{line['draft_line_id']}"
    assert (await p3c_client.patch(url, json={"quantity": "6"}, headers=tenant.admin)
            ).status_code in (200, 422)
    assert (await p3c_client.delete(url, headers=tenant.admin)).status_code in (200, 204)
    assert query(tenant, "SELECT status FROM payroll.payrolldraftlines "
                         "WHERE draftlineid = %s", (line["draft_line_id"],)) == [("Void",)]
    # A driver who joined the Branch after the period was frozen is not on its roster.
    late = add_driver(tenant)
    assert (await p3c_client.post(
        _lines(period), json=_body(late, ids["ALPHA"], _day(period, 1)), headers=tenant.admin
    )).status_code == 422


# ---------------------------------------------------------------------------
# Access boundary
# ---------------------------------------------------------------------------

async def test_line_routes_deny_driver_self_accounts_and_enforce_permissions(
    p3c_client, p3c_engine, tenant,
):
    period, ids = await _two_definition_period(p3c_client, p3c_engine, tenant)
    line = await _add(p3c_client, tenant, period, tenant.driver_a, ids["ALPHA"], 0)
    url = f"{_lines(period)}/{line['draft_line_id']}"
    driver = make_driver_self_user(tenant, ["payroll.view", "payroll.entry"])
    viewer = make_user(tenant, ["payroll.view"])
    entry = make_user(tenant, ["payroll.entry"])

    for headers in (driver, make_user(tenant, [])):
        assert (await p3c_client.get(_lines(period), headers=headers)).status_code == 403
    assert (await p3c_client.get(_lines(period), headers=viewer)).status_code == 200
    assert (await p3c_client.get(f"{_lines(period)}/summary", headers=viewer)).status_code == 200
    body = _body(tenant.driver_a, ids["BETA"], _day(period, 2))
    for headers in (driver, viewer):
        assert (await p3c_client.post(_lines(period), json=body, headers=headers)
                ).status_code == 403
        assert (await p3c_client.patch(url, json={"quantity": "1"}, headers=headers)
                ).status_code == 403
        assert (await p3c_client.delete(url, headers=headers)).status_code == 403
    assert (await p3c_client.post(_lines(period), json=body, headers=entry)).status_code == 201
