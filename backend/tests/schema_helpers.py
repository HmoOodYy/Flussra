"""Small helpers for disposable schema databases and Alembic subprocesses."""
from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql

ROOT = Path(__file__).resolve().parents[2]


def _alembic(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "alembic", *args],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )


def _database_env(dsn: dict) -> dict[str, str]:
    return {
        **os.environ,
        "DATABASE_URL": f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:{dsn['port']}/{dsn['database']}",
        "SECRET_KEY": "disposable-schema-test",
        "ENVIRONMENT": "test",
    }


@contextmanager
def _schema_database(schema_databases, prefix: str, *, authority: str = "alembic"):
    """Clone a pristine session schema and provide the matching application environment."""
    with schema_databases.database(prefix, authority=authority) as dsn:
        yield _database_env(dsn), dsn


@contextmanager
def _disposable_database(pg_instance, prefix: str):
    """Create an actually empty database for fresh-install/bootstrap checks."""
    dsn = pg_instance.dsn()
    name = f"{prefix}_{uuid4().hex[:12]}"
    admin = psycopg2.connect(**dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    env = os.environ.copy()
    env["DATABASE_URL"] = f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:{dsn['port']}/{name}"
    env["SECRET_KEY"] = "disposable-schema-test"
    try:
        yield env, {**dsn, "database": name}
    finally:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def _version(dsn) -> str:
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT version_num FROM public.alembic_version")
        return cursor.fetchone()[0]


def _columns(dsn, table: str, schema: str = "payroll") -> set[str]:
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
            (schema, table),
        )
        return {row[0] for row in cursor.fetchall()}


def _tables(dsn, schema: str = "payroll") -> set[str]:
    with psycopg2.connect(**dsn) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,))
        return {row[0] for row in cursor.fetchall()}
