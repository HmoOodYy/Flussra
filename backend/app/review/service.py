"""
Review domain service — manager review items and decisions.

Security model:
  - Reading items requires only branch access (no specific permission).
  - Creating an item requires the ``payroll.entry`` permission on that branch.
  - Deciding on an item (approve / reject / comment) requires the
    ``review.decide`` permission on that branch.

Audit logging:
  - Creating a review item writes REVIEW_ITEM_CREATED to audit.auditlog.
  - Substantive decisions (Approved/Rejected/EditRequested) write
    REVIEW_ITEM_DECIDED.  Comment decisions are not audited — they are
    discussion notes, not security-relevant sign-off actions.
  - All audit writes are inside the same transaction as the primary write
    so a failure rolls back both together.

Race condition safety:
  - decide_review_item() uses SELECT … FOR UPDATE on the item row.
    This holds a row-level exclusive lock for the transaction duration,
    so two concurrent decisions cannot both pass the status check.

Entity fields (entity_schema, entity_name, entity_id) are free-form
polymorphic metadata — the same pattern as audit.AuditLog.  They are NOT
validated against actual tables; the review item's own branch_id is the
authoritative scope boundary.

Self-approval policy:
  - Whether the same user who created/submitted a review item may also record
    a substantive decision on it is controlled by core.Companies.AllowSelfApproval.
  - When AllowSelfApproval = FALSE, Approved / Rejected / EditRequested decisions
    by the original submitter are blocked with HTTP 422.
  - Comment decisions are always permitted regardless of this policy.

Review workflow integration status (as of M7):
  - This module is a review *foundation queue*, not a full payroll-approval
    workflow.  Approving or rejecting a review item does NOT automatically
    update any linked payroll period status.  Payroll period status transitions
    are controlled exclusively by PATCH /payroll/periods/{id}/status and
    POST /payroll/periods/{id}/finalize.
  - The payroll period endpoint blocks InReview → Approved while pending review
    items exist for the branch, but the review service itself does not write
    back to payroll.PayrollPeriods.  A future milestone may wire these together
    once the workflow policy decisions are made.

All database access is raw parameterised SQL via sqlalchemy.text().
"""
import json
from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import (
    _check_branch_access, _build_in_clause, _check_permission, _check_any_permission,
    _require_not_driver_role, _has_any_permission,
)
from app.payroll.service import _write_period_status_audit  # M16: period write-back
from app.review.schemas import (
    ReviewItemSummary,
    ReviewItemDetail,
    ReviewDecisionSummary,
    ReviewItemCreate,
    ReviewDecide,
    _DECIDABLE_STATUSES,
)


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------

_REVIEW_AUDIT_REASONS: dict[str, str] = {
    "REVIEW_ITEM_CREATED": "Review item submitted",
    "REVIEW_ITEM_DECIDED": "Review item decision recorded",
}


async def _write_review_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    branch_id: int,
    user_id: int,
    item_id: int,
    action_code: str,
    old_value: dict | None = None,
    new_value: dict | None = None,
) -> None:
    """
    Insert one row into audit.AuditLog for a review-domain event.

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
                 'review', 'ManagerReviewItems', :entity_id,
                 :old_val, :new_val, :reason, 'Application')
        """),
        {
            "company_id":  company_id,
            "branch_id":   branch_id,
            "actor_id":    user_id,
            "action_code": action_code,
            "entity_id":   str(item_id),
            "old_val":     json.dumps(old_value)  if old_value  is not None else None,
            "new_val":     json.dumps(new_value)  if new_value  is not None else None,
            "reason":      _REVIEW_AUDIT_REASONS.get(action_code, action_code),
        },
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Base SELECT used for both list and single-item queries.  The JOINs give us
# display names for the requester and decision-maker so the caller doesn't
# need a second round-trip.
_ITEM_SELECT = """
    SELECT
        m.reviewitemid,
        m.companyid,
        m.branchid,
        b.branchname,
        m.requestedbyuserid,
        ru.displayname   AS requestedby,
        m.requesttype,
        m.entityschema,
        m.entityname,
        m.entityid,
        m.title,
        m.description,
        m.oldvaluejson,
        m.newvaluejson,
        m.status,
        m.priority,
        m.createdatutc,
        m.dueatutc,
        m.finaldecisionbyuserid,
        du.displayname   AS finaldecisionby,
        m.finaldecisionatutc,
        m.finaldecisionreason
    FROM   review.managerreviewitems m
    JOIN   core.branches b  ON b.branchid  = m.branchid
    LEFT JOIN sec.users ru  ON ru.userid   = m.requestedbyuserid
    LEFT JOIN sec.users du  ON du.userid   = m.finaldecisionbyuserid
"""


