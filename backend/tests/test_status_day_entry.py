"""Status input and usage limits at the Day Grid, over the Company Status catalog.

A Status is one catalog entry of the Company. The Day Grid resolves the text typed for a day
(name, alias or technical code) Company-wide to one Status, stores the canonical StatusKeyID,
shows the canonical primary name, and counts the Branch's usage limits from the canonical
day-entry selections of the period.
"""
from __future__ import annotations

from datetime import date, timedelta

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
    add_driver,
    assign_weekly_setup,
    build_catalog_tenant,
    build_tenant,
    create_period,
    get_grid,
    open_period_with_grid,
    query,
    seed_status_key,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(name="tenant")
def catalog_tenant(p3b_dsn):
    """A Tenant whose owner may also write the Company Status catalog."""
    return build_catalog_tenant(p3b_dsn)


def _url(period) -> str:
    return f"/payroll/periods/{period['payroll_period_id']}/day-grid"


def _day(period, offset=0) -> str:
    return (date.fromisoformat(period["start_date"]) + timedelta(days=offset)).isoformat()


async def _save(client, tenant, period, rows, *, offset=0, headers=None):
    return await client.post(
        _url(period), json={"work_date": _day(period, offset), "rows": rows},
        headers=headers or tenant.admin)


def _row(driver_assignment_id, status=None, **extra) -> dict:
    return {"driver_assignment_id": driver_assignment_id, "values": {}, "status_key": status, **extra}


def _grid_row(grid, driver_assignment_id) -> dict:
    return next(r for r in grid["rows"] if r["driver_assignment_id"] == driver_assignment_id)


def _entry_key(tenant, period, driver_assignment_id, offset=0):
    rows = query(tenant, """
        SELECT statuskeyid FROM payroll.payrollperioddriverdayentrystate
        WHERE payrollperiodid = %s AND driverassignmentid = %s AND workdate = %s AND NOT isvoided
    """, (period["payroll_period_id"], driver_assignment_id, _day(period, offset)))
    return rows[0][0] if rows else None


def _legacy_code(tenant, period, driver_assignment_id, offset=0):
    rows = query(tenant, """
        SELECT notes FROM payroll.payrolldraftlines
        WHERE payrollperiodid = %s AND driverassignmentid = %s AND workdate = %s
          AND linetype = 'DailyStatus' AND status <> 'Void'
    """, (period["payroll_period_id"], driver_assignment_id, _day(period, offset)))
    return rows[0][0] if rows else None


def _key_code(tenant, key_id) -> str:
    return query(tenant, "SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = %s",
                 (key_id,))[0][0]


# ---------------------------------------------------------------------------
# One catalog for every Branch
# ---------------------------------------------------------------------------

async def test_the_company_catalog_is_offered_and_usable_in_every_branch(
    p3c_client, p3c_engine, tenant,
):
    key_id = seed_status_key(tenant, "VACATION", category="Vacation")
    period_a, grid_a = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_b)
    period_b = await create_period(p3c_client, tenant, tenant.branch_b)
    grid_b = await get_grid(p3c_client, tenant, period_b)

    assert [k["status_key_id"] for k in grid_a["status_keys"]] == [key_id]
    assert [k["status_key_id"] for k in grid_b["status_keys"]] == [key_id]
    for period, driver in ((period_a, tenant.driver_a), (period_b, tenant.driver_b)):
        saved = await _save(p3c_client, tenant, period, [_row(driver, "VACATION")])
        assert saved.status_code == 200, saved.text
        assert _entry_key(tenant, period, driver) == key_id


async def test_another_companys_status_is_not_resolvable(p3c_client, p3c_engine, tenant, p3b_dsn):
    other = build_tenant(p3b_dsn)
    seed_status_key(other, "FOREIGN_ONLY")
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    rejected = await _save(p3c_client, tenant, period, [_row(tenant.driver_a, "FOREIGN_ONLY")])
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "STATUS_NOT_FOUND"


