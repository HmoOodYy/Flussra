"""Tenant-scoped read models for the Payroll Setup API."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from .errors import PolicyError
from .resolver import resolve_payroll_setup_version


def _setup(row) -> dict:
    return {
        "setup_id": row["payrollsetupid"],
        "setup_code": row["setupcode"],
        "setup_name": row["setupname"],
        "description": row["description"],
        "status": row["status"],
    }


def _schedule(row, prefix: str = "") -> dict:
    return {
        "payroll_frequency": row[f"{prefix}payrollfrequency"],
        "anchor_start_date": row[f"{prefix}anchorstartdate"],
        "custom_interval_days": row[f"{prefix}customintervaldays"],
        "normal_days_off_mask": row[f"{prefix}normaldaysoffmask"],
    }


async def get_setup(company_id: int, setup_id: int, db: AsyncConnection) -> dict:
    result = await db.execute(text("""
        SELECT PayrollSetupID, SetupCode, SetupName, Description, Status
        FROM payroll.PayrollSetups
        WHERE CompanyID = :cid AND PayrollSetupID = :sid
    """), {"cid": company_id, "sid": setup_id})
    row = result.mappings().one_or_none()
    if row is None:
        raise PolicyError("SETUP_NOT_FOUND", "Payroll Setup does not belong to Company")
    return _setup(row)


async def get_draft(
    company_id: int, setup_id: int, draft_id: int, db: AsyncConnection,
) -> dict:
    result = await db.execute(text("""
        SELECT PayrollSetupID, PayrollSetupVersionID, LifecycleState,
               PayrollFrequency, AnchorStartDate, CustomIntervalDays,
               NormalDaysOffMask, CreatedAtUtc, DiscardedAtUtc
        FROM payroll.PayrollSetupVersions
        WHERE CompanyID = :cid AND PayrollSetupID = :sid
          AND PayrollSetupVersionID = :vid AND LifecycleState = 'Draft'
    """), {"cid": company_id, "sid": setup_id, "vid": draft_id})
    row = result.mappings().one_or_none()
    if row is None:
        raise PolicyError("DRAFT_NOT_FOUND", "Draft does not belong to Payroll Setup")
    return {
        "setup_id": row["payrollsetupid"], "version_id": row["payrollsetupversionid"],
        "lifecycle_state": row["lifecyclestate"],
        "payroll_frequency": row["payrollfrequency"],
        "anchor_start_date": row["anchorstartdate"],
        "custom_interval_days": row["customintervaldays"],
        "normal_days_off_mask": row["normaldaysoffmask"],
        "created_at_utc": row["createdatutc"],
        "discarded_at_utc": row["discardedatutc"],
    }


async def list_drafts(company_id: int, setup_id: int, db: AsyncConnection) -> list[dict]:
    await get_setup(company_id, setup_id, db)
    result = await db.execute(text("""
        SELECT PayrollSetupID, PayrollSetupVersionID, LifecycleState,
               PayrollFrequency, AnchorStartDate, CustomIntervalDays,
               NormalDaysOffMask, CreatedAtUtc, DiscardedAtUtc
        FROM payroll.PayrollSetupVersions
        WHERE CompanyID = :cid AND PayrollSetupID = :sid AND LifecycleState = 'Draft'
          AND DiscardedAtUtc IS NULL
        ORDER BY CreatedAtUtc DESC, PayrollSetupVersionID DESC
    """), {"cid": company_id, "sid": setup_id})
    rows = result.mappings().all()
    return [
        {
            "setup_id": row["payrollsetupid"], "version_id": row["payrollsetupversionid"],
            "lifecycle_state": row["lifecyclestate"],
            "payroll_frequency": row["payrollfrequency"],
            "anchor_start_date": row["anchorstartdate"],
            "custom_interval_days": row["customintervaldays"],
            "normal_days_off_mask": row["normaldaysoffmask"],
            "created_at_utc": row["createdatutc"],
            "discarded_at_utc": row["discardedatutc"],
        }
        for row in rows
    ]


async def list_versions(company_id: int, setup_id: int, db: AsyncConnection) -> list[dict]:
    await get_setup(company_id, setup_id, db)
    result = await db.execute(text("""
        SELECT v.PayrollSetupID, v.PayrollSetupVersionID, v.LifecycleState,
               v.VersionNumber, v.EffectiveFromDate, v.PayrollFrequency,
               v.AnchorStartDate, v.CustomIntervalDays, v.NormalDaysOffMask,
               v.ConfigHash, v.ReplacesVersionID, child.PayrollSetupVersionID AS ReplacedByVersionID,
               CASE WHEN child.PayrollSetupVersionID IS NOT NULL
                    THEN v.EffectiveFromDate
                    ELSE next_version.NextEffectiveFromDate END AS EffectiveToDate
        FROM payroll.PayrollSetupVersions v
        LEFT JOIN payroll.PayrollSetupVersions child
          ON child.CompanyID = v.CompanyID AND child.PayrollSetupID = v.PayrollSetupID
         AND child.ReplacesVersionID = v.PayrollSetupVersionID
        LEFT JOIN LATERAL (
            SELECT MIN(later.EffectiveFromDate) AS NextEffectiveFromDate
            FROM payroll.PayrollSetupVersions later
            WHERE later.CompanyID = v.CompanyID AND later.PayrollSetupID = v.PayrollSetupID
              AND later.LifecycleState = 'Published'
              AND later.EffectiveFromDate > v.EffectiveFromDate
        ) next_version ON TRUE
        WHERE v.CompanyID = :cid AND v.PayrollSetupID = :sid
          AND v.LifecycleState = 'Published'
        ORDER BY v.EffectiveFromDate, v.VersionNumber, v.PayrollSetupVersionID
    """), {"cid": company_id, "sid": setup_id})
    rows = result.mappings().all()
    return [
        {
            "setup_id": row["payrollsetupid"], "version_id": row["payrollsetupversionid"],
            "lifecycle_state": row["lifecyclestate"], "version_number": row["versionnumber"],
            "effective_from_date": row["effectivefromdate"],
            "effective_to_date": row["effectivetodate"],
            "schedule": _schedule(row), "config_hash": row["confighash"],
            "replaces_version_id": row["replacesversionid"],
            "replaced_by_version_id": row["replacedbyversionid"],
            "is_terminal": row["replacedbyversionid"] is None,
        }
        for row in rows
    ]


async def get_publication_schedule(
    company_id: int, setup_id: int, draft_id: int, db: AsyncConnection,
) -> dict:
    row = await get_draft(company_id, setup_id, draft_id, db)
    if row["discarded_at_utc"] is not None:
        raise PolicyError("DRAFT_NOT_EDITABLE", "Version is not an editable Draft")
    if row["payroll_frequency"] is None or row["anchor_start_date"] is None:
        raise PolicyError("INVALID_SCHEDULE", "Draft schedule is incomplete")
    try:
        from .chronology import Schedule

        Schedule(
            row["payroll_frequency"], row["anchor_start_date"],
            row["custom_interval_days"], row["normal_days_off_mask"],
        )
    except (TypeError, ValueError) as exc:
        raise PolicyError("INVALID_SCHEDULE", str(exc)) from exc
    return {
        "frequency": row["payroll_frequency"],
        "anchor_start_date": row["anchor_start_date"],
        "custom_interval_days": row["custom_interval_days"],
        "normal_days_off_mask": row["normal_days_off_mask"],
    }


async def get_default_setup(company_id: int, db: AsyncConnection) -> dict | None:
    result = await db.execute(text("""
        SELECT s.PayrollSetupID, s.SetupCode, s.SetupName, s.Description, s.Status
        FROM core.Companies c
        LEFT JOIN payroll.PayrollSetups s
          ON s.CompanyID = c.CompanyID AND s.PayrollSetupID = c.DefaultPayrollSetupID
        WHERE c.CompanyID = :cid
    """), {"cid": company_id})
    row = result.mappings().one_or_none()
    if row is None:
        raise PolicyError("COMPANY_NOT_FOUND", "Company not found")
    return _setup(row) if row["payrollsetupid"] is not None else None


def _assignment(row) -> dict:
    return {
        "assignment_id": row["branchpayrollsetupassignmentid"],
        "branch_id": row["branchid"], "setup_id": row["payrollsetupid"],
        "setup_code": row["setupcode"], "setup_name": row["setupname"],
        "effective_from_date": row["effectivefromdate"],
        "effective_to_date": row["effectivetodate"], "reason": row["changereason"],
        "created_at_utc": row["createdatutc"], "withdrawn_at_utc": row["withdrawnatutc"],
        "withdrawal_reason": row["withdrawalreason"],
    }


async def list_assignments(
    company_id: int, branch_id: int, db: AsyncConnection,
) -> list[dict]:
    branch = (await db.execute(text("""
        SELECT BranchID FROM core.Branches WHERE CompanyID = :cid AND BranchID = :bid
    """), {"cid": company_id, "bid": branch_id})).scalar_one_or_none()
    if branch is None:
        raise PolicyError("BRANCH_NOT_FOUND", "Branch does not belong to Company")
    result = await db.execute(text("""
        SELECT a.BranchPayrollSetupAssignmentID, a.BranchID, a.PayrollSetupID,
               s.SetupCode, s.SetupName, a.EffectiveFromDate, a.EffectiveToDate,
               a.ChangeReason, a.CreatedAtUtc, a.WithdrawnAtUtc, a.WithdrawalReason
        FROM payroll.BranchPayrollSetupAssignments a
        JOIN payroll.PayrollSetups s
          ON s.CompanyID = a.CompanyID AND s.PayrollSetupID = a.PayrollSetupID
        WHERE a.CompanyID = :cid AND a.BranchID = :bid
        ORDER BY a.EffectiveFromDate, a.BranchPayrollSetupAssignmentID
    """), {"cid": company_id, "bid": branch_id})
    return [_assignment(row) for row in result.mappings().all()]


async def get_assignment(
    company_id: int, assignment_id: int, db: AsyncConnection,
) -> dict:
    result = await db.execute(text("""
        SELECT a.BranchPayrollSetupAssignmentID, a.BranchID, a.PayrollSetupID,
               s.SetupCode, s.SetupName, a.EffectiveFromDate, a.EffectiveToDate,
               a.ChangeReason, a.CreatedAtUtc, a.WithdrawnAtUtc, a.WithdrawalReason
        FROM payroll.BranchPayrollSetupAssignments a
        JOIN payroll.PayrollSetups s
          ON s.CompanyID = a.CompanyID AND s.PayrollSetupID = a.PayrollSetupID
        WHERE a.CompanyID = :cid AND a.BranchPayrollSetupAssignmentID = :aid
    """), {"cid": company_id, "aid": assignment_id})
    row = result.mappings().one_or_none()
    if row is None:
        raise PolicyError("ASSIGNMENT_NOT_FOUND", "Assignment not found")
    return _assignment(row)


async def get_branch_history(
    company_id: int, branch_id: int, db: AsyncConnection,
) -> dict:
    branch = (await db.execute(text("""
        SELECT BranchID FROM core.Branches WHERE CompanyID = :cid AND BranchID = :bid
    """), {"cid": company_id, "bid": branch_id})).scalar_one_or_none()
    if branch is None:
        raise PolicyError("BRANCH_NOT_FOUND", "Branch does not belong to Company")
    result = await db.execute(text("""
        SELECT a.BranchPayrollSetupAssignmentID, a.BranchID, a.PayrollSetupID,
               s.SetupCode, s.SetupName, a.EffectiveFromDate, a.EffectiveToDate,
               a.ChangeReason, a.CreatedAtUtc, a.WithdrawnAtUtc, a.WithdrawalReason
        FROM payroll.BranchPayrollSetupAssignments a
        JOIN payroll.PayrollSetups s
          ON s.CompanyID = a.CompanyID AND s.PayrollSetupID = a.PayrollSetupID
        WHERE a.CompanyID = :cid AND a.BranchID = :bid
        ORDER BY a.EffectiveFromDate, a.BranchPayrollSetupAssignmentID
    """), {"cid": company_id, "bid": branch_id})
    assignments = result.mappings().all()
    histories = []
    versions_cache: dict[int, list[dict]] = {}
    for row in assignments:
        setup_id = row["payrollsetupid"]
        if setup_id not in versions_cache:
            versions_cache[setup_id] = await list_versions(company_id, setup_id, db)
        assignment = _assignment(row)
        versions = []
        assignment_last = row["effectivetodate"]
        for version in versions_cache[setup_id]:
            if not version["is_terminal"]:
                continue
            start = max(row["effectivefromdate"], version["effective_from_date"])
            version_end = version["effective_to_date"]
            ends = [value for value in (assignment_last, version_end) if value is not None]
            end = min(ends) if ends else None
            if end is not None and start >= end:
                continue
            versions.append({
                "version_id": version["version_id"],
                "version_number": version["version_number"],
                "effective_from_date": start,
                "effective_to_date": end,
                "schedule": version["schedule"], "config_hash": version["config_hash"],
            })
        histories.append({**assignment, "versions": versions})
    return {"branch_id": branch_id, "assignments": histories}


async def get_effective_authority(
    company_id: int, branch_id: int, period_start_date, db: AsyncConnection,
) -> dict:
    authority = await resolve_payroll_setup_version(
        company_id, branch_id, period_start_date, db,
    )
    setup = await get_setup(company_id, authority.setup_id, db)
    return {
        "company_id": authority.company_id, "branch_id": authority.branch_id,
        "assignment_id": authority.assignment_id, "setup_id": authority.setup_id,
        "setup_code": setup["setup_code"], "setup_name": setup["setup_name"],
        "version_id": authority.version_id, "version_number": authority.version_number,
        "schedule": {
            "payroll_frequency": authority.schedule.frequency,
            "anchor_start_date": authority.schedule.anchor_start_date,
            "custom_interval_days": authority.schedule.custom_interval_days,
            "normal_days_off_mask": authority.schedule.normal_days_off_mask,
        },
        "config_hash": authority.config_hash,
        "period_start_date": authority.start_date, "period_end_date": authority.end_date,
        "next_boundary_date": authority.next_boundary_date,
        "next_boundary_kind": authority.next_boundary_kind,
    }
