"""
Payroll domain service — periods (list, create, status change) and draft lines.

All SQL is raw parameterised via sqlalchemy.text().
Branch-access enforcement is performed at the top of every mutating function;
read functions filter by the user's allowed branches directly in the query.
"""
import base64
import calendar as _calendar
from functools import wraps
import hashlib
import hmac as _hmac_mod
import json
import math
import uuid as _uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from dataclasses import dataclass
from typing import Any, NamedTuple

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import (
    _check_branch_access, _build_in_clause, _check_permission, _check_any_permission,
    _require_not_driver_role, _has_any_permission,
)
from app.payroll.calculation.per_unit import (
    PER_UNIT_CALCULATION_VERSION,
    PerUnitInput as _PerUnitInput,
    calculate_per_unit as _calculate_per_unit,
)
from app.payroll.immutable_evidence import (
    capture_snapshot_used_rate_definitions,
    capture_workflow_action_evidence,
)
from app.payroll.audit_evidence import (
    capture_period_audit_evidence,
    initialize_period_audit_evidence_coverage,
    link_unmapped_audit_evidence_to_snapshot,
)
from app.payroll.snapshot_hash import (
    CURRENT_PAYROLL_CALCULATION_VERSION,
    CURRENT_REPORT_EVIDENCE_VERSION,
    canonical_json,
    calculate_report_evidence_hash,
    calculate_snapshot_hash,
    calculate_source_config_hash,
)
from app.payroll import status_evidence
from app.payroll.schemas import (
    PeriodSummary, PeriodCreate, PeriodStatusChange, NextPeriodDates, PeriodEntryCount,
    _VALID_TRANSITIONS,
    DraftLineSummary, DraftLineCreate, DraftLineUpdate,
    DriverPeriodSummary, ENTRY_ALLOWED_STATUSES, SOURCE_ENTRY_STATUSES,
    FinalLineSummary,
    RateTypeSummary, DriverRateSummary, DriverRateCreate, DriverRateUpdate,
    TierSummary, OrdinalTierCreate, RangeTierCreate,
    PeriodPayLineCreate, PeriodPayLineUpdate,
    BonusEventCreate, BonusEventUpdate, BonusEventResponse,
    BonusSummaryEvent, BonusSummaryCapabilities, BonusSummaryDriver, BonusSummaryResponse,
    BonusBatchItem, BonusBatchCreate, BonusBatchResponse,
    DriverPayRuleSummary, DriverPayRuleCreate,
    DriverRateMatrix, RateMatrixGroup, RateMatrixCurrentRate,
    BatchRateRequest, BatchRateSaveResult,
    DriverRatesSummary,
    CopyRatesRequest, CopyRatesResult,
    DayGridColumn, DayGridStatusKey, DayGridLineValue,
    DayGridRow, DayGridSummary, DayGridPeriod, DayGridResponse,
    DayGridSaveRequest,
    CandidateSelectedInfo, CandidateNavigationInfo, CandidatePreviewResponse,
    PeriodCreationRequest, PeriodCreationResponse,
    WorkflowCapability, WorkflowSlotItem, WorkflowBranchSlots,
    WorkflowAlert, PeriodWorkflowCapabilities, BranchWorkflowCapabilities,
    BranchWorkflowEntry, CurrentWorkflowResponse,
)
from app.config import settings


# ---------------------------------------------------------------------------
# Period status-change: permission gates and audit
# ---------------------------------------------------------------------------

# Each (from_status, to_status) pair maps to the permission code the caller
# must hold on the period's branch.
#
# Forward lightweight transitions (open/submit) require payroll.entry.
# Approval, reversal, cancellation, and archiving require payroll.finalize —
# these are irreversible or senior-level decisions.
_TRANSITION_PERMISSIONS: dict[tuple[str, str], str] = {
    # CP-1D: ("Draft", "Open") removed — Draft promotion is now done atomically
    # inside the Open→InReview submit path, not via a standalone PATCH transition.
    ("Draft",    "Cancelled"):  "payroll.finalize",   # CP-0C: was missing — any user could cancel Draft
    ("Open",     "InReview"):   "payroll.entry",
    ("Open",     "Cancelled"):  "payroll.finalize",
    # CP-1A: InReview→Open and InReview→Cancelled both removed.
    # InReview has no PATCH exits — the review decision flow is the only exit path.
    # CP-1A: Approved→Cancelled removed — Approved has no PATCH exits.
    #   Approved exits only via POST /finalize (→ Locked).
    ("Locked",   "Archived"):   "payroll.finalize",
}


def _translate_submit_transaction_failures(func):
    """Map retryable PostgreSQL submit/resubmit transaction failures to 409."""
    @wraps(func)
    async def wrapped(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except DBAPIError as exc:
            if _is_retryable_transaction_failure(exc):
                raise HTTPException(
                    status_code=409,
                    detail="Payroll changed concurrently. Refresh and retry the submission.",
                ) from exc
            raise
    return wrapped

_PERIOD_AUDIT_REASONS: dict[str, str] = {
    "PERIOD_STATUS_CHANGED": "Payroll period status changed",
}


async def _write_period_status_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    branch_id: int,
    user_id: int,
    period_id: int,
    old_status: str,
    new_status: str,
    extra: dict | None = None,
) -> None:
    """
    Insert one row into audit.AuditLog for a period status-change event.

    Module-level so tests can monkeypatch it to verify that the preceding
    UPDATE rolls back when this raises.

    extra: optional additional fields merged into newvaluejson (e.g. trigger context).
    """
    new_val_dict: dict = {"status": new_status}
    if extra:
        new_val_dict.update(extra)
    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid,
                 oldvaluejson, newvaluejson, reason, sourcetype)
            VALUES
                (:cid, :bid, :uid, 'PERIOD_STATUS_CHANGED',
                 'payroll', 'PayrollPeriods', :eid,
                 :old_val, :new_val, :reason, 'Application')
        """),
        {
            "cid":     company_id,
            "bid":     branch_id,
            "uid":     user_id,
            "eid":     str(period_id),
            "old_val": json.dumps({"status": old_status}),
            "new_val": json.dumps(new_val_dict),
            "reason":  _PERIOD_AUDIT_REASONS["PERIOD_STATUS_CHANGED"],
        },
    )


# ---------------------------------------------------------------------------
# Draft-line / Period-Pay mutation audit helper
# ---------------------------------------------------------------------------

async def _write_line_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    branch_id: int,
    user_id: int,
    line_id: int,
    action_code: str,
    old_value: dict | None = None,
    new_value: dict | None = None,
    entity_name: str = "PayrollDraftLines",
    correlation_id: str | None = None,
) -> None:
    """
    Insert one row into audit.AuditLog for a draft-line, period-pay, or bonus-event mutation.

    Known action codes:
      DRAFT_LINE_ADDED      — new draft line inserted
      DRAFT_LINE_UPDATED    — draft line fields changed
      DRAFT_LINE_VOIDED     — draft line status set to Void
      PERIOD_PAY_ADDED      — new period-pay line inserted
      PERIOD_PAY_UPDATED    — period-pay line fields changed
      PERIOD_PAY_VOIDED     — period-pay line status set to Void
      BONUS_EVENT_ADDED     — new canonical bonus event created (CP-3A)
      BONUS_EVENT_UPDATED   — bonus event fields changed (CP-3A)
      BONUS_EVENT_VOIDED    — bonus event voided (CP-3A)

    correlation_id: optional UUID string (CP-3B2a).  audit.AuditLog.CorrelationID
    defaults to gen_random_uuid() per row; when correlation_id is supplied,
    this row's CorrelationID is set to that value instead, so every audit row
    written by one logical operation (e.g. a future bonus batch) can share a
    single BatchCorrelationID. When not supplied, behavior is unchanged —
    the INSERT omits the column entirely and the table default applies.

    Module-level so tests can monkeypatch it to verify that all preceding
    writes roll back when this raises.
    """
    _LINE_AUDIT_REASONS: dict[str, str] = {
        "DRAFT_LINE_ADDED":    "Draft line added",
        "DRAFT_LINE_UPDATED":  "Draft line updated",
        "DRAFT_LINE_VOIDED":   "Draft line voided",
        "PERIOD_PAY_ADDED":    "Period pay line added",
        "PERIOD_PAY_UPDATED":  "Period pay line updated",
        "PERIOD_PAY_VOIDED":   "Period pay line voided",
        "BONUS_EVENT_ADDED":   "Bonus event added",
        "BONUS_EVENT_UPDATED": "Bonus event updated",
        "BONUS_EVENT_VOIDED":  "Bonus event voided",
        "BONUS_BATCH_APPLIED": "Bonus batch applied",
    }
    params = {
        "cid":         company_id,
        "bid":         branch_id,
        "uid":         user_id,
        "action_code": action_code,
        "entity_name": entity_name,
        "eid":         str(line_id),
        "old_val":     json.dumps(old_value)  if old_value  is not None else None,
        "new_val":     json.dumps(new_value)  if new_value  is not None else None,
        "reason":      _LINE_AUDIT_REASONS.get(action_code, action_code),
    }
    if correlation_id is not None:
        params["correlation_id"] = correlation_id
        await db.execute(
            text("""
                INSERT INTO audit.auditlog
                    (companyid, branchid, actoruserid, actioncode,
                     entityschema, entityname, entityid,
                     oldvaluejson, newvaluejson, reason, sourcetype, correlationid)
                VALUES
                    (:cid, :bid, :uid, :action_code,
                     'payroll', :entity_name, :eid,
                     :old_val, :new_val, :reason, 'Application', :correlation_id)
            """),
            params,
        )
    else:
        await db.execute(
            text("""
                INSERT INTO audit.auditlog
                    (companyid, branchid, actoruserid, actioncode,
                     entityschema, entityname, entityid,
                     oldvaluejson, newvaluejson, reason, sourcetype)
                VALUES
                    (:cid, :bid, :uid, :action_code,
                     'payroll', :entity_name, :eid,
                     :old_val, :new_val, :reason, 'Application')
            """),
            params,
        )


async def _capture_source_evidence(
    *, company_id: int, branch_id: int, period_id: int, user_id: int,
    line_id: int, action_code: str, db: AsyncConnection,
    before_state: dict[str, Any] | None, after_state: dict[str, Any] | None,
    driver_id: int | None, work_date: date | None, line_type: str,
) -> None:
    """Capture one non-compatibility DraftLine mutation for P6D."""
    pay_item_id = (await db.execute(text("""
        SELECT payitemid
        FROM payroll.payrollperiodpayitems
        WHERE companyid = :company_id AND branchid = :branch_id
          AND payrollperiodid = :period_id AND payitemcode = :line_type
        LIMIT 1
    """), {
        "company_id": company_id, "branch_id": branch_id,
        "period_id": period_id, "line_type": line_type,
    })).scalar_one_or_none()
    await capture_period_audit_evidence(
        company_id=company_id, branch_id=branch_id, period_id=period_id,
        domain="SOURCE", action_code=action_code,
        source_entity_type="PayrollDraftLines", source_entity_id=line_id,
        user_id=user_id, required_permission_code="payroll.entry", db=db,
        before_state=before_state, after_state=after_state, driver_id=driver_id,
        work_date=work_date, pay_item_id=pay_item_id,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _auto_period_name(period_type: str, start: date, end: date) -> str:
    """Generate a human-readable period name from its type and date range."""
    month_names = {
        1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
        7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
    }
    if period_type == "Week":
        return f"Week of {month_names[start.month]} {start.day}, {start.year}"
    if period_type == "Month":
        full_months = {
            1: "January", 2: "February", 3: "March", 4: "April",
            5: "May", 6: "June", 7: "July", 8: "August",
            9: "September", 10: "October", 11: "November", 12: "December",
        }
        return f"{full_months[start.month]} {start.year}"
    # Biweek or Custom
    return (
        f"{month_names[start.month]} {start.day}"
        f" – {month_names[end.month]} {end.day}, {end.year}"
    )


def _row_to_summary(r: Any) -> PeriodSummary:
    return PeriodSummary(
        payroll_period_id=r["payrollperiodid"],
        branch_id=r["branchid"],
        branch_name=r["branchname"],
        parent_period_id=r["parentpayrollperiodid"],
        period_code=r["periodcode"],
        period_name=r["periodname"],
        period_type=r["periodtype"],
        start_date=r["startdate"],
        end_date=r["enddate"],
        pay_date=r["paydate"],
        status=r["status"],
        notes=r["notes"],
        created_by_user_id=r["createdbyuserid"],
        created_at_utc=r["createdatutc"],
        current_return_review_item_id=r.get("currentreturnreviewitemid"),
        draft_drivers=r["draftdrivers"] or 0,
        draft_lines=r["draftlines"] or 0,
        draft_lines_needing_attention=r["draftlinesneedingattention"] or 0,
        final_lines=r["finallines"] or 0,
        final_gross=Decimal(str(r["finalgross"])) if r["finalgross"] is not None else Decimal("0"),
        final_driver_count=r["finaldrivercount"] or 0,
    )


# Base SELECT that joins the view with the base table for the extra fields
# (paydate, notes, createdbyuserid, createdatutc, currentreturnreviewitemid).
_BASE_SELECT = """
    SELECT
        v.payrollperiodid,
        v.branchid,
        v.branchname,
        v.parentpayrollperiodid,
        v.periodcode,
        v.periodname,
        v.periodtype,
        v.startdate,
        v.enddate,
        v.status,
        v.draftdrivers,
        v.draftlines,
        v.draftlinesneedingattention,
        v.finallines,
        v.finalgross,
        v.finaldrivercount,
        p.paydate,
        p.notes,
        p.createdbyuserid,
        p.createdatutc,
        p.currentreturnreviewitemid
    FROM   app.vw_payrollperiodlist v
    JOIN   payroll.payrollperiods   p ON p.payrollperiodid = v.payrollperiodid
"""


# ---------------------------------------------------------------------------
# List periods
# ---------------------------------------------------------------------------

async def get_periods(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    branch_id: int | None = None,
    period_status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[PeriodSummary]:
    # ── Driver-role hard-block (Current Payroll is not a Driver Screen) ─────── #
    await _require_not_driver_role(company_id, user_id, db)

    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    # ── Payroll read permission gate ─────────────────────────────────────────── #
    # Operational users must have payroll.view OR payroll.entry on at least one
    # accessible branch.  Branch access alone (e.g. drivers.view only) is not
    # sufficient to read payroll data.
    #
    # can_see_all only proves branch *access*, not which branches hold payroll
    # permission — a user's AllCompanyBranches assignment can grant an
    # unrelated permission while a separate SpecificBranch assignment grants
    # payroll.view.  sec.fn_UserHasPermission never matches a SpecificBranch
    # row when p_BranchID is NULL (SQL's `x = NULL` is NULL, not TRUE), so
    # every candidate branch must be checked individually.
    _PAYROLL_READ_PERMS = ["payroll.view", "payroll.entry", "payroll.finalize"]
    if can_see_all:
        candidate_rows = (await db.execute(
            text("SELECT branchid FROM core.branches WHERE companyid = :company_id"),
            {"company_id": company_id},
        )).mappings().all()
        candidate_branch_ids = [int(r["branchid"]) for r in candidate_rows]
    else:
        candidate_branch_ids = branch_ids

    permitted_branches = [
        bid for bid in candidate_branch_ids
        if await _has_any_permission(company_id, user_id, bid, _PAYROLL_READ_PERMS, db)
    ]
    if not permitted_branches:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You do not have payroll.view, payroll.entry, or payroll.finalize "
                "permission on any accessible branch."
            ),
        )

    conditions: list[str] = ["v.companyid = :company_id"]
    params: dict[str, Any] = {"company_id": company_id}

    if branch_id is not None:
        if branch_id not in permitted_branches:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to the requested branch.",
            )
        conditions.append("v.branchid = :branch_id")
        params["branch_id"] = branch_id
    else:
        in_clause, in_params = _build_in_clause(permitted_branches, "pb")
        conditions.append(f"v.branchid IN ({in_clause})")
        params.update(in_params)

    if period_status:
        conditions.append("v.status = :period_status")
        params["period_status"] = period_status

    where = " AND ".join(conditions)
    params["limit"] = limit
    params["offset"] = offset

    result = await db.execute(
        text(
            f"{_BASE_SELECT}"
            f"WHERE  {where} "
            f"ORDER  BY v.startdate DESC, v.branchname "
            f"LIMIT  :limit OFFSET :offset"
        ),
        params,
    )
    return [_row_to_summary(r) for r in result.mappings().all()]


# ---------------------------------------------------------------------------
# Single period
# ---------------------------------------------------------------------------

async def get_period_by_id(
    company_id: int,
    user_id: int,
    period_id: int,
    db: AsyncConnection,
) -> PeriodSummary:
    # ── Driver-role hard-block — must be first, before any data is read ─────── #
    await _require_not_driver_role(company_id, user_id, db)

    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    result = await db.execute(
        text(
            f"{_BASE_SELECT}"
            f"WHERE  v.payrollperiodid = :period_id "
            f"  AND  v.companyid       = :company_id"
        ),
        {"period_id": period_id, "company_id": company_id},
    )
    row = result.mappings().first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payroll period not found.",
        )

    if not can_see_all and row["branchid"] not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this period's branch.",
        )

    # ── Payroll permission gate — any payroll-capable role is sufficient ─────── #
    await _check_any_permission(
        company_id, user_id, row["branchid"],
        ["payroll.view", "payroll.entry", "payroll.finalize"],
        db,
    )

    return _row_to_summary(row)


# ---------------------------------------------------------------------------
# Create period
# ---------------------------------------------------------------------------

async def create_period(
    company_id: int,
    user_id: int,
    data: PeriodCreate,
    db: AsyncConnection,
) -> PeriodSummary:
    # ── Driver-role hard-block ───────────────────────────────────────────────── #
    await _require_not_driver_role(company_id, user_id, db)

    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    if not can_see_all and data.branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to the target branch.",
        )

    # Permission gate: creating a period requires payroll.period.create.
    # This is intentionally separate from payroll.entry (data entry/editing).
    # Migration 0030 seeds this permission and assigns it to appropriate roles.
    await _check_permission(company_id, user_id, data.branch_id, "payroll.period.create", db)

    # CP-1C: Acquire branch advisory lock before overlap check and insert.
    # Serializes all period creation (both legacy and candidate-based) for this branch.
    await _acquire_branch_workflow_lock(company_id, data.branch_id, db)

    # CP-1D: Draft creation guard — legacy POST /payroll/periods creates a Draft;
    # this is only valid when exactly one Open exists and no Draft already exists.
    _slot_result = await db.execute(
        text("""
            SELECT status FROM payroll.payrollperiods
            WHERE  companyid = :cid AND branchid = :bid
              AND  status IN ('Draft', 'Open', 'InReview', 'Returned')
        """),
        {"cid": company_id, "bid": data.branch_id},
    )
    _slot_statuses = [r["status"] for r in _slot_result.mappings().all()]
    if "Draft" in _slot_statuses:
        raise HTTPException(
            status_code=409,
            detail={
                "code":    "DRAFT_SLOT_OCCUPIED",
                "message": (
                    "A Draft period already exists for this branch. "
                    "Only one Draft period is permitted per branch at a time."
                ),
            },
        )
    _open_count = _slot_statuses.count("Open")
    if _open_count == 0:
        raise HTTPException(
            status_code=409,
            detail={
                "code":    "DRAFT_CREATION_REQUIRES_OPEN",
                "message": (
                    "No Open period exists for this branch. "
                    "Legacy Draft creation requires exactly one Open period. "
                    "Use the candidate-based period creation endpoint instead."
                ),
            },
        )
    if _open_count > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "code":    "WORKFLOW_SLOT_CONFLICT",
                "message": (
                    "More than one Open period exists for this branch — "
                    "the workflow is in an inconsistent state."
                ),
            },
        )
    # Exactly one Open, no Draft → allow (InReview/Returned co-existence is valid)

    # Verify branch belongs to this company and get its BranchCode for period_code
    br_result = await db.execute(
        text(
            "SELECT branchcode FROM core.branches "
            "WHERE branchid = :bid AND companyid = :cid"
        ),
        {"bid": data.branch_id, "cid": company_id},
    )
    br_row = br_result.mappings().first()
    if br_row is None:
        raise HTTPException(
            status_code=422,
            detail="branch_id does not exist in this company.",
        )
    branch_code: str = br_row["branchcode"]

    # Guard: reject dates that overlap any existing non-cancelled period for this branch.
    # Every status except Cancelled reserves the date range — Locked and Archived
    # represent official historical records that must not be overlapped.
    overlap_row = await db.execute(
        text("""
            SELECT payrollperiodid, status, startdate, enddate
            FROM   payroll.payrollperiods
            WHERE  branchid  = :bid
              AND  companyid = :cid
              AND  status    != 'Cancelled'
              AND  startdate <= :end_date
              AND  enddate   >= :start_date
            LIMIT  1
        """),
        {
            "bid":        data.branch_id,
            "cid":        company_id,
            "start_date": data.start_date,
            "end_date":   data.end_date,
        },
    )
    existing = overlap_row.mappings().first()
    if existing is not None:
        ex_id     = existing["payrollperiodid"]
        ex_status = existing["status"]
        ex_start  = existing["startdate"]
        ex_end    = existing["enddate"]
        raise HTTPException(
            status_code=422,
            detail=(
                f"A payroll period already exists for this date range. "
                f"Existing period (ID {ex_id}) status: {ex_status}, "
                f"dates: {ex_start} – {ex_end}. "
                f"Open the existing period instead of creating a new one. "
                f"Locked and Archived periods are official historical records and cannot be overlapped."
            ),
        )

    # Auto-generate period_name if not provided
    period_name = data.period_name or _auto_period_name(
        data.period_type, data.start_date, data.end_date
    )

    # Auto-generate a unique period_code
    base_code = f"{branch_code}-{data.start_date.strftime('%Y%m%d')}"
    period_code = await _unique_period_code(base_code, company_id, data.branch_id, db)

    # CP-2A: ensure schedule version and set on new period. Still under advisory lock.
    # None means no active setup — reject before inserting a period with NULL version.
    legacy_sv_id = await ensure_current_schedule_version(company_id, data.branch_id, user_id, db)
    if legacy_sv_id is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code":    "PAYROLL_SETUP_REQUIRED",
                "message": (
                    "No active payroll setup or schedule version exists for this branch. "
                    "Configure payroll setup before creating periods."
                ),
            },
        )

    # Insert
    insert_result = await db.execute(
        text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, periodcode, periodname, periodtype,
                 startdate, enddate, paydate, status, notes, createdbyuserid,
                 scheduleversionid)
            VALUES
                (:company_id, :branch_id, :period_code, :period_name, :period_type,
                 :start_date, :end_date, :pay_date, 'Draft', :notes, :created_by,
                 :sv_id)
            RETURNING payrollperiodid
        """),
        {
            "company_id":  company_id,
            "branch_id":   data.branch_id,
            "period_code": period_code,
            "period_name": period_name,
            "period_type": data.period_type,
            "start_date":  data.start_date,
            "end_date":    data.end_date,
            "pay_date":    data.pay_date,
            "notes":       data.notes,
            "created_by":  user_id,
            "sv_id":       legacy_sv_id,
        },
    )
    period_id: int = insert_result.scalar_one()
    await initialize_period_audit_evidence_coverage(
        company_id=company_id, branch_id=data.branch_id, period_id=period_id, db=db,
    )

    # CP-2B: create period-day snapshot from the schedule version's mask.
    # Read from PayrollScheduleVersions (immutable) — not from mutable BranchPayrollSettings.
    sv_mask_row = (await db.execute(
        text(
            "SELECT normaldaysoffmask FROM payroll.PayrollScheduleVersions "
            "WHERE scheduleversionid = :sv_id"
        ),
        {"sv_id": legacy_sv_id},
    )).mappings().first()
    period_mask = sv_mask_row["normaldaysoffmask"] if sv_mask_row else None
    await _create_period_day_rows(
        period_id, company_id, data.branch_id,
        legacy_sv_id, data.start_date, data.end_date, period_mask, db,
    )

    # CP-2C: create period pay-item layout snapshot.
    await _create_period_pay_item_rows(
        period_id, company_id, data.branch_id, data.start_date, db,
    )

    # CP-2E: create driver eligibility snapshot for legacy Draft periods.
    # Draft stays provisional (freeze=False); freeze happens when promoted to Open.
    await _create_period_driver_eligibility_rows(
        period_id, company_id, data.branch_id, db,
        snapshot_source="Generated",
        freeze=False,
        created_by_user_id=user_id,
    )

    return await get_period_by_id(company_id, user_id, period_id, db)


async def _unique_period_code(
    base: str,
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> str:
    """
    Return `base` if it's not already used in this branch, otherwise
    try `base-2`, `base-3`, … until a free slot is found.
    """
    candidate = base
    suffix = 1
    while True:
        result = await db.execute(
            text("""
                SELECT 1 FROM payroll.payrollperiods
                WHERE  companyid = :cid
                  AND  branchid  = :bid
                  AND  periodcode = :code
                  AND  status    != 'Cancelled'
            """),
            {"cid": company_id, "bid": branch_id, "code": candidate},
        )
        if result.first() is None:
            return candidate
        suffix += 1
        candidate = f"{base}-{suffix}"


# ---------------------------------------------------------------------------
# Period-date calculation from payroll setup
# ---------------------------------------------------------------------------

def _month_end(start: date) -> date:
    """
    Return the last day of a one-calendar-month period that starts on `start`.

    The exclusive boundary is the same day of the next month; we subtract one
    day to get the inclusive end:

      start = 2026-06-21  →  next_same_day = 2026-07-21  →  end = 2026-07-20
      start = 2026-01-31  →  next_same_day = 2026-02-28  →  end = 2026-02-27
    """
    month = start.month + 1
    year  = start.year + (1 if month > 12 else 0)
    if month > 12:
        month -= 12
    max_day = _calendar.monthrange(year, month)[1]
    next_month_same_day = start.replace(year=year, month=month, day=min(start.day, max_day))
    return next_month_same_day - timedelta(days=1)


def compute_period_dates(
    frequency: str,
    anchor_start_date: date,
    last_end_date: date | None = None,
    custom_interval_days: int | None = None,
) -> tuple[date, date]:
    """
    Compute the next period's (start, end) dates from a branch's payroll setup.

    Rules
    -----
    - If *last_end_date* is None the first period starts on *anchor_start_date*.
    - Otherwise the next period starts the day after *last_end_date*.
    - Period length depends on *frequency*:

      ======= =============================================
      Week    7 days  (start + 6 days)
      Biweek  14 days (start + 13 days)
      Month   One calendar month (start to same day next month minus 1 day)
      Custom  Requires custom_interval_days > 0 (inclusive period length)
      ======= =============================================

    Both start and end are inclusive.

    Raises
    ------
    ValueError if *frequency* is unrecognised, or if 'Custom' and
    *custom_interval_days* is None or ≤ 0.
    """
    start = anchor_start_date if last_end_date is None else last_end_date + timedelta(days=1)

    if frequency == "Week":
        end = start + timedelta(days=6)
    elif frequency == "Biweek":
        end = start + timedelta(days=13)
    elif frequency == "Month":
        end = _month_end(start)
    elif frequency == "Custom":
        if not custom_interval_days or custom_interval_days <= 0:
            raise ValueError(
                "Custom frequency requires custom_interval_days > 0. "
                "Configure the custom cadence in Payroll Setup first."
            )
        end = start + timedelta(days=custom_interval_days - 1)
    else:
        raise ValueError(f"Unknown payroll frequency: {frequency!r}")

    return start, end


async def get_next_period_dates(
    company_id: int,
    user_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> NextPeriodDates:
    """
    Return suggested start/end dates for the next payroll period of *branch_id*,
    derived from its BranchPayrollSettings and the latest existing period.

    - If no prior non-cancelled periods exist the first period starts on the
      setup's anchor_start_date.
    - Returns ``is_custom=True`` and ``start_date=None`` for Custom frequency.

    Raises 403 if the caller lacks access; 404 if no payroll setup is configured.
    """
    await _require_not_driver_role(company_id, user_id, db)

    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to the requested branch.",
        )
    await _check_any_permission(
        company_id, user_id, branch_id,
        ["payroll.view", "payroll.entry", "payroll.finalize"],
        db,
    )

    # Fetch branch payroll setup
    setup_row = await db.execute(
        text("""
            SELECT payrollfrequency, anchorstartdate, customintervaldays
            FROM   payroll.branchpayrollsettings
            WHERE  branchid  = :bid
              AND  companyid = :cid
              AND  isactive  = TRUE
        """),
        {"bid": branch_id, "cid": company_id},
    )
    setup = setup_row.mappings().first()
    if setup is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No active payroll setup found for branch {branch_id}. "
                "Configure it in Settings → Payroll Setup first."
            ),
        )

    frequency: str         = setup["payrollfrequency"]
    anchor: date           = setup["anchorstartdate"]
    interval_days: int | None = setup.get("customintervaldays")

    # MAX end_date of non-cancelled periods for this branch
    last_row = await db.execute(
        text("""
            SELECT MAX(enddate) AS last_end
            FROM   payroll.payrollperiods
            WHERE  branchid  = :bid
              AND  companyid = :cid
              AND  status    != 'Cancelled'
        """),
        {"bid": branch_id, "cid": company_id},
    )
    last_end: date | None = last_row.scalar_one_or_none()

    is_custom = (frequency == "Custom")
    start_date_out: date | None = None
    end_date_out:   date | None = None

    if frequency == "Custom":
        if interval_days and interval_days > 0:
            # Custom with a valid saved interval — compute automatically
            start_date_out, end_date_out = compute_period_dates(
                frequency, anchor, last_end, custom_interval_days=interval_days
            )
        # else: interval missing → leave start/end as None (setup incomplete)
    else:
        start_date_out, end_date_out = compute_period_dates(frequency, anchor, last_end)

    return NextPeriodDates(
        branch_id=branch_id,
        period_type=frequency,
        anchor_start_date=anchor,
        last_period_end_date=last_end,
        start_date=start_date_out,
        end_date=end_date_out,
        is_custom=is_custom,
        custom_interval_days=interval_days,
    )


async def get_period_entry_count(
    company_id: int,
    user_id: int,
    period_id: int,
    db: AsyncConnection,
) -> PeriodEntryCount:
    """
    Return a count of non-voided draft entries in *period_id* for the caller.

    Counts all rows in payroll.PayrollDraftLines (both 'Day' and 'Period'
    linescope) that are not Void.  Used by the UI to show a data-loss warning
    before cancelling a period.

    Raises 403 / 404 via ``get_period_by_id`` if the caller lacks access.
    """
    # Access check reuses the existing get_period_by_id guard
    await get_period_by_id(company_id, user_id, period_id, db)

    row = await db.execute(
        text("""
            SELECT
                COUNT(DISTINCT driverid) AS driver_count,
                COUNT(*)                 AS entry_count
            FROM   payroll.payrolldraftlines
            WHERE  payrollperiodid = :pid
              AND  companyid       = :cid
              AND  status          != 'Void'
        """),
        {"pid": period_id, "cid": company_id},
    )
    r = row.mappings().first()
    driver_count = int(r["driver_count"] or 0)
    entry_count  = int(r["entry_count"]  or 0)

    return PeriodEntryCount(
        period_id=period_id,
        driver_count=driver_count,
        entry_count=entry_count,
        has_data=(entry_count > 0),
    )


# ---------------------------------------------------------------------------
# Status change
# ---------------------------------------------------------------------------

@_translate_submit_transaction_failures
async def change_period_status(
    company_id: int,
    user_id: int,
    period_id: int,
    change: PeriodStatusChange,
    db: AsyncConnection,
) -> PeriodSummary:
    # CP-4D: must be the first SQL on this request connection for submission.
    if change.status == "InReview":
        await _set_submit_transaction_isolation(db)

    # Load (and access-check) the existing period
    existing = await get_period_by_id(company_id, user_id, period_id, db)

    allowed = _VALID_TRANSITIONS.get(existing.status, set())
    if change.status not in allowed:
        if not allowed:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Period with status '{existing.status}' cannot be "
                    f"transitioned to any other status."
                ),
            )
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot transition from '{existing.status}' to '{change.status}'. "
                f"Allowed transitions: {sorted(allowed)}."
            ),
        )

    # Action-level permission gate: each transition requires a specific code.
    required_perm = _TRANSITION_PERMISSIONS.get((existing.status, change.status))
    if required_perm:
        await _check_permission(company_id, user_id, existing.branch_id, required_perm, db)

    # M16: Open → InReview pre-submission guards + auto-create PeriodApproval review item.
    #
    # The review item is created inside this same transaction so that if any guard
    # fails — or if the audit write raises — everything rolls back atomically.
    # The period never reaches InReview without a corresponding review item existing.
    if existing.status == "Open" and change.status == "InReview":
        # CP-1D: Acquire branch advisory lock before touching any workflow rows.
        # Same lock used by period creation (CP-1C) and resubmission — ensures all
        # per-branch workflow mutations are fully serialized.
        await _acquire_branch_workflow_lock(company_id, existing.branch_id, db)

        # CP-1D: Lock all active workflow rows in a deterministic order to prevent
        # deadlock when two concurrent submits race on the same branch.
        _wf_result = await db.execute(
            text("""
                SELECT payrollperiodid, status, startdate, enddate
                FROM   payroll.payrollperiods
                WHERE  companyid = :cid
                  AND  branchid  = :bid
                  AND  status    IN ('Draft', 'Open', 'InReview', 'Returned')
                ORDER  BY payrollperiodid ASC
                FOR UPDATE
            """),
            {"cid": company_id, "bid": existing.branch_id},
        )
        _wf_rows = list(_wf_result.mappings().all())

        _open_row = next(
            (r for r in _wf_rows if r["payrollperiodid"] == period_id and r["status"] == "Open"),
            None,
        )
        if _open_row is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Period is no longer Open — a concurrent transition may have already "
                    "moved it. Please refresh and try again."
                ),
            )

        # CP-1D: Returned backlog and anomaly check — fail closed on any Returned presence.
        # Older Returned (EndDate < Open.StartDate): unresolved backlog → block.
        # Overlapping, same-boundary, or newer Returned: chronologically anomalous → fail closed.
        for _ret in _wf_rows:
            if _ret["status"] == "Returned":
                if _ret["enddate"] < _open_row["startdate"]:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code":    "RETURNED_BACKLOG_BLOCKS_SUBMIT",
                            "message": (
                                "A Returned period (ending "
                                f"{_ret['enddate']}) exists before this period's start date "
                                f"({_open_row['startdate']}). Resolve the backlog Returned "
                                "period before submitting."
                            ),
                        },
                    )
                else:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code":    "WORKFLOW_SLOT_CONFLICT",
                            "message": (
                                "A Returned period (ending "
                                f"{_ret['enddate']}) chronologically overlaps or is newer than "
                                f"this Open period (starting {_open_row['startdate']}). "
                                "This represents an anomalous workflow state. Resolve the "
                                "Returned period before submitting."
                            ),
                        },
                    )

        # CP-1D: Draft promotion eligibility.
        # If a Draft period exists it must start immediately after this Open period
        # ends (Draft.StartDate == Open.EndDate + 1 day); otherwise block the submit.
        from datetime import timedelta as _timedelta
        _draft_rows = [r for r in _wf_rows if r["status"] == "Draft"]
        _eligible_draft = None
        if _draft_rows:
            _draft_row = _draft_rows[0]
            _expected_draft_start = _open_row["enddate"] + _timedelta(days=1)
            if _draft_row["startdate"] == _expected_draft_start:
                _eligible_draft = _draft_row
            else:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code":    "DRAFT_PROMOTION_CONFLICT",
                        "message": (
                            f"A Draft period exists (start {_draft_row['startdate']}) "
                            f"but does not immediately follow this period's end date "
                            f"({_open_row['enddate']}). Resolve the Draft period before "
                            "submitting."
                        ),
                    },
                )

        # CP-1B: Friendly InReview slot guard.  The partial unique index is the
        # concurrency authority; this check gives a clearer 409 message on the
        # common (non-race) path.
        await _check_inreview_slot_available(company_id, existing.branch_id, period_id, db)

        # CP-2D2: re-sync status payment lines from canonical PPDES state before
        # refresh so that rate changes (new/backdated driver rates for STATUS_PAY)
        # are reflected before submit guards run.
        await _refresh_status_payment_lines(
            period_id=period_id,
            company_id=company_id,
            branch_id=existing.branch_id,
            user_id=user_id,
            db=db,
        )

        # Auto-refresh: re-compute calculatedamount + needsmanagerreview for all
        # rate-dependent draft lines using the currently approved effective-dated
        # rates.  This ensures that backdated approved rates added since lines
        # were entered are reflected BEFORE the submit guards run, so the user
        # does not need to manually touch each line to trigger recalculation.
        await _refresh_draft_calculations(
            period_id=period_id,
            company_id=company_id,
            period_start_date=existing.start_date,
            db=db,
        )

        # Guard 1: empty period — refuse to submit a period with no payroll data.
        # CP-3A: count non-BONUS DraftLines + Active BonusEvents (BONUS DraftLines
        # are no longer used; bonus data lives in PayrollBonusEvents).
        empty_result = await db.execute(
            text("""
                SELECT (
                    SELECT COUNT(*) FROM payroll.payrolldraftlines
                    WHERE  payrollperiodid = :pid AND companyid = :cid
                      AND  status != 'Void' AND linetype != 'BONUS'
                ) + (
                    SELECT COUNT(*) FROM payroll.payrollbonusevents
                    WHERE  payrollperiodid = :pid AND companyid = :cid
                      AND  status = 'Active'
                ) AS total_lines
            """),
            {"pid": period_id, "cid": company_id},
        )
        if int(empty_result.scalar_one()) == 0:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Cannot submit an empty period for review. "
                    "Add at least one non-voided draft line before submitting."
                ),
            )

        # Guard 2: unresolved NeedsManagerReview lines.
        unresolved_result = await db.execute(
            text("""
                SELECT COUNT(*) AS cnt
                FROM   payroll.payrolldraftlines
                WHERE  payrollperiodid    = :pid
                  AND  companyid          = :cid
                  AND  status            != 'Void'
                  AND  needsmanagerreview  = TRUE
            """),
            {"pid": period_id, "cid": company_id},
        )
        unresolved_count = int(unresolved_result.scalar_one())
        if unresolved_count > 0:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Cannot submit: {unresolved_count} draft line(s) still require "
                    f"manager review (calculatedamount unresolved or manually flagged). "
                    f"Resolve all flagged lines before submitting for review."
                ),
            )

        # Guard 3: zero-calc unresolved lines (same three-clause check as finalization).
        # CP-0: EXISTS subqueries now match system items (companyid IS NULL) as well as
        # custom items (companyid = dl.companyid) so that new rows storing canonical
        # PayItemCodes ("HOURS") are caught alongside legacy rows ("Hours").
        zero_calc_result = await db.execute(
            text("""
                SELECT COUNT(*) AS cnt
                FROM   payroll.payrolldraftlines dl
                WHERE  dl.payrollperiodid    = :pid
                  AND  dl.companyid          = :cid
                  AND  dl.status            != 'Void'
                  AND  dl.needsmanagerreview  = FALSE
                  AND  dl.calculatedamount   IS NULL
                  AND  (
                    EXISTS (
                        SELECT 1 FROM payroll.payitems pi
                        WHERE  pi.payitemcode  = dl.linetype
                          AND  (pi.companyid IS NULL OR pi.companyid = dl.companyid)
                          AND  pi.ratebehavior IN (
                              'OrdinalTier', 'RangeBracket',
                              'RangeProgressive', 'Block'
                          )
                    )
                    OR
                    (
                        dl.rateamount IS NULL
                        AND EXISTS (
                            SELECT 1 FROM payroll.payitems pi
                            WHERE  pi.payitemcode  = dl.linetype
                              AND  (pi.companyid IS NULL OR pi.companyid = dl.companyid)
                              AND  pi.ratebehavior = 'PerUnit'
                        )
                    )
                    OR dl.linescope = 'Period'
                  )
            """),
            {"pid": period_id, "cid": company_id},
        )
        zero_calc_count = int(zero_calc_result.scalar_one())
        if zero_calc_count > 0:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Cannot submit: {zero_calc_count} rate-dependent draft line(s) have no "
                    f"resolved calculation amount. Fix or void these lines before submitting."
                ),
            )

        # Guard 4: duplicate Pending review item.
        # Block only if a Pending item exists — EditRequested/Rejected items are historical.
        dup_result = await db.execute(
            text("""
                SELECT reviewitemid FROM review.managerreviewitems
                WHERE  companyid    = :cid
                  AND  entityschema = 'payroll'
                  AND  entityname   = 'PayrollPeriods'
                  AND  entityid     = :eid
                  AND  requesttype  = 'PeriodApproval'
                  AND  status       = 'Pending'
                LIMIT 1
            """),
            {"cid": company_id, "eid": str(period_id)},
        )
        dup_row = dup_result.first()
        if dup_row is not None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"A Pending review item already exists for this period "
                    f"(review item ID {dup_row[0]}). Resolve it before re-submitting."
                ),
            )

        packet = await _build_live_calculation_packet(existing, company_id, db)
        snapshot_id = await _capture_calculation_snapshot(
            period=existing,
            company_id=company_id,
            user_id=user_id,
            packet=packet,
            db=db,
            context="Submit",
        )
        await capture_workflow_action_evidence(
            company_id=company_id,
            branch_id=existing.branch_id,
            period_id=period_id,
            snapshot_id=snapshot_id,
            action_code="SUBMITTED",
            user_id=user_id,
            required_permission_code="payroll.entry",
            db=db,
        )
        await link_unmapped_audit_evidence_to_snapshot(
            company_id=company_id, branch_id=existing.branch_id, period_id=period_id,
            snapshot_id=snapshot_id, db=db,
        )

        # Auto-create the PeriodApproval review item inside this transaction.
        # The review item is owned by the submitting user; AllowSelfApproval
        # applies when the same user later tries to approve it.
        #
        # SAIntegrityError guard: in a true race two concurrent requests can
        # both pass the duplicate-Pending SELECT check above before either
        # INSERT commits.  The partial unique index
        # ux_ReviewItems_OnePendingPeriodApproval then rejects the second
        # INSERT.  We catch that violation here and surface a clean 422
        # instead of letting a raw DB error propagate to the client.
        try:
            ri_result = await db.execute(
                text("""
                    INSERT INTO review.managerreviewitems
                        (companyid, branchid, requestedbyuserid,
                         requesttype, entityschema, entityname, entityid,
                         title, description, priority, status, payrollcalculationsnapshotid)
                    VALUES
                        (:cid, :bid, :uid,
                         'PeriodApproval', 'payroll', 'PayrollPeriods', :eid,
                         :title, :description, 'Normal', 'Pending', :snapshot_id)
                    RETURNING reviewitemid
                """),
                {
                    "cid":         company_id,
                    "bid":         existing.branch_id,
                    "uid":         user_id,
                    "eid":         str(period_id),
                    "snapshot_id": snapshot_id,
                    "title":       f"Payroll Period Approval: {existing.period_name} ({existing.branch_name})",
                    "description": (
                        f"Period {existing.period_name} ({existing.period_code}) has been "
                        f"submitted for approval. Date range: {existing.start_date} to "
                        f"{existing.end_date}."
                    ),
                },
            )
        except SAIntegrityError:
            raise HTTPException(
                status_code=422,
                detail="A pending review already exists for this payroll period.",
            )
        review_item_id: int = ri_result.scalar_one()

        # Write review item creation audit (inside the same transaction).
        await db.execute(
            text("""
                INSERT INTO audit.auditlog
                    (companyid, branchid, actoruserid, actioncode,
                     entityschema, entityname, entityid,
                     newvaluejson, reason, sourcetype)
                VALUES
                    (:cid, :bid, :uid, 'REVIEW_ITEM_CREATED',
                     'review', 'ManagerReviewItems', :riid,
                     :new_val, 'Review item submitted', 'Application')
            """),
            {
                "cid":     company_id,
                "bid":     existing.branch_id,
                "uid":     user_id,
                "riid":    str(review_item_id),
                "new_val": json.dumps({
                    "request_type": "PeriodApproval",
                    "period_id":    period_id,
                    "period_name":  existing.period_name,
                }),
            },
        )

    # Build the SET clause — also stamp the relevant timestamp column
    extra_set = ""
    extra_params: dict[str, Any] = {}

    if change.status == "Locked":
        extra_set = ", lockedbyuserid = :locker, lockedatutc = NOW()"
        extra_params["locker"] = user_id
    elif existing.status in ("Open", "Returned") and change.status == "InReview":
        # CP-1D: populate SubmittedAtUtc atomically with the status transition
        # (covers both initial submission and Returned→InReview resubmission).
        extra_set = ", submittedatutc = NOW()"

    notes_set = ", notes = :notes" if change.notes is not None else ""
    notes_params = {"notes": change.notes} if change.notes is not None else {}

    # CP-1A: InReview→Open and InReview→Cancelled are now blocked via _VALID_TRANSITIONS.
    # The CP-0C lock acquisition for those exits is removed because the transitions
    # are unreachable — change_period_status raises before this point when they're
    # attempted.  Returned→anything is also blocked via PATCH.

    # For Open→InReview use an atomic UPDATE WHERE status='Open' RETURNING to
    # prevent a double-submit race.  Two concurrent requests that both passed the
    # duplicate-Pending guard could both try to update; only the first succeeds.
    # All other transitions keep the simple UPDATE (no race risk: the status guard
    # above already held a FOR UPDATE lock via get_period_by_id).
    if existing.status == "Open" and change.status == "InReview":
        try:
            update_result = await db.execute(
                text(
                    f"UPDATE payroll.payrollperiods "
                    f"SET    status = :new_status{extra_set}{notes_set} "
                    f"WHERE  payrollperiodid = :period_id "
                    f"  AND  companyid       = :company_id "
                    f"  AND  branchid        = :branch_id "
                    f"  AND  status          = 'Open' "
                    f"RETURNING payrollperiodid"
                ),
                {
                    "new_status": change.status,
                    "period_id": period_id,
                    "company_id": company_id,
                    "branch_id": existing.branch_id,
                    **extra_params,
                    **notes_params,
                },
            )
        except SAIntegrityError as exc:
            if _is_inreview_slot_violation(exc):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "A concurrent submission created an InReview period for this "
                        "branch at the same time. Only one period may be in review per "
                        "branch. This submission has been rolled back."
                    ),
                )
            raise
        if update_result.first() is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Period is no longer Open — a concurrent submission may have "
                    "already moved it. Please refresh and try again."
                ),
            )

        # CP-1D: Atomically promote eligible adjacent Draft → Open in the same transaction.
        if _eligible_draft is not None:
            _draft_promote = await db.execute(
                text("""
                    UPDATE payroll.payrollperiods
                    SET    status = 'Open'
                    WHERE  payrollperiodid = :did
                      AND  companyid       = :cid
                      AND  branchid        = :bid
                      AND  status          = 'Draft'
                    RETURNING payrollperiodid
                """),
                {
                    "did": _eligible_draft["payrollperiodid"],
                    "cid": company_id,
                    "bid": existing.branch_id,
                },
            )
            if _draft_promote.first() is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Adjacent Draft period moved concurrently during submission. "
                        "Transaction rolled back. Please refresh and try again."
                    ),
                )
            await _write_period_status_audit(
                db,
                company_id=company_id,
                branch_id=existing.branch_id,
                user_id=user_id,
                period_id=_eligible_draft["payrollperiodid"],
                old_status="Draft",
                new_status="Open",
                extra={
                    "trigger":             "submit_promotion",
                    "submitted_period_id": period_id,
                },
            )
            # CP-2E: promoted period is now Open — generate and freeze its snapshot.
            await _regenerate_period_driver_eligibility_rows(
                _eligible_draft["payrollperiodid"],
                company_id,
                existing.branch_id,
                db,
                created_by_user_id=user_id,
                frozen_by_user_id=user_id,
            )
            # CP-2F: refresh status payment lines for Draft-era PPDES rows now that
            # the period is Open and rates are resolved.
            await _refresh_status_payment_lines(
                period_id=_eligible_draft["payrollperiodid"],
                company_id=company_id,
                branch_id=existing.branch_id,
                user_id=user_id,
                db=db,
            )
            # CP-2F: refresh daily calculations for Draft-era source lines now that
            # the period is Open and approved rates can be looked up.
            _draft_period_summary = await get_period_by_id(
                company_id, user_id, _eligible_draft["payrollperiodid"], db
            )
            await _refresh_draft_calculations(
                period_id=_eligible_draft["payrollperiodid"],
                company_id=company_id,
                period_start_date=_draft_period_summary.start_date,
                db=db,
            )
    else:
        # CP-0B: All non-Open→InReview transitions use an expected-status predicate
        # so that a stale request whose pre-flight read is now out of date cannot
        # silently overwrite a status that changed concurrently.
        #
        # The predicate is: payrollperiodid=:period_id AND companyid=:company_id AND
        # status=:old_status.  Zero RETURNING rows means another transaction already
        # moved this period; we surface a 409 Conflict rather than a silent no-op.
        update_result = await db.execute(
            text(
                f"UPDATE payroll.payrollperiods "
                f"SET    status = :new_status{extra_set}{notes_set} "
                f"WHERE  payrollperiodid = :period_id "
                f"  AND  companyid       = :company_id "
                f"  AND  branchid        = :branch_id "
                f"  AND  status          = :old_status "
                f"RETURNING payrollperiodid"
            ),
            {
                "new_status": change.status,
                "period_id": period_id,
                "company_id": company_id,
                "branch_id": existing.branch_id,
                "old_status": existing.status,
                **extra_params,
                **notes_params,
            },
        )
        if update_result.first() is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Period is no longer '{existing.status}' — "
                    "a concurrent transition may have already moved it. "
                    "Please refresh and try again."
                ),
            )

    # Audit: write inside the same transaction so a failure rolls back the UPDATE.
    await _write_period_status_audit(
        db,
        company_id=company_id,
        branch_id=existing.branch_id,
        user_id=user_id,
        period_id=period_id,
        old_status=existing.status,
        new_status=change.status,
    )

    return await get_period_by_id(company_id, user_id, period_id, db)


# ===========================================================================
# CP-1B: One-InReview-per-branch slot helpers
# ===========================================================================

def _is_inreview_slot_violation(exc: SAIntegrityError) -> bool:
    """Return True iff the IntegrityError is from the InReview-slot unique index."""
    orig = getattr(exc, "orig", None)
    if orig is not None:
        name = getattr(orig, "constraint_name", None)
        if name is not None:
            return name.lower() == "ux_payrollperiods_oneinreviewperbranch"
    return "ux_payrollperiods_oneinreviewperbranch" in str(exc).lower()


async def _check_inreview_slot_available(
    company_id: int,
    branch_id: int,
    current_period_id: int,
    db: AsyncConnection,
) -> None:
    """
    Friendly pre-write guard for the InReview slot.

    Raises HTTP 409 if another period for the same company/branch is already
    InReview.  current_period_id is excluded defensively (e.g., when the caller
    is a resubmit path and the period is Returned, not InReview).

    The partial unique index ux_payrollperiods_oneinreviewperbranch is the
    concurrency authority.  This check provides a friendlier error message on
    the non-race (sequential) path.
    """
    row = await db.execute(
        text("""
            SELECT payrollperiodid
            FROM   payroll.payrollperiods
            WHERE  companyid        = :cid
              AND  branchid         = :bid
              AND  status           = 'InReview'
              AND  payrollperiodid != :pid
            LIMIT 1
        """),
        {"cid": company_id, "bid": branch_id, "pid": current_period_id},
    )
    if row.first() is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "An InReview period already exists for this branch. "
                "Only one period may be in review at a time. "
                "Wait for the current review to complete before submitting another."
            ),
        )


# ===========================================================================
# CP-1A: Resubmission
# ===========================================================================

@_translate_submit_transaction_failures
async def resubmit_period(
    company_id: int,
    user_id: int,
    period_id: int,
    db: AsyncConnection,
) -> "PeriodSummary":
    """
    POST /payroll/periods/{period_id}/resubmissions

    Resubmit a Returned period for review.  Requires payroll.entry permission.
    Driver/ODA roles are blocked.

    Steps:
      1. Driver/ODA guard.
      2. Load the period (access + permission check).
      3. Acquire FOR UPDATE lock; verify status is still Returned.
      4. Run all Open→InReview submission guards (refresh, empty, NMR, zero-calc,
         duplicate-Pending).
      5. Create a new Pending PeriodApproval review item.
      6. UPDATE period: status='InReview', CurrentReturnReviewItemID=NULL
         WHERE status='Returned' RETURNING.
      7. Write audits (review item created + period status changed).
      8. Return refreshed PeriodSummary.
    """
    # CP-4D: must precede every resubmission database helper.
    await _set_submit_transaction_isolation(db)

    # ── Step 1: driver/ODA guard ─────────────────────────────────────────── #
    await _require_not_driver_role(company_id, user_id, db)
    own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)
    if own_driver_id is not None:
        raise HTTPException(
            status_code=403,
            detail="Payroll resubmission is not accessible to driver-role users.",
        )

    # ── Step 2: load period (access check) ───────────────────────────────── #
    existing = await get_period_by_id(company_id, user_id, period_id, db)

    # Friendly pre-flight (race-safe lock comes next).
    if existing.status != "Returned":
        raise HTTPException(
            status_code=422,
            detail=(
                f"Only Returned periods can be resubmitted "
                f"(current status: '{existing.status}'). "
                "Use POST /payroll/periods/{id}/resubmissions only on Returned periods."
            ),
        )

    # Permission gate: resubmission requires payroll.entry.
    await _check_permission(company_id, user_id, existing.branch_id, "payroll.entry", db)

    # ── Step 3: acquire branch advisory lock, then period row lock ───────── #
    # CP-1D: branch lock must come first (same order as submit and creation) to
    # prevent deadlock when concurrent submit + resubmit race on the same branch.
    await _acquire_branch_workflow_lock(company_id, existing.branch_id, db)

    lock_result = await db.execute(
        text(
            "SELECT status FROM payroll.payrollperiods "
            "WHERE payrollperiodid = :pid AND companyid = :cid "
            "FOR UPDATE"
        ),
        {"pid": period_id, "cid": company_id},
    )
    lock_row = lock_result.mappings().first()
    locked_status = lock_row["status"] if lock_row else "unknown"
    if locked_status != "Returned":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Period is no longer Returned (current status: '{locked_status}'). "
                "A concurrent resubmission or state change may have moved it. "
                "Please refresh and try again."
            ),
        )

    # ── Step 3b: CP-1B InReview slot guard ───────────────────────────────── #
    await _check_inreview_slot_available(company_id, existing.branch_id, period_id, db)

    # ── Step 4: run submission guards (same as Open→InReview) ────────────── #

    # Keep Returned resubmission in parity with first submission. The stored
    # projection remains compatibility-only; the captured packet uses live PPDES.
    await _refresh_status_payment_lines(
        period_id=period_id,
        company_id=company_id,
        branch_id=existing.branch_id,
        user_id=user_id,
        db=db,
    )

    # Refresh draft calculations so the guards see current rates.
    await _refresh_draft_calculations(
        period_id=period_id,
        company_id=company_id,
        period_start_date=existing.start_date,
        db=db,
    )

    # Guard 1: empty period.
    # CP-3A: count non-BONUS DraftLines + Active BonusEvents.
    empty_result = await db.execute(
        text("""
            SELECT (
                SELECT COUNT(*) FROM payroll.payrolldraftlines
                WHERE  payrollperiodid = :pid AND companyid = :cid
                  AND  status != 'Void' AND linetype != 'BONUS'
            ) + (
                SELECT COUNT(*) FROM payroll.payrollbonusevents
                WHERE  payrollperiodid = :pid AND companyid = :cid
                  AND  status = 'Active'
            ) AS total_lines
        """),
        {"pid": period_id, "cid": company_id},
    )
    if int(empty_result.scalar_one()) == 0:
        raise HTTPException(
            status_code=422,
            detail=(
                "Cannot resubmit an empty period for review. "
                "Add at least one non-voided draft line before resubmitting."
            ),
        )

    # Guard 2: unresolved NeedsManagerReview lines.
    unresolved_result = await db.execute(
        text("""
            SELECT COUNT(*) AS cnt
            FROM   payroll.payrolldraftlines
            WHERE  payrollperiodid    = :pid
              AND  companyid          = :cid
              AND  status            != 'Void'
              AND  needsmanagerreview  = TRUE
        """),
        {"pid": period_id, "cid": company_id},
    )
    unresolved_count = int(unresolved_result.scalar_one())
    if unresolved_count > 0:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot resubmit: {unresolved_count} draft line(s) still require "
                f"manager review (calculatedamount unresolved or manually flagged). "
                f"Resolve all flagged lines before resubmitting."
            ),
        )

    # Guard 3: zero-calc unresolved lines.
    zero_calc_result = await db.execute(
        text("""
            SELECT COUNT(*) AS cnt
            FROM   payroll.payrolldraftlines dl
            WHERE  dl.payrollperiodid    = :pid
              AND  dl.companyid          = :cid
              AND  dl.status            != 'Void'
              AND  dl.needsmanagerreview  = FALSE
              AND  dl.calculatedamount   IS NULL
              AND  (
                EXISTS (
                    SELECT 1 FROM payroll.payitems pi
                    WHERE  pi.payitemcode  = dl.linetype
                      AND  (pi.companyid IS NULL OR pi.companyid = dl.companyid)
                      AND  pi.ratebehavior IN (
                          'OrdinalTier', 'RangeBracket',
                          'RangeProgressive', 'Block'
                      )
                )
                OR
                (
                    dl.rateamount IS NULL
                    AND EXISTS (
                        SELECT 1 FROM payroll.payitems pi
                        WHERE  pi.payitemcode  = dl.linetype
                          AND  (pi.companyid IS NULL OR pi.companyid = dl.companyid)
                          AND  pi.ratebehavior = 'PerUnit'
                    )
                )
                OR dl.linescope = 'Period'
              )
        """),
        {"pid": period_id, "cid": company_id},
    )
    zero_calc_count = int(zero_calc_result.scalar_one())
    if zero_calc_count > 0:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot resubmit: {zero_calc_count} rate-dependent draft line(s) have no "
                f"resolved calculation amount. Fix or void these lines before resubmitting."
            ),
        )

    # Guard 4: duplicate Pending review item.
    dup_result = await db.execute(
        text("""
            SELECT reviewitemid FROM review.managerreviewitems
            WHERE  companyid    = :cid
              AND  entityschema = 'payroll'
              AND  entityname   = 'PayrollPeriods'
              AND  entityid     = :eid
              AND  requesttype  = 'PeriodApproval'
              AND  status       = 'Pending'
            LIMIT 1
        """),
        {"cid": company_id, "eid": str(period_id)},
    )
    dup_row = dup_result.first()
    if dup_row is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"A Pending review item already exists for this period "
                f"(review item ID {dup_row[0]}). Resolve it before resubmitting."
            ),
        )

    packet = await _build_live_calculation_packet(existing, company_id, db)
    snapshot_id = await _capture_calculation_snapshot(
        period=existing,
        company_id=company_id,
        user_id=user_id,
        packet=packet,
        db=db,
        context="Resubmit",
    )
    await capture_workflow_action_evidence(
        company_id=company_id,
        branch_id=existing.branch_id,
        period_id=period_id,
        snapshot_id=snapshot_id,
        action_code="RESUBMITTED",
        user_id=user_id,
        required_permission_code="payroll.entry",
        db=db,
    )
    await link_unmapped_audit_evidence_to_snapshot(
        company_id=company_id, branch_id=existing.branch_id, period_id=period_id,
        snapshot_id=snapshot_id, db=db,
    )

    # ── Step 5: create new Pending PeriodApproval review item ────────────── #
    try:
        ri_result = await db.execute(
            text("""
                INSERT INTO review.managerreviewitems
                    (companyid, branchid, requestedbyuserid,
                     requesttype, entityschema, entityname, entityid,
                     title, description, priority, status, payrollcalculationsnapshotid)
                VALUES
                    (:cid, :bid, :uid,
                     'PeriodApproval', 'payroll', 'PayrollPeriods', :eid,
                     :title, :description, 'Normal', 'Pending', :snapshot_id)
                RETURNING reviewitemid
            """),
            {
                "cid":         company_id,
                "bid":         existing.branch_id,
                "uid":         user_id,
                "eid":         str(period_id),
                "snapshot_id": snapshot_id,
                "title":       f"Payroll Period Resubmission: {existing.period_name} ({existing.branch_name})",
                "description": (
                    f"Period {existing.period_name} ({existing.period_code}) has been "
                    f"resubmitted for approval after correction. Date range: "
                    f"{existing.start_date} to {existing.end_date}."
                ),
            },
        )
    except SAIntegrityError:
        raise HTTPException(
            status_code=422,
            detail="A pending review already exists for this payroll period.",
        )
    new_review_item_id: int = ri_result.scalar_one()

    # Write review item creation audit.
    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid,
                 newvaluejson, reason, sourcetype)
            VALUES
                (:cid, :bid, :uid, 'REVIEW_ITEM_CREATED',
                 'review', 'ManagerReviewItems', :riid,
                 :new_val, 'Review item submitted', 'Application')
        """),
        {
            "cid":     company_id,
            "bid":     existing.branch_id,
            "uid":     user_id,
            "riid":    str(new_review_item_id),
            "new_val": json.dumps({
                "request_type": "PeriodApproval",
                "period_id":    period_id,
                "period_name":  existing.period_name,
                "resubmission": True,
            }),
        },
    )

    # ── Step 6: atomic transition Returned→InReview, clear pointer ────────── #
    # CP-1D: Include branchid predicate and populate SubmittedAtUtc atomically.
    try:
        update_result = await db.execute(
            text("""
                UPDATE payroll.payrollperiods
                SET    status                    = 'InReview',
                       currentreturnreviewitemid = NULL,
                       submittedatutc            = NOW()
                WHERE  payrollperiodid = :pid
                  AND  companyid       = :cid
                  AND  branchid        = :bid
                  AND  status          = 'Returned'
                RETURNING payrollperiodid
            """),
            {"pid": period_id, "cid": company_id, "bid": existing.branch_id},
        )
    except SAIntegrityError as exc:
        if _is_inreview_slot_violation(exc):
            raise HTTPException(
                status_code=409,
                detail=(
                    "A concurrent resubmission created an InReview period for this "
                    "branch at the same time. Only one period may be in review per "
                    "branch. This resubmission has been rolled back."
                ),
            )
        raise
    if update_result.first() is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Period is no longer Returned — a concurrent resubmission may have "
                "already moved it. Please refresh and try again."
            ),
        )

    # ── Step 7: period status audit ──────────────────────────────────────── #
    await _write_period_status_audit(
        db,
        company_id=company_id,
        branch_id=existing.branch_id,
        user_id=user_id,
        period_id=period_id,
        old_status="Returned",
        new_status="InReview",
    )

    return await get_period_by_id(company_id, user_id, period_id, db)


# ===========================================================================
# Draft lines — entry service
# ===========================================================================

# CP-0A: Periods frozen to all source mutations.
# InReview is now included: once a period enters review it must not be
# mutated.  Open is the only editable status.
_WRITE_BLOCKED_STATUSES = {"Draft", "InReview", "Approved", "Locked", "Archived", "Cancelled"}

async def _lock_period_for_mutation(
    period_id: int,
    company_id: int,
    db: AsyncConnection,
) -> None:
    """
    CP-0A/CP-1A: Lock the PayrollPeriods row FOR UPDATE and verify it is still
    in an editable status (Open or Returned).

    Must be called immediately before the first DML write in every source
    mutation path.  The lock ensures that a concurrent status transition
    cannot commit after this mutation has already passed the upfront status
    check but before it writes.

    Raises HTTP 409 Conflict when the current status is not Open or Returned.
    """
    result = await db.execute(
        text(
            "SELECT status FROM payroll.payrollperiods "
            "WHERE payrollperiodid = :pid AND companyid = :cid "
            "FOR UPDATE"
        ),
        {"pid": period_id, "cid": company_id},
    )
    row = result.mappings().first()
    current_status = row["status"] if row else "unknown"
    # CP-2F: Draft (Prepared) is also editable for operational source-entry paths.
    # SOURCE_ENTRY_STATUSES = {"Draft", "Open", "Returned"}
    if current_status not in SOURCE_ENTRY_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Period is no longer editable (current status: '{current_status}'). "
                "Only Open, Returned, or Prepared (Draft) periods accept source mutations. "
                "The mutation was rejected to preserve payroll data integrity."
            ),
        )


async def _lock_pay_item_for_source_write(
    line_type: str,
    company_id: int,
    db: AsyncConnection,
    *,
    period_id: int | None = None,
) -> None:
    """
    CP-0A: Acquire a FOR UPDATE row lock on the company-owned PayItems catalog
    row before inserting a DraftLine reference.

    Serializes with the physical-delete path's own FOR UPDATE on the same row,
    preventing the first-reference race:
      1. Deletion reads zero usage (no DraftLines yet).
      2. Source creation validates the item via a plain read (no lock).
      3. Source creation inserts the first DraftLine reference.
      4. Deletion physically removes the catalog row → DraftLine orphaned.

    Call order: acquire this lock BEFORE _lock_period_for_mutation so both
    paths lock PayItem then Period (same order as the deletion path), avoiding
    deadlock.

    System items (companyid IS NULL) cannot be physically deleted; no lock
    needed.  Informational-only items (DailyStatus, DailyNote) have no catalog
    row and are skipped.

    CP-2C: if period_id is provided and the item appears in that period's
    PayrollPeriodPayItems snapshot with IsActiveInPeriod=TRUE, the live
    status check is bypassed.  The FK lock is still acquired so a concurrent
    physical-delete (which the snapshot FK blocks anyway) is serialised.
    Physical deletion of an item with snapshot rows is already prevented by
    the FK constraint on PayrollPeriodPayItems; this code path is reached only
    when a concurrent retirement races with the write.

    Raises HTTP 422 if the custom row is absent when the lock is attempted,
    meaning a concurrent deletion committed between validation and this call.
    """
    if line_type in _INFORMATIONAL_ONLY:
        return  # no PayItems catalog row

    # CP-2C: snapshot authorisation — item active in period snapshot remains
    # usable even if live PayItems.Status was later changed to Retired.
    snapshot_authorised = False
    if period_id is not None:
        snap_auth = await db.execute(
            text("""
                SELECT 1 FROM payroll.payrollperiodpayitems
                WHERE payrollperiodid = :pid
                  AND payitemcode     = :code
                  AND isactiveinperiod = TRUE
                LIMIT 1
            """),
            {"pid": period_id, "code": line_type},
        )
        if snap_auth.first() is not None:
            snapshot_authorised = True

    # Try to lock the custom (company-specific) row and read its status.
    # Selecting status here means the retirement race is caught: if a concurrent
    # deletion/retirement committed while this call was waiting for the lock, we
    # see the committed 'Retired' status and reject rather than inserting a
    # DraftLine for a no-longer-Active item.
    result = await db.execute(
        text("""
            SELECT payitemid, status FROM payroll.payitems
            WHERE  payitemcode = :code AND companyid = :cid
            FOR UPDATE
        """),
        {"code": line_type, "cid": company_id},
    )
    row = result.mappings().first()
    if row is not None:
        if row["status"] != "Active" and not snapshot_authorised:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Pay item '{line_type}' is no longer active "
                    f"(status: '{row['status']}'). "
                    "Refresh and try again."
                ),
            )
        # Custom row locked — held until this transaction commits.
        return

    # No custom row.  Check whether a system row exists (system items can't
    # be deleted, so no lock is needed for them).
    sys_result = await db.execute(
        text("""
            SELECT payitemid FROM payroll.payitems
            WHERE  payitemcode = :code AND companyid IS NULL
        """),
        {"code": line_type},
    )
    if sys_result.mappings().first() is not None:
        return  # system item — safe without a lock

    # Neither custom nor system — item was concurrently deleted between the
    # initial _validate_line_type read and this lock attempt.
    raise HTTPException(
        status_code=422,
        detail=(
            f"Pay item '{line_type}' is no longer available. "
            "It may have been deleted concurrently. Refresh and try again."
        ),
    )


_LINE_SELECT = """
    SELECT
        dl.draftlineid,
        dl.payrollperiodid,
        dl.branchid,
        dl.driverid,
        e.fullname         AS drivername,
        dl.workdate,
        dl.linetype,
        dl.linescope,
        dl.quantity,
        dl.rateamount,
        dl.calculatedamount,
        dl.sourcetype,
        dl.status,
        dl.needsmanagerreview,
        dl.notes,
        dl.addedbyuserid,
        dl.addedatutc
    FROM   payroll.payrolldraftlines dl
    JOIN   core.drivers              d  ON d.driverid   = dl.driverid
    JOIN   core.employees            e  ON e.employeeid = d.employeeid
"""


def _line_row_to_summary(r: Any) -> DraftLineSummary:
    return DraftLineSummary(
        draft_line_id=r["draftlineid"],
        period_id=r["payrollperiodid"],
        branch_id=r["branchid"],
        driver_id=r["driverid"],
        driver_name=r["drivername"],
        work_date=r["workdate"],
        line_type=r["linetype"],
        line_scope=r["linescope"],
        quantity=r["quantity"],
        rate_amount=r["rateamount"],
        calculated_amount=r["calculatedamount"],
        source_type=r["sourcetype"],
        status=r["status"],
        needs_manager_review=r["needsmanagerreview"],
        notes=r["notes"],
        added_by_user_id=r["addedbyuserid"],
        added_at_utc=r["addedatutc"],
    )


# ---------------------------------------------------------------------------
# List lines
# ---------------------------------------------------------------------------

async def get_period_lines(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    driver_id: int | None = None,
    work_date: date | None = None,
    line_status: str | None = None,
) -> list[DraftLineSummary]:
    """Return draft lines for a period (access-checked via the period lookup)."""
    period = await get_period_by_id(company_id, user_id, period_id, db)

    conditions = [
        "dl.payrollperiodid  = :period_id",
        "dl.companyid        = :company_id",
        # M14: exclude Period Pay lines from the daily lines list.
        # Period Pay lines have linescope='Period' and are returned by
        # get_period_pay_lines() instead.  Using the explicit LineScope column
        # (not WorkDate IS NOT NULL) because daily lines can also have NULL WorkDate.
        "dl.linescope        = 'Daily'",
    ]
    params: dict[str, Any] = {
        "period_id": period_id,
        "company_id": company_id,
    }

    # CP-2F: for Draft periods, exclude System-sourced lines, STATUS_PAYMENT, and
    # ADJUSTMENT / MINIMUM / MAXIMUM pay items — these are financial and must not be
    # visible until the period is promoted to Open.
    if period.status == "Draft":
        conditions.append("dl.sourcetype != 'System'")
        conditions.append(
            "dl.linetype NOT IN ('STATUS_PAYMENT', 'ADJUSTMENT', 'MINIMUM', 'MAXIMUM',"
            " 'SYS_MIN_TOPUP', 'SYS_MAX_CAP')"
        )

    if driver_id is not None:
        conditions.append("dl.driverid = :driver_id")
        params["driver_id"] = driver_id

    if work_date is not None:
        conditions.append("dl.workdate = :work_date")
        params["work_date"] = work_date

    if line_status is not None:
        conditions.append("dl.status = :line_status")
        params["line_status"] = line_status

    where = " AND ".join(conditions)
    result = await db.execute(
        text(f"{_LINE_SELECT} WHERE {where} ORDER BY dl.workdate, dl.driverid, dl.linetype"),
        params,
    )
    rows = [_line_row_to_summary(r) for r in result.mappings().all()]

    # CP-2F: sanitize money fields for Draft so callers never see stale rates/amounts.
    if period.status == "Draft":
        sanitized = []
        for ln in rows:
            ln = ln.model_copy(update={
                "rate_amount": None,
                "calculated_amount": None,
                "needs_manager_review": False,
            })
            sanitized.append(ln)
        return sanitized

    return rows


# ---------------------------------------------------------------------------
# Summary (aggregated per driver × line_type)
# ---------------------------------------------------------------------------

async def get_period_draft_summary(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> list[DriverPeriodSummary]:
    """
    Aggregated totals per driver × line_type for one period.

    Uses a direct query (not vw_PayrollDraftSummary) so that Void lines
    are explicitly excluded from the aggregation.
    """
    period = await get_period_by_id(company_id, user_id, period_id, db)

    # CP-2F: Draft periods have no financial summary — block to avoid returning
    # zero totals that could mislead callers into thinking the period is empty.
    if period.status == "Draft":
        raise HTTPException(
            status_code=422,
            detail="Lines summary is not available for Prepared (Draft) periods.",
        )

    result = await db.execute(
        text("""
            SELECT
                dl.driverid,
                e.fullname                                                              AS drivername,
                dl.payrollperiodid,
                :period_name                                                            AS periodname,
                dl.linetype,
                SUM(dl.quantity)                                                        AS totalquantity,
                SUM(COALESCE(dl.calculatedamount, 0))                                  AS totalcalculatedamount,
                COUNT(*)                                                                AS linecount,
                SUM(CASE WHEN dl.status IN ('NeedsReview', 'Rejected')
                          OR dl.needsmanagerreview THEN 1 ELSE 0 END)                  AS linesneedingattention
            FROM   payroll.payrolldraftlines dl
            JOIN   core.drivers              d  ON d.driverid   = dl.driverid
            JOIN   core.employees            e  ON e.employeeid = d.employeeid
            WHERE  dl.payrollperiodid = :period_id
              AND  dl.companyid       = :company_id
              AND  dl.status         != 'Void'
              AND  dl.linescope       = 'Daily'
            GROUP  BY dl.driverid, e.fullname, dl.payrollperiodid, dl.linetype
            ORDER  BY e.fullname, dl.linetype
        """),
        {
            "period_id":   period_id,
            "company_id":  company_id,
            "period_name": period.period_name,
        },
    )
    return [
        DriverPeriodSummary(
            driver_id=r["driverid"],
            driver_name=r["drivername"],
            period_id=r["payrollperiodid"],
            period_name=r["periodname"],
            line_type=r["linetype"],
            total_quantity=r["totalquantity"],
            total_calculated_amount=r["totalcalculatedamount"],
            line_count=r["linecount"],
            lines_needing_attention=r["linesneedingattention"],
        )
        for r in result.mappings().all()
    ]


# ===========================================================================
# M13a: PayItem-driven line-type validation
# M13b: PerUnit / EnteredAmount calculated-amount engine
# ===========================================================================
#
# TRANSITIONAL DESIGN NOTE (target cleanup: M14)
# ─────────────────────────────────────────────
# System pay items were established before the PayItems catalog.
# Their line-type strings (e.g. "Hours", "Miles") are stored verbatim in
# PayrollDraftLines.LineType and differ from their PayItemCodes ("HOURS", "MILES").
#
# _SYSTEM_LINE_TYPES    — fast-path set; these bypass the full DB lookup.
# _SYSTEM_LINE_TYPE_INFO — rate_behavior, rate_code, and item_scope for each
#                          system string.
#
# Custom pay items (CompanyID IS NOT NULL) use their PayItemCode directly as
# LineType — no alias.  They are validated via the DB slow path.
#
# BranchPayItemConfig IS checked for system items (Fix 2, M13b): the fast path
# now validates branch activation via a DB query for items that have a DB counterpart
# (DailyStatus and DailyNote have no DB counterpart and are accepted unconditionally).
#
# PayItemRateTypeMap rows for system PerUnit items are seeded by migration 0008.
# The fast path queries PayItemRateTypeMap for rate_code (Fix 4, M13b) and falls
# back to the hardcoded _SYSTEM_LINE_TYPE_INFO value ONLY as a backward-compat
# measure during zero-downtime deploys.  On a properly migrated DB (head >= 0008)
# the fallback should never trigger.
#
# ─────────────────────────────────────────────


class _LineTypeInfo(NamedTuple):
    """Validation result for a draft line's line_type value."""
    rate_behavior: str         # 'PerUnit', 'EnteredAmount', 'Fixed', 'None', etc.
    rate_code: str | None      # RateTypes.RateCode for PerUnit; None for other behaviors
    item_scope: str = "Daily"  # 'Daily' | 'Period' — Period items are blocked from daily entry


class _CalcResult(NamedTuple):
    """
    Return value of _compute_calculated_amount.

    Phase 3B adds source fields so callers can store them in PayrollFinalLines:
      driver_rate_id       -- DriverRates.DriverRateID used (PerUnit / Tiered / Block)
      rate_type_id         -- RateTypes.RateTypeID of the matched rate
      resolved_rate_amount -- dr.Amount (PerUnit only; NULL for tiered/block)

    Fields default to None so existing callers that only unpack (calc, nmr) are
    unaffected as long as they use positional unpacking of the first two fields
    or attribute access.
    """
    calculated_amount:    Decimal | None
    needs_manager_review: bool
    driver_rate_id:       int | None = None
    rate_type_id:         int | None = None
    resolved_rate_amount: Decimal | None = None
    rate_behavior:        str | None = None


# Maps legacy system line-type strings to their calculation metadata.
# Any string NOT in this dict goes through the custom-item DB slow path.
_SYSTEM_LINE_TYPE_INFO: dict[str, _LineTypeInfo] = {
    "Hours":       _LineTypeInfo("PerUnit",       "HOURLY"),
    "Miles":       _LineTypeInfo("PerUnit",       "MILEAGE"),
    "Loads":       _LineTypeInfo("PerUnit",       "LOAD"),
    "Overnight":   _LineTypeInfo("Fixed",         None),   # fixed per-night; PayItemSettings (M13c+)
    "Wait":        _LineTypeInfo("PerUnit",       "WAIT"),
    "Pallets":     _LineTypeInfo("PerUnit",       "PALLET"),
    "Silos":       _LineTypeInfo("PerUnit",       "SILO"),
    "DailyStatus": _LineTypeInfo("None",          None),   # informational
    "DailyNote":   _LineTypeInfo("None",          None),   # informational
    "Bonus":       _LineTypeInfo("Fixed",         None,  "Period"),  # Period item; blocked from daily entry
    "Adjustment":  _LineTypeInfo("Fixed",         None,  "Period"),  # Period item; blocked from daily entry
}

# Maps each legacy line-type string to its PayItemCode in payroll.PayItems
# (where CompanyID IS NULL).  None means no DB counterpart exists for that
# legacy string (informational items only — no branch-activation check needed).
_SYSTEM_ITEM_DB_CODES: dict[str, str | None] = {
    "Hours":       "HOURS",
    "Miles":       "MILES",
    "Loads":       "LOADS",
    "Overnight":   "OVERNIGHT",
    "Wait":        "WAIT_TIME",
    "Pallets":     "PALLETS",
    "Silos":       "SILOS",
    "DailyStatus": None,      # no DB counterpart; informational only
    "DailyNote":   None,      # no DB counterpart; informational only
    "Bonus":       "BONUS",
    "Adjustment":  "ADJUSTMENT",
}

# CP-0: Maps legacy display-name line-type strings to their canonical PayItemCode.
# Callers may send either form; the service normalises to canonical before validation
# and stores the canonical code in PayrollDraftLines.LineType for new rows.
# Historical rows created before CP-0 still contain legacy strings — reads return
# them verbatim.  Validation re-normalises on update so old rows validate correctly.
_LEGACY_TO_CANONICAL: dict[str, str] = {
    legacy: code
    for legacy, code in _SYSTEM_ITEM_DB_CODES.items()
    if code is not None   # DailyStatus / DailyNote have no canonical PayItemCode
}
# e.g. {"Hours": "HOURS", "Miles": "MILES", ..., "Silos": "SILOS", "Bonus": "BONUS", ...}

# Pure-informational items that have no PayItems catalog counterpart.
# Accepted unconditionally (no scope, branch, or rate checks).
_INFORMATIONAL_ONLY: frozenset[str] = frozenset({"DailyStatus", "DailyNote"})

_SYSTEM_LINE_TYPES: frozenset[str] = frozenset(_SYSTEM_LINE_TYPE_INFO)

# M13c: rate behaviors that use DriverRateTiers for calculation.
_TIERED_BEHAVIORS: frozenset[str] = frozenset({"OrdinalTier", "RangeBracket", "RangeProgressive"})
# Range behaviors (RangeBracket + RangeProgressive) share the same tier input/storage.
_RANGE_BEHAVIORS: frozenset[str] = frozenset({"RangeBracket", "RangeProgressive"})
# All behaviors that need a DriverRate (including Block which doesn't use tiers).
_RATE_USING_BEHAVIORS: frozenset[str] = frozenset(
    {"PerUnit", "OrdinalTier", "RangeBracket", "RangeProgressive", "Block"}
)
# Behaviors where calculatedamount is strictly required for approval/finalization.
# rateamount is NOT a valid fallback for these behaviors — tier/block lines must have
# a resolved calculatedamount from the calculation engine.  PerUnit is intentionally
# excluded because qty × rateamount is a safe and correct finalization path for it.
_CALC_REQUIRED_BEHAVIORS: frozenset[str] = frozenset(
    {"OrdinalTier", "RangeBracket", "RangeProgressive", "Block"}
)

# M14: RateBehaviors allowed for manual Period Pay entry.
# EnteredAmount — custom Period items (user enters dollar amount directly).
# Fixed         — system BONUS / ADJUSTMENT (same semantics: direct amount entry).
# Calculated (GUARANTEED_MINIMUM) and all Daily behaviors are blocked in M14.
_PERIOD_PAY_ALLOWED_BEHAVIORS: frozenset[str] = frozenset({"EnteredAmount", "Fixed"})

# System period items that can be entered via the Period Pay endpoint (M14).
# Both legacy display names ("Bonus") and canonical PayItemCodes ("BONUS") are
# accepted — both map to the same canonical code for DB lookup and storage.
_SYSTEM_PERIOD_ALLOWED: dict[str, str] = {
    "Bonus":       "BONUS",
    "Adjustment":  "ADJUSTMENT",
    "BONUS":       "BONUS",       # canonical alias
    "ADJUSTMENT":  "ADJUSTMENT",  # canonical alias
}
_SYSTEM_PERIOD_ALLOWED_TYPES: frozenset[str] = frozenset(_SYSTEM_PERIOD_ALLOWED)

# System period items blocked in M14 (need automated pay-rule engine, M15+).
# Both display-name and DB-code forms accepted so callers get a clear message
# regardless of which form they use.
# System lines written ONLY by the finalization engine — never accepted from user endpoints.
_SYSTEM_FINALIZATION_ONLY: frozenset[str] = frozenset({"SYS_MIN_TOPUP", "SYS_MAX_CAP"})

_SYSTEM_PERIOD_BLOCKED: frozenset[str] = frozenset({
    "GuaranteedMinimum", "GUARANTEED_MINIMUM",
    "SYS_MIN_TOPUP", "SYS_MAX_CAP",
})


async def _validate_line_type(
    line_type: str,
    branch_id: int,
    company_id: int,
    as_of_date: date,
    db: AsyncConnection,
    period_id: int | None = None,
) -> _LineTypeInfo:
    """
    Validate a daily draft line's line_type and return its calculation metadata.

    CP-0 unified DB path:
        Accepts both canonical PayItemCodes (e.g. "HOURS") and legacy display
        strings (e.g. "Hours") — the caller normalises via _LEGACY_TO_CANONICAL
        before calling, so this function always receives a canonical code or an
        informational-only string (DailyStatus / DailyNote).

        Validates against payroll.PayItems for BOTH system rows
        (companyid IS NULL) and company-custom rows (companyid = company_id).
        Company-specific takes precedence when both happen to exist.

        Checks (in order):
          1. Finalization-only codes rejected.
          2. Informational-only items (DailyStatus, DailyNote) accepted without
             any DB checks — they have no PayItems catalog row.
          3. CP-2C: if period_id is supplied and PayrollPeriodPayItems rows exist,
             validate against the snapshot instead of live BranchPayItemConfig.
          4. DB lookup — both system and custom.
          5. Retired / Period-scope guards.
          6. Branch activation (BranchPayItemConfig LEFT JOIN + COALESCE fallback).
          7. Rate-type mapping (from PayItemRateTypeMap).

    Raises HTTP 422 for any invalid condition.
    """
    # ── 1. Finalization-only system codes ─────────────────────────────── #
    if line_type in _SYSTEM_FINALIZATION_ONLY:
        raise HTTPException(
            status_code=422,
            detail=f"'{line_type}' is a system finalization line and cannot be entered manually.",
        )

    # ── 2. Informational-only items (no catalog row) ───────────────────── #
    if line_type in _INFORMATIONAL_ONLY:
        # DailyStatus / DailyNote — no monetary value, no branch check needed.
        return _LineTypeInfo("None", None, "Daily")

    # ── 3. CP-2C: snapshot-first validation ───────────────────────────── #
    # When period_id is provided and PayrollPeriodPayItems rows exist, validate
    # against the frozen snapshot rather than live BranchPayItemConfig.
    if period_id is not None:
        snap_result = await db.execute(
            text("""
                SELECT payitemcode, ratebehavior, isactiveinperiod, itemscope
                FROM payroll.payrollperiodpayitems
                WHERE payrollperiodid = :pid
                  AND companyid       = :cid
                  AND payitemcode     = :code
            """),
            {"pid": period_id, "cid": company_id, "code": line_type},
        )
        snap_row = snap_result.mappings().first()

        # Only use snapshot path if the period actually has snapshot rows.
        has_any_snap = (await db.execute(
            text("SELECT 1 FROM payroll.payrollperiodpayitems WHERE payrollperiodid = :pid LIMIT 1"),
            {"pid": period_id},
        )).first()

        if has_any_snap is not None:
            if snap_row is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"'{line_type}' is not in the pay-item snapshot for this period. "
                        "The item was not active or did not exist when the period was created."
                    ),
                )
            if snap_row["itemscope"] == "Period":
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"'{line_type}' is a Period-scope pay item and cannot be entered "
                        "as a daily draft line. Use the Period Pay endpoint instead."
                    ),
                )
            if not bool(snap_row["isactiveinperiod"]):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Pay item '{line_type}' was not active for this branch when the "
                        "period was created and cannot be used for new entries."
                    ),
                )
            # Snapshot validates scope and activation; still need rate-type mapping.
            rt_result = await db.execute(
                text("""
                    SELECT rt.ratecode
                    FROM   payroll.payitemratetypemap  pirtm
                    JOIN   payroll.payitems            pi
                           ON pi.payitemid = pirtm.payitemid
                    JOIN   payroll.ratetypes           rt
                           ON rt.ratetypeid = pirtm.ratetypeid
                    WHERE  pi.payitemcode = :code
                      AND  (pi.companyid IS NULL OR pi.companyid = :cid)
                      AND  pirtm.status  = 'Active'
                      AND  rt.isactive   = TRUE
                    ORDER BY pirtm.isprimary DESC
                    LIMIT 1
                """),
                {"code": line_type, "cid": company_id},
            )
            rt_row = rt_result.mappings().first()
            return _LineTypeInfo(
                rate_behavior=snap_row["ratebehavior"],
                rate_code=rt_row["ratecode"] if rt_row else None,
            )

    # ── 4. Unified DB lookup ───────────────────────────────────────────── #
    # Covers system items (companyid IS NULL) AND custom items (companyid = :cid).
    # When both exist for the same PayItemCode (should not happen in practice)
    # the company-specific row takes precedence (ORDER BY companyid NULLS LAST).
    pi_result = await db.execute(
        text("""
            SELECT pi.payitemid, pi.itemscope, pi.ratebehavior,
                   pi.isdefaultbranchactive, pi.status
            FROM   payroll.payitems pi
            WHERE  pi.payitemcode = :code
              AND  (pi.companyid IS NULL OR pi.companyid = :cid)
            ORDER BY pi.companyid NULLS LAST
            LIMIT 1
        """),
        {"code": line_type, "cid": company_id},
    )
    pi_row = pi_result.mappings().first()

    if pi_row is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{line_type}' is not a recognised pay item for this company. "
                "Use a PayItemCode from the active pay items list, or a legacy "
                "system line-type string (Hours, Miles, Loads, …)."
            ),
        )

    # ── 4. Status and scope guards ─────────────────────────────────────── #
    if pi_row["status"] == "Retired":
        raise HTTPException(
            status_code=422,
            detail=f"Pay item '{line_type}' has been retired and cannot be used for new entries.",
        )

    if pi_row["itemscope"] == "Period":
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{line_type}' is a Period-scope pay item and cannot be entered "
                "as a daily draft line. Use the Period Pay endpoint instead."
            ),
        )

    if pi_row["itemscope"] != "Daily":
        raise HTTPException(
            status_code=422,
            detail=f"'{line_type}' has an unrecognised item scope '{pi_row['itemscope']}'.",
        )

    # ── 5. Branch activation — LEFT JOIN + COALESCE fallback ──────────── #
    cfg_result = await db.execute(
        text("""
            SELECT isactive
            FROM   payroll.branchpayitemconfig
            WHERE  payitemid      = :piid
              AND  companyid      = :cid
              AND  branchid       = :bid
              AND  effectivefrom <= :dt
              AND  (effectiveto IS NULL OR effectiveto >= :dt)
            ORDER BY effectivefrom DESC
            LIMIT 1
        """),
        {
            "piid": pi_row["payitemid"],
            "cid":  company_id,
            "bid":  branch_id,
            "dt":   as_of_date,
        },
    )
    cfg_row = cfg_result.mappings().first()

    if cfg_row is not None:
        is_active = bool(cfg_row["isactive"])
    else:
        is_active = bool(pi_row["isdefaultbranchactive"])

    if not is_active:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Pay item '{line_type}' is not active for this branch "
                f"as of {as_of_date}. Activate it in Branch Pay Items settings first."
            ),
        )

    # Rate type mapping (may be absent for newly created custom items) -------
    rt_result = await db.execute(
        text("""
            SELECT rt.ratecode
            FROM   payroll.payitemratetypemap  pirtm
            JOIN   payroll.ratetypes           rt
                   ON rt.ratetypeid = pirtm.ratetypeid
            WHERE  pirtm.payitemid = :piid
              AND  pirtm.status    = 'Active'
              AND  rt.isactive     = TRUE
            ORDER BY pirtm.isprimary DESC
            LIMIT 1
        """),
        {"piid": pi_row["payitemid"]},
    )
    rt_row = rt_result.mappings().first()

    return _LineTypeInfo(
        rate_behavior=pi_row["ratebehavior"],
        rate_code=rt_row["ratecode"] if rt_row else None,
    )


_RATE_DEPENDENT_BEHAVIORS = frozenset(
    {"PerUnit", "OrdinalTier", "RangeBracket", "RangeProgressive", "Block"}
)


async def _refresh_draft_calculations(
    period_id: int,
    company_id: int,
    period_start_date: date,
    db: AsyncConnection,
) -> int:
    """
    Automatically re-compute calculatedamount + needsmanagerreview for every
    non-void, rate-dependent draft line in a period using the currently
    approved effective-dated rates for each line's work_date.

    Called automatically at:
      • Open → InReview (before submit guards) so newly approved backdated
        rates are reflected before blocking checks run.
      • finalize_period (after the period is confirmed Approved, before
        blocker guards) so finalization uses the most current rates.

    Only touches PerUnit / OrdinalTier / RangeBracket / RangeProgressive /
    Block lines.  EnteredAmount (BONUS, etc.), Fixed, and None lines are
    left unchanged — their calculatedamount is either entered directly by
    the user or not applicable.

    Returns the count of lines whose stored values were updated.
    """
    # Step 1: fetch all non-void, non-informational draft lines for the period.
    lines_result = await db.execute(
        text("""
            SELECT draftlineid, driverid, linetype, workdate,
                   quantity, rateamount, calculatedamount, needsmanagerreview
            FROM   payroll.payrolldraftlines
            WHERE  payrollperiodid = :pid
              AND  companyid       = :cid
              AND  status         != 'Void'
              AND  linetype       NOT IN ('DailyStatus', 'DailyNote')
        """),
        {"pid": period_id, "cid": company_id},
    )
    rows = list(lines_result.mappings().all())
    if not rows:
        return 0

    # Step 2: for each unique canonical line type, fetch ratebehavior + rate_code
    # once and cache.  Avoids per-line round-trips for the metadata lookup.
    lt_info_cache: dict[str, "_LineTypeInfo | None"] = {}

    async def _get_lt_info(canonical: str) -> "_LineTypeInfo | None":
        if canonical in lt_info_cache:
            return lt_info_cache[canonical]
        pi_result = await db.execute(
            text("""
                SELECT pi.ratebehavior,
                       (
                           SELECT rt.ratecode
                           FROM   payroll.payitemratetypemap pirtm
                           JOIN   payroll.ratetypes rt ON rt.ratetypeid = pirtm.ratetypeid
                           WHERE  pirtm.payitemid = pi.payitemid
                             AND  pirtm.status    = 'Active'
                             AND  rt.isactive     = TRUE
                           ORDER BY pirtm.isprimary DESC
                           LIMIT 1
                       ) AS rate_code
                FROM   payroll.payitems pi
                WHERE  pi.payitemcode = :code
                  AND  (pi.companyid IS NULL OR pi.companyid = :cid)
                  AND  pi.status    != 'Retired'
                LIMIT 1
            """),
            {"code": canonical, "cid": company_id},
        )
        pi_row = pi_result.mappings().first()
        if pi_row is None:
            lt_info_cache[canonical] = None
            return None
        info = _LineTypeInfo(
            rate_behavior=pi_row["ratebehavior"],
            rate_code=pi_row["rate_code"],
        )
        lt_info_cache[canonical] = info
        return info

    # Step 3: for each line, recompute and update if values changed.
    refresh_count = 0
    for row in rows:
        canonical = _LEGACY_TO_CANONICAL.get(row["linetype"], row["linetype"])
        lt_info = await _get_lt_info(canonical)
        if lt_info is None:
            continue  # unknown/retired item — leave as-is
        if lt_info.rate_behavior not in _RATE_DEPENDENT_BEHAVIORS:
            continue  # EnteredAmount / Fixed / None — not our concern

        as_of: date = (
            row["workdate"] if row["workdate"] is not None else period_start_date
        )
        qty = Decimal(str(row["quantity"])) if row["quantity"] is not None else Decimal("0")
        rate_ovr = (
            Decimal(str(row["rateamount"])) if row["rateamount"] is not None else None
        )

        _cr = await _compute_calculated_amount(
            rate_behavior=lt_info.rate_behavior,
            rate_code=lt_info.rate_code,
            quantity=qty,
            rate_amount_override=rate_ovr,
            driver_id=row["driverid"],
            company_id=company_id,
            as_of_date=as_of,
            db=db,
        )
        new_calc, new_review = _cr.calculated_amount, _cr.needs_manager_review

        old_calc = (
            Decimal(str(row["calculatedamount"]))
            if row["calculatedamount"] is not None
            else None
        )
        old_review = bool(row["needsmanagerreview"])
        old_rate_ovr = (
            Decimal(str(row["rateamount"])) if row["rateamount"] is not None else None
        )

        # Guard: respect manager-controlled NMR flags.
        #
        # Two cases where we DO NOT auto-clear needsmanagerreview:
        #   a) NMR=True AND calc IS NOT NULL:
        #      The line already has a computed amount; the manager manually
        #      flagged it for human review.  The refresh must not overrule that.
        #   b) NMR=True AND rate_amount IS NOT NULL (but calc IS NULL):
        #      A manual rate override was supplied.  Finalization will use
        #      COALESCE(calc, qty * rate_amount), so the line is resolvable.
        #      The manager's flag is still deliberate — leave it alone.
        #
        # We DO refresh when:
        #   NMR=True AND calc IS NULL AND rate_amount IS NULL:
        #      Truly unresolved — no approved rate was found at entry time.
        #      A rate may now exist (backdated approval); re-compute and,
        #      if resolved, auto-clear NMR so submission is no longer blocked.
        #   NMR=False (regardless of calc state):
        #      Normal line — calc may have become stale if the approved rate
        #      changed since the line was entered.  Re-compute to stay current.
        if old_review and (old_calc is not None or old_rate_ovr is not None):
            continue  # manager-flagged with a resolvable path — do not touch

        if new_calc != old_calc or new_review != old_review:
            await db.execute(
                text("""
                    UPDATE payroll.payrolldraftlines
                    SET    calculatedamount   = :calc,
                           needsmanagerreview = :review
                    WHERE  draftlineid = :lid
                """),
                {"calc": new_calc, "review": new_review, "lid": row["draftlineid"]},
            )
            refresh_count += 1

    return refresh_count


async def _compute_calculated_amount(
    rate_behavior: str,
    rate_code: str | None,
    quantity: Decimal,
    rate_amount_override: Decimal | None,
    driver_id: int,
    company_id: int,
    as_of_date: date,
    db: AsyncConnection,
) -> "_CalcResult":
    """
    Compute the calculated amount for a draft line.

    Returns a _CalcResult NamedTuple:
      (calculated_amount, needs_manager_review,
       driver_rate_id, rate_type_id, resolved_rate_amount)

    Phase 3B: the last three fields are populated for rate-based lookups so
    callers (finalization, preview) can snapshot the source into FinalLines.
    Existing callers that only unpack the first two positional fields are
    unaffected — NamedTuple positional access still works.

    rate_behavior dispatch:
      PerUnit:       qty × approved DriverRate for rate_code (looked up by date).
                     No approved rate → (None, True) — flagged for review.
                     No rate_code mapping → (None, True).
      EnteredAmount: (rate_amount_override, False) — user-supplied dollar amount.
      Fixed:         (None, False) — fixed amounts from PayItemSettings (M13c+).
      None / other:  (None, False) — informational or unimplemented behavior.
    Tiered/Block:    delegates to dedicated helpers which return (amount, nmr,
                     driver_rate_id, rate_type_id).
    """
    if rate_behavior == "EnteredAmount":
        return _CalcResult(rate_amount_override, False, rate_behavior="EnteredAmount")

    # M13c tiered / block behaviors — dispatch to dedicated helpers.
    if rate_behavior in _TIERED_BEHAVIORS or rate_behavior == "Block":
        if not rate_code:
            # No RateType mapping → calculation unresolvable → flag for review.
            return _CalcResult(None, True, rate_behavior=rate_behavior)
        if rate_behavior == "OrdinalTier":
            amt, nmr, rid, rtid = await _compute_ordinal_tier(
                quantity, driver_id, company_id, as_of_date, rate_code, db
            )
            return _CalcResult(amt, nmr, driver_rate_id=rid, rate_type_id=rtid, rate_behavior="OrdinalTier")
        if rate_behavior == "RangeBracket":
            amt, nmr, rid, rtid = await _compute_range_bracket(
                quantity, driver_id, company_id, as_of_date, rate_code, db
            )
            return _CalcResult(amt, nmr, driver_rate_id=rid, rate_type_id=rtid, rate_behavior="RangeBracket")
        if rate_behavior == "RangeProgressive":
            amt, nmr, rid, rtid = await _compute_range_progressive(
                quantity, driver_id, company_id, as_of_date, rate_code, db
            )
            return _CalcResult(amt, nmr, driver_rate_id=rid, rate_type_id=rtid, rate_behavior="RangeProgressive")
        # Block
        amt, nmr, rid, rtid = await _compute_block(
            quantity, driver_id, company_id, as_of_date, rate_code, db
        )
        return _CalcResult(amt, nmr, driver_rate_id=rid, rate_type_id=rtid, rate_behavior="Block")

    if rate_behavior != "PerUnit":
        # Fixed, None — not computed yet.
        return _CalcResult(None, False, rate_behavior=rate_behavior)

    # PerUnit: look up the driver's approved rate for rate_code as-of as_of_date.
    if not rate_code:
        # PerUnit item but no rate type mapping in PayItemRateTypeMap.
        # If the caller supplied a manual rate_amount, the finalization COALESCE
        # will produce a non-zero result (qty * rate_amount) — no review needed.
        # If no rate_amount either, the line would finalize as zero — flag for review.
        if rate_amount_override is not None:
            return _CalcResult(None, False, rate_behavior="PerUnit")
        return _CalcResult(None, True, rate_behavior="PerUnit")

    # Include Superseded rows — a superseded rate is still the correct rate
    # for work dates that fall within its original effective range.
    # (Mirrors the lookup logic in get_driver_rate_on_date.)
    rate_result = await db.execute(
        text("""
            SELECT dr.driverrateid, dr.ratetypeid, dr.amount
            FROM   payroll.driverrates dr
            JOIN   payroll.ratetypes   rt ON rt.ratetypeid = dr.ratetypeid
            WHERE  dr.driverid       = :did
              AND  dr.companyid      = :cid
              AND  rt.ratecode       = :rcode
              AND  dr.status         IN ('Approved', 'Superseded')
              AND  dr.effectivefrom <= :dt
              AND  (dr.effectiveto IS NULL OR dr.effectiveto >= :dt)
            ORDER BY dr.effectivefrom DESC
            LIMIT 1
        """),
        {
            "did":   driver_id,
            "cid":   company_id,
            "rcode": rate_code,
            "dt":    as_of_date,
        },
    )
    rate_row = rate_result.mappings().first()

    if rate_row is None:
        # No approved rate for this driver / type / date.
        # If the caller supplied a manual rate_amount, the finalization COALESCE
        # will produce a non-zero result — no review needed.
        # If no rate_amount either, the line would finalize as zero — flag for review.
        if rate_amount_override is not None:
            return _CalcResult(None, False, rate_behavior="PerUnit")
        return _CalcResult(None, True, rate_behavior="PerUnit")

    resolved_amt = Decimal(str(rate_row["amount"]))
    # CP-4A: authoritative PerUnit multiply/quantize now lives in the pure core.
    calculated = _calculate_per_unit(
        _PerUnitInput(quantity=quantity, rate_amount=resolved_amt)
    ).calculated_amount
    return _CalcResult(
        calculated,
        False,
        driver_rate_id=int(rate_row["driverrateid"]),
        rate_type_id=int(rate_row["ratetypeid"]),
        resolved_rate_amount=resolved_amt,
        rate_behavior="PerUnit",
    )


# ---------------------------------------------------------------------------
# Driver eligibility helpers (shared by all payroll write/finalization paths)
# ---------------------------------------------------------------------------

async def _assert_driver_eligible_for_date(
    company_id: int,
    driver_id: int,
    branch_id: int,
    work_date: "date",
    db: AsyncConnection,
) -> None:
    """
    Raise HTTP 422 if the driver is not eligible to work on *work_date* in the
    given branch.

    Uses the exact same criteria as the day-grid eligibility query so that the
    write paths are consistent with what the grid shows:
      - driver belongs to company and branch
      - employee employment_status = 'Active'
      - driver_status = 'Active'
        OR (driver_status = 'Transferred' AND effectiveto IS NOT NULL AND effectiveto >= work_date)
      - hire_date IS NULL OR hire_date <= work_date
      - termination_date IS NULL OR termination_date >= work_date
      - effective_from IS NULL OR effective_from <= work_date
      - effective_to   IS NULL OR effective_to   >= work_date
    """
    result = await db.execute(
        text("""
            SELECT 1
            FROM   core.drivers   d
            JOIN   core.employees e ON e.employeeid = d.employeeid
            WHERE  d.driverid         = :did
              AND  d.companyid        = :cid
              AND  d.branchid         = :bid
              AND  e.employmentstatus = 'Active'
              AND  (
                       d.driverstatus = 'Active'
                    OR (d.driverstatus = 'Transferred'
                        AND d.effectiveto IS NOT NULL
                        AND d.effectiveto >= :dt)
                   )
              AND  (e.hiredate IS NULL OR e.hiredate <= :dt)
              AND  (e.terminationdate IS NULL OR e.terminationdate >= :dt)
              AND  (d.effectivefrom IS NULL OR d.effectivefrom <= :dt)
              AND  (d.effectiveto   IS NULL OR d.effectiveto   >= :dt)
        """),
        {"did": driver_id, "cid": company_id, "bid": branch_id, "dt": work_date},
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Driver is not eligible for this payroll period, branch, or work date."
            ),
        )


async def _assert_driver_eligible_for_period(
    company_id: int,
    driver_id: int,
    branch_id: int,
    period_start: "date",
    period_end: "date",
    db: AsyncConnection,
) -> None:
    """
    Raise HTTP 422 if the driver is not eligible for at least one day in the
    payroll period.

    Mirrors the period-pay eligibility list in get_period_eligible_drivers:
      - driver belongs to company and branch
      - employment_status = 'Active'
      - driver_status = 'Active'  (no in-transfer; transferred drivers keep their
        old profile Active until the effective date passes)
      - hire/termination window overlaps the period
      - driver effective window (effectivefrom/effectiveto) overlaps the period

    This intentionally excludes 'Transferred' drivers (driverstatus = 'Transferred'
    means they have already left this branch).
    """
    result = await db.execute(
        text("""
            SELECT 1
            FROM   core.drivers   d
            JOIN   core.employees e ON e.employeeid = d.employeeid
            WHERE  d.driverid         = :did
              AND  d.companyid        = :cid
              AND  d.branchid         = :bid
              AND  e.employmentstatus = 'Active'
              AND  d.driverstatus     = 'Active'
              AND  (e.hiredate IS NULL OR e.hiredate <= :period_end)
              AND  (e.terminationdate IS NULL OR e.terminationdate >= :period_start)
              AND  (d.effectivefrom IS NULL OR d.effectivefrom <= :period_end)
              AND  (d.effectiveto   IS NULL OR d.effectiveto   >= :period_start)
        """),
        {
            "did":          driver_id,
            "cid":          company_id,
            "bid":          branch_id,
            "period_start": period_start,
            "period_end":   period_end,
        },
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Driver is not eligible for this payroll period or branch."
            ),
        )


# ── CP-2E: Canonical Eligibility Snapshot helpers ────────────────────────────

async def _period_has_driver_eligibility_snapshot(
    period_id: int, db: AsyncConnection
) -> bool:
    """Check marker table — one row per snapshotted period, even if zero drivers eligible."""
    row = (await db.execute(
        text(
            "SELECT 1 FROM payroll.payrollperiodeligibilitysnapshots "
            "WHERE payrollperiodid = :pid LIMIT 1"
        ),
        {"pid": period_id},
    )).first()
    return row is not None


async def _get_driver_eligibility_row(
    period_id: int, driver_id: int, company_id: int, branch_id: int,
    db: AsyncConnection,
):
    return (await db.execute(
        text("""
            SELECT payrollperioddrivereligibilityid,
                   eligibilityreasoncode,
                   hiredatesnapshot,
                   terminationdatesnapshot,
                   drivereffectivefromsnapshot,
                   drivereffectivetosnapshot,
                   frozenatutc,
                   drivernamesnapshot,
                   drivercodesnapshot,
                   employeekeysnapshot,
                   iseligibleforperiod
            FROM   payroll.payrollperioddrivereligibility
            WHERE  payrollperiodid = :pid
              AND  driverid        = :did
              AND  companyid       = :cid
              AND  branchid        = :bid
        """),
        {"pid": period_id, "did": driver_id, "cid": company_id, "bid": branch_id},
    )).first()


def _is_snapshot_row_eligible_for_workdate(row, work_date: date) -> bool:
    """Derive date-level eligibility from a snapshot row.

    Returns True only if the driver's snapshotted date windows cover work_date.
    IncludedByExistingData always returns False here — such drivers require an
    existing-source check (DB query) and are never eligible purely by window.
    Generated-row reason codes (Active/TerminatedHistorical/Transferred) are
    evaluated against their snapshotted hire/termination/effectivefrom/effectiveto.
    """
    if row is None or not row.iseligibleforperiod:
        return False
    reason = row.eligibilityreasoncode

    # IBED is never window-eligible; always requires existing-source rescue
    if reason == "IncludedByExistingData":
        return False

    hire     = row.hiredatesnapshot
    term     = row.terminationdatesnapshot
    eff_from = row.drivereffectivefromsnapshot
    eff_to   = row.drivereffectivetosnapshot

    if hire is not None and hire > work_date:
        return False
    if eff_from is not None and eff_from > work_date:
        return False
    if eff_to is not None and eff_to < work_date:
        return False

    if reason == "TerminatedHistorical":
        if term is None or term < work_date:
            return False
    else:
        if term is not None and term < work_date:
            return False
    return True


async def _driver_has_existing_daily_source_on_date(
    period_id: int, driver_id: int, work_date: date, db: AsyncConnection
) -> bool:
    r = (await db.execute(
        text("""
            SELECT 1 FROM payroll.payrolldraftlines
            WHERE payrollperiodid = :pid AND driverid = :did
              AND workdate = :dt AND status != 'Void'
            LIMIT 1
        """),
        {"pid": period_id, "did": driver_id, "dt": work_date},
    )).first()
    if r:
        return True
    r2 = (await db.execute(
        text("""
            SELECT 1 FROM payroll.payrollperioddriverdayentrystate
            WHERE payrollperiodid = :pid AND driverid = :did
              AND workdate = :dt AND isvoided = FALSE
            LIMIT 1
        """),
        {"pid": period_id, "did": driver_id, "dt": work_date},
    )).first()
    return r2 is not None


async def _driver_has_existing_period_pay_source(
    period_id: int, driver_id: int, db: AsyncConnection
) -> bool:
    r = (await db.execute(
        text("""
            SELECT 1 FROM payroll.payrolldraftlines
            WHERE payrollperiodid = :pid AND driverid = :did
              AND linescope = 'Period' AND status != 'Void'
            LIMIT 1
        """),
        {"pid": period_id, "did": driver_id},
    )).first()
    return r is not None


async def _assert_driver_eligible_for_workdate_via_snapshot(
    company_id: int, branch_id: int, period_id: int,
    driver_id: int, work_date: date, db: AsyncConnection,
    allow_existing_source_rescue: bool = True,
) -> None:
    """Check eligibility for a specific work date, using snapshot when available.

    Primary gate: date-window check via _is_snapshot_row_eligible_for_workdate.
    Secondary gate (rescue): if allow_existing_source_rescue=True, a driver
    whose date window does not cover work_date is still allowed if they have
    existing daily source (DraftLine or EntryState) on that exact date.
    This rescue applies to ALL reason codes, not just IncludedByExistingData —
    a generated-row driver (Active/TerminatedHistorical/Transferred) may have
    data outside their eligibility window from before the snapshot was frozen.

    Callers:
    - save_day_grid:         allow_existing_source_rescue=True  (default)
    - update_draft_line:     allow_existing_source_rescue=True  (default)
    - add_draft_line:        allow_existing_source_rescue=False (new source — no rescue)
    """
    if not await _period_has_driver_eligibility_snapshot(period_id, db):
        await _assert_driver_eligible_for_date(company_id, driver_id, branch_id, work_date, db)
        return
    row = await _get_driver_eligibility_row(period_id, driver_id, company_id, branch_id, db)
    if row is None:
        raise HTTPException(
            status_code=422,
            detail="Driver has no eligibility snapshot for this period.",
        )
    # Primary: date-window check
    if _is_snapshot_row_eligible_for_workdate(row, work_date):
        return
    # Secondary rescue: existing source on exact date (any reason code)
    if allow_existing_source_rescue:
        has_existing = await _driver_has_existing_daily_source_on_date(
            period_id, driver_id, work_date, db
        )
        if has_existing:
            return
    # Build a useful error message based on reason code
    if row.eligibilityreasoncode == "IncludedByExistingData":
        raise HTTPException(
            status_code=422,
            detail=(
                "Driver is only eligible for existing saved dates. "
                "New entries on new work dates are not permitted."
            ),
        )
    raise HTTPException(
        status_code=422,
        detail="Driver is not eligible for this work date.",
    )


async def _assert_driver_eligible_for_period_via_snapshot(
    company_id: int, branch_id: int, period_id: int,
    driver_id: int, db: AsyncConnection,
) -> None:
    """Check period-level eligibility, using snapshot when available."""
    if not await _period_has_driver_eligibility_snapshot(period_id, db):
        period = (await db.execute(
            text(
                "SELECT startdate, enddate FROM payroll.payrollperiods "
                "WHERE payrollperiodid = :pid"
            ),
            {"pid": period_id},
        )).first()
        await _assert_driver_eligible_for_period(
            company_id, driver_id, branch_id, period.startdate, period.enddate, db
        )
        return
    row = await _get_driver_eligibility_row(period_id, driver_id, company_id, branch_id, db)
    if row is None:
        raise HTTPException(
            status_code=422,
            detail="Driver has no eligibility snapshot for this period.",
        )
    if row.eligibilityreasoncode == "IncludedByExistingData":
        has_existing = await _driver_has_existing_period_pay_source(period_id, driver_id, db)
        if not has_existing:
            raise HTTPException(
                status_code=422,
                detail="Driver is not eligible for new period-pay entries in this period.",
            )
        return
    # Active / TerminatedHistorical / Transferred — present in snapshot = period-eligible


async def _create_period_driver_eligibility_rows(
    period_id: int,
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
    snapshot_source: str = "Generated",
    freeze: bool = False,
    created_by_user_id: int | None = None,
    frozen_by_user_id: int | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    frozen_at = now if freeze else None

    # Paths 1–3: Active / TerminatedHistorical / Transferred
    await db.execute(text("""
        INSERT INTO payroll.payrollperioddrivereligibility
            (companyid, branchid, payrollperiodid, driverid, sourceemployeeid,
             drivercodesnapshot, drivernamesnapshot, employeekeysnapshot,
             driverstatussnapshot,
             employmentstatussnapshot,
             transferredfromdriveridsnapshot, transferredtodriveridsnapshot,
             hiredatesnapshot, terminationdatesnapshot,
             drivereffectivefromsnapshot, drivereffectivetosnapshot,
             iseligibleforperiod, eligibilityreasoncode, snapshotsource,
             createdatutc, createdbyuserid, updatedatutc, frozenatutc, frozenbyuserid)
        SELECT
            pp.companyid, pp.branchid, pp.payrollperiodid,
            d.driverid, e.employeeid,
            d.drivercode, e.fullname, e.employeekey, d.driverstatus, e.employmentstatus,
            d.transferredfromdriverid, d.transferredtodriverid,
            e.hiredate, e.terminationdate, d.effectivefrom, d.effectiveto,
            TRUE,
            CASE
                WHEN d.driverstatus = 'Terminated' THEN 'TerminatedHistorical'
                WHEN d.driverstatus = 'Transferred' THEN 'Transferred'
                ELSE 'Active'
            END,
            :src, CAST(:now AS TIMESTAMPTZ), CAST(:uid AS INTEGER), CAST(:now AS TIMESTAMPTZ), CAST(:frozen_at AS TIMESTAMPTZ), CAST(:fuid AS INTEGER)
        FROM payroll.payrollperiods pp
        JOIN core.drivers d ON d.companyid = pp.companyid AND d.branchid = pp.branchid
        JOIN core.employees e ON e.employeeid = d.employeeid
        WHERE pp.payrollperiodid = :pid
          AND (
            (    d.driverstatus     = 'Active'
             AND e.employmentstatus = 'Active'
             AND (e.hiredate IS NULL OR e.hiredate <= pp.enddate)
             AND (e.terminationdate IS NULL OR e.terminationdate >= pp.startdate)
             AND (d.effectivefrom IS NULL OR d.effectivefrom <= pp.enddate)
             AND (d.effectiveto   IS NULL OR d.effectiveto   >= pp.startdate)
            )
            OR
            (    d.driverstatus     = 'Terminated'
             AND e.employmentstatus = 'Terminated'
             AND e.terminationdate IS NOT NULL
             AND e.terminationdate >= pp.startdate
             AND (e.hiredate IS NULL OR e.hiredate <= pp.enddate)
             AND (d.effectivefrom IS NULL OR d.effectivefrom <= pp.enddate)
             AND (d.effectiveto   IS NULL OR d.effectiveto   >= pp.startdate)
            )
            OR
            (    d.driverstatus     = 'Transferred'
             AND e.employmentstatus = 'Active'
             AND d.effectiveto IS NOT NULL
             AND d.effectiveto >= pp.startdate
             AND (d.effectivefrom IS NULL OR d.effectivefrom <= pp.enddate)
             AND (e.hiredate IS NULL OR e.hiredate <= pp.enddate)
             AND (e.terminationdate IS NULL OR e.terminationdate >= pp.startdate)
            )
          )
        ON CONFLICT (payrollperiodid, driverid) DO NOTHING
    """), {
        "pid": period_id, "src": snapshot_source, "now": now,
        "uid": created_by_user_id, "frozen_at": frozen_at, "fuid": frozen_by_user_id,
    })

    # Path 4: IncludedByExistingData
    await db.execute(text("""
        INSERT INTO payroll.payrollperioddrivereligibility
            (companyid, branchid, payrollperiodid, driverid, sourceemployeeid,
             drivercodesnapshot, drivernamesnapshot, employeekeysnapshot,
             driverstatussnapshot,
             employmentstatussnapshot,
             transferredfromdriveridsnapshot, transferredtodriveridsnapshot,
             hiredatesnapshot, terminationdatesnapshot,
             drivereffectivefromsnapshot, drivereffectivetosnapshot,
             iseligibleforperiod, eligibilityreasoncode, snapshotsource,
             createdatutc, createdbyuserid, updatedatutc, frozenatutc, frozenbyuserid)
        SELECT DISTINCT
            pp.companyid, pp.branchid, pp.payrollperiodid,
            d.driverid, e.employeeid,
            d.drivercode, e.fullname, e.employeekey, d.driverstatus, e.employmentstatus,
            d.transferredfromdriverid, d.transferredtodriverid,
            e.hiredate, e.terminationdate, d.effectivefrom, d.effectiveto,
            TRUE, 'IncludedByExistingData', :src,
            CAST(:now AS TIMESTAMPTZ), CAST(:uid AS INTEGER), CAST(:now AS TIMESTAMPTZ), CAST(:frozen_at AS TIMESTAMPTZ), CAST(:fuid AS INTEGER)
        FROM payroll.payrollperiods pp
        JOIN (
            SELECT payrollperiodid, driverid, companyid
            FROM   payroll.payrolldraftlines
            WHERE  status != 'Void'
            UNION
            SELECT payrollperiodid, driverid, companyid
            FROM   payroll.payrollperioddriverdayentrystate
            WHERE  isvoided = FALSE
        ) src2 ON src2.payrollperiodid = pp.payrollperiodid
               AND src2.companyid = pp.companyid
        JOIN core.drivers   d ON d.driverid  = src2.driverid
                              AND d.companyid = pp.companyid
                              AND d.branchid  = pp.branchid
        JOIN core.employees e ON e.employeeid = d.employeeid
        WHERE pp.payrollperiodid = :pid
        ON CONFLICT (payrollperiodid, driverid) DO NOTHING
    """), {
        "pid": period_id, "src": snapshot_source, "now": now,
        "uid": created_by_user_id, "frozen_at": frozen_at, "fuid": frozen_by_user_id,
    })

    # Upsert marker row — ensure period is tracked as snapshotted even if zero drivers
    await db.execute(text("""
        INSERT INTO payroll.payrollperiodeligibilitysnapshots
            (payrollperiodid, companyid, branchid, snapshotsource,
             createdatutc, createdbyuserid, updatedatutc, frozenatutc, frozenbyuserid)
        SELECT
            pp.payrollperiodid, pp.companyid, pp.branchid, :src,
            CAST(:now AS TIMESTAMPTZ), CAST(:uid AS INTEGER),
            CAST(:now AS TIMESTAMPTZ),
            CAST(:frozen_at AS TIMESTAMPTZ), CAST(:fuid AS INTEGER)
        FROM payroll.payrollperiods pp
        WHERE pp.payrollperiodid = :pid
        ON CONFLICT (payrollperiodid) DO NOTHING
    """), {
        "pid": period_id, "src": snapshot_source, "now": now,
        "uid": created_by_user_id, "frozen_at": frozen_at, "fuid": frozen_by_user_id,
    })


async def _regenerate_period_driver_eligibility_rows(
    period_id: int,
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
    created_by_user_id: int | None = None,
    frozen_by_user_id: int | None = None,
) -> None:
    """Drop provisional snapshot rows and re-create frozen for Draft→Open promotion."""
    await db.execute(
        text(
            "DELETE FROM payroll.payrollperioddrivereligibility "
            "WHERE payrollperiodid = :pid"
        ),
        {"pid": period_id},
    )
    await _create_period_driver_eligibility_rows(
        period_id, company_id, branch_id, db,
        snapshot_source="Generated",
        freeze=True,
        created_by_user_id=created_by_user_id,
        frozen_by_user_id=frozen_by_user_id,
    )


async def _freeze_period_driver_eligibility_snapshot(
    period_id: int,
    db: AsyncConnection,
    frozen_by_user_id: int | None = None,
) -> None:
    """Freeze all unfrozen eligibility rows and the marker row for a period."""
    now = datetime.now(timezone.utc)
    await db.execute(
        text("""
            UPDATE payroll.payrollperioddrivereligibility
            SET frozenatutc    = CAST(:now AS TIMESTAMPTZ),
                frozenbyuserid = :uid,
                updatedatutc   = CAST(:now AS TIMESTAMPTZ)
            WHERE payrollperiodid = :pid
              AND frozenatutc IS NULL
        """),
        {"pid": period_id, "now": now, "uid": frozen_by_user_id},
    )
    # Also freeze the marker row
    await db.execute(
        text("""
            UPDATE payroll.payrollperiodeligibilitysnapshots
            SET frozenatutc    = CAST(:now AS TIMESTAMPTZ),
                frozenbyuserid = :uid,
                updatedatutc   = CAST(:now AS TIMESTAMPTZ)
            WHERE payrollperiodid = :pid
              AND frozenatutc IS NULL
        """),
        {"pid": period_id, "now": now, "uid": frozen_by_user_id},
    )

# ── End CP-2E helpers ─────────────────────────────────────────────────────────


# ---------------------------------------------------------------------------
# Add a draft line
# ---------------------------------------------------------------------------

async def add_draft_line(
    period_id: int,
    company_id: int,
    user_id: int,
    data: DraftLineCreate,
    db: AsyncConnection,
) -> DraftLineSummary:
    """
    Insert a new draft line.

    Guards:
      - Period must be Open or InReview.
      - Driver must exist in this company and share the period's branch.
    """
    period = await get_period_by_id(company_id, user_id, period_id, db)

    # CP-2F: Draft periods allow daily source-only lines (operational entry).
    # Period Pay, Bonus, System lines, STATUS_PAYMENT, ADJUSTMENT, MINIMUM/MAXIMUM,
    # NeedsManagerReview=True, and lines without work_date are blocked.
    if period.status == "Draft":
        if data.source_type == "System":
            raise HTTPException(
                status_code=422,
                detail="System lines cannot be added to a Prepared (Draft) period.",
            )
        if data.work_date is None:
            raise HTTPException(
                status_code=422,
                detail="work_date is required when adding lines to a Prepared (Draft) period.",
            )
        # CP-2F: Draft is source-only — rate_amount is a financial field, always rejected.
        if data.rate_amount is not None:
            raise HTTPException(
                status_code=422,
                detail="rate_amount cannot be supplied for a Prepared (Draft) period line.",
            )
        if data.needs_manager_review:
            raise HTTPException(
                status_code=422,
                detail="needs_manager_review cannot be set on a Prepared (Draft) period.",
            )
        # Period-scope and financial items are blocked in Draft
        _draft_pi_check = await db.execute(
            text("""
                SELECT itemscope, payitemcode FROM payroll.payitems
                WHERE payitemcode = :code
                  AND (companyid IS NULL OR companyid = :cid)
                  AND status != 'Retired'
                LIMIT 1
            """),
            {"code": _LEGACY_TO_CANONICAL.get(data.line_type, data.line_type), "cid": company_id},
        )
        _draft_pi_row = _draft_pi_check.mappings().first()
        if _draft_pi_row:
            if _draft_pi_row["itemscope"] == "Period":
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Period-scope pay lines (Period Pay, Bonus, etc.) cannot be added "
                        "to a Prepared (Draft) period."
                    ),
                )
            # Block STATUS_PAYMENT / ADJUSTMENT / MINIMUM / MAXIMUM pay items
            _blocked_codes = {"STATUS_PAYMENT", "ADJUSTMENT", "MINIMUM", "MAXIMUM",
                              "SYS_MIN_TOPUP", "SYS_MAX_CAP"}
            if _draft_pi_row["payitemcode"] in _blocked_codes:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Pay item '{_draft_pi_row['payitemcode']}' cannot be added "
                        "to a Prepared (Draft) period."
                    ),
                )
        # informational (DailyStatus, DailyNote) and Daily pay items are allowed
    elif period.status not in ENTRY_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Draft lines can only be added to Open or Returned periods "
                f"(current status: '{period.status}')."
            ),
        )

    # Permission gate: adding payroll entries requires payroll.entry
    await _check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    # Daily lines require a work_date: the eligibility check, duplicate guard,
    # and rate lookups all depend on it.
    if data.work_date is None:
        raise HTTPException(
            status_code=422,
            detail="work_date is required for Daily draft lines.",
        )

    # CP-2B: snapshot-aware work_date validation.
    # Checks StartDate/EndDate bounds and, when PayrollPeriodDays rows exist for
    # the period, also verifies the work_date appears in the snapshot.
    # Raises 400 for out-of-bounds; raises 400 for snapshot-missing dates.
    # IsConfiguredOffDay does not block entry in CP-2B.
    await _validate_period_work_date(
        period.payroll_period_id, data.work_date,
        period.start_date, period.end_date, db,
    )

    # CP-2E: use snapshot-based eligibility when available; legacy fallback otherwise.
    # add_draft_line creates new source — rescue not allowed; driver must be in window.
    await _assert_driver_eligible_for_workdate_via_snapshot(
        company_id, period.branch_id, period_id, data.driver_id, data.work_date, db,
        allow_existing_source_rescue=False,
    )

    # CP-0: Normalise caller-supplied line_type to canonical PayItemCode before
    # validation and storage.  Legacy display strings ("Hours", "Miles") are
    # silently promoted to their canonical counterparts ("HOURS", "MILES").
    # Informational-only items (DailyStatus, DailyNote) have no canonical code
    # and are kept verbatim.  Custom company items are already canonical.
    canonical_line_type: str = _LEGACY_TO_CANONICAL.get(data.line_type, data.line_type)

    # M13a: PayItem-driven line-type validation.
    # as_of_date: use work_date when supplied, otherwise fall back to the period
    # start date (covers retro-entry where work_date is omitted).
    as_of_date: date = data.work_date if data.work_date is not None else period.start_date
    lt_info: _LineTypeInfo = await _validate_line_type(
        canonical_line_type, period.branch_id, company_id, as_of_date, db,
        period_id=period.payroll_period_id,
    )

    # M13c: OrdinalTier items require a positive integer quantity.
    if lt_info.rate_behavior == "OrdinalTier":
        if data.quantity <= 0 or data.quantity % 1 != 0:
            raise HTTPException(
                status_code=422,
                detail=(
                    "OrdinalTier items require a positive integer quantity; "
                    "fractional or zero quantities are not allowed."
                ),
            )

    # Phase 4C: block manual rate overrides for PerUnit daily lines.
    # Rates must come exclusively from approved DriverRates.
    if lt_info.rate_behavior == "PerUnit" and data.rate_amount is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Manual rate overrides are not allowed. "
                "Update the driver's rate in Pay Rates."
            ),
        )

    # CP-2F: Draft (Prepared) periods store NULL financial fields — no calculation,
    # no rate lookup, no NeedsManagerReview.  Calculations are applied at Draft→Open.
    if period.status == "Draft":
        calc_amount = None
        needs_review = False
        # rate_amount was already rejected above; force NULL at INSERT level as defence-in-depth.
        _insert_rate_amount = None
    else:
        # M13b: compute calculated amount.
        _cr_add = await _compute_calculated_amount(
            rate_behavior=lt_info.rate_behavior,
            rate_code=lt_info.rate_code,
            quantity=data.quantity,
            rate_amount_override=data.rate_amount,
            driver_id=data.driver_id,
            company_id=company_id,
            as_of_date=as_of_date,
            db=db,
        )
        calc_amount = _cr_add.calculated_amount
        flag_review = _cr_add.needs_manager_review
        needs_review: bool = data.needs_manager_review or flag_review

    # ── P0 duplicate guard ────────────────────────────────────────────────── #
    # Reject if an active (non-Void) Daily line already exists for the same
    # business key: (company, period, driver, work_date, canonical line_type).
    # This prevents double-pay bugs from concurrent or repeated submissions.
    # void_draft_line frees the slot; update_draft_line is the correct path
    # when the caller wants to change quantity on an existing line.
    dup_check = await db.execute(
        text("""
            SELECT draftlineid
            FROM   payroll.payrolldraftlines
            WHERE  companyid       = :company_id
              AND  payrollperiodid = :period_id
              AND  driverid        = :driver_id
              AND  workdate        = :work_date
              AND  linetype        = :line_type
              AND  linescope       = 'Daily'
              AND  status         != 'Void'
            LIMIT 1
        """),
        {
            "company_id": company_id,
            "period_id":  period_id,
            "driver_id":  data.driver_id,
            "work_date":  data.work_date,
            "line_type":  canonical_line_type,
        },
    )
    if dup_check.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                "A payroll line for this driver, date, and pay item already exists. "
                "Update the existing line instead."
            ),
        )

    # CP-2D1: validate DailyStatus code before any mutation.
    # Blank/missing notes for DailyStatus add is rejected — there is no clear
    # operation on the add path; callers must supply a valid active status code.
    _direct_add_sk_row: dict | None = None
    if canonical_line_type == "DailyStatus":
        if not data.notes or not data.notes.strip():
            raise HTTPException(
                status_code=422,
                detail="DailyStatus lines require a valid status code in 'notes'.",
            )
        _direct_add_sk_row = await _validate_status_key(
            data.notes, company_id, period.branch_id, db,
        )

    # CP-0A: Lock custom PayItem catalog row before period lock so both this path
    # and the physical-delete path acquire locks in the same order (PayItem then
    # Period), preventing deadlock while serializing against concurrent deletion.
    # CP-2C: pass period_id so snapshot-authorised items bypass the live status check.
    await _lock_pay_item_for_source_write(canonical_line_type, company_id, db, period_id=period_id)
    # CP-0A: Recheck period status under a row-level lock before writing.
    await _lock_period_for_mutation(period_id, company_id, db)

    insert_result = await db.execute(
        text("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid,
                 workdate, linetype, linescope, quantity, rateamount, calculatedamount,
                 sourcetype, status, needsmanagerreview, notes, addedbyuserid)
            VALUES
                (:company_id, :branch_id, :period_id, :driver_id,
                 :work_date, :line_type, 'Daily', :quantity, :rate_amount, :calc_amount,
                 :source_type, 'Active', :needs_review, :notes, :added_by)
            RETURNING draftlineid
        """),
        {
            "company_id":   company_id,
            "branch_id":    period.branch_id,
            "period_id":    period_id,
            "driver_id":    data.driver_id,
            "work_date":    data.work_date,
            "line_type":    canonical_line_type,   # store canonical, not raw input
            "quantity":     data.quantity,
            "rate_amount":  _insert_rate_amount if period.status == "Draft" else data.rate_amount,
            "calc_amount":  calc_amount,
            "source_type":  data.source_type,
            "needs_review": needs_review,
            "notes":        data.notes,
            "added_by":     user_id,
        },
    )
    line_id: int = insert_result.scalar_one()

    await _write_line_audit(
        db,
        company_id=company_id,
        branch_id=period.branch_id,
        user_id=user_id,
        line_id=line_id,
        action_code="DRAFT_LINE_ADDED",
        new_value={
            "period_id":   period_id,
            "driver_id":   data.driver_id,
            "line_type":   canonical_line_type,
            "quantity":    float(data.quantity),
            "source_type": data.source_type,
        },
    )
    if canonical_line_type not in _INFORMATIONAL_ONLY:
        await _capture_source_evidence(
            company_id=company_id, branch_id=period.branch_id, period_id=period_id,
            user_id=user_id, line_id=line_id, action_code="SOURCE_CREATED", db=db,
            before_state=None,
            after_state={
                "line_type": canonical_line_type, "line_scope": "Daily",
                "quantity": data.quantity, "rate_amount": data.rate_amount,
                "calculated_amount": calc_amount, "source_type": data.source_type,
                "status": "Active", "notes": data.notes,
            },
            driver_id=data.driver_id, work_date=data.work_date, line_type=canonical_line_type,
        )

    # CP-2D1: dual-write canonical entry-state for informational lines.
    # DailyStatus: use statuskeyid from pre-validated _direct_add_sk_row (guaranteed active).
    # DailyNote: plain text, no StatusKey involved.
    if data.work_date is not None:
        if canonical_line_type == "DailyStatus":
            sk_id = _direct_add_sk_row["statuskeyid"] if _direct_add_sk_row else None
            await _upsert_entry_state(
                company_id, period.branch_id, period_id,
                data.driver_id, data.work_date, user_id, db,
                status_key_id=sk_id,
                note_text=None,
                set_status=True,
                set_note=False,
            )
        elif canonical_line_type == "DailyNote":
            await _upsert_entry_state(
                company_id, period.branch_id, period_id,
                data.driver_id, data.work_date, user_id, db,
                status_key_id=None,
                note_text=data.notes or None,
                set_status=False,
                set_note=True,
            )

    return await _get_line_by_id(line_id, company_id, db)


# ---------------------------------------------------------------------------
# Update a draft line
# ---------------------------------------------------------------------------

async def update_draft_line(
    period_id: int,
    draft_line_id: int,
    company_id: int,
    user_id: int,
    data: DraftLineUpdate,
    db: AsyncConnection,
) -> DraftLineSummary:
    """Partially update a draft line — only non-None fields are touched."""
    period = await get_period_by_id(company_id, user_id, period_id, db)

    # CP-2F: Draft (Prepared) periods allow updating daily source lines only.
    if period.status == "Draft":
        if data.rate_amount is not None:
            raise HTTPException(
                status_code=422,
                detail="rate_amount cannot be set on a Prepared (Draft) period line.",
            )
        if data.needs_manager_review is True:
            raise HTTPException(
                status_code=422,
                detail="needs_manager_review cannot be set to True on a Prepared (Draft) period.",
            )
    elif period.status in _WRITE_BLOCKED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot modify lines on a period with status '{period.status}'.",
        )

    # Permission gate: editing payroll entries requires payroll.entry
    await _check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    line = await _get_line_by_id(draft_line_id, company_id, db)
    if line.period_id != period_id:
        raise HTTPException(status_code=404, detail="Draft line not found in this period.")
    if line.status == "Void":
        raise HTTPException(status_code=422, detail="Cannot modify a voided draft line.")

    # CP-2F: For Draft periods, only daily source lines may be updated.
    if period.status == "Draft":
        if line.line_scope != "Daily" or line.source_type == "System":
            raise HTTPException(
                status_code=422,
                detail="Only daily source lines can be updated on a Prepared (Draft) period.",
            )

    # CP-2D2: guard — STATUS_PAYMENT lines are managed automatically
    _sp_check = await db.execute(
        text(
            "SELECT sourceid FROM payroll.payrolldraftlines "
            "WHERE draftlineid = :lid AND companyid = :cid"
        ),
        {"lid": draft_line_id, "cid": company_id},
    )
    _sp_row = _sp_check.mappings().first()
    if _sp_row and (_sp_row.get("sourceid") or "").startswith("STATUS_PAYMENT:"):
        raise HTTPException(
            status_code=422,
            detail=(
                "Status payment lines are managed automatically. "
                "Update the status key configuration or driver rates instead."
            ),
        )

    as_of_date: date = line.work_date if line.work_date is not None else period.start_date
    canonical_existing_lt: str = _LEGACY_TO_CANONICAL.get(line.line_type, line.line_type)

    # Determine void-only before validation: void cleanup must remain possible
    # even when the live PayItem was retired after the line was created.
    # Meaningful edits (quantity / rate / notes / NMR change) re-validate using
    # the period snapshot so snapshot-authorised items stay usable.
    is_void_only = (
        data.status == "Void"
        and data.quantity is None
        and data.rate_amount is None
        and data.notes is None
        and data.needs_manager_review is None
    )

    lt_info: _LineTypeInfo | None = None
    if not is_void_only:
        # CP-2C: pass period_id so snapshot-authorised items (e.g. retired after
        # period creation) remain editable for this period.
        lt_info = await _validate_line_type(
            canonical_existing_lt, period.branch_id, company_id, as_of_date, db,
            period_id=period.payroll_period_id,
        )

    # M13c: OrdinalTier items require a positive integer quantity.
    if lt_info is not None and lt_info.rate_behavior == "OrdinalTier" and data.quantity is not None:
        if data.quantity <= 0 or data.quantity % 1 != 0:
            raise HTTPException(
                status_code=422,
                detail=(
                    "OrdinalTier items require a positive integer quantity; "
                    "fractional or zero quantities are not allowed."
                ),
            )

    # Phase 4C: block manual rate overrides for PerUnit daily lines on update.
    if lt_info is not None and lt_info.rate_behavior == "PerUnit" and data.rate_amount is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Manual rate overrides are not allowed. "
                "Update the driver's rate in Pay Rates."
            ),
        )

    # Driver eligibility guard for non-void updates.
    # Voiding a stale line (data.status == 'Void', nothing else) is always
    # permitted — it is the recovery action for ineligible lines.
    # For any other meaningful change (quantity / rate / notes / NMR), verify
    # that the line's existing driver/work_date combination is still eligible.
    # CP-2E: use snapshot-based eligibility when available; legacy fallback otherwise.
    if not is_void_only and line.work_date is not None:
        await _assert_driver_eligible_for_workdate_via_snapshot(
            company_id, period.branch_id, period_id, line.driver_id, line.work_date, db
        )

    fields: dict[str, Any] = {}
    if data.quantity is not None:
        fields["quantity"] = data.quantity
    if data.rate_amount is not None:
        fields["rateamount"] = data.rate_amount
    if data.notes is not None:
        fields["notes"] = data.notes
    if data.status is not None:
        fields["status"] = data.status
    if data.needs_manager_review is not None:
        fields["needsmanagerreview"] = data.needs_manager_review

    # CP-2F: Draft (Prepared) periods keep NULL financial fields — skip calculation.
    # Apply on every non-void Draft edit regardless of which fields changed.
    if period.status == "Draft" and not is_void_only:
        fields["calculatedamount"] = None
        fields["rateamount"] = None
        fields["needsmanagerreview"] = False

    # M13b: re-compute calculatedamount when quantity or rate_amount changes.
    elif data.quantity is not None or data.rate_amount is not None:
        new_qty  = data.quantity    if data.quantity    is not None else line.quantity
        new_rate = data.rate_amount if data.rate_amount is not None else line.rate_amount
        _cr_upd = await _compute_calculated_amount(
            rate_behavior=lt_info.rate_behavior,
            rate_code=lt_info.rate_code,
            quantity=new_qty,
            rate_amount_override=new_rate,
            driver_id=line.driver_id,
            company_id=company_id,
            as_of_date=as_of_date,
            db=db,
        )
        calc_amount = _cr_upd.calculated_amount
        flag_review = _cr_upd.needs_manager_review
        # Always write calculatedamount (even None) to clear stale values.
        fields["calculatedamount"] = calc_amount

        # Issue 3 fix: if the calculation still requires review (no approved rate
        # found) and the caller is explicitly trying to clear the flag, refuse.
        # Allowing this would create a PerUnit line with NULL calc and no review flag
        # that would silently finalize as zero via COALESCE(NULL, qty * 0).
        if flag_review and data.needs_manager_review is False:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Cannot clear manager review flag: the calculation engine could not "
                    "resolve an amount for this line (no approved driver rate found). "
                    "Set up an approved rate for this driver first, then clear the flag."
                ),
            )

        # Auto-flag for review only when not explicitly set by caller.
        if flag_review and data.needs_manager_review is None:
            fields.setdefault("needsmanagerreview", True)

        # Auto-clear the review flag when the calculation resolves successfully
        # and the caller did not explicitly control the flag.  This mirrors the
        # INSERT path: if the engine can now compute an amount, the line no longer
        # needs manager attention for a missing rate.
        if not flag_review and data.needs_manager_review is None:
            fields["needsmanagerreview"] = False

    elif data.needs_manager_review is False:
        # M13b/M13c fix: caller is clearing the review flag without updating qty/rate.
        # For OrdinalTier / RangeBracket / RangeProgressive / Block, calculatedamount is
        # strictly required — rateamount is NOT a valid fallback because the calculation
        # engine must resolve the tier/block result.  NULL calc cannot be cleared.
        # For PerUnit, qty × rateamount is a valid finalization path, so clearing the
        # flag is allowed when rateamount is present even if calculatedamount is NULL.
        # For EnteredAmount / Fixed / Calculated / None, NULL calc is expected — allow.
        if lt_info.rate_behavior in _CALC_REQUIRED_BEHAVIORS and line.calculated_amount is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Cannot clear manager review flag: this {lt_info.rate_behavior} line "
                    "requires a resolved calculation amount. "
                    "rateamount is not a valid fallback for tier/block calculations. "
                    "Update the quantity to trigger recalculation with an approved rate, "
                    "or void this line."
                ),
            )
        elif (
            lt_info.rate_behavior == "PerUnit"
            and line.calculated_amount is None
            and line.rate_amount is None
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Cannot clear manager review flag: this PerUnit line has no resolved "
                    "calculation amount and no rate_amount override. "
                    "Update the quantity to trigger recalculation with an approved rate, "
                    "or supply a manual rate_amount, or void this line."
                ),
            )

    # CP-2D1: validate DailyStatus code before any DraftLine mutation.
    # Policy for direct update_draft_line: always require an active StatusKey when
    # notes is being changed, regardless of whether the new code matches the existing
    # selection. The deactivated-bypass is only available via save_day_grid.
    # Blank notes on update is treated as clearing the status (returns None, no raise).
    _direct_upd_sk_row: dict | None = None
    if canonical_existing_lt == "DailyStatus" and data.notes is not None and not is_void_only:
        _direct_upd_sk_row = await _validate_status_key(
            data.notes, company_id, period.branch_id, db,
        )
        # _validate_status_key returns None for blank/empty (clear operation — allowed).
        # It raises 422 for invalid or inactive codes.

    if fields:
        # CP-0A: Lock custom PayItem catalog row before period lock (same order as
        # deletion path) to prevent the zero-to-meaningful race: deletion reads zero
        # usage, update changes a line to meaningful, deletion physically deletes.
        # CP-2C: pass period_id so snapshot-authorised items bypass live status check.
        await _lock_pay_item_for_source_write(canonical_existing_lt, company_id, db,
                                              period_id=period.payroll_period_id)
        # CP-0A: Recheck period status under a row-level lock before writing.
        await _lock_period_for_mutation(period_id, company_id, db)
        set_clause = ", ".join(f"{col} = :{col}" for col in fields)
        await db.execute(
            text(
                f"UPDATE payroll.payrolldraftlines SET {set_clause} "
                "WHERE draftlineid = :line_id "
                "  AND payrollperiodid = :period_id AND companyid = :company_id"
            ),
            {**fields, "line_id": draft_line_id, "period_id": period_id, "company_id": company_id},
        )
        await _write_line_audit(
            db,
            company_id=company_id,
            branch_id=period.branch_id,
            user_id=user_id,
            line_id=draft_line_id,
            action_code="DRAFT_LINE_UPDATED",
            new_value={k: (float(v) if isinstance(v, Decimal) else v) for k, v in fields.items()},
        )
        if canonical_existing_lt not in _INFORMATIONAL_ONLY:
            before_state = {
                "line_type": line.line_type, "line_scope": line.line_scope,
                "quantity": line.quantity, "rate_amount": line.rate_amount,
                "calculated_amount": line.calculated_amount, "source_type": line.source_type,
                "status": line.status, "notes": line.notes,
            }
            await _capture_source_evidence(
                company_id=company_id, branch_id=period.branch_id, period_id=period_id,
                user_id=user_id, line_id=draft_line_id, action_code="SOURCE_UPDATED", db=db,
                before_state=before_state, after_state={**before_state, **fields},
                driver_id=line.driver_id, work_date=line.work_date, line_type=line.line_type,
            )

        # CP-2D1: dual-write canonical entry-state for informational lines.
        # DailyStatus: notes was validated pre-mutation; use statuskeyid from that row.
        # DailyNote: plain text, no StatusKey involved.
        if line.work_date is not None and canonical_existing_lt in _INFORMATIONAL_ONLY:
            if canonical_existing_lt == "DailyStatus":
                # If notes changed: _direct_upd_sk_row holds the validated result.
                # If notes not in fields (no change): resolve via _resolve_status_key_id
                # so the canonical row stays in sync with the unchanged existing code.
                if "notes" in fields:
                    sk_id = _direct_upd_sk_row["statuskeyid"] if _direct_upd_sk_row else None
                else:
                    existing_code = line.notes
                    sk_id = (
                        await _resolve_status_key_id(company_id, period.branch_id, existing_code, db)
                        if existing_code
                        else None
                    )
                await _upsert_entry_state(
                    company_id, period.branch_id, period_id,
                    line.driver_id, line.work_date, user_id, db,
                    status_key_id=sk_id,
                    note_text=None,
                    set_status=True,
                    set_note=False,
                )
            else:  # DailyNote
                new_note = fields.get("notes", line.notes)
                await _upsert_entry_state(
                    company_id, period.branch_id, period_id,
                    line.driver_id, line.work_date, user_id, db,
                    status_key_id=None,
                    note_text=new_note or None,
                    set_status=False,
                    set_note=True,
                )

    return await _get_line_by_id(draft_line_id, company_id, db)


# ---------------------------------------------------------------------------
# Void a draft line (soft-delete)
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

    # CP-2F: Draft (Prepared) periods allow voiding daily source lines only.
    if period.status == "Draft":
        pass  # allowed for daily source lines — checked after line is loaded
    elif period.status in _WRITE_BLOCKED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot void lines on a period with status '{period.status}'.",
        )

    # Permission gate: voiding an entry requires payroll.entry
    await _check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    line = await _get_line_by_id(draft_line_id, company_id, db)
    if line.period_id != period_id:
        raise HTTPException(status_code=404, detail="Draft line not found in this period.")
    if line.status == "Void":
        return  # Idempotent

    # CP-2F: For Draft periods, only daily source lines may be voided.
    if period.status == "Draft":
        if line.line_scope != "Daily" or line.source_type == "System":
            raise HTTPException(
                status_code=422,
                detail="Only daily source lines can be voided on a Prepared (Draft) period.",
            )

    # CP-2D2: guard — STATUS_PAYMENT lines are managed automatically
    _sp_void_check = await db.execute(
        text(
            "SELECT sourceid FROM payroll.payrolldraftlines "
            "WHERE draftlineid = :lid AND companyid = :cid"
        ),
        {"lid": draft_line_id, "cid": company_id},
    )
    _sp_void_row = _sp_void_check.mappings().first()
    if _sp_void_row and (_sp_void_row.get("sourceid") or "").startswith("STATUS_PAYMENT:"):
        raise HTTPException(
            status_code=422,
            detail=(
                "Status payment lines are managed automatically. "
                "Update the status key configuration or driver rates instead."
            ),
        )

    # CP-0A: Recheck period status under a row-level lock before writing.
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
        db,
        company_id=company_id,
        branch_id=period.branch_id,
        user_id=user_id,
        line_id=draft_line_id,
        action_code="DRAFT_LINE_VOIDED",
        old_value={"status": "Active"},
        new_value={"status": "Void"},
    )
    if line.line_type not in _INFORMATIONAL_ONLY:
        await _capture_source_evidence(
            company_id=company_id, branch_id=period.branch_id, period_id=period_id,
            user_id=user_id, line_id=draft_line_id, action_code="SOURCE_VOIDED", db=db,
            before_state={
                "line_type": line.line_type, "line_scope": line.line_scope,
                "quantity": line.quantity, "rate_amount": line.rate_amount,
                "calculated_amount": line.calculated_amount, "source_type": line.source_type,
                "status": line.status, "notes": line.notes,
            },
            after_state={"status": "Void"}, driver_id=line.driver_id, work_date=line.work_date,
            line_type=line.line_type,
        )

    # CP-2D1: clear canonical entry-state field for informational lines.
    if line.line_type in _INFORMATIONAL_ONLY and line.work_date is not None:
        await _void_entry_state_field(
            period_id, company_id, line.driver_id, line.work_date, user_id, db,
            clear_status=(line.line_type == "DailyStatus"),
            clear_note=(line.line_type == "DailyNote"),
        )


# ---------------------------------------------------------------------------
# Internal: fetch a single line by its primary key
# ---------------------------------------------------------------------------

async def _get_line_by_id(
    draft_line_id: int,
    company_id: int,
    db: AsyncConnection,
) -> DraftLineSummary:
    result = await db.execute(
        text(f"{_LINE_SELECT} WHERE dl.draftlineid = :lid AND dl.companyid = :cid"),
        {"lid": draft_line_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Draft line not found.")
    return _line_row_to_summary(row)


# ===========================================================================
# Finalization — Approved → Locked
# ===========================================================================

# ---------------------------------------------------------------------------
# Phase 7: Shared finalization validator
# Used by both finalize_period and get_finalization_preview so that preview
# surfaces exactly the same blockers that finalize_period would enforce.
# Does NOT check: period status, permissions, empty-period, NMR/zero-calc,
# or min/max cross-rule guards (those depend on per-path state).
# ---------------------------------------------------------------------------

async def _validate_period_can_finalize(
    period_id: int,
    company_id: int,
    branch_id: int,
    period_start: "date",
    period_end: "date",
    db: AsyncConnection,
) -> list[str]:
    """
    Run shared pre-finalization checks used by both finalize_period and
    get_finalization_preview.  Returns a list of human-readable blocker
    strings (empty list = no blockers found).

    Checks (in order):
      1. Duplicate active Daily draft lines for the same (driver, date, type).
      2. Driver eligibility for Daily lines (per-date window).
      3. Driver eligibility for Period Pay lines (period overlap window).
      4. Contaminated/foreign RateType used by any rate-driven draft line.

    The messages are intentionally kept identical to the strings previously
    raised as individual HTTPException 422 details in finalize_period so that
    existing test assertions (e.g. "duplicate" in detail.lower()) continue to
    pass unchanged.
    """
    blockers: list[str] = []

    # ── 1. Duplicate active Daily draft lines ─────────────────────────────────
    dup_result = await db.execute(
        text("""
            SELECT driverid, workdate, linetype, COUNT(*) AS cnt
            FROM   payroll.payrolldraftlines
            WHERE  payrollperiodid = :period_id
              AND  companyid       = :company_id
              AND  linescope       = 'Daily'
              AND  status         != 'Void'
            GROUP BY driverid, workdate, linetype
            HAVING COUNT(*) > 1
            LIMIT 5
        """),
        {"period_id": period_id, "company_id": company_id},
    )
    dup_rows = dup_result.mappings().all()
    if dup_rows:
        examples = "; ".join(
            f"driver {r['driverid']} {r['workdate']} {r['linetype']} ×{r['cnt']}"
            for r in dup_rows
        )
        blockers.append(
            f"Cannot finalize: duplicate active Daily draft lines detected "
            f"({examples}). Void the extra lines before finalizing."
        )

    # ── 2. Driver eligibility — Daily lines ───────────────────────────────────
    # CP-2E: use snapshot-based eligibility for snapshotted periods to correctly
    # handle IncludedByExistingData, TerminatedHistorical, and Transferred drivers.
    # Legacy live-query path retained for periods without a snapshot.
    _has_snapshot = await _period_has_driver_eligibility_snapshot(period_id, db)
    if _has_snapshot:
        elig_daily_result = await db.execute(
            text("""
                SELECT dl.draftlineid, dl.driverid, dl.workdate, dl.linetype
                FROM   payroll.payrolldraftlines dl
                WHERE  dl.payrollperiodid = :period_id
                  AND  dl.companyid       = :company_id
                  AND  dl.status         != 'Void'
                  AND  dl.linescope       = 'Daily'
                  AND  NOT EXISTS (
                           SELECT 1
                           FROM   payroll.payrollperioddrivereligibility ppde
                           WHERE  ppde.payrollperiodid = dl.payrollperiodid
                             AND  ppde.driverid        = dl.driverid
                             AND  ppde.iseligibleforperiod = TRUE
                             -- CP-2E: a DraftLine that already exists proves existing source
                             -- on that exact date for any reason code (including generated-row
                             -- drivers outside their date window). Pass if in snapshot at all.
                       )
                LIMIT 5
            """),
            {"period_id": period_id, "company_id": company_id},
        )
    else:
        elig_daily_result = await db.execute(
            text("""
                SELECT dl.draftlineid, dl.driverid, dl.workdate, dl.linetype
                FROM   payroll.payrolldraftlines dl
                WHERE  dl.payrollperiodid = :period_id
                  AND  dl.companyid       = :company_id
                  AND  dl.status         != 'Void'
                  AND  dl.linescope       = 'Daily'
                  AND  NOT EXISTS (
                           SELECT 1
                           FROM   core.drivers   d
                           JOIN   core.employees e ON e.employeeid = d.employeeid
                           WHERE  d.driverid         = dl.driverid
                             AND  d.companyid        = :company_id
                             AND  d.branchid         = :branch_id
                             AND  e.employmentstatus = 'Active'
                             AND  (
                                      d.driverstatus = 'Active'
                                   OR (d.driverstatus = 'Transferred'
                                       AND d.effectiveto IS NOT NULL
                                       AND d.effectiveto >= dl.workdate)
                                  )
                             AND  (e.hiredate IS NULL OR e.hiredate <= dl.workdate)
                             AND  (e.terminationdate IS NULL OR e.terminationdate >= dl.workdate)
                             AND  (d.effectivefrom IS NULL OR d.effectivefrom <= dl.workdate)
                             AND  (d.effectiveto   IS NULL OR d.effectiveto   >= dl.workdate)
                       )
                LIMIT 5
            """),
            {"period_id": period_id, "company_id": company_id, "branch_id": branch_id},
        )
    elig_daily_rows = elig_daily_result.mappings().all()
    if elig_daily_rows:
        examples = "; ".join(
            f"driver {r['driverid']} {r['workdate']} {r['linetype']}"
            for r in elig_daily_rows
        )
        blockers.append(
            f"Cannot finalize: {len(elig_daily_rows)} Daily draft line(s) reference "
            f"driver/date combinations that are no longer eligible "
            f"({examples}). Void these lines before finalizing."
        )

    # ── 3. Driver eligibility — Period Pay lines ──────────────────────────────
    if _has_snapshot:
        elig_period_result = await db.execute(
            text("""
                SELECT dl.draftlineid, dl.driverid, dl.linetype
                FROM   payroll.payrolldraftlines dl
                WHERE  dl.payrollperiodid = :period_id
                  AND  dl.companyid       = :company_id
                  AND  dl.status         != 'Void'
                  AND  dl.linescope       = 'Period'
                  AND  NOT EXISTS (
                           SELECT 1
                           FROM   payroll.payrollperioddrivereligibility ppde
                           WHERE  ppde.payrollperiodid = dl.payrollperiodid
                             AND  ppde.driverid        = dl.driverid
                             AND  ppde.iseligibleforperiod = TRUE
                       )
                LIMIT 5
            """),
            {"period_id": period_id, "company_id": company_id},
        )
    else:
        elig_period_result = await db.execute(
            text("""
                SELECT dl.draftlineid, dl.driverid, dl.linetype
                FROM   payroll.payrolldraftlines dl
                WHERE  dl.payrollperiodid = :period_id
                  AND  dl.companyid       = :company_id
                  AND  dl.status         != 'Void'
                  AND  dl.linescope       = 'Period'
                  AND  NOT EXISTS (
                           SELECT 1
                           FROM   core.drivers   d
                           JOIN   core.employees e ON e.employeeid = d.employeeid
                           WHERE  d.driverid         = dl.driverid
                             AND  d.companyid        = :company_id
                             AND  d.branchid         = :branch_id
                             AND  e.employmentstatus = 'Active'
                             AND  d.driverstatus     = 'Active'
                             AND  (e.hiredate IS NULL OR e.hiredate <= :period_end)
                             AND  (e.terminationdate IS NULL OR e.terminationdate >= :period_start)
                             AND  (d.effectivefrom IS NULL OR d.effectivefrom <= :period_end)
                             AND  (d.effectiveto   IS NULL OR d.effectiveto   >= :period_start)
                       )
                LIMIT 5
            """),
            {
                "period_id":    period_id,
                "company_id":   company_id,
                "branch_id":    branch_id,
                "period_start": period_start,
                "period_end":   period_end,
            },
        )
    elig_period_rows = elig_period_result.mappings().all()
    if elig_period_rows:
        examples = "; ".join(
            f"driver {r['driverid']} {r['linetype']}"
            for r in elig_period_rows
        )
        blockers.append(
            f"Cannot finalize: {len(elig_period_rows)} Period Pay draft line(s) reference "
            f"ineligible drivers ({examples}). Void these lines before finalizing."
        )

    # ── 4. Contaminated / foreign RateType ────────────────────────────────────
    # (unchanged from Phase 7)
    contaminated_result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT rt.ratetypeid) AS cnt
            FROM   payroll.payrolldraftlines dl
            JOIN   payroll.payitems pi
                   ON pi.payitemcode = dl.linetype
                  AND (pi.companyid IS NULL OR pi.companyid = :company_id)
                  AND pi.status      != 'Retired'
                  AND pi.requiresrate = TRUE
            JOIN   payroll.payitemratetypemap pirm
                   ON pirm.payitemid = pi.payitemid AND pirm.status = 'Active'
            JOIN   payroll.ratetypes rt
                   ON rt.ratetypeid = pirm.ratetypeid AND rt.isactive = TRUE
            WHERE  dl.payrollperiodid = :period_id
              AND  dl.companyid       = :company_id
              AND  dl.status         != 'Void'
              AND NOT (rt.companyid IS NULL OR rt.companyid = :company_id)
        """),
        {"period_id": period_id, "company_id": company_id},
    )
    contaminated_count = int(contaminated_result.scalar_one())
    if contaminated_count > 0:
        blockers.append(
            f"Cannot finalize: {contaminated_count} rate type(s) used by draft lines "
            "in this period are not valid for this company (foreign-owned, contaminated, "
            "or orphaned). Investigate and void or correct the affected draft lines."
        )

    # ── 5. Unresolvable rate type mapping (Phase 8 — fail-closed) ────────────
    # Finds non-void, rate-dependent draft lines whose PayItem has no active
    # PayItemRateTypeMap entry.  These lines cannot be correctly calculated
    # because their rate_code is unknown — the rate behavior is unresolvable.
    # NOTE: such lines will also be caught by the NMR blocker (Blocker 2 in
    # preview / Step 1.8 in finalize) because _compute_calculated_amount
    # returns NMR=True when rate_code is None.  This check provides the
    # specific "configure the mapping" message that the generic NMR message
    # does not.
    unresolvable_result = await db.execute(
        text("""
            SELECT dl.draftlineid, dl.linetype, pi.ratebehavior
            FROM   payroll.payrolldraftlines dl
            JOIN   payroll.payitems pi
                   ON pi.payitemcode = dl.linetype
                  AND (pi.companyid IS NULL OR pi.companyid = :company_id)
                  AND pi.status     != 'Retired'
            WHERE  dl.payrollperiodid = :period_id
              AND  dl.companyid       = :company_id
              AND  dl.status         != 'Void'
              AND  pi.ratebehavior   IN ('PerUnit', 'OrdinalTier',
                                         'RangeBracket', 'RangeProgressive', 'Block')
              AND  NOT EXISTS (
                       SELECT 1
                       FROM   payroll.payitemratetypemap pirtm
                       JOIN   payroll.ratetypes rt
                              ON rt.ratetypeid = pirtm.ratetypeid
                       WHERE  pirtm.payitemid = pi.payitemid
                         AND  pirtm.status    = 'Active'
                         AND  rt.isactive     = TRUE
                   )
            LIMIT 5
        """),
        {"period_id": period_id, "company_id": company_id},
    )
    unresolvable_rows = unresolvable_result.mappings().all()
    if unresolvable_rows:
        examples = "; ".join(
            f"line {r['draftlineid']} ({r['linetype']}, {r['ratebehavior']})"
            for r in unresolvable_rows
        )
        cnt = len(unresolvable_rows)
        blockers.append(
            f"Cannot finalize: {cnt} rate-dependent draft line(s) have no pay item "
            f"rate type mapping configured ({examples}). "
            "Rate behavior could not be resolved — configure the pay item rate mapping "
            "or void these lines before finalizing."
        )

    return blockers


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
# CP-3A / CP-5 — Virtual rate refresh helper (read-only)
# ---------------------------------------------------------------------------

async def _compute_draft_line_preview_amounts(
    period_id: int,
    company_id: int,
    period_start_date: date,
    db: AsyncConnection,
) -> "dict[int, tuple[Decimal | None, bool]]":
    """
    Read-only virtual equivalent of _refresh_draft_calculations.

    Computes what (calculatedamount, needsmanagerreview) WOULD be after a
    real refresh for every rate-dependent draft line that passes the
    manager-NMR guard — without writing anything to the database.

    Returns {draftlineid: (refreshed_calc, refreshed_review)} for each
    eligible line.  Lines excluded by the manager-NMR guard are absent from
    the dict; callers must fall back to the stored values for those.

    Guarantees:
      • No UPDATE / INSERT / DELETE is executed.
      • Safe to call on any period status — purely read-only.
    """
    lines_result = await db.execute(
        text("""
            SELECT draftlineid, driverid, linetype, workdate,
                   quantity, rateamount, calculatedamount, needsmanagerreview
            FROM   payroll.payrolldraftlines
            WHERE  payrollperiodid = :pid
              AND  companyid       = :cid
              AND  status         != 'Void'
              AND  linetype       NOT IN ('DailyStatus', 'DailyNote')
        """),
        {"pid": period_id, "cid": company_id},
    )
    rows = list(lines_result.mappings().all())
    if not rows:
        return {}

    lt_info_cache: "dict[str, _LineTypeInfo | None]" = {}

    async def _get_lt_info(canonical: str) -> "_LineTypeInfo | None":
        if canonical in lt_info_cache:
            return lt_info_cache[canonical]
        pi_result = await db.execute(
            text("""
                SELECT pi.ratebehavior,
                       (
                           SELECT rt.ratecode
                           FROM   payroll.payitemratetypemap pirtm
                           JOIN   payroll.ratetypes rt ON rt.ratetypeid = pirtm.ratetypeid
                           WHERE  pirtm.payitemid = pi.payitemid
                             AND  pirtm.status    = 'Active'
                             AND  rt.isactive     = TRUE
                           ORDER BY pirtm.isprimary DESC
                           LIMIT 1
                       ) AS rate_code
                FROM   payroll.payitems pi
                WHERE  pi.payitemcode = :code
                  AND  (pi.companyid IS NULL OR pi.companyid = :cid)
                  AND  pi.status    != 'Retired'
                LIMIT 1
            """),
            {"code": canonical, "cid": company_id},
        )
        pi_row = pi_result.mappings().first()
        if pi_row is None:
            lt_info_cache[canonical] = None
            return None
        info = _LineTypeInfo(
            rate_behavior=pi_row["ratebehavior"],
            rate_code=pi_row["rate_code"],
        )
        lt_info_cache[canonical] = info
        return info

    result: "dict[int, tuple[Decimal | None, bool]]" = {}
    for row in rows:
        canonical = _LEGACY_TO_CANONICAL.get(row["linetype"], row["linetype"])
        lt_info = await _get_lt_info(canonical)
        if lt_info is None or lt_info.rate_behavior not in _RATE_DEPENDENT_BEHAVIORS:
            continue  # not rate-dependent — stored value is authoritative

        old_calc     = Decimal(str(row["calculatedamount"])) if row["calculatedamount"] is not None else None
        old_review   = bool(row["needsmanagerreview"])
        old_rate_ovr = Decimal(str(row["rateamount"])) if row["rateamount"] is not None else None

        # Same manager-NMR guard as _refresh_draft_calculations:
        # skip if NMR=True AND (calc IS NOT NULL OR rate_amount IS NOT NULL)
        if old_review and (old_calc is not None or old_rate_ovr is not None):
            continue  # manager-flagged with a resolvable path — honour stored values

        as_of: date = row["workdate"] if row["workdate"] is not None else period_start_date
        qty = Decimal(str(row["quantity"])) if row["quantity"] is not None else Decimal("0")

        _cr_prev = await _compute_calculated_amount(
            rate_behavior=lt_info.rate_behavior,
            rate_code=lt_info.rate_code,
            quantity=qty,
            rate_amount_override=old_rate_ovr,
            driver_id=row["driverid"],
            company_id=company_id,
            as_of_date=as_of,
            db=db,
        )
        result[int(row["draftlineid"])] = _cr_prev

    return result


# ---------------------------------------------------------------------------
# CP-3A — Finalization Preview (read-only)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CP-4F — approved immutable snapshot finalization
# ---------------------------------------------------------------------------

def _snapshot_finalization_error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"{code}: {message}")


async def _load_approved_snapshot_packet(
    *, period_id: int, company_id: int, branch_id: int,
    db: AsyncConnection, lock_review_item: bool = False,
) -> dict[str, Any]:
    """Load the one immutable packet authorized by an Approved PeriodApproval."""
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
            "the approved PeriodApproval is historical and has no immutable calculation snapshot.",
        )
    snapshot = (await db.execute(text("""
        SELECT payrollcalculationsnapshotid, companyid, branchid, payrollperiodid,
               revisionnumber, snapshothash, totalexpectedpay, createdatutc
        FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid = :snapshot_id
          AND companyid = :cid AND branchid = :bid
    """), {"snapshot_id": snapshot_id, "cid": company_id, "bid": branch_id})).mappings().first()
    if snapshot is None or int(snapshot["payrollperiodid"]) != period_id:
        raise _snapshot_finalization_error(
            "APPROVED_SNAPSHOT_INTEGRITY_ERROR",
            "the approved review item does not reference a snapshot for this exact period.",
        )
    totals = (await db.execute(text("""
        SELECT dt.payrollcalculationdrivertotalid, dt.driverid,
               dt.drivercodesnapshot, dt.drivernamesnapshot,
               dt.dailypay, dt.statuspay, dt.periodpay, dt.minimumadjustment,
               dt.maximumadjustment, dt.bonustotal, dt.expectedpay
        FROM payroll.payrollcalculationdrivertotals dt
        JOIN core.drivers d ON d.driverid = dt.driverid
            AND d.companyid = dt.companyid AND d.branchid = dt.branchid
        WHERE dt.payrollcalculationsnapshotid = :snapshot_id
          AND dt.companyid = :cid AND dt.branchid = :bid
        ORDER BY dt.driverid, dt.payrollcalculationdrivertotalid
    """), {"snapshot_id": snapshot_id, "cid": company_id, "bid": branch_id})).mappings().all()
    lines = (await db.execute(text("""
        SELECT sl.payrollcalculationsnapshotlineid,
               sl.payrollcalculationdrivertotalid, dt.driverid, sl.sourcetype,
               sl.sourceid, sl.linetype, sl.linescope, sl.workdate, sl.payitemid,
               sl.ratetypeid, sl.driverrateid, sl.bonuseventid, sl.quantity,
               sl.resolvedrateamount, sl.calculatedamount, sl.sourceevidencejsonb
        FROM payroll.payrollcalculationsnapshotlines sl
        JOIN payroll.payrollcalculationdrivertotals dt
          ON dt.payrollcalculationdrivertotalid = sl.payrollcalculationdrivertotalid
        WHERE dt.payrollcalculationsnapshotid = :snapshot_id
          AND dt.companyid = :cid AND dt.branchid = :bid
        ORDER BY dt.driverid, sl.payrollcalculationsnapshotlineid
    """), {"snapshot_id": snapshot_id, "cid": company_id, "bid": branch_id})).mappings().all()
    return {"review": review, "snapshot": snapshot, "totals": totals, "lines": lines}


def _reconcile_approved_snapshot_packet(packet: dict[str, Any]) -> None:
    """Check persisted packet arithmetic only; never consult mutable sources."""
    snapshot, totals, lines = packet["snapshot"], packet["totals"], packet["lines"]
    header_total = Decimal(str(snapshot["totalexpectedpay"]))
    totals_total = sum((Decimal(str(row["expectedpay"])) for row in totals), Decimal("0"))
    if header_total != totals_total:
        raise _snapshot_finalization_error("APPROVED_SNAPSHOT_INTEGRITY_ERROR", "snapshot header total does not reconcile with driver totals.")
    line_totals: dict[int, Decimal] = {}
    for line in lines:
        key = int(line["payrollcalculationdrivertotalid"])
        line_totals[key] = line_totals.get(key, Decimal("0")) + Decimal(str(line["calculatedamount"]))
    for total in totals:
        key = int(total["payrollcalculationdrivertotalid"])
        components = sum((Decimal(str(total[column])) for column in (
            "dailypay", "statuspay", "periodpay", "minimumadjustment",
            "maximumadjustment", "bonustotal",
        )), Decimal("0"))
        expected = Decimal(str(total["expectedpay"]))
        if components != expected or line_totals.get(key, Decimal("0")) != expected:
            raise _snapshot_finalization_error("APPROVED_SNAPSHOT_INTEGRITY_ERROR", "snapshot lines do not reconcile with a driver total.")


def _snapshot_line_draft_line_id(line: Any) -> int | None:
    if line["sourcetype"] != "DraftLine" or line["sourceid"] is None:
        return None
    try:
        return int(str(line["sourceid"]))
    except ValueError:
        return None


def _snapshot_line_provenance(packet: dict[str, Any], line: Any) -> str:
    snapshot = packet["snapshot"]
    return json.dumps({
        "payroll_calculation_snapshot_id": int(snapshot["payrollcalculationsnapshotid"]),
        "revision_number": int(snapshot["revisionnumber"]),
        "snapshot_hash": snapshot["snapshothash"],
        "snapshot_line_id": int(line["payrollcalculationsnapshotlineid"]),
        "source_type": line["sourcetype"], "source_id": line["sourceid"],
        "source_evidence": line["sourceevidencejsonb"] or {},
    }, default=str)


async def _project_approved_snapshot_final_lines(
    *, packet: dict[str, Any], period_id: int, company_id: int, branch_id: int,
    user_id: int, db: AsyncConnection,
) -> None:
    await db.execute(text("SELECT set_config('app.allow_payroll_final_line_insert', 'true', true)"))
    for line in packet["lines"]:
        evidence = line["sourceevidencejsonb"] or {}
        source_type = str(line["sourcetype"])
        rate_behavior = evidence.get("RateBehavior")
        if source_type == "System":
            rate_behavior = "System"
        elif source_type == "BonusEvent":
            rate_behavior = "Fixed"
        await db.execute(text("""
            INSERT INTO payroll.payrollfinallines
                (companyid, branchid, payrollperiodid, draftlineid, bonuseventid,
                 driverid, workdate, linetype, linescope, quantity, rateamount,
                 finalamount, sourcetype, sourceid, approvedbyuserid, approvedatutc,
                 lockedatutc, notes, payitemid, ratebehavior, ratetypeid,
                 driverrateid, resolvedrateamount, sourcesnapshot)
            VALUES
                (:cid, :bid, :period_id, :draft_line_id, :bonus_event_id,
                 :driver_id, :work_date, :line_type, :line_scope, :quantity,
                 :rate_amount, :final_amount, :source_type, :source_id,
                 :approved_by, NOW(), NOW(), :notes, :pay_item_id,
                 :rate_behavior, :rate_type_id, :driver_rate_id,
                 :resolved_rate_amount, CAST(:source_snapshot AS jsonb))
        """), {
            "cid": company_id, "bid": branch_id, "period_id": period_id,
            "draft_line_id": _snapshot_line_draft_line_id(line),
            "bonus_event_id": line["bonuseventid"], "driver_id": line["driverid"],
            "work_date": line["workdate"], "line_type": line["linetype"],
            "line_scope": line["linescope"] or "Period", "quantity": line["quantity"] or Decimal("0"),
            "rate_amount": line["resolvedrateamount"], "final_amount": line["calculatedamount"],
            "source_type": source_type, "source_id": line["sourceid"], "approved_by": user_id,
            "notes": evidence.get("Notes"), "pay_item_id": line["payitemid"],
            "rate_behavior": rate_behavior, "rate_type_id": line["ratetypeid"],
            "driver_rate_id": line["driverrateid"], "resolved_rate_amount": line["resolvedrateamount"],
            "source_snapshot": _snapshot_line_provenance(packet, line),
        })


async def finalize_period(period_id: int, company_id: int, user_id: int, db: AsyncConnection) -> PeriodSummary:
    """Project the exact approved immutable packet into FinalLines and lock the period."""
    if await _get_oda_own_driver_id(company_id, user_id, db) is not None:
        raise HTTPException(status_code=403, detail="Current Payroll is not accessible to driver-role users.")
    period = await get_period_by_id(company_id, user_id, period_id, db)
    if period.status != "Approved":
        raise HTTPException(status_code=422, detail=f"Only Approved periods can be finalized (current status: '{period.status}').")
    await _check_permission(company_id, user_id, period.branch_id, "payroll.finalize", db)
    await _acquire_branch_workflow_lock(company_id, period.branch_id, db)
    locked = (await db.execute(text("""
        SELECT payrollperiodid FROM payroll.payrollperiods
        WHERE payrollperiodid = :period_id AND companyid = :cid AND branchid = :bid AND status = 'Approved'
        FOR UPDATE
    """), {"period_id": period_id, "cid": company_id, "bid": period.branch_id})).scalar_one_or_none()
    if locked is None:
        raise HTTPException(status_code=422, detail="Period could not be claimed for finalization — its status may have changed concurrently.")
    packet = await _load_approved_snapshot_packet(period_id=period_id, company_id=company_id, branch_id=period.branch_id, db=db, lock_review_item=True)
    _reconcile_approved_snapshot_packet(packet)
    claimed = await db.execute(text("""
        UPDATE payroll.payrollperiods SET status = 'Locked', lockedbyuserid = :locker, lockedatutc = NOW()
        WHERE payrollperiodid = :period_id AND companyid = :cid AND status = 'Approved'
        RETURNING payrollperiodid
    """), {"locker": user_id, "period_id": period_id, "cid": company_id})
    if claimed.scalar_one_or_none() is None:
        raise HTTPException(status_code=422, detail="Period could not be claimed for finalization — its status may have changed concurrently.")
    await _project_approved_snapshot_final_lines(packet=packet, period_id=period_id, company_id=company_id, branch_id=period.branch_id, user_id=user_id, db=db)
    snapshot = packet["snapshot"]
    await capture_workflow_action_evidence(
        company_id=company_id,
        branch_id=period.branch_id,
        period_id=period_id,
        snapshot_id=int(snapshot["payrollcalculationsnapshotid"]),
        review_item_id=int(packet["review"]["reviewitemid"]),
        action_code="FINALIZED",
        user_id=user_id,
        required_permission_code="payroll.finalize",
        db=db,
    )
    await _write_finalization_audit(
        db, company_id=company_id, branch_id=period.branch_id, user_id=user_id,
        period_id=period_id, line_count=len(packet["lines"]),
        total_amount=Decimal(str(snapshot["totalexpectedpay"])),
        approved_review_item_id=int(packet["review"]["reviewitemid"]),
        snapshot_id=int(snapshot["payrollcalculationsnapshotid"]),
        revision_number=int(snapshot["revisionnumber"]), snapshot_hash=str(snapshot["snapshothash"]),
    )
    return await get_period_by_id(company_id, user_id, period_id, db)


async def get_finalization_preview(period_id: int, company_id: int, user_id: int, db: AsyncConnection) -> "FinalizationPreviewResponse":
    """Read the same immutable approved packet that finalization will project."""
    from app.payroll.schemas import BonusEventPreviewEntry, FinalizationPreviewDriverTotal, FinalizationPreviewLine, FinalizationPreviewResponse, FinalizationPreviewSysAdjustment
    if await _get_oda_own_driver_id(company_id, user_id, db) is not None:
        raise HTTPException(status_code=403, detail="Current Payroll is not accessible to driver-role users.")
    period = await get_period_by_id(company_id, user_id, period_id, db)
    if period.status != "Approved":
        raise HTTPException(status_code=422, detail=f"Finalization preview requires an Approved period. Current status: '{period.status}'.")
    await _check_permission(company_id, user_id, period.branch_id, "payroll.finalize", db)
    packet = await _load_approved_snapshot_packet(period_id=period_id, company_id=company_id, branch_id=period.branch_id, db=db)
    _reconcile_approved_snapshot_packet(packet)
    total_rows = {int(row["payrollcalculationdrivertotalid"]): row for row in packet["totals"]}
    lines, adjustments, bonuses = [], [], []
    for row in packet["lines"]:
        total = total_rows[int(row["payrollcalculationdrivertotalid"])]
        amount, evidence = Decimal(str(row["calculatedamount"])), row["sourceevidencejsonb"] or {}
        lines.append(FinalizationPreviewLine(
            draft_line_id=_snapshot_line_draft_line_id(row), source_key=f"snapshot-line:{row['payrollcalculationsnapshotlineid']}",
            driver_id=int(row["driverid"]), driver_name=total["drivernamesnapshot"], work_date=row["workdate"],
            line_type=row["linetype"], line_scope=row["linescope"] or "Period", quantity=row["quantity"],
            rate_amount=row["resolvedrateamount"], calculated_amount=amount, final_amount=amount,
            needs_manager_review=False, rate_behavior=evidence.get("RateBehavior"),
            driver_rate_id=row["driverrateid"], rate_type_id=row["ratetypeid"], resolved_rate_amount=row["resolvedrateamount"],
        ))
        normal_base = sum((Decimal(str(total[key])) for key in ("dailypay", "statuspay", "periodpay")), Decimal("0"))
        if row["linetype"] in {"SYS_MIN_TOPUP", "SYS_MAX_CAP"}:
            adjustments.append(FinalizationPreviewSysAdjustment(driver_id=int(row["driverid"]), driver_name=total["drivernamesnapshot"], adjustment_type=row["linetype"], gross_before=normal_base, adjustment_amount=amount, bonus_total=Decimal(str(total["bonustotal"])), final_pay=Decimal(str(total["expectedpay"]))))
        if row["sourcetype"] == "BonusEvent" and row["bonuseventid"] is not None:
            bonuses.append(BonusEventPreviewEntry(bonus_event_id=int(row["bonuseventid"]), driver_id=int(row["driverid"]), driver_name=total["drivernamesnapshot"], amount=amount, reason=evidence.get("Reason"), notes=evidence.get("Notes")))
    driver_totals = [FinalizationPreviewDriverTotal(
        driver_id=int(row["driverid"]), driver_name=row["drivernamesnapshot"],
        daily_pay=Decimal(str(row["dailypay"])), status_pay=Decimal(str(row["statuspay"])), period_pay=Decimal(str(row["periodpay"])),
        gross_pay=sum((Decimal(str(row[key])) for key in ("dailypay", "statuspay", "periodpay")), Decimal("0")),
        sys_adjustment=Decimal(str(row["minimumadjustment"])) + Decimal(str(row["maximumadjustment"])),
        bonus_total=Decimal(str(row["bonustotal"])), final_pay=Decimal(str(row["expectedpay"])),
        line_count=sum(1 for line in packet["lines"] if line["payrollcalculationdrivertotalid"] == row["payrollcalculationdrivertotalid"]),
    ) for row in packet["totals"]]
    non_bonus_non_system = [line for line in packet["lines"] if line["sourcetype"] not in {"BonusEvent", "System"}]
    return FinalizationPreviewResponse(
        period_id=period_id, period_name=period.period_name, period_status=period.status, branch_id=period.branch_id, branch_name=period.branch_name,
        can_finalize=True, blockers=[], warnings=[], driver_totals=driver_totals, sys_adjustments=adjustments, lines=lines, bonus_events=bonuses, bonus_event_count=len(bonuses),
        total_final_gross=Decimal(str(packet["snapshot"]["totalexpectedpay"])), draft_line_count=len(non_bonus_non_system), sys_adjustment_count=len(adjustments), final_line_count_estimate=len(packet["lines"]), driver_count=len(driver_totals),
    )


# ---------------------------------------------------------------------------
# CP-4B — Open/Returned live read-only calculation preview
# ---------------------------------------------------------------------------

# CP-4B fix (Codex P2): exact identity predicate for the persisted Status-
# payment compatibility projection DraftLine written by
# `_sync_status_payment_for_entry_state`. LineType cannot be hardcoded here
# (it is the mapped RateType's RateCode, which is data-driven per company/
# StatusRateColumn configuration) — the projection is instead uniquely
# identified by the combination of SourceType='System' and a SourceID that
# matches the exact 'STATUS_PAYMENT:{entry_state_id}:{status_key_id}:
# {status_rate_column_id}' format (three integer segments), not merely a
# SourceID text prefix. A prefix-only match could incorrectly exclude an
# unrelated line whose SourceID happens to start with the same text but has
# a different SourceType or a malformed/foreign suffix.
_STATUS_PAYMENT_PROJECTION_SQL = (
    "(dl.sourcetype = 'System' "
    "AND dl.sourceid ~ '^STATUS_PAYMENT:[0-9]+:[0-9]+:[0-9]+$')"
)


@dataclass(frozen=True)
class _CalculationPacketLine:
    """Persistence-grade result from the live CP-4B calculation assembly."""

    source_type: str
    source_id: str | None
    line_type: str
    line_scope: str | None
    work_date: date | None
    driver_id: int
    quantity: Decimal | None
    resolved_rate_amount: Decimal | None
    calculated_amount: Decimal | None
    needs_manager_review: bool
    blocker_reason: str | None
    pay_item_id: int | None = None
    rate_column_id: int | None = None
    rate_type_id: int | None = None
    driver_rate_id: int | None = None
    bonus_event_id: int | None = None
    source_evidence: dict[str, Any] | None = None
    snapshot_source_type: str | None = None
    snapshot_source_id: str | None = None
    snapshot_calculated_amount: Decimal | None = None


@dataclass(frozen=True)
class _CalculationPacketDriverTotal:
    driver_id: int
    driver_code: str | None
    driver_name: str | None
    daily_pay: Decimal
    status_pay: Decimal
    period_pay: Decimal
    minimum_adjustment: Decimal
    maximum_adjustment: Decimal
    bonus_total: Decimal
    expected_pay: Decimal
    needs_manager_review: bool
    blockers: list[str]
    lines: list[_CalculationPacketLine]


@dataclass(frozen=True)
class _LiveCalculationPacket:
    payroll_period_id: int
    company_id: int
    branch_id: int
    status: str
    blockers: list[str]
    warnings: list[str]
    drivers: list[_CalculationPacketDriverTotal]
    total_expected_pay: Decimal


def _is_retryable_transaction_failure(exc: DBAPIError) -> bool:
    """PostgreSQL transaction failures which are safe for the client to retry."""
    return getattr(exc.orig, "sqlstate", None) in {"40001", "40P01"}


async def _set_submit_transaction_isolation(db: AsyncConnection) -> None:
    """Set the submit/resubmit request transaction before any database read."""
    await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))



async def _load_active_bonus_events(
    period_id: int,
    company_id: int,
    db: AsyncConnection,
) -> list[Any]:
    """Return the canonical Active BonusEvent selection used by CP-4B/CP-4D."""
    result = await db.execute(
        text("""
            SELECT
                be.payrollbonuseventid,
                be.driverid,
                d.drivercode,
                e.fullname AS drivername,
                be.amount,
                be.reason,
                be.notes,
                be.datarevision,
                be.createdbyuserid,
                creator.displayname AS creatordisplaynamesnapshot,
                be.createdatutc
            FROM payroll.payrollbonusevents be
            LEFT JOIN core.drivers d ON d.driverid = be.driverid
            LEFT JOIN core.employees e ON e.employeeid = d.employeeid
            LEFT JOIN sec.users creator ON creator.userid = be.createdbyuserid
            WHERE be.payrollperiodid = :period_id
              AND be.companyid = :company_id
              AND be.status = 'Active'
            ORDER BY be.driverid, be.payrollbonuseventid
        """),
        {"period_id": period_id, "company_id": company_id},
    )
    return list(result.mappings().all())


async def _load_report_evidence(
    *,
    period: PeriodSummary,
    company_id: int,
    db: AsyncConnection,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read CP-5C report evidence from the same CP-4D transaction view."""
    status_result = await db.execute(
        text("""
            SELECT
                ppdes.payrollperioddriverdayentrystateid,
                ppdes.driverid,
                ppdes.workdate,
                ppdes.statuskeyid,
                sk.statuscode,
                sk.keyname,
                sk.isoffreason
            FROM payroll.payrollperioddriverdayentrystate ppdes
            JOIN payroll.payrollstatuskeys sk ON sk.statuskeyid = ppdes.statuskeyid
            WHERE ppdes.payrollperiodid = :pid
              AND ppdes.companyid = :cid
              AND ppdes.branchid = :bid
              AND ppdes.isvoided = FALSE
              AND ppdes.statuskeyid IS NOT NULL
              AND ppdes.workdate BETWEEN :period_start AND :period_end
            ORDER BY ppdes.driverid, ppdes.workdate,
                     ppdes.payrollperioddriverdayentrystateid
        """),
        {
            "pid": period.payroll_period_id,
            "cid": company_id,
            "bid": period.branch_id,
            "period_start": period.start_date,
            "period_end": period.end_date,
        },
    )
    status_entries = [
        {
            "PayrollPeriodDriverDayEntryStateID": int(row["payrollperioddriverdayentrystateid"]),
            "DriverID": int(row["driverid"]),
            "WorkDate": row["workdate"],
            "StatusKeyID": int(row["statuskeyid"]),
            "StatusCodeSnapshot": row["statuscode"],
            "StatusLabelSnapshot": row["keyname"],
            "StatusIsOffReasonSnapshot": bool(row["isoffreason"]),
        }
        for row in status_result.mappings().all()
    ]
    bonus_events = [
        {
            "PayrollBonusEventID": int(row["payrollbonuseventid"]),
            "DriverID": int(row["driverid"]),
            "Amount": Decimal(str(row["amount"])),
            "Reason": row["reason"],
            "Notes": row["notes"],
            "DataRevision": int(row["datarevision"]),
            "CreatedByUserID": (
                int(row["createdbyuserid"])
                if row["createdbyuserid"] is not None
                else None
            ),
            "CreatorDisplayNameSnapshot": row["creatordisplaynamesnapshot"],
            "CreatedAtUtc": row["createdatutc"],
        }
        for row in await _load_active_bonus_events(period.payroll_period_id, company_id, db)
    ]
    return status_entries, bonus_events


async def _validate_legacy_status_canonicalized(
    period: PeriodSummary,
    company_id: int,
    db: AsyncConnection,
) -> list[str]:
    """
    CP-4D completeness guard: reject Submit/Resubmit if a legacy DailyStatus
    DraftLine exists for a driver/day with no corresponding live-selected
    canonical PayrollPeriodDriverDayEntryState row.

    `_load_report_evidence` (the CP-4D immutable Status-evidence reader) reads
    only canonical entry-state rows with a non-voided StatusKeyID -- it never
    falls back to DraftLines. Without this guard, such a day would silently
    capture zero Status evidence while the snapshot still reports a versioned,
    "complete" ReportEvidenceVersion.

    A canonical row that exists but only carries NoteText (StatusKeyID NULL --
    e.g. a note-only save on an old period whose legacy Status code was never
    re-entered) does not satisfy this check; the legacy Status is still
    unrepresented. DailyNote-only legacy lines are out of scope: NoteText is
    never part of the immutable Status evidence contract (`_load_report_evidence`
    requires `StatusKeyID IS NOT NULL`), so a missing canonical row can never
    cause a note to disappear from that evidence.

    Read-only. Returns a blocker list (empty = no gap found).
    """
    rows = (await db.execute(
        text("""
            SELECT DISTINCT ds.driverid, ds.workdate
            FROM   payroll.payrolldraftlines ds
            WHERE  ds.payrollperiodid = :pid
              AND  ds.companyid       = :cid
              AND  ds.linetype        = 'DailyStatus'
              AND  ds.status         != 'Void'
              AND  ds.workdate BETWEEN :period_start AND :period_end
              AND  NOT EXISTS (
                       SELECT 1
                       FROM   payroll.payrollperioddriverdayentrystate e
                       WHERE  e.payrollperiodid = ds.payrollperiodid
                         AND  e.companyid       = ds.companyid
                         AND  e.driverid        = ds.driverid
                         AND  e.workdate        = ds.workdate
                         AND  e.statuskeyid    IS NOT NULL
                         AND  e.isvoided        = FALSE
                   )
            ORDER BY ds.driverid, ds.workdate
            LIMIT 5
        """),
        {
            "pid": period.payroll_period_id,
            "cid": company_id,
            "period_start": period.start_date,
            "period_end": period.end_date,
        },
    )).mappings().all()
    if not rows:
        return []

    examples = "; ".join(f"driver {r['driverid']} on {r['workdate']}" for r in rows)
    return [
        "LEGACY_STATUS_NOT_CANONICAL: one or more days have a Status set only "
        "through the legacy Daily Status representation, with no matching "
        f"entry in the current Day Grid entry state ({examples}). Open the "
        "Day Grid for the affected day(s), re-select the Status, and save "
        "before this period can be submitted."
    ]


async def _build_live_calculation_packet(
    period: PeriodSummary,
    company_id: int,
    db: AsyncConnection,
) -> _LiveCalculationPacket:
    """
    Read-only, live provisional expected-income breakdown for an Open or
    Returned period, calculated from CURRENT effective source/config data.

    Distinct from `get_finalization_preview` above (Approved-only, mirrors
    exactly what `finalize_period` will write): this is a live preview for
    the two lifecycle statuses that still allow correction/entry. It is
    never a submitted snapshot -- InReview/Approved/Locked/Archived/
    Cancelled all remain out of scope (see the CP-4C+ future snapshot
    contract for those).

    Absolute read-only guarantee: no INSERT/UPDATE/DELETE, no audit write,
    no period/source/derived-state mutation of any kind. Does not call
    `_refresh_draft_calculations`, `_sync_status_payment_for_entry_state`,
    `_refresh_status_payment_lines`, or `finalize_period`.

    Reuses, unchanged:
      - `_compute_draft_line_preview_amounts` -> `_compute_calculated_amount`
        -> CP-4A's `calculate_per_unit` for daily PerUnit lines (and the
        existing EnteredAmount/Fixed/None/manual dispatch for the rest);
      - the canonical PayrollBonusEvents Active-only read;
      - the CP-3C minimum/maximum-then-bonus ordering.

    Adds, new to CP-4B:
      - `_resolve_live_status_payment_lines`, which reads the canonical
        `PayrollPeriodDriverDayEntryState.StatusKeyID` selection directly
        and resolves the CURRENT applicable DriverRate live -- the stored
        STATUS_PAYMENT/STATUS_PAY compatibility-projection DraftLine is
        excluded from the stored-line aggregation below and is never used
        as live truth, so a stale projection can never be double-counted.

    Driver inclusion is financial-source-driven only (a driver with a
    current daily line, a canonical selected Status, a non-BONUS period-pay
    line, or an Active bonus event) -- not a full eligible-driver roster.
    """
    period_id = period.payroll_period_id

    blockers: list[str] = []
    warnings: list[str] = []

    # ── CP-4B fix (Codex P1): shared structural blockers (duplicate active
    # Daily lines, driver eligibility violations, contaminated/foreign
    # RateType references, unresolvable rate mapping) — the SAME read-only
    # checks enforced by finalize_period / get_finalization_preview. These
    # checks are structural, not Approved-specific (none of them reference
    # period.status), so they apply directly and unmodified to Open/Returned
    # periods. Surfacing them here prevents a preview from looking
    # financially complete (has_blockers=false) while a structural condition
    # that would block finalize_period is silently present.
    blockers.extend(await _validate_period_can_finalize(
        period_id=period_id,
        company_id=company_id,
        branch_id=period.branch_id,
        period_start=period.start_date,
        period_end=period.end_date,
        db=db,
    ))

    # ── CP-4D completeness guard: a legacy DailyStatus DraftLine with no
    # canonical entry-state row would silently vanish from the immutable
    # Status evidence captured at Submit/Resubmit (_load_report_evidence
    # reads canonical rows only). Block until the day is re-saved through
    # the Day Grid so the Status is canonically represented.
    blockers.extend(await _validate_legacy_status_canonicalized(
        period=period,
        company_id=company_id,
        db=db,
    ))

    # ── Daily/period-pay lines: virtual (unpersisted) rate refresh, exactly
    # like get_finalization_preview — but excluding the persisted
    # STATUS_PAYMENT/STATUS_PAY compatibility projection and legacy BONUS
    # lines, since Status and Bonus are supplied live/canonically below.
    refreshed_calcs = await _compute_draft_line_preview_amounts(
        period_id, company_id, period.start_date, db
    )

    lines_result = await db.execute(
        text(f"""
            SELECT
                dl.draftlineid,
                dl.driverid,
                d.drivercode,
                pi.payitemid,
                e.fullname          AS drivername,
                dl.workdate,
                dl.linetype,
                dl.linescope,
                dl.quantity,
                dl.rateamount,
                dl.calculatedamount,
                dl.needsmanagerreview,
                dl.sourcetype,
                dl.sourceid
            FROM   payroll.payrolldraftlines dl
            LEFT JOIN core.drivers   d ON d.driverid   = dl.driverid
            LEFT JOIN core.employees e ON e.employeeid = d.employeeid
            LEFT JOIN LATERAL (
                SELECT pi.payitemid
                FROM payroll.payitems pi
                WHERE pi.payitemcode = dl.linetype
                  AND (pi.companyid IS NULL OR pi.companyid = dl.companyid)
                ORDER BY CASE WHEN pi.companyid = dl.companyid THEN 0 ELSE 1 END
                LIMIT 1
            ) pi ON TRUE
            WHERE  dl.payrollperiodid = :period_id
              AND  dl.companyid       = :company_id
              AND  dl.status         != 'Void'
              AND  dl.linetype       != 'BONUS'
              AND  dl.linetype       NOT IN ('DailyStatus', 'DailyNote')
              AND  NOT {_STATUS_PAYMENT_PROJECTION_SQL}
            ORDER BY dl.driverid, dl.workdate NULLS LAST, dl.draftlineid
        """),
        {"period_id": period_id, "company_id": company_id},
    )
    raw_lines = lines_result.mappings().fetchall()

    driver_names: dict[int, str | None] = {}
    driver_codes: dict[int, str | None] = {}
    driver_daily: dict[int, Decimal] = {}
    driver_period: dict[int, Decimal] = {}
    driver_status: dict[int, Decimal] = {}
    driver_bonus: dict[int, Decimal] = {}
    driver_line_nmr: dict[int, bool] = {}
    driver_lines: dict[int, list[_CalculationPacketLine]] = {}

    stale_count = 0
    for r in raw_lines:
        lid = int(r["draftlineid"])
        drv = int(r["driverid"])
        driver_names.setdefault(drv, r["drivername"])
        driver_codes.setdefault(drv, r["drivercode"])
        driver_lines.setdefault(drv, [])

        stored_calc = Decimal(str(r["calculatedamount"])) if r["calculatedamount"] is not None else None
        qty = Decimal(str(r["quantity"])) if r["quantity"] is not None else Decimal("0")
        rate = Decimal(str(r["rateamount"])) if r["rateamount"] is not None else None

        if lid in refreshed_calcs:
            _cr = refreshed_calcs[lid]
            effective_calc = _cr.calculated_amount
            effective_nmr = _cr.needs_manager_review
            resolved_rate = _cr.resolved_rate_amount
            if effective_calc != stored_calc:
                stale_count += 1
        else:
            effective_calc = stored_calc
            effective_nmr = bool(r["needsmanagerreview"])
            resolved_rate = rate

        if effective_nmr:
            driver_line_nmr[drv] = True

        amt = effective_calc if effective_calc is not None else qty * (rate if rate is not None else Decimal("0"))

        if r["linescope"] == "Daily":
            driver_daily[drv] = driver_daily.get(drv, Decimal("0")) + amt
        else:
            driver_period[drv] = driver_period.get(drv, Decimal("0")) + amt

        driver_lines[drv].append(_CalculationPacketLine(
            source_type=r["sourcetype"] or "DraftLine",
            source_id=r["sourceid"],
            line_type=r["linetype"],
            line_scope=r["linescope"],
            work_date=r["workdate"],
            driver_id=drv,
            quantity=qty,
            resolved_rate_amount=resolved_rate,
            calculated_amount=effective_calc,
            needs_manager_review=effective_nmr,
            blocker_reason=(
                "Calculated amount unresolved or manually flagged for manager review."
                if effective_nmr else None
            ),
            rate_type_id=(
                _cr.rate_type_id if lid in refreshed_calcs else None
            ),
            pay_item_id=(int(r["payitemid"]) if r["payitemid"] is not None else None),
            driver_rate_id=(
                _cr.driver_rate_id if lid in refreshed_calcs else None
            ),
            source_evidence={
                "DraftLineID": lid,
                "StoredSourceType": r["sourcetype"],
                "StoredSourceID": r["sourceid"],
                "StoredCalculatedAmount": stored_calc,
                "StoredRateAmount": rate,
                "RateBehavior": (
                    _cr.rate_behavior if lid in refreshed_calcs else "Stored"
                ),
                "PerUnitCalculationVersion": (
                    PER_UNIT_CALCULATION_VERSION
                    if lid in refreshed_calcs and _cr.rate_behavior == "PerUnit"
                    else None
                ),
            },
            snapshot_source_type="DraftLine",
            snapshot_source_id=str(lid),
            # Preserve CP-4B's historical public NULL CalculatedAmount while
            # freezing the actual fallback amount used in the packet total.
            snapshot_calculated_amount=amt,
        ))

    if stale_count > 0:
        warnings.append(
            f"{stale_count} line(s) had stale stored calculations. "
            f"Preview amounts reflect the latest effective-dated rates."
        )

    # ── Canonical live Status-derived pay (CP-4B) — never the stored
    # STATUS_PAYMENT/STATUS_PAY projection, which was already excluded above.
    live_status_lines = await _resolve_live_status_payment_lines(
        period_id, company_id, period.branch_id, db,
    )
    for sl in live_status_lines:
        drv = sl.driver_id
        driver_names.setdefault(drv, None)
        driver_lines.setdefault(drv, [])

        amt = sl.calculated_amount if sl.calculated_amount is not None else Decimal("0")
        driver_status[drv] = driver_status.get(drv, Decimal("0")) + amt

        if sl.needs_manager_review:
            driver_line_nmr[drv] = True

        driver_lines[drv].append(_CalculationPacketLine(
            source_type="StatusEntryState",
            source_id=f"STATUS_LIVE:{drv}:{sl.work_date}:{sl.status_key_id}",
            line_type=sl.line_type,
            line_scope="Daily",
            work_date=sl.work_date,
            driver_id=drv,
            quantity=sl.hours_value,
            resolved_rate_amount=sl.resolved_rate_amount,
            calculated_amount=sl.calculated_amount,
            needs_manager_review=sl.needs_manager_review,
            blocker_reason=(
                "No applicable approved DriverRate found for this driver's "
                "selected Status as of its work date."
                if sl.needs_manager_review else None
            ),
            rate_type_id=sl.rate_type_id,
            rate_column_id=sl.status_rate_column_id,
            driver_rate_id=sl.driver_rate_id,
            source_evidence={
                "PayrollPeriodDriverDayEntryStateID": sl.entry_state_id,
                "StatusKeyID": sl.status_key_id,
                "StatusCode": sl.status_code,
                "StatusRateColumnID": sl.status_rate_column_id,
                "HoursValue": sl.hours_value,
                "WorkDate": sl.work_date,
                "RateTypeID": sl.rate_type_id,
                "DriverRateID": sl.driver_rate_id,
            },
            snapshot_source_type="StatusEntryState",
            snapshot_source_id=str(sl.entry_state_id),
        ))

    # ── Canonical Active bonus (never Voided; never legacy BONUS DraftLines).
    for b in await _load_active_bonus_events(period_id, company_id, db):
        drv = int(b["driverid"])
        driver_names.setdefault(drv, b["drivername"])
        driver_codes.setdefault(drv, b["drivercode"])
        driver_lines.setdefault(drv, [])
        amt = Decimal(str(b["amount"]))
        driver_bonus[drv] = driver_bonus.get(drv, Decimal("0")) + amt
        driver_lines[drv].append(_CalculationPacketLine(
            source_type="BonusEvent",
            source_id=str(b["payrollbonuseventid"]),
            line_type="BONUS",
            line_scope="Period",
            work_date=None,
            driver_id=drv,
            quantity=None,
            resolved_rate_amount=None,
            calculated_amount=amt,
            needs_manager_review=False,
            blocker_reason=None,
            bonus_event_id=int(b["payrollbonuseventid"]),
            source_evidence={
                "PayrollBonusEventID": int(b["payrollbonuseventid"]),
                "Amount": amt,
                "Reason": b["reason"],
                "Notes": b["notes"],
                "DataRevision": b["datarevision"],
                "Status": "Active",
            },
        ))

    # ── Financial-source-driven driver union (CP-4B: not a full roster).
    all_driver_ids = (
        set(driver_daily) | set(driver_period) | set(driver_status) | set(driver_bonus)
    )

    if not all_driver_ids:
        warnings.append("No current financial source lines for this period.")
    else:
        driver_identity_result = await db.execute(
            text("""
                SELECT d.driverid, d.drivercode, e.fullname
                FROM core.drivers d
                JOIN core.employees e ON e.employeeid = d.employeeid
                WHERE d.companyid = :cid
                  AND d.driverid = ANY(:driver_ids)
            """),
            {"cid": company_id, "driver_ids": sorted(all_driver_ids)},
        )
        for identity in driver_identity_result.mappings().all():
            driver_names.setdefault(int(identity["driverid"]), identity["fullname"])
            driver_codes.setdefault(int(identity["driverid"]), identity["drivercode"])

    # ── Minimum/maximum: same as-of-period-start rule and ordering as
    # get_finalization_preview — normal base excludes bonus by construction;
    # bonus is added back in only after minimum/maximum is applied (CP-3C).
    period_start = period.start_date
    driver_blockers: dict[int, list[str]] = {}
    driver_min_adj: dict[int, Decimal] = {}
    driver_max_adj: dict[int, Decimal] = {}
    driver_min_rule: dict[int, Any] = {}
    driver_max_rule: dict[int, Any] = {}

    for drv_id in all_driver_ids:
        normal_base = (
            driver_daily.get(drv_id, Decimal("0"))
            + driver_status.get(drv_id, Decimal("0"))
            + driver_period.get(drv_id, Decimal("0"))
        )

        min_row = (await db.execute(
            text("""
                SELECT driverpayruleid, amount, status, effectivefrom, effectiveto
                FROM payroll.driverpayrules
                WHERE  driverid      = :did
                  AND  companyid     = :cid
                  AND  ruletype      = 'MinimumPay'
                  AND  status        IN ('Active', 'Ended')
                  AND  effectivefrom <= :as_of
                  AND  (effectiveto IS NULL OR effectiveto >= :as_of)
                ORDER BY effectivefrom DESC
                LIMIT 1
            """),
            {"did": drv_id, "cid": company_id, "as_of": period_start},
        )).mappings().first()
        max_row = (await db.execute(
            text("""
                SELECT driverpayruleid, amount, status, effectivefrom, effectiveto
                FROM payroll.driverpayrules
                WHERE  driverid      = :did
                  AND  companyid     = :cid
                  AND  ruletype      = 'MaximumPay'
                  AND  status        IN ('Active', 'Ended')
                  AND  effectivefrom <= :as_of
                  AND  (effectiveto IS NULL OR effectiveto >= :as_of)
                ORDER BY effectivefrom DESC
                LIMIT 1
            """),
            {"did": drv_id, "cid": company_id, "as_of": period_start},
        )).mappings().first()

        min_amount = Decimal(str(min_row["amount"])) if min_row else None
        max_amount = Decimal(str(max_row["amount"])) if max_row else None
        if min_row is not None:
            driver_min_rule[drv_id] = min_row
        if max_row is not None:
            driver_max_rule[drv_id] = max_row

        if min_amount is not None and max_amount is not None and min_amount > max_amount:
            driver_blockers.setdefault(drv_id, []).append(
                f"Minimum pay ({min_amount}) exceeds maximum pay ({max_amount}). "
                f"Correct the pay rules before this driver's total can be trusted."
            )
            driver_min_adj[drv_id] = Decimal("0")
            driver_max_adj[drv_id] = Decimal("0")
            continue

        if min_amount is not None and normal_base < min_amount:
            driver_min_adj[drv_id] = min_amount - normal_base
        else:
            driver_min_adj[drv_id] = Decimal("0")

        if max_amount is not None and normal_base > max_amount:
            driver_max_adj[drv_id] = max_amount - normal_base
        else:
            driver_max_adj[drv_id] = Decimal("0")

    # ── Assemble driver totals.
    driver_totals: list[_CalculationPacketDriverTotal] = []
    for drv_id in sorted(all_driver_ids):
        daily = driver_daily.get(drv_id, Decimal("0"))
        status_pay = driver_status.get(drv_id, Decimal("0"))
        period_pay = driver_period.get(drv_id, Decimal("0"))
        normal_base = daily + status_pay + period_pay
        min_adj = driver_min_adj.get(drv_id, Decimal("0"))
        max_adj = driver_max_adj.get(drv_id, Decimal("0"))
        bonus = driver_bonus.get(drv_id, Decimal("0"))
        expected_pay = normal_base + min_adj + max_adj + bonus
        drv_blockers = driver_blockers.get(drv_id, [])
        drv_nmr = driver_line_nmr.get(drv_id, False)

        if min_adj != 0:
            min_rule = driver_min_rule[drv_id]
            driver_lines[drv_id].append(_CalculationPacketLine(
                source_type="System",
                source_id=str(min_rule["driverpayruleid"]),
                line_type="SYS_MIN_TOPUP",
                line_scope="Period",
                work_date=None,
                driver_id=drv_id,
                quantity=Decimal("1"),
                resolved_rate_amount=None,
                calculated_amount=min_adj,
                needs_manager_review=False,
                blocker_reason=None,
                source_evidence={
                    "DriverPayRuleID": int(min_rule["driverpayruleid"]),
                    "RuleType": "MinimumPay",
                    "RuleAmount": Decimal(str(min_rule["amount"])),
                    "RuleStatus": min_rule["status"],
                    "EffectiveFrom": min_rule["effectivefrom"],
                    "EffectiveTo": min_rule["effectiveto"],
                    "NormalBase": normal_base,
                },
            ))
        if max_adj != 0:
            max_rule = driver_max_rule[drv_id]
            driver_lines[drv_id].append(_CalculationPacketLine(
                source_type="System",
                source_id=str(max_rule["driverpayruleid"]),
                line_type="SYS_MAX_CAP",
                line_scope="Period",
                work_date=None,
                driver_id=drv_id,
                quantity=Decimal("1"),
                resolved_rate_amount=None,
                calculated_amount=max_adj,
                needs_manager_review=False,
                blocker_reason=None,
                source_evidence={
                    "DriverPayRuleID": int(max_rule["driverpayruleid"]),
                    "RuleType": "MaximumPay",
                    "RuleAmount": Decimal(str(max_rule["amount"])),
                    "RuleStatus": max_rule["status"],
                    "EffectiveFrom": max_rule["effectivefrom"],
                    "EffectiveTo": max_rule["effectiveto"],
                    "NormalBase": normal_base,
                },
            ))

        driver_totals.append(_CalculationPacketDriverTotal(
            driver_id=drv_id,
            driver_code=driver_codes.get(drv_id),
            driver_name=driver_names.get(drv_id),
            daily_pay=daily,
            status_pay=status_pay,
            period_pay=period_pay,
            minimum_adjustment=min_adj,
            maximum_adjustment=max_adj,
            bonus_total=bonus,
            expected_pay=expected_pay,
            needs_manager_review=drv_nmr,
            blockers=drv_blockers,
            lines=driver_lines.get(drv_id, []),
        ))
        if drv_blockers:
            blockers.extend(f"Driver {drv_id}: {b}" for b in drv_blockers)
        if drv_nmr:
            blockers.append(
                f"Driver {drv_id}: one or more lines require manager review "
                f"(calculation unresolved or manually flagged)."
            )

    total_expected_pay = sum((dt.expected_pay for dt in driver_totals), Decimal("0"))

    return _LiveCalculationPacket(
        payroll_period_id=period_id,
        company_id=company_id,
        branch_id=period.branch_id,
        status=period.status,
        blockers=blockers,
        warnings=warnings,
        drivers=driver_totals,
        total_expected_pay=total_expected_pay,
    )


async def get_calculation_preview(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> "CalculationPreviewResponse":
    """Adapt the shared live packet to CP-4B's unchanged public contract."""
    from app.payroll.schemas import (
        CalculationPreviewResponse,
        CalculationPreviewDriverTotal,
        CalculationPreviewLine,
    )

    own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)
    if own_driver_id is not None:
        raise HTTPException(
            status_code=403,
            detail="Current Payroll is not accessible to driver-role users.",
        )
    period = await get_period_by_id(company_id, user_id, period_id, db)
    if period.status not in ENTRY_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                "Calculation preview requires an Open or Returned period. "
                f"Current status: '{period.status}'."
            ),
        )
    await _check_any_permission(
        company_id, user_id, period.branch_id, ["payroll.view", "payroll.entry"], db,
    )
    packet = await _build_live_calculation_packet(period, company_id, db)
    return CalculationPreviewResponse(
        payroll_period_id=packet.payroll_period_id,
        company_id=packet.company_id,
        branch_id=packet.branch_id,
        branch_name=period.branch_name,
        status=packet.status,
        provisional=True,
        financials_available=True,
        has_blockers=bool(packet.blockers),
        blockers=packet.blockers,
        warnings=packet.warnings,
        drivers=[
            CalculationPreviewDriverTotal(
                driver_id=driver.driver_id,
                driver_name=driver.driver_name,
                daily_pay=driver.daily_pay,
                status_pay=driver.status_pay,
                period_pay=driver.period_pay,
                normal_base=driver.daily_pay + driver.status_pay + driver.period_pay,
                minimum_adjustment=driver.minimum_adjustment,
                maximum_adjustment=driver.maximum_adjustment,
                bonus_total=driver.bonus_total,
                expected_pay=driver.expected_pay,
                needs_manager_review=driver.needs_manager_review,
                blockers=driver.blockers,
                lines=[
                    CalculationPreviewLine(
                        source_type=line.source_type,
                        source_id=line.source_id,
                        line_type=line.line_type,
                        work_date=line.work_date,
                        pay_item_id=line.pay_item_id,
                        rate_column_id=line.rate_column_id,
                        driver_id=line.driver_id,
                        quantity=line.quantity,
                        resolved_rate=line.resolved_rate_amount,
                        calculated_amount=line.calculated_amount,
                        needs_manager_review=line.needs_manager_review,
                        blocker_reason=line.blocker_reason,
                    )
                    for line in driver.lines
                ],
            )
            for driver in packet.drivers
        ],
        total_expected_pay=packet.total_expected_pay,
    )


def _packet_driver_totals_for_hash(
    packet: _LiveCalculationPacket,
) -> list[dict[str, Any]]:
    """Project the shared live packet into CP-4C's hash contract."""
    return [
        {
            "DriverID": driver.driver_id,
            "DriverCodeSnapshot": driver.driver_code,
            "DriverNameSnapshot": driver.driver_name,
            "DailyPay": driver.daily_pay,
            "StatusPay": driver.status_pay,
            "PeriodPay": driver.period_pay,
            "MinimumAdjustment": driver.minimum_adjustment,
            "MaximumAdjustment": driver.maximum_adjustment,
            "BonusTotal": driver.bonus_total,
            "ExpectedPay": driver.expected_pay,
            "Lines": [
                {
                    "SourceType": line.snapshot_source_type or line.source_type,
                    "SourceID": line.snapshot_source_id if line.snapshot_source_id is not None else line.source_id,
                    "LineType": line.line_type,
                    "LineScope": line.line_scope,
                    "WorkDate": line.work_date,
                    "PayItemID": line.pay_item_id,
                    "RateTypeID": line.rate_type_id,
                    "DriverRateID": line.driver_rate_id,
                    "BonusEventID": line.bonus_event_id,
                    "Quantity": line.quantity,
                    "ResolvedRateAmount": line.resolved_rate_amount,
                    "CalculatedAmount": (
                        line.snapshot_calculated_amount
                        if line.snapshot_calculated_amount is not None
                        else line.calculated_amount
                    ),
                    "SourceEvidenceJSONB": line.source_evidence or {},
                }
                for line in driver.lines
            ],
        }
        for driver in packet.drivers
    ]


async def _capture_calculation_snapshot(
    *,
    period: PeriodSummary,
    company_id: int,
    user_id: int,
    packet: _LiveCalculationPacket,
    db: AsyncConnection,
    context: str,
) -> int:
    """Persist one complete immutable CP-4D submission packet.

    The caller already owns the period/workflow locks.  This writer performs no
    calculation and never uses generated IDs in either hash.
    """
    if packet.blockers:
        raise HTTPException(
            status_code=422,
            detail="Cannot submit an incomplete calculation packet: " + "; ".join(packet.blockers),
        )
    if any(
        line.snapshot_calculated_amount is None and line.calculated_amount is None
        for driver in packet.drivers
        for line in driver.lines
    ):
        raise HTTPException(
            status_code=422,
            detail="Cannot submit: an authoritative calculation line is unresolved.",
        )

    status_entries, bonus_events = await _load_report_evidence(
        period=period,
        company_id=company_id,
        db=db,
    )
    snapshot_bonus_lines = sorted(
        (
            line.bonus_event_id,
            line.driver_id,
            line.snapshot_calculated_amount
            if line.snapshot_calculated_amount is not None
            else line.calculated_amount,
        )
        for driver in packet.drivers
        for line in driver.lines
        if line.source_type == "BonusEvent" and line.bonus_event_id is not None
    )
    evidence_bonus_lines = sorted(
        (event["PayrollBonusEventID"], event["DriverID"], event["Amount"])
        for event in bonus_events
    )
    if snapshot_bonus_lines != evidence_bonus_lines:
        raise HTTPException(
            status_code=422,
            detail=(
                "Cannot submit: captured Bonus evidence does not reconcile "
                "with the authoritative calculation packet."
            ),
        )
    report_evidence_hash = calculate_report_evidence_hash(
        status_entries=status_entries,
        bonus_events=bonus_events,
    )

    eligibility_rows = (await db.execute(
        text("""
            SELECT driverid, iseligibleforperiod, eligibilityreasoncode,
                   hiredatesnapshot, terminationdatesnapshot,
                   drivereffectivefromsnapshot, drivereffectivetosnapshot,
                   drivercodesnapshot, drivernamesnapshot
            FROM payroll.payrollperioddrivereligibility
            WHERE payrollperiodid = :pid AND companyid = :cid AND branchid = :bid
            ORDER BY driverid
        """),
        {"pid": period.payroll_period_id, "cid": company_id, "bid": period.branch_id},
    )).mappings().all()

    source_config_payload = {
        "PacketContract": "cp4d-source-config-v1",
        "PayrollPeriod": {
            "PayrollPeriodID": period.payroll_period_id,
            "CompanyID": company_id,
            "BranchID": period.branch_id,
            "PeriodCode": period.period_code,
            "PeriodType": period.period_type,
            "StartDate": period.start_date,
            "EndDate": period.end_date,
        },
        "Eligibility": [dict(row) for row in eligibility_rows],
        "Sources": [
            {
                "DriverID": driver.driver_id,
                "Lines": [
                    {
                        "SourceType": line.snapshot_source_type or line.source_type,
                        "SourceID": line.snapshot_source_id if line.snapshot_source_id is not None else line.source_id,
                        "LineType": line.line_type,
                        "LineScope": line.line_scope,
                        "WorkDate": line.work_date,
                        "Quantity": line.quantity,
                        "ResolvedRateAmount": line.resolved_rate_amount,
                        "SourceEvidenceJSONB": line.source_evidence or {},
                    }
                    for line in driver.lines
                ],
            }
            for driver in packet.drivers
        ],
    }
    source_config_hash = calculate_source_config_hash(source_config_payload)
    revision_result = await db.execute(
        text("""
            SELECT COALESCE(MAX(revisionnumber), 0) + 1
            FROM payroll.payrollcalculationsnapshots
            WHERE payrollperiodid = :pid
        """),
        {"pid": period.payroll_period_id},
    )
    revision_number = int(revision_result.scalar_one())
    hash_totals = _packet_driver_totals_for_hash(packet)
    snapshot_hash = calculate_snapshot_hash(
        company_id=company_id,
        branch_id=period.branch_id,
        payroll_period_id=period.payroll_period_id,
        revision_number=revision_number,
        calculation_version=CURRENT_PAYROLL_CALCULATION_VERSION,
        source_config_hash=source_config_hash,
        driver_totals=hash_totals,
    )

    header_result = await db.execute(
        text("""
            INSERT INTO payroll.payrollcalculationsnapshots
                (companyid, branchid, payrollperiodid, revisionnumber,
                 calculationversion, sourceconfighash, snapshothash,
                 reportevidenceversion, reportevidencehash,
                 createdbyuserid, totalexpectedpay)
            VALUES
                (:cid, :bid, :pid, :revision, :version, :source_hash,
                 :snapshot_hash, :report_evidence_version, :report_evidence_hash,
                 :uid, :total)
            RETURNING payrollcalculationsnapshotid
        """),
        {
            "cid": company_id,
            "bid": period.branch_id,
            "pid": period.payroll_period_id,
            "revision": revision_number,
            "version": CURRENT_PAYROLL_CALCULATION_VERSION,
            "source_hash": source_config_hash,
            "snapshot_hash": snapshot_hash,
            "report_evidence_version": CURRENT_REPORT_EVIDENCE_VERSION,
            "report_evidence_hash": report_evidence_hash,
            "uid": user_id,
            "total": packet.total_expected_pay,
        },
    )
    snapshot_id = int(header_result.scalar_one())
    snapshot_lines = [
        {**line, "DriverID": hash_total["DriverID"]}
        for hash_total in hash_totals
        for line in hash_total["Lines"]
    ]
    used_rate_definition_ids = await capture_snapshot_used_rate_definitions(
        snapshot_id=snapshot_id,
        company_id=company_id,
        branch_id=period.branch_id,
        period_id=period.payroll_period_id,
        snapshot_line_rows=snapshot_lines,
        db=db,
    )
    snapshot_line_ordinal = 0

    for driver, hash_total in zip(packet.drivers, hash_totals, strict=True):
        driver_result = await db.execute(
            text("""
                INSERT INTO payroll.payrollcalculationdrivertotals
                    (payrollcalculationsnapshotid, companyid, branchid, driverid,
                     drivercodesnapshot, drivernamesnapshot, dailypay, statuspay,
                     periodpay, minimumadjustment, maximumadjustment, bonustotal,
                     expectedpay)
                VALUES
                    (:snapshot_id, :cid, :bid, :driver_id, :driver_code, :driver_name,
                     :daily, :status, :period, :minimum, :maximum, :bonus, :expected)
                RETURNING payrollcalculationdrivertotalid
            """),
            {
                "snapshot_id": snapshot_id,
                "cid": company_id,
                "bid": period.branch_id,
                "driver_id": driver.driver_id,
                "driver_code": driver.driver_code,
                "driver_name": driver.driver_name,
                "daily": driver.daily_pay,
                "status": driver.status_pay,
                "period": driver.period_pay,
                "minimum": driver.minimum_adjustment,
                "maximum": driver.maximum_adjustment,
                "bonus": driver.bonus_total,
                "expected": driver.expected_pay,
            },
        )
        driver_total_id = int(driver_result.scalar_one())
        for line in hash_total["Lines"]:
            await db.execute(
                text("""
                    INSERT INTO payroll.payrollcalculationsnapshotlines
                        (payrollcalculationdrivertotalid, sourcetype, sourceid,
                         linetype, linescope, workdate, payitemid, ratetypeid,
                         driverrateid, bonuseventid, quantity, resolvedrateamount,
                         calculatedamount, sourceevidencejsonb, usedratedefinitionid)
                    VALUES
                        (:driver_total_id, :source_type, :source_id, :line_type,
                         :line_scope, :work_date, :pay_item_id, :rate_type_id,
                         :driver_rate_id, :bonus_event_id, :quantity, :resolved_rate,
                         :calculated_amount, CAST(:evidence AS jsonb), :used_rate_definition_id)
                """),
                {
                    "driver_total_id": driver_total_id,
                    "source_type": line["SourceType"],
                    "source_id": line["SourceID"],
                    "line_type": line["LineType"],
                    "line_scope": line["LineScope"],
                    "work_date": line["WorkDate"],
                    "pay_item_id": line["PayItemID"],
                    "rate_type_id": line["RateTypeID"],
                    "driver_rate_id": line["DriverRateID"],
                    "bonus_event_id": line["BonusEventID"],
                    "quantity": line["Quantity"],
                    "resolved_rate": line["ResolvedRateAmount"],
                    "calculated_amount": line["CalculatedAmount"],
                    "evidence": canonical_json(line["SourceEvidenceJSONB"]),
                    "used_rate_definition_id": used_rate_definition_ids.get(snapshot_line_ordinal),
                },
            )
            snapshot_line_ordinal += 1

    for entry in status_entries:
        await db.execute(
            text("""
                INSERT INTO payroll.payrollcalculationsnapshotstatusentries
                    (payrollcalculationsnapshotid, companyid, branchid,
                     payrollperiodid, driverid, workdate,
                     payrollperioddriverdayentrystateid, statuskeyid,
                     statuscodesnapshot, statuslabelsnapshot,
                     statusisoffreasonsnapshot)
                VALUES
                    (:snapshot_id, :cid, :bid, :pid, :driver_id, :work_date,
                     :entry_state_id, :status_key_id, :status_code,
                     :status_label, :status_is_off_reason)
            """),
            {
                "snapshot_id": snapshot_id,
                "cid": company_id,
                "bid": period.branch_id,
                "pid": period.payroll_period_id,
                "driver_id": entry["DriverID"],
                "work_date": entry["WorkDate"],
                "entry_state_id": entry["PayrollPeriodDriverDayEntryStateID"],
                "status_key_id": entry["StatusKeyID"],
                "status_code": entry["StatusCodeSnapshot"],
                "status_label": entry["StatusLabelSnapshot"],
                "status_is_off_reason": entry["StatusIsOffReasonSnapshot"],
            },
        )

    for event in bonus_events:
        await db.execute(
            text("""
                INSERT INTO payroll.payrollcalculationsnapshotbonusevents
                    (payrollcalculationsnapshotid, companyid, branchid,
                     payrollperiodid, payrollbonuseventid, driverid, amount,
                     reason, notes, datarevision, createdbyuserid,
                     creatordisplaynamesnapshot, createdatutc)
                VALUES
                    (:snapshot_id, :cid, :bid, :pid, :bonus_event_id, :driver_id,
                     :amount, :reason, :notes, :data_revision, :created_by_user_id,
                     :creator_display_name, :created_at)
            """),
            {
                "snapshot_id": snapshot_id,
                "cid": company_id,
                "bid": period.branch_id,
                "pid": period.payroll_period_id,
                "bonus_event_id": event["PayrollBonusEventID"],
                "driver_id": event["DriverID"],
                "amount": event["Amount"],
                "reason": event["Reason"],
                "notes": event["Notes"],
                "data_revision": event["DataRevision"],
                "created_by_user_id": event["CreatedByUserID"],
                "creator_display_name": event["CreatorDisplayNameSnapshot"],
                "created_at": event["CreatedAtUtc"],
            },
        )

    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid, newvaluejson, reason, sourcetype)
            VALUES
                (:cid, :bid, :uid, 'CALCULATION_SNAPSHOT_CAPTURED',
                 'payroll', 'PayrollCalculationSnapshots', :snapshot_id, :new_value,
                 :reason, 'Application')
        """),
        {
            "cid": company_id,
            "bid": period.branch_id,
            "uid": user_id,
            "snapshot_id": str(snapshot_id),
            "new_value": json.dumps({
                "payroll_period_id": period.payroll_period_id,
                "snapshot_id": snapshot_id,
                "revision_number": revision_number,
                "source_config_hash": source_config_hash,
                "snapshot_hash": snapshot_hash,
                "report_evidence_version": CURRENT_REPORT_EVIDENCE_VERSION,
                "report_evidence_hash": report_evidence_hash,
                "context": context,
            }),
            "reason": "Immutable calculation snapshot captured for review submission",
        },
    )
    return snapshot_id


# ---------------------------------------------------------------------------
# Ledger read — final lines for a locked period
# ---------------------------------------------------------------------------

_FINAL_SELECT = """
    SELECT
        fl.finallineid,
        fl.payrollperiodid,
        fl.branchid,
        fl.driverid,
        e.fullname         AS drivername,
        fl.draftlineid,
        fl.workdate,
        fl.linetype,
        fl.linescope,
        fl.quantity,
        fl.rateamount,
        fl.finalamount,
        fl.sourcetype,
        fl.approvedbyuserid,
        fl.approvedatutc,
        fl.lockedatutc,
        fl.notes,
        fl.payitemid,
        fl.ratetypeid,
        fl.driverrateid,
        fl.resolvedrateamount,
        fl.ratebehavior,
        fl.sourcesnapshot
    FROM   payroll.payrollfinallines fl
    JOIN   core.drivers              d  ON d.driverid   = fl.driverid
    JOIN   core.employees            e  ON e.employeeid = d.employeeid
"""


async def get_final_lines(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    driver_id: int | None = None,
) -> list[FinalLineSummary]:
    """Return locked final lines for a period (access-checked via period lookup).

    Ledger is operational/admin only — Driver/ODA users are blocked.
    Period must be Locked or Archived; draft data is never exposed via this path.
    """
    # ── Driver-role hard-block (Ledger is not the Driver Screen) ────────────── #
    await _require_not_driver_role(company_id, user_id, db)

    period = await get_period_by_id(company_id, user_id, period_id, db)

    # ── Period status guard — final lines only exist on Locked/Archived periods ─ #
    if period.status not in ("Locked", "Archived"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Final lines are only available for Locked or Archived periods "
                f"(current status: '{period.status}'). "
                "Finalize the period first via POST /periods/{id}/finalize."
            ),
        )

    # ── Payroll read permission — view or finalize role required ───────────────── #
    await _check_any_permission(
        company_id, user_id, period.branch_id,
        ["payroll.view", "payroll.entry", "payroll.finalize"],
        db,
    )

    conditions = [
        "fl.payrollperiodid = :period_id",
        "fl.companyid       = :company_id",
    ]
    params: dict[str, Any] = {
        "period_id": period_id,
        "company_id": company_id,
    }

    if driver_id is not None:
        conditions.append("fl.driverid = :driver_id")
        params["driver_id"] = driver_id

    where = " AND ".join(conditions)
    result = await db.execute(
        text(
            f"{_FINAL_SELECT} "
            f"WHERE {where} "
            f"ORDER BY fl.workdate, fl.driverid, fl.linetype"
        ),
        params,
    )
    return [
        FinalLineSummary(
            final_line_id=r["finallineid"],
            period_id=r["payrollperiodid"],
            branch_id=r["branchid"],
            driver_id=r["driverid"],
            driver_name=r["drivername"],
            draft_line_id=r["draftlineid"],
            work_date=r["workdate"],
            line_type=r["linetype"],
            line_scope=r["linescope"],
            quantity=r["quantity"],
            rate_amount=r["rateamount"],
            final_amount=r["finalamount"],
            source_type=r["sourcetype"],
            approved_by_user_id=r["approvedbyuserid"],
            approved_at_utc=r["approvedatutc"],
            locked_at_utc=r["lockedatutc"],
            notes=r["notes"],
            pay_item_id=r["payitemid"],
            rate_type_id=r["ratetypeid"],
            driver_rate_id=r["driverrateid"],
            resolved_rate_amount=r["resolvedrateamount"],
            rate_behavior=r["ratebehavior"],
            source_snapshot=r["sourcesnapshot"],
        )
        for r in result.mappings().all()
    ]


# ===========================================================================
# Pay Rates — rate type catalog + driver rate matrix
# ===========================================================================

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_RATE_SELECT = """
    SELECT
        dr.driverrateid,
        dr.companyid,
        dr.branchid,
        dr.driverid,
        e.fullname       AS drivername,
        dr.ratetypeid,
        rt.ratecode,
        rt.ratename,
        rt.unitname,
        dr.amount,
        dr.effectivefrom,
        dr.effectiveto,
        dr.status,
        dr.createdbyuserid,
        dr.createdatutc,
        dr.approvedbyuserid,
        dr.approvedatutc,
        dr.notes,
        dr.blocksize,
        dr.roundingrule
    FROM   payroll.driverrates      dr
    JOIN   payroll.ratetypes        rt ON rt.ratetypeid  = dr.ratetypeid
    JOIN   core.drivers              d  ON d.driverid    = dr.driverid
    JOIN   core.employees            e  ON e.employeeid  = d.employeeid
"""


def _rate_row_to_summary(r: Any) -> DriverRateSummary:
    return DriverRateSummary(
        driver_rate_id=r["driverrateid"],
        company_id=r["companyid"],
        branch_id=r["branchid"],
        driver_id=r["driverid"],
        driver_name=r["drivername"],
        rate_type_id=r["ratetypeid"],
        rate_code=r["ratecode"],
        rate_name=r["ratename"],
        unit_name=r["unitname"],
        amount=r["amount"],
        effective_from=r["effectivefrom"],
        effective_to=r["effectiveto"],
        status=r["status"],
        created_by_user_id=r["createdbyuserid"],
        created_at_utc=r["createdatutc"],
        approved_by_user_id=r["approvedbyuserid"],
        approved_at_utc=r["approvedatutc"],
        notes=r["notes"],
        block_size=r["blocksize"],
        rounding_rule=r["roundingrule"],
        # tiers are not loaded here (list-endpoint performance).
        # Use _get_rate_with_tiers() for detail responses.
    )


async def _get_rate_by_id(
    rate_id: int,
    company_id: int,
    db: AsyncConnection,
) -> DriverRateSummary:
    result = await db.execute(
        text(f"{_RATE_SELECT} WHERE dr.driverrateid = :rid AND dr.companyid = :cid"),
        {"rid": rate_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Driver rate not found.")
    return _rate_row_to_summary(row)


async def _get_rate_with_tiers(
    rate_id: int,
    company_id: int,
    db: AsyncConnection,
) -> DriverRateSummary:
    """Like _get_rate_by_id but also loads DriverRateTiers into the response."""
    rate = await _get_rate_by_id(rate_id, company_id, db)
    tiers = await _load_tiers(rate_id, db)
    return rate.model_copy(update={"tiers": tiers})


# ---------------------------------------------------------------------------
# M13c: Tier helpers
# ---------------------------------------------------------------------------

async def _assert_rate_type_allowed_for_company(
    db: AsyncConnection,
    company_id: int,
    rate_type_id: int,
) -> None:
    """
    Guard: ensure rate_type_id is usable by company_id.

    Phase 4C: uses the structural RateTypes.CompanyID column as the primary check.

    Decision table:
      RateTypes.CompanyID IS NULL                  -> system type, ALLOW
      RateTypes.CompanyID = company_id             -> own custom type, ALLOW
      RateTypes.CompanyID != company_id (NOT NULL) -> foreign type, REJECT
      RateType inactive or not found               -> REJECT

    The mapping-based three-flag logic from Phase 4B is preserved as a secondary
    defence-in-depth validation for system types (ensuring a system-type RateType
    actually has a system PayItem mapping and is not merely an unmapped row that
    somehow kept companyid=NULL after the migration).
    """
    # Step 1: fetch the rate type row (existence, activity, and structural ownership).
    rt_result = await db.execute(
        text("""
            SELECT ratetypeid, ratecode, companyid, isactive
            FROM   payroll.ratetypes
            WHERE  ratetypeid = :rtid
        """),
        {"rtid": rate_type_id},
    )
    rt_row = rt_result.mappings().first()

    if rt_row is None or not rt_row["isactive"]:
        raise HTTPException(status_code=422, detail="rate_type_id does not exist or is inactive.")

    rt_company = rt_row["companyid"]  # NULL = system, non-NULL = company-owned

    # Step 2: structural ownership check (Phase 4C primary guard).
    if rt_company is None:
        return  # system/global type -- allowed for any company
    if rt_company == company_id:
        return  # own-company custom type -- allowed
    # rt_company is set to a different company: reject.
    raise HTTPException(
        status_code=422,
        detail="Rate type does not belong to this company.",
    )


async def _is_status_rate_type(rate_type_id: int, db: AsyncConnection) -> bool:
    """Return True if rate_type_id is referenced by any active StatusRateColumns row."""
    row = (await db.execute(
        text("""
            SELECT 1 FROM payroll.statusratecolumns
            WHERE ratetypeid = :rtid AND isactive = TRUE
            LIMIT 1
        """),
        {"rtid": rate_type_id},
    )).first()
    return row is not None


async def _assert_status_rate_type_for_branch(
    rate_type_id: int,
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> None:
    """
    CP-2D2 branch guard for status-payment RateTypes.

    If rate_type_id is referenced by payroll.StatusRateColumns (anywhere), then it is a
    status-only RateType and must have an active StatusRateColumns row for (branch_id,
    company_id) specifically.  This prevents a driver in branch A from creating rates
    using a status RateType that only exists for branch B.

    Raises HTTPException 422 if the RateType is status-only but does not belong to the
    driver's branch.  Does nothing for ordinary PayItem RateTypes.
    """
    # First: is this a status-backed RateType at all?
    is_status = await _is_status_rate_type(rate_type_id, db)
    if not is_status:
        return  # ordinary PayItem rate — existing validation applies

    # Second: require an active StatusRateColumns row for this specific branch/company.
    branch_row = (await db.execute(
        text("""
            SELECT 1 FROM payroll.statusratecolumns
            WHERE ratetypeid = :rtid
              AND branchid   = :bid
              AND companyid  = :cid
              AND isactive   = TRUE
            LIMIT 1
        """),
        {"rtid": rate_type_id, "bid": branch_id, "cid": company_id},
    )).first()
    if branch_row is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"rate_type_id={rate_type_id} is a status-payment RateType but is not "
                "configured for this driver's branch. Use the batch rate save endpoint "
                "with status_rate_column_id to set status pay rates."
            ),
        )


async def _resolve_rate_behavior(
    rate_type_id: int,
    company_id: int,
    db: AsyncConnection,
) -> str:
    """
    Return the RateBehavior of the PayItem mapped to this RateType for this company.

    Company-specific (custom) items take priority over system items (companyid IS NULL).

    Phase 8 — Fail-closed: raises HTTPException 422 when no PayItemRateTypeMap entry
    exists for this RateType.

    CP-2D2 exception: RateTypes that are referenced by active StatusRateColumns rows are
    status-payment-only types that always use PerUnit behavior (HoursValue × Amount).
    Membership is verified via a DB lookup — RateCode prefix alone is not sufficient.
    """
    # CP-2D2: StatusRateColumn-backed types skip PayItemRateTypeMap resolution.
    # Verified by actual DB membership, not by RateCode prefix.
    if await _is_status_rate_type(rate_type_id, db):
        return "PerUnit"

    result = await db.execute(
        text("""
            SELECT pi.ratebehavior
            FROM   payroll.payitemratetypemap pirtm
            JOIN   payroll.payitems pi ON pi.payitemid = pirtm.payitemid
            WHERE  pirtm.ratetypeid = :rtid
              AND  pirtm.status     = 'Active'
              AND  pi.status        = 'Active'
              AND  (pi.companyid = :cid OR pi.companyid IS NULL)
            ORDER BY
                CASE WHEN pi.companyid = :cid THEN 0 ELSE 1 END,
                pirtm.isprimary DESC
            LIMIT 1
        """),
        {"rtid": rate_type_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Rate behavior could not be resolved for this rate type. "
                "Please configure the pay item rate mapping before creating "
                "or approving driver rates for this rate type."
            ),
        )
    return row["ratebehavior"]


async def _load_tiers(
    rate_id: int,
    db: AsyncConnection,
) -> list[TierSummary]:
    """Load all DriverRateTiers for a rate, ordered by TierSequence."""
    result = await db.execute(
        text("""
            SELECT tiersequence, fromunit, tounit, tieramount
            FROM   payroll.driverratetiers
            WHERE  driverrateid = :rid
            ORDER  BY tiersequence
        """),
        {"rid": rate_id},
    )
    return [
        TierSummary(
            tier_sequence=r["tiersequence"],
            from_unit=r["fromunit"],
            to_unit=r["tounit"],
            tier_amount=r["tieramount"],
        )
        for r in result.mappings().all()
    ]


async def _insert_tiers(
    rate_id: int,
    prepared: list[dict],
    db: AsyncConnection,
) -> None:
    """Delete existing tiers for rate_id, then bulk-insert the prepared list."""
    await db.execute(
        text("DELETE FROM payroll.driverratetiers WHERE driverrateid = :rid"),
        {"rid": rate_id},
    )
    for t in prepared:
        await db.execute(
            text("""
                INSERT INTO payroll.driverratetiers
                    (driverrateid, tiersequence, fromunit, tounit, tieramount)
                VALUES (:rid, :seq, :from_unit, :to_unit, :amount)
            """),
            {
                "rid":       rate_id,
                "seq":       t["tier_sequence"],
                "from_unit": t["from_unit"],
                "to_unit":   t["to_unit"],
                "amount":    t["tier_amount"],
            },
        )


def _validate_ordinal_tiers(
    tiers: list[OrdinalTierCreate],
) -> list[dict]:
    """
    Validate OrdinalTier tier list and return prepared dicts ready for DB insert.

    Rules:
      - At least 1 tier.
      - TierSequence gapless: 1, 2, ..., N.
      - Tier 1 from_unit must == 1.
      - Each tier i > 1: from_unit == prev to_unit + 1 (integer, contiguous).
      - Last tier: to_unit must be None.
      - All other tiers: to_unit must not be None.
      - All from_unit / to_unit must be integers (no fractional ordinal positions).
      - tier_amount > 0 (enforced by OrdinalTierCreate validator).
    """
    if not tiers:
        raise HTTPException(422, "OrdinalTier rates require at least one tier.")

    seqs = [t.tier_sequence for t in tiers]
    if seqs != list(range(1, len(tiers) + 1)):
        raise HTTPException(422, "ordinal_tiers: tier_sequence must be gapless starting at 1.")

    sorted_tiers = sorted(tiers, key=lambda t: t.tier_sequence)

    if sorted_tiers[0].from_unit != 1:
        raise HTTPException(422, "OrdinalTier: first tier from_unit must be 1.")

    result: list[dict] = []
    for i, tier in enumerate(sorted_tiers):
        is_last = (i == len(sorted_tiers) - 1)

        if not is_last:
            if tier.to_unit is None:
                raise HTTPException(
                    422,
                    f"Tier {tier.tier_sequence}: only the last tier can have to_unit=NULL.",
                )
            if tier.to_unit < tier.from_unit:
                raise HTTPException(
                    422,
                    f"Tier {tier.tier_sequence}: to_unit must be >= from_unit.",
                )
            next_tier = sorted_tiers[i + 1]
            expected_from = tier.to_unit + 1
            if next_tier.from_unit != expected_from:
                raise HTTPException(
                    422,
                    f"Tier {next_tier.tier_sequence}: from_unit must equal previous to_unit + 1 "
                    f"(expected {expected_from}, got {next_tier.from_unit}).",
                )
        else:
            if tier.to_unit is not None:
                raise HTTPException(
                    422,
                    "OrdinalTier: the last tier must have to_unit=NULL (open-ended).",
                )

        result.append({
            "tier_sequence": tier.tier_sequence,
            "from_unit":     Decimal(str(tier.from_unit)),
            "to_unit":       None if tier.to_unit is None else Decimal(str(tier.to_unit)),
            "tier_amount":   tier.tier_amount,
        })

    return result


def _validate_range_tiers(
    tiers: list[RangeTierCreate],
) -> list[dict]:
    """
    Validate RangeBracket / RangeProgressive tiers and derive from_unit for each.

    Rules:
      - At least 2 tiers.
      - TierSequence gapless: 1, 2, ..., N.
      - Tier 1 from_unit is derived as 0.
      - Each tier i > 1: from_unit derived == previous tier's to_unit.
      - Each to_unit must be strictly greater than the previous to_unit.
      - Last tier: to_unit must be None.
      - All other tiers: to_unit must not be None.
      - tier_amount > 0 (enforced by RangeTierCreate validator).

    Boundary semantics (inclusive upper, exclusive upper for next tier):
      Tier 1: 0   <= qty <= to_unit_1
      Tier i: to_unit_{i-1} < qty <= to_unit_i
      Last:   qty > to_unit_{N-1}
    """
    if len(tiers) < 2:
        raise HTTPException(422, "Range tiers require at least 2 tiers.")

    seqs = [t.tier_sequence for t in tiers]
    if seqs != list(range(1, len(tiers) + 1)):
        raise HTTPException(422, "range_tiers: tier_sequence must be gapless starting at 1.")

    sorted_tiers = sorted(tiers, key=lambda t: t.tier_sequence)

    result: list[dict] = []
    for i, tier in enumerate(sorted_tiers):
        is_last = (i == len(sorted_tiers) - 1)

        from_unit = Decimal("0") if i == 0 else Decimal(str(sorted_tiers[i - 1].to_unit))

        if not is_last:
            if tier.to_unit is None:
                raise HTTPException(
                    422,
                    f"Tier {tier.tier_sequence}: only the last tier can have to_unit=NULL.",
                )
            if i > 0:
                prev_to = Decimal(str(sorted_tiers[i - 1].to_unit))
                if Decimal(str(tier.to_unit)) <= prev_to:
                    raise HTTPException(
                        422,
                        f"Tier {tier.tier_sequence}: to_unit must be strictly greater than "
                        f"previous tier's to_unit ({prev_to}).",
                    )
        else:
            if tier.to_unit is not None:
                raise HTTPException(
                    422,
                    "Range tiers: the last tier must have to_unit=NULL (open-ended).",
                )

        result.append({
            "tier_sequence": tier.tier_sequence,
            "from_unit":     from_unit,
            "to_unit":       None if tier.to_unit is None else Decimal(str(tier.to_unit)),
            "tier_amount":   tier.tier_amount,
        })

    return result


# ---------------------------------------------------------------------------
# M13c: Calculation helpers for tiered / block behaviors
# ---------------------------------------------------------------------------

async def _compute_ordinal_tier(
    quantity: Decimal,
    driver_id: int,
    company_id: int,
    as_of_date: date,
    rate_code: str,
    db: AsyncConnection,
) -> tuple[Decimal | None, bool, int | None, int | None]:
    """
    OrdinalTier: each successive unit gets the rate for its ordinal position.
    quantity must be a positive integer (validated at draft-line entry).

    Returns (calculated_amount, needs_manager_review, driver_rate_id, rate_type_id).
    Phase 3B: driver_rate_id and rate_type_id added for source snapshot.

    Example: tiers [(1,1,$50),(2,5,$40),(6+,$35)], qty=7 →
      1×$50 + 4×$40 + 2×$35 = $50 + $160 + $70 = $280
    """
    rate_result = await db.execute(
        text("""
            SELECT dr.driverrateid, dr.ratetypeid
            FROM   payroll.driverrates  dr
            JOIN   payroll.ratetypes    rt ON rt.ratetypeid = dr.ratetypeid
            WHERE  dr.driverid         = :did
              AND  dr.companyid        = :cid
              AND  rt.ratecode         = :rcode
              AND  dr.status           IN ('Approved', 'Superseded')
              AND  dr.effectivefrom   <= :dt
              AND  (dr.effectiveto IS NULL OR dr.effectiveto >= :dt)
            ORDER BY dr.effectivefrom DESC
            LIMIT 1
        """),
        {"did": driver_id, "cid": company_id, "rcode": rate_code, "dt": as_of_date},
    )
    rate_row = rate_result.mappings().first()
    if rate_row is None:
        return (None, True, None, None)

    rid  = int(rate_row["driverrateid"])
    rtid = int(rate_row["ratetypeid"])

    tiers = await _load_tiers(rid, db)
    if not tiers:
        return (None, True, None, None)

    units = int(quantity)
    result = Decimal("0")
    for i in range(1, units + 1):
        tier = next(
            (
                t for t in tiers
                if Decimal(str(t.from_unit)) <= i
                and (t.to_unit is None or Decimal(str(t.to_unit)) >= i)
            ),
            None,
        )
        if tier is None:
            # Ordinal position not covered — defensive; should not happen with valid tiers
            return (None, True, None, None)
        result += Decimal(str(tier.tier_amount))

    return (result.quantize(Decimal("0.0001")), False, rid, rtid)


async def _compute_range_bracket(
    quantity: Decimal,
    driver_id: int,
    company_id: int,
    as_of_date: date,
    rate_code: str,
    db: AsyncConnection,
) -> tuple[Decimal | None, bool, int | None, int | None]:
    """
    RangeBracket: entire quantity gets one rate — whichever bracket contains qty.

    Returns (calculated_amount, needs_manager_review, driver_rate_id, rate_type_id).
    Phase 3B: driver_rate_id and rate_type_id added for source snapshot.

    Boundary semantics:
      Tier 1: 0   <= qty <= to_unit_1   (inclusive upper; qty=to_unit belongs to tier 1)
      Tier i: to_unit_{i-1} < qty <= to_unit_i
      Last:   qty > to_unit_{N-1}
    """
    rate_result = await db.execute(
        text("""
            SELECT dr.driverrateid, dr.ratetypeid
            FROM   payroll.driverrates  dr
            JOIN   payroll.ratetypes    rt ON rt.ratetypeid = dr.ratetypeid
            WHERE  dr.driverid         = :did
              AND  dr.companyid        = :cid
              AND  rt.ratecode         = :rcode
              AND  dr.status           IN ('Approved', 'Superseded')
              AND  dr.effectivefrom   <= :dt
              AND  (dr.effectiveto IS NULL OR dr.effectiveto >= :dt)
            ORDER BY dr.effectivefrom DESC
            LIMIT 1
        """),
        {"did": driver_id, "cid": company_id, "rcode": rate_code, "dt": as_of_date},
    )
    rate_row = rate_result.mappings().first()
    if rate_row is None:
        return (None, True, None, None)

    rid  = int(rate_row["driverrateid"])
    rtid = int(rate_row["ratetypeid"])

    tiers = await _load_tiers(rid, db)
    if not tiers:
        return (None, True, None, None)

    bracket = None
    for i, tier in enumerate(tiers):
        if i == 0:
            # First tier: from_unit (0) <= qty <= to_unit
            if tier.to_unit is None or quantity <= Decimal(str(tier.to_unit)):
                bracket = tier
                break
        else:
            prev_to = Decimal(str(tiers[i - 1].to_unit))
            if quantity > prev_to:
                if tier.to_unit is None or quantity <= Decimal(str(tier.to_unit)):
                    bracket = tier
                    break

    if bracket is None:
        return (None, True, None, None)

    result = (quantity * Decimal(str(bracket.tier_amount))).quantize(Decimal("0.0001"))
    return (result, False, rid, rtid)


async def _compute_range_progressive(
    quantity: Decimal,
    driver_id: int,
    company_id: int,
    as_of_date: date,
    rate_code: str,
    db: AsyncConnection,
) -> tuple[Decimal | None, bool, int | None, int | None]:
    """
    RangeProgressive: each slice of quantity is rated at the rate for that tier.

    Same tier structure as RangeBracket; different calculation (marginal not flat).

    Example: tiers [(0,10,$1),(10,20,$2),(20+,$3)], qty=25 →
      10×$1 + 10×$2 + 5×$3 = $10 + $20 + $15 = $45
    """
    rate_result = await db.execute(
        text("""
            SELECT dr.driverrateid, dr.ratetypeid
            FROM   payroll.driverrates  dr
            JOIN   payroll.ratetypes    rt ON rt.ratetypeid = dr.ratetypeid
            WHERE  dr.driverid         = :did
              AND  dr.companyid        = :cid
              AND  rt.ratecode         = :rcode
              AND  dr.status           IN ('Approved', 'Superseded')
              AND  dr.effectivefrom   <= :dt
              AND  (dr.effectiveto IS NULL OR dr.effectiveto >= :dt)
            ORDER BY dr.effectivefrom DESC
            LIMIT 1
        """),
        {"did": driver_id, "cid": company_id, "rcode": rate_code, "dt": as_of_date},
    )
    rate_row = rate_result.mappings().first()
    if rate_row is None:
        return (None, True, None, None)

    rid  = int(rate_row["driverrateid"])
    rtid = int(rate_row["ratetypeid"])
    tiers = await _load_tiers(rid, db)
    if not tiers:
        return (None, True, None, None)

    result = Decimal("0")
    remaining = quantity

    for i, tier in enumerate(tiers):
        if remaining <= 0:
            break
        from_unit = Decimal(str(tier.from_unit))
        to_unit   = Decimal(str(tier.to_unit)) if tier.to_unit is not None else None

        # Capacity of this tier slice
        if to_unit is not None:
            tier_capacity = to_unit - from_unit
        else:
            tier_capacity = remaining  # open-ended last tier: consume all remaining

        units_in_tier = min(remaining, tier_capacity)
        result   += units_in_tier * Decimal(str(tier.tier_amount))
        remaining -= units_in_tier

    return (result.quantize(Decimal("0.0001")), False, rid, rtid)


async def _compute_block(
    quantity: Decimal,
    driver_id: int,
    company_id: int,
    as_of_date: date,
    rate_code: str,
    db: AsyncConnection,
) -> tuple[Decimal | None, bool, int | None, int | None]:
    """
    Block: flat dollar amount per complete block of units.

    number_of_blocks = qty / block_size, rounded per RoundingRule:
      Floor:         floor(raw_blocks)
      Ceiling:       ceil(raw_blocks)
      NearestHalfUp: floor(raw_blocks + 0.5)  — always rounds .5 up (no banker's rounding)

    A result of 0 blocks is valid (Floor rounding with qty < block_size) — not flagged
    for review.
    """
    rate_result = await db.execute(
        text("""
            SELECT dr.driverrateid, dr.ratetypeid, dr.amount, dr.blocksize, dr.roundingrule
            FROM   payroll.driverrates  dr
            JOIN   payroll.ratetypes    rt ON rt.ratetypeid = dr.ratetypeid
            WHERE  dr.driverid         = :did
              AND  dr.companyid        = :cid
              AND  rt.ratecode         = :rcode
              AND  dr.status           IN ('Approved', 'Superseded')
              AND  dr.effectivefrom   <= :dt
              AND  (dr.effectiveto IS NULL OR dr.effectiveto >= :dt)
            ORDER BY dr.effectivefrom DESC
            LIMIT 1
        """),
        {"did": driver_id, "cid": company_id, "rcode": rate_code, "dt": as_of_date},
    )
    rate_row = rate_result.mappings().first()
    if rate_row is None:
        return (None, True, None, None)

    rid           = int(rate_row["driverrateid"])
    rtid          = int(rate_row["ratetypeid"])
    amount        = Decimal(str(rate_row["amount"]))
    block_size    = Decimal(str(rate_row["blocksize"]))
    rounding_rule = rate_row["roundingrule"]

    raw_blocks = quantity / block_size
    if rounding_rule == "Floor":
        blocks = math.floor(raw_blocks)
    elif rounding_rule == "Ceiling":
        blocks = math.ceil(raw_blocks)
    else:  # NearestHalfUp: floor(x + 0.5) — never uses Python's banker's rounding
        blocks = math.floor(raw_blocks + Decimal("0.5"))

    result = (Decimal(str(blocks)) * amount).quantize(Decimal("0.0001"))
    return (result, False, rid, rtid)


# ---------------------------------------------------------------------------
# Audit helper for rate events
# ---------------------------------------------------------------------------

_RATE_AUDIT_REASONS: dict[str, str] = {
    "RATE_CREATED":    "Driver rate created",
    "RATE_UPDATED":    "Driver rate updated",
    "RATE_APPROVED":   "Driver rate approved",
    "RATE_SUPERSEDED": "Driver rate superseded by newer approved rate",
    "RATE_VOIDED":     "Driver rate voided",
}


async def _write_rate_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    branch_id: int,
    user_id: int,
    rate_id: int,
    action_code: str,
    old_value: dict | None = None,
    new_value: dict | None = None,
) -> None:
    """
    Insert one row into audit.AuditLog for a rate event.

    Extracted as a module-level function so tests can monkeypatch it to verify
    that all preceding writes roll back when this raises.
    """
    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid,
                 oldvaluejson, newvaluejson, reason, sourcetype)
            VALUES
                (:company_id, :branch_id, :actor_id, :action_code,
                 'payroll', 'DriverRates', :entity_id,
                 :old_val, :new_val, :reason, 'Application')
        """),
        {
            "company_id":  company_id,
            "branch_id":   branch_id,
            "actor_id":    user_id,
            "action_code": action_code,
            "entity_id":   str(rate_id),
            "old_val":     json.dumps(old_value)  if old_value  is not None else None,
            "new_val":     json.dumps(new_value)  if new_value  is not None else None,
            "reason":      _RATE_AUDIT_REASONS.get(action_code, action_code),
        },
    )


# ---------------------------------------------------------------------------
# OwnDriverDataOnly scope guard — shared by all rate read/write endpoints
# ---------------------------------------------------------------------------

async def _get_oda_own_driver_id(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> int | None:
    """
    Returns the caller's own driver_id if and only if they have exactly one
    active role assignment AND it has OwnDriverDataOnly scope.

    Returns None (no ODA restriction) when:
    - No active assignment exists.
    - The single active assignment is SpecificBranch or AllCompanyBranches.
    - Multiple active assignments all have non-ODA scope (unusual but safe).

    Raises HTTP 403 (fail-closed) when multiple active assignments exist and
    ANY of them has OwnDriverDataOnly scope — this indicates data corruption
    that assign_company_role's revoke logic should have prevented.

    Raises HTTP 403 if ODA scope is confirmed but the user has no linked
    driver profile (Users.EmployeeID → Employees → Drivers).
    """
    scope_result = await db.execute(
        text("""
            SELECT scopetype, userbranchroleid
            FROM   sec.userbranchroles
            WHERE  userid    = :uid
              AND  companyid = :cid
              AND  isactive  = TRUE
            ORDER  BY userbranchroleid DESC
        """),
        {"uid": user_id, "cid": company_id},
    )
    scope_rows = scope_result.mappings().all()

    if not scope_rows:
        return None  # No active assignment — no ODA restriction

    if len(scope_rows) > 1:
        # Multiple active assignments: fail-closed if any is ODA (bad data).
        scope_types = {r["scopetype"] for r in scope_rows}
        if "OwnDriverDataOnly" in scope_types:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Ambiguous role assignments: multiple active company-role assignments "
                    "exist, including OwnDriverDataOnly scope. Access denied until "
                    "assignments are resolved by an administrator."
                ),
            )
        return None  # Multiple non-ODA assignments — no ODA restriction here

    # Exactly one active assignment
    if scope_rows[0]["scopetype"] != "OwnDriverDataOnly":
        return None  # AllCompanyBranches or SpecificBranch — no ODA restriction

    # ODA confirmed: resolve caller's linked driver profile
    own_drv_result = await db.execute(
        text("""
            SELECT d.driverid
            FROM   sec.users      u
            JOIN   core.employees e ON e.employeeid = u.employeeid
            JOIN   core.drivers   d ON d.employeeid = e.employeeid
                                    AND d.companyid  = :cid
                                    AND d.driverstatus NOT IN ('Transferred', 'Terminated')
            WHERE  u.userid = :uid
        """),
        {"uid": user_id, "cid": company_id},
    )
    own_drv_row = own_drv_result.mappings().first()
    if own_drv_row is None:
        raise HTTPException(
            status_code=403,
            detail="OwnDriverDataOnly: no linked driver profile found for your account.",
        )
    return int(own_drv_row["driverid"])


async def _check_own_driver_only(
    company_id: int,
    user_id: int,
    target_driver_id: int,
    db: AsyncConnection,
) -> None:
    """
    If the caller's single active assignment has OwnDriverDataOnly scope,
    verify that *target_driver_id* is their own linked driver.

    Raises HTTP 403 if the scope is OwnDriverDataOnly and the target driver
    does not match, if no linked driver profile exists, or if multiple active
    assignments exist with conflicting ODA scope (fail-closed).

    No-op for AllCompanyBranches and SpecificBranch scopes.

    Call this AFTER the branch-access check so that scope type is already
    known to be within the caller's company.
    """
    own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)
    if own_driver_id is None:
        return  # Not ODA — no restriction
    if own_driver_id != target_driver_id:
        raise HTTPException(
            status_code=403,
            detail="OwnDriverDataOnly: you may only access your own driver's rates.",
        )


# ---------------------------------------------------------------------------
# Finalized-period guard — shared by approve_rate and batch_save_rates
# ---------------------------------------------------------------------------

async def _check_not_in_finalized_period(
    company_id: int,
    branch_id: int,
    effective_from: date,
    db: AsyncConnection,
    *,
    label: str = "rate",
) -> None:
    """
    Raise HTTP 422 if effective_from falls inside a Locked or Archived payroll
    period for the same company and branch.

    Called before approving or auto-approving a rate (label="rate") or creating/
    copying a pay rule (label="pay rule") so that payroll history cannot be
    retroactively altered inside finalized pay periods.
    """
    result = await db.execute(
        text("""
            SELECT payrollperiodid, startdate, enddate, status
            FROM   payroll.payrollperiods
            WHERE  companyid = :cid
              AND  branchid  = :bid
              AND  status    IN ('Locked', 'Archived')
              AND  startdate <= CAST(:eff_from AS date)
              AND  enddate   >= CAST(:eff_from AS date)
            LIMIT 1
        """),
        {"cid": company_id, "bid": branch_id, "eff_from": effective_from},
    )
    row = result.mappings().first()
    if row is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot set a {label} effective inside a finalized payroll period "
                f"({row['status']} period {row['startdate']} to {row['enddate']})."
            ),
        )


# ---------------------------------------------------------------------------
# List rate types (reference catalog — no branch scope needed)
# ---------------------------------------------------------------------------

async def get_rate_types(
    company_id: int,
    db: AsyncConnection,
    *,
    active_only: bool = True,
) -> list[RateTypeSummary]:
    """
    Return rate types visible to the requesting company.

    Phase 4C structural ownership rule:
      SHOW if:
        - rt.companyid IS NULL          -- system/global type (usable by all companies)
        - rt.companyid = company_id     -- own company's custom type

      HIDE everything else:
        - rt.companyid = other company  -- foreign-owned custom type
        - rt.isactive = FALSE           -- inactive (when active_only=True)

    This replaces the Phase 4B.4 three-flag PayItemRateTypeMap analysis.
    The structural column makes the check O(1) and cannot be contaminated
    by direct-DB PayItemRateTypeMap inserts.
    """
    params: dict[str, Any] = {"cid": company_id}
    active_filter = "AND rt.isactive = TRUE" if active_only else ""

    result = await db.execute(
        text(f"""
            SELECT rt.ratetypeid, rt.ratecode, rt.ratename, rt.unitname, rt.isactive
            FROM   payroll.ratetypes rt
            WHERE
              (TRUE {active_filter})
              -- Phase 4C: structural ownership -- system OR own company
              AND (rt.companyid IS NULL OR rt.companyid = :cid)
            ORDER BY rt.ratename
        """),
        params,
    )
    return [
        RateTypeSummary(
            rate_type_id=r["ratetypeid"],
            rate_code=r["ratecode"],
            rate_name=r["ratename"],
            unit_name=r["unitname"],
            is_active=r["isactive"],
        )
        for r in result.mappings().all()
    ]


# ---------------------------------------------------------------------------
# List driver rates
# ---------------------------------------------------------------------------

async def get_rates(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    branch_id: int | None = None,
    driver_id: int | None = None,
    rate_status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[DriverRateSummary]:
    # OwnDriverDataOnly: detect scope early so we can pass the correct branch_id
    # to the permission check.  ODA users have permission scoped to a specific branch
    # (fn_UserHasPermission returns FALSE with NULL branch_id for ODA users).
    # Resolving own_driver_id first also lets us force the driver_id filter.
    own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)

    if own_driver_id is not None:
        # ODA user: look up own driver's branch for the permission check.
        oda_drv_result = await db.execute(
            text("SELECT branchid FROM core.drivers WHERE driverid = :did AND companyid = :cid"),
            {"did": own_driver_id, "cid": company_id},
        )
        oda_drv_row = oda_drv_result.mappings().first()
        perm_branch_id: int | None = oda_drv_row["branchid"] if oda_drv_row else branch_id

        # Permission gate using ODA driver's branch (so fn_UserHasPermission resolves correctly).
        await _check_any_permission(
            company_id, user_id, perm_branch_id,
            ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
        )

        # ODA enforcement: caller may only list their own driver's rates.
        if driver_id is not None and driver_id != own_driver_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="OwnDriverDataOnly: you may only list your own driver's rates.",
            )
        driver_id = own_driver_id  # force filter to own driver
    else:
        # Non-ODA: standard permission gate (branch_id=None is acceptable for AllCompanyBranches).
        await _check_any_permission(
            company_id, user_id, branch_id,
            ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
        )

    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    conditions: list[str] = ["dr.companyid = :company_id"]
    params: dict[str, Any] = {"company_id": company_id}

    # Branch scope
    if branch_id is not None:
        if not can_see_all and branch_id not in branch_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to the requested branch.",
            )
        conditions.append("dr.branchid = :branch_id")
        params["branch_id"] = branch_id
    elif not can_see_all:
        if not branch_ids:
            return []
        in_clause, in_params = _build_in_clause(branch_ids, "rb")
        conditions.append(f"dr.branchid IN ({in_clause})")
        params.update(in_params)

    if driver_id is not None:
        conditions.append("dr.driverid = :driver_id")
        params["driver_id"] = driver_id

    if rate_status is not None:
        conditions.append("dr.status = :rate_status")
        params["rate_status"] = rate_status

    where = " AND ".join(conditions)
    params["limit"] = limit
    params["offset"] = offset

    result = await db.execute(
        text(
            f"{_RATE_SELECT} "
            f"WHERE {where} "
            f"ORDER BY e.fullname, rt.ratename, dr.effectivefrom DESC "
            f"LIMIT :limit OFFSET :offset"
        ),
        params,
    )
    return [_rate_row_to_summary(r) for r in result.mappings().all()]


# ---------------------------------------------------------------------------
# Get single rate
# ---------------------------------------------------------------------------

async def get_rate_by_id(
    rate_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> DriverRateSummary:
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    rate = await _get_rate_with_tiers(rate_id, company_id, db)
    if not can_see_all and rate.branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this rate's branch.",
        )
    # Permission gate: reading a rate requires payrates.view or payrates.edit (or admin fallbacks)
    await _check_any_permission(
        company_id, user_id, rate.branch_id,
        ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
    )
    # OwnDriverDataOnly: caller may only read their own driver's rates
    await _check_own_driver_only(company_id, user_id, rate.driver_id, db)
    return rate


# ---------------------------------------------------------------------------
# Create rate
# ---------------------------------------------------------------------------

async def create_rate(
    company_id: int,
    user_id: int,
    data: DriverRateCreate,
    db: AsyncConnection,
) -> DriverRateSummary:
    """
    Create a new driver rate in PendingApproval status.

    Guards:
      - Driver must exist in this company.
      - User must have access to the driver's branch.
      - rate_type_id must exist and be active.
    """
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    # Resolve driver → branch
    drv_result = await db.execute(
        text("SELECT driverid, branchid FROM core.drivers WHERE driverid = :did AND companyid = :cid"),
        {"did": data.driver_id, "cid": company_id},
    )
    drv_row = drv_result.mappings().first()
    if drv_row is None:
        raise HTTPException(status_code=422, detail="driver_id does not exist in this company.")

    driver_branch_id: int = drv_row["branchid"]
    if not can_see_all and driver_branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this driver's branch.",
        )

    # Permission gate: creating a rate requires payrates.edit (or admin fallbacks)
    await _check_any_permission(
        company_id, user_id, driver_branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    # OwnDriverDataOnly: caller may only create rates for their own driver
    await _check_own_driver_only(company_id, user_id, data.driver_id, db)

    # Validate rate type: existence, activity, and company scope (closes P0 cross-company leak).
    await _assert_rate_type_allowed_for_company(db, company_id, data.rate_type_id)

    # CP-2D2: Status-payment RateTypes are branch-scoped; reject if this branch has no
    # active StatusRateColumns row for this RateType.
    await _assert_status_rate_type_for_branch(data.rate_type_id, company_id, driver_branch_id, db)

    # Fix 8: When this rate type is mapped to pay items (via PayItemRateTypeMap),
    # validate that at least one of those pay items is active for this branch.
    # Mirrors the activation logic in _validate_line_type:
    #   1. Explicit BranchPayItemConfig row → use isactive from that row.
    #   2. No config row → fall back to PayItems.IsDefaultBranchActive.
    # If the rate type has no PayItemRateTypeMap entries (e.g. Fixed-behavior OVERNIGHT),
    # the branch check is skipped — the rate type itself is valid, just not PerUnit-mapped.
    branch_rt_result = await db.execute(
        text("""
            SELECT
                pi.payitemid,
                pi.isdefaultbranchactive,
                bpic.isactive AS cfg_isactive
            FROM payroll.payitems pi
            JOIN payroll.payitemratetypemap pirm ON pirm.payitemid = pi.payitemid
              AND pirm.ratetypeid = :rtid AND pirm.status = 'Active'
            LEFT JOIN payroll.branchpayitemconfig bpic ON bpic.payitemid = pi.payitemid
              AND bpic.companyid = :cid AND bpic.branchid = :bid
              AND (bpic.effectiveto IS NULL OR bpic.effectiveto >= :effective_from)
            WHERE pi.status != 'Retired'
              AND pi.requiresrate = TRUE
              AND (pi.companyid IS NULL OR pi.companyid = :cid)
            ORDER BY bpic.effectivefrom DESC NULLS LAST
            LIMIT 1
        """),
        {
            "rtid": data.rate_type_id,
            "cid": company_id,
            "bid": driver_branch_id,
            "effective_from": data.effective_from,
        },
    )
    branch_rt_row = branch_rt_result.mappings().first()
    if branch_rt_row is not None:
        # Rate type IS mapped to a pay item — check branch activation
        if branch_rt_row["cfg_isactive"] is not None:
            rt_is_active = bool(branch_rt_row["cfg_isactive"])
        else:
            rt_is_active = bool(branch_rt_row["isdefaultbranchactive"])
        if not rt_is_active:
            raise HTTPException(
                status_code=422,
                detail="This rate type is not active for this driver's branch.",
            )
    # branch_rt_row is None: rate type has no PayItemRateTypeMap entry (e.g. Fixed/OVERNIGHT);
    # it is not restricted to branch pay item config — allow through (company scope already
    # validated above by _assert_rate_type_allowed_for_company).

    # M13c: resolve rate behavior and validate tier / block inputs BEFORE inserting.
    rate_behavior = await _resolve_rate_behavior(data.rate_type_id, company_id, db)

    # Validate and prepare tier / block data.
    prepared_tiers: list[dict] = []
    block_size:    Decimal | None = None
    rounding_rule: str | None = None

    if rate_behavior == "OrdinalTier":
        if not data.ordinal_tiers:
            raise HTTPException(422, "OrdinalTier rates require ordinal_tiers.")
        if data.range_tiers or data.block_size or data.rounding_rule:
            raise HTTPException(422, "OrdinalTier rates must not include range_tiers or block params.")
        prepared_tiers = _validate_ordinal_tiers(data.ordinal_tiers)

    elif rate_behavior in _RANGE_BEHAVIORS:
        if not data.range_tiers:
            raise HTTPException(422, f"{rate_behavior} rates require range_tiers.")
        if data.ordinal_tiers or data.block_size or data.rounding_rule:
            raise HTTPException(422, f"{rate_behavior} rates must not include ordinal_tiers or block params.")
        prepared_tiers = _validate_range_tiers(data.range_tiers)

    elif rate_behavior == "Block":
        if data.block_size is None or data.rounding_rule is None:
            raise HTTPException(422, "Block rates require both block_size and rounding_rule.")
        if data.block_size <= 0:
            raise HTTPException(422, "block_size must be > 0.")
        if data.ordinal_tiers or data.range_tiers:
            raise HTTPException(422, "Block rates must not include tier lists.")
        block_size = data.block_size
        rounding_rule = data.rounding_rule

    else:  # PerUnit, EnteredAmount, Fixed, None
        if data.ordinal_tiers or data.range_tiers:
            raise HTTPException(
                422,
                f"Tiers are not applicable to '{rate_behavior}' items. "
                "Remove ordinal_tiers or range_tiers from the request.",
            )
        if data.block_size or data.rounding_rule:
            raise HTTPException(
                422,
                f"block_size / rounding_rule are not applicable to '{rate_behavior}' items.",
            )

    insert_result = await db.execute(
        text("""
            INSERT INTO payroll.driverrates
                (companyid, branchid, driverid, ratetypeid, amount,
                 effectivefrom, effectiveto, status, createdbyuserid, notes,
                 blocksize, roundingrule)
            VALUES
                (:company_id, :branch_id, :driver_id, :rate_type_id, :amount,
                 :effective_from, :effective_to, 'PendingApproval', :created_by, :notes,
                 :block_size, :rounding_rule)
            RETURNING driverrateid
        """),
        {
            "company_id":     company_id,
            "branch_id":      driver_branch_id,
            "driver_id":      data.driver_id,
            "rate_type_id":   data.rate_type_id,
            "amount":         data.amount,
            "effective_from": data.effective_from,
            "effective_to":   data.effective_to,
            "created_by":     user_id,
            "notes":          data.notes,
            "block_size":     block_size,
            "rounding_rule":  rounding_rule,
        },
    )
    rate_id: int = insert_result.scalar_one()

    # Insert tier rows (inside same transaction).
    if prepared_tiers:
        await _insert_tiers(rate_id, prepared_tiers, db)

    # Audit write — inside the same transaction; failure rolls back everything.
    await _write_rate_audit(
        db,
        company_id=company_id,
        branch_id=driver_branch_id,
        user_id=user_id,
        rate_id=rate_id,
        action_code="RATE_CREATED",
        new_value={
            "driver_id":      data.driver_id,
            "rate_type_id":   data.rate_type_id,
            "rate_behavior":  rate_behavior,
            "amount":         str(data.amount),
            "effective_from": str(data.effective_from),
            "effective_to":   str(data.effective_to) if data.effective_to else None,
            "status":         "PendingApproval",
            "tier_count":     len(prepared_tiers) if prepared_tiers else None,
            "block_size":     str(block_size) if block_size else None,
            "rounding_rule":  rounding_rule,
        },
    )

    return await _get_rate_with_tiers(rate_id, company_id, db)


# ---------------------------------------------------------------------------
# Update rate (PendingApproval only)
# ---------------------------------------------------------------------------

async def update_rate(
    rate_id: int,
    company_id: int,
    user_id: int,
    data: DriverRateUpdate,
    db: AsyncConnection,
) -> DriverRateSummary:
    """
    Partially update a rate.  Only PendingApproval rates may be edited;
    Approved/Superseded/Voided rates are immutable.
    """
    rate = await get_rate_by_id(rate_id, company_id, user_id, db)

    if rate.status != "PendingApproval":
        raise HTTPException(
            status_code=422,
            detail=(
                f"Only PendingApproval rates can be edited "
                f"(current status: '{rate.status}')."
            ),
        )

    # Permission gate: editing a rate requires payrates.edit (or admin fallbacks)
    await _check_any_permission(
        company_id, user_id, rate.branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    # OwnDriverDataOnly: caller may only edit their own driver's rates
    await _check_own_driver_only(company_id, user_id, rate.driver_id, db)

    # Defensive cross-company scope guard: reject contaminated rows that reference
    # another company's RateType (e.g. created before P0 was closed).
    await _assert_rate_type_allowed_for_company(db, company_id, rate.rate_type_id)

    # CP-2D2: Status-payment RateTypes are branch-scoped; reject if this branch has no
    # active StatusRateColumns row for this RateType.
    await _assert_status_rate_type_for_branch(rate.rate_type_id, company_id, rate.branch_id, db)

    # Validate date range if either date is being changed
    new_from = data.effective_from if data.effective_from is not None else rate.effective_from
    new_to   = data.effective_to   if data.effective_to   is not None else rate.effective_to
    if new_to is not None and new_to <= new_from:
        raise HTTPException(
            status_code=422,
            detail="effective_to must be strictly after effective_from.",
        )

    # M13c: resolve behavior to validate any tier / block updates.
    rate_behavior = await _resolve_rate_behavior(rate.rate_type_id, company_id, db)

    fields: dict[str, Any] = {}
    if data.amount         is not None: fields["amount"]        = data.amount
    if data.effective_from is not None: fields["effectivefrom"] = data.effective_from
    if data.effective_to   is not None: fields["effectiveto"]   = data.effective_to
    if data.notes          is not None: fields["notes"]         = data.notes

    # M13c: block metadata updates
    if data.block_size is not None or data.rounding_rule is not None:
        if rate_behavior != "Block":
            raise HTTPException(
                422,
                f"block_size / rounding_rule are not applicable to '{rate_behavior}' items.",
            )
        if data.block_size is not None:
            if data.block_size <= 0:
                raise HTTPException(422, "block_size must be > 0.")
            fields["blocksize"] = data.block_size
        if data.rounding_rule is not None:
            fields["roundingrule"] = data.rounding_rule

    # Phase 12C — status-predicated scalar UPDATE.
    # Do NOT rely solely on the earlier status check: a concurrent approve_rate can
    # transition the row from PendingApproval → Approved between that check and this
    # UPDATE.  Adding AND status='PendingApproval' to the WHERE clause makes the UPDATE
    # a no-op (0 rows) if the row was concurrently approved, and RETURNING lets us
    # detect that atomically.  This prevents updating the amount/dates/block fields of
    # an already-Approved rate through a stale pending path.
    fields_updated = False
    if fields:
        set_clause = ", ".join(f"{col} = :{col}" for col in fields)
        upd_result = await db.execute(
            text(f"""
                UPDATE payroll.driverrates
                SET    {set_clause}
                WHERE  driverrateid = :rid
                  AND  companyid    = :company_id
                  AND  status       = 'PendingApproval'
                RETURNING driverrateid
            """),
            {**fields, "rid": rate_id, "company_id": company_id},
        )
        if upd_result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=409,
                detail="Rate changed while editing. Refresh and try again.",
            )
        fields_updated = True

    # M13c: tier replacement (replace-all if provided).
    # Phase 12C — tier lock: before replacing tiers, guarantee the parent row is still
    # PendingApproval.  If a scalar UPDATE ran above, its RETURNING already confirmed
    # the row status at UPDATE time.  If only tiers are being replaced (no scalar
    # fields), acquire a row-level lock with a status predicate so the DELETE/INSERT
    # cannot run against a concurrently-approved rate.
    tiers_updated = False
    if data.ordinal_tiers is not None or data.range_tiers is not None:
        if data.ordinal_tiers is not None and data.range_tiers is not None:
            raise HTTPException(422, "Provide either ordinal_tiers or range_tiers — not both.")
        if rate_behavior == "OrdinalTier":
            if data.range_tiers is not None:
                raise HTTPException(422, "OrdinalTier rates require ordinal_tiers, not range_tiers.")
            prepared = _validate_ordinal_tiers(data.ordinal_tiers)  # type: ignore[arg-type]
        elif rate_behavior in _RANGE_BEHAVIORS:
            if data.ordinal_tiers is not None:
                raise HTTPException(422, f"{rate_behavior} rates require range_tiers, not ordinal_tiers.")
            prepared = _validate_range_tiers(data.range_tiers)  # type: ignore[arg-type]
        else:
            raise HTTPException(
                422,
                f"Tiers are not applicable to '{rate_behavior}' items. Remove tier fields.",
            )
        if not fields_updated:
            # No scalar UPDATE confirmed the row is PendingApproval — lock it now.
            lock_row = await db.execute(
                text("""
                    SELECT driverrateid FROM payroll.driverrates
                    WHERE  driverrateid = :rid
                      AND  companyid    = :company_id
                      AND  status       = 'PendingApproval'
                    FOR UPDATE
                """),
                {"rid": rate_id, "company_id": company_id},
            )
            if lock_row.scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=409,
                    detail="Rate changed while editing. Refresh and try again.",
                )
        await _insert_tiers(rate_id, prepared, db)
        tiers_updated = True

    # Audit write (only when something actually changed) — inside the transaction.
    if fields or tiers_updated:
        await _write_rate_audit(
            db,
            company_id=company_id,
            branch_id=rate.branch_id,
            user_id=user_id,
            rate_id=rate_id,
            action_code="RATE_UPDATED",
            old_value={
                "amount":         str(rate.amount),
                "effective_from": str(rate.effective_from),
                "effective_to":   str(rate.effective_to) if rate.effective_to else None,
                "notes":          rate.notes,
            },
            new_value={
                **{k: str(v) if v is not None else None
                   for k, v in data.model_dump(exclude_none=True).items()
                   if k not in ("ordinal_tiers", "range_tiers")},
                "tiers_replaced": tiers_updated,
            },
        )

    return await _get_rate_with_tiers(rate_id, company_id, db)


# ---------------------------------------------------------------------------
# Shared DriverRate lifecycle helpers
# ---------------------------------------------------------------------------
# These three helpers centralise the approval/supersession invariants so that
# approve_rate, batch_save_rates (via approve_rate), and copy_driver_rates all
# enforce exactly the same rules.
#
# Invariants enforced together:
#   1. No future Approved rate for the same company/driver/rate_type may start
#      on or after the new effective_from (conflict guard).
#   2. Any existing Approved rate that started BEFORE effective_from is
#      superseded with EffectiveTo = effective_from - 1 day (supersession).
#   3. All supersession and void mutations write audit rows inside the same
#      transaction.
# ---------------------------------------------------------------------------

async def _check_no_future_approved_conflict(
    company_id: int,
    driver_id: int,
    rate_type_id: int,
    effective_from: date,
    db: AsyncConnection,
    *,
    exclude_rate_id: int | None = None,
) -> None:
    """
    Raise HTTP 422 if an Approved rate for this company/driver/rate_type starts
    on or after ``effective_from``.

    Superseding such a rate would set its EffectiveTo BEFORE its own
    EffectiveFrom (invalid dates) and would leave two Approved rows violating
    the partial unique index ``ux_DriverRates_Driver_Type_Approved``.

    ``exclude_rate_id`` — when approving an existing PendingApproval row that
    already lives in the DB, pass its ID so the SELECT does not match itself.
    """
    params: dict[str, Any] = {
        "company_id":   company_id,
        "driver_id":    driver_id,
        "rate_type_id": rate_type_id,
        "new_from":     effective_from,
    }
    exclude_clause = ""
    if exclude_rate_id is not None:
        exclude_clause = "AND driverrateid != :exclude_id"
        params["exclude_id"] = exclude_rate_id

    conflict = await db.execute(
        text(f"""
            SELECT driverrateid, effectivefrom
            FROM   payroll.driverrates
            WHERE  companyid    = :company_id
              AND  driverid     = :driver_id
              AND  ratetypeid   = :rate_type_id
              AND  status       = 'Approved'
              AND  effectivefrom >= CAST(:new_from AS date)
              {exclude_clause}
            LIMIT 1
        """),
        params,
    )
    row = conflict.mappings().first()
    if row is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot apply this rate — an Approved rate already exists "
                f"starting on or after {effective_from} "
                f"(rate ID {row['driverrateid']}, effective from {row['effectivefrom']}). "
                f"Void that rate first, or choose a later effective date."
            ),
        )


async def _supersede_current_approved_rates(
    company_id: int,
    driver_id: int,
    rate_type_id: int,
    effective_from: date,
    changed_by_user_id: int,
    branch_id: int,
    db: AsyncConnection,
    *,
    exclude_rate_id: int | None = None,
) -> list[int]:
    """
    Supersede any Approved rate whose ``EffectiveFrom < effective_from``.

    The closing rule (prevents date-range gaps or overlaps):
      • EffectiveTo IS NULL (open-ended)      → close at effective_from - 1 day.
      • EffectiveTo >= effective_from (overlap) → trim to effective_from - 1 day.
      • EffectiveTo < effective_from (already closed before new range) → unchanged.

    Only rates with EffectiveFrom strictly BEFORE effective_from are touched;
    future Approved rates (EffectiveFrom >= effective_from) are never modified
    here — the caller must have run _check_no_future_approved_conflict first.

    Returns a list of superseded DriverRateIDs.
    Writes RATE_SUPERSEDED audit for each inside the same transaction.
    """
    params: dict[str, Any] = {
        "new_from":     effective_from,
        "company_id":   company_id,
        "driver_id":    driver_id,
        "rate_type_id": rate_type_id,
    }
    exclude_clause = ""
    if exclude_rate_id is not None:
        exclude_clause = "AND driverrateid != :exclude_id"
        params["exclude_id"] = exclude_rate_id

    supersede_result = await db.execute(
        text(f"""
            UPDATE payroll.driverrates
            SET    status      = 'Superseded',
                   effectiveto = CASE
                       WHEN effectiveto IS NULL
                            OR effectiveto >= CAST(:new_from AS date)
                       THEN CAST(:new_from AS date) - 1
                       ELSE effectiveto
                   END
            WHERE  companyid    = :company_id
              AND  driverid     = :driver_id
              AND  ratetypeid   = :rate_type_id
              AND  status       = 'Approved'
              AND  effectivefrom < CAST(:new_from AS date)
              {exclude_clause}
            RETURNING driverrateid
        """),
        params,
    )
    superseded_ids: list[int] = [row[0] for row in supersede_result.fetchall()]

    for sid in superseded_ids:
        await _write_rate_audit(
            db,
            company_id=company_id,
            branch_id=branch_id,
            user_id=changed_by_user_id,
            rate_id=sid,
            action_code="RATE_SUPERSEDED",
            old_value={"status": "Approved"},
            new_value={
                "status":                        "Superseded",
                "superseded_at_effective_from":  str(effective_from),
            },
        )

    return superseded_ids


async def _void_pending_rates_for_rate_type(
    company_id: int,
    driver_id: int,
    rate_type_id: int,
    changed_by_user_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> list[int]:
    """
    Void all PendingApproval rows for this company/driver/rate_type.

    Called *after* all validations pass so that a pre-validation failure leaves
    no partial state.

    Returns a list of voided DriverRateIDs.
    Writes RATE_VOIDED audit for each inside the same transaction.
    """
    void_result = await db.execute(
        text("""
            UPDATE payroll.driverrates
            SET    status = 'Voided'
            WHERE  companyid  = :cid
              AND  driverid   = :did
              AND  ratetypeid = :rtid
              AND  status     = 'PendingApproval'
            RETURNING driverrateid
        """),
        {"cid": company_id, "did": driver_id, "rtid": rate_type_id},
    )
    voided_ids: list[int] = [row[0] for row in void_result.fetchall()]

    for vid in voided_ids:
        await _write_rate_audit(
            db,
            company_id=company_id,
            branch_id=branch_id,
            user_id=changed_by_user_id,
            rate_id=vid,
            action_code="RATE_VOIDED",
            old_value={"status": "PendingApproval"},
            new_value={"status": "Voided"},
        )

    return voided_ids


# ---------------------------------------------------------------------------
# Approve rate (PendingApproval → Approved; supersedes prior Approved rate)
# ---------------------------------------------------------------------------

async def approve_rate(
    rate_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> DriverRateSummary:
    """
    Approve a PendingApproval rate.  All steps run inside the single
    engine.begin() transaction; any failure rolls back everything.

    Operation order is critical:
      Step 1 — Access check + friendly status guard (read-only).
      Step 1.5 — Conflict guard: reject if an existing Approved rate starts on or after
               this rate's effective_from.  Superseding such a rate would set its
               effective_to BEFORE its own effective_from (invalid dates) and would
               leave two Approved rows violating the partial unique index.
               The caller must void the conflicting rate first.
      Step 2 — Supersede any prior Approved rate(s) that started BEFORE this rate.
               MUST happen BEFORE Step 3 so the partial unique index
               (ux_DriverRates_Driver_Type_Approved) is clear when we approve.
               Trims effective_to to (new_from - 1) whenever the old rate's effective_to
               is NULL or overlaps with the new rate's range — ensuring no date gaps or
               overlaps are left in the historical record.
               Only rates with effectivefrom < new_from are touched.
      Step 3 — Atomic claim: UPDATE WHERE status='PendingApproval' RETURNING.
               If 0 rows, a concurrent request won the race → 422.
               Wrapped in try/except SAIntegrityError as a belt-and-suspenders guard.
      Step 4 — Audit log for each superseded rate.
      Step 5 — Audit log for this rate's approval.
      Step 6 — Return refreshed DriverRateSummary.
    """
    # Step 1 — access check + friendly guard
    # Use get_rate_by_id (public — includes branch scope check) for the access check,
    # then use the lightweight _get_rate_by_id for subsequent internal operations to
    # avoid reloading tiers unnecessarily during the approval flow.
    rate = await get_rate_by_id(rate_id, company_id, user_id, db)
    if rate.status != "PendingApproval":
        raise HTTPException(
            status_code=422,
            detail=(
                f"Only PendingApproval rates can be approved "
                f"(current status: '{rate.status}')."
            ),
        )

    # Step 1.3 — permission gate: approving a rate requires payrates.edit (or admin fallbacks)
    await _check_any_permission(
        company_id, user_id, rate.branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    # Step 1.34 — OwnDriverDataOnly: caller may only approve their own driver's rates
    await _check_own_driver_only(company_id, user_id, rate.driver_id, db)

    # Step 1.36 — Defensive cross-company scope guard: reject contaminated rows
    # (e.g. created before P0 was closed) that reference another company's RateType.
    await _assert_rate_type_allowed_for_company(db, company_id, rate.rate_type_id)

    # Step 1.37 — CP-2D2: status-payment RateTypes are branch-scoped. Reject approval of a
    # contaminated pending rate that references a status RateType not active for this branch.
    await _assert_status_rate_type_for_branch(rate.rate_type_id, company_id, rate.branch_id, db)

    # Step 1.4 — M13c: belt-and-suspenders tier-existence check.
    # The create/update paths already enforce this, but a defensive check here
    # prevents approval of a tiered rate with no tiers (e.g. if tiers were deleted
    # directly from the DB or the rate was created before M13c was deployed).
    rate_behavior = await _resolve_rate_behavior(rate.rate_type_id, company_id, db)
    if rate_behavior in _TIERED_BEHAVIORS:
        tiers = await _load_tiers(rate_id, db)
        if not tiers:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Cannot approve a {rate_behavior} rate with no tiers defined. "
                    "Update the rate to add tiers first."
                ),
            )
    if rate_behavior == "Block":
        if rate.block_size is None:
            raise HTTPException(
                status_code=422,
                detail="Cannot approve a Block rate without block_size. Update the rate first.",
            )

    # Phase 12 / 12B — Finalization atomicity advisory lock.
    # Acquire the same transaction-level advisory lock that finalize_period holds
    # during its refresh→claim→insert window.  This serialises rate approval against
    # ongoing finalization for the same company+branch so that:
    #   • If finalization is in progress: this approval waits until finalize commits.
    #   • If no finalization is in progress: lock is acquired immediately.
    # After this lock is released (on commit/rollback), any subsequent finalization
    # for this branch will see the approved/superseded rate from the start of its
    # refresh step, ensuring finalamount and SourceSnapshot agree.
    # See the Phase 12 comment in finalize_period for the full race description.
    #
    # Phase 12B fix: _check_not_in_finalized_period is intentionally moved to AFTER
    # the advisory lock.  Running it before the lock creates a TOCTOU window: the
    # guard reads period status (Approved), this request waits for the lock while
    # finalization claims the period (Locked), and when the lock is finally acquired
    # the guard decision is already stale — the approval would proceed against a now-
    # finalized period.  By checking under the lock the guard always sees a consistent
    # view of period status.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:cid, :bid)"),
        {"cid": company_id, "bid": rate.branch_id},
    )

    # Step 1.35 — Backdating guard (re-run under lock for TOCTOU safety).
    # Block approval when effective_from falls inside a Locked or Archived period.
    # Must run AFTER the advisory lock is held so the period-status read is fresh.
    await _check_not_in_finalized_period(company_id, rate.branch_id, rate.effective_from, db)

    # Step 1.5 — conflict guard (shared helper).
    # Rejects if any Approved rate starts on or after this rate's effective_from.
    # Superseding such a rate would produce EffectiveTo < EffectiveFrom (invalid)
    # and would momentarily leave two Approved rows violating the partial unique index.
    # exclude_rate_id=rate_id: exclude this PendingApproval row from the check
    # (it is in the DB but not yet Approved, so the STATUS filter already excludes
    # it — the exclude_rate_id is a belt-and-suspenders guard against edge cases).
    await _check_no_future_approved_conflict(
        company_id, rate.driver_id, rate.rate_type_id, rate.effective_from, db,
        exclude_rate_id=rate_id,
    )

    # Step 2 — supersede prior Approved rate(s) FIRST via shared helper.
    # The helper only touches rates with EffectiveFrom < effective_from.
    # Writes RATE_SUPERSEDED audit inside the same transaction.
    await _supersede_current_approved_rates(
        company_id, rate.driver_id, rate.rate_type_id, rate.effective_from,
        user_id, rate.branch_id, db,
        exclude_rate_id=rate_id,
    )

    # Step 3 — atomic claim (AFTER supersede so the unique index is clear).
    # UPDATE WHERE status='PendingApproval' acquires a row lock and transitions the
    # rate in one statement.  A concurrent request issuing the same UPDATE gets 0
    # rows (the row is already Approved) and is safely rejected below.
    # SAIntegrityError is caught as a belt-and-suspenders guard for the rare race
    # where two concurrent approvals of different rates for the same driver+type
    # slip past the supersede gate simultaneously.
    try:
        claimed = await db.execute(
            text("""
                UPDATE payroll.driverrates
                SET    status           = 'Approved',
                       approvedbyuserid = :approver,
                       approvedatutc    = NOW()
                WHERE  driverrateid = :rate_id
                  AND  companyid    = :company_id
                  AND  status       = 'PendingApproval'
                RETURNING driverrateid
            """),
            {"approver": user_id, "rate_id": rate_id, "company_id": company_id},
        )
    except SAIntegrityError:
        raise HTTPException(
            status_code=422,
            detail=(
                "Rate could not be approved — another rate was approved concurrently. "
                "Please refresh and try again."
            ),
        )
    if claimed.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Rate could not be approved — its status may have changed concurrently."
            ),
        )

    # Step 4 — supersede audit already written by _supersede_current_approved_rates.

    # Step 5 — audit this approval (failure here rolls back steps 2+3+4)
    await _write_rate_audit(
        db,
        company_id=company_id,
        branch_id=rate.branch_id,
        user_id=user_id,
        rate_id=rate_id,
        action_code="RATE_APPROVED",
        old_value={"status": "PendingApproval"},
        new_value={
            "status":         "Approved",
            "amount":         str(rate.amount),
            "effective_from": str(rate.effective_from),
            "effective_to":   str(rate.effective_to) if rate.effective_to else None,
        },
    )

    # Step 6
    return await _get_rate_with_tiers(rate_id, company_id, db)


# ---------------------------------------------------------------------------
# Void rate (PendingApproval or Approved → Voided)
# ---------------------------------------------------------------------------

async def void_rate(
    rate_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> None:
    """
    Void a rate.

    PendingApproval, Approved, and Superseded rates can all be voided.
    Superseded rates represent historical closed periods; an admin may need
    to void one to correct a data-entry mistake.  Once voided the row is
    excluded from the effective-date uniqueness constraint so the date range
    can be reused.

    Already-Voided rates cannot be voided again.
    """
    rate = await get_rate_by_id(rate_id, company_id, user_id, db)

    if rate.status == "Voided":
        raise HTTPException(
            status_code=422,
            detail="Rate is already Voided.",
        )

    # Permission gate: voiding a rate requires payrates.edit (or admin fallbacks)
    await _check_any_permission(
        company_id, user_id, rate.branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    # OwnDriverDataOnly: caller may only void their own driver's rates
    await _check_own_driver_only(company_id, user_id, rate.driver_id, db)

    # Phase 12B — advisory lock for Approved / Superseded void path.
    #
    # WHY: voiding an Approved or Superseded DriverRate changes the set of rates
    # visible to finalization's LATERAL rate_sub query.  If finalize_period has
    # already run _refresh_draft_calculations (step 1.6) using this rate and is
    # about to INSERT final lines (step 3), a concurrent void would make the LATERAL
    # return NULL for driverrateid — the finalamount would be computed from a rate
    # that no longer exists in the snapshot.
    #
    # Acquire the same transaction-level advisory lock used by finalize_period and
    # approve_rate before the used-lines check and the status mutation.  This
    # serialises void against any in-progress finalization for the same branch.
    #
    # PendingApproval rates are never referenced by finalised lines (the Phase 5
    # guard below would have caught that), and they are not visible to the
    # finalization LATERAL (which filters dr.status IN ('Approved','Superseded')).
    # The lock is therefore only needed for Approved/Superseded rates.
    if rate.status in ("Approved", "Superseded"):
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:cid, :bid)"),
            {"cid": company_id, "bid": rate.branch_id},
        )

    # Phase 5 guard: block void if this DriverRate is referenced by PayrollFinalLines.
    # Runs after ownership validation so cross-company ID probing does not leak info.
    # Runs before mutation and before any audit write.
    # (For Approved/Superseded rates this now also runs under the advisory lock.)
    used = await db.execute(
        text(
            "SELECT 1 FROM payroll.payrollfinallines "
            "WHERE driverrateid = :rid LIMIT 1"
        ),
        {"rid": rate_id},
    )
    if used.first() is not None:
        raise HTTPException(
            status_code=422,
            detail="This rate has been used in finalized payroll and cannot be voided.",
        )

    # Phase 12C — status-predicated void UPDATE.
    # Do NOT rely solely on the earlier status check: for PendingApproval rates a
    # concurrent approve_rate can transition the row to Approved between the initial
    # status read and this UPDATE.  Without a predicate, the UPDATE would void an
    # already-Approved rate, bypassing the Phase 12B active-rate advisory lock path.
    #
    # Use the status read at the top (rate.status) as the expected value.  For
    # Approved/Superseded rates the Phase 12B advisory lock was already acquired above,
    # so the lock serialises against concurrent finalization; the predicate here is an
    # additional belt-and-suspenders guard.  For PendingApproval rates the predicate
    # is the primary protection: if the row was concurrently approved we detect 0 rows
    # and raise a clean 409 rather than silently voiding an active rate.
    void_result = await db.execute(
        text("""
            UPDATE payroll.driverrates
            SET    status = 'Voided'
            WHERE  driverrateid = :rid
              AND  companyid    = :company_id
              AND  status       = :expected_status
            RETURNING driverrateid
        """),
        {"rid": rate_id, "company_id": company_id, "expected_status": rate.status},
    )
    if void_result.scalar_one_or_none() is None:
        # 0 rows — the rate's status changed concurrently (e.g. PendingApproval → Approved).
        re_read = await db.execute(
            text("SELECT status FROM payroll.driverrates WHERE driverrateid = :rid"),
            {"rid": rate_id},
        )
        current_status = re_read.scalar_one_or_none()
        if current_status in ("Approved", "Superseded"):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Rate was approved or superseded concurrently. "
                    "To void an active rate, refresh and try again."
                ),
            )
        raise HTTPException(
            status_code=409,
            detail="Rate changed while voiding. Refresh and try again.",
        )

    # Audit write — inside the same transaction; failure rolls back the UPDATE
    await _write_rate_audit(
        db,
        company_id=company_id,
        branch_id=rate.branch_id,
        user_id=user_id,
        rate_id=rate_id,
        action_code="RATE_VOIDED",
        old_value={"status": rate.status},
        new_value={"status": "Voided"},
    )


# ---------------------------------------------------------------------------
# Resolve the applicable rate for a specific driver, rate type, and work date
# ---------------------------------------------------------------------------

async def resolve_rate_for_date(
    driver_id: int,
    rate_type_id: int,
    work_date: date,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> "DriverRateSummary | None":
    """
    Return the rate row that applies to *driver_id* / *rate_type_id* on *work_date*.

    Both **Approved** and **Superseded** rows are considered.  Superseded rows
    represent historically-closed rates whose effective date range is still valid
    for payroll lines whose work date falls inside that range — they must not be
    excluded from the lookup.

    Security:
      - Requires payrates.view / payrates.edit / settings.manage / setup.manage.
      - Enforces branch-scope: caller must have access to the driver's branch.
      - Enforces OwnDriverDataOnly: caller may only look up their own driver.
      - Cross-company lookup is impossible (company_id filter on driver lookup).

    Returns None (found=False) when the driver does not exist in this company or
    no rate covers the requested date.
    """
    # Step 1 — driver lookup (company-scoped; cross-company driver → None, not 403).
    # Done first so we have the branch_id for the permission check below.
    drv_lookup = await db.execute(
        text("SELECT branchid FROM core.drivers WHERE driverid = :did AND companyid = :cid"),
        {"did": driver_id, "cid": company_id},
    )
    drv_lookup_row = drv_lookup.mappings().first()
    if drv_lookup_row is None:
        # Driver not in this company — return None without revealing existence via 403.
        # Permission check still runs (with None branch_id) to gate unauthenticated calls.
        await _check_any_permission(
            company_id, user_id, None,
            ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
        )
        return None

    driver_branch_id: int = drv_lookup_row["branchid"]

    # Step 2 — permission gate with the driver's branch so OwnDriverDataOnly users
    # (whose permission is granted per-branch, not company-wide) can pass.
    await _check_any_permission(
        company_id, user_id, driver_branch_id,
        ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
    )

    # Step 3 — branch-scope gate
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and driver_branch_id not in branch_ids:
        raise HTTPException(
            status_code=403,
            detail="Access denied to this driver's branch.",
        )

    # Step 4 — OwnDriverDataOnly: caller may only look up their own driver
    await _check_own_driver_only(company_id, user_id, driver_id, db)

    result = await db.execute(
        text(f"""
            {_RATE_SELECT}
            WHERE  dr.companyid    = :company_id
              AND  dr.driverid     = :driver_id
              AND  dr.ratetypeid   = :rate_type_id
              AND  dr.status       IN ('Approved', 'Superseded')
              AND  dr.effectivefrom <= CAST(:work_date AS date)
              AND  (dr.effectiveto IS NULL OR dr.effectiveto >= CAST(:work_date AS date))
            ORDER BY dr.effectivefrom DESC, dr.driverrateid DESC
            LIMIT 1
        """),
        {
            "company_id":    company_id,
            "driver_id":     driver_id,
            "rate_type_id":  rate_type_id,
            "work_date":     work_date,
        },
    )
    row = result.mappings().fetchone()
    return _rate_row_to_summary(row) if row is not None else None


# ===========================================================================
# M14 — Period Pay
# ===========================================================================
#
# Period Pay lines are stored in PayrollDraftLines with WorkDate = NULL.
# They represent period-level lump-sum amounts (bonuses, adjustments, etc.)
# that do not belong to a specific work date.
#
# Supported RateBehaviors in M14:
#   EnteredAmount — custom Period items (user enters dollar amount directly)
#   Fixed         — system BONUS / ADJUSTMENT (same semantics: direct entry)
#
# Storage contract:
#   Quantity         = 1           (fixed; period pay has no unit count)
#   RateAmount       = NULL        (no rate lookup; amount is entered directly)
#   CalculatedAmount = amount      (set at creation; immediately resolved)
#   NeedsManagerReview = FALSE     (always resolved; no rate engine needed)
#   WorkDate         = NULL        (discriminator: period pay vs daily)
#   SourceType       = 'Manual'
# ===========================================================================


async def _validate_period_line_type(
    line_type: str,
    branch_id: int,
    company_id: int,
    as_of_date: date,
    db: AsyncConnection,
    period_id: int | None = None,
) -> _LineTypeInfo:
    """
    Validate a line_type for a Period Pay line.

    CP-0 unified DB path — mirrors _validate_line_type design.

    as_of_date: callers pass period.start_date (not CURRENT_DATE) so that
    backdated and future periods validate against their own effective window.

    Accepts both legacy display names ("Bonus") and canonical PayItemCodes
    ("BONUS") via _LEGACY_TO_CANONICAL normalisation before the DB lookup.

    Checks (in order):
      1. Finalization-only / explicitly blocked items — clear deferral error.
      2. Normalise to canonical PayItemCode.
      3. CP-2C: if period_id supplied and snapshot exists, validate against it.
      4. Unified DB lookup (system companyid IS NULL + custom companyid = :cid).
      5. Status guard (Retired → 422).
      6. Scope guard — must be 'Period'; daily items → clear redirect message.
      7. Rate behavior guard — must be EnteredAmount or Fixed (not Calculated).
      8. Branch activation check.
    """
    # --- 1. Blocked codes (GuaranteedMinimum, SYS lines) ---
    if line_type in _SYSTEM_PERIOD_BLOCKED:
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{line_type}' uses Calculated behavior which requires the automated "
                "pay-rule engine. This item cannot be entered as a manual Period Pay line "
                "in the current version. It will be available when pay-rule processing "
                "is implemented."
            ),
        )

    # --- 2. Normalise to canonical PayItemCode ---
    canonical_code: str = _LEGACY_TO_CANONICAL.get(line_type, line_type)

    # --- 3. CP-2C: snapshot-first validation ---
    if period_id is not None:
        snap_result = await db.execute(
            text("""
                SELECT payitemcode, ratebehavior, isactiveinperiod, itemscope
                FROM payroll.payrollperiodpayitems
                WHERE payrollperiodid = :pid
                  AND companyid       = :cid
                  AND payitemcode     = :code
            """),
            {"pid": period_id, "cid": company_id, "code": canonical_code},
        )
        snap_row = snap_result.mappings().first()

        has_any_snap = (await db.execute(
            text("SELECT 1 FROM payroll.payrollperiodpayitems WHERE payrollperiodid = :pid LIMIT 1"),
            {"pid": period_id},
        )).first()

        if has_any_snap is not None:
            if snap_row is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"'{line_type}' is not in the pay-item snapshot for this period. "
                        "The item was not active or did not exist when the period was created."
                    ),
                )
            if snap_row["itemscope"] == "Daily":
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"'{line_type}' is a Daily-scope pay item and cannot be used as a "
                        "Period Pay line. Use POST /periods/{id}/lines for daily entry."
                    ),
                )
            if not bool(snap_row["isactiveinperiod"]):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Pay item '{line_type}' was not active for this branch when the "
                        "period was created and cannot be used for new entries."
                    ),
                )
            behavior = snap_row["ratebehavior"]
            if behavior == "Calculated":
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"'{line_type}' uses Calculated behavior which requires the automated "
                        "pay-rule engine. This item cannot be entered as a manual Period Pay line."
                    ),
                )
            if behavior not in _PERIOD_PAY_ALLOWED_BEHAVIORS:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Pay item '{line_type}' uses '{behavior}' rate behavior, which is not "
                        f"supported for manual Period Pay entry. "
                        f"Supported behaviors: {sorted(_PERIOD_PAY_ALLOWED_BEHAVIORS)}."
                    ),
                )
            return _LineTypeInfo(rate_behavior=behavior, rate_code=None, item_scope="Period")

    # --- 4. Unified DB lookup ---
    pi_result = await db.execute(
        text("""
            SELECT pi.payitemid, pi.itemscope, pi.ratebehavior,
                   pi.isdefaultbranchactive, pi.status
            FROM   payroll.payitems pi
            WHERE  pi.payitemcode = :code
              AND  (pi.companyid IS NULL OR pi.companyid = :cid)
            ORDER BY pi.companyid NULLS LAST
            LIMIT 1
        """),
        {"code": canonical_code, "cid": company_id},
    )
    pi_row = pi_result.mappings().first()

    if pi_row is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{line_type}' is not a recognised Period Pay item for this company. "
                "Use a system period item (Bonus / BONUS, Adjustment / ADJUSTMENT) "
                "or an active custom Period pay item PayItemCode."
            ),
        )

    # --- 4. Status guard ---
    if pi_row["status"] == "Retired":
        raise HTTPException(
            status_code=422,
            detail=f"Pay item '{line_type}' has been retired and cannot be used for new entries.",
        )

    # --- 5. Scope guard ---
    if pi_row["itemscope"] == "Daily":
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{line_type}' is a Daily-scope pay item and cannot be used as a "
                "Period Pay line. Use POST /periods/{id}/lines for daily entry."
            ),
        )
    if pi_row["itemscope"] != "Period":
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{line_type}' is a {pi_row['itemscope']}-scope item and cannot be used "
                "as a Period Pay line. Period Pay only accepts items with ItemScope='Period'."
            ),
        )

    # --- 6. Rate behavior guard ---
    behavior = pi_row["ratebehavior"]
    if behavior == "Calculated":
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{line_type}' uses Calculated behavior which requires the automated "
                "pay-rule engine. This item cannot be entered as a manual Period Pay line."
            ),
        )
    if behavior not in _PERIOD_PAY_ALLOWED_BEHAVIORS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Pay item '{line_type}' uses '{behavior}' rate behavior, which is not "
                f"supported for manual Period Pay entry. "
                f"Supported behaviors: {sorted(_PERIOD_PAY_ALLOWED_BEHAVIORS)}."
            ),
        )

    # --- 7. Branch activation (LEFT JOIN + COALESCE fallback) ---
    cfg_result = await db.execute(
        text("""
            SELECT isactive
            FROM   payroll.branchpayitemconfig
            WHERE  payitemid     = :piid
              AND  companyid     = :cid
              AND  branchid      = :bid
              AND  effectivefrom <= :as_of_date
              AND  (effectiveto IS NULL OR effectiveto >= :as_of_date)
            ORDER BY effectivefrom DESC
            LIMIT 1
        """),
        {"piid": pi_row["payitemid"], "cid": company_id, "bid": branch_id,
         "as_of_date": as_of_date},
    )
    cfg_row = cfg_result.mappings().first()
    is_active = bool(cfg_row["isactive"]) if cfg_row else bool(pi_row["isdefaultbranchactive"])
    if not is_active:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Pay item '{line_type}' is not active for this branch as of {as_of_date}. "
                "Activate it in Branch Pay Items settings first."
            ),
        )

    return _LineTypeInfo(rate_behavior=behavior, rate_code=None, item_scope="Period")


# ---------------------------------------------------------------------------
# Add a period pay line
# ---------------------------------------------------------------------------

async def add_period_pay_line(
    period_id: int,
    company_id: int,
    user_id: int,
    data: PeriodPayLineCreate,
    db: AsyncConnection,
) -> DraftLineSummary:
    """
    Insert a Period Pay line for a driver.

    Guards:
      - ODA/Driver users are blocked unconditionally (Current Payroll is not
        a driver self-service screen).
      - Period must be Open or InReview.
      - Driver must exist in this company and belong to the period's branch.
      - line_type must be an active Period-scope item (EnteredAmount or Fixed behavior).
    """
    # ── Driver-role hard-block ───────────────────────────────────────────────── #
    await _require_not_driver_role(company_id, user_id, db)

    period = await get_period_by_id(company_id, user_id, period_id, db)

    if period.status not in ENTRY_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Period Pay lines can only be added to Open or Returned periods "
                f"(current status: '{period.status}')."
            ),
        )

    await _check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    # Period-level driver eligibility check.
    # CP-2E: use snapshot-based eligibility when available; legacy fallback otherwise.
    await _assert_driver_eligible_for_period_via_snapshot(
        company_id, period.branch_id, period_id, data.driver_id, db
    )

    # CP-0: Normalise to canonical PayItemCode before validation and storage.
    canonical_period_lt: str = _LEGACY_TO_CANONICAL.get(data.line_type, data.line_type)

    # CP-3A: BONUS is now a canonical bonus event, not a generic period pay line.
    # All bonus creation must go through POST /payroll/periods/{id}/bonuses.
    if canonical_period_lt == "BONUS":
        raise HTTPException(
            status_code=422,
            detail=(
                "BONUS lines must be added through the canonical Bonus Events API: "
                f"POST /payroll/periods/{period_id}/bonuses. "
                "The period-pay path no longer accepts BONUS line type (CP-3A)."
            ),
        )

    # Validate the line type (scope, status, behavior, branch activation).
    # Use period.start_date as the effective date so backdated and future periods
    # validate against the period date, not CURRENT_DATE.
    await _validate_period_line_type(
        canonical_period_lt, period.branch_id, company_id, period.start_date, db,
        period_id=period_id,
    )

    # CP-0A: Lock custom PayItem catalog row before period lock (same order as the
    # physical-delete path) to prevent the first-reference orphan race.
    # CP-2C: pass period_id so snapshot-authorised items bypass live status check.
    await _lock_pay_item_for_source_write(canonical_period_lt, company_id, db, period_id=period_id)
    # CP-0A: Recheck period status under a row-level lock before writing.
    await _lock_period_for_mutation(period_id, company_id, db)

    # Insert with the M14 storage contract:
    #   WorkDate         = NULL      (period pay discriminator)
    #   Quantity         = 1         (no meaningful unit count)
    #   RateAmount       = NULL      (no rate lookup)
    #   CalculatedAmount = amount    (immediately resolved)
    #   NeedsManagerReview = FALSE   (no rate engine needed)
    insert_result = await db.execute(
        text("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid,
                 workdate, linetype, linescope, quantity, rateamount, calculatedamount,
                 sourcetype, status, needsmanagerreview, notes, addedbyuserid)
            VALUES
                (:company_id, :branch_id, :period_id, :driver_id,
                 NULL, :line_type, 'Period', 1, NULL, :calc_amount,
                 'Manual', 'Active', FALSE, :notes, :added_by)
            RETURNING draftlineid
        """),
        {
            "company_id":  company_id,
            "branch_id":   period.branch_id,
            "period_id":   period_id,
            "driver_id":   data.driver_id,
            "line_type":   canonical_period_lt,    # store canonical
            "calc_amount": data.amount,
            "notes":       data.notes,
            "added_by":    user_id,
        },
    )
    line_id: int = insert_result.scalar_one()

    await _write_line_audit(
        db,
        company_id=company_id,
        branch_id=period.branch_id,
        user_id=user_id,
        line_id=line_id,
        action_code="PERIOD_PAY_ADDED",
        new_value={
            "period_id":  period_id,
            "driver_id":  data.driver_id,
            "line_type":  canonical_period_lt,
            "amount":     float(data.amount),
        },
    )
    await _capture_source_evidence(
        company_id=company_id, branch_id=period.branch_id, period_id=period_id,
        user_id=user_id, line_id=line_id, action_code="SOURCE_CREATED", db=db,
        before_state=None,
        after_state={
            "line_type": canonical_period_lt, "line_scope": "Period", "quantity": 1,
            "calculated_amount": data.amount, "source_type": "Manual", "status": "Active",
            "notes": data.notes,
        },
        driver_id=data.driver_id, work_date=None, line_type=canonical_period_lt,
    )

    return await _get_line_by_id(line_id, company_id, db)


# ---------------------------------------------------------------------------
# List period pay lines
# ---------------------------------------------------------------------------

async def get_period_pay_lines(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    driver_id: int | None = None,
) -> list[DraftLineSummary]:
    """
    Return Period Pay lines for a period (WorkDate IS NULL lines only).
    Void lines are included (consistent with get_period_lines behavior).

    ODA/Driver users are blocked unconditionally (P1 #2 security boundary).
    """
    # ── Driver-role hard-block ───────────────────────────────────────────────── #
    await _require_not_driver_role(company_id, user_id, db)

    period = await get_period_by_id(company_id, user_id, period_id, db)

    # CP-2F: Period Pay is a financial path — block for Draft (Prepared) periods.
    if period.status == "Draft":
        raise HTTPException(
            status_code=422,
            detail="Period Pay lines are not available for Prepared (Draft) periods.",
        )

    conditions = [
        "dl.payrollperiodid = :period_id",
        "dl.companyid       = :company_id",
        "dl.linescope       = 'Period'",    # period pay lines only
        "dl.linetype        != 'BONUS'",    # CP-3A: BONUS is canonical bonus events, not period-pay
    ]
    params: dict[str, Any] = {"period_id": period_id, "company_id": company_id}

    if driver_id is not None:
        conditions.append("dl.driverid = :driver_id")
        params["driver_id"] = driver_id

    where = " AND ".join(conditions)
    result = await db.execute(
        text(f"{_LINE_SELECT} WHERE {where} ORDER BY dl.driverid, dl.linetype, dl.draftlineid"),
        params,
    )
    return [_line_row_to_summary(r) for r in result.mappings().all()]


# ---------------------------------------------------------------------------
# Update a period pay line
# ---------------------------------------------------------------------------

async def update_period_pay_line(
    period_id: int,
    line_id: int,
    company_id: int,
    user_id: int,
    data: PeriodPayLineUpdate,
    db: AsyncConnection,
) -> DraftLineSummary:
    """
    Update the amount and/or notes of a Period Pay line.

    Updating amount rewrites CalculatedAmount immediately.
    The period must be Open or InReview.
    """
    # ── Driver-role hard-block ───────────────────────────────────────────────── #
    await _require_not_driver_role(company_id, user_id, db)

    period = await get_period_by_id(company_id, user_id, period_id, db)

    if period.status in _WRITE_BLOCKED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot modify lines on a period with status '{period.status}'.",
        )

    await _check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    line = await _get_line_by_id(line_id, company_id, db)
    if line.period_id != period_id:
        raise HTTPException(status_code=404, detail="Period Pay line not found in this period.")
    if line.line_scope != "Period":
        raise HTTPException(
            status_code=422,
            detail="This line is a daily line, not a Period Pay line. Use the daily line endpoint.",
        )
    if line.status == "Void":
        raise HTTPException(status_code=422, detail="Cannot modify a voided Period Pay line.")
    if line.line_type.upper() == "ADJUSTMENT":
        raise HTTPException(
            status_code=422,
            detail="ADJUSTMENT lines cannot be modified. Use the standard payroll entry workflow.",
        )
    if line.line_type.upper() == "BONUS":
        raise HTTPException(
            status_code=422,
            detail=(
                "BONUS lines cannot be modified through /period-pay. "
                f"Use PATCH /payroll/periods/{period_id}/bonuses/<bonus_event_id> (CP-3A)."
            ),
        )

    fields: dict[str, Any] = {}
    if data.notes is not None:
        fields["notes"] = data.notes
    if data.amount is not None:
        fields["calculatedamount"] = data.amount

    if not fields:
        # No changes — return as-is
        return line

    canonical_period_pay_lt = _LEGACY_TO_CANONICAL.get(line.line_type, line.line_type)

    # CP-2C: for periods with a snapshot, validate the line's pay item against
    # the snapshot before writing.  This allows updates on items that were
    # active at period creation but later retired, while still rejecting items
    # that were inactive or absent in the snapshot.
    has_snap = await _period_has_pay_item_snapshot(period_id, db)
    if has_snap:
        snap_upd = (await db.execute(
            text("""
                SELECT isactiveinperiod FROM payroll.payrollperiodpayitems
                WHERE payrollperiodid = :pid
                  AND payitemcode     = :code
            """),
            {"pid": period_id, "code": canonical_period_pay_lt},
        )).mappings().first()
        if snap_upd is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"'{line.line_type}' is not in the pay-item snapshot for this period."
                ),
            )
        if not bool(snap_upd["isactiveinperiod"]):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Pay item '{line.line_type}' was not active for this branch when "
                    "the period was created and cannot be updated."
                ),
            )

    # CP-0A: Lock custom PayItem catalog row before period lock to prevent the
    # zero-to-meaningful race on period-pay lines (same lock ordering as deletion).
    # CP-2C: pass period_id so snapshot-authorised items bypass live status check.
    await _lock_pay_item_for_source_write(canonical_period_pay_lt, company_id, db,
                                          period_id=period_id)
    # CP-0A: Recheck period status under a row-level lock before writing.
    await _lock_period_for_mutation(period_id, company_id, db)
    set_clause = ", ".join(f"{col} = :{col}" for col in fields)
    await db.execute(
        text(
            f"UPDATE payroll.payrolldraftlines SET {set_clause} "
            "WHERE draftlineid = :line_id "
            "  AND payrollperiodid = :period_id AND companyid = :company_id"
        ),
        {**fields, "line_id": line_id, "period_id": period_id, "company_id": company_id},
    )
    await _write_line_audit(
        db,
        company_id=company_id,
        branch_id=period.branch_id,
        user_id=user_id,
        line_id=line_id,
        action_code="PERIOD_PAY_UPDATED",
        new_value={k: (float(v) if isinstance(v, Decimal) else v) for k, v in fields.items()},
    )
    before_state = {
        "line_type": line.line_type, "line_scope": line.line_scope, "quantity": line.quantity,
        "calculated_amount": line.calculated_amount, "source_type": line.source_type,
        "status": line.status, "notes": line.notes,
    }
    await _capture_source_evidence(
        company_id=company_id, branch_id=period.branch_id, period_id=period_id,
        user_id=user_id, line_id=line_id, action_code="SOURCE_UPDATED", db=db,
        before_state=before_state, after_state={**before_state, **fields},
        driver_id=line.driver_id, work_date=None, line_type=line.line_type,
    )
    return await _get_line_by_id(line_id, company_id, db)


# ---------------------------------------------------------------------------
# Void a period pay line
# ---------------------------------------------------------------------------

async def void_period_pay_line(
    period_id: int,
    line_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> DraftLineSummary:
    """
    Void a Period Pay line (sets status = 'Void').

    Idempotent: voiding an already-voided line succeeds without error.
    The period must be Open (CP-0A).
    ODA/Driver users are blocked unconditionally (P1 #2 security boundary).
    """
    # ── Driver-role hard-block ───────────────────────────────────────────────── #
    await _require_not_driver_role(company_id, user_id, db)

    period = await get_period_by_id(company_id, user_id, period_id, db)

    if period.status in _WRITE_BLOCKED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot void lines on a period with status '{period.status}'.",
        )

    await _check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    line = await _get_line_by_id(line_id, company_id, db)
    if line.period_id != period_id:
        raise HTTPException(status_code=404, detail="Period Pay line not found in this period.")
    if line.line_scope != "Period":
        raise HTTPException(
            status_code=422,
            detail="This line is a daily line, not a Period Pay line. Use the daily line endpoint.",
        )
    if line.line_type.upper() == "BONUS":
        raise HTTPException(
            status_code=422,
            detail=(
                "BONUS lines cannot be voided through /period-pay. "
                f"Use DELETE /payroll/periods/{period_id}/bonuses/<bonus_event_id> (CP-3A)."
            ),
        )

    # Idempotent: already voided → return as-is
    if line.status != "Void":
        # CP-0A: Recheck period status under a row-level lock before writing.
        await _lock_period_for_mutation(period_id, company_id, db)
        await db.execute(
            text(
                "UPDATE payroll.payrolldraftlines SET status = 'Void' "
                "WHERE draftlineid = :lid "
                "  AND payrollperiodid = :period_id AND companyid = :company_id"
            ),
            {"lid": line_id, "period_id": period_id, "company_id": company_id},
        )
        await _write_line_audit(
            db,
            company_id=company_id,
            branch_id=period.branch_id,
            user_id=user_id,
            line_id=line_id,
            action_code="PERIOD_PAY_VOIDED",
            old_value={"status": "Active"},
            new_value={"status": "Void"},
        )
        await _capture_source_evidence(
            company_id=company_id, branch_id=period.branch_id, period_id=period_id,
            user_id=user_id, line_id=line_id, action_code="SOURCE_VOIDED", db=db,
            before_state={
                "line_type": line.line_type, "line_scope": line.line_scope,
                "quantity": line.quantity, "calculated_amount": line.calculated_amount,
                "source_type": line.source_type, "status": line.status, "notes": line.notes,
            },
            after_state={"status": "Void"}, driver_id=line.driver_id, work_date=None,
            line_type=line.line_type,
        )

    return await _get_line_by_id(line_id, company_id, db)


# ---------------------------------------------------------------------------
# CP-3A: Canonical Bonus Event CRUD
# ---------------------------------------------------------------------------

_BONUS_ENTRY_ALLOWED_STATUSES: set[str] = {"Open", "Returned"}


async def _get_bonus_event_by_id(
    bonus_event_id: int,
    company_id: int,
    db: AsyncConnection,
) -> BonusEventResponse:
    result = await db.execute(
        text("""
            SELECT
                be.payrollbonuseventid,
                be.payrollperiodid,
                be.companyid,
                be.branchid,
                be.driverid,
                be.amount,
                be.reason,
                be.notes,
                be.status,
                be.datarevision,
                be.sourcedraftlineid,
                be.voidedbyuserid,
                be.voidedatutc,
                be.voidreason,
                be.createdbyuserid,
                be.createdatutc,
                be.updatedbyuserid,
                be.updatedatutc
            FROM payroll.payrollbonusevents be
            WHERE be.payrollbonuseventid = :beid
              AND be.companyid           = :company_id
        """),
        {"beid": bonus_event_id, "company_id": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Bonus event not found.")
    return BonusEventResponse(
        bonus_event_id=int(row["payrollbonuseventid"]),
        period_id=int(row["payrollperiodid"]),
        company_id=int(row["companyid"]),
        branch_id=int(row["branchid"]),
        driver_id=int(row["driverid"]),
        amount=Decimal(str(row["amount"])),
        reason=row["reason"],
        notes=row["notes"],
        status=row["status"],
        data_revision=int(row["datarevision"]),
        source_draft_line_id=row["sourcedraftlineid"],
        voided_by_user_id=row["voidedbyuserid"],
        voided_at_utc=row["voidedatutc"],
        void_reason=row["voidreason"],
        created_by_user_id=row["createdbyuserid"],
        created_at_utc=row["createdatutc"],
        updated_by_user_id=row["updatedbyuserid"],
        updated_at_utc=row["updatedatutc"],
    )


async def list_bonus_events(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    driver_id: int | None = None,
) -> list[BonusEventResponse]:
    await _require_not_driver_role(company_id, user_id, db)
    period = await get_period_by_id(company_id, user_id, period_id, db)
    await _check_permission(company_id, user_id, period.branch_id, "payroll.view", db)

    params: dict = {"period_id": period_id, "company_id": company_id}
    driver_filter = ""
    if driver_id is not None:
        driver_filter = "AND be.driverid = :driver_id"
        params["driver_id"] = driver_id

    result = await db.execute(
        text(f"""
            SELECT
                be.payrollbonuseventid,
                be.payrollperiodid,
                be.companyid,
                be.branchid,
                be.driverid,
                be.amount,
                be.reason,
                be.notes,
                be.status,
                be.datarevision,
                be.sourcedraftlineid,
                be.voidedbyuserid,
                be.voidedatutc,
                be.voidreason,
                be.createdbyuserid,
                be.createdatutc,
                be.updatedbyuserid,
                be.updatedatutc
            FROM payroll.payrollbonusevents be
            WHERE be.payrollperiodid = :period_id
              AND be.companyid       = :company_id
              {driver_filter}
            ORDER BY be.driverid, be.payrollbonuseventid
        """),
        params,
    )
    rows = result.mappings().fetchall()
    return [
        BonusEventResponse(
            bonus_event_id=int(r["payrollbonuseventid"]),
            period_id=int(r["payrollperiodid"]),
            company_id=int(r["companyid"]),
            branch_id=int(r["branchid"]),
            driver_id=int(r["driverid"]),
            amount=Decimal(str(r["amount"])),
            reason=r["reason"],
            notes=r["notes"],
            status=r["status"],
            data_revision=int(r["datarevision"]),
            source_draft_line_id=r["sourcedraftlineid"],
            voided_by_user_id=r["voidedbyuserid"],
            voided_at_utc=r["voidedatutc"],
            void_reason=r["voidreason"],
            created_by_user_id=r["createdbyuserid"],
            created_at_utc=r["createdatutc"],
            updated_by_user_id=r["updatedbyuserid"],
            updated_at_utc=r["updatedatutc"],
        )
        for r in rows
    ]


async def _increment_bonus_data_revision(
    db: AsyncConnection,
    *,
    company_id: int,
    period_id: int,
    expected_bonus_data_revision: int | None = None,
) -> int:
    """
    Atomically increment payroll.PayrollPeriods.BonusDataRevision by 1 and
    return the new value (CP-3B2a).

    This is the period-level bonus-mutation concurrency token. Every
    successful bonus create/update/void increments it exactly once, in the
    same transaction as the event write. It is intentionally NOT derived
    from MAX(PayrollBonusEvents.DataRevision) — a voided or superseded
    event's per-row revision is not a reliable period-wide aggregate.

    With expected_bonus_data_revision supplied, the UPDATE is predicated on
    the current value matching it; zero rows means another writer already
    moved the revision, and this raises 409. This is the guard a future
    bonus batch (CP-3B2b) will use. Without it (today's single-event
    mutation paths), the UPDATE is unconditional and only fails to find a
    row if the period itself vanished mid-transaction — which cannot happen
    under the FOR UPDATE lock already held by every caller of this helper
    via _lock_period_for_mutation.
    """
    params: dict = {"pid": period_id, "cid": company_id}
    where_extra = ""
    if expected_bonus_data_revision is not None:
        where_extra = "AND bonusdatarevision = :expected"
        params["expected"] = expected_bonus_data_revision

    result = await db.execute(
        text(f"""
            UPDATE payroll.payrollperiods
            SET    bonusdatarevision = bonusdatarevision + 1
            WHERE  payrollperiodid = :pid
              AND  companyid       = :cid
              {where_extra}
            RETURNING bonusdatarevision
        """),
        params,
    )
    row = result.first()
    if row is None:
        if expected_bonus_data_revision is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Bonus data has been modified by another request. "
                    f"Expected revision {expected_bonus_data_revision}. "
                    "Re-fetch and retry."
                ),
            )
        raise HTTPException(status_code=404, detail="Payroll period not found.")
    return int(row[0])


async def create_bonus_event(
    period_id: int,
    company_id: int,
    user_id: int,
    data: "BonusEventCreate",
    db: AsyncConnection,
) -> BonusEventResponse:
    await _require_not_driver_role(company_id, user_id, db)
    period = await get_period_by_id(company_id, user_id, period_id, db)

    if period.status not in _BONUS_ENTRY_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Bonus events can only be added to Open or Returned periods. "
                f"Current status: '{period.status}'."
            ),
        )

    await _check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    # CP-3A: same snapshot-aware eligibility guard as non-BONUS period-pay.
    await _assert_driver_eligible_for_period_via_snapshot(
        company_id, period.branch_id, period_id, data.driver_id, db
    )

    await _lock_period_for_mutation(period_id, company_id, db)

    insert_result = await db.execute(
        text("""
            INSERT INTO payroll.payrollbonusevents
                (companyid, branchid, payrollperiodid, driverid,
                 amount, reason, notes, status,
                 createdbyuserid, createdatutc, datarevision)
            VALUES
                (:company_id, :branch_id, :period_id, :driver_id,
                 :amount, :reason, :notes, 'Active',
                 :user_id, NOW(), 1)
            RETURNING payrollbonuseventid
        """),
        {
            "company_id": company_id,
            "branch_id":  period.branch_id,
            "period_id":  period_id,
            "driver_id":  data.driver_id,
            "amount":     data.amount,
            "reason":     data.reason,
            "notes":      data.notes,
            "user_id":    user_id,
        },
    )
    new_id = insert_result.scalar_one()

    # CP-3B2a: one successful create = period BonusDataRevision +1, in the
    # same transaction as the insert. A later failure (e.g. audit write)
    # rolls this back along with the event insert.
    await _increment_bonus_data_revision(db, company_id=company_id, period_id=period_id)

    await _write_line_audit(
        db,
        company_id=company_id,
        branch_id=period.branch_id,
        user_id=user_id,
        line_id=new_id,
        action_code="BONUS_EVENT_ADDED",
        old_value=None,
        new_value={"driver_id": data.driver_id, "amount": str(data.amount)},
        entity_name="PayrollBonusEvents",
    )
    await capture_period_audit_evidence(
        company_id=company_id, branch_id=period.branch_id, period_id=period_id,
        domain="BONUS", action_code="BONUS_CREATED", source_entity_type="PayrollBonusEvents",
        source_entity_id=new_id, user_id=user_id, required_permission_code="payroll.entry", db=db,
        after_state={
            "driver_id": data.driver_id, "amount": data.amount, "reason": data.reason,
            "notes": data.notes, "status": "Active", "data_revision": 1,
        }, driver_id=data.driver_id, source_revision=1,
    )

    return await _get_bonus_event_by_id(new_id, company_id, db)


async def update_bonus_event(
    period_id: int,
    bonus_event_id: int,
    company_id: int,
    user_id: int,
    data: "BonusEventUpdate",
    db: AsyncConnection,
) -> BonusEventResponse:
    await _require_not_driver_role(company_id, user_id, db)
    period = await get_period_by_id(company_id, user_id, period_id, db)

    if period.status not in _BONUS_ENTRY_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Bonus events can only be updated on Open or Returned periods. "
                f"Current status: '{period.status}'."
            ),
        )

    await _check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    event = await _get_bonus_event_by_id(bonus_event_id, company_id, db)
    if event.period_id != period_id:
        raise HTTPException(status_code=404, detail="Bonus event not found in this period.")
    if event.status == "Voided":
        raise HTTPException(status_code=422, detail="Cannot update a voided bonus event.")

    # Friendlier early 409 when the caller supplied a stale revision. This is
    # NOT the actual concurrency guard — a concurrent writer could still slip
    # in between this check and the UPDATE below. The atomic WHERE-clause
    # predicate on the UPDATE itself (CP-3B2a) is what actually enforces it.
    if data.data_revision is not None and data.data_revision != event.data_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Bonus event has been modified by another request. "
                f"Expected revision {data.data_revision}, found {event.data_revision}. "
                "Re-fetch and retry."
            ),
        )

    # Build SET clause dynamically — only update provided fields.
    set_parts = ["updatedbyuserid = :user_id", "updatedatutc = NOW()",
                 "datarevision = datarevision + 1"]
    params: dict = {
        "beid": bonus_event_id,
        "company_id": company_id,
        "period_id": period_id,
        "branch_id": period.branch_id,
        "user_id": user_id,
    }
    old_snap: dict = {}
    new_snap: dict = {}

    if data.amount is not None:
        set_parts.append("amount = :amount")
        params["amount"] = data.amount
        old_snap["amount"] = str(event.amount)
        new_snap["amount"] = str(data.amount)
    if data.reason is not None:
        set_parts.append("reason = :reason")
        params["reason"] = data.reason
        old_snap["reason"] = event.reason
        new_snap["reason"] = data.reason
    if data.notes is not None:
        set_parts.append("notes = :notes")
        params["notes"] = data.notes
        old_snap["notes"] = event.notes
        new_snap["notes"] = data.notes

    if not (old_snap or new_snap):
        # No-op update — return current state unchanged.
        return event

    await _lock_period_for_mutation(period_id, company_id, db)

    # CP-3B2a: atomic predicate. Always scoped by period/company/branch AND
    # Status='Active' (a concurrent void between the checks above and this
    # UPDATE must not silently overwrite a now-voided event). When the caller
    # supplied data_revision, it is also part of the WHERE clause — this is
    # the real optimistic-concurrency guard, not the earlier pre-check.
    where_parts = [
        "payrollbonuseventid = :beid",
        "companyid           = :company_id",
        "payrollperiodid     = :period_id",
        "branchid            = :branch_id",
        "status              = 'Active'",
    ]
    if data.data_revision is not None:
        where_parts.append("datarevision = :expected_data_revision")
        params["expected_data_revision"] = data.data_revision

    set_clause = ", ".join(set_parts)
    where_clause = " AND ".join(where_parts)
    result = await db.execute(
        text(f"""
            UPDATE payroll.payrollbonusevents
            SET    {set_clause}
            WHERE  {where_clause}
            RETURNING payrollbonuseventid
        """),
        params,
    )
    if result.first() is None:
        # Stale revision or concurrently voided between the fetch above and
        # this UPDATE. Neither the event nor the period's BonusDataRevision
        # was changed by this request.
        raise HTTPException(
            status_code=409,
            detail=(
                "Bonus event has been modified by another request. "
                "Re-fetch and retry."
            ),
        )

    await _increment_bonus_data_revision(db, company_id=company_id, period_id=period_id)

    await _write_line_audit(
        db,
        company_id=company_id,
        branch_id=period.branch_id,
        user_id=user_id,
        line_id=bonus_event_id,
        action_code="BONUS_EVENT_UPDATED",
        old_value=old_snap,
        new_value=new_snap,
        entity_name="PayrollBonusEvents",
    )
    await capture_period_audit_evidence(
        company_id=company_id, branch_id=period.branch_id, period_id=period_id,
        domain="BONUS", action_code="BONUS_UPDATED", source_entity_type="PayrollBonusEvents",
        source_entity_id=bonus_event_id, user_id=user_id, required_permission_code="payroll.entry", db=db,
        before_state={
            "driver_id": event.driver_id, "amount": event.amount, "reason": event.reason,
            "notes": event.notes, "status": event.status, "data_revision": event.data_revision,
        },
        after_state={
            "driver_id": event.driver_id,
            "amount": data.amount if data.amount is not None else event.amount,
            "reason": data.reason if data.reason is not None else event.reason,
            "notes": data.notes if data.notes is not None else event.notes,
            "status": "Active", "data_revision": event.data_revision + 1,
        }, driver_id=event.driver_id, source_revision=event.data_revision + 1,
    )

    return await _get_bonus_event_by_id(bonus_event_id, company_id, db)


async def void_bonus_event(
    period_id: int,
    bonus_event_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> BonusEventResponse:
    await _require_not_driver_role(company_id, user_id, db)
    period = await get_period_by_id(company_id, user_id, period_id, db)

    if period.status not in _BONUS_ENTRY_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Bonus events can only be voided on Open or Returned periods. "
                f"Current status: '{period.status}'."
            ),
        )

    await _check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    event = await _get_bonus_event_by_id(bonus_event_id, company_id, db)
    if event.period_id != period_id:
        raise HTTPException(status_code=404, detail="Bonus event not found in this period.")

    # Idempotent: already voided → return as-is. No revision bump — this
    # request changed nothing.
    if event.status == "Voided":
        return event

    await _lock_period_for_mutation(period_id, company_id, db)

    # CP-3B2a: atomic predicate — Status='Active' in the WHERE clause means a
    # concurrent void between the check above and this UPDATE affects zero
    # rows here, which we then treat as the same idempotent case (re-fetch
    # and return the now-Voided state without bumping the revision again).
    result = await db.execute(
        text("""
            UPDATE payroll.payrollbonusevents
            SET    status          = 'Voided',
                   voidedbyuserid  = :user_id,
                   voidedatutc     = NOW(),
                   updatedbyuserid = :user_id,
                   updatedatutc    = NOW(),
                   datarevision    = datarevision + 1
            WHERE  payrollbonuseventid = :beid
              AND  companyid           = :company_id
              AND  payrollperiodid     = :period_id
              AND  branchid            = :branch_id
              AND  status              = 'Active'
            RETURNING payrollbonuseventid
        """),
        {
            "beid":       bonus_event_id,
            "company_id": company_id,
            "period_id":  period_id,
            "branch_id":  period.branch_id,
            "user_id":    user_id,
        },
    )
    if result.first() is None:
        # Concurrently voided between the fetch above and this UPDATE.
        return await _get_bonus_event_by_id(bonus_event_id, company_id, db)

    await _increment_bonus_data_revision(db, company_id=company_id, period_id=period_id)

    await _write_line_audit(
        db,
        company_id=company_id,
        branch_id=period.branch_id,
        user_id=user_id,
        line_id=bonus_event_id,
        action_code="BONUS_EVENT_VOIDED",
        old_value={"status": "Active"},
        new_value={"status": "Voided"},
        entity_name="PayrollBonusEvents",
    )
    await capture_period_audit_evidence(
        company_id=company_id, branch_id=period.branch_id, period_id=period_id,
        domain="BONUS", action_code="BONUS_VOIDED", source_entity_type="PayrollBonusEvents",
        source_entity_id=bonus_event_id, user_id=user_id, required_permission_code="payroll.entry", db=db,
        before_state={
            "driver_id": event.driver_id, "amount": event.amount, "reason": event.reason,
            "notes": event.notes, "status": event.status, "data_revision": event.data_revision,
        },
        after_state={"status": "Voided", "data_revision": event.data_revision + 1},
        driver_id=event.driver_id, source_revision=event.data_revision + 1,
    )

    return await _get_bonus_event_by_id(bonus_event_id, company_id, db)


# ---------------------------------------------------------------------------
# CP-3B2b: Create-only transactional bonus batch
# ---------------------------------------------------------------------------

def _bonus_batch_canonical_payload(
    expected_bonus_data_revision: int,
    items: list["BonusBatchItem"],
) -> dict:
    """Build the canonical request payload used both for hashing and durable
    storage.  Item order is preserved (significant); amounts are fixed
    two-decimal strings so numerically-equal inputs hash identically."""
    return {
        "version": 1,
        "expected_bonus_data_revision": expected_bonus_data_revision,
        "items": [
            {
                "driver_id": it.driver_id,
                "amount":    f"{it.amount:.2f}",
                "reason":    it.reason,
                "notes":     it.notes,
            }
            for it in items
        ],
    }


def _bonus_batch_request_hash(payload: dict) -> str:
    """SHA-256 hex of the canonical payload: UTF-8, keys sorted, compact
    separators.  Deterministic across equal requests; sensitive to item order
    (a reordered item list is a different request)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _get_bonus_events_in_order(
    ids: list[int],
    company_id: int,
    db: AsyncConnection,
) -> list[BonusEventResponse]:
    return [await _get_bonus_event_by_id(i, company_id, db) for i in ids]


async def apply_bonus_batch(
    period_id: int,
    company_id: int,
    user_id: int,
    data: "BonusBatchCreate",
    db: AsyncConnection,
) -> tuple[BonusBatchResponse, bool]:
    """
    Create-only transactional bonus batch (CP-3B2b).

    All-or-nothing: every item is validated before any event is inserted, and
    the whole request runs in one transaction (via get_db's engine.begin()), so
    any failure rolls back all events, the batch-request row, the revision bump,
    and every audit row.

    Idempotency: keyed on (Company, Branch, Period, IdempotencyKey). An exact
    replay (same key + same request hash) returns the stored result read-only —
    no writes, no revision bump, and it succeeds even if the period later became
    non-editable. A same-key/different-payload request is a 409.

    Returns (response, is_new): is_new=True for a fresh apply (HTTP 201),
    False for an idempotent replay (HTTP 200).
    """
    await _require_not_driver_role(company_id, user_id, db)
    period = await get_period_by_id(company_id, user_id, period_id, db)
    await _check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    branch_id = period.branch_id
    payload = _bonus_batch_canonical_payload(data.expected_bonus_data_revision, data.items)
    request_hash = _bonus_batch_request_hash(payload)

    # ── Lock the period row (does NOT enforce editability — replay must work on
    #    a now-locked/archived period). All idempotency and write decisions
    #    below happen under this lock, serializing concurrent batches and
    #    single-event mutations on the same period. ──────────────────────────── #
    locked = (await db.execute(
        text(
            "SELECT status FROM payroll.payrollperiods "
            "WHERE payrollperiodid = :pid AND companyid = :cid "
            "FOR UPDATE"
        ),
        {"pid": period_id, "cid": company_id},
    )).mappings().first()
    if locked is None:
        raise HTTPException(status_code=404, detail="Payroll period not found.")
    locked_status = locked["status"]

    # ── Idempotency lookup (under lock) ────────────────────────────────────── #
    existing = (await db.execute(
        text("""
            SELECT payrollbonusbatchrequestid, requesthash, batchcorrelationid,
                   idempotencykey, expectedbonusdatarevision, resultbonusdatarevision,
                   createdeventids, createdeventcount
            FROM   payroll.payrollbonusbatchrequests
            WHERE  companyid       = :cid
              AND  branchid        = :bid
              AND  payrollperiodid = :pid
              AND  idempotencykey  = :key
        """),
        {"cid": company_id, "bid": branch_id, "pid": period_id, "key": data.idempotency_key},
    )).mappings().first()

    if existing is not None:
        if existing["requesthash"] != request_hash:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Idempotency key already used for a different bonus batch "
                    "payload (or a different expected revision) in this period."
                ),
            )
        # Exact replay — read-only, no status check, no writes, no bump.
        raw_ids = existing["createdeventids"]
        stored_ids = raw_ids if isinstance(raw_ids, list) else json.loads(raw_ids)
        events = await _get_bonus_events_in_order(list(stored_ids), company_id, db)
        response = BonusBatchResponse(
            period_id=period_id,
            branch_id=branch_id,
            batch_request_id=int(existing["payrollbonusbatchrequestid"]),
            idempotency_key=existing["idempotencykey"],
            batch_correlation_id=str(existing["batchcorrelationid"]),
            expected_bonus_data_revision=int(existing["expectedbonusdatarevision"]),
            result_bonus_data_revision=int(existing["resultbonusdatarevision"]),
            created_event_count=int(existing["createdeventcount"]),
            created_event_ids=[int(i) for i in stored_ids],
            events=events,
            replayed=True,
        )
        return response, False

    # ── New apply path ─────────────────────────────────────────────────────── #
    if locked_status not in _BONUS_ENTRY_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Bonus batches can only be applied to Open or Returned periods. "
                f"Current status: '{locked_status}'."
            ),
        )

    # Validate every item before any insert (all-or-nothing). The eligibility
    # guard raises the same 422s as single-event POST /bonuses, including the
    # IncludedByExistingData-without-source and wrong-branch cases.
    for idx, item in enumerate(data.items):
        try:
            await _assert_driver_eligible_for_period_via_snapshot(
                company_id, branch_id, period_id, item.driver_id, db
            )
        except HTTPException as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=f"items[{idx}] (driver {item.driver_id}): {exc.detail}",
            )

    # Predicated revision bump — 409 if the caller's expected revision is stale.
    # Exactly one increment for the whole batch.
    result_revision = await _increment_bonus_data_revision(
        db,
        company_id=company_id,
        period_id=period_id,
        expected_bonus_data_revision=data.expected_bonus_data_revision,
    )

    batch_correlation_id = str(_uuid.uuid4())

    # Insert every event, stamped with the shared correlation id and the batch
    # idempotency key (non-unique on events — the authoritative unique record is
    # the PayrollBonusBatchRequests row).
    created_ids: list[int] = []
    total_amount = Decimal("0")
    for item in data.items:
        ins = await db.execute(
            text("""
                INSERT INTO payroll.payrollbonusevents
                    (companyid, branchid, payrollperiodid, driverid,
                     amount, reason, notes, status,
                     batchcorrelationid, idempotencykey,
                     createdbyuserid, createdatutc, datarevision)
                VALUES
                    (:cid, :bid, :pid, :did,
                     :amount, :reason, :notes, 'Active',
                     CAST(:corr AS UUID), :key,
                     :uid, NOW(), 1)
                RETURNING payrollbonuseventid
            """),
            {
                "cid":    company_id,
                "bid":    branch_id,
                "pid":    period_id,
                "did":    item.driver_id,
                "amount": item.amount,
                "reason": item.reason,
                "notes":  item.notes,
                "corr":   batch_correlation_id,
                "key":    data.idempotency_key,
                "uid":    user_id,
            },
        )
        created_ids.append(int(ins.scalar_one()))
        total_amount += item.amount

    # Insert the durable batch-request row. A concurrent same-key insert (should
    # be serialized by the period lock, but belt-and-suspenders) trips the unique
    # idempotency index — surface a clean 409 instead of a raw 500.
    try:
        batch_ins = await db.execute(
            text("""
                INSERT INTO payroll.payrollbonusbatchrequests
                    (companyid, branchid, payrollperiodid,
                     idempotencykey, requesthash, requestpayloadjson, batchcorrelationid,
                     expectedbonusdatarevision, resultbonusdatarevision,
                     createdeventids, createdeventcount, status,
                     createdbyuserid, createdatutc, appliedatutc)
                VALUES
                    (:cid, :bid, :pid,
                     :key, :hash, CAST(:payload AS JSONB), CAST(:corr AS UUID),
                     :expected, :result,
                     CAST(:event_ids AS JSONB), :event_count, 'Applied',
                     :uid, NOW(), NOW())
                RETURNING payrollbonusbatchrequestid
            """),
            {
                "cid":         company_id,
                "bid":         branch_id,
                "pid":         period_id,
                "key":         data.idempotency_key,
                "hash":        request_hash,
                "payload":     json.dumps(payload),
                "corr":        batch_correlation_id,
                "expected":    data.expected_bonus_data_revision,
                "result":      result_revision,
                "event_ids":   json.dumps(created_ids),
                "event_count": len(created_ids),
                "uid":         user_id,
            },
        )
    except SAIntegrityError:
        raise HTTPException(
            status_code=409,
            detail=(
                "A bonus batch with this idempotency key is already being "
                "applied for this period. Retry to receive the applied result."
            ),
        )
    batch_request_id = int(batch_ins.scalar_one())

    # Per-event audit rows, all sharing the batch correlation id.
    for created_id, item in zip(created_ids, data.items):
        await _write_line_audit(
            db,
            company_id=company_id,
            branch_id=branch_id,
            user_id=user_id,
            line_id=created_id,
            action_code="BONUS_EVENT_ADDED",
            old_value=None,
            new_value={"driver_id": item.driver_id, "amount": str(item.amount)},
            entity_name="PayrollBonusEvents",
            correlation_id=batch_correlation_id,
        )
        await capture_period_audit_evidence(
            company_id=company_id, branch_id=branch_id, period_id=period_id,
            domain="BONUS", action_code="BONUS_CREATED", source_entity_type="PayrollBonusEvents",
            source_entity_id=created_id, user_id=user_id, required_permission_code="payroll.entry",
            db=db,
            after_state={
                "driver_id": item.driver_id, "amount": item.amount, "reason": item.reason,
                "notes": item.notes, "status": "Active", "data_revision": 1,
            }, driver_id=item.driver_id, correlation_id=batch_correlation_id, source_revision=1,
        )

    # One batch-level audit row, same correlation id.
    await _write_line_audit(
        db,
        company_id=company_id,
        branch_id=branch_id,
        user_id=user_id,
        line_id=batch_request_id,
        action_code="BONUS_BATCH_APPLIED",
        old_value=None,
        new_value={
            "idempotency_key":               data.idempotency_key,
            "request_hash":                  request_hash,
            "batch_correlation_id":          batch_correlation_id,
            "item_count":                    len(created_ids),
            "created_event_ids":             created_ids,
            "expected_bonus_data_revision":  data.expected_bonus_data_revision,
            "result_bonus_data_revision":    result_revision,
            "total_amount":                  str(total_amount),
        },
        entity_name="PayrollBonusBatchRequests",
        correlation_id=batch_correlation_id,
    )

    events = await _get_bonus_events_in_order(created_ids, company_id, db)
    response = BonusBatchResponse(
        period_id=period_id,
        branch_id=branch_id,
        batch_request_id=batch_request_id,
        idempotency_key=data.idempotency_key,
        batch_correlation_id=batch_correlation_id,
        expected_bonus_data_revision=data.expected_bonus_data_revision,
        result_bonus_data_revision=result_revision,
        created_event_count=len(created_ids),
        created_event_ids=created_ids,
        events=events,
        replayed=False,
    )
    return response, True


# ---------------------------------------------------------------------------
# CP-3B1: Zero-inclusive bonus summary
# ---------------------------------------------------------------------------

async def _bonus_summary_driver_create_eligible(
    eligibility_reason_code: str,
    period_id: int,
    driver_id: int,
    db: AsyncConnection,
) -> bool:
    """Read-only mirror of the snapshot branch of
    _assert_driver_eligible_for_period_via_snapshot — never raises, never
    mutates.  The bonus summary is only ever computed for periods that have
    a CP-2E snapshot (enforced earlier in get_bonus_summary), so only the
    snapshot branch of that eligibility check is relevant here.

    IncludedByExistingData drivers are period-eligible for VIEWING but not for
    NEW bonus creation unless they already have a period-pay source — this
    must match create_bonus_event's guard exactly so summary capabilities
    never claim can_create=true when POST /bonuses would reject.
    """
    if eligibility_reason_code == "IncludedByExistingData":
        return await _driver_has_existing_period_pay_source(period_id, driver_id, db)
    return True  # Active / TerminatedHistorical / Transferred


async def get_bonus_summary(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> BonusSummaryResponse:
    """
    Zero-inclusive bonus summary for one period.

    Roster source is the CP-2E period eligibility snapshot ONLY — every
    snapshot-eligible driver appears even with zero bonus events, and bonus
    events for drivers not in the snapshot never expand the roster.  Periods
    without a CP-2E marker get a controlled 422 (no live-roster fallback).
    """
    await _require_not_driver_role(company_id, user_id, db)
    period = await get_period_by_id(company_id, user_id, period_id, db)

    # Draft (Prepared) periods have no financial exposure — same rule as the
    # period-eligible-drivers endpoint (CP-2F).
    if period.status == "Draft":
        raise HTTPException(
            status_code=422,
            detail="Bonus summary is not available for Prepared (Draft) periods.",
        )

    await _check_any_permission(
        company_id, user_id, period.branch_id, ["payroll.view", "payroll.entry"], db
    )

    if not await _period_has_driver_eligibility_snapshot(period_id, db):
        raise HTTPException(
            status_code=422,
            detail=(
                "BONUS_SUMMARY_UNAVAILABLE_NO_ELIGIBILITY_SNAPSHOT: "
                "the zero-inclusive bonus summary requires a period eligibility "
                "snapshot (CP-2E). This period has no snapshot marker, and the "
                "summary never falls back to the live driver roster."
            ),
        )

    # ── Roster: snapshot rows only ─────────────────────────────────────────── #
    roster_result = await db.execute(
        text("""
            SELECT ppde.driverid,
                   ppde.drivercodesnapshot,
                   ppde.drivernamesnapshot,
                   ppde.eligibilityreasoncode
            FROM   payroll.payrollperioddrivereligibility ppde
            WHERE  ppde.payrollperiodid     = :period_id
              AND  ppde.companyid           = :company_id
              AND  ppde.branchid            = :branch_id
              AND  ppde.iseligibleforperiod = TRUE
        """),
        {"period_id": period_id, "company_id": company_id, "branch_id": period.branch_id},
    )
    roster_rows = roster_result.mappings().fetchall()

    # ── Events: canonical PayrollBonusEvents only ──────────────────────────── #
    # BranchID is part of the filter (not just PeriodID/CompanyID) so a
    # same-company, cross-branch contaminated row can never contribute to a
    # snapshot driver's totals — PayrollBonusEvents.BranchID is denormalized
    # from the period at write time, so this filter is authoritative.
    events_result = await db.execute(
        text("""
            SELECT
                be.payrollbonuseventid,
                be.driverid,
                be.amount,
                be.reason,
                be.notes,
                be.status,
                be.datarevision,
                be.batchcorrelationid,
                be.idempotencykey,
                be.sourcedraftlineid,
                be.voidedbyuserid,
                be.voidedatutc,
                be.voidreason,
                be.createdbyuserid,
                be.createdatutc,
                be.updatedbyuserid,
                be.updatedatutc
            FROM payroll.payrollbonusevents be
            WHERE be.payrollperiodid = :period_id
              AND be.companyid       = :company_id
              AND be.branchid        = :branch_id
            ORDER BY be.driverid, be.payrollbonuseventid
        """),
        {"period_id": period_id, "company_id": company_id, "branch_id": period.branch_id},
    )
    events_by_driver: dict[int, list[BonusSummaryEvent]] = {}
    for r in events_result.mappings().fetchall():
        events_by_driver.setdefault(int(r["driverid"]), []).append(
            BonusSummaryEvent(
                bonus_event_id=int(r["payrollbonuseventid"]),
                driver_id=int(r["driverid"]),
                amount=Decimal(str(r["amount"])),
                reason=r["reason"],
                notes=r["notes"],
                status=r["status"],
                created_by_user_id=r["createdbyuserid"],
                created_at_utc=r["createdatutc"],
                updated_by_user_id=r["updatedbyuserid"],
                updated_at_utc=r["updatedatutc"],
                voided_by_user_id=r["voidedbyuserid"],
                voided_at_utc=r["voidedatutc"],
                void_reason=r["voidreason"],
                data_revision=int(r["datarevision"]),
                batch_correlation_id=(
                    str(r["batchcorrelationid"]) if r["batchcorrelationid"] is not None else None
                ),
                idempotency_key=r["idempotencykey"],
                source_draft_line_id=r["sourcedraftlineid"],
            )
        )

    # ── Period/user-wide mutation preconditions (shared by every driver) ──── #
    period_reason_codes: list[str] = []
    if period.status not in _BONUS_ENTRY_ALLOWED_STATUSES:
        period_reason_codes.append("status_read_only")
    has_entry = await _has_any_permission(
        company_id, user_id, period.branch_id, ["payroll.entry"], db
    )
    if not has_entry:
        period_reason_codes.append("permission_entry_required")
    period_allows_mutation = not period_reason_codes

    # ── Aggregate events onto the snapshot roster, with per-driver capabilities #
    drivers: list[BonusSummaryDriver] = []
    for row in roster_rows:
        driver_id = int(row["driverid"])
        reason_code = row["eligibilityreasoncode"]
        events = events_by_driver.get(driver_id, [])
        active_events = [e for e in events if e.status == "Active"]

        reason_codes = list(period_reason_codes)
        can_create = period_allows_mutation
        if period_allows_mutation:
            driver_create_eligible = await _bonus_summary_driver_create_eligible(
                reason_code, period_id, driver_id, db
            )
            if not driver_create_eligible:
                can_create = False
                reason_codes.append("eligibility_existing_data_only")

        can_update_void = period_allows_mutation
        if period_allows_mutation and not active_events:
            can_update_void = False
            reason_codes.append("no_active_bonus_event")

        drivers.append(
            BonusSummaryDriver(
                driver_id=driver_id,
                driver_code=row["drivercodesnapshot"],
                driver_name=row["drivernamesnapshot"],
                eligibility_reason_code=reason_code,
                total_bonus=sum((e.amount for e in active_events), Decimal("0")),
                active_event_count=len(active_events),
                voided_event_count=len(events) - len(active_events),
                events=events,
                capabilities=BonusSummaryCapabilities(
                    can_create=can_create,
                    can_update=can_update_void,
                    can_void=can_update_void,
                    reason_codes=reason_codes,
                ),
            )
        )

    # Nonzero totals first (descending), then stable name/code/id order.
    drivers.sort(
        key=lambda d: (
            0 if d.total_bonus > 0 else 1,
            -d.total_bonus,
            d.driver_name or "",
            d.driver_code or "",
            d.driver_id,
        )
    )

    # CP-3B2a: bonus_data_revision comes straight from PayrollPeriods, never
    # from MAX(PayrollBonusEvents.DataRevision).
    revision_row = (await db.execute(
        text(
            "SELECT bonusdatarevision FROM payroll.payrollperiods "
            "WHERE payrollperiodid = :pid AND companyid = :cid"
        ),
        {"pid": period_id, "cid": company_id},
    )).first()
    bonus_data_revision = int(revision_row[0]) if revision_row is not None else 0

    return BonusSummaryResponse(
        period_id=period_id,
        branch_id=period.branch_id,
        period_status=period.status,
        eligibility_source="PeriodEligibilitySnapshot",
        drivers=drivers,
        active_event_count=sum(d.active_event_count for d in drivers),
        active_bonus_total=sum((d.total_bonus for d in drivers), Decimal("0")),
        bonus_data_revision=bonus_data_revision,
    )


# ---------------------------------------------------------------------------
# Period-eligible drivers (P1 #1)
# ---------------------------------------------------------------------------

async def get_period_eligible_drivers(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> list[dict]:
    """
    Return drivers eligible for a Bonus (or other period-pay line) for the
    given period.

    Eligibility = period-scoped, NOT day-scoped:
      1. Active drivers (employmentstatus='Active' AND driverstatus='Active')
         whose hire/termination window overlaps the period dates.
      2. OR any driver who already has period-pay lines in this period —
         so existing bonuses stay voidable even if the driver was later
         terminated.

    ODA/Driver users are blocked unconditionally (same boundary as day-grid).
    payroll.view OR payroll.entry permission is required.
    """
    # ── Driver-role hard-block ───────────────────────────────────────────────── #
    await _require_not_driver_role(company_id, user_id, db)

    period = await get_period_by_id(company_id, user_id, period_id, db)

    # CP-2F: Period Pay / Bonus eligible driver list is a financial path — block for Draft.
    if period.status == "Draft":
        raise HTTPException(
            status_code=422,
            detail="Period eligible drivers are not available for Prepared (Draft) periods.",
        )

    await _check_any_permission(
        company_id, user_id, period.branch_id, ["payroll.view", "payroll.entry"], db
    )

    # CP-2E: For snapshotted periods use the snapshot roster instead of live tables.
    _has_snap = await _period_has_driver_eligibility_snapshot(period_id, db)
    if _has_snap:
        # Active / TerminatedHistorical / Transferred → prospective choices
        # IncludedByExistingData → only if they already have a period-pay line
        snap_result = await db.execute(
            text("""
                SELECT ppde.driverid,
                       COALESCE(ppde.drivernamesnapshot, '') AS drivername,
                       COALESCE(ppde.drivercodesnapshot, '') AS drivercode,
                       ppde.eligibilityreasoncode
                FROM   payroll.payrollperioddrivereligibility ppde
                WHERE  ppde.payrollperiodid = :period_id
                  AND  ppde.companyid       = :cid
                  AND  ppde.branchid        = :bid
                  AND  ppde.iseligibleforperiod = TRUE
                ORDER BY ppde.drivernamesnapshot
            """),
            {"period_id": period_id, "cid": company_id, "bid": period.branch_id},
        )
        snap_rows = list(snap_result.mappings().all())

        # For IBED: check which have existing period-pay lines
        ibed_ids = [r["driverid"] for r in snap_rows if r["eligibilityreasoncode"] == "IncludedByExistingData"]
        ibed_with_period_pay: set[int] = set()
        if ibed_ids:
            in_cl, in_pr = _build_in_clause(ibed_ids, "ibed")
            ibed_res = await db.execute(
                text(f"""
                    SELECT DISTINCT driverid FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :period_id AND linescope = 'Period'
                      AND status != 'Void'
                      AND driverid IN ({in_cl})
                """),
                {"period_id": period_id, **in_pr},
            )
            ibed_with_period_pay = {r["driverid"] for r in ibed_res.mappings().all()}

        out = []
        for r in snap_rows:
            if r["eligibilityreasoncode"] == "IncludedByExistingData":
                if r["driverid"] not in ibed_with_period_pay:
                    continue
            out.append({
                "driver_id":   int(r["driverid"]),
                "driver_name": r["drivername"],
                "driver_code": r["drivercode"],
            })
        return out

    result = await db.execute(
        text("""
            SELECT DISTINCT d.driverid, e.fullname AS drivername, d.drivercode
            FROM   core.drivers   d
            JOIN   core.employees e ON e.employeeid = d.employeeid
            WHERE  d.companyid = :cid
              AND  d.branchid  = :bid
              AND  (
                    -- Active driver whose hire/termination window overlaps the period
                    (    e.employmentstatus = 'Active'
                     AND d.driverstatus     = 'Active'
                     AND (e.hiredate IS NULL OR e.hiredate <= :period_end)
                     AND (e.terminationdate IS NULL OR e.terminationdate >= :period_start)
                    )
                    OR
                    -- Driver who already has period-pay lines in this period
                    -- (keeps existing bonuses voidable even if driver was terminated)
                    EXISTS (
                        SELECT 1
                        FROM   payroll.payrolldraftlines pdl
                        WHERE  pdl.driverid        = d.driverid
                          AND  pdl.payrollperiodid = :period_id
                          AND  pdl.linescope        = 'Period'
                    )
              )
            ORDER BY e.fullname
        """),
        {
            "cid":          company_id,
            "bid":          period.branch_id,
            "period_start": period.start_date,
            "period_end":   period.end_date,
            "period_id":    period_id,
        },
    )
    rows = result.mappings().all()
    return [
        {
            "driver_id":   int(r["driverid"]),
            "driver_name": r["drivername"],
            "driver_code": r["drivercode"],
        }
        for r in rows
    ]


# ===========================================================================
# M15 — Driver Pay Rules (Minimum / Maximum Pay)
# ===========================================================================

_RULE_AUDIT_REASONS: dict[str, str] = {
    "DRIVER_PAY_RULE_CREATED":       "Driver pay rule created",
    "DRIVER_PAY_RULE_ENDED":         "Driver pay rule ended",
    "DRIVER_PAY_RULE_NOTES_UPDATED": "Driver pay rule notes updated",
    "DRIVER_PAY_RULE_VOIDED":        "Driver pay rule voided",
}


async def _write_pay_rule_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    branch_id: int,
    user_id: int,
    rule_id: int,
    action_code: str,
    old_value: dict | None = None,
    new_value: dict | None = None,
) -> None:
    """
    Insert one audit.AuditLog row for a DriverPayRule event.

    Extracted as a module-level function so tests can monkeypatch it to verify
    that all preceding writes roll back when this raises.
    """
    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid,
                 oldvaluejson, newvaluejson, reason, sourcetype)
            VALUES
                (:company_id, :branch_id, :actor_id, :action_code,
                 'payroll', 'DriverPayRules', :entity_id,
                 :old_val, :new_val, :reason, 'Application')
        """),
        {
            "company_id":  company_id,
            "branch_id":   branch_id,
            "actor_id":    user_id,
            "action_code": action_code,
            "entity_id":   str(rule_id),
            "old_val":     json.dumps(old_value) if old_value is not None else None,
            "new_val":     json.dumps(new_value) if new_value is not None else None,
            "reason":      _RULE_AUDIT_REASONS.get(action_code, action_code),
        },
    )


_RULE_SELECT = """
    SELECT
        r.driverpayruleid,
        r.companyid,
        r.branchid,
        r.driverid,
        r.ruletype,
        r.amount,
        r.effectivefrom,
        r.effectiveto,
        r.status,
        r.createdbyuserid,
        r.createdatutc,
        r.updatedbyuserid,
        r.updatedatutc,
        r.notes
    FROM payroll.driverpayrules r
"""


def _rule_row_to_summary(r: Any) -> DriverPayRuleSummary:
    return DriverPayRuleSummary(
        driver_pay_rule_id=r["driverpayruleid"],
        company_id=r["companyid"],
        branch_id=r["branchid"],
        driver_id=r["driverid"],
        rule_type=r["ruletype"],
        amount=r["amount"],
        effective_from=r["effectivefrom"],
        effective_to=r["effectiveto"],
        status=r["status"],
        created_by_user_id=r["createdbyuserid"],
        created_at_utc=r["createdatutc"],
        updated_by_user_id=r["updatedbyuserid"],
        updated_at_utc=r["updatedatutc"],
        notes=r["notes"],
    )


async def _get_rule_by_id_internal(
    rule_id: int,
    company_id: int,
    db: AsyncConnection,
) -> DriverPayRuleSummary:
    """Internal fetch — no branch-access check. Used by other service functions."""
    result = await db.execute(
        text(f"{_RULE_SELECT} WHERE r.driverpayruleid = :rid AND r.companyid = :cid"),
        {"rid": rule_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Driver pay rule not found.")
    return _rule_row_to_summary(row)


async def _count_finalized_periods_in_rule_range(
    driver_id: int,
    company_id: int,
    effective_from: date,
    effective_to: "date | None",
    db: AsyncConnection,
) -> int:
    """
    Count Locked or Archived periods for this driver whose start_date falls
    within the rule's effective range [effective_from, effective_to].

    Both Locked and Archived are treated as finalized: Archived periods have
    already been finalized and locked; their history must be preserved.

    Used by both void (block if > 0) and end (derive minimum closure date).
    A period 'relied on' the rule if it is Locked/Archived AND has at least one
    PayrollFinalLine for this driver (proof that finalization ran).
    """
    result = await db.execute(
        text("""
            SELECT COUNT(*) AS cnt
            FROM   payroll.payrollperiods pp
            WHERE  pp.companyid  = :company_id
              AND  pp.status     IN ('Locked', 'Archived')
              AND  pp.startdate >= :eff_from
              AND  (CAST(:eff_to AS date) IS NULL OR pp.startdate <= CAST(:eff_to AS date))
              AND  EXISTS (
                  SELECT 1
                  FROM   payroll.payrollfinallines fl
                  WHERE  fl.payrollperiodid = pp.payrollperiodid
                    AND  fl.driverid        = :driver_id
              )
        """),
        {
            "company_id": company_id,
            "driver_id":  driver_id,
            "eff_from":   effective_from,
            "eff_to":     effective_to,
        },
    )
    return int(result.scalar_one())


async def _latest_finalized_period_start_in_range(
    driver_id: int,
    company_id: int,
    effective_from: date,
    effective_to: "date | None",
    db: AsyncConnection,
) -> "date | None":
    """
    Return the latest period.start_date among Locked or Archived periods for
    this driver whose start_date falls within the rule's effective range.
    Returns None if no such periods exist.

    Archived is treated as finalized for the same reasons as Locked.
    """
    result = await db.execute(
        text("""
            SELECT MAX(pp.startdate) AS latest
            FROM   payroll.payrollperiods pp
            WHERE  pp.companyid  = :company_id
              AND  pp.status     IN ('Locked', 'Archived')
              AND  pp.startdate >= :eff_from
              AND  (CAST(:eff_to AS date) IS NULL OR pp.startdate <= CAST(:eff_to AS date))
              AND  EXISTS (
                  SELECT 1
                  FROM   payroll.payrollfinallines fl
                  WHERE  fl.payrollperiodid = pp.payrollperiodid
                    AND  fl.driverid        = :driver_id
              )
        """),
        {
            "company_id": company_id,
            "driver_id":  driver_id,
            "eff_from":   effective_from,
            "eff_to":     effective_to,
        },
    )
    row = result.first()
    return row[0] if row and row[0] is not None else None


# ---------------------------------------------------------------------------
# Create driver pay rule
# ---------------------------------------------------------------------------

async def create_driver_pay_rule(
    company_id: int,
    user_id: int,
    data: DriverPayRuleCreate,
    db: AsyncConnection,
) -> DriverPayRuleSummary:
    """
    Create a new Active DriverPayRule.

    Guards:
      - Driver must exist in this company; user must have branch access.
      - Permission: setup.manage
      - Overlap: enforced by DB EXCLUDE constraint (also checked at service level).
      - If both MinimumPay and MaximumPay rules exist for the same period range,
        min <= max is checked at finalization time, not here.
    """
    drv_result = await db.execute(
        text("SELECT driverid, branchid FROM core.drivers WHERE driverid = :did AND companyid = :cid"),
        {"did": data.driver_id, "cid": company_id},
    )
    drv_row = drv_result.mappings().first()
    if drv_row is None:
        raise HTTPException(status_code=404, detail="Driver not found in this company.")

    driver_branch_id: int = drv_row["branchid"]
    await _check_any_permission(
        company_id, user_id, driver_branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )
    await _check_own_driver_only(company_id, user_id, data.driver_id, db)

    # Service-level overlap check (belt-and-suspenders on top of DB EXCLUDE constraint).
    # Check for any Active or Ended rule for same driver+type whose date range overlaps.
    overlap_result = await db.execute(
        text("""
            SELECT driverpayruleid
            FROM   payroll.driverpayrules
            WHERE  companyid     = :cid
              AND  driverid      = :did
              AND  ruletype      = :rtype
              AND  status        IN ('Active', 'Ended')
              AND  effectivefrom <= COALESCE(:eff_to, '9999-12-31'::date)
              AND  (effectiveto IS NULL OR effectiveto >= :eff_from)
            LIMIT 1
        """),
        {
            "cid":     company_id,
            "did":     data.driver_id,
            "rtype":   data.rule_type,
            "eff_from": data.effective_from,
            "eff_to":   data.effective_to,
        },
    )
    if overlap_result.first() is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"A {data.rule_type} rule already exists for this driver that overlaps "
                f"the requested date range [{data.effective_from}, {data.effective_to or 'open'}]. "
                "End the existing rule first, or choose a non-overlapping date range."
            ),
        )

    # Finalized-period guard: a pay rule must not be created with effective_from
    # inside a Locked or Archived payroll period (same as rates backdating guard).
    await _check_not_in_finalized_period(
        company_id, driver_branch_id, data.effective_from, db, label="pay rule"
    )

    insert_result = await db.execute(
        text("""
            INSERT INTO payroll.driverpayrules
                (companyid, branchid, driverid, ruletype, amount,
                 effectivefrom, effectiveto, status, createdbyuserid, notes)
            VALUES
                (:cid, :bid, :did, :rtype, :amount,
                 :eff_from, :eff_to, 'Active', :creator, :notes)
            RETURNING driverpayruleid
        """),
        {
            "cid":      company_id,
            "bid":      driver_branch_id,
            "did":      data.driver_id,
            "rtype":    data.rule_type,
            "amount":   data.amount,
            "eff_from": data.effective_from,
            "eff_to":   data.effective_to,
            "creator":  user_id,
            "notes":    data.notes,
        },
    )
    rule_id: int = insert_result.scalar_one()

    await _write_pay_rule_audit(
        db,
        company_id=company_id,
        branch_id=driver_branch_id,
        user_id=user_id,
        rule_id=rule_id,
        action_code="DRIVER_PAY_RULE_CREATED",
        new_value={
            "driver_id":      data.driver_id,
            "rule_type":      data.rule_type,
            "amount":         str(data.amount),
            "effective_from": str(data.effective_from),
            "effective_to":   str(data.effective_to) if data.effective_to else None,
            "status":         "Active",
        },
    )

    return await _get_rule_by_id_internal(rule_id, company_id, db)


# ---------------------------------------------------------------------------
# List / get driver pay rules
# ---------------------------------------------------------------------------

async def get_driver_pay_rules(
    company_id: int,
    user_id: int,
    driver_id: int,
    db: AsyncConnection,
    *,
    rule_type: str | None = None,
    rule_status: str | None = None,
) -> list[DriverPayRuleSummary]:
    """Return all pay rules for a driver (branch-access + ODA checked)."""
    await _check_driver_read_access(driver_id, company_id, user_id, db)

    conditions = ["r.companyid = :cid", "r.driverid = :did"]
    params: dict[str, Any] = {"cid": company_id, "did": driver_id}

    if rule_type is not None:
        conditions.append("r.ruletype = :rtype")
        params["rtype"] = rule_type
    if rule_status is not None:
        conditions.append("r.status = :rstatus")
        params["rstatus"] = rule_status

    where = " AND ".join(conditions)
    result = await db.execute(
        text(f"{_RULE_SELECT} WHERE {where} ORDER BY r.ruletype, r.effectivefrom DESC"),
        params,
    )
    return [_rule_row_to_summary(r) for r in result.mappings().all()]


async def get_driver_pay_rule_by_id(
    rule_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> DriverPayRuleSummary:
    """Return a single pay rule (branch-access + ODA checked)."""
    rule = await _get_rule_by_id_internal(rule_id, company_id, db)
    await _check_any_permission(
        company_id, user_id, rule.branch_id,
        ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
    )
    await _check_own_driver_only(company_id, user_id, rule.driver_id, db)
    return rule


# ---------------------------------------------------------------------------
# End driver pay rule
# ---------------------------------------------------------------------------

async def end_driver_pay_rule(
    rule_id: int,
    company_id: int,
    user_id: int,
    effective_to: date,
    db: AsyncConnection,
) -> DriverPayRuleSummary:
    """
    Close a rule by setting Status='Ended' and EffectiveTo.

    Guards:
      - Rule must be Active (cannot end an already-Ended or Voided rule).
      - effective_to must be >= rule.effective_from.
      - effective_to must not precede the latest finalized period's start_date
        that falls within the rule's current effective range (that would make
        historical final lines inconsistent).
    """
    rule = await get_driver_pay_rule_by_id(rule_id, company_id, user_id, db)

    if rule.status != "Active":
        raise HTTPException(
            status_code=422,
            detail=f"Only Active rules can be ended (current status: '{rule.status}').",
        )

    await _check_any_permission(
        company_id, user_id, rule.branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    if effective_to < rule.effective_from:
        raise HTTPException(
            status_code=422,
            detail=f"effective_to ({effective_to}) cannot be before effective_from ({rule.effective_from}).",
        )

    # Guard: cannot close the rule before the latest finalized period that relied on it.
    latest = await _latest_finalized_period_start_in_range(
        rule.driver_id, company_id, rule.effective_from, rule.effective_to, db
    )
    if latest is not None and effective_to < latest:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot end this rule on {effective_to}: a finalized period with "
                f"start date {latest} already relied on it. "
                f"The closure date must be on or after {latest}."
            ),
        )

    await db.execute(
        text("""
            UPDATE payroll.driverpayrules
            SET    status        = 'Ended',
                   effectiveto   = :eff_to,
                   updatedbyuserid = :uid,
                   updatedatutc  = NOW()
            WHERE  driverpayruleid = :rid
              AND  companyid       = :cid
        """),
        {"eff_to": effective_to, "uid": user_id, "rid": rule_id, "cid": company_id},
    )

    await _write_pay_rule_audit(
        db,
        company_id=company_id,
        branch_id=rule.branch_id,
        user_id=user_id,
        rule_id=rule_id,
        action_code="DRIVER_PAY_RULE_ENDED",
        old_value={"status": "Active", "effective_to": str(rule.effective_to) if rule.effective_to else None},
        new_value={"status": "Ended", "effective_to": str(effective_to)},
    )

    return await _get_rule_by_id_internal(rule_id, company_id, db)


# ---------------------------------------------------------------------------
# Update notes (notes-only PATCH)
# ---------------------------------------------------------------------------

async def update_driver_pay_rule_notes(
    rule_id: int,
    company_id: int,
    user_id: int,
    notes: "str | None",
    db: AsyncConnection,
) -> DriverPayRuleSummary:
    """
    Update notes on a pay rule. Notes-only — amount/dates cannot be changed in-place.
    Rule must not be Voided.
    """
    rule = await get_driver_pay_rule_by_id(rule_id, company_id, user_id, db)

    if rule.status == "Voided":
        raise HTTPException(status_code=422, detail="Cannot update a Voided rule.")

    await _check_any_permission(
        company_id, user_id, rule.branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    await db.execute(
        text("""
            UPDATE payroll.driverpayrules
            SET    notes           = :notes,
                   updatedbyuserid = :uid,
                   updatedatutc    = NOW()
            WHERE  driverpayruleid = :rid
              AND  companyid       = :cid
        """),
        {"notes": notes, "uid": user_id, "rid": rule_id, "cid": company_id},
    )

    await _write_pay_rule_audit(
        db,
        company_id=company_id,
        branch_id=rule.branch_id,
        user_id=user_id,
        rule_id=rule_id,
        action_code="DRIVER_PAY_RULE_NOTES_UPDATED",
        old_value={"notes": rule.notes},
        new_value={"notes": notes},
    )

    return await _get_rule_by_id_internal(rule_id, company_id, db)


# ---------------------------------------------------------------------------
# Void driver pay rule
# ---------------------------------------------------------------------------

async def void_driver_pay_rule(
    rule_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> DriverPayRuleSummary:
    """
    Void a rule that was created by mistake.

    Guards:
      - Rule must be Active or Ended (not already Voided).
      - No finalized period must have been governed by this rule.
        (Any Locked period for this driver whose start_date falls within the
        rule's effective range counts as 'governed'.)
    """
    rule = await get_driver_pay_rule_by_id(rule_id, company_id, user_id, db)

    if rule.status == "Voided":
        raise HTTPException(status_code=422, detail="Rule is already Voided.")

    await _check_any_permission(
        company_id, user_id, rule.branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    governed = await _count_finalized_periods_in_rule_range(
        rule.driver_id, company_id, rule.effective_from, rule.effective_to, db
    )
    if governed > 0:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot void this rule: {governed} finalized payroll period(s) were governed by it. "
                "Voiding is only allowed when no finalized period has relied on the rule. "
                "Use 'end' to close the rule going forward while preserving history."
            ),
        )

    await db.execute(
        text("""
            UPDATE payroll.driverpayrules
            SET    status           = 'Voided',
                   updatedbyuserid  = :uid,
                   updatedatutc     = NOW()
            WHERE  driverpayruleid  = :rid
              AND  companyid        = :cid
        """),
        {"uid": user_id, "rid": rule_id, "cid": company_id},
    )

    await _write_pay_rule_audit(
        db,
        company_id=company_id,
        branch_id=rule.branch_id,
        user_id=user_id,
        rule_id=rule_id,
        action_code="DRIVER_PAY_RULE_VOIDED",
        old_value={"status": rule.status},
        new_value={"status": "Voided"},
    )

    return await _get_rule_by_id_internal(rule_id, company_id, db)


# ===========================================================================
# Driver Rate Matrix (Phase 1)
# ===========================================================================

async def get_driver_rate_matrix(
    driver_id: int,
    company_id: int,
    user_id: int,
    as_of_date: date,
    db: AsyncConnection,
) -> DriverRateMatrix:
    """
    Return the full rate matrix for a driver as-of a given date.

    Steps:
      1. Get driver + branch info.
      2. Get active pay items for the branch that RequiresRate=TRUE.
      3. For each pay item / rate type combination, look up the current
         Approved rate and any PendingApproval rate.
    """
    # Step 1 — driver + branch
    drv_result = await db.execute(
        text("""
            SELECT d.driverid, d.drivercode, d.driverstatus,
                   e.fullname AS drivername,
                   d.branchid,
                   b.branchname
            FROM   core.drivers   d
            JOIN   core.employees e ON e.employeeid = d.employeeid
            JOIN   core.branches  b ON b.branchid   = d.branchid
            WHERE  d.driverid  = :did
              AND  d.companyid = :cid
        """),
        {"did": driver_id, "cid": company_id},
    )
    drv_row = drv_result.mappings().first()
    if drv_row is None:
        raise HTTPException(status_code=404, detail="Driver not found.")

    branch_id: int = drv_row["branchid"]

    # Permission gate — requires payrates.view, payrates.edit, settings.manage, or setup.manage
    await _check_any_permission(
        company_id=company_id,
        user_id=user_id,
        branch_id=branch_id,
        permission_codes=["payrates.view", "payrates.edit", "settings.manage", "setup.manage"],
        db=db,
    )

    # OwnDriverDataOnly scope — caller may only view their own driver's matrix.
    # Uses the shared fail-closed helper (_check_own_driver_only → _get_oda_own_driver_id)
    # which rejects ambiguous overlapping active assignments rather than trusting latest row.
    await _check_own_driver_only(company_id, user_id, driver_id, db)

    # Step 2 — active pay items for this branch.
    # Uses LEFT JOIN on BranchPayItemConfig so that system items with
    # IsDefaultBranchActive=TRUE appear even when no explicit config row exists for
    # this branch.  Explicit config rows (IsActive=FALSE) override the default.
    items_result = await db.execute(
        text("""
            SELECT pi.payitemid,
                   pi.payitemname,
                   pi.itemscope,
                   pi.ratebehavior,
                   rt.ratetypeid,
                   rt.ratecode,
                   rt.ratename,
                   rt.unitname,
                   bpic.effectivefrom AS pay_item_effective_from
            FROM   payroll.payitems pi
            JOIN   payroll.payitemratetypemap pirm
                   ON pirm.payitemid = pi.payitemid AND pirm.status = 'Active'
            JOIN   payroll.ratetypes rt
                   ON rt.ratetypeid = pirm.ratetypeid AND rt.isactive = TRUE
            LEFT JOIN payroll.branchpayitemconfig bpic
                   ON bpic.payitemid   = pi.payitemid
                  AND bpic.companyid   = :company_id
                  AND bpic.branchid    = :branch_id
                  AND (bpic.effectiveto IS NULL OR bpic.effectiveto >= :as_of)
            WHERE  pi.status       != 'Retired'
              AND  pi.requiresrate  = TRUE
              AND  (pi.companyid IS NULL OR pi.companyid = :company_id)
              AND  COALESCE(bpic.isactive, pi.isdefaultbranchactive) = TRUE
              -- Phase 4C: structural RateType ownership via RateTypes.CompanyID.
              -- Only show system types (companyid IS NULL) or own-company types.
              AND (rt.companyid IS NULL OR rt.companyid = :company_id)
            ORDER BY pi.sortorder, pi.payitemname
        """),
        {"company_id": company_id, "branch_id": branch_id, "as_of": as_of_date},
    )
    items = items_result.mappings().all()

    groups: list[RateMatrixGroup] = []
    for row in items:
        rate_type_id: int = row["ratetypeid"]

        # Step 3a — current approved/superseded rate (Fix 6: include Superseded for historical as_of)
        approved_result = await db.execute(
            text("""
                SELECT driverrateid, amount, effectivefrom, effectiveto, status
                FROM   payroll.driverrates
                WHERE  driverid       = :did
                  AND  companyid      = :cid
                  AND  ratetypeid     = :rtid
                  AND  status         IN ('Approved', 'Superseded')
                  AND  effectivefrom <= :as_of
                  AND  (effectiveto IS NULL OR effectiveto >= :as_of)
                ORDER BY effectivefrom DESC
                LIMIT 1
            """),
            {"did": driver_id, "cid": company_id, "rtid": rate_type_id, "as_of": as_of_date},
        )
        approved_row = approved_result.mappings().first()

        # Step 3b — pending rate
        pending_result = await db.execute(
            text("""
                SELECT driverrateid, amount, effectivefrom, effectiveto, status
                FROM   payroll.driverrates
                WHERE  driverid   = :did
                  AND  companyid  = :cid
                  AND  ratetypeid = :rtid
                  AND  status     = 'PendingApproval'
                ORDER BY effectivefrom DESC
                LIMIT 1
            """),
            {"did": driver_id, "cid": company_id, "rtid": rate_type_id},
        )
        pending_row = pending_result.mappings().first()

        current_rate = (
            RateMatrixCurrentRate(
                driver_rate_id=approved_row["driverrateid"],
                amount=Decimal(str(approved_row["amount"])),
                effective_from=approved_row["effectivefrom"],
                effective_to=approved_row["effectiveto"],
                status=approved_row["status"],
            )
            if approved_row else None
        )
        pending_rate = (
            RateMatrixCurrentRate(
                driver_rate_id=pending_row["driverrateid"],
                amount=Decimal(str(pending_row["amount"])),
                effective_from=pending_row["effectivefrom"],
                effective_to=pending_row["effectiveto"],
                status=pending_row["status"],
            )
            if pending_row else None
        )

        pay_item_id: int = row["payitemid"]
        groups.append(
            RateMatrixGroup(
                group_key=f"{pay_item_id}:{rate_type_id}",
                rate_source="PayItem",
                pay_item_id=pay_item_id,
                pay_item_name=row["payitemname"],
                item_scope=row["itemscope"],
                rate_behavior=row["ratebehavior"],
                status_rate_column_id=None,
                rate_type_id=rate_type_id,
                rate_code=row["ratecode"],
                rate_name=row["ratename"],
                unit_name=row["unitname"],
                current_rate=current_rate,
                pending_rate=pending_rate,
                is_required=True,
                is_missing=(current_rate is None),
                pay_item_effective_from=row["pay_item_effective_from"],
            )
        )

    # Step 4 — StatusRateColumn groups for the branch.
    # Each active StatusRateColumn produces a separate rate group so the
    # driver can have a STATUS_PAY (or custom SRC_) rate set here.
    src_result = await db.execute(
        text("""
            SELECT src.statusratecolumnid, src.columnname,
                   src.ratetypeid, rt.ratecode, rt.ratename, rt.unitname
            FROM   payroll.statusratecolumns src
            JOIN   payroll.ratetypes rt ON rt.ratetypeid = src.ratetypeid
            WHERE  src.branchid  = :bid
              AND  src.companyid = :cid
              AND  src.isactive  = TRUE
            ORDER BY src.isdefault DESC, src.statusratecolumnid
        """),
        {"bid": branch_id, "cid": company_id},
    )
    for src_row in src_result.mappings().all():
        src_rate_type_id: int = src_row["ratetypeid"]
        src_col_id: int       = src_row["statusratecolumnid"]

        approved_result2 = await db.execute(
            text("""
                SELECT driverrateid, amount, effectivefrom, effectiveto, status
                FROM   payroll.driverrates
                WHERE  driverid       = :did
                  AND  companyid      = :cid
                  AND  ratetypeid     = :rtid
                  AND  status         IN ('Approved', 'Superseded')
                  AND  effectivefrom <= :as_of
                  AND  (effectiveto IS NULL OR effectiveto >= :as_of)
                ORDER BY effectivefrom DESC
                LIMIT 1
            """),
            {"did": driver_id, "cid": company_id, "rtid": src_rate_type_id, "as_of": as_of_date},
        )
        src_approved = approved_result2.mappings().first()

        pending_result2 = await db.execute(
            text("""
                SELECT driverrateid, amount, effectivefrom, effectiveto, status
                FROM   payroll.driverrates
                WHERE  driverid   = :did
                  AND  companyid  = :cid
                  AND  ratetypeid = :rtid
                  AND  status     = 'PendingApproval'
                ORDER BY effectivefrom DESC
                LIMIT 1
            """),
            {"did": driver_id, "cid": company_id, "rtid": src_rate_type_id},
        )
        src_pending = pending_result2.mappings().first()

        src_current = (
            RateMatrixCurrentRate(
                driver_rate_id=src_approved["driverrateid"],
                amount=Decimal(str(src_approved["amount"])),
                effective_from=src_approved["effectivefrom"],
                effective_to=src_approved["effectiveto"],
                status=src_approved["status"],
            )
            if src_approved else None
        )
        src_pending_rate = (
            RateMatrixCurrentRate(
                driver_rate_id=src_pending["driverrateid"],
                amount=Decimal(str(src_pending["amount"])),
                effective_from=src_pending["effectivefrom"],
                effective_to=src_pending["effectiveto"],
                status=src_pending["status"],
            )
            if src_pending else None
        )

        groups.append(
            RateMatrixGroup(
                group_key=f"SRC:{src_col_id}:{src_rate_type_id}",
                rate_source="StatusRateColumn",
                pay_item_id=None,
                pay_item_name=None,
                item_scope="Daily",
                rate_behavior="PerUnit",
                status_rate_column_id=src_col_id,
                rate_type_id=src_rate_type_id,
                rate_code=src_row["ratecode"],
                rate_name=src_row["columnname"],
                unit_name=src_row["unitname"],
                current_rate=src_current,
                pending_rate=src_pending_rate,
                is_required=True,
                is_missing=(src_current is None),
                pay_item_effective_from=None,
            )
        )

    return DriverRateMatrix(
        driver_id=driver_id,
        driver_name=drv_row["drivername"],
        driver_code=drv_row["drivercode"],
        branch_id=branch_id,
        branch_name=drv_row["branchname"],
        as_of=as_of_date,
        groups=groups,
    )


# ===========================================================================
# Batch rate save (Phase 2A)
# ===========================================================================

async def batch_save_rates(
    driver_id: int,
    company_id: int,
    user_id: int,
    data: BatchRateRequest,
    db: AsyncConnection,
) -> BatchRateSaveResult:
    """
    Atomically create or update pending rates for a driver and optionally
    auto-approve them when AllowSelfApproval is True.

    All validation runs before any writes so the operation is all-or-nothing
    within the caller's transaction.

    Phase 2A: only PerUnit, EnteredAmount, and Fixed rate behaviors are
    supported.  Tiered (OrdinalTier, RangeBracket, RangeProgressive) and
    Block rates must be saved via individual endpoints.
    """
    # Step 1 — validate changes list: empty and duplicate rate_type_id entries.
    # Empty list is caught by the schema validator; double-check here.
    if not data.changes:
        raise HTTPException(status_code=422, detail="changes must not be empty.")

    seen_rt_ids: set[int] = set()
    for change in data.changes:
        if change.rate_type_id in seen_rt_ids:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Duplicate rate_type_id={change.rate_type_id} in the same batch request."
                ),
            )
        seen_rt_ids.add(change.rate_type_id)

    # Step 2 — driver lookup + branch check
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    drv_result = await db.execute(
        text("""
            SELECT driverid, branchid FROM core.drivers
            WHERE driverid = :did AND companyid = :cid
        """),
        {"did": driver_id, "cid": company_id},
    )
    drv_row = drv_result.mappings().first()
    if drv_row is None:
        raise HTTPException(status_code=404, detail="Driver not found.")

    driver_branch_id: int = drv_row["branchid"]
    if not can_see_all and driver_branch_id not in branch_ids:
        raise HTTPException(status_code=403, detail="Access denied to this driver's branch.")

    # Step 3 — permission gate
    await _check_any_permission(
        company_id, user_id, driver_branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    # Step 3.5 — OwnDriverDataOnly: caller may only batch-save their own driver's rates
    await _check_own_driver_only(company_id, user_id, driver_id, db)

    # Step 4 — read AllowSelfApproval from company settings
    settings_result = await db.execute(
        text("SELECT allowselfapproval FROM core.companies WHERE companyid = :cid"),
        {"cid": company_id},
    )
    settings_row = settings_result.mappings().first()
    allow_self_approval: bool = bool(settings_row["allowselfapproval"]) if settings_row else True

    # Step 5 — finalized-period guard (check once for the shared effective_from)
    # Only relevant when we will auto-approve.  PendingApproval rows created when
    # allow_self_approval=False can be approved later — the guard runs at that point.
    if allow_self_approval:
        await _check_not_in_finalized_period(
            company_id, driver_branch_id, data.effective_from, db
        )

    # Step 6 — validate each change.
    #
    # All validation runs before any writes (all-or-nothing).
    #
    # PayItem path:  validates PayItemRateTypeMap + branch-active + behavior.
    # StatusRateColumn path: validates StatusRateColumns membership + rate_type_id match.
    for change in data.changes:
        if change.status_rate_column_id is not None:
            # ---- StatusRateColumn validation path ----
            src_result = await db.execute(
                text("""
                    SELECT src.ratetypeid
                    FROM   payroll.statusratecolumns src
                    WHERE  src.statusratecolumnid = :src_id
                      AND  src.branchid           = :bid
                      AND  src.companyid          = :cid
                      AND  src.isactive           = TRUE
                """),
                {
                    "src_id": change.status_rate_column_id,
                    "bid":    driver_branch_id,
                    "cid":    company_id,
                },
            )
            src_row = src_result.mappings().first()
            if src_row is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"status_rate_column_id={change.status_rate_column_id} not found "
                        "or not active for this driver's branch."
                    ),
                )
            if src_row["ratetypeid"] != change.rate_type_id:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"rate_type_id={change.rate_type_id} does not match the backing RateType "
                        f"for status_rate_column_id={change.status_rate_column_id}."
                    ),
                )
            # StatusRateColumn RateTypes are company-owned or system — ownership already
            # enforced by trg_src_ratetype_owner trigger; skip redundant check here.
        else:
            # ---- PayItem validation path ----
            # Exact pay_item_id + rate_type_id mapping check
            map_result = await db.execute(
                text("""
                    SELECT
                        pi.payitemid,
                        pi.ratebehavior,
                        pi.isdefaultbranchactive,
                        bpic.isactive AS cfg_isactive
                    FROM payroll.payitemratetypemap pirm
                    JOIN payroll.payitems  pi ON pi.payitemid    = pirm.payitemid
                    JOIN payroll.ratetypes rt ON rt.ratetypeid   = pirm.ratetypeid
                                             AND rt.isactive      = TRUE
                    LEFT JOIN payroll.branchpayitemconfig bpic
                           ON bpic.payitemid  = pi.payitemid
                          AND bpic.companyid  = :cid
                          AND bpic.branchid   = :bid
                          AND (bpic.effectiveto IS NULL OR bpic.effectiveto >= CAST(:effective_from AS date))
                    WHERE pirm.payitemid  = :piid
                      AND pirm.ratetypeid = :rtid
                      AND pirm.status     = 'Active'
                      AND pi.status       != 'Retired'
                      AND pi.requiresrate = TRUE
                      AND (pi.companyid IS NULL OR pi.companyid = :cid)
                    ORDER BY bpic.effectivefrom DESC NULLS LAST
                    LIMIT 1
                """),
                {
                    "piid":           change.pay_item_id,
                    "rtid":           change.rate_type_id,
                    "cid":            company_id,
                    "bid":            driver_branch_id,
                    "effective_from": data.effective_from,
                },
            )
            map_row = map_result.mappings().first()

            if map_row is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"pay_item_id={change.pay_item_id} is not actively mapped to "
                        f"rate_type_id={change.rate_type_id} for this company. "
                        "Verify the PayItem → RateType mapping is Active."
                    ),
                )

            # Phase 4B.3 — defense-in-depth: validate RateType ownership
            await _assert_rate_type_allowed_for_company(db, company_id, change.rate_type_id)

            cfg = map_row["cfg_isactive"]
            is_branch_active = bool(cfg) if cfg is not None else bool(map_row["isdefaultbranchactive"])
            if not is_branch_active:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"pay_item_id={change.pay_item_id} is not active for this driver's branch "
                        f"as of {data.effective_from}."
                    ),
                )

            rate_behavior: str = map_row["ratebehavior"] or "PerUnit"
            if rate_behavior in _TIERED_BEHAVIORS or rate_behavior == "Block":
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"pay_item_id={change.pay_item_id} uses '{rate_behavior}' behavior "
                        "which requires tier or block configuration. "
                        "Use the individual rate endpoint to save this rate."
                    ),
                )

    # Step 7 — write: create or update PendingApproval rows
    # All validation passed — now write inside the same transaction.
    created_ids: list[int] = []
    updated_pending_ids: list[int] = []

    batch_note = data.notes  # batch-level note

    for change in data.changes:
        # Check for an existing PendingApproval rate for this driver + rate type
        existing = await db.execute(
            text("""
                SELECT driverrateid FROM payroll.driverrates
                WHERE  driverid   = :did
                  AND  ratetypeid = :rtid
                  AND  companyid  = :cid
                  AND  status     = 'PendingApproval'
                ORDER  BY createdatutc DESC
                LIMIT  1
            """),
            {"did": driver_id, "rtid": change.rate_type_id, "cid": company_id},
        )
        existing_row = existing.mappings().first()

        if existing_row is not None:
            # Update the existing pending row.
            # effectiveto is reset to NULL to clear any stale bounded end date
            # that may have been set by the individual rate endpoint or a prior
            # batch edit — the batch always creates open-ended rates.
            #
            # Phase 12C — status-predicated UPDATE.
            # The SELECT above found a PendingApproval row, but a concurrent
            # approve_rate can approve it between that SELECT and this UPDATE.
            # Without a status predicate the UPDATE would silently mutate an
            # Approved rate's amount/date through a stale pending path.
            # AND status='PendingApproval' + RETURNING makes this atomic: if
            # the row was concurrently approved, 0 rows are returned → 409.
            existing_rate_id: int = existing_row["driverrateid"]
            batch_upd = await db.execute(
                text("""
                    UPDATE payroll.driverrates
                    SET    amount        = :amount,
                           effectivefrom = :effective_from,
                           effectiveto   = NULL,
                           notes         = :notes
                    WHERE  driverrateid  = :rid
                      AND  companyid     = :cid
                      AND  status        = 'PendingApproval'
                    RETURNING driverrateid
                """),
                {
                    "amount":         change.amount,
                    "effective_from": data.effective_from,
                    "notes":          change.notes or batch_note,
                    "rid":            existing_rate_id,
                    "cid":            company_id,
                },
            )
            if batch_upd.scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Rate (id={existing_rate_id}) was concurrently approved or "
                        "changed. Refresh and try again."
                    ),
                )
            await _write_rate_audit(
                db,
                company_id=company_id,
                branch_id=driver_branch_id,
                user_id=user_id,
                rate_id=existing_rate_id,
                action_code="RATE_UPDATED",
                old_value=None,
                new_value={
                    "amount":         str(change.amount),
                    "effective_from": str(data.effective_from),
                    "source":         "batch_save",
                },
            )
            updated_pending_ids.append(existing_rate_id)
        else:
            # Create a new PendingApproval row
            ins = await db.execute(
                text("""
                    INSERT INTO payroll.driverrates
                        (companyid, branchid, driverid, ratetypeid, amount,
                         effectivefrom, effectiveto, status, createdbyuserid, notes)
                    VALUES
                        (:cid, :bid, :did, :rtid, :amount,
                         :effective_from, NULL, 'PendingApproval', :uid, :notes)
                    RETURNING driverrateid
                """),
                {
                    "cid":            company_id,
                    "bid":            driver_branch_id,
                    "did":            driver_id,
                    "rtid":           change.rate_type_id,
                    "amount":         change.amount,
                    "effective_from": data.effective_from,
                    "uid":            user_id,
                    "notes":          change.notes or batch_note,
                },
            )
            new_rate_id: int = ins.scalar_one()
            await _write_rate_audit(
                db,
                company_id=company_id,
                branch_id=driver_branch_id,
                user_id=user_id,
                rate_id=new_rate_id,
                action_code="RATE_CREATED",
                new_value={
                    "driver_id":      driver_id,
                    "rate_type_id":   change.rate_type_id,
                    "amount":         str(change.amount),
                    "effective_from": str(data.effective_from),
                    "status":         "PendingApproval",
                    "source":         "batch_save",
                },
            )
            created_ids.append(new_rate_id)

    all_rate_ids = created_ids + updated_pending_ids

    # Step 8 — auto-approve if allowed
    approved_count = 0
    pending_count = 0

    if allow_self_approval:
        for rate_id in all_rate_ids:
            # Call approve_rate — it re-checks permissions (safe, same user),
            # handles the supersede logic, and writes the approval audit.
            await approve_rate(rate_id, company_id, user_id, db)
        approved_count = len(all_rate_ids)
    else:
        pending_count = len(all_rate_ids)

    # Step 9 — fetch final state of all affected rates
    rate_summaries = [await _get_rate_with_tiers(rid, company_id, db) for rid in all_rate_ids]

    return BatchRateSaveResult(
        driver_id=driver_id,
        effective_from=data.effective_from,
        allow_self_approval=allow_self_approval,
        created_count=len(created_ids),
        updated_pending_count=len(updated_pending_ids),
        approved_count=approved_count,
        pending_count=pending_count,
        rates=rate_summaries,
    )


# ===========================================================================
# Phase 2B — driver rate summary / pending / history
# ===========================================================================

async def _check_driver_read_access(
    driver_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> int:
    """
    Shared security gate for all driver-scoped rate read endpoints
    (pending, history, summary).

    Performs:
      1. Driver lookup — 404 if not in this company.
      2. Permission check with driver's branch_id — 403 if no payrates.* permission.
      3. OwnDriverDataOnly scope check — 403 if ODA mismatch or ambiguous assignments.

    Returns the driver's branch_id on success.

    Note: explicit _check_branch_access is not called separately because
    _check_any_permission with the driver's actual branch_id is sufficient
    to enforce branch scope (fn_UserHasPermission returns FALSE for
    SpecificBranch users on a different branch).
    """
    drv_result = await db.execute(
        text("SELECT branchid FROM core.drivers WHERE driverid = :did AND companyid = :cid"),
        {"did": driver_id, "cid": company_id},
    )
    drv_row = drv_result.mappings().first()
    if drv_row is None:
        raise HTTPException(status_code=404, detail="Driver not found.")
    branch_id: int = drv_row["branchid"]
    await _check_any_permission(
        company_id, user_id, branch_id,
        ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
    )
    await _check_own_driver_only(company_id, user_id, driver_id, db)
    return branch_id


async def get_driver_rates_summary(
    driver_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> DriverRatesSummary:
    """
    Return quick status counts for a driver:
    - pending_count: PendingApproval rates
    - future_approved_count: Approved rates with effective_from > today
    - missing_required_count: required matrix slots with no current Approved/Superseded rate

    missing_required_count uses the same INNER JOIN logic as get_driver_rate_matrix
    (BranchPayItemConfig INNER JOIN) so items with only IsDefaultBranchActive=TRUE
    and no explicit config row are NOT counted as required.
    """
    branch_id = await _check_driver_read_access(driver_id, company_id, user_id, db)

    # Counts from driverrates
    counts_result = await db.execute(
        text("""
            SELECT
                SUM(CASE WHEN status = 'PendingApproval' THEN 1 ELSE 0 END)
                    AS pending_count,
                SUM(CASE WHEN status = 'Approved'
                          AND effectivefrom > CURRENT_DATE THEN 1 ELSE 0 END)
                    AS future_approved_count
            FROM payroll.driverrates
            WHERE driverid  = :did
              AND companyid = :cid
              AND status IN ('PendingApproval', 'Approved')
        """),
        {"did": driver_id, "cid": company_id},
    )
    counts_row = counts_result.mappings().first()
    pending_count: int       = int(counts_row["pending_count"] or 0)
    future_approved_count: int = int(counts_row["future_approved_count"] or 0)

    # Missing required count — mirrors matrix logic exactly (LEFT JOIN + COALESCE
    # fallback so that system items with IsDefaultBranchActive=TRUE are counted
    # even when no explicit BranchPayItemConfig row exists for this branch).
    missing_result = await db.execute(
        text("""
            SELECT
                COUNT(*) AS total_required,
                COUNT(approved.driverrateid) AS with_current_rate
            FROM payroll.payitems pi
            JOIN payroll.payitemratetypemap pirm
                 ON pirm.payitemid = pi.payitemid AND pirm.status = 'Active'
            JOIN payroll.ratetypes rt
                 ON rt.ratetypeid = pirm.ratetypeid AND rt.isactive = TRUE
            LEFT JOIN payroll.branchpayitemconfig bpic
                 ON bpic.payitemid    = pi.payitemid
                AND bpic.companyid   = :cid
                AND bpic.branchid    = :bid
                AND (bpic.effectiveto IS NULL OR bpic.effectiveto >= CURRENT_DATE)
                AND bpic.effectivefrom <= CURRENT_DATE
            LEFT JOIN LATERAL (
                SELECT driverrateid FROM payroll.driverrates
                WHERE  driverid      = :did
                  AND  companyid     = :cid
                  AND  ratetypeid    = pirm.ratetypeid
                  AND  status        IN ('Approved', 'Superseded')
                  AND  effectivefrom <= CURRENT_DATE
                  AND  (effectiveto IS NULL OR effectiveto >= CURRENT_DATE)
                ORDER BY effectivefrom DESC
                LIMIT 1
            ) approved ON TRUE
            WHERE pi.status      != 'Retired'
              AND pi.requiresrate = TRUE
              AND (pi.companyid IS NULL OR pi.companyid = :cid)
              AND COALESCE(bpic.isactive, pi.isdefaultbranchactive) = TRUE
        """),
        {"did": driver_id, "cid": company_id, "bid": branch_id},
    )
    missing_row = missing_result.mappings().first()
    if missing_row is not None:
        total = int(missing_row["total_required"] or 0)
        with_rate = int(missing_row["with_current_rate"] or 0)
        missing_required_count: int | None = max(0, total - with_rate)
    else:
        missing_required_count = None

    return DriverRatesSummary(
        driver_id=driver_id,
        pending_count=pending_count,
        future_approved_count=future_approved_count,
        missing_required_count=missing_required_count,
    )


async def get_driver_rates_pending(
    driver_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> list[DriverRateSummary]:
    """
    Return all PendingApproval rates for a driver, newest first.

    Security: same as get_driver_rates_summary (_check_driver_read_access).
    Returns 404 if the driver doesn't exist in this company.
    """
    await _check_driver_read_access(driver_id, company_id, user_id, db)

    result = await db.execute(
        text(
            f"{_RATE_SELECT} "
            "WHERE dr.driverid  = :did "
            "  AND dr.companyid = :cid "
            "  AND dr.status    = 'PendingApproval' "
            "ORDER BY dr.createdatutc DESC"
        ),
        {"did": driver_id, "cid": company_id},
    )
    return [_rate_row_to_summary(r) for r in result.mappings().all()]


async def get_driver_rates_history(
    driver_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    limit: int = 200,
    offset: int = 0,
) -> list[DriverRateSummary]:
    """
    Return the full rate history for a driver across all statuses:
    Approved, PendingApproval, Superseded, Voided.

    Sorted newest-first: effective_from DESC, created_at_utc DESC.
    Defaults to 200 rows; callers may paginate via limit/offset.

    Security: same as get_driver_rates_summary (_check_driver_read_access).
    Returns 404 if the driver doesn't exist in this company.
    """
    await _check_driver_read_access(driver_id, company_id, user_id, db)

    result = await db.execute(
        text(
            f"{_RATE_SELECT} "
            "WHERE dr.driverid  = :did "
            "  AND dr.companyid = :cid "
            "ORDER BY dr.effectivefrom DESC, dr.createdatutc DESC "
            "LIMIT :limit OFFSET :offset"
        ),
        {"did": driver_id, "cid": company_id, "limit": limit, "offset": offset},
    )
    return [_rate_row_to_summary(r) for r in result.mappings().all()]


# ---------------------------------------------------------------------------
# Bulk driver rates summary (Phase 2C — D)
# ---------------------------------------------------------------------------

async def get_bulk_driver_rates_summary(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    branch_id: int | None = None,
) -> list[DriverRatesSummary]:
    """
    Return DriverRatesSummary for every driver the caller can access.

    Security:
      - ODA users: only own driver.
      - SpecificBranch users: only drivers in their assigned branch(es).
      - AllCompanyBranches users: all company drivers (filtered by branch_id param).

    This is designed for the left-panel badge overlay — it must NOT leak driver
    existence across companies.

    missing_required_count is NOT computed here (it would require N LATERAL subqueries).
    It is always None in bulk responses; callers may use the single-driver summary
    endpoint for the full count on a selected driver.
    """
    # ODA: only own driver
    own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)
    if own_driver_id is not None:
        accessible_driver_ids = [own_driver_id]
    else:
        await _check_any_permission(
            company_id, user_id, branch_id,
            ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
        )
        can_see_all, allowed_branch_ids = await _check_branch_access(company_id, user_id, db)
        drv_q_parts = ["companyid = :cid"]
        drv_params: dict = {"cid": company_id}
        if not can_see_all:
            if not allowed_branch_ids:
                return []
            in_clause, extra = _build_in_clause(allowed_branch_ids, "bid")
            drv_q_parts.append(f"branchid IN ({in_clause})")
            drv_params.update(extra)
        elif branch_id is not None:
            drv_q_parts.append("branchid = :filter_bid")
            drv_params["filter_bid"] = branch_id
        where = " AND ".join(drv_q_parts)
        drv_result = await db.execute(
            text(f"SELECT driverid FROM core.drivers WHERE {where}"),
            drv_params,
        )
        accessible_driver_ids = [r["driverid"] for r in drv_result.mappings().all()]

    if not accessible_driver_ids:
        return []

    in_clause2, params2 = _build_in_clause(accessible_driver_ids, "did")

    counts_result = await db.execute(
        text(f"""
            SELECT
                driverid,
                SUM(CASE WHEN status = 'PendingApproval' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN status = 'Approved'
                          AND effectivefrom > CURRENT_DATE THEN 1 ELSE 0 END) AS future_approved_count
            FROM payroll.driverrates
            WHERE companyid = :cid
              AND driverid IN ({in_clause2})
              AND status IN ('PendingApproval', 'Approved')
            GROUP BY driverid
        """),
        {"cid": company_id, **params2},
    )
    counts_map: dict[int, dict] = {
        r["driverid"]: {
            "pending_count": int(r["pending_count"] or 0),
            "future_approved_count": int(r["future_approved_count"] or 0),
        }
        for r in counts_result.mappings().all()
    }

    return [
        DriverRatesSummary(
            driver_id=did,
            pending_count=counts_map.get(did, {}).get("pending_count", 0),
            future_approved_count=counts_map.get(did, {}).get("future_approved_count", 0),
            missing_required_count=None,
        )
        for did in accessible_driver_ids
    ]


# ---------------------------------------------------------------------------
# Copy Rates From Driver (Phase 2C — C)
# ---------------------------------------------------------------------------

async def copy_driver_rates(
    target_driver_id: int,
    source_driver_id: int,
    company_id: int,
    user_id: int,
    data: CopyRatesRequest,
    db: AsyncConnection,
) -> CopyRatesResult:
    """
    Copy current Approved rates from source_driver to target_driver.

    Rules:
    - Caller must have payrates.edit on BOTH source and target driver branches.
    - ODA users cannot use this endpoint.
    - Same company only (enforced by driver lookup).
    - Only current Approved rates as-of data.effective_from are copied.
    - PendingApproval rates are never copied.
    - Atomic — validated before any writes.
    - Backdating guard applies (same as batch save).
    - AllowSelfApproval controls whether copied rates auto-approve or become
      PendingApproval.
    - include_pay_rules=True copies Active MinimumPay/MaximumPay rules from source;
      rejected with 422 if target already has an overlapping rule of same type.
    - Rates with tiered/block structure: only flat amount is copied; tier details
      are not copied (this is safe — the new rate row is a simple Flat rate).
    """
    # ODA users must not use copy-from
    own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)
    if own_driver_id is not None:
        raise HTTPException(
            status_code=403,
            detail="OwnDriverDataOnly users cannot use copy-rates-from.",
        )

    # Verify both drivers exist in this company
    src_result = await db.execute(
        text("SELECT branchid FROM core.drivers WHERE driverid = :did AND companyid = :cid"),
        {"did": source_driver_id, "cid": company_id},
    )
    src_row = src_result.mappings().first()
    if src_row is None:
        raise HTTPException(status_code=404, detail="Source driver not found in this company.")
    source_branch_id: int = src_row["branchid"]

    tgt_result = await db.execute(
        text("SELECT branchid FROM core.drivers WHERE driverid = :did AND companyid = :cid"),
        {"did": target_driver_id, "cid": company_id},
    )
    tgt_row = tgt_result.mappings().first()
    if tgt_row is None:
        raise HTTPException(status_code=404, detail="Target driver not found in this company.")
    target_branch_id: int = tgt_row["branchid"]

    # Require payrates.edit on both branches
    await _check_any_permission(
        company_id, user_id, source_branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )
    await _check_any_permission(
        company_id, user_id, target_branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    # AllowSelfApproval
    settings_result = await db.execute(
        text("SELECT allowselfapproval FROM core.companies WHERE companyid = :cid"),
        {"cid": company_id},
    )
    settings_row = settings_result.mappings().first()
    allow_self_approval: bool = bool(settings_row["allowselfapproval"]) if settings_row else True

    # Fetch current Approved rates from source as-of effective_from.
    # Also fetch:
    #   - rate_name / rate_code (from ratetypes) for error messages
    #   - has_tiers: TRUE if at least one row exists in driverratetiers for this rate
    #   - has_block: TRUE if blocksizecustomunit IS NOT NULL (Block-behavior rates)
    #
    # Note: ratebehavior is NOT used here because PerUnit/PerHour/etc. pay items
    # produce simple flat-amount rate rows (no tiers). Only the actual presence of
    # tier rows or block metadata on a specific rate row indicates an advanced rate.
    source_rates_result = await db.execute(
        text("""
            SELECT DISTINCT ON (dr.ratetypeid)
                dr.driverrateid,
                dr.ratetypeid,
                dr.amount,
                rt.ratename,
                rt.ratecode,
                (dr.blocksize IS NOT NULL) AS has_block,
                EXISTS (
                    SELECT 1 FROM payroll.driverratetiers dt
                    WHERE dt.driverrateid = dr.driverrateid
                ) AS has_tiers
            FROM payroll.driverrates dr
            JOIN payroll.ratetypes rt ON rt.ratetypeid = dr.ratetypeid
            WHERE dr.driverid      = :src_did
              AND dr.companyid     = :cid
              AND dr.status        = 'Approved'
              AND dr.effectivefrom <= :eff_from
              AND (dr.effectiveto IS NULL OR dr.effectiveto >= :eff_from)
            ORDER BY dr.ratetypeid, dr.effectivefrom DESC
        """),
        {"src_did": source_driver_id, "cid": company_id, "eff_from": data.effective_from},
    )
    source_rates = source_rates_result.mappings().all()

    if not source_rates:
        return CopyRatesResult(
            target_driver_id=target_driver_id,
            source_driver_id=source_driver_id,
            effective_from=data.effective_from,
            allow_self_approval=allow_self_approval,
            rates_copied=0, rates_approved=0, rates_pending=0, pay_rules_copied=0,
        )

    # Fix 3 — Reject advanced/tier/block rates.
    # Copying only the flat amount of a tiered or block rate would silently discard
    # the rate structure that payroll finalization relies on.  Reject the entire
    # copy request if any source rate has actual tier rows (driverratetiers) or
    # block metadata (blocksizecustomunit) on the specific rate row.
    advanced_rate_names = [
        r["ratename"] or r["ratecode"]
        for r in source_rates
        if r["has_tiers"] or r["has_block"]
    ]
    if advanced_rate_names:
        raise HTTPException(
            status_code=422,
            detail=(
                "Advanced/tiered rates cannot be copied: "
                f"{', '.join(advanced_rate_names)}. "
                "Copy individual advanced rates manually after the copy, "
                "or first replace them with flat rates in the source driver."
            ),
        )

    # Fix 2 — All-or-nothing: every source rate must be valid for the target branch.
    # Previously invalid rates were silently skipped. Now any mismatch rejects the
    # entire request so no partial copy occurs.
    rate_type_ids = [r["ratetypeid"] for r in source_rates]
    in_clause, in_params = _build_in_clause(rate_type_ids, "rtid")
    valid_result = await db.execute(
        text(f"""
            SELECT DISTINCT pirm.ratetypeid
            FROM payroll.payitemratetypemap pirm
            JOIN payroll.payitems  pi ON pi.payitemid    = pirm.payitemid
                                     AND pi.status       != 'Retired'
                                     AND pi.requiresrate = TRUE
            JOIN payroll.ratetypes rt ON rt.ratetypeid   = pirm.ratetypeid
                                     AND rt.isactive      = TRUE
            LEFT JOIN payroll.branchpayitemconfig bpic
                                 ON bpic.payitemid    = pi.payitemid
                                AND bpic.companyid    = :cid
                                AND bpic.branchid     = :bid
                                AND bpic.effectivefrom <= :eff_from
                                AND (bpic.effectiveto IS NULL OR bpic.effectiveto >= :eff_from)
            WHERE pirm.status    = 'Active'
              AND pirm.ratetypeid IN ({in_clause})
              AND (pi.companyid IS NULL OR pi.companyid = :cid)
              AND COALESCE(bpic.isactive, pi.isdefaultbranchactive) = TRUE
        """),
        {"cid": company_id, "bid": target_branch_id, "eff_from": data.effective_from, **in_params},
    )
    valid_rate_type_ids = {r["ratetypeid"] for r in valid_result.mappings().all()}

    # All-or-nothing: any invalid rate type for target branch → reject
    invalid_rates = [
        r["ratename"] or r["ratecode"]
        for r in source_rates
        if r["ratetypeid"] not in valid_rate_type_ids
    ]
    if invalid_rates:
        raise HTTPException(
            status_code=422,
            detail=(
                "Cannot copy: the following rate type(s) are not configured for the "
                f"target driver's branch: {', '.join(invalid_rates)}. "
                "Ensure the target branch has these rate types active "
                "in its pay item configuration before copying."
            ),
        )

    # All source rates are valid for target branch
    copyable_rates = list(source_rates)

    # ── Pre-write conflict validation (ALL checks before ANY mutation) ────────
    #
    # For every rate type we are about to copy, verify that the target driver
    # has no Approved rate starting on or after data.effective_from.  Superseding
    # such a rate would set its EffectiveTo BEFORE its own EffectiveFrom (invalid
    # dates), corrupt the effective-date history, and trigger the DB EXCLUDE
    # constraint with an error rather than a clean 422.
    #
    # This mirrors the conflict guard in approve_rate (Step 1.5) and ensures
    # copy_driver_rates and approve_rate enforce the same lifecycle invariants.
    # Phase 12 / 12B — Finalization atomicity advisory lock (copy path).
    # copy_driver_rates with allow_self_approval=True directly writes Approved rates
    # and calls _supersede_current_approved_rates, bypassing approve_rate.  Acquire
    # the same transaction-level advisory lock as finalize_period and approve_rate so
    # that this copy path is also serialised against concurrent finalization.
    #
    # Phase 12B fix: _check_not_in_finalized_period and _check_no_future_approved_conflict
    # are intentionally placed AFTER this lock so their guard decisions reflect the
    # post-lock state of the DB (TOCTOU safety).  Previously these ran before the lock;
    # a concurrent finalize_period could Lock the period while we waited, making the
    # guard decisions stale.
    # The lock is a no-op when allow_self_approval=False (PendingApproval only,
    # no rate-state change that could affect finalization).
    if allow_self_approval:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:cid, :bid)"),
            {"cid": company_id, "bid": target_branch_id},
        )
        # Backdating guard — run under lock for TOCTOU safety.
        await _check_not_in_finalized_period(
            company_id, target_branch_id, data.effective_from, db
        )

    # ── Pre-write conflict validation (ALL checks before ANY mutation) ────────
    # Phase 12B: moved to after the advisory lock so guard decisions are post-lock.
    for src_rate in copyable_rates:
        await _check_no_future_approved_conflict(
            company_id, target_driver_id, src_rate["ratetypeid"],
            data.effective_from, db,
        )
    # ── End pre-write validation ───────────────────────────────────────────────

    rates_approved = 0
    rates_pending = 0
    _copy_note = f"Copied from driver {source_driver_id}"

    for src_rate in copyable_rates:
        rate_type_id = src_rate["ratetypeid"]
        amount = src_rate["amount"]

        # Void existing PendingApproval rows for this rate type (shared helper).
        # Must happen BEFORE supersession so the pending rows are cleared first.
        await _void_pending_rates_for_rate_type(
            company_id, target_driver_id, rate_type_id, user_id, target_branch_id, db,
        )

        if allow_self_approval:
            # Supersede any existing Approved rate that started BEFORE effective_from
            # (shared helper — uses the same CASE/WHERE logic as approve_rate Step 2).
            # The conflict guard above already ensures no Approved rate starts on
            # or after effective_from, so this UPDATE is safe.
            await _supersede_current_approved_rates(
                company_id, target_driver_id, rate_type_id,
                data.effective_from, user_id, target_branch_id, db,
            )

        # Use two separate SQL statements to avoid asyncpg NULL type-inference issues
        # with nullable TIMESTAMPTZ parameters.
        if allow_self_approval:
            insert_result = await db.execute(
                text("""
                    INSERT INTO payroll.driverrates
                        (companyid, branchid, driverid, ratetypeid,
                         amount, effectivefrom, status,
                         createdbyuserid, approvedbyuserid, approvedatutc, notes)
                    VALUES
                        (:cid, :bid, :did, :rtid,
                         :amount, :eff_from, 'Approved',
                         :creator, :creator, NOW(), :notes)
                    RETURNING driverrateid
                """),
                {
                    "cid": company_id, "bid": target_branch_id, "did": target_driver_id,
                    "rtid": rate_type_id, "amount": amount, "eff_from": data.effective_from,
                    "creator": user_id, "notes": _copy_note,
                },
            )
        else:
            insert_result = await db.execute(
                text("""
                    INSERT INTO payroll.driverrates
                        (companyid, branchid, driverid, ratetypeid,
                         amount, effectivefrom, status,
                         createdbyuserid, notes)
                    VALUES
                        (:cid, :bid, :did, :rtid,
                         :amount, :eff_from, 'PendingApproval',
                         :creator, :notes)
                    RETURNING driverrateid
                """),
                {
                    "cid": company_id, "bid": target_branch_id, "did": target_driver_id,
                    "rtid": rate_type_id, "amount": amount, "eff_from": data.effective_from,
                    "creator": user_id, "notes": _copy_note,
                },
            )
        new_rate_id: int = insert_result.scalar_one()
        await _write_rate_audit(
            db,
            company_id=company_id,
            branch_id=target_branch_id,
            user_id=user_id,
            rate_id=new_rate_id,
            action_code="RATE_CREATED",
            new_value={
                "driver_id":             target_driver_id,
                "rate_type_id":          rate_type_id,
                "amount":                str(amount),
                "effective_from":        str(data.effective_from),
                "status":                "Approved" if allow_self_approval else "PendingApproval",
                "copied_from_driver_id": source_driver_id,
            },
        )

        if allow_self_approval:
            rates_approved += 1
        else:
            rates_pending += 1

    # Copy pay rules if requested
    pay_rules_copied = 0
    if data.include_pay_rules:
        src_rules_result = await db.execute(
            text("""
                SELECT ruletype, amount, effectivefrom, effectiveto, notes
                FROM   payroll.driverpayrules
                WHERE  driverid  = :src_did
                  AND  companyid = :cid
                  AND  status    = 'Active'
                ORDER BY ruletype, effectivefrom
            """),
            {"src_did": source_driver_id, "cid": company_id},
        )
        src_rules = src_rules_result.mappings().all()

        for src_rule in src_rules:
            # P1 #2 — Finalized-period guard for copied pay rules.
            #
            # Copied rules use data.effective_from (the request date), NOT the source
            # driver's historical effectivefrom.  This is consistent with how rates are
            # copied: all copied data starts from the single request effective_from date.
            #
            # Guard behaviour by allow_self_approval value:
            #   True  — the rates backdating guard (earlier in this function) already
            #           raised 422 for the same date, so this guard is never reached.
            #           It is kept here for correctness when future callers bypass the
            #           rates section (e.g. no Approved source rates exist).
            #   False — rates are written as PendingApproval (no backdating guard for
            #           rates); this guard is the primary protection for pay rules.
            await _check_not_in_finalized_period(
                company_id, target_branch_id, data.effective_from, db,
                label="pay rule",
            )

            # Overlap check uses data.effective_from; the new rule is open-ended (no
            # effectiveto) so any existing active/ended rule whose effectiveto is NULL
            # or >= data.effective_from would conflict.
            overlap = await db.execute(
                text("""
                    SELECT driverpayruleid
                    FROM   payroll.driverpayrules
                    WHERE  companyid = :cid
                      AND  driverid  = :did
                      AND  ruletype  = :rtype
                      AND  status    IN ('Active', 'Ended')
                      AND  (effectiveto IS NULL OR effectiveto >= :eff_from)
                    LIMIT 1
                """),
                {
                    "cid":      company_id,
                    "did":      target_driver_id,
                    "rtype":    src_rule["ruletype"],
                    "eff_from": data.effective_from,
                },
            )
            if overlap.first() is not None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Cannot copy {src_rule['ruletype']} rule: target driver already has "
                        f"an overlapping rule. End the existing rule first."
                    ),
                )

            # Insert with request effective_from and open effectiveto.
            # The source rule's historical effectiveto is not carried over — the target
            # gets a fresh open-ended rule starting at the requested date.
            rule_insert_result = await db.execute(
                text("""
                    INSERT INTO payroll.driverpayrules
                        (companyid, branchid, driverid, ruletype, amount,
                         effectivefrom, effectiveto, status, createdbyuserid, notes)
                    VALUES
                        (:cid, :bid, :did, :rtype, :amount,
                         :eff_from, NULL, 'Active', :creator, :notes)
                    RETURNING driverpayruleid
                """),
                {
                    "cid":      company_id,
                    "bid":      target_branch_id,
                    "did":      target_driver_id,
                    "rtype":    src_rule["ruletype"],
                    "amount":   src_rule["amount"],
                    "eff_from": data.effective_from,   # ← request date, not source date
                    "creator":  user_id,
                    "notes":    f"Copied from driver {source_driver_id}",
                },
            )
            new_rule_id: int = rule_insert_result.scalar_one()
            await _write_pay_rule_audit(
                db,
                company_id=company_id,
                branch_id=target_branch_id,
                user_id=user_id,
                rule_id=new_rule_id,
                action_code="DRIVER_PAY_RULE_CREATED",
                new_value={
                    "driver_id":             target_driver_id,
                    "rule_type":             src_rule["ruletype"],
                    "amount":                str(src_rule["amount"]),
                    "effective_from":        str(data.effective_from),
                    "effective_to":          None,
                    "status":                "Active",
                    "copied_from_driver_id": source_driver_id,
                },
            )
            pay_rules_copied += 1

    return CopyRatesResult(
        target_driver_id=target_driver_id,
        source_driver_id=source_driver_id,
        effective_from=data.effective_from,
        allow_self_approval=allow_self_approval,
        rates_copied=len(copyable_rates),
        rates_approved=rates_approved,
        rates_pending=rates_pending,
        pay_rules_copied=pay_rules_copied,
    )


# ===========================================================================
# CP-1 — Day Grid
# ===========================================================================

def _canonical_aliases(canonical_code: str) -> list[str]:
    """
    Return the canonical code plus all legacy aliases that map to it.

    Example: _canonical_aliases("HOURS") -> ["HOURS", "Hours"]
    Used in DB lookups so we find legacy rows ("Hours") when the caller sends
    the canonical code ("HOURS").
    """
    legacy = [k for k, v in _LEGACY_TO_CANONICAL.items() if v == canonical_code]
    return [canonical_code] + legacy


def _parse_quantity(raw: str | None, code: str) -> "Decimal | None":
    """
    Parse a quantity string from the day-grid save payload.

    - None or empty string → None (means clear/void the line)
    - Valid numeric string  → Decimal
    - Non-numeric non-empty → raises HTTP 422

    Raises before any DB writes so the caller can validate all rows first.
    """
    from decimal import InvalidOperation as _InvalidOperation

    if raw is None or raw.strip() == "":
        return None  # caller interprets as clear/void
    try:
        return Decimal(raw.strip())
    except _InvalidOperation:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid quantity '{raw}' for pay item {code}. "
                "Must be a number (e.g. '8', '8.5')."
            ),
        )


async def _validate_status_key(
    key_code: str | None,
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
    *,
    allow_deactivated: bool = False,
) -> dict | None:
    """
    Validate that key_code exists in payrollstatuskeys for the branch.

    - None / blank           → returns None (caller treats as clear)
    - Valid active           → returns the full row dict (including all limit fields)
    - Not found              → raises HTTP 422
    - Inactive key           → raises HTTP 422 unless allow_deactivated=True
      (allow_deactivated is used when the user is re-submitting a status code that
       matches an existing DraftLine selection — key was deactivated after it was
       originally applied, so saving the same value unchanged should not be blocked)
    """
    if not key_code or key_code.strip() == "":
        return None
    result = await db.execute(
        text("""
            SELECT statuskeyid, statuscode, keyname, isoffreason, hoursvalue, isactive,
                   limitusesperperiodenabled, limitusesperperiod,
                   limitusesperdriverenabled, limitusesperdriver,
                   limitusesacrossdriversenabled, limitusesacrossdrivers,
                   limitusesperdayenabled, limitusesperday
            FROM   payroll.payrollstatuskeys
            WHERE  companyid  = :cid
              AND  branchid   = :bid
              AND  statuscode = :code
        """),
        {"cid": company_id, "bid": branch_id, "code": key_code.strip()},
    )
    row = result.mappings().first()
    if row is None or (not allow_deactivated and not row["isactive"]):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Status key '{key_code}' is not a valid active status "
                "for this branch."
            ),
        )
    return dict(row)


# ---------------------------------------------------------------------------
# CP-2D1: canonical daily driver/day entry-state helpers
# ---------------------------------------------------------------------------

_KEEP = object()  # sentinel: do not modify this field in ON CONFLICT UPDATE


async def _resolve_status_key_id(
    company_id: int,
    branch_id: int,
    status_code: str,
    db: AsyncConnection,
) -> "int | None":
    """Return StatusKeyID for status_code — no isactive filter (accepts deactivated)."""
    result = await db.execute(
        text(
            "SELECT statuskeyid FROM payroll.payrollstatuskeys "
            "WHERE companyid = :cid AND branchid = :bid AND statuscode = :code LIMIT 1"
        ),
        {"cid": company_id, "bid": branch_id, "code": status_code},
    )
    row = result.first()
    return row[0] if row else None


async def _upsert_entry_state(
    company_id: int,
    branch_id: int,
    period_id: int,
    driver_id: int,
    work_date: "date",
    user_id: int,
    db: AsyncConnection,
    *,
    status_key_id: "int | None" = None,
    note_text: "str | None" = None,
    set_status: bool = True,
    set_note: bool = True,
) -> None:
    """
    Upsert canonical PayrollPeriodDriverDayEntryState row.

    set_status / set_note control which fields are updated on conflict.
    When both are True (default, save_day_grid path), both fields are set and
    IsVoided is derived from whether both would be empty.
    When only one is True (direct DraftLine API path), the other field is
    not touched by the ON CONFLICT UPDATE — only the INSERT uses NULL as default.
    """
    if not set_status and not set_note:
        return

    before_row = (await db.execute(text("""
        SELECT e.payrollperioddriverdayentrystateid, e.statuskeyid, e.notetext,
               sk.statuscode, sk.keyname AS statuslabel
        FROM payroll.payrollperioddriverdayentrystate e
        LEFT JOIN payroll.payrollstatuskeys sk ON sk.statuskeyid = e.statuskeyid
        WHERE e.companyid = :cid AND e.payrollperiodid = :pid
          AND e.driverid = :did AND e.workdate = :dt
    """), {"cid": company_id, "pid": period_id, "did": driver_id, "dt": work_date})).mappings().first()

    day_id_result = await db.execute(
        text(
            "SELECT payrollperioddayid FROM payroll.payrollperioddays "
            "WHERE payrollperiodid = :pid AND workdate = :dt LIMIT 1"
        ),
        {"pid": period_id, "dt": work_date},
    )
    day_id = day_id_result.scalar_one_or_none()

    # IsVoided: only deterministic when setting both fields simultaneously.
    # Single-field updates use FALSE (we are setting something, so not voided).
    if set_status and set_note:
        is_voided = status_key_id is None and not note_text
    else:
        is_voided = False

    # Build ON CONFLICT SET clause — only include fields being updated.
    conflict_parts = []
    if set_status:
        conflict_parts.append("statuskeyid = EXCLUDED.statuskeyid")
    if set_note:
        conflict_parts.append("notetext = EXCLUDED.notetext")
    if set_status and set_note:
        conflict_parts.append("isvoided = EXCLUDED.isvoided")
    else:
        conflict_parts.append("isvoided = FALSE")
    conflict_parts += [
        "updatedbyuserid = EXCLUDED.updatedbyuserid",
        "updatedatutc    = NOW()",
    ]
    conflict_clause = ",\n                    ".join(conflict_parts)

    await db.execute(
        text(f"""
            INSERT INTO payroll.payrollperioddriverdayentrystate
                (companyid, branchid, payrollperiodid, payrollperioddayid,
                 workdate, driverid, statuskeyid, notetext, isvoided,
                 createdbyuserid, updatedbyuserid, createdatutc, updatedatutc)
            VALUES
                (:cid, :bid, :pid, :day_id,
                 :dt, :did, :skid, :note, :voided,
                 :uid, :uid, NOW(), NOW())
            ON CONFLICT (payrollperiodid, driverid, workdate) DO UPDATE SET
                {conflict_clause}
        """),
        {
            "cid":    company_id,
            "bid":    branch_id,
            "pid":    period_id,
            "day_id": day_id,
            "dt":     work_date,
            "did":    driver_id,
            "skid":   status_key_id,
            "note":   note_text or None,
            "voided": is_voided,
            "uid":    user_id,
        },
    )
    after_row = (await db.execute(text("""
        SELECT e.payrollperioddriverdayentrystateid, e.statuskeyid, e.notetext,
               sk.statuscode, sk.keyname AS statuslabel
        FROM payroll.payrollperioddriverdayentrystate e
        LEFT JOIN payroll.payrollstatuskeys sk ON sk.statuskeyid = e.statuskeyid
        WHERE e.companyid = :cid AND e.payrollperiodid = :pid
          AND e.driverid = :did AND e.workdate = :dt
    """), {"cid": company_id, "pid": period_id, "did": driver_id, "dt": work_date})).mappings().one()

    def status_payload(row: Any | None) -> dict[str, Any] | None:
        if row is None or row["statuskeyid"] is None:
            return None
        return {
            "status_key_id": row["statuskeyid"], "status_code": row["statuscode"],
            "status_label": row["statuslabel"],
        }

    before_status = status_payload(before_row)
    after_status = status_payload(after_row)
    entry_id = int(after_row["payrollperioddriverdayentrystateid"])
    if set_status and before_status != after_status:
        action = "STATUS_CLEARED" if after_status is None else (
            "STATUS_SET" if before_status is None else "STATUS_CHANGED"
        )
        await capture_period_audit_evidence(
            company_id=company_id, branch_id=branch_id, period_id=period_id,
            domain="STATUS_NOTE", action_code=action,
            source_entity_type="PayrollPeriodDriverDayEntryState", source_entity_id=entry_id,
            user_id=user_id, required_permission_code="payroll.entry", db=db,
            before_state=before_status, after_state=after_status,
            driver_id=driver_id, work_date=work_date,
        )
    before_note = None if before_row is None else before_row["notetext"]
    after_note = after_row["notetext"]
    if set_note and before_note != after_note:
        action = "NOTE_CLEARED" if not after_note else (
            "NOTE_SET" if not before_note else "NOTE_CHANGED"
        )
        await capture_period_audit_evidence(
            company_id=company_id, branch_id=branch_id, period_id=period_id,
            domain="STATUS_NOTE", action_code=action,
            source_entity_type="PayrollPeriodDriverDayEntryState", source_entity_id=entry_id,
            user_id=user_id, required_permission_code="payroll.entry", db=db,
            before_state=None if before_note is None else {"note": before_note},
            after_state=None if after_note is None else {"note": after_note},
            driver_id=driver_id, work_date=work_date,
        )


async def _void_entry_state_field(
    period_id: int,
    company_id: int,
    driver_id: int,
    work_date: "date",
    user_id: int,
    db: AsyncConnection,
    *,
    clear_status: bool = False,
    clear_note: bool = False,
) -> None:
    """
    Update canonical entry-state row when a DailyStatus or DailyNote DraftLine is voided.

    Sets the corresponding field to NULL and recomputes IsVoided (TRUE only when
    both fields would then be empty).  No-op if the canonical row does not exist.
    """
    if not clear_status and not clear_note:
        return

    before_row = (await db.execute(text("""
        SELECT e.payrollperioddriverdayentrystateid, e.statuskeyid, e.notetext,
               sk.statuscode, sk.keyname AS statuslabel
        FROM payroll.payrollperioddriverdayentrystate e
        LEFT JOIN payroll.payrollstatuskeys sk ON sk.statuskeyid = e.statuskeyid
        WHERE e.companyid = :cid AND e.payrollperiodid = :pid
          AND e.driverid = :did AND e.workdate = :dt AND e.isvoided = FALSE
    """), {"cid": company_id, "pid": period_id, "did": driver_id, "dt": work_date})).mappings().first()

    if clear_status and clear_note:
        set_clause = "statuskeyid = NULL, notetext = NULL, isvoided = TRUE"
    elif clear_status:
        set_clause = "statuskeyid = NULL, isvoided = (notetext IS NULL OR notetext = '')"
    else:
        set_clause = "notetext = NULL, isvoided = (statuskeyid IS NULL)"

    await db.execute(
        text(f"""
            UPDATE payroll.payrollperioddriverdayentrystate
            SET    {set_clause},
                   updatedbyuserid = :uid,
                   updatedatutc    = NOW()
            WHERE  payrollperiodid = :pid
              AND  companyid       = :cid
              AND  driverid        = :did
              AND  workdate        = :dt
              AND  isvoided          = FALSE
        """),
        {"pid": period_id, "cid": company_id, "did": driver_id, "dt": work_date, "uid": user_id},
    )
    if before_row is None:
        return
    entry_id = int(before_row["payrollperioddriverdayentrystateid"])
    branch_id = int((await db.execute(text("""
        SELECT branchid FROM payroll.payrollperiods WHERE payrollperiodid = :pid
    """), {"pid": period_id})).scalar_one())
    if clear_status and before_row["statuskeyid"] is not None:
        await capture_period_audit_evidence(
            company_id=company_id, branch_id=branch_id, period_id=period_id,
            domain="STATUS_NOTE", action_code="STATUS_CLEARED",
            source_entity_type="PayrollPeriodDriverDayEntryState", source_entity_id=entry_id,
            user_id=user_id, required_permission_code="payroll.entry", db=db,
            before_state={
                "status_key_id": before_row["statuskeyid"], "status_code": before_row["statuscode"],
                "status_label": before_row["statuslabel"],
            }, after_state=None, driver_id=driver_id, work_date=work_date,
        )
    if clear_note and before_row["notetext"]:
        await capture_period_audit_evidence(
            company_id=company_id, branch_id=branch_id, period_id=period_id,
            domain="STATUS_NOTE", action_code="NOTE_CLEARED",
            source_entity_type="PayrollPeriodDriverDayEntryState", source_entity_id=entry_id,
            user_id=user_id, required_permission_code="payroll.entry", db=db,
            before_state={"note": before_row["notetext"]}, after_state=None,
            driver_id=driver_id, work_date=work_date,
        )


# =============================================================================
# CP-2D2: Status payment sync helpers
# =============================================================================

def _calculate_status_payment_amount(
    hours_value: "Decimal | None",
    resolved_rate: "Decimal | None",
) -> "Decimal | None":
    """
    Pure Status-payment arithmetic (CP-4B fix-forward): the exact single
    operation shared by the write-based synchronizer
    (`_sync_status_payment_for_entry_state`) and the CP-4B read-only live
    Status resolver (`_resolve_live_status_payment_lines`), so the two can
    never independently drift.

    `HoursValue x DriverRate.Amount`, quantized once to `Decimal("0.0001")`
    with explicit `ROUND_HALF_EVEN` -- identical to the ambient-context
    behavior this replaces (Python's implicit default rounding mode is
    already `ROUND_HALF_EVEN`; making it explicit here does not change any
    result). Returns `None` under the same truthiness guard the original
    inline expression used: no resolved rate, or a falsy (zero) hours value.

    Contains no SQL, no persistence, no workflow -- callers remain
    responsible for resolving `hours_value`/`resolved_rate` and for any
    write/void/upsert behavior.
    """
    if resolved_rate is None or not hours_value:
        return None
    return (hours_value * resolved_rate).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)


async def _sync_status_payment_for_entry_state(
    company_id: int,
    branch_id: int,
    period_id: int,
    driver_id: int,
    work_date: "date",
    status_key_id: "int | None",
    user_id: int,
    db: AsyncConnection,
) -> None:
    """
    Create, update, or void the STATUS_PAYMENT draft line for a driver/day.

    Called from save_day_grid after _upsert_entry_state.
    Called from _refresh_status_payment_lines for each PPDES row at submit/finalize.

    Logic:
      - If status_key_id is None or StatusKey has no StatusRateColumnID → void any existing line.
      - Otherwise → look up driver rate for StatusRateColumns.RateTypeID as-of work_date,
        compute HoursValue × rate, and upsert the draft line.

    SOURCE_ID format: 'STATUS_PAYMENT:{entry_state_id}:{status_key_id}:{status_rate_column_id}'
    Line type: the RateType's RateCode (e.g., 'STATUS_PAY').
    """
    # 1. Get canonical entry state row ID
    es_result = await db.execute(
        text("""
            SELECT payrollperioddriverdayentrystateid
            FROM   payroll.payrollperioddriverdayentrystate
            WHERE  payrollperiodid = :pid AND driverid = :did AND workdate = :dt
        """),
        {"pid": period_id, "did": driver_id, "dt": work_date},
    )
    es_row = es_result.mappings().first()
    entry_state_id: int | None = (
        es_row["payrollperioddriverdayentrystateid"] if es_row else None
    )

    # 2. Look up StatusKey payment config if status is set
    src_col_id: int | None = None
    src_rate_type_id: int | None = None
    rate_code: str | None = None
    hours_value: "Decimal | None" = None
    status_code_val: str | None = None
    key_name_val: str | None = None
    col_name_val: str | None = None

    if status_key_id is not None:
        sk_result = await db.execute(
            text("""
                SELECT sk.statuskeyid, sk.statuscode, sk.keyname,
                       sk.hoursvalue, sk.statusratecolumnid,
                       src.columnname AS src_col_name,
                       src.ratetypeid, rt.ratecode
                FROM   payroll.payrollstatuskeys sk
                LEFT JOIN payroll.statusratecolumns src
                       ON src.statusratecolumnid = sk.statusratecolumnid
                LEFT JOIN payroll.ratetypes rt
                       ON rt.ratetypeid = src.ratetypeid
                WHERE  sk.statuskeyid = :skid
            """),
            {"skid": status_key_id},
        )
        sk_row = sk_result.mappings().first()
        if sk_row:
            src_col_id       = sk_row["statusratecolumnid"]
            src_rate_type_id = sk_row["ratetypeid"]
            rate_code        = sk_row["ratecode"]
            hv               = sk_row["hoursvalue"]
            hours_value      = Decimal(str(hv)) if hv is not None else Decimal("0")
            status_code_val  = sk_row["statuscode"]
            key_name_val     = sk_row["keyname"]
            col_name_val     = sk_row["src_col_name"]

    # 3. Find any existing non-void STATUS_PAYMENT line for this slot
    existing_result = await db.execute(
        text("""
            SELECT draftlineid, sourceid
            FROM   payroll.payrolldraftlines
            WHERE  payrollperiodid = :pid
              AND  driverid        = :did
              AND  workdate        = :dt
              AND  companyid       = :cid
              AND  sourceid        LIKE 'STATUS_PAYMENT:%'
              AND  status         != 'Void'
            LIMIT 1
        """),
        {"pid": period_id, "did": driver_id, "dt": work_date, "cid": company_id},
    )
    existing = existing_result.mappings().first()

    # 4. If no status, no rate column, or no entry state → void existing and return
    if status_key_id is None or src_col_id is None or entry_state_id is None:
        if existing:
            await db.execute(
                text(
                    "UPDATE payroll.payrolldraftlines SET status = 'Void' "
                    "WHERE draftlineid = :lid"
                ),
                {"lid": existing["draftlineid"]},
            )
        return

    # 5. Compute amount
    import json as _json
    rate_result = await db.execute(
        text("""
            SELECT driverrateid, amount
            FROM   payroll.driverrates
            WHERE  driverid     = :did
              AND  ratetypeid   = :rtid
              AND  companyid    = :cid
              AND  status       IN ('Approved', 'Superseded')
              AND  effectivefrom <= :dt
              AND  (effectiveto IS NULL OR effectiveto >= :dt)
            ORDER BY effectivefrom DESC
            LIMIT 1
        """),
        {"did": driver_id, "rtid": src_rate_type_id, "cid": company_id, "dt": work_date},
    )
    rate_row = rate_result.mappings().first()
    resolved_rate    = Decimal(str(rate_row["amount"])) if rate_row else None
    resolved_rate_id = rate_row["driverrateid"] if rate_row else None
    calc_amount = _calculate_status_payment_amount(hours_value, resolved_rate)
    needs_review = resolved_rate is None

    new_source_id = (
        f"STATUS_PAYMENT:{entry_state_id}:{status_key_id}:{src_col_id}"
    )

    # Build SourceSnapshot — immutable audit record of inputs at draft time.
    snapshot_dict: dict = {
        "entry_state_id":           entry_state_id,
        "payroll_period_id":        period_id,
        "driver_id":                driver_id,
        "work_date":                str(work_date),
        "status_key_id":            status_key_id,
        "status_code":              status_code_val,
        "status_key_name":          key_name_val,
        "hours_value_used":         float(hours_value) if hours_value is not None else None,
        "status_rate_column_id":    src_col_id,
        "status_rate_column_name":  col_name_val,
        "rate_type_id":             src_rate_type_id,
        "rate_code":                rate_code,
        "driver_rate_id":           resolved_rate_id,
        "resolved_rate_amount":     float(resolved_rate) if resolved_rate is not None else None,
        "calculated_amount":        float(calc_amount) if calc_amount is not None else None,
        "formula":                  "HoursValue * DriverRate.Amount",
    }
    # Remove None values to keep snapshot lean
    source_snapshot = _json.dumps(
        {k: v for k, v in snapshot_dict.items() if v is not None}
    )

    # 6. Upsert draft line
    if existing:
        if existing["sourceid"] != new_source_id:
            # Status key or column changed — void old, fall through to insert
            await db.execute(
                text(
                    "UPDATE payroll.payrolldraftlines SET status = 'Void' "
                    "WHERE draftlineid = :lid"
                ),
                {"lid": existing["draftlineid"]},
            )
            existing = None
        else:
            # In-place update (also refresh SourceSnapshot in case rate changed)
            await db.execute(
                text("""
                    UPDATE payroll.payrolldraftlines
                    SET    quantity           = :qty,
                           calculatedamount   = :calc,
                           needsmanagerreview = :review,
                           sourcesnapshot     = CAST(:snap AS JSONB),
                           status             = 'Active'
                    WHERE  draftlineid = :lid
                """),
                {
                    "qty":    hours_value,
                    "calc":   calc_amount,
                    "review": needs_review,
                    "snap":   source_snapshot,
                    "lid":    existing["draftlineid"],
                },
            )
            return

    # Insert new STATUS_PAYMENT line
    await db.execute(
        text("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid,
                 workdate, linetype, linescope, quantity,
                 calculatedamount, sourcetype, sourceid,
                 status, needsmanagerreview, addedbyuserid, sourcesnapshot)
            VALUES
                (:cid, :bid, :pid, :did,
                 :dt, :lt, 'Daily', :qty,
                 :calc, 'System', :sid,
                 'Active', :review, :uid, CAST(:snap AS JSONB))
        """),
        {
            "cid":    company_id,
            "bid":    branch_id,
            "pid":    period_id,
            "did":    driver_id,
            "dt":     work_date,
            "lt":     rate_code,
            "qty":    hours_value,
            "calc":   calc_amount,
            "sid":    new_source_id,
            "review": needs_review,
            "uid":    user_id,
            "snap":   source_snapshot,
        },
    )


class _LiveStatusLine(NamedTuple):
    """One canonically-resolved, read-only, live Status-derived pay line
    (CP-4B). Distinct from the persisted STATUS_PAYMENT compatibility
    projection DraftLine, which may be stale."""
    driver_id: int
    entry_state_id: int
    work_date: "date"
    status_key_id: int
    status_code: str
    status_rate_column_id: "int | None"
    rate_type_id: int | None
    driver_rate_id: int | None
    line_type: str
    hours_value: Decimal
    resolved_rate_amount: "Decimal | None"
    calculated_amount: "Decimal | None"
    needs_manager_review: bool


async def _resolve_live_status_payment_lines(
    period_id: int,
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> "list[_LiveStatusLine]":
    """
    CP-4B read-only resolver: computes current, live Status-derived pay for
    every canonical selected Status in this period, directly from
    `PayrollPeriodDriverDayEntryState.StatusKeyID` -- never from the
    persisted STATUS_PAYMENT/STATUS_PAY compatibility-projection DraftLine,
    which is a write-time snapshot that can go stale after an effective-
    dated rate change (see `_sync_status_payment_for_entry_state`, which
    only re-runs on save/submit/finalize, not on every read).

    Uses the exact same rate-resolution rule as the synchronizer (Approved
    or Superseded `DriverRates`, effective-dated as-of the entry's own
    `WorkDate`, most-recent `EffectiveFrom` wins -- Pending rates are
    excluded by the `status IN ('Approved','Superseded')` filter) and
    shares its arithmetic via `_calculate_status_payment_amount`, so this
    can never independently drift from the persisted-write formula.

    Guarantees: no INSERT/UPDATE/DELETE; no audit write; no mutation of the
    canonical entry-state rows or any StatusKey/StatusRateColumns row.
    A StatusKey with no configured `StatusRateColumnID` (a non-payment
    status, e.g. an off-reason with no rate) is not a blocker and is simply
    omitted -- it never expected a payment line.
    """
    rows = (await db.execute(
        text("""
            SELECT
                ppdes.driverid,
                ppdes.payrollperioddriverdayentrystateid,
                ppdes.workdate,
                sk.statuskeyid,
                sk.statuscode,
                sk.hoursvalue,
                sk.statusratecolumnid,
                src.ratetypeid,
                rt.ratecode
            FROM   payroll.payrollperioddriverdayentrystate ppdes
            JOIN   payroll.payrollstatuskeys sk ON sk.statuskeyid = ppdes.statuskeyid
            LEFT JOIN payroll.statusratecolumns src ON src.statusratecolumnid = sk.statusratecolumnid
            LEFT JOIN payroll.ratetypes rt ON rt.ratetypeid = src.ratetypeid
            WHERE  ppdes.payrollperiodid = :pid
              AND  ppdes.companyid       = :cid
              AND  ppdes.branchid        = :bid
              AND  ppdes.isvoided        = FALSE
              AND  ppdes.statuskeyid IS NOT NULL
            ORDER BY ppdes.driverid, ppdes.workdate
        """),
        {"pid": period_id, "cid": company_id, "bid": branch_id},
    )).mappings().all()

    results: list[_LiveStatusLine] = []
    for row in rows:
        src_rate_type_id = row["ratetypeid"]
        if src_rate_type_id is None:
            # No configured Status rate column -- this status never expects
            # a payment line; not a blocker, simply not applicable.
            continue

        hv = row["hoursvalue"]
        hours_value = Decimal(str(hv)) if hv is not None else Decimal("0")

        rate_row = (await db.execute(
            text("""
                SELECT driverrateid, amount
                FROM   payroll.driverrates
                WHERE  driverid      = :did
                  AND  ratetypeid    = :rtid
                  AND  companyid     = :cid
                  AND  status        IN ('Approved', 'Superseded')
                  AND  effectivefrom <= :dt
                  AND  (effectiveto IS NULL OR effectiveto >= :dt)
                ORDER BY effectivefrom DESC
                LIMIT 1
            """),
            {
                "did": row["driverid"], "rtid": src_rate_type_id,
                "cid": company_id, "dt": row["workdate"],
            },
        )).mappings().first()
        resolved_rate = Decimal(str(rate_row["amount"])) if rate_row else None

        results.append(_LiveStatusLine(
            driver_id=row["driverid"],
            entry_state_id=row["payrollperioddriverdayentrystateid"],
            work_date=row["workdate"],
            status_key_id=row["statuskeyid"],
            status_code=row["statuscode"],
            status_rate_column_id=row["statusratecolumnid"],
            rate_type_id=src_rate_type_id,
            driver_rate_id=(int(rate_row["driverrateid"]) if rate_row else None),
            line_type=row["ratecode"] or "STATUS_PAY",
            hours_value=hours_value,
            resolved_rate_amount=resolved_rate,
            calculated_amount=_calculate_status_payment_amount(hours_value, resolved_rate),
            needs_manager_review=(resolved_rate is None),
        ))

    return results


async def _refresh_status_payment_lines(
    period_id: int,
    company_id: int,
    branch_id: int,
    user_id: int,
    db: AsyncConnection,
) -> int:
    """
    Re-sync all STATUS_PAYMENT draft lines for a period from PPDES state.

    Called before _refresh_draft_calculations at submit and finalize so that
    status-payment lines are current before the NMR guard runs.

    Returns the count of PPDES rows processed.
    """
    # Fetch all non-voided PPDES rows for this period
    ppdes_result = await db.execute(
        text("""
            SELECT payrollperioddriverdayentrystateid,
                   driverid, workdate, statuskeyid
            FROM   payroll.payrollperioddriverdayentrystate
            WHERE  payrollperiodid = :pid
              AND  companyid       = :cid
              AND  isvoided        = FALSE
        """),
        {"pid": period_id, "cid": company_id},
    )
    rows = list(ppdes_result.mappings().all())

    # Also void any orphaned STATUS_PAYMENT lines for slots with no PPDES row
    # (e.g., status was cleared and PPDES was voided after a prior sync)
    await db.execute(
        text("""
            UPDATE payroll.payrolldraftlines
            SET    status = 'Void'
            WHERE  payrollperiodid = :pid
              AND  companyid       = :cid
              AND  sourceid        LIKE 'STATUS_PAYMENT:%'
              AND  status         != 'Void'
              AND  NOT EXISTS (
                SELECT 1 FROM payroll.payrollperioddriverdayentrystate ppdes
                WHERE  ppdes.payrollperiodid = payrolldraftlines.payrollperiodid
                  AND  ppdes.driverid        = payrolldraftlines.driverid
                  AND  ppdes.workdate        = payrolldraftlines.workdate
                  AND  ppdes.isvoided        = FALSE
                  AND  ppdes.statuskeyid     IS NOT NULL
              )
        """),
        {"pid": period_id, "cid": company_id},
    )

    # CP-2E: pre-load snapshot rows for this period (one query, not per-row)
    _refresh_has_snap = await _period_has_driver_eligibility_snapshot(period_id, db)
    _snap_row_by_driver: dict[int, Any] = {}
    if _refresh_has_snap:
        snap_res = await db.execute(
            text("""
                SELECT driverid, eligibilityreasoncode,
                       hiredatesnapshot, terminationdatesnapshot,
                       drivereffectivefromsnapshot, drivereffectivetosnapshot,
                       iseligibleforperiod
                FROM   payroll.payrollperioddrivereligibility
                WHERE  payrollperiodid = :pid AND companyid = :cid
            """),
            {"pid": period_id, "cid": company_id},
        )
        for snap in snap_res.mappings().all():
            _snap_row_by_driver[snap["driverid"]] = snap

    for row in rows:
        driver_id = row["driverid"]
        work_date = row["workdate"]

        # CP-2E: eligibility guard — skip ineligible dates in snapshotted periods
        if _refresh_has_snap:
            snap = _snap_row_by_driver.get(driver_id)
            if snap is None:
                # Driver not in snapshot → skip
                continue
            # Primary: date-window check. Secondary rescue: existing source on
            # exact date (any reason code — covers generated-row drivers with
            # PPDES outside their eligibility window from before the snapshot).
            if not _is_snapshot_row_eligible_for_workdate(snap, work_date):
                has_src = await _driver_has_existing_daily_source_on_date(
                    period_id, driver_id, work_date, db
                )
                if not has_src:
                    continue

        await _sync_status_payment_for_entry_state(
            company_id=company_id,
            branch_id=branch_id,
            period_id=period_id,
            driver_id=driver_id,
            work_date=work_date,
            status_key_id=row["statuskeyid"],
            user_id=user_id,
            db=db,
        )

    return len(rows)


async def _enforce_status_key_limits(
    status_code: str,
    key_row: dict,
    period_id: int,
    company_id: int,
    work_date: "date",
    batch_driver_ids: list[int],
    all_batch_pairs: list[tuple],
    db: AsyncConnection,
) -> None:
    """
    Enforce the four optional usage limits for a status key within save_day_grid.

    Called once per unique status_code being SET in the batch, after all
    existence/active checks have passed.

    Counting rules:
      - Only active (non-Void) DailyStatus lines are counted.
      - The batch's own (driver_id, work_date) pairs are excluded from
        existing counts (those slots are being overwritten).
      - The batch's new contributions are counted separately and added.
      - Per-period:  total row count in period ≤ LimitUsesPerPeriod
      - Per-driver:  row count per driver in period ≤ LimitUsesPerDriver
      - Across-all-drivers: distinct driver count in period ≤ LimitUsesAcrossDrivers
      - Per-day:    row count on this work_date ≤ LimitUsesPerDay

    Raises HTTP 422 if any enabled limit would be exceeded; does not write.
    """
    any_limit = (
        key_row["limitusesperperiodenabled"]
        or key_row["limitusesperdriverenabled"]
        or key_row["limitusesacrossdriversenabled"]
        or key_row["limitusesperdayenabled"]
    )
    if not any_limit:
        return

    batch_count = len(batch_driver_ids)

    # Build exclusion clause: exclude all (driver_id, work_date) pairs in
    # this batch because those slots will be overwritten regardless.
    pair_params: dict = {}
    if all_batch_pairs:
        pair_conds = " OR ".join(
            f"(driverid = :ex_did_{i} AND workdate = :ex_wdt_{i})"
            for i in range(len(all_batch_pairs))
        )
        for i, (did, wdt) in enumerate(all_batch_pairs):
            pair_params[f"ex_did_{i}"] = did
            pair_params[f"ex_wdt_{i}"] = wdt
        excl = f"AND NOT ({pair_conds})"
    else:
        excl = ""

    base_params = {
        "period_id": period_id,
        "company_id": company_id,
        "status_code": status_code,
        **pair_params,
    }

    # ── Per-period limit ─────────────────────────────────────────────── #
    if key_row["limitusesperperiodenabled"]:
        limit = key_row["limitusesperperiod"]
        r = await db.execute(
            text(f"""
                SELECT COUNT(*) FROM payroll.payrolldraftlines
                WHERE payrollperiodid = :period_id
                  AND companyid      = :company_id
                  AND linetype       = 'DailyStatus'
                  AND status        != 'Void'
                  AND notes          = :status_code
                  {excl}
            """),
            base_params,
        )
        existing = r.scalar_one()
        if existing + batch_count > limit:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Status key '{status_code}' has reached its period limit "
                    f"of {limit} use(s). "
                    f"({existing} existing + {batch_count} in this save = {existing + batch_count})"
                ),
            )

    # ── Per-driver limit ─────────────────────────────────────────────── #
    if key_row["limitusesperdriverenabled"]:
        limit = key_row["limitusesperdriver"]
        for driver_id in batch_driver_ids:
            r = await db.execute(
                text(f"""
                    SELECT COUNT(*) FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :period_id
                      AND companyid      = :company_id
                      AND driverid       = :driver_id
                      AND linetype       = 'DailyStatus'
                      AND status        != 'Void'
                      AND notes          = :status_code
                      {excl}
                """),
                {"driver_id": driver_id, **base_params},
            )
            existing = r.scalar_one()
            if existing + 1 > limit:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Status key '{status_code}' has reached its per-driver limit "
                        f"of {limit} use(s) per period. "
                        f"Driver {driver_id} already has {existing} use(s) in this period."
                    ),
                )

    # ── Across-all-drivers limit (distinct drivers in period) ─────────── #
    if key_row["limitusesacrossdriversenabled"]:
        limit = key_row["limitusesacrossdrivers"]
        # Compute total distinct drivers after the batch by unioning:
        #   existing rows (excl. batch pairs) UNION batch drivers setting this key.
        # This correctly handles drivers who appear in both (counted once).
        if batch_driver_ids:
            new_selects = " UNION ALL ".join(
                f"SELECT :new_did_{i} AS driverid"
                for i in range(len(batch_driver_ids))
            )
            new_params = {f"new_did_{i}": d for i, d in enumerate(batch_driver_ids)}
            combined_query = f"""
                SELECT COUNT(DISTINCT driverid) FROM (
                    SELECT driverid FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :period_id
                      AND companyid      = :company_id
                      AND linetype       = 'DailyStatus'
                      AND status        != 'Void'
                      AND notes          = :status_code
                      {excl}
                    UNION ALL
                    {new_selects}
                ) combined
            """
        else:
            new_params = {}
            combined_query = f"""
                SELECT COUNT(DISTINCT driverid) FROM (
                    SELECT driverid FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :period_id
                      AND companyid      = :company_id
                      AND linetype       = 'DailyStatus'
                      AND status        != 'Void'
                      AND notes          = :status_code
                      {excl}
                ) combined
            """
        r = await db.execute(
            text(combined_query),
            {**base_params, **new_params},
        )
        total_distinct = r.scalar_one()
        if total_distinct > limit:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Status key '{status_code}' has reached its across-all-drivers limit "
                    f"of {limit} distinct driver(s) per period. "
                    f"Would have {total_distinct} distinct driver(s)."
                ),
            )

    # ── Per-day limit ─────────────────────────────────────────────────── #
    if key_row["limitusesperdayenabled"]:
        limit = key_row["limitusesperday"]
        r = await db.execute(
            text(f"""
                SELECT COUNT(*) FROM payroll.payrolldraftlines
                WHERE payrollperiodid = :period_id
                  AND companyid      = :company_id
                  AND workdate       = :work_date
                  AND linetype       = 'DailyStatus'
                  AND status        != 'Void'
                  AND notes          = :status_code
                  {excl}
            """),
            {"work_date": work_date, **base_params},
        )
        existing = r.scalar_one()
        if existing + batch_count > limit:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Status key '{status_code}' has reached its per-day limit "
                    f"of {limit} use(s) for {work_date}. "
                    f"({existing} existing + {batch_count} in this save = {existing + batch_count})"
                ),
            )


async def _finalized_drivers_off_entries(
    period: PeriodSummary,
    company_id: int,
    db: AsyncConnection,
) -> tuple[list[dict], dict[str, str | None]]:
    """
    Stage B3 Unit 8C-7: Locked/Archived Status evidence for CP-2.5 Drivers Off.

    Reuses the same Approved-PeriodApproval-review-item snapshot authority
    and shared read primitives as Day Grid (Unit 8C-3) and CP-5B Off Drivers
    (Unit 8C-5) via status_evidence.py -- never the EntryState freeze columns
    (StatusCodeSnapshot/StatusLabelSnapshot/StatusIsOffReasonSnapshot/
    FinalizedAtUtc), never live PayrollStatusKeys, never legacy DailyStatus
    DraftLines. Returns (entries, {state, reason_code}); entries is always
    [] unless state is AVAILABLE with at least one captured off-reason row.
    """
    snapshot, availability = await status_evidence.resolve_finalized_snapshot(
        db, period_id=period.payroll_period_id, company_id=company_id, branch_id=period.branch_id,
    )
    if snapshot is None:
        # No usable snapshot provenance -- never fall back to another
        # snapshot or to mutable current state; evidence is unavailable.
        return [], availability

    status_rows = await status_evidence.read_status_entries(
        db,
        snapshot_id=snapshot["payrollcalculationsnapshotid"],
        company_id=company_id,
        branch_id=period.branch_id,
        period_id=period.payroll_period_id,
    )
    evidence_state = status_evidence.status_evidence_availability(snapshot, status_rows)
    off_rows = [row for row in status_rows if row["is_off_reason"]]
    if not off_rows:
        return [], evidence_state

    driver_ids = sorted({row["driver_id"] for row in off_rows})
    identity_rows = (await db.execute(
        text("""
            SELECT d.driverid, e.fullname AS drivername, d.drivercode
            FROM core.drivers d
            JOIN core.employees e ON e.employeeid = d.employeeid
            WHERE d.companyid = :company_id
              AND d.driverid  = ANY(:driver_ids)
        """),
        {"company_id": company_id, "driver_ids": driver_ids},
    )).mappings().all()
    identities = {int(r["driverid"]): r for r in identity_rows}

    # NoteText is not part of the immutable Status-evidence table's contract
    # (see status_evidence.read_status_entries) -- read it separately from
    # canonical EntryState, which no write path can change once a period is
    # Locked (Locked/Archived are in _WRITE_BLOCKED_STATUSES).
    note_rows = (await db.execute(
        text("""
            SELECT driverid, workdate, notetext
            FROM payroll.payrollperioddriverdayentrystate
            WHERE payrollperiodid = :period_id
              AND companyid = :company_id
              AND isvoided = FALSE
        """),
        {"period_id": period.payroll_period_id, "company_id": company_id},
    )).mappings().all()
    notes_by_day = {
        (int(row["driverid"]), row["workdate"]): row["notetext"] for row in note_rows
    }

    entries: list[dict] = []
    for row in sorted(off_rows, key=lambda r: (r["work_date"], r["driver_id"])):
        identity = identities.get(row["driver_id"])
        if identity is None:
            continue
        entries.append({
            "driver_id":       row["driver_id"],
            "driver_name":     identity["drivername"],
            "driver_code":     identity["drivercode"],
            "work_date":       row["work_date"],
            "status_key_code": row["status_code"],
            "status_label":    row["status_label"],
            "notes":           notes_by_day.get((row["driver_id"], row["work_date"])),
        })
    return entries, evidence_state


async def get_drivers_off(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> tuple[list[dict], dict[str, str | None] | None]:
    """
    Return all off-driver records for the entire period (all work dates).

    Draft/Open/InReview/Returned/Approved (unchanged): an off-driver record
    is a legacy DailyStatus line whose status code maps to a live
    PayrollStatusKeys row with IsOffReason = TRUE. The status key code is
    stored in the Notes column of DailyStatus lines. An optional DailyNote
    line for the same driver/date is joined to supply the driver-level notes
    text.

    Locked/Archived (Stage B3 Unit 8C-7): Status meaning comes only from
    immutable calculation-snapshot evidence via _finalized_drivers_off_entries
    -- never live PayrollStatusKeys, never legacy DailyStatus DraftLines.
    Returns (entries, {state, reason_code}) instead of (entries, None).

    ODA/Driver users are blocked unconditionally.
    payroll.view OR payroll.entry permission is required.
    """
    # ── Driver-role hard-block ───────────────────────────────────────────────── #
    await _require_not_driver_role(company_id, user_id, db)

    period = await get_period_by_id(company_id, user_id, period_id, db)

    await _check_any_permission(
        company_id, user_id, period.branch_id, ["payroll.view", "payroll.entry"], db
    )

    if period.status in ("Locked", "Archived"):
        entries, evidence_state = await _finalized_drivers_off_entries(period, company_id, db)
        return entries, evidence_state

    result = await db.execute(
        text("""
            SELECT
                d.driverid,
                e.fullname       AS drivername,
                d.drivercode,
                dl.workdate,
                dl.notes         AS status_key_code,
                sk.keyname       AS status_label,
                dn.notes         AS driver_notes
            FROM payroll.payrolldraftlines dl
            JOIN core.drivers   d  ON d.driverid  = dl.driverid
            JOIN core.employees e  ON e.employeeid = d.employeeid
            JOIN payroll.payrollstatuskeys sk
                ON  sk.companyid  = dl.companyid
                AND sk.branchid   = dl.branchid
                AND sk.statuscode = dl.notes
                AND sk.isoffreason = TRUE
                AND sk.isactive    = TRUE
            LEFT JOIN payroll.payrolldraftlines dn
                ON  dn.payrollperiodid = dl.payrollperiodid
                AND dn.driverid        = dl.driverid
                AND dn.workdate        = dl.workdate
                AND dn.linetype        = 'DailyNote'
                AND dn.status         != 'Void'
            WHERE dl.payrollperiodid = :period_id
              AND dl.companyid       = :company_id
              AND dl.linetype        = 'DailyStatus'
              AND dl.status         != 'Void'
            ORDER BY dl.workdate, e.fullname
        """),
        {"period_id": period_id, "company_id": company_id},
    )

    rows = result.mappings().all()
    return [
        {
            "driver_id":        int(r["driverid"]),
            "driver_name":      r["drivername"],
            "driver_code":      r["drivercode"],
            "work_date":        r["workdate"],
            "status_key_code":  r["status_key_code"],
            "status_label":     r["status_label"],
            "notes":            r["driver_notes"],
        }
        for r in rows
    ], None


async def get_day_grid(
    period_id: int,
    company_id: int,
    user_id: int,
    work_date: date | None,
    db: AsyncConnection,
) -> DayGridResponse:
    """
    Return the full daily entry grid for a single work_date within a period.

    If work_date is omitted (None), the backend resolves it:
      - today's date if today falls within the period
      - period.start_date otherwise

    - Columns: active Daily-scope pay items for the branch.
    - Status keys: active PayrollStatusKeys for the branch.
    - Rows: all eligible active drivers; populated with existing draft lines.
    """
    # ── ODA / Driver-role guard ───────────────────────────────────────────── #
    # Current Payroll is a manager/dispatcher screen, not a driver self-service
    # screen.  OwnDriverDataOnly users are blocked unconditionally — they must
    # not receive any payroll data (driver names, quantities, amounts, summary
    # counts, etc.).  This is a belt-and-suspenders guard on top of the
    # payroll.view / payroll.entry permission check below.
    own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)
    if own_driver_id is not None:
        raise HTTPException(
            status_code=403,
            detail="Current Payroll is not accessible to driver-role users.",
        )

    # ── Load and access-check the period ─────────────────────────────────── #
    period = await get_period_by_id(company_id, user_id, period_id, db)

    # ── P1 #1: resolve work_date when omitted ────────────────────────────── #
    if work_date is None:
        today = date.today()
        if period.start_date <= today <= period.end_date:
            work_date = today
        else:
            work_date = period.start_date

    # ── work_date bounds check (CP-2B: snapshot-aware) ───────────────────── #
    await _validate_period_work_date(
        period.payroll_period_id, work_date,
        period.start_date, period.end_date, db,
    )

    # ── Permission: payroll.view OR payroll.entry ─────────────────────────── #
    await _check_any_permission(
        company_id, user_id, period.branch_id, ["payroll.view", "payroll.entry"], db
    )

    branch_id = period.branch_id

    # ── Load active Daily columns for the branch ─────────────────────────── #
    # CP-2C: use snapshot when period has PayrollPeriodPayItems rows (post-0053
    # periods); fall back to live BranchPayItemConfig query for legacy periods.
    # Use _period_has_pay_item_snapshot to distinguish "post-0053 period with
    # zero active Daily items" from "legacy period with no snapshot" — both
    # would produce an empty snap_cols list, but only the latter should fall back.
    columns: list[DayGridColumn] = []
    if await _period_has_pay_item_snapshot(period.payroll_period_id, db):
        snap_cols = await _get_period_pay_item_snapshot(
            period.payroll_period_id, company_id, db, scope="Daily", active_only=True,
        )
        for row in snap_cols:
            code = row["payitemcode"]
            if code in _INFORMATIONAL_ONLY:
                continue
            columns.append(DayGridColumn(
                pay_item_code=code,
                label=row["displaylabel"] or row["payitemname"] or code,
                rate_behavior=row["ratebehavior"] or "None",
                is_time=(code in ("HOURS", "WAIT_TIME")) or (row["datatype"] == "Time"),
            ))
    else:
        cols_result = await db.execute(
            text("""
                SELECT pi.payitemcode, pi.payitemname, pi.ratebehavior, pi.datatype
                FROM   payroll.payitems pi
                LEFT JOIN payroll.branchpayitemconfig bpic
                       ON bpic.payitemid  = pi.payitemid
                      AND bpic.companyid  = :cid
                      AND bpic.branchid   = :bid
                      AND bpic.effectivefrom <= :dt
                      AND (bpic.effectiveto IS NULL OR bpic.effectiveto >= :dt)
                WHERE  (pi.companyid IS NULL OR pi.companyid = :cid)
                  AND  pi.itemscope  = 'Daily'
                  AND  pi.status    != 'Retired'
                  AND  COALESCE(bpic.isactive, pi.isdefaultbranchactive) = TRUE
                ORDER BY pi.sortorder NULLS LAST, pi.payitemcode
            """),
            {"cid": company_id, "bid": branch_id, "dt": work_date},
        )
        for row in cols_result.mappings().all():
            code = row["payitemcode"]
            if code in _INFORMATIONAL_ONLY:
                continue
            columns.append(DayGridColumn(
                pay_item_code=code,
                label=row["payitemname"] or code,
                rate_behavior=row["ratebehavior"] or "None",
                is_time=(code in ("HOURS", "WAIT_TIME")) or (row["datatype"] == "Time"),
            ))

    # ── Load status keys for the branch ──────────────────────────────────── #
    sk_result = await db.execute(
        text("""
            SELECT statuskeyid, statuscode, keyname, isoffreason, hoursvalue
            FROM   payroll.payrollstatuskeys
            WHERE  companyid = :cid
              AND  branchid  = :bid
              AND  isactive  = TRUE
            ORDER BY displayorder, keyname
        """),
        {"cid": company_id, "bid": branch_id},
    )
    status_keys: list[DayGridStatusKey] = []
    status_key_map: dict[str, DayGridStatusKey] = {}  # statuscode -> key
    for row in sk_result.mappings().all():
        sk = DayGridStatusKey(
            status_key_id=row["statuskeyid"],
            key_code=row["statuscode"],
            label=row["keyname"] or row["statuscode"],
            is_off_reason=bool(row["isoffreason"]),
            hours_value=row["hoursvalue"],
        )
        status_keys.append(sk)
        status_key_map[row["statuscode"]] = sk

    # ── Load eligible drivers (CP-2E: snapshot-aware) ────────────────────── #
    # For snapshotted periods use the canonical eligibility snapshot roster.
    # Legacy periods (no marker) fall back to the live EmploymentStatus query.
    _has_snapshot = await _period_has_driver_eligibility_snapshot(period_id, db)
    if _has_snapshot:
        # Load all snapshot rows for this period
        snap_rows_result = await db.execute(
            text("""
                SELECT driverid,
                       COALESCE(drivernamesnapshot, '') AS drivername,
                       COALESCE(drivercodesnapshot, '') AS drivercode,
                       eligibilityreasoncode,
                       hiredatesnapshot,
                       terminationdatesnapshot,
                       drivereffectivefromsnapshot,
                       drivereffectivetosnapshot,
                       iseligibleforperiod
                FROM   payroll.payrollperioddrivereligibility
                WHERE  payrollperiodid = :pid
                  AND  companyid       = :cid
                  AND  branchid        = :bid
                ORDER BY drivername
            """),
            {"pid": period_id, "cid": company_id, "bid": branch_id},
        )
        snap_rows_all = list(snap_rows_result.mappings().all())

        # Existing-source rescue: for ANY reason code, a driver out of their
        # date window is still shown if they have existing daily source on this
        # exact work_date (DraftLine or EntryState).  Collect the out-of-window
        # candidates first, then batch-check them.
        out_of_window_candidates = [
            r["driverid"] for r in snap_rows_all
            if not _is_snapshot_row_eligible_for_workdate(r, work_date)
        ]
        rescue_driver_ids_on_date: set[int] = set()
        if out_of_window_candidates:
            in_clause_rescue, in_params_rescue = _build_in_clause(
                out_of_window_candidates, "rescue"
            )
            rescue_result = await db.execute(
                text(f"""
                    SELECT DISTINCT driverid FROM (
                        SELECT driverid FROM payroll.payrolldraftlines
                        WHERE payrollperiodid = :pid AND workdate = :dt
                          AND status != 'Void'
                          AND driverid IN ({in_clause_rescue})
                        UNION
                        SELECT driverid FROM payroll.payrollperioddriverdayentrystate
                        WHERE payrollperiodid = :pid AND workdate = :dt
                          AND isvoided = FALSE
                          AND driverid IN ({in_clause_rescue})
                    ) src
                """),
                {"pid": period_id, "dt": work_date, **in_params_rescue},
            )
            rescue_driver_ids_on_date = {
                r["driverid"] for r in rescue_result.mappings().all()
            }

        # Filter snapshot rows to those eligible for this work_date
        # (primary: date-window check; secondary: existing-source rescue)
        drivers_raw = []
        for snap in snap_rows_all:
            if _is_snapshot_row_eligible_for_workdate(snap, work_date):
                pass  # window eligible — include
            elif snap["driverid"] in rescue_driver_ids_on_date:
                pass  # existing source rescue — include
            else:
                continue
            drivers_raw.append({
                "driverid":   snap["driverid"],
                "drivername": snap["drivername"],
                "drivercode": snap["drivercode"],
            })
        drivers = drivers_raw
    else:
        # Legacy fallback: live roster query
        drv_result = await db.execute(
            text("""
                SELECT d.driverid, e.fullname AS drivername, d.drivercode
                FROM   core.drivers   d
                JOIN   core.employees e ON e.employeeid = d.employeeid
                WHERE  d.companyid          = :cid
                  AND  d.branchid           = :bid
                  AND  e.employmentstatus   = 'Active'
                  AND  (
                           d.driverstatus = 'Active'
                        OR (d.driverstatus = 'Transferred'
                            AND d.effectiveto IS NOT NULL
                            AND d.effectiveto >= :dt)
                       )
                  AND  (e.hiredate IS NULL OR e.hiredate <= :dt)
                  AND  (e.terminationdate IS NULL OR e.terminationdate >= :dt)
                  AND  (d.effectivefrom IS NULL OR d.effectivefrom <= :dt)
                  AND  (d.effectiveto   IS NULL OR d.effectiveto   >= :dt)
                ORDER BY e.fullname
            """),
            {"cid": company_id, "bid": branch_id, "dt": work_date},
        )
        drivers = list(drv_result.mappings().all())
    driver_ids = [d["driverid"] for d in drivers]

    # ── Load existing draft lines for these drivers on this date ─────────── #
    # driver_id -> list of line rows
    lines_by_driver: dict[int, list[Any]] = {did: [] for did in driver_ids}

    if driver_ids:
        in_clause, in_params = _build_in_clause(driver_ids, "drv")
        dl_result = await db.execute(
            text(f"""
                SELECT dl.draftlineid, dl.driverid, dl.linetype,
                       dl.quantity, dl.calculatedamount, dl.needsmanagerreview,
                       dl.notes, dl.status
                FROM   payroll.payrolldraftlines dl
                WHERE  dl.payrollperiodid = :pid
                  AND  dl.workdate        = :dt
                  AND  dl.driverid        IN ({in_clause})
                  AND  dl.status         != 'Void'
            """),
            {"pid": period_id, "dt": work_date, **in_params},
        )
        for row in dl_result.mappings().all():
            did = row["driverid"]
            if did in lines_by_driver:
                lines_by_driver[did].append(dict(row))

    # ── CP-2D1: batch-load canonical entry-state rows for this date ───────── #
    canonical_by_driver: dict[int, dict] = {}
    if driver_ids:
        in_clause_ces, in_params_ces = _build_in_clause(driver_ids, "ces_drv")
        ces_result = await db.execute(
            text(f"""
                SELECT driverid, statuskeyid, notetext,
                       statuscodesnapshot, statuslabelsnapshot,
                       statusisoffreasonsnapshot, finalizedatutc
                FROM   payroll.payrollperioddriverdayentrystate
                WHERE  payrollperiodid = :pid
                  AND  workdate        = :dt
                  AND  driverid        IN ({in_clause_ces})
                  AND  isvoided          = FALSE
            """),
            {"pid": period_id, "dt": work_date, **in_params_ces},
        )
        for row in ces_result.mappings().all():
            canonical_by_driver[row["driverid"]] = dict(row)

    # Reverse lookup: StatusKeyID → DayGridStatusKey (for editable canonical path).
    status_key_id_map: dict[int, DayGridStatusKey] = {sk.status_key_id: sk for sk in status_keys}

    # Pre-load any deactivated keys referenced in canonical rows for editable periods.
    # (Deactivated keys are absent from status_key_id_map; fetch them in one batch.)
    deactivated_key_map: dict[int, dict] = {}
    deactivated_sk_ids = {
        row["statuskeyid"]
        for row in canonical_by_driver.values()
        if row.get("finalizedatutc") is None
        and row.get("statuskeyid") is not None
        and row["statuskeyid"] not in status_key_id_map
    }
    if deactivated_sk_ids:
        in_clause_dk, in_params_dk = _build_in_clause(list(deactivated_sk_ids), "dkid")
        dk_result = await db.execute(
            text(f"""
                SELECT statuskeyid, statuscode, keyname, isoffreason
                FROM   payroll.payrollstatuskeys
                WHERE  statuskeyid IN ({in_clause_dk})
            """),
            in_params_dk,
        )
        for row in dk_result.mappings().all():
            deactivated_key_map[row["statuskeyid"]] = dict(row)

    # ── Stage B3 Unit 8C-3: immutable Status evidence for Locked/Archived ──── #
    # After Submit, immutable calculation-snapshot Status evidence is the
    # historical authority for finalized periods. Locked/Archived rows below
    # must not use the EntryState freeze columns (StatusCodeSnapshot/
    # StatusLabelSnapshot/StatusIsOffReasonSnapshot/FinalizedAtUtc), current
    # mutable PayrollStatusKeys, or legacy DraftLine Status codes to determine
    # historical Status meaning. Draft/Open/InReview/Returned/Approved periods
    # are unaffected -- this block only runs for Locked/Archived.
    _is_finalized_period = period.status in ("Locked", "Archived")
    _status_evidence_by_driver: dict[int, dict[str, Any]] = {}
    _status_evidence_state: dict[str, str | None] | None = None
    # Set once evidence resolution below concludes UNAVAILABLE -- guards the
    # worked/off tally further down so an unreadable historical Status never
    # gets silently counted as "worked" (see the tally comment for why EMPTY
    # does not need the same guard).
    _status_evidence_unavailable = False
    if _is_finalized_period:
        _fin_snapshot, _fin_snapshot_avail = await status_evidence.resolve_finalized_snapshot(
            db, period_id=period.payroll_period_id, company_id=company_id, branch_id=branch_id,
        )
        if _fin_snapshot is None:
            # No usable snapshot provenance -- never fall back to another
            # snapshot or to mutable current state; evidence is unavailable.
            _status_evidence_state = _fin_snapshot_avail
        else:
            _fin_status_entries = await status_evidence.read_status_entries(
                db,
                snapshot_id=_fin_snapshot["payrollcalculationsnapshotid"],
                company_id=company_id,
                branch_id=branch_id,
                period_id=period.payroll_period_id,
            )
            _status_evidence_state = status_evidence.status_evidence_availability(
                _fin_snapshot, _fin_status_entries,
            )
            _status_evidence_by_driver = {
                entry["driver_id"]: entry
                for entry in _fin_status_entries
                if entry["work_date"] == work_date
            }
        _status_evidence_unavailable = _status_evidence_state["state"] == "UNAVAILABLE"

    # ── Build rows ────────────────────────────────────────────────────────── #
    col_codes = {c.pay_item_code for c in columns}
    rows: list[DayGridRow] = []
    total_hours = Decimal("0")
    total_miles = Decimal("0")
    gross_total = Decimal("0")
    needs_attention = 0
    worked_count = 0
    pto_count = 0
    off_count = 0

    for drv in drivers:
        did = drv["driverid"]
        drv_lines = lines_by_driver.get(did, [])

        values: dict[str, DayGridLineValue] = {}
        status_key_code: str | None = None
        notes_text: str | None = None
        sk_label: str | None = None
        is_off: bool = False

        # CP-2D1: canonical-first per-driver status/note read.
        # Falls back to DraftLines when no canonical row exists (legacy periods).
        ces_row = canonical_by_driver.get(did)
        if ces_row is not None:
            notes_text = ces_row.get("notetext")
            # Stage B3 Unit 8C-3: for Locked/Archived, Status meaning comes
            # only from immutable snapshot evidence (applied further below) --
            # never from these EntryState freeze columns or the live StatusKey.
            if not _is_finalized_period:
                if ces_row["finalizedatutc"] is not None:
                    # Locked period (pre-Solution-B path): use frozen snapshot values.
                    status_key_code = ces_row["statuscodesnapshot"]
                    sk_label = ces_row["statuslabelsnapshot"]
                    snap_off = ces_row["statusisoffreasonsnapshot"]
                    is_off = bool(snap_off) if snap_off is not None else False
                else:
                    # Editable period: live label/flags via StatusKeyID.
                    sk_id = ces_row.get("statuskeyid")
                    if sk_id is not None:
                        live_sk = status_key_id_map.get(sk_id)
                        if live_sk is not None:
                            status_key_code = live_sk.key_code
                            sk_label = live_sk.label
                            is_off = bool(live_sk.is_off_reason)
                        else:
                            dk = deactivated_key_map.get(sk_id)
                            if dk:
                                status_key_code = dk["statuscode"]
                                sk_label = dk["keyname"]
                                is_off = bool(dk["isoffreason"])

        for line in drv_lines:
            lt = line["linetype"]
            canonical = _LEGACY_TO_CANONICAL.get(lt, lt)

            if lt == "DailyStatus":
                if ces_row is None and not _is_finalized_period:
                    # Legacy fallback: status code stored in DraftLine notes.
                    # Not used for Locked/Archived -- see Stage B3 Unit 8C-3
                    # note above: legacy DraftLine Status must not be used to
                    # fabricate historical meaning.
                    status_key_code = line["notes"]
                continue
            if lt == "DailyNote":
                if ces_row is None:
                    # Legacy fallback: note text stored in DraftLine notes.
                    notes_text = line["notes"]
                continue

            if canonical in col_codes:
                qty = line["quantity"]
                calc = line["calculatedamount"]
                nmr = bool(line["needsmanagerreview"])
                values[canonical] = DayGridLineValue(
                    line_id=line["draftlineid"],
                    quantity=str(qty) if qty is not None else None,
                    calculated_amount=str(calc) if calc is not None else None,
                    needs_manager_review=nmr,
                )
                if nmr:
                    needs_attention += 1
                if canonical == "HOURS" and qty:
                    total_hours += Decimal(str(qty))
                if canonical == "MILES" and qty:
                    total_miles += Decimal(str(qty))
                if calc:
                    gross_total += Decimal(str(calc))

        # Legacy path: resolve label/is_off from status_key_map when no canonical row.
        if not _is_finalized_period and ces_row is None and status_key_code:
            sk_obj = status_key_map.get(status_key_code)
            if sk_obj is not None:
                sk_label = sk_obj.label
                is_off = bool(sk_obj.is_off_reason)

        # Stage B3 Unit 8C-3: for Locked/Archived, override whatever the
        # blocks above computed (they are guarded off above, but this stays
        # authoritative even if that guarding is ever loosened) -- Status
        # meaning comes only from immutable snapshot evidence for this
        # specific work_date, or is left unknown (None/False) when no
        # evidence entry exists for this driver/day, regardless of whether
        # the overall evidence state is AVAILABLE, EMPTY, or UNAVAILABLE.
        if _is_finalized_period:
            evidence_row = _status_evidence_by_driver.get(did)
            if evidence_row is not None:
                status_key_code = evidence_row["status_code"]
                sk_label = evidence_row["status_label"]
                is_off = bool(evidence_row["is_off_reason"])
            else:
                status_key_code = None
                sk_label = None
                is_off = False

        # Stage B3 Unit 8C-3 follow-up: when the overall Status evidence for
        # this Locked/Archived period is UNAVAILABLE, every row's is_off
        # above is a default (False), not a historical fact -- tallying it
        # into worked/off would silently present an unknown historical
        # Status as a confirmed "worked" day. Skip the tally entirely in
        # that case (leaves worked=off=0, distinguishable from a real
        # all-worked day via the top-level status_evidence field). EMPTY
        # does not need this guard: a captured snapshot with zero Status
        # rows is a positive historical fact that nobody had an off/PTO
        # Status that period, matching the same "no Status recorded means
        # worked" rule already applied to every other period status here.
        if _status_evidence_unavailable:
            pass
        elif is_off:
            off_count += 1
        else:
            worked_count += 1

        rows.append(DayGridRow(
            driver_id=did,
            driver_name=drv["drivername"],
            driver_code=drv.get("drivercode"),
            status_key=status_key_code,
            status_label=sk_label,
            is_off=is_off,
            notes=notes_text,
            values=values,
        ))

    # CP-2F: suppress gross_total for Draft (Prepared) periods — financials not available.
    _is_draft = period.status == "Draft"
    summary = DayGridSummary(
        total_drivers=len(drivers),
        worked=worked_count,
        pto=pto_count,
        off=off_count,
        total_hours=str(total_hours.quantize(Decimal("0.01"))),
        total_miles=str(total_miles.quantize(Decimal("0.01"))),
        gross_total=None if _is_draft else str(gross_total.quantize(Decimal("0.01"))),
        needs_attention=needs_attention,
        financials_available=not _is_draft,
    )

    grid_period = DayGridPeriod(
        period_id=period.payroll_period_id,
        period_name=period.period_name,
        start_date=period.start_date,
        end_date=period.end_date,
        pay_date=period.pay_date,
        status=period.status,
        branch_id=period.branch_id,
        branch_name=period.branch_name,
    )

    return DayGridResponse(
        period=grid_period,
        work_date=work_date,
        columns=columns,
        status_keys=status_keys,
        rows=rows,
        summary=summary,
        status_evidence=_status_evidence_state,
    )


async def save_day_grid(
    period_id: int,
    company_id: int,
    user_id: int,
    data: DayGridSaveRequest,
    db: AsyncConnection,
) -> DayGridResponse:
    """
    Batch-save a day grid.  For each row:
      - Upsert pay item lines (create / update / void-zero)
      - Upsert DailyStatus line (status_key stored in Notes)
      - Upsert DailyNote line  (notes text)

    All operations share the caller's transaction (no nested BEGIN).
    Returns the refreshed day-grid response.
    """
    # ── ODA / Driver-role guard ───────────────────────────────────────────── #
    # Same as get_day_grid: OwnDriverDataOnly users are unconditionally blocked
    # from writing to the day grid.  They must not be able to modify any payroll
    # data — not their own row, not anyone else's.
    own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)
    if own_driver_id is not None:
        raise HTTPException(
            status_code=403,
            detail="Current Payroll is not accessible to driver-role users.",
        )

    # ── Load and access-check the period ─────────────────────────────────── #
    period = await get_period_by_id(company_id, user_id, period_id, db)

    # CP-2F: Draft (Prepared) periods support operational day-grid entry.
    if period.status not in SOURCE_ENTRY_STATUSES:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Period is not editable (status: '{period.status}'). "
                "Only Open, Returned, or Prepared (Draft) periods accept entry."
            ),
        )

    work_date = data.work_date

    # CP-2B: snapshot-aware date validation
    await _validate_period_work_date(
        period.payroll_period_id, work_date,
        period.start_date, period.end_date, db,
    )

    # ── Permission: payroll.entry ─────────────────────────────────────────── #
    await _check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    branch_id = period.branch_id

    # ── Load active Daily columns for this branch/date ───────────────────── #
    # CP-2C: snapshot-first. Post-0053 periods use PayrollPeriodPayItems;
    # legacy periods fall back to live BranchPayItemConfig.
    # Use _period_has_pay_item_snapshot so a post-0053 period with zero active
    # Daily rows doesn't fall back to live config (an empty active set is the
    # correct answer — no codes should pass the validate step).
    if await _period_has_pay_item_snapshot(period.payroll_period_id, db):
        snap_active = await _get_period_pay_item_snapshot(
            period.payroll_period_id, company_id, db, scope="Daily", active_only=True,
        )
        active_col_codes: set[str] = {
            _LEGACY_TO_CANONICAL.get(r["payitemcode"], r["payitemcode"])
            for r in snap_active
        }
    else:
        _active_cols_result = await db.execute(
            text("""
                SELECT pi.payitemcode
                FROM   payroll.payitems pi
                LEFT JOIN payroll.branchpayitemconfig bpic
                       ON bpic.payitemid  = pi.payitemid
                      AND bpic.companyid  = :cid
                      AND bpic.branchid   = :bid
                      AND bpic.effectivefrom <= :dt
                      AND (bpic.effectiveto IS NULL OR bpic.effectiveto >= :dt)
                WHERE  (pi.companyid IS NULL OR pi.companyid = :cid)
                  AND  pi.itemscope  = 'Daily'
                  AND  pi.status    != 'Retired'
                  AND  COALESCE(bpic.isactive, pi.isdefaultbranchactive) = TRUE
            """),
            {"cid": company_id, "bid": branch_id, "dt": work_date},
        )
        active_col_codes: set[str] = {
            _LEGACY_TO_CANONICAL.get(r["payitemcode"], r["payitemcode"])
            for r in _active_cols_result.mappings().all()
        }

    # ── Phase 1: validate ALL inputs before any DB writes ────────────────── #
    # P1 #2: strict quantity parsing (non-numeric → 422 before writes)
    # P1 #3: status key validation (invalid/inactive → 422 before writes)
    # This preserves all-or-nothing atomicity.

    # Each row: (save_row, driver_id, parsed_values, validated_status_key, key_row)
    parsed_rows: list[tuple] = []

    for save_row in data.rows:
        driver_id = save_row.driver_id

        # CP-2E: use snapshot-based eligibility when available; legacy fallback otherwise.
        try:
            await _assert_driver_eligible_for_workdate_via_snapshot(
                company_id, branch_id, period.payroll_period_id, driver_id, work_date, db
            )
        except HTTPException:
            raise HTTPException(
                status_code=422,
                detail=f"Driver {driver_id} is not eligible for this branch or work date.",
            )

        # P1 #2: parse all quantities; reject unknown or branch-inactive codes
        # before any writes so the batch is rejected atomically.
        parsed_values: dict[str, "Decimal | None"] = {}
        for pay_item_code, raw_val in save_row.values.items():
            canonical = _LEGACY_TO_CANONICAL.get(pay_item_code, pay_item_code)
            if canonical not in active_col_codes:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Pay item '{canonical}' is not an active daily column "
                        "for this period branch and date."
                    ),
                )
            parsed_values[canonical] = _parse_quantity(raw_val, canonical)

        # P1 #3: validate status key — raises 422 for invalid/inactive.
        # Returns the full key row (with limit fields) or None when clearing.
        # CP-2D1: if the submitted code matches the existing DraftLine value,
        # the user is not changing the status — allow saves of deactivated keys
        # that were applied before the key was deactivated.
        allow_deactivated = False
        if save_row.status_key:
            existing_sk_q = await db.execute(
                text("""
                    SELECT notes FROM payroll.payrolldraftlines
                    WHERE  payrollperiodid = :pid AND workdate = :dt AND driverid = :did
                      AND  linetype = 'DailyStatus' AND status != 'Void'
                    LIMIT 1
                """),
                {"pid": period_id, "dt": work_date, "did": driver_id},
            )
            existing_sk_row = existing_sk_q.mappings().first()
            if (
                existing_sk_row is not None
                and existing_sk_row["notes"] == save_row.status_key.strip()
            ):
                allow_deactivated = True
        key_row = await _validate_status_key(
            save_row.status_key, company_id, branch_id, db,
            allow_deactivated=allow_deactivated,
        )
        validated_status_key = save_row.status_key  # None/blank = clear

        parsed_rows.append((save_row, driver_id, parsed_values, validated_status_key, key_row))

    # ── Phase 1b: status key usage limit enforcement ──────────────────── #
    # All existence/active checks passed. Now enforce configured usage limits
    # batch-wide so that intra-batch writes that would collectively exceed a
    # limit are caught before any DB write happens (all-or-nothing atomicity).
    all_batch_pairs: list[tuple] = [(r.driver_id, work_date) for r in data.rows]
    batch_by_code: dict[str, list[int]] = {}
    for _, drv_id, _, vsk, _ in parsed_rows:
        if vsk:
            batch_by_code.setdefault(vsk, []).append(drv_id)

    for status_code, batch_driver_ids in batch_by_code.items():
        code_key_row = next(
            krow
            for _, _, _, vsk, krow in parsed_rows
            if vsk == status_code and krow is not None
        )
        await _enforce_status_key_limits(
            status_code=status_code,
            key_row=code_key_row,
            period_id=period_id,
            company_id=company_id,
            work_date=work_date,
            batch_driver_ids=batch_driver_ids,
            all_batch_pairs=all_batch_pairs,
            db=db,
        )

    # ── Phase 2: execute DB writes (all validations passed) ──────────────── #
    # CP-0A lock ordering: pre-lock ALL distinct custom PayItems in the batch in
    # sorted (deterministic) order BEFORE acquiring the Period lock.
    #
    # Without batch pre-locking, a multi-item transaction can interleave:
    #   save_day_grid: PayItem A → Period → (tries) PayItem B
    #   deletion:      PayItem B → (tries) Period
    # → deadlock.
    #
    # With batch pre-locking the order is always:
    #   PayItem codes (sorted) → Period
    # which matches the deletion path (PayItem → Period), so no deadlock is possible.
    #
    # Re-locking an already-held row in the same transaction is a no-op in
    # PostgreSQL, so the per-line calls inside add_draft_line / update_draft_line
    # are safe duplicates of these batch locks.
    all_canonical_codes: set[str] = set()
    for _, _, pv, _, _ in parsed_rows:
        all_canonical_codes.update(pv.keys())
    all_canonical_codes.discard("DailyStatus")
    all_canonical_codes.discard("DailyNote")
    for code in sorted(all_canonical_codes):
        await _lock_pay_item_for_source_write(code, company_id, db, period_id=period.payroll_period_id)

    for save_row, driver_id, parsed_values, validated_status_key, key_row in parsed_rows:

        # ── Pay item lines ────────────────────────────────────────────────── #
        for canonical, qty in parsed_values.items():
            # Treat None (empty/blank) as zero (clear)
            effective_qty: Decimal = qty if qty is not None else Decimal("0")

            # P1 #5: look for existing line using canonical + all legacy aliases
            aliases = _canonical_aliases(canonical)
            lt_keys = {f"lt{i}": v for i, v in enumerate(aliases)}
            lt_in_clause = ", ".join(f":{k}" for k in lt_keys)
            existing_result = await db.execute(
                text(f"""
                    SELECT draftlineid, linetype FROM payroll.payrolldraftlines
                    WHERE  payrollperiodid = :pid
                      AND  workdate        = :dt
                      AND  driverid        = :did
                      AND  linetype        IN ({lt_in_clause})
                      AND  status         != 'Void'
                """),
                {"pid": period_id, "dt": work_date, "did": driver_id, **lt_keys},
            )
            existing_rows = existing_result.mappings().all()

            # P1 #5: detect duplicates (both "Hours" and "HOURS" rows exist)
            if len(existing_rows) > 1:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Duplicate payroll lines found for '{canonical}' "
                        f"(driver {driver_id}, {work_date}). Contact admin to resolve."
                    ),
                )
            existing = existing_rows[0] if existing_rows else None

            if effective_qty == 0 and existing:
                # P1 #4: route void through void_draft_line to write audit
                await void_draft_line(
                    period_id=period_id,
                    draft_line_id=existing["draftlineid"],
                    company_id=company_id,
                    user_id=user_id,
                    db=db,
                )
            elif effective_qty == 0 and not existing:
                # Skip: don't create zero rows
                continue
            elif effective_qty != 0 and not existing:
                # Create new line via add_draft_line (writes audit)
                await add_draft_line(
                    period_id=period_id,
                    company_id=company_id,
                    user_id=user_id,
                    data=DraftLineCreate(
                        driver_id=driver_id,
                        work_date=work_date,
                        line_type=canonical,
                        quantity=effective_qty,
                        source_type="Manual",
                    ),
                    db=db,
                )
            else:
                # Update existing line qty (writes audit)
                await update_draft_line(
                    period_id=period_id,
                    draft_line_id=existing["draftlineid"],
                    company_id=company_id,
                    user_id=user_id,
                    data=DraftLineUpdate(quantity=effective_qty),
                    db=db,
                )

        # ── Period lock for DailyStatus / DailyNote writes ───────────────── #
        # CP-0A: If this row had no pay-item writes (empty parsed_values or all
        # zeroes/voids), add_draft_line was never called so the period lock has
        # not been acquired yet.  Lock now before any DML.  If the period lock
        # was already acquired by a preceding add_draft_line call this is a
        # no-op (same transaction already holds the lock).
        await _lock_period_for_mutation(period_id, company_id, db)

        # ── DailyStatus upsert ────────────────────────────────────────────── #
        # P1 #4: write audit via _write_line_audit for all DailyStatus mutations
        status_val = validated_status_key  # None = clear
        existing_status = await db.execute(
            text("""
                SELECT draftlineid FROM payroll.payrolldraftlines
                WHERE  payrollperiodid = :pid
                  AND  workdate        = :dt
                  AND  driverid        = :did
                  AND  linetype        = 'DailyStatus'
                  AND  status         != 'Void'
                LIMIT 1
            """),
            {"pid": period_id, "dt": work_date, "did": driver_id},
        )
        es_row = existing_status.mappings().first()

        if status_val:
            if es_row:
                # Update notes (status code stored in notes)
                upd = await db.execute(
                    text(
                        "UPDATE payroll.payrolldraftlines SET notes = :n "
                        "WHERE draftlineid = :lid "
                        "  AND payrollperiodid = :period_id AND companyid = :company_id"
                    ),
                    {
                        "n": status_val,
                        "lid": es_row["draftlineid"],
                        "period_id": period_id,
                        "company_id": company_id,
                    },
                )
                if upd.rowcount:
                    await _write_line_audit(
                        db,
                        company_id=company_id,
                        branch_id=branch_id,
                        user_id=user_id,
                        line_id=es_row["draftlineid"],
                        action_code="DRAFT_LINE_UPDATED",
                        new_value={"status_key": status_val},
                    )
            else:
                # Insert new DailyStatus line
                ins_result = await db.execute(
                    text("""
                        INSERT INTO payroll.payrolldraftlines
                            (companyid, branchid, payrollperiodid, driverid,
                             workdate, linetype, linescope, quantity,
                             sourcetype, status, needsmanagerreview, notes, addedbyuserid)
                        VALUES
                            (:cid, :bid, :pid, :did,
                             :dt, 'DailyStatus', 'Daily', 0,
                             'Manual', 'Active', FALSE, :n, :uid)
                        RETURNING draftlineid
                    """),
                    {
                        "cid": company_id, "bid": branch_id, "pid": period_id,
                        "did": driver_id, "dt": work_date,
                        "n": status_val, "uid": user_id,
                    },
                )
                new_lid = ins_result.scalar_one()
                await _write_line_audit(
                    db,
                    company_id=company_id,
                    branch_id=branch_id,
                    user_id=user_id,
                    line_id=new_lid,
                    action_code="DRAFT_LINE_ADDED",
                    new_value={"line_type": "DailyStatus", "status_key": status_val},
                )
        elif es_row:
            # Clear status: void the line (with audit)
            upd = await db.execute(
                text(
                    "UPDATE payroll.payrolldraftlines SET status = 'Void' "
                    "WHERE draftlineid = :lid "
                    "  AND payrollperiodid = :period_id AND companyid = :company_id"
                ),
                {
                    "lid": es_row["draftlineid"],
                    "period_id": period_id,
                    "company_id": company_id,
                },
            )
            if upd.rowcount:
                await _write_line_audit(
                    db,
                    company_id=company_id,
                    branch_id=branch_id,
                    user_id=user_id,
                    line_id=es_row["draftlineid"],
                    action_code="DRAFT_LINE_VOIDED",
                    old_value={"line_type": "DailyStatus"},
                    new_value={"status": "Void"},
                )

        # ── DailyNote upsert ──────────────────────────────────────────────── #
        # P1 #4: write audit via _write_line_audit for all DailyNote mutations
        notes_val = save_row.notes  # may be None or ''
        existing_note = await db.execute(
            text("""
                SELECT draftlineid FROM payroll.payrolldraftlines
                WHERE  payrollperiodid = :pid
                  AND  workdate        = :dt
                  AND  driverid        = :did
                  AND  linetype        = 'DailyNote'
                  AND  status         != 'Void'
                LIMIT 1
            """),
            {"pid": period_id, "dt": work_date, "did": driver_id},
        )
        en_row = existing_note.mappings().first()

        if notes_val:
            if en_row:
                upd = await db.execute(
                    text(
                        "UPDATE payroll.payrolldraftlines SET notes = :n "
                        "WHERE draftlineid = :lid "
                        "  AND payrollperiodid = :period_id AND companyid = :company_id"
                    ),
                    {
                        "n": notes_val,
                        "lid": en_row["draftlineid"],
                        "period_id": period_id,
                        "company_id": company_id,
                    },
                )
                if upd.rowcount:
                    await _write_line_audit(
                        db,
                        company_id=company_id,
                        branch_id=branch_id,
                        user_id=user_id,
                        line_id=en_row["draftlineid"],
                        action_code="DRAFT_LINE_UPDATED",
                        new_value={"notes": notes_val},
                    )
            else:
                ins_result2 = await db.execute(
                    text("""
                        INSERT INTO payroll.payrolldraftlines
                            (companyid, branchid, payrollperiodid, driverid,
                             workdate, linetype, linescope, quantity,
                             sourcetype, status, needsmanagerreview, notes, addedbyuserid)
                        VALUES
                            (:cid, :bid, :pid, :did,
                             :dt, 'DailyNote', 'Daily', 0,
                             'Manual', 'Active', FALSE, :n, :uid)
                        RETURNING draftlineid
                    """),
                    {
                        "cid": company_id, "bid": branch_id, "pid": period_id,
                        "did": driver_id, "dt": work_date,
                        "n": notes_val, "uid": user_id,
                    },
                )
                new_lid2 = ins_result2.scalar_one()
                await _write_line_audit(
                    db,
                    company_id=company_id,
                    branch_id=branch_id,
                    user_id=user_id,
                    line_id=new_lid2,
                    action_code="DRAFT_LINE_ADDED",
                    new_value={"line_type": "DailyNote", "notes": notes_val},
                )
        elif en_row:
            upd = await db.execute(
                text(
                    "UPDATE payroll.payrolldraftlines SET status = 'Void' "
                    "WHERE draftlineid = :lid "
                    "  AND payrollperiodid = :period_id AND companyid = :company_id"
                ),
                {
                    "lid": en_row["draftlineid"],
                    "period_id": period_id,
                    "company_id": company_id,
                },
            )
            if upd.rowcount:
                await _write_line_audit(
                    db,
                    company_id=company_id,
                    branch_id=branch_id,
                    user_id=user_id,
                    line_id=en_row["draftlineid"],
                    action_code="DRAFT_LINE_VOIDED",
                    old_value={"line_type": "DailyNote"},
                    new_value={"status": "Void"},
                )

        # CP-2D1: upsert canonical entry-state after both DailyStatus/DailyNote writes.
        await _upsert_entry_state(
            company_id, branch_id, period_id, driver_id, work_date, user_id, db,
            status_key_id=key_row["statuskeyid"] if key_row else None,
            note_text=notes_val if notes_val else None,
        )

        # CP-2D2: sync STATUS_PAYMENT draft line from the updated entry state.
        # CP-2F: Draft (Prepared) is source-only — skip money derivation.
        if period.status == "Draft":
            continue
        await _sync_status_payment_for_entry_state(
            company_id=company_id,
            branch_id=branch_id,
            period_id=period_id,
            driver_id=driver_id,
            work_date=work_date,
            status_key_id=key_row["statuskeyid"] if key_row else None,
            user_id=user_id,
            db=db,
        )

    # Return the refreshed grid
    return await get_day_grid(
        period_id=period_id,
        company_id=company_id,
        user_id=user_id,
        work_date=data.work_date,
        db=db,
    )


# =============================================================================
# CP-1C: Branch-locked candidate-based period creation
# =============================================================================

_CP1C_VERSION = "cp1c-v1"
_CP1C_PURPOSE = "period_creation"
_CP1C_MAX_FUTURE = 12

# Active slot statuses that govern mode-eligibility checks:
_ACTIVE_SLOT_STATUSES = frozenset({"Draft", "Open", "InReview", "Returned"})


# ---------------------------------------------------------------------------
# Helpers: HMAC signing / verification
# ---------------------------------------------------------------------------

def _make_candidate_key(payload: dict) -> tuple[str, str]:
    """
    Sign payload and return (candidate_key, candidate_hash).
    candidate_key  = base64url(canonical_json).hmac_sha256_hex
    candidate_hash = hmac_sha256_hex (64 hex chars stored in DB)
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    b64_part = base64.urlsafe_b64encode(canonical.encode()).decode().rstrip("=")
    sig = _hmac_mod.new(
        settings.SECRET_KEY.encode(),
        b64_part.encode(),
        "sha256",
    ).hexdigest()
    return f"{b64_part}.{sig}", sig


def _decode_candidate_key(key: str) -> tuple[dict, str]:
    """
    Decode and verify a candidate_key.
    Returns (payload_dict, hmac_hex).
    Raises HTTPException 409 INVALID_CANDIDATE_KEY on any failure.
    """
    try:
        b64_part, sig = key.rsplit(".", 1)
    except (ValueError, AttributeError):
        _cp1c_error("INVALID_CANDIDATE_KEY", "Malformed candidate key.")

    expected = _hmac_mod.new(
        settings.SECRET_KEY.encode(),
        b64_part.encode(),
        "sha256",
    ).hexdigest()
    if not _hmac_mod.compare_digest(expected, sig):
        _cp1c_error("INVALID_CANDIDATE_KEY", "Candidate key signature invalid.")

    try:
        padding = (4 - len(b64_part) % 4) % 4
        payload = json.loads(
            base64.urlsafe_b64decode(b64_part + "=" * padding).decode()
        )
    except Exception:
        _cp1c_error("INVALID_CANDIDATE_KEY", "Candidate key payload unreadable.")

    if payload.get("ver") != _CP1C_VERSION or payload.get("purpose") != _CP1C_PURPOSE:
        _cp1c_error("INVALID_CANDIDATE_KEY", "Wrong version or purpose in candidate key.")

    return payload, sig


def _cp1c_error(code: str, message: str, http_status: int = 409) -> None:
    raise HTTPException(
        status_code=http_status,
        detail={"code": code, "message": message},
    )


# ---------------------------------------------------------------------------
# Helpers: fingerprints
# ---------------------------------------------------------------------------

def _setup_fingerprint(freq: str, anchor: date, interval_days: int | None) -> str:
    return json.dumps(
        {"anchor": str(anchor), "freq": freq, "interval": interval_days},
        sort_keys=True,
        separators=(",", ":"),
    )


def _slot_fingerprint(periods: list[dict]) -> str:
    """Deterministic fingerprint of all non-Cancelled periods (sorted status+id pairs)."""
    pairs = sorted(
        [(r["status"], r["payrollperiodid"])
         for r in periods
         if r["status"] != "Cancelled"],
        key=lambda x: (x[0], x[1]),
    )
    return json.dumps(pairs, separators=(",", ":"))


# ---------------------------------------------------------------------------
# CP-2A: ensure current schedule version
# ---------------------------------------------------------------------------

async def ensure_current_schedule_version(
    company_id: int,
    branch_id: int,
    user_id: int | None,
    db: AsyncConnection,
) -> int | None:
    """
    Return the current ScheduleVersionID for this branch.

    Must be called while holding the branch workflow advisory lock.

    If BranchPayrollSettings.CurrentScheduleVersionID is already set and the
    referenced version row exists for the correct company/branch, return it.

    If it is NULL (e.g. setup pre-dates CP-2A migration and the backfill missed
    this row, which should not happen but is handled defensively), create a new
    repair version (SourceAction='REPAIR') and update CurrentScheduleVersionID.

    Returns None if there is no active setup row at all. Callers must treat
    None as PAYROLL_SETUP_REQUIRED — the same condition as a missing setup.

    Does not create versions for missing/inactive setup; period creation paths
    must reject PAYROLL_SETUP_REQUIRED before calling this.
    """
    setup_row = (await db.execute(
        text("""
            SELECT payrollfrequency, anchorstartdate, customintervaldays,
                   normaldaysoffmask, paydayofweek, firstpaydate,
                   includepaydayasworkday, currentscheduleversionid
            FROM   payroll.branchpayrollsettings
            WHERE  branchid = :bid AND companyid = :cid AND isactive = TRUE
        """),
        {"bid": branch_id, "cid": company_id},
    )).mappings().first()

    if setup_row is None:
        return None

    sv_id = setup_row.get("currentscheduleversionid")

    if sv_id is not None:
        # Verify the referenced version exists for this company/branch.
        exists = (await db.execute(
            text("""
                SELECT 1 FROM payroll.PayrollScheduleVersions
                WHERE scheduleversionid = :sv_id
                  AND companyid = :cid AND branchid = :bid
            """),
            {"sv_id": sv_id, "cid": company_id, "bid": branch_id},
        )).first()
        if exists is not None:
            return sv_id

    # CurrentScheduleVersionID is missing or points to a stale/wrong row.
    # Create a repair version from the current live setup values.
    freq = setup_row["payrollfrequency"]
    anchor = setup_row["anchorstartdate"]
    interval_days = setup_row.get("customintervaldays")
    config_hash = json.dumps(
        {"anchor": str(anchor), "freq": freq, "interval": interval_days},
        sort_keys=True,
        separators=(",", ":"),
    )

    max_row = await db.execute(
        text("""
            SELECT COALESCE(MAX(versionnumber), 0) AS maxver
            FROM   payroll.PayrollScheduleVersions
            WHERE  companyid = :cid AND branchid = :bid
        """),
        {"cid": company_id, "bid": branch_id},
    )
    next_version = (max_row.scalar_one() or 0) + 1

    ins = await db.execute(
        text("""
            INSERT INTO payroll.PayrollScheduleVersions
                (CompanyID, BranchID, VersionNumber,
                 PayrollFrequency, AnchorStartDate,
                 CustomIntervalDays, NormalDaysOffMask,
                 PayDayOfWeek, FirstPayDate, IncludePayDayAsWorkDay,
                 EffectiveFromDate, EffectiveToDate,
                 CreatedByUserID, SourceAction, ConfigHash)
            VALUES
                (:cid, :bid, :vnum,
                 :freq, :anchor,
                 :interval_days, :mask,
                 :pdow, :fpd, :incl,
                 :anchor, NULL,
                 :uid, 'REPAIR', :chash)
            RETURNING ScheduleVersionID
        """),
        {
            "cid":           company_id,
            "bid":           branch_id,
            "vnum":          next_version,
            "freq":          freq,
            "anchor":        anchor,
            "interval_days": interval_days,
            "mask":          setup_row.get("normaldaysoffmask"),
            "pdow":          setup_row.get("paydayofweek"),
            "fpd":           setup_row.get("firstpaydate"),
            "incl":          bool(setup_row.get("includepaydayasworkday") or False),
            "uid":           user_id,
            "chash":         config_hash,
        },
    )
    new_sv_id: int = ins.scalar_one()

    await db.execute(
        text("""
            UPDATE payroll.BranchPayrollSettings
            SET    CurrentScheduleVersionID = :sv_id
            WHERE  CompanyID = :cid AND BranchID = :bid
        """),
        {"sv_id": new_sv_id, "cid": company_id, "bid": branch_id},
    )

    return new_sv_id


# ---------------------------------------------------------------------------
# Helper: advisory lock
# ---------------------------------------------------------------------------

async def _acquire_branch_workflow_lock(
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> None:
    """
    Acquire a transaction-level advisory lock for branch period creation.
    Released automatically when the transaction commits or rolls back.
    Idempotent within the same transaction.
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:cid, :bid)"),
        {"cid": company_id, "bid": branch_id},
    )


# ---------------------------------------------------------------------------
# Helper: slot matrix
# ---------------------------------------------------------------------------

def _check_slot_matrix(
    mode: str,
    periods: list[dict],
) -> tuple[bool, str | None]:
    """
    Apply the mode/slot matrix.
    Returns (creatable, error_code | None).
    """
    counts: dict[str, int] = {}
    for p in periods:
        s = p["status"]
        if s in _ACTIVE_SLOT_STATUSES:
            counts[s] = counts.get(s, 0) + 1

    for s, cnt in counts.items():
        if cnt > 1:
            return False, "SLOT_INVARIANT_VIOLATION"

    has_open = "Open" in counts
    has_draft = "Draft" in counts

    if mode == "OPEN_CREATION":
        if has_open and has_draft:
            return False, "ACTIVE_PERIOD_SLOTS_FULL"
        if has_open:
            return False, "OPEN_FILLED"
        if has_draft:
            return False, "DRAFT_WITHOUT_OPEN"
        return True, None

    if mode == "PREPARED_CREATION":
        if has_open and has_draft:
            return False, "ACTIVE_PERIOD_SLOTS_FULL"
        if has_draft and not has_open:
            return False, "DRAFT_WITHOUT_OPEN"
        if not has_open:
            return False, "OPEN_REQUIRED"
        return True, None

    _cp1c_error("INVALID_CANDIDATE_KEY", f"Unknown mode: {mode!r}.")
    return False, None  # unreachable


# ---------------------------------------------------------------------------
# Helper: candidate date computation at offset N
# ---------------------------------------------------------------------------

def _candidate_dates_at_offset(
    frequency: str,
    anchor: date,
    interval_days: int | None,
    pred_end: date | None,
    offset: int,
) -> tuple[date, date]:
    """Compute (start, end) for the candidate at position `offset` from current."""
    if pred_end is None:
        start = anchor
    else:
        start = max(anchor, pred_end + timedelta(days=1))

    end = _period_end(frequency, start, interval_days)

    for _ in range(offset):
        start = end + timedelta(days=1)
        end = _period_end(frequency, start, interval_days)

    return start, end


# ---------------------------------------------------------------------------
# CP-2B: Period-day snapshot helpers
# ---------------------------------------------------------------------------

def _generate_period_day_rows(
    start_date: date,
    end_date: date,
    normal_days_off_mask: int | None,
) -> list[dict]:
    """
    Return one dict per calendar day from start_date through end_date inclusive.

    DayOfWeek uses Sun=0 … Sat=6 to match NormalDaysOffMask bit positions
    (bit 0=Sun, bit 1=Mon, …, bit 6=Sat).

    Conversion from Python date.weekday() (Mon=0 … Sun=6):
        day_of_week = (python_weekday + 1) % 7
    """
    rows = []
    current = start_date
    while current <= end_date:
        py_wd = current.weekday()          # Mon=0 … Sun=6
        day_of_week = (py_wd + 1) % 7     # Sun=0 … Sat=6

        mask = normal_days_off_mask or 0
        is_configured_off = bool(mask & (1 << day_of_week))
        rows.append({
            "work_date":           current,
            "day_of_week":         day_of_week,
            "is_default_work_day": not is_configured_off,
            "is_configured_off_day": is_configured_off,
        })
        current += timedelta(days=1)
    return rows


async def _create_period_day_rows(
    period_id: int,
    company_id: int,
    branch_id: int,
    schedule_version_id: int,
    start_date: date,
    end_date: date,
    normal_days_off_mask: int | None,
    db: AsyncConnection,
) -> None:
    """
    Insert one PayrollPeriodDays row per calendar day for the given period.
    Called inside the period-creation transaction, still under the branch advisory lock.
    ON CONFLICT DO NOTHING ensures idempotency (candidate replay guard).
    Only called when schedule_version_id is not None.
    """
    day_rows = _generate_period_day_rows(start_date, end_date, normal_days_off_mask)
    if not day_rows:
        return

    values_sql = ", ".join(
        f"(:pid, :cid, :bid, :sv_id, :wd_{i}, :dow_{i}, :isd_{i}, :ico_{i})"
        for i in range(len(day_rows))
    )
    params: dict = {"pid": period_id, "cid": company_id, "bid": branch_id, "sv_id": schedule_version_id}
    for i, row in enumerate(day_rows):
        params[f"wd_{i}"]  = row["work_date"]
        params[f"dow_{i}"] = row["day_of_week"]
        params[f"isd_{i}"] = row["is_default_work_day"]
        params[f"ico_{i}"] = row["is_configured_off_day"]

    await db.execute(
        text(f"""
            INSERT INTO payroll.PayrollPeriodDays
                (PayrollPeriodID, CompanyID, BranchID, ScheduleVersionID,
                 WorkDate, DayOfWeek, IsDefaultWorkDay, IsConfiguredOffDay)
            VALUES {values_sql}
            ON CONFLICT (PayrollPeriodID, WorkDate) DO NOTHING
        """),
        params,
    )


async def _validate_period_work_date(
    period_id: int,
    work_date: date,
    start_date: date,
    end_date: date,
    db: AsyncConnection,
) -> None:
    """
    Validate that work_date is valid for a period.

    1. Existing StartDate/EndDate bounds check (always applied).
    2. If PayrollPeriodDays rows exist for the period, work_date must appear
       in the snapshot. Missing dates are rejected (400).
    3. Configured-off days (IsConfiguredOffDay=TRUE) are NOT rejected in CP-2B
       — IsScheduledWorkDay is metadata-only in this version.

    Legacy periods (created before 0052, no day rows) fall back to step 1 only.
    """
    if not (start_date <= work_date <= end_date):
        raise HTTPException(
            status_code=400,
            detail=(
                f"work_date {work_date} is outside the period range "
                f"({start_date} to {end_date})."
            ),
        )

    has_snapshot = (await db.execute(
        text("SELECT 1 FROM payroll.PayrollPeriodDays WHERE payrollperiodid = :pid LIMIT 1"),
        {"pid": period_id},
    )).first()

    if has_snapshot is not None:
        day_row = (await db.execute(
            text("""
                SELECT 1 FROM payroll.PayrollPeriodDays
                WHERE payrollperiodid = :pid AND workdate = :dt
            """),
            {"pid": period_id, "dt": work_date},
        )).first()
        if day_row is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"work_date {work_date} is not in the period day snapshot. "
                    "The requested date was not part of this period's calendar."
                ),
            )


# ---------------------------------------------------------------------------
# CP-2C: Period pay-item layout snapshot helpers
# ---------------------------------------------------------------------------

async def _create_period_pay_item_rows(
    period_id: int,
    company_id: int,
    branch_id: int,
    start_date: date,
    db: AsyncConnection,
) -> None:
    """
    Insert one PayrollPeriodPayItems row per non-Retired PayItem (system +
    company custom) as of start_date.

    Branch activation is resolved from BranchPayItemConfig using start_date.
    Both Daily and Period scope items are snapshotted.
    DailyStatus / DailyNote pseudo-lines are excluded (no PayItems catalog row).

    Called once at period creation; rows are immutable afterward.
    """
    items_result = await db.execute(
        text("""
            SELECT
                pi.payitemid,
                pi.payitemcode,
                pi.payitemname,
                pi.displaylabel,
                pi.category,
                pi.datatype,
                pi.unit,
                pi.itemscope,
                pi.ratebehavior,
                pi.appearsinpayrollentry,
                pi.appearsinledger,
                pi.appearsinreports,
                pi.requiresrate,
                pi.issystemstandard,
                (pi.companyid IS NOT NULL) AS iscustom,
                pi.status,
                pi.sortorder,
                pi.isdefaultbranchactive,
                bpic.isactive          AS cfg_isactive,
                bpic.effectivefrom     AS cfg_effectivefrom,
                bpic.configid          AS cfg_configid
            FROM payroll.payitems pi
            LEFT JOIN payroll.branchpayitemconfig bpic
                   ON bpic.payitemid      = pi.payitemid
                  AND bpic.companyid      = :cid
                  AND bpic.branchid       = :bid
                  AND bpic.effectivefrom <= :dt
                  AND (bpic.effectiveto IS NULL OR bpic.effectiveto >= :dt)
            WHERE (pi.companyid IS NULL OR pi.companyid = :cid)
              AND pi.status != 'Retired'
            ORDER BY pi.sortorder NULLS LAST, pi.payitemcode
        """),
        {"cid": company_id, "bid": branch_id, "dt": start_date},
    )
    rows = items_result.mappings().all()
    if not rows:
        return

    for r in rows:
        is_active_in_period = bool(
            r["cfg_isactive"] if r["cfg_isactive"] is not None else r["isdefaultbranchactive"]
        )
        await db.execute(
            text("""
                INSERT INTO payroll.payrollperiodpayitems
                    (payrollperiodid, companyid, branchid, payitemid,
                     payitemcode, payitemname, displaylabel,
                     category, datatype, unit,
                     itemscope, ratebehavior,
                     appearsinpayrollentry, appearsinledger, appearsinreports,
                     requiresrate, issystemstandard, iscustom,
                     payitemstatusatsnapshot, isactiveinperiod, sortorder,
                     snapshoteffectivefrom, sourcebranchpayitemconfigid,
                     createdatutc)
                VALUES
                    (:period_id, :cid, :bid, :payitemid,
                     :payitemcode, :payitemname, :displaylabel,
                     :category, :datatype, :unit,
                     :itemscope, :ratebehavior,
                     :appearsinpayrollentry, :appearsinledger, :appearsinreports,
                     :requiresrate, :issystemstandard, :iscustom,
                     :payitemstatus, :isactiveinperiod, :sortorder,
                     :snapshoteffectivefrom, :sourceconfigid,
                     NOW())
                ON CONFLICT (payrollperiodid, payitemid) DO NOTHING
            """),
            {
                "period_id":             period_id,
                "cid":                   company_id,
                "bid":                   branch_id,
                "payitemid":             r["payitemid"],
                "payitemcode":           r["payitemcode"],
                "payitemname":           r["payitemname"],
                "displaylabel":          r["displaylabel"],
                "category":              r["category"],
                "datatype":              r["datatype"],
                "unit":                  r["unit"],
                "itemscope":             r["itemscope"],
                "ratebehavior":          r["ratebehavior"],
                "appearsinpayrollentry": r["appearsinpayrollentry"],
                "appearsinledger":       r["appearsinledger"],
                "appearsinreports":      r["appearsinreports"],
                "requiresrate":          r["requiresrate"],
                "issystemstandard":      r["issystemstandard"],
                "iscustom":              bool(r["iscustom"]),
                "payitemstatus":         r["status"],
                "isactiveinperiod":      is_active_in_period,
                "sortorder":             r["sortorder"] if r["sortorder"] is not None else 0,
                "snapshoteffectivefrom": r["cfg_effectivefrom"],
                "sourceconfigid":        r["cfg_configid"],
            },
        )


async def _period_has_pay_item_snapshot(
    period_id: int,
    db: AsyncConnection,
) -> bool:
    """Return True if the period has any PayrollPeriodPayItems rows (post-0053 period)."""
    result = await db.execute(
        text("SELECT 1 FROM payroll.payrollperiodpayitems WHERE payrollperiodid = :pid LIMIT 1"),
        {"pid": period_id},
    )
    return result.first() is not None


async def _get_period_pay_item_snapshot(
    period_id: int,
    company_id: int,
    db: AsyncConnection,
    scope: str | None = None,
    active_only: bool = False,
) -> list:
    """
    Return PayrollPeriodPayItems rows for a period.

    scope: 'Daily' | 'Period' | None (all)
    active_only: if True, only rows with IsActiveInPeriod = TRUE

    Returns list of mapping rows. Empty list if no snapshot exists (legacy period).
    """
    filters = ["payrollperiodid = :pid", "companyid = :cid"]
    params: dict = {"pid": period_id, "cid": company_id}
    if scope:
        filters.append("itemscope = :scope")
        params["scope"] = scope
    if active_only:
        filters.append("isactiveinperiod = TRUE")
    where = " AND ".join(filters)
    result = await db.execute(
        text(f"""
            SELECT payitemcode, payitemname, displaylabel, category, datatype, unit,
                   itemscope, ratebehavior, appearsinpayrollentry, isactiveinperiod,
                   payitemid, payitemstatusatsnapshot, sortorder
            FROM payroll.payrollperiodpayitems
            WHERE {where}
            ORDER BY sortorder NULLS LAST, payitemcode
        """),
        params,
    )
    return result.mappings().all()


def _period_end(frequency: str, start: date, interval_days: int | None) -> date:
    if frequency == "Week":
        return start + timedelta(days=6)
    if frequency == "Biweek":
        return start + timedelta(days=13)
    if frequency == "Month":
        return _month_end(start)
    if frequency == "Custom":
        if not interval_days or interval_days <= 0:
            raise ValueError("Custom frequency requires custom_interval_days > 0.")
        return start + timedelta(days=interval_days - 1)
    raise ValueError(f"Unknown payroll frequency: {frequency!r}")


# ---------------------------------------------------------------------------
# Helper: audit for CP-1C creation
# ---------------------------------------------------------------------------

async def _write_period_created_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    branch_id: int,
    user_id: int,
    period_id: int,
    candidate_hash: str,
    mode: str,
    initial_status: str,
    start_date: date,
    end_date: date,
    setup_fp: str,
) -> None:
    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid,
                 oldvaluejson, newvaluejson, reason, sourcetype)
            VALUES
                (:cid, :bid, :uid, 'PERIOD_CREATED',
                 'payroll', 'PayrollPeriods', :eid,
                 NULL, :new_val, 'Period created via candidate key', 'Application')
        """),
        {
            "cid": company_id,
            "bid": branch_id,
            "uid": user_id,
            "eid": str(period_id),
            "new_val": json.dumps({
                "candidate_hash": candidate_hash,
                "mode": mode,
                "initial_status": initial_status,
                "start_date": str(start_date),
                "end_date": str(end_date),
                "setup_fingerprint": setup_fp,
                "result": "CREATED",
            }),
        },
    )


# ---------------------------------------------------------------------------
# Service: GET /payroll/branches/{branch_id}/period-candidates
# ---------------------------------------------------------------------------

async def get_period_candidates(
    company_id: int,
    user_id: int,
    branch_id: int,
    mode: str,
    cursor_key: str | None,
    db: AsyncConnection,
) -> CandidatePreviewResponse:
    # Security checks
    await _require_not_driver_role(company_id, user_id, db)

    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise HTTPException(status_code=403, detail="Access denied to the requested branch.")

    await _check_permission(company_id, user_id, branch_id, "payroll.period.create", db)

    # Validate mode
    if mode not in ("OPEN_CREATION", "PREPARED_CREATION"):
        raise HTTPException(
            status_code=422,
            detail="mode must be OPEN_CREATION or PREPARED_CREATION.",
        )

    # Determine preview offset from cursor (if provided)
    preview_offset = 0
    if cursor_key:
        cursor_payload, _ = _decode_candidate_key(cursor_key)
        if cursor_payload.get("cid") != company_id:
            _cp1c_error("INVALID_CANDIDATE_KEY", "Cross-company cursor rejected.")
        if cursor_payload.get("bid") != branch_id:
            _cp1c_error("INVALID_CANDIDATE_KEY", "Cross-branch cursor rejected.")
        if cursor_payload.get("mode") != mode:
            _cp1c_error("INVALID_CANDIDATE_KEY", "Wrong mode in cursor.")
        preview_offset = int(cursor_payload.get("offset", 0))

    # Read branch status
    branch_row = (await db.execute(
        text(
            "SELECT b.status FROM core.branches b "
            "WHERE b.branchid = :bid AND b.companyid = :cid"
        ),
        {"bid": branch_id, "cid": company_id},
    )).mappings().first()
    if branch_row is None or branch_row["status"] != "Active":
        _cp1c_error("BRANCH_INACTIVE", "Branch is inactive or not found.")

    # Read payroll setup — also fetch currentscheduleversionid for CP-2A binding.
    setup_row = (await db.execute(
        text("""
            SELECT payrollfrequency, anchorstartdate, customintervaldays,
                   currentscheduleversionid
            FROM payroll.branchpayrollsettings
            WHERE branchid = :bid AND companyid = :cid AND isactive = TRUE
        """),
        {"bid": branch_id, "cid": company_id},
    )).mappings().first()
    if setup_row is None:
        _cp1c_error("PAYROLL_SETUP_REQUIRED", "No active payroll setup found for this branch.")

    freq = setup_row["payrollfrequency"]
    anchor = setup_row["anchorstartdate"]
    interval_days = setup_row.get("customintervaldays")
    if freq == "Custom" and (not interval_days or interval_days <= 0):
        _cp1c_error("PAYROLL_SETUP_INCOMPLETE", "Custom frequency requires custom_interval_days > 0.")

    setup_fp = _setup_fingerprint(freq, anchor, interval_days)
    # CP-2A: include the current schedule version ID in the signed payload so
    # any setup change (which creates a new version) invalidates this candidate.
    current_sv_id = setup_row.get("currentscheduleversionid")

    # Read all non-Cancelled periods for slot/date computation
    period_rows = (await db.execute(
        text("""
            SELECT payrollperiodid, status, enddate
            FROM payroll.payrollperiods
            WHERE branchid = :bid AND companyid = :cid AND status != 'Cancelled'
            ORDER BY enddate DESC, payrollperiodid DESC
        """),
        {"bid": branch_id, "cid": company_id},
    )).mappings().all()

    periods = [dict(r) for r in period_rows]
    pred_end = periods[0]["enddate"] if periods else None
    pred_id = periods[0]["payrollperiodid"] if periods else None
    slot_fp = _slot_fingerprint(periods)

    # Slot matrix check
    creatable_base, blocked_reason = _check_slot_matrix(mode, periods)
    target_status = "Open" if mode == "OPEN_CREATION" else "Draft"

    # Compute dates at preview_offset
    try:
        start_date, end_date = _candidate_dates_at_offset(freq, anchor, interval_days, pred_end, preview_offset)
    except ValueError as e:
        _cp1c_error("PAYROLL_SETUP_INCOMPLETE", str(e))

    # Offset > 0 candidates are never directly creatable (navigation only)
    creatable = creatable_base and preview_offset == 0
    eff_blocked_reason = blocked_reason
    if preview_offset > 0 and eff_blocked_reason is None:
        eff_blocked_reason = "CANDIDATE_NOT_CURRENT"

    # Build candidate payload and sign it.
    # CP-2A: sv_id (schedule_version_id) is included so that any setup PUT
    # (which creates a new version row) invalidates candidates from the prior version.
    payload = {
        "ver": _CP1C_VERSION,
        "purpose": _CP1C_PURPOSE,
        "cid": company_id,
        "bid": branch_id,
        "mode": mode,
        "target_status": target_status,
        "freq": freq,
        "anchor": str(anchor),
        "interval": interval_days,
        "period_type": freq,
        "start": str(start_date),
        "end": str(end_date),
        "slot_fp": slot_fp,
        "setup_fp": setup_fp,
        "sv_id": current_sv_id,
        "pred_id": pred_id,
        "offset": preview_offset,
    }
    candidate_key, _ = _make_candidate_key(payload)
    label = _auto_period_name(freq, start_date, end_date)

    # Build navigation cursors
    prev_cursor: str | None = None
    next_cursor: str | None = None

    if preview_offset > 0:
        try:
            ps, pe = _candidate_dates_at_offset(freq, anchor, interval_days, pred_end, preview_offset - 1)
            prev_payload = {**payload, "offset": preview_offset - 1, "start": str(ps), "end": str(pe)}
            prev_cursor, _ = _make_candidate_key(prev_payload)
        except ValueError:
            pass

    if preview_offset < _CP1C_MAX_FUTURE - 1:
        try:
            ns, ne = _candidate_dates_at_offset(freq, anchor, interval_days, pred_end, preview_offset + 1)
            next_payload = {**payload, "offset": preview_offset + 1, "start": str(ns), "end": str(ne)}
            next_cursor, _ = _make_candidate_key(next_payload)
        except ValueError:
            pass

    return CandidatePreviewResponse(
        mode=mode,
        selected=CandidateSelectedInfo(
            candidate_key=candidate_key,
            target_status=target_status,
            start_date=start_date,
            end_date=end_date,
            period_type=freq,
            label=label,
            creatable=creatable,
            blocked_reason=eff_blocked_reason,
        ),
        navigation=CandidateNavigationInfo(
            previous_cursor=prev_cursor,
            next_cursor=next_cursor,
        ),
    )


# ---------------------------------------------------------------------------
# Service: POST /payroll/branches/{branch_id}/period-creations
# ---------------------------------------------------------------------------

async def create_period_from_candidate(
    company_id: int,
    user_id: int,
    branch_id: int,
    data: PeriodCreationRequest,
    db: AsyncConnection,
) -> PeriodCreationResponse:
    # Security checks
    await _require_not_driver_role(company_id, user_id, db)

    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise HTTPException(status_code=403, detail="Access denied to the requested branch.")

    await _check_permission(company_id, user_id, branch_id, "payroll.period.create", db)

    # Decode and verify candidate key
    payload, candidate_hash = _decode_candidate_key(data.candidate_key)

    if payload.get("cid") != company_id:
        _cp1c_error("INVALID_CANDIDATE_KEY", "Cross-company key rejected.")
    if payload.get("bid") != branch_id:
        _cp1c_error("INVALID_CANDIDATE_KEY", "Cross-branch key rejected.")

    mode = payload.get("mode", "")
    if mode not in ("OPEN_CREATION", "PREPARED_CREATION"):
        _cp1c_error("INVALID_CANDIDATE_KEY", "Unknown mode in candidate key.")

    claimed_offset = int(payload.get("offset", 0))
    if claimed_offset != 0:
        _cp1c_error("CANDIDATE_NOT_CURRENT", "Only offset-0 candidates may be created.")

    claimed_setup_fp = payload.get("setup_fp", "")
    claimed_slot_fp = payload.get("slot_fp", "")
    claimed_start = date.fromisoformat(payload["start"])
    claimed_end = date.fromisoformat(payload["end"])
    target_status = payload["target_status"]
    # CP-2A: schedule version ID embedded in the candidate payload.
    # None means this candidate was generated before CP-2A was deployed.
    claimed_sv_id = payload.get("sv_id")

    # Acquire branch advisory lock (transaction-level)
    await _acquire_branch_workflow_lock(company_id, branch_id, db)

    # Replay check: if this hash already exists, return the existing period
    existing_row = (await db.execute(
        text("""
            SELECT payrollperiodid, status, periodcode, periodname,
                   periodtype, startdate, enddate, branchid
            FROM payroll.payrollperiods
            WHERE companyid = :cid AND branchid = :bid
              AND creationcandidatekeyhash = :hash
        """),
        {"cid": company_id, "bid": branch_id, "hash": candidate_hash},
    )).mappings().first()

    if existing_row is not None:
        if existing_row["status"] == "Cancelled":
            _cp1c_error(
                "CANDIDATE_ALREADY_CANCELLED",
                "The period created from this candidate was later cancelled. "
                "Generate a fresh candidate; this key cannot be replayed.",
            )
        return PeriodCreationResponse(
            result="ALREADY_EXISTS",
            payroll_period_id=existing_row["payrollperiodid"],
            branch_id=existing_row["branchid"],
            period_code=existing_row["periodcode"],
            period_name=existing_row["periodname"],
            period_type=existing_row["periodtype"],
            start_date=existing_row["startdate"],
            end_date=existing_row["enddate"],
            status=existing_row["status"],
        )

    # Re-read branch under lock
    branch_row = (await db.execute(
        text(
            "SELECT b.status FROM core.branches b "
            "WHERE b.branchid = :bid AND b.companyid = :cid"
        ),
        {"bid": branch_id, "cid": company_id},
    )).mappings().first()
    if branch_row is None or branch_row["status"] != "Active":
        _cp1c_error("BRANCH_INACTIVE", "Branch is inactive or not found.")

    # Re-read setup under lock — also fetch currentscheduleversionid for CP-2A validation.
    setup_row = (await db.execute(
        text("""
            SELECT payrollfrequency, anchorstartdate, customintervaldays,
                   currentscheduleversionid
            FROM payroll.branchpayrollsettings
            WHERE branchid = :bid AND companyid = :cid AND isactive = TRUE
        """),
        {"bid": branch_id, "cid": company_id},
    )).mappings().first()
    if setup_row is None:
        _cp1c_error("PAYROLL_SETUP_REQUIRED", "No active payroll setup found.")

    freq = setup_row["payrollfrequency"]
    anchor = setup_row["anchorstartdate"]
    interval_days = setup_row.get("customintervaldays")
    current_setup_fp = _setup_fingerprint(freq, anchor, interval_days)

    if current_setup_fp != claimed_setup_fp:
        _cp1c_error("CANDIDATE_SETUP_CHANGED", "Payroll setup changed since this candidate was generated.")

    # CP-2A: schedule version validation.
    # If sv_id is absent the candidate is pre-CP-2A. Replay of an already-created
    # pre-CP-2A period is allowed (handled by the replay check above). New creation
    # from a no-sv_id candidate is rejected — clients must regenerate a fresh candidate.
    current_sv_id_from_settings = setup_row.get("currentscheduleversionid")
    if claimed_sv_id is None:
        _cp1c_error(
            "CANDIDATE_STALE",
            "Candidate lacks a schedule version ID (pre-CP-2A candidate). "
            "Regenerate a fresh candidate before creating a new period.",
        )
    if current_sv_id_from_settings != claimed_sv_id:
        _cp1c_error(
            "CANDIDATE_SETUP_CHANGED",
            "Payroll schedule version changed since this candidate was generated.",
        )

    # Re-read periods under lock
    period_rows = (await db.execute(
        text("""
            SELECT payrollperiodid, status, startdate, enddate
            FROM payroll.payrollperiods
            WHERE branchid = :bid AND companyid = :cid AND status != 'Cancelled'
            ORDER BY enddate DESC, payrollperiodid DESC
        """),
        {"bid": branch_id, "cid": company_id},
    )).mappings().all()

    periods = [dict(r) for r in period_rows]
    current_slot_fp = _slot_fingerprint(periods)

    if current_slot_fp != claimed_slot_fp:
        _cp1c_error("CANDIDATE_STALE", "Branch period slot state changed since this candidate was generated.")

    # Recompute candidate dates and compare to claimed values
    pred_end = periods[0]["enddate"] if periods else None
    try:
        computed_start, computed_end = _candidate_dates_at_offset(freq, anchor, interval_days, pred_end, 0)
    except ValueError as e:
        _cp1c_error("PAYROLL_SETUP_INCOMPLETE", str(e))

    if computed_start != claimed_start or computed_end != claimed_end:
        _cp1c_error("CANDIDATE_STALE", "Candidate dates no longer match current branch state.")

    # Check slot matrix under lock
    creatable, slot_error = _check_slot_matrix(mode, periods)
    if not creatable:
        _cp1c_error(slot_error or "PERIOD_SLOT_CONFLICT", f"Cannot create period: {slot_error}.")

    # Date overlap check under lock
    overlap_row = (await db.execute(
        text("""
            SELECT payrollperiodid FROM payroll.payrollperiods
            WHERE branchid = :bid AND companyid = :cid
              AND status != 'Cancelled'
              AND startdate <= :end_date
              AND enddate >= :start_date
            LIMIT 1
        """),
        {"bid": branch_id, "cid": company_id,
         "start_date": computed_start, "end_date": computed_end},
    )).first()
    if overlap_row is not None:
        _cp1c_error("PERIOD_DATE_OVERLAP", "Date range overlaps an existing non-cancelled period.")

    # Fetch branch code for period code generation
    br_row = (await db.execute(
        text("SELECT branchcode FROM core.branches WHERE branchid = :bid AND companyid = :cid"),
        {"bid": branch_id, "cid": company_id},
    )).mappings().first()
    branch_code = br_row["branchcode"] if br_row else str(branch_id)

    period_name = _auto_period_name(freq, computed_start, computed_end)
    base_code = f"{branch_code}-{computed_start.strftime('%Y%m%d')}"
    period_code = await _unique_period_code(base_code, company_id, branch_id, db)

    # CP-2A: ensure a valid schedule version exists and get its ID for the new period.
    # Still under the branch advisory lock. Uses repair path if needed (defensive).
    period_sv_id = await ensure_current_schedule_version(company_id, branch_id, user_id, db)

    # Insert period with candidate hash and schedule version
    insert_result = await db.execute(
        text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, periodcode, periodname, periodtype,
                 startdate, enddate, status, notes, createdbyuserid,
                 creationcandidatekeyhash, scheduleversionid)
            VALUES
                (:cid, :bid, :code, :name, :ptype,
                 :start, :end, :status, NULL, :uid,
                 :hash, :sv_id)
            RETURNING payrollperiodid
        """),
        {
            "cid":   company_id,
            "bid":   branch_id,
            "code":  period_code,
            "name":  period_name,
            "ptype": freq,
            "start": computed_start,
            "end":   computed_end,
            "status": target_status,
            "uid":   user_id,
            "hash":  candidate_hash,
            "sv_id": period_sv_id,
        },
    )
    new_period_id: int = insert_result.scalar_one()
    await initialize_period_audit_evidence_coverage(
        company_id=company_id, branch_id=branch_id, period_id=new_period_id, db=db,
    )

    # Write PERIOD_CREATED audit (exactly once — never on replay)
    await _write_period_created_audit(
        db,
        company_id=company_id,
        branch_id=branch_id,
        user_id=user_id,
        period_id=new_period_id,
        candidate_hash=candidate_hash,
        mode=mode,
        initial_status=target_status,
        start_date=computed_start,
        end_date=computed_end,
        setup_fp=current_setup_fp,
    )

    # CP-2B: create period-day snapshot from the schedule version's mask.
    # Read from PayrollScheduleVersions (immutable) — not from mutable BranchPayrollSettings.
    sv_mask_row = (await db.execute(
        text(
            "SELECT normaldaysoffmask FROM payroll.PayrollScheduleVersions "
            "WHERE scheduleversionid = :sv_id"
        ),
        {"sv_id": period_sv_id},
    )).mappings().first()
    period_mask = sv_mask_row["normaldaysoffmask"] if sv_mask_row else None
    await _create_period_day_rows(
        new_period_id, company_id, branch_id,
        period_sv_id, computed_start, computed_end, period_mask, db,
    )

    # CP-2C: create period pay-item layout snapshot.
    await _create_period_pay_item_rows(
        new_period_id, company_id, branch_id, computed_start, db,
    )

    # CP-2E: create driver eligibility snapshot.
    # Open periods are frozen immediately; Draft (Prepared) periods are provisional.
    _is_open_creation = (target_status == "Open")
    await _create_period_driver_eligibility_rows(
        new_period_id, company_id, branch_id, db,
        snapshot_source="Generated",
        freeze=_is_open_creation,
        created_by_user_id=user_id,
        frozen_by_user_id=user_id if _is_open_creation else None,
    )

    created_at = datetime.now(timezone.utc)

    return PeriodCreationResponse(
        result="CREATED",
        payroll_period_id=new_period_id,
        branch_id=branch_id,
        period_code=period_code,
        period_name=period_name,
        period_type=freq,
        start_date=computed_start,
        end_date=computed_end,
        status=target_status,
        created_at_utc=created_at,
    )


# ===========================================================================
# CP-1E: GET /payroll/current-workflow
# ===========================================================================

_WF_READ_PERMS = ["payroll.view", "payroll.entry", "payroll.finalize"]

_LIFECYCLE_POSITION: dict[str, int] = {
    "Returned": 1,
    "Open":     2,
    "Draft":    3,
    "InReview": 4,
}


def _cap(allowed: bool, reason_code: str | None = None, reason_message: str | None = None) -> WorkflowCapability:
    return WorkflowCapability(allowed=allowed, reason_code=reason_code, reason_message=reason_message)


def _denied(reason_code: str, reason_message: str) -> WorkflowCapability:
    return WorkflowCapability(allowed=False, reason_code=reason_code, reason_message=reason_message)


def _build_slot_item(row: dict, branch_name: str) -> WorkflowSlotItem:
    status = row["status"]
    display_status = "Prepared" if status == "Draft" else status
    is_read_only = status == "InReview"
    return WorkflowSlotItem(
        period_id=row["payrollperiodid"],
        branch_id=row["branchid"],
        branch_name=branch_name,
        status=status,
        display_status=display_status,
        period_name=row["periodname"],
        period_code=row["periodcode"],
        period_type=row["periodtype"],
        start_date=row["startdate"],
        end_date=row["enddate"],
        submitted_at_utc=row.get("submittedatutc"),
        current_return_review_item_id=row.get("currentreturnreviewitemid"),
        is_active_workflow_slot=True,
        is_read_only=is_read_only,
        read_only_reason_code="PERIOD_IN_REVIEW_READ_ONLY" if is_read_only else None,
        lifecycle_position=_LIFECYCLE_POSITION.get(status, 0),
    )


def _build_branch_entry(
    branch_id: int,
    branch_name: str,
    setup_status: str,
    active_periods: list[dict],
    has_view: bool,
    has_entry: bool,
    has_finalize: bool,
    has_period_create: bool,
) -> BranchWorkflowEntry:
    open_row = next((p for p in active_periods if p["status"] == "Open"), None)
    draft_row = next((p for p in active_periods if p["status"] == "Draft"), None)
    inreview_row = next((p for p in active_periods if p["status"] == "InReview"), None)
    returned_row = next((p for p in active_periods if p["status"] == "Returned"), None)

    slots = WorkflowBranchSlots(
        open=_build_slot_item(open_row, branch_name) if open_row else None,
        prepared=_build_slot_item(draft_row, branch_name) if draft_row else None,
        in_review=_build_slot_item(inreview_row, branch_name) if inreview_row else None,
        returned=_build_slot_item(returned_row, branch_name) if returned_row else None,
    )

    alerts: list[WorkflowAlert] = []

    # SLOT_INVARIANT: Draft without Open
    draft_alone = draft_row is not None and open_row is None
    if draft_alone:
        alerts.append(WorkflowAlert(
            code="SLOT_INVARIANT",
            severity="warning",
            title="Draft period without Open",
            message="A Prepared (Draft) period exists without a corresponding Open period. This is an unexpected workflow state.",
            related_period_id=draft_row["payrollperiodid"],
            affected_action_codes=["can_submit_for_review", "can_create_open_candidate"],
        ))

    # RETURNED_BACKLOG: older Returned exists with end_date < open.start_date
    returned_backlog = (
        returned_row is not None
        and open_row is not None
        and returned_row["enddate"] < open_row["startdate"]
    )
    returned_anomaly = (
        returned_row is not None
        and open_row is not None
        and returned_row["enddate"] >= open_row["startdate"]
    )
    if returned_backlog:
        alerts.append(WorkflowAlert(
            code="RETURNED_BACKLOG",
            severity="blocker",
            title="Unresolved Returned period",
            message=(
                f"A Returned period (ending {returned_row['enddate']}) exists before the "
                f"current Open period (starting {open_row['startdate']}). "
                "Resolve the Returned period before submitting."
            ),
            related_period_id=returned_row["payrollperiodid"],
            affected_action_codes=["can_submit_for_review"],
        ))
    elif returned_anomaly:
        alerts.append(WorkflowAlert(
            code="SLOT_INVARIANT",
            severity="warning",
            title="Returned period workflow anomaly",
            message=(
                f"A Returned period (ending {returned_row['enddate']}) overlaps or is newer than "
                f"the Open period (starting {open_row['startdate']}). This is an unexpected state."
            ),
            related_period_id=returned_row["payrollperiodid"],
            affected_action_codes=["can_submit_for_review"],
        ))

    # Draft promotion conflict: Draft exists but not adjacent to Open
    draft_promotion_conflict = False
    if draft_row and open_row and not draft_alone:
        expected = open_row["enddate"] + timedelta(days=1)
        if draft_row["startdate"] != expected:
            draft_promotion_conflict = True
            alerts.append(WorkflowAlert(
                code="SLOT_INVARIANT",
                severity="warning",
                title="Draft period not adjacent to Open",
                message=(
                    f"A Draft period exists (start {draft_row['startdate']}) but does not "
                    f"immediately follow the Open period end ({open_row['enddate']}). "
                    "Submit will be blocked until resolved."
                ),
                related_period_id=draft_row["payrollperiodid"],
                affected_action_codes=["can_submit_for_review"],
            ))

    # INREVIEW_AWAITING
    if inreview_row:
        alerts.append(WorkflowAlert(
            code="INREVIEW_AWAITING",
            severity="info",
            title="Period awaiting review",
            message="A period is currently in review.",
            related_period_id=inreview_row["payrollperiodid"],
            affected_action_codes=[],
        ))

    # PREPARED_NOTICE
    if draft_row:
        alerts.append(WorkflowAlert(
            code="PREPARED_NOTICE",
            severity="info",
            title="Prepared period exists",
            message="A Prepared period exists and cannot be submitted directly.",
            related_period_id=draft_row["payrollperiodid"],
            affected_action_codes=[],
        ))

    # Setup alerts
    if setup_status != "complete":
        sev = "warning"
        if setup_status == "missing":
            msg = "No payroll setup found for this branch."
        elif setup_status == "inactive":
            msg = "The payroll setup for this branch is inactive."
        else:
            msg = "The payroll setup for this branch is incomplete."
        alerts.append(WorkflowAlert(
            code="SETUP_MISSING" if setup_status == "missing" else "SETUP_INCOMPLETE",
            severity=sev,
            title="Payroll setup issue",
            message=msg,
            affected_action_codes=["can_create_open_candidate", "can_create_prepared_candidate"],
        ))

    # Build branch-level capabilities
    can_view = _cap(has_view or has_entry or has_finalize)

    # can_create_open_candidate
    if not has_period_create:
        co_cap = _denied("PERMISSION_DENIED", "payroll.period.create required.")
    elif setup_status != "complete":
        co_cap = _denied("NO_PAYROLL_SETUP" if setup_status == "missing" else "SETUP_INCOMPLETE",
                         "Payroll setup must be complete to create candidates.")
    elif draft_alone:
        co_cap = _denied("DRAFT_WITHOUT_OPEN", "Cannot create Open candidate while a Draft exists without Open.")
    else:
        ok, code = _check_slot_matrix("OPEN_CREATION", active_periods)
        co_cap = _cap(ok, code, None if ok else f"Slot matrix blocked: {code}")

    # can_create_prepared_candidate — Returned backlog does NOT block this
    if not has_period_create:
        cp_cap = _denied("PERMISSION_DENIED", "payroll.period.create required.")
    elif setup_status != "complete":
        cp_cap = _denied("NO_PAYROLL_SETUP" if setup_status == "missing" else "SETUP_INCOMPLETE",
                         "Payroll setup must be complete to create candidates.")
    elif draft_alone:
        cp_cap = _denied("DRAFT_WITHOUT_OPEN", "Cannot create Prepared candidate while a Draft exists without Open.")
    else:
        ok, code = _check_slot_matrix("PREPARED_CREATION", active_periods)
        cp_cap = _cap(ok, code, None if ok else f"Slot matrix blocked: {code}")

    # can_view_candidates
    if not has_period_create:
        cv_cap = _denied("PERMISSION_DENIED", "payroll.period.create required.")
    else:
        cv_cap = _cap(True)

    # Period-level capabilities
    period_caps: dict[str, PeriodWorkflowCapabilities] = {}
    for row in active_periods:
        pid = row["payrollperiodid"]
        st = row["status"]
        key = str(pid)

        # can_enter_source
        # CP-2F: Draft (Prepared) supports operational source entry (day grid save,
        # daily lines). Financial entry (Period Pay, Bonus) is blocked separately.
        if not (has_entry or has_view):
            ce = _denied("PERMISSION_DENIED", "payroll.view or payroll.entry required.")
        elif st == "InReview":
            ce = _denied("PERIOD_IN_REVIEW_READ_ONLY", "Period is in review; source entry is locked.")
        elif st in ("Open", "Returned", "Draft"):
            if not has_entry:
                ce = _denied("PERMISSION_DENIED", "payroll.entry required for source entry.")
            else:
                ce = _cap(True)
        else:
            ce = _denied("PERIOD_CANCELLED", "Period is not in an editable state.")

        # can_submit_for_review
        if st != "Open":
            cs = _denied("PERIOD_NOT_OPEN", "Only Open periods can be submitted.")
        elif not has_entry:
            cs = _denied("PERMISSION_DENIED", "payroll.entry required to submit.")
        elif inreview_row:
            cs = _denied("INREVIEW_SLOT_OCCUPIED", "Another period is already in review.")
        elif returned_backlog:
            cs = _denied("RETURNED_BACKLOG_BLOCKS_SUBMIT",
                         f"Returned backlog (ending {returned_row['enddate']}) must be resolved first.")
        elif returned_anomaly:
            cs = _denied("WORKFLOW_SLOT_CONFLICT", "Returned period has an anomalous chronological position.")
        elif draft_promotion_conflict:
            cs = _denied("DRAFT_PROMOTION_CONFLICT", "Draft period is not adjacent to this Open period.")
        else:
            cs = _cap(True)

        # can_resubmit_returned
        if st != "Returned":
            cr = _denied("PERIOD_NOT_RETURNED", "Only Returned periods can be resubmitted.")
        elif not has_entry:
            cr = _denied("PERMISSION_DENIED", "payroll.entry required to resubmit.")
        elif inreview_row:
            cr = _denied("INREVIEW_SLOT_OCCUPIED", "Another period is already in review.")
        else:
            cr = _cap(True)

        # can_view_review
        if st == "InReview":
            if not (has_view or has_entry):
                cvr = _denied("PERMISSION_DENIED", "payroll.view or payroll.entry required.")
            else:
                cvr = _cap(True)
        else:
            cvr = _cap(False, "PERIOD_NOT_IN_REVIEW", "Period is not currently in review.")

        # can_cancel
        if st == "Draft":
            if not has_finalize:
                cc = _denied("PERMISSION_DENIED", "payroll.finalize required to cancel a Draft period.")
            else:
                cc = _cap(True)
        elif st == "Open":
            if not has_finalize:
                cc = _denied("PERMISSION_DENIED", "payroll.finalize required to cancel an Open period.")
            else:
                cc = _cap(True)
        else:
            cc = _cap(False, "PERIOD_NOT_OPEN", "Only Draft and Open periods can be cancelled via this workflow.")

        # can_open_day_grid
        # CP-2F: Draft (Prepared) periods expose the day grid for operational entry.
        if not (has_view or has_entry):
            cg = _denied("PERMISSION_DENIED", "payroll.view or payroll.entry required.")
        elif st in ("Open", "Returned", "Draft"):
            cg = _cap(True)
        elif st == "InReview":
            cg = _cap(True)  # read-only access allowed; is_read_only on slot signals that
        else:
            cg = _cap(False, "PERIOD_NOT_EDITABLE", "Day grid not available for this period status.")

        period_caps[key] = PeriodWorkflowCapabilities(
            can_enter_source=ce,
            can_submit_for_review=cs,
            can_resubmit_returned=cr,
            can_view_review=cvr,
            can_cancel=cc,
            can_open_day_grid=cg,
        )

    branch_caps = BranchWorkflowCapabilities(
        can_view_current_workflow=can_view,
        can_create_open_candidate=co_cap,
        can_create_prepared_candidate=cp_cap,
        can_view_candidates=cv_cap,
        periods=period_caps,
    )

    return BranchWorkflowEntry(
        branch_id=branch_id,
        branch_name=branch_name,
        setup_status=setup_status,
        slots=slots,
        capabilities=branch_caps,
        alerts=alerts,
    )


async def get_current_workflow(
    company_id: int,
    user_id: int,
    branch_id: int | None,
    db: AsyncConnection,
) -> CurrentWorkflowResponse:
    # Security: block driver and ODA roles
    await _require_not_driver_role(company_id, user_id, db)
    own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)
    if own_driver_id is not None:
        raise HTTPException(
            status_code=403,
            detail="Current workflow is not accessible to driver-role users.",
        )

    can_see_all, allowed_branch_ids = await _check_branch_access(company_id, user_id, db)

    # If a specific branch was requested, verify it's accessible
    if branch_id is not None:
        if not can_see_all and branch_id not in allowed_branch_ids:
            raise HTTPException(status_code=403, detail="Access denied to the requested branch.")

    # Determine which branches to include
    if branch_id is not None:
        target_branch_ids: list[int] | None = [branch_id]
    elif can_see_all:
        target_branch_ids = None  # query all active branches
    else:
        target_branch_ids = allowed_branch_ids if allowed_branch_ids else []

    # Load branch metadata
    if target_branch_ids is not None and len(target_branch_ids) == 0:
        return CurrentWorkflowResponse(
            scope="branch" if not can_see_all else "company",
            company_id=company_id,
            requested_branch_id=branch_id,
            branches=[],
        )

    if target_branch_ids is None:
        branch_rows = (await db.execute(
            text("""
                SELECT b.branchid, b.branchname
                FROM   core.branches b
                WHERE  b.companyid = :cid AND b.status = 'Active'
                ORDER  BY b.branchname
            """),
            {"cid": company_id},
        )).mappings().all()
    else:
        in_clause, in_params = _build_in_clause(target_branch_ids, "bid")
        branch_rows = (await db.execute(
            text(f"""
                SELECT b.branchid, b.branchname
                FROM   core.branches b
                WHERE  b.companyid = :cid AND b.status = 'Active'
                  AND  b.branchid IN ({in_clause})
                ORDER  BY b.branchname
            """),
            {"cid": company_id, **in_params},
        )).mappings().all()

    if not branch_rows:
        return CurrentWorkflowResponse(
            scope="branch" if branch_id else "company",
            company_id=company_id,
            requested_branch_id=branch_id,
            branches=[],
        )

    branch_id_list = [r["branchid"] for r in branch_rows]
    branch_name_map = {r["branchid"]: r["branchname"] for r in branch_rows}

    # Check read permission: require at least one payroll read perm on at least one accessible branch
    # We verify per-branch below when building capabilities, but first ensure user has any access
    any_read = False
    for bid in branch_id_list:
        if await _has_any_permission(company_id, user_id, bid, _WF_READ_PERMS, db):
            any_read = True
            break
    if not any_read:
        raise HTTPException(status_code=403, detail="No payroll read permission on any accessible branch.")

    # Load all active workflow periods for these branches in one query
    in_clause2, in_params2 = _build_in_clause(branch_id_list, "bid")
    period_rows = (await db.execute(
        text(f"""
            SELECT payrollperiodid, branchid, periodcode, periodname, periodtype,
                   startdate, enddate, status,
                   submittedatutc, currentreturnreviewitemid
            FROM   payroll.payrollperiods
            WHERE  companyid = :cid
              AND  branchid IN ({in_clause2})
              AND  status   IN ('Draft', 'Open', 'InReview', 'Returned')
            ORDER  BY branchid, startdate, payrollperiodid
        """),
        {"cid": company_id, **in_params2},
    )).mappings().all()

    # Group periods by branch_id
    periods_by_branch: dict[int, list[dict]] = {bid: [] for bid in branch_id_list}
    for row in period_rows:
        periods_by_branch[row["branchid"]].append(dict(row))

    # Load payroll setup status for all branches
    setup_rows = (await db.execute(
        text(f"""
            SELECT branchid, payrollfrequency, customintervaldays, isactive
            FROM   payroll.branchpayrollsettings
            WHERE  companyid = :cid
              AND  branchid IN ({in_clause2})
        """),
        {"cid": company_id, **in_params2},
    )).mappings().all()

    setup_by_branch: dict[int, str] = {}
    for sr in setup_rows:
        bid = sr["branchid"]
        if not sr["isactive"]:
            setup_by_branch[bid] = "inactive"
        elif sr["payrollfrequency"] == "Custom" and not sr.get("customintervaldays"):
            setup_by_branch[bid] = "incomplete"
        else:
            setup_by_branch[bid] = "complete"

    result_branches: list[BranchWorkflowEntry] = []
    scope = "branch" if (branch_id is not None or not can_see_all) else "company"

    for bid in branch_id_list:
        bname = branch_name_map[bid]
        setup_st = setup_by_branch.get(bid, "missing")
        active_periods = periods_by_branch.get(bid, [])

        # Per-branch permission checks
        b_view = await _has_any_permission(company_id, user_id, bid, _WF_READ_PERMS, db)
        if not b_view:
            # Include branch in response but with denied view capability
            branch_entry = BranchWorkflowEntry(
                branch_id=bid,
                branch_name=bname,
                setup_status=setup_st,
                slots=WorkflowBranchSlots(),
                capabilities=BranchWorkflowCapabilities(
                    can_view_current_workflow=_denied("PERMISSION_DENIED", "No payroll read permission for this branch."),
                    can_create_open_candidate=_denied("PERMISSION_DENIED", "No payroll read permission."),
                    can_create_prepared_candidate=_denied("PERMISSION_DENIED", "No payroll read permission."),
                    can_view_candidates=_denied("PERMISSION_DENIED", "No payroll read permission."),
                ),
                alerts=[],
            )
            result_branches.append(branch_entry)
            continue

        has_view = await _has_any_permission(company_id, user_id, bid, ["payroll.view"], db)
        has_entry = await _has_any_permission(company_id, user_id, bid, ["payroll.entry"], db)
        has_finalize = await _has_any_permission(company_id, user_id, bid, ["payroll.finalize"], db)
        has_period_create = await _has_any_permission(company_id, user_id, bid, ["payroll.period.create"], db)

        entry = _build_branch_entry(
            branch_id=bid,
            branch_name=bname,
            setup_status=setup_st,
            active_periods=active_periods,
            has_view=has_view,
            has_entry=has_entry,
            has_finalize=has_finalize,
            has_period_create=has_period_create,
        )
        result_branches.append(entry)

    return CurrentWorkflowResponse(
        scope=scope,
        company_id=company_id,
        requested_branch_id=branch_id,
        branches=result_branches,
    )
