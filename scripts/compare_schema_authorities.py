"""Empirically compare the direct SQL bootstrap with the Alembic schema."""

from __future__ import annotations

import ast
import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
MIGRATIONS_SQL = ROOT / "migrations" / "sql"
ALEMBIC_CONFIG = ROOT / "alembic.ini"
EXPECTED_HEAD = "0080"
OWNED_DATABASE = re.compile(r"flussra_g02d_(?:direct|alembic)_[0-9a-f]{12}\Z")
ALEMBIC_BOOKKEEPING = {("public", "alembic_version")}

sys.path.insert(0, str(BACKEND))


def _configure_postgres_path() -> None:
    """Match the repository's Windows test-fixture PostgreSQL discovery."""
    if os.name != "nt":
        return
    for candidate in sorted(glob.glob(r"C:\Program Files\PostgreSQL\*\bin"), reverse=True):
        if Path(candidate, "initdb.exe").is_file():
            os.environ["PATH"] = candidate + os.pathsep + os.environ.get("PATH", "")
            return


def _verify_alembic_graph() -> list[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(ALEMBIC_CONFIG))
    heads = ScriptDirectory.from_config(config).get_heads()
    if heads != [EXPECTED_HEAD]:
        raise RuntimeError(f"Expected one Alembic head {EXPECTED_HEAD}; found {heads!r}")
    return heads


def _fixture_seed_statements() -> list[str]:
    """Read the existing direct-bootstrap seed literal without importing conftest."""
    source = (BACKEND / "tests" / "conftest.py").read_text(encoding="utf-8")
    module = ast.parse(source, filename="backend/tests/conftest.py")
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_SEED_STMTS"
            for target in node.targets
        ):
            statements = ast.literal_eval(node.value)
            if not isinstance(statements, list) or not all(
                isinstance(statement, str) for statement in statements
            ):
                break
            return statements
    raise RuntimeError("Could not read the literal _SEED_STMTS from backend/tests/conftest.py")


def _stop_owned_postgresql(pg: Any) -> None:
    """Stop only the exact temporary cluster created by testing.postgresql."""
    if os.name != "nt":
        pg.stop()
        return

    process = pg.child_process
    if process is None:
        pg.cleanup()
        return

    data_dir = Path(pg.get_data_directory()).resolve()
    base_dir = Path(pg.base_dir).resolve()
    if data_dir.parent != base_dir or not (data_dir / "PG_VERSION").is_file():
        raise RuntimeError(f"Refusing to stop a PostgreSQL cluster not owned by this run: {data_dir}")

    try:
        pg.stop()
    except (ValueError, OSError):
        pg_bin = next(
            (
                Path(path)
                for path in sorted(glob.glob(r"C:\Program Files\PostgreSQL\*\bin"), reverse=True)
                if (Path(path) / "pg_ctl.exe").is_file()
            ),
            None,
        )
        if pg_bin is None:
            raise RuntimeError("PostgreSQL pg_ctl.exe is unavailable for owned-cluster cleanup")
        result = subprocess.run(
            [str(pg_bin / "pg_ctl.exe"), "stop", "-D", str(data_dir), "-m", "fast", "-w", "-t", "15"],
            capture_output=True,
            text=True,
            check=False,
        )
        deadline = time.monotonic() + 15
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if process.poll() is None:
            fallback = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
            process.wait(timeout=10)
            if fallback.returncode != 0:
                raise RuntimeError(
                    "Could not stop the owned PostgreSQL cluster: "
                    f"pg_ctl={result.returncode} {result.stderr.strip()}; "
                    f"taskkill={fallback.returncode} {fallback.stderr.strip()}"
                )
    finally:
        if process.poll() is not None:
            pg.child_process = None
            pg.cleanup()

    if process.poll() is None or base_dir.exists():
        raise RuntimeError(f"Temporary PostgreSQL cleanup incomplete: {base_dir}")


def _connect_kwargs(cluster_dsn: dict[str, Any], database: str | None = None) -> dict[str, Any]:
    result = dict(cluster_dsn)
    if database is not None:
        result["database"] = database
    return result


def _create_database(admin_dsn: dict[str, Any], name: str, created: set[str]) -> None:
    import psycopg2
    from psycopg2 import sql

    if not OWNED_DATABASE.fullmatch(name):
        raise RuntimeError(f"Refusing to create a database outside the G0.2D name pattern: {name}")
    connection = psycopg2.connect(**admin_dsn)
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created.add(name)
    finally:
        connection.close()


