"""A Status a day already holds keeps its identity through every save of that row.

Keeping the held Status and choosing a Status by typing are different operations. A grid row
carries the held Status as its canonical identity, so an unrelated edit of the row (a note, a
quantity) never reads the Status back as free text, where another Status's alias could take the
day over. Newly typed names and aliases still resolve Company-wide, with the name, alias, code
precedence unchanged.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

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
    _sql,
    assign_weekly_setup,
    build_payroll_tenant,
    create_period,
    query,
    seed_status_rate,
    status_column_of,
)

pytestmark = pytest.mark.asyncio
D = Decimal

VACATION_CODE = "SK_AAAA1111"


@pytest.fixture(name="tenant")
def usd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


def _day(period: dict, offset: int = 0) -> str:
    return (date.fromisoformat(period["start_date"]) + timedelta(days=offset)).isoformat()


def _status(tenant, name: str, code: str, hours: str, *, aliases=(), active=True) -> int:
    """A paid Status whose technical code is not its name (as for a generated code)."""
    column, _, _ = status_column_of(tenant)
    key_id = _sql(tenant, """
        INSERT INTO payroll.payrollstatuskeys
            (companyid, keyname, normalizedkeyname, statuscode, normalizedstatuscode,
             allowancecategory, isoffreason, hoursvalue, statusratecolumnid, isactive)
        VALUES (%s, %s, %s, %s, %s, 'Other', FALSE, %s, %s, %s) RETURNING statuskeyid
    """, (tenant.company_id, name, name.lower(), code, code.upper(), D(hours), column, active))[0][0]
    for alias in aliases:
        _sql(tenant, """
            INSERT INTO payroll.payrollstatuskeyaliases
                (companyid, statuskeyid, isactive, aliastext, normalizedalias)
            VALUES (%s, %s, %s, %s, %s)
        """, (tenant.company_id, key_id, active, alias, alias.lower()))
    return key_id


async def _scene(client, engine, tenant):
    """Status A (8 hours, code SK_AAAA1111) and Status B (4 hours) whose alias is A's code, with
    a Status rate of 25. Returns (period, A, B)."""
    a = _status(tenant, "Vacation", VACATION_CODE, "8")
    b = _status(tenant, "Half day", "SK_BBBB2222", "4", aliases=(VACATION_CODE, "HD"))
    seed_status_rate(tenant, "25")
    await assign_weekly_setup(engine, tenant, tenant.branch_a, mask=0)
    period = await create_period(client, tenant, tenant.branch_a)
    return period, a, b


async def _save(client, tenant, period, row, *, offset=0):
    return await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid",
        json={"work_date": _day(period, offset), "rows": [row]}, headers=tenant.admin)


def _grid_row(response, tenant) -> dict:
    assert response.status_code == 200, response.text
    return next(r for r in response.json()["rows"] if r["driver_assignment_id"] == tenant.driver_a)


def _entry_key(tenant, period, offset=0):
    rows = query(tenant, """
        SELECT statuskeyid FROM payroll.payrollperioddriverdayentrystate
        WHERE payrollperiodid = %s AND driverassignmentid = %s AND workdate = %s AND NOT isvoided
    """, (period["payroll_period_id"], tenant.driver_a, _day(period, offset)))
    return rows[0][0] if rows else None


async def _status_pay(client, tenant, period) -> D:
    response = await client.get(
        f"/payroll/periods/{period['payroll_period_id']}/calculation-preview", headers=tenant.admin)
    assert response.status_code == 200, response.text
    driver = next(d for d in response.json()["drivers"] if d["driver_assignment_id"] == tenant.driver_a)
    return D(str(driver["status_pay"]))


async def _select_vacation(client, tenant, period) -> dict:
    row = _grid_row(
        await _save(client, tenant, period,
                    {"driver_assignment_id": tenant.driver_a, "values": {}, "status_key": "Vacation"}),
        tenant)
    assert row["status_key"] == VACATION_CODE and row["status_label"] == "Vacation"
    return row


async def test_the_grid_returns_the_canonical_identity_of_the_held_status(
    p3c_client, p3c_engine, tenant,
):
    period, a, _ = await _scene(p3c_client, p3c_engine, tenant)

    row = await _select_vacation(p3c_client, tenant, period)

    assert row["status_key_id"] == a


async def test_a_note_only_save_with_the_grid_row_keeps_the_held_status_whatever_aliases_exist(
    p3c_client, p3c_engine, tenant,
):
    """The review scenario: A is 8 hours x 25 = 200, B is 4 hours and has A's technical code as an
    alias. The grid row round-trips A; changing only its note must not turn the day into B."""
    period, a, _ = await _scene(p3c_client, p3c_engine, tenant)
    row = await _select_vacation(p3c_client, tenant, period)
    assert await _status_pay(p3c_client, tenant, period) == D("200.00")

    saved = _grid_row(await _save(p3c_client, tenant, period, {
        "driver_assignment_id": tenant.driver_a, "values": {},
        "status_key_id": row["status_key_id"], "status_key": row["status_key"],
        "notes": "only the note changed"}), tenant)

    assert _entry_key(tenant, period) == a
    assert saved["status_label"] == "Vacation" and saved["notes"] == "only the note changed"
    assert await _status_pay(p3c_client, tenant, period) == D("200.00")


async def test_a_text_only_save_that_round_trips_the_technical_code_keeps_the_held_status(
    p3c_client, p3c_engine, tenant,
):
    """A client that sends back only the row's `status_key` (the technical code) is the same
    unrelated edit: the code names the Status the day holds, not another Status's alias."""
    period, a, _ = await _scene(p3c_client, p3c_engine, tenant)
    row = await _select_vacation(p3c_client, tenant, period)

    saved = _grid_row(await _save(p3c_client, tenant, period, {
        "driver_assignment_id": tenant.driver_a, "values": {},
        "status_key": row["status_key"], "notes": "text-only round trip"}), tenant)

    assert _entry_key(tenant, period) == a
    assert saved["status_label"] == "Vacation"
    assert await _status_pay(p3c_client, tenant, period) == D("200.00")


