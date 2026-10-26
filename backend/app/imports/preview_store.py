"""Bounded preparation and guarded binary publication of transient preview detail."""
import pickle
from contextlib import ExitStack
from dataclasses import dataclass
from functools import partial
from tempfile import SpooledTemporaryFile

import anyio
from sqlalchemy import text

from app.imports.mapping import encoded
from app.imports.staging import clear_derived_state

COPY_BATCH = 2048
PREPARED_BATCH_BYTES = 1_048_576
SPOOL_MEMORY_BYTES = 1_048_576


@dataclass
class PreparedPreview:
    """Invocation-owned, automatically removed streams; never uploaded pickle."""

    resources: ExitStack
    streams: dict
    metadata: dict
    source_rows: list
    match_keys: list

    def close(self):
        self.resources.close()


def prepare_preview(preview):
    """Serialize outside the period lock, retaining at most one bounded batch."""
    resources = ExitStack()
    streams = {}

    def spool(table, columns, rows):
        stream = resources.enter_context(SpooledTemporaryFile(max_size=SPOOL_MEMORY_BYTES, mode="w+b"))
        batch, size = [], 0
        for row in rows:
            batch.append(row)
            size += sum(len(value.encode("utf-8")) if isinstance(value, str) else 32 for value in row)
            if len(batch) < COPY_BATCH and size < PREPARED_BATCH_BYTES:
                continue
            # Only our own typed tuples enter this private, invocation-local file.
            pickle.dump(batch, stream, protocol=pickle.HIGHEST_PROTOCOL)
            batch, size = [], 0
        if batch:
            pickle.dump(batch, stream, protocol=pickle.HIGHEST_PROTOCOL)
        stream.seek(0)
        streams[table] = (columns.split(), stream)

    try:
        rows = preview["rows"]
        spool("importsourcerecords", "sourcerecordid importid companyid locator payload",
              ((row["source_row"], encoded({"record": row["source_row"]}), encoded({"cells": row["cells"]})) for row in rows))
        spool("filesourcerecords", "sourcerecordid importid companyid sheetname sourcerownumber",
              ((row["source_row"], preview["interpretation"]["sheet_name"], row["source_row"]) for row in rows))
        spool("payrollworkrecords", "sourcerecordid importid companyid rowfingerprint isskipped",
              ((row["source_row"], row["fingerprint"], row["skipped"]) for row in rows))
        match_keys = [(item["kind"], item["value"]) for item in preview["matches"]]
        match_ordinals = {key: index for index, key in enumerate(match_keys)}
        spool("payrollworkdrivermatches", "drivermatchid importid companyid identifierkind normalizedvalue driverid resolution",
              ((index, item["kind"], item["value"], item["driver_id"], item["resolution"])
               for index, item in enumerate(preview["matches"])))
        spool("payrollworkrecordidentifiers", "sourcerecordid importid companyid sourcecolumnindex drivermatchid",
              ((row["source_row"], item["column"], match_ordinals[(item["kind"], item["value"])])
               for row in rows for item in row["identifiers"]))

        def cells():
            for row in rows:
                for cell in row["work_cells"]:
                    target, definition = cell.target, cell.definition
                    yield (row["source_row"], cell.column, target.work_date, target.assignment_id,
                           definition.payroll_period_definition_id, definition.pay_definition_id, definition.rate_definition_id,
                           None if cell.source["value"] is None else str(cell.source["value"]), cell.quantity, cell["blank"],
                           cell.outcome, encoded({"quantity": cell.current_quantity}), target.held_status_key_id is not None,
                           target.held_status_key_id, target.day_off)

        spool("payrollworkcells", "sourcerecordid importid companyid branchid payrollperiodid sourcecolumnindex workdate "
              "driverassignmentid payrollperioddefinitionid paydefinitionid ratedefinitionid rawvalue quantity isblank outcome "
              "currentvalue hasstatus heldstatuskeyid isdayoff", cells())
        spool("importissues", "sourcerecordid importid companyid locator severity code message",
              ((item["source_row"], encoded({"column": item["column"], "value": item["value"]}),
                item["severity"], item["code"], item["message"]) for item in preview["issues"]))
        metadata = {key: value for key, value in preview.items() if key not in ("rows", "issues", "matches")}
        return PreparedPreview(resources, streams, metadata, [row["source_row"] for row in rows], match_keys)
    except BaseException:
        resources.close()
        raise


def read_batch(stream):
    try:
        return pickle.load(stream)
    except EOFError:
        return None


async def allocate_ids(db, table, column, count):
    # Sequence values are reserved by PostgreSQL, including concurrent requests.
    # Gaps after rollback are normal; no staged identity is a semantic identity.
    return (await db.execute(text("""SELECT nextval(pg_get_serial_sequence(:table, :column))
        FROM generate_series(1, :count)"""), {"table": f"import.{table}", "column": column, "count": count})).scalars().all()


async def copy_prepared(driver, table, columns, stream, records, matches, scope):
    while batch := await anyio.to_thread.run_sync(partial(read_batch, stream)):
        if table == "payrollworkdrivermatches":
            values = ((matches[item[0]], scope[0], scope[1], *item[1:]) for item in batch)
        elif table == "payrollworkrecordidentifiers":
            values = ((records[item[0]], scope[0], scope[1], item[1], matches[item[2]]) for item in batch)
        elif table == "payrollworkcells":
            values = ((records[item[0]], *scope, *item[1:]) for item in batch)
        else:
            values = ((records[item[0]], scope[0], scope[1], *item[1:]) for item in batch)
        # Same transaction/connection as the authorized locks. COPY executes all
        # ordinary row triggers, constraints and FKs on the real target tables.
        await driver.copy_records_to_table(table, schema_name="import", columns=columns, records=values)


async def persist_preview(db, cid, bid, pid, iid, prepared):
    await clear_derived_state(db, cid, iid)
    records = dict(zip(prepared.source_rows, await allocate_ids(db, "importsourcerecords", "sourcerecordid", len(prepared.source_rows)), strict=True))
    matches = await allocate_ids(db, "payrollworkdrivermatches", "drivermatchid", len(prepared.match_keys))
    raw = await db.get_raw_connection()
    for table, (columns, stream) in prepared.streams.items():
        await copy_prepared(raw.driver_connection, table, columns, stream, records, matches, (iid, cid, bid, pid))
    preview = prepared.metadata
    await db.execute(text("""
        UPDATE import.payrollworkimports SET columnmapping = CAST(:mapping AS jsonb),
            mappingfingerprint = :mf, contentfingerprint = :cf, planfingerprint = :pf,
            previewcounts = CAST(:counts AS jsonb)
        WHERE importid = :iid AND companyid = :cid
    """), {"cid": cid, "iid": iid, "mapping": encoded(preview["mapping_contract"]), "mf": preview["mapping_fingerprint"],
           "cf": preview["content_fingerprint"], "pf": preview["plan_fingerprint"], "counts": encoded(preview["counts"])})
