"""Live target PerUnit calculation: source facts in, derived money out, no stored money."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import psycopg2
import pytest

from tests.p3b_fixtures import p3b_cursor, p3b_database  # noqa: F401 - register fixtures
from tests.p3c_fixtures import (  # noqa: F401 - register fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.p4b_fixtures import (
    approve_rate,
    assign_weekly_setup,
    build_payroll_tenant,
    create_period,
    rated_definition,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _open_period(client, engine, tenant) -> dict:
    await assign_weekly_setup(engine, tenant, tenant.branch_a)
    return await create_period(client, tenant, tenant.branch_a)


def _day(period: dict, offset: int = 0) -> str:
    return (date.fromisoformat(period["start_date"]) + timedelta(days=offset)).isoformat()


async def _save(client, tenant, period, values: dict, *, offset=0, driver_id=None):
    return await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid",
        json={"work_date": _day(period, offset),
              "rows": [{"driver_id": driver_id or tenant.driver_a, "values": values}]},
        headers=tenant.admin)


async def _grid(client, tenant, period, offset=0) -> dict:
    response = await client.get(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid",
        params={"work_date": _day(period, offset)}, headers=tenant.admin)
    assert response.status_code == 200, response.text
    return response.json()


def _cell(grid: dict, definition_id: int, driver_id: int) -> dict:
    row = next(r for r in grid["rows"] if r["driver_id"] == driver_id)
    return row["values"][str(definition_id)]


def _definition_id(grid: dict) -> int:
    return grid["columns"][0]["payroll_period_definition_id"]


# ---------------------------------------------------------------------------
# The canonical scenario
# ---------------------------------------------------------------------------

async def test_eight_units_at_a_rate_of_twenty_five_is_two_hundred(
    p3c_client, p3c_engine, tenant,
):
    await rated_definition(p3c_client, tenant, rate="25", definition_code="ITEM_ALPHA")
    period = await _open_period(p3c_client, p3c_engine, tenant)
    grid = await _grid(p3c_client, tenant, period)
    ppd = _definition_id(grid)

    saved = await _save(p3c_client, tenant, period, {str(ppd): "8"})
    assert saved.status_code == 200, saved.text
    grid = saved.json()
    cell = _cell(grid, ppd, tenant.driver_a)
    assert Decimal(cell["calculated_amount"]) == Decimal("200")
    assert cell["calculation_status"] == "Calculated"
    assert cell["needs_manager_review"] is False
    assert Decimal(grid["summary"]["gross_total"]) == Decimal("200")
    totals = grid["summary"]["quantity_totals"]
    assert [(t["payroll_period_definition_id"], Decimal(t["quantity"])) for t in totals] == [
        (ppd, Decimal("8"))]


async def test_the_work_date_selects_the_rate_inside_one_period(
    p3c_client, p3c_engine, tenant,
):
    definition = await rated_definition(p3c_client, tenant, rate="10", effective_from="2020-01-01")
    period = await _open_period(p3c_client, p3c_engine, tenant)
    await approve_rate(p3c_client, tenant, definition, "20", effective_from=_day(period, 3))
    ppd = _definition_id(await _grid(p3c_client, tenant, period))

    assert (await _save(p3c_client, tenant, period, {str(ppd): "5"}, offset=1)).status_code == 200
    assert (await _save(p3c_client, tenant, period, {str(ppd): "5"}, offset=4)).status_code == 200

    early = _cell(await _grid(p3c_client, tenant, period, 1), ppd, tenant.driver_a)
    late = _cell(await _grid(p3c_client, tenant, period, 4), ppd, tenant.driver_a)
    assert Decimal(early["calculated_amount"]) == Decimal("50")
    assert Decimal(late["calculated_amount"]) == Decimal("100")

    preview = await p3c_client.get(
        f"/payroll/periods/{period['payroll_period_id']}/calculation-preview",
        headers=tenant.admin)
    assert preview.status_code == 200, preview.text
    assert Decimal(preview.json()["drivers"][0]["daily_pay"]) == Decimal("150")


async def test_a_zero_rate_is_valid_and_is_not_a_missing_rate(p3c_client, p3c_engine, tenant):
    await rated_definition(p3c_client, tenant, rate="0")
    period = await _open_period(p3c_client, p3c_engine, tenant)
    ppd = _definition_id(await _grid(p3c_client, tenant, period))
    grid = (await _save(p3c_client, tenant, period, {str(ppd): "7"})).json()
    cell = _cell(grid, ppd, tenant.driver_a)
    assert Decimal(cell["calculated_amount"]) == Decimal("0")
    assert cell["calculation_status"] == "Calculated"
    assert cell["needs_manager_review"] is False
    assert grid["summary"]["needs_attention"] == 0


async def test_a_missing_assignment_is_an_explicit_blocker_never_zero(
    p3c_client, p3c_engine, tenant,
):
    await rated_definition(p3c_client, tenant, rate=None)
    period = await _open_period(p3c_client, p3c_engine, tenant)
    ppd = _definition_id(await _grid(p3c_client, tenant, period))
    grid = (await _save(p3c_client, tenant, period, {str(ppd): "7"})).json()
    cell = _cell(grid, ppd, tenant.driver_a)
    assert cell["calculated_amount"] is None
    assert cell["calculation_status"] == "MissingRate"
    assert cell["needs_manager_review"] is True
    assert grid["summary"]["needs_attention"] == 1

    preview = (await p3c_client.get(
        f"/payroll/periods/{period['payroll_period_id']}/calculation-preview",
        headers=tenant.admin)).json()
    assert preview["has_blockers"] is True
    line = preview["drivers"][0]["lines"][0]
    assert line["calculation_status"] == "MissingRate"
    assert line["needs_manager_review"] is True


async def test_input_type_comes_from_the_frozen_definition(p3c_client, p3c_engine, tenant):
    await rated_definition(
        p3c_client, tenant, definition_code="WHOLE", definition_name="Whole", rate="3",
        input_type="WholeNumber")
    await rated_definition(
        p3c_client, tenant, definition_code="FRACTIONAL", definition_name="Fractional", rate="3",
        input_type="Decimal")
    period = await _open_period(p3c_client, p3c_engine, tenant)
    grid = await _grid(p3c_client, tenant, period)
    by_code = {c["definition_code"]: c["payroll_period_definition_id"] for c in grid["columns"]}

    rejected = await _save(p3c_client, tenant, period, {str(by_code["WHOLE"]): "2.5"})
    assert rejected.status_code == 422
    assert "whole number" in rejected.json()["detail"].lower()
    accepted = await _save(p3c_client, tenant, period, {str(by_code["FRACTIONAL"]): "2.5"})
    assert accepted.status_code == 200, accepted.text
    ok_whole = await _save(p3c_client, tenant, period, {str(by_code["WHOLE"]): "2"})
    assert ok_whole.status_code == 200, ok_whole.text
    zero = await _save(p3c_client, tenant, period, {str(by_code["WHOLE"]): "0"})
    assert zero.status_code == 200


# ---------------------------------------------------------------------------
# Stored DraftLine money is never authority
# ---------------------------------------------------------------------------

def _tamper_money(tenant, period_id: int) -> None:
    """Corrupt the legacy monetary columns directly.

    The row-identity CHECK forbids stored money on a target row, so it is dropped for
    the duration of the tamper and restored (as the same definition) afterwards.
    """
    conn = psycopg2.connect(client_encoding="utf-8", **tenant.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT pg_get_constraintdef(oid) FROM pg_constraint
                WHERE conname = 'ck_draftlines_rowidentity'
            """)
            definition = cur.fetchone()[0]
            cur.execute("ALTER TABLE payroll.payrolldraftlines "
                        "DROP CONSTRAINT ck_DraftLines_RowIdentity")
            try:
                cur.execute("""
                    UPDATE payroll.payrolldraftlines
                    SET rateamount = 9999, calculatedamount = 3, needsmanagerreview = TRUE
                    WHERE payrollperiodid = %s
                """, (period_id,))
            finally:
                cur.execute(f"ALTER TABLE payroll.payrolldraftlines "
                            f"ADD CONSTRAINT ck_DraftLines_RowIdentity {definition} NOT VALID")
    finally:
        conn.close()


