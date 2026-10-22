"""Driver Transfer Workflow service layer.

All SQL is raw parameterised via sqlalchemy.text().
All authorization is enforced before any write occurs.

A transfer moves one permanent Driver from its current home-Branch assignment to
a new assignment in the target Branch; the Driver, driver number and CDL never
change. Stored request statuses:
PendingSourceApproval, PendingTargetApproval, Returned, Rejected, Approved,
Scheduled, Completed, Cancelled.

Transfer lifecycle:
  create   → PendingSourceApproval   (when initiated_by='Driver' — by DRIVER/Self user or manager)
           → PendingTargetApproval   (when initiated_by='SourceBranch' — source
                                       approval is implicit)
  approve_source → PendingTargetApproval
                   (accepts PendingSourceApproval OR Returned status)
  decide_target  → Approved | Rejected | Returned
  complete       → Completed  (EffectiveAt NULL: the decision instant is the boundary; the
                               source assignment closes and the destination opens at it)
                 → Scheduled  (future EffectiveAt: both windows are materialized now and
                               currentness switches at EffectiveAt by time alone)
  reschedule     → same status (before completion: only EffectiveAt changes; Scheduled:
                               both assignment boundaries move, destination id is stable)
  cancel         → Cancelled  (a Scheduled transfer only while EffectiveAt is still future)

The assignment windows are the single authority for currentness. The request
status every reader sees comes from ``core.fn_DriverTransferStatus``: a stored
Scheduled request reads as Completed once its EffectiveAt has been reached.

Lock order: Employee → transfer request → the Driver's assignments (ascending) →
Branch workflow advisory locks (ascending BranchID) → PayrollPeriod rows (ascending).
Every future/past decision of complete, cancel and reschedule uses one wall-clock
instant (``decision_instant``) read after those locks, never the transaction-start NOW().
A terminated Employee's Scheduled transfer can no longer be cancelled or rescheduled.

P1 scope rules:
  - DRIVER/Self users may call create only for their own Driver with initiated_by='Driver'.
    All other transfer lifecycle endpoints are blocked for DRIVER/Self users.
  - Operational users need drivers.view to list/get.
  - Operational users need drivers.edit on the relevant branch for write operations.
  - list/get results are filtered to branches the caller can see.

P2 behavior:
  - Returned: target returned the request for source revision.  Source may
    re-approve (approve_source accepts both PendingSourceApproval and Returned).
    This preserves the returned status in history while allowing the workflow
    to progress without a new endpoint.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.branch_access import check_branch_access
from app.access.permissions import check_permission, has_any_permission
from app.access.policy import (
    is_driver_self_subject,
    require_non_driver_subject,
    resolve_driver_self,
)
from app.transfer.schemas import (
    CancelRequest,
    DriverTransferCreate,
    DriverTransferResponse,
    RescheduleRequest,
    SourceApprovalRequest,
    TargetDecisionRequest,
    TransferDriverCandidate,
    TransferListResponse,
)
from app.workforce import audit as workforce_audit
from app.workforce.effective import resolve_effective_driver_assignment
from app.workforce.ops import decision_instant, workforce_operation
from app.workforce.projection import sync_employee_branch_projection

log = logging.getLogger(__name__)

# Effective statuses (after core.fn_DriverTransferStatus) that end a request's life.
_TERMINAL = {"Completed", "Cancelled", "Rejected"}

# Read permissions for transfer data
_TRANSFER_READ_PERMS = ["drivers.view", "drivers.edit"]

_TRANSFER_SELECT = """
    SELECT
        dtr.transferrequestid,
        dtr.companyid,
        dtr.driverid,
        dtr.sourcedriverassignmentid,
        dtr.destinationdriverassignmentid,
        dtr.sourcebranchid,
        dtr.targetbranchid,
        dtr.requestedbyuserid,
        dtr.initiatedby,
        dtr.status AS stored_status,
        core.fn_DriverTransferStatus(dtr.status, dtr.effectiveat) AS status,
        dtr.effectiveat,
        dtr.reason,
        dtr.notes,
        dtr.sourceapprovedbyuserid,
        dtr.sourceapprovedatutc,
        dtr.targetdecidedbyuserid,
        dtr.targetdecidedatutc,
        dtr.targetdecisionnotes,
        dtr.scheduledatutc,
        dtr.scheduledbyuserid,
        dtr.completedatutc,
        dtr.completedbyuserid,
        dtr.cancelledatutc,
        dtr.cancelledbyuserid,
        dtr.cancelreason,
        dtr.createdatutc,
        dtr.updatedatutc,
        -- Denormalized display fields
        dr.drivernumber    AS driver_number,
        e.fullname         AS driver_name,
        sb.branchname      AS source_branch_name,
        tb.branchname      AS target_branch_name,
        u.displayname      AS requested_by_name
    FROM   core.drivertransferrequests dtr
    JOIN   core.drivers   dr ON dr.driverid  = dtr.driverid
    JOIN   core.employees e  ON e.employeeid = dr.employeeid
    JOIN   core.branches  sb ON sb.branchid  = dtr.sourcebranchid
    JOIN   core.branches  tb ON tb.branchid  = dtr.targetbranchid
    JOIN   sec.users      u  ON u.userid      = dtr.requestedbyuserid
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(UTC)


