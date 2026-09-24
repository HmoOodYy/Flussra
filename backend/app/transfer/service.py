"""Driver Transfer Workflow service layer.

All SQL is raw parameterised via sqlalchemy.text().
All authorization is enforced before any write occurs.

Transfer lifecycle:
  create   → PendingSourceApproval   (when initiated_by='Driver' — by ODA user or manager)
           → PendingTargetApproval   (when initiated_by='SourceBranch' — source
                                       approval is implicit)
  approve_source → PendingTargetApproval
                   (accepts PendingSourceApproval OR Returned status)
  decide_target  → Approved | Rejected | Returned
  complete       → Completed  (creates new driver profile in target branch,
                                closes old profile, updates employee branch)
  cancel         → Cancelled

P1 scope rules:
  - ODA/driver users may ONLY call create (for their own driver, initiated_by='Driver').
    All other endpoints are blocked for ODA users.
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
from datetime import UTC, date, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import (
    _check_branch_access,
    _check_permission,
    _has_any_permission,
    _require_not_driver_role,
)
from app.transfer.schemas import (
    CancelRequest,
    DriverTransferCreate,
    DriverTransferResponse,
    SourceApprovalRequest,
    TargetDecisionRequest,
    TransferListResponse,
)

log = logging.getLogger(__name__)

_TERMINAL = {"Completed", "Cancelled", "Rejected"}
# Statuses that block a new transfer request for the same driver
_ACTIVE_TRANSFER_STATUSES = {
    "PendingSourceApproval",
    "PendingTargetApproval",
    "Returned",
    "Approved",
}

# Read permissions for transfer data
_TRANSFER_READ_PERMS = ["drivers.view", "drivers.edit"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(UTC)


async def _get_transfer_or_404(
    transfer_request_id: int,
    company_id: int,
    db: AsyncConnection,
) -> dict:
    result = await db.execute(
        text("""
            SELECT
                dtr.transferrequestid,
                dtr.companyid,
                dtr.driverid,
                dtr.sourcebranchid,
                dtr.targetbranchid,
                dtr.requestedbyuserid,
                dtr.initiatedby,
                dtr.status,
                dtr.effectivedate,
                dtr.reason,
                dtr.notes,
                dtr.sourceapprovedbyuserid,
                dtr.sourceapprovedatutc,
                dtr.targetdecidedbyuserid,
                dtr.targetdecidedatutc,
                dtr.targetdecisionnotes,
                dtr.newdriverid,
                dtr.completedatutc,
                dtr.completedbyuserid,
                dtr.cancelledatutc,
                dtr.cancelledbyuserid,
                dtr.cancelreason,
                dtr.createdatutc,
                dtr.updatedatutc,
                -- Denormalized display fields
                e.fullname         AS driver_name,
                sb.branchname      AS source_branch_name,
                tb.branchname      AS target_branch_name,
                u.displayname      AS requested_by_name
            FROM   core.drivertransferrequests dtr
            JOIN   core.drivers   d  ON d.driverid   = dtr.driverid
            JOIN   core.employees e  ON e.employeeid = d.employeeid
            JOIN   core.branches  sb ON sb.branchid  = dtr.sourcebranchid
            JOIN   core.branches  tb ON tb.branchid  = dtr.targetbranchid
            JOIN   sec.users      u  ON u.userid      = dtr.requestedbyuserid
            WHERE  dtr.transferrequestid = :tid
              AND  dtr.companyid         = :cid
        """),
        {"tid": transfer_request_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Transfer request not found.")
    return dict(row)


def _row_to_response(row: dict) -> DriverTransferResponse:
    return DriverTransferResponse(
        transfer_request_id=row["transferrequestid"],
        company_id=row["companyid"],
        driver_id=row["driverid"],
        source_branch_id=row["sourcebranchid"],
        target_branch_id=row["targetbranchid"],
        requested_by_user_id=row["requestedbyuserid"],
        initiated_by=row["initiatedby"],
        status=row["status"],
        effective_date=row["effectivedate"],
        reason=row["reason"],
        notes=row["notes"],
        source_approved_by_user_id=row["sourceapprovedbyuserid"],
        source_approved_at_utc=row["sourceapprovedatutc"],
        target_decided_by_user_id=row["targetdecidedbyuserid"],
        target_decided_at_utc=row["targetdecidedatutc"],
        target_decision_notes=row["targetdecisionnotes"],
        new_driver_id=row["newdriverid"],
        completed_at_utc=row["completedatutc"],
        completed_by_user_id=row["completedbyuserid"],
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


async def _assert_driver_belongs_to_branch(
    driver_id: int,
    branch_id: int,
    company_id: int,
    db: AsyncConnection,
) -> dict:
    """Return active driver row, raising 404 / 422 if not valid."""
    result = await db.execute(
        text("""
            SELECT driverid, employeeid, driverstatus, branchid
            FROM   core.drivers
            WHERE  driverid   = :did
              AND  companyid  = :cid
        """),
        {"did": driver_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Driver not found.")
    if row["driverstatus"] in ("Transferred", "Terminated"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Driver is already {row['driverstatus']} and cannot be transferred."
            ),
        )
    if row["branchid"] != branch_id:
        raise HTTPException(
            status_code=422,
            detail="Driver does not belong to the source branch.",
        )
    return dict(row)


async def _assert_no_active_transfer(
    driver_id: int,
    company_id: int,
    db: AsyncConnection,
) -> None:
    """Raise 422 if driver already has an in-flight transfer request."""
    result = await db.execute(
        text("""
            SELECT transferrequestid FROM core.drivertransferrequests
            WHERE  driverid  = :did
              AND  companyid = :cid
              AND  status    NOT IN ('Completed','Cancelled','Rejected')
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


