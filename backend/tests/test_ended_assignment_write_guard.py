"""Historical compensation is read-only after a permanent transfer.

All compensation is Driver-owned (People contract section 9.1): ordinary rate schedules, Status
rate schedules (a schedule of a Company Status rate column) and Min/Max pay rules. A change is
refused with ``DRIVER_COMPENSATION_HISTORICAL`` when its start date falls in a tenure that has
ended (`trg_DriverCompensationContextWritable`). Schedules and pay rules are owned by the
permanent Driver, so the transfer itself moves nothing; only dates inside the ended tenure become
read-only. A pay rule's notes edit has no payroll effect and stays allowed.

The rule is enforced by the server for every writer, even for a caller whose Branch permission
is valid and for a Company-wide admin, and it covers the empty never-effective Terminated
window. Reads stay allowed. The current destination and the pending destination of a Scheduled
transfer stay writable.

Writers (route -> function):
  POST   /compensation/driver-rate-schedules                  schedules.create_pending
  PATCH  /compensation/driver-rate-schedules/{id}             schedules.update_pending
  PUT    /compensation/driver-rate-schedules/{id}/values      schedules.replace_values
  POST   /compensation/driver-rate-schedules/{id}/approve     schedules.approve
  DELETE /compensation/driver-rate-schedules/{id}             schedules.discard (a Pending row has
                                                              no payroll effect: its own rule)
  POST   /compensation/driver-rate-schedules/{id}/void        schedules.void
  POST   /payroll/driver-pay-rules                            driver_pay_rules.create
  POST   /payroll/driver-pay-rules/{id}/end                   driver_pay_rules.end
  POST   /payroll/driver-pay-rules/{id}/void                  driver_pay_rules.void
  PATCH  /payroll/driver-pay-rules/{id}                       driver_pay_rules.notes (no payroll
                                                              effect: its own rule)

Each schedule writer is exercised against both an ordinary PayDefinition rate and a Status rate
column rate.
"""

import json
from dataclasses import dataclass, field
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from tests.builders.access import (
    create_company_role_with_permissions,
    create_user_with_role_token,
)
from tests.builders.pay_rules import ensure_branch_pay_calendar
from tests.builders.transfer_scenarios import (
    approved_transfer,
    auth,
    branch_today,
    db_now,
    make_branch,
    make_driver,
    scheduled_transfer,
    sfx,
)
from tests.db_state import DRIVER_ASSIGNMENT_HISTORY_TRIGGER, suspended_test_triggers

pytestmark = pytest.mark.asyncio

ENDED_MESSAGE = "This Driver assignment has ended; its compensation is historical and read-only."
HISTORICAL_MESSAGE = ("This compensation change starts in a Driver tenure that has ended; "
                      "historical compensation is read-only.")
# Inside the first tenure (every scenario driver starts 2020-01-01) and before any transfer.
EFFECTIVE_FROM = "2026-01-01"


# ---------------------------------------------------------------------------
# Scenario building
# ---------------------------------------------------------------------------

async def _definition(client, admin) -> dict:
    created = await client.post("/compensation/pay-definitions", json={
        "definition_code": f"EG{sfx()}", "definition_name": "Ended guard",
        "input_type": "Decimal", "unit": "unit", "calculation_method": "PerUnit",
    }, headers=auth(admin))
    assert created.status_code == 201, created.text
    return created.json()


async def _columns(client, admin, count) -> list[dict]:
    """Company Status rate columns, shaped like a definition (RateDefinition + one component)."""
    columns = []
    for _ in range(count):
        created = await client.post(
            "/settings/status-rate-columns", json={"column_name": f"EG {sfx()}"},
            headers=auth(admin))
        assert created.status_code in (200, 201), created.text
        body = created.json()
        columns.append({
            "status_rate_column_id": body["status_rate_column_id"],
            "rate_definition_id": body["rate_definition_id"],
            "components": [{"rate_component_definition_id": body["rate_component_definition_id"]}],
        })
    return columns


