"""Small read-only readiness projection over canonical Payroll Setup authority."""

from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from .errors import PolicyError
from .resolver import resolve_payroll_setup_version


async def branch_schedule_readiness(
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
    *,
    period_start_date: date | None = None,
) -> tuple[bool, str]:
    """Return (ready, reason-code), without consulting legacy branch settings."""
    result = await db.execute(text("""
        SELECT b.Status AS BranchStatus, c.Status AS CompanyStatus,
               c.IsSuspended AS CompanyIsSuspended
        FROM core.Branches b JOIN core.Companies c ON c.CompanyID = b.CompanyID
        WHERE b.CompanyID = :cid AND b.BranchID = :bid
    """), {"cid": company_id, "bid": branch_id})
    operational = result.mappings().one_or_none()
    if operational is None:
        raise PolicyError("BRANCH_NOT_FOUND", "Branch does not belong to Company")
    if (operational["branchstatus"] != "Active"
            or operational["companystatus"] != "Active"
            or operational["companyissuspended"]):
        return False, "BRANCH_NOT_OPERATIONAL"
    start_date = period_start_date
    if start_date is None:
        result = await db.execute(text("""
            SELECT (SELECT MAX(p.EndDate) + 1
                    FROM payroll.PayrollPeriods p
                    WHERE p.CompanyID = :cid AND p.BranchID = :bid
                      AND p.Status <> 'Cancelled') AS NextAfterPeriod,
                   (SELECT MIN(a.EffectiveFromDate)
                    FROM payroll.BranchPayrollSetupAssignments a
                    WHERE a.CompanyID = :cid AND a.BranchID = :bid
                      AND a.WithdrawnAtUtc IS NULL) AS FirstAssignmentStart
        """), {"cid": company_id, "bid": branch_id})
        row = result.mappings().one()
        start_date = row["nextafterperiod"] or row["firstassignmentstart"]
    if start_date is None:
        return False, "NO_ASSIGNMENT"
    try:
        await resolve_payroll_setup_version(company_id, branch_id, start_date, db)
    except PolicyError as exc:
        reason_map = {
            "ASSIGNMENT_NOT_FOUND": "NO_ASSIGNMENT",
            "VERSION_NOT_FOUND": "NO_PUBLISHED_VERSION",
            "AUTHORITY_BOUNDARY_CROSSING": "AUTHORITY_BOUNDARY_CONFLICT",
            "SETUP_NOT_ACTIVE": "SETUP_NOT_ACTIVE",
            "INVALID_SCHEDULE_BOUNDARY": "INVALID_SCHEDULE_BOUNDARY",
            "BRANCH_NOT_OPERATIONAL": "BRANCH_NOT_OPERATIONAL",
        }
        if exc.code not in reason_map:
            raise
        return False, reason_map[exc.code]
    return True, "READY"