async def _get_transfer_or_404(
    transfer_request_id: int,
    company_id: int,
    db: AsyncConnection,
    *,
    at: datetime | None = None,
) -> dict:
    """Load one request; ``at`` evaluates the effective status at a decision instant."""
    select = _TRANSFER_SELECT
    params: dict = {"tid": transfer_request_id, "cid": company_id}
    if at is not None:
        select = select.replace(
            "core.fn_DriverTransferStatus(dtr.status, dtr.effectiveat) AS status",
            "core.fn_DriverTransferStatus(dtr.status, dtr.effectiveat, CAST(:at AS TIMESTAMPTZ)) AS status",
        )
        params["at"] = at
    result = await db.execute(
        text(f"""
            {select}
            WHERE  dtr.transferrequestid = :tid
              AND  dtr.companyid         = :cid
        """),
        params,
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Transfer request not found.")
    return dict(row)


async def _lock_driver_assignments(
    driver_id: int,
    company_id: int,
    db: AsyncConnection,
) -> None:
    await db.execute(
        text("""
            SELECT DriverAssignmentID
            FROM core.DriverAssignments
            WHERE CompanyID = :company_id AND DriverID = :driver_id
            ORDER BY DriverAssignmentID
            FOR NO KEY UPDATE
        """),
        {"company_id": company_id, "driver_id": driver_id},
    )


async def _lock_transfer_lifecycle(
    transfer_request_id: int,
    company_id: int,
    db: AsyncConnection,
) -> tuple[int, dict]:
    """Lock Employee, request, then all of the Driver's assignments in canonical order."""
    identity = await db.execute(
        text("""
            SELECT dr.EmployeeID, dr.DriverID
            FROM core.DriverTransferRequests dtr
            JOIN core.Drivers dr ON dr.DriverID = dtr.DriverID
            WHERE dtr.TransferRequestID = :request_id AND dtr.CompanyID = :company_id
        """),
        {"request_id": transfer_request_id, "company_id": company_id},
    )
    identity_row = identity.mappings().first()
    if identity_row is None:
        raise HTTPException(status_code=404, detail="Transfer request not found.")
    employee_id = identity_row["employeeid"]
    locked_employee = await db.execute(
        text("""
            SELECT EmployeeID
            FROM core.Employees
            WHERE EmployeeID = :employee_id AND CompanyID = :company_id
            FOR UPDATE
        """),
        {"employee_id": employee_id, "company_id": company_id},
    )
    if locked_employee.first() is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    request_lock = await db.execute(
        text("""
            SELECT TransferRequestID
            FROM core.DriverTransferRequests
            WHERE TransferRequestID = :request_id AND CompanyID = :company_id
            FOR UPDATE
        """),
        {"request_id": transfer_request_id, "company_id": company_id},
    )
    if request_lock.first() is None:
        raise HTTPException(status_code=404, detail="Transfer request not found.")
    await _lock_driver_assignments(identity_row["driverid"], company_id, db)
    return employee_id, await _get_transfer_or_404(transfer_request_id, company_id, db)


async def _lock_employee_for_transfer_create(
    driver_id: int,
    company_id: int,
    db: AsyncConnection,
) -> tuple[int, dict]:
    identity = await db.execute(
        text("""
            SELECT EmployeeID
            FROM core.Drivers
            WHERE DriverID = :driver_id AND CompanyID = :company_id
        """),
        {"driver_id": driver_id, "company_id": company_id},
    )
    row = identity.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Driver not found.")
    employee_id = row["employeeid"]
    employee_result = await db.execute(
        text("""
            SELECT EmployeeID, BranchID
            FROM core.Employees
            WHERE EmployeeID = :employee_id AND CompanyID = :company_id
            FOR UPDATE
        """),
        {"employee_id": employee_id, "company_id": company_id},
    )
    employee = employee_result.mappings().first()
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    await _lock_driver_assignments(driver_id, company_id, db)
    return employee_id, dict(employee)


def _row_to_response(row: dict) -> DriverTransferResponse:
    completed_at = row["completedatutc"]
    completed_by = row["completedbyuserid"]
    if row["stored_status"] == "Scheduled" and row["status"] == "Completed":
        # A scheduled transfer completes at its EffectiveAt instant.
        completed_at = row["effectiveat"]
        completed_by = row["scheduledbyuserid"]
    return DriverTransferResponse(
        transfer_request_id=row["transferrequestid"],
        company_id=row["companyid"],
        driver_id=row["driverid"],
        driver_number=row.get("driver_number"),
        source_driver_assignment_id=row["sourcedriverassignmentid"],
        destination_driver_assignment_id=row["destinationdriverassignmentid"],
        source_branch_id=row["sourcebranchid"],
        target_branch_id=row["targetbranchid"],
        requested_by_user_id=row["requestedbyuserid"],
        initiated_by=row["initiatedby"],
        status=row["status"],
        stored_status=row["stored_status"],
        effective_at=row["effectiveat"],
        reason=row["reason"],
        notes=row["notes"],
        source_approved_by_user_id=row["sourceapprovedbyuserid"],
        source_approved_at_utc=row["sourceapprovedatutc"],
        target_decided_by_user_id=row["targetdecidedbyuserid"],
        target_decided_at_utc=row["targetdecidedatutc"],
        target_decision_notes=row["targetdecisionnotes"],
        scheduled_at_utc=row["scheduledatutc"],
        scheduled_by_user_id=row["scheduledbyuserid"],
        completed_at_utc=completed_at,
        completed_by_user_id=completed_by,
        cancelled_at_utc=row["cancelledatutc"],
        cancelled_by_user_id=row["cancelledbyuserid"],
        cancel_reason=row["cancelreason"],
        created_at_utc=row["createdatutc"],
        updated_at_utc=row["updatedatutc"],
        driver_name=row.get("driver_name"),
        source_branch_name=row.get("source_branch_name"),
        target_branch_name=row.get("target_branch_name"),
        requested_by_name=row.get("requested_by_name"),
    )


async def _assert_no_active_transfer(
    driver_id: int,
    company_id: int,
    db: AsyncConnection,
) -> None:
    """Raise 422 if the Driver already has an in-flight transfer request.

    A Scheduled request is in flight only until its EffectiveAt; afterwards it
    reads Completed and the Driver's new assignment may start another transfer.
    """
    result = await db.execute(
        text("""
            SELECT transferrequestid FROM core.drivertransferrequests
            WHERE  driverid  = :did
              AND  companyid = :cid
              AND  core.fn_DriverTransferStatus(status, effectiveat)
                       NOT IN ('Completed','Cancelled','Rejected')
            LIMIT 1
        """),
        {"did": driver_id, "cid": company_id},
    )
    if result.first() is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Driver already has an active transfer request. "
                "Cancel or complete it before creating a new one."
            ),
        )


