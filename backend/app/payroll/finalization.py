"""
Approved-snapshot verification, FinalLines projection, and finalization preview.

Finalization uses only the snapshot bound to the single Approved PeriodApproval. It verifies
the snapshot (calculation version, the hash recomputed from the persisted rows, and the
arithmetic of the evidence), then projects one FinalLine per official component. It never
consults live sources or configuration and never selects a "latest" snapshot. The preview
reads the same snapshot through the same loader and projection, so what it shows is exactly
what finalization writes.

The preview is read-only. Finalization projects lines, updates period status, and writes its
audit in the caller's transaction, so an audit failure rolls back the entire operation.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.permissions import check_permission
from app.access.policy import require_non_driver_subject
from app.company_currency import (
    frozen_currency,
    lock_and_get_company_currency_for_monetary_write,
    require_matching_snapshot_currency,
)
from app.payroll.evidence_gate import require_target_payroll_evidence
from app.payroll.final_line_projection import project_final_lines
from app.payroll.immutable_evidence import capture_workflow_action_evidence
from app.payroll.period_read import get_period_by_id
from app.payroll.schemas import PeriodSummary
from app.payroll.snapshot_verification import SnapshotEvidenceError, verify_snapshot
from app.payroll.workflow_lock import acquire_branch_workflow_lock

if TYPE_CHECKING:
    from app.payroll.schemas import FinalizationPreviewResponse

# ===========================================================================
# Finalization — Approved → Locked
# ===========================================================================


# ---------------------------------------------------------------------------
# Audit helper — extracted so tests can monkeypatch it to verify rollback.
# All writes in finalize_period() share the same engine.begin() transaction,
# so if _write_finalization_audit() raises, the entire transaction rolls back:
# the UPDATE and INSERT are undone and the period reverts to Approved.
# ---------------------------------------------------------------------------

async def _write_finalization_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    branch_id: int,
    user_id: int,
    period_id: int,
    line_count: int,
    total_amount: Decimal,
    approved_review_item_id: int | None = None,
    snapshot_id: int | None = None,
    revision_number: int | None = None,
    snapshot_hash: str | None = None,
) -> None:
    """Insert one row into audit.AuditLog for the finalization event."""
    old_val = json.dumps({"status": "Approved"})
    new_value = {
        "status":             "Locked",
        "final_line_count":   line_count,
        "total_final_amount": str(total_amount),
    }
    if approved_review_item_id is not None:
        new_value.update({
            "approved_review_item_id": approved_review_item_id,
            "payroll_calculation_snapshot_id": snapshot_id,
            "revision_number": revision_number,
            "snapshot_hash": snapshot_hash,
        })
    new_val = json.dumps(new_value)
    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid,
                 oldvaluejson, newvaluejson, reason, sourcetype)
            VALUES
                (:company_id, :branch_id, :actor_id, 'PAYROLL_FINALIZED',
                 'payroll', 'PayrollPeriods', :entity_id,
                 :old_val, :new_val, 'Payroll period finalized', 'Application')
        """),
        {
            "company_id": company_id,
            "branch_id":  branch_id,
            "actor_id":   user_id,
            "entity_id":  str(period_id),
            "old_val":    old_val,
            "new_val":    new_val,
        },
    )


# ---------------------------------------------------------------------------
# Approved immutable snapshot -> verified content -> FinalLines projection
# ---------------------------------------------------------------------------

def _snapshot_finalization_error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"{code}: {message}")


def _evidence_error(exc: SnapshotEvidenceError) -> HTTPException:
    """A snapshot that cannot be consumed fails finalization closed with its stable code."""
    code = "APPROVED_SNAPSHOT_INTEGRITY_ERROR" if exc.code == "SNAPSHOT_NOT_FOUND" else exc.code
    return _snapshot_finalization_error(code, exc.message)