# ---------------------------------------------------------------------------
# Input resolution: name, alias or code, always stored and shown canonically
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("typed", [
    "Annual Leave", "annual leave", "  ANNUAL   LEAVE ", "AL", "al", "A  L", "Vac", "vac",
])
async def test_name_alias_and_code_resolve_to_the_same_canonical_status(
    p3c_client, p3c_engine, tenant, typed,
):
    key_id = seed_status_key(tenant, "Annual Leave", aliases=("AL", "A L", "Vac"))
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    saved = await _save(p3c_client, tenant, period, [_row(tenant.driver_a, typed)])
    assert saved.status_code == 200, saved.text
    row = _grid_row(saved.json(), tenant.driver_a)
    # The grid shows the canonical name and code, never the alias that was typed.
    assert row["status_label"] == "Annual Leave"
    assert row["status_key"] == _key_code(tenant, key_id)
    assert _entry_key(tenant, period, tenant.driver_a) == key_id
    assert _legacy_code(tenant, period, tenant.driver_a) == _key_code(tenant, key_id)


async def test_the_technical_code_resolves_too(p3c_client, p3c_engine, tenant):
    key_id = seed_status_key(tenant, "Annual Leave")
    code = _key_code(tenant, key_id)
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    saved = await _save(p3c_client, tenant, period, [_row(tenant.driver_a, code.lower())])
    assert saved.status_code == 200, saved.text
    assert _grid_row(saved.json(), tenant.driver_a)["status_label"] == "Annual Leave"


async def test_a_name_beats_an_alias_beats_a_code(p3c_client, p3c_engine, tenant):
    from tests.payroll_runtime_fixtures import _sql

    def key(name, code, aliases=()):
        key_id = _sql(tenant, """
            INSERT INTO payroll.payrollstatuskeys
                (companyid, keyname, normalizedkeyname, statuscode, normalizedstatuscode,
                 allowancecategory)
            VALUES (%s, %s, %s, %s, %s, 'Other') RETURNING statuskeyid
        """, (tenant.company_id, name, name.lower(), code, code.upper()))[0][0]
        for alias in aliases:
            _sql(tenant, """
                INSERT INTO payroll.payrollstatuskeyaliases
                    (companyid, statuskeyid, isactive, aliastext, normalizedalias)
                VALUES (%s, %s, TRUE, %s, %s)""", (tenant.company_id, key_id, alias, alias.lower()))
        return key_id

    by_name = key("ZED", "CODE_A")
    key("Other one", "ZED")                      # its code equals the first Status's name
    by_alias = key("Third", "CODE_C", aliases=("QQ",))
    key("Fourth", "QQ")                          # its code equals the third Status's alias
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, "zed")])).status_code == 200
    assert _entry_key(tenant, period, tenant.driver_a) == by_name
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, "qq")])).status_code == 200
    assert _entry_key(tenant, period, tenant.driver_a) == by_alias


async def test_an_unknown_input_rejects_the_whole_batch_before_any_write(
    p3c_client, p3c_engine, tenant,
):
    seed_status_key(tenant, "VAC")
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    rejected = await _save(p3c_client, tenant, period, [
        _row(tenant.driver_a, "VAC"), _row(tenant.driver_a2, "nonsense")])
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "STATUS_NOT_FOUND"
    assert "nonsense" in rejected.json()["detail"]["message"]
    assert query(tenant, "SELECT count(*) FROM payroll.payrollperioddriverdayentrystate "
                         "WHERE payrollperiodid = %s", (period["payroll_period_id"],)) == [(0,)]


async def test_a_blank_input_clears_the_status(p3c_client, p3c_engine, tenant):
    seed_status_key(tenant, "VAC", aliases=("Vacation",))
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    await _save(p3c_client, tenant, period, [_row(tenant.driver_a, "vacation")])
    cleared = await _save(p3c_client, tenant, period, [_row(tenant.driver_a, "   ")])
    assert cleared.status_code == 200
    assert _grid_row(cleared.json(), tenant.driver_a)["status_key"] is None
    assert _entry_key(tenant, period, tenant.driver_a) is None
    assert _legacy_code(tenant, period, tenant.driver_a) is None


async def test_a_deactivated_status_cannot_be_newly_selected_by_any_input(
    p3c_client, p3c_engine, tenant,
):
    seed_status_key(tenant, "OLD", aliases=("Retired",), active=False)
    period, grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    assert grid["status_keys"] == []
    for typed in ("OLD", "retired"):
        rejected = await _save(p3c_client, tenant, period, [_row(tenant.driver_a, typed)])
        assert rejected.status_code == 422, typed


