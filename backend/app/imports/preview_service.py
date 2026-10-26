"""Revisioned synchronous mapping/refresh and bounded read-only preview pages."""
from functools import partial
from time import perf_counter

import anyio
from sqlalchemy import text

from app.imports.files import read_mapped_file
from app.imports.mapping import (
    blank,
    canonical,
    encoded,
    fingerprint,
    infer_date_order,
    normalized_identifier,
    parse_date,
    suggest_mapping,
    targets,
)
from app.imports.payroll_context import context_identity, load_context
from app.imports.preview_model import build_preview
from app.imports.preview_store import persist_preview, prepare_preview
from app.imports.service import (
    admission,
    authorize,
    check_versions,
    failure,
    load,
    lock_mutable,
    metadata,
)
from app.payroll.period_definitions import list_period_definitions
from app.payroll.schemas import SOURCE_ENTRY_STATUSES

ROW_PAGE_DEFAULT = 25
ROW_PAGE_MAX = 100
ISSUE_PAGE_DEFAULT = 50
ISSUE_PAGE_MAX = 200
MAX_PREVIEW_RESPONSE_BYTES = 2_000_000


def context_fingerprint(context):
    return fingerprint("context", context_identity(context))


def materialize(content, filename, mapping, period, labels, settings):
    result = read_mapped_file(content, filename,
        [int(column) for column, target in mapping.items() if target != "Ignore"], header_labels=labels, **settings)
    if any(int(column) > result["inspection"]["column_count"] for column in mapping):
        raise failure("IMPORT_MAPPING_INVALID", "A mapped source column does not exist in the selected sheet.", 422)
    date_column = next(column for column, target in mapping.items() if target == "WorkDate")
    quantity_columns = [column for column, target in mapping.items() if target.startswith("Definition:")]
    active = [row for row in result["rows"] if any(not blank(row["cells"][column]) for column in quantity_columns)]
    order, _ = infer_date_order((row["cells"][date_column] for row in active),
                                period.start_date, period.end_date, settings["date_order"])
    result["work_dates"] = sorted({day for row in active if (day := parse_date(row["cells"][date_column], order))
                                   and period.start_date <= day <= period.end_date})
    result["stable_identifiers"] = {kind: sorted({normalized_identifier(kind, row["cells"][column]["value"])
        for row in active for column, target in mapping.items() if target == kind
        and not row["cells"][column]["missing_cache"] and not blank(row["cells"][column])})
        for kind in ("DriverNumber", "EmployeeKey")}
    return result


async def completed_notice(db, cid, row):
    completed = (await db.execute(text("""
        SELECT i.importid AS import_id, i.completedatutc AS completed_at,
            u.displayname AS completed_by_display_name, i.completedbyuserid AS completed_by_user_id,
            f.filehashsha256 = :sha AS byte_match,
            (w.contentfingerprint = :content) IS TRUE AS semantic_match
        FROM import.payrollworkimports w
        JOIN import.imports i ON i.importid = w.importid AND i.companyid = w.companyid
        JOIN import.filesources f ON f.importid = i.importid AND f.companyid = i.companyid
        JOIN sec.users u ON u.userid = i.completedbyuserid AND u.companyid = i.companyid
        WHERE w.companyid = :cid AND w.payrollperiodid = :pid AND i.status = 'Completed'
          AND (f.filehashsha256 = :sha OR w.contentfingerprint = :content)
        ORDER BY i.completedatutc DESC, i.importid DESC LIMIT 1
    """), {"cid": cid, "pid": row["payrollperiodid"], "sha": row["filehashsha256"],
           "content": row["contentfingerprint"]})).mappings().first()
    return dict(completed) if completed else None


