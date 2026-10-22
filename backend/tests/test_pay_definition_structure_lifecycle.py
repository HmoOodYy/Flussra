"""The structure lifecycle of a PayDefinition-owned RateDefinition.

Creation is not a use. The structure (method, input type and OrdinalTier topology) is editable
through generic PayDefinition governance while the RateDefinition is unlocked and no Pending
schedule exists; the first Approved schedule or the first payroll period that references it locks
it for good. Edits and the writers that lock it are serialized by the one RateDefinition lock.
"""
from __future__ import annotations

import asyncio

import pytest
from psycopg2 import errors
from sqlalchemy import text

from app.compensation import definitions, schedules
from app.compensation.schemas import PayDefinitionStructureUpdate, ScheduleCreate
from app.payroll.period_creation import create_period_from_candidate
from app.payroll.schemas import PeriodCreationRequest
from tests.compensation_api_fixtures import (  # noqa: F401 - register fixtures
    ORDINAL_TIERS,
    approve_schedule,
    authored_schedule,
    build_tenant,
    create_definition,
    create_ordinal_definition,
    create_pending,
    grant_applicability,
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
    p3c_tenant,
    set_scalar_value,
    set_tier_values,
)
from tests.compensation_database_fixtures import (  # noqa: F401 - register fixtures
    p3b_cursor,
    p3b_database,
)
from tests.payroll_runtime_fixtures import (
    assign_weekly_setup,
    build_payroll_tenant,
    create_period,
    status_column_of,
)
from tests.test_p3c_compensation_concurrency import _blocked_with_pid, _code

pytestmark = pytest.mark.asyncio

DEFINITIONS = "/compensation/pay-definitions"
SCHEDULES = "/compensation/driver-rate-schedules"


