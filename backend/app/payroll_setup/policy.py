"""Internal policy mutations; callers supply one transaction for state and audit."""

import hashlib
import json
from datetime import date, timedelta
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from .audit import write_policy_audit
from .chronology import Schedule, is_period_start
from .errors import PolicyError
from .locks import lock_branches, lock_company, lock_setups
from .security import require_policy_permission
from .validation import (
    next_version_boundary,
    terminal_version,
    validate_boundary,
    validate_next_version,
    version_schedule,
)


def canonical_config_hash(schedule: Schedule) -> str:
    payload = {
        "anchor_start_date": schedule.anchor_start_date.isoformat(),
        "custom_interval_days": schedule.custom_interval_days,
        "normal_days_off_mask": schedule.normal_days_off_mask,
        "payroll_frequency": schedule.frequency,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


async def _one(db: AsyncConnection, query: str, **params):
    result = await db.execute(text(query), params)
    return result.mappings().one_or_none()


async def _authorize_write(
    company_id: int, user_id: int, permission_code: str, db: AsyncConnection,
) -> None:
    if (not db.in_transaction()
            or db.sync_connection.get_execution_options().get("isolation_level") == "AUTOCOMMIT"):
        raise RuntimeError("Payroll Setup policy writes require a managed transaction")
    await require_policy_permission(company_id, user_id, permission_code, db)


async def _setup(db: AsyncConnection, company_id: int, setup_id: int, *, active=False):
    row = await _one(db, """
        SELECT PayrollSetupID, SetupCode, SetupName, Description, Status
        FROM payroll.PayrollSetups
        WHERE CompanyID = :cid AND PayrollSetupID = :sid
    """, cid=company_id, sid=setup_id)
    if row is None:
        raise PolicyError("SETUP_NOT_FOUND", "Payroll Setup does not belong to Company")
    if active and row["status"] != "Active":
        raise PolicyError("SETUP_NOT_ACTIVE", "Payroll Setup is not Active")
    return row


async def _draft(db: AsyncConnection, company_id: int, setup_id: int, draft_id: int):
    row = await _one(db, """
        SELECT * FROM payroll.PayrollSetupVersions
        WHERE CompanyID = :cid AND PayrollSetupID = :sid
          AND PayrollSetupVersionID = :vid
    """, cid=company_id, sid=setup_id, vid=draft_id)
    if row is None or row["lifecyclestate"] != "Draft" or row["discardedatutc"] is not None:
        raise PolicyError("DRAFT_NOT_EDITABLE", "Version is not an editable Draft")
    return row


def _schedule(frequency: str, anchor: date, interval: int | None, mask: int) -> Schedule:
    try:
        return Schedule(frequency, anchor, interval, mask)
    except (ValueError, TypeError) as exc:
        raise PolicyError("INVALID_SCHEDULE", str(exc)) from exc


async def list_setups(company_id: int, user_id: int, db: AsyncConnection) -> list[dict]:
    await require_policy_permission(company_id, user_id, "payroll_setup.view", db)
    result = await db.execute(text("""
        SELECT PayrollSetupID, SetupCode, SetupName, Description, Status
        FROM payroll.PayrollSetups WHERE CompanyID = :cid
        ORDER BY SetupCode, PayrollSetupID
    """), {"cid": company_id})
    return [dict(row) for row in result.mappings().all()]


async def get_setup(
    company_id: int, user_id: int, setup_id: int, db: AsyncConnection,
) -> dict:
    await require_policy_permission(company_id, user_id, "payroll_setup.view", db)
    return dict(await _setup(db, company_id, setup_id))


async def create_setup(
    company_id: int, user_id: int, setup_code: str, setup_name: str,
    db: AsyncConnection, *, description: str | None = None,
) -> int:
    await _authorize_write(company_id, user_id, "payroll_setup.manage", db)
    result = await db.execute(text("""
        INSERT INTO payroll.PayrollSetups
            (CompanyID, SetupCode, SetupName, Description, CreatedByUserID)
        VALUES (:cid, :code, :name, :description, :uid)
        ON CONFLICT (CompanyID, SetupCode) DO NOTHING
        RETURNING PayrollSetupID
    """), {"cid": company_id, "code": setup_code, "name": setup_name,
           "description": description, "uid": user_id})
    setup_id = result.scalar_one_or_none()
    if setup_id is None:
        raise PolicyError(
            "SETUP_CODE_CONFLICT", "Payroll Setup code already exists for this Company",
        )
    await write_policy_audit(db, company_id=company_id, actor_user_id=user_id,
                             event_type="SetupCreated", payroll_setup_id=setup_id,
                             new_state={"code": setup_code, "name": setup_name,
                                        "description": description})
    return setup_id


async def update_setup_metadata(
    company_id: int, user_id: int, setup_id: int, setup_name: str,
    db: AsyncConnection, *, description: str | None = None,
) -> None:
    await _authorize_write(company_id, user_id, "payroll_setup.manage", db)
    await lock_setups(company_id, [setup_id], db)
    prior = await _setup(db, company_id, setup_id, active=True)
    await db.execute(text("""
        UPDATE payroll.PayrollSetups SET SetupName = :name, Description = :description,
            UpdatedByUserID = :uid, UpdatedAtUtc = NOW()
        WHERE CompanyID = :cid AND PayrollSetupID = :sid
    """), {"name": setup_name, "description": description, "uid": user_id,
           "cid": company_id, "sid": setup_id})
    await write_policy_audit(db, company_id=company_id, actor_user_id=user_id,
                             event_type="SetupMetadataChanged", payroll_setup_id=setup_id,
                             old_state={"name": prior["setupname"],
                                        "description": prior["description"]},
                             new_state={"name": setup_name, "description": description})


async def create_draft(
    company_id: int, user_id: int, setup_id: int, db: AsyncConnection,
    *, payroll_frequency: str | None = None, anchor_start_date: date | None = None,
    custom_interval_days: int | None = None, normal_days_off_mask: int | None = None,
) -> int:
    await _authorize_write(company_id, user_id, "payroll_setup.manage", db)
    await lock_setups(company_id, [setup_id], db)
    await _setup(db, company_id, setup_id, active=True)
    result = await db.execute(text("""
        INSERT INTO payroll.PayrollSetupVersions
            (CompanyID, PayrollSetupID, PayrollFrequency, AnchorStartDate,
             CustomIntervalDays, NormalDaysOffMask, CreatedByUserID)
        VALUES (:cid, :sid, :frequency, :anchor, :interval, :mask, :uid)
        RETURNING PayrollSetupVersionID
    """), {"cid": company_id, "sid": setup_id, "frequency": payroll_frequency,
           "anchor": anchor_start_date, "interval": custom_interval_days,
           "mask": normal_days_off_mask, "uid": user_id})
    draft_id = result.scalar_one()
    await write_policy_audit(db, company_id=company_id, actor_user_id=user_id,
                             event_type="DraftCreated", payroll_setup_id=setup_id,
                             payroll_setup_version_id=draft_id,
                             new_state={"frequency": payroll_frequency,
                                        "anchor": anchor_start_date.isoformat() if anchor_start_date else None,
                                        "interval": custom_interval_days, "mask": normal_days_off_mask})
    return draft_id


async def edit_draft(
    company_id: int, user_id: int, setup_id: int, draft_id: int, db: AsyncConnection,
    *, payroll_frequency: str, anchor_start_date: date,
    custom_interval_days: int | None, normal_days_off_mask: int,
) -> None:
    await _authorize_write(company_id, user_id, "payroll_setup.manage", db)
    await lock_setups(company_id, [setup_id], db)
    prior = await _draft(db, company_id, setup_id, draft_id)
    await db.execute(text("""
        UPDATE payroll.PayrollSetupVersions SET PayrollFrequency = :frequency,
            AnchorStartDate = :anchor, CustomIntervalDays = :interval,
            NormalDaysOffMask = :mask
        WHERE PayrollSetupVersionID = :vid
    """), {"frequency": payroll_frequency, "anchor": anchor_start_date,
           "interval": custom_interval_days, "mask": normal_days_off_mask, "vid": draft_id})
    await write_policy_audit(db, company_id=company_id, actor_user_id=user_id,
                             event_type="DraftChanged", payroll_setup_id=setup_id,
                             payroll_setup_version_id=draft_id,
                             old_state={"frequency": prior["payrollfrequency"],
                                        "anchor": prior["anchorstartdate"].isoformat() if prior["anchorstartdate"] else None,
                                        "interval": prior["customintervaldays"],
                                        "mask": prior["normaldaysoffmask"]},
                             new_state={"frequency": payroll_frequency,
                                        "anchor": anchor_start_date.isoformat(),
                                        "interval": custom_interval_days,
                                        "mask": normal_days_off_mask})


async def discard_draft(
    company_id: int, user_id: int, setup_id: int, draft_id: int, db: AsyncConnection,
) -> None:
    await _authorize_write(company_id, user_id, "payroll_setup.manage", db)
    await lock_setups(company_id, [setup_id], db)
    await _draft(db, company_id, setup_id, draft_id)
    await db.execute(text("""
        UPDATE payroll.PayrollSetupVersions
        SET DiscardedByUserID = :uid, DiscardedAtUtc = NOW()
        WHERE PayrollSetupVersionID = :vid
    """), {"uid": user_id, "vid": draft_id})
    await write_policy_audit(db, company_id=company_id, actor_user_id=user_id,
                             event_type="DraftDiscarded", payroll_setup_id=setup_id,
                             payroll_setup_version_id=draft_id)


async def _publication_assignments(
    db: AsyncConnection, company_id: int, setup_id: int,
    effective_from_date: date, next_date: date | None,
):
    result = await db.execute(text("""
        SELECT BranchID, EffectiveFromDate, EffectiveToDate
        FROM payroll.BranchPayrollSetupAssignments
        WHERE CompanyID = :cid AND PayrollSetupID = :sid AND WithdrawnAtUtc IS NULL
          AND (EffectiveToDate IS NULL OR EffectiveToDate > :effective)
          AND (CAST(:next_date AS DATE) IS NULL
               OR EffectiveFromDate < CAST(:next_date AS DATE))
        ORDER BY BranchID, EffectiveFromDate
    """), {"cid": company_id, "sid": setup_id, "effective": effective_from_date,
           "next_date": next_date})
    return result.mappings().all()


async def _validate_publication_assignment(
    db: AsyncConnection, company_id: int, setup_id: int,
    effective_from_date: date, next_date: date | None,
    schedule: Schedule, assignment, current_at_date,
) -> None:
    branch_id = assignment["branchid"]
    boundary = max(effective_from_date, assignment["effectivefromdate"])
    same_date = (current_at_date is not None
                 and current_at_date["effectivefromdate"] == effective_from_date)
    predecessor = None
    if boundary == effective_from_date and not same_date:
        predecessor = version_schedule(
            await terminal_version(db, setup_id, boundary - timedelta(days=1)))
        if predecessor is None and assignment["effectivefromdate"] < boundary:
            raise PolicyError("VERSION_COVERAGE_GAP",
                              "Existing assignment has no predecessor Version coverage")
    elif boundary > effective_from_date:
        preceding = await _one(db, """
            SELECT PayrollSetupID FROM payroll.BranchPayrollSetupAssignments
            WHERE CompanyID = :cid AND BranchID = :bid AND WithdrawnAtUtc IS NULL
              AND EffectiveFromDate <= :prior
              AND (EffectiveToDate IS NULL OR EffectiveToDate > :prior)
        """, cid=company_id, bid=branch_id, prior=boundary - timedelta(days=1))
        if preceding is not None:
            predecessor = version_schedule(await terminal_version(
                db, preceding["payrollsetupid"], boundary - timedelta(days=1)))
    elif same_date:
        predecessor = version_schedule(current_at_date)
    impact_end = min(
        (x for x in (next_date, assignment["effectivetodate"]) if x is not None),
        default=None,
    )
    await validate_boundary(db, company_id, branch_id, boundary,
                            predecessor, schedule, affected_until=impact_end)
    await validate_next_version(db, company_id, branch_id, setup_id, schedule,
                                effective_from_date,
                                assignment_end=assignment["effectivetodate"])
    if assignment["effectivetodate"] is not None and (
        next_date is None or assignment["effectivetodate"] < next_date
    ):
        successor_assignment = await _one(db, """
            SELECT PayrollSetupID FROM payroll.BranchPayrollSetupAssignments
            WHERE CompanyID = :cid AND BranchID = :bid AND WithdrawnAtUtc IS NULL
              AND EffectiveFromDate = :boundary
        """, cid=company_id, bid=branch_id,
            boundary=assignment["effectivetodate"])
        successor = (version_schedule(await terminal_version(
            db, successor_assignment["payrollsetupid"], assignment["effectivetodate"]))
            if successor_assignment is not None else None)
        await validate_boundary(db, company_id, branch_id,
                                assignment["effectivetodate"], schedule, successor,
                                protect_future_periods=False)


async def publish_version(
    company_id: int, user_id: int, setup_id: int, draft_id: int,
    effective_from_date: date, db: AsyncConnection,
    *, replaces_version_id: int | None = None,
) -> int:
    await _authorize_write(company_id, user_id, "payroll_setup.publish", db)
    await lock_setups(company_id, [setup_id], db)
    await _setup(db, company_id, setup_id, active=True)
    draft = await _draft(db, company_id, setup_id, draft_id)
    schedule = _schedule(draft["payrollfrequency"], draft["anchorstartdate"],
                         draft["customintervaldays"], draft["normaldaysoffmask"])
    if effective_from_date < schedule.anchor_start_date:
        raise PolicyError("INVALID_EFFECTIVE_DATE", "Publication precedes the schedule anchor")
    if not is_period_start(schedule, effective_from_date):
        raise PolicyError("SUCCESSOR_BOUNDARY_INVALID", "Effective date is not a schedule boundary")
    current_at_date = await terminal_version(db, setup_id, effective_from_date)
    same_date = (current_at_date is not None
                 and current_at_date["effectivefromdate"] == effective_from_date)
    if replaces_version_id is None and same_date:
        raise PolicyError("REPLACEMENT_REQUIRED", "Same-date publication must replace terminal version")
    if replaces_version_id is not None:
        if not same_date or current_at_date["payrollsetupversionid"] != replaces_version_id:
            raise PolicyError("REPLACEMENT_NOT_TERMINAL", "Replacement must target same-date terminal version")
    result = await db.execute(text("""
        SELECT MAX(EffectiveFromDate) FROM payroll.PayrollSetupVersions
        WHERE PayrollSetupID = :sid AND LifecycleState = 'Published'
    """), {"sid": setup_id})
    latest_existing_date = result.scalar_one_or_none()
    next_date = await next_version_boundary(db, setup_id, effective_from_date)
    result = await db.execute(text("""
        SELECT DISTINCT BranchID FROM payroll.BranchPayrollSetupAssignments
        WHERE CompanyID = :cid AND PayrollSetupID = :sid AND WithdrawnAtUtc IS NULL
          AND (EffectiveToDate IS NULL OR EffectiveToDate > :effective)
          AND (CAST(:next_date AS DATE) IS NULL
               OR EffectiveFromDate < CAST(:next_date AS DATE))
        ORDER BY BranchID
    """), {"cid": company_id, "sid": setup_id, "effective": effective_from_date,
           "next_date": next_date})
    affected_ids = [row[0] for row in result.all()]
    await lock_branches(company_id, affected_ids, db)

    # The Setup lock prevents assignment mutations from changing this set.
    affected_assignments = await _publication_assignments(
        db, company_id, setup_id, effective_from_date, next_date,
    )
    if sorted(set(r["branchid"] for r in affected_assignments)) != affected_ids:
        raise PolicyError("CONCURRENT_ASSIGNMENT_CHANGE", "Affected Branches changed during publication")
    for assignment in affected_assignments:
        await _validate_publication_assignment(
            db, company_id, setup_id, effective_from_date, next_date,
            schedule, assignment, current_at_date,
        )

    result = await db.execute(text("""
        SELECT COALESCE(MAX(VersionNumber), 0) + 1
        FROM payroll.PayrollSetupVersions WHERE PayrollSetupID = :sid
    """), {"sid": setup_id})
    version_number = result.scalar_one()
    config_hash = canonical_config_hash(schedule)
    await db.execute(text("""
        UPDATE payroll.PayrollSetupVersions SET LifecycleState = 'Published',
            VersionNumber = :number, EffectiveFromDate = :effective,
            ConfigHash = :hash, ReplacesVersionID = :replaces,
            PublishedByUserID = :uid, PublishedAtUtc = NOW()
        WHERE PayrollSetupVersionID = :vid AND CompanyID = :cid AND PayrollSetupID = :sid
    """), {"number": version_number, "effective": effective_from_date,
           "hash": config_hash, "replaces": replaces_version_id, "uid": user_id,
           "vid": draft_id, "cid": company_id, "sid": setup_id})
    await write_policy_audit(
        db, company_id=company_id, actor_user_id=user_id,
        event_type=("VersionReplaced" if replaces_version_id else
                    "FutureVersionScheduled" if latest_existing_date is not None
                    and effective_from_date > latest_existing_date else "VersionPublished"),
        payroll_setup_id=setup_id, payroll_setup_version_id=draft_id,
        old_payroll_setup_id=setup_id if current_at_date else None,
        old_payroll_setup_version_id=(current_at_date["payrollsetupversionid"]
                                      if current_at_date else None),
        new_payroll_setup_id=setup_id, new_payroll_setup_version_id=draft_id,
        effective_date=effective_from_date,
        old_config_hash=current_at_date["confighash"] if current_at_date else None,
        new_config_hash=config_hash,
        old_state={"version_number": current_at_date["versionnumber"]}
        if current_at_date else None,
        new_state={"version_number": version_number,
                   "frequency": schedule.frequency,
                   "anchor": schedule.anchor_start_date.isoformat(),
                   "interval": schedule.custom_interval_days,
                   "mask": schedule.normal_days_off_mask},
        affected_branch_ids=affected_ids,
    )
    return draft_id


async def set_default_setup(
    company_id: int, user_id: int, setup_id: int | None, db: AsyncConnection,
) -> None:
    await _authorize_write(company_id, user_id, "payroll_setup.assign", db)
    await lock_company(company_id, db)
    prior = await _one(db, """
        SELECT DefaultPayrollSetupID FROM core.Companies WHERE CompanyID = :cid
    """, cid=company_id)
    if prior is None:
        raise PolicyError("COMPANY_NOT_FOUND", "Company not found")
    old_id = prior["defaultpayrollsetupid"]
    if setup_id == old_id:
        return
    if setup_id is not None:
        await lock_setups(company_id, [setup_id], db)
        await _setup(db, company_id, setup_id, active=True)
    await db.execute(text("""
        UPDATE core.Companies SET DefaultPayrollSetupID = :sid WHERE CompanyID = :cid
    """), {"sid": setup_id, "cid": company_id})
    await write_policy_audit(db, company_id=company_id, actor_user_id=user_id,
                             event_type="DefaultChanged",
                             old_payroll_setup_id=old_id,
                             new_payroll_setup_id=setup_id,
                             old_state={"setup_id": old_id},
                             new_state={"setup_id": setup_id})


async def _assignment_timeline(db: AsyncConnection, company_id: int, branch_id: int):
    result = await db.execute(text("""
        SELECT * FROM payroll.BranchPayrollSetupAssignments
        WHERE CompanyID = :cid AND BranchID = :bid AND WithdrawnAtUtc IS NULL
        ORDER BY EffectiveFromDate, BranchPayrollSetupAssignmentID
    """), {"cid": company_id, "bid": branch_id})
    return result.mappings().all()


async def _branch(db: AsyncConnection, company_id: int, branch_id: int):
    row = await _one(db, """
        SELECT BranchID, Status FROM core.Branches
        WHERE CompanyID = :cid AND BranchID = :bid
    """, cid=company_id, bid=branch_id)
    if row is None:
        raise PolicyError("BRANCH_NOT_FOUND", "Branch does not belong to Company")
    if row["status"] != "Active":
        raise PolicyError("BRANCH_NOT_OPERATIONAL", "Branch is not Active")
    return row


async def assign_setup(
    company_id: int, user_id: int, branch_id: int, setup_id: int,
    effective_from_date: date, db: AsyncConnection, *, reason: str | None = None,
) -> int:
    await _authorize_write(company_id, user_id, "payroll_setup.assign", db)
    preceding_hint = await _one(db, """
        SELECT PayrollSetupID FROM payroll.BranchPayrollSetupAssignments
        WHERE CompanyID = :cid AND BranchID = :bid AND WithdrawnAtUtc IS NULL
          AND EffectiveToDate = :effective
    """, cid=company_id, bid=branch_id, effective=effective_from_date)
    preceding_setup_id = preceding_hint["payrollsetupid"] if preceding_hint else None
    await lock_setups(company_id,
                      [sid for sid in (setup_id, preceding_setup_id) if sid is not None], db)
    await lock_branches(company_id, [branch_id], db)
    await _branch(db, company_id, branch_id)
    await _setup(db, company_id, setup_id, active=True)
    timeline = await _assignment_timeline(db, company_id, branch_id)
    prior = next((row for row in timeline if row["effectivetodate"] == effective_from_date), None)
    if (prior["payrollsetupid"] if prior else None) != preceding_setup_id:
        raise PolicyError("CONCURRENT_ASSIGNMENT_CHANGE", "Predecessor changed before lock")
    future = next((row for row in timeline if row["effectivefromdate"] > effective_from_date), None)
    if any(row["effectivefromdate"] <= effective_from_date
           and (row["effectivetodate"] is None or row["effectivetodate"] > effective_from_date)
           for row in timeline):
        raise PolicyError("ASSIGNMENT_OVERLAP", "Branch already has effective assignment")
    earlier = [row for row in timeline if row["effectivefromdate"] < effective_from_date]
    if earlier and prior is None:
        raise PolicyError("ASSIGNMENT_GAP", "Assignment would leave earlier schedule coverage gap")
    if timeline and prior is None and future is None:
        last = timeline[-1]
        if last["effectivetodate"] is not None and last["effectivetodate"] < effective_from_date:
            raise PolicyError("ASSIGNMENT_GAP", "Assignment would leave a schedule gap")
    version = await terminal_version(db, setup_id, effective_from_date)
    if version is None:
        raise PolicyError("VERSION_NOT_FOUND", "No Published Version applies at assignment start")
    successor = version_schedule(version)
    predecessor = (version_schedule(await terminal_version(
        db, prior["payrollsetupid"], effective_from_date - timedelta(days=1)))
        if prior is not None else None)
    effective_to = future["effectivefromdate"] if future else None
    await validate_boundary(db, company_id, branch_id, effective_from_date,
                            predecessor, successor, affected_until=effective_to)
    await validate_next_version(db, company_id, branch_id, setup_id,
                                successor, effective_from_date,
                                assignment_end=effective_to)
    if future is not None:
        following = version_schedule(await terminal_version(
            db, future["payrollsetupid"], effective_to))
        if following is None:
            raise PolicyError("VERSION_NOT_FOUND", "Future assignment has no Published Version")
        await validate_boundary(db, company_id, branch_id, effective_to,
                                successor, following, protect_future_periods=False)
    correlation_id = uuid4()
    result = await db.execute(text("""
        INSERT INTO payroll.BranchPayrollSetupAssignments
            (CompanyID, BranchID, PayrollSetupID, EffectiveFromDate, EffectiveToDate,
             CreatedByUserID, ChangeReason, CorrelationID)
        VALUES (:cid, :bid, :sid, :effective, :until, :uid, :reason, :correlation)
        RETURNING BranchPayrollSetupAssignmentID
    """), {"cid": company_id, "bid": branch_id, "sid": setup_id,
           "effective": effective_from_date, "until": effective_to,
           "uid": user_id, "reason": reason, "correlation": correlation_id})
    assignment_id = result.scalar_one()
    await write_policy_audit(
        db, company_id=company_id, actor_user_id=user_id,
        event_type="BranchAssigned", payroll_setup_id=setup_id,
        branch_payroll_setup_assignment_id=assignment_id, branch_id=branch_id,
        new_payroll_setup_id=setup_id,
        new_branch_payroll_setup_assignment_id=assignment_id,
        effective_date=effective_from_date,
        new_state={"effective_to": effective_to.isoformat() if effective_to else None,
                   "reason": reason},
        affected_branch_ids=[branch_id], correlation_id=correlation_id,
    )
    return assignment_id


async def reassign_setup(
    company_id: int, user_id: int, branch_id: int, destination_setup_id: int,
    effective_from_date: date, db: AsyncConnection, *, reason: str | None = None,
) -> int:
    await _authorize_write(company_id, user_id, "payroll_setup.assign", db)
    current = await _one(db, """
        SELECT PayrollSetupID FROM payroll.BranchPayrollSetupAssignments
        WHERE CompanyID = :cid AND BranchID = :bid AND WithdrawnAtUtc IS NULL
          AND EffectiveFromDate < :effective
          AND (EffectiveToDate IS NULL OR EffectiveToDate > :effective)
    """, cid=company_id, bid=branch_id, effective=effective_from_date)
    if current is None:
        raise PolicyError("ASSIGNMENT_NOT_FOUND", "No predecessor assignment spans transition")
    source_setup_id = current["payrollsetupid"]
    if source_setup_id == destination_setup_id:
        raise PolicyError("UNCHANGED_ASSIGNMENT", "Reassignment requires a different Setup")
    await lock_setups(company_id, [source_setup_id, destination_setup_id], db)
    await lock_branches(company_id, [branch_id], db)
    await _branch(db, company_id, branch_id)
    await _setup(db, company_id, destination_setup_id, active=True)
    timeline = await _assignment_timeline(db, company_id, branch_id)
    source = next((row for row in timeline
                   if row["payrollsetupid"] == source_setup_id
                   and row["effectivefromdate"] < effective_from_date
                   and (row["effectivetodate"] is None
                        or row["effectivetodate"] > effective_from_date)), None)
    if source is None:
        raise PolicyError("CONCURRENT_ASSIGNMENT_CHANGE", "Assignment changed before lock")
    old_end = source["effectivetodate"]
    previous_version = await terminal_version(db, source_setup_id,
                                              effective_from_date - timedelta(days=1))
    next_version = await terminal_version(db, destination_setup_id, effective_from_date)
    if previous_version is None or next_version is None:
        raise PolicyError("VERSION_NOT_FOUND", "Both sides require Published Versions")
    predecessor = version_schedule(previous_version)
    successor = version_schedule(next_version)
    await validate_boundary(db, company_id, branch_id, effective_from_date,
                            predecessor, successor, affected_until=old_end)
    await validate_next_version(db, company_id, branch_id, destination_setup_id,
                                successor, effective_from_date, assignment_end=old_end)
    if old_end is not None:
        following = next((row for row in timeline if row["effectivefromdate"] == old_end), None)
        if following is not None:
            following_version = await terminal_version(db, following["payrollsetupid"], old_end)
            if following_version is None:
                raise PolicyError("VERSION_NOT_FOUND", "Future assignment has no Published Version")
            await validate_boundary(db, company_id, branch_id, old_end,
                                    successor, version_schedule(following_version),
                                    protect_future_periods=False)
    await db.execute(text("""
        UPDATE payroll.BranchPayrollSetupAssignments SET EffectiveToDate = :effective
        WHERE BranchPayrollSetupAssignmentID = :aid
    """), {"effective": effective_from_date,
           "aid": source["branchpayrollsetupassignmentid"]})
    correlation_id = uuid4()
    result = await db.execute(text("""
        INSERT INTO payroll.BranchPayrollSetupAssignments
            (CompanyID, BranchID, PayrollSetupID, EffectiveFromDate, EffectiveToDate,
             CreatedByUserID, ChangeReason, CorrelationID)
        VALUES (:cid, :bid, :sid, :effective, :until, :uid, :reason, :correlation)
        RETURNING BranchPayrollSetupAssignmentID
    """), {"cid": company_id, "bid": branch_id, "sid": destination_setup_id,
           "effective": effective_from_date, "until": old_end,
           "uid": user_id, "reason": reason, "correlation": correlation_id})
    new_id = result.scalar_one()
    await write_policy_audit(
        db, company_id=company_id, actor_user_id=user_id,
        event_type="BranchReassigned", payroll_setup_id=destination_setup_id,
        branch_payroll_setup_assignment_id=new_id, branch_id=branch_id,
        old_payroll_setup_id=source_setup_id,
        new_payroll_setup_id=destination_setup_id,
        old_branch_payroll_setup_assignment_id=source["branchpayrollsetupassignmentid"],
        new_branch_payroll_setup_assignment_id=new_id,
        effective_date=effective_from_date,
        old_state={"effective_to": old_end.isoformat() if old_end else None},
        new_state={"effective_to": old_end.isoformat() if old_end else None,
                   "reason": reason},
        affected_branch_ids=[branch_id], correlation_id=correlation_id,
    )
    return new_id


async def withdraw_assignment(
    company_id: int, user_id: int, assignment_id: int, db: AsyncConnection,
    *, reason: str | None = None,
) -> None:
    await _authorize_write(company_id, user_id, "payroll_setup.assign", db)
    initial = await _one(db, """
        SELECT BranchID, PayrollSetupID, EffectiveFromDate
        FROM payroll.BranchPayrollSetupAssignments
        WHERE CompanyID = :cid AND BranchPayrollSetupAssignmentID = :aid
          AND WithdrawnAtUtc IS NULL
    """, cid=company_id, aid=assignment_id)
    if initial is None:
        raise PolicyError("ASSIGNMENT_NOT_FOUND", "Assignment not found or already withdrawn")
    branch_id = initial["branchid"]
    setup_id = initial["payrollsetupid"]
    preceding = await _one(db, """
        SELECT PayrollSetupID FROM payroll.BranchPayrollSetupAssignments
        WHERE CompanyID = :cid AND BranchID = :bid AND WithdrawnAtUtc IS NULL
          AND EffectiveToDate = :effective
    """, cid=company_id, bid=branch_id,
        effective=initial["effectivefromdate"])
    prior_setup_id = preceding["payrollsetupid"] if preceding else None
    await lock_setups(company_id,
                      [sid for sid in (setup_id, prior_setup_id) if sid is not None], db)
    await lock_branches(company_id, [branch_id], db)
    timeline = await _assignment_timeline(db, company_id, branch_id)
    target = next((row for row in timeline
                   if row["branchpayrollsetupassignmentid"] == assignment_id), None)
    if target is None:
        raise PolicyError("CONCURRENT_ASSIGNMENT_CHANGE", "Assignment changed before lock")
    prior = next((row for row in timeline
                  if row["effectivetodate"] == target["effectivefromdate"]), None)
    if (prior["payrollsetupid"] if prior else None) != prior_setup_id:
        raise PolicyError("CONCURRENT_ASSIGNMENT_CHANGE", "Predecessor changed before lock")
    result = await db.execute(text("""
        SELECT 1 FROM payroll.PayrollPeriods
        WHERE CompanyID = :cid AND BranchID = :bid AND Status <> 'Cancelled'
          AND StartDate < COALESCE(:until, 'infinity'::date)
          AND EndDate >= :effective LIMIT 1
    """), {"cid": company_id, "bid": branch_id,
           "effective": target["effectivefromdate"],
           "until": target["effectivetodate"]})
    if result.first() is not None:
        raise PolicyError("PERIOD_HISTORY_CONFLICT", "Assignment has non-cancelled period history")
    if prior is not None:
        predecessor = version_schedule(await terminal_version(
            db, prior_setup_id, target["effectivefromdate"]))
        if predecessor is None:
            raise PolicyError("VERSION_NOT_FOUND", "Predecessor has no Published Version")
        await validate_boundary(db, company_id, branch_id,
                                target["effectivefromdate"], predecessor, predecessor,
                                affected_until=target["effectivetodate"])
        await validate_next_version(
            db, company_id, branch_id, prior_setup_id, predecessor,
            target["effectivefromdate"], assignment_end=target["effectivetodate"],
        )
        if target["effectivetodate"] is not None:
            following = next((row for row in timeline
                              if row["effectivefromdate"] == target["effectivetodate"]), None)
            if following is not None:
                last_predecessor = version_schedule(await terminal_version(
                    db, prior_setup_id, target["effectivetodate"] - timedelta(days=1)))
                next_successor = version_schedule(await terminal_version(
                    db, following["payrollsetupid"], target["effectivetodate"]))
                await validate_boundary(db, company_id, branch_id,
                                        target["effectivetodate"],
                                        last_predecessor, next_successor,
                                        protect_future_periods=False)
    await db.execute(text("""
        UPDATE payroll.BranchPayrollSetupAssignments
        SET WithdrawnByUserID = :uid, WithdrawnAtUtc = NOW(),
            WithdrawalReason = :reason
        WHERE BranchPayrollSetupAssignmentID = :aid
    """), {"uid": user_id, "reason": reason, "aid": assignment_id})
    if prior is not None:
        await db.execute(text("""
            UPDATE payroll.BranchPayrollSetupAssignments
            SET EffectiveToDate = :until
            WHERE BranchPayrollSetupAssignmentID = :aid
        """), {"until": target["effectivetodate"],
               "aid": prior["branchpayrollsetupassignmentid"]})
    await write_policy_audit(
        db, company_id=company_id, actor_user_id=user_id,
        event_type="AssignmentWithdrawn", payroll_setup_id=setup_id,
        branch_payroll_setup_assignment_id=assignment_id, branch_id=branch_id,
        old_payroll_setup_id=setup_id,
        old_branch_payroll_setup_assignment_id=assignment_id,
        effective_date=target["effectivefromdate"],
        old_state={"effective_to": target["effectivetodate"].isoformat()
                   if target["effectivetodate"] else None},
        new_state={"withdrawn": True, "reason": reason},
        affected_branch_ids=[branch_id],
    )


async def archive_setup(
    company_id: int, user_id: int, setup_id: int, db: AsyncConnection,
) -> None:
    await _authorize_write(company_id, user_id, "payroll_setup.manage", db)
    await lock_setups(company_id, [setup_id], db)
    await _setup(db, company_id, setup_id, active=True)
    result = await db.execute(text("""
        SELECT 1 FROM core.Companies
        WHERE CompanyID = :cid AND DefaultPayrollSetupID = :sid
    """), {"cid": company_id, "sid": setup_id})
    if result.first() is not None:
        raise PolicyError("DEFAULT_SETUP_IN_USE", "Select another default before archiving")
    result = await db.execute(text("""
        SELECT DISTINCT BranchID FROM payroll.BranchPayrollSetupAssignments
        WHERE CompanyID = :cid AND PayrollSetupID = :sid
          AND WithdrawnAtUtc IS NULL
    """), {"cid": company_id, "sid": setup_id})
    await lock_branches(company_id, result.scalars().all(), db)
    result = await db.execute(text("""
        SELECT a.EffectiveToDate,
               (SELECT MAX(p.EndDate) FROM payroll.PayrollPeriods p
                WHERE p.CompanyID = a.CompanyID AND p.BranchID = a.BranchID
                  AND p.Status <> 'Cancelled') AS LastPeriodEnd
        FROM payroll.BranchPayrollSetupAssignments a
        WHERE a.CompanyID = :cid AND a.PayrollSetupID = :sid
          AND a.WithdrawnAtUtc IS NULL
    """), {"cid": company_id, "sid": setup_id})
    for assignment in result.mappings():
        end = assignment["effectivetodate"]
        last_period_end = assignment["lastperiodend"]
        if end is None or last_period_end is None or last_period_end < end - timedelta(days=1):
            raise PolicyError("SETUP_ASSIGNED", "An assignment can still govern new period creation")
    await db.execute(text("""
        UPDATE payroll.PayrollSetups SET Status = 'Archived',
            UpdatedByUserID = :uid, UpdatedAtUtc = NOW()
        WHERE CompanyID = :cid AND PayrollSetupID = :sid
    """), {"uid": user_id, "cid": company_id, "sid": setup_id})
    await write_policy_audit(db, company_id=company_id, actor_user_id=user_id,
                             event_type="SetupArchived", payroll_setup_id=setup_id,
                             old_state={"status": "Active"},
                             new_state={"status": "Archived"})


async def preview_policy_impact(
    company_id: int, user_id: int, setup_id: int,
    effective_from_date: date, successor: Schedule,
    db: AsyncConnection, *, replaces_version_id: int | None = None,
) -> dict:
    """Read-only preflight; commit operations always validate again under locks."""
    await require_policy_permission(company_id, user_id, "payroll_setup.view", db)
    await _setup(db, company_id, setup_id, active=True)
    current_at_date = await terminal_version(db, setup_id, effective_from_date)
    prior_version = await terminal_version(
        db, setup_id, effective_from_date - timedelta(days=1))
    next_date = await next_version_boundary(db, setup_id, effective_from_date)
    assignments = await _publication_assignments(
        db, company_id, setup_id, effective_from_date, next_date,
    )
    branch_ids = sorted(set(row["branchid"] for row in assignments))
    conflicts = []
    same_date_id = (
        current_at_date["payrollsetupversionid"]
        if current_at_date and current_at_date["effectivefromdate"] == effective_from_date
        else None
    )
    if same_date_id is not None and replaces_version_id != same_date_id:
        conflicts.append({"branch_id": None, "code": "REPLACEMENT_NOT_TERMINAL",
                          "reason": "Same-date publication must replace terminal Version"})
    elif same_date_id is None and replaces_version_id is not None:
        conflicts.append({"branch_id": None, "code": "REPLACEMENT_NOT_TERMINAL",
                          "reason": "No same-date terminal Version exists"})
    if not is_period_start(successor, effective_from_date):
        conflicts.append({"branch_id": None, "code": "SUCCESSOR_BOUNDARY_INVALID",
                          "reason": "Effective date is not a successor period start"})
    for assignment in assignments:
        if conflicts and conflicts[0]["branch_id"] is None:
            break
        try:
            await _validate_publication_assignment(
                db, company_id, setup_id, effective_from_date, next_date,
                successor, assignment, current_at_date,
            )
        except PolicyError as exc:
            conflicts.append({"branch_id": assignment["branchid"], "code": exc.code,
                              "reason": str(exc)})
    return {
        "setup_id": setup_id, "affected_branch_ids": branch_ids,
        "effective_date": effective_from_date,
        "predecessor_version_id": prior_version["payrollsetupversionid"]
        if prior_version else None,
        "predecessor_hash": prior_version["confighash"] if prior_version else None,
        "current_same_date_version_id": same_date_id,
        "successor_hash": canonical_config_hash(successor),
        "successor_schedule": {
            "frequency": successor.frequency,
            "anchor_start_date": successor.anchor_start_date,
            "custom_interval_days": successor.custom_interval_days,
            "normal_days_off_mask": successor.normal_days_off_mask,
        },
        "next_version_boundary": next_date,
        "conflicts": conflicts, "allowed": not conflicts,
    }


async def preview_reassignment_impact(
    company_id: int, user_id: int, branch_id: int, destination_setup_id: int,
    effective_from_date: date, db: AsyncConnection,
) -> dict:
    """Read-only impact evidence; reassignment repeats this under ordered locks."""
    await require_policy_permission(company_id, user_id, "payroll_setup.view", db)
    await _branch(db, company_id, branch_id)
    await _setup(db, company_id, destination_setup_id, active=True)
    source = await _one(db, """
        SELECT PayrollSetupID, EffectiveToDate
        FROM payroll.BranchPayrollSetupAssignments
        WHERE CompanyID = :cid AND BranchID = :bid AND WithdrawnAtUtc IS NULL
          AND EffectiveFromDate < :effective
          AND (EffectiveToDate IS NULL OR EffectiveToDate > :effective)
    """, cid=company_id, bid=branch_id, effective=effective_from_date)
    successor_version = await terminal_version(db, destination_setup_id,
                                               effective_from_date)
    predecessor_version = (await terminal_version(
        db, source["payrollsetupid"], effective_from_date - timedelta(days=1))
        if source else None)
    conflicts = []
    if source is None:
        conflicts.append({"code": "ASSIGNMENT_NOT_FOUND",
                          "reason": "No predecessor assignment spans transition"})
    elif source["payrollsetupid"] == destination_setup_id:
        conflicts.append({"code": "UNCHANGED_ASSIGNMENT",
                          "reason": "Reassignment requires a different Setup"})
    if successor_version is None or predecessor_version is None:
        conflicts.append({"code": "VERSION_NOT_FOUND",
                          "reason": "Both authorities require Published Versions"})
    if not conflicts:
        try:
            await validate_boundary(
                db, company_id, branch_id, effective_from_date,
                version_schedule(predecessor_version),
                version_schedule(successor_version),
                affected_until=source["effectivetodate"],
            )
            await validate_next_version(
                db, company_id, branch_id, destination_setup_id,
                version_schedule(successor_version), effective_from_date,
                assignment_end=source["effectivetodate"],
            )
            if source["effectivetodate"] is not None:
                following = await _one(db, """
                    SELECT PayrollSetupID FROM payroll.BranchPayrollSetupAssignments
                    WHERE CompanyID = :cid AND BranchID = :bid
                      AND WithdrawnAtUtc IS NULL AND EffectiveFromDate = :boundary
                """, cid=company_id, bid=branch_id,
                    boundary=source["effectivetodate"])
                if following is not None:
                    following_version = await terminal_version(
                        db, following["payrollsetupid"], source["effectivetodate"])
                    if following_version is None:
                        raise PolicyError("VERSION_NOT_FOUND",
                                          "Future assignment has no Published Version")
                    await validate_boundary(
                        db, company_id, branch_id, source["effectivetodate"],
                        version_schedule(successor_version),
                        version_schedule(following_version),
                        protect_future_periods=False,
                    )
        except PolicyError as exc:
            conflicts.append({"code": exc.code, "reason": str(exc)})
    return {
        "branch_id": branch_id,
        "source_setup_id": source["payrollsetupid"] if source else None,
        "destination_setup_id": destination_setup_id,
        "predecessor_version_id": predecessor_version["payrollsetupversionid"]
        if predecessor_version else None,
        "successor_version_id": successor_version["payrollsetupversionid"]
        if successor_version else None,
        "effective_date": effective_from_date,
        "conflicts": conflicts,
        "allowed": not conflicts,
    }