def _row_to_summary(row) -> ReviewItemSummary:
    return ReviewItemSummary(
        review_item_id=row["reviewitemid"],
        company_id=row["companyid"],
        branch_id=row["branchid"],
        branch_name=row.get("branchname"),
        requested_by_user_id=row.get("requestedbyuserid"),
        requested_by=row.get("requestedby"),
        request_type=row["requesttype"],
        entity_schema=row.get("entityschema"),
        entity_name=row.get("entityname"),
        entity_id=row.get("entityid"),
        title=row["title"],
        description=row.get("description"),
        status=row["status"],
        priority=row["priority"],
        created_at_utc=row["createdatutc"],
        due_at_utc=row.get("dueatutc"),
        final_decision_by_user_id=row.get("finaldecisionbyuserid"),
        final_decision_by=row.get("finaldecisionby"),
        final_decision_at_utc=row.get("finaldecisionatutc"),
        final_decision_reason=row.get("finaldecisionreason"),
    )


def _row_to_detail(item_row, decision_rows: list) -> ReviewItemDetail:
    decisions = [
        ReviewDecisionSummary(
            review_decision_id=d["reviewdecisionid"],
            review_item_id=d["reviewitemid"],
            decided_by_user_id=d["decidedbyuserid"],
            decided_by=d.get("decidedby"),
            decision=d["decision"],
            decision_reason=d.get("decisionreason"),
            created_at_utc=d["createdatutc"],
        )
        for d in decision_rows
    ]
    return ReviewItemDetail(
        review_item_id=item_row["reviewitemid"],
        company_id=item_row["companyid"],
        branch_id=item_row["branchid"],
        branch_name=item_row.get("branchname"),
        requested_by_user_id=item_row.get("requestedbyuserid"),
        requested_by=item_row.get("requestedby"),
        request_type=item_row["requesttype"],
        entity_schema=item_row.get("entityschema"),
        entity_name=item_row.get("entityname"),
        entity_id=item_row.get("entityid"),
        title=item_row["title"],
        description=item_row.get("description"),
        status=item_row["status"],
        priority=item_row["priority"],
        created_at_utc=item_row["createdatutc"],
        due_at_utc=item_row.get("dueatutc"),
        final_decision_by_user_id=item_row.get("finaldecisionbyuserid"),
        final_decision_by=item_row.get("finaldecisionby"),
        final_decision_at_utc=item_row.get("finaldecisionatutc"),
        final_decision_reason=item_row.get("finaldecisionreason"),
        old_value_json=item_row.get("oldvaluejson"),
        new_value_json=item_row.get("newvaluejson"),
        decisions=decisions,
    )


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------