async def _expect(response, status):
    assert response.status_code == status, (response.request.method, response.request.url,
                                            response.status_code, response.text)
    return response


async def _pending(client, token, driver_id, definition, effective_from=EFFECTIVE_FROM) -> int:
    response = await _expect(await client.post(
        "/compensation/driver-rate-schedules",
        json={"driver_id": driver_id,
              "rate_definition_id": definition["rate_definition_id"],
              "effective_from": effective_from},
        headers=auth(token)), 201)
    return response.json()["driver_rate_schedule_id"]


async def _values(client, token, schedule_id, definition, amount="25"):
    return await client.put(
        f"/compensation/driver-rate-schedules/{schedule_id}/values",
        json={"values": [{
            "rate_component_definition_id":
                definition["components"][0]["rate_component_definition_id"],
            "amount": amount}]},
        headers=auth(token))


async def _rule(client, token, driver_id, rule_type, start, end=None) -> int:
    response = await _expect(await client.post("/payroll/driver-pay-rules", json={
        "driver_id": driver_id, "rule_type": rule_type, "amount": "50",
        "effective_from": start, "effective_to": end}, headers=auth(token)), 201)
    return response.json()["driver_pay_rule_id"]


@dataclass
class Seed:
    """Rows seeded while the first tenure was still writable."""
    driver_id: int
    assignment_id: int
    definitions: list[dict]
    columns: list[dict]
    comp: dict[str, int] = field(default_factory=dict)
    rules: dict[str, int] = field(default_factory=dict)
    status: dict[str, int] = field(default_factory=dict)


async def _seed(client, admin, driver_id, assignment_id, definitions, columns) -> Seed:
    """Pending/Approved rows of every kind, so each writer has a real target."""
    seed = Seed(driver_id, assignment_id, definitions, columns)
    d_update, d_replace, d_approve, d_discard, d_void, _ = definitions
    for name, definition in (("update", d_update), ("replace", d_replace),
                             ("approve", d_approve), ("discard", d_discard)):
        seed.comp[name] = await _pending(client, admin, driver_id, definition)
    await _expect(await _values(client, admin, seed.comp["approve"], d_approve), 200)
    approved = await _pending(client, admin, driver_id, d_void)
    await _expect(await _values(client, admin, approved, d_void), 200)
    await _expect(await client.post(
        f"/compensation/driver-rate-schedules/{approved}/approve", headers=auth(admin)), 200)
    seed.comp["void"] = approved

    seed.rules["end"] = await _rule(client, admin, driver_id, "MinimumPay", "2026-01-01", "2026-03-31")
    seed.rules["notes"] = await _rule(client, admin, driver_id, "MaximumPay", "2026-01-01", "2026-03-31")
    seed.rules["void"] = await _rule(client, admin, driver_id, "MinimumPay", "2026-06-01")

    c_update, c_replace, c_approve, c_void, _ = columns
    for name, column in (("update", c_update), ("replace", c_replace), ("approve", c_approve)):
        seed.status[name] = await _pending(client, admin, driver_id, column)
    await _expect(await _values(client, admin, seed.status["approve"], c_approve), 200)
    approved = await _pending(client, admin, driver_id, c_void)
    await _expect(await _values(client, admin, approved, c_void), 200)
    await _expect(await client.post(
        f"/compensation/driver-rate-schedules/{approved}/approve", headers=auth(admin)), 200)
    seed.status["void"] = approved
    return seed


