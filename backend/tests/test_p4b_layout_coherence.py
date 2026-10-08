"""The period layout snapshot is exactly one coherent authority state.

Branch applicability writers and canonical period creation share the Branch
compensation-config lock (app.branch_compensation_config_concurrency), so a multi
definition configuration change is wholly before or wholly after a creation snapshot.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.compensation import branch_config, definitions
from app.compensation.branch_config import apply_config
from app.compensation.schemas import BranchConfigTarget, BulkBranchConfigUpdate
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
    period_definitions,
)
from tests.test_p3c_compensation_concurrency import _blocked_with_pid

pytestmark = pytest.mark.asyncio

# Both versions apply at the (past) period start; "version" adds a later version that
# closes the old one the day before today, so the snapshot's provenance tells the
# before-state from the after-state.
MODES = ["deactivate", "version"]


@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _candidate_key(client, tenant, branch_id) -> str:
    response = await client.get(
        f"/payroll/branches/{branch_id}/period-candidates",
        params={"mode": "OPEN_CREATION"}, headers=tenant.admin)
    assert response.status_code == 200, response.text
    return response.json()["selected"]["candidate_key"]


def _creator(engine, tenant, branch_id, key):
    async def operation(conn):
        return await create_period_from_candidate(
            tenant.company_id, tenant.owner, branch_id,
            PeriodCreationRequest(candidate_key=key), conn)
    return operation


async def _run(engine, operation):
    try:
        async with engine.begin() as conn:
            return await operation(conn)
    except Exception as exc:  # noqa: BLE001 - asserted by the caller
        return exc


async def _writer_changes(conn, tenant, mode, changed, added):
    """One transaction that changes definition A and makes definition B applicable."""
    if mode == "deactivate":
        await apply_config(
            conn, company_id=tenant.company_id, branch_id=tenant.branch_a,
            pay_definition_id=changed["pay_definition_id"], user_id=tenant.owner,
            is_active=False, notes=None, effective_from=date(2000, 1, 1))
    else:
        await apply_config(
            conn, company_id=tenant.company_id, branch_id=tenant.branch_a,
            pay_definition_id=changed["pay_definition_id"], user_id=tenant.owner,
            is_active=False, notes=None, effective_from=date.today())
    await apply_config(
        conn, company_id=tenant.company_id, branch_id=tenant.branch_a,
        pay_definition_id=added["pay_definition_id"], user_id=tenant.owner,
        is_active=True, notes=None, effective_from=date(2000, 1, 1))


async def _frozen(engine, period_id) -> dict:
    async with engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT definitioncodesnapshot, isactiveinperiod, sourcebranchconfigeffectiveto
            FROM payroll.payrollperioddefinitions WHERE payrollperiodid = :p
            ORDER BY definitioncodesnapshot
        """), {"p": period_id})).all()
    return {code: (active, effective_to) for code, active, effective_to in rows}


def _expected(mode, state, today=None) -> dict:
    """The only two layouts a creation may ever freeze."""
    if state == "before":
        return {"ALPHA": (True, None)}
    closed = (today or date.today()) - timedelta(days=1)
    return {
        "ALPHA": (False, None) if mode == "deactivate" else (True, closed),
        "BETA": (True, None),
    }


async def _setup(client, engine, tenant):
    changed = await create_definition(client, tenant, definition_code="ALPHA")
    added = await create_definition(client, tenant, definition_code="BETA")
    grant_applicability(tenant, changed, tenant.branch_a)      # BETA stays unconfigured
    await assign_weekly_setup(engine, tenant, tenant.branch_a)
    key = await _candidate_key(client, tenant, tenant.branch_a)
    return changed, added, key


@pytest.mark.parametrize("mode", MODES)
async def test_a_multi_definition_change_in_flight_is_wholly_before_the_snapshot_never_split(
    p3c_client, p3c_engine, tenant, mode,
):
    """Config for A changes while B becomes newly applicable: creation waits and then
    freezes the complete AFTER state (A changed AND B present)."""
    changed, added, key = await _setup(p3c_client, p3c_engine, tenant)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            await _writer_changes(holder, tenant, mode, changed, added)
            task, waiter_pid = await _blocked_with_pid(
                p3c_engine, _creator(p3c_engine, tenant, tenant.branch_a, key))
            async with p3c_engine.connect() as observer:
                blockers = (await observer.execute(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid})).scalar_one()
            assert blockers == [holder_pid]
    created = await task
    assert not isinstance(created, Exception), created
    assert await _frozen(p3c_engine, created.payroll_period_id) == _expected(mode, "after")


