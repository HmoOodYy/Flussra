"""Database-level Driver assignment builders for tests that need exact windows.

The application only ever creates windows that start now or in the future and closes
them at transfer or termination. Tests that need history in the past insert ended
assignments directly (INSERT is not history-guarded) or end an existing one through
the shared exact-trigger suspension, never through a parallel mechanism.
"""

from datetime import datetime

from sqlalchemy import text

from tests.db_state import DRIVER_ASSIGNMENT_HISTORY_TRIGGER, suspended_test_triggers


def instant(value: str | datetime | None) -> datetime | None:
    """Parse an ISO-8601 instant (a trailing Z is UTC) into an aware datetime."""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def insert_driver_assignment(
    db,
    *,
    company_id: int,
    branch_id: int,
    employee_id: int,
    driver_id: int,
    effective_from_at: str | datetime,
    effective_to_at: str | datetime | None = None,
    end_reason: str | None = None,
    transferred_from_assignment_id: int | None = None,
) -> int:
    """Insert one DriverAssignment with exact instants and return its id."""
    return (await db.execute(text("""
        INSERT INTO core.DriverAssignments
            (CompanyID, BranchID, EmployeeID, DriverID, EffectiveFromAt, EffectiveToAt,
             EndReason, TransferredFromAssignmentID)
        VALUES (:company_id, :branch_id, :employee_id, :driver_id,
                :from_at, :to_at,
                :end_reason, :transferred_from)
        RETURNING DriverAssignmentID
    """), {
        "company_id": company_id, "branch_id": branch_id, "employee_id": employee_id,
        "driver_id": driver_id, "from_at": instant(effective_from_at),
        "to_at": instant(effective_to_at),
        "end_reason": end_reason, "transferred_from": transferred_from_assignment_id,
    })).scalar_one()


async def end_driver_assignment(
    db,
    driver_assignment_id: int,
    *,
    ended_at: str | datetime,
    end_reason: str = "Transferred",
    transferred_to_assignment_id: int | None = None,
) -> None:
    """Make an existing assignment historical (a past end), bypassing only the history guard."""
    async with suspended_test_triggers(db, (DRIVER_ASSIGNMENT_HISTORY_TRIGGER,)):
        result = await db.execute(text("""
            UPDATE core.DriverAssignments
            SET EndReason = :end_reason, EffectiveToAt = :ended_at,
                TransferredToAssignmentID = COALESCE(:transferred_to, TransferredToAssignmentID)
            WHERE DriverAssignmentID = :assignment_id
        """), {"end_reason": end_reason, "ended_at": instant(ended_at),
               "transferred_to": transferred_to_assignment_id,
               "assignment_id": driver_assignment_id})
        assert result.rowcount == 1, f"DriverAssignment {driver_assignment_id} not found"


async def current_driver_assignment_id(db, driver_id: int) -> int | None:
    """The Driver's effective assignment at database NOW()."""
    return (await db.execute(
        text("SELECT core.fn_EffectiveDriverAssignment("
             "(SELECT CompanyID FROM core.Drivers WHERE DriverID = :driver_id), :driver_id)"),
        {"driver_id": driver_id},
    )).scalar_one()
