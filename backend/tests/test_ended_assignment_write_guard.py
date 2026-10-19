"""An ENDED DriverAssignment is historical: every compensation-authoring write is refused.

After a permanent transfer the old home-Branch assignment is read-only. The rule is
enforced by the server for every writer, even for a caller whose Branch permission is
valid and for a Company-wide admin, and it covers the empty never-effective Terminated
window. Reads stay allowed. The current destination assignment and the pending
assignment of a Scheduled transfer stay writable.

Writers (route -> function):
  POST   /compensation/driver-rate-assignments                  assignments.create_pending
  PATCH  /compensation/driver-rate-assignments/{id}             assignments.update_pending
  PUT    /compensation/driver-rate-assignments/{id}/values      assignments.replace_values
  POST   /compensation/driver-rate-assignments/{id}/approve     assignments.approve
  DELETE /compensation/driver-rate-assignments/{id}             assignments.discard
  POST   /compensation/driver-rate-assignments/{id}/void        assignments.void
  POST   /payroll/driver-pay-rules                              driver_pay_rules.create
  POST   /payroll/driver-pay-rules/{id}/end                     driver_pay_rules.end
  PATCH  /payroll/driver-pay-rules/{id}                         driver_pay_rules.notes
  POST   /payroll/driver-pay-rules/{id}/void                    driver_pay_rules.void
  POST   /payroll/rates                                         rates.create_rate
  PATCH  /payroll/rates/{id}                                    rates.update_rate
  POST   /payroll/rates/{id}/approve                            rates.approve_rate
  DELETE /payroll/rates/{id}                                    rates.void_rate
  POST   /payroll/driver-assignments/{id}/rates/batch           rates.batch_save_rates
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from tests.builders.access import (
    create_company_role_with_permissions,
    create_user_with_role_token,
)
from tests.builders.transfer_scenarios import (
    approved_transfer,
    auth,
    db_now,
    make_branch,
    make_driver,
    scheduled_transfer,
    sfx,
)
from tests.db_state import DRIVER_ASSIGNMENT_HISTORY_TRIGGER, suspended_test_triggers

pytestmark = pytest.mark.asyncio

ENDED_MESSAGE = "This Driver assignment has ended; its compensation is historical and read-only."
EFFECTIVE_FROM = "2026-01-01"


# ---------------------------------------------------------------------------
# Scenario building
# ---------------------------------------------------------------------------

async def _definition(client, admin, db, branch_ids) -> dict:
    created = await client.post("/compensation/pay-definitions", json={
        "definition_code": f"EG{sfx()}", "definition_name": "Ended guard",
        "input_type": "Decimal", "unit": "unit", "calculation_method": "PerUnit",
    }, headers=auth(admin))
    assert created.status_code == 201, created.text
    definition = created.json()
    for branch_id in branch_ids:
        await db.execute(text("""
            DELETE FROM payroll.branchpayitemconfig
            WHERE companyid = 1 AND branchid = :b AND paydefinitionid = :d
        """), {"b": branch_id, "d": definition["pay_definition_id"]})
        await db.execute(text("""
            INSERT INTO payroll.branchpayitemconfig
                (companyid, branchid, paydefinitionid, isactive, effectivefrom)
            VALUES (1, :b, :d, TRUE, DATE '2000-01-01')
        """), {"b": branch_id, "d": definition["pay_definition_id"]})
    return definition


async def _columns(client, admin, branch_id, count) -> list[dict]:
    columns = []
    for _ in range(count):
        created = await client.post(
            f"/settings/branches/{branch_id}/status-rate-columns",
            json={"column_name": f"EG {sfx()}"}, headers=auth(admin),
        )
        assert created.status_code in (200, 201), created.text
        columns.append(created.json())
    return columns


async def _expect(response, status):
    assert response.status_code == status, (response.request.method, response.request.url,
                                            response.status_code, response.text)
    return response


async def _pending(client, token, assignment_id, definition, effective_from=EFFECTIVE_FROM) -> int:
    response = await _expect(await client.post(
        "/compensation/driver-rate-assignments",
        json={"driver_assignment_id": assignment_id,
              "rate_definition_id": definition["rate_definition_id"],
              "effective_from": effective_from},
        headers=auth(token)), 201)
    return response.json()["driver_rate_assignment_id"]


async def _values(client, token, assignment_id, definition, amount="25"):
    return await client.put(
        f"/compensation/driver-rate-assignments/{assignment_id}/values",
        json={"values": [{
            "rate_component_definition_id":
                definition["components"][0]["rate_component_definition_id"],
            "amount": amount}]},
        headers=auth(token))


async def _rule(client, token, assignment_id, rule_type, start, end=None) -> int:
    response = await _expect(await client.post("/payroll/driver-pay-rules", json={
        "driver_assignment_id": assignment_id, "rule_type": rule_type, "amount": "50",
        "effective_from": start, "effective_to": end}, headers=auth(token)), 201)
    return response.json()["driver_pay_rule_id"]


async def _rate(client, token, assignment_id, column, effective_from=EFFECTIVE_FROM) -> int:
    response = await _expect(await client.post("/payroll/rates", json={
        "driver_assignment_id": assignment_id, "rate_type_id": column["rate_type_id"],
        "amount": "12", "effective_from": effective_from}, headers=auth(token)), 201)
    return response.json()["driver_rate_id"]


@dataclass
class Seed:
    """Rows seeded on one assignment while it was still writable."""
    assignment_id: int
    definitions: list[dict]
    columns: list[dict]
    comp: dict[str, int] = field(default_factory=dict)
    rules: dict[str, int] = field(default_factory=dict)
    rates: dict[str, int] = field(default_factory=dict)


async def _seed(client, admin, assignment_id, definitions, columns) -> Seed:
    """Pending/Approved rows of every kind, so each writer has a real target."""
    seed = Seed(assignment_id, definitions, columns)
    d_update, d_replace, d_approve, d_discard, d_void, _ = definitions
    for name, definition in (("update", d_update), ("replace", d_replace),
                             ("approve", d_approve), ("discard", d_discard)):
        seed.comp[name] = await _pending(client, admin, assignment_id, definition)
    await _expect(await _values(client, admin, seed.comp["approve"], d_approve), 200)
    approved = await _pending(client, admin, assignment_id, d_void)
    await _expect(await _values(client, admin, approved, d_void), 200)
    await _expect(await client.post(
        f"/compensation/driver-rate-assignments/{approved}/approve", headers=auth(admin)), 200)
    seed.comp["void"] = approved

    seed.rules["end"] = await _rule(client, admin, assignment_id, "MinimumPay",
                                    "2026-01-01", "2026-03-31")
    seed.rules["notes"] = await _rule(client, admin, assignment_id, "MaximumPay",
                                      "2026-01-01", "2026-03-31")
    seed.rules["void"] = await _rule(client, admin, assignment_id, "MinimumPay", "2026-06-01")

    c_update, c_approve, c_void, _, _ = columns
    seed.rates["update"] = await _rate(client, admin, assignment_id, c_update)
    seed.rates["approve"] = await _rate(client, admin, assignment_id, c_approve)
    seed.rates["void"] = await _rate(client, admin, assignment_id, c_void)
    await _expect(await client.post(
        f"/payroll/rates/{seed.rates['void']}/approve", headers=auth(admin)), 200)
    return seed


def _writers(seed: Seed) -> list[tuple[str, str, str, dict | None]]:
    """Every compensation-authoring write against the seeded assignment."""
    d_update, d_replace, d_approve, d_discard, d_void, d_create = seed.definitions
    _, _, _, c_create, c_batch = seed.columns
    comp = "/compensation/driver-rate-assignments"
    return [
        ("comp.create_pending", "POST", comp, {
            "driver_assignment_id": seed.assignment_id,
            "rate_definition_id": d_create["rate_definition_id"],
            "effective_from": EFFECTIVE_FROM}),
        ("comp.update_pending", "PATCH", f"{comp}/{seed.comp['update']}", {"notes": "changed"}),
        ("comp.replace_values", "PUT", f"{comp}/{seed.comp['replace']}/values", {"values": [{
            "rate_component_definition_id":
                d_replace["components"][0]["rate_component_definition_id"],
            "amount": "31"}]}),
        ("comp.approve", "POST", f"{comp}/{seed.comp['approve']}/approve", None),
        ("comp.discard", "DELETE", f"{comp}/{seed.comp['discard']}", None),
        ("comp.void", "POST", f"{comp}/{seed.comp['void']}/void", {"reason": "mistake"}),
        ("rule.create", "POST", "/payroll/driver-pay-rules", {
            "driver_assignment_id": seed.assignment_id, "rule_type": "MaximumPay",
            "amount": "70", "effective_from": "2026-08-01", "effective_to": "2026-08-31"}),
        ("rule.end", "POST", f"/payroll/driver-pay-rules/{seed.rules['end']}/end",
         {"effective_to": "2026-02-15"}),
        ("rule.notes", "PATCH", f"/payroll/driver-pay-rules/{seed.rules['notes']}",
         {"notes": "changed"}),
        ("rule.void", "POST", f"/payroll/driver-pay-rules/{seed.rules['void']}/void", None),
        ("rate.create", "POST", "/payroll/rates", {
            "driver_assignment_id": seed.assignment_id, "rate_type_id": c_create["rate_type_id"],
            "amount": "13", "effective_from": "2026-03-01"}),
        ("rate.update", "PATCH", f"/payroll/rates/{seed.rates['update']}", {"amount": "14"}),
        ("rate.approve", "POST", f"/payroll/rates/{seed.rates['approve']}/approve", None),
        ("rate.void", "DELETE", f"/payroll/rates/{seed.rates['void']}", None),
        ("rate.batch", "POST", f"/payroll/driver-assignments/{seed.assignment_id}/rates/batch", {
            "effective_from": "2026-04-01",
            "changes": [{"status_rate_column_id": c_batch["status_rate_column_id"],
                         "rate_type_id": c_batch["rate_type_id"], "amount": "9"}]}),
    ]


async def _fingerprint(db, assignment_id) -> str:
    """Every compensation row owned by one DriverAssignment, as comparable text."""
    parts = []
    for query in (
        "SELECT to_jsonb(r) FROM payroll.driverrateassignments r "
        "WHERE r.driverassignmentid = :a ORDER BY r.driverrateassignmentid",
        "SELECT to_jsonb(v) FROM payroll.driverratevalues v "
        "JOIN payroll.driverrateassignments r "
        "  ON r.driverrateassignmentid = v.driverrateassignmentid "
        "WHERE r.driverassignmentid = :a ORDER BY v.driverrateassignmentid, v.ratecomponentdefinitionid",
        "SELECT to_jsonb(p) FROM payroll.driverpayrules p "
        "WHERE p.driverassignmentid = :a ORDER BY p.driverpayruleid",
        "SELECT to_jsonb(d) FROM payroll.driverrates d "
        "WHERE d.driverassignmentid = :a ORDER BY d.driverrateid",
    ):
        rows = (await db.execute(text(query), {"a": assignment_id})).scalars().all()
        parts.append(rows)
    return json.dumps(parts, sort_keys=True, default=str)


@dataclass
class World:
    admin: str
    old_manager: str
    new_manager: str
    old_branch: int
    new_branch: int
    driver_id: int
    employee_id: int
    old_assignment: int
    new_assignment: int
    seed: Seed
    definitions: list[dict]
    columns: dict[int, list[dict]]


async def _manager(client, admin, role_id, branch_id) -> str:
    return await create_user_with_role_token(
        client, admin, f"eg_{sfx()}", role_id, scope_type="SpecificBranch", branch_id=branch_id)


async def _build_world(client, admin, db) -> World:
    """A Driver with seeded compensation on the old assignment, then a permanent transfer."""
    old_branch, new_branch = await make_branch(db), await make_branch(db)
    definitions = [await _definition(client, admin, db, (old_branch, new_branch))
                   for _ in range(6)]
    columns = {old_branch: await _columns(client, admin, old_branch, 5),
               new_branch: await _columns(client, admin, new_branch, 2)}
    role_id = await create_company_role_with_permissions(
        client, admin, f"EG manager {sfx()}",
        ["drivers.view", "payrates.view", "payrates.edit", "payitems.view"])
    old_manager = await _manager(client, admin, role_id, old_branch)
    new_manager = await _manager(client, admin, role_id, new_branch)

    driver = await make_driver(client, admin, old_branch)
    seed = await _seed(client, admin, driver["assignment_id"], definitions, columns[old_branch])

    request_id = await approved_transfer(client, admin, driver["driver_id"], new_branch, None)
    completed = await client.post(
        f"/driver-transfers/{request_id}/complete", json={}, headers=auth(admin))
    await _expect(completed, 200)
    body = completed.json()
    assert body["status"] == "Completed", body
    assert body["source_driver_assignment_id"] == driver["assignment_id"]
    ended_at = (await db.execute(text(
        "SELECT EffectiveToAt FROM core.DriverAssignments WHERE DriverAssignmentID = :a"
    ), {"a": driver["assignment_id"]})).scalar_one()
    assert ended_at <= await db_now(db)
    return World(
        admin=admin, old_manager=old_manager, new_manager=new_manager,
        old_branch=old_branch, new_branch=new_branch, driver_id=driver["driver_id"],
        employee_id=driver["employee_id"], old_assignment=driver["assignment_id"],
        new_assignment=body["destination_driver_assignment_id"], seed=seed,
        definitions=definitions, columns=columns,
    )


async def _assert_all_writers_refused(client, db, seed: Seed, token: str) -> None:
    failures = []
    for name, method, path, body in _writers(seed):
        before = await _fingerprint(db, seed.assignment_id)
        response = await client.request(method, path, json=body, headers=auth(token))
        if response.status_code != 409:
            failures.append(f"{name}: expected 409, got {response.status_code}")
            continue
        detail = response.json()["detail"]
        if detail != {"code": "DRIVER_ASSIGNMENT_ENDED", "message": ENDED_MESSAGE}:
            failures.append(f"{name}: wrong refusal {detail}")
        elif await _fingerprint(db, seed.assignment_id) != before:
            failures.append(f"{name}: refused but changed rows")
    assert not failures, "; ".join(failures)


# ---------------------------------------------------------------------------
# Reads stay allowed; every writer is refused
# ---------------------------------------------------------------------------

async def test_the_old_branch_manager_can_still_read_the_ended_assignments_compensation(
    client, auth_token, direct_db,
):
    world = await _build_world(client, auth_token, direct_db)
    headers = auth(world.old_manager)
    old, seed = world.old_assignment, world.seed
    definition = world.definitions[4]
    reads = [
        f"/compensation/driver-assignments/{old}/pay-rates",
        f"/compensation/driver-assignments/{old}/rate-assignments/summary",
        f"/compensation/driver-assignments/{old}/rate-definitions/"
        f"{definition['rate_definition_id']}/assignments",
        f"/compensation/driver-assignments/{old}/rate-definitions/"
        f"{definition['rate_definition_id']}/resolution?work_date=2026-02-01",
        f"/compensation/driver-rate-assignments/{seed.comp['void']}",
        f"/payroll/driver-assignments/{old}/pay-rules",
        f"/payroll/driver-pay-rules/{seed.rules['end']}",
        f"/payroll/rates?branch_id={world.old_branch}&driver_assignment_id={old}",
        f"/payroll/rates/{seed.rates['void']}",
        f"/payroll/driver-assignments/{old}/rates/history",
        f"/payroll/driver-assignments/{old}/rates/pending",
        f"/payroll/driver-assignments/{old}/rates/summary",
        f"/core/drivers/{world.driver_id}/assignments",
    ]
    for path in reads:
        response = await client.get(path, headers=headers)
        assert response.status_code == 200, (path, response.status_code, response.text)
    history = (await client.get(
        f"/compensation/driver-assignments/{old}/rate-definitions/"
        f"{definition['rate_definition_id']}/assignments", headers=headers)).json()
    assert [row["driver_rate_assignment_id"] for row in history] == [seed.comp["void"]]
    rules = (await client.get(f"/payroll/driver-assignments/{old}/pay-rules", headers=headers)).json()
    assert len(rules) == 3


async def test_every_writer_is_refused_on_an_ended_assignment_even_for_authorized_callers(
    client, auth_token, direct_db,
):
    world = await _build_world(client, auth_token, direct_db)
    seed = world.seed
    # The old-Branch manager keeps valid Branch permission: the refusal is the assignment's.
    await _assert_all_writers_refused(client, direct_db, seed, world.old_manager)
    # A Company-wide admin is refused the same way.
    await _assert_all_writers_refused(client, direct_db, seed, world.admin)


async def test_an_unauthorized_caller_is_denied_before_the_ended_state_is_revealed(
    client, auth_token, direct_db,
):
    world = await _build_world(client, auth_token, direct_db)
    # The destination-Branch manager has no permission on the old Branch's assignment.
    for name, method, path, body in _writers(world.seed):
        response = await client.request(method, path, json=body, headers=auth(world.new_manager))
        assert response.status_code in (403, 404), (name, response.status_code, response.text)
        assert "DRIVER_ASSIGNMENT_ENDED" not in response.text, name


async def test_an_empty_never_effective_terminated_assignment_is_read_only(
    client, auth_token, direct_db,
):
    old_branch = await make_branch(direct_db)
    definitions = [await _definition(client, auth_token, direct_db, (old_branch,))
                   for _ in range(6)]
    columns = await _columns(client, auth_token, old_branch, 5)
    role_id = await create_company_role_with_permissions(
        client, auth_token, f"EG manager {sfx()}",
        ["drivers.view", "payrates.view", "payrates.edit", "payitems.view"])
    manager = await _manager(client, auth_token, role_id, old_branch)
    driver = await make_driver(client, auth_token, old_branch)
    seed = await _seed(client, auth_token, driver["assignment_id"], definitions, columns)

    # What termination does to an assignment that starts at or after its cutoff: the
    # window collapses to the empty Terminated window [start, start).
    async with suspended_test_triggers(direct_db, (DRIVER_ASSIGNMENT_HISTORY_TRIGGER,)):
        await direct_db.execute(text("""
            UPDATE core.DriverAssignments
            SET EffectiveFromAt = NOW() - INTERVAL '1 day',
                EffectiveToAt = NOW() - INTERVAL '1 day', EndReason = 'Terminated'
            WHERE DriverAssignmentID = :a
        """), {"a": driver["assignment_id"]})
    window = (await direct_db.execute(text(
        "SELECT EffectiveFromAt = EffectiveToAt FROM core.DriverAssignments "
        "WHERE DriverAssignmentID = :a"), {"a": driver["assignment_id"]})).scalar_one()
    assert window is True

    await _assert_all_writers_refused(client, direct_db, seed, manager)
    await _assert_all_writers_refused(client, direct_db, seed, auth_token)
    readable = await client.get(
        f"/payroll/driver-assignments/{driver['assignment_id']}/pay-rules", headers=auth(manager))
    assert readable.status_code == 200, readable.text


# ---------------------------------------------------------------------------
# Positive controls: current and pending assignments stay writable
# ---------------------------------------------------------------------------

async def _exercise_every_writer(client, token, assignment_id, definitions, columns, tag):
    """Run the full authoring lifecycle: each writer must succeed on a writable assignment."""
    d_a, d_b = definitions[0], definitions[1]
    c_rate, c_batch = columns
    comp = "/compensation/driver-rate-assignments"

    first = await _pending(client, token, assignment_id, d_a)
    await _expect(await _values(client, token, first, d_a), 200)
    await _expect(await client.patch(f"{comp}/{first}", json={"notes": tag}, headers=auth(token)), 200)
    await _expect(await client.post(f"{comp}/{first}/approve", headers=auth(token)), 200)
    await _expect(await client.post(
        f"{comp}/{first}/void", json={"reason": "control"}, headers=auth(token)), 200)
    second = await _pending(client, token, assignment_id, d_b)
    await _expect(await _values(client, token, second, d_b, "26"), 200)
    await _expect(await client.delete(f"{comp}/{second}", headers=auth(token)), 204)

    minimum = await _rule(client, token, assignment_id, "MinimumPay", "2026-01-01", "2026-03-31")
    await _expect(await client.patch(
        f"/payroll/driver-pay-rules/{minimum}", json={"notes": tag}, headers=auth(token)), 200)
    await _expect(await client.post(
        f"/payroll/driver-pay-rules/{minimum}/end", json={"effective_to": "2026-02-15"},
        headers=auth(token)), 200)
    maximum = await _rule(client, token, assignment_id, "MaximumPay", "2026-01-01")
    await _expect(await client.post(
        f"/payroll/driver-pay-rules/{maximum}/void", headers=auth(token)), 200)

    rate = await _rate(client, token, assignment_id, c_rate)
    await _expect(await client.patch(
        f"/payroll/rates/{rate}", json={"amount": "15"}, headers=auth(token)), 200)
    await _expect(await client.post(f"/payroll/rates/{rate}/approve", headers=auth(token)), 200)
    await _expect(await client.delete(f"/payroll/rates/{rate}", headers=auth(token)), 204)
    await _expect(await client.post(
        f"/payroll/driver-assignments/{assignment_id}/rates/batch", json={
            "effective_from": "2026-04-01",
            "changes": [{"status_rate_column_id": c_batch["status_rate_column_id"],
                         "rate_type_id": c_batch["rate_type_id"], "amount": "9"}]},
        headers=auth(token)), 200)


async def test_the_current_destination_assignment_stays_writable(client, auth_token, direct_db):
    world = await _build_world(client, auth_token, direct_db)
    # The destination-Branch manager (valid Branch permission) and the admin both succeed.
    await _exercise_every_writer(
        client, world.new_manager, world.new_assignment, world.definitions,
        world.columns[world.new_branch], "destination")
    # ... while the ended source is still refused.
    await _assert_all_writers_refused(client, direct_db, world.seed, world.old_manager)


async def test_pending_destination_and_current_source_of_a_scheduled_transfer_stay_writable(
    client, auth_token, direct_db,
):
    source_branch, target_branch = await make_branch(direct_db), await make_branch(direct_db)
    definitions = [await _definition(client, auth_token, direct_db, (source_branch, target_branch))
                   for _ in range(2)]
    source_columns = await _columns(client, auth_token, source_branch, 2)
    target_columns = await _columns(client, auth_token, target_branch, 2)
    driver = await make_driver(client, auth_token, source_branch)
    scheduled = await scheduled_transfer(
        client, auth_token, driver["driver_id"], target_branch,
        await db_now(direct_db) + timedelta(days=3))

    ends = (await direct_db.execute(text("""
        SELECT EffectiveToAt > clock_timestamp(), EndReason
        FROM core.DriverAssignments WHERE DriverAssignmentID = :a
    """), {"a": scheduled["source_id"]})).one()
    assert tuple(ends) == (True, "Transferred")  # current, its Transferred end still ahead

    await _exercise_every_writer(
        client, auth_token, scheduled["destination_id"], definitions, target_columns, "pending")
    await _exercise_every_writer(
        client, auth_token, scheduled["source_id"], definitions, source_columns, "source")


# ---------------------------------------------------------------------------
# The guard decides on wall-clock time, not the transaction-start instant
# ---------------------------------------------------------------------------

async def test_the_guard_uses_clock_time_not_the_transaction_start(
    client, auth_token, direct_db, test_engine,
):
    from app.workforce.effective import require_writable_driver_assignment

    branch_id = await make_branch(direct_db)
    driver = await make_driver(client, auth_token, branch_id)
    assignment_id = driver["assignment_id"]
    async with suspended_test_triggers(direct_db, (DRIVER_ASSIGNMENT_HISTORY_TRIGGER,)):
        await direct_db.execute(text("""
            UPDATE core.DriverAssignments
            SET EffectiveToAt = clock_timestamp() + INTERVAL '2 seconds', EndReason = 'Terminated'
            WHERE DriverAssignmentID = :a
        """), {"a": assignment_id})
    ends_at = (await direct_db.execute(text(
        "SELECT EffectiveToAt FROM core.DriverAssignments WHERE DriverAssignmentID = :a"
    ), {"a": assignment_id})).scalar_one()

    async with test_engine.begin() as conn:
        transaction_start = (await conn.execute(text("SELECT NOW()"))).scalar_one()
        assert transaction_start < ends_at
        # Still ahead: the assignment is writable.
        await require_writable_driver_assignment(1, assignment_id, conn)

        # The assignment ends while this transaction is open.
        while (await conn.execute(text("SELECT clock_timestamp()"))).scalar_one() <= ends_at:
            await asyncio.sleep(0.2)
        assert (await conn.execute(text("SELECT NOW()"))).scalar_one() == transaction_start
        assert transaction_start < ends_at  # NOW() would still call it open
        with pytest.raises(HTTPException) as refused:
            await require_writable_driver_assignment(1, assignment_id, conn)
    assert refused.value.status_code == 409
    assert refused.value.detail == {"code": "DRIVER_ASSIGNMENT_ENDED", "message": ENDED_MESSAGE}


async def test_a_missing_or_foreign_assignment_is_not_found(test_engine):
    from app.workforce.effective import require_writable_driver_assignment

    async with test_engine.begin() as conn:
        with pytest.raises(HTTPException) as missing:
            await require_writable_driver_assignment(1, 2_000_000_000, conn)
    assert missing.value.status_code == 404
