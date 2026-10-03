"""Migration 0076 clean-state, refusal, downgrade and schema trust regression."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql

from app.db.schema_guard import _check_payroll_trust

_ROOT = Path(__file__).resolve().parents[2]


def _alembic(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "alembic", *args],
        cwd=_ROOT, env=env, capture_output=True, text=True, check=False,
    )


@pytest.fixture
def p3a_migration_database(pg_instance):
    dsn = pg_instance.dsn()
    name = "p3a_" + uuid4().hex[:12]
    admin = psycopg2.connect(**dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    env = os.environ.copy()
    env["DATABASE_URL"] = (
        f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:{dsn['port']}/{name}"
    )
    env["SECRET_KEY"] = "p3a-disposable-migration-test"
    check_dsn = {**dsn, "database": name}
    try:
        result = _alembic(env, "upgrade", "0075")
        assert result.returncode == 0, result.stderr
        yield env, check_dsn
    finally:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def test_0076_refuses_dirty_state_then_upgrades_clean_and_downgrades_safely(p3a_migration_database):
    env, dsn = p3a_migration_database
    with psycopg2.connect(**dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO core.companies(companycode, companyname) VALUES ('P3AMIG', 'P3a Migration') RETURNING companyid")
            company_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO payroll.payprofiles(companyid, profilecode, profilename, effectivefrom)
                VALUES (%s, 'P3A-MIG', 'P3a Migration', DATE '2099-01-01')
                RETURNING payprofileid
            """, (company_id,))
            profile_id = cur.fetchone()[0]
            cur.execute("SELECT ratetypeid FROM payroll.ratetypes ORDER BY ratetypeid LIMIT 1")
            rate_type_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO payroll.payprofilerates(payprofileid, ratetypeid, rateamount, effectivefrom)
                VALUES (%s, %s, 1.2345, DATE '2099-01-01')
            """, (profile_id, rate_type_id))
        conn.commit()
    dirty = _alembic(env, "upgrade", "0076")
    assert dirty.returncode != 0
    assert "P3A_REQUIRES_CLEAN_PREPRODUCTION_MONETARY_STATE" in dirty.stderr
    with psycopg2.connect(**dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            assert cur.fetchone() == ("0075",)
            cur.execute("SELECT to_regclass('core.supportedcurrencies')")
            assert cur.fetchone() == (None,)
            cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='core' AND table_name='companies' AND column_name='currencycode'")
            assert cur.fetchone() is None
            cur.execute("DELETE FROM payroll.payprofilerates WHERE payprofileid = %s", (profile_id,))
            cur.execute("DELETE FROM payroll.payprofiles WHERE payprofileid = %s", (profile_id,))
        conn.commit()
    clean = _alembic(env, "upgrade", "0076")
    assert clean.returncode == 0, clean.stderr
    with psycopg2.connect(**dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM core.supportedcurrencies")
            assert cur.fetchone()[0] >= 160
            cur.execute("SELECT minorunitdigits FROM core.supportedcurrencies WHERE currencycode='CLF'")
            assert cur.fetchone() == (4,)
            assert _check_payroll_trust(cur) == []
            cur.execute("UPDATE core.companies SET currencycode='USD' WHERE companyid=%s", (company_id,))
        conn.commit()
    protected = _alembic(env, "downgrade", "0075")
    assert protected.returncode != 0 and "Downgrade of 0076 refused" in protected.stderr
    with psycopg2.connect(**dsn) as conn:
        with conn.cursor() as cur:
            # Safe empty-state downgrade requires removing the configured Company.
            cur.execute("DELETE FROM core.companies WHERE companyid=%s", (company_id,))
        conn.commit()
    downgraded = _alembic(env, "downgrade", "0075")
    assert downgraded.returncode == 0, downgraded.stderr
    reupgraded = _alembic(env, "upgrade", "0076")
    assert reupgraded.returncode == 0, reupgraded.stderr
