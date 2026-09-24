"""PostgreSQL integration coverage for the Payroll Setup Phase 2 policy domain."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.payroll_setup.errors import PolicyError
from app.payroll_setup.policy import (
    archive_setup,
    assign_setup,
    create_draft,
    create_setup,
    discard_draft,
    edit_draft,
    publish_version,
    reassign_setup,
    set_default_setup,
    withdraw_assignment,
    preview_policy_impact,
)
from app.payroll_setup.chronology import Schedule
from app.payroll_setup.resolver import resolve_payroll_setup_version


@pytest_asyncio.fixture
async def payroll_setup_db(test_database_url):
    """Seed an isolated branch and use a rollback-only transaction per test."""
    engine = create_async_engine(test_database_url, echo=False)
    marker = uuid4().hex[:12]
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            try:
                tenant = (await conn.execute(text("""
                    SELECT c.CompanyID, u.UserID
                    FROM core.Companies c
                    JOIN sec.Users u ON u.CompanyID = c.CompanyID
                    WHERE c.CompanyCode = 'DEMO' AND u.Username = 'admin'
                """))).mappings().one()
                company_id = int(tenant["companyid"])
                user_id = int(tenant["userid"])
                branch_id = (await conn.execute(text("""
                    INSERT INTO core.Branches
                        (CompanyID, BranchCode, BranchName, Status, IsDefault)
                    VALUES (:cid, :code, :name, 'Active', FALSE)
                    RETURNING BranchID
                """), {
                    "cid": company_id,
                    "code": f"P2_{marker}",
                    "name": f"Phase 2 test branch {marker}",
                })).scalar_one()
                yield SimpleNamespace(
                    db=conn, company_id=company_id, user_id=user_id,
                    branch_id=int(branch_id), marker=marker,
                )
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


async def _new_setup(db, *, suffix: str = "A") -> int:
    return await create_setup(
        db.company_id, db.user_id, f"P2_{db.marker}_{suffix}",
        f"Phase 2 setup {suffix}", db.db,
    )


async def _publish(
    db, setup_id: int, *, frequency: str, anchor: date,
    interval: int | None = None, effective: date | None = None,
    replaces_version_id: int | None = None,
) -> int:
    draft_id = await create_draft(
        db.company_id, db.user_id, setup_id, db.db,
        payroll_frequency=frequency, anchor_start_date=anchor,
        custom_interval_days=interval, normal_days_off_mask=0,
    )
    return await publish_version(
        db.company_id, db.user_id, setup_id, draft_id,
        effective or anchor, db.db, replaces_version_id=replaces_version_id,
    )


@pytest.mark.asyncio
async def test_draft_create_edit_discard_lifecycle_and_audit(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db)
    draft_id = await create_draft(
        db.company_id, db.user_id, setup_id, db.db,
        payroll_frequency="Week", anchor_start_date=date(2090, 1, 1),
        normal_days_off_mask=0,
    )
    await edit_draft(
        db.company_id, db.user_id, setup_id, draft_id, db.db,
        payroll_frequency="Custom", anchor_start_date=date(2090, 1, 1),
        custom_interval_days=10, normal_days_off_mask=5,
    )
    stored = (await db.db.execute(text("""
        SELECT PayrollFrequency, CustomIntervalDays, NormalDaysOffMask
        FROM payroll.PayrollSetupVersions
        WHERE PayrollSetupVersionID = :vid
    """), {"vid": draft_id})).one()
    assert tuple(stored) == ("Custom", 10, 5)

    await discard_draft(db.company_id, db.user_id, setup_id, draft_id, db.db)
    with pytest.raises(PolicyError, match="editable Draft") as error:
        await edit_draft(
            db.company_id, db.user_id, setup_id, draft_id, db.db,
            payroll_frequency="Week", anchor_start_date=date(2090, 1, 1),
            custom_interval_days=None, normal_days_off_mask=0,
        )
    assert error.value.code == "DRAFT_NOT_EDITABLE"

    events = (await db.db.execute(text("""
        SELECT EventType FROM payroll.PayrollSetupPolicyAuditEvents
        WHERE CompanyID = :cid AND PayrollSetupID = :sid
        ORDER BY PayrollSetupPolicyAuditEventID
    """), {"cid": db.company_id, "sid": setup_id})).scalars().all()
    assert events == ["SetupCreated", "DraftCreated", "DraftChanged", "DraftDiscarded"]


@pytest.mark.parametrize(
    ("frequency", "interval", "expected_end"),
    [
        ("Week", None, date(2090, 1, 7)),
        ("Biweek", None, date(2090, 1, 14)),
        ("Month", None, date(2090, 1, 31)),
        ("Custom", 10, date(2090, 1, 10)),
    ],
)
@pytest.mark.asyncio
async def test_publish_and_resolver_cover_each_schedule_cadence(
    payroll_setup_db, frequency, interval, expected_end,
):
    db = payroll_setup_db
    setup_id = await _new_setup(db)
    version_id = await _publish(
        db, setup_id, frequency=frequency, interval=interval,
        anchor=date(2090, 1, 1),
    )
    await assign_setup(
        db.company_id, db.user_id, db.branch_id, setup_id,
        date(2090, 1, 1), db.db,
    )

    authority = await resolve_payroll_setup_version(
        db.company_id, db.branch_id, date(2090, 1, 1), db.db,
    )
    assert authority.setup_id == setup_id
    assert authority.version_id == version_id
    assert authority.schedule.frequency == frequency
    assert authority.end_date == expected_end


@pytest.mark.asyncio
async def test_same_date_replacement_resolves_terminal_published_version(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db)
    effective = date(2090, 1, 1)
    original_id = await _publish(
        db, setup_id, frequency="Week", anchor=effective,
    )
    replacement_id = await _publish(
        db, setup_id, frequency="Week", anchor=effective,
        replaces_version_id=original_id,
    )

    # A default is not an assignment; without one, resolution must fail closed.
    with pytest.raises(PolicyError) as error:
        await resolve_payroll_setup_version(
            db.company_id, db.branch_id, effective, db.db,
        )
    assert error.value.code == "ASSIGNMENT_NOT_FOUND"

    await assign_setup(db.company_id, db.user_id, db.branch_id, setup_id, effective, db.db)
    authority = await resolve_payroll_setup_version(
        db.company_id, db.branch_id, effective, db.db,
    )
    assert authority.version_id == replacement_id
    assert authority.version_id != original_id


@pytest.mark.asyncio
async def test_default_does_not_fallback_and_resolver_requires_assignment(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db)
    effective = date(2090, 1, 1)
    await _publish(db, setup_id, frequency="Week", anchor=effective)
    await set_default_setup(db.company_id, db.user_id, setup_id, db.db)

    with pytest.raises(PolicyError) as error:
        await resolve_payroll_setup_version(
            db.company_id, db.branch_id, effective, db.db,
        )
    assert error.value.code == "ASSIGNMENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_reassignment_records_exact_audit_and_archive_guards(payroll_setup_db):
    db = payroll_setup_db
    first_setup = await _new_setup(db, suffix="A")
    second_setup = await _new_setup(db, suffix="B")
    anchor = date(2090, 1, 1)
    await _publish(db, first_setup, frequency="Week", anchor=anchor)
    await _publish(db, second_setup, frequency="Week", anchor=anchor)
    initial_assignment = await assign_setup(
        db.company_id, db.user_id, db.branch_id, first_setup, anchor, db.db,
    )
    boundary = date(2090, 1, 8)
    next_assignment = await reassign_setup(
        db.company_id, db.user_id, db.branch_id, second_setup, boundary, db.db,
        reason="Phase 2 domain test",
    )

    old_range = (await db.db.execute(text("""
        SELECT EffectiveToDate FROM payroll.BranchPayrollSetupAssignments
        WHERE BranchPayrollSetupAssignmentID = :aid
    """), {"aid": initial_assignment})).scalar_one()
    assert old_range == boundary

    authority = await resolve_payroll_setup_version(
        db.company_id, db.branch_id, boundary, db.db,
    )
    assert authority.setup_id == second_setup
    assert authority.assignment_id == next_assignment

    event = (await db.db.execute(text("""
        SELECT EventType, PayrollSetupID, BranchPayrollSetupAssignmentID,
               OldPayrollSetupID, NewPayrollSetupID, EffectiveDate
        FROM payroll.PayrollSetupPolicyAuditEvents
        WHERE CompanyID = :cid AND BranchID = :bid AND EventType = 'BranchReassigned'
    """), {"cid": db.company_id, "bid": db.branch_id})).mappings().one()
    assert (
        event["eventtype"], event["payrollsetupid"],
        event["branchpayrollsetupassignmentid"], event["oldpayrollsetupid"],
        event["newpayrollsetupid"], event["effectivedate"],
    ) == ("BranchReassigned", second_setup, next_assignment, first_setup, second_setup, boundary)

    affected = (await db.db.execute(text("""
        SELECT eb.BranchID FROM payroll.PayrollSetupPolicyAuditEventBranches eb
        JOIN payroll.PayrollSetupPolicyAuditEvents e
          USING (PayrollSetupPolicyAuditEventID, CompanyID)
        WHERE e.CompanyID = :cid AND e.BranchID = :bid AND e.EventType = 'BranchReassigned'
    """), {"cid": db.company_id, "bid": db.branch_id})).scalars().all()
    assert affected == [db.branch_id]

    with pytest.raises(PolicyError) as error:
        await archive_setup(db.company_id, db.user_id, second_setup, db.db)
    assert error.value.code == "SETUP_ASSIGNED"


@pytest.mark.asyncio
async def test_archive_rejects_open_ended_and_future_assignments(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db)
    anchor = date(2090, 1, 1)
    await _publish(db, setup_id, frequency="Week", anchor=anchor)
    await assign_setup(db.company_id, db.user_id, db.branch_id, setup_id, anchor, db.db)

    with pytest.raises(PolicyError) as error:
        await archive_setup(db.company_id, db.user_id, setup_id, db.db)
    assert error.value.code == "SETUP_ASSIGNED"

    future_branch = (await db.db.execute(text("""
        INSERT INTO core.Branches (CompanyID, BranchCode, BranchName)
        VALUES (:cid, :code, :name) RETURNING BranchID
    """), {"cid": db.company_id, "code": f"P2_{db.marker}_F",
           "name": f"Future branch {db.marker}"})).scalar_one()
    future_setup = await _new_setup(db, suffix="F")
    await _publish(db, future_setup, frequency="Week", anchor=date(2090, 2, 5))
    await assign_setup(db.company_id, db.user_id, future_branch,
                       future_setup, date(2090, 2, 5), db.db)
    with pytest.raises(PolicyError) as error:
        await archive_setup(db.company_id, db.user_id, future_setup, db.db)
    assert error.value.code == "SETUP_ASSIGNED"


@pytest.mark.asyncio
async def test_archive_requires_payroll_chronology_beyond_ended_assignment(payroll_setup_db):
    db = payroll_setup_db
    first = await _new_setup(db, suffix="A")
    second = await _new_setup(db, suffix="B")
    anchor = date(2090, 1, 1)
    version_id = await _publish(db, first, frequency="Week", anchor=anchor)
    await _publish(db, second, frequency="Week", anchor=anchor)
    assignment_id = await assign_setup(
        db.company_id, db.user_id, db.branch_id, first, anchor, db.db,
    )
    await reassign_setup(
        db.company_id, db.user_id, db.branch_id, second, date(2090, 1, 8), db.db,
    )

    # An ended interval can still be used when its first payroll has not been created.
    with pytest.raises(PolicyError) as error:
        await archive_setup(db.company_id, db.user_id, first, db.db)
    assert error.value.code == "SETUP_ASSIGNED"

    period_id = (await db.db.execute(text("""
        INSERT INTO payroll.PayrollPeriods
            (CompanyID, BranchID, PeriodCode, PeriodName, PeriodType,
             StartDate, EndDate, Status, BranchPayrollSetupAssignmentID,
             PayrollSetupVersionID, FrozenPayrollSetupID, FrozenPayrollSetupCode,
             FrozenPayrollSetupVersionNumber, FrozenPayrollFrequency,
             FrozenAnchorStartDate, FrozenCustomIntervalDays,
             FrozenNormalDaysOffMask, ScheduleConfigHash)
        SELECT :cid, :bid, :code, 'Historical A', 'Week',
               '2090-01-01', '2090-01-07', 'Approved', :aid,
               v.PayrollSetupVersionID, s.PayrollSetupID, s.SetupCode,
               v.VersionNumber, v.PayrollFrequency, v.AnchorStartDate,
               v.CustomIntervalDays, v.NormalDaysOffMask, v.ConfigHash
        FROM payroll.PayrollSetupVersions v
        JOIN payroll.PayrollSetups s ON s.PayrollSetupID = v.PayrollSetupID
        WHERE v.PayrollSetupVersionID = :vid AND v.CompanyID = :cid
        RETURNING PayrollPeriodID
    """), {"cid": db.company_id, "bid": db.branch_id,
           "code": f"P2_ARCHIVE_{db.marker}", "aid": assignment_id,
           "vid": version_id})).scalar_one()

    await archive_setup(db.company_id, db.user_id, first, db.db)
    history = (await db.db.execute(text("""
        SELECT p.BranchPayrollSetupAssignmentID, p.PayrollSetupVersionID,
               p.ScheduleConfigHash, a.EffectiveToDate, a.WithdrawnAtUtc,
               s.Status, v.ConfigHash
        FROM payroll.PayrollPeriods p
        JOIN payroll.BranchPayrollSetupAssignments a
          ON a.BranchPayrollSetupAssignmentID = p.BranchPayrollSetupAssignmentID
        JOIN payroll.PayrollSetupVersions v
          ON v.PayrollSetupVersionID = p.PayrollSetupVersionID
        JOIN payroll.PayrollSetups s ON s.PayrollSetupID = a.PayrollSetupID
        WHERE p.CompanyID = :cid AND p.PayrollPeriodID = :pid
    """), {"cid": db.company_id, "pid": period_id})).mappings().one()
    assert history["branchpayrollsetupassignmentid"] == assignment_id
    assert history["payrollsetupversionid"] == version_id
    assert history["scheduleconfighash"] == history["confighash"]
    assert history["effectivetodate"] == date(2090, 1, 8)
    assert history["withdrawnatutc"] is None
    assert history["status"] == "Archived"
    events = (await db.db.execute(text("""
        SELECT EventType FROM payroll.PayrollSetupPolicyAuditEvents
        WHERE CompanyID = :cid AND PayrollSetupID = :sid
    """), {"cid": db.company_id, "sid": first})).scalars().all()
    assert "BranchAssigned" in events
    assert "SetupArchived" in events


@pytest.mark.asyncio
async def test_company_policy_permission_rejects_branch_scoped_user(payroll_setup_db):
    db = payroll_setup_db
    branch_user_id = (await db.db.execute(text("""
        SELECT UserID FROM sec.Users WHERE CompanyID = :cid AND Username = 'branch_user'
    """), {"cid": db.company_id})).scalar_one()
    with pytest.raises(HTTPException) as error:
        await create_setup(
            db.company_id, int(branch_user_id), f"P2_DENIED_{db.marker}",
            "Must be denied", db.db,
        )
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_invalid_schedule_and_unpublished_assignment_fail_closed(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db)
    invalid_draft = await create_draft(
        db.company_id, db.user_id, setup_id, db.db,
        payroll_frequency="Custom", anchor_start_date=date(2090, 1, 1),
        custom_interval_days=None, normal_days_off_mask=0,
    )
    with pytest.raises(PolicyError) as error:
        await publish_version(
            db.company_id, db.user_id, setup_id, invalid_draft,
            date(2090, 1, 1), db.db,
        )
    assert error.value.code == "INVALID_SCHEDULE"

    other_setup = await _new_setup(db, suffix="UNPUBLISHED")
    with pytest.raises(PolicyError) as error:
        await assign_setup(
            db.company_id, db.user_id, db.branch_id, other_setup,
            date(2090, 1, 1), db.db,
        )
    assert error.value.code == "VERSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_future_versions_resolve_by_start_date_and_crossing_is_rejected(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db)
    anchor = date(2090, 1, 1)
    first_id = await _publish(db, setup_id, frequency="Week", anchor=anchor)
    second_id = await _publish(
        db, setup_id, frequency="Week", anchor=anchor,
        effective=date(2090, 1, 15),
    )
    await assign_setup(db.company_id, db.user_id, db.branch_id, setup_id, anchor, db.db)
    first = await resolve_payroll_setup_version(db.company_id, db.branch_id, anchor, db.db)
    second = await resolve_payroll_setup_version(
        db.company_id, db.branch_id, date(2090, 1, 15), db.db,
    )
    assert (first.version_id, first.next_boundary_date) == (first_id, date(2090, 1, 15))
    assert second.version_id == second_id
    preceding = await resolve_payroll_setup_version(
        db.company_id, db.branch_id, date(2090, 1, 8), db.db,
    )
    assert (preceding.version_id, preceding.end_date) == (first_id, date(2090, 1, 14))


@pytest.mark.asyncio
async def test_shared_publication_is_atomic_when_one_branch_has_period_history(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db)
    anchor = date(2090, 1, 1)
    await _publish(db, setup_id, frequency="Week", anchor=anchor)
    second_branch = (await db.db.execute(text("""
        INSERT INTO core.Branches (CompanyID, BranchCode, BranchName)
        VALUES (:cid, :code, :name) RETURNING BranchID
    """), {"cid": db.company_id, "code": f"P2_{db.marker}_B",
           "name": f"Phase 2 second branch {db.marker}"})).scalar_one()
    await assign_setup(db.company_id, db.user_id, db.branch_id, setup_id, anchor, db.db)
    await assign_setup(db.company_id, db.user_id, second_branch, setup_id, anchor, db.db)
    await db.db.execute(text("""
        INSERT INTO payroll.PayrollPeriods
            (CompanyID, BranchID, PeriodCode, PeriodName, PeriodType,
             StartDate, EndDate, Status)
        VALUES (:cid, :bid, :code, 'Protected', 'Week',
                '2090-01-15', '2090-01-21', 'Open')
    """), {"cid": db.company_id, "bid": second_branch,
           "code": f"P2_PROTECTED_{db.marker}"})
    draft_id = await create_draft(
        db.company_id, db.user_id, setup_id, db.db,
        payroll_frequency="Week", anchor_start_date=anchor, normal_days_off_mask=1,
    )
    preview = await preview_policy_impact(
        db.company_id, db.user_id, setup_id, date(2090, 1, 15),
        Schedule("Week", anchor, None, 1), db.db,
    )
    assert preview["affected_branch_ids"] == sorted([db.branch_id, second_branch])
    assert preview["allowed"] is False
    assert preview["conflicts"][0]["branch_id"] == second_branch
    with pytest.raises(PolicyError) as error:
        await publish_version(db.company_id, db.user_id, setup_id, draft_id,
                              date(2090, 1, 15), db.db)
    assert error.value.code == "PERIOD_HISTORY_CONFLICT"
    state = (await db.db.execute(text("""
        SELECT LifecycleState FROM payroll.PayrollSetupVersions
        WHERE PayrollSetupVersionID = :vid
    """), {"vid": draft_id})).scalar_one()
    assert state == "Draft"
    count = (await db.db.execute(text("""
        SELECT COUNT(*) FROM payroll.PayrollSetupPolicyAuditEvents
        WHERE PayrollSetupVersionID = :vid AND EventType = 'VersionPublished'
    """), {"vid": draft_id})).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_cancelled_period_does_not_reserve_publication_chronology(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db)
    anchor = date(2090, 1, 1)
    await _publish(db, setup_id, frequency="Week", anchor=anchor)
    await assign_setup(db.company_id, db.user_id, db.branch_id, setup_id, anchor, db.db)
    await db.db.execute(text("""
        INSERT INTO payroll.PayrollPeriods
            (CompanyID, BranchID, PeriodCode, PeriodName, PeriodType,
             StartDate, EndDate, Status)
        VALUES (:cid, :bid, :code, 'Cancelled', 'Week',
                '2090-01-15', '2090-01-21', 'Cancelled')
    """), {"cid": db.company_id, "bid": db.branch_id,
           "code": f"P2_CANCELLED_{db.marker}"})
    version_id = await _publish(
        db, setup_id, frequency="Week", anchor=anchor,
        effective=date(2090, 1, 15),
    )
    assert version_id > 0


@pytest.mark.asyncio
async def test_impact_preview_reports_conflicting_branch_without_mutation(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db)
    anchor = date(2090, 1, 1)
    await _publish(db, setup_id, frequency="Week", anchor=anchor)
    await assign_setup(db.company_id, db.user_id, db.branch_id, setup_id, anchor, db.db)
    preview = await preview_policy_impact(
        db.company_id, db.user_id, setup_id, date(2090, 1, 3),
        Schedule("Week", anchor, None, 0), db.db,
    )
    assert preview["allowed"] is False
    assert preview["conflicts"][0]["code"] == "SUCCESSOR_BOUNDARY_INVALID"


@pytest.mark.asyncio
async def test_withdraw_future_reassignment_restores_predecessor_coverage(payroll_setup_db):
    db = payroll_setup_db
    first_setup = await _new_setup(db, suffix="A")
    second_setup = await _new_setup(db, suffix="B")
    anchor = date(2090, 1, 1)
    await _publish(db, first_setup, frequency="Week", anchor=anchor)
    await _publish(db, second_setup, frequency="Week", anchor=anchor)
    first_assignment = await assign_setup(
        db.company_id, db.user_id, db.branch_id, first_setup, anchor, db.db,
    )
    second_assignment = await reassign_setup(
        db.company_id, db.user_id, db.branch_id, second_setup,
        date(2090, 1, 8), db.db,
    )
    await withdraw_assignment(
        db.company_id, db.user_id, second_assignment, db.db,
        reason="Scheduled change withdrawn",
    )
    authority = await resolve_payroll_setup_version(
        db.company_id, db.branch_id, date(2090, 1, 8), db.db,
    )
    assert (authority.setup_id, authority.assignment_id) == (first_setup, first_assignment)
    rows = (await db.db.execute(text("""
        SELECT BranchPayrollSetupAssignmentID, EffectiveToDate, WithdrawnAtUtc
        FROM payroll.BranchPayrollSetupAssignments
        WHERE BranchID = :bid AND CompanyID = :cid
        ORDER BY BranchPayrollSetupAssignmentID
    """), {"cid": db.company_id, "bid": db.branch_id})).all()
    assert rows[0][1] is None
    assert rows[1][2] is not None


@pytest.mark.asyncio
async def test_reassignment_rejects_midperiod_boundary_and_preserves_timeline(payroll_setup_db):
    db = payroll_setup_db
    first_setup = await _new_setup(db, suffix="A")
    second_setup = await _new_setup(db, suffix="B")
    anchor = date(2090, 1, 1)
    await _publish(db, first_setup, frequency="Week", anchor=anchor)
    await _publish(db, second_setup, frequency="Week", anchor=anchor)
    original = await assign_setup(
        db.company_id, db.user_id, db.branch_id, first_setup, anchor, db.db,
    )
    with pytest.raises(PolicyError) as error:
        await reassign_setup(
            db.company_id, db.user_id, db.branch_id, second_setup,
            date(2090, 1, 4), db.db,
        )
    assert error.value.code == "PREDECESSOR_BOUNDARY_INVALID"
    authority = await resolve_payroll_setup_version(
        db.company_id, db.branch_id, date(2090, 1, 8), db.db,
    )
    assert authority.assignment_id == original


@pytest.mark.asyncio
async def test_non_cancelled_period_blocks_reassignment(payroll_setup_db):
    db = payroll_setup_db
    first_setup = await _new_setup(db, suffix="A")
    second_setup = await _new_setup(db, suffix="B")
    anchor = date(2090, 1, 1)
    await _publish(db, first_setup, frequency="Week", anchor=anchor)
    await _publish(db, second_setup, frequency="Week", anchor=anchor)
    await assign_setup(db.company_id, db.user_id, db.branch_id, first_setup, anchor, db.db)
    await db.db.execute(text("""
        INSERT INTO payroll.PayrollPeriods
            (CompanyID, BranchID, PeriodCode, PeriodName, PeriodType,
             StartDate, EndDate, Status)
        VALUES (:cid, :bid, :code, 'Protected', 'Week',
                '2090-01-08', '2090-01-14', 'Open')
    """), {"cid": db.company_id, "bid": db.branch_id,
           "code": f"P2_REASSIGN_{db.marker}"})
    with pytest.raises(PolicyError) as error:
        await reassign_setup(db.company_id, db.user_id, db.branch_id,
                             second_setup, date(2090, 1, 8), db.db)
    assert error.value.code == "PERIOD_HISTORY_CONFLICT"


@pytest.mark.asyncio
async def test_archived_and_cross_company_setups_cannot_be_assigned(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db)
    anchor = date(2090, 1, 1)
    await _publish(db, setup_id, frequency="Week", anchor=anchor)
    await archive_setup(db.company_id, db.user_id, setup_id, db.db)
    with pytest.raises(PolicyError) as error:
        await assign_setup(db.company_id, db.user_id, db.branch_id,
                           setup_id, anchor, db.db)
    assert error.value.code == "SETUP_NOT_ACTIVE"
    other_company = (await db.db.execute(text("""
        INSERT INTO core.Companies (CompanyCode, CompanyName)
        VALUES (:code, 'Other company') RETURNING CompanyID
    """), {"code": f"P2_OTHER_{db.marker}"})).scalar_one()
    other_setup = (await db.db.execute(text("""
        INSERT INTO payroll.PayrollSetups (CompanyID, SetupCode, SetupName)
        VALUES (:cid, 'OTHER', 'Other setup') RETURNING PayrollSetupID
    """), {"cid": other_company})).scalar_one()
    with pytest.raises(PolicyError) as error:
        await assign_setup(db.company_id, db.user_id, db.branch_id,
                           other_setup, anchor, db.db)
    assert error.value.code == "SETUP_NOT_FOUND"


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_policy_mutation(payroll_setup_db):
    db = payroll_setup_db
    setup_code = f"P2_AUDIT_FAILURE_{db.marker}"
    with patch("app.payroll_setup.policy.write_policy_audit", side_effect=RuntimeError("audit unavailable")):
        with pytest.raises(RuntimeError, match="audit unavailable"):
            async with db.db.begin_nested():
                await create_setup(db.company_id, db.user_id, setup_code,
                                   "Should roll back", db.db)
    remaining = (await db.db.execute(text("""
        SELECT COUNT(*) FROM payroll.PayrollSetups
        WHERE CompanyID = :cid AND SetupCode = :code
    """), {"cid": db.company_id, "code": setup_code})).scalar_one()
    assert remaining == 0


@pytest.mark.asyncio
async def test_resolver_rejects_withdrawn_assignment_without_default_fallback(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db)
    anchor = date(2090, 1, 1)
    await _publish(db, setup_id, frequency="Week", anchor=anchor)
    await set_default_setup(db.company_id, db.user_id, setup_id, db.db)
    assignment_id = await assign_setup(
        db.company_id, db.user_id, db.branch_id, setup_id, anchor, db.db,
    )
    await withdraw_assignment(db.company_id, db.user_id, assignment_id, db.db)
    with pytest.raises(PolicyError) as error:
        await resolve_payroll_setup_version(db.company_id, db.branch_id, anchor, db.db)
    assert error.value.code == "ASSIGNMENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_withdrawal_preserves_non_cancelled_period_history(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db)
    anchor = date(2090, 1, 1)
    await _publish(db, setup_id, frequency="Week", anchor=anchor)
    assignment_id = await assign_setup(
        db.company_id, db.user_id, db.branch_id, setup_id, anchor, db.db,
    )
    await db.db.execute(text("""
        INSERT INTO payroll.PayrollPeriods
            (CompanyID, BranchID, PeriodCode, PeriodName, PeriodType,
             StartDate, EndDate, Status)
        VALUES (:cid, :bid, :code, 'Protected', 'Week',
                '2090-01-01', '2090-01-07', 'Approved')
    """), {"cid": db.company_id, "bid": db.branch_id,
           "code": f"P2_WITHDRAW_{db.marker}"})
    with pytest.raises(PolicyError) as error:
        await withdraw_assignment(db.company_id, db.user_id, assignment_id, db.db)
    assert error.value.code == "PERIOD_HISTORY_CONFLICT"


@pytest.mark.asyncio
async def test_resolver_rejects_persisted_midperiod_version_boundary(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db)
    anchor = date(2090, 1, 1)
    await _publish(db, setup_id, frequency="Week", anchor=anchor)
    await assign_setup(db.company_id, db.user_id, db.branch_id, setup_id, anchor, db.db)
    await db.db.execute(text("""
        INSERT INTO payroll.PayrollSetupVersions
            (CompanyID, PayrollSetupID, LifecycleState, VersionNumber,
             EffectiveFromDate, PayrollFrequency, AnchorStartDate,
             NormalDaysOffMask, ConfigHash, PublishedByUserID, PublishedAtUtc)
        VALUES (:cid, :sid, 'Published', 2, '2090-01-04', 'Week',
                '2090-01-04', 0, :hash, :uid, NOW())
    """), {"cid": db.company_id, "sid": setup_id,
           "hash": "a" * 64, "uid": db.user_id})
    with pytest.raises(PolicyError) as error:
        await resolve_payroll_setup_version(db.company_id, db.branch_id, anchor, db.db)
    assert error.value.code == "AUTHORITY_BOUNDARY_CROSSING"


@pytest.mark.asyncio
async def test_default_change_does_not_rewrite_existing_assignment(payroll_setup_db):
    db = payroll_setup_db
    first = await _new_setup(db, suffix="A")
    second = await _new_setup(db, suffix="B")
    anchor = date(2090, 1, 1)
    await _publish(db, first, frequency="Week", anchor=anchor)
    await _publish(db, second, frequency="Week", anchor=anchor)
    await set_default_setup(db.company_id, db.user_id, first, db.db)
    assignment_id = await assign_setup(
        db.company_id, db.user_id, db.branch_id, first, anchor, db.db,
    )
    await set_default_setup(db.company_id, db.user_id, second, db.db)
    authority = await resolve_payroll_setup_version(
        db.company_id, db.branch_id, anchor, db.db,
    )
    assert (authority.setup_id, authority.assignment_id) == (first, assignment_id)
    with pytest.raises(PolicyError) as error:
        await archive_setup(db.company_id, db.user_id, second, db.db)
    assert error.value.code == "DEFAULT_SETUP_IN_USE"


@pytest.mark.asyncio
async def test_resolver_rejects_persisted_midperiod_assignment_boundary(payroll_setup_db):
    db = payroll_setup_db
    first = await _new_setup(db, suffix="A")
    second = await _new_setup(db, suffix="B")
    anchor = date(2090, 1, 1)
    await _publish(db, first, frequency="Week", anchor=anchor)
    await _publish(db, second, frequency="Week", anchor=date(2090, 1, 4))
    first_assignment = await assign_setup(
        db.company_id, db.user_id, db.branch_id, first, anchor, db.db,
    )
    await db.db.execute(text("""
        UPDATE payroll.BranchPayrollSetupAssignments
        SET EffectiveToDate = '2090-01-04'
        WHERE BranchPayrollSetupAssignmentID = :aid
    """), {"aid": first_assignment})
    await db.db.execute(text("""
        INSERT INTO payroll.BranchPayrollSetupAssignments
            (CompanyID, BranchID, PayrollSetupID, EffectiveFromDate,
             CreatedByUserID)
        VALUES (:cid, :bid, :sid, '2090-01-04', :uid)
    """), {"cid": db.company_id, "bid": db.branch_id,
           "sid": second, "uid": db.user_id})
    with pytest.raises(PolicyError) as error:
        await resolve_payroll_setup_version(db.company_id, db.branch_id, anchor, db.db)
    assert error.value.code == "AUTHORITY_BOUNDARY_CROSSING"


@pytest.mark.asyncio
async def test_publication_rejects_existing_assignment_version_coverage_gap(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db)
    anchor = date(2090, 1, 1)
    await db.db.execute(text("""
        INSERT INTO payroll.BranchPayrollSetupAssignments
            (CompanyID, BranchID, PayrollSetupID, EffectiveFromDate, CreatedByUserID)
        VALUES (:cid, :bid, :sid, :start, :uid)
    """), {"cid": db.company_id, "bid": db.branch_id,
           "sid": setup_id, "start": anchor, "uid": db.user_id})
    draft_id = await create_draft(
        db.company_id, db.user_id, setup_id, db.db,
        payroll_frequency="Week", anchor_start_date=anchor,
        normal_days_off_mask=0,
    )
    with pytest.raises(PolicyError) as error:
        await publish_version(db.company_id, db.user_id, setup_id,
                              draft_id, date(2090, 1, 8), db.db)
    assert error.value.code == "VERSION_COVERAGE_GAP"


@pytest.mark.asyncio
async def test_default_can_be_cleared_and_archived_setup_cannot_be_selected(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db)
    await set_default_setup(db.company_id, db.user_id, setup_id, db.db)
    await set_default_setup(db.company_id, db.user_id, None, db.db)
    await archive_setup(db.company_id, db.user_id, setup_id, db.db)
    with pytest.raises(PolicyError) as error:
        await set_default_setup(db.company_id, db.user_id, setup_id, db.db)
    assert error.value.code == "SETUP_NOT_ACTIVE"
    current = (await db.db.execute(text("""
        SELECT DefaultPayrollSetupID FROM core.Companies WHERE CompanyID = :cid
    """), {"cid": db.company_id})).scalar_one_or_none()
    assert current is None


@pytest.mark.asyncio
async def test_initial_assignment_can_fill_coverage_before_future_assignment(payroll_setup_db):
    db = payroll_setup_db
    first = await _new_setup(db, suffix="A")
    second = await _new_setup(db, suffix="B")
    anchor = date(2090, 1, 1)
    await _publish(db, first, frequency="Week", anchor=anchor)
    await _publish(db, second, frequency="Week", anchor=anchor)
    await db.db.execute(text("""
        INSERT INTO payroll.BranchPayrollSetupAssignments
            (CompanyID, BranchID, PayrollSetupID, EffectiveFromDate,
             CreatedByUserID)
        VALUES (:cid, :bid, :sid, '2090-01-15', :uid)
    """), {"cid": db.company_id, "bid": db.branch_id,
           "sid": second, "uid": db.user_id})
    assignment_id = await assign_setup(
        db.company_id, db.user_id, db.branch_id, first, anchor, db.db,
    )
    first_authority = await resolve_payroll_setup_version(
        db.company_id, db.branch_id, anchor, db.db,
    )
    second_authority = await resolve_payroll_setup_version(
        db.company_id, db.branch_id, date(2090, 1, 15), db.db,
    )
    assert first_authority.assignment_id == assignment_id
    assert first_authority.next_boundary_date == date(2090, 1, 15)
    assert second_authority.setup_id == second
