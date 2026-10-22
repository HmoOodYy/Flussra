"""A day that counts as a full day off carries no ordinary work.

A Status with ``IsOffReason`` cannot share a DriverAssignment day with a nonzero ordinary daily
quantity. The rule is judged on the final state of a save (so clearing the Status or the quantity
in the same save is fine), holds in either write order and for every writer (Day Grid save and
ordinary source-line mutation), is all-or-nothing for a Day Grid batch, and is repeated by the
database as a backstop whose advisory lock serializes writers that bypass the Period lock.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import date, timedelta

import psycopg2
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
    build_payroll_tenant,
    open_period_with_grid,
    query,
    seed_status_key,
)

pytestmark = pytest.mark.asyncio
CONFLICT = "STATUS_OFF_WORK_CONFLICT"


@pytest.fixture(name="tenant")
def usd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


def _day(period: dict, offset: int = 0) -> str:
    return (date.fromisoformat(period["start_date"]) + timedelta(days=offset)).isoformat()


async def _scene(client, engine, tenant) -> tuple[dict, int]:
    """An Open period with one ordinary column; a full-day-off Status (OFF) and a partial one
    (PART) exist."""
    seed_status_key(tenant, "OFF", off=True)
    seed_status_key(tenant, "PART", off=False)
    period, grid = await open_period_with_grid(client, engine, tenant)
    return period, grid["columns"][0]["payroll_period_definition_id"]


async def _save(client, tenant, period, rows, *, offset=0):
    return await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid",
        json={"work_date": _day(period, offset), "rows": rows}, headers=tenant.admin)


def _row(assignment, ppd=None, quantity=None, status=None) -> dict:
    values = {} if ppd is None else {str(ppd): quantity}
    return {"driver_assignment_id": assignment, "values": values, "status_key": status}


async def _add_line(client, tenant, period, ppd, quantity, *, offset=0, driver=None):
    return await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/lines",
        json={"driver_assignment_id": driver or tenant.driver_a, "work_date": _day(period, offset),
              "payroll_period_definition_id": ppd, "quantity": quantity},
        headers=tenant.admin)


def _status_of(tenant, period, driver, offset=0):
    rows = query(tenant, """
        SELECT k.statuscode FROM payroll.payrollperioddriverdayentrystate e
        JOIN payroll.payrollstatuskeys k ON k.statuskeyid = e.statuskeyid
        WHERE e.payrollperiodid = %s AND e.driverassignmentid = %s AND e.workdate = %s
          AND NOT e.isvoided
    """, (period["payroll_period_id"], driver, _day(period, offset)))
    return rows[0][0] if rows else None


def _quantity_of(tenant, period, driver, ppd, offset=0):
    rows = query(tenant, """
        SELECT quantity FROM payroll.payrolldraftlines
        WHERE payrollperiodid = %s AND driverassignmentid = %s AND workdate = %s
          AND payrollperioddefinitionid = %s AND status <> 'Void'
    """, (period["payroll_period_id"], driver, _day(period, offset), ppd))
    return rows[0][0] if rows else None


def _code(response) -> str:
    return response.json()["detail"]["code"]


# ---------------------------------------------------------------------------
# Day Grid save: the final state of the row decides
# ---------------------------------------------------------------------------

async def test_an_off_status_with_a_quantity_in_one_save_is_refused_and_writes_nothing(
    p3c_client, p3c_engine, tenant,
):
    period, ppd = await _scene(p3c_client, p3c_engine, tenant)
    refused = await _save(p3c_client, tenant, period, [_row(tenant.driver_a, ppd, "3", "OFF")])
    assert refused.status_code == 422 and _code(refused) == CONFLICT
    assert _status_of(tenant, period, tenant.driver_a) is None
    assert _quantity_of(tenant, period, tenant.driver_a, ppd) is None


@pytest.mark.parametrize("quantity", ["", "0"])
async def test_an_off_status_with_no_quantity_is_allowed(
    p3c_client, p3c_engine, tenant, quantity,
):
    period, ppd = await _scene(p3c_client, p3c_engine, tenant)
    saved = await _save(p3c_client, tenant, period, [_row(tenant.driver_a, ppd, quantity, "OFF")])
    assert saved.status_code == 200, saved.text
    assert _status_of(tenant, period, tenant.driver_a) == "OFF"
    assert not _quantity_of(tenant, period, tenant.driver_a, ppd)


async def test_a_partial_or_non_off_status_may_coexist_with_ordinary_work(
    p3c_client, p3c_engine, tenant,
):
    period, ppd = await _scene(p3c_client, p3c_engine, tenant)
    saved = await _save(p3c_client, tenant, period, [_row(tenant.driver_a, ppd, "3", "PART")])
    assert saved.status_code == 200, saved.text
    assert _status_of(tenant, period, tenant.driver_a) == "PART"
    assert _quantity_of(tenant, period, tenant.driver_a, ppd) == 3


async def test_an_off_status_over_a_day_that_already_has_work_is_refused(
    p3c_client, p3c_engine, tenant,
):
    period, ppd = await _scene(p3c_client, p3c_engine, tenant)
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, ppd, "3")])
            ).status_code == 200
    # The save does not mention the quantity, so the day keeps it: the final state conflicts.
    refused = await _save(p3c_client, tenant, period, [_row(tenant.driver_a, status="OFF")])
    assert refused.status_code == 422 and _code(refused) == CONFLICT
    assert _status_of(tenant, period, tenant.driver_a) is None
    assert _quantity_of(tenant, period, tenant.driver_a, ppd) == 3


async def test_an_off_status_that_also_clears_the_quantity_in_the_same_save_is_allowed(
    p3c_client, p3c_engine, tenant,
):
    period, ppd = await _scene(p3c_client, p3c_engine, tenant)
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, ppd, "3")])
            ).status_code == 200
    saved = await _save(p3c_client, tenant, period, [_row(tenant.driver_a, ppd, "0", "OFF")])
    assert saved.status_code == 200, saved.text
    assert _status_of(tenant, period, tenant.driver_a) == "OFF"
    assert not _quantity_of(tenant, period, tenant.driver_a, ppd)


async def test_replacing_an_off_status_by_work_in_the_same_save_is_allowed(
    p3c_client, p3c_engine, tenant,
):
    period, ppd = await _scene(p3c_client, p3c_engine, tenant)
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, status="OFF")])
            ).status_code == 200
    # Clearing the Status and writing the quantity in one save leaves a valid final state.
    saved = await _save(p3c_client, tenant, period, [_row(tenant.driver_a, ppd, "4")])
    assert saved.status_code == 200, saved.text
    assert _status_of(tenant, period, tenant.driver_a) is None
    assert _quantity_of(tenant, period, tenant.driver_a, ppd) == 4
    # ... and so is switching to a partial Status with the quantity.
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, status="OFF")],
                        offset=1)).status_code == 200
    swapped = await _save(p3c_client, tenant, period, [_row(tenant.driver_a, ppd, "2", "PART")],
                          offset=1)
    assert swapped.status_code == 200, swapped.text
    assert _status_of(tenant, period, tenant.driver_a, 1) == "PART"
    assert _quantity_of(tenant, period, tenant.driver_a, ppd, 1) == 2


async def test_one_conflicting_row_refuses_the_whole_batch(p3c_client, p3c_engine, tenant):
    period, ppd = await _scene(p3c_client, p3c_engine, tenant)
    refused = await _save(p3c_client, tenant, period, [
        _row(tenant.driver_a, ppd, "5", "PART"),
        _row(tenant.driver_a2, ppd, "3", "OFF"),
    ])
    assert refused.status_code == 422 and _code(refused) == CONFLICT
    for driver in (tenant.driver_a, tenant.driver_a2):
        assert _status_of(tenant, period, driver) is None
        assert _quantity_of(tenant, period, driver, ppd) is None


async def test_the_rule_is_per_assignment_and_per_day(p3c_client, p3c_engine, tenant):
    period, ppd = await _scene(p3c_client, p3c_engine, tenant)
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, status="OFF")])
            ).status_code == 200
    # Another Driver works the same day, and the same Driver works another day.
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a2, ppd, "6")])
            ).status_code == 200
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, ppd, "6")], offset=1)
            ).status_code == 200


# ---------------------------------------------------------------------------
# Ordinary source-line writers
# ---------------------------------------------------------------------------

async def test_a_source_line_with_a_quantity_is_refused_on_an_off_day(
    p3c_client, p3c_engine, tenant,
):
    period, ppd = await _scene(p3c_client, p3c_engine, tenant)
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, status="OFF")])
            ).status_code == 200
    refused = await _add_line(p3c_client, tenant, period, ppd, "2")
    assert refused.status_code == 422 and _code(refused) == CONFLICT
    assert _quantity_of(tenant, period, tenant.driver_a, ppd) is None
    # Another day of the same Driver and the same day of another Driver are open.
    assert (await _add_line(p3c_client, tenant, period, ppd, "2", offset=1)).status_code == 201
    assert (await _add_line(p3c_client, tenant, period, ppd, "2", driver=tenant.driver_a2)
            ).status_code == 201


async def test_a_zero_quantity_source_line_is_allowed_on_an_off_day(p3c_client, p3c_engine, tenant):
    period, ppd = await _scene(p3c_client, p3c_engine, tenant)
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, status="OFF")])
            ).status_code == 200
    assert (await _add_line(p3c_client, tenant, period, ppd, "0")).status_code == 201


async def test_updating_a_source_line_into_work_is_refused_on_an_off_day(
    p3c_client, p3c_engine, tenant,
):
    period, ppd = await _scene(p3c_client, p3c_engine, tenant)
    created = await _add_line(p3c_client, tenant, period, ppd, "0")
    assert created.status_code == 201, created.text
    line_id = created.json()["draft_line_id"]
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, status="OFF")])
            ).status_code == 200
    url = f"/payroll/periods/{period['payroll_period_id']}/lines/{line_id}"
    refused = await p3c_client.patch(url, json={"quantity": "4"}, headers=tenant.admin)
    assert refused.status_code == 422 and _code(refused) == CONFLICT
    assert not _quantity_of(tenant, period, tenant.driver_a, ppd)
    # A note edit, and voiding the line, are not new work.
    assert (await p3c_client.patch(url, json={"notes": "n"}, headers=tenant.admin)).status_code == 200
    assert (await p3c_client.delete(url, headers=tenant.admin)).status_code == 204


async def test_source_line_work_blocks_an_off_status_until_the_line_is_cleared(
    p3c_client, p3c_engine, tenant,
):
    period, ppd = await _scene(p3c_client, p3c_engine, tenant)
    created = await _add_line(p3c_client, tenant, period, ppd, "3")
    assert created.status_code == 201, created.text
    refused = await _save(p3c_client, tenant, period, [_row(tenant.driver_a, status="OFF")])
    assert refused.status_code == 422 and _code(refused) == CONFLICT
    line_url = (f"/payroll/periods/{period['payroll_period_id']}/lines/"
                f"{created.json()['draft_line_id']}")
    assert (await p3c_client.delete(line_url, headers=tenant.admin)).status_code == 204
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, status="OFF")])
            ).status_code == 200


# ---------------------------------------------------------------------------
# The database backstop
# ---------------------------------------------------------------------------

def _connect(tenant):
    return psycopg2.connect(client_encoding="utf-8", **tenant.dsn)


def _insert_off_selection(cur, tenant, period, driver, offset=0):
    cur.execute("SELECT statuskeyid FROM payroll.payrollstatuskeys "
                "WHERE companyid = %s AND statuscode = 'OFF'", (tenant.company_id,))
    key = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO payroll.payrollperioddriverdayentrystate
            (companyid, branchid, payrollperiodid, workdate, driverassignmentid, statuskeyid,
             isvoided, createdbyuserid)
        VALUES (%s, %s, %s, %s, %s, %s, FALSE, %s)
    """, (tenant.company_id, tenant.branch_a, period["payroll_period_id"], _day(period, offset),
          driver, key, tenant.owner))


