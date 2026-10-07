"""Migration 0084: BranchPayItemConfig is re-keyed to PayDefinitionID."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import errors, sql

_ROOT = Path(__file__).resolve().parents[2]


def _alembic(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "alembic", *args],
        cwd=_ROOT, env=env, capture_output=True, text=True, check=False,
    )


@pytest.fixture
def p4a_database(pg_instance):
    dsn = pg_instance.dsn()
    name = "p4am_" + uuid4().hex[:12]
    admin = psycopg2.connect(**dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    env = os.environ.copy()
    env["DATABASE_URL"] = f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:{dsn['port']}/{name}"
    env["SECRET_KEY"] = "p4a-disposable-migration-test"
    try:
        yield env, {**dsn, "database": name}
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def _company_with_branch(cur, code: str) -> tuple[int, int]:
    cur.execute("""
        INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended,
                                    timezonename, currencycode)
        VALUES (%s, %s, %s, 'Active', FALSE, 'UTC', 'USD') RETURNING companyid
    """, (code, code, code))
    company_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (%s, %s, %s, 'Active', TRUE) RETURNING branchid
    """, (company_id, code, code))
    return company_id, cur.fetchone()[0]


def _definition(cur, company_id: int, code: str) -> int:
    cur.execute("""
        INSERT INTO payroll.paydefinitions
            (companyid, definitioncode, definitionname, inputtype, calculationmethod)
        VALUES (%s, %s, %s, 'Decimal', 'PerUnit') RETURNING paydefinitionid
    """, (company_id, code, code))
    return cur.fetchone()[0]


def _columns(cur) -> dict[str, str]:
    cur.execute("""
        SELECT column_name, is_nullable FROM information_schema.columns
        WHERE table_schema = 'payroll' AND table_name = 'branchpayitemconfig'
    """)
    return {name: nullable for name, nullable in cur.fetchall()}


def test_0083_state_is_reset_without_backfill_and_other_state_is_untouched(p4a_database):
    env, dsn = p4a_database
    assert _alembic(env, "upgrade", "0083").returncode == 0
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        company_id, branch_id = _company_with_branch(cur, "P4AM")
        definition_id = _definition(cur, company_id, "KEEP")
        cur.execute("SELECT payitemid FROM payroll.payitems WHERE companyid IS NULL LIMIT 1")
        item_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO payroll.branchpayitemconfig
                (companyid, branchid, payitemid, isactive, effectivefrom)
            VALUES (%s, %s, %s, TRUE, '2020-01-01')
        """, (company_id, branch_id, item_id))
        cur.execute("SELECT count(*) FROM payroll.payitems")
        payitems_before = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM payroll.ratetypes")
        rate_types_before = cur.fetchone()[0]
        conn.commit()

    upgraded = _alembic(env, "upgrade", "0084")
    assert upgraded.returncode == 0, upgraded.stderr

    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM payroll.branchpayitemconfig")
        assert cur.fetchone() == (0,)
        cur.execute("SELECT definitioncode FROM payroll.paydefinitions WHERE paydefinitionid = %s",
                    (definition_id,))
        assert cur.fetchone() == ("KEEP",)
        cur.execute("SELECT count(*) FROM payroll.payitems")
        assert cur.fetchone() == (payitems_before,)
        cur.execute("SELECT count(*) FROM payroll.ratetypes")
        assert cur.fetchone() == (rate_types_before,)
        cur.execute("SELECT version_num FROM public.alembic_version")
        assert cur.fetchone() == ("0084",)


def test_final_shape_and_constraints(p4a_database):
    env, dsn = p4a_database
    assert _alembic(env, "upgrade", "0084").returncode == 0
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
        columns = _columns(cur)
        assert columns["paydefinitionid"] == "NO"
        assert columns["payitemid"] == "YES"
        assert "branchdisplayname" not in columns
        assert {"companyid", "branchid", "isactive", "effectivefrom", "effectiveto",
                "notes"} <= set(columns)

        company_id, branch_id = _company_with_branch(cur, "P4AS")
        other_company_id, _ = _company_with_branch(cur, "P4AT")
        definition_id = _definition(cur, company_id, "ONE")
        foreign_definition_id = _definition(cur, other_company_id, "FOREIGN")
        conn.commit()

        insert = """
            INSERT INTO payroll.branchpayitemconfig
                (companyid, branchid, paydefinitionid, isactive, effectivefrom, effectiveto)
            VALUES (%s, %s, %s, TRUE, %s, %s)
        """

        def rejected(error, *args) -> None:
            with pytest.raises(error):
                cur.execute(insert, args)
            conn.rollback()

        cur.execute(insert, (company_id, branch_id, definition_id, "2024-01-01", "2024-12-31"))
        cur.execute(insert, (company_id, branch_id, definition_id, "2025-01-01", None))
        conn.commit()

        rejected(errors.UniqueViolation, company_id, branch_id, definition_id, "2026-01-01", None)
        rejected(errors.ExclusionViolation, company_id, branch_id, definition_id,
                 "2024-06-01", "2024-07-01")
        rejected(errors.CheckViolation, company_id, branch_id, definition_id,
                 "2023-02-01", "2023-01-01")
        rejected(errors.ForeignKeyViolation, company_id, branch_id, foreign_definition_id,
                 "2020-01-01", None)

        with pytest.raises(errors.CheckViolation):
            cur.execute("""
                INSERT INTO payroll.branchpayitemconfig
                    (companyid, branchid, paydefinitionid, payitemid, isactive, effectivefrom)
                VALUES (%s, %s, %s, (SELECT payitemid FROM payroll.payitems LIMIT 1),
                        TRUE, '2019-01-01')
            """, (company_id, branch_id, definition_id))
        conn.rollback()


def test_0084_is_irreversible(p4a_database):
    env, _ = p4a_database
    assert _alembic(env, "upgrade", "0084").returncode == 0
    result = _alembic(env, "downgrade", "0083")
    assert result.returncode != 0
    assert "irreversible" in (result.stderr + result.stdout).lower()