async def get_review_items(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    branch_id: int | None = None,
    item_status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ReviewItemSummary]:
    """
    List review items accessible to the user (branch-scoped).

    Optionally filtered by branch_id and/or item status.
    Returns items ordered newest-first.

    CP-6 security: OwnDriverDataOnly (driver-role) users cannot access the
    review queue.  Review items expose payroll period data that drivers must
    not see until finalization.
    """
    # ── Driver-role hard-block ───────────────────────────────────────────────── #
    await _require_not_driver_role(company_id, user_id, db)

    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    # ── Review read permission — any payroll or review-decide role required ───── #
    _REVIEW_READ_PERMS = ["payroll.view", "payroll.entry", "payroll.finalize", "review.decide"]
    if can_see_all:
        await _check_any_permission(company_id, user_id, None, _REVIEW_READ_PERMS, db)
    else:
        permitted_branches = [
            bid for bid in branch_ids
            if await _has_any_permission(company_id, user_id, bid, _REVIEW_READ_PERMS, db)
        ]
        if not permitted_branches:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "You do not have permission to view review items "
                    "(required: payroll.view, payroll.entry, payroll.finalize, or review.decide)."
                ),
            )
        branch_ids = permitted_branches

    # If the caller requested a specific branch, verify they can access it.
    if branch_id is not None and not can_see_all and branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this branch.",
        )

    # Build WHERE clause fragments and params dict.
    filters = ["m.companyid = :company_id"]
    params: dict = {"company_id": company_id, "lim": limit, "off": offset}

    if branch_id is not None:
        filters.append("m.branchid = :branch_id")
        params["branch_id"] = branch_id
    elif not can_see_all:
        if not branch_ids:
            return []  # user has no branch access at all
        in_clause, in_params = _build_in_clause(branch_ids, "bid")
        filters.append(f"m.branchid IN ({in_clause})")
        params.update(in_params)

    if item_status is not None:
        filters.append("m.status = :item_status")
        params["item_status"] = item_status

    where = " AND ".join(filters)

    result = await db.execute(
        text(f"""
            {_ITEM_SELECT}
            WHERE  {where}
            ORDER BY m.createdatutc DESC
            LIMIT  :lim
            OFFSET :off
        """),
        params,
    )
    return [_row_to_summary(r) for r in result.mappings().all()]


async def get_review_item_by_id(
    review_item_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> ReviewItemDetail:
    """
    Fetch a single review item with its full payload and decision history.
    Enforces branch access.  ODA users are blocked (CP-6 security).
    """
    # ── Driver-role hard-block ───────────────────────────────────────────────── #
    await _require_not_driver_role(company_id, user_id, db)

    result = await db.execute(
        text(f"""
            {_ITEM_SELECT}
            WHERE  m.reviewitemid = :iid
              AND  m.companyid    = :cid
        """),
        {"iid": review_item_id, "cid": company_id},
    )
    row = result.mappings().first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review item {review_item_id} not found.",
        )

    # Branch access check.
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and row["branchid"] not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this branch.",
        )

    # ── Review read permission gate ──────────────────────────────────────────── #
    await _check_any_permission(
        company_id, user_id, row["branchid"],
        ["payroll.view", "payroll.entry", "payroll.finalize", "review.decide"],
        db,
    )

    # Fetch decisions ordered oldest-first (chronological timeline).
    dec_result = await db.execute(
        text("""
            SELECT
                d.reviewdecisionid,
                d.reviewitemid,
                d.decidedbyuserid,
                u.displayname  AS decidedby,
                d.decision,
                d.decisionreason,
                d.createdatutc
            FROM   review.managerreviewdecisions d
            JOIN   sec.users u ON u.userid = d.decidedbyuserid
            WHERE  d.reviewitemid = :iid
            ORDER BY d.createdatutc ASC
        """),
        {"iid": review_item_id},
    )
    decision_rows = dec_result.mappings().all()

    return _row_to_detail(row, decision_rows)


