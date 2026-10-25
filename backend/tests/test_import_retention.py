"""Payroll finalization atomically purges all Import states and serializes staging."""
import asyncio
import threading
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.imports import service
from app.imports.retention import purge_payroll_work_import_details
from app.payroll import finalization
from tests.calculation_snapshot_helpers import (
    finalize,
    period_evidence_counts,
    period_status,
    rows,
    standard_scenario,
    submit_and_approve,
)
from tests.compensation_api_fixtures import (  # noqa: F401
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import p3b_database  # noqa: F401
from tests.payroll_runtime_fixtures import build_payroll_tenant

pytestmark = pytest.mark.asyncio
DATA = b"Driver Number,Date,Haul\n001,2026-10-20,1\n"


@pytest.fixture
def tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def upload(client, tenant, pid, *, key=None):
    return await client.post(f"/payroll/periods/{pid}/imports",
                             files={"file": ("work.csv", DATA)},
                             headers={**tenant.admin, "Idempotency-Key": str(key or uuid4())})


def details(tenant, iid):
    return rows(tenant, """SELECT i.status, i.revision, f.filecontent IS NULL, f.contentpurgedatutc,
        f.inspectionmetadata, f.sheetname, w.reviewsummary, w.columnmapping
        FROM import.imports i JOIN import.filesources f USING (importid, companyid)
        JOIN import.payrollworkimports w USING (importid, companyid) WHERE i.importid = %s""", iid)[0]


async def test_finalization_purges_unfinished_cancelled_and_completed_detail_and_keeps_metadata(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    pid = scenario.period_id
    staged = []
    for desired in ("NeedsMapping", "NeedsReview", "Cancelled", "Completed"):
        response = await upload(p3c_client, tenant, pid)
        assert response.status_code == 200, response.text
        iid = response.json()["import_id"]
        async with p3c_engine.begin() as db:
            if desired in ("NeedsReview", "Completed"):
                await db.execute(text("UPDATE import.imports SET status = 'NeedsReview', revision = revision + 1 WHERE importid = :iid"), {"iid": iid})
            # Populate A2-ready schema directly to exercise the retention
            # contract. No mapping or apply behavior is exposed by A1.
            record = (await db.execute(text("""INSERT INTO import.importsourcerecords
                (importid, companyid, locator, payload) VALUES (:iid, :cid, '{"row": 2}', '{"value":"1"}')
                RETURNING sourcerecordid"""), {"iid": iid, "cid": tenant.company_id})).scalar_one()
            await db.execute(text("""INSERT INTO import.payrollworkrecords (sourcerecordid, importid, companyid)
                VALUES (:rid, :iid, :cid)"""), {"rid": record, "iid": iid, "cid": tenant.company_id})
            await db.execute(text("""INSERT INTO import.importissues (importid, companyid, sourcerecordid, severity, code, message)
                VALUES (:iid, :cid, :rid, 'Warning', 'TEST_WARNING', 'Staged detail')"""), {"iid": iid, "cid": tenant.company_id, "rid": record})
            await db.execute(text("""UPDATE import.payrollworkimports SET columnmapping = '{"1":"DriverNumber"}',
                reviewsummary = CASE WHEN :completed THEN '{"applied": 1}'::jsonb ELSE NULL END WHERE importid = :iid"""),
                             {"iid": iid, "completed": desired == "Completed"})
            if desired == "Completed":
                await db.execute(text("""UPDATE import.imports SET status = 'Completed', revision = revision + 1,
                    completedbyuserid = :uid, completedatutc = NOW() WHERE importid = :iid"""), {"iid": iid, "uid": tenant.owner})
        if desired == "Cancelled":
            response = await p3c_client.post(f"/payroll/imports/{iid}/cancel", json={"expected_revision": 1}, headers=tenant.admin)
            assert response.status_code == 200, response.text
        staged.append((iid, desired, details(tenant, iid)[1]))
    await submit_and_approve(p3c_client, tenant, scenario.period)
    result = await finalize(p3c_client, tenant, scenario.period)
    assert Decimal(result.json()["final_gross"]) == Decimal("42.00")
    for iid, desired, revision in staged:
        state = details(tenant, iid)
        assert state[:3] == (desired, revision + 1, True)
        assert state[3] is not None and state[4:6] == (None, None)
        assert state[6] == ({"applied": 1} if desired == "Completed" else None)
        assert state[7] is None
        for table in ("importsourcerecords", "importissues", "payrollworkrecords"):
            assert rows(tenant, f"SELECT count(*) FROM import.{table} WHERE importid = %s", iid) == [(0,)]
        read = await p3c_client.get(f"/payroll/imports/{iid}", headers=tenant.admin)
        assert read.status_code == 200, read.text
        assert read.json()["file"]["content_purged_at"] is not None
    # Repeated cleanup is a no-op, including revisions.
    async with p3c_engine.begin() as db:
        await purge_payroll_work_import_details(db, company_id=tenant.company_id, period_id=pid, actor_id=tenant.owner)
    assert [details(tenant, iid)[1] for iid, _, _ in staged] == [rev + 1 for _, _, rev in staged]


@pytest.mark.parametrize("failure_point", ["purge", "audit"])
async def test_purge_and_finalization_failures_roll_back_the_entire_lifecycle(
    p3c_client, p3c_engine, tenant, monkeypatch, failure_point,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    created = await upload(p3c_client, tenant, scenario.period_id)
    assert created.status_code == 200, created.text
    iid = created.json()["import_id"]
    await submit_and_approve(p3c_client, tenant, scenario.period)
    before = period_evidence_counts(tenant, scenario.period)
    original = details(tenant, iid)

    async def fail(*args, **kwargs):
        if failure_point == "purge":
            await purge_payroll_work_import_details(*args, **kwargs)
        raise RuntimeError("injected finalization failure after detail cleanup")

    monkeypatch.setattr(finalization, "purge_payroll_work_import_details" if failure_point == "purge" else "_write_finalization_audit", fail)
    with pytest.raises(RuntimeError, match="injected finalization"):
        await finalize(p3c_client, tenant, scenario.period)
    assert details(tenant, iid) == original
    assert period_status(tenant, scenario.period) == "Approved"
    assert period_evidence_counts(tenant, scenario.period) == before


@pytest.mark.parametrize("operation", ["upload", "reinterpret"])
async def test_finalization_during_parser_work_cannot_recreate_details(
    p3c_client, p3c_engine, tenant, monkeypatch, operation,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    created = await upload(p3c_client, tenant, scenario.period_id)
    assert created.status_code == 200, created.text
    iid = created.json()["import_id"]
    parser_entered, parser_release = threading.Event(), threading.Event()
    inspect = service.inspect_file

    def held_parser(*args, **kwargs):
        parser_entered.set()
        if not parser_release.wait(15):
            raise RuntimeError("test did not release parser")
        return inspect(*args, **kwargs)

    monkeypatch.setattr(service, "inspect_file", held_parser)
    if operation == "upload":
        pending = asyncio.create_task(upload(p3c_client, tenant, scenario.period_id))
    else:
        pending = asyncio.create_task(p3c_client.put(f"/payroll/imports/{iid}/mapping",
            json={"expected_revision": 1, "date_order": "DMY"}, headers=tenant.admin))
    try:
        assert await asyncio.to_thread(parser_entered.wait, 5)
        # These actual workflow requests would wait on a period lock or idle
        # request transaction if parsing held one. They must finish first.
        await asyncio.wait_for(submit_and_approve(p3c_client, tenant, scenario.period), 10)
        await asyncio.wait_for(finalize(p3c_client, tenant, scenario.period), 10)
    finally:
        parser_release.set()
    response = await asyncio.wait_for(pending, 10)
    assert response.status_code == 409, response.text
    assert details(tenant, iid)[2] is True
    assert rows(tenant, "SELECT count(*) FROM import.payrollworkimports WHERE payrollperiodid = %s", scenario.period_id) == [(1,)]