@pytest.fixture(name="payroll_tenant")
def payroll_tenant_fixture(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


def _structure(pay_definition_id: int) -> str:
    return f"{DEFINITIONS}/{pay_definition_id}/structure"


async def _change(client, tenant, definition, **body):
    return await client.put(
        _structure(definition["pay_definition_id"]), json=body, headers=tenant.admin)


async def _get(client, tenant, definition) -> dict:
    return (await client.get(
        f"{DEFINITIONS}/{definition['pay_definition_id']}", headers=tenant.admin)).json()


def _bounds(definition: dict) -> list[tuple]:
    return [(c["ordinal_from"], c["ordinal_to"]) for c in definition["components"]]


def _lock_state(cur, definition) -> bool:
    cur.execute("SELECT structurelockedatutc IS NOT NULL FROM payroll.ratedefinitions "
                "WHERE ratedefinitionid = %s", (definition["rate_definition_id"],))
    return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Creation does not lock; a change keeps the definition
# ---------------------------------------------------------------------------

async def test_a_new_definition_of_either_method_is_unlocked_until_first_use(
    p3c_client, tenant, cur,
):
    for definition in (await create_definition(p3c_client, tenant),
                       await create_ordinal_definition(p3c_client, tenant)):
        assert definition["structure_locked_at_utc"] is None
        assert _lock_state(cur, definition) is False


async def test_an_unused_per_unit_definition_becomes_an_ordinal_tier_definition(
    p3c_client, tenant, cur,
):
    created = await create_definition(p3c_client, tenant, definition_code="ITEM_ALPHA")
    changed = await _change(
        p3c_client, tenant, created, calculation_method="OrdinalTier", input_type="WholeNumber",
        ordinal_tiers=[{"ordinal_from": 1, "ordinal_to": 2}, {"ordinal_from": 3}])
    assert changed.status_code == 200, changed.text
    definition = changed.json()
    # Same PayDefinition and RateDefinition, same provenance: nothing was replaced.
    assert definition["pay_definition_id"] == created["pay_definition_id"]
    assert definition["rate_definition_id"] == created["rate_definition_id"]
    assert definition["definition_code"] == "ITEM_ALPHA"
    assert definition["provenance"] == created["provenance"]
    assert (definition["calculation_method"], definition["input_type"],
            definition["rate_shape"]) == ("OrdinalTier", "WholeNumber", "OrdinalTierSchedule")
    assert _bounds(definition) == [(1, 2), (3, None)]
    assert definition["structure_locked_at_utc"] is None
    assert _bounds(await _get(p3c_client, tenant, definition)) == [(1, 2), (3, None)]
    cur.execute("""
        SELECT actioncode, oldvaluejson::jsonb->>'calculation_method', newvaluejson::jsonb->>'calculation_method'
        FROM audit.auditlog WHERE actioncode = 'PAY_DEFINITION_STRUCTURE_CHANGED' AND entityid = %s
    """, (str(created["pay_definition_id"]),))
    assert cur.fetchall() == [("PAY_DEFINITION_STRUCTURE_CHANGED", "PerUnit", "OrdinalTier")]


async def test_an_unused_ordinal_tier_definition_can_be_corrected_or_become_per_unit(
    p3c_client, tenant,
):
    created = await create_ordinal_definition(p3c_client, tenant)
    corrected = await _change(
        p3c_client, tenant, created, calculation_method="OrdinalTier", input_type="WholeNumber",
        ordinal_tiers=[{"ordinal_from": 1, "ordinal_to": 4}, {"ordinal_from": 5}])
    assert corrected.status_code == 200, corrected.text
    assert _bounds(corrected.json()) == [(1, 4), (5, None)]
    assert [c["sequence_no"] for c in corrected.json()["components"]] == [1, 2]

    scalar = await _change(
        p3c_client, tenant, created, calculation_method="PerUnit", input_type="Decimal")
    assert scalar.status_code == 200, scalar.text
    body = scalar.json()
    assert (body["calculation_method"], body["input_type"], body["rate_shape"]) == (
        "PerUnit", "Decimal", "Scalar")
    assert [(c["sequence_no"], c["ordinal_from"], c["ordinal_to"]) for c in body["components"]] == [
        (1, None, None)]
    assert body["pay_definition_id"] == created["pay_definition_id"]

    back = await _change(
        p3c_client, tenant, created, calculation_method="OrdinalTier", input_type="WholeNumber",
        ordinal_tiers=ORDINAL_TIERS)
    assert back.status_code == 200 and _bounds(back.json()) == [(1, 1), (2, 2), (3, None)]


async def test_the_replacement_is_atomic_and_an_invalid_one_changes_nothing(
    p3c_client, tenant,
):
    created = await create_ordinal_definition(p3c_client, tenant)
    before = await _get(p3c_client, tenant, created)
    for body in (
        {"calculation_method": "OrdinalTier", "input_type": "WholeNumber",
         "ordinal_tiers": [{"ordinal_from": 1, "ordinal_to": 1}, {"ordinal_from": 3}]},
        {"calculation_method": "OrdinalTier", "input_type": "WholeNumber",
         "ordinal_tiers": [{"ordinal_from": 1, "ordinal_to": 3}, {"ordinal_from": 3}]},
        {"calculation_method": "OrdinalTier", "input_type": "WholeNumber",
         "ordinal_tiers": [{"ordinal_from": 1}, {"ordinal_from": 2}]},
        {"calculation_method": "OrdinalTier", "input_type": "WholeNumber",
         "ordinal_tiers": [{"ordinal_from": 1, "ordinal_to": 2}]},
        {"calculation_method": "OrdinalTier", "input_type": "WholeNumber", "ordinal_tiers": []},
        {"calculation_method": "OrdinalTier", "input_type": "WholeNumber"},
    ):
        response = await _change(p3c_client, tenant, created, **body)
        assert response.status_code == 422, body
        assert response.json()["detail"]["code"] == "INVALID_TIER_TOPOLOGY", body
    decimal = await _change(
        p3c_client, tenant, created, calculation_method="OrdinalTier", input_type="Decimal",
        ordinal_tiers=ORDINAL_TIERS)
    assert decimal.json()["detail"]["code"] == "ORDINAL_TIER_REQUIRES_WHOLE_NUMBER"
    tiers_on_scalar = await _change(
        p3c_client, tenant, created, calculation_method="PerUnit", input_type="Decimal",
        ordinal_tiers=ORDINAL_TIERS)
    assert tiers_on_scalar.json()["detail"]["code"] == "TIERS_NOT_APPLICABLE"
    for method in ("RangeBracket", "RangeProgressive", "Block", "Fixed"):
        assert (await _change(
            p3c_client, tenant, created, calculation_method=method,
            input_type="Decimal")).status_code == 422
    assert await _get(p3c_client, tenant, created) == before


async def test_a_component_of_another_shape_cannot_join_a_structure(p3c_client, tenant, cur):
    scalar = await create_definition(p3c_client, tenant)
    with pytest.raises(errors.ForeignKeyViolation):
        cur.execute("""
            INSERT INTO payroll.ratecomponentdefinitions
                (ratedefinitionid, shape, sequenceno, ordinalfrom)
            VALUES (%s, 'OrdinalTierSchedule', 2, 5)
        """, (scalar["rate_definition_id"],))
    ordinal = await create_ordinal_definition(p3c_client, tenant)
    with pytest.raises(errors.CheckViolation):      # a scalar component is only ever sequence 1
        cur.execute("""
            INSERT INTO payroll.ratecomponentdefinitions (ratedefinitionid, shape, sequenceno)
            VALUES (%s, 'Scalar', 7)
        """, (ordinal["rate_definition_id"],))


async def test_only_company_wide_editors_change_a_structure(p3c_client, tenant, p3b_dsn):
    created = await create_definition(p3c_client, tenant)
    body = {"calculation_method": "PerUnit", "input_type": "Decimal"}
    for user in (tenant.branch_user, tenant.viewer, tenant.no_permissions):
        response = await p3c_client.put(
            _structure(created["pay_definition_id"]), json=body, headers=tenant.headers(user))
        assert response.status_code == 403
    other = build_tenant(p3b_dsn)
    cross = await p3c_client.put(
        _structure(created["pay_definition_id"]), json=body, headers=other.admin)
    assert cross.status_code == 404
    missing = await p3c_client.put(_structure(created["pay_definition_id"] + 10_000),
                                   json=body, headers=tenant.admin)
    assert missing.status_code == 404


async def test_a_retired_definition_cannot_change_structure(p3c_client, tenant):
    created = await create_definition(p3c_client, tenant)
    assert (await p3c_client.post(
        f"{DEFINITIONS}/{created['pay_definition_id']}/retire", headers=tenant.admin)).status_code == 200
    response = await _change(
        p3c_client, tenant, created, calculation_method="PerUnit", input_type="Decimal")
    assert (response.status_code, response.json()["detail"]["code"]) == (
        422, "PAY_DEFINITION_NOT_ACTIVE")


# ---------------------------------------------------------------------------
# Pending schedules block the change; discarding restores it
# ---------------------------------------------------------------------------

async def test_a_pending_schedule_blocks_the_change_until_it_is_discarded(
    p3c_client, tenant, cur,
):
    created = await create_definition(p3c_client, tenant)
    pending = await create_pending(p3c_client, tenant, created)
    assert (await set_scalar_value(p3c_client, tenant, pending, created, "5")).status_code == 200
    body = {"calculation_method": "OrdinalTier", "input_type": "WholeNumber",
            "ordinal_tiers": ORDINAL_TIERS}
    blocked = await _change(p3c_client, tenant, created, **body)
    assert (blocked.status_code, blocked.json()["detail"]["code"]) == (
        409, "RATE_STRUCTURE_PENDING_SCHEDULE")
    assert (await _get(p3c_client, tenant, created))["calculation_method"] == "PerUnit"

    discarded = await p3c_client.delete(
        f"{SCHEDULES}/{pending['driver_rate_schedule_id']}", headers=tenant.admin)
    assert discarded.status_code == 204
    assert _lock_state(cur, created) is False
    changed = await _change(p3c_client, tenant, created, **body)
    assert changed.status_code == 200, changed.text
    assert _bounds(changed.json()) == [(1, 1), (2, 2), (3, None)]
    # The next schedule is authored against the new structure.
    schedule = await create_pending(p3c_client, tenant, changed.json())
    assert [v["ordinal_from"] for v in schedule["values"]] == [1, 2, 3]


# ---------------------------------------------------------------------------
# The first authoritative use locks for good
# ---------------------------------------------------------------------------

async def test_the_first_approved_schedule_locks_the_structure_permanently(
    p3c_client, tenant, cur,
):
    created = await create_definition(p3c_client, tenant)
    assert _lock_state(cur, created) is False
    first = await authored_schedule(p3c_client, tenant, created, "25", effective_from="2026-01-01")
    assert _lock_state(cur, created) is True
    body = {"calculation_method": "OrdinalTier", "input_type": "WholeNumber",
            "ordinal_tiers": ORDINAL_TIERS}
    refused = await _change(p3c_client, tenant, created, **body)
    assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "RATE_STRUCTURE_LOCKED")

    # Still locked once that schedule is superseded or voided: history is never reinterpreted.
    await authored_schedule(p3c_client, tenant, created, "30", effective_from="2026-06-01")
    voided = await p3c_client.post(
        f"{SCHEDULES}/{first['driver_rate_schedule_id']}/void", headers=tenant.admin,
        json={"reason": "wrong"})
    assert voided.status_code == 200
    again = await _change(p3c_client, tenant, created, **body)
    assert again.json()["detail"]["code"] == "RATE_STRUCTURE_LOCKED"
    assert (await _get(p3c_client, tenant, created))["calculation_method"] == "PerUnit"


