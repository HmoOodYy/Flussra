"""Period creation freezes the target PayDefinition layout (PayrollPeriodDefinitions)."""
from __future__ import annotations

import asyncio
from datetime import date
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.compensation import definitions
from app.compensation.branch_config import apply_config
from app.payroll.period_creation import create_period_from_candidate
from app.payroll.schemas import PeriodCreationRequest
from tests.p3b_fixtures import p3b_cursor, p3b_database  # noqa: F401 - register fixtures
from tests.p3c_fixtures import (  # noqa: F401 - register fixtures
    create_definition,
    grant_applicability,
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.p4b_fixtures import (
    assign_weekly_setup,
    build_payroll_tenant,
    create_period,
    period_definitions,
)
from tests.test_p3c_compensation_concurrency import _blocked_with_pid

pytestmark = pytest.mark.asyncio


@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _candidate_key(client, tenant, branch_id, mode="OPEN_CREATION") -> str:
    response = await client.get(
        f"/payroll/branches/{branch_id}/period-candidates",
        params={"mode": mode}, headers=tenant.admin)
    assert response.status_code == 200, response.text
    return response.json()["selected"]["candidate_key"]


async def _structure_locked(engine, rate_definition_id: int) -> bool:
    async with engine.connect() as conn:
        return (await conn.execute(
            text("SELECT structurelockedatutc IS NOT NULL FROM payroll.ratedefinitions "
                 "WHERE ratedefinitionid = :r"), {"r": rate_definition_id})).scalar_one()


async def _direct_definition(engine, tenant, *, method="PerUnit", active=True, branch_id=None):
    """A definition created straight in the database (the governance flow does not author
    OrdinalTier yet), with its complete RateDefinition structure."""
    async with engine.begin() as conn:
        pd = (await conn.execute(text("""
            INSERT INTO payroll.paydefinitions
                (companyid, definitioncode, definitionname, inputtype, calculationmethod)
            VALUES (:cid, :code, 'Direct item', :input, :method) RETURNING paydefinitionid
        """), {"cid": tenant.company_id, "code": "D" + uuid4().hex[:10],
               "input": "WholeNumber" if method == "OrdinalTier" else "Decimal",
               "method": method})).scalar_one()
        rd = (await conn.execute(text("""
            INSERT INTO payroll.ratedefinitions (companyid, paydefinitionid, shape)
            VALUES (:cid, :pd, payroll.fn_shapeforcalculationmethod(:method))
            RETURNING ratedefinitionid
        """), {"cid": tenant.company_id, "pd": pd, "method": method})).scalar_one()
        if method == "OrdinalTier":
            for seq, (low, high) in enumerate([(1, 1), (2, 2), (3, None)], start=1):
                await conn.execute(text("""
                    INSERT INTO payroll.ratecomponentdefinitions
                        (ratedefinitionid, shape, sequenceno, ordinalfrom, ordinalto)
                    VALUES (:rd, 'OrdinalTierSchedule', :seq, :low, :high)
                """), {"rd": rd, "seq": seq, "low": low, "high": high})
        else:
            await conn.execute(text("""
                INSERT INTO payroll.ratecomponentdefinitions (ratedefinitionid, shape, sequenceno)
                VALUES (:rd, 'Scalar', 1)
            """), {"rd": rd})
    definition = {"pay_definition_id": pd, "rate_definition_id": rd}
    grant_applicability(tenant, definition, branch_id or tenant.branch_a, active=active)
    return definition


# ---------------------------------------------------------------------------
# Layout semantics
# ---------------------------------------------------------------------------

async def test_a_company_with_no_pay_definitions_gets_a_valid_period_with_no_rows(
    p3c_client, p3c_engine, tenant,
):
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    period = await create_period(p3c_client, tenant, tenant.branch_a)
    assert period["result"] == "CREATED"
    assert period_definitions(period["payroll_period_id"], tenant) == []


async def test_arbitrary_codes_and_names_are_frozen_with_their_structure(
    p3c_client, p3c_engine, tenant,
):
    first = await create_definition(
        p3c_client, tenant, definition_code="ITEM_ALPHA", definition_name="Zeta route",
        input_type="WholeNumber", unit="stop")
    second = await create_definition(
        p3c_client, tenant, definition_code="HOURS", definition_name="Alpha route")
    for definition in (first, second):
        grant_applicability(tenant, definition, tenant.branch_a)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    period = await create_period(p3c_client, tenant, tenant.branch_a)

    async with p3c_engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT definitioncodesnapshot, definitionnamesnapshot, inputtypesnapshot,
                   unitsnapshot, calculationmethodsnapshot, rateshapesnapshot,
                   calculationmethodversionsnapshot, definitionstatusatsnapshot,
                   isactiveinperiod, sortorder
            FROM payroll.payrollperioddefinitions
            WHERE payrollperiodid = :p ORDER BY sortorder
        """), {"p": period["payroll_period_id"]})).all()
    # Frozen display order follows the name, never a code.
    assert [(r[0], r[1]) for r in rows] == [("HOURS", "Alpha route"), ("ITEM_ALPHA", "Zeta route")]
    assert [r[9] for r in rows] == [0, 1]
    assert rows[1][2:9] == ("WholeNumber", "stop", "PerUnit", "Scalar", 1, "Active", True)


async def test_only_definitions_with_an_applicable_branch_version_are_in_the_layout(
    p3c_client, p3c_engine, tenant,
):
    applicable = await create_definition(p3c_client, tenant, definition_code="APPLIES")
    inactive = await create_definition(p3c_client, tenant, definition_code="OFF_HERE")
    elsewhere = await create_definition(p3c_client, tenant, definition_code="OTHER_BRANCH")
    unconfigured = await create_definition(p3c_client, tenant, definition_code="UNCONFIGURED")
    future = await create_definition(p3c_client, tenant, definition_code="STARTS_LATER")
    grant_applicability(tenant, applicable, tenant.branch_a)
    grant_applicability(tenant, inactive, tenant.branch_a, active=False)
    grant_applicability(tenant, elsewhere, tenant.branch_b)
    grant_applicability(tenant, future, tenant.branch_a, effective_from="2099-01-01")
    assert unconfigured["pay_definition_id"]
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    period = await create_period(p3c_client, tenant, tenant.branch_a)

    layout = {d["code"]: d["is_active"] for d in period_definitions(
        period["payroll_period_id"], tenant)}
    assert layout == {"APPLIES": True, "OFF_HERE": False}


async def test_a_later_branch_or_definition_change_does_not_alter_the_period(
    p3c_client, p3c_engine, tenant,
):
    definition = await create_definition(
        p3c_client, tenant, definition_code="STABLE", definition_name="Stable name")
    grant_applicability(tenant, definition, tenant.branch_a)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    period = await create_period(p3c_client, tenant, tenant.branch_a)
    before = period_definitions(period["payroll_period_id"], tenant)

    # Rename, deactivate in the Branch and retire the definition afterwards.
    async with p3c_engine.begin() as conn:
        await conn.execute(text(
            "UPDATE payroll.paydefinitions SET definitionname = 'Renamed' "
            "WHERE paydefinitionid = :p"), {"p": definition["pay_definition_id"]})
        await conn.execute(text(
            "UPDATE payroll.branchpayitemconfig SET isactive = FALSE "
            "WHERE paydefinitionid = :p"), {"p": definition["pay_definition_id"]})
    retired = await p3c_client.post(
        f"/compensation/pay-definitions/{definition['pay_definition_id']}/retire",
        headers=tenant.admin)
    assert retired.status_code == 200

    assert period_definitions(period["payroll_period_id"], tenant) == before
    async with p3c_engine.connect() as conn:
        frozen = (await conn.execute(text(
            "SELECT definitionnamesnapshot, definitionstatusatsnapshot, isactiveinperiod "
            "FROM payroll.payrollperioddefinitions WHERE payrollperiodid = :p"),
            {"p": period["payroll_period_id"]})).one()
    assert tuple(frozen) == ("Stable name", "Active", True)


async def test_creation_sets_the_structural_lock_and_the_snapshot_references_the_branch_config(
    p3c_client, p3c_engine, tenant,
):
    """A definition authored without the governed lock gets it from the period reference."""
    definition = await _direct_definition(p3c_engine, tenant)
    async with p3c_engine.begin() as conn:
        await conn.execute(text(
            "UPDATE payroll.branchpayitemconfig SET effectivefrom = '2020-02-02' "
            "WHERE paydefinitionid = :p"), {"p": definition["pay_definition_id"]})
    assert not await _structure_locked(p3c_engine, definition["rate_definition_id"])
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    period = await create_period(p3c_client, tenant, tenant.branch_a)
    assert await _structure_locked(p3c_engine, definition["rate_definition_id"])

    async with p3c_engine.connect() as conn:
        row = (await conn.execute(text("""
            SELECT ppd.sourcebranchconfigeffectivefrom, ppd.sourcebranchconfigeffectiveto,
                   bpic.effectivefrom
            FROM payroll.payrollperioddefinitions ppd
            JOIN payroll.branchpayitemconfig bpic ON bpic.configid = ppd.sourcebranchconfigid
            WHERE ppd.payrollperiodid = :p
        """), {"p": period["payroll_period_id"]})).one()
    assert row[0] == row[2] == date(2020, 2, 2) and row[1] is None

    # The locked topology can no longer change underneath the period.
    with pytest.raises(Exception, match="RATE_STRUCTURE_LOCKED"):
        async with p3c_engine.begin() as conn:
            await conn.execute(text(
                "UPDATE payroll.paydefinitions SET inputtype = 'WholeNumber' "
                "WHERE paydefinitionid = :p"), {"p": definition["pay_definition_id"]})


async def test_candidate_replay_is_idempotent_and_does_not_duplicate_the_layout(
    p3c_client, p3c_engine, tenant,
):
    definition = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, definition, tenant.branch_a)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    key = await _candidate_key(p3c_client, tenant, tenant.branch_a)
    body = {"candidate_key": key}
    first = await p3c_client.post(
        f"/payroll/branches/{tenant.branch_a}/period-creations", json=body, headers=tenant.admin)
    second = await p3c_client.post(
        f"/payroll/branches/{tenant.branch_a}/period-creations", json=body, headers=tenant.admin)
    assert first.json()["result"] == "CREATED"
    assert second.json()["result"] == "ALREADY_EXISTS"
    assert second.json()["payroll_period_id"] == first.json()["payroll_period_id"]
    assert len(period_definitions(first.json()["payroll_period_id"], tenant)) == 1


async def test_period_days_definitions_and_eligibility_are_one_atomic_unit(
    p3c_client, p3c_engine, tenant,
):
    definition = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, definition, tenant.branch_a)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    period = await create_period(p3c_client, tenant, tenant.branch_a)
    async with p3c_engine.connect() as conn:
        counts = (await conn.execute(text("""
            SELECT (SELECT count(*) FROM payroll.payrollperioddays WHERE payrollperiodid = :p),
                   (SELECT count(*) FROM payroll.payrollperioddefinitions WHERE payrollperiodid = :p),
                   (SELECT count(*) FROM payroll.payrollperioddrivereligibility
                    WHERE payrollperiodid = :p)
        """), {"p": period["payroll_period_id"]})).one()
    assert counts[0] == 7 and counts[1] == 1 and counts[2] >= 1


# ---------------------------------------------------------------------------
# Calculation methods that are not operational
# ---------------------------------------------------------------------------

async def test_an_active_ordinal_tier_definition_fails_period_creation_closed(
    p3c_client, p3c_engine, tenant,
):
    await _direct_definition(p3c_engine, tenant, method="OrdinalTier", active=True)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    key = await _candidate_key(p3c_client, tenant, tenant.branch_a)
    response = await p3c_client.post(
        f"/payroll/branches/{tenant.branch_a}/period-creations",
        json={"candidate_key": key}, headers=tenant.admin)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CALCULATION_METHOD_NOT_READY"
    async with p3c_engine.connect() as conn:
        persisted = (await conn.execute(text("""
            SELECT (SELECT count(*) FROM payroll.payrollperiods WHERE branchid = :b),
                   (SELECT count(*) FROM payroll.payrollperioddefinitions WHERE branchid = :b),
                   (SELECT count(*) FROM payroll.payrollperioddays WHERE branchid = :b)
        """), {"b": tenant.branch_a})).one()
    assert tuple(persisted) == (0, 0, 0)


async def test_an_inactive_ordinal_tier_definition_is_frozen_but_never_a_column(
    p3c_client, p3c_engine, tenant,
):
    inactive = await _direct_definition(p3c_engine, tenant, method="OrdinalTier", active=False)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    period = await create_period(p3c_client, tenant, tenant.branch_a)
    layout = period_definitions(period["payroll_period_id"], tenant)
    assert [(d["pay_definition_id"], d["is_active"]) for d in layout] == [
        (inactive["pay_definition_id"], False)]
    grid = await p3c_client.get(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid", headers=tenant.admin)
    assert grid.status_code == 200, grid.text
    assert grid.json()["columns"] == []


# ---------------------------------------------------------------------------
# Concurrency: creation against retirement, structure and configuration
# ---------------------------------------------------------------------------

async def _create_with(engine, tenant, key):
    async def operation(conn):
        return await create_period_from_candidate(
            tenant.company_id, tenant.owner, tenant.branch_a,
            PeriodCreationRequest(candidate_key=key), conn)
    return operation


async def test_creation_waits_for_an_in_flight_retirement_and_then_excludes_the_definition(
    p3c_client, p3c_engine, tenant,
):
    retiring = await create_definition(p3c_client, tenant, definition_code="RETIRING")
    staying = await create_definition(p3c_client, tenant, definition_code="STAYING")
    for definition in (retiring, staying):
        grant_applicability(tenant, definition, tenant.branch_a)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    key = await _candidate_key(p3c_client, tenant, tenant.branch_a)
    operation = await _create_with(p3c_engine, tenant, key)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            await definitions.retire_definition(
                tenant.company_id, tenant.owner, retiring["pay_definition_id"], holder)
            task, waiter_pid = await _blocked_with_pid(p3c_engine, operation)
            async with p3c_engine.connect() as observer:
                blockers = (await observer.execute(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid})).scalar_one()
            assert blockers == [holder_pid]
    created = await task
    assert not isinstance(created, Exception), created
    layout = period_definitions(created.payroll_period_id, tenant)
    assert [d["code"] for d in layout] == ["STAYING"]


async def test_retirement_waits_for_an_in_flight_creation_and_the_period_keeps_the_definition(
    p3c_client, p3c_engine, tenant,
):
    definition = await create_definition(p3c_client, tenant, definition_code="KEPT")
    grant_applicability(tenant, definition, tenant.branch_a)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    key = await _candidate_key(p3c_client, tenant, tenant.branch_a)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            created = await create_period_from_candidate(
                tenant.company_id, tenant.owner, tenant.branch_a,
                PeriodCreationRequest(candidate_key=key), holder)

            async def retire(conn):
                return await definitions.retire_definition(
                    tenant.company_id, tenant.owner, definition["pay_definition_id"], conn)

            task, waiter_pid = await _blocked_with_pid(p3c_engine, retire)
            async with p3c_engine.connect() as observer:
                blockers = (await observer.execute(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid})).scalar_one()
            assert blockers == [holder_pid]
    retired = await task
    assert not isinstance(retired, Exception), retired
    assert retired.status == "Retired"
    assert [d["code"] for d in period_definitions(created.payroll_period_id, tenant)] == ["KEPT"]


async def test_creation_waits_for_a_structural_writer_holding_the_rate_definition(
    p3c_client, p3c_engine, tenant,
):
    definition = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, definition, tenant.branch_a)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    key = await _candidate_key(p3c_client, tenant, tenant.branch_a)
    operation = await _create_with(p3c_engine, tenant, key)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            await holder.execute(
                text("SELECT payroll.fn_LockRateDefinitionStructure(:r)"),
                {"r": definition["rate_definition_id"]})
            task, waiter_pid = await _blocked_with_pid(p3c_engine, operation)
            async with p3c_engine.connect() as observer:
                blockers = (await observer.execute(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid})).scalar_one()
            assert blockers == [holder_pid]
    created = await task
    assert not isinstance(created, Exception), created
    assert await _structure_locked(p3c_engine, definition["rate_definition_id"])


async def test_creation_freezes_the_configuration_it_read_even_if_a_change_commits_meanwhile(
    p3c_client, p3c_engine, tenant,
):
    """A Branch configuration change in flight is ordered AFTER the snapshot: creation may
    wait for the configuration row, but the layout is exactly the state it read."""
    definition = await create_definition(p3c_client, tenant, definition_code="CONFIGURED")
    grant_applicability(tenant, definition, tenant.branch_a)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    key = await _candidate_key(p3c_client, tenant, tenant.branch_a)
    operation = await _create_with(p3c_engine, tenant, key)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            await apply_config(
                holder, company_id=tenant.company_id, branch_id=tenant.branch_a,
                pay_definition_id=definition["pay_definition_id"], user_id=tenant.owner,
                is_active=False, notes=None, effective_from=date.today())
            task, _ = await _blocked_with_pid(p3c_engine, operation)
    created = await task
    assert not isinstance(created, Exception), created
    async with p3c_engine.connect() as conn:
        frozen = (await conn.execute(text("""
            SELECT definitioncodesnapshot, isactiveinperiod, sourcebranchconfigeffectiveto
            FROM payroll.payrollperioddefinitions WHERE payrollperiodid = :p
        """), {"p": created.payroll_period_id})).all()
    assert [tuple(row) for row in frozen] == [("CONFIGURED", True, None)]


async def _run(engine, operation):
    try:
        async with engine.begin() as conn:
            return await operation(conn)
    except Exception as exc:  # noqa: BLE001 - asserted by the caller
        return exc


async def test_concurrent_creation_and_retirement_never_deadlock(
    p3c_client, p3c_engine, tenant,
):
    definition = await create_definition(p3c_client, tenant, definition_code="RACE")
    other = await create_definition(p3c_client, tenant, definition_code="RACE_TWO")
    for item in (definition, other):
        grant_applicability(tenant, item, tenant.branch_a)
        grant_applicability(tenant, item, tenant.branch_b)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_b)
    key_a = await _candidate_key(p3c_client, tenant, tenant.branch_a)
    key_b = await _candidate_key(p3c_client, tenant, tenant.branch_b)

    async def create_a(conn):
        return await create_period_from_candidate(
            tenant.company_id, tenant.owner, tenant.branch_a,
            PeriodCreationRequest(candidate_key=key_a), conn)

    async def create_b(conn):
        return await create_period_from_candidate(
            tenant.company_id, tenant.owner, tenant.branch_b,
            PeriodCreationRequest(candidate_key=key_b), conn)

    async def retire(conn):
        return await definitions.retire_definition(
            tenant.company_id, tenant.owner, definition["pay_definition_id"], conn)

    results = await asyncio.wait_for(asyncio.gather(
        _run(p3c_engine, create_a), _run(p3c_engine, create_b), _run(p3c_engine, retire)),
        timeout=30)
    for result in results:
        assert not isinstance(result, Exception) or isinstance(result, HTTPException), result
        assert "deadlock" not in str(result).lower()
    # Each period carries a coherent layout: the retired definition is in a period only if
    # that period was created before the retirement committed.
    for created in results[:2]:
        assert not isinstance(created, Exception), created
        codes = [d["code"] for d in period_definitions(created.payroll_period_id, tenant)]
        assert "RACE_TWO" in codes
        assert codes in (["RACE", "RACE_TWO"], ["RACE_TWO"])

