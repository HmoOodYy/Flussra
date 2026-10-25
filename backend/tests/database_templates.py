"""Session-owned pristine schemas, cloned into independently owned test databases.

Templates have no application rows and accept no connections after construction.
Each clone has its own committed state: multi-connection tests keep real transaction
and locking semantics, while module teardown still releases every connection pool.
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql


class SchemaDatabases:
    def __init__(self, cluster_dsn: dict, project_root: Path) -> None:
        self._control = {**cluster_dsn, "database": "postgres"}
        self._root = project_root
        self._templates: dict[str, str] = {}
        self._owned: set[str] = set()

    def _admin(self, statement: sql.Composable) -> None:
        conn = psycopg2.connect(client_encoding="utf-8", **self._control)
        try:
            conn.autocommit = True
            with conn.cursor() as cursor:
                cursor.execute(statement)
        finally:
            conn.close()

    def _create(self, name: str, template: str) -> dict:
        self._admin(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
            sql.Identifier(name), sql.Identifier(template)))
        self._owned.add(name)
        return {**self._control, "database": name}

    def _drop(self, name: str) -> None:
        if name not in self._owned:
            raise ValueError(f"Database is not owned by this test session: {name}")
        self._admin(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        self._owned.remove(name)

    def template(self, authority: str = "sql") -> str:
        if authority not in {"sql", "alembic"}:
            raise ValueError(f"Unknown schema authority: {authority}")
        if authority in self._templates:
            return self._templates[authority]
        name = f"schema_{authority}_{uuid4().hex[:12]}"
        dsn = self._create(name, "template0")
        try:
            if authority == "sql":
                conn = psycopg2.connect(client_encoding="utf-8", **dsn)
                try:
                    conn.autocommit = True
                    with conn.cursor() as cursor:
                        for migration in sorted((self._root / "migrations" / "sql").glob("*.sql")):
                            cursor.execute(migration.read_text(encoding="utf-8"))
                finally:
                    conn.close()
            else:
                env = {**os.environ, "DATABASE_URL": (
                    f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:{dsn['port']}/{name}"),
                    "SECRET_KEY": "test-only-schema-template", "ENVIRONMENT": "test"}
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "alembic", "upgrade", "head"],
                    cwd=self._root, env=env, capture_output=True, text=True, check=False,
                )
                if result.returncode:
                    raise RuntimeError(f"Fresh Alembic template failed:\n{result.stdout}\n{result.stderr}")
            self._admin(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(sql.Identifier(name)))
        except BaseException:
            self._drop(name)
            raise
        self._templates[authority] = name
        return name

    @contextmanager
    def database(self, prefix: str, *, authority: str = "sql") -> Iterator[dict]:
        name = f"{prefix}_{uuid4().hex[:12]}"
        dsn = self._create(name, self.template(authority))
        try:
            yield dsn
        finally:
            self._drop(name)

    def close(self) -> None:
        for name in list(self._owned):
            self._drop(name)
        self._templates.clear()