async def test_the_first_tier_schedule_approval_locks_an_ordinal_structure(
    p3c_client, tenant, cur,
):
    from tests.compensation_api_fixtures import authored_tier_schedule

    created = await create_ordinal_definition(p3c_client, tenant)
    await authored_tier_schedule(p3c_client, tenant, created)
    assert _lock_state(cur, created) is True
    refused = await _change(
        p3c_client, tenant, created, calculation_method="OrdinalTier", input_type="WholeNumber",
        ordinal_tiers=[{"ordinal_from": 1}])
    assert refused.json()["detail"]["code"] == "RATE_STRUCTURE_LOCKED"


async def test_the_first_period_reference_locks_the_structure_without_any_schedule(
    p3c_client, p3c_engine, payroll_tenant, cur,
):
    tenant = payroll_tenant
    created = await create_ordinal_definition(p3c_client, tenant)
    grant_applicability(tenant, created, tenant.branch_a)
    assert _lock_state(cur, created) is False
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    period = await create_period(p3c_client, tenant, tenant.branch_a)
    assert period["result"] == "CREATED"
    assert _lock_state(cur, created) is True
    refused = await _change(
        p3c_client, tenant, created, calculation_method="PerUnit", input_type="Decimal")
    assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "RATE_STRUCTURE_LOCKED")


