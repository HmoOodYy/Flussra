"""Preview publication serializes with editing and real payroll finalization."""
import asyncio
import threading

import pytest
from sqlalchemy import text

from app.imports import preview_service, preview_store
from tests.calculation_snapshot_helpers import finalize, standard_scenario, submit_and_approve
from tests.compensation_api_fixtures import (  # noqa: F401
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import p3b_database  # noqa: F401
from tests.test_import_preview_api import (
    _definition_target,  # noqa: F401
    _map,
    _payroll_inputs,
    _payroll_snapshot,
    _period,
    _upload,
    payroll_tenant,  # noqa: F401 - register the aliased tenant fixture
)

pytestmark = pytest.mark.asyncio


async def stage(client, engine, tenant, period):
    source = await _payroll_inputs(engine, tenant, period)
    data = f"DriverNumber,WorkDate,Quantity\n{source['drivernumber']},{source['dates'][0]},4\n".encode()
    response = await _upload(client, tenant, period, data)
    assert response.status_code == 200, response.text
    iid = response.json()["import_id"]
    return iid, await _definition_target(client, tenant, iid), source


async def assert_empty_details(engine, iid):
    async with engine.connect() as db:
        for table in ("importsourcerecords", "filesourcerecords", "importissues", "payrollworkrecords",
                      "payrollworkdrivermatches", "payrollworkrecordidentifiers", "payrollworkcells"):
            assert await db.scalar(text(f"SELECT count(*) FROM import.{table} WHERE importid = :iid"), {"iid": iid}) == 0


async def test_two_mapping_publications_with_one_revision_have_one_atomic_winner(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    period = await _period(p3c_client, p3c_engine, tenant)
    iid, target, _ = await stage(p3c_client, p3c_engine, tenant, period)
    barrier = threading.Barrier(2)
    build = preview_service.build_preview

    def together(*args, **kwargs):
        barrier.wait(timeout=10)
        return build(*args, **kwargs)

    monkeypatch.setattr(preview_service, "build_preview", together)
    responses = await asyncio.gather(*(_map(p3c_client, tenant, iid, target) for _ in range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    refused = next(response for response in responses if response.status_code == 409)
    assert refused.json()["detail"]["code"] == "IMPORT_REVISION_CONFLICT"
    async with p3c_engine.connect() as db:
        assert await db.scalar(text("SELECT revision FROM import.imports WHERE importid = :iid"), {"iid": iid}) == 2
        assert await db.scalar(text("SELECT count(*) FROM import.payrollworkcells WHERE importid = :iid"), {"iid": iid}) == 1


async def test_preview_refuses_payroll_changes_between_comparison_and_publication(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    period = await _period(p3c_client, p3c_engine, tenant)
    iid, target, source = await stage(p3c_client, p3c_engine, tenant, period)
    started, release = threading.Event(), threading.Event()
    build = preview_service.build_preview

    def paused(*args, **kwargs):
        started.set()
        if not release.wait(timeout=15):
            raise RuntimeError("preview was not released")
        return build(*args, **kwargs)

    monkeypatch.setattr(preview_service, "build_preview", paused)
    task = asyncio.create_task(_map(p3c_client, tenant, iid, target))
    try:
        assert await asyncio.to_thread(started.wait, 10)
        assert p3c_engine.sync_engine.pool.checkedout() == 0
        changed = await p3c_client.post(f"/payroll/periods/{period['payroll_period_id']}/lines",
            json={"driver_assignment_id": tenant.driver_a, "payroll_period_definition_id": int(target.split(":")[1]),
                  "work_date": source["dates"][0].isoformat(), "quantity": "2"}, headers=tenant.admin)
        assert changed.status_code == 201, changed.text
        before = await _payroll_snapshot(p3c_engine, period["payroll_period_id"])
    finally:
        release.set()
    response = await task
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "IMPORT_PREVIEW_CONTEXT_CHANGED"
    await assert_empty_details(p3c_engine, iid)
    assert await _payroll_snapshot(p3c_engine, period["payroll_period_id"]) == before


@pytest.mark.parametrize("operation", ["mapping", "refresh"])
async def test_mapping_and_refresh_cannot_recreate_detail_after_real_finalization(
    p3c_client, p3c_engine, tenant, monkeypatch, operation,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    iid, target, _ = await stage(p3c_client, p3c_engine, tenant, period)
    if operation == "refresh":
        mapped = await _map(p3c_client, tenant, iid, target)
        assert mapped.status_code == 200, mapped.text
    started, release = threading.Event(), threading.Event()
    build = preview_service.build_preview

    def paused(*args, **kwargs):
        started.set()
        if not release.wait(timeout=20):
            raise RuntimeError("preview was not released")
        return build(*args, **kwargs)

    monkeypatch.setattr(preview_service, "build_preview", paused)
    task = asyncio.create_task(_map(p3c_client, tenant, iid, target) if operation == "mapping" else
        p3c_client.post(f"/payroll/imports/{iid}/refresh", json={"expected_revision": 2}, headers=tenant.admin))
    try:
        assert await asyncio.to_thread(started.wait, 10)
        assert p3c_engine.sync_engine.pool.checkedout() == 0
        await submit_and_approve(p3c_client, tenant, period)
        await finalize(p3c_client, tenant, period)
        finalized = await _payroll_snapshot(p3c_engine, period["payroll_period_id"])
    finally:
        release.set()
    response = await task
    assert response.status_code == 409, response.text
    await assert_empty_details(p3c_engine, iid)
    assert await _payroll_snapshot(p3c_engine, period["payroll_period_id"]) == finalized
    metadata = (await p3c_client.get(f"/payroll/imports/{iid}", headers=tenant.admin)).json()
    assert metadata["file"]["content_purged_at"]
    assert metadata["fingerprints"]["plan"] is None


async def test_prepared_refresh_failure_rolls_back_old_detail_and_closes_all_streams(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    period = await _period(p3c_client, p3c_engine, tenant)
    iid, target, _ = await stage(p3c_client, p3c_engine, tenant, period)
    mapped = await _map(p3c_client, tenant, iid, target)
    assert mapped.status_code == 200, mapped.text
    async with p3c_engine.connect() as db:
        before = (await db.execute(text("SELECT to_jsonb(c) FROM import.payrollworkcells c WHERE importid = :iid"),
                                   {"iid": iid})).scalars().all()
    streams, tables = [], []
    prepare, copy = preview_service.prepare_preview, preview_store.copy_prepared

    def observed(value):
        result = prepare(value)
        streams.extend(stream for _, stream in result.streams.values())
        return result

    async def fail_after_cells(driver, table, *args):
        tables.append(table)
        await copy(driver, table, *args)
        if table == "payrollworkcells":
            raise RuntimeError("injected publication failure after guarded cells")

    monkeypatch.setattr(preview_service, "prepare_preview", observed)
    monkeypatch.setattr(preview_store, "copy_prepared", fail_after_cells)
    with pytest.raises(RuntimeError, match="injected publication failure"):
        await p3c_client.post(f"/payroll/imports/{iid}/refresh", headers=tenant.admin,
                              json={"expected_revision": 2})
    assert "payrollworkcells" in tables
    assert streams and all(stream.closed for stream in streams)
    async with p3c_engine.connect() as db:
        after = (await db.execute(text("SELECT to_jsonb(c) FROM import.payrollworkcells c WHERE importid = :iid"),
                                  {"iid": iid})).scalars().all()
        assert after == before
        assert await db.scalar(text("SELECT revision FROM import.imports WHERE importid = :iid"), {"iid": iid}) == 2


async def test_stale_context_after_preparation_discards_streams_before_any_publication(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    period = await _period(p3c_client, p3c_engine, tenant)
    iid, target, source = await stage(p3c_client, p3c_engine, tenant, period)
    prepared_event, release = threading.Event(), threading.Event()
    streams = []
    prepare = preview_service.prepare_preview

    def paused(value):
        prepared = prepare(value)
        streams.extend(stream for _, stream in prepared.streams.values())
        prepared_event.set()
        if not release.wait(timeout=15):
            prepared.close()
            raise RuntimeError("preparation was not released")
        return prepared

    monkeypatch.setattr(preview_service, "prepare_preview", paused)
    task = asyncio.create_task(_map(p3c_client, tenant, iid, target))
    try:
        assert await asyncio.to_thread(prepared_event.wait, 10)
        assert p3c_engine.sync_engine.pool.checkedout() == 0
        changed = await p3c_client.post(f"/payroll/periods/{period['payroll_period_id']}/lines",
            json={"driver_assignment_id": tenant.driver_a, "payroll_period_definition_id": int(target.split(":")[1]),
                  "work_date": source["dates"][0].isoformat(), "quantity": "2"}, headers=tenant.admin)
        assert changed.status_code == 201, changed.text
    finally:
        release.set()
    result = await task
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "IMPORT_PREVIEW_CONTEXT_CHANGED"
    assert streams and all(stream.closed for stream in streams)
    await assert_empty_details(p3c_engine, iid)
