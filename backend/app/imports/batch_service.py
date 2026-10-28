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
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.imports.canonical import (
    canonical_source_unit,
    decode_canonical_file,
    extract_canonical_file,
    inspect_canonical_headers,
)
from app.imports.contracts import (
    CANONICAL_FILE_VERSION,
    CANONICALIZATION_VERSION,
    FILE_SOURCE_CONTRACT_VERSION,
    IMPORT_CONTRACT_VERSION,
    RULE_CONTRACT_VERSION,
    SOURCE_CONTRACT_VERSION,
    TARGET_CONTRACT_VERSION,
    require_supported_import_contract,
)
from app.imports.errors import ImportFailure
from app.imports.headers import (
    analyze_header_sets,
    combined_column_count,
    describe_headers,
    normalize_header,
)
from app.imports.mapping import (
    IDENTIFIERS,
    blank,
    canonical,
    definition_meaning,
    encoded,
    fingerprint,
    normalized_identifier,
    suggest_mapping,
    target_key,
)
from app.imports.mapping import (
    targets as mapping_targets,
)
from app.imports.payroll_context import context_identity, load_context
from app.imports.policy import DEFAULT_IMPORT_RESOURCE_POLICY, ImportResourcePolicy
from app.imports.preview_model import build_batch_preview
from app.imports.preview_store import persist_preview, prepare_preview
from app.imports.rules import current_rule_context
from app.imports.runtime import admission, authorize, failure
from app.imports.staging import clear_derived_state
from app.imports.target_mapping import (
    ensure_duplicate_outcomes,
    resolve_selector,
    selector_identity,
    selectors_overlap,
)
from app.imports.transport import FilePayload, receive_files
from app.payroll.mutation_lock import _lock_period_for_mutation
from app.payroll.period_definitions import list_period_definitions
from app.payroll.schemas import SOURCE_ENTRY_STATUSES

IMPORT_CONTRACT = IMPORT_CONTRACT_VERSION
SOURCE_CONTRACT = SOURCE_CONTRACT_VERSION
FILE_SOURCE_CONTRACT = FILE_SOURCE_CONTRACT_VERSION
TARGET_CONTRACT = TARGET_CONTRACT_VERSION
CANONICALIZATION = CANONICALIZATION_VERSION
MAX_IMPORT_METADATA_BYTES = DEFAULT_IMPORT_RESOURCE_POLICY.max_response_bytes


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
    shared_columns: dict[str, ColumnSelector] = Field(default_factory=dict)
    ignored_columns: list[ColumnSelector] = Field(default_factory=list)
    targets: list[TargetRuleRequest] = Field(default_factory=list)


class DuplicateOccurrence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    duplicate_signature: str | None
    occurrence_ordinal: int | None = Field(strict=True, ge=1)

    @model_validator(mode="after")
    def validate_occurrence(self):
        if self.duplicate_signature is None:
            if self.occurrence_ordinal not in (None, 1):
                raise ValueError("a unique arrangement uses occurrence ordinal 1 or an explicit absence")
        elif (not re.fullmatch(r"[0-9a-f]{64}", self.duplicate_signature)
              or self.occurrence_ordinal is not None
              and (isinstance(self.occurrence_ordinal, bool) or self.occurrence_ordinal < 1)):
            raise ValueError("duplicate selectors require a valid signature and ordinal")
        return self


class ColumnSelector(BaseModel):
    model_config = ConfigDict(extra="forbid")
    header: str = Field(min_length=1)
    occurrences: list[DuplicateOccurrence] | None = None


class TargetRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(pattern=r"^Definition:[1-9][0-9]*$")
    transform: str
    inputs: list[ColumnSelector] = Field(min_length=1)
    expected_head_revision: int = Field(strict=True, ge=0)
    replace_existing: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_transform(self):
        if self.transform not in {"Direct", "Sum"}:
            raise ValueError("transform must be Direct or Sum")
        if self.transform == "Direct" and len(self.inputs) != 1:
            raise ValueError("Direct requires exactly one input")
        if self.transform == "Sum" and len(self.inputs) < 2:
            raise ValueError("Sum requires at least two inputs")
        return self


MappingRequest.model_rebuild()