async def test_a_status_rate_column_structure_stays_fixed_and_is_not_a_pay_definition(
    p3c_client, payroll_tenant, cur,
):
    tenant = payroll_tenant
    column_id, _, _ = status_column_of(tenant)
    cur.execute("""
        SELECT ratedefinitionid, shape, structurelockedatutc IS NOT NULL FROM payroll.ratedefinitions
        WHERE statusratecolumnid = %s
    """, (column_id,))
    ((rate_definition_id, shape, locked),) = cur.fetchall()
    assert (shape, locked) == ("Scalar", True)
    with pytest.raises(errors.CheckViolation, match="RATE_STRUCTURE_LOCKED"):
        cur.execute("DELETE FROM payroll.ratecomponentdefinitions WHERE ratedefinitionid = %s",
                    (rate_definition_id,))
    # No PayDefinition owns it, so the governance structure route has nothing to address.
    assert (await p3c_client.put(
        _structure(rate_definition_id), json={"calculation_method": "OrdinalTier",
                                              "input_type": "WholeNumber",
                                              "ordinal_tiers": ORDINAL_TIERS},
        headers=tenant.admin)).status_code == 404


# ---------------------------------------------------------------------------
# Real races under the one RateDefinition lock
# ---------------------------------------------------------------------------

def _to_ordinal(tenant, definition):
    async def operation(conn):
        return await definitions.change_structure(
            tenant.company_id, tenant.owner, definition["pay_definition_id"],
            PayDefinitionStructureUpdate(
                calculation_method="OrdinalTier", input_type="WholeNumber",
                ordinal_tiers=ORDINAL_TIERS), conn)
    return operation


