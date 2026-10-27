"""Measure compact multi-file/layout preview on disposable production-schema databases.

Run from backend with Python 3.12: python -m scripts.benchmark_import_files --scenario current
Named historical presets remain reference workloads and may exceed current product rules.
Each workload runs in its own process; peak working set includes fixture overhead.
No application database or public apply endpoint is used.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from scripts.import_benchmark_support import (
    application,
    memory_snapshot,
    private_database,
    seed_period,
)

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ("representative_csv", "monthly_daily_csv", "monthly_daily_xlsx", "rare_csv", "issue_heavy_csv")
CURRENT_SCENARIOS = ("current_daily_csv", "current_daily_xlsx", "current_daily_issue_csv")


def source_files(scene, fixture):
    definitions, drivers, days = fixture["definitions"], fixture["drivers"], fixture["days"]
    def csv_bytes(selected_definitions, selected_days, *, date_column):
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(["DriverNumber", *(["WorkDate"] if date_column else []),
                         *[definition.code for definition in selected_definitions]])
        for day in selected_days:
            for driver in drivers:
                writer.writerow([driver["drivernumber"], *([day.isoformat()] if date_column else []),
                    *[("invalid" if scene in ("issue_heavy_csv", "current_daily_issue_csv") else "1")
                      for _ in selected_definitions]])
        return buffer.getvalue().encode("utf-8")
    if scene == "monthly_daily_csv":
        # Two different layouts share a Driver/day grid; each supplies half the dynamic definitions.
        midpoint = len(definitions) // 2
        return [(f"{day.isoformat()}-family-{family}.csv", csv_bytes(group, [day], date_column=False))
                for day in days for family, group in enumerate((definitions[:midpoint], definitions[midpoint:]), 1)]
    if scene == "monthly_daily_xlsx":
        from openpyxl import Workbook
        book = Workbook(write_only=True)
        try:
            for day in days:
                sheet = book.create_sheet(day.isoformat())
                sheet.append(["DriverNumber", *[definition.code for definition in definitions]])
                for driver in drivers:
                    sheet.append([driver["drivernumber"], *[1 for _ in definitions]])
            buffer = io.BytesIO()
            book.save(buffer)
            return [("monthly.xlsx", buffer.getvalue())]
        finally:
            book.close()
    if scene in CURRENT_SCENARIOS:
        if scene == "current_daily_xlsx":
            from openpyxl import Workbook
            files = []
            for day in days:
                book = Workbook(write_only=True)
                try:
                    sheet = book.create_sheet(day.isoformat())
                    sheet.append(["DriverNumber", *[definition.code for definition in definitions]])
                    for driver in drivers:
                        sheet.append([driver["drivernumber"], *[1 for _ in definitions]])
                    buffer = io.BytesIO()
                    book.save(buffer)
                    files.append((f"{day.isoformat()}.xlsx", buffer.getvalue()))
                finally:
                    book.close()
            return files
        return [(f"{day.isoformat()}.csv", csv_bytes(definitions, [day], date_column=False))
                for day in days]
    return [("supplied.csv", csv_bytes(definitions, days, date_column=True))]


class Phases:
    def __init__(self):
        self.values = {}
        self.originals = []
        self.lock_started = None
        self.copies = {}

    def install(self):
        from app.imports import batch_service
        for name in ("_inspect_batch", "_materialize_batch", "build_batch_preview", "prepare_preview"):
            original = getattr(batch_service, name)
            def measured(*args, _original=original, _name=name, **kwargs):
                started = time.perf_counter()
                try:
                    return _original(*args, **kwargs)
                finally:
                    self.values[_name] = self.values.get(_name, 0) + time.perf_counter() - started
            self.originals.append((batch_service, name, original))
            setattr(batch_service, name, measured)
        from app.imports import preview_store
        original_copy = preview_store._copy_records
        async def measured_copy(db, table, columns, records):
            count = 0
            def counted():
                nonlocal count
                for row in records:
                    count += 1
                    yield row
            started = time.perf_counter()
            await original_copy(db, table, columns, counted())
            self.values["copy_" + table] = time.perf_counter() - started
            self.copies[table] = {"records": count, "batches": (count + 999) // 1000}
        self.originals.append((preview_store, "_copy_records", original_copy))
        preview_store._copy_records = measured_copy
        for name in ("load_context", "persist_preview", "clear_derived_state", "_lock_mutable"):
            original = getattr(batch_service, name)
            async def measured(*args, _original=original, _name=name, **kwargs):
                started = time.perf_counter()
                try:
                    result = await _original(*args, **kwargs)
                    if _name == "_lock_mutable":
                        self.lock_started = time.perf_counter()
                        args[0].info["measured_import_lock"] = self.lock_started
                    return result
                finally:
                    self.values[_name] = self.values.get(_name, 0) + time.perf_counter() - started
            self.originals.append((batch_service, name, original))
            setattr(batch_service, name, measured)

    def reset(self):
        self.values = {}
        self.lock_started = None
        self.copies = {}

    def restore(self):
        for module, name, original in self.originals:
            setattr(module, name, original)


async def timed_request(client, path, headers, send, phases):
    phases.reset()
    baseline_started = time.perf_counter()
    ordinary = await client.get(path, headers=headers)
    assert ordinary.status_code == 200, ordinary.text
    baseline = time.perf_counter() - baseline_started
    probes = []
    started = time.perf_counter()
    pending = asyncio.create_task(send())
    while not pending.done():
        before = time.perf_counter()
        ordinary = await client.get(path, headers=headers)
        probes.append(time.perf_counter() - before)
        assert ordinary.status_code == 200, ordinary.text
        if not pending.done():
            await asyncio.sleep(0.05)
    response = await pending
    finished = time.perf_counter()
    assert response.status_code == 200, response.text
    return response.json(), {"request_seconds": finished - started, "phases_seconds": dict(phases.values),
        "copy": dict(phases.copies),
        "locked_to_response_seconds": finished - phases.lock_started if phases.lock_started else None,
        "ordinary_get_baseline_seconds": baseline, "ordinary_get_count": len(probes),
        "ordinary_get_median_seconds": statistics.median(probes) if probes else None,
        "ordinary_get_max_seconds": max(probes) if probes else None, "memory": memory_snapshot()}


def _file_shapes(files):
    """Describe generated upload tables; this is not canonical extraction evidence."""
    shapes = []
    for name, payload in files:
        if name.endswith(".csv"):
            rows = list(csv.reader(io.StringIO(payload.decode("utf-8"))))
            header_columns = len(rows[0]) if rows else 0
            data_rows = max(0, len(rows) - 1)
            worksheet_count = 0
        else:
            from openpyxl import load_workbook

            workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
            try:
                worksheet_count = len(workbook.sheetnames)
                rows = list(workbook.active.iter_rows(values_only=True))
                header_columns = len(rows[0]) if rows else 0
                data_rows = max(0, len(rows) - 1)
            finally:
                workbook.close()
        shapes.append({"name": name, "upload_bytes": len(payload), "header_columns": header_columns,
                       "data_rows": data_rows, "table_cells": (data_rows + bool(header_columns)) * header_columns,
                       "worksheets": worksheet_count})
    return shapes


async def measure(scene, dsn, *, profile_cleanup=False):
    from sqlalchemy import event, text

    from app.imports.retention import purge_payroll_work_import_details
    from tests.payroll_runtime_fixtures import force_status

    dimensions = ((150, 14, 31) if scene in CURRENT_SCENARIOS else
                  (20, 4, 7) if scene == "representative_csv" else
                  (1000, 20, 10) if scene in ("rare_csv", "issue_heavy_csv") else (150, 20, 31))
    async with application(dsn) as (client, engine, _app):
        fixture = await seed_period(client, engine, dsn, drivers=dimensions[0], definitions=dimensions[1], days=dimensions[2])
        tenant, period = fixture["tenant"], fixture["period"]
        files = source_files(scene, fixture)
        policy_metadata = None
        if scene in CURRENT_SCENARIOS:
            response = await client.get(
                f"/payroll/periods/{period['payroll_period_id']}/imports/policy", headers=tenant.admin,
            )
            assert response.status_code == 200, response.text
            policy_metadata = response.json()
            assert policy_metadata["product_policy"]["max_company_pay_items"] == 50
            assert policy_metadata["product_policy"]["max_branch_input_concepts"] == 15
            assert policy_metadata["product_policy"]["max_import_combined_columns"] == 40
            assert policy_metadata["product_policy"]["max_import_canonical_file_bytes"] == 1_000_000
            assert policy_metadata["import_resource_policy"]["max_file_bytes"] == 10_000_000
        async with engine.connect() as db:
            database_temp_before = dict((await db.execute(text("""SELECT temp_files,temp_bytes
                FROM pg_stat_database WHERE datname=current_database()"""))).mappings().one())
        temporary_before = set(Path(tempfile.gettempdir()).iterdir())
        initial_memory = memory_snapshot()
        phases = Phases()
        phases.install()
        sql_count = 0
        sql_seconds = 0
        sql_writes = {}
        cleanup_plans = []
        def transaction_begin(conn):
            conn.info["measured_transaction_begin"] = time.perf_counter()
        def transaction_commit(conn):
            locked = conn.info.pop("measured_import_lock", None)
            started = conn.info.pop("measured_transaction_begin", None)
            if locked is not None and started is not None:
                now = time.perf_counter()
                phases.values["publication_transaction_to_commit_dispatch"] = now - started
                phases.values["period_lock_to_commit_dispatch"] = now - locked
        def before(conn, _cursor, statement, parameters, _context, _many):
            if conn.info.get("cleanup_probe"):
                return statement, parameters
            conn.info["measurement_started"] = time.perf_counter()
            if profile_cleanup and statement.strip().lower().startswith("delete from import.payrollworkpreviewgroups"):
                conn.info["cleanup_probe"] = True
                try:
                    child = conn.exec_driver_sql("""EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
                        SELECT 1 FROM ONLY import.payrollworkrecords r
                        WHERE r.previewgroupid=(SELECT previewgroupid FROM import.payrollworkpreviewgroups
                            WHERE importid=$1 AND companyid=$2 LIMIT 1)
                          AND r.importid=$1 AND r.companyid=$2 FOR KEY SHARE OF r""", parameters).scalar_one()
                    cleanup_plans.append({"child_lookup": child})
                finally:
                    conn.info.pop("cleanup_probe", None)
                return "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters
            return statement, parameters
        def after(conn, _cursor, statement, _params, _context, _many):
            nonlocal sql_count, sql_seconds
            if conn.info.get("cleanup_probe"):
                return
            sql_count += 1
            elapsed = time.perf_counter() - conn.info.pop("measurement_started")
            sql_seconds += elapsed
            normalized = statement.strip().lower()
            if profile_cleanup and normalized.startswith("explain") and "delete from import.payrollworkpreviewgroups" in normalized:
                cleanup_plans.append({"group_delete": [row[0] for row in _cursor.fetchall()]})
            if normalized.startswith(("delete from import.", "update import.", "insert into import.")):
                key = " ".join(normalized.split()[:3])
                sql_writes[key] = sql_writes.get(key, 0) + elapsed
        event.listen(engine.sync_engine, "before_cursor_execute", before, retval=True)
        event.listen(engine.sync_engine, "after_cursor_execute", after)
        event.listen(engine.sync_engine, "begin", transaction_begin)
        event.listen(engine.sync_engine, "commit", transaction_commit)
        try:
            ordinary_path = f"/payroll/periods/{period['payroll_period_id']}"
            upload, ingress = await timed_request(client, ordinary_path, tenant.admin,
                lambda: client.post(f"/payroll/periods/{period['payroll_period_id']}/imports",
                    files=[("file", (name, data, "application/octet-stream")) for name, data in files],
                    headers={**tenant.admin, "Idempotency-Key": str(uuid4())}), phases)
            iid = upload["import_id"]
            header_structure = upload.get("header_structure") if scene in CURRENT_SCENARIOS else None
            if scene in CURRENT_SCENARIOS:
                assert header_structure and header_structure["available"] is True
                first_inspection = upload["files"][0]["units"][0]["inspection"]
                expected_columns = first_inspection["headed_column_count"]
                assert header_structure["combined_column_count"] == expected_columns
                assert len(header_structure["headed_column_counts"]) == len(files)
                assert {item["count"] for item in header_structure["headed_column_counts"]} == {expected_columns}
                assert len(upload["layouts"]) == 1
                assert header_structure["combined_column_count"] <= policy_metadata["product_policy"]["max_import_combined_columns"]
            if scene.startswith("monthly_daily") or scene in CURRENT_SCENARIOS:
                units = []
                for file in upload["files"]:
                    for unit in file["units"]:
                        candidates = unit["inspection"]["filename_date_candidates" if file["type"] == "csv" else "unit_date_candidates"]
                        assert len(candidates) == 1, candidates
                        units.append({"source_unit_id": unit["source_unit_id"], "selected": True,
                                      "date_source": {"kind": "filename" if file["type"] == "csv" else "sheet", "value": candidates[0]}})
                response = await client.put(f"/payroll/imports/{iid}/interpretation",
                    json={"expected_revision": upload["revision"], "units": units}, headers=tenant.admin)
                assert response.status_code == 200, response.text
                upload = response.json()
            definitions = {definition.code: definition.payroll_period_definition_id for definition in fixture["definitions"]}
            for layout in upload["layouts"]:
                headers = []
                offset = 0
                while True:
                    inspected = await client.get(f"/payroll/imports/{iid}", params={
                        "header_layout_id": layout["layout_id"], "header_offset": offset, "header_limit": 25},
                        headers=tenant.admin)
                    assert inspected.status_code == 200, inspected.text
                    page = inspected.json()["header_page"]
                    headers.extend(item["label"] for item in page["items"])
                    if page["next_offset"] is None:
                        break
                    offset = page["next_offset"]
                mapping = {str(index): ("DriverNumber" if header == "DriverNumber" else "WorkDate" if header == "WorkDate"
                    else f"Definition:{definitions[header]}") for index, header in enumerate(headers, 1)}
                response = await client.put(f"/payroll/imports/{iid}/layouts/{layout['layout_id']}/mapping",
                    json={"expected_revision": upload["revision"], "column_mapping": mapping}, headers=tenant.admin)
                assert response.status_code == 200, response.text
                upload = response.json()
            if scene in CURRENT_SCENARIOS:
                import json

                from app.imports.contracts import (
                    CANONICALIZATION_VERSION,
                    FILE_SOURCE_CONTRACT_VERSION,
                    RULE_CONTRACT_VERSION,
                    SOURCE_CONTRACT_VERSION,
                    TARGET_CONTRACT_VERSION,
                )
                from app.imports.mapping import definition_meaning, fingerprint
                from app.imports.rules import bind_pending_rule

                definition = fixture["definitions"][0]
                target = next(item for item in fixture["definitions"]
                              if item.pay_definition_id == definition.pay_definition_id)
                payload = {"kind": "Direct", "source_field": target.code}
                async with engine.begin() as db:
                    rule_id = await db.scalar(text("""
                        INSERT INTO import.PayrollWorkRuleVersions
                            (CompanyID, BranchID, PayDefinitionID, OriginImportID,
                             OriginPayrollPeriodID, RuleContractVersion, SourceContractVersion,
                             TargetContractVersion, CanonicalizationVersion, RuleFingerprint,
                             SourceSignature, TargetMeaning, RulePayload, CreatedByUserID)
                        VALUES (:cid,:bid,:definition,:iid,:pid,:rule_version,:source_version,
                                :target_version,:canonical_version,:fingerprint,
                                CAST(:source AS jsonb),CAST(:meaning AS jsonb),CAST(:payload AS jsonb),:uid)
                        RETURNING RuleVersionID
                    """), {"cid": tenant.company_id, "bid": period["branch_id"],
                           "definition": definition.pay_definition_id, "iid": iid,
                           "pid": period["payroll_period_id"], "fingerprint": fingerprint("rule", payload),
                           "rule_version": RULE_CONTRACT_VERSION,
                           "source_version": SOURCE_CONTRACT_VERSION,
                           "target_version": TARGET_CONTRACT_VERSION,
                           "canonical_version": CANONICALIZATION_VERSION,
                           "source": json.dumps({"kind": "Direct", "header": target.code,
                                                 "file_contract_version": FILE_SOURCE_CONTRACT_VERSION}),
                           "meaning": json.dumps(definition_meaning(definition)),
                           "payload": json.dumps(payload), "uid": tenant.owner})
                    await bind_pending_rule(db, company_id=tenant.company_id, user_id=tenant.owner,
                        import_id=iid, expected_import_revision=upload["revision"],
                        pay_definition_id=definition.pay_definition_id,
                        period_definition_id=definition.payroll_period_definition_id,
                        rate_definition_id=definition.rate_definition_id, rule_version_id=rule_id,
                        expected_head_revision=0)
                latest = await client.get(f"/payroll/imports/{iid}", headers=tenant.admin)
                assert latest.status_code == 200, latest.text
                upload = latest.json()
            sql_count = 0
            sql_seconds = 0
            sql_writes.clear()
            preview, initial = await timed_request(client, ordinary_path, tenant.admin,
                lambda: client.post(f"/payroll/imports/{iid}/preview", json={"expected_revision": upload["revision"]}, headers=tenant.admin), phases)
            initial["sqlalchemy_calls_including_probe_reads"] = sql_count
            initial["sqlalchemy_seconds_including_probe_reads"] = sql_seconds
            initial["sql_write_seconds_by_statement"] = dict(sql_writes)
            sql_count = 0
            sql_seconds = 0
            sql_writes.clear()
            refreshed, refresh = await timed_request(client, ordinary_path, tenant.admin,
                lambda: client.post(f"/payroll/imports/{iid}/preview", json={"expected_revision": preview["revision"]}, headers=tenant.admin), phases)
            refresh["sqlalchemy_calls_including_probe_reads"] = sql_count
            refresh["sqlalchemy_seconds_including_probe_reads"] = sql_seconds
            refresh["sql_write_seconds_by_statement"] = dict(sql_writes)
            for key in ("mapping_fingerprint", "content_fingerprint", "plan_fingerprint", "counts"):
                assert refreshed["preview"][key] == preview["preview"][key], key
            params = {"cid": tenant.company_id, "iid": iid, "pid": period["payroll_period_id"]}
            async with engine.connect() as db:
                counts = dict((await db.execute(text("""SELECT
                    (SELECT count(*) FROM import.payrollworkrecords WHERE importid=:iid) AS normalized_rows,
                    (SELECT count(*) FROM import.payrollworkpreviewgroups WHERE importid=:iid) AS grid_rows,
                    (SELECT count(*) FROM import.importissues WHERE importid=:iid) AS relational_issues,
                    (SELECT count(*) FROM payroll.payrolldraftlines WHERE payrollperiodid=:pid) AS canonical_lines,
                    (SELECT count(*) FROM payroll.payrollperiodauditevidenceevents WHERE payrollperiodid=:pid AND evidencedomain='SOURCE') AS source_events,
                    (SELECT count(*) FROM import.filesources WHERE importid=:iid) AS files,
                    (SELECT count(*) FROM import.filesourceunits WHERE importid=:iid) AS units,
                    (SELECT count(*) FROM import.sourcelayouts WHERE importid=:iid) AS layouts"""), params)).mappings().one())
                canonical_files = []
                if scene in CURRENT_SCENARIOS:
                    counts["pending_rules"] = await db.scalar(text(
                        "SELECT count(*) FROM import.PayrollWorkPendingRules WHERE ImportID=:iid"), params)
                    canonical_files = [dict(row) for row in (await db.execute(text("""
                        SELECT f.fileordinal AS ordinal, f.originalfilename AS filename,
                               f.filesizebytes AS uploaded_bytes, c.FormatVersion AS format_version,
                               c.ByteCount AS canonical_bytes, c.HashSha256 AS sha256
                        FROM import.FileSources f
                        LEFT JOIN import.FileCanonicalData c
                          ON c.FileSourceID=f.FileSourceID AND c.ImportID=f.ImportID AND c.CompanyID=f.CompanyID
                        WHERE f.ImportID=:iid AND f.CompanyID=:cid ORDER BY f.FileOrdinal
                    """), params)).mappings()]
                    counts["canonical_file_data"] = sum(row["format_version"] is not None for row in canonical_files)
                    counts["canonical_bytes"] = sum(row["canonical_bytes"] or 0 for row in canonical_files)
                    assert len(canonical_files) == len(files)
                    assert all(row["format_version"] == 1 and row["canonical_bytes"] is not None and row["canonical_bytes"] > 0 and row["sha256"]
                               for row in canonical_files)
            assert counts["canonical_lines"] == counts["source_events"] == 0
            query = {"expected_revision": refreshed["revision"], "plan_fingerprint": refreshed["preview"]["plan_fingerprint"]}
            response_sizes = {}
            for name in ("rows", "issues", "source-records"):
                response = await client.get(f"/payroll/imports/{iid}/{name}", params=query, headers=tenant.admin)
                assert response.status_code == 200, response.text
                response_sizes[name] = len(response.content)
                assert len(response.content) <= 2_000_000
            force_status(tenant, period["payroll_period_id"], "Locked")
            sql_writes.clear()
            started = time.perf_counter()
            async with engine.begin() as db:
                await purge_payroll_work_import_details(db, company_id=tenant.company_id,
                    period_id=period["payroll_period_id"], actor_id=tenant.owner)
            purge = time.perf_counter() - started
            purge_statements = dict(sql_writes)
            async with engine.connect() as db:
                assert await db.scalar(text("SELECT count(*) FROM import.payrollworkrecords WHERE importid=:iid"), params) == 0
                assert await db.scalar(text("SELECT count(*) FROM import.filesources WHERE importid=:iid AND filecontent IS NOT NULL"), params) == 0
                database_temp_after = dict((await db.execute(text("""SELECT temp_files,temp_bytes
                    FROM pg_stat_database WHERE datname=current_database()"""))).mappings().one())
            return {"scenario": scene, "python": platform.python_version(), "platform": platform.platform(),
                "transport": "HTTPX ASGITransport, real disposable PostgreSQL, production guards enabled",
                "dimensions": {"drivers": dimensions[0], "definitions": dimensions[1],
                               "ordinary_definitions": dimensions[1],
                               "days": dimensions[2], "files": len(files),
                               "worksheets_per_file": (1 if scene == "current_daily_xlsx" else
                                                        0 if scene in CURRENT_SCENARIOS else None)},
                "file_bytes": sum(len(data) for _, data in files), "initial_memory": initial_memory,
                "file_shapes": _file_shapes(files) if scene in CURRENT_SCENARIOS else None,
                "ingress": ingress, "preview": initial, "refresh": refresh, "counts": counts,
                "header_structure": header_structure,
                "canonical_files": canonical_files if scene in CURRENT_SCENARIOS else None,
                "preview_counts": preview["preview"]["counts"], "purge_seconds": purge,
                "purge_sql_write_seconds": purge_statements,
                "page_response_bytes": response_sizes, "final_memory": memory_snapshot(),
                "database_temp_before": database_temp_before, "database_temp_after": database_temp_after,
                "cleanup_plans": cleanup_plans,
                "temporary_files_left": sorted(str(path) for path in set(Path(tempfile.gettempdir()).iterdir()) - temporary_before),
                "effective_policy": policy_metadata,
                "coverage": ({"legal_policy_shape": True, "canonical_extraction_and_admission": True,
                    "canonical_byte_measurement": True,
                    "combined_column_enforcement": True, "full_mapping_identity_date_aggregation_and_repeat_semantics": False,
                    "final_revised_contract_acceptance": False} if scene in CURRENT_SCENARIOS else None)}
        finally:
            phases.restore()
            event.remove(engine.sync_engine, "before_cursor_execute", before)
            event.remove(engine.sync_engine, "after_cursor_execute", after)
            event.remove(engine.sync_engine, "begin", transaction_begin)
            event.remove(engine.sync_engine, "commit", transaction_commit)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=(*SCENARIOS, *CURRENT_SCENARIOS, "current", "all"),
                        default="current", help="Current supported daily shapes by default; named legacy presets are reference workloads.")
    parser.add_argument("--profile-cleanup", action="store_true",
                        help="Instrument real group deletion/FK lookup with EXPLAIN ANALYZE (extra overhead).")
    parser.add_argument("--output", type=Path, default=ROOT / ".artifacts/import-batch-benchmark.json")
    args = parser.parse_args()
    if args.profile_cleanup and args.scenario in ("all", "current"):
        parser.error("Use --profile-cleanup with one explicit scenario.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.scenario == "all":
        results = []
        for scene in SCENARIOS:
            path = args.output.with_name(args.output.stem + "-" + scene + ".json")
            subprocess.run([sys.executable, "-m", "scripts.benchmark_import_files", "--scenario", scene,
                            "--output", str(path)], check=True, env=os.environ.copy())
            results.append(json.loads(path.read_text(encoding="utf-8")))
        args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    elif args.scenario == "current":
        results = []
        for scene in CURRENT_SCENARIOS:
            path = args.output.with_name(args.output.stem + "-" + scene + ".json")
            subprocess.run([sys.executable, "-m", "scripts.benchmark_import_files", "--scenario", scene,
                            "--output", str(path)], check=True, env=os.environ.copy())
            results.append(json.loads(path.read_text(encoding="utf-8")))
        args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    else:
        with private_database() as dsn:
            result = asyncio.run(measure(args.scenario, dsn, profile_cleanup=args.profile_cleanup))
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