async def test_a_day_keeps_its_status_after_the_status_is_deactivated(
    p3c_client, p3c_engine, tenant,
):
    key_id = seed_status_key(tenant, "OLD", aliases=("Retired",))
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, "OLD")])).status_code == 200
    query(tenant, "UPDATE payroll.payrollstatuskeys SET isactive = FALSE WHERE statuskeyid = %s",
          (key_id,))

    # The day still shows it and re-saving it unchanged (by the name the grid shows, an alias or
    # the code) is allowed; a different day cannot newly select it.
    shown = await get_grid(p3c_client, tenant, period, work_date=_day(period))
    assert _grid_row(shown, tenant.driver_a)["status_label"] == "OLD"
    for typed in ("OLD", "Retired"):
        assert (await _save(p3c_client, tenant, period,
                            [_row(tenant.driver_a, typed, notes="same")])).status_code == 200
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a2, "OLD")])).status_code == 422
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, "OLD")],
                        offset=1)).status_code == 422
    assert _entry_key(tenant, period, tenant.driver_a) == key_id


async def test_aliases_are_editable_after_use_and_old_days_keep_the_canonical_name(
    p3c_client, p3c_engine, tenant,
):
    key_id = seed_status_key(tenant, "Annual Leave", aliases=("AL",))
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, "AL")])).status_code == 200

    edited = await p3c_client.patch(
        f"/settings/status-keys/{key_id}",
        json={"aliases": ["Leave"], "key_name": "Holiday Leave"}, headers=tenant.admin)
    assert edited.status_code == 200, edited.text
    shown = await get_grid(p3c_client, tenant, period, work_date=_day(period))
    assert _grid_row(shown, tenant.driver_a)["status_label"] == "Holiday Leave"
    # The old alias no longer resolves; the new alias and the new name do.
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a2, "AL")])).status_code == 422
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a2, "Leave")])).status_code == 200


# ---------------------------------------------------------------------------
# The semantic lock of a used Status, and its lifecycle
# ---------------------------------------------------------------------------

async def test_a_used_status_cannot_change_its_semantics_but_can_be_replaced(
    p3c_client, p3c_engine, tenant,
):
    key_id = seed_status_key(tenant, "Vacation", hoursvalue=8, category="Vacation")
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    url = f"/settings/status-keys/{key_id}"
    free = await p3c_client.patch(url, json={"hours_value": "6", "category": "Sick"}, headers=tenant.admin)
    assert free.status_code == 200, free.text   # unused: semantics may still change

    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, "Vacation")])).status_code == 200
    listing = (await p3c_client.get("/settings/status-keys", headers=tenant.admin)).json()
    assert next(k for k in listing if k["status_key_id"] == key_id)["is_in_use"] is True

    for change in ({"hours_value": "4"}, {"is_off_reason": False}, {"category": "Personal"},
                   {"status_rate_column_id": None, "hours_value": "7"}):
        locked = await p3c_client.patch(url, json=change, headers=tenant.admin)
        assert locked.status_code == 409, change
        assert locked.json()["detail"]["code"] == "STATUS_KEY_IN_USE"
        assert "create a new Status" in locked.json()["detail"]["message"]

    # Name, display order and aliases stay editable; writing the same semantics is no change.
    editable = await p3c_client.patch(
        url, json={"key_name": "Annual leave", "display_order": 3, "aliases": ["AL"],
                   "hours_value": "6", "category": "Sick"}, headers=tenant.admin)
    assert editable.status_code == 200, editable.text

    # Lifecycle: deactivate the used Status and create a new one with other semantics.
    assert (await p3c_client.delete(url, headers=tenant.admin)).status_code == 200
    replacement = await p3c_client.post(
        "/settings/status-keys",
        json={"key_name": "Vacation", "category": "Vacation", "hours_value": "4"},
        headers=tenant.admin)
    assert replacement.status_code == 201, replacement.text
    # The old day keeps the old Status.
    assert _entry_key(tenant, period, tenant.driver_a) == key_id


async def test_a_status_without_selections_is_not_in_use(p3c_client, p3c_engine, tenant):
    key_id = seed_status_key(tenant, "Idle", hoursvalue=8)
    listing = (await p3c_client.get("/settings/status-keys", headers=tenant.admin)).json()
    assert next(k for k in listing if k["status_key_id"] == key_id)["is_in_use"] is False