async def test_stored_draft_line_money_cannot_change_any_live_result(
    p3c_client, p3c_engine, tenant,
):
    await rated_definition(p3c_client, tenant, rate="25")
    period = await _open_period(p3c_client, p3c_engine, tenant)
    ppd = _definition_id(await _grid(p3c_client, tenant, period))
    assert (await _save(p3c_client, tenant, period, {str(ppd): "8"})).status_code == 200
    period_id = period["payroll_period_id"]

    _tamper_money(tenant, period_id)
    conn = psycopg2.connect(client_encoding="utf-8", **tenant.dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT rateamount, calculatedamount, needsmanagerreview "
                        "FROM payroll.payrolldraftlines WHERE payrollperiodid = %s",
                        (period_id,))
            assert cur.fetchall() == [(Decimal("9999"), Decimal("3"), True)]
    finally:
        conn.close()

    grid = await _grid(p3c_client, tenant, period)
    cell = _cell(grid, ppd, tenant.driver_a)
    assert Decimal(cell["calculated_amount"]) == Decimal("200")
    assert cell["needs_manager_review"] is False
    assert Decimal(grid["summary"]["gross_total"]) == Decimal("200")
    assert grid["summary"]["needs_attention"] == 0

    lines = (await p3c_client.get(
        f"/payroll/periods/{period_id}/lines", headers=tenant.admin)).json()
    assert [Decimal(line["calculated_amount"]) for line in lines] == [Decimal("200")]
    # The stored columns (RateAmount 9999, CalculatedAmount 3, NeedsManagerReview TRUE)
    # are never surfaced for a target ordinary row: no scalar rate, live money, live flag.
    assert [line["rate_amount"] for line in lines] == [None]
    assert [line["needs_manager_review"] for line in lines] == [False]
    assert [line["calculation_status"] for line in lines] == ["Calculated"]

    summary = (await p3c_client.get(
        f"/payroll/periods/{period_id}/lines/summary", headers=tenant.admin)).json()
    assert [Decimal(row["total_calculated_amount"]) for row in summary] == [Decimal("200")]
    assert summary[0]["lines_needing_attention"] == 0

    preview = (await p3c_client.get(
        f"/payroll/periods/{period_id}/calculation-preview", headers=tenant.admin)).json()
    assert Decimal(preview["drivers"][0]["daily_pay"]) == Decimal("200")
    assert Decimal(preview["total_expected_pay"]) == Decimal("200")
    assert preview["has_blockers"] is False

    hub = (await p3c_client.get("/payroll/current", headers=tenant.admin)).json()
    slots = [b["slots"]["open"] for b in hub["branches"] if b["branch_id"] == tenant.branch_a]
    assert Decimal(slots[0]["financial_summary"]["total_expected_pay"]) == Decimal("200")

    # Nothing in the target flow ever wrote money into the row.
    conn = psycopg2.connect(client_encoding="utf-8", **tenant.dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT rateamount, calculatedamount FROM payroll.payrolldraftlines "
                        "WHERE payrollperiodid = %s", (period_id,))
            assert cur.fetchall() == [(Decimal("9999"), Decimal("3"))]  # only the tamper
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# No payable rounding in P4b
# ---------------------------------------------------------------------------

