"""G0.1 migration safety for the canonical Bonus amount precision."""
from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql
from pydantic import ValidationError

from app.payroll.schemas import BonusBatchItem, BonusEventCreate, BonusEventUpdate

_ROOT = Path(__file__).resolve().parents[2]


def _alembic(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "alembic", *args],
        cwd=_ROOT, env=env, capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize("model, payload", [
    (BonusEventCreate, {"driver_id": 1, "amount": "1.2345"}),
    (BonusEventUpdate, {"amount": "1.2345"}),
    (BonusBatchItem, {"driver_id": 1, "amount": "1.2345"}),
])
def test_shared_bonus_amount_contract_accepts_four_decimals(model, payload):
    assert model.model_validate(payload).amount == Decimal("1.2345")


@pytest.mark.parametrize("amount", [
    "1.23456", "0", "-1", "100000000000000", "NaN", "Infinity", "-Infinity",
])
def test_all_bonus_amount_models_reject_invalid_numeric_18_4_values(amount):
    for model, base in (
        (BonusEventCreate, {"driver_id": 1}),
        (BonusEventUpdate, {}),
        (BonusBatchItem, {"driver_id": 1}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate({**base, "amount": amount})


@pytest.fixture
def g0_1_migration_database(pg_instance):
    dsn = pg_instance.dsn()
    name = "g01_" + uuid4().hex[:12]
    admin = psycopg2.connect(**dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    env = os.environ.copy()
    env["DATABASE_URL"] = (
        f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:{dsn['port']}/{name}"
    )
    env["SECRET_KEY"] = "g0-1-disposable-migration-test"
    check_dsn = {**dsn, "database": name}
    try:
        result = _alembic(env, "upgrade", "0076")
        assert result.returncode == 0, result.stderr
        yield env, check_dsn
    finally:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def _insert_bonus_amount(conn, amount: str) -> int:
    with conn.cursor() as cursor:
        cursor.execute("ALTER TABLE payroll.payrollbonusevents DISABLE TRIGGER ALL")
        cursor.execute("""
            INSERT INTO payroll.payrollbonusevents
                (companyid, branchid, payrollperiodid, driverid, amount)
            VALUES (1, 1, 1, 1, %s)
            RETURNING payrollbonuseventid
        """, (amount,))
        event_id = cursor.fetchone()[0]
        cursor.execute("ALTER TABLE payroll.payrollbonusevents ENABLE TRIGGER ALL")
    conn.commit()
    return event_id


def test_0077_upgrade_precision_preflight_and_safe_downgrade(g0_1_migration_database):
    env, dsn = g0_1_migration_database
    with psycopg2.connect(**dsn) as conn:
        overflow_id = _insert_bonus_amount(conn, "100000000000000.00")
    refused = _alembic(env, "upgrade", "0077")
    assert refused.returncode != 0
    assert "G0.1 preflight failed" in refused.stderr
    with psycopg2.connect(**dsn) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            assert cursor.fetchone() == ("0076",)
            cursor.execute("DELETE FROM payroll.payrollbonusevents WHERE payrollbonuseventid=%s", (overflow_id,))
        conn.commit()

    with psycopg2.connect(**dsn) as conn:
        original_id = _insert_bonus_amount(conn, "12.34")
    upgraded = _alembic(env, "upgrade", "0077")
    assert upgraded.returncode == 0, upgraded.stderr
    with psycopg2.connect(**dsn) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT numeric_precision, numeric_scale
                FROM information_schema.columns
                WHERE table_schema='payroll' AND table_name='payrollbonusevents'
                  AND column_name='amount'
            """)
            assert cursor.fetchone() == (18, 4)
            cursor.execute("SELECT amount FROM payroll.payrollbonusevents WHERE payrollbonuseventid=%s", (original_id,))
            assert str(cursor.fetchone()[0]) == "12.3400"
    with psycopg2.connect(**dsn) as conn:
        precise_id = _insert_bonus_amount(conn, "1.2345")
    refused_downgrade = _alembic(env, "downgrade", "0076")
    assert refused_downgrade.returncode != 0
    assert "Downgrade of 0077 refused" in refused_downgrade.stderr
    with psycopg2.connect(**dsn) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            assert cursor.fetchone() == ("0077",)
            cursor.execute("SELECT amount FROM payroll.payrollbonusevents WHERE payrollbonuseventid=%s", (precise_id,))
            assert str(cursor.fetchone()[0]) == "1.2345"
            cursor.execute("DELETE FROM payroll.payrollbonusevents WHERE payrollbonuseventid=%s", (precise_id,))
        conn.commit()
    safe_downgrade = _alembic(env, "downgrade", "0076")
    assert safe_downgrade.returncode == 0, safe_downgrade.stderr
    with psycopg2.connect(**dsn) as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT numeric_precision, numeric_scale
                FROM information_schema.columns
                WHERE table_schema='payroll' AND table_name='payrollbonusevents'
                  AND column_name='amount'
            """)
            assert cursor.fetchone() == (18, 2)
            cursor.execute("SELECT amount FROM payroll.payrollbonusevents WHERE payrollbonuseventid=%s", (original_id,))
            assert str(cursor.fetchone()[0]) == "12.34"
