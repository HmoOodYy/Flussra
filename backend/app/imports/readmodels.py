"""Revision-bound, response-bounded Import preview projections."""
from __future__ import annotations

from functools import partial

import anyio
from sqlalchemy import text

from app.imports.contracts import require_supported_import_contract
from app.imports.errors import ImportFailure
from app.imports.mapping import canonical, encoded
from app.imports.policy import DEFAULT_IMPORT_RESOURCE_POLICY, ImportResourcePolicy
from app.imports.rules import require_preview_rule_context
from app.imports.runtime import authorize, failure
from app.payroll.period_definitions import list_period_definitions

MAX_RESPONSE_BYTES = DEFAULT_IMPORT_RESOURCE_POLICY.max_response_bytes
ROW_DEFAULT = DEFAULT_IMPORT_RESOURCE_POLICY.row_page_default
ROW_MAX = DEFAULT_IMPORT_RESOURCE_POLICY.row_page_max
ISSUE_DEFAULT = DEFAULT_IMPORT_RESOURCE_POLICY.issue_page_default
ISSUE_MAX = DEFAULT_IMPORT_RESOURCE_POLICY.issue_page_max
SOURCE_DEFAULT = DEFAULT_IMPORT_RESOURCE_POLICY.source_page_default
SOURCE_MAX = DEFAULT_IMPORT_RESOURCE_POLICY.source_page_max
ROW_DETAIL_SAMPLE = DEFAULT_IMPORT_RESOURCE_POLICY.row_detail_sample


def bounded_page(base: dict, items: list[dict], offset: int, limit: int, total: int,
                 *, policy: ImportResourcePolicy = DEFAULT_IMPORT_RESOURCE_POLICY) -> dict:
    response = {**base, "offset": offset, "limit": limit, "filtered_count": total,
                "items": [], "next_offset": None}
    base_size = len(encoded(response).encode("utf-8"))
    if base_size > policy.max_response_bytes:
        raise failure("IMPORT_PREVIEW_BASE_TOO_LARGE", "Preview page metadata exceeds the response limit.", 422)
    selected_size = 0
    for item in items:
        item_size = len(encoded(item).encode("utf-8"))
        next_count = len(response["items"]) + 1
        next_offset = offset + next_count if offset + next_count < total else None
        next_offset_growth = len(str(next_offset)) - len("null") if next_offset is not None else 0
        projected_size = base_size + selected_size + (1 if next_count > 1 else 0) + item_size + next_offset_growth
        if projected_size > policy.max_response_bytes:
            if not response["items"]:
                raise failure("IMPORT_PREVIEW_ITEM_TOO_LARGE", "A source item exceeds the response limit; request source detail separately.", 422)
            break
        response["items"].append(item)
        selected_size += item_size + (1 if next_count > 1 else 0)
    consumed = len(response["items"])
    if offset + consumed < total:
        response["next_offset"] = offset + consumed
    result = canonical(response)
    if len(encoded(result).encode("utf-8")) > policy.max_response_bytes:
        raise failure("IMPORT_PREVIEW_RESPONSE_TOO_LARGE", "Preview page exceeds the configured response limit.", 422)
    return result


