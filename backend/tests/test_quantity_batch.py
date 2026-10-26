"""Canonical quantity batches retain the interactive source invariants and evidence."""
import asyncio
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.payroll.quantity_batch import (
    ReviewedQuantityChange,
    apply_reviewed_quantities,
    prepare_quantity_mutation_scope,
)
from tests.compensation_api_fixtures import (  # noqa: F401 - register isolated API fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import p3b_database  # noqa: F401
from tests.payroll_runtime_fixtures import (
    build_payroll_tenant,
    make_driver_self_user,
    open_period_with_grid,
    rated_definition,
    seed_status_key,
    set_employment,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _fixture(client, engine, tenant, **overrides):
    await rated_definition(client, tenant, code="PARTIAL", name="Other quantity")
    period, _ = await open_period_with_grid(client, engine, tenant, **overrides)
    async with engine.connect() as db:
        definition_ids = list((await db.execute(text("""SELECT payrollperioddefinitionid
            FROM payroll.payrollperioddefinitions WHERE companyid=:cid AND payrollperiodid=:pid
            ORDER BY payrollperioddefinitionid"""), {"cid": tenant.company_id, "pid": period["payroll_period_id"]})).scalars())
        day = await db.scalar(text("""SELECT min(d) FROM payroll.payrollperioddrivereligibility,
            LATERAL unnest(eligibleworkdates) AS d WHERE companyid=:cid AND payrollperiodid=:pid
            AND driverassignmentid=:aid"""), {"cid": tenant.company_id,
                "pid": period["payroll_period_id"], "aid": tenant.driver_a})
    assert day is not None
    return period, definition_ids, day


def _change(tenant, day, definition, quantity, expected=None, *, assignment=None):
    return ReviewedQuantityChange(assignment or tenant.driver_a, day, definition, Decimal(quantity),
                                  expected is not None, None if expected is None else Decimal(expected))


async def _apply(engine, tenant, period, changes, *, actor=None, correlation=None):
    async with engine.begin() as db:
        scope = await prepare_quantity_mutation_scope(db, company_id=tenant.company_id,
            period_id=period["payroll_period_id"], user_id=actor or tenant.owner)
        return await apply_reviewed_quantities(scope, changes, correlation_id=correlation or str(uuid4()))


async def test_bulk_create_update_void_noop_keeps_unsupplied_facts_and_complete_evidence(
    p3c_client, p3c_engine, tenant,
):
    period, definitions, day = await _fixture(p3c_client, p3c_engine, tenant)
    first, omitted = definitions[:2]
    created = await _apply(p3c_engine, tenant, period, [
        _change(tenant, day, first, "2"), _change(tenant, day, omitted, "9"),
    ])
    assert (created.created, created.updated, created.voided, created.unchanged) == (2, 0, 0, 0)
    correlation = str(uuid4())
    updated = await _apply(p3c_engine, tenant, period, [_change(tenant, day, first, "3.0", "2")], correlation=correlation)
    assert (updated.created, updated.updated, updated.voided, updated.unchanged) == (0, 1, 0, 0)
    noop = await _apply(p3c_engine, tenant, period, [_change(tenant, day, first, "3.00", "3")])
    assert noop.unchanged == 1 and not noop.changed
    voided = await _apply(p3c_engine, tenant, period, [_change(tenant, day, first, "0", "3")])
    assert voided.voided == 1
    async with p3c_engine.connect() as db:
        rows = (await db.execute(text("""SELECT payrollperioddefinitionid, quantity, status, sourcetype
            FROM payroll.payrolldraftlines WHERE companyid=:cid AND payrollperiodid=:pid
            ORDER BY payrollperioddefinitionid"""), {"cid": tenant.company_id, "pid": period["payroll_period_id"]})).all()
        assert rows == [(first, Decimal("3"), "Void", "Import"), (omitted, Decimal("9"), "Active", "Import")]
        events = (await db.execute(text("""SELECT actioncode, beforestatejson, afterstatejson,
            actoruserid, driverid, responsibilitycontextsnapshot
            FROM payroll.payrollperiodauditevidenceevents
            WHERE companyid=:cid AND payrollperiodid=:pid AND evidencedomain='SOURCE' ORDER BY payrollperiodauditevidenceeventid"""),
            {"cid": tenant.company_id, "pid": period["payroll_period_id"]})).mappings().all()
        assert [e["actioncode"] for e in events] == ["SOURCE_CREATED", "SOURCE_CREATED", "SOURCE_UPDATED", "SOURCE_VOIDED"]
        assert all(e["actoruserid"] == tenant.owner and e["driverid"] == tenant.driver_a_id
                   and e["responsibilitycontextsnapshot"] for e in events)
        assert Decimal(events[2]["beforestatejson"]["quantity"]) == 2
        assert Decimal(events[2]["afterstatejson"]["quantity"]) == 3
        assert events[3]["afterstatejson"]["status"] == "Void"
        assert await db.scalar(text("SELECT count(*) FROM audit.auditlog WHERE correlationid=CAST(:c AS uuid)"), {"c": correlation}) == 1


async def test_stale_batch_duplicate_scope_precision_and_authorization_are_rejected(p3c_client, p3c_engine, tenant):
    period, definitions, day = await _fixture(p3c_client, p3c_engine, tenant)
    change = _change(tenant, day, definitions[0], "2")
    await _apply(p3c_engine, tenant, period, [change])
    for changes, code in [
        ([change], "QUANTITY_BATCH_STALE"),
        ([change, change], "QUANTITY_BATCH_DUPLICATE"),
        ([_change(tenant, day, definitions[0], "1.00001", "2")], "QUANTITY_BATCH_PRECISION"),
        ([_change(tenant, day, definitions[0], "1", assignment=tenant.driver_b)], "QUANTITY_BATCH_SCOPE"),
        ([_change(tenant, day, -1, "1")], "QUANTITY_BATCH_DEFINITION"),
        ([_change(tenant, date(2001, 1, 1), definitions[0], "1")], "QUANTITY_BATCH_DATE"),
    ]:
        with pytest.raises(HTTPException) as error:
            await _apply(p3c_engine, tenant, period, changes)
        assert error.value.detail["code"] == code
    with pytest.raises(HTTPException) as error:
        await _apply(p3c_engine, tenant, period, [], actor=tenant.no_permissions)
    assert error.value.status_code == 403


async def test_bulk_evidence_failure_rolls_back_source_and_audit(p3c_client, p3c_engine, tenant, monkeypatch):
    period, definitions, day = await _fixture(p3c_client, p3c_engine, tenant)
    async def failed(**kwargs):
        raise RuntimeError("evidence unavailable")
    monkeypatch.setattr("app.payroll.quantity_batch.capture_source_evidence_batch", failed)
    with pytest.raises(RuntimeError, match="evidence unavailable"):
        await _apply(p3c_engine, tenant, period, [_change(tenant, day, definitions[0], "2")])
    async with p3c_engine.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM payroll.payrolldraftlines WHERE companyid=:cid"), {"cid": tenant.company_id}) == 0
        assert await db.scalar(text("SELECT count(*) FROM audit.auditlog WHERE companyid=:cid AND actioncode='DRAFT_LINE_ADDED'"), {"cid": tenant.company_id}) == 0


async def test_lifecycle_rescue_updates_exact_existing_target_but_never_creates_new_source(p3c_client, p3c_engine, tenant):
    period, definitions, day = await _fixture(p3c_client, p3c_engine, tenant)
    await _apply(p3c_engine, tenant, period, [_change(tenant, day, definitions[0], "2")])
    set_employment(tenant, tenant.driver_a, termination_date=day - timedelta(days=1),
                   employment_status="Terminated", driver_status="Inactive")
    rescued = await _apply(p3c_engine, tenant, period, [_change(tenant, day, definitions[0], "3", "2")])
    assert rescued.updated == 1
    for change in [_change(tenant, day, definitions[1], "1"),
                   _change(tenant, day + timedelta(days=1), definitions[0], "1")]:
        with pytest.raises(HTTPException) as error:
            await _apply(p3c_engine, tenant, period, [change])
        assert error.value.detail["code"] == "QUANTITY_BATCH_ELIGIBILITY"
    assert (await _apply(p3c_engine, tenant, period, [_change(tenant, day, definitions[0], "0", "3")])).voided == 1


async def test_full_day_off_whole_number_and_driver_subject_rules_remain_active(p3c_client, p3c_engine, tenant):
    period, definitions, day = await _fixture(p3c_client, p3c_engine, tenant, input_type="WholeNumber")
    seed_status_key(tenant, "ABSENT", off=True)
    saved = await p3c_client.post(f"/payroll/periods/{period['payroll_period_id']}/day-grid",
        json={"work_date": day.isoformat(), "rows": [{"driver_assignment_id": tenant.driver_a,
              "values": {}, "status_key": "ABSENT"}]}, headers=tenant.admin)
    assert saved.status_code == 200, saved.text
    with pytest.raises(HTTPException) as error:
        await _apply(p3c_engine, tenant, period, [_change(tenant, day, definitions[1], "1")])
    assert error.value.detail["code"] == "STATUS_OFF_WORK_CONFLICT"
    with pytest.raises(HTTPException) as error:
        await _apply(p3c_engine, tenant, period, [_change(tenant, day, definitions[1], "1.5")])
    assert error.value.status_code == 422
    make_driver_self_user(tenant, permissions=["payroll.view", "payroll.entry"])
    async with p3c_engine.connect() as db:
        driver_actor = await db.scalar(text("""SELECT userid FROM sec.userbranchroles
            WHERE companyid=:cid AND scopetype='Self' LIMIT 1"""), {"cid": tenant.company_id})
    assert driver_actor is not None
    with pytest.raises(HTTPException) as error:
        await _apply(p3c_engine, tenant, period, [], actor=driver_actor)
    assert error.value.status_code == 403


async def test_scope_owns_period_lock_and_rejects_after_transaction_ends(p3c_client, p3c_engine, tenant):
    period, definitions, day = await _fixture(p3c_client, p3c_engine, tenant)
    async with p3c_engine.begin() as db:
        scope = await prepare_quantity_mutation_scope(db, company_id=tenant.company_id,
            period_id=period["payroll_period_id"], user_id=tenant.owner)
        async def competing_lock():
            async with p3c_engine.begin() as other:
                await other.execute(text("SET LOCAL lock_timeout='150ms'"))
                await other.execute(text("SELECT 1 FROM payroll.payrollperiods WHERE payrollperiodid=:pid FOR UPDATE"),
                                    {"pid": period["payroll_period_id"]})
        from sqlalchemy.exc import DBAPIError
        with pytest.raises(DBAPIError, match="lock timeout"):
            await asyncio.create_task(competing_lock())
        await apply_reviewed_quantities(scope, [_change(tenant, day, definitions[0], "1")], correlation_id=str(uuid4()))
    with pytest.raises(RuntimeError, match="expired"):
        await apply_reviewed_quantities(scope, [], correlation_id=str(uuid4()))