def _set_quantity(cur, period, driver, ppd, quantity, offset=0):
    cur.execute("""
        UPDATE payroll.payrolldraftlines SET quantity = %s
        WHERE payrollperiodid = %s AND driverassignmentid = %s AND workdate = %s
          AND payrollperioddefinitionid = %s AND status <> 'Void'
    """, (quantity, period["payroll_period_id"], driver, _day(period, offset), ppd))
    assert cur.rowcount == 1


async def test_the_database_refuses_work_on_an_off_day_and_an_off_status_on_a_worked_day(
    p3c_client, p3c_engine, tenant,
):
    period, ppd = await _scene(p3c_client, p3c_engine, tenant)
    # Day 0: off first, then work.
    assert (await _add_line(p3c_client, tenant, period, ppd, "0")).status_code == 201
    conn = _connect(tenant)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            _insert_off_selection(cur, tenant, period, tenant.driver_a)
            with pytest.raises(psycopg2.errors.CheckViolation, match=CONFLICT):
                _set_quantity(cur, period, tenant.driver_a, ppd, 5)
        # Day 1: work first, then off.
        assert (await _add_line(p3c_client, tenant, period, ppd, "2", offset=1)).status_code == 201
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.CheckViolation, match=CONFLICT):
                _insert_off_selection(cur, tenant, period, tenant.driver_a, offset=1)
    finally:
        conn.close()
    assert not _quantity_of(tenant, period, tenant.driver_a, ppd)
    assert _quantity_of(tenant, period, tenant.driver_a, ppd, 1) == 2
    assert _status_of(tenant, period, tenant.driver_a, 1) is None