def _build_direct_database(dsn: dict[str, Any], name: str) -> tuple[int, int]:
    import bcrypt
    import psycopg2

    migration_files = sorted(MIGRATIONS_SQL.glob("*.sql"), key=lambda path: path.name)
    if not migration_files:
        raise RuntimeError(f"No direct SQL migrations found under {MIGRATIONS_SQL}")

    connection = psycopg2.connect(**_connect_kwargs(dsn, name))
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            for path in migration_files:
                print(f"Database A direct SQL: {path.relative_to(ROOT)}")
                cursor.execute(path.read_text(encoding="utf-8"))

            seed_statements = _fixture_seed_statements()
            password_hash = bcrypt.hashpw(
                b"TestPass123!", bcrypt.gensalt(rounds=12)
            ).decode("utf-8")
            for statement in seed_statements:
                if "%(pw_hash)s" in statement:
                    cursor.execute(statement, {"pw_hash": password_hash})
                else:
                    cursor.execute(statement)
    finally:
        connection.close()
    return len(migration_files), len(seed_statements)


def _alembic_environment(dsn: dict[str, Any], database: str) -> dict[str, str]:
    from sqlalchemy.engine import URL

    url = URL.create(
        "postgresql+asyncpg",
        username=dsn["user"],
        password=dsn.get("password") or None,
        host=dsn["host"],
        port=int(dsn["port"]),
        database=database,
    ).render_as_string(hide_password=False)
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": url,
            "SECRET_KEY": "g0-2d-schema-comparison-only-not-a-real-secret",
            "ENVIRONMENT": "test",
            "PYTHONPATH": str(BACKEND),
        }
    )
    return environment


def _run_alembic(dsn: dict[str, Any], name: str) -> str:
    environment = _alembic_environment(dsn, name)
    command = [sys.executable, "-m", "alembic", "upgrade", "head"]
    print("Database B Alembic command:", " ".join(command))
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"Alembic upgrade head failed with exit code {result.returncode}")

    import psycopg2

    connection = psycopg2.connect(**_connect_kwargs(dsn, name))
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM public.alembic_version ORDER BY version_num")
            versions = [row[0] for row in cursor.fetchall()]
    finally:
        connection.close()
    if versions != [EXPECTED_HEAD]:
        raise RuntimeError(f"Expected database revision {EXPECTED_HEAD}; found {versions!r}")
    return versions[0]


def _query(connection: Any, statement: str) -> list[dict[str, Any]]:
    from psycopg2.extras import RealDictCursor

    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(statement)
        return [dict(row) for row in cursor.fetchall()]


def _keyed(rows: list[dict[str, Any]], key_fields: tuple[str, ...]) -> dict[tuple[str, ...], dict[str, Any]]:
    result: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(str(row[field]) for field in key_fields)
        if key in result:
            raise RuntimeError(f"Catalog query returned a duplicate key: {key!r}")
        result[key] = {field: value for field, value in row.items() if field not in key_fields}
    return result


def _normalize_function_comments(definition: str | None) -> str | None:
    """Ignore SQL line comments without stripping text inside quoted bodies."""
    if definition is None:
        return None
    body_match = re.search(r"\bAS\s+(\$[A-Za-z_0-9]*\$)(.*?)(\1)", definition, re.DOTALL)
    if body_match is None:
        return definition

    body = body_match.group(2)
    normalized: list[str] = []
    index = 0
    state = "normal"
    dollar_tag = ""
    while index < len(body):
        char = body[index]
        if state == "normal":
            if body.startswith("--", index):
                while normalized and normalized[-1] in " \t":
                    normalized.pop()
                newline = body.find("\n", index)
                if newline < 0:
                    break
                index = newline + 1
                continue
            if char == "'":
                state = "single"
            elif char == '"':
                state = "double"
            elif char == "$":
                tag_match = re.match(r"\$[A-Za-z_0-9]*\$", body[index:])
                if tag_match:
                    dollar_tag = tag_match.group(0)
                    state = "dollar"
                    normalized.append(dollar_tag)
                    index += len(dollar_tag)
                    continue
            normalized.append(char)
            index += 1
        elif state == "single":
            normalized.append(char)
            index += 1
            if char == "\\" and index < len(body):
                normalized.append(body[index])
                index += 1
            elif char == "'":
                if index < len(body) and body[index] == "'":
                    normalized.append(body[index])
                    index += 1
                else:
                    state = "normal"
        elif state == "double":
            normalized.append(char)
            index += 1
            if char == '"':
                if index < len(body) and body[index] == '"':
                    normalized.append(body[index])
                    index += 1
                else:
                    state = "normal"
        else:
            if body.startswith(dollar_tag, index):
                normalized.append(dollar_tag)
                index += len(dollar_tag)
                state = "normal"
            else:
                normalized.append(char)
                index += 1

    normalized_body = "".join(normalized)
    return definition[: body_match.start(2)] + normalized_body + definition[body_match.end(2) :]