async def enrich_metadata(db, cid, row):
    result = metadata(row)
    status = await db.scalar(text("""SELECT status FROM payroll.payrollperiods
        WHERE companyid = :cid AND payrollperiodid = :pid"""), {"cid": cid, "pid": row["payrollperiodid"]})
    result["period_status"] = status
    result["source_editable"] = (status in SOURCE_ENTRY_STATUSES and row["status"] in ("NeedsMapping", "NeedsReview")
                                 and row["contentpurgedatutc"] is None)
    definitions = await list_period_definitions(row["payrollperiodid"], cid, db, active_only=True)
    inspection = row["inspectionmetadata"] or {}
    result["mapping_targets"] = targets(definitions)
    result["mapping_suggestions"] = suggest_mapping(inspection.get("header_cells", []), definitions)
    result["completed_import_notice"] = await completed_notice(db, cid, row)
    result["preview"] = {"revision": row["revision"], "plan_fingerprint": row["planfingerprint"],
                         "interpretation": inspection.get("mapped_date_interpretation"),
                         "row_page_default": ROW_PAGE_DEFAULT, "row_page_max": ROW_PAGE_MAX,
                         "issue_page_default": ISSUE_PAGE_DEFAULT, "issue_page_max": ISSUE_PAGE_MAX,
                         "max_response_bytes": MAX_PREVIEW_RESPONSE_BYTES}
    return result


async def map_or_refresh(request, engine, cid, uid, iid, body, *, refresh=False):
    async with admission(request):
        timings = {}
        started = perf_counter()
        async with engine.begin() as db:
            row = await load(db, cid, iid)
            period = await authorize(db, cid, uid, row["payrollperiodid"], mutate=True)
            check_versions(row)
            if row["revision"] != body.expected_revision:
                raise failure("IMPORT_REVISION_CONFLICT", "Import changed. Read its current revision and retry.")
            if period.status not in SOURCE_ENTRY_STATUSES or row["status"] not in ("NeedsMapping", "NeedsReview") or row["contentpurgedatutc"]:
                raise failure("IMPORT_NOT_EDITABLE", "This Import can no longer be mapped/refreshed.")
            mapping = body.column_mapping if not refresh else None
            if refresh and row["columnmapping"]:
                contract = row["columnmapping"]
                if contract.get("version") != 1 or not isinstance(contract.get("columns"), list):
                    raise failure("IMPORT_CONTRACT_UNSUPPORTED", "Unsupported accepted mapping; re-upload this staged Import.")
                mapping = {str(item["source_column"]): item["target"] for item in contract["columns"]}
            if not mapping:
                raise failure("IMPORT_MAPPING_REQUIRED", "Accept a column mapping before refreshing.", 422)
            content = bytes((await db.execute(text("""
                SELECT filecontent FROM import.filesources WHERE importid = :iid AND companyid = :cid
            """), {"iid": iid, "cid": cid})).scalar_one())
            settings = {"sheet_name": row["sheetname"], "header_row_number": row["headerrownumber"], "date_order": row["dateorder"]}
            # A detected order belongs to the mapped date column and is
            # inferred afresh. A user's selected order remains authoritative.
            if (row["inspectionmetadata"] or {}).get("date_order_state") == "detected":
                settings["date_order"] = None
            if not refresh:
                settings.update(body.model_dump(exclude_unset=True, exclude={"expected_revision", "column_mapping"}))
            # Reject malformed mapping keys before converting them or opening the file.
            from app.imports.mapping import validate_mapping

            definitions = await list_period_definitions(row["payrollperiodid"], cid, db, active_only=True)
            validate_mapping(mapping, [], definitions)
            labels = [value for d in definitions for value in (d.code, d.name)]
        parse_start = perf_counter()
        materialized = await anyio.to_thread.run_sync(partial(materialize, content, row["originalfilename"],
            mapping, period, labels, settings))
        timings["parsing_mapping_seconds"] = perf_counter() - parse_start
        context_start = perf_counter()
        async with engine.begin() as db:
            await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            period = await authorize(db, cid, uid, row["payrollperiodid"], mutate=True)
            context = await load_context(db, cid, period, dates=materialized["work_dates"], identifiers=materialized["stable_identifiers"])
        timings["current_context_seconds"] = perf_counter() - context_start
        compute_start = perf_counter()
        preview = await anyio.to_thread.run_sync(partial(build_preview, materialized, mapping, context, settings["date_order"]))
        context_hash = await anyio.to_thread.run_sync(partial(context_fingerprint, context))
        timings["validation_comparison_seconds"] = perf_counter() - compute_start
        preparation_start = perf_counter()
        prepared = await anyio.to_thread.run_sync(partial(prepare_preview, preview))
        try:
            context_scope = {"dates": materialized["work_dates"], "identifiers": materialized["stable_identifiers"]}
            inspection = materialized["inspection"]
            preview = prepared.metadata
            # Publication retains bounded streams, not the file/model/JSON copies.
            del materialized, context, content
            timings["serialization_preparation_seconds"] = perf_counter() - preparation_start
            persist_start = perf_counter()
            async with engine.begin() as db:
                locked = await lock_mutable(db, cid, uid, iid, body.expected_revision)
                period = await authorize(db, cid, uid, locked["payrollperiodid"], mutate=True)
                current_context = await load_context(db, cid, period, **context_scope)
                current_hash = await anyio.to_thread.run_sync(partial(context_fingerprint, current_context))
                if current_hash != context_hash:
                    raise failure("IMPORT_PREVIEW_CONTEXT_CHANGED", "Payroll or eligibility changed while previewing. Retry mapping/refresh.")
                await persist_preview(db, cid, row["branchid"], row["payrollperiodid"], iid, prepared)
                inspection["mapped_date_interpretation"] = preview["interpretation"]
                inspection["date_order"] = preview["interpretation"]["date_order"]
                inspection["date_order_state"] = preview["interpretation"]["date_order_state"]
                await db.execute(text("""
                    UPDATE import.filesources SET sheetname = :sheet, headerrownumber = :header, dateorder = :date,
                        encoding = :encoding, delimiter = :delimiter, inspectionmetadata = CAST(:inspection AS jsonb)
                    WHERE importid = :iid AND companyid = :cid
                """), {"iid": iid, "cid": cid, "sheet": inspection["selected_sheet"], "header": inspection["header_row_number"],
                       "date": preview["interpretation"]["date_order"], "encoding": inspection["encoding"], "delimiter": inspection["delimiter"],
                       "inspection": encoded(inspection)})
                await db.execute(text("""
                    UPDATE import.imports SET revision = revision + 1, status = 'NeedsReview',
                        updatedbyuserid = :uid, updatedatutc = NOW() WHERE importid = :iid AND companyid = :cid
                """), {"iid": iid, "cid": cid, "uid": uid})
                result = await enrich_metadata(db, cid, await load(db, cid, iid))
        finally:
            prepared.close()
        timings["staging_persistence_seconds"] = perf_counter() - persist_start
        timings["total_seconds"] = perf_counter() - started
        # Invocation-only benchmark hook. No job state or client timings contract.
        observer = getattr(request.app.state, "import_preview_observer", None)
        if observer:
            observer(timings)
        return result


