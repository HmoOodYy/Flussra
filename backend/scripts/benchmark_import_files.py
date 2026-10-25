"""Generate supported C2 file shapes and record repeatable parser/API measurements.

Run from ``backend`` with the worktree's venv and process TEMP/TMP set to the
repository validation temp (``C:\\Temp`` on Windows). The default output is the
ignored repository artifact ``.artifacts/import-benchmark.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import ctypes
import ctypes.wintypes
import json
import os
import platform
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
BOUNDARY = "flussra-import-benchmark"
UPLOAD_PATH_TEMPLATE = "/payroll/periods/{period_id}/imports"
ORDINARY_GET_PATH_TEMPLATE = "/payroll/periods/{period_id}/day-grid"


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
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--inspect-child", nargs=2, metavar=("PATH", "FILENAME"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inspect_child:
        result = _measure_inspection_child(Path(args.inspect_child[0]), args.inspect_child[1])
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    evidence = _run(args.output.resolve())
    print(json.dumps(evidence, indent=2, ensure_ascii=False))
    print(f"Raw benchmark evidence: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
