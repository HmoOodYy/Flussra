"""
Pay-item source-write lock — catalog-row concurrency primitive for payroll
source writes.

Extracted from app.payroll.service (Stage B4-11B) as a dependency-closed leaf
module — no behavior change, pure relocation.

_lock_pay_item_for_source_write is a concurrency-safety primitive: it
acquires a FOR UPDATE row lock on the company-owned payroll.PayItems catalog
row before a source mutation (draft line, period pay line, or day-grid save)
inserts or updates a DraftLine reference to it. It serializes against the
physical-delete/retirement path's own FOR UPDATE on the same row (see
app.settings.service, the pay-item delete endpoint), closing the
first-reference race where a concurrent deletion could otherwise orphan a
newly-inserted DraftLine.

It is distinct from app.payroll.mutation_lock._lock_period_for_mutation
(locks the payrollperiods row to verify period editability) and from
app.payroll.workflow_lock._acquire_branch_workflow_lock (a branch-level
advisory lock for period-workflow operations) — three different objects
locked for three different reasons. It must not be merged with either.

Call order: every caller acquires this lock BEFORE
_lock_period_for_mutation, so all source-write paths lock PayItem then
Period — the same order as the delete/retirement path — avoiding deadlock.
This module does not call _lock_period_for_mutation itself; ordering is
enforced entirely by the caller (still app.payroll.service in this stage),
so this extraction does not and cannot change it.

Genuinely shared by five call sites that all still live in
app.payroll.service — Draft-line CRUD (add_draft_line, update_draft_line),
Period Pay Lines (add_period_pay_line, update_period_pay_line), and Day Grid
(save_day_grid) — none of which is more entitled to own it than the others.
app.payroll.service imports it back via a compatibility facade: four
concurrency tests in test_cp0a_mutation_status_guard.py patch
service._lock_pay_item_for_source_write directly, and their exercised
callers (add_draft_line, update_draft_line) remain in app.payroll.service in
this unit, so the plain imported binding is load-bearing, not incidental.

Do not add unrelated helpers here. This is not a general utilities module.
"""
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.line_type_vocabulary import _INFORMATIONAL_ONLY


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