# ---------------------------------------------------------------------------
# Branch usage limits, counted from canonical selections
# ---------------------------------------------------------------------------

def _limit(tenant, key_id, branch_id=None, **limits) -> None:
    """The Branch limit row of a Status (replacing any existing one)."""
    branch = branch_id or tenant.branch_a
    query(tenant, "DELETE FROM payroll.payrollstatuskeybranchlimits "
                  "WHERE branchid = %s AND statuskeyid = %s", (branch, key_id))
    names = ", ".join(limits)
    query(tenant, f"INSERT INTO payroll.payrollstatuskeybranchlimits "
                  f"(branchid, statuskeyid, companyid, {names}) "
                  f"VALUES (%s, %s, %s, {', '.join(['%s'] * len(limits))})",
          (branch, key_id, tenant.company_id, *limits.values()))


async def _period_with_three_drivers(p3c_client, p3c_engine, tenant):
    third = add_driver(tenant)
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    return period, (tenant.driver_a, tenant.driver_a2, third)


# (limit column, [slots that fit a limit of 2], the slot that must be refused, its message)
LIMIT_BOUNDARIES = {
    "per_period": ("limitusesperperiod", [(0, 0), (1, 1)], (2, 2), "period limit"),
    "per_driver": ("limitusesperdriver", [(0, 0), (0, 1)], (0, 2), "per-driver limit"),
    "across_drivers": ("limitusesacrossdrivers", [(0, 0), (1, 0)], (2, 1), "across-all-drivers limit"),
    "per_day": ("limitusesperday", [(0, 0), (1, 0)], (2, 0), "per-day limit"),
}


@pytest.mark.parametrize("limit", sorted(LIMIT_BOUNDARIES))
async def test_a_limit_allows_exactly_its_value_and_blocks_the_next_use(
    p3c_client, p3c_engine, tenant, limit,
):
    column, fitting, refused, message = LIMIT_BOUNDARIES[limit]
    key_id = seed_status_key(tenant, "LIM")
    _limit(tenant, key_id, **{column + "enabled": True, column: 2})
    period, drivers = await _period_with_three_drivers(p3c_client, p3c_engine, tenant)

    for driver, offset in fitting:
        ok = await _save(p3c_client, tenant, period, [_row(drivers[driver], "LIM")], offset=offset)
        assert ok.status_code == 200, (limit, ok.text)
    over = await _save(p3c_client, tenant, period,
                       [_row(drivers[refused[0]], "LIM")], offset=refused[1])
    assert over.status_code == 422, (limit, over.text)
    assert message in over.json()["detail"]
    assert _entry_key(tenant, period, drivers[refused[0]], refused[1]) is None


async def test_a_limit_only_counts_what_it_limits(p3c_client, p3c_engine, tenant):
    """The same Status stays usable wherever the enabled limit is not reached."""
    key_id = seed_status_key(tenant, "LIM")
    _limit(tenant, key_id, limitusesperdriverenabled=True, limitusesperdriver=1)
    period, (a, a2, a3) = await _period_with_three_drivers(p3c_client, p3c_engine, tenant)
    assert (await _save(p3c_client, tenant, period, [_row(a, "LIM")])).status_code == 200
    # Other drivers are not limited by driver a's use.
    assert (await _save(p3c_client, tenant, period, [_row(a2, "LIM"), _row(a3, "LIM")])).status_code == 200
    assert (await _save(p3c_client, tenant, period, [_row(a2, "LIM")], offset=1)).status_code == 422


async def test_limits_are_the_branchs_own(p3c_client, p3c_engine, tenant):
    key_id = seed_status_key(tenant, "LIM")
    _limit(tenant, key_id, branch_id=tenant.branch_a,
           limitusesperperiodenabled=True, limitusesperperiod=1)
    period_a, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_b)
    period_b = await create_period(p3c_client, tenant, tenant.branch_b)

    assert (await _save(p3c_client, tenant, period_a, [_row(tenant.driver_a, "LIM")])).status_code == 200
    assert (await _save(p3c_client, tenant, period_a, [_row(tenant.driver_a, "LIM")],
                        offset=1)).status_code == 422
    # Branch B has no row for the Status: unlimited, whatever Branch A used.
    for offset in range(3):
        assert (await _save(p3c_client, tenant, period_b, [_row(tenant.driver_b, "LIM")],
                            offset=offset)).status_code == 200
    # A limit of Branch B does not constrain Branch A either.
    _limit(tenant, key_id, branch_id=tenant.branch_b,
           limitusesperdayenabled=True, limitusesperday=1)
    assert (await _save(p3c_client, tenant, period_a, [_row(tenant.driver_a2, "LIM")])).status_code == 422
    assert (await _save(p3c_client, tenant, period_b, [_row(tenant.driver_b, "LIM")],
                        offset=3)).status_code == 200