async def _get_accessible_branches_for_transfers(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> tuple[bool, list[int]]:
    """
    Return (can_see_all, branch_ids) for transfer read access.

    Caller must have drivers.view (or drivers.edit) on at least one branch.
    - AllCompanyBranches scope + drivers.view or drivers.edit → can_see_all=True, branch_ids=[]
    - SpecificBranch scope → branch_ids = branches where caller has drivers.view
    - No qualifying permission → raises HTTP 403

    This function is for OPERATIONAL users only.  DRIVER/Self users are blocked before
    this is called.
    """
    can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)

    if can_see_all:
        # Company level (branch_id=None → AllCompanyBranches only): drivers.view or drivers.edit,
        # the same pair a SpecificBranch caller is held to below.
        if not await has_any_permission(company_id, user_id, None, _TRANSFER_READ_PERMS, db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "You do not have permission to perform this action "
                    f"(required: one of {_TRANSFER_READ_PERMS})."
                ),
            )
        return True, []

    # SpecificBranch: keep only branches where caller has drivers.view or drivers.edit
    allowed: list[int] = []
    for bid in branch_ids:
        if await has_any_permission(company_id, user_id, bid, _TRANSFER_READ_PERMS, db):
            allowed.append(bid)

    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You do not have drivers.view permission on any branch. "
                "Transfer request data is not accessible."
            ),
        )
    return False, allowed


async def _require_edit_on_source_or_target(
    company_id: int,
    user_id: int,
    row: dict,
    db: AsyncConnection,
    *,
    action: str,
) -> None:
    src_ok = await has_any_permission(
        company_id, user_id, row["sourcebranchid"], ["drivers.edit"], db
    )
    tgt_ok = await has_any_permission(
        company_id, user_id, row["targetbranchid"], ["drivers.edit"], db
    )
    if not (src_ok or tgt_ok):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You need drivers.edit on the source or target branch to {action}.",
        )


async def _db_instant_is_future(instant: datetime, db: AsyncConnection) -> bool:
    result = await db.execute(text("SELECT CAST(:instant AS TIMESTAMPTZ) > NOW()"),
                              {"instant": instant})
    return bool(result.scalar_one())


# ---------------------------------------------------------------------------
# Service functions
# ---------------------------------------------------------------------------

async def create_driver_transfer_request(
    company_id: int,
    user_id: int,
    data: DriverTransferCreate,
    db: AsyncConnection,
) -> DriverTransferResponse:
    """
    Create a new transfer request for a permanent Driver.

    The source is the Driver's current home-Branch assignment.

    Two caller types are supported:

    DRIVER/Self users:
      - May only initiate for their OWN Driver.
      - initiated_by must be 'Driver'.
      - Source-branch approval is still required (PendingSourceApproval).
      - No operational payroll data is touched or exposed.

    Operational users (SpecificBranch / AllCompanyBranches with drivers.edit):
      - May initiate for any Driver with a current assignment in their accessible
        source branch.
      - initiated_by may be 'Driver' or 'SourceBranch'.
      - When 'SourceBranch', source approval is implicit (goes to PendingTargetApproval).
    """
    caller_is_driver_self = await is_driver_self_subject(company_id, user_id, db)

    if caller_is_driver_self and data.initiated_by != "Driver":
        raise HTTPException(
            status_code=422,
            detail="Driver-role users must set initiated_by='Driver'.",
        )
    if caller_is_driver_self:
        await resolve_driver_self(
            company_id,
            user_id,
            db,
            target_driver_id=data.driver_id,
        )

    employee_id, _ = await _lock_employee_for_transfer_create(
        data.driver_id, company_id, db,
    )
    await sync_employee_branch_projection(
        company_id, employee_id, None, db,
    )
    current = await resolve_effective_driver_assignment(company_id, data.driver_id, None, db)
    if current is None:
        raise HTTPException(status_code=422, detail="Driver has no current assignment to transfer from.")
    if current["endreason"] is not None:
        raise HTTPException(
            status_code=422,
            detail="A Driver assignment that is already ending cannot start another transfer.",
        )
    if data.effective_at is not None:
        if not await _db_instant_is_future(data.effective_at, db):
            raise HTTPException(status_code=422, detail="Scheduled transfer time must be in the future.")
        if data.effective_at <= current["effectivefromat"]:
            raise HTTPException(status_code=422, detail="Transfer time must be after the source assignment start instant.")
    source_branch_id = int(current["branchid"])
    if not caller_is_driver_self:
        await check_permission(company_id, user_id, source_branch_id, "drivers.edit", db)

    tgt_result = await db.execute(
        text("""
            SELECT branchid FROM core.branches
            WHERE  branchid  = :bid AND companyid = :cid
        """),
        {"bid": data.target_branch_id, "cid": company_id},
    )
    if tgt_result.first() is None:
        raise HTTPException(status_code=404, detail="Target branch not found.")

    if source_branch_id == data.target_branch_id:
        raise HTTPException(
            status_code=422,
            detail="Source and target branch must be different.",
        )

    await _assert_no_active_transfer(data.driver_id, company_id, db)
    pending_result = await db.execute(
        text("""
            SELECT 1 FROM core.DriverAssignments
            WHERE CompanyID = :company_id AND DriverID = :driver_id
              AND DriverAssignmentID <> :source_assignment_id
              AND EffectiveFromAt > NOW()
              AND (EffectiveToAt IS NULL OR EffectiveToAt > EffectiveFromAt)
            LIMIT 1
        """),
        {
            "company_id": company_id,
            "driver_id": data.driver_id,
            "source_assignment_id": current["driverassignmentid"],
        },
    )
    if pending_result.first() is not None:
        raise HTTPException(
            status_code=422,
            detail="A pending Driver assignment already exists for this Driver.",
        )

    # DRIVER/Self users always get PendingSourceApproval.
    # SourceBranch-initiated by operational users gets PendingTargetApproval.
    if caller_is_driver_self or data.initiated_by == "Driver":
        initial_status = "PendingSourceApproval"
        source_approved_by = None
        source_approved_at = None
    else:
        initial_status = "PendingTargetApproval"
        source_approved_by = user_id
        source_approved_at = _now_utc()

    now = _now_utc()
    ins = await db.execute(
        text("""
            INSERT INTO core.drivertransferrequests (
                companyid, driverid, sourcedriverassignmentid,
                sourcebranchid, targetbranchid,
                requestedbyuserid, initiatedby, status,
                effectiveat, reason, notes,
                sourceapprovedbyuserid, sourceapprovedatutc,
                createdatutc
            ) VALUES (
                :cid, :did, :said,
                :sbid, :tbid,
                :uid, :initiated_by, :status,
                :effective_at, :reason, :notes,
                :src_by, :src_at,
                :now
            )
            RETURNING transferrequestid
        """),
        {
            "cid":          company_id,
            "did":          data.driver_id,
            "said":         current["driverassignmentid"],
            "sbid":         source_branch_id,
            "tbid":         data.target_branch_id,
            "uid":          user_id,
            "initiated_by": data.initiated_by,
            "status":       initial_status,
            "effective_at": data.effective_at,
            "reason":       data.reason,
            "notes":        data.notes,
            "src_by":       source_approved_by,
            "src_at":       source_approved_at,
            "now":          now,
        },
    )
    new_id: int = ins.scalar_one()
    row = await _get_transfer_or_404(new_id, company_id, db)
    return _row_to_response(row)