def _pending(tenant, definition):
    async def operation(conn):
        return await schedules.create_pending(
            tenant.company_id, tenant.owner,
            ScheduleCreate(driver_id=tenant.driver_a_id,
                           rate_definition_id=definition["rate_definition_id"],
                           effective_from="2026-01-01"), conn)
    return operation


async def _blockers(engine, pid: int):
    async with engine.connect() as observer:
        return (await observer.execute(
            text("SELECT pg_blocking_pids(:pid)"), {"pid": pid})).scalar_one()


async def test_a_change_in_flight_makes_pending_creation_wait_and_see_the_new_structure(
    p3c_client, p3c_engine, tenant,
):
    created = await create_definition(p3c_client, tenant)
    async with p3c_engine.connect() as holder:
        async with holder.begin():
            holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            assert not isinstance(
                await _to_ordinal(tenant, created)(holder), Exception)
            task, waiter_pid = await _blocked_with_pid(p3c_engine, _pending(tenant, created))
            assert await _blockers(p3c_engine, waiter_pid) == [holder_pid]
    pending = await task
    assert not isinstance(pending, Exception), pending
    assert [v.ordinal_from for v in pending.values] == [1, 2, 3]


async def test_a_pending_creation_in_flight_makes_the_change_wait_and_then_fail(
    p3c_client, p3c_engine, tenant,
):
    created = await create_definition(p3c_client, tenant)
    async with p3c_engine.connect() as holder:
        async with holder.begin():
            holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            assert not isinstance(await _pending(tenant, created)(holder), Exception)
            task, waiter_pid = await _blocked_with_pid(p3c_engine, _to_ordinal(tenant, created))
            assert await _blockers(p3c_engine, waiter_pid) == [holder_pid]
    assert _code(await task) == "RATE_STRUCTURE_PENDING_SCHEDULE"
    assert (await _get(p3c_client, tenant, created))["calculation_method"] == "PerUnit"


async def test_an_approval_in_flight_makes_the_change_wait_and_then_fail_locked(
    p3c_client, p3c_engine, tenant,
):
    created = await create_definition(p3c_client, tenant)
    pending = await create_pending(p3c_client, tenant, created)
    assert (await set_scalar_value(p3c_client, tenant, pending, created, "5")).status_code == 200
    async with p3c_engine.connect() as holder:
        async with holder.begin():
            holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            await schedules.approve(
                tenant.company_id, tenant.owner, pending["driver_rate_schedule_id"], holder)
            task, waiter_pid = await _blocked_with_pid(p3c_engine, _to_ordinal(tenant, created))
            assert await _blockers(p3c_engine, waiter_pid) == [holder_pid]
    assert _code(await task) == "RATE_STRUCTURE_LOCKED"
    assert (await _get(p3c_client, tenant, created))["calculation_method"] == "PerUnit"


async def test_a_change_in_flight_makes_approval_wait_and_a_changed_structure_is_not_approvable(
    p3c_client, p3c_engine, tenant,
):
    """An approval can only race a change when no Pending exists to approve: the change waits for
    nothing, commits, and the earlier Pending (created before) would have blocked it instead."""
    created = await create_definition(p3c_client, tenant)
    pending = await create_pending(p3c_client, tenant, created)
    assert (await set_scalar_value(p3c_client, tenant, pending, created, "5")).status_code == 200
    result = await asyncio.gather(
        asyncio.create_task(_run(p3c_engine, _to_ordinal(tenant, created))),
        asyncio.create_task(_run(p3c_engine, _approve(tenant, pending))))
    outcomes = {type(r).__name__ if isinstance(r, Exception) else "ok" for r in result}
    structure = (await _get(p3c_client, tenant, created))["calculation_method"]
    # Never both: the structure changed only if the approval did not (and the Pending is gone
    # or still Pending), and an approved schedule is always of the structure it was filled for.
    async with p3c_engine.connect() as conn:
        status = (await conn.execute(
            text("SELECT status FROM payroll.driverrateschedules WHERE driverratescheduleid = :s"),
            {"s": pending["driver_rate_schedule_id"]})).scalar_one()
    assert not (structure == "OrdinalTier" and status == "Approved"), (structure, status, outcomes)
    assert (structure, status) in {("PerUnit", "Approved"), ("PerUnit", "Pending")}