def _blocked_by(tenant, holder_pid: int) -> list[str]:
    return [row[0] for row in query(tenant, """
        SELECT query FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))
    """, (holder_pid,))]


async def _wait_blocked(tenant, holder_pid: int, finished: threading.Event) -> None:
    for _ in range(300):
        if _blocked_by(tenant, holder_pid):
            return
        if finished.is_set():
            raise AssertionError("the second writer finished instead of waiting")
        await asyncio.sleep(0.02)
    raise AssertionError("the second writer never waited for the first")


async def _run_race(
    tenant, first, second, *,
    refusal: type[psycopg2.Error] = psycopg2.errors.CheckViolation, marker: str = CONFLICT,
) -> tuple[str, str]:
    """Run ``first`` on connection A (uncommitted), start ``second`` on connection B, which must
    WAIT for A, then commit A. Returns (A's outcome, B's outcome). B's refusal is an error of
    class ``refusal`` whose message carries ``marker``."""
    a = _connect(tenant)
    b = _connect(tenant)
    outcome: dict[str, str] = {}
    finished = threading.Event()
    try:
        with a.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            holder_pid = cur.fetchone()[0]
            first(cur)

        def run_second():
            try:
                with b.cursor() as cur:
                    second(cur)
                b.commit()
                outcome["second"] = "committed"
            except refusal as error:
                b.rollback()
                outcome["second"] = f"refused:{marker in str(error)}"
            finally:
                finished.set()

        worker = threading.Thread(target=run_second)
        worker.start()
        await _wait_blocked(tenant, holder_pid, finished)
        assert "second" not in outcome, "the second writer must still be waiting"
        a.commit()
        outcome["first"] = "committed"
        await asyncio.get_running_loop().run_in_executor(None, worker.join, 20)
        assert not worker.is_alive()
    finally:
        a.close()
        b.close()
    return outcome["first"], outcome["second"]


