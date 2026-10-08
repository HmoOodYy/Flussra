"""Ordinary target source rows: identity, validation, audit and the Day Grid contract."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.p3b_fixtures import p3b_cursor, p3b_database  # noqa: F401 - register fixtures
from tests.p3c_fixtures import (  # noqa: F401 - register fixtures
    create_definition,
    grant_applicability,
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.p4b_fixtures import (
    add_driver,
    assign_weekly_setup,
    build_payroll_tenant,
    create_period,
    rated_definition,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _setup(client, engine, tenant, **definition_kwargs) -> tuple[dict, int, str]:
    await rated_definition(client, tenant, **definition_kwargs)
    await assign_weekly_setup(engine, tenant, tenant.branch_a)
    period = await create_period(client, tenant, tenant.branch_a)
    grid = (await client.get(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid", headers=tenant.admin)).json()
    return period, grid["columns"][0]["payroll_period_definition_id"], period["start_date"]


def _lines_url(period: dict) -> str:
    return f"/payroll/periods/{period['payroll_period_id']}/lines"


def _body(tenant, ppd: int, work_date: str, quantity="5", **extra) -> dict:
    return {"driver_id": tenant.driver_a, "work_date": work_date,
            "payroll_period_definition_id": ppd, "quantity": quantity, **extra}


async def test_a_line_is_identified_by_its_period_definition_and_stores_no_money(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, start = await _setup(p3c_client, p3c_engine, tenant, definition_code="ITEM_ALPHA")
    response = await p3c_client.post(_lines_url(period), json=_body(tenant, ppd, start),
                                     headers=tenant.admin)
    assert response.status_code == 201, response.text
    line = response.json()
    assert line["payroll_period_definition_id"] == ppd
    assert line["line_type"] is None
    assert line["rate_amount"] is None and line["calculated_amount"] is None
    async with p3c_engine.connect() as conn:
        stored = (await conn.execute(text(
            "SELECT linetype, rateamount, calculatedamount, needsmanagerreview "
            "FROM payroll.payrolldraftlines WHERE draftlineid = :i"),
            {"i": line["draft_line_id"]})).one()
    assert tuple(stored) == (None, None, None, False)


async def test_money_and_review_flags_are_not_accepted_as_input(p3c_client, p3c_engine, tenant):
    period, ppd, start = await _setup(p3c_client, p3c_engine, tenant)
    for forbidden in ({"rate_amount": "9"}, {"calculated_amount": "9"},
                      {"needs_manager_review": True}, {"line_type": "HOURS"}):
        response = await p3c_client.post(
            _lines_url(period), json=_body(tenant, ppd, start, **forbidden), headers=tenant.admin)
        assert response.status_code == 422, (forbidden, response.text)
    created = await p3c_client.post(
        _lines_url(period), json=_body(tenant, ppd, start), headers=tenant.admin)
    line_id = created.json()["draft_line_id"]
    for forbidden in ({"rate_amount": "9"}, {"needs_manager_review": False}):
        response = await p3c_client.patch(
            f"{_lines_url(period)}/{line_id}", json=forbidden, headers=tenant.admin)
        assert response.status_code == 422, (forbidden, response.text)
    system = await p3c_client.post(
        _lines_url(period), json=_body(tenant, ppd, start, source_type="System"),
        headers=tenant.admin)
    assert system.status_code == 422


async def test_one_active_line_per_driver_date_and_definition(p3c_client, p3c_engine, tenant):
    period, ppd, start = await _setup(p3c_client, p3c_engine, tenant)
    other_day = (date.fromisoformat(start) + timedelta(days=1)).isoformat()
    first = await p3c_client.post(
        _lines_url(period), json=_body(tenant, ppd, start), headers=tenant.admin)
    assert first.status_code == 201
    duplicate = await p3c_client.post(
        _lines_url(period), json=_body(tenant, ppd, start), headers=tenant.admin)
    assert duplicate.status_code == 422 and "already exists" in duplicate.text
    assert (await p3c_client.post(
        _lines_url(period), json=_body(tenant, ppd, other_day), headers=tenant.admin
    )).status_code == 201
    other_driver = add_driver(tenant)
    # The same definition and day for a different driver is a different fact.
    await p3c_client.get(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid", headers=tenant.admin)
    assert other_driver != tenant.driver_a


async def test_a_voided_line_frees_its_slot_and_update_changes_only_the_quantity(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, start = await _setup(p3c_client, p3c_engine, tenant)
    created = (await p3c_client.post(
        _lines_url(period), json=_body(tenant, ppd, start, "3"), headers=tenant.admin)).json()
    line_url = f"{_lines_url(period)}/{created['draft_line_id']}"
    updated = await p3c_client.patch(line_url, json={"quantity": "4", "notes": "n"},
                                     headers=tenant.admin)
    assert updated.status_code == 200 and Decimal(updated.json()["quantity"]) == Decimal("4")
    assert (await p3c_client.delete(line_url, headers=tenant.admin)).status_code in (200, 204)
    assert (await p3c_client.delete(line_url, headers=tenant.admin)).status_code in (200, 204)
    again = await p3c_client.post(
        _lines_url(period), json=_body(tenant, ppd, start, "6"), headers=tenant.admin)
    assert again.status_code == 201


async def test_a_definition_of_another_period_is_not_usable(p3c_client, p3c_engine, tenant):
    period, ppd, start = await _setup(p3c_client, p3c_engine, tenant)
    unknown = await p3c_client.post(
        _lines_url(period), json=_body(tenant, ppd + 1000, start), headers=tenant.admin)
    assert unknown.status_code == 422
    assert "not part of this payroll period" in unknown.text


async def test_an_inactive_definition_cannot_receive_source(p3c_client, p3c_engine, tenant):
    inactive = await create_definition(p3c_client, tenant, definition_code="OFF_HERE")
    grant_applicability(tenant, inactive, tenant.branch_a, active=False)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    period = await create_period(p3c_client, tenant, tenant.branch_a)
    async with p3c_engine.connect() as conn:
        ppd = (await conn.execute(text(
            "SELECT payrollperioddefinitionid FROM payroll.payrollperioddefinitions "
            "WHERE payrollperiodid = :p"), {"p": period["payroll_period_id"]})).scalar_one()
    response = await p3c_client.post(
        _lines_url(period), json=_body(tenant, ppd, period["start_date"]), headers=tenant.admin)
    assert response.status_code == 422
    assert "was not active for this branch" in response.text


async def test_work_dates_outside_the_period_are_rejected(p3c_client, p3c_engine, tenant):
    period, ppd, start = await _setup(p3c_client, p3c_engine, tenant)
    before = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
    after = (date.fromisoformat(period["end_date"]) + timedelta(days=1)).isoformat()
    for work_date in (before, after):
        response = await p3c_client.post(
            _lines_url(period), json=_body(tenant, ppd, work_date), headers=tenant.admin)
        assert response.status_code == 400, response.text


async def test_whole_number_definitions_reject_fractions_on_the_line_api(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, start = await _setup(
        p3c_client, p3c_engine, tenant, input_type="WholeNumber", definition_code="STOPS")
    fraction = await p3c_client.post(
        _lines_url(period), json=_body(tenant, ppd, start, "2.5"), headers=tenant.admin)
    assert fraction.status_code == 422 and "whole number" in fraction.text.lower()
    whole = await p3c_client.post(
        _lines_url(period), json=_body(tenant, ppd, start, "2"), headers=tenant.admin)
    assert whole.status_code == 201, whole.text
    update = await p3c_client.patch(
        f"{_lines_url(period)}/{whole.json()['draft_line_id']}", json={"quantity": "2.5"},
        headers=tenant.admin)
    assert update.status_code == 422


async def test_source_mutations_write_audit_and_audit_failure_rolls_the_line_back(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    period, ppd, start = await _setup(p3c_client, p3c_engine, tenant)
    created = (await p3c_client.post(
        _lines_url(period), json=_body(tenant, ppd, start), headers=tenant.admin)).json()
    line_id = created["draft_line_id"]
    await p3c_client.delete(f"{_lines_url(period)}/{line_id}", headers=tenant.admin)
    async with p3c_engine.connect() as conn:
        audited = dict((await conn.execute(text("""
            SELECT actioncode, count(*) FROM audit.auditlog
            WHERE entityid = :e AND actioncode IN ('DRAFT_LINE_ADDED', 'DRAFT_LINE_VOIDED')
            GROUP BY actioncode"""), {"e": str(line_id)})).all())
        evidence = (await conn.execute(text("""
            SELECT actioncode, afterstatejson ->> 'payroll_period_definition_id', payitemid
            FROM payroll.payrollperiodauditevidenceevents
            WHERE sourceentitytype = 'PayrollDraftLines' AND sourceentityid = :e
            ORDER BY payrollperiodauditevidenceeventid"""), {"e": str(line_id)})).all()
    assert audited == {"DRAFT_LINE_ADDED": 1, "DRAFT_LINE_VOIDED": 1}
    # The evidence carries the target identity and never a fabricated PayItem.
    assert [(r[0], r[1], r[2]) for r in evidence] == [
        ("SOURCE_CREATED", str(ppd), None), ("SOURCE_VOIDED", str(ppd), None)]

    async def failing_audit(*_args, **_kwargs):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr("app.payroll.draft_line_mutation._write_line_audit", failing_audit)
    next_day = (date.fromisoformat(start) + timedelta(days=2)).isoformat()
    with pytest.raises(RuntimeError):
        await p3c_client.post(
            _lines_url(period), json=_body(tenant, ppd, next_day), headers=tenant.admin)
    async with p3c_engine.connect() as conn:
        count = (await conn.execute(text(
            "SELECT count(*) FROM payroll.payrolldraftlines "
            "WHERE payrollperiodid = :p AND workdate = :d"),
            {"p": period["payroll_period_id"], "d": date.fromisoformat(next_day)})).scalar_one()
    assert count == 0


async def test_the_day_grid_is_keyed_by_definition_identity_and_idempotent(
    p3c_client, p3c_engine, tenant,
):
    await rated_definition(p3c_client, tenant, definition_code="ZZZ_LAST", definition_name="Zed")
    await rated_definition(p3c_client, tenant, definition_code="AAA_FIRST", definition_name="Aye")
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    period = await create_period(p3c_client, tenant, tenant.branch_a)
    url = f"/payroll/periods/{period['payroll_period_id']}/day-grid"
    grid = (await p3c_client.get(url, headers=tenant.admin)).json()
    columns = grid["columns"]
    assert [c["label"] for c in columns] == ["Aye", "Zed"]
    assert set(columns[0]) >= {"payroll_period_definition_id", "pay_definition_id",
                               "definition_code", "label", "input_type", "unit",
                               "calculation_method"}
    ids = [c["payroll_period_definition_id"] for c in columns]
    payload = {"work_date": grid["work_date"], "rows": [
        {"driver_id": tenant.driver_a, "values": {str(ids[0]): "4", str(ids[1]): "6"}}]}
    first = await p3c_client.post(url, json=payload, headers=tenant.admin)
    second = await p3c_client.post(url, json=payload, headers=tenant.admin)
    assert first.status_code == second.status_code == 200
    values = second.json()["rows"][0]["values"]
    assert {k: Decimal(v["quantity"]) for k, v in values.items()} == {
        str(ids[0]): Decimal("4"), str(ids[1]): Decimal("6")}
    # Codes do not route a save.
    by_code = await p3c_client.post(
        url, json={"work_date": grid["work_date"], "rows": [
            {"driver_id": tenant.driver_a, "values": {"AAA_FIRST": "1"}}]},
        headers=tenant.admin)
    assert by_code.status_code == 422
    async with p3c_engine.connect() as conn:
        lines = (await conn.execute(text(
            "SELECT count(*) FROM payroll.payrolldraftlines WHERE payrollperiodid = :p"),
            {"p": period["payroll_period_id"]})).scalar_one()
    assert lines == 2


async def test_clearing_a_cell_voids_the_line(p3c_client, p3c_engine, tenant):
    period, ppd, start = await _setup(p3c_client, p3c_engine, tenant)
    url = f"/payroll/periods/{period['payroll_period_id']}/day-grid"
    base = {"work_date": start}
    await p3c_client.post(
        url, json={**base, "rows": [{"driver_id": tenant.driver_a, "values": {str(ppd): "5"}}]},
        headers=tenant.admin)
    cleared = await p3c_client.post(
        url, json={**base, "rows": [{"driver_id": tenant.driver_a, "values": {str(ppd): ""}}]},
        headers=tenant.admin)
    assert cleared.status_code == 200
    assert cleared.json()["rows"][0]["values"] == {}
    lines = (await p3c_client.get(_lines_url(period), headers=tenant.admin)).json()
    assert [line["status"] for line in lines] == ["Void"] or lines == []


async def test_draft_periods_accept_source_but_show_no_money(p3c_client, p3c_engine, tenant):
    await rated_definition(p3c_client, tenant, rate="25")
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    await create_period(p3c_client, tenant, tenant.branch_a)  # Open slot
    prepared = await create_period(
        p3c_client, tenant, tenant.branch_a, mode="PREPARED_CREATION")
    assert prepared["status"] == "Draft"
    grid = (await p3c_client.get(
        f"/payroll/periods/{prepared['payroll_period_id']}/day-grid", headers=tenant.admin)).json()
    ppd = grid["columns"][0]["payroll_period_definition_id"]
    saved = await p3c_client.post(
        f"/payroll/periods/{prepared['payroll_period_id']}/day-grid",
        json={"work_date": grid["work_date"], "rows": [
            {"driver_id": tenant.driver_a, "values": {str(ppd): "8"}}]},
        headers=tenant.admin)
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["summary"]["financials_available"] is False
    assert body["summary"]["gross_total"] is None
    cell = body["rows"][0]["values"][str(ppd)]
    assert cell["calculated_amount"] is None and cell["calculation_status"] is None