def _writers(seed: Seed) -> list[tuple[str, str, str, dict | None, str]]:
    """Every authoring write the ended tenure refuses: (name, method, path, body, code).

    ``DELETE`` of a Pending schedule is not here: it has no payroll effect and follows its own
    rule (authority over the Driver now), tested below.
    """
    d_update, d_replace, d_approve, d_discard, d_void, d_create = seed.definitions
    _, c_replace, _, _, c_create = seed.columns
    comp = "/compensation/driver-rate-schedules"
    hist = "DRIVER_COMPENSATION_HISTORICAL"
    return [
        ("comp.create_pending", "POST", comp, {
            "driver_id": seed.driver_id,
            "rate_definition_id": d_create["rate_definition_id"],
            "effective_from": EFFECTIVE_FROM}, hist),
        ("comp.update_pending", "PATCH", f"{comp}/{seed.comp['update']}", {"notes": "changed"}, hist),
        ("comp.replace_values", "PUT", f"{comp}/{seed.comp['replace']}/values", {"values": [{
            "rate_component_definition_id":
                d_replace["components"][0]["rate_component_definition_id"],
            "amount": "31"}]}, hist),
        ("comp.approve", "POST", f"{comp}/{seed.comp['approve']}/approve", None, hist),
        ("comp.void", "POST", f"{comp}/{seed.comp['void']}/void", {"reason": "mistake"}, hist),
        ("rule.create", "POST", "/payroll/driver-pay-rules", {
            "driver_id": seed.driver_id, "rule_type": "MaximumPay", "amount": "70",
            "effective_from": "2026-08-01", "effective_to": "2026-08-31"}, hist),
        ("rule.end", "POST", f"/payroll/driver-pay-rules/{seed.rules['end']}/end",
         {"effective_to": "2026-02-15"}, hist),
        ("rule.void", "POST", f"/payroll/driver-pay-rules/{seed.rules['void']}/void", None, hist),
        ("status.create_pending", "POST", comp, {
            "driver_id": seed.driver_id,
            "rate_definition_id": c_create["rate_definition_id"],
            "effective_from": EFFECTIVE_FROM}, hist),
        ("status.update_pending", "PATCH", f"{comp}/{seed.status['update']}",
         {"notes": "changed"}, hist),
        ("status.replace_values", "PUT", f"{comp}/{seed.status['replace']}/values", {"values": [{
            "rate_component_definition_id":
                c_replace["components"][0]["rate_component_definition_id"],
            "amount": "31"}]}, hist),
        ("status.approve", "POST", f"{comp}/{seed.status['approve']}/approve", None, hist),
        ("status.void", "POST", f"{comp}/{seed.status['void']}/void", {"reason": "mistake"}, hist),
    ]


async def _fingerprint(db, seed: Seed) -> str:
    """Every compensation row of the Driver: ordinary and Status schedules, and pay rules."""
    parts = []
    for query, key in (
        ("SELECT to_jsonb(r) FROM payroll.driverrateschedules r "
         "WHERE r.driverid = :a ORDER BY r.driverratescheduleid", seed.driver_id),
        ("SELECT to_jsonb(v) FROM payroll.driverrateschedulevalues v "
         "JOIN payroll.driverrateschedules r "
         "  ON r.driverratescheduleid = v.driverratescheduleid "
         "WHERE r.driverid = :a ORDER BY v.driverratescheduleid, v.ratecomponentdefinitionid",
         seed.driver_id),
        ("SELECT to_jsonb(p) FROM payroll.driverpayrules p "
         "WHERE p.driverid = :a ORDER BY p.driverpayruleid", seed.driver_id),
    ):
        rows = (await db.execute(text(query), {"a": key})).scalars().all()
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
    columns: list[dict]


async def _manager(client, admin, role_id, branch_id) -> str:
    return await create_user_with_role_token(
        client, admin, f"eg_{sfx()}", role_id, scope_type="SpecificBranch", branch_id=branch_id)


