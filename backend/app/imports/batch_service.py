"""Application orchestration for multi-file PayrollWork imports."""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import date
from functools import partial
from uuid import UUID, uuid4

import anyio
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.imports.errors import ImportFailure
from app.imports.files import inspect_source_file, read_source_units
from app.imports.mapping import (
    canonical,
    definition_meaning,
    fingerprint,
    normalized_identifier,
    suggest_mapping,
    target_key,
    validate_mapping,
)
from app.imports.mapping import (
    targets as mapping_targets,
)
from app.imports.payroll_context import context_identity, load_context
from app.imports.policy import ImportResourcePolicy
from app.imports.preview_model import build_batch_preview
from app.imports.preview_store import persist_preview, prepare_preview
from app.imports.runtime import admission, authorize, failure
from app.imports.staging import clear_derived_state
from app.imports.transport import FilePayload, receive_files
from app.payroll.mutation_lock import _lock_period_for_mutation
from app.payroll.period_definitions import list_period_definitions
from app.payroll.schemas import SOURCE_ENTRY_STATUSES

IMPORT_CONTRACT = 1
SOURCE_CONTRACT = 2
FILE_SOURCE_CONTRACT = 2
TARGET_CONTRACT = 2
CANONICALIZATION = 3
MAX_IMPORT_METADATA_BYTES = 2_000_000


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(strict=True, ge=1)


class UnitInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_unit_id: int = Field(strict=True, ge=1)
    selected: bool
    header_row_number: int | None = Field(default=None, strict=True, ge=1)
    date_order: str | None = None
    date_source: dict | None = None


class InterpretationRequest(RevisionRequest):
    units: list[UnitInterpretation] = Field(default_factory=list)
    default_work_date: date | None = None


class RegroupRequest(RevisionRequest):
    source_unit_ids: list[int] = Field(min_length=1)
    label: str | None = Field(default=None, max_length=120)


class MappingRequest(RevisionRequest):
    column_mapping: dict[str, str]