def project_source_record(item: dict, cell_offset: int, cell_limit: int) -> dict:
    """Project one source row's cells and matching normalized details as a page."""
    projected = dict(item)
    payload = projected.pop("payload", None) or {}
    context = projected.pop("context_json", None) or {}
    ordered = sorted(payload.items(), key=lambda pair: (0, int(pair[0])) if str(pair[0]).isdigit()
                     else (1, str(pair[0])))
    page = ordered[cell_offset:cell_offset + cell_limit]
    page_columns = {int(key) for key, _ in page if str(key).isdigit()}
    values = projected.get("values_json") or {}
    issues = projected.get("issues") or []
    projected["values_count"] = len(values)
    projected["issue_count"] = len(issues)
    projected["values_json"] = {
        key: dict(value) for key, value in values.items()
        if isinstance(value, dict) and (
            value.get("source_column") in page_columns
            or any(column in page_columns for column in value.get("source_columns", [])))
    }
    for value in projected["values_json"].values():
        _page_operand_details(value, "raw_value", page_columns)
    projected["issues"] = [
        dict(issue) for issue in issues
        if issue.get("column") is None or issue.get("column") in page_columns
    ]
    for issue in projected["issues"]:
        _page_operand_details(issue, "value", page_columns)
    projected["row_handling"] = context.get("row_handling")
    projected["category"] = context.get("category")
    projected["category_decision"] = context.get("category_decision")
    projected["proposed_exclusion"] = context.get("proposed_exclusion")
    projected["cell_count"] = len(ordered)
    projected["cell_offset"] = cell_offset
    projected["cell_limit"] = cell_limit
    projected["cells"] = dict(page)
    projected["next_cell_offset"] = (cell_offset + len(page) if cell_offset + len(page) < len(ordered) else None)
    return canonical(projected)


def _page_operand_details(record: dict, field: str, page_columns: set[int]) -> None:
    """Keep operand provenance available without repeating all operands on each cell page."""
    operands = record.get(field)
    if not isinstance(operands, list) or not all(
        operand is None or isinstance(operand, dict) and "column" in operand
        for operand in operands
    ):
        return
    record[f"{field}_operand_count"] = len(operands)
    record[f"{field}_missing_operand_count"] = sum(operand is None for operand in operands)
    record[field] = [operand for operand in operands
                     if isinstance(operand, dict) and operand["column"] in page_columns]


def _visible_group_predicate(group_alias: str = "g", records_relation: str = "import.payrollworkrecords") -> str:
    """Keep a preview group visible whenever it contains any non-excluded record."""
    return f"""AND EXISTS (
        SELECT 1 FROM {records_relation} visible_row
        WHERE visible_row.previewgroupid={group_alias}.previewgroupid
          AND visible_row.importid={group_alias}.importid AND visible_row.companyid={group_alias}.companyid
          AND NOT (visible_row.contextjson ? 'proposed_exclusion'))"""


async def _read_identity(db, cid: int, uid: int, iid: int, revision: int, plan_fingerprint: str):
    row = (await db.execute(text("""
        SELECT w.payrollperiodid, w.planfingerprint, w.previewcounts, i.revision, i.status
        FROM import.imports i JOIN import.payrollworkimports w
          ON w.importid = i.importid AND w.companyid = i.companyid
        WHERE i.importid = :iid AND i.companyid = :cid
    """), {"iid": iid, "cid": cid})).mappings().first()
    if row is None:
        raise ImportFailure("IMPORT_NOT_FOUND", "Import not found.", 404)
    await authorize(db, cid, uid, row["payrollperiodid"], mutate=False)
    await require_supported_import_contract(db, cid, iid)
    if row["revision"] != revision or row["planfingerprint"] != plan_fingerprint or not plan_fingerprint:
        raise failure("IMPORT_PREVIEW_STALE", "Preview changed. Read current metadata before requesting another page.")
    # A live preview is bound to the current pending-rule context. Terminal
    # Imports retain their historical reads even after a later successful
    # Import advances a target's current rule head.
    if row["status"] in ("NeedsMapping", "NeedsReview"):
        await require_preview_rule_context(db, cid, iid)
    return row