@pytest.mark.parametrize("mode", MODES)
async def test_a_change_arriving_during_creation_waits_and_the_snapshot_is_the_before_state(
    p3c_client, p3c_engine, tenant, mode,
):
    """The writer arrives while creation is running: it is ordered after the snapshot, so
    the layout is the complete BEFORE state, and the change still lands afterwards."""
    changed, added, key = await _setup(p3c_client, p3c_engine, tenant)

    async with p3c_engine.connect() as structural:
        async with structural.begin():
            await structural.execute(
                text("SELECT payroll.fn_LockRateDefinitionStructure(:r)"),
                {"r": changed["rate_definition_id"]})
            structural_pid = (await structural.execute(
                text("SELECT pg_backend_pid()"))).scalar_one()
            creation, creator_pid = await _blocked_with_pid(
                p3c_engine, _creator(p3c_engine, tenant, tenant.branch_a, key))
            async with p3c_engine.connect() as observer:
                assert (await observer.execute(
                    text("SELECT pg_blocking_pids(:pid)"),
                    {"pid": creator_pid})).scalar_one() == [structural_pid]

            async def writer(conn):
                await _writer_changes(conn, tenant, mode, changed, added)

            write, writer_pid = await _blocked_with_pid(p3c_engine, writer)
            async with p3c_engine.connect() as observer:
                # The writer waits for the creation itself, not for any row.
                assert (await observer.execute(
                    text("SELECT pg_blocking_pids(:pid)"),
                    {"pid": writer_pid})).scalar_one() == [creator_pid]
    created = await asyncio.wait_for(creation, timeout=30)
    assert not isinstance(created, Exception), created
    assert await _frozen(p3c_engine, created.payroll_period_id) == _expected(mode, "before")
    assert not isinstance(await asyncio.wait_for(write, timeout=30), Exception)
    # The change was not lost: it applies to the next layout.
    async with p3c_engine.connect() as conn:
        configured = (await conn.execute(text("""
            SELECT count(*) FROM payroll.branchpayitemconfig
            WHERE branchid = :b AND paydefinitionid = :pd
        """), {"b": tenant.branch_a, "pd": added["pay_definition_id"]})).scalar_one()
    assert configured == 1


async def test_a_definition_newly_configured_during_creation_is_in_or_out_never_partial(
    p3c_client, p3c_engine, tenant,
):
    """Through the real API writer: a definition becoming applicable while creation waits
    is frozen only if its configuration committed before the creation took the lock."""
    changed, added, key = await _setup(p3c_client, p3c_engine, tenant)

    async with p3c_engine.connect() as structural:
        async with structural.begin():
            await structural.execute(
                text("SELECT payroll.fn_LockRateDefinitionStructure(:r)"),
                {"r": changed["rate_definition_id"]})
            creation, creator_pid = await _blocked_with_pid(
                p3c_engine, _creator(p3c_engine, tenant, tenant.branch_a, key))

            async def api_writer(conn):
                return await branch_config.update_branch_config(
                    tenant.company_id, tenant.owner, tenant.branch_a, added["pay_definition_id"],
                    branch_config.BranchConfigUpdate(is_active=True), conn)

            write, writer_pid = await _blocked_with_pid(p3c_engine, api_writer)
            async with p3c_engine.connect() as observer:
                assert (await observer.execute(
                    text("SELECT pg_blocking_pids(:pid)"),
                    {"pid": writer_pid})).scalar_one() == [creator_pid]
    created = await asyncio.wait_for(creation, timeout=30)
    assert not isinstance(created, Exception), created
    assert [d["code"] for d in period_definitions(created.payroll_period_id, tenant)] == ["ALPHA"]
    outcome = await asyncio.wait_for(write, timeout=30)
    # Behind the new Open period the writer is subject to the open-period boundary, which
    # it can now see: it either moved its effective date after the period or was rejected.
    assert not isinstance(outcome, Exception) or isinstance(outcome, HTTPException), outcome


async def test_creation_and_every_applicability_writer_never_deadlock(
    p3c_client, p3c_engine, tenant,
):
    """Creation (two Branches), bulk and single applicability writers, retirement and an
    assignment-structure lock holder run concurrently: nothing deadlocks."""
    alpha = await create_definition(p3c_client, tenant, definition_code="ALPHA")
    beta = await create_definition(p3c_client, tenant, definition_code="BETA")
    gamma = await create_definition(p3c_client, tenant, definition_code="GAMMA")
    for item in (alpha, beta):
        grant_applicability(tenant, item, tenant.branch_a)
        grant_applicability(tenant, item, tenant.branch_b)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_b)
    key_a = await _candidate_key(p3c_client, tenant, tenant.branch_a)
    key_b = await _candidate_key(p3c_client, tenant, tenant.branch_b)

    async def bulk(conn):
        return await branch_config.bulk_update_branch_config(
            tenant.company_id, tenant.owner, gamma["pay_definition_id"],
            BulkBranchConfigUpdate(
                target=BranchConfigTarget.SelectedBranches,
                branch_ids=[tenant.branch_b, tenant.branch_a], is_active=True,
                effective_from=date.today() + timedelta(days=30)), conn)

    async def single(conn):
        return await branch_config.update_branch_config(
            tenant.company_id, tenant.owner, tenant.branch_b, beta["pay_definition_id"],
            branch_config.BranchConfigUpdate(
                is_active=False, effective_from=date.today() + timedelta(days=40)), conn)

    async def retire(conn):
        return await definitions.retire_definition(
            tenant.company_id, tenant.owner, alpha["pay_definition_id"], conn)

    async def lock_holder(conn):
        await conn.execute(
            text("SELECT payroll.fn_LockRateDefinitionStructure(:r)"),
            {"r": beta["rate_definition_id"]})
        await asyncio.sleep(0.3)

    results = await asyncio.wait_for(asyncio.gather(
        _run(p3c_engine, _creator(p3c_engine, tenant, tenant.branch_a, key_a)),
        _run(p3c_engine, _creator(p3c_engine, tenant, tenant.branch_b, key_b)),
        _run(p3c_engine, bulk), _run(p3c_engine, single),
        _run(p3c_engine, retire), _run(p3c_engine, lock_holder)), timeout=60)
    for result in results:
        assert "deadlock" not in str(result).lower(), result
        assert not isinstance(result, Exception) or isinstance(result, HTTPException), result
    for created in results[:2]:
        assert not isinstance(created, Exception), created
        codes = {d["code"] for d in period_definitions(created.payroll_period_id, tenant)}
        # BETA is untouched; ALPHA is in unless the retirement committed first.
        assert "BETA" in codes and codes <= {"ALPHA", "BETA"}
