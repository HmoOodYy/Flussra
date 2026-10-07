"""Migration 0083: PayDefinition governance and provenance."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql

_ROOT = Path(__file__).resolve().parents[2]

GOVERNANCE_TABLES = {
    "paydefinitionrequests", "paydefinitionrequestevents", "paydefinitionprovenance",
}


def _alembic(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "alembic", *args],
        cwd=_ROOT, env=env, capture_output=True, text=True, check=False,
    )


@pytest.fixture
def governance_database(pg_instance):
    dsn = pg_instance.dsn()
    name = "p3cm_" + uuid4().hex[:12]
    admin = psycopg2.connect(**dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    env = os.environ.copy()
    env["DATABASE_URL"] = f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:{dsn['port']}/{name}"
    env["SECRET_KEY"] = "p3c-disposable-migration-test"
    try:
        yield env, {**dsn, "database": name}
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def _governance_tables(cur) -> set[str]:
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'payroll' AND table_name = ANY(%s)
    """, (sorted(GOVERNANCE_TABLES),))
    return {row[0] for row in cur.fetchall()}


def test_0082_state_upgrades_to_0083_without_touching_it(governance_database):
    env, dsn = governance_database
    assert _alembic(env, "upgrade", "0082").returncode == 0
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        assert _governance_tables(cur) == set()
        cur.execute("""
            INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended,
                                        timezonename)
            VALUES ('P3CM', 'P3CM', 'P3CM', 'Active', FALSE, 'UTC') RETURNING companyid
        """)
        company_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO payroll.paydefinitions
                (companyid, definitioncode, definitionname, inputtype, calculationmethod)
            VALUES (%s, 'KEEP', 'Keep', 'Decimal', 'PerUnit') RETURNING paydefinitionid
        """, (company_id,))
        definition_id = cur.fetchone()[0]
        conn.commit()

    upgraded = _alembic(env, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM public.alembic_version")
        assert cur.fetchone() == ("0083",)
        assert _governance_tables(cur) == GOVERNANCE_TABLES
        for table in GOVERNANCE_TABLES:
            cur.execute(sql.SQL("SELECT count(*) FROM payroll.{}").format(sql.Identifier(table)))
            assert cur.fetchone()[0] == 0
        cur.execute("SELECT definitioncode FROM payroll.paydefinitions WHERE paydefinitionid = %s",
                    (definition_id,))
        assert cur.fetchone() == ("KEEP",)


def test_fresh_database_upgrades_through_0083_with_the_governance_objects(governance_database):
    env, dsn = governance_database
    assert _alembic(env, "upgrade", "head").returncode == 0
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        assert _governance_tables(cur) == GOVERNANCE_TABLES
        cur.execute("""
            SELECT t.tgname FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'payroll' AND NOT t.tgisinternal AND c.relname = ANY(%s)
        """, (sorted(GOVERNANCE_TABLES),))
        assert {row[0] for row in cur.fetchall()} == {
            "trg_paydefinitionrequests_guard",
            "trg_paydefinitionrequestevents_appendonly",
            "trg_paydefinitionprovenance_immutable",
            "trg_paydefinitionprovenance_requestlink",
        }


def test_downgrade_removes_empty_governance_tables_and_refuses_when_rows_exist(
    governance_database,
):
    env, dsn = governance_database
    assert _alembic(env, "upgrade", "head").returncode == 0
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended,
                                        timezonename)
            VALUES ('P3CD', 'P3CD', 'P3CD', 'Active', FALSE, 'UTC') RETURNING companyid
        """)
        company_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
            VALUES (%s, 'P3CD', 'P3CD', 'Active', TRUE) RETURNING branchid
        """, (company_id,))
        branch_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO sec.users (companyid, username, displayname)
            VALUES (%s, 'p3cd', 'p3cd') RETURNING userid
        """, (company_id,))
        user_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO payroll.paydefinitionrequests
                (companyid, requestingbranchid, createdbyuserid) VALUES (%s, %s, %s)
        """, (company_id, branch_id, user_id))
        conn.commit()

    refused = _alembic(env, "downgrade", "0082")
    assert refused.returncode != 0
    assert "Downgrade of 0083 refused" in refused.stderr

    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM payroll.paydefinitionrequests")
        conn.commit()
    downgraded = _alembic(env, "downgrade", "0082")
    assert downgraded.returncode == 0, downgraded.stderr
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        assert _governance_tables(cur) == set()
        cur.execute("SELECT version_num FROM public.alembic_version")
        assert cur.fetchone() == ("0082",)
    assert _alembic(env, "upgrade", "head").returncode == 0