async def test_two_writers_that_bypass_the_period_lock_cannot_create_the_conflict(
    p3c_client, p3c_engine, tenant,
):
    """The true race: neither writer holds the Period lock. The first to reach the day holds the
    day's advisory lock until it commits; the second waits, then sees the committed row and is
    refused. The invalid pair never exists, whichever writer is first."""
    period, ppd = await _scene(p3c_client, p3c_engine, tenant)
    driver = tenant.driver_a
    for offset in (0, 1):
        assert (await _add_line(p3c_client, tenant, period, ppd, "0", offset=offset)
                ).status_code == 201

    # Off Status first, then ordinary work.
    first, second = await _run_race(
        tenant,
        lambda cur: _insert_off_selection(cur, tenant, period, driver, 0),
        lambda cur: _set_quantity(cur, period, driver, ppd, 5, 0))
    assert (first, second) == ("committed", "refused:True")
    assert _status_of(tenant, period, driver, 0) == "OFF"
    assert not _quantity_of(tenant, period, driver, ppd, 0)

    # Ordinary work first, then the off Status.
    first, second = await _run_race(
        tenant,
        lambda cur: _set_quantity(cur, period, driver, ppd, 5, 1),
        lambda cur: _insert_off_selection(cur, tenant, period, driver, 1))
    assert (first, second) == ("committed", "refused:True")
    assert _quantity_of(tenant, period, driver, ppd, 1) == 5
    assert _status_of(tenant, period, driver, 1) is None


