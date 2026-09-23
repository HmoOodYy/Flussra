"""Both supported Phase 1 upgrade paths run on disposable PostgreSQL databases."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
import pytest


_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("starting_revision", ["base", "0065"])
def test_phase1_alembic_upgrade_paths(pg_instance, starting_revision):
    dsn = pg_instance.dsn()
    database = "phase1_" + uuid4().hex[:12]
    admin = psycopg2.connect(**dsn)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        url = (
            f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:"
            f"{dsn['port']}/{database}"
        )
        env = os.environ.copy()
        env["DATABASE_URL"] = url
        env["SECRET_KEY"] = "phase1-disposable-migration-test"
        if starting_revision == "0065":
            result = subprocess.run(
                [sys.executable, "-B", "-m", "alembic", "upgrade", "0065"],
                cwd=_ROOT, env=env, capture_output=True, text=True, check=False,
            )
            assert result.returncode == 0, result.stderr
        result = subprocess.run(
            [sys.executable, "-B", "-m", "alembic", "upgrade", "head"],
            cwd=_ROOT, env=env, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        check_dsn = dict(dsn)
        check_dsn["database"] = database
        with psycopg2.connect(**check_dsn) as check:
            with check.cursor() as cursor:
                cursor.execute("SELECT version_num FROM alembic_version")
                assert cursor.fetchone() == ("0068",)
                cursor.execute("SELECT COUNT(*) FROM payroll.PayrollSetups")
                assert cursor.fetchone() == (0,)
    finally:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))
        admin.close()
