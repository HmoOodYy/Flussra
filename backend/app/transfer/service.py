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
                                closes old profile, syncs branch projection when effective)
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
from app.workforce.clock import company_today
from app.workforce.effective import resolve_effective_driver_profile
from app.workforce.projection import sync_employee_branch_projection
from app.workforce.service import _write_workforce_audit

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


async def _lock_transfer_lifecycle(
    transfer_request_id: int,
    company_id: int,
    db: AsyncConnection,
) -> tuple[int, dict]:
    """Lock Employee, request, then all Employee Driver rows in canonical order."""
    identity = await db.execute(
        text("""
            SELECT d.EmployeeID
            FROM core.DriverTransferRequests dtr
            JOIN core.Drivers d ON d.DriverID = dtr.DriverID
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
    await db.execute(
        text("""
            SELECT DriverID
            FROM core.Drivers
            WHERE CompanyID = :company_id AND EmployeeID = :employee_id
            ORDER BY DriverID
            FOR UPDATE
        """),
        {"company_id": company_id, "employee_id": employee_id},
    )
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
    await db.execute(
        text("""
            SELECT DriverID
            FROM core.Drivers
            WHERE CompanyID = :company_id AND EmployeeID = :employee_id
            ORDER BY DriverID
            FOR UPDATE
        """),
        {"company_id": company_id, "employee_id": employee_id},
    )
    return employee_id, dict(employee)


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
    """Return the requested Driver's branch only when it is effective today."""
    result = await db.execute(
        text("""
            SELECT employeeid, branchid FROM core.drivers
            WHERE  driverid  = :did
              AND  companyid = :cid
        """),
        {"did": driver_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Driver not found.")
    today = await company_today(company_id, db)
    current = await resolve_effective_driver_profile(company_id, row["employeeid"], today, db)
    if current is None or current["driverid"] != driver_id:
        raise HTTPException(status_code=422, detail="Driver is not the effective current profile.")
    return int(current["branchid"])


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
            SELECT EmployeeID
            FROM sec.Users
            WHERE UserID = :uid AND CompanyID = :cid
        """),
        {"uid": user_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=403,
            detail="No active driver profile linked to your account.",
        )
    today = await company_today(company_id, db)
    current = await resolve_effective_driver_profile(
        company_id, row["employeeid"], today, db,
    )
    if current is None:
        raise HTTPException(
            status_code=403,
            detail="No effective current Driver profile linked to your account.",
        )
    return int(current["driverid"])


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

    if caller_is_oda and data.initiated_by != "Driver":
        raise HTTPException(
            status_code=422,
            detail="Driver-role users must set initiated_by='Driver'.",
        )

    employee_id, _ = await _lock_employee_for_transfer_create(
        data.driver_id, company_id, db,
    )
    today = await company_today(company_id, db)
    await sync_employee_branch_projection(
        company_id, employee_id, today, db,
    )
    current = await resolve_effective_driver_profile(company_id, employee_id, today, db)
    if current is None or current["driverid"] != data.driver_id:
        raise HTTPException(status_code=422, detail="Transfer source is not the current Driver profile.")
    if current["driverstatus"] in {"Transferred", "Terminated"}:
        raise HTTPException(
            status_code=422,
            detail="A transferred or terminated Driver profile cannot start another transfer.",
        )
    if current["effectivefrom"] is not None and data.effective_date <= current["effectivefrom"]:
        raise HTTPException(
            status_code=422,
            detail="Transfer effective date must be after the source profile start date.",
        )
    if caller_is_oda:
        own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)
        if data.driver_id != own_driver_id:
            raise HTTPException(
                status_code=403,
                detail="You may only create a transfer request for your own driver profile.",
            )

    source_branch_id = int(current["branchid"])
    await _assert_driver_belongs_to_branch(data.driver_id, source_branch_id, company_id, db)
    if not caller_is_oda:
        await _check_permission(company_id, user_id, source_branch_id, "drivers.edit", db)

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
            SELECT 1 FROM core.Drivers
            WHERE CompanyID = :company_id AND EmployeeID = :employee_id
              AND DriverID <> :driver_id
              AND EffectiveFrom > :business_date
              AND NOT (DriverStatus = 'Terminated' AND EffectiveTo = EffectiveFrom - 1)
            LIMIT 1
        """),
        {
            "company_id": company_id,
            "employee_id": employee_id,
            "driver_id": data.driver_id,
            "business_date": today,
        },
    )
    if pending_result.first() is not None:
        raise HTTPException(
            status_code=422,
            detail="A pending Driver profile already exists for this Employee.",
        )

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

    _, row = await _lock_transfer_lifecycle(transfer_request_id, company_id, db)

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
    await _require_not_driver_role(company_id, user_id, db)

    _, row = await _lock_transfer_lifecycle(transfer_request_id, company_id, db)

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
    """Complete an approved effective-dated transfer in one transaction."""
    await _require_not_driver_role(company_id, user_id, db)

    _, row = await _lock_transfer_lifecycle(transfer_request_id, company_id, db)

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

    source_result = await db.execute(
        text("""
            SELECT DriverID, EmployeeID, BranchID, DriverStatus, EffectiveFrom, EffectiveTo
            FROM core.Drivers
            WHERE DriverID = :driver_id AND CompanyID = :company_id
        """),
        {"driver_id": row["driverid"], "company_id": company_id},
    )
    source = source_result.mappings().first()
    if source is None or source["driverstatus"] in {"Transferred", "Terminated"}:
        raise HTTPException(
            status_code=409,
            detail="Transfer source is no longer available for completion.",
        )

    employee_id = source["employeeid"]
    today = await company_today(company_id, db)
    current = await resolve_effective_driver_profile(company_id, employee_id, today, db)
    if current is None or current["driverid"] != source["driverid"]:
        raise HTTPException(status_code=409, detail="Transfer source is no longer current.")

    effective_date: date = row["effectivedate"]
    if source["effectivefrom"] is not None and effective_date <= source["effectivefrom"]:
        raise HTTPException(status_code=409, detail="Transfer date precedes the source profile window.")
    pending_result = await db.execute(
        text("""
            SELECT 1 FROM core.Drivers
            WHERE CompanyID = :company_id AND EmployeeID = :employee_id
              AND DriverID <> :source_driver_id
              AND EffectiveFrom > :business_date
              AND NOT (DriverStatus = 'Terminated' AND EffectiveTo = EffectiveFrom - 1)
            LIMIT 1
        """),
        {
            "company_id": company_id,
            "employee_id": employee_id,
            "source_driver_id": source["driverid"],
            "business_date": today,
        },
    )
    if pending_result.first() is not None:
        raise HTTPException(status_code=409, detail="Employee already has a pending Driver profile.")

    employee_before = await db.execute(
        text("""
            SELECT BranchID FROM core.Employees
            WHERE CompanyID = :company_id AND EmployeeID = :employee_id
        """),
        {"company_id": company_id, "employee_id": employee_id},
    )
    old_employee_branch = employee_before.scalar_one()

    now = _now_utc()

    # Insert the destination with its effective start so it can remain pending.
    new_drv_ins = await db.execute(
        text("""
            INSERT INTO core.drivers (
                companyid, employeeid, branchid,
                drivercode, driverstatus, effectivefrom,
                transferredfromdriverid
            ) VALUES (
                :cid, :eid, :tbid,
                :code, 'Active', :effective_from,
                :from_did
            )
            RETURNING driverid
        """),
        {
            "cid":      company_id,
            "eid":      employee_id,
            "tbid":     row["targetbranchid"],
            "code":     f"DRV-TR{row['transferrequestid']:06d}",
            "effective_from": effective_date,
            "from_did": row["driverid"],
        },
    )
    new_driver_id: int = new_drv_ins.scalar_one()

    # Close the source in one trigger-compatible update.
    source_closed = await db.execute(
        text("""
            UPDATE core.drivers
            SET    driverstatus          = 'Transferred',
                   transferredtodriverid = :new_did,
                   effectiveto           = :effective_to
            WHERE  driverid  = :old_did
              AND  companyid = :cid
              AND  driverstatus NOT IN ('Transferred', 'Terminated')
            RETURNING driverid
        """),
        {"new_did": new_driver_id, "effective_to": effective_date - timedelta(days=1),
         "old_did": row["driverid"], "cid": company_id},
    )
    if source_closed.scalar_one_or_none() is None:
        raise HTTPException(status_code=409, detail="Transfer source changed; refresh and retry.")

    request_completed = await db.execute(
        text("""
            UPDATE core.drivertransferrequests
            SET    status             = 'Completed',
                   newdriverid        = :new_did,
                   completedatutc     = :now,
                   completedbyuserid  = :uid,
                   updatedatutc       = :now
            WHERE  transferrequestid = :tid AND status = 'Approved'
            RETURNING transferrequestid
        """),
        {
            "new_did": new_driver_id,
            "now":     now,
            "uid":     user_id,
            "tid":     transfer_request_id,
        },
    )
    if request_completed.scalar_one_or_none() is None:
        raise HTTPException(status_code=409, detail="Transfer request changed; refresh and retry.")

    _, new_employee_branch = await sync_employee_branch_projection(
        company_id, employee_id, today, db,
    )
    projection_changed = new_employee_branch != old_employee_branch
    await _write_workforce_audit(
        db,
        company_id=company_id,
        branch_id=row["sourcebranchid"],
        actor_user_id=user_id,
        action_code="DRIVER_TRANSFER_COMPLETED",
        entity_name="DriverTransferRequests",
        entity_id=transfer_request_id,
        old_value={"status": "Approved", "employee_branch_id": old_employee_branch},
        new_value={
            "transfer_request_id": transfer_request_id,
            "employee_id": employee_id,
            "source_driver_id": source["driverid"],
            "destination_driver_id": new_driver_id,
            "source_branch_id": row["sourcebranchid"],
            "target_branch_id": row["targetbranchid"],
            "effective_date": effective_date,
            "employee_branch_projection_changed": projection_changed,
            "employee_branch_id": new_employee_branch,
            "projection_pending_until_effective_date": not projection_changed
                and effective_date > today,
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

    _, row = await _lock_transfer_lifecycle(transfer_request_id, company_id, db)

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
            "old_status": row["status"],
        },
    )
    if changed.scalar_one_or_none() is None:
        raise HTTPException(status_code=409, detail="Transfer request changed; refresh and retry.")
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