async def _load_approved_snapshot_packet(
    *, period_id: int, company_id: int, branch_id: int,
    db: AsyncConnection, lock_review_item: bool = False,
) -> dict[str, Any]:
    """Load and verify the one immutable snapshot authorized by an Approved PeriodApproval."""
    review_lock = " FOR UPDATE OF ri" if lock_review_item else ""
    reviews = (await db.execute(text(f"""
        SELECT ri.reviewitemid, ri.payrollcalculationsnapshotid
        FROM review.managerreviewitems ri
        WHERE ri.companyid = :cid AND ri.branchid = :bid
          AND ri.requesttype = 'PeriodApproval'
          AND ri.entityschema = 'payroll' AND ri.entityname = 'PayrollPeriods'
          AND ri.entityid = :period_id AND ri.status = 'Approved'{review_lock}
    """), {"cid": company_id, "bid": branch_id, "period_id": str(period_id)})).mappings().all()
    if not reviews:
        raise _snapshot_finalization_error(
            "APPROVED_SNAPSHOT_NOT_FOUND_FOR_FINALIZATION",
            "no approved PeriodApproval review item authorizes this period.",
        )
    if len(reviews) != 1:
        raise _snapshot_finalization_error(
            "APPROVED_SNAPSHOT_INTEGRITY_ERROR",
            "more than one approved PeriodApproval review item exists for this period.",
        )
    review = reviews[0]
    snapshot_id = review["payrollcalculationsnapshotid"]
    if snapshot_id is None:
        raise _snapshot_finalization_error(
            "SNAPSHOT_REQUIRED_FOR_FINALIZATION",
            "the approved PeriodApproval has no immutable calculation snapshot.",
        )
    try:
        content = await verify_snapshot(
            db, snapshot_id=int(snapshot_id), company_id=company_id, branch_id=branch_id,
            period_id=period_id)
    except SnapshotEvidenceError as exc:
        raise _evidence_error(exc) from exc
    projection = project_final_lines(content)
    if not projection:
        raise _snapshot_finalization_error(
            "APPROVED_SNAPSHOT_INTEGRITY_ERROR", "the approved snapshot has no official component.")
    return {"review": review, "snapshot": content, "projection": projection}


async def _project_approved_snapshot_final_lines(
    *, packet: dict[str, Any], period_id: int, company_id: int, branch_id: int,
    user_id: int, db: AsyncConnection,
) -> None:
    snapshot = packet["snapshot"]
    await db.execute(text("SELECT set_config('app.allow_payroll_final_line_insert', 'true', true)"))
    for row in packet["projection"]:
        await db.execute(text("""
            INSERT INTO payroll.payrollfinallines
                (companyid, branchid, payrollperiodid, driverassignmentid, driverid, employeeid,
                 linetype, linescope, finalamount, sourcetype, sourceid, approvedbyuserid,
                 approvedatutc, lockedatutc, sourcesnapshot, currencycode,
                 currencyminorunitdigits, payrollcalculationsnapshotid,
                 payrollcalculationsnapshotcomponentid, componentkind,
                 payrollperioddefinitionid, paydefinitionid)
            VALUES
                (:cid, :bid, :period_id, :driver_assignment_id, :driver_id, :employee_id,
                 :line_type, 'Period', :amount, 'SnapshotComponent', :source_id, :approved_by,
                 NOW(), NOW(), CAST(:source_snapshot AS jsonb), :currency_code, :currency_minor,
                 :snapshot_id, :component_id, :component_kind, :ppd_id, :pay_definition_id)
        """), {
            "cid": company_id, "bid": branch_id, "period_id": period_id,
            "driver_assignment_id": row["driver_assignment_id"], "driver_id": row["driver_id"],
            "employee_id": row["employee_id"], "line_type": row["line_type"],
            "amount": row["amount"], "source_id": str(row["component_id"]),
            "approved_by": user_id, "source_snapshot": json.dumps(row["source_snapshot"]),
            "currency_code": snapshot["CurrencyCode"],
            "currency_minor": snapshot["CurrencyMinorUnitDigits"],
            "snapshot_id": snapshot["PayrollCalculationSnapshotID"],
            "component_id": row["component_id"], "component_kind": row["component_kind"],
            "ppd_id": row["payroll_period_definition_id"],
            "pay_definition_id": row["pay_definition_id"],
        })