async def read_rows(engine, cid: int, uid: int, iid: int, revision: int,
                    plan_fingerprint: str, offset: int, limit: int, severity: str | None,
                    *, policy: ImportResourcePolicy = DEFAULT_IMPORT_RESOURCE_POLICY):
    if limit < 1 or limit > policy.row_page_max or offset < 0:
        raise failure("IMPORT_PAGE_INVALID", f"Rows require limit 1–{policy.row_page_max} and a non-negative offset.", 422)
    if severity not in (None, "Error", "Warning", "Conflict"):
        raise failure("IMPORT_SEVERITY_INVALID", "Severity must be Error, Warning or Conflict.", 422)
    async with engine.begin() as db:
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        identity = await _read_identity(db, cid, uid, iid, revision, plan_fingerprint)
        params = {"cid": cid, "iid": iid, "severity": severity}
        severity_sql = "AND g.errorcount > 0" if severity == "Error" else "AND g.warningcount > 0" if severity == "Warning" else "AND g.conflictcount > 0" if severity == "Conflict" else ""
        visible_group_sql = _visible_group_predicate()
        total = (await db.execute(text(f"""
            SELECT count(*) FROM import.payrollworkpreviewgroups g
            WHERE g.companyid = :cid AND g.importid = :iid {visible_group_sql} {severity_sql}
        """), params)).scalar_one()
        groups = (await db.execute(text(f"""
            SELECT g.previewgroupid, g.groupfingerprint, g.driverid, g.workdate,
                g.driverdisplay, g.errorcount, g.warningcount, g.conflictcount
            FROM import.payrollworkpreviewgroups g
            WHERE g.companyid = :cid AND g.importid = :iid {visible_group_sql} {severity_sql}
            ORDER BY g.workdate NULLS LAST, g.driverdisplay->>'driver_name' NULLS LAST,
                     g.groupfingerprint OFFSET :offset LIMIT :limit
        """), {**params, "offset": offset, "limit": limit})).mappings().all()
        group_ids = [group["previewgroupid"] for group in groups]
        records_by_group = {key: [] for key in group_ids}
        record_counts = {key: 0 for key in group_ids}
        candidates_by_group = {key: {} for key in group_ids}
        if group_ids:
            for record in (await db.execute(text("""
                WITH ranked AS (
                SELECT r.previewgroupid, r.sourcerecordid, r.driverassignmentid, r.workdate,
                    r.isskipped, r.valuesjson, r.issuesjson, r.contextjson, f.fileordinal, u.unitname,
                    f.originalfilename, fr.sourcerownumber,
                    count(*) OVER (PARTITION BY r.previewgroupid) AS total_records,
                    row_number() OVER (PARTITION BY r.previewgroupid
                        ORDER BY f.fileordinal, u.unitordinal, fr.sourcerownumber) AS record_rank
                FROM import.payrollworkrecords r
                JOIN import.filesourcerecords fr ON fr.sourcerecordid = r.sourcerecordid
                    AND fr.importid = r.importid AND fr.companyid = r.companyid
                JOIN import.filesourceunits u ON u.sourceunitid = fr.sourceunitid
                    AND u.importid = fr.importid AND u.companyid = fr.companyid
                JOIN import.filesources f ON f.filesourceid = u.filesourceid
                    AND f.importid = u.importid AND f.companyid = u.companyid
                WHERE r.companyid = :cid AND r.importid = :iid
                    AND r.previewgroupid = ANY(CAST(:ids AS bigint[]))
                )
                SELECT * FROM ranked WHERE record_rank <= :sample
                ORDER BY previewgroupid, record_rank
            """), {"cid": cid, "iid": iid, "ids": group_ids, "sample": policy.row_detail_sample})).mappings():
                records_by_group[record["previewgroupid"]].append(dict(record))
                record_counts[record["previewgroupid"]] = record["total_records"]
            for candidate in (await db.execute(text("""
                WITH expanded AS (
                    SELECT r.previewgroupid, r.sourcerecordid, entry.key AS definition_id,
                        entry.value AS value, f.fileordinal, f.originalfilename, u.unitname,
                        u.unitordinal, fr.sourcerownumber,
                        count(*) OVER (PARTITION BY r.previewgroupid, entry.key) AS candidate_count,
                        count(*) FILTER (WHERE NOT COALESCE((entry.value->>'is_blank')::boolean, FALSE))
                            OVER (PARTITION BY r.previewgroupid, entry.key) AS nonblank_candidate_count,
                        row_number() OVER (PARTITION BY r.previewgroupid, entry.key
                            ORDER BY COALESCE((entry.value->>'is_blank')::boolean, FALSE),
                                f.fileordinal, u.unitordinal, fr.sourcerownumber) AS candidate_rank
                    FROM import.payrollworkrecords r
                    CROSS JOIN LATERAL jsonb_each(r.valuesjson) AS entry(key, value)
                    JOIN import.filesourcerecords fr ON fr.sourcerecordid=r.sourcerecordid
                        AND fr.importid=r.importid AND fr.companyid=r.companyid
                    JOIN import.filesourceunits u ON u.sourceunitid=fr.sourceunitid
                        AND u.importid=fr.importid AND u.companyid=fr.companyid
                    JOIN import.filesources f ON f.filesourceid=u.filesourceid
                        AND f.importid=u.importid AND f.companyid=u.companyid
                    WHERE r.companyid=:cid AND r.importid=:iid
                        AND COALESCE((entry.value->>'is_candidate')::boolean, TRUE)
                        AND r.previewgroupid = ANY(CAST(:ids AS bigint[]))
                )
                SELECT * FROM expanded WHERE candidate_rank <= :sample
                ORDER BY previewgroupid, definition_id, candidate_rank
            """), {"cid": cid, "iid": iid, "ids": group_ids, "sample": policy.row_detail_sample})).mappings():
                values = candidates_by_group[candidate["previewgroupid"]]
                entry = values.setdefault(str(candidate["definition_id"]), {
                    "candidate_count": candidate["candidate_count"],
                    "nonblank_candidate_count": candidate["nonblank_candidate_count"], "candidates": []})
                value = candidate["value"]
                entry["candidates"].append({"source_record_id": candidate["sourcerecordid"],
                    "file_ordinal": candidate["fileordinal"], "file_name": candidate["originalfilename"],
                    "unit_name": candidate["unitname"], "source_row": candidate["sourcerownumber"],
                    "source_column": value["source_column"], "source_columns": value.get("source_columns", []),
                    "transform": value.get("transform", "Direct"), "quantity": value["quantity"],
                    "is_blank": value["is_blank"], "outcome": value["outcome"],
                    "current_quantity": value["current_quantity"],
                    "current_source_exists": value["current_source_exists"]})
        definitions = await list_period_definitions(identity["payrollperiodid"], cid, db, active_only=True)
        definitions_by_id = {str(d.payroll_period_definition_id): {
            "payroll_period_definition_id": d.payroll_period_definition_id, "code": d.code,
            "name": d.name, "unit": d.unit, "input_type": d.input_type}
            for d in definitions}
        items = []
        for group in groups:
            values = {}
            sources = []
            for record in records_by_group[group["previewgroupid"]]:
                record_issues = record["issuesjson"] or []
                sources.append({"source_record_id": record["sourcerecordid"], "file_ordinal": record["fileordinal"],
                    "file_name": record["originalfilename"], "unit_name": record["unitname"],
                    "source_row": record["sourcerownumber"], "skipped": record["isskipped"],
                    "date_interpretation": (record["contextjson"] or {}).get("date_interpretation"),
                    "proposed_exclusion": (record["contextjson"] or {}).get("proposed_exclusion"),
                    "contributions": {key: {"is_candidate": value.get("is_candidate", True),
                        "contribution_quantity": value.get("contribution_quantity"),
                        "aggregate_status": value.get("aggregate_status"),
                        "outcome": value.get("outcome")}
                        for key, value in (record["valuesjson"] or {}).items()},
                    "issue_count": len(record_issues),
                    "issue_codes": [item["code"] for item in record_issues[:policy.row_detail_sample]]})
            for def_id, candidates in candidates_by_group[group["previewgroupid"]].items():
                values[def_id] = {**definitions_by_id.get(def_id, {"payroll_period_definition_id": int(def_id)}),
                                  **candidates}
            items.append({"driver_id": group["driverid"], "driver": group["driverdisplay"],
                "work_date": group["workdate"], "error_count": group["errorcount"],
                "warning_count": group["warningcount"], "conflict_count": group["conflictcount"],
                "values": values, "source_records": sources,
                "source_record_count": record_counts[group["previewgroupid"]],
                "source_records_sampled": record_counts[group["previewgroupid"]] > len(sources)})
        base = {"revision": identity["revision"], "plan_fingerprint": plan_fingerprint,
                "summary": identity["previewcounts"], "filter": severity}
        return await anyio.to_thread.run_sync(partial(bounded_page, base, items, offset, limit, total, policy=policy))


