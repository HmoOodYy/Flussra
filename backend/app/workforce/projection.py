"""Reconcile the stored Employee branch from instant-based Driver identity."""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.sql import build_in_clause


def _require_aware(value: datetime | None) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("as_of must be a timezone-aware datetime")


def _projection_driver_id_sql(instant_sql: str) -> str:
    """Resolve current profile, then last nonempty terminated history, for Employee alias ``e``."""
    return f"""
        COALESCE(
            core.fn_EffectiveDriverProfile(e.CompanyID, e.EmployeeID, {instant_sql}),
            CASE WHEN e.EmploymentStatus = 'Terminated'
                 THEN (SELECT h.DriverID FROM core.Drivers h
                       WHERE h.CompanyID = e.CompanyID AND h.EmployeeID = e.EmployeeID
                         AND h.EffectiveToAt IS NOT NULL
                         AND h.EffectiveToAt <= {instant_sql}
                         AND h.EffectiveToAt > h.EffectiveFromAt
                       ORDER BY h.EffectiveToAt DESC, h.DriverID DESC LIMIT 1)
            END
        )
    """


async def sync_employee_branch_projection(
    company_id: int,
    employee_id: int,
    as_of: datetime | None,
    db: AsyncConnection,
) -> tuple[int | None, int | None]:
    """Align one Employee branch to its current Driver profile or last history."""
    _require_aware(as_of)
    result = await db.execute(text("""
        SELECT BranchID, EmploymentStatus
        FROM core.Employees
        WHERE CompanyID = :company_id AND EmployeeID = :employee_id
        FOR UPDATE
    """), {"company_id": company_id, "employee_id": employee_id})
    row = result.mappings().first()
    if row is None:
        return None, None
    old_branch_id = row["branchid"]
    instant_sql = ":as_of" if as_of is not None else "NOW()"
    resolved_driver_id = _projection_driver_id_sql(instant_sql)
    result = await db.execute(text(f"""
        SELECT d.DriverID, d.BranchID
        FROM core.Employees e
        LEFT JOIN core.Drivers d
          ON d.CompanyID = e.CompanyID AND d.EmployeeID = e.EmployeeID
         AND d.DriverID = {resolved_driver_id}
        WHERE e.CompanyID = :company_id AND e.EmployeeID = :employee_id
    """), {
        "company_id": company_id, "employee_id": employee_id,
        **({"as_of": as_of} if as_of is not None else {}),
    })
    current = result.mappings().one()
    if current["driverid"] is None or current["branchid"] == old_branch_id:
        return old_branch_id, old_branch_id
    await db.execute(text("""
        UPDATE core.Employees
        SET BranchID = :branch_id, UpdatedAtUtc = NOW()
        WHERE CompanyID = :company_id AND EmployeeID = :employee_id
          AND BranchID IS DISTINCT FROM :branch_id
    """), {
        "company_id": company_id,
        "employee_id": employee_id,
        "branch_id": current["branchid"],
    })
    return old_branch_id, current["branchid"]


async def sync_company_employee_branch_projections(
    company_id: int,
    as_of: datetime | None,
    db: AsyncConnection,
) -> int:
    """Reconcile stale projections in key order at one database transaction instant."""
    _require_aware(as_of)
    instant = ":as_of" if as_of is not None else "NOW()"
    params = {"company_id": company_id}
    if as_of is not None:
        params["as_of"] = as_of
    resolved = _projection_driver_id_sql(instant)
    mismatched = await db.execute(text(f"""
        SELECT e.EmployeeID
        FROM core.Employees e
        JOIN core.Drivers d ON d.CompanyID = e.CompanyID AND d.EmployeeID = e.EmployeeID
                            AND d.DriverID = {resolved}
        WHERE e.CompanyID = :company_id AND e.BranchID IS DISTINCT FROM d.BranchID
        ORDER BY e.EmployeeID
    """), params)
    employee_ids = [row["employeeid"] for row in mismatched.mappings().all()]
    if not employee_ids:
        return 0
    await db.execute(text("""
        SELECT EmployeeID FROM core.Employees
        WHERE CompanyID = :company_id AND EmployeeID = ANY(:employee_ids)
        ORDER BY EmployeeID FOR UPDATE
    """), {"company_id": company_id, "employee_ids": employee_ids})
    result = await db.execute(text(f"""
        UPDATE core.Employees e
        SET BranchID = d.BranchID, UpdatedAtUtc = NOW()
        FROM core.Drivers d
        WHERE e.CompanyID = :company_id AND e.EmployeeID = ANY(:employee_ids)
          AND d.CompanyID = e.CompanyID AND d.EmployeeID = e.EmployeeID
          AND d.DriverID = {resolved}
          AND e.BranchID IS DISTINCT FROM d.BranchID
        RETURNING e.EmployeeID
    """), {**params, "employee_ids": employee_ids})
    return len(result.fetchall())


async def count_employee_branch_projections(
    company_id: int,
    branch_ids: list[int],
    db: AsyncConnection,
) -> dict[int, int]:
    """Count people under the current read-only projection policy at database NOW()."""
    if not branch_ids:
        return {}
    in_clause, in_params = build_in_clause(branch_ids, "bid")
    resolved = _projection_driver_id_sql("NOW()")
    result = await db.execute(text(f"""
        SELECT projected.BranchID, COUNT(*) AS cnt
        FROM (
            SELECT COALESCE(d.BranchID, e.BranchID) AS BranchID
            FROM core.Employees e
            LEFT JOIN core.Drivers d
              ON d.CompanyID = e.CompanyID AND d.EmployeeID = e.EmployeeID
             AND d.DriverID = {resolved}
            WHERE e.CompanyID = :company_id
        ) projected
        WHERE projected.BranchID IN ({in_clause})
        GROUP BY projected.BranchID
    """), {"company_id": company_id, **in_params})
    return {row["branchid"]: int(row["cnt"]) for row in result.mappings().all()}