async def test_the_day_grid_gross_is_the_exact_live_aggregate_not_a_rounded_one(
    p3c_client, p3c_engine, tenant,
):
    """The authoritative live value keeps the calculation precision (four decimals).

    Payable/minor-unit rounding is decided by the evidence work unit; until then the
    Day Grid and the Calculation Preview must agree on the exact aggregate.
    """
    await rated_definition(
        p3c_client, tenant, rate="0.3333", definition_code="ITEM_A", definition_name="Alpha")
    await rated_definition(
        p3c_client, tenant, rate="1.1111", definition_code="ITEM_B", definition_name="Beta")
    period = await _open_period(p3c_client, p3c_engine, tenant)
    grid = await _grid(p3c_client, tenant, period)
    by_code = {c["definition_code"]: c["payroll_period_definition_id"] for c in grid["columns"]}

    saved = await _save(p3c_client, tenant, period, {
        str(by_code["ITEM_A"]): "3", str(by_code["ITEM_B"]): "2"})
    assert saved.status_code == 200, saved.text
    gross = Decimal((await _grid(p3c_client, tenant, period))["summary"]["gross_total"])
    assert gross == Decimal("3.2221")            # 3 x 0.3333 + 2 x 1.1111, exactly
    assert gross != gross.quantize(Decimal("0.01"))

    preview = (await p3c_client.get(
        f"/payroll/periods/{period['payroll_period_id']}/calculation-preview",
        headers=tenant.admin)).json()
    assert Decimal(preview["total_expected_pay"]) == gross
    assert Decimal(preview["drivers"][0]["daily_pay"]) == gross