def _snapshot(connection: Any) -> dict[str, dict[tuple[str, ...], dict[str, Any]]]:
    def schema_filter(namespace: str) -> str:
        return f"""
            {namespace}.nspname NOT IN ('pg_catalog', 'information_schema')
            AND {namespace}.nspname NOT LIKE 'pg_toast%'
            AND {namespace}.nspname NOT LIKE 'pg_temp_%'
        """

    app_schema_filter = schema_filter("n")
    app_rel_filter = f"({app_schema_filter}) AND NOT (n.nspname = 'public' AND c.relname = 'alembic_version')"
    index_rel_filter = (
        f"({schema_filter('table_ns')}) AND NOT "
        "(table_ns.nspname = 'public' AND tbl.relname = 'alembic_version')"
    )
    trigger_rel_filter = (
        f"({schema_filter('n')}) AND NOT "
        "(n.nspname = 'public' AND tbl.relname = 'alembic_version')"
    )
    queries: dict[str, tuple[str, tuple[str, ...]]] = {
        "schemas": (
            f"SELECT n.nspname AS schema_name FROM pg_namespace n WHERE {app_schema_filter}",
            ("schema_name",),
        ),
        "relations": (
            f"""
            SELECT n.nspname AS schema_name, c.relname AS object_name,
                   c.relkind::text AS relation_kind, c.relpersistence::text AS persistence,
                   c.relispartition AS is_partition, c.relrowsecurity AS row_security,
                   c.relforcerowsecurity AS force_row_security, c.reloptions AS options,
                   am.amname AS access_method,
                   CASE WHEN c.relkind = 'p' THEN pg_get_partkeydef(c.oid) END AS partition_key
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_am am ON am.oid = c.relam
            WHERE {app_rel_filter} AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
            """,
            ("schema_name", "object_name"),
        ),
        "columns": (
            f"""
            SELECT n.nspname AS schema_name, c.relname AS table_name, a.attname AS column_name,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                   a.attnotnull AS not_null, pg_get_expr(d.adbin, d.adrelid) AS default_expression,
                   a.attidentity::text AS identity_kind, a.attgenerated::text AS generated_kind,
                   coll_ns.nspname AS collation_schema, coll.collname AS collation_name,
                   a.attstorage::text AS storage_kind, a.attcompression::text AS compression_kind
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_type t ON t.oid = a.atttypid
            LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            LEFT JOIN pg_collation coll ON coll.oid = a.attcollation
            LEFT JOIN pg_namespace coll_ns ON coll_ns.oid = coll.collnamespace
            WHERE {app_rel_filter} AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY n.nspname, c.relname, a.attname
            """,
            ("schema_name", "table_name", "column_name"),
        ),
        "constraints": (
            f"""
            SELECT COALESCE(rel_ns.nspname, type_ns.nspname) AS schema_name,
                   COALESCE(rel.relname, typ.typname) AS table_or_domain,
                   con.conname AS object_name, con.contype::text AS constraint_kind,
                   pg_get_constraintdef(con.oid, true) AS definition,
                   con.convalidated AS validated, con.condeferrable AS deferrable,
                   con.condeferred AS initially_deferred, con.connoinherit AS no_inherit,
                   con.conislocal AS is_local, con.coninhcount AS inheritance_count
            FROM pg_constraint con
            LEFT JOIN pg_class rel ON rel.oid = con.conrelid
            LEFT JOIN pg_namespace rel_ns ON rel_ns.oid = rel.relnamespace
            LEFT JOIN pg_class referenced_rel ON referenced_rel.oid = con.confrelid
            LEFT JOIN pg_namespace referenced_ns ON referenced_ns.oid = referenced_rel.relnamespace
            LEFT JOIN pg_type typ ON typ.oid = con.contypid
            LEFT JOIN pg_namespace type_ns ON type_ns.oid = typ.typnamespace
            WHERE con.contype IN ('c', 'f', 'p', 'u', 'x')
              AND ((rel_ns.nspname IS NOT NULL AND {schema_filter('rel_ns')})
                   OR (type_ns.nspname IS NOT NULL AND {schema_filter('type_ns')}))
              AND NOT (rel_ns.nspname = 'public' AND rel.relname = 'alembic_version')
              AND (referenced_rel.oid IS NULL OR NOT
                   (referenced_ns.nspname = 'public' AND referenced_rel.relname = 'alembic_version'))
            """,
            ("schema_name", "table_or_domain", "object_name"),
        ),
        "indexes": (
            f"""
            SELECT table_ns.nspname AS schema_name, tbl.relname AS table_name,
                   idx.relname AS object_name, am.amname AS access_method,
                   i.indisunique AS is_unique, i.indisprimary AS is_primary,
                   i.indisexclusion AS is_exclusion, i.indisvalid AS is_valid,
                   i.indisready AS is_ready, i.indimmediate AS immediate,
                   i.indnullsnotdistinct AS nulls_not_distinct,
                   pg_get_indexdef(i.indexrelid) AS definition,
                   pg_get_expr(i.indpred, i.indrelid) AS predicate,
                   pg_get_expr(i.indexprs, i.indrelid) AS expressions
            FROM pg_index i
            JOIN pg_class tbl ON tbl.oid = i.indrelid
            JOIN pg_namespace table_ns ON table_ns.oid = tbl.relnamespace
            JOIN pg_class idx ON idx.oid = i.indexrelid
            JOIN pg_am am ON am.oid = idx.relam
            WHERE {index_rel_filter}
            """,
            ("schema_name", "table_name", "object_name"),
        ),
        "triggers": (
            f"""
            SELECT n.nspname AS schema_name, tbl.relname AS table_name,
                   t.tgname AS object_name, t.tgenabled::text AS enabled_state,
                   t.tgisinternal AS is_internal, t.tgtype AS event_bits,
                   t.tgdeferrable AS deferrable, t.tginitdeferred AS initially_deferred,
                   t.tgfoid::regprocedure::text AS function_binding,
                   pg_get_triggerdef(t.oid, true) AS definition,
                   con_ns.nspname AS constraint_schema, con_tbl.relname AS constraint_table,
                   con.conname AS constraint_name
            FROM pg_trigger t
            JOIN pg_class tbl ON tbl.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = tbl.relnamespace
            LEFT JOIN pg_constraint con ON con.oid = t.tgconstraint
            LEFT JOIN pg_class con_tbl ON con_tbl.oid = con.conrelid
            LEFT JOIN pg_namespace con_ns ON con_ns.oid = con_tbl.relnamespace
            WHERE {trigger_rel_filter}
            """,
            ("schema_name", "table_name", "object_name", "constraint_schema", "constraint_table", "constraint_name", "event_bits", "function_binding"),
        ),
        "functions": (
            f"""
            SELECT n.nspname AS schema_name, p.proname AS object_name,
                   p.prokind::text AS routine_kind,
                   pg_get_function_identity_arguments(p.oid) AS identity_arguments,
                   pg_get_function_result(p.oid) AS result_type,
                   lang.lanname AS language, p.proisstrict AS strict,
                   p.provolatile::text AS volatility, p.prosecdef AS security_definer,
                   p.proleakproof AS leakproof, p.proparallel::text AS parallel,
                   p.proconfig AS configuration,
                   CASE WHEN p.prokind IN ('f', 'p') THEN pg_get_functiondef(p.oid) END AS definition,
                   agg.aggkind::text AS aggregate_kind,
                   CASE WHEN agg.aggtransfn <> 0 THEN agg.aggtransfn::regprocedure::text END AS transition_function,
                   CASE WHEN agg.aggfinalfn <> 0 THEN agg.aggfinalfn::regprocedure::text END AS final_function,
                   CASE WHEN agg.aggcombinefn <> 0 THEN agg.aggcombinefn::regprocedure::text END AS combine_function,
                   CASE WHEN agg.aggserialfn <> 0 THEN agg.aggserialfn::regprocedure::text END AS serialize_function,
                   CASE WHEN agg.aggdeserialfn <> 0 THEN agg.aggdeserialfn::regprocedure::text END AS deserialize_function,
                   pg_catalog.format_type(agg.aggtranstype, NULL) AS transition_type,
                   agg.agginitval AS initial_value, agg.aggnumdirectargs AS direct_arguments
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            JOIN pg_language lang ON lang.oid = p.prolang
            LEFT JOIN pg_aggregate agg ON agg.aggfnoid = p.oid
            WHERE {app_schema_filter}
            """,
            ("schema_name", "object_name", "identity_arguments"),
        ),
        "views": (
            f"""
            SELECT n.nspname AS schema_name, c.relname AS object_name,
                   c.relkind::text AS view_kind, pg_get_viewdef(c.oid, true) AS definition,
                   c.reloptions AS options
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE {app_rel_filter} AND c.relkind IN ('v', 'm')
            """,
            ("schema_name", "object_name"),
        ),
        "sequences": (
            f"""
            SELECT n.nspname AS schema_name, seq.relname AS object_name,
                   pg_catalog.format_type(s.seqtypid, NULL) AS data_type,
                   s.seqstart AS start_value, s.seqincrement AS increment_by,
                   s.seqmin AS minimum_value, s.seqmax AS maximum_value,
                   s.seqcache AS cache_size, s.seqcycle AS cycles,
                   own_ns.nspname AS owner_schema, own_tbl.relname AS owner_table,
                   own_att.attname AS owner_column, dep.deptype::text AS ownership_kind
            FROM pg_class seq
            JOIN pg_namespace n ON n.oid = seq.relnamespace
            JOIN pg_sequence s ON s.seqrelid = seq.oid
            LEFT JOIN pg_depend dep ON dep.classid = 'pg_class'::regclass
                 AND dep.objid = seq.oid AND dep.refclassid = 'pg_class'::regclass
                 AND dep.deptype IN ('a', 'i')
            LEFT JOIN pg_class own_tbl ON own_tbl.oid = dep.refobjid
            LEFT JOIN pg_namespace own_ns ON own_ns.oid = own_tbl.relnamespace
            LEFT JOIN pg_attribute own_att ON own_att.attrelid = own_tbl.oid AND own_att.attnum = dep.refobjsubid
            WHERE {app_schema_filter} AND seq.relkind = 'S'
            """,
            ("schema_name", "object_name"),
        ),
        "custom_types": (
            f"""
            SELECT n.nspname AS schema_name, t.typname AS object_name,
                   t.typtype::text AS type_kind, t.typcategory::text AS category,
                   pg_catalog.format_type(t.typbasetype, NULL) AS base_type,
                   t.typnotnull AS not_null, pg_get_expr(t.typdefaultbin, 0) AS default_expression,
                   t.typcollation::regcollation::text AS collation,
                   enum_labels.labels AS enum_labels,
                   pg_catalog.format_type(rng.rngsubtype, NULL) AS range_subtype
            FROM pg_type t
            JOIN pg_namespace n ON n.oid = t.typnamespace
            LEFT JOIN pg_range rng ON rng.rngtypid = t.oid
            LEFT JOIN LATERAL (
                SELECT array_agg(e.enumlabel ORDER BY e.enumsortorder) AS labels
                FROM pg_enum e WHERE e.enumtypid = t.oid
            ) enum_labels ON true
            WHERE {app_schema_filter}
              AND (t.typtype IN ('d', 'e', 'r', 'm') OR (t.typtype = 'c' AND t.typrelid = 0))
            """,
            ("schema_name", "object_name"),
        ),
        "composite_type_columns": (
            f"""
            SELECT n.nspname AS schema_name, typ.typname AS type_name, a.attname AS column_name,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                   a.attnotnull AS not_null
            FROM pg_type typ
            JOIN pg_namespace n ON n.oid = typ.typnamespace
            JOIN pg_class c ON c.oid = typ.typrelid
            JOIN pg_attribute a ON a.attrelid = c.oid
            WHERE {app_schema_filter} AND typ.typtype = 'c' AND typ.typrelid <> 0
              AND a.attnum > 0 AND NOT a.attisdropped
              AND NOT (n.nspname = 'public' AND typ.typname = 'alembic_version')
            """,
            ("schema_name", "type_name", "column_name"),
        ),
        "policies": (
            """
            SELECT schemaname AS schema_name, tablename AS table_name,
                   policyname AS object_name, permissive, roles, cmd AS command,
                   qual AS using_expression, with_check AS check_expression
            FROM pg_policies
            WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
              AND schemaname NOT LIKE 'pg_toast%' AND schemaname NOT LIKE 'pg_temp_%'
              AND NOT (schemaname = 'public' AND tablename = 'alembic_version')
            """,
            ("schema_name", "table_name", "object_name"),
        ),
        "rules": (
            f"""
            SELECT n.nspname AS schema_name, c.relname AS table_name,
                   rw.rulename AS object_name, pg_get_ruledef(rw.oid, true) AS definition
            FROM pg_rewrite rw
            JOIN pg_class c ON c.oid = rw.ev_class
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE {app_rel_filter} AND rw.rulename <> '_RETURN'
            """,
            ("schema_name", "table_name", "object_name"),
        ),
        "extensions": (
            """
            SELECT e.extname AS object_name, e.extversion AS version,
                   n.nspname AS schema_name
            FROM pg_extension e
            JOIN pg_namespace n ON n.oid = e.extnamespace
            """,
            ("object_name",),
        ),
        "partition_links": (
            f"""
            SELECT parent_ns.nspname AS parent_schema, parent.relname AS parent_table,
                   child_ns.nspname AS child_schema, child.relname AS child_table,
                   pg_get_expr(child.relpartbound, child.oid, true) AS partition_bound
            FROM pg_inherits inh
            JOIN pg_class parent ON parent.oid = inh.inhparent
            JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
            JOIN pg_class child ON child.oid = inh.inhrelid
            JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
            WHERE ({schema_filter('parent_ns')})
              AND ({schema_filter('child_ns')})
            """,
            ("parent_schema", "parent_table", "child_schema", "child_table"),
        ),
    }

    categories = {}
    for category, (statement, key_fields) in queries.items():
        rows = _query(connection, statement)
        if category == "functions":
            for row in rows:
                row["definition"] = _normalize_function_comments(row["definition"])
        if category == "triggers":
            # PostgreSQL assigns OID suffixes to internal FK trigger names;
            # those identifiers vary across independently built databases.
            for row in rows:
                if row["is_internal"]:
                    row["object_name"] = re.sub(
                        r"(RI_ConstraintTrigger_[ac]_)[0-9]+$|Unique_ConstraintTrigger_[0-9]+$",
                        lambda match: (
                            f"{match.group(1)}<oid>"
                            if match.group(1)
                            else "Unique_ConstraintTrigger_<oid>"
                        ),
                        row["object_name"],
                    )
                    row["definition"] = re.sub(
                        r"RI_ConstraintTrigger_[ac]_[0-9]+|Unique_ConstraintTrigger_[0-9]+",
                        lambda match: re.sub(r"_[0-9]+$", "_<oid>", match.group(0)),
                        row["definition"],
                    )
        categories[category] = _keyed(rows, key_fields)
    return categories


