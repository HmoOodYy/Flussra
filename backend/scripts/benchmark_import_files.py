"""Generate supported C2 file shapes and record repeatable parser/API measurements.

Run from ``backend`` with the worktree's venv and process TEMP/TMP set to the
repository validation temp (``C:\\Temp`` on Windows). The default output is the
ignored repository artifact ``.artifacts/import-benchmark.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import csv
import ctypes
import ctypes.wintypes
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
DEFAULT_OUTPUT = ROOT / ".artifacts" / "import-benchmark.json"
DEFAULT_PREVIEW_OUTPUT = ROOT / ".artifacts" / "import-preview-benchmark.json"
DEFAULT_GUARD_PROFILE_OUTPUT = ROOT / ".artifacts" / "import-preview-guard-profile.json"
DEFAULT_NEAR_UPPER_PREVIEW_OUTPUT = ROOT / ".artifacts" / "import-preview-near-upper.json"
DEFAULT_PERSISTENCE_DIAGNOSTIC_OUTPUT = ROOT / ".artifacts" / "import-persistence-diagnostic.json"
BOUNDARY = "flussra-import-benchmark"
UPLOAD_PATH_TEMPLATE = "/payroll/periods/{period_id}/imports"
ORDINARY_GET_PATH_TEMPLATE = "/payroll/periods/{period_id}/day-grid"
PREVIEW_ORDINARY_GET_PATH_TEMPLATE = "/payroll/periods/{period_id}"


def _working_set() -> dict[str, int] | None:
    if os.name != "nt":
        try:
            import resource

            return {"peak_rss_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)}
        except (ImportError, AttributeError):
            return None

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = (
        ctypes.wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        ctypes.wintypes.DWORD,
    )
    psapi.GetProcessMemoryInfo.restype = ctypes.wintypes.BOOL
    process = kernel32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(
        process, ctypes.byref(counters), counters.cb
    ):
        return None
    return {
        "working_set_bytes": int(counters.WorkingSetSize),
        "peak_working_set_bytes": int(counters.PeakWorkingSetSize),
        "peak_pagefile_bytes": int(counters.PeakPagefileUsage),
    }


def _temp_snapshot() -> set[str]:
    root = Path(tempfile.gettempdir())
    try:
        return {item.name for item in root.iterdir()}
    except OSError:
        return set()


def _measure_inspection_child(path: Path, filename: str) -> dict[str, Any]:
    from app.imports.errors import ImportFailure
    from app.imports.files import inspect_file

    before_memory = _working_set()
    before_temp = _temp_snapshot()
    data = path.read_bytes()
    started = time.perf_counter()
    metadata = None
    error = None
    try:
        metadata = inspect_file(data, filename)
    except ImportFailure as exc:
        error = {"code": exc.code, "status_code": exc.status_code}
    elapsed = time.perf_counter() - started
    after_temp = _temp_snapshot()
    result = {
        "parser_seconds": round(elapsed, 6),
        "file_bytes": len(data),
        "working_set_before": before_memory,
        "working_set_after": _working_set(),
        "parser_temp_files_created": sorted(after_temp - before_temp),
    }
    if error is not None:
        result["error"] = error
    else:
        result["metadata"] = metadata
    return result


def _run_inspection_child(path: Path, filename: str) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(BACKEND) + os.pathsep + environment.get("PYTHONPATH", "")
    with tempfile.TemporaryDirectory(
        prefix="flussra-import-parser-child-", dir=tempfile.gettempdir()
    ) as child_temp:
        environment["TEMP"] = child_temp
        environment["TMP"] = child_temp
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--inspect-child", str(path), filename],
            cwd=BACKEND,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
    if result.returncode:
        raise RuntimeError(
            f"Parser measurement failed for {filename} ({result.returncode}):\n{result.stdout}\n{result.stderr}"
        )
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Parser measurement did not emit JSON: {result.stdout}\n{result.stderr}") from exc


def _workbook_file(path: Path, *, rows: int, columns: int, row_labels: bool = True) -> None:
    from openpyxl import Workbook

    workbook = Workbook(write_only=True)
    worksheet = workbook.create_sheet("Payroll")
    if row_labels:
        header = ["Driver Number", "Work Date"] + [f"Quantity {index}" for index in range(1, columns - 1)]
    else:
        header = ["Value"] * columns
    worksheet.append(header)
    for row_index in range(1, rows):
        values = [f"{row_index:05d}", "2026-10-20"] if row_labels else []
        values.extend([1] * (columns - len(values)))
        worksheet.append(values)
    workbook.save(path)


def _text_heavy_workbook(path: Path, *, rows: int = 118, columns: int = 50, text_bytes: int = 8_000) -> None:
    from openpyxl import Workbook

    workbook = Workbook(write_only=True)
    selected = workbook.create_sheet("Selected")
    selected.append(["Driver Number", "Work Date", "Quantity"])
    selected.append(["0012", "2026-10-20", 1])
    heavy = workbook.create_sheet("TextHeavy")
    heavy.sheet_state = "hidden"
    value = "x" * text_bytes
    for _row in range(rows):
        heavy.append([value] * columns)
    workbook.save(path)


def _csv_file(path: Path, *, rows: int, columns: int) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["Driver Number", "Work Date"] + [f"Quantity {index}" for index in range(1, columns - 1)])
        for row_index in range(1, rows):
            writer.writerow([f"{row_index:05d}", "2026-10-20"] + [1] * (columns - 2))


def _append_empty_members(path: Path, total_members: int) -> None:
    with zipfile.ZipFile(path, "r") as archive:
        member_count = len(archive.infolist())
    if member_count > total_members:
        raise ValueError(f"Workbook already has {member_count} members, requested total is {total_members}.")
    with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_STORED) as archive:
        for index in range(total_members - member_count):
            archive.writestr(f"benchmark-padding/member-{index:04d}.bin", b"")


def _append_nested_xml(path: Path, *, depth: int) -> None:
    document = b"<n>" + b"<n>" * (depth - 1) + b"</n>" * depth
    with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("custom/structural-depth.xml", document)


def _file_facts(path: Path, *, rows: int, columns: int, file_type: str) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "filename": path.name,
        "file_bytes": path.stat().st_size,
        "shape": {"rows": rows, "columns": columns, "rectangular_cells": rows * columns},
        "zip_members": 0,
        "actual_decompressed_bytes": 0,
    }
    if file_type == "xlsx":
        from xml.parsers import expat

        with zipfile.ZipFile(path, "r") as archive:
            facts["zip_members"] = len(archive.infolist())
            decompressed = 0
            max_xml_depth = 0
            max_xml_depth_member = None
            for info in archive.infolist():
                if info.is_dir():
                    continue
                parser = None
                depth_state = {"current": 0, "max": 0}
                if info.filename.lower().endswith((".xml", ".rels")):
                    parser = expat.ParserCreate()

                    def start_element(_name: str, _attributes: dict[str, str]) -> None:
                        depth_state["current"] += 1
                        depth_state["max"] = max(depth_state["max"], depth_state["current"])

                    def end_element(_name: str) -> None:
                        depth_state["current"] -= 1

                    parser.StartElementHandler = start_element
                    parser.EndElementHandler = end_element
                with archive.open(info) as member:
                    while chunk := member.read(128 * 1024):
                        decompressed += len(chunk)
                        if parser is not None:
                            parser.Parse(chunk, False)
                    if parser is not None:
                        parser.Parse(b"", True)
                        if depth_state["max"] > max_xml_depth:
                            max_xml_depth = depth_state["max"]
                            max_xml_depth_member = info.filename
            facts["actual_decompressed_bytes"] = decompressed
            facts["max_xml_depth"] = max_xml_depth
            facts["max_xml_depth_member"] = max_xml_depth_member
    else:
        facts["actual_decompressed_bytes"] = facts["file_bytes"]
    return facts


def _generate_cases(directory: Path) -> list[dict[str, Any]]:
    cases = []

    representative = directory / "representative-1000x10.xlsx"
    _workbook_file(representative, rows=1_000, columns=10)
    cases.append({
        **_file_facts(representative, rows=1_000, columns=10, file_type="xlsx"),
        "kind": "representative_xlsx",
        "file_type": "xlsx",
        "path": str(representative),
    })

    near_xlsx = directory / "near-upper-dense-20000x50.xlsx"
    _workbook_file(near_xlsx, rows=20_000, columns=50)
    cases.append({
        **_file_facts(near_xlsx, rows=20_000, columns=50, file_type="xlsx"),
        "kind": "near_upper_dense_xlsx",
        "file_type": "xlsx",
        "path": str(near_xlsx),
    })

    near_csv = directory / "near-upper-dense-20000x50.csv"
    _csv_file(near_csv, rows=20_000, columns=50)
    cases.append({
        **_file_facts(near_csv, rows=20_000, columns=50, file_type="csv"),
        "kind": "near_upper_dense_csv",
        "file_type": "csv",
        "path": str(near_csv),
    })

    text_heavy = directory / "text-heavy-5900x8000.xlsx"
    _text_heavy_workbook(text_heavy)
    cases.append({
        **_file_facts(text_heavy, rows=118, columns=50, file_type="xlsx"),
        "kind": "whole_workbook_text_heavy_xlsx",
        "file_type": "xlsx",
        "unselected_sheet_shape": {"rows": 118, "columns": 50, "rectangular_cells": 5_900},
        "cell_text_bytes": 8_000,
        "path": str(text_heavy),
    })

    member_stress = directory / "member-stress-4096.xlsx"
    _workbook_file(member_stress, rows=2, columns=3)
    _append_empty_members(member_stress, 4_096)
    cases.append({
        **_file_facts(member_stress, rows=2, columns=3, file_type="xlsx"),
        "kind": "member_ceiling_4096_xlsx",
        "file_type": "xlsx",
        "path": str(member_stress),
    })

    structural_limit = directory / "structural-depth-256.xlsx"
    _workbook_file(structural_limit, rows=2, columns=3)
    _append_nested_xml(structural_limit, depth=256)
    cases.append({
        **_file_facts(structural_limit, rows=2, columns=3, file_type="xlsx"),
        "kind": "structural_depth_boundary_256_xlsx",
        "file_type": "xlsx",
        "path": str(structural_limit),
    })

    structural_over = directory / "structural-depth-257.xlsx"
    _workbook_file(structural_over, rows=2, columns=3)
    _append_nested_xml(structural_over, depth=257)
    cases.append({
        **_file_facts(structural_over, rows=2, columns=3, file_type="xlsx"),
        "kind": "structural_depth_over_limit_257_xlsx",
        "file_type": "xlsx",
        "expected_parser_error_code": "IMPORT_XLSX_XML_DEPTH_LIMIT_EXCEEDED",
        "path": str(structural_over),
    })

    for case in cases:
        if case["file_bytes"] > 10_000_000:
            raise ValueError(f"Generated supported-shape file exceeds 10 MB: {case['filename']}.")
        if case["actual_decompressed_bytes"] > 50_000_000:
            raise ValueError(f"Generated supported-shape workbook exceeds 50 MB: {case['filename']}.")
    return cases


def _find_postgres_bin() -> str:
    import glob

    candidates = sorted(glob.glob(r"C:\Program Files\PostgreSQL\*\bin"), reverse=True)
    for candidate in candidates:
        if Path(candidate, "initdb.exe").is_file():
            return candidate
    raise RuntimeError("PostgreSQL initdb.exe was not found under C:\\Program Files\\PostgreSQL.")


@contextmanager
def _private_database():
    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/configuration_only")
    os.environ.setdefault("SECRET_KEY", "c2-import-benchmark-test-secret-not-for-production-000000000000")
    os.environ.setdefault("ENVIRONMENT", "test")
    from tests.conftest import _stop_test_postgresql_safely
    from tests.database_templates import SchemaDatabases
    from tests.postgresql_compat import create_test_postgresql

    os.environ["PATH"] = _find_postgres_bin() + os.pathsep + os.environ.get("PATH", "")
    cluster = create_test_postgresql()
    cluster.start()
    databases = SchemaDatabases(cluster.dsn(), ROOT)
    try:
        with databases.database("import_benchmark") as dsn:
            yield dsn
    finally:
        databases.close()
        _stop_test_postgresql_safely(cluster)


async def _application_benchmarks(dsn: dict, cases: list[dict[str, Any]]) -> dict[str, Any]:
    import httpx
    from httpx import ASGITransport
    from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

    from app.config import get_settings
    from app.dependencies import get_db
    from app.imports import service
    from app.main import create_app
    from tests.payroll_runtime_fixtures import build_payroll_tenant, open_period_with_grid

    database_url = (
        f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:{dsn['port']}/{dsn['database']}"
    )
    os.environ["DATABASE_URL"] = database_url
    os.environ["SECRET_KEY"] = "c2-import-benchmark-test-secret-not-for-production-000000000000"
    os.environ["ENVIRONMENT"] = "test"
    get_settings.cache_clear()
    engine = create_async_engine(database_url, echo=False)
    app = create_app()
    app.state.engine = engine

    async def _override_get_db() -> AsyncConnection:
        async with engine.begin() as connection:
            yield connection

    app.dependency_overrides[get_db] = _override_get_db
    tenant = build_payroll_tenant(dsn)
    result: dict[str, Any] = {"http_transport": "HTTPX ASGITransport against create_app()"}
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://benchmark.test"
        ) as client:
            period, _grid = await open_period_with_grid(client, engine, tenant)
            period_id = period["payroll_period_id"]
            endpoint = UPLOAD_PATH_TEMPLATE.format(period_id=period_id)
            get_endpoint = ORDINARY_GET_PATH_TEMPLATE.format(period_id=period_id)
            app_cases = [
                case for case in cases
                if case["kind"] in {"near_upper_dense_xlsx", "whole_workbook_text_heavy_xlsx"}
            ]
            scenario_results = []
            original_inspect = service.inspect_file
            for case in app_cases:
                started = threading.Event()
                completed = threading.Event()
                parser_temp: dict[str, list[str]] = {}

                def measured_inspect(*args, **kwargs):
                    previous_tempdir = tempfile.tempdir or tempfile.gettempdir()
                    try:
                        with tempfile.TemporaryDirectory(
                            prefix="flussra-import-http-parser-", dir=previous_tempdir
                        ) as parser_tempdir:
                            tempfile.tempdir = parser_tempdir
                            before = _temp_snapshot()
                            started.set()
                            try:
                                return original_inspect(*args, **kwargs)
                            finally:
                                parser_temp["created"] = sorted(_temp_snapshot() - before)
                    finally:
                        tempfile.tempdir = previous_tempdir
                        completed.set()

                service.inspect_file = measured_inspect
                data = Path(case["path"]).read_bytes()
                body = _multipart_body(data, case["filename"])
                headers = {
                    **tenant.admin,
                    "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
                    "Idempotency-Key": str(uuid4()),
                }

                async def send_upload():
                    began = time.perf_counter()
                    response = await client.post(endpoint, content=body, headers=headers)
                    return response, time.perf_counter() - began

                upload_task = asyncio.create_task(send_upload())
                await asyncio.wait_for(asyncio.to_thread(started.wait, 30), timeout=35)
                get_started = time.perf_counter()
                get_response = await client.get(get_endpoint, headers=tenant.admin)
                get_seconds = time.perf_counter() - get_started
                parser_still_running_at_get_complete = not completed.is_set()
                upload_response, upload_seconds = await upload_task
                service.inspect_file = original_inspect
                if upload_response.status_code != 200:
                    raise RuntimeError(
                        f"Benchmark upload for {case['filename']} returned "
                        f"{upload_response.status_code}: {upload_response.text}"
                    )
                if get_response.status_code != 200:
                    raise RuntimeError(
                        f"Authorized concurrent GET returned {get_response.status_code}: "
                        f"{get_response.text}"
                    )
                scenario_results.append({
                    "kind": case["kind"],
                    "http_upload_seconds": round(upload_seconds, 6),
                    "http_upload_status": upload_response.status_code,
                    "http_response_bytes": len(upload_response.content),
                    "parser_running_during_get": parser_still_running_at_get_complete,
                    "ordinary_authorized_get_path": get_endpoint,
                    "ordinary_authorized_get_status": get_response.status_code,
                    "ordinary_authorized_get_seconds": round(get_seconds, 6),
                    "parser_temp_files_created": parser_temp.get("created", []),
                })
            result["http_scenarios"] = scenario_results
    finally:
        service.inspect_file = original_inspect
        app.dependency_overrides.clear()
        await engine.dispose()
    return result


def _multipart_body(data: bytes, filename: str) -> bytes:
    return (
        f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
        + data
        + f"\r\n--{BOUNDARY}--\r\n".encode()
    )


def _preview_csv_file(
    path: Path,
    *,
    roster: list[dict[str, Any]],
    dates: list[str],
    definition_codes: list[str],
    data_rows: int,
    invalid_rows: int = 0,
    invalid_value: str = "not-a-number",
) -> None:
    """Write unique Driver/date facts with a real roster and frozen pay items."""
    if data_rows > len(roster) * len(dates):
        raise ValueError("Requested preview rows exceed the generated roster/date identities.")
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow([
            "DriverNumber", "DriverName", "WorkDate",
            *(f"Pay Item: {code}" for code in definition_codes),
        ])
        for index in range(data_rows):
            driver = roster[index // len(dates)]
            work_date = dates[index % len(dates)]
            quantities = ([invalid_value] * len(definition_codes)
                          if index < invalid_rows else ["1"] * len(definition_codes))
            writer.writerow([driver["drivernumber"], driver["drivername"], work_date, *quantities])


def _preview_xlsx_file(path: Path, *, roster: list[dict[str, Any]], dates: list[str],
                       definition_codes: list[str], data_rows: int) -> None:
    from openpyxl import Workbook

    if data_rows > len(roster) * len(dates):
        raise ValueError("Requested preview rows exceed the generated roster/date identities.")
    workbook = Workbook(write_only=True)
    worksheet = workbook.create_sheet("Payroll")
    worksheet.append(["DriverNumber", "DriverName", "WorkDate",
                      *(f"Pay Item: {code}" for code in definition_codes)])
    for index in range(data_rows):
        driver = roster[index // len(dates)]
        work_date = dates[index % len(dates)]
        worksheet.append([driver["drivernumber"], driver["drivername"], work_date,
                          *([1] * len(definition_codes))])
    workbook.save(path)


async def _create_preview_world(client, engine, tenant, *, driver_count: int = 2_858,
                                definition_count: int = 47) -> tuple[dict, list[dict], list[str], list[dict], dict]:
    """Create a period whose frozen roster and active target count fit the A2 workload."""
    import psycopg2
    from sqlalchemy import text

    from tests.compensation_api_fixtures import create_definition, grant_applicability
    from tests.compensation_database_fixtures import make_driver
    from tests.payroll_runtime_fixtures import assign_weekly_setup, create_period

    started = time.perf_counter()
    connection = psycopg2.connect(client_encoding="utf-8", **tenant.dsn)
    try:
        with connection.cursor() as cursor:
            for index in range(driver_count):
                assignment_id = make_driver(cursor, tenant.company_id, tenant.branch_a)
                cursor.execute("""
                    UPDATE core.employees e SET fullname = %s
                    FROM core.driverassignments a
                    WHERE a.driverassignmentid = %s AND a.employeeid = e.employeeid
                """, (f"Import Benchmark Driver {index:04d}", assignment_id))
            cursor.execute("""
                UPDATE core.employees e SET fullname = 'Benchmark Driver ' || d.drivernumber
                FROM core.drivers d
                WHERE d.employeeid = e.employeeid AND d.companyid = %s AND e.branchid = %s
            """, (tenant.company_id, tenant.branch_a))
        connection.commit()
    finally:
        connection.close()
    driver_setup_seconds = time.perf_counter() - started

    definitions = []
    definition_started = time.perf_counter()
    for index in range(definition_count):
        definition = await create_definition(
            client, tenant,
            definition_code=f"BM{index:03d}",
            definition_name=f"Benchmark Quantity {index:03d}",
        )
        grant_applicability(tenant, definition, tenant.branch_a)
        definitions.append(definition)
    definition_setup_seconds = time.perf_counter() - definition_started

    period_started = time.perf_counter()
    await assign_weekly_setup(engine, tenant, tenant.branch_a)
    period = await create_period(client, tenant, tenant.branch_a)
    period_setup_seconds = time.perf_counter() - period_started

    async with engine.connect() as connection:
        dates = [value.isoformat() for value in (await connection.execute(text("""
            SELECT workdate FROM payroll.payrollperioddays
            WHERE payrollperiodid = :pid ORDER BY workdate
        """), {"pid": period["payroll_period_id"]})).scalars()]
        roster = [dict(row) for row in (await connection.execute(text("""
            SELECT d.drivernumber, p.drivernamesnapshot AS drivername, a.driverassignmentid
            FROM payroll.payrollperioddrivereligibility p
            JOIN core.driverassignments a
              ON a.driverassignmentid = p.driverassignmentid AND a.companyid = p.companyid
            JOIN core.drivers d ON d.driverid = a.driverid AND d.companyid = a.companyid
            WHERE p.companyid = :cid AND p.payrollperiodid = :pid AND p.iseligibleforperiod
            ORDER BY d.drivernumber
        """), {"cid": tenant.company_id, "pid": period["payroll_period_id"]})).mappings()]
    if len(roster) < driver_count:
        raise RuntimeError(f"Generated {len(roster)} eligible drivers; expected at least {driver_count}.")
    if len(dates) != 7:
        raise RuntimeError(f"Generated a {len(dates)}-day period; benchmark requires seven work dates.")
    if len(definitions) != definition_count:
        raise RuntimeError(f"Generated {len(definitions)} definitions; expected {definition_count}.")
    return period, roster, dates, definitions, {
        "driver_count": len(roster), "frozen_definition_count": len(definitions),
        "period_days": len(dates), "driver_setup_seconds": round(driver_setup_seconds, 6),
        "definition_setup_seconds": round(definition_setup_seconds, 6),
        "period_setup_seconds": round(period_setup_seconds, 6),
    }


def _enable_guard_function_tracking(dsn: dict) -> None:
    import psycopg2
    from psycopg2.extensions import quote_ident

    connection = psycopg2.connect(client_encoding="utf-8", **dsn)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            database = quote_ident(dsn["database"], cursor)
            cursor.execute(f"ALTER DATABASE {database} SET track_functions = 'pl'")
    finally:
        connection.close()


async def _guard_function_stats(engine, *, reset: bool = False) -> dict[str, Any]:
    from sqlalchemy import text

    async with engine.begin() as connection:
        tracking = await connection.scalar(text("SELECT current_setting('track_functions')"))
        flush_supported = await connection.scalar(text(
            "SELECT to_regprocedure('pg_catalog.pg_stat_force_next_flush()') IS NOT NULL"
        ))
        reset_supported = await connection.scalar(text(
            "SELECT to_regprocedure('pg_catalog.pg_stat_reset_single_function_counters(oid)') IS NOT NULL"
        ))
        function_id = await connection.scalar(text(
            "SELECT to_regprocedure('import.fn_guard_payroll_work_detail()')::oid"
        ))
        if reset and reset_supported and function_id is not None:
            await connection.execute(
                text("SELECT pg_stat_reset_single_function_counters(:function_id)"),
                {"function_id": function_id},
            )
        if flush_supported:
            await connection.execute(text("SELECT pg_stat_force_next_flush()"))
        await connection.execute(text("SELECT pg_stat_clear_snapshot()"))
        row = (await connection.execute(text("""
            SELECT calls, total_time, self_time FROM pg_stat_user_functions
            WHERE schemaname = 'import' AND funcname = 'fn_guard_payroll_work_detail'
        """))).mappings().first()
        values = dict(row) if row else {"calls": 0, "total_time": 0.0, "self_time": 0.0}
    return {
        "track_functions": tracking,
        "flush_supported": bool(flush_supported),
        "reset_supported": bool(reset_supported),
        "function_found": function_id is not None,
        "values": values,
    }


async def _foreign_key_delete_probe(engine, *, company_id: int, import_id: int) -> dict[str, Any]:
    """Measure parent-delete FK probes after removing children, then roll back."""
    from sqlalchemy import text

    statements = {
        "driver_matches": """
            EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
            DELETE FROM import.payrollworkdrivermatches
            WHERE importid = :iid AND companyid = :cid
        """,
        "source_records": """
            EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
            DELETE FROM import.importsourcerecords
            WHERE importid = :iid AND companyid = :cid
        """,
    }
    children = (
        "payrollworkcells", "payrollworkrecordidentifiers", "importissues",
        "filesourcerecords", "payrollworkrecords",
    )
    parameters = {"iid": import_id, "cid": company_id}
    evidence: dict[str, Any] = {"rolled_back": True, "children_removed_before_probe": list(children)}
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(
                text("SELECT set_config('flussra.import_purge_id', :iid, TRUE)"),
                {"iid": str(import_id)},
            )
            for table in children:
                await connection.execute(text(
                    f"DELETE FROM import.{table} WHERE importid = :iid AND companyid = :cid"
                ), parameters)
            for label, statement in statements.items():
                raw_plan = (await connection.execute(text(statement), parameters)).scalar_one()
                plan = raw_plan[0] if isinstance(raw_plan, list) else raw_plan
                evidence[label] = {
                    "plan": plan.get("Plan"),
                    "triggers": plan.get("Triggers", []),
                    "execution_time_ms": plan.get("Planning Time", 0) + plan.get("Execution Time", 0),
                }
            indexes = (await connection.execute(text("""
                SELECT tablename, indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'import'
                  AND tablename IN ('importissues', 'payrollworkrecordidentifiers')
                ORDER BY tablename, indexname
            """))).mappings().all()
            evidence["relevant_child_indexes"] = [dict(row) for row in indexes]
        finally:
            await transaction.rollback()
    return evidence


async def _foreign_key_insert_probe(engine, *, company_id: int, import_id: int) -> dict[str, Any]:
    """Profile real target INSERT triggers/FKs, then roll back the sampled rows."""
    from sqlalchemy import text

    columns = (
        "sourcerecordid, importid, companyid, branchid, payrollperiodid, sourcecolumnindex, "
        "workdate, driverassignmentid, payrollperioddefinitionid, paydefinitionid, ratedefinitionid, "
        "rawvalue, quantity, isblank, outcome, currentvalue, hasstatus, heldstatuskeyid, isdayoff"
    )
    parameters = {"iid": import_id, "cid": company_id, "sample_count": 2048}
    evidence: dict[str, Any] = {"rolled_back": True, "requested_rows": parameters["sample_count"]}
    async with engine.connect() as connection:
        transaction = await connection.begin()
        started = time.perf_counter()
        try:
            await connection.execute(text(f"""
                CREATE TEMP TABLE benchmark_import_cell_insert_probe ON COMMIT DROP AS
                SELECT {columns}
                FROM import.payrollworkcells
                WHERE companyid = :cid AND importid = :iid
                ORDER BY sourcerecordid, sourcecolumnindex
                LIMIT :sample_count
            """), parameters)
            sample_count = await connection.scalar(text(
                "SELECT count(*) FROM pg_temp.benchmark_import_cell_insert_probe"
            ))
            evidence["sample_rows"] = sample_count
            if sample_count != parameters["sample_count"]:
                raise RuntimeError(
                    f"Insert trigger probe found {sample_count} rows; expected {parameters['sample_count']}."
                )
            deleted = await connection.execute(text("""
                DELETE FROM import.payrollworkcells target
                USING pg_temp.benchmark_import_cell_insert_probe sample
                WHERE target.sourcerecordid = sample.sourcerecordid
                  AND target.sourcecolumnindex = sample.sourcecolumnindex
            """))
            evidence["deleted_rows_for_probe"] = deleted.rowcount
            if deleted.rowcount != sample_count:
                raise RuntimeError(
                    f"Insert trigger probe removed {deleted.rowcount} rows; expected {sample_count}."
                )
            evidence["setup_seconds"] = round(time.perf_counter() - started, 6)
            explain_started = time.perf_counter()
            raw_plan = (await connection.execute(text(f"""
                EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
                INSERT INTO import.payrollworkcells ({columns})
                SELECT {columns} FROM pg_temp.benchmark_import_cell_insert_probe
            """))).scalar_one()
            plan = raw_plan[0] if isinstance(raw_plan, list) else raw_plan
            evidence["insert_seconds"] = round(time.perf_counter() - explain_started, 6)
            evidence["plan"] = plan.get("Plan")
            evidence["triggers"] = plan.get("Triggers", [])
            evidence["execution_time_ms"] = plan.get("Execution Time", 0)
            inserted_rows = await connection.scalar(text("""
                SELECT count(*)
                FROM import.payrollworkcells target
                JOIN pg_temp.benchmark_import_cell_insert_probe sample
                  ON target.sourcerecordid = sample.sourcerecordid
                 AND target.sourcecolumnindex = sample.sourcecolumnindex
            """))
            evidence["inserted_rows"] = inserted_rows
            if inserted_rows != sample_count:
                raise RuntimeError(
                    f"Insert trigger probe restored {inserted_rows} rows; expected {sample_count}."
                )
        finally:
            await transaction.rollback()
    return evidence


def _deep_python_size(value: Any) -> tuple[int, float]:
    """Estimate retained Python object bytes for bounded diagnostic samples only."""
    import sys

    started = time.perf_counter()
    seen: set[int] = set()
    pending = [value]
    total = 0
    while pending:
        item = pending.pop()
        item_id = id(item)
        if item_id in seen:
            continue
        seen.add(item_id)
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            pending.extend(item)
        elif hasattr(type(item), "__slots__"):
            pending.extend(getattr(item, slot) for slot in type(item).__slots__)
    return total, time.perf_counter() - started


def _select_preview_cases(generated: list[dict[str, Any]], *, profile_only: bool,
                          near_upper_csv_only: bool, persistence_diagnostic: bool,
                          diagnostic_with_upper: bool, diagnostic_upper_only: bool,
                          diagnostic_xlsx_only: bool) -> list[dict[str, Any]]:
    if profile_only:
        allowed = {"issue_volume_1000x50_47000_errors"}
    elif near_upper_csv_only:
        allowed = {"near_upper_19999x50_csv"}
    elif persistence_diagnostic:
        if diagnostic_xlsx_only:
            allowed = {"near_upper_19999x50_xlsx"}
        elif diagnostic_upper_only:
            allowed = {"near_upper_19999x50_csv", "near_upper_19999x50_xlsx"}
        else:
            allowed = {"representative_1000x10", "issue_volume_1000x50_47000_errors"}
        if diagnostic_with_upper and not diagnostic_upper_only:
            allowed |= {"near_upper_19999x50_csv", "near_upper_19999x50_xlsx"}
    else:
        return generated
    return [case for case in generated if case["kind"] in allowed]


async def _preview_application_benchmarks(dsn: dict, directory: Path, *,
                                          profile_only: bool = False,
                                          near_upper_csv_only: bool = False,
                                          persistence_diagnostic: bool = False,
                                          diagnostic_with_upper: bool = False,
                                          diagnostic_upper_only: bool = False,
                                          diagnostic_xlsx_only: bool = False) -> dict[str, Any]:
    import httpx
    from httpx import ASGITransport
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import get_settings
    from app.dependencies import get_db
    from app.imports import preview_service, preview_store
    from app.imports.retention import purge_payroll_work_import_details
    from app.main import create_app
    from tests.payroll_runtime_fixtures import build_payroll_tenant, force_status

    if profile_only or persistence_diagnostic:
        _enable_guard_function_tracking(dsn)

    database_url = (
        f"postgresql+asyncpg://{dsn['user']}@{dsn['host']}:{dsn['port']}/{dsn['database']}"
    )
    os.environ["DATABASE_URL"] = database_url
    os.environ["SECRET_KEY"] = "c2-import-benchmark-test-secret-not-for-production-000000000000"
    os.environ["ENVIRONMENT"] = "test"
    get_settings.cache_clear()
    engine = create_async_engine(database_url, echo=False)
    app = create_app()
    app.state.engine = engine

    async def override_get_db():
        async with engine.begin() as connection:
            yield connection

    app.dependency_overrides[get_db] = override_get_db
    tenant = build_payroll_tenant(dsn)
    diagnostic_originals = None
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://benchmark.test"
        ) as client:
            period, roster, dates, definitions, setup = await _create_preview_world(
                client, engine, tenant
            )
            directory.mkdir(parents=True, exist_ok=True)
            small_path = directory / "preview-representative-1000x10.csv"
            _preview_csv_file(
                small_path, roster=roster, dates=dates,
                definition_codes=[item["definition_code"] for item in definitions[:7]],
                data_rows=999,
            )
            full_path = directory / "preview-near-upper-19999x50.csv"
            _preview_csv_file(
                full_path, roster=roster, dates=dates,
                definition_codes=[item["definition_code"] for item in definitions],
                data_rows=19_999, invalid_rows=5,
            )
            full_xlsx_path = directory / "preview-near-upper-19999x50.xlsx"
            _preview_xlsx_file(
                full_xlsx_path, roster=roster, dates=dates,
                definition_codes=[item["definition_code"] for item in definitions],
                data_rows=19_999,
            )
            page_cap_path = directory / "preview-page-cap-10x50.csv"
            _preview_csv_file(
                page_cap_path, roster=roster, dates=dates,
                definition_codes=[item["definition_code"] for item in definitions],
                data_rows=10, invalid_rows=10, invalid_value="x" * 8_000,
            )
            issue_volume_path = directory / "preview-issue-volume-1000x50.csv"
            _preview_csv_file(
                issue_volume_path, roster=roster, dates=dates,
                definition_codes=[item["definition_code"] for item in definitions],
                data_rows=1_000, invalid_rows=1_000,
            )
            generated = []
            for path, row_count, column_count, definition_slice in (
                (small_path, 1_000, 10, definitions[:7]),
                (full_path, 20_000, 50, definitions),
                (full_xlsx_path, 20_000, 50, definitions),
                (page_cap_path, 11, 50, definitions),
                (issue_volume_path, 1_001, 50, definitions),
            ):
                is_xlsx = path.suffix == ".xlsx"
                generated.append({
                    **_file_facts(path, rows=row_count, columns=column_count,
                                  file_type="xlsx" if is_xlsx else "csv"),
                    "kind": (
                        "representative_1000x10" if path == small_path else
                        "near_upper_19999x50_xlsx" if is_xlsx else
                        "page_cap_10x50_8000_text" if path == page_cap_path else
                        "issue_volume_1000x50_47000_errors" if path == issue_volume_path else
                        "near_upper_19999x50_csv"
                    ),
                    "data_rows": row_count - 1,
                    "mapped_definition_count": len(definition_slice),
                    "definitions": definition_slice,
                    "path": path,
                })
                if (path.stat().st_size > 10_000_000
                        or generated[-1]["actual_decompressed_bytes"] > 50_000_000):
                    raise RuntimeError(f"Generated benchmark input exceeds an accepted file cap: {path.name}.")
            generated = _select_preview_cases(
                generated, profile_only=profile_only, near_upper_csv_only=near_upper_csv_only,
                persistence_diagnostic=persistence_diagnostic,
                diagnostic_with_upper=diagnostic_with_upper,
                diagnostic_upper_only=diagnostic_upper_only,
                diagnostic_xlsx_only=diagnostic_xlsx_only,
            )

            result: dict[str, Any] = {
                "setup": setup,
                "guard_profile_only": profile_only,
                "persistence_diagnostic": persistence_diagnostic,
                "diagnostic_with_upper": diagnostic_with_upper,
                "diagnostic_upper_only": diagnostic_upper_only,
                "diagnostic_xlsx_only": diagnostic_xlsx_only,
                "ordinary_get_path": PREVIEW_ORDINARY_GET_PATH_TEMPLATE.format(
                    period_id=period["payroll_period_id"]
                ),
                "scenarios": [],
            }
            ordinary_get_path = result["ordinary_get_path"]
            observers: list[dict[str, float]] = []
            app.state.import_preview_observer = lambda timings: observers.append(dict(timings))
            active_diagnostic = contextvars.ContextVar("import_diagnostic_operation", default=None)
            diagnostic_phase = contextvars.ContextVar("import_diagnostic_phase", default="persist")
            diagnostic_lock = threading.Lock()

            def record_metric(operation, phase, table, key, value):
                if operation is None:
                    return
                bucket = operation["database"].setdefault(f"{phase}:{table}", {})
                with diagnostic_lock:
                    bucket[key] = bucket.get(key, 0) + value

            class TimedConnection:
                def __init__(self, connection):
                    self.connection = connection

                def __getattr__(self, name):
                    return getattr(self.connection, name)

                async def execute(self, statement, parameters=None, *args, **kwargs):
                    sql = str(statement)
                    match = re.search(
                        r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+import\.([a-z0-9_]+)",
                        sql, re.IGNORECASE,
                    )
                    table = match.group(1).lower() if match else "other"
                    operation = active_diagnostic.get()
                    phase = diagnostic_phase.get()
                    started = time.perf_counter()
                    result_proxy = await self.connection.execute(statement, parameters, *args, **kwargs)
                    elapsed = time.perf_counter() - started
                    record_metric(operation, phase, table, "execute_calls", 1)
                    record_metric(operation, phase, table, "execute_wall_seconds", elapsed)
                    record_metric(operation, phase, table, "sql_text_bytes", len(sql.encode("utf-8")))
                    if isinstance(parameters, dict):
                        for value in parameters.values():
                            if isinstance(value, str):
                                record_metric(operation, phase, table, "bound_string_bytes",
                                              len(value.encode("utf-8")))
                            elif isinstance(value, (bytes, bytearray, memoryview)):
                                record_metric(operation, phase, table, "bound_binary_bytes", len(value))
                    rowcount = getattr(result_proxy, "rowcount", None)
                    if isinstance(rowcount, int) and rowcount > 0:
                        record_metric(operation, phase, table, "rows_affected", rowcount)
                    return result_proxy

            if persistence_diagnostic:
                original_copy = preview_store.copy_prepared
                original_clear = preview_store.clear_derived_state
                original_persist = preview_service.persist_preview
                original_prepare = preview_service.prepare_preview
                original_pickle_dump = preview_store.pickle.dump
                diagnostic_originals = (original_copy, original_clear, original_persist, original_prepare,
                                        original_pickle_dump)

                class TimedDriver:
                    def __init__(self, driver):
                        self.driver = driver

                    async def copy_records_to_table(self, table, *, records, **kwargs):
                        count = 0

                        def counted():
                            nonlocal count
                            for item in records:
                                count += 1
                                yield item

                        started = time.perf_counter()
                        result = await self.driver.copy_records_to_table(table, records=counted(), **kwargs)
                        operation = active_diagnostic.get()
                        record_metric(operation, "copy", table, "execute_wall_seconds", time.perf_counter() - started)
                        record_metric(operation, "copy", table, "execute_calls", 1)
                        record_metric(operation, "copy", table, "rows_affected", count)
                        return result

                async def diagnostic_copy(driver, table, *args):
                    started = time.perf_counter()
                    try:
                        return await original_copy(TimedDriver(driver), table, *args)
                    finally:
                        record_metric(active_diagnostic.get(), "copy", table, "copy_with_batch_read_seconds",
                                      time.perf_counter() - started)

                def timed_pickle_dump(value, stream, *args, **kwargs):
                    operation = active_diagnostic.get()
                    if operation is None:
                        return original_pickle_dump(value, stream, *args, **kwargs)
                    started = time.perf_counter()
                    cpu_started = time.thread_time()
                    byte_started = stream.tell()
                    try:
                        return original_pickle_dump(value, stream, *args, **kwargs)
                    finally:
                        elapsed = time.perf_counter() - started
                        cpu_elapsed = time.thread_time() - cpu_started
                        bucket = operation.setdefault("pickle_by_stream", {}).setdefault(
                            id(stream), {"batch_count": 0, "wall_seconds": 0.0,
                                         "thread_cpu_seconds": 0.0, "serialized_bytes": 0},
                        )
                        bucket["batch_count"] += 1
                        bucket["wall_seconds"] += elapsed
                        bucket["thread_cpu_seconds"] += cpu_elapsed
                        bucket["serialized_bytes"] += stream.tell() - byte_started

                def diagnostic_prepare(value):
                    started = time.perf_counter()
                    prepared = original_prepare(value)
                    operation = active_diagnostic.get()
                    if operation is not None:
                        operation["preparation_seconds"] = time.perf_counter() - started
                        operation["prepared_stream_bytes"] = {}
                        operation["prepared_stream_spooled_to_disk"] = {}
                        operation["prepare_pickle_by_table"] = {}
                        pickle_stats = operation.get("pickle_by_stream", {})
                        for table, (_, stream) in prepared.streams.items():
                            stream.seek(0, 2)
                            operation["prepared_stream_bytes"][table] = stream.tell()
                            operation["prepared_stream_spooled_to_disk"][table] = getattr(stream, "_rolled", None)
                            operation["prepare_pickle_by_table"][table] = pickle_stats.get(
                                id(stream), {"batch_count": 0, "wall_seconds": 0.0,
                                              "thread_cpu_seconds": 0.0, "serialized_bytes": 0},
                            )
                            stream.seek(0)
                    return prepared

                async def diagnostic_clear(db, cid, iid, *, reset_mapping=True):
                    phase_token = diagnostic_phase.set("clear")
                    started = time.perf_counter()
                    try:
                        return await original_clear(db, cid, iid, reset_mapping=reset_mapping)
                    finally:
                        operation = active_diagnostic.get()
                        if operation is not None:
                            operation["clear_derived_state_seconds"] = time.perf_counter() - started
                        diagnostic_phase.reset(phase_token)

                async def diagnostic_persist(db, cid, bid, pid, iid, preview):
                    operation = active_diagnostic.get()
                    started = time.perf_counter()
                    try:
                        return await original_persist(TimedConnection(db), cid, bid, pid, iid, preview)
                    finally:
                        if operation is not None:
                            operation["persist_preview_seconds"] = time.perf_counter() - started

                preview_store.copy_prepared = diagnostic_copy
                preview_store.pickle.dump = timed_pickle_dump
                preview_service.prepare_preview = diagnostic_prepare
                preview_store.clear_derived_state = diagnostic_clear
                preview_service.persist_preview = diagnostic_persist

            for case in generated:
                data = Path(case["path"]).read_bytes()
                endpoint = UPLOAD_PATH_TEMPLATE.format(period_id=period["payroll_period_id"])
                upload_headers = {
                    **tenant.admin,
                    "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
                    "Idempotency-Key": str(uuid4()),
                }
                before_temp = _temp_snapshot()
                before_memory = _working_set()
                upload_started = time.perf_counter()
                uploaded = await client.post(
                    endpoint, content=_multipart_body(data, Path(case["path"]).name),
                    headers=upload_headers,
                )
                upload_seconds = time.perf_counter() - upload_started
                if uploaded.status_code != 200:
                    raise RuntimeError(f"Preview benchmark upload failed: {uploaded.status_code} {uploaded.text}")
                import_id = uploaded.json()["import_id"]
                metadata = await client.get(f"/payroll/imports/{import_id}", headers=tenant.admin)
                if metadata.status_code != 200:
                    raise RuntimeError(f"Preview benchmark metadata read failed: {metadata.status_code} {metadata.text}")
                mapping_targets = {
                    item["code"]: item["target"] for item in metadata.json()["mapping_targets"]
                    if item["target"].startswith("Definition:")
                }
                mapping = {"1": "DriverNumber", "2": "DriverName", "3": "WorkDate"}
                for offset, definition in enumerate(case["definitions"], start=4):
                    mapping[str(offset)] = mapping_targets[definition["definition_code"]]

                started = threading.Event()
                completed = threading.Event()
                phase_events = {
                    name: (threading.Event(), threading.Event())
                    for name in ("materialization", "compute", "preparation", "persistence")
                }
                original_materialize = preview_service.materialize
                original_build_preview = preview_service.build_preview
                original_prepare_preview = preview_service.prepare_preview
                original_persist_preview = preview_service.persist_preview
                collect_representations = case["kind"] in {
                    "representative_1000x10", "issue_volume_1000x50_47000_errors"
                }
                diagnostic_results = []

                def new_diagnostic_operation(label):
                    return {
                        "operation": label,
                        "database": {},
                        "representations": {},
                        "collect_representations": collect_representations,
                    }

                def finish_diagnostic_operation(operation, guard_start, guard_end):
                    representations = operation["representations"]
                    sizes = {}
                    if operation["collect_representations"]:
                        for label, value in representations.items():
                            size, traversal = _deep_python_size(value)
                            sizes[label] = {
                                "estimated_deep_python_bytes": size,
                                "measurement_traversal_seconds": round(traversal, 6),
                            }
                    representations.clear()
                    return {
                        "operation": operation["operation"],
                        "clear_derived_state_seconds": round(
                            operation.get("clear_derived_state_seconds", 0.0), 6),
                        "persist_preview_seconds": round(
                            operation.get("persist_preview_seconds", 0.0), 6),
                        "database_by_phase_and_table": operation["database"],
                        "preparation_seconds": operation.get("preparation_seconds"),
                        "prepared_stream_bytes": operation.get("prepared_stream_bytes"),
                        "prepared_stream_spooled_to_disk": operation.get("prepared_stream_spooled_to_disk"),
                        "prepare_pickle_by_table": operation.get("prepare_pickle_by_table"),
                        "representation_memory": {
                            "deep_size_estimated": operation["collect_representations"],
                            "objects": sizes,
                            "prepared_json_bytes_by_table": {
                                key.split(":", 1)[-1]: value.get("serialized_json_bytes", 0)
                                for key, value in operation["database"].items()
                                if key.startswith("insert:")
                            },
                        },
                        "detail_guard_stats": {
                            "before": guard_start,
                            "after": guard_end,
                            "delta": ({
                                key: guard_end["values"][key] - guard_start["values"][key]
                                for key in ("calls", "total_time", "self_time")
                            } if guard_start and guard_end else None),
                        },
                    }

                def measured_materialize(*args, **kwargs):
                    started.set()
                    phase_events["materialization"][0].set()
                    try:
                        value = original_materialize(*args, **kwargs)
                        operation = active_diagnostic.get()
                        if operation is not None and operation["collect_representations"]:
                            operation["representations"]["materialized_rows"] = value
                        return value
                    finally:
                        completed.set()
                        phase_events["materialization"][1].set()

                def measured_build_preview(*args, **kwargs):
                    phase_events["compute"][0].set()
                    try:
                        value = original_build_preview(*args, **kwargs)
                        operation = active_diagnostic.get()
                        if operation is not None and operation["collect_representations"]:
                            operation["representations"]["preview_model"] = value
                        return value
                    finally:
                        phase_events["compute"][1].set()

                def measured_prepare_preview(*args, **kwargs):
                    phase_events["preparation"][0].set()
                    try:
                        return original_prepare_preview(*args, **kwargs)
                    finally:
                        phase_events["preparation"][1].set()

                async def measured_persist_preview(*args, **kwargs):
                    phase_events["persistence"][0].set()
                    try:
                        return await original_persist_preview(*args, **kwargs)
                    finally:
                        phase_events["persistence"][1].set()

                preview_service.materialize = measured_materialize
                preview_service.build_preview = measured_build_preview
                preview_service.prepare_preview = measured_prepare_preview
                preview_service.persist_preview = measured_persist_preview
                observers.clear()
                diagnostic_initial = new_diagnostic_operation("initial_mapping") if persistence_diagnostic else None
                diagnostic_token = (active_diagnostic.set(diagnostic_initial)
                                    if diagnostic_initial is not None else None)
                guard_before = (await _guard_function_stats(engine, reset=True)
                                if profile_only or persistence_diagnostic else None)
                mapping_start = time.perf_counter()
                print(f"[benchmark] {case['kind']} mapping start", file=sys.stderr, flush=True)
                mapping_task = asyncio.create_task(client.put(
                    f"/payroll/imports/{import_id}/mapping",
                    json={"expected_revision": 1, "column_mapping": mapping},
                    headers=tenant.admin,
                ))
                async def get_during_phase(name: str) -> dict[str, Any]:
                    phase_started, phase_completed = phase_events[name]
                    await asyncio.wait_for(asyncio.to_thread(phase_started.wait, 180), timeout=185)
                    active_at_start = not phase_completed.is_set()
                    get_started = time.perf_counter()
                    response = await client.get(ordinary_get_path, headers=tenant.admin)
                    elapsed = time.perf_counter() - get_started
                    return {
                        "phase": name, "status": response.status_code,
                        "response_bytes": len(response.content), "seconds": round(elapsed, 6),
                        "phase_active_at_request_start": active_at_start,
                        "phase_active_at_response": not phase_completed.is_set(),
                    }

                get_tasks = [asyncio.create_task(get_during_phase(name)) for name in phase_events]
                try:
                    await asyncio.wait_for(asyncio.to_thread(started.wait, 180), timeout=185)
                    mapped = await mapping_task
                    phase_gets = await asyncio.gather(*get_tasks)
                finally:
                    preview_service.materialize = original_materialize
                    preview_service.build_preview = original_build_preview
                    preview_service.prepare_preview = original_prepare_preview
                    preview_service.persist_preview = original_persist_preview
                mapping_seconds = time.perf_counter() - mapping_start
                print(f"[benchmark] {case['kind']} mapping complete ({mapping_seconds:.3f}s, HTTP {mapped.status_code})",
                      file=sys.stderr, flush=True)
                guard_after = (await _guard_function_stats(engine)
                               if profile_only or persistence_diagnostic else None)
                if mapped.status_code != 200:
                    raise RuntimeError(f"Preview benchmark mapping failed: {mapped.status_code} {mapped.text}")
                if not observers:
                    raise RuntimeError("The preview timing observer did not report this mapping operation.")
                if any(item["status"] != 200 for item in phase_gets):
                    raise RuntimeError(f"A concurrent phase GET failed: {phase_gets!r}")
                if diagnostic_initial is not None:
                    diagnostic_results.append(finish_diagnostic_operation(
                        diagnostic_initial, guard_before, guard_after,
                    ))
                plan = mapped.json()["fingerprints"]["plan"]
                revision = mapped.json()["revision"]
                rows_page = await client.get(
                    f"/payroll/imports/{import_id}/rows",
                    params={"expected_revision": revision, "plan_fingerprint": plan, "limit": 100},
                    headers=tenant.admin,
                )
                issues_page = await client.get(
                    f"/payroll/imports/{import_id}/issues",
                    params={"expected_revision": revision, "plan_fingerprint": plan,
                            "limit": 200, "severity": "Error"},
                    headers=tenant.admin,
                )
                if rows_page.status_code != 200 or issues_page.status_code != 200:
                    raise RuntimeError(
                        f"Preview page failed: rows={rows_page.status_code}, issues={issues_page.status_code}"
                    )
                initial_phase_seconds = observers[-1]
                refresh_result = None
                if persistence_diagnostic:
                    diagnostic_refresh = new_diagnostic_operation("refresh")
                    active_diagnostic.set(diagnostic_refresh)
                    observers.clear()
                    refresh_guard_before = await _guard_function_stats(engine, reset=True)
                    refresh_started = time.perf_counter()
                    print(f"[benchmark] {case['kind']} refresh start", file=sys.stderr, flush=True)
                    refreshed = await client.post(
                        f"/payroll/imports/{import_id}/refresh",
                        json={"expected_revision": revision}, headers=tenant.admin,
                    )
                    refresh_seconds = time.perf_counter() - refresh_started
                    print(f"[benchmark] {case['kind']} refresh complete ({refresh_seconds:.3f}s, HTTP {refreshed.status_code})",
                          file=sys.stderr, flush=True)
                    refresh_guard_after = await _guard_function_stats(engine)
                    if refreshed.status_code != 200:
                        raise RuntimeError(
                            f"Preview diagnostic refresh failed: {refreshed.status_code} {refreshed.text}"
                        )
                    diagnostic_results.append(finish_diagnostic_operation(
                        diagnostic_refresh, refresh_guard_before, refresh_guard_after,
                    ))
                    refresh_result = {
                        "status": refreshed.status_code,
                        "seconds": round(refresh_seconds, 6),
                        "phase_seconds": observers[-1] if observers else None,
                    }
                case_result = {
                    "kind": case["kind"], "file_bytes": len(data),
                    "upload_seconds": round(upload_seconds, 6), "upload_status": uploaded.status_code,
                    "mapping_seconds": round(mapping_seconds, 6), "mapping_status": mapped.status_code,
                    "mapping_phase_seconds": initial_phase_seconds,
                    "concurrent_ordinary_authorized_gets": phase_gets,
                    "row_page": {
                        "status": rows_page.status_code, "requested_limit": 100,
                        "returned_items": len(rows_page.json()["items"]),
                        "response_bytes": len(rows_page.content),
                        "next_offset": rows_page.json()["next_offset"],
                    },
                    "error_issue_page": {
                        "status": issues_page.status_code, "requested_limit": 200,
                        "filtered_count": issues_page.json()["filtered_count"],
                        "returned_items": len(issues_page.json()["items"]),
                        "response_bytes": len(issues_page.content),
                        "next_offset": issues_page.json()["next_offset"],
                    },
                    "summary": mapped.json()["summary"],
                    "working_set_before": before_memory,
                    "working_set_after": _working_set(),
                    "temp_files_created": sorted(_temp_snapshot() - before_temp),
                    "import_id": import_id,
                    "revision": revision,
                    "mapping_fingerprint": mapped.json()["fingerprints"]["mapping"],
                    "content_fingerprint": mapped.json()["fingerprints"]["content"],
                    "plan_fingerprint": plan,
                }
                if persistence_diagnostic:
                    case_result["persistence_diagnostic_operations"] = diagnostic_results
                    case_result["refresh"] = refresh_result
                    case_result["diagnostic_process_memory_after"] = _working_set()
                    active_diagnostic.reset(diagnostic_token)
                if profile_only:
                    before_values = guard_before["values"]
                    after_values = guard_after["values"]
                    case_result["detail_guard_profile"] = {
                        "before": guard_before,
                        "after": guard_after,
                        "delta": {
                            key: after_values[key] - before_values[key]
                            for key in ("calls", "total_time", "self_time")
                        },
                    }
                    if guard_after["track_functions"] != "pl" or not guard_after["function_found"]:
                        raise RuntimeError(f"PostgreSQL did not expose the detail guard function stats: {guard_after!r}")
                    if case_result["detail_guard_profile"]["delta"]["calls"] <= 0:
                        raise RuntimeError("The detail guard did not report calls for the mapped source workload.")
                if case_result["error_issue_page"]["returned_items"] > 200:
                    raise RuntimeError("Issue response exceeded the documented 200-item page limit.")
                if case["kind"] == "page_cap_10x50_8000_text":
                    row_pages = []
                    issue_pages = []
                    for page_kind, path, limit, destination in (
                        ("rows", f"/payroll/imports/{import_id}/rows", 100, row_pages),
                        ("issues", f"/payroll/imports/{import_id}/issues", 200, issue_pages),
                    ):
                        offset = 0
                        seen = 0
                        while True:
                            page_response = await client.get(
                                path,
                                params={"expected_revision": revision, "plan_fingerprint": plan,
                                        "offset": offset, "limit": limit,
                                        **({"severity": "Error"} if page_kind == "issues" else {})},
                                headers=tenant.admin,
                            )
                            if page_response.status_code != 200:
                                raise RuntimeError(
                                    f"Page-cap {page_kind} traversal failed: "
                                    f"{page_response.status_code} {page_response.text}"
                                )
                            body = page_response.json()
                            returned = len(body["items"])
                            if returned == 0 and body["next_offset"] is not None:
                                raise RuntimeError(f"Page-cap {page_kind} traversal made no progress.")
                            seen += returned
                            destination.append({
                                "offset": offset, "requested_limit": limit,
                                "returned_items": returned, "response_bytes": len(page_response.content),
                                "next_offset": body["next_offset"],
                            })
                            if body["next_offset"] is None:
                                if seen != body["filtered_count"]:
                                    raise RuntimeError(
                                        f"Page-cap {page_kind} traversal lost items: "
                                        f"seen={seen}, total={body['filtered_count']}"
                                    )
                                break
                            if body["next_offset"] <= offset:
                                raise RuntimeError(f"Page-cap {page_kind} traversal offset did not advance.")
                            offset = body["next_offset"]
                    case_result["page_cap_traversal"] = {
                        "rows": row_pages, "issues": issue_pages,
                        "row_total_seen": sum(item["returned_items"] for item in row_pages),
                        "issue_total_seen": sum(item["returned_items"] for item in issue_pages),
                        "row_byte_short_page_observed": any(
                            item["returned_items"] < item["requested_limit"] and item["next_offset"] is not None
                            for item in row_pages
                        ),
                    }
                    if not case_result["page_cap_traversal"]["row_byte_short_page_observed"]:
                        raise RuntimeError("The generated long-cell source did not exercise the row response byte cap.")
                result["scenarios"].append(case_result)
                case["import_id"] = import_id

            # The near-upper scenario intentionally creates five invalid rows
            # so that the actual 200-item Error page is exercised with data.
            period_id = period["payroll_period_id"]
            insert_probe_case = max(generated, key=lambda case: case["data_rows"])
            fk_insert_probe = (await _foreign_key_insert_probe(
                engine, company_id=tenant.company_id, import_id=insert_probe_case["import_id"],
            ) if profile_only or persistence_diagnostic else None)
            force_status(tenant, period_id, "Locked")
            fk_delete_probe = None
            if profile_only or persistence_diagnostic:
                probe_case = next(
                    (case for case in generated
                     if case["kind"] == "issue_volume_1000x50_47000_errors"),
                    max(generated, key=lambda case: case["data_rows"]),
                )
                fk_delete_probe = await _foreign_key_delete_probe(
                    engine, company_id=tenant.company_id, import_id=probe_case["import_id"],
                )
            purge_guard_before = (await _guard_function_stats(engine, reset=True)
                                  if profile_only or persistence_diagnostic else None)
            purge_started = time.perf_counter()
            print(f"[benchmark] retention purge start ({len(generated)} import(s))", file=sys.stderr, flush=True)
            async with engine.begin() as connection:
                await purge_payroll_work_import_details(
                    connection, company_id=tenant.company_id, period_id=period_id,
                    actor_id=tenant.owner,
                )
            purge_seconds = time.perf_counter() - purge_started
            print(f"[benchmark] retention purge complete ({purge_seconds:.3f}s)", file=sys.stderr, flush=True)
            purge_guard_after = (await _guard_function_stats(engine)
                                 if profile_only or persistence_diagnostic else None)
            imports = []
            detail_tables = (
                "importsourcerecords", "filesourcerecords", "importissues", "payrollworkrecords",
                "payrollworkdrivermatches", "payrollworkrecordidentifiers", "payrollworkcells",
            )
            async with engine.connect() as connection:
                for case in generated:
                    import_id = case["import_id"]
                    counts = {}
                    for table in detail_tables:
                        counts[table] = await connection.scalar(text(
                            f"SELECT count(*) FROM import.{table} WHERE companyid = :cid AND importid = :iid"
                        ), {"cid": tenant.company_id, "iid": import_id})
                    durable = (await connection.execute(text("""
                        SELECT i.status, i.revision, f.filecontent IS NULL AS content_purged,
                            f.inspectionmetadata IS NULL AS inspection_purged,
                            f.contentpurgedatutc IS NOT NULL AS purge_marked,
                            w.mappingfingerprint, w.contentfingerprint, w.planfingerprint
                        FROM import.imports i
                        JOIN import.filesources f USING (importid, companyid)
                        JOIN import.payrollworkimports w USING (importid, companyid)
                        WHERE i.companyid = :cid AND i.importid = :iid
                    """), {"cid": tenant.company_id, "iid": import_id})).mappings().one()
                    imports.append({"import_id": import_id, "detail_counts_after_purge": counts,
                                    "durable_metadata_after_purge": dict(durable)})
            result["purge"] = {
                "seconds": round(purge_seconds, 6), "period_status": "Locked",
                "imports": imports,
            }
            if profile_only or persistence_diagnostic:
                result["purge"]["foreign_key_insert_probe"] = fk_insert_probe
                result["purge"]["foreign_key_delete_probe"] = fk_delete_probe
                before_values = purge_guard_before["values"]
                after_values = purge_guard_after["values"]
                result["purge"]["detail_guard_profile"] = {
                    "before": purge_guard_before,
                    "after": purge_guard_after,
                    "delta": {
                        key: after_values[key] - before_values[key]
                        for key in ("calls", "total_time", "self_time")
                    },
                }
                if result["purge"]["detail_guard_profile"]["delta"]["calls"] <= 0:
                    raise RuntimeError("The detail guard did not report calls for purge deletes.")
            return result
    finally:
        app.dependency_overrides.clear()
        if diagnostic_originals is not None:
            (preview_store.copy_prepared, preview_store.clear_derived_state,
             preview_service.persist_preview, preview_service.prepare_preview,
             preview_store.pickle.dump) = diagnostic_originals
        await engine.dispose()


def _run_preview_only(output_path: Path, *, profile_only: bool = False,
                      near_upper_csv_only: bool = False,
                      persistence_diagnostic: bool = False,
                      diagnostic_with_upper: bool = False,
                      diagnostic_upper_only: bool = False,
                      diagnostic_xlsx_only: bool = False) -> dict[str, Any]:
    import openpyxl
    import starlette

    evidence: dict[str, Any] = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": {
            "python": sys.version, "platform": platform.platform(),
            "starlette": starlette.__version__, "openpyxl": openpyxl.__version__,
            "temp_directory": tempfile.gettempdir(),
        },
        "scope": "A2 real PayrollWork mapping, persistence, bounded pages, ordinary reads and retention purge",
        "persistence_diagnostic": persistence_diagnostic,
        "diagnostic_with_upper": diagnostic_with_upper,
        "diagnostic_upper_only": diagnostic_upper_only,
        "diagnostic_xlsx_only": diagnostic_xlsx_only,
        "diagnostic_measurement_notes": ({
            "db_execute_proxy": True,
            "binary_copy_and_owned_spool_instrumented": True,
            "representation_deep_size_enabled_only_for_representative_and_issue_heavy_shapes": True,
            "representation_size_traversal_runs_after_request_phase": True,
            "tracemalloc_enabled": False,
            "detail_guard_tracking": persistence_diagnostic,
        } if persistence_diagnostic else None),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="flussra-import-preview-benchmark-") as directory:
        with _private_database() as dsn:
            evidence["application"] = asyncio.run(
                _preview_application_benchmarks(
                    dsn, Path(directory), profile_only=profile_only,
                    near_upper_csv_only=near_upper_csv_only,
                    persistence_diagnostic=persistence_diagnostic,
                    diagnostic_with_upper=diagnostic_with_upper,
                    diagnostic_upper_only=diagnostic_upper_only,
                    diagnostic_xlsx_only=diagnostic_xlsx_only,
                )
            )
    output_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return evidence


def _run(output_path: Path) -> dict[str, Any]:
    import openpyxl
    import starlette

    evidence: dict[str, Any] = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "starlette": starlette.__version__,
            "openpyxl": openpyxl.__version__,
            "temp_directory": tempfile.gettempdir(),
        },
        "generation_seconds": {},
        "cases": [],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="flussra-import-benchmark-") as temporary_directory:
        generation_start = time.perf_counter()
        cases = _generate_cases(Path(temporary_directory))
        evidence["generation_seconds"]["all_cases"] = round(time.perf_counter() - generation_start, 6)
        for case in cases:
            parser_result = _run_inspection_child(Path(case["path"]), case["filename"])
            expected_error = case.get("expected_parser_error_code")
            actual_error = parser_result.get("error", {}).get("code")
            if actual_error != expected_error:
                raise RuntimeError(
                    f"Parser outcome for {case['filename']} was {actual_error!r}; "
                    f"expected {expected_error!r}."
                )
            if expected_error and parser_result["error"].get("status_code") != 422:
                raise RuntimeError(
                    f"Expected a 422 parser refusal for {case['filename']}, got {parser_result['error']!r}."
                )
            case["parser_result"] = parser_result
        ordinary_xlsx = [
            case for case in cases
            if case["file_type"] == "xlsx"
            and case["kind"] not in {
                "structural_depth_boundary_256_xlsx",
                "structural_depth_over_limit_257_xlsx",
            }
        ]
        accepted_depth = next(
            case for case in cases if case["kind"] == "structural_depth_boundary_256_xlsx"
        )
        rejected_depth = next(
            case for case in cases if case["kind"] == "structural_depth_over_limit_257_xlsx"
        )
        evidence["xml_depth_measurement"] = {
            "guard_limit_elements": 256,
            "ordinary_generated_max_depth": max(case["max_xml_depth"] for case in ordinary_xlsx),
            "ordinary_generated_max_depth_member": max(
                ordinary_xlsx, key=lambda case: case["max_xml_depth"]
            )["max_xml_depth_member"],
            "accepted_boundary_depth": accepted_depth["max_xml_depth"],
            "rejected_one_over_depth": rejected_depth["max_xml_depth"],
            "one_over_error": rejected_depth["parser_result"]["error"]["code"],
            "one_over_parser_seconds": rejected_depth["parser_result"]["parser_seconds"],
        }
        with _private_database() as dsn:
            evidence["application"] = asyncio.run(_application_benchmarks(dsn, cases))
        for case in cases:
            case.pop("path")
            evidence["cases"].append(case)
    output_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--preview-only", action="store_true",
                        help="Measure real PayrollWork preview staging, pages and retention only.")
    parser.add_argument("--guard-profile-only", action="store_true",
                        help="Profile the detail trigger around a generated 47k-issue mapping and purge.")
    parser.add_argument("--near-upper-csv-only", action="store_true",
                        help="Measure only the 19,999-row by 50-column CSV preview and retention case.")
    parser.add_argument("--persistence-diagnostic", action="store_true",
                        help="Instrument representative and 47k-issue mapping/refresh/purge persistence.")
    parser.add_argument("--diagnostic-with-upper", action="store_true",
                        help="Also instrument the 19,999-row by 50-column CSV and XLSX mapping/refresh cases.")
    parser.add_argument("--diagnostic-upper-only", action="store_true",
                        help="Instrument only the 19,999-row by 50-column CSV and XLSX mapping/refresh cases.")
    parser.add_argument("--diagnostic-xlsx-only", action="store_true",
                        help="Instrument only the 19,999-row by 50-column XLSX mapping, refresh and purge case.")
    parser.add_argument("--inspect-child", nargs=2, metavar=("PATH", "FILENAME"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.diagnostic_with_upper and not args.persistence_diagnostic:
        parser.error("--diagnostic-with-upper requires --persistence-diagnostic")
    if args.diagnostic_upper_only and not args.persistence_diagnostic:
        parser.error("--diagnostic-upper-only requires --persistence-diagnostic")
    if args.diagnostic_xlsx_only and not args.persistence_diagnostic:
        parser.error("--diagnostic-xlsx-only requires --persistence-diagnostic")
    if args.diagnostic_xlsx_only and (
        args.diagnostic_upper_only or args.diagnostic_with_upper
        or args.near_upper_csv_only or args.guard_profile_only
    ):
        parser.error("--diagnostic-xlsx-only cannot be combined with another diagnostic scenario selector")
    if args.inspect_child:
        result = _measure_inspection_child(Path(args.inspect_child[0]), args.inspect_child[1])
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    preview_only = (args.preview_only or args.guard_profile_only or args.near_upper_csv_only
                    or args.persistence_diagnostic)
    output_path = args.output or (
        DEFAULT_GUARD_PROFILE_OUTPUT if args.guard_profile_only else
        DEFAULT_NEAR_UPPER_PREVIEW_OUTPUT if args.near_upper_csv_only else
        DEFAULT_PERSISTENCE_DIAGNOSTIC_OUTPUT if args.persistence_diagnostic else
        DEFAULT_PREVIEW_OUTPUT if preview_only else DEFAULT_OUTPUT
    )
    evidence = (_run_preview_only(
        output_path.resolve(), profile_only=args.guard_profile_only,
        near_upper_csv_only=args.near_upper_csv_only,
        persistence_diagnostic=args.persistence_diagnostic,
        diagnostic_with_upper=args.diagnostic_with_upper,
        diagnostic_upper_only=args.diagnostic_upper_only,
        diagnostic_xlsx_only=args.diagnostic_xlsx_only,
    ) if preview_only
                else _run(output_path.resolve()))
    print(json.dumps(evidence, indent=2, ensure_ascii=False))
    print(f"Raw benchmark evidence: {output_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
