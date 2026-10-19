"""Instant-based resolution of a Driver's effective home-Branch assignment."""

from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.workforce.ops import decision_instant


def _require_aware(effective_at: datetime | None) -> None:
    if effective_at is not None and (
        effective_at.tzinfo is None or effective_at.utcoffset() is None
    ):
        raise ValueError("effective_at must be timezone-aware")


async def resolve_employee_driver(
    company_id: int,
    employee_id: int,
    db: AsyncConnection,
) -> dict[str, Any] | None:
    """Return the Employee's permanent Driver (0..1), or None for a non-Driver."""
    result = await db.execute(text("""
        SELECT dr.*
        FROM core.Drivers dr
        WHERE dr.CompanyID = :company_id AND dr.EmployeeID = :employee_id
    """), {"company_id": company_id, "employee_id": employee_id})
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


async def resolve_effective_driver_assignment(
    company_id: int,
    driver_id: int,
    effective_at: datetime | None,
    db: AsyncConnection,
) -> dict[str, Any] | None:
    """Return the Driver's assignment whose half-open window contains the instant.

    ``effective_at=None`` evaluates at database ``NOW()``. The row carries the
    permanent Driver's number, CDL number and work status next to the assignment.
    """
    _require_aware(effective_at)
    if effective_at is None:
        result = await db.execute(text("""
            SELECT a.*, dr.DriverNumber, dr.CDLNumber, dr.DriverStatus
            FROM core.DriverAssignments a
            JOIN core.Drivers dr ON dr.DriverID = a.DriverID
            WHERE a.DriverAssignmentID = core.fn_EffectiveDriverAssignment(:company_id, :driver_id)
        """), {"company_id": company_id, "driver_id": driver_id})
    else:
        result = await db.execute(text("""
            SELECT a.*, dr.DriverNumber, dr.CDLNumber, dr.DriverStatus
            FROM core.DriverAssignments a
            JOIN core.Drivers dr ON dr.DriverID = a.DriverID
            WHERE a.DriverAssignmentID =
                  core.fn_EffectiveDriverAssignment(:company_id, :driver_id, :effective_at)
        """), {"company_id": company_id, "driver_id": driver_id, "effective_at": effective_at})
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


async def resolve_effective_employee_assignment(
    company_id: int,
    employee_id: int,
    effective_at: datetime | None,
    db: AsyncConnection,
) -> dict[str, Any] | None:
    """Map Employee to its Driver (0..1), then resolve that Driver's effective assignment."""
    driver = await resolve_employee_driver(company_id, employee_id, db)
    if driver is None:
        return None
    return await resolve_effective_driver_assignment(
        company_id, int(driver["driverid"]), effective_at, db,
    )


DRIVER_ASSIGNMENT_ENDED_MESSAGE = (
    "This Driver assignment has ended; its compensation is historical and read-only."
)
# Raised by core.trg_CompensationAssignmentWritable (migration 0087) when a compensation
# row of an ended DriverAssignment is inserted, updated or deleted.
DRIVER_ASSIGNMENT_ENDED_SQLSTATE = "FL001"


def driver_assignment_ended_detail() -> dict[str, str]:
    """The one API error body for a refused write to an ended DriverAssignment."""
    return {"code": "DRIVER_ASSIGNMENT_ENDED", "message": DRIVER_ASSIGNMENT_ENDED_MESSAGE}


def is_driver_assignment_ended_error(exc: DBAPIError) -> bool:
    """Whether the database refused a write because its DriverAssignment has ended."""
    original = exc.orig
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return sqlstate == DRIVER_ASSIGNMENT_ENDED_SQLSTATE


async def require_writable_driver_assignment(
    company_id: int,
    driver_assignment_id: int,
    db: AsyncConnection,
) -> None:
    """Refuse EARLY a compensation write against an already-ENDED DriverAssignment (409).

    Ended means ``EffectiveToAt IS NOT NULL AND EffectiveToAt <= now`` at one wall-clock
    decision instant, which includes the empty never-effective window of a terminated
    destination. A current source whose Transferred end is still in the future and any
    pending/future-effective assignment stay writable.

    Acceptance here is never final. The writer can still block afterwards (advisory
    lock, row lock, unique-index wait) while a Scheduled source's future end passes, and
    ``FOR SHARE`` freezes the ``EffectiveToAt`` value, not time. The authority is the
    database trigger ``core.trg_CompensationAssignmentWritable``: it re-decides at each
    mutation's own wall-clock instant, after that statement's waits, and its refusal
    reaches the client as the same 409 through the application's DBAPIError handler. This
    call refuses only what is already ended, so such a request does not wait for the
    Branch advisory lock first.

    Call it after the writer's authorization and after any Company-currency or
    RateDefinition-structure lock, and BEFORE the Branch workflow advisory lock and the
    first DML. The row is locked FOR SHARE so a concurrent transfer/termination (FOR NO
    KEY UPDATE, which conflicts with FOR SHARE but not with the FOR KEY SHARE that
    child-row inserts take) cannot close the assignment between this decision and the
    write's commit; the instant is read only after that lock. Transfers and termination
    take the assignment row and THEN the Branch advisory lock, so this row lock must stay
    ahead of the advisory lock or the two orders form a deadlock cycle.
    """
    row = (await db.execute(text("""
        SELECT EffectiveToAt
        FROM core.DriverAssignments
        WHERE DriverAssignmentID = :assignment_id AND CompanyID = :company_id
        FOR SHARE
    """), {"assignment_id": driver_assignment_id, "company_id": company_id})
    ).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Driver assignment not found.")
    ended_at = row["effectivetoat"]
    if ended_at is None:
        return
    if ended_at <= await decision_instant(db):
        raise HTTPException(status_code=409, detail=driver_assignment_ended_detail())


# Joins one assignment (alias ``va``) per Driver (alias ``dr``): the current
# assignment, else the next pending one, else the latest historical one.
VISIBLE_ASSIGNMENT_JOIN = """
    LEFT JOIN LATERAL (
        SELECT a.*, vb.BranchName
        FROM core.DriverAssignments a
        JOIN core.Branches vb ON vb.BranchID = a.BranchID
        WHERE a.DriverID = dr.DriverID
        ORDER BY (a.DriverAssignmentID = core.fn_EffectiveDriverAssignment(dr.CompanyID, dr.DriverID)) DESC,
                 (a.EffectiveFromAt > NOW()
                  AND (a.EffectiveToAt IS NULL OR a.EffectiveToAt > a.EffectiveFromAt)) DESC,
                 a.EffectiveFromAt DESC, a.DriverAssignmentID DESC
        LIMIT 1
    ) va ON TRUE
"""