async def test_resaving_a_slot_is_an_update_and_clearing_or_replacing_frees_the_use(
    p3c_client, p3c_engine, tenant,
):
    key_id = seed_status_key(tenant, "LIM")
    seed_status_key(tenant, "OTHER")
    _limit(tenant, key_id, limitusesperperiodenabled=True, limitusesperperiod=1)
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)

    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, "LIM")])).status_code == 200
    # The slot being overwritten is not counted against itself.
    assert (await _save(p3c_client, tenant, period,
                        [_row(tenant.driver_a, "LIM", notes="again")])).status_code == 200
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a2, "LIM")])).status_code == 422
    # Replacing the Status of the slot frees the use.
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, "OTHER")])).status_code == 200
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a2, "LIM")])).status_code == 200
    # Clearing frees it as well.
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a2, None)])).status_code == 200
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, "LIM")],
                        offset=1)).status_code == 200


async def test_a_batch_over_the_limit_is_refused_whole(p3c_client, p3c_engine, tenant):
    key_id = seed_status_key(tenant, "LIM")
    _limit(tenant, key_id, limitusesperdayenabled=True, limitusesperday=1)
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    over = await _save(p3c_client, tenant, period, [
        _row(tenant.driver_a, "LIM"), _row(tenant.driver_a2, "LIM")])
    assert over.status_code == 422
    assert query(tenant, "SELECT count(*) FROM payroll.payrollperioddriverdayentrystate "
                         "WHERE payrollperiodid = %s", (period["payroll_period_id"],)) == [(0,)]
    assert query(tenant, "SELECT count(*) FROM payroll.payrolldraftlines "
                         "WHERE payrollperiodid = %s", (period["payroll_period_id"],)) == [(0,)]


async def test_a_voided_selection_does_not_count(p3c_client, p3c_engine, tenant):
    key_id = seed_status_key(tenant, "LIM")
    _limit(tenant, key_id, limitusesperperiodenabled=True, limitusesperperiod=1)
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, "LIM")])).status_code == 200
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a2, "LIM")])).status_code == 422
    query(tenant, "UPDATE payroll.payrollperioddriverdayentrystate SET isvoided = TRUE "
                  "WHERE payrollperiodid = %s", (period["payroll_period_id"],))
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a2, "LIM")])).status_code == 200


async def test_counts_come_from_canonical_selections_not_the_legacy_duplicate(
    p3c_client, p3c_engine, tenant,
):
    key_id = seed_status_key(tenant, "LIM")
    code = _key_code(tenant, key_id)
    _limit(tenant, key_id, limitusesperperiodenabled=True, limitusesperperiod=1)
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]

    # A stray legacy DailyStatus line (no canonical selection) is not a use of the Status.
    query(tenant, """
        INSERT INTO payroll.payrolldraftlines
            (companyid, branchid, payrollperiodid, driverassignmentid, workdate, linetype,
             quantity, sourcetype, status, notes)
        VALUES (%s, %s, %s, %s, %s, 'DailyStatus', 0, 'Manual', 'Active', %s)
    """, (tenant.company_id, tenant.branch_a, period_id, tenant.driver_a2, _day(period, 2), code))
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, "LIM")])).status_code == 200

    # Removing the legacy duplicate of a canonical selection does not free the use either.
    query(tenant, "UPDATE payroll.payrolldraftlines SET status = 'Void' "
                  "WHERE payrollperiodid = %s AND linetype = 'DailyStatus'", (period_id,))
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a2, "LIM")])).status_code == 422


async def test_removing_the_limit_row_lifts_the_limit(p3c_client, p3c_engine, tenant):
    key_id = seed_status_key(tenant, "LIM")
    _limit(tenant, key_id, limitusesperperiodenabled=True, limitusesperperiod=1)
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, "LIM")])).status_code == 200
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a2, "LIM")])).status_code == 422
    lifted = await p3c_client.put(
        f"/settings/branches/{tenant.branch_a}/status-keys/{key_id}/limits",
        json={}, headers=tenant.admin)
    assert lifted.status_code == 200, lifted.text
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a2, "LIM")])).status_code == 200


