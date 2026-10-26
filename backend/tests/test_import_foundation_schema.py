"""Import source/target ownership, immutable metadata and fail-closed replacement."""
import json
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest

from app.db.schema_guard import _check_import_foundation
from tests.db_state import IMPORT_FILE_DETAIL_WRITABLE_TRIGGER, suspended_test_triggers_sync
from tests.schema_helpers import _alembic, _disposable_database, _schema_database

SQL = Path(__file__).resolve().parents[2] / "migrations/sql/0094_import_foundation.sql"


@pytest.fixture(scope="module")
def foundation(schema_databases):
    with _schema_database(schema_databases, "import_foundation") as (_, dsn):
        yield dsn


@pytest.fixture(scope="module")
def predecessors(pg_instance):
    with _disposable_database(pg_instance, "import_predecessor") as (env, dsn):
        upgraded = _alembic(env, "upgrade", "0093")
        assert upgraded.returncode == 0, upgraded.stderr
        yield dsn


def identities(cur):
    code = uuid4().hex[:12]
    cur.execute("""INSERT INTO core.companies (companycode, companyname, status, issuspended)
        VALUES (%s, 'Import schema', 'Active', FALSE) RETURNING companyid""", (code,))
    cid = cur.fetchone()[0]
    cur.execute("INSERT INTO sec.users (companyid, username, displayname) VALUES (%s, %s, 'Importer') RETURNING userid", (cid, code))
    return cid, cur.fetchone()[0]


def envelope(cur, cid, uid, *, kind="People", source="Integration"):
    cur.execute("""INSERT INTO import.imports (companyid, importkind, sourcekind, correlationid,
        idempotencykey, creationfingerprint, createdbyuserid, updatedbyuserid)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING importid""",
                (cid, kind, source, str(uuid4()), str(uuid4()), "0" * 64, uid, uid))
    return cur.fetchone()[0]


def test_generic_records_need_no_file_payroll_or_driver_authority(foundation):
    with psycopg2.connect(**foundation) as conn, conn.cursor() as cur:
        assert _check_import_foundation(cur) == []
        cid, uid = identities(cur)
        iid = envelope(cur, cid, uid)
        cur.execute("""INSERT INTO import.importsourcerecords (importid, companyid, locator, payload)
            VALUES (%s, %s, '{"external_record":"abc"}', '{"name":"Person"}') RETURNING sourcerecordid""", (iid, cid))
        record = cur.fetchone()[0]
        cur.execute("""INSERT INTO import.importissues (importid, companyid, sourcerecordid, severity, code, message)
            VALUES (%s, %s, %s, 'Warning', 'SOURCE_NOTE', 'Source-neutral issue')""", (iid, cid, record))
        cur.execute("SELECT branchid, revision FROM import.imports WHERE importid = %s", (iid,))
        assert cur.fetchone() == (None, 1)
        conn.rollback()


@pytest.mark.parametrize("mutation", [
    "DELETE FROM import.imports WHERE importid = %s",
    "UPDATE import.imports SET revision = revision + 2 WHERE importid = %s",
    "UPDATE import.imports SET importkind = 'Other', revision = revision + 1 WHERE importid = %s",
])
def test_envelope_creation_identity_is_durable(foundation, mutation):
    conn = psycopg2.connect(**foundation)
    try:
        with conn.cursor() as cur:
            cid, uid = identities(cur)
            iid = envelope(cur, cid, uid)
            with pytest.raises(psycopg2.Error, match="IMPORT_IMMUTABLE"):
                cur.execute(mutation, (iid,))
    finally:
        conn.rollback()
        conn.close()