# ---------------------------------------------------------------------------
# The frozen method version controls dispatch
# ---------------------------------------------------------------------------

def _execute(tenant, sql: str, params=()) -> None:
    conn = psycopg2.connect(client_encoding="utf-8", **tenant.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
    finally:
        conn.close()


async def test_the_frozen_version_not_live_metadata_controls_calculation_and_entry(
    p3c_client, p3c_engine, tenant,
):
    definition = await rated_definition(p3c_client, tenant, rate="25")
    period = await _open_period(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]
    ppd = _definition_id(await _grid(p3c_client, tenant, period))
    assert (await _save(p3c_client, tenant, period, {str(ppd): "8"})).status_code == 200

    # Live metadata moves on to a version nobody has frozen: the period is unaffected.
    _execute(tenant, "ALTER TABLE payroll.paydefinitionprovenance DISABLE TRIGGER USER")
    try:
        _execute(tenant, "UPDATE payroll.paydefinitionprovenance "
                         "SET calculationmethodversion = 2 WHERE paydefinitionid = %s",
                 (definition["pay_definition_id"],))
    finally:
        _execute(tenant, "ALTER TABLE payroll.paydefinitionprovenance ENABLE TRIGGER USER")
    cell = _cell(await _grid(p3c_client, tenant, period), ppd, tenant.driver_a)
    assert Decimal(cell["calculated_amount"]) == Decimal("200")
    assert cell["calculation_status"] == "Calculated"

    # An unsupported FROZEN version fails closed everywhere: V1 is never executed.
    _execute(tenant, "ALTER TABLE payroll.payrollperioddefinitions "
                     "DISABLE TRIGGER trg_PayrollPeriodDefinitions_Immutable")
    try:
        _execute(tenant, "UPDATE payroll.payrollperioddefinitions "
                         "SET calculationmethodversionsnapshot = 2 WHERE payrollperiodid = %s",
                 (period_id,))
    finally:
        _execute(tenant, "ALTER TABLE payroll.payrollperioddefinitions "
                         "ENABLE TRIGGER trg_PayrollPeriodDefinitions_Immutable")

    grid = await _grid(p3c_client, tenant, period)
    cell = _cell(grid, ppd, tenant.driver_a)
    assert cell["calculated_amount"] is None
    assert cell["calculation_status"] == "MethodNotReady"
    assert cell["needs_manager_review"] is True
    assert Decimal(grid["summary"]["gross_total"]) == Decimal("0")

    lines = (await p3c_client.get(
        f"/payroll/periods/{period_id}/lines", headers=tenant.admin)).json()
    assert [(line["calculated_amount"], line["calculation_status"]) for line in lines] == [
        (None, "MethodNotReady")]

    next_day = _day(period, 1)
    blocked = await p3c_client.post(
        f"/payroll/periods/{period_id}/lines",
        json={"driver_id": tenant.driver_a, "work_date": next_day,
              "payroll_period_definition_id": ppd, "quantity": "1"}, headers=tenant.admin)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "CALCULATION_METHOD_NOT_READY"
    grid_save = await _save(p3c_client, tenant, period, {str(ppd): "1"}, offset=1)
    assert grid_save.status_code == 409

    preview = (await p3c_client.get(
        f"/payroll/periods/{period_id}/calculation-preview", headers=tenant.admin)).json()
    assert preview["has_blockers"] is True
    assert preview["drivers"][0]["lines"][0]["calculation_status"] == "MethodNotReady"
