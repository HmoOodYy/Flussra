"""Phase 3 period creation against persisted Payroll Setup authority."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.payroll.period_creation import (
    _decode_candidate_key,
    create_period_from_candidate,
    get_period_candidates,
)
from app.payroll.period_lifecycle import change_period_status
from app.payroll.schemas import PeriodCreationRequest, PeriodStatusChange
from app.payroll_setup.payroll_policy import (
    assign_setup,
    create_draft,
    create_setup,
    publish_version,
    reassign_setup,
    set_default_setup,
)
from app.payroll_setup.resolver import resolve_payroll_setup_version
from tests.builders.company import create_branch

pytestmark = pytest.mark.pre_cutover_legacy


@pytest_asyncio.fixture
async def period_creation_db(test_database_url):
    """Use a fresh Branch and rollback every policy and payroll write."""
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
                branch_id = await create_branch(
                    conn, company_id, user_id,
                    branch_code=f"P3_{marker}",
                    branch_name=f"Phase 3 creation {marker}",
                )
                yield SimpleNamespace(
                    db=conn, company_id=company_id, user_id=user_id,
                    branch_id=int(branch_id), marker=marker,
                )
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


async def _setup(db, suffix: str, *, frequency: str = "Week",
                 anchor: date = date(2090, 1, 1), mask: int = 5) -> tuple[int, int]:
    setup_id = await create_setup(
        db.company_id, db.user_id, f"P3_{db.marker}_{suffix}",
        f"Phase 3 {suffix}", db.db,
    )
    draft_id = await create_draft(
        db.company_id, db.user_id, setup_id, db.db,
        payroll_frequency=frequency, anchor_start_date=anchor,
        normal_days_off_mask=mask,
    )
    version_id = await publish_version(
        db.company_id, db.user_id, setup_id, draft_id, anchor, db.db,
    )
    return setup_id, version_id


async def _assign(db, setup_id: int, start: date = date(2090, 1, 1)) -> int:
    return await assign_setup(
        db.company_id, db.user_id, db.branch_id, setup_id, start, db.db,
    )


async def _preview(db):
    return await get_period_candidates(
        db.company_id, db.user_id, db.branch_id, "OPEN_CREATION", None, db.db,
    )


async def _confirm(db, candidate_key: str):
    return await create_period_from_candidate(
        db.company_id, db.user_id, db.branch_id,
        PeriodCreationRequest(candidate_key=candidate_key), db.db,
    )


@pytest.mark.asyncio
async def test_preview_confirm_binds_exact_authority_and_replay_is_idempotent(
    period_creation_db,
):
    db = period_creation_db
    setup_id, version_id = await _setup(db, "BASE")
    assignment_id = await _assign(db, setup_id)

    preview = await _preview(db)
    selected = preview.selected
    assert (selected.start_date, selected.end_date) == (
        date(2090, 1, 1), date(2090, 1, 7),
    )
    assert selected.creatable is True
    assert "pay_date" not in preview.model_dump()

    created = await _confirm(db, selected.candidate_key)
    assert created.result == "CREATED"
    assert (created.start_date, created.end_date, created.status) == (
        selected.start_date, selected.end_date, "Open",
    )

    period = (await db.db.execute(text("""
        SELECT BranchPayrollSetupAssignmentID, PayrollSetupVersionID,
               FrozenPayrollSetupID, FrozenPayrollSetupCode,
               FrozenPayrollSetupVersionNumber, FrozenPayrollFrequency,
               FrozenAnchorStartDate, FrozenCustomIntervalDays,
               FrozenNormalDaysOffMask, ScheduleConfigHash
        FROM payroll.PayrollPeriods
        WHERE CompanyID = :cid AND PayrollPeriodID = :pid
    """), {"cid": db.company_id, "pid": created.payroll_period_id})).mappings().one()
    version = (await db.db.execute(text("""
        SELECT VersionNumber, ConfigHash FROM payroll.PayrollSetupVersions
        WHERE PayrollSetupVersionID = :vid AND CompanyID = :cid
    """), {"vid": version_id, "cid": db.company_id})).mappings().one()
    assert period["branchpayrollsetupassignmentid"] == assignment_id
    assert period["payrollsetupversionid"] == version_id
    assert period["frozenpayrollsetupid"] == setup_id
    assert period["frozenpayrollsetupcode"] == f"P3_{db.marker}_BASE"
    assert period["frozenpayrollsetupversionnumber"] == version["versionnumber"]
    assert period["frozenpayrollfrequency"] == "Week"
    assert period["frozenanchorstartdate"] == date(2090, 1, 1)
    assert period["frozencustomintervaldays"] is None
    assert period["frozennormaldaysoffmask"] == 5
    assert period["scheduleconfighash"] == version["confighash"]

    days = (await db.db.execute(text("""
        SELECT BranchPayrollSetupAssignmentID, PayrollSetupVersionID
        FROM payroll.PayrollPeriodDays
        WHERE CompanyID = :cid AND PayrollPeriodID = :pid
        ORDER BY WorkDate
    """), {"cid": db.company_id, "pid": created.payroll_period_id})).all()
    assert len(days) == 7
    assert all(row == (assignment_id, version_id) for row in days)

    audit = (await db.db.execute(text("""
        SELECT EventType, PayrollSetupID, PayrollSetupVersionID,
               BranchPayrollSetupAssignmentID, PayrollPeriodID, BranchID,
               EffectiveDate, NewConfigHash, NewStateJSON
        FROM payroll.PayrollSetupPolicyAuditEvents
        WHERE CompanyID = :cid AND EventType = 'PeriodCreated'
          AND PayrollPeriodID = :pid
    """), {"cid": db.company_id, "pid": created.payroll_period_id})).mappings().one()
    assert audit["eventtype"] == "PeriodCreated"
    assert audit["payrollsetupid"] == setup_id
    assert audit["payrollsetupversionid"] == version_id
    assert audit["branchpayrollsetupassignmentid"] == assignment_id
    assert audit["branchid"] == db.branch_id
    assert audit["effectivedate"] == selected.start_date
    assert audit["newconfighash"] == version["confighash"]
    assert audit["newstatejson"]["period_id"] == created.payroll_period_id

    replay = await _confirm(db, selected.candidate_key)
    assert replay.result == "ALREADY_EXISTS"
    assert replay.payroll_period_id == created.payroll_period_id
    count = (await db.db.execute(text("""
        SELECT COUNT(*) FROM payroll.PayrollSetupPolicyAuditEvents
        WHERE CompanyID = :cid AND EventType = 'PeriodCreated'
          AND PayrollPeriodID = :pid
    """), {"cid": db.company_id, "pid": created.payroll_period_id})).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_cancelled_candidate_cannot_be_replayed(period_creation_db):
    db = period_creation_db
    setup_id, _ = await _setup(db, "CANCEL")
    await _assign(db, setup_id)
    preview = await _preview(db)
    created = await _confirm(db, preview.selected.candidate_key)

    await change_period_status(
        db.company_id, db.user_id, created.payroll_period_id,
        PeriodStatusChange(status="Cancelled"), db.db,
    )
    with pytest.raises(HTTPException) as error:
        await _confirm(db, preview.selected.candidate_key)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "CANDIDATE_ALREADY_CANCELLED"


@pytest.mark.asyncio
async def test_future_version_timeline_change_stales_preview(period_creation_db):
    db = period_creation_db
    setup_id, current_version_id = await _setup(db, "VERSION")
    await _assign(db, setup_id)
    preview = await _preview(db)
    before = await resolve_payroll_setup_version(
        db.company_id, db.branch_id, preview.selected.start_date, db.db,
    )

    future_draft = await create_draft(
        db.company_id, db.user_id, setup_id, db.db,
        payroll_frequency="Week", anchor_start_date=date(2090, 1, 1),
        normal_days_off_mask=5,
    )
    future_version_id = await publish_version(
        db.company_id, db.user_id, setup_id, future_draft, date(2090, 1, 8), db.db,
    )
    after = await resolve_payroll_setup_version(
        db.company_id, db.branch_id, preview.selected.start_date, db.db,
    )
    assert (before.assignment_id, before.version_id, before.config_hash) == (
        after.assignment_id, after.version_id, after.config_hash,
    )
    assert before.version_id == current_version_id
    assert future_version_id != current_version_id

    with pytest.raises(HTTPException) as error:
        await _confirm(db, preview.selected.candidate_key)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "CANDIDATE_STALE"


@pytest.mark.asyncio
async def test_preview_stales_on_timeline_fingerprint_change_with_authority_unchanged(
    period_creation_db,
):
    """A timeline-only mutation must stale an otherwise unchanged candidate."""
    db = period_creation_db
    setup_id, current_version_id = await _setup(db, "FINGERPRINT")
    await _assign(db, setup_id)

    preview = await _preview(db)
    payload_before, _ = _decode_candidate_key(preview.selected.candidate_key)
    assert payload_before["setup_version_id"] == current_version_id

    # Pure timeline mutation: a future version, effective after this
    # candidate's period, cannot change which authority resolves for the
    # already-previewed start date.
    future_draft = await create_draft(
        db.company_id, db.user_id, setup_id, db.db,
        payroll_frequency="Week", anchor_start_date=date(2090, 1, 1),
        normal_days_off_mask=5,
    )
    await publish_version(
        db.company_id, db.user_id, setup_id, future_draft, date(2090, 1, 8), db.db,
    )

    after_preview = await _preview(db)
    payload_after, _ = _decode_candidate_key(after_preview.selected.candidate_key)

    # Resolved authority, candidate dates, and slot state are byte-identical...
    assert payload_after["assignment_id"] == payload_before["assignment_id"]
    assert payload_after["setup_id"] == payload_before["setup_id"]
    assert payload_after["setup_version_id"] == payload_before["setup_version_id"]
    assert payload_after["config_hash"] == payload_before["config_hash"]
    assert payload_after["start"] == payload_before["start"]
    assert payload_after["end"] == payload_before["end"]
    assert payload_after["slot_fp"] == payload_before["slot_fp"]

    # ...yet the signed setup timeline fingerprint itself has moved.
    assert payload_after["setup_timeline"] != payload_before["setup_timeline"]
    assert payload_after["branch_timeline"] == payload_before["branch_timeline"]

    with pytest.raises(HTTPException) as error:
        await _confirm(db, preview.selected.candidate_key)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "CANDIDATE_STALE"


@pytest.mark.asyncio
async def test_future_assignment_timeline_change_stales_preview(period_creation_db):
    db = period_creation_db
    first_setup, first_version = await _setup(db, "ASSIGN_A")
    second_setup, _ = await _setup(
        db, "ASSIGN_B", anchor=date(2090, 1, 8),
    )
    await _assign(db, first_setup)
    preview = await _preview(db)
    before = await resolve_payroll_setup_version(
        db.company_id, db.branch_id, preview.selected.start_date, db.db,
    )

    await reassign_setup(
        db.company_id, db.user_id, db.branch_id, second_setup,
        date(2090, 1, 8), db.db,
    )
    after = await resolve_payroll_setup_version(
        db.company_id, db.branch_id, preview.selected.start_date, db.db,
    )
    assert (before.assignment_id, before.version_id, before.config_hash) == (
        after.assignment_id, after.version_id, after.config_hash,
    )
    assert before.version_id == first_version

    with pytest.raises(HTTPException) as error:
        await _confirm(db, preview.selected.candidate_key)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "CANDIDATE_STALE"


@pytest.mark.asyncio
async def test_default_only_change_does_not_stale_branch_candidate(period_creation_db):
    db = period_creation_db
    assigned_setup, version_id = await _setup(db, "DEFAULT_A")
    other_setup, _ = await _setup(db, "DEFAULT_B")
    assignment_id = await _assign(db, assigned_setup)
    preview = await _preview(db)

    await set_default_setup(db.company_id, db.user_id, other_setup, db.db)
    created = await _confirm(db, preview.selected.candidate_key)
    assert created.result == "CREATED"
    period = (await db.db.execute(text("""
        SELECT BranchPayrollSetupAssignmentID, PayrollSetupVersionID, FrozenPayrollSetupID
        FROM payroll.PayrollPeriods
        WHERE CompanyID = :cid AND PayrollPeriodID = :pid
    """), {"cid": db.company_id, "pid": created.payroll_period_id})).one()
    assert period == (assignment_id, version_id, assigned_setup)


@pytest.mark.asyncio
async def test_company_default_does_not_authorize_unassigned_branch_candidate(
    period_creation_db,
):
    db = period_creation_db
    setup_id, _ = await _setup(db, "DEFAULT_ONLY")
    await set_default_setup(db.company_id, db.user_id, setup_id, db.db)

    with pytest.raises(HTTPException) as error:
        await _preview(db)

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "PAYROLL_SETUP_REQUIRED"


@pytest.mark.asyncio
async def test_candidate_preview_rejects_period_crossing_invalid_authority_boundary(
    period_creation_db,
):
    db = period_creation_db
    setup_id, _ = await _setup(db, "BOUNDARY")
    await _assign(db, setup_id)

    # Simulate an out-of-band invalid timeline; policy publication rejects this boundary.
    await db.db.execute(text("""
        INSERT INTO payroll.PayrollSetupVersions
            (CompanyID, PayrollSetupID, LifecycleState, VersionNumber,
             EffectiveFromDate, PayrollFrequency, AnchorStartDate,
             NormalDaysOffMask, ConfigHash, PublishedByUserID, PublishedAtUtc)
        VALUES (:cid, :sid, 'Published', 2, '2090-01-04', 'Week',
                '2090-01-04', 5, :hash, :uid, NOW())
    """), {
        "cid": db.company_id, "sid": setup_id,
        "hash": "a" * 64, "uid": db.user_id,
    })

    with pytest.raises(HTTPException) as error:
        await _preview(db)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "AUTHORITY_BOUNDARY_CROSSING"
@pytest.mark.asyncio
async def test_candidate_preview_rejects_period_crossing_invalid_assignment_boundary(
    period_creation_db,
):
    db = period_creation_db
    first_setup, _ = await _setup(db, "ASSIGN_BOUNDARY_A")
    second_setup, _ = await _setup(
        db, "ASSIGN_BOUNDARY_B", anchor=date(2090, 1, 4),
    )
    first_assignment_id = await _assign(db, first_setup)

    # Exercise resolver fail-closed behavior for a persisted mid-period boundary.
    await db.db.execute(text("""
        UPDATE payroll.BranchPayrollSetupAssignments
        SET EffectiveToDate = '2090-01-04'
        WHERE BranchPayrollSetupAssignmentID = :aid
    """), {"aid": first_assignment_id})
    await db.db.execute(text("""
        INSERT INTO payroll.BranchPayrollSetupAssignments
            (CompanyID, BranchID, PayrollSetupID, EffectiveFromDate, CreatedByUserID)
        VALUES (:cid, :bid, :sid, '2090-01-04', :uid)
    """), {
        "cid": db.company_id, "bid": db.branch_id,
        "sid": second_setup, "uid": db.user_id,
    })

    with pytest.raises(HTTPException) as error:
        await _preview(db)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "AUTHORITY_BOUNDARY_CROSSING"