async def _build_world(client, admin, db) -> World:
    """A Driver with seeded compensation on the old assignment, then a permanent transfer."""
    old_branch, new_branch = await make_branch(db), await make_branch(db)
    definitions = [await _definition(client, admin) for _ in range(6)]
    for branch_id in (old_branch, new_branch):
        await ensure_branch_pay_calendar(db, 1, branch_id)
    columns = await _columns(client, admin, 5)
    role_id = await create_company_role_with_permissions(
        client, admin, f"EG manager {sfx()}",
        ["drivers.view", "payrates.view", "payrates.edit", "payitems.view"])
    old_manager = await _manager(client, admin, role_id, old_branch)
    new_manager = await _manager(client, admin, role_id, new_branch)

    driver = await make_driver(client, admin, old_branch)
    seed = await _seed(
        client, admin, driver["driver_id"], driver["assignment_id"], definitions, columns)

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
    for name, method, path, body, code in _writers(seed):
        before = await _fingerprint(db, seed)
        response = await client.request(method, path, json=body, headers=auth(token))
        if response.status_code != 409:
            failures.append(f"{name}: expected 409, got {response.status_code} {response.text}")
            continue
        detail = response.json()["detail"]
        message = HISTORICAL_MESSAGE if code == "DRIVER_COMPENSATION_HISTORICAL" else ENDED_MESSAGE
        if detail != {"code": code, "message": message}:
            failures.append(f"{name}: wrong refusal {detail}")
        elif await _fingerprint(db, seed) != before:
            failures.append(f"{name}: refused but changed rows")
    assert not failures, "; ".join(failures)


# ---------------------------------------------------------------------------
# Reads stay allowed; every writer is refused
# ---------------------------------------------------------------------------

async def test_the_old_branch_manager_can_still_read_the_ended_tenures_compensation(
    client, auth_token, direct_db,
):
    world = await _build_world(client, auth_token, direct_db)
    headers = auth(world.old_manager)
    seed = world.seed
    definition = world.definitions[4]
    driver = world.driver_id
    reads = [
        f"/compensation/drivers/{driver}/pay-rates",
        f"/compensation/drivers/{driver}/rate-schedules/summary",
        f"/compensation/drivers/{driver}/rate-definitions/"
        f"{definition['rate_definition_id']}/rate-schedules",
        f"/compensation/drivers/{driver}/rate-definitions/"
        f"{definition['rate_definition_id']}/resolution?work_date=2026-02-01",
        f"/compensation/driver-rate-schedules/{seed.comp['void']}",
        f"/payroll/drivers/{driver}/pay-rules",
        f"/payroll/driver-pay-rules/{seed.rules['end']}",
        f"/compensation/driver-rate-schedules/{seed.status['void']}",
        f"/compensation/drivers/{driver}/rate-definitions/"
        f"{world.columns[3]['rate_definition_id']}/rate-schedules",
    ]
    for path in reads:
        response = await client.get(path, headers=headers)
        assert response.status_code == 200, (path, response.status_code, response.text)
    roster = await client.get(
        "/core/driver-assignments", params={"branch_id": world.old_branch}, headers=headers,
    )
    assert roster.status_code == 200, roster.text
    history = (await client.get(
        f"/compensation/drivers/{driver}/rate-definitions/"
        f"{definition['rate_definition_id']}/rate-schedules", headers=headers)).json()
    assert [row["driver_rate_schedule_id"] for row in history] == [seed.comp["void"]]
    summary = (await client.get(
        f"/compensation/drivers/{driver}/pay-rates", headers=headers)).json()
    assert {row["kind"] for row in summary} >= {"PayDefinition", "StatusRateColumn"}
    rules = (await client.get(f"/payroll/drivers/{driver}/pay-rules", headers=headers)).json()
    assert len(rules) == 3
    # A Pending row is never visible to a Branch whose tenure has ended.
    for pending in (seed.comp["update"], seed.status["update"]):
        hidden = await client.get(f"/compensation/driver-rate-schedules/{pending}", headers=headers)
        assert hidden.status_code == 404


async def test_every_writer_is_refused_in_an_ended_tenure_even_for_authorized_callers(
    client, auth_token, direct_db,
):
    world = await _build_world(client, auth_token, direct_db)
    seed = world.seed
    # The old-Branch manager keeps valid Branch permission: the refusal is the assignment's.
    await _assert_all_writers_refused(client, direct_db, seed, world.old_manager)
    # A Company-wide admin is refused the same way.
    await _assert_all_writers_refused(client, direct_db, seed, world.admin)


