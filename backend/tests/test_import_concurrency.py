"""Concurrent creation, resource editing and bounded admission use actual HTTP requests."""
import asyncio
import threading
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.imports import service
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
    path = f"/payroll/imports/{iid}/mapping"
    outcomes = await asyncio.gather(*(p3c_client.put(path,
        json={"expected_revision": 1, "date_order": order}, headers=tenant.admin) for order in ("DMY", "MDY")))
    assert sorted(response.status_code for response in outcomes) == [200, 409]
    refused = next(response for response in outcomes if response.status_code == 409)
    assert refused.json()["detail"]["code"] == "IMPORT_REVISION_CONFLICT"
    current = (await p3c_client.get(f"/payroll/imports/{iid}", headers=tenant.admin)).json()
    assert current["revision"] == 2


async def test_two_active_parsers_bound_capacity_while_ordinary_requests_continue(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    two_started = threading.Barrier(3)
    release = threading.Event()
    inspect = service.inspect_file

    def paused_parser(*args, **kwargs):
        two_started.wait(timeout=10)
        if not release.wait(timeout=10):
            raise RuntimeError("parser was not released")
        return inspect(*args, **kwargs)

    monkeypatch.setattr(service, "inspect_file", paused_parser)
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
        # A stored future contract is valid persistence but not interpretable
        # by this application; no fallback or compatibility adapter may guess.
        iid = (await db.execute(text("""INSERT INTO import.imports
            (companyid, branchid, importkind, sourcekind, correlationid, contractversion,
             idempotencykey, creationfingerprint, createdbyuserid, updatedbyuserid)
            VALUES (:cid, :bid, 'PayrollWork', 'File', :uuid, 2, :key, :hash, :uid, :uid) RETURNING importid"""),
            {"cid": tenant.company_id, "bid": tenant.branch_a, "uuid": uuid4(), "key": uuid4(), "hash": "0" * 64, "uid": tenant.owner})).scalar_one()
        await db.execute(text("""INSERT INTO import.payrollworkimports (importid, companyid, branchid, payrollperiodid)
            VALUES (:iid, :cid, :bid, :pid)"""), {"iid": iid, "cid": tenant.company_id, "bid": tenant.branch_a, "pid": period["payroll_period_id"]})
        await db.execute(text("""INSERT INTO import.filesources
            (importid, companyid, originalfilename, filetype, filesizebytes, filehashsha256, filecontent)
            VALUES (:iid, :cid, 'future.csv', 'csv', 1, :hash, :content)"""),
            {"iid": iid, "cid": tenant.company_id, "hash": "0" * 64, "content": b"x"})
    response = await p3c_client.get(f"/payroll/imports/{iid}", headers=tenant.admin)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "IMPORT_CONTRACT_UNSUPPORTED"


async def test_payroll_work_schema_supports_multiple_identifiers_and_refuses_foreign_scope(
    p3c_client, p3c_engine, tenant, p3b_dsn,
):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    iid = (await upload(p3c_client, tenant, period, uuid4())).json()["import_id"]
    other = build_payroll_tenant(p3b_dsn)
    async with p3c_engine.begin() as db:
        params = {"iid": iid, "cid": tenant.company_id}
        rid = (await db.execute(text("""INSERT INTO import.importsourcerecords (importid, companyid, locator, payload)
            VALUES (:iid, :cid, '{"row": 2}', '{}') RETURNING sourcerecordid"""), params)).scalar_one()
        params["rid"] = rid
        await db.execute(text("""INSERT INTO import.payrollworkrecords (sourcerecordid, importid, companyid)
            VALUES (:rid, :iid, :cid)"""), params)
        for column, kind in enumerate(("DriverNumber", "EmployeeKey"), 1):
            match = (await db.execute(text("""INSERT INTO import.payrollworkdrivermatches
                (importid, companyid, identifierkind, normalizedvalue) VALUES (:iid, :cid, :kind, '001') RETURNING drivermatchid"""),
                {**params, "kind": kind})).scalar_one()
            await db.execute(text("""INSERT INTO import.payrollworkrecordidentifiers
                (sourcerecordid, importid, companyid, sourcecolumnindex, drivermatchid)
                VALUES (:rid, :iid, :cid, :column, :match)"""), {**params, "column": column, "match": match})
        assert (await db.execute(text("SELECT count(*) FROM import.payrollworkrecordidentifiers WHERE sourcerecordid = :rid"), params)).scalar_one() == 2
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(text("""INSERT INTO import.payrollworkdrivermatches
                    (importid, companyid, identifierkind, normalizedvalue, driverid)
                    VALUES (:iid, :cid, 'Other', 'foreign', :driver)"""), {**params, "driver": other.driver_a_id})
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(text("""INSERT INTO import.payrollworkcells
                    (sourcerecordid, importid, companyid, branchid, payrollperiodid, sourcecolumnindex, driverassignmentid)
                    VALUES (:rid, :iid, :cid, :bid, :pid, 3, :assignment)"""),
                    {**params, "bid": tenant.branch_a, "pid": period["payroll_period_id"], "assignment": tenant.driver_b})


async def test_authority_is_rechecked_after_parser_returns(p3c_client, p3c_engine, tenant, monkeypatch):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    entered, release = threading.Event(), threading.Event()
    inspect = service.inspect_file

    def held(*args, **kwargs):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("test did not release parser")
        return inspect(*args, **kwargs)

    monkeypatch.setattr(service, "inspect_file", held)
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
