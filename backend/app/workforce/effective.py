"""Instant-based resolution of a Driver's effective home-Branch assignment."""

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


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
