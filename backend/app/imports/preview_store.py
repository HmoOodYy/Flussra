"""Bulk persistence of transient Import preview detail; no canonical payroll writes."""
from functools import partial
from itertools import islice

import anyio
from sqlalchemy import text

from app.imports.mapping import encoded
from app.imports.staging import clear_derived_state


async def insert_json(db, sql, rows, *, batch=500):
    result = []
    iterator = iter(rows)
    while chunk := list(islice(iterator, batch)):
        data = await anyio.to_thread.run_sync(partial(encoded, chunk))
        query = await db.execute(text(sql), {"data": data})
        if query.returns_rows:
            result.extend(query.mappings().all())
    return result


async def persist_preview(db, cid, bid, pid, iid, preview):
    await clear_derived_state(db, cid, iid)
    source = await insert_json(db, """
        INSERT INTO import.importsourcerecords(importid, companyid, locator, payload)
        SELECT x.iid, x.cid, x.locator, x.payload FROM jsonb_to_recordset(CAST(:data AS jsonb))
        AS x(iid bigint, cid integer, locator jsonb, payload jsonb)
        RETURNING sourcerecordid, locator
    """, ({"iid": iid, "cid": cid, "locator": {"record": row["source_row"]}, "payload": {"cells": row["cells"]}}
          for row in preview["rows"]), batch=100)
    records = {row["locator"]["record"]: row["sourcerecordid"] for row in source}
    scope = {"iid": iid, "cid": cid, "bid": bid, "pid": pid}
    await insert_json(db, """
        INSERT INTO import.filesourcerecords(sourcerecordid, importid, companyid, sheetname, sourcerownumber)
        SELECT x.sid, x.iid, x.cid, x.sheet, x.row FROM jsonb_to_recordset(CAST(:data AS jsonb))
        AS x(sid bigint, iid bigint, cid integer, sheet text, row integer)
    """, ({**scope, "sid": records[row["source_row"]], "sheet": preview["interpretation"]["sheet_name"],
           "row": row["source_row"]} for row in preview["rows"]))
    await insert_json(db, """
        INSERT INTO import.payrollworkrecords(sourcerecordid, importid, companyid, rowfingerprint, isskipped)
        SELECT x.sid, x.iid, x.cid, x.fingerprint, x.skipped FROM jsonb_to_recordset(CAST(:data AS jsonb))
        AS x(sid bigint, iid bigint, cid integer, fingerprint text, skipped boolean)
    """, ({**scope, "sid": records[row["source_row"]], "fingerprint": row["fingerprint"], "skipped": row["skipped"]}
          for row in preview["rows"]))
    matches = await insert_json(db, """
        INSERT INTO import.payrollworkdrivermatches(importid, companyid, identifierkind, normalizedvalue, driverid, resolution)
        SELECT x.iid, x.cid, x.kind, x.value, x.driver_id, x.resolution FROM jsonb_to_recordset(CAST(:data AS jsonb))
        AS x(iid bigint, cid integer, kind text, value text, driver_id integer, resolution text)
        RETURNING drivermatchid, identifierkind, normalizedvalue
    """, ({**scope, **match} for match in preview["matches"]))
    match_ids = {(row["identifierkind"], row["normalizedvalue"]): row["drivermatchid"] for row in matches}
    await insert_json(db, """
        INSERT INTO import.payrollworkrecordidentifiers(sourcerecordid, importid, companyid, sourcecolumnindex, drivermatchid)
        SELECT x.sid, x.iid, x.cid, x.column, x.mid FROM jsonb_to_recordset(CAST(:data AS jsonb))
        AS x(sid bigint, iid bigint, cid integer, "column" integer, mid bigint)
    """, ({**scope, "sid": records[row["source_row"]], "column": identifier["column"],
           "mid": match_ids[(identifier["kind"], identifier["value"])]}
          for row in preview["rows"] for identifier in row["identifiers"]))
    await insert_json(db, """
        INSERT INTO import.payrollworkcells(sourcerecordid, importid, companyid, branchid, payrollperiodid,
            sourcecolumnindex, workdate, driverassignmentid, payrollperioddefinitionid, paydefinitionid, ratedefinitionid,
            rawvalue, quantity, isblank, outcome, currentvalue, hasstatus, heldstatuskeyid, isdayoff)
        SELECT x.sid, x.iid, x.cid, x.bid, x.pid, x.column, x.work_date, x.assignment_id, x.definition_id,
            x.pay_definition_id, x.rate_definition_id, x.raw_value, x.quantity, x.blank, x.outcome,
            x.current, x.held_status_key_id IS NOT NULL, x.held_status_key_id, x.day_off
        FROM jsonb_to_recordset(CAST(:data AS jsonb)) AS x(sid bigint, iid bigint, cid integer, bid integer,
            pid integer, "column" integer, work_date date, assignment_id integer, definition_id integer,
            pay_definition_id integer, rate_definition_id integer, raw_value text, quantity numeric, blank boolean,
            outcome text, current jsonb, held_status_key_id integer, day_off boolean)
    """, ({**scope, "sid": records[row["source_row"]], **cell} for row in preview["rows"] for cell in row["work_cells"]))
    await insert_json(db, """
        INSERT INTO import.importissues(importid, companyid, sourcerecordid, locator, severity, code, message)
        SELECT x.iid, x.cid, x.sid, x.locator, x.severity, x.code, x.message
        FROM jsonb_to_recordset(CAST(:data AS jsonb)) AS x(iid bigint, cid integer, sid bigint,
            locator jsonb, severity text, code text, message text)
    """, ({**scope, **issue, "sid": records[issue["source_row"]],
           "locator": {"column": issue["column"], "value": issue["value"]}}
          for issue in preview["issues"]))
    await db.execute(text("""
        UPDATE import.payrollworkimports SET columnmapping = CAST(:mapping AS jsonb),
            mappingfingerprint = :mf, contentfingerprint = :cf, planfingerprint = :pf,
            previewcounts = CAST(:counts AS jsonb)
        WHERE importid = :iid AND companyid = :cid
    """), {**scope, "mapping": encoded(preview["mapping_contract"]), "mf": preview["mapping_fingerprint"],
           "cf": preview["content_fingerprint"], "pf": preview["plan_fingerprint"], "counts": encoded(preview["counts"])})