async def approve_source_transfer(
    company_id: int,
    user_id: int,
    transfer_request_id: int,
    data: SourceApprovalRequest,
    db: AsyncConnection,
) -> DriverTransferResponse:
    """
    Source-branch manager approves a PendingSourceApproval or Returned request.
    Transitions → PendingTargetApproval.

    Accepts both PendingSourceApproval (initial driver request) and Returned
    (target branch returned for revision) so that the Returned status can
    progress without requiring a separate resubmit endpoint.
    """
    await require_non_driver_subject(company_id, user_id, db)

    _, row = await _lock_transfer_lifecycle(transfer_request_id, company_id, db)

    if row["status"] not in ("PendingSourceApproval", "Returned"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Request is '{row['status']}'. "
                "approve-source requires PendingSourceApproval or Returned status."
            ),
        )

    await check_permission(
        company_id, user_id, row["sourcebranchid"], "drivers.edit", db
    )

    now = _now_utc()
    changed = await db.execute(
        text("""
            UPDATE core.drivertransferrequests
            SET    status                  = 'PendingTargetApproval',
                   sourceapprovedbyuserid  = :uid,
                   sourceapprovedatutc     = :now,
                   notes                  = COALESCE(:notes, notes),
                   updatedatutc           = :now
            WHERE  transferrequestid = :tid AND status = :old_status
            RETURNING transferrequestid
        """),
        {"uid": user_id, "now": now, "notes": data.notes,
         "tid": transfer_request_id, "old_status": row["status"]},
    )
    if changed.scalar_one_or_none() is None:
        raise HTTPException(status_code=409, detail="Transfer request changed; refresh and retry.")
    row = await _get_transfer_or_404(transfer_request_id, company_id, db)
    return _row_to_response(row)


async def decide_target_transfer(
    company_id: int,
    user_id: int,
    transfer_request_id: int,
    data: TargetDecisionRequest,
    db: AsyncConnection,
) -> DriverTransferResponse:
    """
    Target-branch manager accepts (Approved), rejects (Rejected), or
    returns (Returned) the request.

    Returned: the request goes back to source branch for revision.
    Source may then re-approve (approve_source accepts Returned).
    """
    await require_non_driver_subject(company_id, user_id, db)

    _, row = await _lock_transfer_lifecycle(transfer_request_id, company_id, db)

    if row["status"] != "PendingTargetApproval":
        raise HTTPException(
            status_code=422,
            detail=f"Request is '{row['status']}', not PendingTargetApproval.",
        )

    await check_permission(
        company_id, user_id, row["targetbranchid"], "drivers.edit", db
    )

    now = _now_utc()
    new_status: str
    if data.decision == "Approved":
        new_status = "Approved"
    elif data.decision == "Rejected":
        new_status = "Rejected"
    else:  # "Returned"
        new_status = "Returned"

    changed = await db.execute(
        text("""
            UPDATE core.drivertransferrequests
            SET    status                 = :status,
                   targetdecidedbyuserid = :uid,
                   targetdecidedatutc    = :now,
                   targetdecisionnotes   = :dnotes,
                   updatedatutc          = :now
            WHERE  transferrequestid = :tid AND status = 'PendingTargetApproval'
            RETURNING transferrequestid
        """),
        {
            "status": new_status,
            "uid":    user_id,
            "now":    now,
            "dnotes": data.decision_notes,
            "tid":    transfer_request_id,
        },
    )
    if changed.scalar_one_or_none() is None:
        raise HTTPException(status_code=409, detail="Transfer request changed; refresh and retry.")
    row = await _get_transfer_or_404(transfer_request_id, company_id, db)
    return _row_to_response(row)