async def create_review_item(
    company_id: int,
    user_id: int,
    data: ReviewItemCreate,
    db: AsyncConnection,
) -> ReviewItemDetail:
    """
    Create a new review item (Status starts as Pending).

    Requires branch access + the ``payroll.entry`` permission on the target branch.

    PeriodApproval items may NOT be created manually — they are only auto-created
    by the payroll Open→InReview submission path.  Accepting free-form PeriodApproval
    items from the API would allow a caller to attach an arbitrary entity_id and trigger
    the decide_review_item() write-back against any InReview period.
    """
    # ── Driver-role hard-block ───────────────────────────────────────────────── #
    await _require_not_driver_role(company_id, user_id, db)

    # Block manual creation of PeriodApproval items.
    if data.request_type == "PeriodApproval":
        raise HTTPException(
            status_code=422,
            detail=(
                "PeriodApproval review items cannot be created manually. "
                "Submit a payroll period for review via "
                "PATCH /payroll/periods/{id}/status with status='InReview' — "
                "the system creates the review item automatically."
            ),
        )

    # Branch access check.
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and data.branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this branch.",
        )

    # Validate the target branch exists and belongs to this company.
    branch_check = await db.execute(
        text("""
            SELECT branchid FROM core.branches
            WHERE  branchid = :bid AND companyid = :cid
        """),
        {"bid": data.branch_id, "cid": company_id},
    )
    if branch_check.first() is None:
        raise HTTPException(
            status_code=422,
            detail=f"Branch {data.branch_id} not found for this company.",
        )

    # Permission gate: creating a review item requires payroll.entry.
    await _check_permission(company_id, user_id, data.branch_id, "payroll.entry", db)

    result = await db.execute(
        text("""
            INSERT INTO review.managerreviewitems (
                companyid, branchid, requestedbyuserid,
                requesttype, entityschema, entityname, entityid,
                title, description, oldvaluejson, newvaluejson,
                priority, dueatutc
            ) VALUES (
                :cid, :bid, :uid,
                :request_type, :entity_schema, :entity_name, :entity_id,
                :title, :description, :old_value_json, :new_value_json,
                :priority, :due_at_utc
            )
            RETURNING reviewitemid
        """),
        {
            "cid":            company_id,
            "bid":            data.branch_id,
            "uid":            user_id,
            "request_type":   data.request_type,
            "entity_schema":  data.entity_schema,
            "entity_name":    data.entity_name,
            "entity_id":      data.entity_id,
            "title":          data.title,
            "description":    data.description,
            "old_value_json": data.old_value_json,
            "new_value_json": data.new_value_json,
            "priority":       data.priority,
            "due_at_utc":     data.due_at_utc,
        },
    )
    new_id = result.scalar_one()

    # Audit: record the submission.  If this raises the INSERT rolls back.
    await _write_review_audit(
        db,
        company_id=company_id,
        branch_id=data.branch_id,
        user_id=user_id,
        item_id=new_id,
        action_code="REVIEW_ITEM_CREATED",
        new_value={
            "request_type": data.request_type,
            "title":        data.title,
            "priority":     data.priority,
        },
    )

    return await get_review_item_by_id(new_id, company_id, user_id, db)


