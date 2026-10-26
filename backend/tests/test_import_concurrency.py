"""Concurrent creation, resource editing and bounded admission use actual HTTP requests."""
import asyncio
import threading
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.imports import batch_service
from app.imports.mapping import targets as mapping_targets
from tests.calculation_snapshot_helpers import finalize, standard_scenario, submit_and_approve
from tests.compensation_api_fixtures import (  # noqa: F401
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import p3b_database  # noqa: F401
from tests.payroll_runtime_fixtures import build_payroll_tenant, open_period_with_grid

pytestmark = pytest.mark.asyncio
DATA = b"Driver Number,Work Date,Units\n001,2026-10-20,1\n"


@pytest.fixture
def tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def upload(client, tenant, period, key, data=DATA):
    return await client.post(f"/payroll/periods/{period['payroll_period_id']}/imports",
                             files={"file": ("work.csv", data)},
                             headers={**tenant.admin, "Idempotency-Key": str(key)})


async def _mapped_import(client, engine, tenant, period, assignment_id, definition_id, definition_code, work_date):
    async with engine.connect() as db:
        driver_number = await db.scalar(text("""
            SELECT d.drivernumber FROM core.driverassignments a
            JOIN core.drivers d ON d.driverid=a.driverid AND d.companyid=a.companyid
            WHERE a.companyid=:cid AND a.driverassignmentid=:aid
        """), {"cid": tenant.company_id, "aid": assignment_id})
    data = (f"DriverNumber,WorkDate,{definition_code}\n"
            f"{driver_number},{work_date.isoformat()},4\n").encode()
    response = await upload(client, tenant, period, uuid4(), data)
    assert response.status_code == 200, response.text
    created = response.json()
    layout = created["layouts"][0]
    mapping = await client.put(
        f"/payroll/imports/{created['import_id']}/layouts/{layout['layout_id']}/mapping",
        json={"expected_revision": created["revision"], "column_mapping": {
            "1": "DriverNumber", "2": "WorkDate", "3": f"Definition:{definition_id}"}},
        headers=tenant.admin,
    )
    assert mapping.status_code == 200, mapping.text
    return created, mapping.json()


@pytest.mark.parametrize("same_payload", [True, False])
async def test_concurrent_creation_uses_key_uniqueness_and_exact_identity(
    p3c_client, p3c_engine, tenant, same_payload,
):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    key = uuid4()
    first, second = await asyncio.gather(upload(p3c_client, tenant, period, key),
        upload(p3c_client, tenant, period, key, DATA if same_payload else DATA + b"\n"))
    if same_payload:
        assert first.status_code == second.status_code == 200, (first.text, second.text)
        assert first.json()["import_id"] == second.json()["import_id"]
    else:
        assert sorted([first.status_code, second.status_code]) == [200, 409]
        refused = first if first.status_code == 409 else second
        assert refused.json()["detail"]["code"] == "IMPORT_IDEMPOTENCY_CONFLICT"
    async with p3c_engine.connect() as db:
        assert (await db.execute(text("SELECT count(*) FROM import.imports WHERE companyid = :cid"), {"cid": tenant.company_id})).scalar_one() == 1


async def test_concurrent_interpretation_rechecks_revision_after_parsing(p3c_client, p3c_engine, tenant):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    created = await upload(p3c_client, tenant, period, uuid4())
    iid = created.json()["import_id"]
    path = f"/payroll/imports/{iid}/interpretation"
    source_unit_id = created.json()["files"][0]["units"][0]["source_unit_id"]
    outcomes = await asyncio.gather(*(p3c_client.put(path,
        json={"expected_revision": 1, "units": [{"source_unit_id": source_unit_id,
            "selected": True, "header_row_number": 2, "date_order": order}]}, headers=tenant.admin)
        for order in ("DMY", "MDY")))
    assert sorted(response.status_code for response in outcomes) == [200, 409]
    refused = next(response for response in outcomes if response.status_code == 409)
    assert refused.json()["detail"]["code"] == "IMPORT_REVISION_CONFLICT"
    current = (await p3c_client.get(f"/payroll/imports/{iid}", headers=tenant.admin)).json()
    assert current["revision"] == 2


async def test_preview_rechecks_canonical_quantity_and_preserves_prior_publication(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    period, _grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    async with p3c_engine.connect() as db:
        driver = dict((await db.execute(text("""
            SELECT p.driverassignmentid, d.drivernumber, x.workdate
            FROM payroll.payrollperioddrivereligibility p
            JOIN core.driverassignments a ON a.driverassignmentid=p.driverassignmentid
                AND a.companyid=p.companyid AND a.branchid=p.branchid
            JOIN core.drivers d ON d.driverid=a.driverid AND d.companyid=a.companyid
            CROSS JOIN LATERAL unnest(p.eligibleworkdates) AS x(workdate)
            WHERE p.companyid=:cid AND p.payrollperiodid=:pid AND p.branchid=:bid
                AND p.iseligibleforperiod ORDER BY d.driverid,x.workdate LIMIT 1
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"],
                "bid": period["branch_id"]})).mappings().one())
        from app.payroll.period_definitions import list_period_definitions
        definitions = await list_period_definitions(period["payroll_period_id"], tenant.company_id, db, active_only=True)
    definition = next(item for item in mapping_targets(definitions)
                      if item["target"].startswith("Definition:"))
    definition_id = int(definition["target"].split(":", 1)[1])
    canonical = await p3c_client.post(f"/payroll/periods/{period['payroll_period_id']}/lines",
        json={"driver_assignment_id": driver["driverassignmentid"], "work_date": driver["workdate"].isoformat(),
              "payroll_period_definition_id": definition_id, "quantity": "2"}, headers=tenant.admin)
    assert canonical.status_code == 201, canonical.text

    data = (f"DriverNumber,WorkDate,{definition['code']}\n"
            f"{driver['drivernumber']},{driver['workdate'].isoformat()},4\n").encode()
    uploaded = await upload(p3c_client, tenant, period, uuid4(), data)
    assert uploaded.status_code == 200, uploaded.text
    created = uploaded.json()
    layout = created["layouts"][0]
    mapping = await p3c_client.put(
        f"/payroll/imports/{created['import_id']}/layouts/{layout['layout_id']}/mapping",
        json={"expected_revision": created["revision"], "column_mapping": {
            "1": "DriverNumber", "2": "WorkDate", "3": f"Definition:{definition_id}"}},
        headers=tenant.admin,
    )
    assert mapping.status_code == 200, mapping.text
    initial = await p3c_client.post(f"/payroll/imports/{created['import_id']}/preview",
        json={"expected_revision": mapping.json()["revision"]}, headers=tenant.admin)
    assert initial.status_code == 200, initial.text
    published = initial.json()
    import_id = created["import_id"]
    async with p3c_engine.connect() as db:
        before = (await db.execute(text("""
            SELECT sourcerecordid,rowfingerprint FROM import.payrollworkrecords
            WHERE companyid=:cid AND importid=:iid ORDER BY sourcerecordid
        """), {"cid": tenant.company_id, "iid": import_id})).all()
    assert before

    entered, release = threading.Event(), threading.Event()
    build = batch_service.build_batch_preview

    def held_build(*args, **kwargs):
        result = build(*args, **kwargs)
        entered.set()
        if not release.wait(10):
            raise RuntimeError("test did not release preview build")
        return result

    monkeypatch.setattr(batch_service, "build_batch_preview", held_build)
    pending = asyncio.create_task(p3c_client.post(
        f"/payroll/imports/{import_id}/preview",
        json={"expected_revision": published["revision"]}, headers=tenant.admin))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        changed = await p3c_client.patch(
            f"/payroll/periods/{period['payroll_period_id']}/lines/{canonical.json()['draft_line_id']}",
            json={"quantity": "3"}, headers=tenant.admin)
        assert changed.status_code == 200, changed.text
    finally:
        release.set()
    stale = await pending
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "IMPORT_CONTEXT_STALE"
    current = await p3c_client.get(f"/payroll/imports/{import_id}", headers=tenant.admin)
    assert current.status_code == 200, current.text
    assert current.json()["revision"] == published["revision"]
    assert current.json()["preview"]["plan_fingerprint"] == published["preview"]["plan_fingerprint"]
    async with p3c_engine.connect() as db:
        after = (await db.execute(text("""
            SELECT sourcerecordid,rowfingerprint FROM import.payrollworkrecords
            WHERE companyid=:cid AND importid=:iid ORDER BY sourcerecordid
        """), {"cid": tenant.company_id, "iid": import_id})).all()
    assert after == before


async def test_interpretation_revision_wins_against_held_preview_without_partial_publication(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    period, grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    async with p3c_engine.connect() as db:
        driver = (await db.execute(text("""
            SELECT p.driverassignmentid, d.drivernumber, x.workdate
            FROM payroll.payrollperioddrivereligibility p
            JOIN core.driverassignments a ON a.driverassignmentid=p.driverassignmentid
                AND a.companyid=p.companyid AND a.branchid=p.branchid
            JOIN core.drivers d ON d.driverid=a.driverid AND d.companyid=a.companyid
            CROSS JOIN LATERAL unnest(p.eligibleworkdates) AS x(workdate)
            WHERE p.companyid=:cid AND p.payrollperiodid=:pid AND p.branchid=:bid
                AND p.iseligibleforperiod ORDER BY d.driverid,x.workdate LIMIT 1
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"],
                "bid": period["branch_id"]})).one()
    definition = next(item for item in grid["columns"] if item.get("payroll_period_definition_id"))
    created, mapping = await _mapped_import(p3c_client, p3c_engine, tenant, period,
        driver.driverassignmentid, definition["payroll_period_definition_id"], definition["definition_code"],
        driver.workdate)
    unit_id = created["files"][0]["units"][0]["source_unit_id"]

    entered, release = threading.Event(), threading.Event()
    build = batch_service.build_batch_preview

    def held_build(*args, **kwargs):
        result = build(*args, **kwargs)
        entered.set()
        if not release.wait(10):
            raise RuntimeError("test did not release preview build")
        return result

    monkeypatch.setattr(batch_service, "build_batch_preview", held_build)
    pending = asyncio.create_task(p3c_client.post(
        f"/payroll/imports/{created['import_id']}/preview",
        json={"expected_revision": mapping["revision"]}, headers=tenant.admin))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        changed = await p3c_client.put(
            f"/payroll/imports/{created['import_id']}/interpretation",
            json={"expected_revision": mapping["revision"], "units": [{
                "source_unit_id": unit_id, "selected": True, "date_order": "YMD"}]},
            headers=tenant.admin,
        )
        assert changed.status_code == 200, changed.text
    finally:
        release.set()
    stale = await pending
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "IMPORT_REVISION_CONFLICT"
    current = await p3c_client.get(f"/payroll/imports/{created['import_id']}", headers=tenant.admin)
    assert current.status_code == 200, current.text
    assert current.json()["revision"] == changed.json()["revision"]
    assert current.json()["status"] == "NeedsMapping"
    async with p3c_engine.connect() as db:
        counts = (await db.execute(text("""
            SELECT (SELECT count(*) FROM import.filesourcerecords WHERE importid=:iid),
                   (SELECT count(*) FROM import.payrollworkrecords WHERE importid=:iid),
                   (SELECT count(*) FROM import.payrollworkpreviewgroups WHERE importid=:iid)
        """), {"iid": created["import_id"]})).one()
    assert counts == (0, 0, 0)


async def test_failed_preview_publication_rolls_back_after_copy_inserts(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    period, grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    async with p3c_engine.connect() as db:
        driver = (await db.execute(text("""
            SELECT p.driverassignmentid, d.drivernumber, x.workdate
            FROM payroll.payrollperioddrivereligibility p
            JOIN core.driverassignments a ON a.driverassignmentid=p.driverassignmentid
                AND a.companyid=p.companyid AND a.branchid=p.branchid
            JOIN core.drivers d ON d.driverid=a.driverid AND d.companyid=a.companyid
            CROSS JOIN LATERAL unnest(p.eligibleworkdates) AS x(workdate)
            WHERE p.companyid=:cid AND p.payrollperiodid=:pid AND p.branchid=:bid
                AND p.iseligibleforperiod ORDER BY d.driverid,x.workdate LIMIT 1
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"],
                "bid": period["branch_id"]})).one()
    definition = next(item for item in grid["columns"] if item.get("payroll_period_definition_id"))
    created, mapping = await _mapped_import(p3c_client, p3c_engine, tenant, period,
        driver.driverassignmentid, definition["payroll_period_definition_id"], definition["definition_code"],
        driver.workdate)
    first = await p3c_client.post(f"/payroll/imports/{created['import_id']}/preview",
        json={"expected_revision": mapping["revision"]}, headers=tenant.admin)
    assert first.status_code == 200, first.text
    published = first.json()
    async with p3c_engine.connect() as db:
        before = (await db.execute(text("""
            SELECT sourcerecordid,rowfingerprint FROM import.payrollworkrecords
            WHERE companyid=:cid AND importid=:iid ORDER BY sourcerecordid
        """), {"cid": tenant.company_id, "iid": created["import_id"]})).all()

    persist = batch_service.persist_preview

    async def fail_after_copy(*args, **kwargs):
        await persist(*args, **kwargs)
        raise RuntimeError("injected failure after guarded COPY inserts")

    monkeypatch.setattr(batch_service, "persist_preview", fail_after_copy)
    with pytest.raises(RuntimeError, match="after guarded COPY"):
        await p3c_client.post(f"/payroll/imports/{created['import_id']}/preview",
            json={"expected_revision": published["revision"]}, headers=tenant.admin)
    current = await p3c_client.get(f"/payroll/imports/{created['import_id']}", headers=tenant.admin)
    assert current.status_code == 200, current.text
    assert current.json()["revision"] == published["revision"]
    assert current.json()["preview"]["plan_fingerprint"] == published["preview"]["plan_fingerprint"]
    async with p3c_engine.connect() as db:
        after = (await db.execute(text("""
            SELECT sourcerecordid,rowfingerprint FROM import.payrollworkrecords
            WHERE companyid=:cid AND importid=:iid ORDER BY sourcerecordid
        """), {"cid": tenant.company_id, "iid": created["import_id"]})).all()
    assert after == before


async def test_finalization_purges_details_while_held_preview_cannot_recreate_them(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    from datetime import date, timedelta

    from tests.calculation_snapshot_helpers import period_status

    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    work_date = date.fromisoformat(period["start_date"]) + timedelta(days=3)
    created, mapping = await _mapped_import(p3c_client, p3c_engine, tenant, period,
        tenant.driver_a, scenario.ppd, "HAUL", work_date)
    first = await p3c_client.post(f"/payroll/imports/{created['import_id']}/preview",
        json={"expected_revision": mapping["revision"]}, headers=tenant.admin)
    assert first.status_code == 200, first.text
    published = first.json()
    entered, release = threading.Event(), threading.Event()
    build = batch_service.build_batch_preview

    def held_build(*args, **kwargs):
        result = build(*args, **kwargs)
        entered.set()
        if not release.wait(20):
            raise RuntimeError("test did not release preview build")
        return result

    monkeypatch.setattr(batch_service, "build_batch_preview", held_build)
    pending = asyncio.create_task(p3c_client.post(
        f"/payroll/imports/{created['import_id']}/preview",
        json={"expected_revision": published["revision"]}, headers=tenant.admin))
    try:
        assert await asyncio.to_thread(entered.wait, 15)
        await submit_and_approve(p3c_client, tenant, period)
        finalized = await finalize(p3c_client, tenant, period)
        assert finalized.status_code == 200, finalized.text
        assert period_status(tenant, period) == "Locked"
    finally:
        release.set()
    stale = await pending
    assert stale.status_code == 409, stale.text
    async with p3c_engine.connect() as db:
        state = (await db.execute(text("""
            SELECT w.detailpurgedatutc, w.planfingerprint,
                   (SELECT count(*) FROM import.filesourcerecords fr WHERE fr.importid=i.importid),
                   (SELECT count(*) FROM import.payrollworkrecords r WHERE r.importid=i.importid),
                   (SELECT count(*) FROM import.filesources f WHERE f.importid=i.importid
                        AND f.filecontent IS NULL AND f.contentpurgedatutc IS NOT NULL)
            FROM import.imports i JOIN import.payrollworkimports w
                ON w.importid=i.importid AND w.companyid=i.companyid
            WHERE i.companyid=:cid AND i.importid=:iid
        """), {"cid": tenant.company_id, "iid": created["import_id"]})).one()
    assert state[0] is not None and state[1] is None
    assert state[2:] == (0, 0, 1)


async def test_import_metadata_get_is_one_consistent_snapshot_during_interpretation_commit(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    period, grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    async with p3c_engine.connect() as db:
        driver = (await db.execute(text("""
            SELECT p.driverassignmentid, d.drivernumber, x.workdate
            FROM payroll.payrollperioddrivereligibility p
            JOIN core.driverassignments a ON a.driverassignmentid=p.driverassignmentid
                AND a.companyid=p.companyid AND a.branchid=p.branchid
            JOIN core.drivers d ON d.driverid=a.driverid AND d.companyid=a.companyid
            CROSS JOIN LATERAL unnest(p.eligibleworkdates) AS x(workdate)
            WHERE p.companyid=:cid AND p.payrollperiodid=:pid AND p.branchid=:bid
                AND p.iseligibleforperiod ORDER BY d.driverid,x.workdate LIMIT 1
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"],
                "bid": period["branch_id"]})).one()
    definition = next(item for item in grid["columns"] if item.get("payroll_period_definition_id"))
    created, mapping = await _mapped_import(p3c_client, p3c_engine, tenant, period,
        driver.driverassignmentid, definition["payroll_period_definition_id"], definition["definition_code"],
        driver.workdate)
    unit_id = created["files"][0]["units"][0]["source_unit_id"]
    entered, release = threading.Event(), threading.Event()
    load = batch_service.load_import
    calls = 0

    async def held_load(db, cid, iid):
        nonlocal calls
        result = await load(db, cid, iid)
        calls += 1
        if calls == 2:
            entered.set()
            if not await asyncio.to_thread(release.wait, 10):
                raise RuntimeError("test did not release metadata read")
        return result

    monkeypatch.setattr(batch_service, "load_import", held_load)
    pending = asyncio.create_task(p3c_client.get(f"/payroll/imports/{created['import_id']}", headers=tenant.admin))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        changed = await p3c_client.put(
            f"/payroll/imports/{created['import_id']}/interpretation",
            json={"expected_revision": mapping["revision"], "units": [{
                "source_unit_id": unit_id, "selected": True, "date_order": "YMD"}]},
            headers=tenant.admin,
        )
        assert changed.status_code == 200, changed.text
    finally:
        release.set()
    first_read = await pending
    assert first_read.status_code == 200, first_read.text
    first_snapshot = first_read.json()
    assert first_snapshot["revision"] == mapping["revision"]
    assert first_snapshot["files"][0]["units"][0]["date_order"] is None
    second_read = await p3c_client.get(f"/payroll/imports/{created['import_id']}", headers=tenant.admin)
    assert second_read.status_code == 200, second_read.text
    assert second_read.json()["revision"] == changed.json()["revision"]
    assert second_read.json()["files"][0]["units"][0]["date_order"] == "YMD"


async def test_two_active_parsers_bound_capacity_while_ordinary_requests_continue(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    two_started = threading.Barrier(3)
    release = threading.Event()
    inspect = batch_service._inspect_batch

    def paused_parser(*args, **kwargs):
        two_started.wait(timeout=10)
        if not release.wait(timeout=10):
            raise RuntimeError("parser was not released")
        return inspect(*args, **kwargs)

    monkeypatch.setattr(batch_service, "_inspect_batch", paused_parser)
    tasks = [asyncio.create_task(upload(p3c_client, tenant, period, uuid4())) for _ in range(2)]
    try:
        await asyncio.to_thread(two_started.wait, 10)
        excess = await upload(p3c_client, tenant, period, uuid4())
        assert excess.status_code == 429, excess.text
        assert excess.json()["detail"]["code"] == "IMPORT_BUSY"
        assert excess.headers["retry-after"] == "1"
        ordinary = await asyncio.wait_for(p3c_client.get(f"/payroll/periods/{period['payroll_period_id']}", headers=tenant.admin), 3)
        assert ordinary.status_code == 200, ordinary.text
        assert p3c_engine.sync_engine.pool.checkedout() == 0
    finally:
        release.set()
    assert all(response.status_code == 200 for response in await asyncio.gather(*tasks))


async def test_parse_failure_and_unsupported_contract_fail_closed(p3c_client, p3c_engine, tenant):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    refused = await upload(p3c_client, tenant, period, uuid4(), b"bad\x00binary")
    assert refused.status_code == 422
    async with p3c_engine.begin() as db:
        assert (await db.execute(text("SELECT count(*) FROM import.imports WHERE companyid = :cid"), {"cid": tenant.company_id})).scalar_one() == 0
        # A stored future Import envelope is valid persistence but not
        # interpretable by this application; no compatibility guess is made.
        iid = (await db.execute(text("""INSERT INTO import.imports
            (companyid, branchid, importkind, sourcekind, correlationid, contractversion,
             idempotencykey, creationfingerprint, createdbyuserid, updatedbyuserid)
            VALUES (:cid, :bid, 'PayrollWork', 'File', :uuid, 2, :key, :hash, :uid, :uid) RETURNING importid"""),
            {"cid": tenant.company_id, "bid": tenant.branch_a, "uuid": uuid4(), "key": uuid4(), "hash": "0" * 64, "uid": tenant.owner})).scalar_one()
        await db.execute(text("""INSERT INTO import.payrollworkimports (importid, companyid, branchid, payrollperiodid)
            VALUES (:iid, :cid, :bid, :pid)"""), {"iid": iid, "cid": tenant.company_id, "bid": tenant.branch_a, "pid": period["payroll_period_id"]})
        await db.execute(text("""INSERT INTO import.filesourcebatches (importid, companyid, sourcemanifest)
            VALUES (:iid, :cid, CAST(:manifest AS jsonb))"""), {"iid": iid, "cid": tenant.company_id,
            "manifest": '[{"ordinal":1,"name":"future.csv","type":"csv","size":1,"sha256":"' + "0" * 64 +
            '","units":[{"name":"CSV","index":1}]}]'})
    response = await p3c_client.get(f"/payroll/imports/{iid}", headers=tenant.admin)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "IMPORT_CONTRACT_UNSUPPORTED"


async def test_batch_source_manifest_is_ordered_and_legacy_cell_store_is_absent(p3c_client, p3c_engine, tenant):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    response = await p3c_client.post(f"/payroll/periods/{period['payroll_period_id']}/imports",
        files=[("file", ("first.csv", DATA, "text/csv")), ("file", ("second.csv", DATA, "text/csv"))],
        headers={**tenant.admin, "Idempotency-Key": str(uuid4())})
    assert response.status_code == 200, response.text
    iid = response.json()["import_id"]
    async with p3c_engine.connect() as db:
        manifest, files, has_legacy_cells = (await db.execute(text("""
            SELECT b.sourcemanifest,
                (SELECT count(*) FROM import.filesources f WHERE f.importid = b.importid),
                to_regclass('import.payrollworkcells') IS NOT NULL
            FROM import.filesourcebatches b WHERE b.importid = :iid AND b.companyid = :cid
        """), {"iid": iid, "cid": tenant.company_id})).one()
    assert [entry["ordinal"] for entry in manifest] == [1, 2]
    assert [entry["name"] for entry in manifest] == ["first.csv", "second.csv"]
    assert files == 2
    assert has_legacy_cells is False


async def test_authority_is_rechecked_after_parser_returns(p3c_client, p3c_engine, tenant, monkeypatch):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    entered, release = threading.Event(), threading.Event()
    inspect = batch_service._inspect_batch

    def held(*args, **kwargs):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("test did not release parser")
        return inspect(*args, **kwargs)

    monkeypatch.setattr(batch_service, "_inspect_batch", held)
    pending = asyncio.create_task(p3c_client.post(f"/payroll/periods/{period['payroll_period_id']}/imports",
        files={"file": ("work.csv", DATA)}, headers={**tenant.headers(tenant.branch_user), "Idempotency-Key": str(uuid4())}))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        async with p3c_engine.begin() as db:
            await db.execute(text("UPDATE sec.userbranchroles SET isactive = FALSE WHERE userid = :uid AND companyid = :cid"),
                             {"uid": tenant.branch_user, "cid": tenant.company_id})
    finally:
        release.set()
    result = await pending
    assert result.status_code == 403, result.text
    async with p3c_engine.connect() as db:
        assert (await db.execute(text("SELECT count(*) FROM import.imports WHERE companyid = :cid"), {"cid": tenant.company_id})).scalar_one() == 0