async def complete_driver_transfer(
    company_id: int,
    user_id: int,
    transfer_request_id: int,
    db: AsyncConnection,
) -> DriverTransferResponse:
    """Complete an Approved transfer in one transaction.

    The same source-close and destination-open windows are materialized in both
    cases: an immediate request (EffectiveAt NULL) switches at the decision instant and
    is Completed; a future EffectiveAt leaves the destination pending and the request
    Scheduled until that instant.

    Closing the source removes its coverage from the boundary onward, so every
    PayrollPeriod overlapping that interval is locked (lock-only: no period state
    blocks a completion) before any window changes, and the source's payroll facts are
    verified against the new window under those locks.
    """
    from app.payroll.eligibility import (
        BoundaryWindow,
        append_driver_assignment_to_editable_periods,
        assert_assignment_payroll_covered,
        lock_boundary_periods,
    )

    await require_non_driver_subject(company_id, user_id, db)

    _, row = await _lock_transfer_lifecycle(transfer_request_id, company_id, db)

    if row["stored_status"] != "Approved":
        raise HTTPException(
            status_code=422,
            detail=f"Request is '{row['status']}', not Approved.",
        )

    await _require_edit_on_source_or_target(
        company_id, user_id, row, db, action="complete this transfer",
    )

    source_result = await db.execute(
        text("""
            SELECT DriverAssignmentID, DriverID, EmployeeID, BranchID, EndReason,
                   EffectiveFromAt, EffectiveToAt
            FROM core.DriverAssignments
            WHERE DriverAssignmentID = :assignment_id AND CompanyID = :company_id
        """),
        {"assignment_id": row["sourcedriverassignmentid"], "company_id": company_id},
    )
    source = source_result.mappings().first()
    if source is None or source["endreason"] is not None:
        raise HTTPException(
            status_code=409,
            detail="Transfer source is no longer available for completion.",
        )

    # Lock every period the source loses coverage in, before anything changes. An
    # immediate boundary is the decision instant, which is never before the
    # transaction start, so the transaction start bounds the interval from below.
    lower_bound = row["effectiveat"]
    if lower_bound is None:
        lower_bound = (await db.execute(text("SELECT NOW()"))).scalar_one()
    await lock_boundary_periods(
        company_id,
        [BoundaryWindow(row["sourcebranchid"], lower_bound, None),
         BoundaryWindow(row["targetbranchid"], lower_bound, None)],
        db,
        lock_only=True,
    )
    decided_at = await decision_instant(db)

    employee_id = source["employeeid"]
    current = await resolve_effective_driver_assignment(company_id, row["driverid"], decided_at, db)
    if current is None or current["driverassignmentid"] != source["driverassignmentid"]:
        raise HTTPException(status_code=409, detail="Transfer source is no longer current.")

    requested_at = row["effectiveat"]
    if requested_at is not None and requested_at <= decided_at:
        raise HTTPException(status_code=409, detail="Scheduled transfer time has expired.")
    effective_at = requested_at if requested_at is not None else decided_at
    is_future = requested_at is not None
    if effective_at <= source["effectivefromat"]:
        raise HTTPException(status_code=409, detail="Transfer instant precedes the source assignment window.")
    pending_result = await db.execute(
        text("""
            SELECT 1 FROM core.DriverAssignments
            WHERE CompanyID = :company_id AND DriverID = :driver_id
              AND DriverAssignmentID <> :source_assignment_id
              AND EffectiveFromAt > :decided_at
              AND (EffectiveToAt IS NULL OR EffectiveToAt > EffectiveFromAt)
            LIMIT 1
        """),
        {
            "company_id": company_id,
            "driver_id": row["driverid"],
            "source_assignment_id": source["driverassignmentid"],
            "decided_at": decided_at,
        },
    )
    if pending_result.first() is not None:
        raise HTTPException(status_code=409, detail="Driver already has a pending assignment.")

    employee_before = await db.execute(
        text("""
            SELECT BranchID FROM core.Employees
            WHERE CompanyID = :company_id AND EmployeeID = :employee_id
        """),
        {"company_id": company_id, "employee_id": employee_id},
    )
    old_employee_branch = employee_before.scalar_one()

    # Insert the destination assignment of the same Driver at the one shared boundary.
    new_assignment_ins = await db.execute(
        text("""
            INSERT INTO core.DriverAssignments (
                CompanyID, BranchID, EmployeeID, DriverID,
                EffectiveFromAt, TransferredFromAssignmentID
            ) VALUES (
                :cid, :tbid, :eid, :did,
                :effective_from_at, :from_aid
            )
            RETURNING DriverAssignmentID
        """),
        {
            "cid":      company_id,
            "tbid":     row["targetbranchid"],
            "eid":      employee_id,
            "did":      row["driverid"],
            "effective_from_at": effective_at,
            "from_aid": source["driverassignmentid"],
        },
    )
    new_assignment_id: int = new_assignment_ins.scalar_one()

    # Close the source: an open assignment ends at the decision instant or later, so
    # no workforce operation is needed.
    source_closed = await db.execute(
        text("""
            UPDATE core.DriverAssignments
            SET    EndReason                  = 'Transferred',
                   TransferredToAssignmentID  = :new_aid,
                   EffectiveToAt              = :effective_to_at,
                   UpdatedAtUtc               = NOW()
            WHERE  DriverAssignmentID = :old_aid
              AND  CompanyID = :cid
              AND  EffectiveToAt IS NULL
            RETURNING DriverAssignmentID
        """),
        {"new_aid": new_assignment_id, "effective_to_at": effective_at,
         "old_aid": source["driverassignmentid"], "cid": company_id},
    )
    if source_closed.scalar_one_or_none() is None:
        raise HTTPException(status_code=409, detail="Transfer source changed; refresh and retry.")

    # The source no longer covers WorkDates from the boundary onward; none of its
    # payroll facts may sit there (period rows are locked, so this cannot race a writer).
    await assert_assignment_payroll_covered(
        company_id, row["sourcebranchid"], source["driverassignmentid"], db,
    )
    await append_driver_assignment_to_editable_periods(
        company_id, row["targetbranchid"], new_assignment_id, user_id, db,
    )

    if is_future:
        request_updated = await db.execute(
            text("""
                UPDATE core.drivertransferrequests
                SET    status                           = 'Scheduled',
                       effectiveat                      = :effective_at,
                       destinationdriverassignmentid    = :new_aid,
                       scheduledatutc                   = NOW(),
                       scheduledbyuserid                = :uid,
                       updatedatutc                     = NOW()
                WHERE  transferrequestid = :tid AND status = 'Approved'
                RETURNING transferrequestid
            """),
            {"new_aid": new_assignment_id, "effective_at": effective_at,
             "uid": user_id, "tid": transfer_request_id},
        )
    else:
        request_updated = await db.execute(
            text("""
                UPDATE core.drivertransferrequests
                SET    status                           = 'Completed',
                       effectiveat                      = :effective_at,
                       destinationdriverassignmentid    = :new_aid,
                       completedatutc                   = NOW(),
                       completedbyuserid                = :uid,
                       updatedatutc                     = NOW()
                WHERE  transferrequestid = :tid AND status = 'Approved'
                RETURNING transferrequestid
            """),
            {"new_aid": new_assignment_id, "effective_at": effective_at,
             "uid": user_id, "tid": transfer_request_id},
        )
    if request_updated.scalar_one_or_none() is None:
        raise HTTPException(status_code=409, detail="Transfer request changed; refresh and retry.")

    _, new_employee_branch = await sync_employee_branch_projection(
        company_id, employee_id, decided_at, db,
    )
    projection_changed = new_employee_branch != old_employee_branch
    await workforce_audit.write_workforce_audit(
        db,
        company_id=company_id,
        branch_id=row["sourcebranchid"],
        actor_user_id=user_id,
        action_code="DRIVER_TRANSFER_SCHEDULED" if is_future else "DRIVER_TRANSFER_COMPLETED",
        entity_name="DriverTransferRequests",
        entity_id=transfer_request_id,
        old_value={"status": "Approved", "employee_branch_id": old_employee_branch},
        new_value={
            "transfer_request_id": transfer_request_id,
            "employee_id": employee_id,
            "driver_id": row["driverid"],
            "source_driver_assignment_id": source["driverassignmentid"],
            "destination_driver_assignment_id": new_assignment_id,
            "source_branch_id": row["sourcebranchid"],
            "target_branch_id": row["targetbranchid"],
            "effective_at": effective_at,
            "status": "Scheduled" if is_future else "Completed",
            "employee_branch_projection_changed": projection_changed,
            "employee_branch_id": new_employee_branch,
            "projection_pending_until_effective_at": is_future,
        },
    )

    row = await _get_transfer_or_404(transfer_request_id, company_id, db)
    return _row_to_response(row)