async def decide_review_item(
    review_item_id: int,
    company_id: int,
    user_id: int,
    data: ReviewDecide,
    db: AsyncConnection,
) -> ReviewItemDetail:
    """
    Record a decision on a review item.

    Requires branch access + the ``review.decide`` permission on the item's branch.

    Decision semantics:
      - Approved / Rejected / EditRequested:
          Updates item Status to the decision value and records the final
          decision metadata (who, when, reason).
      - Comment:
          Inserts a decision record only; item Status is not changed.
          Used for discussion or request-for-info notes during review.

    Raises 422 if the item is already in a terminal status (not Pending / EditRequested).
    """
    # ── Driver-role hard-block ───────────────────────────────────────────────── #
    await _require_not_driver_role(company_id, user_id, db)

    # Fetch the item and acquire a row-level exclusive lock (FOR UPDATE).
    #
    # Why: the status check and the subsequent INSERT + UPDATE are not atomic
    # without a lock.  Two concurrent requests could both read status=Pending,
    # both pass the status gate, and both insert a decision row and update the
    # item — leaving ManagerReviewDecisions with two rows but FinalDecision
    # metadata reflecting only the last committer's values.
    #
    # FOR UPDATE holds the lock until this transaction commits/rolls back.
    # The second concurrent request blocks at this SELECT and, once the first
    # transaction commits, re-reads the now-Approved status and returns 422.
    result = await db.execute(
        text("""
            SELECT reviewitemid, branchid, status, requestedbyuserid,
                   requesttype, entityschema, entityname, entityid
            FROM   review.managerreviewitems
            WHERE  reviewitemid = :iid AND companyid = :cid
            FOR UPDATE
        """),
        {"iid": review_item_id, "cid": company_id},
    )
    row = result.mappings().first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review item {review_item_id} not found.",
        )

    # Branch access check.
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and row["branchid"] not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this branch.",
        )

    # Permission gate: deciding requires review.decide.
    await _check_permission(company_id, user_id, row["branchid"], "review.decide", db)

    # Self-approval policy gate (substantive decisions only — Comments are always allowed).
    # Reads AllowSelfApproval from core.Companies; defaults to TRUE if the column is absent
    # (safety guard for environments not yet migrated to 0004).
    if data.decision != "Comment" and row.get("requestedbyuserid") == user_id:
        policy_row = await db.execute(
            text("SELECT allowselfapproval FROM core.companies WHERE companyid = :cid"),
            {"cid": company_id},
        )
        allow_self = policy_row.scalar_one_or_none()
        if allow_self is False:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Self-approval is not permitted for this company. "
                    "The user who submitted a review item cannot approve, reject, "
                    "or request edits on it.  Another user with review.decide "
                    "permission must record the decision."
                ),
            )

    # Status gate: can only decide on decidable items.
    if row["status"] not in _DECIDABLE_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot record a decision on an item with status '{row['status']}'. "
                f"Decisions are only allowed when the item is in "
                f"{' or '.join(sorted(_DECIDABLE_STATUSES))} status."
            ),
        )

    # Insert the decision record.
    await db.execute(
        text("""
            INSERT INTO review.managerreviewdecisions (
                reviewitemid, decidedbyuserid, decision, decisionreason
            ) VALUES (
                :iid, :uid, :decision, :reason
            )
        """),
        {
            "iid":      review_item_id,
            "uid":      user_id,
            "decision": data.decision,
            "reason":   data.decision_reason,
        },
    )

    # For substantive decisions (not Comment), update the item status,
    # record the final decision metadata, and write to the audit log.
    if data.decision != "Comment":
        await db.execute(
            text("""
                UPDATE review.managerreviewitems
                SET    status                = :new_status,
                       finaldecisionbyuserid = :uid,
                       finaldecisionatutc    = NOW(),
                       finaldecisionreason   = :reason
                WHERE  reviewitemid = :iid
            """),
            {
                "new_status": data.decision,   # Approved / Rejected / EditRequested
                "uid":        user_id,
                "reason":     data.decision_reason,
                "iid":        review_item_id,
            },
        )

        # Audit: record the decision.  If this raises, the decision INSERT
        # and the item UPDATE both roll back (same engine.begin() transaction).
        await _write_review_audit(
            db,
            company_id=company_id,
            branch_id=row["branchid"],
            user_id=user_id,
            item_id=review_item_id,
            action_code="REVIEW_ITEM_DECIDED",
            old_value={"status": row["status"]},
            new_value={
                "status":   data.decision,
                "decision": data.decision,
                "reason":   data.decision_reason,
            },
        )

    # M16: PeriodApproval write-back.
    #
    # When a substantive decision is made on a PeriodApproval review item, the
    # linked payroll period status is updated atomically in the same transaction:
    #   Approved       → period Approved
    #   Rejected       → period Open
    #   EditRequested  → period Open  (reviewer wants corrections; period returns to entry)
    #   Comment        → no change
    #
    # The period UPDATE uses "WHERE status = 'InReview' RETURNING" (atomic claim)
    # so that a concurrent decision or manual status change that already moved the
    # period cannot cause a double-advance.
    #
    # Security: before writing back, we fully validate the linked period:
    #   - entity_id parses as a positive integer
    #   - period exists in the same company
    #   - period's branch matches the review item's branch
    #   - entity_schema / entity_name match exactly
    # These checks prevent a forged review item (created before the manual-creation
    # block was added) from writing back to an unrelated period.
    # Driver-role block (belt-and-suspenders — _require_not_driver_role at the
    # top of decide_review_item already covers this, but kept explicit here).
    await _require_not_driver_role(company_id, user_id, db)

    if data.decision != "Comment" and row.get("requesttype") == "PeriodApproval":
        # Validate entity fields set by the auto-create path.
        if row.get("entityschema") != "payroll" or row.get("entityname") != "PayrollPeriods":
            raise HTTPException(
                status_code=422,
                detail=(
                    "PeriodApproval review item has unexpected entity metadata. "
                    "Cannot apply period write-back."
                ),
            )
        try:
            period_id = int(row["entityid"])
            if period_id <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422,
                detail="PeriodApproval review item has an invalid entity_id.",
            )

        # Validate the linked period: must exist, same company, same branch as
        # the review item, and currently InReview.  This is a pre-flight check
        # that produces a clear error message before we attempt the atomic UPDATE.
        period_check = await db.execute(
            text("""
                SELECT payrollperiodid, branchid, status
                FROM   payroll.payrollperiods
                WHERE  payrollperiodid = :pid
                  AND  companyid       = :cid
            """),
            {"pid": period_id, "cid": company_id},
        )
        period_pre = period_check.mappings().first()
        if period_pre is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"PeriodApproval review item references period {period_id} "
                    f"which does not exist in this company."
                ),
            )
        if int(period_pre["branchid"]) != int(row["branchid"]):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"PeriodApproval review item branch ({row['branchid']}) does not "
                    f"match the linked period's branch ({period_pre['branchid']}). "
                    f"Cannot apply write-back."
                ),
            )

        # CP-6: NMR guard for Approved decisions.
        #
        # The InReview submission guard (transition_period_status) already blocks
        # submission when NeedsManagerReview lines exist, so InReview periods
        # should normally have 0 NMR lines.  However, if a rate was voided or
        # modified after submission (a rare but possible edge case), NMR lines
        # could reappear.  The backend is the authority; we enforce the check
        # here so that approving via the review interface never bypasses it.
        #
        # Return/Reject (EditRequested) are NOT blocked by NMR lines — the
        # reviewer is explicitly returning the period for correction, which is
        # the correct action when NMR lines exist.
        if data.decision == "Approved":
            nmr_check = await db.execute(
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
            nmr_count = int(nmr_check.scalar_one())
            if nmr_count > 0:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Cannot approve: {nmr_count} draft line(s) require manager "
                        f"review (NeedsManagerReview=True). Resolve these lines before "
                        f"approving the period."
                    ),
                )

        # Map decision → new period status
        if data.decision == "Approved":
            new_period_status = "Approved"
        else:
            # Rejected or EditRequested both return the period to Open
            new_period_status = "Open"

        # Stamp approvedbyuserid/approvedatutc when moving to Approved
        if new_period_status == "Approved":
            extra_set = ", approvedbyuserid = :approver, approvedatutc = NOW()"
            extra_params: dict = {"approver": user_id}
        else:
            extra_set = ""
            extra_params = {}

        # Atomic claim: only updates if period is still InReview.
        # Prevents double-advance when two concurrent decisions race.
        period_result = await db.execute(
            text(
                f"UPDATE payroll.payrollperiods "
                f"SET    status = :new_status{extra_set} "
                f"WHERE  payrollperiodid = :pid "
                f"  AND  companyid       = :cid "
                f"  AND  status          = 'InReview' "
                f"RETURNING payrollperiodid, branchid"
            ),
            {"new_status": new_period_status, "pid": period_id, "cid": company_id,
             **extra_params},
        )
        period_row = period_result.mappings().first()
        if period_row is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Period is no longer in InReview status — it may have been "
                    "returned or cancelled concurrently. The review decision has been "
                    "rolled back."
                ),
            )

        # Write the period status-change audit entry (same transaction).
        await _write_period_status_audit(
            db,
            company_id=company_id,
            branch_id=int(period_row["branchid"]),
            user_id=user_id,
            period_id=period_id,
            old_status="InReview",
            new_status=new_period_status,
        )

    return await get_review_item_by_id(review_item_id, company_id, user_id, db)
