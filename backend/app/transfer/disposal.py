"""The one routine that disposes of a Scheduled transfer's never-effective destination.

A Scheduled transfer owns a destination DriverAssignment that does not start yet. Two
operations remove it and must do it identically:

* ``transfer cancel`` reopens the source assignment (open end, no end reason);
* ``termination`` replaces the source's future ``Transferred`` end with the
  termination cutoff and ``EndReason = 'Terminated'``.

Either way the request becomes ``Cancelled`` with its destination link cleared, the
destination's roster rows are deleted (editable and Cancelled periods alike), and the
destination assignment is deleted. The caller owns the transaction, the canonical
locks (Employee, request, the Driver's assignments, Branch workflow advisory, period
rows) and the decision instant. A payroll fact anywhere on the destination, or a roster
row in a period that is neither editable nor Cancelled, refuses the whole operation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.eligibility import (
    assignment_eligibility_in_noneditable_period,
    assignment_payroll_fact_exists,
    delete_assignment_eligibility_rows,
)
from app.workforce.ops import workforce_operation


async def dispose_scheduled_transfer(
    *,
    company_id: int,
    user_id: int,
    request: dict[str, Any],
    cancel_reason: str | None,
    source_cap: datetime | None,
    db: AsyncConnection,
) -> None:
    """Cancel a Scheduled request and dispose of its destination assignment.

    ``source_cap`` is None for a transfer cancel (the source is reopened) and the
    termination cutoff for a termination (the source ends there as ``Terminated``).
    """
    request_id = request["transferrequestid"]
    source_id = request["sourcedriverassignmentid"]
    destination_id = request["destinationdriverassignmentid"]
    if destination_id is None:
        raise HTTPException(status_code=409, detail="Scheduled transfer has no destination assignment.")

    if await assignment_payroll_fact_exists(company_id, destination_id, db):
        raise HTTPException(status_code=409, detail={
            "code": "TRANSFER_DESTINATION_HAS_PAYROLL_FACTS",
            "message": "The destination Driver assignment already has payroll facts; the transfer cannot be cancelled.",
        })
    if await assignment_eligibility_in_noneditable_period(company_id, destination_id, db):
        raise HTTPException(status_code=409, detail={
            "code": "TRANSFER_DESTINATION_IN_LOCKED_PERIOD",
            "message": "The destination Driver assignment is on the roster of a period past Draft, Open or Returned.",
        })

    # The request is detached from its destination first: a Cancelled request keeps
    # none (database CHECK), and the destination is about to be deleted.
    changed = await db.execute(
        text("""
            UPDATE core.DriverTransferRequests
            SET    Status                        = 'Cancelled',
                   DestinationDriverAssignmentID = NULL,
                   CancelledAtUtc                = NOW(),
                   CancelledByUserID             = :user_id,
                   CancelReason                  = :reason,
                   UpdatedAtUtc                  = NOW()
            WHERE  TransferRequestID = :request_id AND CompanyID = :company_id
              AND  Status = 'Scheduled'
            RETURNING TransferRequestID
        """),
        {"user_id": user_id, "reason": cancel_reason,
         "request_id": request_id, "company_id": company_id},
    )
    if changed.scalar_one_or_none() is None:
        raise HTTPException(status_code=409, detail="Transfer request changed; refresh and retry.")

    operation = "transfer_cancel" if source_cap is None else "terminate"
    async with workforce_operation(db, operation):
        await delete_assignment_eligibility_rows(company_id, destination_id, db)
        if source_cap is None:
            await db.execute(
                text("""
                    UPDATE core.DriverAssignments
                    SET    EffectiveToAt = NULL, EndReason = NULL,
                           TransferredToAssignmentID = NULL, UpdatedAtUtc = NOW()
                    WHERE  DriverAssignmentID = :source_id AND CompanyID = :company_id
                """),
                {"source_id": source_id, "company_id": company_id},
            )
        else:
            await db.execute(
                text("""
                    UPDATE core.DriverAssignments
                    SET    EffectiveToAt = :cap, EndReason = 'Terminated',
                           TransferredToAssignmentID = NULL, UpdatedAtUtc = NOW()
                    WHERE  DriverAssignmentID = :source_id AND CompanyID = :company_id
                """),
                {"source_id": source_id, "company_id": company_id, "cap": source_cap},
            )
        await db.execute(
            text("""
                DELETE FROM core.DriverAssignments
                WHERE DriverAssignmentID = :destination_id AND CompanyID = :company_id
            """),
            {"destination_id": destination_id, "company_id": company_id},
        )