def _employee_terminated_conflict() -> HTTPException:
    return HTTPException(status_code=409, detail={
        "code": "TRANSFER_EMPLOYEE_TERMINATED",
        "message": "The Employee is terminated. A scheduled transfer can no longer be "
                   "cancelled or rescheduled; it takes effect at its EffectiveAt.",
    })


async def _employee_is_terminated(company_id: int, employee_id: int, db: AsyncConnection) -> bool:
    result = await db.execute(
        text("""
            SELECT EmploymentStatus = 'Terminated'
            FROM core.Employees
            WHERE CompanyID = :company_id AND EmployeeID = :employee_id
        """),
        {"company_id": company_id, "employee_id": employee_id},
    )
    return bool(result.scalar_one())


async def cancel_driver_transfer(
    company_id: int,
    user_id: int,
    transfer_request_id: int,
    data: CancelRequest,
    db: AsyncConnection,
) -> DriverTransferResponse:
    """
    Cancel an in-flight (non-terminal) transfer request.

    A Scheduled request can be cancelled only while its EffectiveAt is still in the
    future at the decision instant, which is read once after every lock (Employee,
    request, the Driver's assignments, Branch workflow advisory, period rows) is held.
    That disposes of the never-effective destination assignment and reopens the source.
    A terminated Employee's Scheduled transfer can no longer be cancelled: termination
    is authoritative and the transfer takes effect at its EffectiveAt.
    Requires drivers.edit on source or target branch.
    DRIVER/Self users are blocked.
    """
    from app.payroll.eligibility import (
        BoundaryWindow,
        lock_boundary_periods,
        recapture_assignment_eligibility,
    )
    from app.transfer.disposal import dispose_scheduled_transfer

    await require_non_driver_subject(company_id, user_id, db)

    employee_id, row = await _lock_transfer_lifecycle(transfer_request_id, company_id, db)

    stored_status = row["stored_status"]
    if stored_status in _TERMINAL:
        raise HTTPException(
            status_code=422,
            detail=f"Request is already '{stored_status}' and cannot be cancelled.",
        )

    await _require_edit_on_source_or_target(
        company_id, user_id, row, db, action="cancel",
    )

    if stored_status == "Scheduled":
        if await _employee_is_terminated(company_id, employee_id, db):
            raise _employee_terminated_conflict()
        periods = await lock_boundary_periods(
            company_id,
            [BoundaryWindow(row["sourcebranchid"], row["effectiveat"], None),
             BoundaryWindow(row["targetbranchid"], row["effectiveat"], None)],
            db,
        )
        decided_at = await decision_instant(db)
        if row["effectiveat"] <= decided_at:
            raise HTTPException(
                status_code=422,
                detail="Request is already 'Completed' and cannot be cancelled.",
            )
        await dispose_scheduled_transfer(
            company_id=company_id, user_id=user_id, request=row,
            cancel_reason=data.cancel_reason, source_cap=None, db=db,
        )
        await recapture_assignment_eligibility(
            company_id, row["sourcebranchid"], row["sourcedriverassignmentid"],
            periods, user_id, db,
        )
        await sync_employee_branch_projection(company_id, employee_id, None, db)
        await workforce_audit.write_workforce_audit(
            db,
            company_id=company_id,
            branch_id=row["sourcebranchid"],
            actor_user_id=user_id,
            action_code="DRIVER_TRANSFER_CANCELLED",
            entity_name="DriverTransferRequests",
            entity_id=transfer_request_id,
            old_value={"status": "Scheduled", "effective_at": row["effectiveat"],
                       "destination_driver_assignment_id": row["destinationdriverassignmentid"]},
            new_value={"status": "Cancelled",
                       "source_driver_assignment_id": row["sourcedriverassignmentid"],
                       "reason": data.cancel_reason},
        )
    else:
        now = _now_utc()
        changed = await db.execute(
            text("""
                UPDATE core.drivertransferrequests
                SET    status             = 'Cancelled',
                       cancelledatutc    = :now,
                       cancelledbyuserid = :uid,
                       cancelreason      = :reason,
                       updatedatutc      = :now
                WHERE  transferrequestid = :tid AND status = :old_status
                RETURNING transferrequestid
            """),
            {
                "now":    now,
                "uid":    user_id,
                "reason": data.cancel_reason,
                "tid":    transfer_request_id,
                "old_status": stored_status,
            },
        )
        if changed.scalar_one_or_none() is None:
            raise HTTPException(status_code=409, detail="Transfer request changed; refresh and retry.")
    row = await _get_transfer_or_404(transfer_request_id, company_id, db)
    return _row_to_response(row)