def _approve(tenant, pending):
    async def operation(conn):
        return await schedules.approve(
            tenant.company_id, tenant.owner, pending["driver_rate_schedule_id"], conn)
    return operation


async def _run(engine, operation):
    try:
        async with engine.begin() as conn:
            return await operation(conn)
    except Exception as exc:  # noqa: BLE001 - the outcome is asserted by the test
        return exc


async def test_period_creation_and_a_change_never_both_succeed_inconsistently(
    p3c_client, p3c_engine, payroll_tenant,
):
    tenant = payroll_tenant
    created = await create_definition(p3c_client, tenant, definition_code="RACE")
    grant_applicability(tenant, created, tenant.branch_a)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    candidates = await p3c_client.get(
        f"/payroll/branches/{tenant.branch_a}/period-candidates",
        params={"mode": "OPEN_CREATION"}, headers=tenant.admin)
    key = candidates.json()["selected"]["candidate_key"]

    async def create(conn):
        return await create_period_from_candidate(
            tenant.company_id, tenant.owner, tenant.branch_a,
            PeriodCreationRequest(candidate_key=key), conn)

    # The change holds the lock: creation waits, then freezes the CHANGED structure.
    async with p3c_engine.connect() as holder:
        async with holder.begin():
            holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            assert not isinstance(await _to_ordinal(tenant, created)(holder), Exception)
            task, waiter_pid = await _blocked_with_pid(p3c_engine, create)
            assert await _blockers(p3c_engine, waiter_pid) == [holder_pid]
    period = await task
    assert not isinstance(period, Exception), period
    async with p3c_engine.connect() as conn:
        frozen = (await conn.execute(text("""
            SELECT calculationmethodsnapshot, rateshapesnapshot, inputtypesnapshot
            FROM payroll.payrollperioddefinitions WHERE payrollperiodid = :p
        """), {"p": period.payroll_period_id})).all()
    assert [tuple(r) for r in frozen] == [("OrdinalTier", "OrdinalTierSchedule", "WholeNumber")]
    refused = await _run(p3c_engine, _to_ordinal(tenant, created))
    assert _code(refused) == "RATE_STRUCTURE_LOCKED"


async def test_a_period_creation_in_flight_makes_the_change_wait_and_then_fail_locked(
    p3c_client, p3c_engine, payroll_tenant,
):
    tenant = payroll_tenant
    created = await create_definition(p3c_client, tenant, definition_code="RACE_TWO")
    grant_applicability(tenant, created, tenant.branch_a)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    candidates = await p3c_client.get(
        f"/payroll/branches/{tenant.branch_a}/period-candidates",
        params={"mode": "OPEN_CREATION"}, headers=tenant.admin)
    key = candidates.json()["selected"]["candidate_key"]
    async with p3c_engine.connect() as holder:
        async with holder.begin():
            holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            period = await create_period_from_candidate(
                tenant.company_id, tenant.owner, tenant.branch_a,
                PeriodCreationRequest(candidate_key=key), holder)
            task, waiter_pid = await _blocked_with_pid(p3c_engine, _to_ordinal(tenant, created))
            assert await _blockers(p3c_engine, waiter_pid) == [holder_pid]
    assert _code(await task) == "RATE_STRUCTURE_LOCKED"
    async with p3c_engine.connect() as conn:
        frozen = (await conn.execute(text("""
            SELECT calculationmethodsnapshot FROM payroll.payrollperioddefinitions
            WHERE payrollperiodid = :p"""), {"p": period.payroll_period_id})).all()
    assert [tuple(r) for r in frozen] == [("PerUnit",)]