async def test_a_pay_rule_notes_edit_has_no_payroll_effect_and_needs_only_current_authority(
    client, auth_token, direct_db,
):
    world = await _build_world(client, auth_token, direct_db)
    rule = f"/payroll/driver-pay-rules/{world.seed.rules['notes']}"
    # The rule starts inside the ended tenure, yet its notes may be edited by the Driver's
    # current authority Branch and by company-wide roles: it changes no payroll result.
    for token in (world.new_manager, auth_token):
        edited = await client.patch(rule, json={"notes": "still editable"}, headers=auth(token))
        assert edited.status_code == 200, (token, edited.text)
    # The past Branch holds no authority over the Driver any more.
    refused = await client.patch(rule, json={"notes": "no"}, headers=auth(world.old_manager))
    assert refused.status_code == 403


async def test_an_unauthorized_caller_is_denied_before_the_ended_state_is_revealed(
    client, auth_token, direct_db,
):
    world = await _build_world(client, auth_token, direct_db)
    # The destination-Branch manager has no permission on the old Branch's assignment.
    for name, method, path, body, _ in _writers(world.seed):
        response = await client.request(method, path, json=body, headers=auth(world.new_manager))
        assert response.status_code in (403, 404), (name, response.status_code, response.text)
        assert "DRIVER_ASSIGNMENT_ENDED" not in response.text, name
        assert "DRIVER_COMPENSATION_HISTORICAL" not in response.text, name


async def test_an_empty_never_effective_terminated_assignment_is_read_only(
    client, auth_token, direct_db,
):
    old_branch = await make_branch(direct_db)
    definitions = [await _definition(client, auth_token) for _ in range(6)]
    await ensure_branch_pay_calendar(direct_db, 1, old_branch)
    columns = await _columns(client, auth_token, 5)
    role_id = await create_company_role_with_permissions(
        client, auth_token, f"EG manager {sfx()}",
        ["drivers.view", "payrates.view", "payrates.edit", "payitems.view"])
    manager = await _manager(client, auth_token, role_id, old_branch)
    driver = await make_driver(client, auth_token, old_branch)
    seed = await _seed(
        client, auth_token, driver["driver_id"], driver["assignment_id"], definitions, columns)

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

    # No non-empty tenure is left in the Branch, so its manager has no standing over the
    # Driver-owned compensation (403; a pay-rule end/void it cannot even see is 404). A
    # Company-wide admin is refused everywhere (409).
    await _assert_all_writers_refused(client, direct_db, seed, auth_token)
    for name, method, path, body, code in _writers(seed):
        response = await client.request(method, path, json=body, headers=auth(manager))
        expected = 403
        if name in ("rule.end", "rule.void"):
            expected = 404  # a rule the caller cannot see is indistinguishable from a missing one
        assert response.status_code == expected, (name, response.status_code, response.text)


# ---------------------------------------------------------------------------
# Positive controls: current and pending assignments stay writable
# ---------------------------------------------------------------------------