def test_company_actor_and_source_kind_are_enforced(foundation):
    conn = psycopg2.connect(**foundation)
    try:
        with conn.cursor() as cur:
            cid, uid = identities(cur)
            _, foreign_uid = identities(cur)
            cur.execute("SAVEPOINT wrong_actor")
            with pytest.raises(psycopg2.errors.ForeignKeyViolation):
                envelope(cur, cid, foreign_uid)
            cur.execute("ROLLBACK TO SAVEPOINT wrong_actor")
            iid = envelope(cur, cid, uid)
            with pytest.raises(psycopg2.Error, match="IMPORT_SOURCE_KIND"):
                cur.execute("""INSERT INTO import.filesources (importid, companyid, originalfilename,
                    filetype, filesizebytes, filehashsha256, filecontent)
                    VALUES (%s, %s, 'x.csv', 'csv', 1, %s, '\\x61')""", (iid, cid, "0" * 64))
    finally:
        conn.rollback()
        conn.close()


def test_schema_guard_detects_disabled_detail_protection(foundation):
    with psycopg2.connect(**foundation) as conn, conn.cursor() as cur:
        with suspended_test_triggers_sync(cur, [IMPORT_FILE_DETAIL_WRITABLE_TRIGGER]):
            assert any(
                "filesources.trg_importdetail_writable" in error
                for error in _check_import_foundation(cur)
            )


def test_schema_guard_detects_missing_company_scoped_driver_relationship(foundation):
    with psycopg2.connect(**foundation) as conn, conn.cursor() as cur:
        cur.execute("""SELECT conname FROM pg_constraint WHERE conrelid = 'import.payrollworkdrivermatches'::regclass
            AND confrelid = 'core.drivers'::regclass""")
        constraint = cur.fetchone()[0]
        cur.execute(f'ALTER TABLE import.payrollworkdrivermatches DROP CONSTRAINT "{constraint}"')
        assert any("payrollworkdrivermatches(driverid, companyid)" in error for error in _check_import_foundation(cur))
        conn.rollback()


def test_schema_guard_detects_incomplete_child_lookup_index(foundation):
    with psycopg2.connect(**foundation) as conn, conn.cursor() as cur:
        cur.execute("DROP INDEX import.ix_payrollworkrecordidentifiers_matchscope")
        cur.execute("CREATE INDEX ix_payrollworkrecordidentifiers_matchscope ON import.payrollworkrecordidentifiers(companyid)")
        assert any("ix_payrollworkrecordidentifiers_matchscope" in error for error in _check_import_foundation(cur))
        conn.rollback()


def test_schema_guard_rejects_broad_btree_that_masks_ownership_lookup(foundation):
    with psycopg2.connect(**foundation) as conn, conn.cursor() as cur:
        cur.execute("DROP INDEX import.ix_payrollworkrecords_import")
        cur.execute("CREATE INDEX ix_payrollworkrecords_import ON import.payrollworkrecords(importid,companyid,sourcerecordid)")
        assert any("ix_payrollworkrecords_import" in error for error in _check_import_foundation(cur))
        conn.rollback()


def test_file_interpretation_accepts_all_date_orders_without_a_payroll_target(foundation):
    with psycopg2.connect(**foundation) as conn, conn.cursor() as cur:
        cid, uid = identities(cur)
        iid = envelope(cur, cid, uid, source="File")
        cur.execute("""INSERT INTO import.filesourcebatches(importid, companyid, sourcemanifest)
            VALUES (%s, %s, %s::jsonb)""", (iid, cid, json.dumps([{"name": "text.csv", "type": "csv",
                "size": 1, "sha256": "0" * 64, "units": [{"name": "CSV", "index": 1}]}])))
        cur.execute("""INSERT INTO import.filesources (importid, companyid, fileordinal, originalfilename,
            filetype, filesizebytes, filehashsha256, filecontent)
            VALUES (%s, %s, 1, 'text.csv', 'csv', 1, %s, %s) RETURNING filesourceid""", (iid, cid, "0" * 64, b"a"))
        fid = cur.fetchone()[0]
        cur.execute("""INSERT INTO import.filesourceunits(filesourceid, importid, companyid, unitname,
            unitordinal, unitkind, headerrownumber, encoding, delimiter)
            VALUES (%s, %s, %s, 'CSV', 1, 'CSV', 1, 'cp1252', ';')""", (fid, iid, cid))
        for order in ("DMY", "MDY", "YMD", None):
            cur.execute("UPDATE import.filesourceunits SET dateorder = %s WHERE importid = %s", (order, iid))
            cur.execute("SELECT dateorder, encoding, delimiter FROM import.filesourceunits WHERE importid = %s", (iid,))
            assert cur.fetchone() == (order, "cp1252", ";")
        conn.rollback()


