"""Measure real canonical quantity batches with all guards, audit and evidence active.

Run from backend: python -m scripts.benchmark_quantity_batch --drivers 150 --definitions 20 --days 31
Never connects to an application database; setup is excluded from mutation timing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import platform
import time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from scripts.import_benchmark_support import (
    application,
    memory_snapshot,
    private_database,
    seed_period,
)


async def measure(dsn, args):
    from sqlalchemy import event

    from app.payroll.quantity_batch import (
        ReviewedQuantityChange,
        apply_reviewed_quantities,
        prepare_quantity_mutation_scope,
    )

    async with application(dsn) as (client, engine, _app):
        fixture = await seed_period(client, engine, dsn, drivers=args.drivers, definitions=args.definitions, days=args.days)
        tenant, period = fixture["tenant"], fixture["period"]
        keys = [(driver["driverassignmentid"], day, definition.payroll_period_definition_id)
                for driver in fixture["drivers"] for day in fixture["days"] for definition in fixture["definitions"]]
        statements = []
        def statement(_conn, _cursor, sql, _parameters, context, executemany):
            normalized = sql.lower()
            phase = ("current_source_read" if "select x.ordinal, d.draftlineid" in normalized else
                     "source_insert" if "insert into payroll.payrolldraftlines" in normalized else
                     "source_update" if "update payroll.payrolldraftlines" in normalized else
                     "source_evidence" if "insert into payroll.payrollperiodauditevidenceevents" in normalized else
                     "ordinary_audit" if "insert into audit.auditlog" in normalized else "other")
            context.quantity_measurement = {"verb": sql.strip().split()[0], "phase": phase,
                "executemany": executemany, "started": time.perf_counter()}
        def finished_statement(_conn, _cursor, _sql, _parameters, context, _executemany):
            item = context.quantity_measurement
            item["seconds"] = time.perf_counter() - item.pop("started")
            statements.append(item)
        event.listen(engine.sync_engine, "before_cursor_execute", statement)
        event.listen(engine.sync_engine, "after_cursor_execute", finished_statement)
        results = []
        try:
            for operation, value, expected in [("create", "1", None), ("update", "2", "1"),
                                               ("unchanged", "2", "2"), ("void", "0", "2")]:
                changes = [ReviewedQuantityChange(aid, day, definition, Decimal(value), expected is not None,
                            Decimal(expected) if expected is not None else None) for aid, day, definition in keys]
                statements.clear()
                print(f"Measuring {operation}: {len(keys)} reviewed targets", flush=True)
                started = time.perf_counter()
                async with engine.begin() as db:
                    scope = await prepare_quantity_mutation_scope(db, company_id=tenant.company_id,
                        period_id=period["payroll_period_id"], user_id=tenant.owner)
                    locked = time.perf_counter()
                    result = await apply_reviewed_quantities(scope, changes, correlation_id=str(uuid4()))
                finished = time.perf_counter()
                elapsed = finished - started
                statement_count = len(statements)
                phase_seconds = {}
                for item in statements:
                    phase_seconds[item["phase"]] = phase_seconds.get(item["phase"], 0) + item["seconds"]
                evidence = await _counts(engine, tenant.company_id, period["payroll_period_id"])
                results.append({"operation": operation, "targets": len(keys), "seconds": elapsed,
                    "locked_seconds": finished - locked,
                    "sqlalchemy_statements": statement_count, "copy_batches": (len(keys) + 999) // 1000,
                    "database_phase_seconds": phase_seconds,
                    "created": result.created, "updated": result.updated, "voided": result.voided,
                    "unchanged": result.unchanged, "persisted": evidence, "memory": memory_snapshot()})
                print(json.dumps(results[-1]), flush=True)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", statement)
            event.remove(engine.sync_engine, "after_cursor_execute", finished_statement)
        return {"python": platform.python_version(), "platform": platform.platform(),
                "drivers": args.drivers, "definitions": args.definitions, "days": args.days,
                "guards": "production constraints and triggers active; no suspension", "results": results}


async def _counts(engine, cid, pid):
    from sqlalchemy import text
    async with engine.connect() as db:
        return dict((await db.execute(text("""SELECT
            (SELECT count(*) FROM payroll.payrolldraftlines WHERE companyid=:cid AND payrollperiodid=:pid) AS lines,
            (SELECT count(*) FROM payroll.payrollperiodauditevidenceevents WHERE companyid=:cid AND payrollperiodid=:pid
                AND evidencedomain='SOURCE') AS source_events,
            (SELECT count(*) FROM audit.auditlog WHERE companyid=:cid AND entityname='PayrollDraftLines') AS audit_rows"""),
            {"cid": cid, "pid": pid})).mappings().one())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drivers", type=int, default=150)
    parser.add_argument("--definitions", type=int, default=20)
    parser.add_argument("--days", type=int, default=31)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[2] / ".artifacts/quantity-benchmark.json")
    args = parser.parse_args()
    with private_database() as dsn:
        evidence = asyncio.run(measure(dsn, args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