async def reschedule_driver_transfer(
    company_id: int,
    user_id: int,
    transfer_request_id: int,
    data: RescheduleRequest,
    db: AsyncConnection,
) -> DriverTransferResponse:
    """Change a request's EffectiveAt to another future instant.

    Before completion only the request changes. For a Scheduled request, which is
    allowed only while its current EffectiveAt is still future at the decision instant,
    the source end and the destination start move together and the destination
    assignment keeps its id. The decision instant is read once after every lock is held.
    A terminated Employee's Scheduled transfer can no longer be rescheduled.
    """
    from app.payroll.eligibility import BoundaryWindow, lock_boundary_periods

    await require_non_driver_subject(company_id, user_id, db)

    employee_id, row = await _lock_transfer_lifecycle(transfer_request_id, company_id, db)

    stored_status = row["stored_status"]
    if stored_status in _TERMINAL:
        raise HTTPException(
            status_code=422,
            detail=f"Request is already '{stored_status}' and cannot be rescheduled.",
        )

    await _require_edit_on_source_or_target(
        company_id, user_id, row, db, action="reschedule",
    )

    new_effective_at = data.effective_at
    periods: list[dict] = []
    if stored_status == "Scheduled":
        if await _employee_is_terminated(company_id, employee_id, db):
            raise _employee_terminated_conflict()
        low, high = sorted((row["effectiveat"], new_effective_at))
        periods = await lock_boundary_periods(
            company_id,
            [BoundaryWindow(row["sourcebranchid"], low, high),
             BoundaryWindow(row["targetbranchid"], low, high)],
            db,
        )
    decided_at = await decision_instant(db)
    if stored_status == "Scheduled" and row["effectiveat"] <= decided_at:
        raise HTTPException(
            status_code=422,
            detail="Request is already 'Completed' and cannot be rescheduled.",
        )
    if new_effective_at <= decided_at:
        raise HTTPException(status_code=422, detail="Rescheduled transfer time must be in the future.")
    start_result = await db.execute(
        text("""
            SELECT EffectiveFromAt FROM core.DriverAssignments
            WHERE DriverAssignmentID = :assignment_id AND CompanyID = :company_id
        """),
        {"assignment_id": row["sourcedriverassignmentid"], "company_id": company_id},
    )
    source_start = start_result.scalar_one()
    if new_effective_at <= source_start:
        raise HTTPException(status_code=422, detail="Transfer time must be after the source assignment start instant.")

    old_effective_at = row["effectiveat"]
    if stored_status == "Scheduled":
        await _reschedule_scheduled_transfer(
            company_id, user_id, row, new_effective_at, periods, db,
        )

    changed = await db.execute(
        text("""
            UPDATE core.drivertransferrequests
            SET    effectiveat  = :effective_at,
                   updatedatutc = NOW()
            WHERE  transferrequestid = :tid AND status = :old_status
            RETURNING transferrequestid
        """),
        {"effective_at": new_effective_at, "tid": transfer_request_id,
         "old_status": stored_status},
    )
    if changed.scalar_one_or_none() is None:
        raise HTTPException(status_code=409, detail="Transfer request changed; refresh and retry.")
    await workforce_audit.write_workforce_audit(
        db,
        company_id=company_id,
        branch_id=row["sourcebranchid"],
        actor_user_id=user_id,
        action_code="DRIVER_TRANSFER_RESCHEDULED",
        entity_name="DriverTransferRequests",
        entity_id=transfer_request_id,
        old_value={"status": stored_status, "effective_at": old_effective_at},
        new_value={"status": stored_status, "effective_at": new_effective_at,
                   "source_driver_assignment_id": row["sourcedriverassignmentid"],
                   "destination_driver_assignment_id": row["destinationdriverassignmentid"]},
    )
    row = await _get_transfer_or_404(transfer_request_id, company_id, db)
    return _row_to_response(row)


async def _reschedule_scheduled_transfer(
    company_id: int,
    user_id: int,
    row: dict,
    new_effective_at: datetime,
    periods: list[dict],
    db: AsyncConnection,
) -> None:
    """Move both boundaries; ``periods`` are the already-locked editable periods."""
    from app.payroll.eligibility import (
        assert_assignment_payroll_covered,
        recapture_assignment_eligibility,
    )

    source_id = row["sourcedriverassignmentid"]
    destination_id = row["destinationdriverassignmentid"]
    async with workforce_operation(db, "transfer_reschedule"):
        await db.execute(
            text("""
                UPDATE core.DriverAssignments
                SET    EffectiveToAt = :effective_at, UpdatedAtUtc = NOW()
                WHERE  DriverAssignmentID = :source_id AND CompanyID = :company_id
            """),
            {"effective_at": new_effective_at, "source_id": source_id, "company_id": company_id},
        )
        await db.execute(
            text("""
                UPDATE core.DriverAssignments
                SET    EffectiveFromAt = :effective_at, UpdatedAtUtc = NOW()
                WHERE  DriverAssignmentID = :destination_id AND CompanyID = :company_id
            """),
            {"effective_at": new_effective_at, "destination_id": destination_id,
             "company_id": company_id},
        )
    await assert_assignment_payroll_covered(company_id, row["sourcebranchid"], source_id, db)
    await assert_assignment_payroll_covered(company_id, row["targetbranchid"], destination_id, db)
    await recapture_assignment_eligibility(
        company_id, row["sourcebranchid"], source_id, periods, user_id, db,
    )
    await recapture_assignment_eligibility(
        company_id, row["targetbranchid"], destination_id, periods, user_id, db,
    )


