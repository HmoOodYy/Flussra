"""Canonical ordinary quantity batches; no Import decisions or public apply route.

The caller owns one transaction. Prepare the currency/period scope before any
caller-specific Import lock; apply reads and writes on that same locked scope.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.permissions import check_permission
from app.company_currency import lock_and_get_company_currency_for_monetary_write
from app.payroll.draft_line_mutation import _require_usable_definition, _require_valid_quantity
from app.payroll.eligibility import (
    _is_snapshot_row_eligible_for_workdate,
    lifecycle_eligible_assignment_days,
    require_period_eligibility_snapshot,
)
from app.payroll.line_audit import write_quantity_audit_batch
from app.payroll.mutation_lock import _lock_period_for_mutation
from app.payroll.period_definitions import list_period_definitions
from app.payroll.period_read import get_period_by_id
from app.payroll.source_evidence import capture_source_evidence_batch


@dataclass(frozen=True, slots=True)
class ReviewedQuantityChange:
    driver_assignment_id: int
    work_date: date
    payroll_period_definition_id: int
    quantity: Decimal
    expected_present: bool
    expected_quantity: Decimal | None

    @property
    def key(self):
        return self.driver_assignment_id, self.work_date, self.payroll_period_definition_id


@dataclass(frozen=True)
class QuantityMutationScope:
    db: AsyncConnection
    transaction: object
    company_id: int
    branch_id: int
    period_id: int
    user_id: int
    period: object


@dataclass
class QuantityMutationResult:
    created: int
    updated: int
    voided: int
    unchanged: int
    # Business key -> actual canonical line and immutable SOURCE evidence IDs.
    changed: dict[tuple, tuple[int, int]]


def _reject(code, message, status=422):
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


async def prepare_quantity_mutation_scope(
    db: AsyncConnection, *, company_id: int, period_id: int, user_id: int,
) -> QuantityMutationScope:
    if not db.in_transaction():
        raise RuntimeError("Quantity mutation requires the caller's transaction.")
    period = await get_period_by_id(company_id, user_id, period_id, db)
    await check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)
    await lock_and_get_company_currency_for_monetary_write(company_id, db, required=False)
    await _lock_period_for_mutation(period_id, company_id, db)
    return QuantityMutationScope(db, db.get_transaction(), company_id, period.branch_id,
                                 period_id, user_id, period)


def _validate_input(changes):
    keys = set()
    for change in changes:
        if change.key in keys:
            _reject("QUANTITY_BATCH_DUPLICATE", "A reviewed target occurs more than once.")
        keys.add(change.key)
        if (not isinstance(change.work_date, date) or isinstance(change.work_date, datetime)
                or not isinstance(change.expected_present, bool)):
            _reject("QUANTITY_BATCH_INVALID", "Invalid reviewed date or expected presence.")
        for value in (change.quantity, change.expected_quantity):
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite()
                    or value < 0 or value > Decimal("99999999999999.9999")
                    or value.normalize().as_tuple().exponent < -4):
                _reject("QUANTITY_BATCH_PRECISION", "Quantities must be exact nonnegative NUMERIC(18,4) values.")
        if change.quantity is None or change.expected_present != (change.expected_quantity is not None):
            _reject("QUANTITY_BATCH_INVALID", "Blank is not a quantity mutation; expected source presence must be explicit.")


async def apply_reviewed_quantities(
    scope: QuantityMutationScope, changes: list[ReviewedQuantityChange], *,
    correlation_id: str, source_revision: int | None = None, batch_size: int = 1000,
) -> QuantityMutationResult:
    """Apply supplied reviewed quantities set-wise, retaining constraints/audit.

    A positive changed value is Import source; explicit zero voids nonzero
    existing source. An unchanged effective zero never fabricates a mutation.
    Status, note, eligibility and money authorities are not rewritten here.
    """
    db = scope.db
    if not db.in_transaction() or db.get_transaction() is not scope.transaction:
        raise RuntimeError("Quantity mutation scope expired or belongs to another transaction.")
    if batch_size < 1:
        raise ValueError("Batch size must be positive.")
    _validate_input(changes)
    if not changes:
        return QuantityMutationResult(0, 0, 0, 0, {})
    cid, bid, pid = scope.company_id, scope.branch_id, scope.period_id
    params = {"cid": cid, "bid": bid, "pid": pid, "uid": scope.user_id}
    definitions = {d.payroll_period_definition_id: d for d in await list_period_definitions(pid, cid, db)}
    assignment_ids = sorted({c.driver_assignment_id for c in changes})
    owned = set((await db.execute(text("""
        SELECT driverassignmentid FROM core.driverassignments
        WHERE companyid = :cid AND branchid = :bid AND driverassignmentid = ANY(CAST(:ids AS integer[]))
    """), {**params, "ids": assignment_ids})).scalars())
    if owned != set(assignment_ids):
        _reject("QUANTITY_BATCH_SCOPE", "A reviewed assignment is not owned by this Company and Branch.")
    for change in changes:
        definition = definitions.get(change.payroll_period_definition_id)
        if definition is None:
            _reject("QUANTITY_BATCH_DEFINITION", "A reviewed definition is outside this frozen period.")
        if change.quantity > 0:
            _require_usable_definition(definition)
            _require_valid_quantity(definition, change.quantity)

    table = "quantity_changes_" + uuid4().hex
    await db.execute(text(f"""CREATE TEMP TABLE {table} (
        ordinal integer PRIMARY KEY, assignment_id integer NOT NULL, work_date date NOT NULL,
        definition_id integer NOT NULL, quantity numeric NOT NULL,
        expected_present boolean NOT NULL, expected_quantity numeric,
        UNIQUE(assignment_id, work_date, definition_id)
    ) ON COMMIT DROP"""))
    raw = await db.get_raw_connection()
    for offset in range(0, len(changes), batch_size):
        batch = changes[offset:offset + batch_size]
        await raw.driver_connection.copy_records_to_table(table, records=(
            (offset + index, c.driver_assignment_id, c.work_date, c.payroll_period_definition_id,
             c.quantity, c.expected_present, c.expected_quantity) for index, c in enumerate(batch)
        ))
    await db.execute(text(f"ANALYZE {table}"))
    # Missing targets are inferred from absent ordinals below. An inner join
    # lets PostgreSQL start from either side, including immediately after a
    # large create when canonical-table statistics have not caught up yet.
    before_rows = (await db.execute(text(f"""
        SELECT x.ordinal, d.draftlineid, d.quantity, d.sourcetype, d.status, d.notes
        FROM payroll.payrolldraftlines d JOIN {table} x ON d.companyid = :cid
            AND d.branchid = :bid AND d.payrollperiodid = :pid
            AND d.driverassignmentid = x.assignment_id AND d.workdate = x.work_date
            AND d.payrollperioddefinitionid = x.definition_id AND d.status <> 'Void'
        ORDER BY x.ordinal
    """), params)).mappings().all()
    before = {row["ordinal"]: row for row in before_rows if row["draftlineid"] is not None}
    for ordinal, change in enumerate(changes):
        old = before.get(ordinal)
        if change.expected_present != (old is not None) or (
            old is not None and change.expected_quantity != old["quantity"]
        ):
            _reject("QUANTITY_BATCH_STALE", "Reviewed current source changed; rebuild the plan.", 409)

    positive = [c for c in changes if c.quantity > 0]
    if positive:
        await require_period_eligibility_snapshot(pid, cid, bid, db)
        roster = {r.driverassignmentid: r for r in (await db.execute(text("""
            SELECT driverassignmentid, iseligibleforperiod, eligibilityreasoncode, eligibleworkdates
            FROM payroll.payrollperioddrivereligibility
            WHERE companyid = :cid AND branchid = :bid AND payrollperiodid = :pid
                AND driverassignmentid = ANY(CAST(:ids AS integer[]))
        """), {**params, "ids": assignment_ids})).all()}
        live = await lifecycle_eligible_assignment_days(pid, cid, bid, None, assignment_ids, db)
        days = set((await db.execute(text("""SELECT workdate FROM payroll.payrollperioddays
            WHERE companyid = :cid AND payrollperiodid = :pid"""), params)).scalars())
        for ordinal, change in enumerate(changes):
            if change.quantity <= 0:
                continue
            row = roster.get(change.driver_assignment_id)
            if not scope.period.start_date <= change.work_date <= scope.period.end_date or change.work_date not in days:
                _reject("QUANTITY_BATCH_DATE", "A supplied WorkDate is outside this period's day snapshot.")
            fresh = (row is not None and _is_snapshot_row_eligible_for_workdate(row, change.work_date)
                     and (change.driver_assignment_id, change.work_date) in live)
            # Rescue only updates the same existing target; never a new source.
            if row is None or not fresh and ordinal not in before:
                _reject("QUANTITY_BATCH_ELIGIBILITY", "This assignment/date cannot create the reviewed source.")
        off = await db.scalar(text(f"""
            SELECT EXISTS (
                SELECT 1 FROM {table} x JOIN payroll.payrollperioddriverdayentrystate e
                  ON e.companyid = :cid AND e.branchid = :bid AND e.payrollperiodid = :pid
                    AND e.driverassignmentid = x.assignment_id AND e.workdate = x.work_date AND NOT e.isvoided
                JOIN payroll.payrollstatuskeys k ON k.companyid = e.companyid AND k.statuskeyid = e.statuskeyid
                WHERE x.quantity > 0 AND k.isoffreason)
        """), params)
        if off:
            _reject("STATUS_OFF_WORK_CONFLICT", "Reviewed positive work overlaps a full-day-off Status.")

    # All effects below retain production triggers/FKs and share one transaction.
    returned = []
    common_return = """d.draftlineid, d.driverassignmentid, d.workdate, d.payrollperioddefinitionid,
                       d.quantity, d.sourcetype, d.status, d.notes"""
    for lower in range(0, len(changes), batch_size):
        bind = {**params, "lower": lower, "upper": lower + batch_size}
        batch = changes[lower:lower + batch_size]
        if any(not change.expected_present and change.quantity > 0 for change in batch):
            returned.extend((await db.execute(text(f"""
            INSERT INTO payroll.payrolldraftlines AS d
                (companyid, branchid, payrollperiodid, driverassignmentid, workdate,
                 payrollperioddefinitionid, quantity, sourcetype, status, addedbyuserid)
            SELECT :cid, :bid, :pid, x.assignment_id, x.work_date, x.definition_id,
                x.quantity, 'Import', 'Active', :uid FROM {table} x
            WHERE x.ordinal >= :lower AND x.ordinal < :upper AND x.quantity > 0 AND NOT x.expected_present
            RETURNING {common_return}
            """), bind)).mappings().all())
        if any(change.expected_present and change.quantity > 0
               and change.quantity != change.expected_quantity for change in batch):
            returned.extend((await db.execute(text(f"""
            UPDATE payroll.payrolldraftlines d SET quantity = x.quantity, sourcetype = 'Import'
            FROM {table} x WHERE x.ordinal >= :lower AND x.ordinal < :upper AND x.expected_present AND x.quantity > 0
                AND d.companyid = :cid AND d.branchid = :bid AND d.payrollperiodid = :pid
                AND d.driverassignmentid = x.assignment_id AND d.workdate = x.work_date
                AND d.payrollperioddefinitionid = x.definition_id AND d.status <> 'Void'
                AND d.quantity IS DISTINCT FROM x.quantity
            RETURNING {common_return}
            """), bind)).mappings().all())
        if any(change.expected_present and change.quantity == 0
               and change.expected_quantity != 0 for change in batch):
            returned.extend((await db.execute(text(f"""
            UPDATE payroll.payrolldraftlines d SET status = 'Void', sourcetype = 'Import'
            FROM {table} x WHERE x.ordinal >= :lower AND x.ordinal < :upper AND x.expected_present AND x.quantity = 0
                AND d.companyid = :cid AND d.branchid = :bid AND d.payrollperiodid = :pid
                AND d.driverassignmentid = x.assignment_id AND d.workdate = x.work_date
                AND d.payrollperioddefinitionid = x.definition_id AND d.status <> 'Void' AND d.quantity <> 0
            RETURNING {common_return}
            """), bind)).mappings().all())

    by_key = {c.key: index for index, c in enumerate(changes)}
    mutations = []
    counts = {"created": 0, "updated": 0, "voided": 0}
    for row in returned:
        key = (row["driverassignmentid"], row["workdate"], row["payrollperioddefinitionid"])
        old = before.get(by_key[key])
        kind = "created" if old is None else "voided" if row["status"] == "Void" else "updated"
        counts[kind] += 1
        def state(value):
            return {"quantity": value["quantity"], "source_type": value["sourcetype"],
                    "status": value["status"], "notes": value["notes"]}
        mutations.append({"key": key, "line_id": row["draftlineid"], "driver_assignment_id": key[0],
            "work_date": key[1], "definition": definitions[key[2]],
            "audit_action": {"created": "DRAFT_LINE_ADDED", "updated": "DRAFT_LINE_UPDATED", "voided": "DRAFT_LINE_VOIDED"}[kind],
            "evidence_action": {"created": "SOURCE_CREATED", "updated": "SOURCE_UPDATED", "voided": "SOURCE_VOIDED"}[kind],
            "before": state(old) if old else None, "after": state(row)})
    await write_quantity_audit_batch(db, company_id=cid, branch_id=bid, user_id=scope.user_id,
                                    changes=mutations, correlation_id=correlation_id)
    evidence = await capture_source_evidence_batch(company_id=cid, branch_id=bid, period_id=pid,
        user_id=scope.user_id, changes=mutations, correlation_id=correlation_id, db=db, source_revision=source_revision)
    changed = {m["key"]: (m["line_id"], evidence[m["line_id"]]) for m in mutations}
    return QuantityMutationResult(**counts, unchanged=len(changes) - len(mutations), changed=changed)
