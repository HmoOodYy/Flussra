"""
Period Lifecycle — the payroll domain's single owner of:

  A. Status transition permission mapping (_TRANSITION_PERMISSIONS)
  B. Retryable-transaction error translation
     (_is_retryable_transaction_failure, _translate_submit_transaction_failures)
  C. Period-status audit writing (_PERIOD_AUDIT_REASONS, _write_period_status_audit)
  D. Submit transaction-isolation setup (_set_submit_transaction_isolation)
  E. InReview slot protection (_is_inreview_slot_violation,
     _check_inreview_slot_available)
  F. Submit orchestration (change_period_status)
  G. Resubmission orchestration (resubmit_period)

Extracted from app.payroll.service (Stage B4-18) as a dependency-closed leaf
module — no behavior change, pure relocation. A fresh B4-18 discovery proved
the transitive service.py-local closure of these 10 symbols is empty: this
module depends only on already-extracted true owners
(app.payroll.period_calculation, app.payroll.status_payment_sync,
app.payroll.period_read, app.payroll.eligibility, app.payroll.guards,
app.payroll.workflow_lock, app.payroll.audit_evidence,
app.payroll.immutable_evidence, app.payroll.schemas, app.core.service) and
never imports app.payroll.service.

This module owns period status transition policy, submit/resubmit
orchestration, InReview slot protection, submit transaction-isolation
behavior, retryable-transaction error translation, and period-status audit
writing. It does NOT own Calculation, Calculation snapshot capture, Status
Payment implementation, Finalization, Period creation, Day Grid, Draft
mutation, Review approval behavior, or Drivers Off — those stay with their
existing owners and are consumed here only through imports.

The architecture direction is one-way:

    Period Lifecycle -> Period Calculation, Status Payment Sync, Period Read,
                         and other established leaf owners

Calculation is consumed directly from app.payroll.period_calculation
(_refresh_draft_calculations, _build_live_calculation_packet,
_capture_calculation_snapshot) — Lifecycle does not rebuild Calculation
policy, copy packet code, or copy snapshot code. Status Payment is consumed
directly from app.payroll.status_payment_sync
(_refresh_status_payment_lines) — Lifecycle does not duplicate Status
Payment logic.

Finalization (finalize_period, get_finalization_preview, and their approved-
snapshot helpers) remains in app.payroll.service — proven dependency-
disjoint from this module by the B4-17 discovery, reconfirmed unchanged by
this stage. Review approval behavior remains owned by app.review.service,
which imports _write_period_status_audit directly from this module as of
this stage (previously via app.payroll.service).

Transaction ownership is above this module. change_period_status and
resubmit_period receive an ambient AsyncConnection and open no transaction
of their own, commit nothing, and roll back nothing explicitly — the request-
level transaction is owned by the existing DB dependency
(app.dependencies.get_db). _set_submit_transaction_isolation issues exactly
one SQL statement (SET TRANSACTION ISOLATION LEVEL REPEATABLE READ) and must
be the first statement on the connection for a submitting request; both
callers preserve that ordering unchanged from their pre-extraction form.

The InReview slot protection here relies on a DB-enforced partial unique
index as the concurrency authority; _check_inreview_slot_available is a
friendly pre-check only, not a replacement for that race protection.
"""
from __future__ import annotations

import json
from functools import wraps
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import _check_permission, _require_not_driver_role
from app.payroll.audit_evidence import link_unmapped_audit_evidence_to_snapshot
from app.payroll.eligibility import _regenerate_period_driver_eligibility_rows
from app.payroll.guards import _get_oda_own_driver_id
from app.payroll.immutable_evidence import capture_workflow_action_evidence
from app.payroll.period_calculation import (
    _build_live_calculation_packet,
    _capture_calculation_snapshot,
    _refresh_draft_calculations,
)
from app.payroll.period_read import get_period_by_id
from app.payroll.schemas import PeriodStatusChange, PeriodSummary, _VALID_TRANSITIONS
from app.payroll.status_payment_sync import _refresh_status_payment_lines
from app.payroll.workflow_lock import _acquire_branch_workflow_lock


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


def _is_retryable_transaction_failure(exc: DBAPIError) -> bool:
    """PostgreSQL transaction failures which are safe for the client to retry."""
    return getattr(exc.orig, "sqlstate", None) in {"40001", "40P01"}


async def _set_submit_transaction_isolation(db: AsyncConnection) -> None:
    """Set the submit/resubmit request transaction before any database read."""
    await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
