from __future__ import annotations

import asyncio
import threading
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.imports import batch_service
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
from tests.compensation_api_fixtures import (  # noqa: F401 - register isolated API fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import p3b_database  # noqa: F401
from tests.payroll_runtime_fixtures import build_payroll_tenant, map_import_columns

pytestmark = pytest.mark.asyncio


@pytest.fixture
def tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _valid_file(p3c_engine, tenant, period_id):
    from app.imports.mapping import targets
    from app.payroll.period_definitions import list_period_definitions

    async with p3c_engine.connect() as db:
        driver = (await db.execute(text("""
            SELECT d.drivernumber, x.workdate
            FROM payroll.payrollperioddrivereligibility p
            JOIN core.driverassignments a ON a.driverassignmentid=p.driverassignmentid
                AND a.companyid=p.companyid AND a.branchid=p.branchid
            JOIN core.drivers d ON d.driverid=a.driverid AND d.companyid=a.companyid
            CROSS JOIN LATERAL unnest(p.eligibleworkdates) AS x(workdate)
            WHERE p.companyid=:cid AND p.payrollperiodid=:pid AND p.iseligibleforperiod
            ORDER BY d.driverid,x.workdate LIMIT 1
        """), {"cid": tenant.company_id, "pid": period_id})).one()
        definitions = await list_period_definitions(period_id, tenant.company_id, db, active_only=True)
    definition = next(item for item in targets(definitions) if item["target"].startswith("Definition:"))
    definition_id = int(definition["target"].split(":", 1)[1])
    data = (f"DriverNumber,WorkDate,{definition['code']}\n"
            f"{driver.drivernumber},{driver.workdate.isoformat()},1\n").encode()
    return data, definition_id


async def upload(client, tenant, pid, data, *, key=None):
    return await client.post(f"/payroll/periods/{pid}/imports",
        files={"file": ("work.csv", data, "text/csv")},
        headers={**tenant.admin, "Idempotency-Key": str(key or uuid4())})


async def _map_and_preview(client, tenant, created, definition_id):
    iid = created["import_id"]
    mapped = await map_import_columns(client, tenant, created, {
        "1": "DriverNumber", "2": "WorkDate", "3": f"Definition:{definition_id}"})
    assert mapped.status_code == 200, mapped.text
    preview = await client.post(f"/payroll/imports/{iid}/preview",
        json={"expected_revision": mapped.json()["revision"]}, headers=tenant.admin)
    assert preview.status_code == 200, preview.text
    return preview.json()


def details(tenant, iid):
    return rows(tenant, """SELECT i.status, i.revision, f.filecontent IS NULL, f.contentpurgedatutc,
        f.inspectionmetadata, u.inspectionmetadata, l.inspectionmetadata, b.sourcemanifest,
        w.defaultworkdate, w.detailpurgedatutc, w.mappingfingerprint, w.contentfingerprint,
        w.planfingerprint, w.previewcounts, w.reviewsummary
        FROM import.imports i
        JOIN import.filesources f ON f.importid=i.importid AND f.companyid=i.companyid
        JOIN import.filesourceunits u ON u.filesourceid=f.filesourceid AND u.importid=f.importid AND u.companyid=f.companyid
        JOIN import.sourcelayouts l ON l.sourcelayoutid=u.sourcelayoutid AND l.importid=u.importid AND l.companyid=u.companyid
        JOIN import.filesourcebatches b ON b.importid=i.importid AND b.companyid=i.companyid
        JOIN import.payrollworkimports w ON w.importid=i.importid AND w.companyid=i.companyid
        WHERE i.importid=%s""", iid)[0]


async def _detail_counts(tenant, iid):
    tables = ("importsourcerecords", "filesourcerecords", "importissues", "payrollworkrecords",
              "payrollworkpreviewgroups", "payrollworkdrivermatches", "payrollworkrecordidentifiers")
    return tuple(rows(tenant, f"SELECT count(*) FROM import.{table} WHERE importid=%s", iid)[0][0]
                 for table in tables)


async def test_finalization_purges_transient_preview_details_and_retains_batch_history(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    pid = scenario.period_id
    data, definition_id = await _valid_file(p3c_engine, tenant, pid)
    evidence_before = rows(tenant,
        "SELECT payrollperiodauditevidenceeventid FROM payroll.payrollperiodauditevidenceevents WHERE payrollperiodid=%s ORDER BY 1", pid)
    staged = []
    for desired in ("NeedsMapping", "NeedsReview", "Cancelled", "Completed"):
        response = await upload(p3c_client, tenant, pid, data)
        assert response.status_code == 200, response.text
        iid = response.json()["import_id"]
        if desired in ("NeedsReview", "Completed"):
            preview = await _map_and_preview(p3c_client, tenant, response.json(), definition_id)
            assert preview["status"] == "NeedsReview"
        elif desired == "Cancelled":
            cancelled = await p3c_client.post(f"/payroll/imports/{iid}/cancel",
                json={"expected_revision": 1}, headers=tenant.admin)
            assert cancelled.status_code == 200, cancelled.text
        if desired == "Completed":
            async with p3c_engine.begin() as db:
                await db.execute(text("""UPDATE import.imports SET status='Completed', revision=revision+1,
                    completedbyuserid=:uid, completedatutc=NOW(), updatedbyuserid=:uid, updatedatutc=NOW()
                    WHERE importid=:iid AND companyid=:cid"""), {"uid": tenant.owner, "iid": iid, "cid": tenant.company_id})
        staged.append((iid, desired, details(tenant, iid)))

    headers, values = (line.split(",") for line in data.decode().strip().splitlines())
    code = headers[2]
    duplicate_data = (",".join([*headers[:2], code, code, code]) + "\n"
                      + ",".join([*values[:2], values[2], values[2], "99"]) + "\n").encode()
    duplicate_upload = await upload(p3c_client, tenant, pid, duplicate_data)
    assert duplicate_upload.status_code == 200, duplicate_upload.text
    duplicate_created = duplicate_upload.json()
    duplicate_layout = duplicate_created["layouts"][0]
    signature = duplicate_layout["inspection"]["duplicate_header_signatures"][0]["signature"]
    mapping = await p3c_client.put(
        f"/payroll/imports/{duplicate_created['import_id']}/mapping",
        json={"expected_revision": duplicate_created["revision"],
              "shared_columns": {"DriverNumber": {"header": headers[0]},
                                 "WorkDate": {"header": headers[1]}},
              "ignored_columns": [{"header": code, "occurrences": [
                  {"duplicate_signature": signature, "occurrence_ordinal": 3}]}],
              "targets": [{"target": f"Definition:{definition_id}", "transform": "Sum",
                           "inputs": [
                               {"header": code, "occurrences": [
                                   {"duplicate_signature": signature, "occurrence_ordinal": 1}]},
                               {"header": code, "occurrences": [
                                   {"duplicate_signature": signature, "occurrence_ordinal": 2}]},
                           ], "expected_head_revision": 0}]},
        headers=tenant.admin,
    )
    assert mapping.status_code == 200, mapping.text
    duplicate_preview = await p3c_client.post(
        f"/payroll/imports/{duplicate_created['import_id']}/preview",
        json={"expected_revision": mapping.json()["revision"]}, headers=tenant.admin)
    assert duplicate_preview.status_code == 200, duplicate_preview.text
    async with p3c_engine.begin() as db:
        await db.execute(text("""UPDATE import.Imports SET Status='Completed', Revision=Revision+1,
            CompletedByUserID=:uid, CompletedAtUtc=NOW(), UpdatedByUserID=:uid, UpdatedAtUtc=NOW()
            WHERE ImportID=:iid AND CompanyID=:cid"""),
            {"uid": tenant.owner, "iid": duplicate_created["import_id"], "cid": tenant.company_id})
    duplicate_iid = duplicate_created["import_id"]
    staged.append((duplicate_iid, "Completed", details(tenant, duplicate_iid)))

    await submit_and_approve(p3c_client, tenant, scenario.period)
    result = await finalize(p3c_client, tenant, scenario.period)
    assert Decimal(result.json()["final_gross"]) == Decimal("42.00")
    for iid, desired, before in staged:
        state = details(tenant, iid)
        assert state[0] == desired and state[1] == before[1] + 1
        assert state[2] is True and state[3] is not None
        assert state[4:7] == (None, None, None)
        assert state[7:9] == before[7:9]
        assert state[10:12] == before[10:12]
        assert state[12:14] == (None, None)
        assert state[14] == before[14]  # Aggregate review counts remain durable.
        assert state[9] is not None
        assert await _detail_counts(tenant, iid) == (0, 0, 0, 0, 0, 0, 0)
        read = await p3c_client.get(f"/payroll/imports/{iid}", headers=tenant.admin)
        assert read.status_code == 200, read.text
        assert read.json()["files"][0]["content_purged_at"] is not None
        assert read.json()["files"][0]["canonical_format_version"] == 1
        assert read.json()["files"][0]["canonical_data_bytes"] > 0
        assert read.json()["source_editable"] is False
        assert read.json()["preview"]["summary"] == before[14]

    persisted_signature = rows(tenant, """
        SELECT SourceSignature FROM import.PayrollWorkRuleVersions
        WHERE CompanyID=%s AND OriginImportID=%s
    """, tenant.company_id, duplicate_iid)
    assert len(persisted_signature) == 1
    assert persisted_signature[0][0]["ignored_columns"] == [{
        "header": code.casefold(),
        "occurrences": [{"duplicate_signature": signature, "occurrence_ordinal": 3}],
    }]

    async with p3c_engine.begin() as db:
        await purge_payroll_work_import_details(db, company_id=tenant.company_id, period_id=pid, actor_id=tenant.owner)
    assert [details(tenant, iid)[1] for iid, _, _ in staged] == [before[1] + 1 for _, _, before in staged]
    assert rows(tenant, """
        SELECT count(*) FROM import.FileCanonicalData
        WHERE CompanyID=%s AND Payload IS NULL AND PurgedAtUtc IS NOT NULL
          AND ByteCount > 0 AND HashSha256 ~ '^[0-9a-f]{64}$'
    """, tenant.company_id)[0][0] == len(staged)
    assert rows(tenant,
        "SELECT payrollperiodauditevidenceeventid FROM payroll.payrollperiodauditevidenceevents WHERE payrollperiodid=%s ORDER BY 1", pid) == evidence_before
    with pytest.raises(DBAPIError, match="IMPORT_PURGE_SCOPE|IMPORT_IMMUTABLE"):
        async with p3c_engine.begin() as db:
            await db.execute(text("SELECT set_config('flussra.import_purge_id', :iid, TRUE)"), {"iid": str(staged[-1][0])})
            await db.execute(text("UPDATE import.payrollworkimports SET detailpurgedatutc=NOW() WHERE importid=:iid"),
                             {"iid": staged[-1][0]})


@pytest.mark.parametrize("failure_point", ["purge", "audit"])
async def test_purge_and_finalization_failures_roll_back_the_entire_lifecycle(
    p3c_client, p3c_engine, tenant, monkeypatch, failure_point,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    data, _definition_id = await _valid_file(p3c_engine, tenant, scenario.period_id)
    created = await upload(p3c_client, tenant, scenario.period_id, data)
    assert created.status_code == 200, created.text
    before = details(tenant, created.json()["import_id"])
    await submit_and_approve(p3c_client, tenant, scenario.period)
    evidence_before = period_evidence_counts(tenant, scenario.period)

    async def fail(*args, **kwargs):
        if failure_point == "purge":
            await purge_payroll_work_import_details(*args, **kwargs)
        raise RuntimeError("injected finalization failure after detail cleanup")

    monkeypatch.setattr(finalization, "purge_payroll_work_import_details" if failure_point == "purge" else "_write_finalization_audit", fail)
    with pytest.raises(RuntimeError, match="injected finalization"):
        await finalize(p3c_client, tenant, scenario.period)
    assert details(tenant, created.json()["import_id"]) == before
    assert period_status(tenant, scenario.period) == "Approved"
    assert period_evidence_counts(tenant, scenario.period) == evidence_before


async def test_finalization_during_upload_parsing_cannot_recreate_details(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    data, _definition_id = await _valid_file(p3c_engine, tenant, scenario.period_id)
    created = await upload(p3c_client, tenant, scenario.period_id, data)
    assert created.status_code == 200, created.text
    iid = created.json()["import_id"]
    parser_entered, parser_release = threading.Event(), threading.Event()
    inspect = batch_service._inspect_batch

    def held(*args, **kwargs):
        parser_entered.set()
        if not parser_release.wait(15):
            raise RuntimeError("test did not release parser")
        return inspect(*args, **kwargs)

    monkeypatch.setattr(batch_service, "_inspect_batch", held)
    pending = asyncio.create_task(upload(p3c_client, tenant, scenario.period_id, data))
    try:
        assert await asyncio.to_thread(parser_entered.wait, 5)
        await asyncio.wait_for(submit_and_approve(p3c_client, tenant, scenario.period), 10)
        await asyncio.wait_for(finalize(p3c_client, tenant, scenario.period), 10)
    finally:
        parser_release.set()
    response = await asyncio.wait_for(pending, 10)
    assert response.status_code == 409, response.text
    assert details(tenant, iid)[2] is True
    assert rows(tenant, "SELECT count(*) FROM import.payrollworkimports WHERE payrollperiodid=%s", scenario.period_id) == [(1,)]


async def test_normalized_preview_has_no_decision_surface(p3c_client, p3c_engine, tenant):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    data, definition_id = await _valid_file(p3c_engine, tenant, scenario.period_id)
    created = await upload(p3c_client, tenant, scenario.period_id, data)
    assert created.status_code == 200, created.text
    preview = await _map_and_preview(p3c_client, tenant, created.json(), definition_id)
    assert preview["status"] == "NeedsReview"
    async with p3c_engine.connect() as db:
        schema = (await db.execute(text("""
            SELECT to_regclass('import.payrollworkcells') IS NULL,
                to_regclass('import.payrollworkrecords') IS NOT NULL,
                to_regclass('import.payrollworkpreviewgroups') IS NOT NULL
        """))).one()
        columns = (await db.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='import' AND table_name='payrollworkrecords'
        """))).scalars().all()
    assert tuple(schema) == (True, True, True)
    assert not {"decision", "decisionbasisquantity", "appliedevidenceeventid", "confirmed"}.intersection(columns)