async def read_issues(engine, cid, uid, iid, revision, plan_fingerprint, offset, limit, severity,
                      *, policy: ImportResourcePolicy = DEFAULT_IMPORT_RESOURCE_POLICY):
    if limit < 1 or limit > policy.issue_page_max or offset < 0:
        raise failure("IMPORT_PAGE_INVALID", f"Issues require limit 1–{policy.issue_page_max} and a non-negative offset.", 422)
    if severity not in (None, "Error", "Warning", "Conflict"):
        raise failure("IMPORT_SEVERITY_INVALID", "Severity must be Error, Warning or Conflict.", 422)
    async with engine.begin() as db:
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        identity = await _read_identity(db, cid, uid, iid, revision, plan_fingerprint)
        issues = """
            SELECT i.importissueid::text AS issue_id, i.severity, i.code, i.message, i.locator,
                NULL::jsonb AS value,
                i.sourcerecordid AS source_record_id, f.originalfilename AS file_name,
                f.fileordinal AS file_ordinal, u.unitname AS unit_name, fr.sourcerownumber AS source_row,
                u.unitordinal AS unit_ordinal
            FROM import.importissues i
            LEFT JOIN import.filesourcerecords fr ON fr.sourcerecordid = i.sourcerecordid
                AND fr.importid=i.importid AND fr.companyid=i.companyid
            LEFT JOIN import.filesourceunits u ON u.sourceunitid = fr.sourceunitid
                AND u.importid=fr.importid AND u.companyid=fr.companyid
            LEFT JOIN import.filesources f ON f.filesourceid=u.filesourceid
                AND f.importid=u.importid AND f.companyid=u.companyid
            WHERE i.companyid=:cid AND i.importid=:iid
            UNION ALL
            SELECT r.sourcerecordid::text || ':' || entry.ordinality::text AS issue_id,
                entry.item->>'severity' AS severity, entry.item->>'code' AS code,
                entry.item->>'message' AS message,
                jsonb_build_object('file_ordinal', f.fileordinal, 'unit_name', u.unitname,
                    'source_row', fr.sourcerownumber, 'column', entry.item->'column') AS locator,
                entry.item->'value' AS value,
                r.sourcerecordid AS source_record_id, f.originalfilename AS file_name,
                f.fileordinal AS file_ordinal, u.unitname AS unit_name, fr.sourcerownumber AS source_row,
                u.unitordinal AS unit_ordinal
            FROM import.payrollworkrecords r
            JOIN import.filesourcerecords fr ON fr.sourcerecordid=r.sourcerecordid
                AND fr.importid=r.importid AND fr.companyid=r.companyid
            JOIN import.filesourceunits u ON u.sourceunitid=fr.sourceunitid
                AND u.importid=fr.importid AND u.companyid=fr.companyid
            JOIN import.filesources f ON f.filesourceid=u.filesourceid
                AND f.importid=u.importid AND f.companyid=u.companyid
            CROSS JOIN LATERAL jsonb_array_elements(r.issuesjson) WITH ORDINALITY AS entry(item, ordinality)
            WHERE r.companyid=:cid AND r.importid=:iid
        """
        predicate = "WHERE severity = :severity" if severity else ""
        params = {"cid": cid, "iid": iid, "severity": severity}
        total = (await db.execute(text(f"SELECT count(*) FROM ({issues}) AS all_issues {predicate}"), params)).scalar_one()
        items = [dict(row) for row in (await db.execute(text(f"""
            SELECT issue_id, severity, code, message, locator, value, source_record_id, file_name,
                file_ordinal, unit_name, source_row
            FROM ({issues}) AS all_issues {predicate}
            ORDER BY file_ordinal NULLS LAST, unit_ordinal NULLS LAST, source_row NULLS LAST, issue_id
            OFFSET :offset LIMIT :limit
        """), {**params, "offset": offset, "limit": limit})).mappings()]
        base = {"revision": identity["revision"], "plan_fingerprint": plan_fingerprint,
                "summary": identity["previewcounts"], "filter": severity}
        return await anyio.to_thread.run_sync(partial(bounded_page, base, items, offset, limit, total, policy=policy))