async def finalize_period(period_id: int, company_id: int, user_id: int, db: AsyncConnection) -> PeriodSummary:
    """Project the exact approved immutable snapshot into FinalLines and lock the period."""
    await require_non_driver_subject(company_id, user_id, db)
    period = await get_period_by_id(company_id, user_id, period_id, db)
    if period.status != "Approved":
        raise HTTPException(status_code=422, detail=f"Only Approved periods can be finalized (current status: '{period.status}').")
    await check_permission(company_id, user_id, period.branch_id, "payroll.finalize", db)
    require_target_payroll_evidence()
    currency = await lock_and_get_company_currency_for_monetary_write(company_id, db)
    await acquire_branch_workflow_lock(company_id, period.branch_id, db)
    locked = (await db.execute(text("""
        SELECT payrollperiodid FROM payroll.payrollperiods
        WHERE payrollperiodid = :period_id AND companyid = :cid AND branchid = :bid AND status = 'Approved'
        FOR UPDATE
    """), {"period_id": period_id, "cid": company_id, "bid": period.branch_id})).scalar_one_or_none()
    if locked is None:
        raise HTTPException(status_code=422, detail="Period could not be claimed for finalization — its status may have changed concurrently.")
    packet = await _load_approved_snapshot_packet(period_id=period_id, company_id=company_id, branch_id=period.branch_id, db=db, lock_review_item=True)
    snapshot = packet["snapshot"]
    require_matching_snapshot_currency(currency, snapshot["CurrencyCode"], snapshot["CurrencyMinorUnitDigits"])
    claimed = await db.execute(text("""
        UPDATE payroll.payrollperiods SET status = 'Locked', lockedbyuserid = :locker, lockedatutc = NOW()
        WHERE payrollperiodid = :period_id AND companyid = :cid AND status = 'Approved'
        RETURNING payrollperiodid
    """), {"locker": user_id, "period_id": period_id, "cid": company_id})
    if claimed.scalar_one_or_none() is None:
        raise HTTPException(status_code=422, detail="Period could not be claimed for finalization — its status may have changed concurrently.")
    await _project_approved_snapshot_final_lines(packet=packet, period_id=period_id, company_id=company_id, branch_id=period.branch_id, user_id=user_id, db=db)
    await capture_workflow_action_evidence(
        company_id=company_id,
        branch_id=period.branch_id,
        period_id=period_id,
        snapshot_id=int(snapshot["PayrollCalculationSnapshotID"]),
        review_item_id=int(packet["review"]["reviewitemid"]),
        action_code="FINALIZED",
        user_id=user_id,
        required_permission_code="payroll.finalize",
        db=db,
    )
    await _write_finalization_audit(
        db, company_id=company_id, branch_id=period.branch_id, user_id=user_id,
        period_id=period_id, line_count=len(packet["projection"]),
        total_amount=snapshot["TotalExpectedPay"],
        approved_review_item_id=int(packet["review"]["reviewitemid"]),
        snapshot_id=int(snapshot["PayrollCalculationSnapshotID"]),
        revision_number=int(snapshot["RevisionNumber"]), snapshot_hash=str(snapshot["SnapshotHash"]),
    )
    return await get_period_by_id(company_id, user_id, period_id, db)


