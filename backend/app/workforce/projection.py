"""Reconcile the stored Employee branch from effective Driver identity."""

from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.workforce.effective import resolve_effective_driver_profile


async def sync_employee_branch_projection(
    company_id: int,
    employee_id: int,
    as_of: date,
    db: AsyncConnection,
) -> tuple[int | None, int | None]:
    """Align one Employee branch to its effective Driver profile, if any."""
    result = await db.execute(
        text("""
            SELECT BranchID, EmploymentStatus, TerminationDate
            FROM core.Employees
            WHERE CompanyID = :company_id AND EmployeeID = :employee_id
            FOR UPDATE
        """),
        {"company_id": company_id, "employee_id": employee_id},
    )
    row = result.mappings().first()
    if row is None:
        return None, None
    old_branch_id = row["branchid"]
    current = await resolve_effective_driver_profile(company_id, employee_id, as_of, db)
    termination_date = row["terminationdate"]
    if (
        current is None
        and row["employmentstatus"] == "Terminated"
        and termination_date is not None
        and termination_date <= as_of
    ):
        current = await resolve_effective_driver_profile(
            company_id, employee_id, termination_date, db,
        )
    if current is None or current["branchid"] == old_branch_id:
        return old_branch_id, old_branch_id
    await db.execute(
        text("""
            UPDATE core.Employees
            SET BranchID = :branch_id, UpdatedAtUtc = NOW()
            WHERE CompanyID = :company_id AND EmployeeID = :employee_id
              AND BranchID IS DISTINCT FROM :branch_id
        """),
        {
            "company_id": company_id,
            "employee_id": employee_id,
            "branch_id": current["branchid"],
        },
    )
    return old_branch_id, current["branchid"]


async def sync_company_employee_branch_projections(
    company_id: int,
    as_of: date,
    db: AsyncConnection,
) -> int:
    """Reconcile only stale projections, locking those Employees in key order."""
    mismatched = await db.execute(
        text("""
            SELECT e.EmployeeID
            FROM core.Employees e
            JOIN core.Drivers d
              ON d.CompanyID = e.CompanyID AND d.EmployeeID = e.EmployeeID
             AND d.DriverID = COALESCE(
                 core.fn_EffectiveDriverProfile(e.CompanyID, e.EmployeeID, :as_of),
                 CASE WHEN e.EmploymentStatus = 'Terminated'
                            AND e.TerminationDate IS NOT NULL
                            AND e.TerminationDate <= :as_of
                      THEN core.fn_EffectiveDriverProfile(
                               e.CompanyID, e.EmployeeID, e.TerminationDate
                           )
                 END
             )
            WHERE e.CompanyID = :company_id
              AND e.BranchID IS DISTINCT FROM d.BranchID
            ORDER BY e.EmployeeID
        """),
        {"company_id": company_id, "as_of": as_of},
    )
    employee_ids = [row["employeeid"] for row in mismatched.mappings().all()]
    if not employee_ids:
        return 0
    await db.execute(
        text("""
            SELECT EmployeeID
            FROM core.Employees
            WHERE CompanyID = :company_id AND EmployeeID = ANY(:employee_ids)
            ORDER BY EmployeeID
            FOR UPDATE
        """),
        {"company_id": company_id, "employee_ids": employee_ids},
    )
    result = await db.execute(
        text("""
            UPDATE core.Employees e
            SET BranchID = d.BranchID, UpdatedAtUtc = NOW()
            FROM core.Drivers d
            WHERE e.CompanyID = :company_id
              AND e.EmployeeID = ANY(:employee_ids)
              AND d.CompanyID = e.CompanyID
              AND d.EmployeeID = e.EmployeeID
              AND d.DriverID = COALESCE(
                  core.fn_EffectiveDriverProfile(e.CompanyID, e.EmployeeID, :as_of),
                  CASE WHEN e.EmploymentStatus = 'Terminated'
                             AND e.TerminationDate IS NOT NULL
                             AND e.TerminationDate <= :as_of
                       THEN core.fn_EffectiveDriverProfile(
                                e.CompanyID, e.EmployeeID, e.TerminationDate
                            )
                  END
              )
              AND e.BranchID IS DISTINCT FROM d.BranchID
            RETURNING e.EmployeeID
        """),
        {"company_id": company_id, "as_of": as_of, "employee_ids": employee_ids},
    )
    return len(result.fetchall())
