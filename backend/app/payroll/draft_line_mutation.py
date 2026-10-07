"""
Ordinary source-line mutation — add, update and void a Daily source row.

An ordinary PayDefinition source row is a source FACT: a Driver, a WorkDate, a
period definition (PayrollPeriodDefinitionID) and a quantity. It stores no rate,
no amount and no review flag; money is derived live
(app.payroll.live_source_calculation) and frozen only by later evidence.

Identity is the frozen period definition, never a code or a name. InputType comes
from the period snapshot (WholeNumber rejects fractions) and the definition's
calculation method must be operational. Branch applicability was resolved when the
period was created; this module does not read live configuration.

Day Grid (save_day_grid, in app.payroll.day_grid) calls these functions in-process.
Status and notes belong to Day Grid / Day Entry State and are not written here.

Owns the write surface only; reads live in app.payroll.source_line_read.
"""
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import _check_permission
from app.payroll.definition_calculation import (
    PeriodDefinition,
    is_method_operational,
    quantity_error,
)
from app.payroll.eligibility import _assert_driver_eligible_for_workdate_via_snapshot
from app.payroll.line_audit import _write_line_audit
from app.payroll.mutation_lock import _lock_period_for_mutation
from app.payroll.period_day_calendar import _validate_period_work_date
from app.payroll.period_definitions import get_period_definition
from app.payroll.period_read import get_period_by_id
from app.payroll.schemas import (
    _WRITE_BLOCKED_STATUSES,
    ENTRY_ALLOWED_STATUSES,
    DraftLineCreate,
    DraftLineSummary,
    DraftLineUpdate,
)
from app.payroll.source_evidence import _capture_source_evidence
from app.payroll.source_line_read import _get_line_by_id


def _ordinary_lines_only() -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=(
            "Only ordinary pay lines can be changed here. Status, notes and "
            "status pay are managed from the Day Grid."
        ),
    )


def _require_usable_definition(definition: PeriodDefinition | None) -> PeriodDefinition:
    if definition is None:
        raise HTTPException(
            status_code=422,
            detail="The pay definition is not part of this payroll period.",
        )
    if not definition.is_active:
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{definition.name}' was not active for this branch when the period "
                "was created and cannot be used for new entries."
            ),
        )
    if not is_method_operational(definition.calculation_method):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CALCULATION_METHOD_NOT_READY",
                "message": f"'{definition.name}' uses a calculation method that is not "
                           "operational yet.",
            },
        )
    return definition


def _require_valid_quantity(definition: PeriodDefinition, quantity: Decimal) -> None:
    problem = quantity_error(definition.input_type, quantity)
    if problem is not None:
        raise HTTPException(status_code=422, detail=f"{definition.name}: {problem}")


async def _load_line_definition(
    line: DraftLineSummary, period_id: int, company_id: int, db: AsyncConnection,
) -> PeriodDefinition | None:
    if line.payroll_period_definition_id is None:
        return None
    return await get_period_definition(
        period_id, company_id, line.payroll_period_definition_id, db)


# ---------------------------------------------------------------------------
# Add a source line
# ---------------------------------------------------------------------------