async def test_the_identity_wins_over_status_text_in_the_same_row(p3c_client, p3c_engine, tenant):
    period, a, _ = await _scene(p3c_client, p3c_engine, tenant)
    row = await _select_vacation(p3c_client, tenant, period)

    await _save(p3c_client, tenant, period, {
        "driver_assignment_id": tenant.driver_a, "values": {},
        "status_key_id": row["status_key_id"], "status_key": "Half day"})

    assert _entry_key(tenant, period) == a


async def test_newly_typed_names_and_aliases_still_resolve_to_their_status(
    p3c_client, p3c_engine, tenant,
):
    period, a, b = await _scene(p3c_client, p3c_engine, tenant)
    await _select_vacation(p3c_client, tenant, period)

    by_alias = _grid_row(await _save(p3c_client, tenant, period, {
        "driver_assignment_id": tenant.driver_a, "values": {}, "status_key": "hd"}), tenant)
    assert _entry_key(tenant, period) == b
    assert by_alias["status_label"] == "Half day" and by_alias["status_key_id"] == b
    assert await _status_pay(p3c_client, tenant, period) == D("100.00")

    by_name = _grid_row(await _save(p3c_client, tenant, period, {
        "driver_assignment_id": tenant.driver_a, "values": {}, "status_key": "vacation"}), tenant)
    assert _entry_key(tenant, period) == a and by_name["status_label"] == "Vacation"


async def test_text_equal_to_a_code_and_an_alias_resolves_by_the_alias_on_a_day_that_holds_nothing(
    p3c_client, p3c_engine, tenant,
):
    """Precedence for new input is unchanged (name, then alias, then code): where no Status is
    held, A's code typed as text is B's alias."""
    period, _, b = await _scene(p3c_client, p3c_engine, tenant)

    row = _grid_row(await _save(p3c_client, tenant, period, {
        "driver_assignment_id": tenant.driver_a, "values": {}, "status_key": VACATION_CODE}), tenant)

    assert _entry_key(tenant, period) == b and row["status_label"] == "Half day"


async def test_choosing_another_status_by_identity_replaces_the_held_one(
    p3c_client, p3c_engine, tenant,
):
    period, _, b = await _scene(p3c_client, p3c_engine, tenant)
    await _select_vacation(p3c_client, tenant, period)

    row = _grid_row(await _save(p3c_client, tenant, period, {
        "driver_assignment_id": tenant.driver_a, "values": {}, "status_key_id": b}), tenant)

    assert _entry_key(tenant, period) == b and row["status_label"] == "Half day"


async def test_a_deactivated_status_can_be_kept_but_not_newly_chosen_by_identity(
    p3c_client, p3c_engine, tenant,
):
    period, a, b = await _scene(p3c_client, p3c_engine, tenant)
    row = await _select_vacation(p3c_client, tenant, period)
    _sql(tenant, "UPDATE payroll.payrollstatuskeys SET isactive = FALSE WHERE statuskeyid = %s", (a,))

    kept = await _save(p3c_client, tenant, period, {
        "driver_assignment_id": tenant.driver_a, "values": {},
        "status_key_id": row["status_key_id"], "notes": "kept after deactivation"})
    assert _grid_row(kept, tenant)["status_key_id"] == a and _entry_key(tenant, period) == a

    # Another day cannot newly choose the deactivated Status.
    refused = await _save(p3c_client, tenant, period, {
        "driver_assignment_id": tenant.driver_a, "values": {}, "status_key_id": a}, offset=1)
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "STATUS_NOT_FOUND"
    assert _entry_key(tenant, period, 1) is None


async def test_an_unknown_status_identity_is_refused_and_writes_nothing(
    p3c_client, p3c_engine, tenant,
):
    period, _, _ = await _scene(p3c_client, p3c_engine, tenant)

    refused = await _save(p3c_client, tenant, period, {
        "driver_assignment_id": tenant.driver_a, "values": {}, "status_key_id": 99999999})

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "STATUS_NOT_FOUND"
    assert _entry_key(tenant, period) is None