def bounded_page(base, items, offset, limit, total):
    result = {**base, "offset": offset, "limit": limit, "filtered_count": total,
              "items": [], "next_offset": None}
    size = len(encoded(result).encode("utf-8")) + 128
    for item in items:
        item_size = len(encoded(item).encode("utf-8")) + 1
        if size + item_size > MAX_PREVIEW_RESPONSE_BYTES:
            if not result["items"]:
                raise failure("IMPORT_PREVIEW_ROW_TOO_LARGE", "A preview item exceeds the response bound. Read the issues page and correct/re-upload the source row.", 422)
            break
        result["items"].append(item)
        size += item_size
    consumed = len(result["items"])
    if offset + consumed < total:
        result["next_offset"] = offset + consumed
    return canonical(result)


async def read_page(engine, cid, uid, iid, expected_revision, plan_fingerprint, offset, limit, severity, *, issues=False):
    async with engine.begin() as db:
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        row = await load(db, cid, iid)
        await authorize(db, cid, uid, row["payrollperiodid"], mutate=False)
        check_versions(row)
        if row["revision"] != expected_revision or row["planfingerprint"] != plan_fingerprint:
            raise failure("IMPORT_PREVIEW_STALE", "Preview changed. Read current metadata before requesting another page.")
        if not row["planfingerprint"]:
            raise failure("IMPORT_PREVIEW_UNAVAILABLE", "Map the file before reading preview pages.")
        params = {"cid": cid, "iid": iid, "offset": offset, "limit": limit, "severity": severity}
        predicate = "AND severity = :severity" if severity else ""
        base = {"revision": row["revision"], "plan_fingerprint": row["planfingerprint"],
                "summary": row["previewcounts"], "filter": severity,
                "interpretation": (row["inspectionmetadata"] or {}).get("mapped_date_interpretation")}
        if issues:
            total = (await db.execute(text(f"""SELECT count(*) FROM import.importissues
                WHERE companyid = :cid AND importid = :iid {predicate}"""), params)).scalar_one()
            items = [dict(item) for item in (await db.execute(text(f"""
                SELECT f.sheetname AS sheet_name, f.sourcerownumber AS source_row, i.locator, i.severity, i.code, i.message
                FROM import.importissues i LEFT JOIN import.filesourcerecords f
                  ON f.sourcerecordid = i.sourcerecordid AND f.companyid = i.companyid AND f.importid = i.importid
                WHERE i.companyid = :cid AND i.importid = :iid {predicate}
                ORDER BY f.sourcerownumber, (i.locator->>'column')::integer NULLS FIRST, i.code, i.importissueid
                OFFSET :offset LIMIT :limit
            """), params)).mappings()]
        else:
            record_filter = """AND EXISTS (SELECT 1 FROM import.importissues i WHERE i.companyid = r.companyid
                AND i.importid = r.importid AND i.sourcerecordid = r.sourcerecordid AND i.severity = :severity)""" if severity else ""
            total = (await db.execute(text(f"""SELECT count(*) FROM import.payrollworkrecords r
                WHERE r.companyid = :cid AND r.importid = :iid {record_filter}"""), params)).scalar_one()
            selected = (await db.execute(text(f"""
                SELECT r.sourcerecordid, r.rowfingerprint, r.isskipped, f.sheetname, f.sourcerownumber
                FROM import.payrollworkrecords r JOIN import.filesourcerecords f
                  ON f.sourcerecordid = r.sourcerecordid AND f.companyid = r.companyid AND f.importid = r.importid
                WHERE r.companyid = :cid AND r.importid = :iid {record_filter}
                ORDER BY f.sourcerownumber OFFSET :offset LIMIT :limit
            """), params)).mappings().all()
            ids = [item["sourcerecordid"] for item in selected]
            details = {sid: {"cells": [], "identifiers": []} for sid in ids}
            if ids:
                params["ids"] = ids
                for cell in (await db.execute(text("""
                    SELECT sourcerecordid, sourcecolumnindex AS source_column, workdate AS work_date,
                        driverassignmentid AS driver_assignment_id,
                        payrollperioddefinitionid AS payroll_period_definition_id,
                        rawvalue AS raw_value, quantity, isblank AS is_blank, outcome, currentvalue AS current_value,
                        hasstatus AS has_status, heldstatuskeyid AS held_status_key_id, isdayoff AS is_day_off
                    FROM import.payrollworkcells
                    WHERE companyid = :cid AND importid = :iid AND sourcerecordid = ANY(CAST(:ids AS bigint[]))
                    ORDER BY sourcerecordid, sourcecolumnindex
                """), params)).mappings():
                    item = dict(cell)
                    details[item.pop("sourcerecordid")]["cells"].append(item)
                for match in (await db.execute(text("""
                    SELECT a.sourcerecordid, a.sourcecolumnindex AS source_column,
                        m.identifierkind AS identifier_kind, m.normalizedvalue AS normalized_value,
                        m.driverid AS driver_id, m.resolution
                    FROM import.payrollworkrecordidentifiers a JOIN import.payrollworkdrivermatches m
                      ON m.drivermatchid = a.drivermatchid AND m.companyid = a.companyid AND m.importid = a.importid
                    WHERE a.companyid = :cid AND a.importid = :iid AND a.sourcerecordid = ANY(CAST(:ids AS bigint[]))
                    ORDER BY a.sourcerecordid, a.sourcecolumnindex
                """), params)).mappings():
                    item = dict(match)
                    details[item.pop("sourcerecordid")]["identifiers"].append(item)
            items = [{"source_row": item["sourcerownumber"], "sheet_name": item["sheetname"],
                      "row_fingerprint": item["rowfingerprint"], "skipped": item["isskipped"],
                      **details[item["sourcerecordid"]]} for item in selected]
        return await anyio.to_thread.run_sync(partial(bounded_page, base, items, offset, limit, total))
