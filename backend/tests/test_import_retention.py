"""Payroll finalization atomically purges all Import states and serializes staging."""
import asyncio
import threading
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

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
from tests.payroll_runtime_fixtures import build_payroll_tenant, seed_status_key

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
        f.inspectionmetadata, f.sheetname, w.reviewsummary, w.columnmapping,
        f.headerrownumber, f.dateorder, f.encoding, f.delimiter,
        w.mappingfingerprint, w.contentfingerprint, w.planfingerprint, w.previewcounts
        FROM import.imports i JOIN import.filesources f USING (importid, companyid)
        JOIN import.payrollworkimports w USING (importid, companyid) WHERE i.importid = %s""", iid)[0]


async def test_finalization_purges_unfinished_cancelled_and_completed_detail_and_keeps_metadata(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    pid = scenario.period_id
    source_evidence_before = rows(tenant, "SELECT payrollperiodauditevidenceeventid FROM payroll.payrollperiodauditevidenceevents WHERE payrollperiodid = %s ORDER BY 1", pid)
    staged = []
    for desired in ("NeedsMapping", "NeedsReview", "Cancelled", "Completed"):
        response = await upload(p3c_client, tenant, pid)
        assert response.status_code == 200, response.text
        iid = response.json()["import_id"]
        interpreted = await p3c_client.put(f"/payroll/imports/{iid}/mapping",
            json={"expected_revision": 1, "header_row_number": 2, "date_order": "YMD"}, headers=tenant.admin)
        assert interpreted.status_code == 200, interpreted.text
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
            await db.execute(text("""UPDATE import.payrollworkimports
                SET columnmapping = CASE WHEN :mapped THEN '{"1":"DriverNumber"}'::jsonb ELSE NULL END,
                mappingfingerprint = CASE WHEN :mapped THEN :mapping ELSE NULL END,
                contentfingerprint = CASE WHEN :mapped THEN :content ELSE NULL END,
                planfingerprint = :plan, previewcounts = '{"rows": 1}',
                reviewsummary = CASE WHEN :completed THEN '{"applied": 1}'::jsonb ELSE NULL END WHERE importid = :iid"""),
                             {"iid": iid, "completed": desired == "Completed", "mapped": desired != "NeedsMapping",
                              "mapping": "1" * 64, "content": "2" * 64, "plan": "3" * 64})
            if desired == "Completed":
                await db.execute(text("""UPDATE import.imports SET status = 'Completed', revision = revision + 1,
                    completedbyuserid = :uid, completedatutc = NOW() WHERE importid = :iid"""), {"iid": iid, "uid": tenant.owner})
        if desired == "Cancelled":
            response = await p3c_client.post(f"/payroll/imports/{iid}/cancel", json={"expected_revision": 2}, headers=tenant.admin)
            assert response.status_code == 200, response.text
        staged.append((iid, desired, details(tenant, iid)))
    await submit_and_approve(p3c_client, tenant, scenario.period)
    result = await finalize(p3c_client, tenant, scenario.period)
    assert Decimal(result.json()["final_gross"]) == Decimal("42.00")
    for iid, desired, before in staged:
        state = details(tenant, iid)
        assert state[:3] == (desired, before[1] + 1, True)
        assert state[3] is not None and state[4] is None
        assert state[5:14] == before[5:14]  # durable interpretation, mapping, semantic identity and summary
        assert state[8:12] == (2, "YMD", "utf-8", ",")
        assert state[6] == ({"applied": 1} if desired == "Completed" else None)
        assert state[7] == ({"1": "DriverNumber"} if desired != "NeedsMapping" else None)
        assert state[14:] == (None, None)  # only current-plan identity/counts are transient
        for table in ("importsourcerecords", "importissues", "payrollworkrecords"):
            assert rows(tenant, f"SELECT count(*) FROM import.{table} WHERE importid = %s", iid) == [(0,)]
        read = await p3c_client.get(f"/payroll/imports/{iid}", headers=tenant.admin)
        assert read.status_code == 200, read.text
        assert read.json()["file"]["content_purged_at"] is not None
        assert read.json()["interpretation"] == {"sheet_name": "CSV", "header_row_number": 2,
            "date_order": "YMD", "encoding": "utf-8", "delimiter": ",", "inspection": None}
    # Repeated cleanup is a no-op, including revisions.
    async with p3c_engine.begin() as db:
        await purge_payroll_work_import_details(db, company_id=tenant.company_id, period_id=pid, actor_id=tenant.owner)
    assert [details(tenant, iid)[1] for iid, _, _ in staged] == [before[1] + 1 for _, _, before in staged]
    assert rows(tenant, "SELECT payrollperiodauditevidenceeventid FROM payroll.payrollperiodauditevidenceevents WHERE payrollperiodid = %s ORDER BY 1", pid) == source_evidence_before
    # Even the retention marker cannot rewrite history or restore detail.
    completed = next(iid for iid, desired, _ in staged if desired == "Completed")
    async with p3c_engine.begin() as db:
        await db.execute(text("SELECT set_config('flussra.import_purge_id', :iid, TRUE)"), {"iid": str(completed)})
        for table, mutation in (("filesources", "sheetname = 'Changed'"), ("filesources", "dateorder = 'DMY'"),
                                ("filesources", "encoding = 'cp1252'"), ("filesources", "contentpurgedatutc = NOW()"),
                                ("payrollworkimports", "columnmapping = '{}'"),
                                ("payrollworkimports", "mappingfingerprint = 'changed'"),
                                ("payrollworkimports", "contentfingerprint = 'changed'"),
                                ("payrollworkimports", "reviewsummary = '{}'")):
            with pytest.raises(DBAPIError, match="IMPORT_PURGE_SCOPE"):
                async with db.begin_nested():
                    await db.execute(text(f"UPDATE import.{table} SET {mutation} WHERE importid = :iid"), {"iid": completed})
        with pytest.raises(DBAPIError, match="IMPORT_PURGE_SCOPE"):
            async with db.begin_nested():
                await db.execute(text("""INSERT INTO import.importsourcerecords (importid, companyid, locator, payload)
                    VALUES (:iid, :cid, '{}', '{}')"""), {"iid": completed, "cid": tenant.company_id})


async def test_cell_decisions_coexist_with_typed_scoped_bases_and_row_exclusion(
    p3c_client, p3c_engine, tenant, p3b_dsn,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    status = seed_status_key(tenant, "Import decision basis")
    foreign = build_payroll_tenant(p3b_dsn)
    foreign_status = seed_status_key(foreign, "Foreign basis")
    created = await upload(p3c_client, tenant, scenario.period_id)
    assert created.status_code == 200, created.text
    iid = created.json()["import_id"]
    async with p3c_engine.begin() as db:
        params = {"iid": iid, "cid": tenant.company_id, "bid": tenant.branch_a, "pid": scenario.period_id, "status": status}
        rid = (await db.execute(text("""INSERT INTO import.importsourcerecords
            (importid, companyid, locator, payload) VALUES (:iid, :cid, '{}', '{}') RETURNING sourcerecordid"""), params)).scalar_one()
        params["rid"] = rid
        await db.execute(text("INSERT INTO import.payrollworkrecords (sourcerecordid, importid, companyid) VALUES (:rid, :iid, :cid)"), params)
        cell = (await db.execute(text("""INSERT INTO import.payrollworkcells
            (sourcerecordid, importid, companyid, branchid, payrollperiodid, sourcecolumnindex,
             hasstatus, isdayoff, decision, decisionbasisquantity, heldstatuskeyid,
             statusdecision, statusdecisionbasiskeyid, dayoffdecision)
            VALUES (:rid, :iid, :cid, :bid, :pid, 3, TRUE, TRUE, 'UseImported', 7.25,
                    :status, 'RemoveStatus', :status, 'AcceptException') RETURNING payrollworkcellid"""), params)).scalar_one()
        params["cell"] = cell
        expected = ("UseImported", Decimal("7.2500"), status, "RemoveStatus", status, "AcceptException")
        select = text("""SELECT decision, decisionbasisquantity, heldstatuskeyid, statusdecision,
            statusdecisionbasiskeyid, dayoffdecision FROM import.payrollworkcells WHERE payrollworkcellid = :cell""")
        assert tuple((await db.execute(select, params)).one()) == expected
        await db.execute(text("UPDATE import.payrollworkrecords SET isexcluded = TRUE WHERE sourcerecordid = :rid"), params)
        assert tuple((await db.execute(select, params)).one()) == expected
        for column, invalid in (("decision", "RemoveStatus"), ("statusdecision", "UseImported"),
                                ("dayoffdecision", "KeepExisting"), ("statusdecisionbasiskeyid", foreign_status),
                                ("heldstatuskeyid", foreign_status), ("statusdecisionbasiskeyid", None)):
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(text(f"UPDATE import.payrollworkcells SET {column} = :value WHERE payrollworkcellid = :cell"),
                                     {**params, "value": invalid})
        await db.execute(text("""UPDATE import.payrollworkcells SET decision = 'KeepExisting',
            statusdecision = 'Exclude', dayoffdecision = 'Exclude' WHERE payrollworkcellid = :cell"""), params)
        assert tuple((await db.execute(select, params)).one()) == ("KeepExisting", Decimal("7.2500"), status, "Exclude", status, "Exclude")


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