def test_schema_guard_refuses_narrowed_date_order_and_missing_compact_values(foundation):
    with psycopg2.connect(**foundation) as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE import.filesourceunits DROP CONSTRAINT ck_filesourceunits_dateorder")
        cur.execute("ALTER TABLE import.filesourceunits ADD CONSTRAINT ck_filesourceunits_dateorder CHECK (dateorder IN ('DMY', 'MDY'))")
        assert any("ck_filesourceunits_dateorder" in error for error in _check_import_foundation(cur))
        conn.rollback()
        cur.execute("ALTER TABLE import.payrollworkrecords DROP COLUMN valuesjson")
        assert any("valuesjson" in error for error in _check_import_foundation(cur))
        conn.rollback()


@pytest.mark.parametrize("dependency", [
    "CREATE VIEW app.unexpected_import_consumer AS SELECT * FROM import.importbatches",
    "CREATE TABLE app.unexpected_import_consumer (id bigint REFERENCES import.importbatches(importbatchid))",
    "CREATE FUNCTION app.unexpected_import_consumer() RETURNS bigint LANGUAGE plpgsql AS $$ BEGIN RETURN (SELECT count(*) FROM import.importbatches); END $$",
])
def test_replacement_refuses_unexpected_dependencies_without_partial_retirement(predecessors, dependency):
    conn = psycopg2.connect(**predecessors)
    try:
        with conn.cursor() as cur:
            cur.execute(dependency)
            cur.execute("SAVEPOINT retirement")
            with pytest.raises(psycopg2.Error, match="IMPORT_PREDECESSOR_DEPENDENCY"):
                cur.execute(SQL.read_text())
            cur.execute("ROLLBACK TO SAVEPOINT retirement")
            cur.execute("SELECT version_num, to_regclass('import.importbatches') IS NOT NULL FROM alembic_version")
            assert cur.fetchone() == ("0093", True)
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize("table", ["import.importbatches", "import.importfiles", "import.importrows",
    "import.importdetectedcolumns", "import.importmappingtemplates", "import.importmappingtemplatelines",
    "import.importvalidationissues", "integration.integrationconnections", "integration.integrationsyncruns"])
def test_replacement_checks_every_predecessor_for_data(predecessors, table):
    conn = psycopg2.connect(**predecessors)
    try:
        with conn.cursor() as cur:
            # A temporary table substitutes only this target's shape. The DO
            # preflight must inspect all nine names, not just a root row count.
            # Triggers/FKs are deliberately untouched in real application tables.
            schema, name = table.split(".")
            renamed = f"{name}_original"
            cur.execute(f"ALTER TABLE {table} RENAME TO {renamed}")
            cur.execute(f"CREATE TABLE {table} (test_value integer)")
            cur.execute(f"INSERT INTO {table} VALUES (1)")
            cur.execute("SAVEPOINT retirement")
            with pytest.raises(psycopg2.Error, match="IMPORT_PREDECESSOR_DATA"):
                cur.execute(SQL.read_text())
            cur.execute("ROLLBACK TO SAVEPOINT retirement")
            cur.execute(f"SELECT test_value FROM {table}")
            assert cur.fetchone() == (1,)
            cur.execute("SELECT version_num FROM alembic_version")
            assert cur.fetchone() == ("0093",)
    finally:
        conn.rollback()
        conn.close()