async def _get_driver_source_branch(
    driver_id: int,
    company_id: int,
    db: AsyncConnection,
) -> int:
    """Return the driver's current active branch_id or raise 404."""
    result = await db.execute(
        text("""
            SELECT branchid FROM core.drivers
            WHERE  driverid  = :did
              AND  companyid = :cid
              AND  driverstatus NOT IN ('Transferred', 'Terminated')
        """),
        {"did": driver_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Active driver not found.")
    return int(row["branchid"])


async def _is_caller_oda(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> bool:
    """Return True if the caller has OwnDriverDataOnly scope (is a driver-role user)."""
    result = await db.execute(
        text("""
            SELECT 1
            FROM   sec.userbranchroles ubr
            LEFT JOIN sec.companyroles cr ON cr.companyroleid = ubr.companyroleid
            LEFT JOIN sec.roles        r  ON r.roleid         = ubr.roleid
            WHERE  ubr.userid    = :uid
              AND  ubr.companyid = :cid
              AND  ubr.isactive  = TRUE
              AND (
                    ubr.scopetype = 'OwnDriverDataOnly'
                 OR cr.rolecode  = 'DRIVER'
                 OR r.rolecode   = 'DRIVER'
              )
            LIMIT 1
        """),
        {"uid": user_id, "cid": company_id},
    )
    return result.first() is not None


async def _get_oda_own_driver_id(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> int:
    """
    Return the caller's own active driver_id for an ODA user.
    Raises 403 if no linked active driver profile exists.
    """
    result = await db.execute(
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
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=403,
            detail="No active driver profile linked to your account.",
        )
    return int(row["driverid"])


async def _get_accessible_branches_for_transfers(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> tuple[bool, list[int]]:
    """
    Return (can_see_all, branch_ids) for transfer read access.

    Caller must have drivers.view (or drivers.edit) on at least one branch.
    - AllCompanyBranches scope + drivers.view → can_see_all=True, branch_ids=[]
    - SpecificBranch scope → branch_ids = branches where caller has drivers.view
    - No qualifying permission → raises HTTP 403

    This function is for OPERATIONAL users only.  ODA users are blocked before
    this is called.
    """
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    if can_see_all:
        # Check drivers.view at company level (branch_id=None → AllCompanyBranches only)
        await _check_permission(company_id, user_id, None, "drivers.view", db)
        return True, []

    # SpecificBranch: keep only branches where caller has drivers.view or drivers.edit
    allowed: list[int] = []
    for bid in branch_ids:
        if await _has_any_permission(company_id, user_id, bid, _TRANSFER_READ_PERMS, db):
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
    Create a new transfer request.

    Two caller types are supported:

    ODA / Driver-role users:
      - May only initiate for their OWN active driver profile.
      - initiated_by must be 'Driver'.
      - Source-branch approval is still required (PendingSourceApproval).
      - No operational payroll data is touched or exposed.

    Operational users (SpecificBranch / AllCompanyBranches with drivers.edit):
      - May initiate for any active driver in their accessible source branch.
      - initiated_by may be 'Driver' or 'SourceBranch'.
      - When 'SourceBranch', source approval is implicit (goes to PendingTargetApproval).
    """
    caller_is_oda = await _is_caller_oda(company_id, user_id, db)

    if caller_is_oda:
        # --- ODA path ---
        if data.initiated_by != "Driver":
            raise HTTPException(
                status_code=422,
                detail="Driver-role users must set initiated_by='Driver'.",
            )
        # Verify driver_id matches the caller's own active profile
        own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)
        if data.driver_id != own_driver_id:
            raise HTTPException(
                status_code=403,
                detail="You may only create a transfer request for your own driver profile.",
            )
        source_branch_id = await _get_driver_source_branch(data.driver_id, company_id, db)
    else:
        # --- Operational path ---
        source_branch_id = await _get_driver_source_branch(data.driver_id, company_id, db)
        await _check_permission(company_id, user_id, source_branch_id, "drivers.edit", db)

    # Common validation (both paths)
    await _assert_driver_belongs_to_branch(data.driver_id, source_branch_id, company_id, db)

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

    # ODA users always get PendingSourceApproval.
    # SourceBranch-initiated by operational users gets PendingTargetApproval.
    if caller_is_oda or data.initiated_by == "Driver":
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
                companyid, driverid, sourcebranchid, targetbranchid,
                requestedbyuserid, initiatedby, status,
                effectivedate, reason, notes,
                sourceapprovedbyuserid, sourceapprovedatutc,
                createdatutc
            ) VALUES (
                :cid, :did, :sbid, :tbid,
                :uid, :initiated_by, :status,
                :edate, :reason, :notes,
                :src_by, :src_at,
                :now
            )
            RETURNING transferrequestid
        """),
        {
            "cid":          company_id,
            "did":          data.driver_id,
            "sbid":         source_branch_id,
            "tbid":         data.target_branch_id,
            "uid":          user_id,
            "initiated_by": data.initiated_by,
            "status":       initial_status,
            "edate":        data.effective_date,
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
    await _require_not_driver_role(company_id, user_id, db)

    row = await _get_transfer_or_404(transfer_request_id, company_id, db)

    if row["status"] not in ("PendingSourceApproval", "Returned"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Request is '{row['status']}'. "
                "approve-source requires PendingSourceApproval or Returned status."
            ),
        )

    await _check_permission(
        company_id, user_id, row["sourcebranchid"], "drivers.edit", db
    )

    now = _now_utc()
    await db.execute(
        text("""
            UPDATE core.drivertransferrequests
            SET    status                  = 'PendingTargetApproval',
                   sourceapprovedbyuserid  = :uid,
                   sourceapprovedatutc     = :now,
                   notes                  = COALESCE(:notes, notes),
                   updatedatutc           = :now
            WHERE  transferrequestid = :tid
        """),
        {"uid": user_id, "now": now, "notes": data.notes, "tid": transfer_request_id},
    )
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
    await _require_not_driver_role(company_id, user_id, db)

    row = await _get_transfer_or_404(transfer_request_id, company_id, db)

    if row["status"] != "PendingTargetApproval":
        raise HTTPException(
            status_code=422,
            detail=f"Request is '{row['status']}', not PendingTargetApproval.",
        )

    await _check_permission(
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

    await db.execute(
        text("""
            UPDATE core.drivertransferrequests
            SET    status                 = :status,
                   targetdecidedbyuserid = :uid,
                   targetdecidedatutc    = :now,
                   targetdecisionnotes   = :dnotes,
                   updatedatutc          = :now
            WHERE  transferrequestid = :tid
        """),
        {
            "status": new_status,
            "uid":    user_id,
            "now":    now,
            "dnotes": data.decision_notes,
            "tid":    transfer_request_id,
        },
    )
    row = await _get_transfer_or_404(transfer_request_id, company_id, db)
    return _row_to_response(row)


async def complete_driver_transfer(
    company_id: int,
    user_id: int,
    transfer_request_id: int,
    db: AsyncConnection,
) -> DriverTransferResponse:
    """
    Complete an Approved transfer:
      1. Create new Driver profile in the target branch (status=Active,
         TransferredFromDriverID=old driver).
      2. Set old Driver profile DriverStatus='Transferred',
         TransferredToDriverID=new driver.
      3. Update Employee.BranchID to target branch.
      4. Update request: status=Completed, NewDriverID=new driver.

    Old payroll history (draft lines, final lines), rates, and rules remain
    attached to the old driver record — they are never modified.
    The caller must hold drivers.edit on EITHER the source OR target branch.
    """
    await _require_not_driver_role(company_id, user_id, db)

    row = await _get_transfer_or_404(transfer_request_id, company_id, db)

    if row["status"] != "Approved":
        raise HTTPException(
            status_code=422,
            detail=f"Request is '{row['status']}', not Approved.",
        )

    # Require drivers.edit on at least one of the two branches
    src_ok = await _has_any_permission(
        company_id, user_id, row["sourcebranchid"], ["drivers.edit"], db
    )
    tgt_ok = await _has_any_permission(
        company_id, user_id, row["targetbranchid"], ["drivers.edit"], db
    )

    if not (src_ok or tgt_ok):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You need drivers.edit on the source or target branch to "
                "complete this transfer."
            ),
        )

    # Fetch old driver row
    old_drv_result = await db.execute(
        text("""
            SELECT driverid, employeeid, driverstatus
            FROM   core.drivers
            WHERE  driverid  = :did AND companyid = :cid
        """),
        {"did": row["driverid"], "cid": company_id},
    )
    old_drv = old_drv_result.mappings().first()
    if old_drv is None or old_drv["driverstatus"] == "Transferred":
        raise HTTPException(
            status_code=422,
            detail="Driver profile is already Transferred.",
        )

    now = _now_utc()

    # 1. Create new driver profile in target branch.
    #    DriverCode uses the transfer_request_id (SERIAL, globally unique)
    #    to prevent collisions when the same employee is transferred to the
    #    same branch more than once (ux_Drivers_Company_DriverCode enforces
    #    UNIQUE on (CompanyID, DriverCode) WHERE DriverCode IS NOT NULL).
    new_drv_ins = await db.execute(
        text("""
            INSERT INTO core.drivers (
                companyid, employeeid, branchid,
                drivercode, driverstatus,
                transferredfromdriverid
            ) VALUES (
                :cid, :eid, :tbid,
                :code, 'Active',
                :from_did
            )
            RETURNING driverid
        """),
        {
            "cid":      company_id,
            "eid":      old_drv["employeeid"],
            "tbid":     row["targetbranchid"],
            "code":     f"DRV-TR{row['transferrequestid']:06d}",
            "from_did": row["driverid"],
        },
    )
    new_driver_id: int = new_drv_ins.scalar_one()

    # 2. Close old profile
    await db.execute(
        text("""
            UPDATE core.drivers
            SET    driverstatus          = 'Transferred',
                   transferredtodriverid = :new_did
            WHERE  driverid  = :old_did
              AND  companyid = :cid
        """),
        {"new_did": new_driver_id, "old_did": row["driverid"], "cid": company_id},
    )

    # 3. Update Employee.BranchID to target branch
    await db.execute(
        text("""
            UPDATE core.employees
            SET    branchid = :tbid
            WHERE  employeeid = :eid
              AND  companyid  = :cid
        """),
        {
            "tbid": row["targetbranchid"],
            "eid":  old_drv["employeeid"],
            "cid":  company_id,
        },
    )

    # 4. Complete the request
    await db.execute(
        text("""
            UPDATE core.drivertransferrequests
            SET    status             = 'Completed',
                   newdriverid        = :new_did,
                   completedatutc     = :now,
                   completedbyuserid  = :uid,
                   updatedatutc       = :now
            WHERE  transferrequestid = :tid
        """),
        {
            "new_did": new_driver_id,
            "now":     now,
            "uid":     user_id,
            "tid":     transfer_request_id,
        },
    )

    # 5. Apply EffectiveDate to driver profile validity windows.
    #    effectivedate is the DATE from DriverTransferRequests (always NOT NULL).
    #    Old source profile: valid up to and including effective_date - 1 day.
    #    New target profile: valid starting effective_date.
    #    Existing rows with NULL windows are unaffected (treated as always valid).
    effective_date: date = row["effectivedate"]
    await db.execute(
        text("""
            UPDATE core.drivers
            SET    effectiveto = :eto
            WHERE  driverid   = :did
              AND  companyid  = :cid
        """),
        {
            "eto": effective_date - timedelta(days=1),
            "did": row["driverid"],
            "cid": company_id,
        },
    )
    await db.execute(
        text("""
            UPDATE core.drivers
            SET    effectivefrom = :efrom
            WHERE  driverid  = :did
              AND  companyid = :cid
        """),
        {
            "efrom": effective_date,
            "did":   new_driver_id,
            "cid":   company_id,
        },
    )

    row = await _get_transfer_or_404(transfer_request_id, company_id, db)
    return _row_to_response(row)


async def cancel_driver_transfer(
    company_id: int,
    user_id: int,
    transfer_request_id: int,
    data: CancelRequest,
    db: AsyncConnection,
) -> DriverTransferResponse:
    """
    Cancel an in-flight (non-terminal) transfer request.
    Requires drivers.edit on source or target branch.
    ODA/driver users are blocked.
    """
    await _require_not_driver_role(company_id, user_id, db)

    row = await _get_transfer_or_404(transfer_request_id, company_id, db)

    if row["status"] in _TERMINAL:
        raise HTTPException(
            status_code=422,
            detail=f"Request is already '{row['status']}' and cannot be cancelled.",
        )

    src_ok = await _has_any_permission(
        company_id, user_id, row["sourcebranchid"], ["drivers.edit"], db
    )
    tgt_ok = await _has_any_permission(
        company_id, user_id, row["targetbranchid"], ["drivers.edit"], db
    )

    if not (src_ok or tgt_ok):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You need drivers.edit on the source or target branch to cancel.",
        )

    now = _now_utc()
    await db.execute(
        text("""
            UPDATE core.drivertransferrequests
            SET    status             = 'Cancelled',
                   cancelledatutc    = :now,
                   cancelledbyuserid = :uid,
                   cancelreason      = :reason,
                   updatedatutc      = :now
            WHERE  transferrequestid = :tid
        """),
        {
            "now":    now,
            "uid":    user_id,
            "reason": data.cancel_reason,
            "tid":    transfer_request_id,
        },
    )
    row = await _get_transfer_or_404(transfer_request_id, company_id, db)
    return _row_to_response(row)


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
    - ODA users are blocked (they cannot list all transfer data).
    - Caller must have drivers.view (or drivers.edit) on at least one branch.
    - Results are filtered to requests where source OR target branch is in the
      caller's allowed set.
    - AllCompanyBranches users with drivers.view see all company requests.
    - An optional branch_id filter further narrows results.
    """
    await _require_not_driver_role(company_id, user_id, db)

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
        where_parts.append("dtr.status = :status")
        params["status"] = status_filter

    where_sql = " AND ".join(where_parts)

    result = await db.execute(
        text(f"""
            SELECT
                dtr.transferrequestid,
                dtr.companyid,
                dtr.driverid,
                dtr.sourcebranchid,
                dtr.targetbranchid,
                dtr.requestedbyuserid,
                dtr.initiatedby,
                dtr.status,
                dtr.effectivedate,
                dtr.reason,
                dtr.notes,
                dtr.sourceapprovedbyuserid,
                dtr.sourceapprovedatutc,
                dtr.targetdecidedbyuserid,
                dtr.targetdecidedatutc,
                dtr.targetdecisionnotes,
                dtr.newdriverid,
                dtr.completedatutc,
                dtr.completedbyuserid,
                dtr.cancelledatutc,
                dtr.cancelledbyuserid,
                dtr.cancelreason,
                dtr.createdatutc,
                dtr.updatedatutc,
                e.fullname         AS driver_name,
                sb.branchname      AS source_branch_name,
                tb.branchname      AS target_branch_name,
                u.displayname      AS requested_by_name
            FROM   core.drivertransferrequests dtr
            JOIN   core.drivers   d  ON d.driverid   = dtr.driverid
            JOIN   core.employees e  ON e.employeeid = d.employeeid
            JOIN   core.branches  sb ON sb.branchid  = dtr.sourcebranchid
            JOIN   core.branches  tb ON tb.branchid  = dtr.targetbranchid
            JOIN   sec.users      u  ON u.userid      = dtr.requestedbyuserid
            WHERE  {where_sql}
            ORDER  BY dtr.createdatutc DESC
        """),
        params,
    )
    rows = result.mappings().all()
    items = [_row_to_response(dict(r)) for r in rows]
    return TransferListResponse(items=items, total=len(items))


async def get_transfer_request(
    company_id: int,
    user_id: int,
    transfer_request_id: int,
    db: AsyncConnection,
) -> DriverTransferResponse:
    """
    Get a single transfer request by ID.

    Scope: caller must have drivers.view on either the source or target branch
    (or AllCompanyBranches + drivers.view).  ODA users are blocked.
    """
    await _require_not_driver_role(company_id, user_id, db)

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
