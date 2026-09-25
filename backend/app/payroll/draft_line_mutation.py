"""
Draft-line mutation domain — the Draft CRUD write surface: add, update, and
void a Daily draft line, plus their private validation/policy.

Extracted from app.payroll.service (Stage B4-14) as a dependency-closed leaf
module — no behavior change, pure relocation.

Owns exactly:
  - add_draft_line, update_draft_line, void_draft_line (public mutation ops)
  - _validate_line_type (PayItem-driven line-type validation, private)
  - _SYSTEM_FINALIZATION_ONLY, _CALC_REQUIRED_BEHAVIORS (private policy
    constants consumed only by _validate_line_type / update_draft_line)

Does NOT own the read surfaces (get_period_lines, get_period_draft_summary),
which live in app.payroll.source_line_read — "CRUD" here is mutation-only by
design.

Consumes, rather than owns, every adjacent domain:
  - app.payroll.period_read.get_period_by_id
  - app.payroll.period_day_calendar._validate_period_work_date
  - app.payroll.eligibility._assert_driver_eligible_for_workdate_via_snapshot
  - app.payroll.day_entry_state (_validate_status_key, _resolve_status_key_id,
    _upsert_entry_state, _void_entry_state_field)
  - app.payroll.draft_line_calculation._compute_calculated_amount
  - app.payroll.source_line_read._get_line_by_id
  - app.payroll.source_evidence._capture_source_evidence
  - app.payroll.pay_item_write_lock._lock_pay_item_for_source_write
  - app.payroll.mutation_lock._lock_period_for_mutation
  - app.payroll.line_audit._write_line_audit
  - app.payroll.line_type_vocabulary (_INFORMATIONAL_ONLY,
    _LEGACY_TO_CANONICAL, _LineTypeInfo)
  - app.payroll.schemas (DraftLineCreate, DraftLineUpdate, DraftLineSummary,
    ENTRY_ALLOWED_STATUSES, _WRITE_BLOCKED_STATUSES)
  - app.core.service._check_permission

Day Grid (save_day_grid, in app.payroll.day_grid) calls
add_draft_line/update_draft_line/void_draft_line in-process, importing them
directly from this module.
"""
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import _check_permission
from app.payroll.day_entry_state import (
    _resolve_status_key_id,
    _upsert_entry_state,
    _validate_status_key,
    _void_entry_state_field,
)
from app.payroll.draft_line_calculation import _compute_calculated_amount
from app.payroll.eligibility import _assert_driver_eligible_for_workdate_via_snapshot
from app.payroll.line_audit import _write_line_audit
from app.payroll.line_type_vocabulary import (
    _INFORMATIONAL_ONLY,
    _LEGACY_TO_CANONICAL,
    _LineTypeInfo,
)
from app.payroll.mutation_lock import _lock_period_for_mutation
from app.payroll.pay_item_write_lock import _lock_pay_item_for_source_write
from app.payroll.period_day_calendar import _validate_period_work_date
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

# Behaviors where calculatedamount is strictly required for approval/finalization.
# rateamount is NOT a valid fallback for these behaviors — tier/block lines must have
# a resolved calculatedamount from the calculation engine.  PerUnit is intentionally
# excluded because qty × rateamount is a safe and correct finalization path for it.
_CALC_REQUIRED_BEHAVIORS: frozenset[str] = frozenset(
    {"OrdinalTier", "RangeBracket", "RangeProgressive", "Block"}
)

# System lines written ONLY by the finalization engine — never accepted from user endpoints.
_SYSTEM_FINALIZATION_ONLY: frozenset[str] = frozenset({"SYS_MIN_TOPUP", "SYS_MAX_CAP"})


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
