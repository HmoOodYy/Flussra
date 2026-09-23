"""Canonical date-based resolution of persisted Payroll Setup authority."""

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from .chronology import Schedule, is_period_start, period_end
from .errors import PolicyError


@dataclass(frozen=True)
class Authority:
    company_id: int
    branch_id: int
    assignment_id: int
    setup_id: int
    version_id: int
    version_number: int
    schedule: Schedule
    config_hash: str
    start_date: date
    end_date: date
    next_boundary_date: date | None
    next_boundary_kind: str | None


async def resolve_payroll_setup_version(
    company_id: int,
    branch_id: int,
    period_start_date: date,
    db: AsyncConnection,
) -> Authority:
    """Resolve only new persisted policy; never consult branch-owned legacy settings."""
    result = await db.execute(text("""
        SELECT b.Status AS BranchStatus, c.Status AS CompanyStatus,
               c.IsSuspended AS CompanyIsSuspended
        FROM core.Branches b JOIN core.Companies c ON c.CompanyID = b.CompanyID
        WHERE b.BranchID = :bid AND b.CompanyID = :cid
    """), {"cid": company_id, "bid": branch_id})
    branch = result.mappings().one_or_none()
    if branch is None:
        raise PolicyError("BRANCH_NOT_FOUND", "Branch does not belong to Company")
    if (branch["branchstatus"] != "Active" or branch["companystatus"] != "Active"
            or branch["companyissuspended"]):
        raise PolicyError("BRANCH_NOT_OPERATIONAL", "Branch is not operationally eligible")

    result = await db.execute(text("""
        SELECT BranchPayrollSetupAssignmentID, PayrollSetupID, EffectiveToDate
        FROM payroll.BranchPayrollSetupAssignments
        WHERE CompanyID = :cid AND BranchID = :bid AND WithdrawnAtUtc IS NULL
          AND EffectiveFromDate <= :start
          AND (EffectiveToDate IS NULL OR EffectiveToDate > :start)
    """), {"cid": company_id, "bid": branch_id, "start": period_start_date})
    assignments = result.mappings().all()
    if len(assignments) != 1:
        raise PolicyError("ASSIGNMENT_NOT_FOUND", "Exactly one effective assignment is required")
    assignment = assignments[0]
    setup_id = assignment["payrollsetupid"]

    result = await db.execute(text("""
        SELECT Status FROM payroll.PayrollSetups
        WHERE PayrollSetupID = :sid AND CompanyID = :cid
    """), {"sid": setup_id, "cid": company_id})
    status = result.scalar_one_or_none()
    if status != "Active":
        raise PolicyError("SETUP_NOT_ACTIVE", "Assigned Payroll Setup is not Active")

    result = await db.execute(text("""
        SELECT v.PayrollSetupVersionID, v.VersionNumber, v.PayrollFrequency,
               v.AnchorStartDate, v.CustomIntervalDays, v.NormalDaysOffMask,
               v.ConfigHash
        FROM payroll.PayrollSetupVersions v
        WHERE v.CompanyID = :cid AND v.PayrollSetupID = :sid
          AND v.LifecycleState = 'Published'
          AND v.EffectiveFromDate = (
              SELECT MAX(EffectiveFromDate)
              FROM payroll.PayrollSetupVersions
              WHERE PayrollSetupID = :sid AND LifecycleState = 'Published'
                AND EffectiveFromDate <= :start
          )
          AND NOT EXISTS (
              SELECT 1 FROM payroll.PayrollSetupVersions child
              WHERE child.ReplacesVersionID = v.PayrollSetupVersionID
          )
    """), {"cid": company_id, "sid": setup_id, "start": period_start_date})
    versions = result.mappings().all()
    if len(versions) != 1:
        raise PolicyError("VERSION_NOT_FOUND", "Exactly one terminal Published Version is required")
    version = versions[0]
    schedule = Schedule(
        version["payrollfrequency"], version["anchorstartdate"],
        version["customintervaldays"], version["normaldaysoffmask"],
    )
    if period_start_date < schedule.anchor_start_date:
        raise PolicyError("INVALID_SCHEDULE_BOUNDARY", "Start precedes the schedule anchor")

    result = await db.execute(text("""
        SELECT MAX(EndDate) FROM payroll.PayrollPeriods
        WHERE CompanyID = :cid AND BranchID = :bid AND Status <> 'Cancelled'
          AND EndDate < :start
    """), {"cid": company_id, "bid": branch_id, "start": period_start_date})
    prior_end = result.scalar_one_or_none()
    origin = max(schedule.anchor_start_date, prior_end + timedelta(days=1)) if prior_end else None
    if not is_period_start(schedule, period_start_date, origin=origin):
        raise PolicyError("INVALID_SCHEDULE_BOUNDARY", "Start is not a legal schedule boundary")
    end_date = period_end(schedule, period_start_date)

    result = await db.execute(text("""
        SELECT MIN(BoundaryDate) FROM (
            SELECT EffectiveFromDate AS BoundaryDate
            FROM payroll.BranchPayrollSetupAssignments
            WHERE CompanyID = :cid AND BranchID = :bid AND WithdrawnAtUtc IS NULL
              AND EffectiveFromDate > :start
            UNION ALL
            SELECT EffectiveToDate FROM payroll.BranchPayrollSetupAssignments
            WHERE BranchPayrollSetupAssignmentID = :aid AND EffectiveToDate IS NOT NULL
        ) assignment_boundaries
    """), {"cid": company_id, "bid": branch_id,
           "aid": assignment["branchpayrollsetupassignmentid"],
           "start": period_start_date})
    next_assignment_boundary = result.scalar_one_or_none()
    result = await db.execute(text("""
        SELECT MIN(EffectiveFromDate) FROM payroll.PayrollSetupVersions
        WHERE PayrollSetupID = :sid AND LifecycleState = 'Published'
          AND EffectiveFromDate > :start
    """), {"sid": setup_id, "start": period_start_date})
    next_version_boundary = result.scalar_one_or_none()
    next_boundary = min((value for value in
                         (next_assignment_boundary, next_version_boundary)
                         if value is not None), default=None)
    if next_boundary is not None and next_boundary <= end_date:
        raise PolicyError("AUTHORITY_BOUNDARY_CROSSING", "Period crosses an authority boundary")
    kind = None
    if next_boundary is not None:
        if next_boundary == next_assignment_boundary == next_version_boundary:
            kind = "AssignmentAndVersion"
        elif next_boundary == next_assignment_boundary:
            kind = "Assignment"
        else:
            kind = "Version"

    return Authority(
        company_id, branch_id, assignment["branchpayrollsetupassignmentid"],
        setup_id, version["payrollsetupversionid"], version["versionnumber"],
        schedule, version["confighash"], period_start_date, end_date,
        next_boundary, kind,
    )
