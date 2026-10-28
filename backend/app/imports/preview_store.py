"""Compact transactional publication of normalized source rows and preview groups."""
from __future__ import annotations

import json
from itertools import islice

from sqlalchemy import text

from app.imports.mapping import canonical


async def _sequence_ids(db, table: str, column: str, count: int) -> list[int]:
    if count <= 0:
        return []
    result = await db.execute(text("""
        SELECT nextval(pg_get_serial_sequence(:table_name, :column_name))
        FROM generate_series(1, :count)
    """), {"table_name": table, "column_name": column, "count": count})
    return list(result.scalars())


def _json(value) -> str:
    return json.dumps(canonical(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def prepare_preview(result: dict) -> dict:
    """Serialize durable row payloads outside the guarded publication transaction."""
    rows = result["rows"]
    matches = {}
    for row in rows:
        locator = {"file_ordinal": row["file_ordinal"], "unit_name": row["unit_name"],
                   "source_row": row["source_row"]}
        row["_prepared_json"] = {
            "locator": _json(locator),
            "payload": _json(row.get("raw_payload", {})),
            "values": _json(row["values"]),
            "issues": _json([{key: item[key] for key in ("column", "severity", "code", "message", "value")}
                             for item in row["issues"]]),
            "context": _json(row["context"]),
        }
        for identifier in row["identifiers"]:
            key = (identifier["kind"], identifier["value"])
            matches.setdefault(key, identifier)
        # These structures have already contributed to semantic hashes. Keeping
        # them alongside their serialized JSON copies is a major source of peak RSS.
        row.pop("raw_payload", None)
        row.pop("values", None)
        row.pop("issues", None)
        row.pop("semantic", None)
    result["_prepared_matches"] = matches
    result["_prepared_import_issues"] = [
        {"coordinate": (item["file_ordinal"], item["unit_name"], item["source_row"]),
         "locator": _json({"file_ordinal": item["file_ordinal"], "unit_name": item["unit_name"],
                            "source_row": item["source_row"], "column": item["column"]}),
         "severity": item["severity"], "code": item["code"], "message": item["message"]}
        for item in result.get("import_issues", [])
    ]
    result.pop("import_issues", None)
    result.pop("issues", None)
    result["_prepared"] = True
    return result


async def _copy_records(db, table: str, columns: tuple[str, ...], records) -> None:
    """COPY bounded chunks into guarded final tables on the current transaction."""
    raw = await db.get_raw_connection()
    connection = raw.driver_connection
    iterator = iter(records)
    while batch := list(islice(iterator, 1000)):
        await connection.copy_records_to_table(
            table, schema_name="import", columns=columns, records=batch,
        )


async def persist_preview(db, *, company_id: int, import_id: int, branch_id: int,
                          period_id: int, result: dict, rule_context_fingerprint: str) -> None:
    """Publish one fully prepared preview inside the caller's final transaction."""
    if not result.get("_prepared"):
        raise ValueError("Preview persistence requires preparation outside the publication transaction.")
    rows = result["rows"]
    groups = result["groups"]
    record_ids = await _sequence_ids(db, "import.importsourcerecords", "sourcerecordid", len(rows))
    group_ids = await _sequence_ids(db, "import.payrollworkpreviewgroups", "previewgroupid", len(groups))
    issue_coordinates = {item["coordinate"] for item in result["_prepared_import_issues"]}
    source_ids = {}
    group_id_by_fingerprint = {}
    for row, source_record_id in zip(rows, record_ids, strict=True):
        row["source_record_id"] = source_record_id
        coordinate = (row["file_ordinal"], row["unit_name"], row["source_row"])
        if coordinate in issue_coordinates:
            source_ids[coordinate] = source_record_id
    for group, group_id in zip(groups, group_ids, strict=True):
        group["preview_group_id"] = group_id
        group_id_by_fingerprint[group["group_fingerprint"]] = group_id
    if rows:
        await _copy_records(db, "importsourcerecords",
            ("sourcerecordid", "importid", "companyid", "locator", "payload"),
            ((row["source_record_id"], import_id, company_id, row["_prepared_json"]["locator"],
              row["_prepared_json"]["payload"]) for row in rows))
        await _copy_records(db, "filesourcerecords",
            ("sourcerecordid", "importid", "companyid", "sourceunitid", "sourcerownumber"),
            ((row["source_record_id"], import_id, company_id, row["source_unit_id"], row["source_row"])
             for row in rows))
    if groups:
        await _copy_records(db, "payrollworkpreviewgroups",
            ("previewgroupid", "importid", "companyid", "branchid", "payrollperiodid",
             "groupfingerprint", "driverid", "workdate", "driverdisplay", "errorcount", "warningcount", "conflictcount"),
            ((group["preview_group_id"], import_id, company_id, branch_id, period_id,
              group["group_fingerprint"], group["driver_id"], group["work_date"], _json(group["driver_display"]),
              group["error_count"], group["warning_count"], group["conflict_count"]) for group in groups))
    if rows:
        await _copy_records(db, "payrollworkrecords",
            ("sourcerecordid", "importid", "companyid", "branchid", "payrollperiodid", "previewgroupid",
             "driverid", "driverassignmentid", "workdate", "rowfingerprint", "isskipped", "valuesjson",
             "issuesjson", "contextjson"),
            ((row["source_record_id"], import_id, company_id, branch_id, period_id,
              group_id_by_fingerprint[row["group_fingerprint"]], row["driver_id"], row["driver_assignment_id"],
              row["work_date"], row["row_fingerprint"], row["skipped"], row["_prepared_json"]["values"],
              row["_prepared_json"]["issues"], row["_prepared_json"]["context"]) for row in rows))

    issue_rows = result["_prepared_import_issues"]
    if issue_rows:
        await _copy_records(db, "importissues",
            ("importid", "companyid", "sourcerecordid", "locator", "severity", "code", "message"),
            ((import_id, company_id, source_ids.get(item["coordinate"]), item["locator"],
              item["severity"], item["code"], item["message"]) for item in issue_rows))

    matches = result["_prepared_matches"]
    match_ids = await _sequence_ids(db, "import.payrollworkdrivermatches", "drivermatchid", len(matches))
    match_id_by_key = {}
    if matches:
        sorted_matches = sorted(matches.items())
        for ((kind, value), identifier), match_id in zip(sorted_matches, match_ids, strict=True):
            match_id_by_key[(kind, value)] = match_id
        await _copy_records(db, "payrollworkdrivermatches",
            ("drivermatchid", "importid", "companyid", "identifierkind", "normalizedvalue", "driverid", "resolution"),
            ((match_id, import_id, company_id, kind, value, identifier["driver_id"], identifier["resolution"])
             for ((kind, value), identifier), match_id in zip(sorted_matches, match_ids, strict=True)))
        await _copy_records(db, "payrollworkrecordidentifiers",
            ("sourcerecordid", "importid", "companyid", "sourcecolumnindex", "drivermatchid"),
            ((row["source_record_id"], import_id, company_id, identifier["column"],
              match_id_by_key[(identifier["kind"], identifier["value"])])
             for row in rows for identifier in row["identifiers"]))
    review_summary = {**result["counts"]}
    if result.get("date_review") is not None:
        review_summary["date_review"] = result["date_review"]
    await db.execute(text("""
        UPDATE import.payrollworkimports SET mappingfingerprint = :mapping,
            contentfingerprint = :content, planfingerprint = :plan,
            previewcounts = CAST(:counts AS jsonb), reviewsummary = CAST(:summary AS jsonb)
        WHERE importid = :iid AND companyid = :cid
    """), {"iid": import_id, "cid": company_id, "mapping": result["mapping_fingerprint"],
           "content": result["content_fingerprint"], "plan": result["plan_fingerprint"],
           "counts": _json(result["counts"]), "summary": _json(review_summary)})
    await db.execute(text("""
        INSERT INTO import.payrollworkpreviewrulecontexts
            (importid, companyid, branchid, payrollperiodid, fingerprint)
        VALUES (:iid, :cid, :bid, :pid, :fingerprint)
    """), {"iid": import_id, "cid": company_id, "bid": branch_id,
           "pid": period_id, "fingerprint": rule_context_fingerprint})