def _json(value) -> str:
    return json.dumps(canonical(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _enforce_metadata_response_bound(result: dict, max_bytes: int = MAX_IMPORT_METADATA_BYTES) -> dict:
    if len(_json(result).encode("utf-8")) > max_bytes:
        raise failure("IMPORT_METADATA_TOO_LARGE", "Import metadata exceeds the configured response bound.", 422)
    return result


def _policy(request: Request) -> ImportResourcePolicy:
    return request.app.state.settings.import_resource_policy


def _normalize_date_source(source, inspection: dict, default_date: date | None, *, expand_import_fixed: bool) -> dict:
    if not isinstance(source, dict):
        raise failure("IMPORT_DATE_SOURCE_INVALID", "WorkDate provider must be an object.", 422)
    kind = source.get("kind")
    if kind in ("column", "import_fixed"):
        if set(source) != {"kind"}:
            raise failure("IMPORT_DATE_SOURCE_INVALID", "This WorkDate provider accepts only its kind.", 422)
        if kind == "column":
            return {"kind": "column"}
        if default_date is None:
            raise failure("IMPORT_DATE_SOURCE_REQUIRED", "Set an Import default WorkDate before using this provider.", 422)
        return ({"kind": "import_fixed", "value": default_date.isoformat()} if expand_import_fixed
                else {"kind": "import_fixed"})
    if kind not in ("filename", "sheet", "fixed_unit") or set(source) != {"kind", "value"}:
        raise failure("IMPORT_DATE_SOURCE_INVALID", "WorkDate provider must contain only its supported kind and value.", 422)
    value = source["value"]
    if not isinstance(value, str):
        raise failure("IMPORT_DATE_SOURCE_INVALID", "Accepted WorkDate must be a string.", 422)
    if kind == "fixed_unit":
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise failure("IMPORT_DATE_SOURCE_INVALID", "Accepted fixed WorkDate must be an ISO date.", 422) from exc
        if parsed.isoformat() != value:
            raise failure("IMPORT_DATE_SOURCE_INVALID", "Accepted fixed WorkDate must be YYYY-MM-DD.", 422)
    else:
        field = "filename_date_candidates" if kind == "filename" else "unit_date_candidates"
        if value not in (inspection.get(field) or []):
            raise failure("IMPORT_DATE_SOURCE_INVALID", "Accept one of this filename or worksheet's detected date candidates.", 422)
    return {"kind": kind, "value": value}


async def header_labels(db: AsyncConnection, cid: int, pid: int) -> list[str]:
    rows = (await db.execute(text("""
        SELECT definitioncodesnapshot, definitionnamesnapshot
        FROM payroll.payrollperioddefinitions WHERE companyid = :cid AND payrollperiodid = :pid
    """), {"cid": cid, "pid": pid})).all()
    return [str(value) for row in rows for value in row if value is not None]


async def load_import(db: AsyncConnection, cid: int, iid: int):
    row = (await db.execute(text("""
        SELECT i.*, w.payrollperiodid, w.contractversion AS targetversion,
            w.canonicalizationversion, w.defaultworkdate, w.detailpurgedatutc,
            w.mappingfingerprint, w.contentfingerprint, w.planfingerprint,
            w.previewcounts, w.reviewsummary, b.contractversion AS sourceversion,
            b.sourcemanifest
        FROM import.imports i
        JOIN import.payrollworkimports w ON w.importid = i.importid AND w.companyid = i.companyid
        JOIN import.filesourcebatches b ON b.importid = i.importid AND b.companyid = i.companyid
        WHERE i.companyid = :cid AND i.importid = :iid
    """), {"cid": cid, "iid": iid})).mappings().first()
    if row is None:
        raise HTTPException(404, "Import not found.")
    if (row["importkind"] != "PayrollWork" or row["sourcekind"] != "File"
            or row["contractversion"] != IMPORT_CONTRACT or row["sourceversion"] != SOURCE_CONTRACT
            or row["targetversion"] != TARGET_CONTRACT or row["canonicalizationversion"] != CANONICALIZATION):
        raise failure("IMPORT_CONTRACT_UNSUPPORTED", "This staged Import version is unsupported. Reset/re-upload the files.")
    return row


async def load_sources(db: AsyncConnection, cid: int, iid: int) -> tuple[list[dict], list[dict], list[dict]]:
    files = [dict(r) for r in (await db.execute(text("""
        SELECT filesourceid, fileordinal, contractversion, originalfilename, filetype, filesizebytes,
            filehashsha256, filecontent, contentpurgedatutc, inspectionmetadata
        FROM import.filesources WHERE importid = :iid AND companyid = :cid ORDER BY fileordinal
    """), {"iid": iid, "cid": cid})).mappings()]
    _require_file_source_contract(files)
    units = [dict(r) for r in (await db.execute(text("""
        SELECT u.sourceunitid, u.filesourceid, f.fileordinal, f.originalfilename, f.filetype,
            u.unitname, u.unitordinal, u.unitkind, u.isselected, u.headerrownumber,
            u.dateorder, u.datesource, u.encoding, u.delimiter, u.sourcelayoutid,
            u.inspectionmetadata
        FROM import.filesourceunits u JOIN import.filesources f
          ON f.filesourceid = u.filesourceid AND f.importid = u.importid AND f.companyid = u.companyid
        WHERE u.importid = :iid AND u.companyid = :cid
        ORDER BY f.fileordinal, u.unitordinal
    """), {"iid": iid, "cid": cid})).mappings()]
    layouts = [dict(r) for r in (await db.execute(text("""
        SELECT sourcelayoutid, structurefingerprint, label, columnmapping,
            mappingfingerprint, inspectionmetadata
        FROM import.sourcelayouts WHERE importid = :iid AND companyid = :cid
        ORDER BY sourcelayoutid
    """), {"iid": iid, "cid": cid})).mappings()]
    return files, units, layouts


def _require_file_source_contract(files: list[dict]) -> None:
    if any(item["contractversion"] != FILE_SOURCE_CONTRACT for item in files):
        raise failure("IMPORT_CONTRACT_UNSUPPORTED",
                      "This Import contains an unsupported File version. Reset/re-upload the files.")


async def metadata(db: AsyncConnection, cid: int, iid: int, *, target_offset: int = 0, target_limit: int = 50,
                   header_layout_id: int | None = None, header_offset: int = 0,
                   header_limit: int = 25) -> dict:
    if target_offset < 0 or target_limit < 1 or target_limit > 100:
        raise failure("IMPORT_TARGET_PAGE_INVALID", "Mapping targets require a non-negative offset and limit 1–100.", 422)
    if header_offset < 0 or header_limit < 1 or header_limit > 25:
        raise failure("IMPORT_HEADER_PAGE_INVALID", "Header inspection requires a non-negative offset and limit 1–25.", 422)
    row = await load_import(db, cid, iid)
    files, units, layouts = await load_sources(db, cid, iid)
    period_status = (await db.execute(text("""
        SELECT status FROM payroll.payrollperiods WHERE companyid = :cid AND payrollperiodid = :pid
    """), {"cid": cid, "pid": row["payrollperiodid"]})).scalar_one()
    layout_units = defaultdict(list)
    for unit in units:
        layout_units[unit["sourcelayoutid"]].append({"source_unit_id": unit["sourceunitid"],
            "file_ordinal": unit["fileordinal"], "file_name": unit["originalfilename"],
            "unit_name": unit["unitname"], "selected": unit["isselected"]})

    def unit_inspection(value):
        value = value or {}
        keys = ("row_count", "column_count", "cell_count", "header_row_number", "header_state",
                "date_order", "date_order_state", "encoding", "delimiter", "formula_count",
                "cached_formula_count", "missing_cache_count", "filename_date_candidates", "unit_date_candidates")
        return {key: value[key] for key in keys if key in value}

    def layout_inspection(value):
        value = value or {}
        headers = value.get("header_cells", [])
        sample = [str(header)[:80] for header in headers[:12]]
        return {"header_cells": sample, "header_count": len(headers),
                "header_cells_truncated": len(headers) > len(sample)
                    or any(len(str(header)) > 80 for header in headers[:12])}
    targets = []
    definitions = []
    if period_status in SOURCE_ENTRY_STATUSES:
        definitions = await list_period_definitions(row["payrollperiodid"], cid, db, active_only=True)
        targets = mapping_targets(definitions)
    target_count = len(targets)
    target_page = targets[target_offset:target_offset + target_limit]
    header_page = None
    if header_layout_id is not None:
        selected_layout = next((layout for layout in layouts if layout["sourcelayoutid"] == header_layout_id), None)
        if selected_layout is None:
            raise failure("IMPORT_LAYOUT_NOT_FOUND", "Source layout not found.", 404)
        headers = (selected_layout["inspectionmetadata"] or {}).get("header_cells", [])
        header_cells = headers[header_offset:header_offset + header_limit]
        header_page = {"layout_id": header_layout_id, "offset": header_offset, "limit": header_limit,
                       "total": len(headers),
                       "next_offset": (header_offset + len(header_cells)
                                       if header_offset + len(header_cells) < len(headers) else None),
                       "items": [{"source_column": header_offset + index + 1, "label": value}
                                 for index, value in enumerate(header_cells)]}
    result = {
        "import_id": row["importid"], "company_id": row["companyid"], "branch_id": row["branchid"],
        "period_id": row["payrollperiodid"], "import_kind": row["importkind"],
        "source_kind": row["sourcekind"], "status": row["status"], "revision": row["revision"],
        "correlation_id": str(row["correlationid"]), "created_at": row["createdatutc"],
        "updated_at": row["updatedatutc"], "contract_versions": {"import": row["contractversion"],
            "file": row["sourceversion"], "payroll_work": row["targetversion"],
            "canonicalization": row["canonicalizationversion"]},
        "period_status": period_status,
        "source_editable": period_status in SOURCE_ENTRY_STATUSES and row["status"] in ("NeedsMapping", "NeedsReview")
            and row["detailpurgedatutc"] is None,
        "default_work_date": row["defaultworkdate"], "files": [
            {"file_source_id": f["filesourceid"], "ordinal": f["fileordinal"], "name": f["originalfilename"],
             "type": f["filetype"], "size_bytes": f["filesizebytes"], "sha256": f["filehashsha256"],
             "content_purged_at": f["contentpurgedatutc"], "inspection": f["inspectionmetadata"],
             "units": [{"source_unit_id": u["sourceunitid"], "name": u["unitname"], "index": u["unitordinal"],
                        "kind": u["unitkind"], "selected": u["isselected"],
                        "header_row_number": u["headerrownumber"], "date_order": u["dateorder"],
                        "date_source": u["datesource"], "encoding": u["encoding"], "delimiter": u["delimiter"],
                        "inspection": unit_inspection(u["inspectionmetadata"]), "layout_id": u["sourcelayoutid"]}
                       for u in units if u["filesourceid"] == f["filesourceid"]]}
            for f in files],
        "layouts": [{"layout_id": layout["sourcelayoutid"], "structure_fingerprint": layout["structurefingerprint"],
                     "label": layout["label"], "mapping": layout["columnmapping"],
                     "mapping_fingerprint": layout["mappingfingerprint"],
                     "inspection": layout_inspection(layout["inspectionmetadata"]),
                     "mapping_suggestions": suggest_mapping(
                         (layout["inspectionmetadata"] or {}).get("header_cells", []), definitions),
                     "source_units": layout_units[layout["sourcelayoutid"]]}
                    for layout in layouts],
        "mapping_targets": target_page,
        "mapping_target_page": {"offset": target_offset, "limit": target_limit, "total": target_count,
                                "next_offset": (target_offset + len(target_page)
                                    if target_offset + len(target_page) < target_count else None)},
        "header_page": header_page,
        "preview": {"mapping_fingerprint": row["mappingfingerprint"],
            "content_fingerprint": row["contentfingerprint"], "plan_fingerprint": row["planfingerprint"],
            "counts": row["previewcounts"], "summary": row["reviewsummary"]},
    }
    return _enforce_metadata_response_bound(result)


def _manifest_payload(payloads: list[FilePayload], inspected: list[dict]) -> list[dict]:
    return [{"ordinal": ordinal, "name": payload.filename, "type": info["file_type"],
             "size": payload.file_size, "sha256": payload.file_sha256,
             "units": [{"name": unit["name"], "index": unit["index"]} for unit in info["source_units"]]}
            for ordinal, (payload, info) in enumerate(zip(payloads, inspected, strict=True), start=1)]


def _date_candidates(label: str) -> list[str]:
    """Return unambiguous ISO dates and valid slash-order candidates for review."""
    found: set[str] = set()
    for match in re.finditer(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)", label):
        try:
            found.add(date.fromisoformat(match.group(1)).isoformat())
        except ValueError:
            continue
    for match in re.finditer(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?!\d)", label):
        first, second, year = map(int, match.groups())
        for month, day in {(first, second), (second, first)}:
            try:
                found.add(date(year, month, day).isoformat())
            except ValueError:
                continue
    return sorted(found)


def _creation_fingerprint(cid: int, uid: int, bid: int, pid: int, manifest: list[dict]) -> str:
    value = {"import_contract": IMPORT_CONTRACT, "source_contract": SOURCE_CONTRACT,
             "target_contract": TARGET_CONTRACT, "canonicalization": CANONICALIZATION,
             "company": cid, "creator": uid, "branch": bid, "period": pid, "kind": "PayrollWork",
             "source": "File", "manifest": manifest}
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


async def _existing_key(db, cid: int, key: UUID):
    return (await db.execute(text("""
        SELECT importid, creationfingerprint FROM import.imports
        WHERE companyid = :cid AND idempotencykey = :key
    """), {"cid": cid, "key": key})).mappings().first()


async def _retry(db, cid: int, uid: int, existing, expected: str) -> dict:
    if existing["creationfingerprint"] != expected:
        raise failure("IMPORT_IDEMPOTENCY_CONFLICT", "Idempotency-Key was already used for a different ordered file batch.")
    row = await load_import(db, cid, existing["importid"])
    await authorize(db, cid, uid, row["payrollperiodid"], mutate=True)
    return await metadata(db, cid, row["importid"])


async def create_import(request: Request, engine: AsyncEngine, cid: int, uid: int,
                        pid: int, key: UUID) -> dict:
    async with engine.begin() as db:
        period = await authorize(db, cid, uid, pid, mutate=True)
        existing = await _existing_key(db, cid, key)
        if existing is None and period.status not in SOURCE_ENTRY_STATUSES:
            raise failure("IMPORT_PERIOD_NOT_EDITABLE", "This period no longer accepts PayrollWork imports.")
        labels = await header_labels(db, cid, pid)
    async with admission(request):
        payloads = await receive_files(request, _policy(request))
        inspected = await anyio.to_thread.run_sync(partial(
            _inspect_batch, payloads, _policy(request), labels))
        manifest = _manifest_payload(payloads, inspected)
        creation = _creation_fingerprint(cid, uid, period.branch_id, pid, manifest)
        async with engine.begin() as db:
            existing = await _existing_key(db, cid, key)
            if existing is not None:
                return await _retry(db, cid, uid, existing, creation)
        async with engine.begin() as db:
            period = await authorize(db, cid, uid, pid, mutate=True)
            existing = await _existing_key(db, cid, key)
            if existing is not None:
                return await _retry(db, cid, uid, existing, creation)
            if period.status not in SOURCE_ENTRY_STATUSES:
                raise failure("IMPORT_PERIOD_NOT_EDITABLE", "This period no longer accepts PayrollWork imports.")
            await _lock_period_for_mutation(pid, cid, db)
            iid = (await db.execute(text("""
                INSERT INTO import.imports (companyid, branchid, importkind, sourcekind,
                    correlationid, idempotencykey, creationfingerprint, createdbyuserid, updatedbyuserid)
                VALUES (:cid, :bid, 'PayrollWork', 'File', :correlation, :key, :fingerprint, :uid, :uid)
                ON CONFLICT (companyid, idempotencykey) DO NOTHING RETURNING importid
            """), {"cid": cid, "bid": period.branch_id, "correlation": uuid4(), "key": key,
                   "fingerprint": creation, "uid": uid})).scalar_one_or_none()
            if iid is None:
                return await _retry(db, cid, uid, await _existing_key(db, cid, key), creation)
            await db.execute(text("""
                INSERT INTO import.payrollworkimports (importid, companyid, branchid, payrollperiodid)
                VALUES (:iid, :cid, :bid, :pid)
            """), {"iid": iid, "cid": cid, "bid": period.branch_id, "pid": pid})
            await db.execute(text("""
                INSERT INTO import.filesourcebatches (importid, companyid, sourcemanifest)
                VALUES (:iid, :cid, CAST(:manifest AS jsonb))
            """), {"iid": iid, "cid": cid, "manifest": _json(manifest)})
            structure_rows = {}
            file_units: list[dict] = []
            for ordinal, (payload, info) in enumerate(zip(payloads, inspected, strict=True), start=1):
                file_id = (await db.execute(text("""
                    INSERT INTO import.filesources (importid, companyid, fileordinal, originalfilename,
                        filetype, filesizebytes, filehashsha256, filecontent, inspectionmetadata)
                    VALUES (:iid, :cid, :ordinal, :name, :type, :size, :sha, :content, CAST(:inspection AS jsonb))
                    RETURNING filesourceid
                """), {"iid": iid, "cid": cid, "ordinal": ordinal, "name": payload.filename,
                       "type": info["file_type"], "size": payload.file_size, "sha": payload.file_sha256,
                       "content": payload.data,
                       "inspection": _json({"decompressed_bytes": info["decompressed_bytes"],
                                             "unit_count": len(info["source_units"])})})).scalar_one()
                for unit in info["source_units"]:
                    inspection = unit["inspection"]
                    structure = fingerprint("layout-structure", {
                        "headers": [str(value).strip().casefold() for value in inspection.get("header_cells", [])],
                        "column_count": inspection.get("column_count", 0),
                    })
                    inspection = {**inspection, "filename_date_candidates": _date_candidates(payload.filename),
                                  "unit_date_candidates": _date_candidates(unit["name"])}
                    unit["inspection"] = inspection
                    structure_rows.setdefault(structure, {"headers": inspection.get("header_cells", []),
                                                           "units": []})["units"].append((file_id, ordinal, unit, info["file_type"]))
            layout_ids = {}
            for structure, detail in structure_rows.items():
                layout_id = (await db.execute(text("""
                    INSERT INTO import.sourcelayouts (importid, companyid, structurefingerprint, label, inspectionmetadata)
                    VALUES (:iid, :cid, :structure, :label, CAST(:inspection AS jsonb)) RETURNING sourcelayoutid
                """), {"iid": iid, "cid": cid, "structure": structure,
                       "label": f"Layout {len(layout_ids) + 1}",
                       "inspection": _json({"header_cells": detail["headers"]})})).scalar_one()
                layout_ids[structure] = layout_id
            for structure, detail in structure_rows.items():
                for file_id, ordinal, unit, file_type in detail["units"]:
                    inspection = unit["inspection"]
                    file_units.append({"filesourceid": file_id, "importid": iid, "companyid": cid,
                        "unitname": unit["name"], "unitordinal": unit["index"],
                        "unitkind": "CSV" if file_type == "csv" else "Worksheet",
                        "isselected": file_type == "csv", "headerrownumber": inspection["header_row_number"],
                        "datesource": "{}", "sourcelayoutid": layout_ids[structure],
                        "inspection": _json(inspection)})
            if file_units:
                await db.execute(text("""
                    INSERT INTO import.filesourceunits (filesourceid, importid, companyid, unitname,
                        unitordinal, unitkind, isselected, headerrownumber, datesource, sourcelayoutid, inspectionmetadata)
                    VALUES (:filesourceid, :importid, :companyid, :unitname, :unitordinal, :unitkind,
                        :isselected, :headerrownumber, CAST(:datesource AS jsonb), :sourcelayoutid,
                        CAST(:inspection AS jsonb))
                """), file_units)
            return await metadata(db, cid, iid)


def _inspect_batch(payloads: list[FilePayload], policy: ImportResourcePolicy, labels: list[str]) -> list[dict]:
    if not payloads or len(payloads) > policy.max_files:
        raise ImportFailure("IMPORT_FILE_COUNT_LIMIT_EXCEEDED", "Import has an unsupported number of files.")
    total_decompressed = total_units = total_inspection = 0
    result = []
    for payload in payloads:
        info = inspect_source_file(payload.data, payload.filename, policy, header_labels=labels)
        if info["decompressed_bytes"] > policy.max_file_decompressed_bytes:
            raise ImportFailure("IMPORT_DECOMPRESSED_LIMIT_EXCEEDED", "A workbook exceeds its configured decompressed-byte limit.")
        total_decompressed += info["decompressed_bytes"]
        total_units += len(info["source_units"])
        total_inspection += len(_json(info).encode("utf-8"))
        if total_decompressed > policy.max_total_decompressed_bytes:
            raise ImportFailure("IMPORT_TOTAL_DECOMPRESSED_LIMIT_EXCEEDED", "Import exceeds its aggregate decompressed-byte limit.")
        if total_units > policy.max_source_units:
            raise ImportFailure("IMPORT_SOURCE_UNIT_LIMIT_EXCEEDED", "Import exceeds its aggregate source-unit limit.")
        if total_inspection > policy.max_inspection_bytes:
            raise ImportFailure("IMPORT_INSPECTION_TOO_LARGE", "Import inspection metadata exceeds its configured limit.")
        result.append(info)
    return result


def _inspect_accepted_headers(files, units, body, policy):
    files_by_id = {item["filesourceid"]: item for item in files}
    requested = {item.source_unit_id: item for item in body.units if item.header_row_number is not None}
    by_file = defaultdict(list)
    for unit in units:
        item = requested.get(unit["sourceunitid"])
        if item is not None and item.header_row_number != unit["headerrownumber"]:
            by_file[unit["filesourceid"]].append((unit, item))
    resolved = {}
    for file_id, entries in by_file.items():
        file = files_by_id[file_id]
        configs = []
        for unit, item in entries:
            inspection = unit["inspectionmetadata"] or {}
            if not inspection.get("column_count"):
                raise ImportFailure("IMPORT_SOURCE_COLUMNS_INVALID", "A unit without columns cannot accept a header row.")
            configs.append({"unit_name": unit["unitname"], "header_row_number": item.header_row_number,
                            "date_order": item.date_order or unit["dateorder"], "columns": [1]})
        result = read_source_units(file["filecontent"], file["originalfilename"], configs, policy)
        by_name = {item["unit_name"]: item["inspection"] for item in result}
        for unit, _item in entries:
            resolved[unit["sourceunitid"]] = by_name[unit["unitname"]]
    return resolved


async def read_import(engine: AsyncEngine, cid: int, uid: int, iid: int, *, target_offset: int = 0,
                      target_limit: int = 50, header_layout_id: int | None = None,
                      header_offset: int = 0, header_limit: int = 25) -> dict:
    async with engine.begin() as db:
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        row = await load_import(db, cid, iid)
        await authorize(db, cid, uid, row["payrollperiodid"], mutate=False)
        return await metadata(db, cid, iid, target_offset=target_offset, target_limit=target_limit,
                              header_layout_id=header_layout_id, header_offset=header_offset,
                              header_limit=header_limit)


async def _lock_mutable(db, cid: int, uid: int, iid: int, expected: int):
    row = await load_import(db, cid, iid)
    await authorize(db, cid, uid, row["payrollperiodid"], mutate=True)
    await _lock_period_for_mutation(row["payrollperiodid"], cid, db)
    await db.execute(text("SELECT importid FROM import.imports WHERE importid = :iid AND companyid = :cid FOR UPDATE"),
                     {"iid": iid, "cid": cid})
    row = await load_import(db, cid, iid)
    if row["revision"] != expected:
        raise failure("IMPORT_REVISION_CONFLICT", "Import changed. Read its current revision and retry.")
    if row["status"] not in ("NeedsMapping", "NeedsReview") or row["detailpurgedatutc"] is not None:
        raise failure("IMPORT_NOT_EDITABLE", "This Import can no longer be changed.")
    return row


async def update_interpretation(request, engine, cid: int, uid: int, iid: int, body: InterpretationRequest):
    async with admission(request):
        async with engine.begin() as db:
            row = await load_import(db, cid, iid)
            period = await authorize(db, cid, uid, row["payrollperiodid"], mutate=True)
            if row["revision"] != body.expected_revision:
                raise failure("IMPORT_REVISION_CONFLICT", "Import changed. Read its current revision and retry.")
            if period.status not in SOURCE_ENTRY_STATUSES:
                raise failure("IMPORT_NOT_EDITABLE", "This period no longer accepts interpretation changes.")
            if row["status"] not in ("NeedsMapping", "NeedsReview") or row["detailpurgedatutc"] is not None:
                raise failure("IMPORT_NOT_EDITABLE", "This Import can no longer be interpreted.")
            files, units, _layouts = await load_sources(db, cid, iid)
            if len(body.units) > _policy(request).max_source_units:
                raise failure("IMPORT_SOURCE_UNIT_LIMIT_EXCEEDED", "Interpretation exceeds the configured source-unit limit.", 422)
            if len({item.source_unit_id for item in body.units}) != len(body.units):
                raise failure("IMPORT_SOURCE_UNIT_INVALID", "A source unit may be interpreted only once per request.", 422)
            unit_ids = {u["sourceunitid"] for u in units}
            if any(item.source_unit_id not in unit_ids for item in body.units):
                raise failure("IMPORT_SOURCE_UNIT_NOT_FOUND", "A selected source unit does not belong to this Import.", 404)
            default_date_supplied = "default_work_date" in body.model_fields_set
            default_date = body.default_work_date if default_date_supplied else row["defaultworkdate"]
            if not body.units and not default_date_supplied:
                raise failure("IMPORT_INTERPRETATION_EMPTY", "Provide at least one source-unit interpretation change.", 422)
            for item in body.units:
                unit = next(u for u in units if u["sourceunitid"] == item.source_unit_id)
                inspection = unit["inspectionmetadata"] or {}
                if item.header_row_number is not None and item.header_row_number > max(inspection.get("row_count", 1), 1):
                    raise failure("IMPORT_HEADER_ROW_INVALID", "Selected header row is outside the source unit.", 422)
                if item.date_order is not None and item.date_order not in ("DMY", "MDY", "YMD"):
                    raise failure("IMPORT_DATE_ORDER_INVALID", "Date order must be DMY, MDY or YMD.", 422)
                if unit["unitkind"] == "CSV" and not item.selected:
                    # CSV's sole source unit may be deselected, but the batch
                    # must have at least one selected unit after the request.
                    pass
                date_source_supplied = "date_source" in item.model_fields_set
                if date_source_supplied and item.date_source is not None:
                    _normalize_date_source(item.date_source, inspection, default_date, expand_import_fixed=False)
            updated = {item.source_unit_id: item for item in body.units}
            selected_count = sum((updated[u["sourceunitid"]].selected if u["sourceunitid"] in updated else u["isselected"])
                                 for u in units)
            if not selected_count:
                raise failure("IMPORT_SOURCE_SELECTION_REQUIRED", "Select at least one CSV or worksheet source unit.", 422)
        accepted_headers = await anyio.to_thread.run_sync(
            partial(_inspect_accepted_headers, files, units, body, _policy(request))
        )
        async with engine.begin() as db:
            await _lock_mutable(db, cid, uid, iid, body.expected_revision)
            _current_files, current_units, layouts = await load_sources(db, cid, iid)
            layouts_by_structure = defaultdict(list)
            for layout in layouts:
                layouts_by_structure[layout["structurefingerprint"]].append(layout["sourcelayoutid"])
            header_layouts = {}
            changed_units = [unit for unit in current_units if unit["sourceunitid"] in accepted_headers]
            for unit in changed_units:
                inspection = accepted_headers[unit["sourceunitid"]]
                structure = fingerprint("layout-structure", {
                    "headers": [str(value).strip().casefold() for value in inspection.get("header_cells", [])],
                    "column_count": inspection.get("column_count", 0),
                })
                header_layouts[unit["sourceunitid"]] = structure
            new_structures = {}
            for unit in changed_units:
                structure = header_layouts[unit["sourceunitid"]]
                if len(layouts_by_structure[structure]) != 1 and structure not in new_structures:
                    new_structures[structure] = accepted_headers[unit["sourceunitid"]].get("header_cells", [])
            if len(layouts) + len(new_structures) > _policy(request).max_layouts:
                raise failure("IMPORT_LAYOUT_LIMIT_EXCEEDED", "Import exceeds its configured source-layout limit.", 422)
            await clear_derived_state(db, cid, iid, reset_mapping=False)
            for ordinal, (structure, headers) in enumerate(new_structures.items(), start=1):
                layout_id = (await db.execute(text("""
                    INSERT INTO import.sourcelayouts (importid, companyid, structurefingerprint, label, inspectionmetadata)
                    VALUES (:iid, :cid, :structure, :label, CAST(:inspection AS jsonb))
                    RETURNING sourcelayoutid
                """), {"iid": iid, "cid": cid, "structure": structure,
                       "label": f"Layout {len(layouts) + ordinal}",
                       "inspection": _json({"header_cells": headers})})).scalar_one()
                layouts_by_structure[structure] = [layout_id]
            for item in body.units:
                unit = next(u for u in units if u["sourceunitid"] == item.source_unit_id)
                inspection = unit["inspectionmetadata"] or {}
                current_source = unit["datesource"] or {}
                if "date_source" in item.model_fields_set:
                    current_source = item.date_source or {}
                if "date_order" in item.model_fields_set:
                    current_order = item.date_order
                else:
                    current_order = unit["dateorder"]
                await db.execute(text("""
                    UPDATE import.filesourceunits SET isselected = :selected,
                        headerrownumber = COALESCE(:header, headerrownumber), dateorder = :date_order,
                        datesource = CAST(:date_source AS jsonb)
                    WHERE sourceunitid = :unit AND importid = :iid AND companyid = :cid
                """), {"selected": item.selected, "header": item.header_row_number,
                       "date_order": current_order, "date_source": _json(current_source),
                       "unit": item.source_unit_id, "iid": iid, "cid": cid})
                if item.header_row_number is not None:
                    inspected = accepted_headers.get(item.source_unit_id)
                    if inspected is not None:
                        inspection.update(inspected)
                    inspection["header_row_number"] = item.header_row_number
                    inspection["header_state"] = "selected"
                inspection["date_order"] = current_order
                await db.execute(text("""
                    UPDATE import.filesourceunits SET inspectionmetadata = CAST(:inspection AS jsonb)
                    WHERE sourceunitid = :unit AND importid = :iid AND companyid = :cid
                """), {"inspection": _json(inspection), "unit": item.source_unit_id, "iid": iid, "cid": cid})
                structure = header_layouts.get(item.source_unit_id)
                if structure is not None:
                    await db.execute(text("""
                        UPDATE import.filesourceunits SET sourcelayoutid = :layout
                        WHERE sourceunitid = :unit AND importid = :iid AND companyid = :cid
                    """), {"layout": layouts_by_structure[structure][0], "unit": item.source_unit_id,
                           "iid": iid, "cid": cid})
            await db.execute(text("""
                UPDATE import.payrollworkimports SET defaultworkdate = :work_date
                WHERE importid = :iid AND companyid = :cid
            """), {"work_date": default_date, "iid": iid, "cid": cid})
            await db.execute(text("""
                UPDATE import.imports SET revision = revision + 1, status = 'NeedsMapping',
                    updatedbyuserid = :uid, updatedatutc = NOW() WHERE importid = :iid AND companyid = :cid
            """), {"uid": uid, "iid": iid, "cid": cid})
            return await metadata(db, cid, iid)


async def create_layout(request, engine, cid, uid, iid, body: RegroupRequest):
    async with engine.begin() as db:
        await _lock_mutable(db, cid, uid, iid, body.expected_revision)
        if len(body.source_unit_ids) > _policy(request).max_source_units:
            raise failure("IMPORT_SOURCE_UNIT_LIMIT_EXCEEDED", "Regroup exceeds the configured source-unit limit.", 422)
        if len(set(body.source_unit_ids)) != len(body.source_unit_ids):
            raise failure("IMPORT_SOURCE_UNIT_INVALID", "A source unit may appear only once in a regroup request.", 422)
        files, units, layouts = await load_sources(db, cid, iid)
        by_id = {u["sourceunitid"]: u for u in units}
        if any(unit_id not in by_id for unit_id in body.source_unit_ids):
            raise failure("IMPORT_SOURCE_UNIT_NOT_FOUND", "A source unit does not belong to this Import.", 404)
        selected = [by_id[unit_id] for unit_id in body.source_unit_ids]
        unit_structure = {layout["sourcelayoutid"]: layout["structurefingerprint"] for layout in layouts}
        if len({unit_structure[u["sourcelayoutid"]] for u in selected}) != 1:
            raise failure("IMPORT_LAYOUT_INCOMPATIBLE", "Only compatible source structures can share a layout.", 422)
        structure = unit_structure[selected[0]["sourcelayoutid"]]
        if len(layouts) >= _policy(request).max_layouts:
            raise failure("IMPORT_LAYOUT_LIMIT_EXCEEDED", "Import exceeds its configured source-layout limit.", 422)
        info = (await db.execute(text("""
            SELECT inspectionmetadata FROM import.sourcelayouts
            WHERE sourcelayoutid = :layout AND importid = :iid AND companyid = :cid
        """), {"layout": selected[0]["sourcelayoutid"], "iid": iid, "cid": cid})).scalar_one()
        layout_id = (await db.execute(text("""
            INSERT INTO import.sourcelayouts (importid, companyid, structurefingerprint, label, inspectionmetadata)
            VALUES (:iid, :cid, :structure, :label, CAST(:inspection AS jsonb)) RETURNING sourcelayoutid
        """), {"iid": iid, "cid": cid, "structure": structure, "label": body.label,
               "inspection": _json(info or {})})).scalar_one()
        await clear_derived_state(db, cid, iid, reset_mapping=False)
        for unit in selected:
            await db.execute(text("""
                UPDATE import.filesourceunits SET sourcelayoutid = :layout
                WHERE sourceunitid = :unit AND importid = :iid AND companyid = :cid
            """), {"layout": layout_id, "unit": unit["sourceunitid"], "iid": iid, "cid": cid})
        await db.execute(text("""
            UPDATE import.imports SET revision = revision + 1, status = 'NeedsMapping',
                updatedbyuserid = :uid, updatedatutc = NOW() WHERE importid = :iid AND companyid = :cid
        """), {"uid": uid, "iid": iid, "cid": cid})
        return await metadata(db, cid, iid)


async def accept_mapping(request, engine, cid, uid, iid, layout_id, body: MappingRequest):
    async with engine.begin() as db:
        row = await _lock_mutable(db, cid, uid, iid, body.expected_revision)
        layout = (await db.execute(text("""
            SELECT structurefingerprint FROM import.sourcelayouts
            WHERE importid = :iid AND companyid = :cid AND sourcelayoutid = :layout FOR UPDATE
        """), {"iid": iid, "cid": cid, "layout": layout_id})).mappings().first()
        if layout is None:
            raise HTTPException(404, "Source layout not found.")
        definitions = await list_period_definitions(row["payrollperiodid"], cid, db, active_only=True)
        validate_mapping(body.column_mapping, [], definitions,
                         max_column=_policy(request).max_columns_per_unit, require_work_date=False)
        selected = (await db.execute(text("""
            SELECT count(*) FROM import.filesourceunits WHERE importid = :iid AND companyid = :cid
                AND sourcelayoutid = :layout AND isselected
        """), {"iid": iid, "cid": cid, "layout": layout_id})).scalar_one()
        if not selected:
            raise failure("IMPORT_LAYOUT_NOT_SELECTED", "Select at least one source unit in this layout before mapping it.", 422)
        await clear_derived_state(db, cid, iid, reset_mapping=False)
        await db.execute(text("""
            UPDATE import.sourcelayouts SET columnmapping = CAST(:mapping AS jsonb), mappingfingerprint = NULL
            WHERE importid = :iid AND companyid = :cid AND sourcelayoutid = :layout
        """), {"mapping": _json(body.column_mapping), "iid": iid, "cid": cid, "layout": layout_id})
        await db.execute(text("""
            UPDATE import.imports SET revision = revision + 1, status = 'NeedsMapping',
                updatedbyuserid = :uid, updatedatutc = NOW() WHERE importid = :iid AND companyid = :cid
        """), {"uid": uid, "iid": iid, "cid": cid})
        return await metadata(db, cid, iid)


def _materialize_batch(files, units, layouts, policy):
    files_by_id = {item["filesourceid"]: item for item in files}
    layouts_by_id = {item["sourcelayoutid"]: item for item in layouts}
    selected = [unit for unit in units if unit["isselected"]]
    if not selected:
        raise ImportFailure("IMPORT_SOURCE_SELECTION_REQUIRED", "Select at least one source unit before preview.")
    if any(unit["sourcelayoutid"] not in layouts_by_id for unit in selected):
        raise ImportFailure("IMPORT_LAYOUT_REQUIRED", "Every selected source unit must belong to a layout.")
    aggregate_rows = aggregate_cells = candidates = 0
    for unit in selected:
        inspection = unit["inspectionmetadata"] or {}
        layout = layouts_by_id[unit["sourcelayoutid"]]
        mapping = layout["columnmapping"]
        if not mapping:
            raise ImportFailure("IMPORT_MAPPING_REQUIRED", "Accept a mapping for every selected source layout.")
        aggregate_rows += max(0, int(inspection.get("row_count", 0)) - unit["headerrownumber"])
        aggregate_cells += int(inspection.get("cell_count", 0))
        candidates += max(0, int(inspection.get("row_count", 0)) - unit["headerrownumber"]) * sum(
            target.startswith("Definition:") for target in mapping.values())
    if aggregate_rows > policy.max_data_rows:
        raise ImportFailure("IMPORT_DATA_ROW_LIMIT_EXCEEDED", "Selected units exceed the configured aggregate row limit.")
    if aggregate_cells > policy.max_selected_cells:
        raise ImportFailure("IMPORT_SELECTED_CELL_LIMIT_EXCEEDED", "Selected units exceed the configured aggregate cell limit.")
    if candidates > policy.max_candidates:
        raise ImportFailure("IMPORT_CANDIDATE_LIMIT_EXCEEDED", "Selected units exceed the configured mapped-candidate limit.")

    by_file = defaultdict(list)
    for unit in selected:
        by_file[unit["filesourceid"]].append(unit)
    source_rows = []
    for file_id, file_units in by_file.items():
        file = files_by_id[file_id]
        configs = []
        for unit in file_units:
            inspection = unit["inspectionmetadata"] or {}
            configs.append({"unit_name": unit["unitname"], "header_row_number": unit["headerrownumber"],
                            "date_order": unit["dateorder"], "columns": list(range(1, int(inspection.get("column_count", 0)) + 1))})
        materialized = read_source_units(file["filecontent"], file["originalfilename"], configs, policy)
        materialized_by_name = {item["unit_name"]: item for item in materialized}
        for unit in file_units:
            inspection = materialized_by_name[unit["unitname"]]["inspection"]
            inspection["filename_date_candidates"] = _date_candidates(file["originalfilename"])
            inspection["unit_date_candidates"] = _date_candidates(unit["unitname"])
        for unit in file_units:
            layout = layouts_by_id[unit["sourcelayoutid"]]
            source_rows.append({"source_unit_id": unit["sourceunitid"], "file_ordinal": unit["fileordinal"],
                "unit_name": unit["unitname"], "unit_kind": unit["unitkind"],
                "structure_fingerprint": layout["structurefingerprint"],
                "mapping": {str(key): value for key, value in layout["columnmapping"].items()},
                "date_source": unit["datesource"] or {}, "date_order": unit["dateorder"],
                "materialized": materialized_by_name[unit["unitname"]]})
    return source_rows


def _stable_identifiers(sources):
    found = {"DriverNumber": set(), "EmployeeKey": set()}
    for source in sources:
        mapping = source["mapping"]
        for column, target in mapping.items():
            if target not in found:
                continue
            for row in source["materialized"]["rows"]:
                cell = row["cells"].get(column)
                if cell is not None and not cell.missing_cache and cell.value is not None:
                    value = normalized_identifier(target, cell.value)
                    if value:
                        found[target].add(value)
    return {key: sorted(value) for key, value in found.items()}


async def build_preview(request, engine, cid, uid, iid, body: RevisionRequest):
    policy = _policy(request)
    async with admission(request):
        async with engine.begin() as db:
            row = await load_import(db, cid, iid)
            period = await authorize(db, cid, uid, row["payrollperiodid"], mutate=True)
            if row["revision"] != body.expected_revision:
                raise failure("IMPORT_REVISION_CONFLICT", "Import changed. Read its current revision and retry.")
            if period.status not in SOURCE_ENTRY_STATUSES or row["status"] not in ("NeedsMapping", "NeedsReview") or row["detailpurgedatutc"]:
                raise failure("IMPORT_NOT_EDITABLE", "This Import is no longer editable.")
            files, units, layouts = await load_sources(db, cid, iid)
            if any(file["contentpurgedatutc"] is not None or file["filecontent"] is None for file in files):
                raise failure("IMPORT_CONTENT_PURGED", "Source file content has been purged and cannot be previewed.")
            default_date = row["defaultworkdate"]
        materialized = await anyio.to_thread.run_sync(partial(_materialize_batch, files, units, layouts, policy))
        files = units = layouts = None
        for source in materialized:
            date_source = source["date_source"]
            mapping_has_column = any(target == "WorkDate" for target in source["mapping"].values())
            if not date_source:
                if mapping_has_column:
                    date_source = {"kind": "column"}
                elif default_date is not None:
                    date_source = {"kind": "import_fixed"}
                else:
                    raise failure("IMPORT_DATE_SOURCE_REQUIRED", "Accept a WorkDate column or date context for each selected source unit.", 422)
            date_source = _normalize_date_source(date_source, source["materialized"]["inspection"], default_date,
                                                expand_import_fixed=True)
            if date_source.get("kind") == "column" and not mapping_has_column:
                raise failure("IMPORT_DATE_SOURCE_CONFLICT", "A column date provider requires a mapped WorkDate column.", 422)
            source["date_source"] = date_source
        identifiers = _stable_identifiers(materialized)
        dates = set()
        for source in materialized:
            date_column = next((column for column, target in source["mapping"].items() if target == "WorkDate"), None)
            if date_column:
                from app.imports.mapping import infer_date_order, parse_date
                rows = source["materialized"]["rows"]
                cells = [item["cells"].get(date_column) for item in rows]
                order, _ = infer_date_order(cells, period.start_date, period.end_date, source["date_order"])
                dates.update(day for cell in cells if (day := parse_date(cell, order)) is not None)
            elif source["date_source"].get("value"):
                dates.add(date.fromisoformat(source["date_source"]["value"]))
        async with engine.begin() as db:
            # This read is deliberately outside the publication transaction.
            period = await authorize(db, cid, uid, period.payroll_period_id, mutate=True)
            context = await load_context(db, cid, period, dates=sorted(dates), identifiers=identifiers)
        context_token = fingerprint("relational-context", context_identity(context))
        result = await anyio.to_thread.run_sync(partial(build_batch_preview, materialized, context, policy=policy))
        definitions = context["definitions"]
        inspection_updates = []
        for source in materialized:
            inspection = source["materialized"]["inspection"]
            inspection["accepted_date_order"] = source.get("accepted_date_order")
            inspection["date_order_state"] = source.get("date_order_state")
            inspection_updates.append((source["source_unit_id"], inspection.get("encoding"),
                                       inspection.get("delimiter"), _json(inspection)))
        del context
        result = await anyio.to_thread.run_sync(prepare_preview, result)
        del materialized
        started_revision = body.expected_revision
        async with engine.begin() as db:
            await _lock_mutable(db, cid, uid, iid, started_revision)
            current_context = await load_context(db, cid, period, dates=sorted(dates), identifiers=identifiers)
            if fingerprint("relational-context", context_identity(current_context)) != context_token:
                raise failure("IMPORT_CONTEXT_STALE", "Payroll context changed while the preview was being prepared. Refresh and retry.")
            _current_files, current_units, current_layouts = await load_sources(db, cid, iid)
            await clear_derived_state(db, cid, iid, reset_mapping=False)
            await persist_preview(db, company_id=cid, import_id=iid, branch_id=period.branch_id,
                                  period_id=period.payroll_period_id, result=result)
            current_unit_ids = {unit["sourceunitid"] for unit in current_units}
            for source_unit_id, encoding, delimiter, inspection_json in inspection_updates:
                if source_unit_id not in current_unit_ids:
                    raise failure("IMPORT_REVISION_CONFLICT", "Source units changed while the preview was being prepared.")
                await db.execute(text("""
                    UPDATE import.filesourceunits SET encoding = :encoding, delimiter = :delimiter,
                        inspectionmetadata = CAST(:inspection AS jsonb)
                    WHERE sourceunitid = :unit AND importid = :iid AND companyid = :cid
                """), {"encoding": encoding, "delimiter": delimiter,
                       "inspection": inspection_json, "unit": source_unit_id, "iid": iid, "cid": cid})
            # Store per-layout mapping identities without exposing generated IDs in their hashes.
            def_by_target = {target_key(d): d for d in definitions}
            for layout in current_layouts:
                mapping = layout["columnmapping"]
                if not mapping:
                    continue
                sem = {column: definition_meaning(def_by_target[target]) if target in def_by_target else target
                       for column, target in mapping.items()}
                await db.execute(text("""
                    UPDATE import.sourcelayouts SET mappingfingerprint = :fp
                    WHERE sourcelayoutid = :layout AND importid = :iid AND companyid = :cid
                """), {"fp": fingerprint("layout-mapping", {"structure": layout["structurefingerprint"], "mapping": sem}),
                       "layout": layout["sourcelayoutid"], "iid": iid, "cid": cid})
            await db.execute(text("""
                UPDATE import.imports SET revision = revision + 1, status = 'NeedsReview',
                    updatedbyuserid = :uid, updatedatutc = NOW() WHERE importid = :iid AND companyid = :cid
            """), {"uid": uid, "iid": iid, "cid": cid})
            return await metadata(db, cid, iid)


async def cancel_import(engine: AsyncEngine, cid: int, uid: int, iid: int, expected: int):
    async with engine.begin() as db:
        row = await load_import(db, cid, iid)
        await authorize(db, cid, uid, row["payrollperiodid"], mutate=True)
        await _lock_period_for_mutation(row["payrollperiodid"], cid, db)
        await db.execute(text("SELECT importid FROM import.imports WHERE importid=:iid AND companyid=:cid FOR UPDATE"),
                         {"iid": iid, "cid": cid})
        row = await load_import(db, cid, iid)
        if row["revision"] != expected:
            raise failure("IMPORT_REVISION_CONFLICT", "Import changed. Read its current revision and retry.")
        if row["status"] not in ("NeedsMapping", "NeedsReview", "Cancelled") or row["detailpurgedatutc"] is not None:
            raise failure("IMPORT_NOT_EDITABLE", "This Import can no longer be cancelled.")
        if row["status"] != "Cancelled":
            await db.execute(text("""
                UPDATE import.imports SET status = 'Cancelled', revision = revision + 1,
                    cancelledbyuserid = :uid, cancelledatutc = NOW(), updatedbyuserid = :uid, updatedatutc = NOW()
                WHERE importid = :iid AND companyid = :cid
            """), {"uid": uid, "iid": iid, "cid": cid})
        return await metadata(db, cid, iid)