async def get_finalization_preview(period_id: int, company_id: int, user_id: int, db: AsyncConnection) -> FinalizationPreviewResponse:
    """Read the same verified snapshot and projection that finalization will write."""
    from app.payroll.schemas import (
        BonusEventPreviewEntry,
        FinalizationPreviewDriverTotal,
        FinalizationPreviewLine,
        FinalizationPreviewResponse,
        FinalizationPreviewSysAdjustment,
    )
    await require_non_driver_subject(company_id, user_id, db)
    period = await get_period_by_id(company_id, user_id, period_id, db)
    if period.status != "Approved":
        raise HTTPException(status_code=422, detail=f"Finalization preview requires an Approved period. Current status: '{period.status}'.")
    await check_permission(company_id, user_id, period.branch_id, "payroll.finalize", db)
    require_target_payroll_evidence()
    packet = await _load_approved_snapshot_packet(period_id=period_id, company_id=company_id, branch_id=period.branch_id, db=db)
    snapshot = packet["snapshot"]
    currency = frozen_currency(snapshot["CurrencyCode"], snapshot["CurrencyMinorUnitDigits"])
    totals = {t["DriverAssignmentID"]: t for t in snapshot["DriverTotals"]}

    lines, adjustments, bonuses, driver_totals = [], [], [], []
    for row in packet["projection"]:
        total = totals[row["driver_assignment_id"]]
        lines.append(FinalizationPreviewLine(
            draft_line_id=None, source_key=f"component:{row['component_id']}",
            driver_assignment_id=row["driver_assignment_id"], driver_name=row["driver_name"],
            work_date=None, line_type=row["line_type"], line_scope="Period",
            quantity=None, rate_amount=None, calculated_amount=row["amount"],
            final_amount=row["amount"], needs_manager_review=False,
            component_kind=row["component_kind"],
            payroll_period_definition_id=row["payroll_period_definition_id"],
            pay_definition_id=row["pay_definition_id"], label=row["label"],
            pre_rounding_amount=row["pre_rounding_amount"],
        ))
        if row["component_kind"] in ("MINIMUM_ADJUSTMENT", "MAXIMUM_ADJUSTMENT"):
            adjustments.append(FinalizationPreviewSysAdjustment(
                driver_assignment_id=row["driver_assignment_id"], driver_name=row["driver_name"],
                adjustment_type=row["line_type"], gross_before=total["MinMax"]["BaseAmount"],
                adjustment_amount=row["amount"], bonus_total=total["BonusTotal"],
                final_pay=total["ExpectedPay"]))
    for total in snapshot["DriverTotals"]:
        for line in total["Lines"]:
            if line["SourceType"] == "BonusEvent":
                evidence = line["SourceEvidenceJSONB"] or {}
                bonuses.append(BonusEventPreviewEntry(
                    bonus_event_id=int(line["BonusEventID"]),
                    driver_assignment_id=total["DriverAssignmentID"],
                    driver_name=total["DriverNameSnapshot"], amount=line["CalculatedAmount"],
                    reason=evidence.get("Reason"), notes=evidence.get("Notes")))
        driver_totals.append(FinalizationPreviewDriverTotal(
            driver_assignment_id=total["DriverAssignmentID"], driver_id=total["DriverID"],
            driver_number=total["DriverNumberSnapshot"], driver_name=total["DriverNameSnapshot"],
            daily_pay=total["DailyPay"], status_pay=total["StatusPay"],
            gross_pay=total["DailyPay"] + total["StatusPay"],
            sys_adjustment=total["MinimumAdjustment"] + total["MaximumAdjustment"],
            bonus_total=total["BonusTotal"], final_pay=total["ExpectedPay"],
            line_count=len(total["Components"]),
        ))
    return FinalizationPreviewResponse(
        currency_code=currency.code, currency_minor_unit_digits=currency.minor_unit_digits,
        period_id=period_id, period_name=period.period_name, period_status=period.status,
        branch_id=period.branch_id, branch_name=period.branch_name,
        can_finalize=True, blockers=[], warnings=list(snapshot["Warnings"]),
        driver_totals=driver_totals, sys_adjustments=adjustments, lines=lines,
        bonus_events=bonuses, bonus_event_count=len(bonuses),
        total_final_gross=snapshot["TotalExpectedPay"],
        draft_line_count=sum(
            1 for total in snapshot["DriverTotals"] for line in total["Lines"]
            if line["SourceType"] == "DraftLine"),
        sys_adjustment_count=len(adjustments),
        final_line_count_estimate=len(packet["projection"]), driver_count=len(driver_totals),
    )