async def list_transfer_requests(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    branch_id: int | None = None,
    status_filter: str | None = None,
) -> TransferListResponse:
    """
    List transfer requests visible to the calling user.

    Scope rules:
    - DRIVER/Self users are blocked (they cannot list all transfer data).
    - Caller must have drivers.view (or drivers.edit) on at least one branch.
    - Results are filtered to requests where source OR target branch is in the
      caller's allowed set.
    - AllCompanyBranches users with drivers.view see all company requests.
    - An optional branch_id filter further narrows results.
    - The status filter matches the effective status (a Scheduled request whose
      EffectiveAt has been reached matches Completed, not Scheduled).
    """
    await require_non_driver_subject(company_id, user_id, db)

    can_see_all, allowed_branches = await _get_accessible_branches_for_transfers(
        company_id, user_id, db
    )

    where_parts = ["dtr.companyid = :cid"]
    params: dict = {"cid": company_id}

    # Branch-scope filter for SpecificBranch users
    if not can_see_all and allowed_branches:
        placeholders = ", ".join(f":ab{i}" for i in range(len(allowed_branches)))
        where_parts.append(
            f"(dtr.sourcebranchid IN ({placeholders}) "
            f"OR dtr.targetbranchid IN ({placeholders}))"
        )
        for i, bid in enumerate(allowed_branches):
            params[f"ab{i}"] = bid

    # Optional caller-supplied branch filter
    if branch_id is not None:
        where_parts.append(
            "(dtr.sourcebranchid = :bid OR dtr.targetbranchid = :bid)"
        )
        params["bid"] = branch_id

    if status_filter is not None:
        where_parts.append("core.fn_DriverTransferStatus(dtr.status, dtr.effectiveat) = :status")
        params["status"] = status_filter

    where_sql = " AND ".join(where_parts)

    result = await db.execute(
        text(f"""
            {_TRANSFER_SELECT}
            WHERE  {where_sql}
            ORDER  BY dtr.createdatutc DESC
        """),
        params,
    )
    rows = result.mappings().all()
    items = [_row_to_response(dict(r)) for r in rows]
    return TransferListResponse(items=items, total=len(items))


async def list_transfer_driver_candidates(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> list[TransferDriverCandidate]:
    """Drivers a transfer request can be created for by this caller.

    Authority is the transfer-create authority itself (`drivers.edit` on the source Branch), so
    an operator who can create transfers needs no `drivers.view` to populate the selector. The
    eligibility mirrors `create_driver_transfer_request`: a current assignment that is not already
    ending, no transfer in flight and no pending assignment. The permanent Driver work status is
    not part of it.
    """
    await require_non_driver_subject(company_id, user_id, db)
    branch_rows = await db.execute(
        text("SELECT BranchID FROM core.Branches WHERE CompanyID = :cid"), {"cid": company_id},
    )
    editable = [
        int(row["branchid"]) for row in branch_rows.mappings().all()
        if await has_any_permission(company_id, user_id, int(row["branchid"]), ["drivers.edit"], db)
    ]
    if not editable:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You need drivers.edit on at least one branch to start a transfer.",
        )
    result = await db.execute(
        text("""
            SELECT dr.DriverID, dr.DriverNumber, e.FullName,
                   a.DriverAssignmentID, a.BranchID, b.BranchName
            FROM core.Drivers dr
            JOIN core.Employees e ON e.EmployeeID = dr.EmployeeID AND e.CompanyID = dr.CompanyID
            JOIN core.DriverAssignments a
              ON a.DriverAssignmentID = core.fn_EffectiveDriverAssignment(dr.CompanyID, dr.DriverID)
            JOIN core.Branches b ON b.BranchID = a.BranchID
            WHERE dr.CompanyID = :cid
              AND a.EndReason IS NULL
              AND a.BranchID IN :branch_ids
              AND NOT EXISTS (
                  SELECT 1 FROM core.DriverTransferRequests t
                  WHERE t.DriverID = dr.DriverID AND t.CompanyID = dr.CompanyID
                    AND core.fn_DriverTransferStatus(t.Status, t.EffectiveAt)
                            NOT IN ('Completed', 'Cancelled', 'Rejected')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM core.DriverAssignments x
                  WHERE x.CompanyID = dr.CompanyID AND x.DriverID = dr.DriverID
                    AND x.DriverAssignmentID <> a.DriverAssignmentID
                    AND x.EffectiveFromAt > NOW()
                    AND (x.EffectiveToAt IS NULL OR x.EffectiveToAt > x.EffectiveFromAt)
              )
            ORDER BY e.FullName, dr.DriverID
        """).bindparams(bindparam("branch_ids", expanding=True)),
        {"cid": company_id, "branch_ids": editable},
    )
    return [
        TransferDriverCandidate(
            driver_id=row["driverid"], driver_number=row["drivernumber"], full_name=row["fullname"],
            driver_assignment_id=row["driverassignmentid"], source_branch_id=row["branchid"],
            source_branch_name=row["branchname"],
        )
        for row in result.mappings().all()
    ]


async def get_transfer_request(
    company_id: int,
    user_id: int,
    transfer_request_id: int,
    db: AsyncConnection,
) -> DriverTransferResponse:
    """
    Get a single transfer request by ID.

    Scope: caller must have drivers.view on either the source or target branch
    (or AllCompanyBranches + drivers.view).  DRIVER/Self users are blocked.
    """
    await require_non_driver_subject(company_id, user_id, db)

    can_see_all, allowed_branches = await _get_accessible_branches_for_transfers(
        company_id, user_id, db
    )

    row = await _get_transfer_or_404(transfer_request_id, company_id, db)

    # Verify the caller can see this specific request
    if not can_see_all:
        if (row["sourcebranchid"] not in allowed_branches
                and row["targetbranchid"] not in allowed_branches):
            # Leak-safe: return 404 rather than 403 so caller cannot enumerate IDs
            raise HTTPException(
                status_code=404,
                detail="Transfer request not found.",
            )

    return _row_to_response(row)