# ---------------------------------------------------------------------------
# Races between a catalog edit and the first selection of a Status
# ---------------------------------------------------------------------------

async def test_a_semantic_edit_in_flight_either_precedes_the_selection_or_is_refused_after_it(
    p3c_client, p3c_engine, tenant, p3b_dsn,
):
    """A catalog edit of hours and a Day Grid selection of the same unused Status serialize on
    the Status row: the selection waits for the in-flight edit and sees its result, and an edit
    that arrives while a selection is in flight waits and is then refused as in use."""
    import asyncio

    import psycopg2

    editing_first = seed_status_key(tenant, "EDIT_FIRST", hoursvalue=8)
    selecting_first = seed_status_key(tenant, "SELECT_FIRST", hoursvalue=8)
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]

    # 1. The edit is in flight (uncommitted): the selection must wait for it.
    edit = psycopg2.connect(client_encoding="utf-8", **p3b_dsn)
    try:
        with edit.cursor() as cur:
            cur.execute("UPDATE payroll.payrollstatuskeys SET hoursvalue = 4 WHERE statuskeyid = %s",
                        (editing_first,))
        selection = asyncio.create_task(
            _save(p3c_client, tenant, period, [_row(tenant.driver_a, "EDIT_FIRST")]))
        await asyncio.sleep(1.0)
        assert not selection.done(), "the selection must wait for the in-flight edit of its Status"
        edit.commit()
        saved = await asyncio.wait_for(selection, timeout=15)
        assert saved.status_code == 200, saved.text
    finally:
        edit.close()
    assert query(tenant, "SELECT hoursvalue FROM payroll.payrollstatuskeys WHERE statuskeyid = %s",
                 (editing_first,)) == [(4,)]

    # 2. The selection is in flight (uncommitted): the edit must wait, and is then refused.
    selecting = psycopg2.connect(client_encoding="utf-8", **p3b_dsn)
    try:
        with selecting.cursor() as cur:
            cur.execute("""
                INSERT INTO payroll.payrollperioddriverdayentrystate
                    (companyid, branchid, payrollperiodid, workdate, driverassignmentid,
                     statuskeyid, isvoided, createdbyuserid)
                VALUES (%s, %s, %s, %s, %s, %s, FALSE, %s)
            """, (tenant.company_id, tenant.branch_a, period_id, _day(period, 3),
                  tenant.driver_a2, selecting_first, tenant.owner))
        edit_request = asyncio.create_task(p3c_client.patch(
            f"/settings/status-keys/{selecting_first}", json={"hours_value": "4"},
            headers=tenant.admin))
        await asyncio.sleep(1.0)
        assert not edit_request.done(), "the edit must wait for the in-flight selection"
        selecting.commit()
        refused = await asyncio.wait_for(edit_request, timeout=15)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["code"] == "STATUS_KEY_IN_USE"
    finally:
        selecting.close()
    assert query(tenant, "SELECT hoursvalue FROM payroll.payrollstatuskeys WHERE statuskeyid = %s",
                 (selecting_first,)) == [(8,)]


async def test_a_status_deactivated_before_the_selection_commits_cannot_be_newly_selected(
    p3c_client, p3c_engine, tenant, p3b_dsn,
):
    import asyncio

    import psycopg2

    key_id = seed_status_key(tenant, "SOON_GONE")
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    deactivation = psycopg2.connect(client_encoding="utf-8", **p3b_dsn)
    try:
        with deactivation.cursor() as cur:
            cur.execute("UPDATE payroll.payrollstatuskeys SET isactive = FALSE WHERE statuskeyid = %s",
                        (key_id,))
        selection = asyncio.create_task(
            _save(p3c_client, tenant, period, [_row(tenant.driver_a, "SOON_GONE")]))
        await asyncio.sleep(1.0)
        assert not selection.done()
        deactivation.commit()
        refused = await asyncio.wait_for(selection, timeout=15)
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"]["code"] == "STATUS_NOT_FOUND"
    finally:
        deactivation.close()
    assert _entry_key(tenant, period, tenant.driver_a) is None