async def add_draft_line(
    period_id: int,
    company_id: int,
    user_id: int,
    data: DraftLineCreate,
    db: AsyncConnection,
) -> DraftLineSummary:
    """Insert one ordinary source row against a period definition."""
    period = await get_period_by_id(company_id, user_id, period_id, db)

    # Draft (Prepared) and the editable statuses accept operational source entry.
    if period.status != "Draft" and period.status not in ENTRY_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Draft lines can only be added to Open or Returned periods "
                f"(current status: '{period.status}')."
            ),
        )

    await _check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    definition = _require_usable_definition(
        await get_period_definition(
            period.payroll_period_id, company_id, data.payroll_period_definition_id, db)
    )
    _require_valid_quantity(definition, data.quantity)

    await _validate_period_work_date(
        period.payroll_period_id, data.work_date,
        period.start_date, period.end_date, db,
    )
    # Creating new source: an out-of-window driver is not rescued.
    await _assert_driver_eligible_for_workdate_via_snapshot(
        company_id, period.branch_id, period_id, data.driver_id, data.work_date, db,
        allow_existing_source_rescue=False,
    )

    duplicate = await db.execute(
        text("""
            SELECT draftlineid
            FROM   payroll.payrolldraftlines
            WHERE  companyid = :company_id AND payrollperiodid = :period_id
              AND  driverid = :driver_id AND workdate = :work_date
              AND  payrollperioddefinitionid = :ppd
              AND  status != 'Void'
            LIMIT 1
        """),
        {"company_id": company_id, "period_id": period_id, "driver_id": data.driver_id,
         "work_date": data.work_date, "ppd": definition.payroll_period_definition_id},
    )
    if duplicate.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                "A payroll line for this driver, date, and pay item already exists. "
                "Update the existing line instead."
            ),
        )

    # Recheck period status under a row-level lock before writing.
    await _lock_period_for_mutation(period_id, company_id, db)

    line_id: int = (await db.execute(
        text("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid, workdate,
                 payrollperioddefinitionid, quantity, sourcetype, status, notes, addedbyuserid)
            VALUES
                (:company_id, :branch_id, :period_id, :driver_id, :work_date,
                 :ppd, :quantity, :source_type, 'Active', :notes, :added_by)
            RETURNING draftlineid
        """),
        {
            "company_id": company_id, "branch_id": period.branch_id, "period_id": period_id,
            "driver_id": data.driver_id, "work_date": data.work_date,
            "ppd": definition.payroll_period_definition_id, "quantity": data.quantity,
            "source_type": data.source_type, "notes": data.notes, "added_by": user_id,
        },
    )).scalar_one()

    await _write_line_audit(
        db, company_id=company_id, branch_id=period.branch_id, user_id=user_id,
        line_id=line_id, action_code="DRAFT_LINE_ADDED",
        new_value={
            "period_id": period_id, "driver_id": data.driver_id,
            "payroll_period_definition_id": definition.payroll_period_definition_id,
            "quantity": float(data.quantity), "source_type": data.source_type,
        },
    )
    await _capture_source_evidence(
        company_id=company_id, branch_id=period.branch_id, period_id=period_id,
        user_id=user_id, line_id=line_id, action_code="SOURCE_CREATED", db=db,
        before_state=None,
        after_state={"quantity": data.quantity, "source_type": data.source_type,
                     "status": "Active", "notes": data.notes},
        driver_id=data.driver_id, work_date=data.work_date, definition=definition,
    )
    return await _get_line_by_id(line_id, company_id, db)


# ---------------------------------------------------------------------------
# Update a source line
# ---------------------------------------------------------------------------

async def update_draft_line(
    period_id: int,
    draft_line_id: int,
    company_id: int,
    user_id: int,
    data: DraftLineUpdate,
    db: AsyncConnection,
) -> DraftLineSummary:
    """Partially update an ordinary source row — only supplied fields are touched."""
    period = await get_period_by_id(company_id, user_id, period_id, db)

    if period.status != "Draft" and period.status in _WRITE_BLOCKED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot modify lines on a period with status '{period.status}'.",
        )

    await _check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    line = await _get_line_by_id(draft_line_id, company_id, db)
    if line.period_id != period_id:
        raise HTTPException(status_code=404, detail="Draft line not found in this period.")
    if line.payroll_period_definition_id is None:
        raise _ordinary_lines_only()
    if line.status == "Void":
        raise HTTPException(status_code=422, detail="Cannot modify a voided draft line.")

    definition = await _load_line_definition(line, period_id, company_id, db)
    if definition is None:
        raise HTTPException(
            status_code=422, detail="The pay definition is not part of this payroll period.")

    # Voiding stays possible for cleanup; any other change re-validates.
    is_void_only = data.status == "Void" and data.quantity is None and data.notes is None
    if not is_void_only:
        _require_usable_definition(definition)
        if data.quantity is not None:
            _require_valid_quantity(definition, data.quantity)
        await _assert_driver_eligible_for_workdate_via_snapshot(
            company_id, period.branch_id, period_id, line.driver_id, line.work_date, db,
        )

    fields: dict[str, Any] = {}
    if data.quantity is not None:
        fields["quantity"] = data.quantity
    if data.notes is not None:
        fields["notes"] = data.notes
    if data.status is not None:
        fields["status"] = data.status

    if fields:
        await _lock_period_for_mutation(period_id, company_id, db)
        set_clause = ", ".join(f"{col} = :{col}" for col in fields)
        await db.execute(
            text(
                f"UPDATE payroll.payrolldraftlines SET {set_clause} "
                "WHERE draftlineid = :line_id "
                "  AND payrollperiodid = :period_id AND companyid = :company_id"
            ),
            {**fields, "line_id": draft_line_id, "period_id": period_id,
             "company_id": company_id},
        )
        await _write_line_audit(
            db, company_id=company_id, branch_id=period.branch_id, user_id=user_id,
            line_id=draft_line_id, action_code="DRAFT_LINE_UPDATED",
            new_value={k: (float(v) if isinstance(v, Decimal) else v)
                       for k, v in fields.items()},
        )
        before_state = {"quantity": line.quantity, "source_type": line.source_type,
                        "status": line.status, "notes": line.notes}
        await _capture_source_evidence(
            company_id=company_id, branch_id=period.branch_id, period_id=period_id,
            user_id=user_id, line_id=draft_line_id, action_code="SOURCE_UPDATED", db=db,
            before_state=before_state, after_state={**before_state, **fields},
            driver_id=line.driver_id, work_date=line.work_date, definition=definition,
        )

    return await _get_line_by_id(draft_line_id, company_id, db)


# ---------------------------------------------------------------------------
# Void a source line (soft-delete)
# ---------------------------------------------------------------------------

async def void_draft_line(
    period_id: int,
    draft_line_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> None:
    """Soft-delete: set status = 'Void'. Idempotent if already void."""
    period = await get_period_by_id(company_id, user_id, period_id, db)

    if period.status != "Draft" and period.status in _WRITE_BLOCKED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot void lines on a period with status '{period.status}'.",
        )

    await _check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    line = await _get_line_by_id(draft_line_id, company_id, db)
    if line.period_id != period_id:
        raise HTTPException(status_code=404, detail="Draft line not found in this period.")
    if line.payroll_period_definition_id is None:
        raise _ordinary_lines_only()
    if line.status == "Void":
        return  # Idempotent

    definition = await _load_line_definition(line, period_id, company_id, db)
    if definition is None:
        raise HTTPException(
            status_code=422, detail="The pay definition is not part of this payroll period.")

    await _lock_period_for_mutation(period_id, company_id, db)

    await db.execute(
        text(
            "UPDATE payroll.payrolldraftlines SET status = 'Void' "
            "WHERE draftlineid = :line_id "
            "  AND payrollperiodid = :period_id AND companyid = :company_id"
        ),
        {"line_id": draft_line_id, "period_id": period_id, "company_id": company_id},
    )
    await _write_line_audit(
        db, company_id=company_id, branch_id=period.branch_id, user_id=user_id,
        line_id=draft_line_id, action_code="DRAFT_LINE_VOIDED",
        old_value={"status": "Active"}, new_value={"status": "Void"},
    )
    await _capture_source_evidence(
        company_id=company_id, branch_id=period.branch_id, period_id=period_id,
        user_id=user_id, line_id=draft_line_id, action_code="SOURCE_VOIDED", db=db,
        before_state={"quantity": line.quantity, "source_type": line.source_type,
                      "status": line.status, "notes": line.notes},
        after_state={"status": "Void"}, driver_id=line.driver_id,
        work_date=line.work_date, definition=definition,
    )