def _insert_selection(cur, tenant, period, driver, status_key_id, offset=0):
    cur.execute("""
        INSERT INTO payroll.payrollperioddriverdayentrystate
            (companyid, branchid, payrollperiodid, workdate, driverassignmentid, statuskeyid,
             isvoided, createdbyuserid)
        VALUES (%s, %s, %s, %s, %s, %s, FALSE, %s)
    """, (tenant.company_id, tenant.branch_a, period["payroll_period_id"], _day(period, offset),
          driver, status_key_id, tenant.owner))


def _status_state(tenant, status_key_id):
    ((off, hours, category),) = query(tenant, """
        SELECT isoffreason, hoursvalue, allowancecategory
        FROM payroll.payrollstatuskeys WHERE statuskeyid = %s
    """, (status_key_id,))
    return off, hours, category


@pytest.mark.parametrize("edit", [
    "isoffreason = TRUE", "hoursvalue = 5", "allowancecategory = 'Sick'",
], ids=["off-day semantic", "hours", "category"])
async def test_a_semantic_edit_that_races_the_first_use_of_a_status_is_refused_not_lost(
    p3c_client, p3c_engine, tenant, edit,
):
    """The reviewer's race. Writer A selects a not-yet-used non-off Status for a day and gives
    the day ordinary work, uncommitted. Writer B changes the Status's meaning and must wait for
    A: A's selection is a reference B could not see, and committing the edit past it would leave
    a full-day-off Status next to ordinary work. After A commits, B is refused as in use and the
    committed state is the one A wrote."""
    period, ppd = await _scene(p3c_client, p3c_engine, tenant)
    flip = seed_status_key(tenant, "FLIP", off=False)
    before = _status_state(tenant, flip)
    driver = tenant.driver_a
    assert (await _add_line(p3c_client, tenant, period, ppd, "0")).status_code == 201

    def first_use(cur):
        _insert_selection(cur, tenant, period, driver, flip)
        _set_quantity(cur, period, driver, ppd, 5)

    def semantic_edit(cur):
        cur.execute(f"UPDATE payroll.payrollstatuskeys SET {edit} WHERE statuskeyid = %s", (flip,))

    first, second = await _run_race(
        tenant, first_use, semantic_edit,
        refusal=psycopg2.errors.RestrictViolation, marker="STATUS_KEY_IN_USE")

    assert (first, second) == ("committed", "refused:True")
    assert _status_state(tenant, flip) == before
    assert _quantity_of(tenant, period, driver, ppd) == 5
    assert query(tenant, """
        SELECT count(*) FROM payroll.payrollperioddriverdayentrystate
        WHERE payrollperiodid = %s AND statuskeyid = %s
    """, (period["payroll_period_id"], flip)) == [(1,)]


