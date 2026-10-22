"""Instant-based resolution of a Driver's effective home-Branch assignment."""

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
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


DRIVER_ASSIGNMENT_ENDED_MESSAGE = (
    "This Driver assignment has ended; its compensation is historical and read-only."
)
# The one SQLSTATE for a database refusal of historical compensation. Raised with the
# message prefix DRIVER_ASSIGNMENT_ENDED by core.trg_CompensationAssignmentWritable
# (migration 0087; the retired assignment-owned rate tables, which no application writer uses
# any more and which go with them) and with the prefix
# DRIVER_COMPENSATION_HISTORICAL by core.trg_DriverCompensationContextWritable (migration
# 0088; a Driver-owned change whose context tenure has ended).
DRIVER_ASSIGNMENT_ENDED_SQLSTATE = "FL001"

DRIVER_COMPENSATION_HISTORICAL_MESSAGE = (
    "This compensation change starts in a Driver tenure that has ended; "
    "historical compensation is read-only."
)


def driver_compensation_historical_detail() -> dict[str, str]:
    """The one API error body for a refused change whose context tenure has ended."""
    return {
        "code": "DRIVER_COMPENSATION_HISTORICAL",
        "message": DRIVER_COMPENSATION_HISTORICAL_MESSAGE,
    }


def driver_assignment_ended_detail() -> dict[str, str]:
    """The one API error body for a refused write to an ended DriverAssignment."""
    return {"code": "DRIVER_ASSIGNMENT_ENDED", "message": DRIVER_ASSIGNMENT_ENDED_MESSAGE}


def is_driver_assignment_ended_error(exc: DBAPIError) -> bool:
    """Whether the database refused a write because its DriverAssignment has ended."""
    original = exc.orig
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return sqlstate == DRIVER_ASSIGNMENT_ENDED_SQLSTATE


def historical_compensation_refusal_detail(exc: DBAPIError) -> dict[str, str] | None:
    """The API error body for a database refusal of historical compensation (FL001), or None.

    The single mapping point for both triggers: the message prefix tells a write to a retired
    assignment-owned rate of an ended assignment (DRIVER_ASSIGNMENT_ENDED) from a Driver-owned
    change in an ended tenure (DRIVER_COMPENSATION_HISTORICAL).
    """
    if not is_driver_assignment_ended_error(exc):
        return None
    if "DRIVER_COMPENSATION_HISTORICAL" in str(exc.orig):
        return driver_compensation_historical_detail()
    return driver_assignment_ended_detail()


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
