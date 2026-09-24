"""
Period Pay domain — period-level lump-sum lines (Bonus/Adjustment/custom
Period items) stored in payroll.payrolldraftlines with WorkDate = NULL.

Extracted from app.payroll.service (Stage B4-12) as a dependency-closed leaf
module — no behavior change, pure relocation.

Owns:
  - _validate_period_line_type: the Period Pay line-type validator (mirrors
    Draft CRUD's own _validate_line_type, which is private to
    app.payroll.draft_line_mutation — the two validators are deliberately
    not unified).
  - _PERIOD_PAY_ALLOWED_BEHAVIORS / _SYSTEM_PERIOD_BLOCKED: Period-Pay-private
    policy constants consumed only by _validate_period_line_type.
  - add_period_pay_line / get_period_pay_lines / update_period_pay_line /
    void_period_pay_line: the four public Period Pay operations.

Does NOT own generic Bonus entry: BONUS lines are explicitly rejected here
(CP-3A) and routed to the canonical Bonus Events API, owned exclusively by
app.payroll.bonus. This module must never become an alternate path for
Bonus writes.

Consumes (does not own):
  - source-line read model (app.payroll.source_line_read)
  - pay-item source-write locking (app.payroll.pay_item_write_lock)
  - period mutation locking (app.payroll.mutation_lock)
  - source evidence capture (app.payroll.source_evidence)
  - pay-item snapshot reads (app.payroll.period_pay_item_snapshot)
  - driver eligibility snapshot checks (app.payroll.eligibility)
  - line-type vocabulary (app.payroll.line_type_vocabulary)
  - period read model (app.payroll.period_read)
  - permission/role guards (app.core.service)
"""
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import _check_permission, _require_not_driver_role
from app.payroll.eligibility import _assert_driver_eligible_for_period_via_snapshot
from app.payroll.line_audit import _write_line_audit
from app.payroll.line_type_vocabulary import _LEGACY_TO_CANONICAL, _LineTypeInfo
from app.payroll.mutation_lock import _lock_period_for_mutation
from app.payroll.pay_item_write_lock import _lock_pay_item_for_source_write
from app.payroll.period_pay_item_snapshot import _period_has_pay_item_snapshot
from app.payroll.period_read import get_period_by_id
from app.payroll.schemas import (
    _WRITE_BLOCKED_STATUSES,
    ENTRY_ALLOWED_STATUSES,
    DraftLineSummary,
    PeriodPayLineCreate,
    PeriodPayLineUpdate,
)
from app.payroll.source_evidence import _capture_source_evidence
from app.payroll.source_line_read import _LINE_SELECT, _get_line_by_id, _line_row_to_summary

# M14: RateBehaviors allowed for manual Period Pay entry.
# EnteredAmount — custom Period items (user enters dollar amount directly).
# Fixed         — system BONUS / ADJUSTMENT (same semantics: direct amount entry).
# Calculated (GUARANTEED_MINIMUM) and all Daily behaviors are blocked in M14.
_PERIOD_PAY_ALLOWED_BEHAVIORS: frozenset[str] = frozenset({"EnteredAmount", "Fixed"})

_SYSTEM_PERIOD_BLOCKED: frozenset[str] = frozenset({
    "GuaranteedMinimum", "GUARANTEED_MINIMUM",
    "SYS_MIN_TOPUP", "SYS_MAX_CAP",
})


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