def _definition(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, indent=2)


def _report_counts(label: str, snapshot: dict[str, dict[tuple[str, ...], dict[str, Any]]]) -> None:
    tables = sum(
        1 for value in snapshot["relations"].values()
        if value.get("relation_kind") in {"r", "p", "f"}
    )
    constraint_types = {"p": 0, "f": 0, "u": 0, "c": 0, "x": 0}
    for value in snapshot["constraints"].values():
        kind = value.get("constraint_kind")
        if kind in constraint_types:
            constraint_types[kind] += 1
    print(
        f"{label}: schemas={len(snapshot['schemas'])}, tables={tables}, "
        f"columns={len(snapshot['columns'])}, PK={constraint_types['p']}, "
        f"FK={constraint_types['f']}, UNIQUE={constraint_types['u']}, "
        f"CHECK={constraint_types['c']}, EXCLUDE={constraint_types['x']}, "
        f"indexes={len(snapshot['indexes'])}, triggers={len(snapshot['triggers'])}, "
        f"functions/procedures/aggregates={len(snapshot['functions'])}, "
        f"views={len(snapshot['views'])}, sequences={len(snapshot['sequences'])}, "
        f"custom_types={len(snapshot['custom_types'])}, policies={len(snapshot['policies'])}"
    )


def _compare(
    direct: dict[str, dict[tuple[str, ...], dict[str, Any]]],
    alembic: dict[str, dict[tuple[str, ...], dict[str, Any]]],
) -> int:
    difference_count = 0
    for category, direct_objects in direct.items():
        alembic_objects = alembic[category]
        for key in sorted(set(direct_objects) | set(alembic_objects)):
            direct_value = direct_objects.get(key)
            alembic_value = alembic_objects.get(key)
            if direct_value == alembic_value:
                continue
            difference_count += 1
            print(f"REAL_SCHEMA_DRIFT [{category}] object={key!r}")
            print("  Database A:", _definition(direct_value))
            print("  Database B:", _definition(alembic_value))
    return difference_count


