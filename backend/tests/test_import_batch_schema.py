"""The preproduction batch cutover is atomic and retires only disposable staging."""
import json
from pathlib import Path

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.compensation_api_fixtures import (  # noqa: F401 - register isolated API fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import p3b_database  # noqa: F401
from tests.payroll_runtime_fixtures import build_payroll_tenant, open_period_with_grid
from tests.schema_helpers import _alembic, _disposable_database
from tests.test_import_batch_preview_api import (
    _definition,
    _eligible_driver_date,
    _map_and_preview,
    _upload,
)
from tests.test_import_foundation_schema import envelope, identities

SQL = Path(__file__).resolve().parents[2] / "migrations/sql/0095_import_batch_sources.sql"


@pytest.fixture(scope="module")
def single_file_foundation(pg_instance):
    with _disposable_database(pg_instance, "import_batch_cutover") as (env, dsn):
        upgraded = _alembic(env, "upgrade", "0094")
        assert upgraded.returncode == 0, upgraded.stderr
        yield dsn


def _counts(cur):
    cur.execute("""SELECT (SELECT count(*) FROM payroll.payrolldraftlines),
        (SELECT count(*) FROM payroll.payrollperiodauditevidenceevents),
        (SELECT count(*) FROM core.drivers), (SELECT count(*) FROM sec.users)""")
    return cur.fetchone()


def test_cutover_discards_incomplete_file_staging_without_canonical_changes(single_file_foundation):
    with psycopg2.connect(**single_file_foundation) as conn, conn.cursor() as cur:
        cid, uid = identities(cur)
        iid = envelope(cur, cid, uid, kind="PayrollWork", source="File")
        cur.execute("""INSERT INTO import.filesources(importid,companyid,originalfilename,
            filetype,filesizebytes,filehashsha256,filecontent)
            VALUES (%s,%s,'old.csv','csv',1,%s,%s)""", (iid, cid, "0" * 64, b"a"))
        before = _counts(cur)
        cur.execute(SQL.read_text())
        assert _counts(cur) == before
        cur.execute("SELECT count(*) FROM import.imports")
        assert cur.fetchone() == (0,)
        cur.execute("""SELECT to_regclass('import.payrollworkcells'),
            to_regclass('import.filesourceunits') IS NOT NULL,
            to_regclass('import.sourcelayouts') IS NOT NULL,
            to_regclass('import.payrollworkpreviewgroups') IS NOT NULL""")
        assert cur.fetchone() == (None, True, True, True)
        conn.rollback()


def test_cutover_refuses_generic_history_and_rolls_back_every_schema_change(single_file_foundation):
    with psycopg2.connect(**single_file_foundation) as conn, conn.cursor() as cur:
        cid, uid = identities(cur)
        iid = envelope(cur, cid, uid)
        cur.execute("SAVEPOINT cutover")
        with pytest.raises(psycopg2.Error, match="IMPORT_CUTOVER_PROTECTED"):
            cur.execute(SQL.read_text())
        cur.execute("ROLLBACK TO SAVEPOINT cutover")
        cur.execute("SELECT importid FROM import.imports WHERE importid=%s", (iid,))
        assert cur.fetchone() == (iid,)
        cur.execute("SELECT to_regclass('import.payrollworkcells') IS NOT NULL, to_regclass('import.filesourceunits')")
        assert cur.fetchone() == (True, None)
        conn.rollback()


def test_cutover_unexpected_dependency_is_fail_closed(single_file_foundation):
    with psycopg2.connect(**single_file_foundation) as conn, conn.cursor() as cur:
        cur.execute("CREATE VIEW app.unexpected_cell_reader AS SELECT * FROM import.payrollworkcells")
        cur.execute("SAVEPOINT cutover")
        with pytest.raises(psycopg2.errors.DependentObjectsStillExist):
            cur.execute(SQL.read_text())
        cur.execute("ROLLBACK TO SAVEPOINT cutover")
        cur.execute("SELECT to_regclass('import.payrollworkcells') IS NOT NULL, to_regclass('import.filesourceunits')")
        assert cur.fetchone() == (True, None)
        cur.execute("SELECT count(*) FROM pg_trigger WHERE tgrelid='import.filesources'::regclass AND tgenabled='O' AND NOT tgisinternal")
        assert cur.fetchone() == (2,)
        conn.rollback()


@pytest.mark.asyncio
async def test_disposable_cutover_preserves_populated_canonical_payroll_and_immutable_evidence(single_file_foundation, monkeypatch):
    from app.config import get_settings
    from scripts.import_benchmark_support import application, seed_period

    # Reuse the disposable production-schema workload fixture; restore process
    # configuration independently of the database/connection lifetime.
    with monkeypatch.context() as configuration:
        configuration.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/restore_configuration")
        try:
            async with application(single_file_foundation) as (client, engine, _app):
                fixture = await seed_period(client, engine, single_file_foundation, drivers=2, definitions=1, days=7)
                tenant, period = fixture["tenant"], fixture["period"]
                added = await client.post(f"/payroll/periods/{period['payroll_period_id']}/lines", headers=tenant.admin,
                    json={"driver_assignment_id": fixture["drivers"][0]["driverassignmentid"],
                          "work_date": fixture["days"][0].isoformat(),
                          "payroll_period_definition_id": fixture["definitions"][0].payroll_period_definition_id,
                          "quantity": "3", "notes": "retained canonical source"})
                assert added.status_code == 201, added.text
                with psycopg2.connect(**single_file_foundation) as conn, conn.cursor() as cur:
                    iid = envelope(cur, tenant.company_id, tenant.owner, kind="PayrollWork", source="File")
                    cur.execute("""INSERT INTO import.filesources(importid,companyid,originalfilename,
                        filetype,filesizebytes,filehashsha256,filecontent)
                        VALUES (%s,%s,'disposable.csv','csv',1,%s,%s)""", (iid, tenant.company_id, "0" * 64, b"a"))
                    before = _counts(cur)
                    assert before[0] >= 1 and before[1] >= 1
                    cur.execute(SQL.read_text())
                    assert _counts(cur) == before
                    cur.execute("SELECT quantity,notes FROM payroll.payrolldraftlines WHERE draftlineid=%s", (added.json()["draft_line_id"],))
                    assert cur.fetchone() == (3, "retained canonical source")
                    cur.execute("SELECT count(*) FROM import.imports WHERE importid=%s", (iid,))
                    assert cur.fetchone() == (0,)
                    conn.rollback()
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_compact_payload_references_keep_period_driver_and_grid_scope(p3c_client, p3c_engine, p3b_dsn):
    tenant = build_payroll_tenant(p3b_dsn)
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    driver = await _eligible_driver_date(p3c_engine, tenant, period)
    definition, _ = await _definition(p3c_engine, tenant, period)
    uploaded = await _upload(p3c_client, tenant, period, [("source.csv",
        f"Driver,Date,Quantity\n{driver['drivernumber']},{driver['workdate']},1\n".encode())])
    assert uploaded.status_code == 200, uploaded.text
    iid = uploaded.json()["import_id"]
    await _map_and_preview(p3c_client, tenant, uploaded.json(), definition)
    async with p3c_engine.begin() as db:
        original = (await db.execute(text("SELECT * FROM import.payrollworkrecords WHERE importid=:iid"), {"iid": iid})).mappings().one()
        for values, driver_id, message in [
            ({"-1": {"source_column": 3}}, original["driverid"], "IMPORT_PAYLOAD_SHAPE"),
            ({str(definition): {"source_column": True}}, original["driverid"], "IMPORT_PAYLOAD_SHAPE"),
            ({"2147483647": {"source_column": 3}}, original["driverid"], "IMPORT_PAYLOAD_SCOPE"),
            ({str(definition): {"source_column": 3}}, tenant.driver_a2_id if original["driverid"] != tenant.driver_a2_id else tenant.driver_a_id,
             "assignment and permanent Driver disagree"),
        ]:
            with pytest.raises(DBAPIError, match=message):
                async with db.begin_nested():
                    source = await db.scalar(text("""INSERT INTO import.importsourcerecords(importid,companyid,locator,payload)
                        VALUES (:iid,:cid,'{}','{}') RETURNING sourcerecordid"""), {"iid": iid, "cid": tenant.company_id})
                    await db.execute(text("""INSERT INTO import.payrollworkrecords(sourcerecordid,importid,companyid,branchid,
                        payrollperiodid,previewgroupid,driverid,driverassignmentid,workdate,rowfingerprint,valuesjson,issuesjson)
                        SELECT :source,importid,companyid,branchid,payrollperiodid,previewgroupid,:driver,
                            driverassignmentid,workdate,rowfingerprint,CAST(:values AS jsonb),'[]'
                        FROM import.payrollworkrecords WHERE sourcerecordid=:original"""),
                        {"source": source, "original": original["sourcerecordid"], "driver": driver_id, "values": json.dumps(values)})
        with pytest.raises(DBAPIError, match="IMPORT_MAPPING_SCOPE"):
            async with db.begin_nested():
                await db.execute(text("UPDATE import.sourcelayouts SET columnmapping='{" + '"1":"Definition:2147483647"' + "}' WHERE importid=:iid"), {"iid": iid})