def _json(value) -> str:
    return json.dumps(canonical(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _enforce_metadata_response_bound(result: dict,
                                     max_bytes: int = DEFAULT_IMPORT_RESOURCE_POLICY.max_response_bytes) -> dict:
    if len(_json(result).encode("utf-8")) > max_bytes:
        raise failure("IMPORT_METADATA_TOO_LARGE", "Import metadata exceeds the configured response bound.", 422)
    return result


def _policy(request: Request) -> ImportResourcePolicy:
    return request.app.state.settings.import_resource_policy


def _product_policy(request: Request):
    return request.app.state.settings.product_policy


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
    await require_supported_import_contract(db, cid, iid)
    return row


async def load_sources(db: AsyncConnection, cid: int, iid: int, *, include_canonical: bool = False
                       ) -> tuple[list[dict], list[dict], list[dict]]:
    columns = """
        f.filesourceid, f.fileordinal, f.contractversion, f.originalfilename, f.filetype, f.filesizebytes,
        f.filehashsha256, f.contentpurgedatutc, f.inspectionmetadata,
        c.FormatVersion AS canonicalformatversion, c.ByteCount AS canonicalbytecount,
        c.HashSha256 AS canonicalsha256
    """
    canonical_join = "LEFT JOIN import.FileCanonicalData c ON c.FileSourceID=f.FileSourceID " \
                     "AND c.ImportID=f.ImportID AND c.CompanyID=f.CompanyID"
    if include_canonical:
        columns += ", c.Payload AS canonicalpayload"
    files = [dict(r) for r in (await db.execute(text(f"""
        SELECT {columns} FROM import.filesources f {canonical_join}
        WHERE f.importid = :iid AND f.companyid = :cid ORDER BY f.fileordinal
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


async def metadata(db: AsyncConnection, cid: int, iid: int, *, target_offset: int = 0, target_limit: int | None = None,
                   header_layout_id: int | None = None, header_offset: int = 0,
                   header_limit: int | None = None,
                   policy: ImportResourcePolicy = DEFAULT_IMPORT_RESOURCE_POLICY) -> dict:
    target_limit = policy.target_page_default if target_limit is None else target_limit
    header_limit = policy.header_page_default if header_limit is None else header_limit
    if target_offset < 0 or target_limit < 1 or target_limit > policy.target_page_max:
        raise failure("IMPORT_TARGET_PAGE_INVALID", f"Mapping targets require a non-negative offset and limit 1–{policy.target_page_max}.", 422)
    if header_offset < 0 or header_limit < 1 or header_limit > policy.header_page_max:
        raise failure("IMPORT_HEADER_PAGE_INVALID", f"Header inspection requires a non-negative offset and limit 1–{policy.header_page_max}.", 422)
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
        if value is None or "header_cells" not in value:
            return {"available": False}
        value = value or {}
        keys = ("row_count", "column_count", "cell_count", "headed_column_count", "imported_row_count",
                "combined_column_count",
                "header_row_number", "header_state", "date_order", "accepted_date_order",
                "date_order_state", "encoding", "delimiter", "formula_count",
                "formula_cached_value_count", "formula_empty_cache_count", "formula_missing_cache_count",
                "cached_formula_count", "missing_cache_count", "filename_date_candidates", "unit_date_candidates",
                "headerless_columns", "hidden_columns", "notices", "duplicate_header_signatures")
        result = {"available": True, **{key: value[key] for key in keys if key in value}}
        if "duplicate_header_signatures" in result:
            result["duplicate_header_signatures"] = [
                {key: signature[key] for key in ("signature", "count", "columns")}
                for signature in result["duplicate_header_signatures"]]
        return result

    def layout_inspection(value):
        if value is None or "header_cells" not in value:
            return {"available": False, "header_cells": [], "header_count": None,
                    "headed_column_count": None, "duplicate_header_signatures": None,
                    "header_cells_truncated": False}
        value = value or {}
        headers = value.get("header_cells", [])
        sample = [str(header)[:policy.max_header_label_chars]
                  for header in headers[:policy.layout_header_sample]]
        duplicate_signatures = [{key: signature[key] for key in ("signature", "count", "columns")}
                                 for signature in value.get("duplicate_header_signatures", [])]
        return {"available": True, "header_cells": sample, "header_count": len(headers),
                "headed_column_count": value.get("headed_column_count", sum(bool(str(header).strip()) for header in headers)),
                "duplicate_header_signatures": duplicate_signatures,
                "header_cells_truncated": len(headers) > len(sample)
                    or any(len(str(header)) > policy.max_header_label_chars
                           for header in headers[:policy.layout_header_sample])}
    targets = []
    definitions = []
    if period_status in SOURCE_ENTRY_STATUSES:
        definitions = await list_period_definitions(row["payrollperiodid"], cid, db, active_only=True)
        targets = mapping_targets(definitions)
    target_count = len(targets)
    target_page = targets[target_offset:target_offset + target_limit]
    target_definitions = {target_key(item): item for item in definitions}
    definition_ids = [target_definitions[item["target"]].pay_definition_id
                      for item in target_page if item["target"] in target_definitions]
    pending_state = {}
    head_state = {}
    if definition_ids:
        pending_state = {item["paydefinitionid"]: item for item in (await db.execute(text("""
            SELECT p.PayDefinitionID, p.RuleVersionID, p.Revision, p.ExpectedHeadRevision,
                   r.RulePayload, r.TargetMeaning
            FROM import.PayrollWorkPendingRules p
            JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID=p.RuleVersionID
              AND r.CompanyID=p.CompanyID AND r.BranchID=p.BranchID AND r.PayDefinitionID=p.PayDefinitionID
            WHERE p.ImportID=:iid AND p.CompanyID=:cid AND p.PayDefinitionID=ANY(:definitions)
        """), {"iid": iid, "cid": cid, "definitions": definition_ids})).mappings()}
        head_state = {item["paydefinitionid"]: item for item in (await db.execute(text("""
            SELECT h.PayDefinitionID, h.RuleVersionID, h.Revision, r.RulePayload, r.TargetMeaning,
                   r.SourceSignature, r.RuleContractVersion, r.SourceContractVersion,
                   r.TargetContractVersion, r.CanonicalizationVersion,
                   origin.Status AS OriginStatus
            FROM import.PayrollWorkRuleHeads h
            JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID=h.RuleVersionID
              AND r.CompanyID=h.CompanyID AND r.BranchID=h.BranchID AND r.PayDefinitionID=h.PayDefinitionID
            JOIN import.Imports origin ON origin.ImportID=r.OriginImportID AND origin.CompanyID=r.CompanyID
            WHERE h.CompanyID=:cid AND h.BranchID=:bid AND h.PayDefinitionID=ANY(:definitions)
        """), {"cid": cid, "bid": row["branchid"], "definitions": definition_ids})).mappings()}
    selected_ids = {unit["sourcelayoutid"] for unit in units if unit["isselected"]}
    reuse_headers = {layout["sourcelayoutid"]: (layout["inspectionmetadata"] or {}).get("header_cells", [])
                     for layout in layouts if not selected_ids or layout["sourcelayoutid"] in selected_ids}
    for item in target_page:
        definition = target_definitions.get(item["target"])
        if definition is None:
            continue
        did = definition.pay_definition_id
        pending = pending_state.get(did)
        head = head_state.get(did)
        reason, reusable = _rule_reuse_reason(head, definition, reuse_headers)
        state = {"current_head_revision": head["revision"] if head else 0,
                 "current_rule_version_id": head["ruleversionid"] if head else None,
                 "pending_rule_version_id": pending["ruleversionid"] if pending else None,
                 "pending_revision": pending["revision"] if pending else None,
                 "pending_expected_head_revision": pending["expectedheadrevision"] if pending else None,
                 "pending_rule": _stored_rule_payload(pending["rulepayload"]) if pending else None,
                 "current_rule": _stored_rule_payload(head["rulepayload"]) if head else None,
                 "reusable": reusable, "reason": reason}
        state["state"] = ("head_changed" if pending and pending["expectedheadrevision"] != state["current_head_revision"]
                           else "pending" if pending else "missing_inputs" if reusable and reason == "Known source columns are absent"
                           else "reusable" if reusable
                           else "remap_required" if head else "unmapped")
        item["rule_state"] = state
    header_page = None
    if header_layout_id is not None:
        selected_layout = next((layout for layout in layouts if layout["sourcelayoutid"] == header_layout_id), None)
        if selected_layout is None:
            raise failure("IMPORT_LAYOUT_NOT_FOUND", "Source layout not found.", 404)
        layout_meta = selected_layout["inspectionmetadata"]
        if layout_meta is None or "header_cells" not in layout_meta:
            header_page = {"available": False, "layout_id": header_layout_id,
                           "offset": header_offset, "limit": header_limit, "total": None,
                           "next_offset": None, "items": []}
            headers = []
        else:
            headers = layout_meta.get("header_cells", [])
        header_cells = headers[header_offset:header_offset + header_limit]
        if layout_meta is not None and "header_cells" in layout_meta:
            by_column = {item["source_column"]: item for item in layout_meta.get("header_occurrences", [])}
            header_page = {"available": True, "layout_id": header_layout_id,
                           "offset": header_offset, "limit": header_limit,
                           "total": len(headers),
                           "next_offset": (header_offset + len(header_cells)
                                           if header_offset + len(header_cells) < len(headers) else None),
                           "items": [{"source_column": header_offset + index + 1, "label": value,
                                      **{key: by_column[header_offset + index + 1][key]
                                         for key in ("occurrence_ordinal", "occurrence_count", "occurrence_id",
                                                     "duplicate_signature")
                                         if header_offset + index + 1 in by_column
                                         and by_column[header_offset + index + 1]["duplicate_signature"]}}
                                     for index, value in enumerate(header_cells)]}
    unit_headers = []
    headers_available = bool(units)
    for unit in units:
        inspection = unit["inspectionmetadata"]
        if inspection is None or "header_cells" not in inspection:
            headers_available = False
            break
        details = describe_headers(inspection["header_cells"])
        if not details["headed_column_count"]:
            headers_available = False
            break
        unit_headers.append((unit, details))
    header_structure = {"available": headers_available,
        "combined_column_count": (combined_column_count([
            item["header_cells"] for _unit, item in unit_headers]) if headers_available else None),
        "file_count": len(files),
        "headed_column_counts": ([{"source_unit_id": unit["sourceunitid"],
                                   "file_ordinal": unit["fileordinal"],
                                   "count": details["headed_column_count"]}
                                  for unit, details in unit_headers] if headers_available else None)}
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
             "canonical_data_bytes": f["canonicalbytecount"],
             "canonical_format_version": f["canonicalformatversion"],
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
        "header_structure": header_structure,
        "preview": {"mapping_fingerprint": row["mappingfingerprint"],
            "content_fingerprint": row["contentfingerprint"], "plan_fingerprint": row["planfingerprint"],
            "counts": row["previewcounts"], "summary": row["reviewsummary"]},
    }
    return _enforce_metadata_response_bound(result, policy.max_response_bytes)


def _manifest_payload(payloads: list[FilePayload], inspected: list[dict]) -> list[dict]:
    return [{"ordinal": ordinal, "name": payload.filename, "type": info["file_type"],
             "size": payload.file_size, "sha256": payload.file_sha256,
             "units": [{"name": unit["name"], "index": unit["index"]} for unit in info["source_units"]]}
            for ordinal, (payload, info) in enumerate(zip(payloads, inspected, strict=True), start=1)]


def _attach_header_metadata(inspection: dict) -> dict:
    details = describe_headers(inspection.get("header_cells", []))
    inspection.update({key: details[key] for key in (
        "header_cells", "headed_column_count", "header_occurrences", "duplicate_header_signatures")})
    return details


def _validate_header_policy(units: list[dict], maximum_columns: int) -> dict:
    if not units or any(unit.get("inspectionmetadata") is None
                         or "header_cells" not in unit["inspectionmetadata"] for unit in units):
        raise ImportFailure("IMPORT_HEADER_METADATA_UNAVAILABLE",
                            "Stored source headers are unavailable. Reset this Import and upload the files again.", 409)
    return analyze_header_sets([
        (unit.get("inspectionmetadata") or {}).get("header_cells", []) for unit in units
    ], maximum_columns)


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


async def _retry(db, cid: int, uid: int, existing, expected: str,
                 policy: ImportResourcePolicy = DEFAULT_IMPORT_RESOURCE_POLICY) -> dict:
    if existing["creationfingerprint"] != expected:
        raise failure("IMPORT_IDEMPOTENCY_CONFLICT", "Idempotency-Key was already used for a different ordered file batch.")
    row = await load_import(db, cid, existing["importid"])
    await authorize(db, cid, uid, row["payrollperiodid"], mutate=True)
    return await metadata(db, cid, row["importid"], policy=policy)


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
            _inspect_batch, payloads, _policy(request), labels, _product_policy(request)))
        manifest = _manifest_payload(payloads, inspected)
        creation = _creation_fingerprint(cid, uid, period.branch_id, pid, manifest)
        async with engine.begin() as db:
            existing = await _existing_key(db, cid, key)
            if existing is not None:
                return await _retry(db, cid, uid, existing, creation, _policy(request))
        async with engine.begin() as db:
            period = await authorize(db, cid, uid, pid, mutate=True)
            existing = await _existing_key(db, cid, key)
            if existing is not None:
                return await _retry(db, cid, uid, existing, creation, _policy(request))
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
                return await _retry(db, cid, uid, await _existing_key(db, cid, key), creation, _policy(request))
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
                await db.execute(text("""
                    INSERT INTO import.FileCanonicalData
                        (FileSourceID, ImportID, CompanyID, FormatVersion, Payload, ByteCount, HashSha256)
                    VALUES (:fid, :iid, :cid, :version, :payload, :byte_count, :sha256)
                """), {"fid": file_id, "iid": iid, "cid": cid,
                       "version": info["canonical_format_version"],
                       "payload": info["canonical_payload"],
                       "byte_count": info["canonical_byte_count"],
                       "sha256": info["canonical_sha256"]})
                for unit in info["source_units"]:
                    inspection = unit["inspection"]
                    header_details = _attach_header_metadata(inspection)
                    structure = fingerprint("layout-structure", header_details["structure"])
                    inspection = {**inspection, "filename_date_candidates": _date_candidates(payload.filename),
                                  "unit_date_candidates": _date_candidates(unit["name"])}
                    unit["inspection"] = inspection
                    structure_rows.setdefault(structure, {"headers": header_details["header_cells"],
                                                           "header_occurrences": header_details["header_occurrences"],
                                                           "headed_column_count": header_details["headed_column_count"],
                                                           "duplicate_header_signatures": header_details["duplicate_header_signatures"],
                                                           "units": []})["units"].append((file_id, ordinal, unit, info["file_type"]))
            layout_ids = {}
            for structure, detail in structure_rows.items():
                layout_id = (await db.execute(text("""
                    INSERT INTO import.sourcelayouts (importid, companyid, structurefingerprint, label, inspectionmetadata)
                    VALUES (:iid, :cid, :structure, :label, CAST(:inspection AS jsonb)) RETURNING sourcelayoutid
                """), {"iid": iid, "cid": cid, "structure": structure,
                       "label": f"Layout {len(layout_ids) + 1}",
                       "inspection": _json({"header_cells": detail["headers"],
                           "header_occurrences": detail["header_occurrences"],
                           "headed_column_count": detail["headed_column_count"],
                           "duplicate_header_signatures": detail["duplicate_header_signatures"]})})).scalar_one()
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
            await _prepare_current_rules(request, db, cid, uid, iid)
            return await metadata(db, cid, iid, policy=_policy(request))


def _inspect_batch(payloads: list[FilePayload], policy: ImportResourcePolicy, _labels: list[str],
                   product_policy=None) -> list[dict]:
    if not payloads or len(payloads) > policy.max_files:
        raise ImportFailure("IMPORT_FILE_COUNT_LIMIT_EXCEEDED", "Import has an unsupported number of files.")
    if product_policy is None:
        from app.product_policy import DEFAULT_PRODUCT_POLICY
        product_policy = DEFAULT_PRODUCT_POLICY
    total_decompressed = total_units = total_inspection = 0
    probes = []
    for payload in payloads:
        info = inspect_canonical_headers(payload.data, payload.filename,
                                         resource_policy=policy, product_policy=product_policy)
        if info["decompressed_bytes"] > policy.max_file_decompressed_bytes:
            raise ImportFailure("IMPORT_DECOMPRESSED_LIMIT_EXCEEDED", "A workbook exceeds its configured decompressed-byte limit.")
        total_decompressed += info["decompressed_bytes"]
        total_units += len(info["source_units"])
        for unit in info["source_units"]:
            _attach_header_metadata(unit["inspection"])
        safe_info = {key: value for key, value in info.items() if key != "canonical_payload"}
        total_inspection += len(_json(safe_info).encode("utf-8"))
        if total_decompressed > policy.max_total_decompressed_bytes:
            raise ImportFailure("IMPORT_TOTAL_DECOMPRESSED_LIMIT_EXCEEDED", "Import exceeds its aggregate decompressed-byte limit.")
        if total_units > policy.max_source_units:
            raise ImportFailure("IMPORT_SOURCE_UNIT_LIMIT_EXCEEDED", "Import exceeds its aggregate source-unit limit.")
        if total_inspection > policy.max_inspection_bytes:
            raise ImportFailure("IMPORT_INSPECTION_TOO_LARGE", "Import inspection metadata exceeds its configured limit.")
        probes.append(info)

    header_sets = [probe["inspection"]["header_cells"] for probe in probes]
    combined = analyze_header_sets(header_sets, product_policy.max_import_combined_columns)
    layout_structures = {fingerprint("layout-structure", details["structure"])
                         for details in combined["file_header_details"]}
    if len(layout_structures) > policy.max_layouts:
        raise ImportFailure("IMPORT_LAYOUT_LIMIT_EXCEEDED",
                            "The source batch exceeds the configured number of distinct header layouts.", 422)
    result = []
    total_final_inspection = 0
    for payload, probe in zip(payloads, probes, strict=True):
        info = extract_canonical_file(payload.data, payload.filename,
                                      resource_policy=policy, product_policy=product_policy)
        actual_headers = info["inspection"].get("header_cells", [])
        if describe_headers(actual_headers)["structure"] != describe_headers(
                probe["inspection"].get("header_cells", []))["structure"]:
            raise ImportFailure("IMPORT_HEADER_INSPECTION_CHANGED",
                                "File headers changed during bounded inspection. Retry the upload.", 409)
        for unit in info["source_units"]:
            details = _attach_header_metadata(unit["inspection"])
            unit["inspection"]["combined_column_count"] = combined["combined_column_count"]
            unit["inspection"]["header_cells"] = details["header_cells"]
        info["combined_column_count"] = combined["combined_column_count"]
        info["canonical_format_version"] = CANONICAL_FILE_VERSION
        safe_info = {key: value for key, value in info.items() if key != "canonical_payload"}
        total_final_inspection += len(_json(safe_info).encode("utf-8"))
        if total_final_inspection > policy.max_inspection_bytes:
            raise ImportFailure("IMPORT_INSPECTION_TOO_LARGE",
                                "Import inspection metadata exceeds its configured limit.")
        result.append(info)
    return result



async def read_import(engine: AsyncEngine, cid: int, uid: int, iid: int, *, target_offset: int = 0,
                      target_limit: int | None = None, header_layout_id: int | None = None,
                      header_offset: int = 0, header_limit: int | None = None,
                      policy: ImportResourcePolicy = DEFAULT_IMPORT_RESOURCE_POLICY) -> dict:
    async with engine.begin() as db:
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        row = await load_import(db, cid, iid)
        await authorize(db, cid, uid, row["payrollperiodid"], mutate=False)
        return await metadata(db, cid, iid, target_offset=target_offset, target_limit=target_limit,
                              header_layout_id=header_layout_id, header_offset=header_offset,
                              header_limit=header_limit, policy=policy)


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
                if item.header_row_number is not None and item.header_row_number != 1:
                    raise failure("IMPORT_HEADER_ROW_INVALID", "Import tables use the first row as their header.", 422)
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
        async with engine.begin() as db:
            await _lock_mutable(db, cid, uid, iid, body.expected_revision)
            _current_files, current_units, _layouts = await load_sources(db, cid, iid)
            _validate_header_policy(current_units, _product_policy(request).max_import_combined_columns)
            await clear_derived_state(db, cid, iid, reset_mapping=False)
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
                    inspection["header_row_number"] = 1
                    inspection["header_state"] = "selected"
                inspection["date_order"] = current_order
                await db.execute(text("""
                    UPDATE import.filesourceunits SET inspectionmetadata = CAST(:inspection AS jsonb)
                    WHERE sourceunitid = :unit AND importid = :iid AND companyid = :cid
                """), {"inspection": _json(inspection), "unit": item.source_unit_id, "iid": iid, "cid": cid})
            await db.execute(text("""
                UPDATE import.payrollworkimports SET defaultworkdate = :work_date
                WHERE importid = :iid AND companyid = :cid
            """), {"work_date": default_date, "iid": iid, "cid": cid})
            await db.execute(text("""
                UPDATE import.imports SET revision = revision + 1, status = 'NeedsMapping',
                    updatedbyuserid = :uid, updatedatutc = NOW() WHERE importid = :iid AND companyid = :cid
            """), {"uid": uid, "iid": iid, "cid": cid})
            await _prepare_current_rules(request, db, cid, uid, iid)
            return await metadata(db, cid, iid, policy=_policy(request))


async def create_layout(request, engine, cid, uid, iid, body: RegroupRequest):
    async with engine.begin() as db:
        await _lock_mutable(db, cid, uid, iid, body.expected_revision)
        raise failure("IMPORT_LAYOUT_REGROUP_UNSUPPORTED",
                      "Source structures are grouped automatically. Upload unrelated structures as separate Imports.", 422)


def _target_meaning(definition) -> dict:
    return {"definition": definition_meaning(definition), "version": 1}


def _selector_payload(selector: ColumnSelector) -> dict:
    value = selector.model_dump()
    if value["occurrences"] is None:
        value.pop("occurrences")
    value["header"] = normalize_header(value["header"])
    if value.get("occurrences"):
        value["occurrences"] = sorted(value["occurrences"],
                                       key=lambda item: (item["duplicate_signature"] or "",
                                                         item["occurrence_ordinal"] or 0))
    return value


def _stored_rule_payload(value) -> dict | None:
    if (not isinstance(value, dict) or set(value) != {
            "payload_version", "transform", "inputs", "shared_columns"}
            or type(value.get("payload_version")) is not int or value["payload_version"] != 1):
        return None
    if value.get("transform") not in {"Direct", "Sum"}:
        return None
    inputs = value.get("inputs")
    shared = value.get("shared_columns")
    if not isinstance(inputs, list) or not inputs or not isinstance(shared, dict):
        return None
    def selector_ok(selector):
        if not isinstance(selector, dict) or set(selector) - {"header", "occurrences"}:
            return False
        if not isinstance(selector.get("header"), str) or not normalize_header(selector["header"]):
            return False
        occurrences = selector.get("occurrences")
        if occurrences is None:
            return True
        if not isinstance(occurrences, list) or not occurrences:
            return False
        seen = set()
        for occurrence in occurrences:
            if not isinstance(occurrence, dict) or set(occurrence) != {
                    "duplicate_signature", "occurrence_ordinal"}:
                return False
            signature = occurrence["duplicate_signature"]
            ordinal = occurrence["occurrence_ordinal"]
            if signature is None:
                if ordinal not in (None, 1) or isinstance(ordinal, bool) or signature in seen:
                    return False
            elif (not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature)
                  or signature in seen
                  or ordinal is not None and (isinstance(ordinal, bool) or not isinstance(ordinal, int)
                                              or ordinal < 1)):
                return False
            seen.add(signature)
        return True
    if any(not selector_ok(item) for item in inputs) or set(shared) - {
            "DriverNumber", "EmployeeKey", "DriverName", "WorkDate"}:
        return None
    if any(not selector_ok(item) for item in shared.values()):
        return None
    shared_selectors = list(shared.values())
    if (any(selectors_overlap(left, right) for index, left in enumerate(shared_selectors)
            for right in shared_selectors[index + 1:])
            or any(selectors_overlap(left, right) for index, left in enumerate(inputs)
                   for right in inputs[index + 1:])):
        return None
    if any(selectors_overlap(left, right) for left in shared_selectors for right in inputs):
        return None
    if value["transform"] == "Direct" and len(inputs) != 1:
        return None
    if value["transform"] == "Sum" and len(inputs) < 2:
        return None
    return value


def _rule_reuse_reason(head, definition, headers):
    """Explain compatibility without changing a saved or pending relationship."""
    if head is None:
        return "No successfully saved relationship", False
    if head["originstatus"] != "Completed":
        return "The source relationship was not successfully completed", False
    if head["targetmeaning"] != _target_meaning(definition):
        return "The frozen target name or input meaning changed", False
    payload = _stored_rule_payload(head["rulepayload"])
    if (payload is None or head["rulecontractversion"] != RULE_CONTRACT_VERSION
            or head["sourcecontractversion"] != SOURCE_CONTRACT
            or head["targetcontractversion"] != TARGET_CONTRACT
            or head["canonicalizationversion"] != CANONICALIZATION):
        return "The saved relationship format needs remapping", False
    signature = _stored_source_signature(head["sourcesignature"], payload)
    if signature is None:
        return "The saved source relationship needs remapping", False
    try:
        inputs = [resolve_selector(selector, headers, required=False,
                                   allow_unobserved_arrangements=True)
                  for selector in payload["inputs"]]
        for selector in payload["shared_columns"].values():
            resolve_selector(selector, headers, allow_unobserved_arrangements=True)
    except ImportFailure:
        return "A relevant header or duplicate arrangement changed", False
    if any(column is None for result in inputs for column in result.values()):
        return "Known source columns are absent", True
    return "Matched the current successful relationship", True


def _stored_source_signature(value, payload):
    if (payload is None or not isinstance(value, dict)
            or set(value) - {"inputs", "ignored_columns"}
            or value.get("inputs") != payload["inputs"]
            or not isinstance(value.get("ignored_columns", []), list)):
        return None
    try:
        for selector in value.get("ignored_columns", []):
            ColumnSelector.model_validate(selector)
    except ValueError:
        return None
    return value


async def accept_mapping(request, engine, cid, uid, iid, body: MappingRequest):
    """Save one revisioned mapping snapshot without promoting current rule heads."""
    async with engine.begin() as db:
        row = await _lock_mutable(db, cid, uid, iid, body.expected_revision)
        await _save_mapping(request, db, cid, uid, iid, row, body)
        return await metadata(db, cid, iid, policy=_policy(request))


async def _prepare_current_rules(request, db, cid, uid, iid):
    """Reuse unambiguous successful rules during mutation, never during a GET."""
    if await db.scalar(text("""
        SELECT EXISTS (SELECT 1 FROM import.PayrollWorkPendingRules
                       WHERE CompanyID=:cid AND ImportID=:iid)
    """), {"cid": cid, "iid": iid}):
        return
    row = await load_import(db, cid, iid)
    _files, units, layouts = await load_sources(db, cid, iid)
    selected = {item["sourcelayoutid"] for item in units if item["isselected"]}
    headers = {item["sourcelayoutid"]: (item["inspectionmetadata"] or {}).get("header_cells", [])
               for item in layouts if item["sourcelayoutid"] in selected}
    if not headers:
        return
    definitions = await list_period_definitions(row["payrollperiodid"], cid, db, active_only=True)
    operational = {item["target"] for item in mapping_targets(definitions)}
    definitions_by_id = {item.pay_definition_id: item for item in definitions
                         if target_key(item) in operational}
    heads = (await db.execute(text("""
        SELECT h.PayDefinitionID, h.Revision, r.RulePayload, r.SourceSignature, r.TargetMeaning,
               r.RuleContractVersion, r.SourceContractVersion, r.TargetContractVersion,
               r.CanonicalizationVersion, origin.Status AS OriginStatus
        FROM import.PayrollWorkRuleHeads h
        JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID=h.RuleVersionID
          AND r.CompanyID=h.CompanyID AND r.BranchID=h.BranchID AND r.PayDefinitionID=h.PayDefinitionID
        JOIN import.Imports origin ON origin.ImportID=r.OriginImportID AND origin.CompanyID=r.CompanyID
        WHERE h.CompanyID=:cid AND h.BranchID=:bid AND h.PayDefinitionID=ANY(:definitions)
        ORDER BY h.PayDefinitionID FOR UPDATE OF h
    """), {"cid": cid, "bid": row["branchid"],
           "definitions": sorted(definitions_by_id)})).mappings().all()
    groups = defaultdict(list)
    for head in heads:
        definition = definitions_by_id[head["paydefinitionid"]]
        payload = _stored_rule_payload(head["rulepayload"])
        signature = _stored_source_signature(head["sourcesignature"], payload)
        if (head["originstatus"] != "Completed" or payload is None
                or head["targetmeaning"] != _target_meaning(definition)
                or head["rulecontractversion"] != RULE_CONTRACT_VERSION
                or head["sourcecontractversion"] != SOURCE_CONTRACT
                or head["targetcontractversion"] != TARGET_CONTRACT
                or head["canonicalizationversion"] != CANONICALIZATION
                or signature is None
                or not {"DriverNumber", "EmployeeKey", "DriverName"}.intersection(payload["shared_columns"])):
            continue
        try:
            resolutions = [resolve_selector(item, headers, required=False,
                                            allow_unobserved_arrangements=True)
                           for item in payload["inputs"]]
            for selector in payload["shared_columns"].values():
                resolve_selector(selector, headers, allow_unobserved_arrangements=True)
        except ImportFailure:
            continue
        if not any(column is not None for result in resolutions for column in result.values()):
            continue
        groups[encoded(payload["shared_columns"])].append((head, definition, payload, resolutions))
    supplied_groups = [items for items in groups.values()
                       if any(column is not None for _head, _definition, _payload, resolutions in items
                              for resolution in resolutions for column in resolution.values())]
    # Different source dependencies or competing owners need an explicit choice.
    if len(supplied_groups) != 1:
        return
    candidates = supplied_groups[0]
    consumed = {}
    for head, _definition, _payload, resolutions in candidates:
        for resolution in resolutions:
            for layout_id, column in resolution.items():
                if column is None:
                    continue
                key = (layout_id, column)
                if key in consumed:
                    return
                consumed[key] = head["paydefinitionid"]
    shared = candidates[0][2]["shared_columns"]
    selected_inputs = [selector for _head, _definition, payload, _resolutions in candidates
                       for selector in payload["inputs"]]
    ignores = {}
    for head, _definition, _payload, _resolutions in candidates:
        for selector in head["sourcesignature"].get("ignored_columns", []):
            try:
                selector = _selector_payload(ColumnSelector.model_validate(selector))
                resolve_selector(selector, headers, required=False, allow_unobserved_arrangements=True)
            except (ImportFailure, ValueError):
                continue
            if any(selectors_overlap(selector, item) for item in [*selected_inputs, *shared.values()]):
                continue
            ignores[selector_identity(selector)] = selector
    try:
        ensure_duplicate_outcomes(headers, [*selected_inputs, *shared.values(), *ignores.values()],
                                  allow_unobserved_arrangements=True)
    except ImportFailure:
        return
    body = MappingRequest.model_validate({
        "expected_revision": row["revision"], "shared_columns": shared,
        "ignored_columns": list(ignores.values()),
        "targets": [{"target": target_key(definition), "transform": payload["transform"],
                     "inputs": payload["inputs"], "expected_head_revision": head["revision"]}
                    for head, definition, payload, _resolutions in candidates],
    })
    await _save_mapping(request, db, cid, uid, iid, row, body)


async def _save_mapping(request, db, cid, uid, iid, row, body: MappingRequest):
    """Persist validated proposals under the caller's period and Import locks."""
    roles = {"DriverNumber", "EmployeeKey", "DriverName", "WorkDate"}
    await require_supported_import_contract(db, cid, iid)
    _files, units, layouts = await load_sources(db, cid, iid)
    _validate_header_policy(units, _product_policy(request).max_import_combined_columns)
    selected_layout_ids = {u["sourcelayoutid"] for u in units if u["isselected"]}
    if not selected_layout_ids:
        raise failure("IMPORT_SOURCE_SELECTION_REQUIRED", "Select at least one source unit before mapping.", 422)
    layout_headers = {}
    for layout in layouts:
        if layout["sourcelayoutid"] not in selected_layout_ids:
            continue
        inspection = layout["inspectionmetadata"] or {}
        if "header_cells" not in inspection:
            raise failure("IMPORT_HEADER_REQUIRED", "Every selected source must have a readable header.", 422)
        layout_headers[layout["sourcelayoutid"]] = inspection["header_cells"]

    if set(body.shared_columns) - roles:
        raise failure("IMPORT_SHARED_COLUMN_INVALID", "Only Driver identifiers and WorkDate can be shared columns.", 422)
    definitions = await list_period_definitions(row["payrollperiodid"], cid, db, active_only=True)
    importable_targets = {item["target"] for item in mapping_targets(definitions)
                          if item["target"].startswith("Definition:")}
    definitions_by_target = {target_key(item): item for item in definitions
                             if target_key(item) in importable_targets}
    branch_id = row["branchid"]
    active_ids = {item.pay_definition_id for item in definitions
                  if target_key(item) in importable_targets}
    head_rows = (await db.execute(text("""
        SELECT h.PayDefinitionID, h.RuleVersionID, h.Revision, r.RuleFingerprint,
               r.TargetMeaning, r.SourceSignature, r.RulePayload, r.OriginImportID,
               origin.Status AS OriginStatus
        FROM import.PayrollWorkRuleHeads h
        JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID=h.RuleVersionID
          AND r.CompanyID=h.CompanyID AND r.BranchID=h.BranchID AND r.PayDefinitionID=h.PayDefinitionID
        JOIN import.Imports origin ON origin.ImportID=r.OriginImportID AND origin.CompanyID=r.CompanyID
        WHERE h.CompanyID=:cid AND h.BranchID=:bid AND h.PayDefinitionID=ANY(:definitions)
        ORDER BY h.PayDefinitionID FOR UPDATE OF h
    """), {"cid": cid, "bid": branch_id, "definitions": sorted(active_ids)})).mappings().all()
    heads_by_definition = {item["paydefinitionid"]: item for item in head_rows}
    pending_rows = (await db.execute(text("""
        SELECT p.PayDefinitionID, p.RuleVersionID, p.Revision, p.ExpectedHeadRevision,
               p.PayrollPeriodDefinitionID, p.RateDefinitionID,
               r.RuleFingerprint, r.TargetMeaning, r.SourceSignature, r.RulePayload,
               r.OriginImportID, origin.Status AS OriginStatus
        FROM import.PayrollWorkPendingRules p
        JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID=p.RuleVersionID
          AND r.CompanyID=p.CompanyID AND r.BranchID=p.BranchID AND r.PayDefinitionID=p.PayDefinitionID
        JOIN import.Imports origin ON origin.ImportID=r.OriginImportID AND origin.CompanyID=r.CompanyID
        WHERE p.ImportID=:iid AND p.CompanyID=:cid AND p.BranchID=:bid
    """), {"iid": iid, "cid": cid, "bid": branch_id})).mappings().all()
    pending_by_definition = {item["paydefinitionid"]: item for item in pending_rows}
    if len(body.targets) > len(definitions_by_target):
        raise failure("IMPORT_RULE_TARGET_INVALID", "Too many targets for this period's frozen inputs.", 422)
    confirmed_selectors = []
    for candidate in [*pending_rows, *head_rows]:
        if "originstatus" in candidate and candidate["originstatus"] != "Completed":
            continue
        payload = _stored_rule_payload(candidate["rulepayload"])
        if payload is not None:
            confirmed_selectors.extend(payload["inputs"])
            confirmed_selectors.extend(payload["shared_columns"].values())
            signature = _stored_source_signature(candidate["sourcesignature"], payload)
            if signature is not None:
                confirmed_selectors.extend(signature.get("ignored_columns", []))
    observed = [describe_headers(headers) for headers in layout_headers.values()]
    reference_budget = sum(item["headed_column_count"] for item in observed) + len(confirmed_selectors)
    alternative_budget = (sum(len(item["duplicate_header_signatures"]) + 1 for item in observed)
                          + sum(len(item.get("occurrences") or []) for item in confirmed_selectors
                                if isinstance(item, dict)))
    references = [*body.shared_columns.values(), *body.ignored_columns,
                  *(selector for target in body.targets for selector in target.inputs)]
    if (len(body.ignored_columns) > reference_budget
            or any(len(target.inputs) > reference_budget for target in body.targets)
            or any(len(item.occurrences or []) > alternative_budget for item in references)):
        raise failure("IMPORT_MAPPING_CONFIGURATION_TOO_LARGE",
                      "Mapping choices exceed the supplied headers and known saved relationships.", 422)
    if any(len(item.header.encode("utf-8")) > _policy(request).max_cell_text_bytes for item in references):
        raise failure("IMPORT_MAPPING_HEADER_TOO_LARGE",
                      "A selected header exceeds the current source-text resource limit.", 422)

    def known_selector(selector, definition_id=None):
        normalized = _selector_payload(ColumnSelector.model_validate(selector))
        for candidate in [*pending_rows, *head_rows]:
            if definition_id is not None and candidate["paydefinitionid"] != definition_id:
                continue
            if "originstatus" in candidate and candidate["originstatus"] != "Completed":
                continue
            payload = _stored_rule_payload(candidate["rulepayload"])
            if payload is None:
                continue
            confirmed = [*payload["inputs"], *payload["shared_columns"].values()]
            signature = _stored_source_signature(candidate["sourcesignature"], payload)
            if signature is not None:
                confirmed.extend(signature.get("ignored_columns", []))
            if normalized in confirmed:
                return True
        return False

    def resolve_request(selector, definition_id=None):
        confirmed = known_selector(selector, definition_id)
        return resolve_selector(selector, layout_headers, required=not confirmed,
                                allow_unobserved_arrangements=confirmed)

    role_owners = []
    role_physical_owners = {}
    for role, selector in body.shared_columns.items():
        if any(selectors_overlap(selector.model_dump(), previous)
               for previous, _previous_role in role_owners):
            raise failure("IMPORT_SHARED_COLUMN_AMBIGUOUS",
                          "One source occurrence cannot serve two structural roles.", 422)
        role_owners.append((selector.model_dump(), role))
        resolved = resolve_request(selector.model_dump())
        for layout_id, column in resolved.items():
            if column is None:
                continue
            key = (layout_id, column)
            if key in role_physical_owners:
                raise failure("IMPORT_SHARED_COLUMN_AMBIGUOUS",
                              "One physical column cannot serve two structural roles.", 422)
            role_physical_owners[key] = role
    if len({item.target for item in body.targets}) != len(body.targets):
        raise failure("IMPORT_RULE_TARGET_DUPLICATE", "Submit each Pay Item target only once.", 422)
    if not body.targets and not body.shared_columns and not body.ignored_columns:
        raise failure("IMPORT_MAPPING_EMPTY", "Provide at least one mapping or explicit duplicate outcome.", 422)

    ignored_resolved = [resolve_request(item.model_dump())
                        for item in body.ignored_columns]
    ignored_identities = {selector_identity(item.model_dump()) for item in body.ignored_columns}
    if len(ignored_identities) != len(body.ignored_columns):
        raise failure("IMPORT_COLUMN_OUTCOME_DUPLICATE", "A duplicate occurrence may be ignored only once.", 422)
    ignored_physical = {(layout_id, column) for resolved in ignored_resolved
                        for layout_id, column in resolved.items() if column is not None}
    target_resolved = {}
    quantity_owners = []
    quantity_physical_owners = {}
    quantity_selectors = []
    for rule in body.targets:
        definition = definitions_by_target.get(rule.target)
        if definition is None:
            raise failure("IMPORT_RULE_TARGET_INVALID", "Pay Item target is not an active frozen definition in this period.", 422)
        resolved = []
        for selector in rule.inputs:
            selector_data = selector.model_dump()
            if any(selectors_overlap(selector_data, previous) for previous, _previous_target in quantity_owners):
                raise failure("IMPORT_QUANTITY_COLUMN_REUSED",
                              "A quantity occurrence can supply only one Pay Item target.", 422)
            if any(selectors_overlap(selector_data, shared.model_dump())
                   for shared in body.shared_columns.values()):
                raise failure("IMPORT_STRUCTURAL_COLUMN_AS_QUANTITY",
                              "A Driver or WorkDate column cannot also supply a quantity.", 422)
            if any(selectors_overlap(selector_data, item.model_dump()) for item in body.ignored_columns):
                raise failure("IMPORT_COLUMN_OUTCOME_DUPLICATE",
                              "A column occurrence cannot be both used and ignored.", 422)
            quantity_owners.append((selector_data, rule.target))
            quantity_selectors.append(selector_data)
            physical = resolve_request(selector_data, definition.pay_definition_id)
            for layout_id, column in physical.items():
                if column is None:
                    continue
                key = (layout_id, column)
                if key in quantity_physical_owners:
                    raise failure("IMPORT_QUANTITY_COLUMN_REUSED",
                                  "A physical quantity occurrence can supply only one target input.", 422)
                if key in role_physical_owners:
                    raise failure("IMPORT_STRUCTURAL_COLUMN_AS_QUANTITY",
                                  "A Driver or WorkDate column cannot also supply a quantity.", 422)
                quantity_physical_owners[key] = rule.target
            resolved.append(physical)
        target_resolved[rule.target] = (definition, rule, resolved)

    outcome_selectors = [selector.model_dump() for selector in body.shared_columns.values()]
    outcome_selectors.extend(item.model_dump() for item in body.ignored_columns)
    outcome_selectors.extend(quantity_selectors)
    for selector in body.ignored_columns:
        if any(selectors_overlap(selector.model_dump(), item) for item, _target in quantity_owners) or any(
                selectors_overlap(selector.model_dump(), value.model_dump())
                for value in body.shared_columns.values()):
            raise failure("IMPORT_COLUMN_OUTCOME_DUPLICATE", "A column occurrence cannot be both used and ignored.", 422)

    prepared = {}
    shared_json = {role: _selector_payload(selector) for role, selector in body.shared_columns.items()}

    ignored_json = sorted((_selector_payload(item) for item in body.ignored_columns), key=encoded)
    remembered = {selector_identity(item) for item in ignored_json}
    consumed = [*shared_json.values(), *quantity_selectors]
    for candidate in [*pending_rows, *head_rows]:
        if "originstatus" in candidate and candidate["originstatus"] != "Completed":
            continue
        source = _stored_source_signature(candidate["sourcesignature"], _stored_rule_payload(candidate["rulepayload"]))
        if source is None:
            continue
        payload = _stored_rule_payload(candidate["rulepayload"])
        if payload["shared_columns"] != shared_json:
            continue
        requested = next((entry for entry in body.targets
                          if definitions_by_target[entry.target].pay_definition_id == candidate["paydefinitionid"]), None)
        if requested is not None:
            requested_inputs = sorted((_selector_payload(item) for item in requested.inputs), key=encoded)
            if requested_inputs != payload["inputs"] or requested.transform != payload["transform"]:
                continue
        else:
            try:
                present = any(column is not None for selector in payload["inputs"]
                              for column in resolve_selector(selector, layout_headers, required=False,
                                                             allow_unobserved_arrangements=True).values())
            except ImportFailure:
                continue
            if not present:
                continue
        for ignored in source.get("ignored_columns", []):
            try:
                ignored = _selector_payload(ColumnSelector.model_validate(ignored))
                resolve_selector(ignored, layout_headers, required=False,
                                 allow_unobserved_arrangements=True)
            except (ImportFailure, ValueError):
                continue
            identity = selector_identity(ignored)
            if identity in remembered or any(selectors_overlap(ignored, item) for item in consumed):
                continue
            ignored_json.append(ignored)
            remembered.add(identity)
    ignored_json.sort(key=encoded)
    outcome_selectors = [*shared_json.values(), *quantity_selectors, *ignored_json]


    async def save_or_reuse(definition, transform, input_payload, source_signature, *, head_revision,
                            replace_existing, payload_shared=None, reuse_version_id=None):
        pay_definition_id = definition.pay_definition_id
        meaning = _target_meaning(definition)
        payload = {"payload_version": 1, "transform": transform, "inputs": input_payload,
                   "shared_columns": payload_shared if payload_shared is not None else shared_json}
        # Ancillary Ignore choices help repeat setup, but are not dependencies
        # of another target's quantity meaning.
        source_signature = {"inputs": input_payload, "ignored_columns": ignored_json}
        fingerprint_value = fingerprint("payroll-work-rule-v1", {
            "target": meaning, "source": {"inputs": input_payload}, "payload": payload})
        if len(_json({"source": source_signature, "target": meaning, "payload": payload}).encode("utf-8")) > _policy(request).max_mapping_request_bytes:
            raise failure("IMPORT_MAPPING_CONFIGURATION_TOO_LARGE",
                          "Saved mapping metadata exceeds the current configuration resource limit.", 422)
        head = heads_by_definition.get(pay_definition_id)
        actual_head = head["revision"] if head else 0
        if head_revision is not None and head_revision != actual_head:
            raise failure("IMPORT_RULE_HEAD_STALE", "The current saved rule changed. Read it again and retry.")
        same_head = bool(head and head["targetmeaning"] == meaning
                         and head["rulefingerprint"] == fingerprint_value
                         and _stored_rule_payload(head["rulepayload"]) is not None)
        if head and not same_head and not replace_existing:
            raise failure("IMPORT_RULE_REPLACEMENT_REQUIRED",
                          "This target already has a saved rule. Confirm replacement to change it.", 409)
        existing = pending_by_definition.get(pay_definition_id)
        existing_is_current_completed_head = bool(
            head and head["ruleversionid"] == existing["ruleversionid"]
            and head["originstatus"] == "Completed") if existing else False
        existing_version_is_owned_or_current = bool(
            existing and (existing["originimportid"] == iid or existing_is_current_completed_head))
        if (existing and existing_version_is_owned_or_current
                and existing["targetmeaning"] == meaning
                and existing["rulefingerprint"] == fingerprint_value
                and existing["sourcesignature"] == source_signature):
            rule_version_id = existing["ruleversionid"]
            pending_revision = (existing["revision"] if existing["expectedheadrevision"] == actual_head
                                else existing["revision"] + 1)
        elif reuse_version_id is not None and head["sourcesignature"] == source_signature:
            rule_version_id = reuse_version_id
            pending_revision = (existing["revision"] if existing else 0) + 1
        elif (same_head and head["originstatus"] == "Completed"
              and head["sourcesignature"] == source_signature):
            rule_version_id = head["ruleversionid"]
            pending_revision = (existing["revision"] if existing else 0) + 1
        else:
            values = {"cid": cid, "bid": branch_id, "definition": pay_definition_id,
                      "iid": iid, "pid": row["payrollperiodid"], "fingerprint": fingerprint_value,
                      "source": _json(source_signature), "meaning": _json(meaning),
                      "payload": _json(payload), "uid": uid,
                      "rule_version": RULE_CONTRACT_VERSION, "source_version": SOURCE_CONTRACT,
                      "target_version": TARGET_CONTRACT, "canonicalization": CANONICALIZATION}
            rule_version_id = (await db.execute(text("""
                INSERT INTO import.PayrollWorkRuleVersions
                    (CompanyID, BranchID, PayDefinitionID, OriginImportID, OriginPayrollPeriodID,
                     RuleContractVersion, SourceContractVersion, TargetContractVersion, CanonicalizationVersion,
                     RuleFingerprint, SourceSignature, TargetMeaning, RulePayload, CreatedByUserID)
                VALUES (:cid,:bid,:definition,:iid,:pid,:rule_version,:source_version,:target_version,
                        :canonicalization,:fingerprint,CAST(:source AS jsonb),
                        CAST(:meaning AS jsonb),CAST(:payload AS jsonb),:uid)
                RETURNING RuleVersionID
            """), values)).scalar_one()
            pending_revision = (existing["revision"] if existing else 0) + 1
        rate_id = definition.rate_definition_id
        if (existing and existing_version_is_owned_or_current
                and existing["targetmeaning"] == meaning
                and existing["rulefingerprint"] == fingerprint_value
                and existing["sourcesignature"] == source_signature
                and existing["ruleversionid"] == rule_version_id
                and existing["expectedheadrevision"] == actual_head
                and existing["payrollperioddefinitionid"] == definition.payroll_period_definition_id
                and existing["ratedefinitionid"] == rate_id):
            return
        # Every actual binding insert/update advances its own pending revision
        # exactly once; only the exact no-op above preserves the old revision.
        pending_revision = existing["revision"] + 1 if existing else 1
        params = {"iid": iid, "cid": cid, "bid": branch_id, "pid": row["payrollperiodid"],
                  "definition": pay_definition_id, "ppd": definition.payroll_period_definition_id,
                  "rate": rate_id, "rule": rule_version_id, "revision": pending_revision,
                  "head_revision": actual_head, "uid": uid}
        if existing:
            await db.execute(text("""
                UPDATE import.PayrollWorkPendingRules SET PayrollPeriodDefinitionID=:ppd,
                    RateDefinitionID=:rate, RuleVersionID=:rule, Revision=:revision,
                    ExpectedHeadRevision=:head_revision, UpdatedByUserID=:uid, UpdatedAtUtc=NOW()
                WHERE ImportID=:iid AND CompanyID=:cid AND PayDefinitionID=:definition
            """), params)
        else:
            await db.execute(text("""
                INSERT INTO import.PayrollWorkPendingRules
                    (ImportID,CompanyID,BranchID,PayrollPeriodID,PayDefinitionID,
                     PayrollPeriodDefinitionID,RateDefinitionID,RuleVersionID,Revision,
                     ExpectedHeadRevision,UpdatedByUserID)
                VALUES (:iid,:cid,:bid,:pid,:definition,:ppd,:rate,:rule,:revision,:head_revision,:uid)
            """), params)
        prepared[pay_definition_id] = rule_version_id

    for target, (definition, rule, resolved) in sorted(
            target_resolved.items(), key=lambda item: item[1][0].pay_definition_id):
        selectors = sorted((_selector_payload(selector) for selector in rule.inputs), key=encoded)
        source_signature = {"inputs": selectors}
        await save_or_reuse(definition, rule.transform, selectors, source_signature,
                            head_revision=rule.expected_head_revision,
                            replace_existing=rule.replace_existing)

    # Reuse an unchanged confirmed rule only when its complete semantic meaning,
    # selectors and shared dependencies match this receiving-period setup.
    requested_ids = {item.pay_definition_id for item, _rule, _resolved in target_resolved.values()}
    head_candidates = list(head_rows)
    candidates = {}
    auto_selector_owners = []
    auto_physical_usage = defaultdict(set)
    for head in head_candidates:
        did = head["paydefinitionid"]
        if did in requested_ids or head["originstatus"] != "Completed":
            continue
        definition = next((item for item in definitions if item.pay_definition_id == did), None)
        payload = _stored_rule_payload(head["rulepayload"])
        if definition is None or payload is None or head["targetmeaning"] != _target_meaning(definition):
            continue
        if payload["shared_columns"] != shared_json:
            continue
        source_signature = _stored_source_signature(head["sourcesignature"], payload)
        if source_signature is None:
            continue
        try:
            input_resolutions = [resolve_selector(item, layout_headers, required=False,
                                                  allow_unobserved_arrangements=True)
                                 for item in payload["inputs"]]
            for selector in payload["shared_columns"].values():
                resolve_selector(selector, layout_headers, required=False,
                                 allow_unobserved_arrangements=True)
        except ImportFailure:
            continue
        if not any(column is not None for result in input_resolutions for column in result.values()):
            continue
        if any(selectors_overlap(item, prior) for item in payload["inputs"]
               for prior, _target in quantity_owners):
            continue
        if any(selectors_overlap(item, prior) for item in payload["inputs"]
               for prior, _role in role_owners):
            continue
        if any(selectors_overlap(item, ignored) for item in payload["inputs"]
               for ignored in ignored_json):
            continue
        if any((layout_id, column) in role_physical_owners or (layout_id, column) in ignored_physical
               for resolution in input_resolutions for layout_id, column in resolution.items()
               if column is not None):
            continue
        auto_selector_owners.extend((item, did) for item in payload["inputs"])
        for resolution in input_resolutions:
            for layout_id, column in resolution.items():
                if column is not None:
                    auto_physical_usage[(layout_id, column)].add(did)
        candidates[did] = (head, definition, payload, source_signature)

    # If confirmed heads collide, leave every involved target unresolved.
    conflicted_targets = {did for owners in auto_physical_usage.values()
                          if len(owners) > 1 for did in owners}
    for index, (selector, did) in enumerate(auto_selector_owners):
        for other, other_did in auto_selector_owners[index + 1:]:
            if did != other_did and selectors_overlap(selector, other):
                conflicted_targets.update((did, other_did))
    for did in conflicted_targets:
        candidates.pop(did, None)
    auto_selectors = [selector for _head, _definition, payload, _source in candidates.values()
                      for selector in payload["inputs"]]
    ensure_duplicate_outcomes(layout_headers, outcome_selectors + auto_selectors,
                              allow_unobserved_arrangements=True)

    for definition in definitions:
        did = definition.pay_definition_id
        if not definition.is_active or did in requested_ids:
            continue
        existing = pending_by_definition.get(did)
        candidate = candidates.get(did)
        if existing and candidate is None:
            await db.execute(text("""
                DELETE FROM import.PayrollWorkPendingRules
                WHERE ImportID=:iid AND CompanyID=:cid AND PayDefinitionID=:definition
            """), {"iid": iid, "cid": cid, "definition": did})
        if candidate:
            head, definition, payload, source_signature = candidate
            await save_or_reuse(definition, payload["transform"], payload["inputs"], source_signature,
                                head_revision=head["revision"], replace_existing=False,
                                payload_shared=payload["shared_columns"],
                                reuse_version_id=head["ruleversionid"])
    if set(pending_by_definition) - active_ids:
        for did in set(pending_by_definition) - active_ids:
            await db.execute(text("""
                DELETE FROM import.PayrollWorkPendingRules
                WHERE ImportID=:iid AND CompanyID=:cid AND PayDefinitionID=:definition
            """), {"iid": iid, "cid": cid, "definition": did})

    await clear_derived_state(db, cid, iid, reset_mapping=False)
    for layout_id in selected_layout_ids:
        ignored_physical = {
            str(column): "Ignore" for selector in ignored_json
            if (column := resolve_selector(selector, layout_headers, required=False,
                                           allow_unobserved_arrangements=True)[layout_id]) is not None
        }
        await db.execute(text("""
            UPDATE import.SourceLayouts SET ColumnMapping=CAST(:resolution AS jsonb), MappingFingerprint=NULL
            WHERE ImportID=:iid AND CompanyID=:cid AND SourceLayoutID=:layout
        """), {"resolution": _json(ignored_physical),
               "iid": iid, "cid": cid, "layout": layout_id})
    await db.execute(text("""
        UPDATE import.imports SET revision=revision+1, status='NeedsMapping',
            updatedbyuserid=:uid, updatedatutc=NOW() WHERE importid=:iid AND companyid=:cid
    """), {"uid": uid, "iid": iid, "cid": cid})


async def _reject_headerless_mapping(db, cid: int, iid: int, layout_id: int, mapping: dict[str, str]) -> None:
    """Headerless physical columns remain ignored even for handcrafted mappings."""
    mapped_columns = {str(column) for column, target in mapping.items() if target != "Ignore"}
    if not mapped_columns:
        return
    rows = (await db.execute(text("""
        SELECT inspectionmetadata FROM import.filesourceunits
        WHERE importid = :iid AND companyid = :cid AND sourcelayoutid = :layout
    """), {"iid": iid, "cid": cid, "layout": layout_id})).mappings()
    for row in rows:
        inspection = row["inspectionmetadata"] or {}
        headers = inspection.get("header_cells", [])
        headerless = {str(column) for column in inspection.get("headerless_columns", [])}
        unmappable = set()
        for column in mapped_columns:
            if column in headerless:
                unmappable.add(column)
                continue
            try:
                position = int(column) - 1
            except ValueError:
                unmappable.add(column)
                continue
            if position < 0 or position >= len(headers) or not str(headers[position]).strip():
                unmappable.add(column)
        if unmappable:
            raise failure(
                "IMPORT_HEADERLESS_COLUMN_UNSUPPORTED",
                "Columns without first-row headers are ignored and cannot be mapped.",
                422,
            )


def _layout_rule_mappings(layouts, units, pending_rules):
    """Compile immutable target rules into bounded per-layout preview instructions."""
    selected_layout_ids = {unit["sourcelayoutid"] for unit in units if unit["isselected"]}
    headers_by_layout = {layout["sourcelayoutid"]: (layout["inspectionmetadata"] or {}).get("header_cells", [])
                         for layout in layouts if layout["sourcelayoutid"] in selected_layout_ids}
    mappings = {}
    quantity_rules = {}
    duplicate_selectors = []
    for rule in pending_rules:
        payload = _stored_rule_payload(rule["rulepayload"])
        if payload is None:
            raise failure("IMPORT_RULE_PAYLOAD_UNSUPPORTED",
                          "A saved target rule uses an unsupported mapping format. Remap this target.", 409)
        duplicate_selectors.extend(payload["inputs"])
        duplicate_selectors.extend(payload["shared_columns"].values())
    for layout in layouts:
        layout_id = layout["sourcelayoutid"]
        if layout_id not in headers_by_layout:
            continue
        headers = headers_by_layout[layout_id]
        resolution = layout.get("columnmapping") or {}
        ignores = []
        for occurrence in describe_headers(headers)["header_occurrences"]:
            if resolution.get(str(occurrence["source_column"])) == "Ignore":
                selector = {"header": occurrence["normalized_label"]}
                if occurrence["duplicate_signature"]:
                    selector["occurrences"] = [{
                        "duplicate_signature": occurrence["duplicate_signature"],
                        "occurrence_ordinal": occurrence["occurrence_ordinal"],
                    }]
                ignores.append(selector)
        ensure_duplicate_outcomes({layout_id: headers}, duplicate_selectors + ignores,
                                  allow_unobserved_arrangements=True)
    for layout_id, headers in headers_by_layout.items():
        detail = describe_headers(headers)
        mapping = {str(item["source_column"]): "Ignore" for item in detail["header_occurrences"]}
        mappings[layout_id] = mapping
    for rule in pending_rules:
        payload = _stored_rule_payload(rule["rulepayload"])
        if payload is None:
            raise failure("IMPORT_RULE_PAYLOAD_UNSUPPORTED",
                          "A saved target rule uses an unsupported mapping format. Remap this target.", 409)
        target = f"Definition:{rule['payrollperioddefinitionid']}"
        selectors = payload["inputs"]
        resolved_inputs = [resolve_selector(selector, headers_by_layout, required=False,
                                             allow_unobserved_arrangements=True)
                           for selector in selectors]
        for layout_id in headers_by_layout:
            columns = [resolved[layout_id] for resolved in resolved_inputs]
            quantity_rules.setdefault(layout_id, []).append({
                "target": target, "transform": payload["transform"], "columns": columns,
                    "source_signature": {"inputs": payload["inputs"]},
            })
        for role, selector in payload["shared_columns"].items():
            resolved = resolve_selector(selector, headers_by_layout, required=False,
                                        allow_unobserved_arrangements=True)
            for layout_id, column in resolved.items():
                if column is not None:
                    mappings[layout_id][str(column)] = role
    return mappings, quantity_rules


def _materialize_batch(files, units, layouts, policy, product_policy=None, pending_rules=None):
    if product_policy is None:
        from app.product_policy import DEFAULT_PRODUCT_POLICY
        product_policy = DEFAULT_PRODUCT_POLICY
    files_by_id = {item["filesourceid"]: item for item in files}
    layouts_by_id = {item["sourcelayoutid"]: item for item in layouts}
    if len(files) > policy.max_files or len(units) > policy.max_source_units or len(layouts) > policy.max_layouts:
        raise ImportFailure("IMPORT_SOURCE_LIMIT_EXCEEDED", "This Import exceeds current file, unit or layout limits.")
    total_upload_bytes = total_decompressed_bytes = 0
    canonical_by_file = {}
    for file in files:
        if file["filesizebytes"] > policy.max_file_bytes:
            raise ImportFailure("IMPORT_FILE_TOO_LARGE", "A stored file exceeds the current upload limit.")
        total_upload_bytes += file["filesizebytes"]
        file_inspection = file["inspectionmetadata"] or {}
        decompressed = int(file_inspection.get("decompressed_bytes", 0))
        if decompressed > policy.max_file_decompressed_bytes:
            raise ImportFailure("IMPORT_DECOMPRESSED_LIMIT_EXCEEDED", "A workbook exceeds the current decompressed-byte limit.")
        total_decompressed_bytes += decompressed
        if file["contentpurgedatutc"] is not None:
            raise ImportFailure("IMPORT_CONTENT_PURGED", "Source file content has been purged and cannot be previewed.")
        if file.get("canonicalpayload") is None or file.get("canonicalbytecount") is None:
            raise ImportFailure("IMPORT_CANONICAL_CONTENT_MISSING",
                                "Canonical file data is unavailable. Reset this Import and upload the files again.", 409)
        if int(file["canonicalbytecount"]) > product_policy.max_import_canonical_file_bytes:
            raise ImportFailure("IMPORT_CANONICAL_FILE_TOO_LARGE",
                                "Stored file data exceeds the current canonical-data limit. Reset and upload again.")
    if total_upload_bytes > policy.max_total_file_bytes:
        raise ImportFailure("IMPORT_TOTAL_FILE_BYTES_EXCEEDED", "Stored files exceed the current aggregate upload limit.")
    if total_decompressed_bytes > policy.max_total_decompressed_bytes:
        raise ImportFailure("IMPORT_TOTAL_DECOMPRESSED_LIMIT_EXCEEDED", "Import exceeds current decompressed-byte limits.")
    for unit in units:
        inspection = unit["inspectionmetadata"] or {}
        if (int(inspection.get("row_count", 0)) > policy.max_rows_per_unit
                or int(inspection.get("column_count", 0)) > policy.max_columns_per_unit
                or int(inspection.get("cell_count", 0)) > policy.max_cells_per_unit):
            raise ImportFailure("IMPORT_SOURCE_LIMIT_EXCEEDED", "A stored source exceeds current per-unit inspection limits.")
    for file in files:
        canonical_by_file[file["filesourceid"]] = decode_canonical_file(
            file["canonicalpayload"], expected_bytes=int(file["canonicalbytecount"]),
            expected_sha256=file["canonicalsha256"], format_version=int(file["canonicalformatversion"]),
            maximum_bytes=product_policy.max_import_canonical_file_bytes,
            resource_policy=policy, maximum_columns=product_policy.max_import_combined_columns)
    selected = [unit for unit in units if unit["isselected"]]
    if not selected:
        raise ImportFailure("IMPORT_SOURCE_SELECTION_REQUIRED", "Select at least one source unit before preview.")
    if any(unit["sourcelayoutid"] not in layouts_by_id for unit in selected):
        raise ImportFailure("IMPORT_LAYOUT_REQUIRED", "Every selected source unit must belong to a layout.")
    aggregate_rows = aggregate_cells = candidates = 0
    rule_mappings, quantity_rules = _layout_rule_mappings(layouts, units, pending_rules or [])
    for unit in selected:
        inspection = unit["inspectionmetadata"] or {}
        layout = layouts_by_id[unit["sourcelayoutid"]]
        mapping = rule_mappings.get(unit["sourcelayoutid"], {})
        if not mapping:
            raise ImportFailure("IMPORT_MAPPING_REQUIRED", "Accept a mapping for every selected source layout.")
        imported_rows = inspection.get("imported_row_count")
        if imported_rows is None:
            imported_rows = max(0, int(inspection.get("row_count", 0)) - unit["headerrownumber"])
        else:
            imported_rows = max(0, int(imported_rows))
        aggregate_rows += imported_rows
        aggregate_cells += int(inspection.get("cell_count", 0))
        candidates += imported_rows * sum(
            any(column is not None for column in rule["columns"])
            for rule in quantity_rules.get(unit["sourcelayoutid"], []))
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
        canonical = canonical_by_file[file_id]
        materialized_by_name = {}
        for unit in file_units:
            inspection = unit["inspectionmetadata"] or {}
            materialized = canonical_source_unit(canonical, unit_name=unit["unitname"], date_order=unit["dateorder"])
            materialized["inspection"].update(inspection)
            inspection["filename_date_candidates"] = _date_candidates(file["originalfilename"])
            inspection["unit_date_candidates"] = _date_candidates(unit["unitname"])
            materialized_by_name[unit["unitname"]] = materialized
        for unit in file_units:
            layout = layouts_by_id[unit["sourcelayoutid"]]
            source_rows.append({"source_unit_id": unit["sourceunitid"], "file_ordinal": unit["fileordinal"],
                "unit_name": unit["unitname"], "unit_kind": unit["unitkind"],
                "structure_fingerprint": layout["structurefingerprint"],
                "mapping": rule_mappings.get(unit["sourcelayoutid"], {}),
                "quantity_rules": quantity_rules.get(unit["sourcelayoutid"], []),
                "date_source": unit["datesource"] or {}, "date_order": unit["dateorder"],
                "materialized": materialized_by_name[unit["unitname"]]})
    return source_rows


def _stable_identifiers(sources):
    found = {"DriverNumber": set(), "EmployeeKey": set(), "DriverName": set()}
    for source in sources:
        mapping = source["mapping"]
        rules = source.get("quantity_rules", [])
        has_sum = any(rule.get("transform") == "Sum" for rule in rules)

        def has_quantity(row):
            return any(column is not None and not blank(row["cells"].get(str(column),
                {"value": None, "missing_cache": False}))
                for rule in rules for column in rule.get("columns", []))

        active_rows = [row for row in source["materialized"]["rows"] if has_quantity(row) or
            has_sum and any(target in IDENTIFIERS | {"WorkDate"} and
                not blank(row["cells"].get(str(column), {"value": None, "missing_cache": False}))
                for column, target in mapping.items())]
        for column, target in mapping.items():
            if target not in found:
                continue
            for row in active_rows:
                cell = row["cells"].get(column)
                if cell is not None and not cell.missing_cache and cell.value is not None:
                    value = normalized_identifier(target, cell.value)
                    if value:
                        found[target].add(value)
    return {key: sorted(value) for key, value in found.items()}


async def build_preview(request, engine, cid, uid, iid, body: RevisionRequest):
    policy = _policy(request)
    started_rule_context = None
    pending_rules = None
    async with admission(request):
        async with engine.begin() as db:
            row = await load_import(db, cid, iid)
            period = await authorize(db, cid, uid, row["payrollperiodid"], mutate=True)
            if row["revision"] != body.expected_revision:
                raise failure("IMPORT_REVISION_CONFLICT", "Import changed. Read its current revision and retry.")
            if period.status not in SOURCE_ENTRY_STATUSES or row["status"] not in ("NeedsMapping", "NeedsReview") or row["detailpurgedatutc"]:
                raise failure("IMPORT_NOT_EDITABLE", "This Import is no longer editable.")
            files, units, layouts = await load_sources(db, cid, iid)
            _validate_header_policy(units, _product_policy(request).max_import_combined_columns)
            files, units, layouts = await load_sources(db, cid, iid, include_canonical=True)
            started_rule_context = await current_rule_context(db, cid, iid)
            pending_rules = [dict(item) for item in (await db.execute(text("""
                SELECT p.PayDefinitionID, p.PayrollPeriodDefinitionID, r.RulePayload
                FROM import.PayrollWorkPendingRules p
                JOIN import.PayrollWorkRuleVersions r ON r.RuleVersionID=p.RuleVersionID
                  AND r.CompanyID=p.CompanyID AND r.BranchID=p.BranchID
                  AND r.PayDefinitionID=p.PayDefinitionID
                WHERE p.ImportID=:iid AND p.CompanyID=:cid
                ORDER BY p.PayDefinitionID
            """), {"iid": iid, "cid": cid})).mappings()]
            if any(file["contentpurgedatutc"] is not None for file in files):
                raise failure("IMPORT_CONTENT_PURGED", "Source file content has been purged and cannot be previewed.")
            default_date = row["defaultworkdate"]
        materialized = await anyio.to_thread.run_sync(partial(
            _materialize_batch, files, units, layouts, policy, _product_policy(request), pending_rules))
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
            context = await load_context(db, cid, period, dates=sorted(dates), identifiers=identifiers,
                                         resource_policy=policy)
        context_token = fingerprint("relational-context", context_identity(context))
        result = await anyio.to_thread.run_sync(partial(build_batch_preview, materialized, context, policy=policy))
        definitions = context["definitions"]
        inspection_updates = []
        for source in materialized:
            inspection = source["materialized"]["inspection"]
            inspection["accepted_date_order"] = source.get("accepted_date_order")
            inspection["date_order_state"] = source.get("date_order_state")
            inspection_updates.append((source["source_unit_id"], source.get("accepted_date_order"),
                                       inspection.get("encoding"),
                                       inspection.get("delimiter"), _json(inspection)))
        del context
        result = await anyio.to_thread.run_sync(prepare_preview, result)
        del materialized
        started_revision = body.expected_revision
        async with engine.begin() as db:
            await _lock_mutable(db, cid, uid, iid, started_revision)
            published_rule_context = await current_rule_context(db, cid, iid)
            if published_rule_context != started_rule_context:
                raise failure("IMPORT_RULE_CONTEXT_STALE",
                              "Saved mapping rules changed while the preview was being prepared. Refresh and retry.")
            current_context = await load_context(db, cid, period, dates=sorted(dates), identifiers=identifiers,
                                                resource_policy=policy)
            if fingerprint("relational-context", context_identity(current_context)) != context_token:
                raise failure("IMPORT_CONTEXT_STALE", "Payroll context changed while the preview was being prepared. Refresh and retry.")
            _current_files, current_units, current_layouts = await load_sources(db, cid, iid)
            await clear_derived_state(db, cid, iid, reset_mapping=False)
            await persist_preview(db, company_id=cid, import_id=iid, branch_id=period.branch_id,
                                  period_id=period.payroll_period_id, result=result,
                                  rule_context_fingerprint=published_rule_context)
            current_units_by_id = {unit["sourceunitid"]: unit for unit in current_units}
            for source_unit_id, date_order, encoding, delimiter, inspection_json in inspection_updates:
                if source_unit_id not in current_units_by_id:
                    raise failure("IMPORT_REVISION_CONFLICT", "Source units changed while the preview was being prepared.")
                stored_inspection = current_units_by_id[source_unit_id]["inspectionmetadata"] or {}
                prepared_inspection = json.loads(inspection_json)
                for key in ("header_cells", "headed_column_count", "combined_column_count",
                            "header_occurrences", "duplicate_header_signatures", "headerless_columns",
                            "hidden_columns", "notices", "imported_row_count", "row_count",
                            "column_count", "cell_count", "formula_count", "formula_cached_value_count",
                            "formula_empty_cache_count", "formula_missing_cache_count"):
                    if key in stored_inspection:
                        prepared_inspection[key] = stored_inspection[key]
                await db.execute(text("""
                    UPDATE import.filesourceunits SET dateorder = :date_order,
                        encoding = :encoding, delimiter = :delimiter,
                        inspectionmetadata = CAST(:inspection AS jsonb)
                    WHERE sourceunitid = :unit AND importid = :iid AND companyid = :cid
                """), {"date_order": date_order, "encoding": encoding, "delimiter": delimiter,
                       "inspection": _json(prepared_inspection), "unit": source_unit_id, "iid": iid, "cid": cid})
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
            return await metadata(db, cid, iid, policy=_policy(request))


async def cancel_import(engine: AsyncEngine, cid: int, uid: int, iid: int, expected: int,
                        policy: ImportResourcePolicy = DEFAULT_IMPORT_RESOURCE_POLICY):
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
        return await metadata(db, cid, iid, policy=policy)