def _run() -> int:
    _configure_postgres_path()
    heads = _verify_alembic_graph()
    print(f"Alembic graph: one head {heads[0]}; root revision 0001 starts at base.")

    import psycopg2
    from psycopg2 import sql
    from tests.postgresql_compat import create_test_postgresql

    pg = create_test_postgresql()
    created: set[str] = set()
    cluster_dsn: dict[str, Any] | None = None
    cleanup_errors: list[BaseException] = []
    try:
        cluster_dsn = pg.dsn()
        suffix = uuid4().hex[:12]
        direct_name = f"flussra_g02d_direct_{suffix}"
        alembic_name = f"flussra_g02d_alembic_{suffix}"
        if not all(OWNED_DATABASE.fullmatch(name) for name in (direct_name, alembic_name)):
            raise RuntimeError("Generated disposable database names failed the ownership pattern")

        print(f"Owned temporary PostgreSQL cluster: {cluster_dsn['host']}:{cluster_dsn['port']}")
        for name in (direct_name, alembic_name):
            _create_database(cluster_dsn, name, created)
            print(f"Created task-owned database: {name}")

        file_count, seed_count = _build_direct_database(cluster_dsn, direct_name)
        print(
            f"Database A direct bootstrap: {file_count} sorted SQL files plus "
            f"{seed_count} existing conftest seed statements; complete."
        )

        final_revision = _run_alembic(cluster_dsn, alembic_name)
        print(f"Database B Alembic migration: complete at {final_revision}.")

        direct_connection = psycopg2.connect(**_connect_kwargs(cluster_dsn, direct_name))
        alembic_connection = psycopg2.connect(**_connect_kwargs(cluster_dsn, alembic_name))
        try:
            direct_snapshot = _snapshot(direct_connection)
            alembic_snapshot = _snapshot(alembic_connection)
        finally:
            direct_connection.close()
            alembic_connection.close()

        print("Catalog snapshots generated for both databases.")
        print("Explicit exclusion: public.alembic_version and its own catalog objects (Alembic bookkeeping).")
        _report_counts("Database A", direct_snapshot)
        _report_counts("Database B", alembic_snapshot)
        differences = _compare(direct_snapshot, alembic_snapshot)
        if differences:
            print(f"Comparison complete: {differences} real schema difference(s).")
            return 1
        print("NO_APPLICATION_SCHEMA_DIFFERENCES")
        print("Comparison complete: zero unexplained differences.")
        return 0
    finally:
        for name in sorted(created):
            try:
                if not OWNED_DATABASE.fullmatch(name):
                    raise RuntimeError(f"Refusing cleanup outside G0.2D database name pattern: {name}")
                if cluster_dsn is None:
                    raise RuntimeError("No admin connection is available for owned database cleanup")
                connection = psycopg2.connect(**cluster_dsn)
                try:
                    connection.autocommit = True
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
                        if cursor.fetchone():
                            cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
                            print(f"Dropped task-owned database: {name}")
                finally:
                    connection.close()
            except BaseException as exc:  # noqa: BLE001 - cleanup must continue after any failure
                cleanup_errors.append(exc)
        try:
            _stop_owned_postgresql(pg)
            print("Stopped and removed the invocation-owned PostgreSQL cluster.")
        except BaseException as exc:  # noqa: BLE001 - cleanup must continue after any failure
            cleanup_errors.append(exc)
        if cleanup_errors:
            for error in cleanup_errors:
                print(f"CLEANUP ERROR: {error}", file=sys.stderr)
            raise RuntimeError("G0.2D disposable database cleanup was incomplete")


def main() -> int:
    try:
        return _run()
    except Exception as exc:  # noqa: BLE001 - CLI reports an actionable failure and exits nonzero
        print(f"G0.2D comparison failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
