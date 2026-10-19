"""Instant-based resolution of a Driver's effective home-Branch assignment."""

from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
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


async def require_writable_driver_assignment(
    company_id: int,
    driver_assignment_id: int,
    db: AsyncConnection,
) -> None:
    """Refuse a compensation write against an ENDED DriverAssignment (409).

    Ended means ``EffectiveToAt IS NOT NULL AND EffectiveToAt <= now`` at one wall-clock
    decision instant, which includes the empty never-effective window of a terminated
    destination. A current source whose Transferred end is still in the future and any
    pending/future-effective assignment stay writable.

    Call it after the writer's authorization and after any Company-currency or
    RateDefinition-structure lock, before the first DML. The row is locked FOR SHARE so a
    concurrent transfer/termination (FOR NO KEY UPDATE, which conflicts with FOR SHARE but
    not with the FOR KEY SHARE that child-row inserts take) cannot close the assignment
    between this decision and the write's commit; the instant is read only after that lock.
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
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DRIVER_ASSIGNMENT_ENDED",
                "message": DRIVER_ASSIGNMENT_ENDED_MESSAGE,
            },
        )


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