async def _exercise_every_writer(client, token, driver_id, definitions, columns, tag, start):
    """Run the full authoring lifecycle: each writer must succeed on a writable tenure.

    ``start`` is a date inside the writable tenure (schedules are judged by their start date).
    """
    d_a, d_b = definitions[0], definitions[1]
    c_a, c_b = columns
    comp = "/compensation/driver-rate-schedules"

    first = await _pending(client, token, driver_id, d_a, start)
    await _expect(await _values(client, token, first, d_a), 200)
    await _expect(await client.patch(f"{comp}/{first}", json={"notes": tag}, headers=auth(token)), 200)
    await _expect(await client.post(f"{comp}/{first}/approve", headers=auth(token)), 200)
    await _expect(await client.post(
        f"{comp}/{first}/void", json={"reason": "control"}, headers=auth(token)), 200)
    second = await _pending(client, token, driver_id, d_b, start)
    await _expect(await _values(client, token, second, d_b, "26"), 200)
    await _expect(await client.delete(f"{comp}/{second}", headers=auth(token)), 204)

    # Rules of one Driver may not overlap per type, so each exercise takes its own days.
    day = date.fromisoformat(start)
    first = await _rule(client, token, driver_id, "MaximumPay", str(day), str(day + timedelta(days=1)))
    await _expect(await client.patch(
        f"/payroll/driver-pay-rules/{first}", json={"notes": tag}, headers=auth(token)), 200)
    await _expect(await client.post(
        f"/payroll/driver-pay-rules/{first}/end", json={"effective_to": str(day)},
        headers=auth(token)), 200)
    second = await _rule(
        client, token, driver_id, "MaximumPay", str(day + timedelta(days=2)),
        str(day + timedelta(days=2)))
    await _expect(await client.post(
        f"/payroll/driver-pay-rules/{second}/void", headers=auth(token)), 200)

    status = await _pending(client, token, driver_id, c_a, start)
    await _expect(await _values(client, token, status, c_a), 200)
    await _expect(await client.patch(f"{comp}/{status}", json={"notes": tag}, headers=auth(token)), 200)
    await _expect(await client.post(f"{comp}/{status}/approve", headers=auth(token)), 200)
    await _expect(await client.post(
        f"{comp}/{status}/void", json={"reason": "control"}, headers=auth(token)), 200)
    status = await _pending(client, token, driver_id, c_b, start)
    await _expect(await _values(client, token, status, c_b, "26"), 200)
    await _expect(await client.delete(f"{comp}/{status}", headers=auth(token)), 204)


async def test_the_current_destination_assignment_stays_writable(client, auth_token, direct_db):
    world = await _build_world(client, auth_token, direct_db)
    # The destination-Branch manager (valid Branch permission) and the admin both succeed.
    today = str(await branch_today(direct_db, world.new_branch))
    fresh = [await _definition(client, auth_token) for _ in range(2)]  # the seed holds the others
    fresh_columns = await _columns(client, auth_token, 2)
    await _exercise_every_writer(
        client, world.new_manager, world.driver_id, fresh, fresh_columns, "destination", today)
    # ... while the ended source is still refused.
    await _assert_all_writers_refused(client, direct_db, world.seed, world.old_manager)


async def test_pending_destination_and_current_source_of_a_scheduled_transfer_stay_writable(
    client, auth_token, direct_db,
):
    source_branch, target_branch = await make_branch(direct_db), await make_branch(direct_db)
    definitions = [await _definition(client, auth_token) for _ in range(2)]
    for branch_id in (source_branch, target_branch):
        await ensure_branch_pay_calendar(direct_db, 1, branch_id)
    columns = await _columns(client, auth_token, 2)
    driver = await make_driver(client, auth_token, source_branch)
    scheduled = await scheduled_transfer(
        client, auth_token, driver["driver_id"], target_branch,
        await db_now(direct_db) + timedelta(days=3))

    ends = (await direct_db.execute(text("""
        SELECT EffectiveToAt > clock_timestamp(), EndReason
        FROM core.DriverAssignments WHERE DriverAssignmentID = :a
    """), {"a": scheduled["source_id"]})).one()
    assert tuple(ends) == (True, "Transferred")  # current, its Transferred end still ahead

    # Schedules follow the start date: the pending destination's days start with the transfer,
    # the source's days run until it.
    transfer_day = str((await db_now(direct_db) + timedelta(days=3)).date())
    today = str(await branch_today(direct_db, source_branch))
    await _exercise_every_writer(
        client, auth_token, driver["driver_id"], definitions, columns, "pending", transfer_day)
    await _exercise_every_writer(
        client, auth_token, driver["driver_id"], definitions, columns, "source", today)