async def read_sources(engine, cid, uid, iid, revision, plan_fingerprint, offset, limit,
                       cell_offset: int = 0, cell_limit: int | None = None,
                       *, policy: ImportResourcePolicy = DEFAULT_IMPORT_RESOURCE_POLICY):
    cell_limit = policy.source_cell_page_default if cell_limit is None else cell_limit
    if limit < 1 or limit > policy.source_page_max or offset < 0:
        raise failure("IMPORT_PAGE_INVALID", f"Source records require limit 1–{policy.source_page_max} and a non-negative offset.", 422)
    if cell_offset < 0 or cell_limit < 1 or cell_limit > policy.source_cell_page_max:
        raise failure("IMPORT_CELL_PAGE_INVALID", f"Source-cell pages require a non-negative offset and limit 1–{policy.source_cell_page_max}.", 422)
    async with engine.begin() as db:
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        identity = await _read_identity(db, cid, uid, iid, revision, plan_fingerprint)
        params = {"cid": cid, "iid": iid, "offset": offset, "limit": limit}
        total = (await db.execute(text("""
            SELECT count(*) FROM import.filesourcerecords f
            WHERE f.companyid=:cid AND f.importid=:iid
        """), params)).scalar_one()
        items = [dict(row) for row in (await db.execute(text("""
            SELECT r.sourcerecordid AS source_record_id, s.fileordinal AS file_ordinal,
                s.originalfilename AS file_name, u.unitname AS unit_name,
                f.sourcerownumber AS source_row, r.locator, r.payload,
                w.driverid AS driver_id, w.driverassignmentid AS driver_assignment_id,
                w.workdate AS work_date, w.isskipped AS skipped, w.valuesjson AS values_json,
                w.issuesjson AS issues, w.contextjson AS context_json
            FROM import.filesourcerecords f
            JOIN import.importsourcerecords r ON r.sourcerecordid=f.sourcerecordid
                AND r.importid=f.importid AND r.companyid=f.companyid
            JOIN import.filesourceunits u ON u.sourceunitid=f.sourceunitid
                AND u.importid=f.importid AND u.companyid=f.companyid
            JOIN import.filesources s ON s.filesourceid=u.filesourceid
                AND s.importid=u.importid AND s.companyid=u.companyid
            LEFT JOIN import.payrollworkrecords w ON w.sourcerecordid=r.sourcerecordid
                AND w.importid=r.importid AND w.companyid=r.companyid
            WHERE f.companyid=:cid AND f.importid=:iid
            ORDER BY s.fileordinal,u.unitordinal,f.sourcerownumber
            OFFSET :offset LIMIT :limit
        """), params)).mappings()]
        items = [project_source_record(item, cell_offset, cell_limit) for item in items]
        base = {"revision": identity["revision"], "plan_fingerprint": plan_fingerprint,
                "summary": identity["previewcounts"], "filter": None}
        return await anyio.to_thread.run_sync(partial(bounded_page, base, items, offset, limit, total, policy=policy))