async def test_a_first_use_that_races_a_semantic_edit_is_checked_against_the_edited_status(
    p3c_client, p3c_engine, tenant,
):
    """The other order. Writer A turns a not-yet-used Status into a full-day-off Status,
    uncommitted; writer B selects it for a day that has ordinary work and must wait for A. After
    A commits, B sees the Status as it now is and is refused, so the conflict never exists."""
    period, ppd = await _scene(p3c_client, p3c_engine, tenant)
    flip = seed_status_key(tenant, "FLIP", off=False)
    driver = tenant.driver_a
    assert (await _add_line(p3c_client, tenant, period, ppd, "5")).status_code == 201

    first, second = await _run_race(
        tenant,
        lambda cur: cur.execute(
            "UPDATE payroll.payrollstatuskeys SET isoffreason = TRUE WHERE statuskeyid = %s", (flip,)),
        lambda cur: _insert_selection(cur, tenant, period, driver, flip))

    assert (first, second) == ("committed", "refused:True")
    assert _status_state(tenant, flip)[0] is True
    assert _quantity_of(tenant, period, driver, ppd) == 5
    assert query(tenant, """
        SELECT count(*) FROM payroll.payrollperioddriverdayentrystate
        WHERE payrollperiodid = %s AND statuskeyid = %s
    """, (period["payroll_period_id"], flip)) == [(0,)]


async def test_a_first_use_after_a_semantic_edit_is_allowed_when_the_day_has_no_work(
    p3c_client, p3c_engine, tenant,
):
    """The serialization is not a prohibition: with no ordinary work on the day, both writers
    commit and the day holds the full-day-off Status."""
    period, _ = await _scene(p3c_client, p3c_engine, tenant)
    flip = seed_status_key(tenant, "FLIP", off=False)

    first, second = await _run_race(
        tenant,
        lambda cur: cur.execute(
            "UPDATE payroll.payrollstatuskeys SET isoffreason = TRUE WHERE statuskeyid = %s", (flip,)),
        lambda cur: _insert_selection(cur, tenant, period, tenant.driver_a, flip))

    assert (first, second) == ("committed", "committed")
    assert _status_state(tenant, flip)[0] is True
    assert _status_of(tenant, period, tenant.driver_a) == "FLIP"


async def test_a_semantic_edit_of_an_already_used_status_is_still_refused(
    p3c_client, p3c_engine, tenant,
):
    """The in-use lock itself is unchanged: once a selection exists the edit is refused at once."""
    period, _ = await _scene(p3c_client, p3c_engine, tenant)
    flip = seed_status_key(tenant, "FLIP", off=False)
    conn = _connect(tenant)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            _insert_selection(cur, tenant, period, tenant.driver_a, flip)
            with pytest.raises(psycopg2.errors.RestrictViolation, match="STATUS_KEY_IN_USE"):
                cur.execute(
                    "UPDATE payroll.payrollstatuskeys SET isoffreason = TRUE WHERE statuskeyid = %s",
                    (flip,))
            cur.execute("UPDATE payroll.payrollstatuskeys SET keyname = 'FLIP renamed', "
                        "normalizedkeyname = 'flip renamed' WHERE statuskeyid = %s", (flip,))
    finally:
        conn.close()
    assert _status_state(tenant, flip)[0] is False


async def test_concurrent_application_writers_never_leave_an_off_day_with_work(
    p3c_client, p3c_engine, tenant,
):
    """An off-Status save and a source-line add for the same day, sent together through the
    API: they serialize on the Period lock and exactly one wins; the day is never both."""
    period, ppd = await _scene(p3c_client, p3c_engine, tenant)
    for offset in range(5):
        status_save, line_add = await asyncio.gather(
            _save(p3c_client, tenant, period, [_row(tenant.driver_a, status="OFF")], offset=offset),
            _add_line(p3c_client, tenant, period, ppd, "3", offset=offset))
        assert sorted([status_save.status_code, line_add.status_code]) in (
            [200, 422], [201, 422]), (status_save.text, line_add.text)
        loser = status_save if status_save.status_code == 422 else line_add
        assert _code(loser) == CONFLICT
        off = _status_of(tenant, period, tenant.driver_a, offset) == "OFF"
        worked = bool(_quantity_of(tenant, period, tenant.driver_a, ppd, offset))
        assert off != worked, (offset, off, worked)
