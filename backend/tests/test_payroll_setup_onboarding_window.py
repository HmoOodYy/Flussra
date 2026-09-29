"""Focused tests for the onboarding-window guardrail: pure chronology helpers
(next/previous period start, version-timeline walking), the company-local
clock, the onboarding lookback floor, and their wiring into assignment
validation (`_check_assignment`), the read-only evaluator/preview, and the
two product entry points that enforce the guardrail (POST assignments,
POST branches with first_payroll_start_date)."""

from __future__ import annotations

import inspect
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.payroll_setup import clock
from app.payroll_setup.chronology import (
    Schedule,
    ScheduleSegment,
    is_period_start,
    is_timeline_start,
    next_period_start,
    previous_period_start,
    timeline_next_start,
    timeline_previous_start,
    timeline_schedule_at,
)
from app.payroll_setup.errors import PolicyError
from app.payroll_setup.onboarding import onboarding_window_floor
from app.payroll_setup.payroll_policy import (
    archive_setup,
    assign_setup,
    create_draft,
    create_setup,
    evaluate_assignment,
    preview_assignment_impact,
    publish_version,
)
from app.payroll_setup.validation import terminal_segments

# ---------------------------------------------------------------------------
# (a) Pure next_period_start / previous_period_start vs. brute-force is_period_start
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("frequency", "interval", "anchor"), [
    ("Week", None, date(2090, 1, 1)),
    ("Biweek", None, date(2090, 1, 1)),
    ("Custom", 10, date(2090, 1, 1)),
    ("Month", None, date(2090, 1, 31)),
    ("Month", None, date(2090, 1, 15)),
])
def test_next_and_previous_period_start_match_brute_force_enumeration(frequency, interval, anchor):
    schedule = Schedule(frequency, anchor, interval, 0)
    probe_start = anchor - timedelta(days=30)
    probe_days = 400
    buffer_days = 40  # covers the longest possible cycle (Month) beyond the probed range
    all_days = [probe_start + timedelta(days=i) for i in range(probe_days + buffer_days)]
    starts = [d for d in all_days if is_period_start(schedule, d)]
    for i in range(probe_days):
        d = probe_start + timedelta(days=i)
        expected_next = next((x for x in starts if x > d), None)
        assert next_period_start(schedule, d) == expected_next
        expected_previous = max((x for x in starts if x < d), default=None)
        assert previous_period_start(schedule, d) == expected_previous


# ---------------------------------------------------------------------------
# (b) Timeline helpers across a version boundary
# ---------------------------------------------------------------------------


def test_timeline_helpers_walk_across_a_version_boundary():
    week = ScheduleSegment(date(2090, 1, 1), Schedule("Week", date(2090, 1, 1), None, 0))
    biweek = ScheduleSegment(date(2090, 2, 5), Schedule("Biweek", date(2090, 2, 5), None, 0))
    segments = [week, biweek]
    # 2090-02-05 is on the Week grid (35 days after the Week anchor, 35 % 7 == 0).
    assert (biweek.effective_from - week.effective_from).days % 7 == 0

    assert timeline_next_start(segments, date(2090, 1, 20)) == date(2090, 1, 22)
    assert timeline_next_start(segments, date(2090, 2, 1)) == date(2090, 2, 5)
    assert timeline_next_start(segments, date(2089, 12, 1)) == date(2090, 1, 1)
    assert timeline_next_start([], date(2090, 1, 1)) is None

    assert timeline_previous_start(segments, date(2090, 2, 10)) == date(2090, 2, 5)
    assert timeline_previous_start(segments, date(2090, 2, 5)) == date(2090, 1, 29)
    assert timeline_previous_start(segments, date(2090, 1, 1)) is None
    assert timeline_previous_start([], date(2090, 1, 1)) is None

    assert is_timeline_start(segments, date(2090, 2, 5)) is True
    assert is_timeline_start(segments, date(2090, 2, 6)) is False
    assert timeline_schedule_at(segments, date(2090, 1, 15)).frequency == "Week"
    assert timeline_schedule_at(segments, date(2090, 2, 10)).frequency == "Biweek"
    assert timeline_schedule_at([], date(2090, 1, 1)) is None


# ---------------------------------------------------------------------------
# (c) onboarding_window_floor across schedule types
# ---------------------------------------------------------------------------


def test_onboarding_window_floor_across_schedule_types():
    week = ScheduleSegment(date(2090, 1, 1), Schedule("Week", date(2090, 1, 1), None, 0))
    assert onboarding_window_floor([week], date(2090, 3, 10)) == date(2090, 2, 19)  # mid-period
    assert onboarding_window_floor([week], date(2090, 3, 12)) == date(2090, 2, 26)  # exact start

    future_only = ScheduleSegment(date(2090, 6, 1), Schedule("Week", date(2090, 6, 1), None, 0))
    assert onboarding_window_floor([future_only], date(2090, 1, 1)) is None

    biweek = ScheduleSegment(date(2090, 2, 5), Schedule("Biweek", date(2090, 2, 5), None, 0))
    assert onboarding_window_floor([week, biweek], date(2090, 2, 10)) == date(2090, 1, 22)

    month = ScheduleSegment(date(2090, 1, 1), Schedule("Month", date(2090, 1, 1), None, 0))
    assert onboarding_window_floor([month], date(2090, 3, 15)) == date(2090, 1, 1)

    custom = ScheduleSegment(date(2090, 1, 1), Schedule("Custom", date(2090, 1, 1), 10, 0))
    assert onboarding_window_floor([custom], date(2090, 3, 11)) == date(2090, 2, 10)


# ---------------------------------------------------------------------------
# DB-backed fixture (copied from tests/test_payroll_setup_phase2_domain.py):
# a rollback-only transaction against the shared DEMO tenant.
# ---------------------------------------------------------------------------


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
                    "code": f"OW_{marker}",
                    "name": f"Onboarding window test branch {marker}",
                })).scalar_one()
                yield SimpleNamespace(
                    db=conn, company_id=company_id, user_id=user_id,
                    branch_id=int(branch_id), marker=marker,
                )
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


async def _new_branch(db, suffix: str) -> int:
    result = await db.db.execute(text("""
        INSERT INTO core.Branches (CompanyID, BranchCode, BranchName, Status, IsDefault)
        VALUES (:cid, :code, :name, 'Active', FALSE)
        RETURNING BranchID
    """), {"cid": db.company_id, "code": f"OW{suffix}_{db.marker}",
           "name": f"Onboarding window {suffix}"})
    return result.scalar_one()


async def _new_week_setup(db, code_suffix: str) -> int:
    setup_id = await create_setup(
        db.company_id, db.user_id, f"OW{code_suffix}_{db.marker}",
        f"Onboarding window {code_suffix}", db.db,
    )
    draft_id = await create_draft(
        db.company_id, db.user_id, setup_id, db.db,
        payroll_frequency="Week", anchor_start_date=date(2090, 1, 1),
        normal_days_off_mask=0,
    )
    await publish_version(db.company_id, db.user_id, setup_id, draft_id, date(2090, 1, 1), db.db)
    return setup_id


# ---------------------------------------------------------------------------
# (d) terminal_segments excludes replaced versions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_segments_excludes_replaced_versions(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await create_setup(
        db.company_id, db.user_id, f"OWD_{db.marker}", "Terminal segments", db.db,
    )
    draft1 = await create_draft(
        db.company_id, db.user_id, setup_id, db.db,
        payroll_frequency="Week", anchor_start_date=date(2090, 1, 1), normal_days_off_mask=0,
    )
    version1 = await publish_version(
        db.company_id, db.user_id, setup_id, draft1, date(2090, 1, 1), db.db,
    )
    draft2 = await create_draft(
        db.company_id, db.user_id, setup_id, db.db,
        payroll_frequency="Biweek", anchor_start_date=date(2090, 1, 1), normal_days_off_mask=0,
    )
    await publish_version(
        db.company_id, db.user_id, setup_id, draft2, date(2090, 1, 1), db.db,
        replaces_version_id=version1,
    )
    segments = await terminal_segments(db.db, setup_id)
    assert len(segments) == 1
    assert segments[0].effective_from == date(2090, 1, 1)
    assert segments[0].schedule.frequency == "Biweek"


# ---------------------------------------------------------------------------
# (e) assign_setup: the onboarding window is a canonical rule, never an opt-in
# ---------------------------------------------------------------------------


def test_assign_setup_and_evaluate_assignment_have_no_bypass_keyword():
    """The onboarding look-back is a business rule of first assignment, not a
    caller-controlled option: neither entry point exposes a way to skip it."""
    assert "enforce_onboarding_window" not in inspect.signature(assign_setup).parameters
    assert "enforce_onboarding_window" not in inspect.signature(evaluate_assignment).parameters


@pytest.mark.asyncio
async def test_assign_setup_always_enforces_onboarding_window_for_a_first_assignment(
    payroll_setup_db, monkeypatch,
):
    db = payroll_setup_db
    today = date(2090, 3, 12)  # an exact Week-grid period start

    async def _stub_today(company_id, conn):
        return today

    monkeypatch.setattr(clock, "company_today", _stub_today)
    setup_id = await _new_week_setup(db, "E")

    # Current period start: accepted.
    current_branch = await _new_branch(db, "CUR")
    await assign_setup(db.company_id, db.user_id, current_branch, setup_id, today, db.db)

    # Two periods back (== floor): accepted.
    two_back_branch = await _new_branch(db, "TWO")
    await assign_setup(
        db.company_id, db.user_id, two_back_branch, setup_id,
        today - timedelta(days=14), db.db,
    )

    # Three periods back: rejected.
    three_back_branch = await _new_branch(db, "THR")
    with pytest.raises(PolicyError) as exc:
        await assign_setup(
            db.company_id, db.user_id, three_back_branch, setup_id,
            today - timedelta(days=21), db.db,
        )
    assert exc.value.code == "ONBOARDING_START_TOO_EARLY"

    # A future start: accepted.
    future_branch = await _new_branch(db, "FUT")
    await assign_setup(
        db.company_id, db.user_id, future_branch, setup_id,
        today + timedelta(days=7), db.db,
    )

    # A second, independent fresh Branch three periods back: rejected too — there is
    # no keyword or code path left that can opt a first assignment out of the window.
    no_bypass_branch = await _new_branch(db, "NOBP")
    with pytest.raises(PolicyError) as exc:
        await assign_setup(
            db.company_id, db.user_id, no_bypass_branch, setup_id,
            today - timedelta(days=21), db.db,
        )
    assert exc.value.code == "ONBOARDING_START_TOO_EARLY"

    # Non-empty timeline: the window is not applied, even to a too-early start,
    # because it only ever guards a Branch's *first* assignment.
    timeline_branch = await _new_branch(db, "TL")
    await assign_setup(
        db.company_id, db.user_id, timeline_branch, setup_id,
        today + timedelta(days=7), db.db,
    )
    conflicts = await evaluate_assignment(
        db.company_id, timeline_branch, setup_id, today - timedelta(days=21), db.db,
    )
    assert conflicts == []


# ---------------------------------------------------------------------------
# (g) evaluate_assignment conflict codes and preview_assignment_impact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_assignment_conflict_codes_and_preview_impact(
    payroll_setup_db, monkeypatch,
):
    db = payroll_setup_db
    today = date(2090, 3, 12)

    async def _stub_today(company_id, conn):
        return today

    monkeypatch.setattr(clock, "company_today", _stub_today)
    setup_id = await _new_week_setup(db, "G")

    # branch_id=None, on-grid, within window: allowed.
    assert await evaluate_assignment(db.company_id, None, setup_id, today, db.db) == []

    # Off-grid date (still within window): SUCCESSOR_BOUNDARY_INVALID.
    off_grid = await evaluate_assignment(
        db.company_id, None, setup_id, today + timedelta(days=2), db.db,
    )
    assert len(off_grid) == 1
    assert off_grid[0]["code"] == "SUCCESSOR_BOUNDARY_INVALID"
    assert off_grid[0]["branch_id"] is None

    # Before the first Version: VERSION_NOT_FOUND.
    before_first = await evaluate_assignment(
        db.company_id, None, setup_id, date(2089, 12, 25), db.db,
    )
    assert len(before_first) == 1
    assert before_first[0]["code"] == "VERSION_NOT_FOUND"

    # Three periods back: ONBOARDING_START_TOO_EARLY.
    too_early = await evaluate_assignment(
        db.company_id, None, setup_id, today - timedelta(days=21), db.db,
    )
    assert len(too_early) == 1
    assert too_early[0]["code"] == "ONBOARDING_START_TOO_EARLY"

    # Archived Setup: SETUP_NOT_ACTIVE.
    archived_id = await _new_week_setup(db, "GA")
    await archive_setup(db.company_id, db.user_id, archived_id, db.db)
    archived = await evaluate_assignment(db.company_id, None, archived_id, date(2090, 1, 1), db.db)
    assert len(archived) == 1
    assert archived[0]["code"] == "SETUP_NOT_ACTIVE"

    # preview_assignment_impact mirrors allowed/conflicts for a real Branch.
    allowed = await preview_assignment_impact(
        db.company_id, db.user_id, db.branch_id, setup_id, today, db.db,
    )
    assert allowed["allowed"] is True
    assert allowed["conflicts"] == []

    blocked = await preview_assignment_impact(
        db.company_id, db.user_id, db.branch_id, setup_id,
        today - timedelta(days=21), db.db,
    )
    assert blocked["allowed"] is False
    assert blocked["conflicts"][0]["code"] == "ONBOARDING_START_TOO_EARLY"


# ---------------------------------------------------------------------------
# (h) company_today
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_company_today_returns_date_for_demo_and_raises_for_missing_company(
    payroll_setup_db,
):
    db = payroll_setup_db
    today = await clock.company_today(db.company_id, db.db)
    assert isinstance(today, date)
    with pytest.raises(PolicyError) as exc:
        await clock.company_today(999_999_999, db.db)
    assert exc.value.code == "COMPANY_NOT_FOUND"


# ---------------------------------------------------------------------------
# (f) API: the guardrail wired into the two product entry points
# ---------------------------------------------------------------------------


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_published_week_setup(client, token: str, marker: str) -> int:
    created = await client.post("/payroll-setup/setups", headers=_auth(token), json={
        "setup_code": "OWF_" + marker[:10], "setup_name": "Onboarding window API",
    })
    assert created.status_code == 201, created.text
    setup_id = created.json()["setup_id"]
    draft = await client.post(
        f"/payroll-setup/setups/{setup_id}/drafts", headers=_auth(token), json={
            "payroll_frequency": "Week", "anchor_start_date": "2090-01-01",
            "normal_days_off_mask": 0,
        },
    )
    assert draft.status_code == 201, draft.text
    published = await client.post(
        f"/payroll-setup/setups/{setup_id}/drafts/{draft.json()['version_id']}/publish",
        headers=_auth(token), json={"effective_from_date": "2090-01-01"},
    )
    assert published.status_code == 201, published.text
    return setup_id


@pytest.mark.asyncio
async def test_api_assignment_rejects_and_accepts_relative_to_onboarding_window(
    client, auth_token, db_conn, monkeypatch,
):
    marker = uuid4().hex
    today = date(2090, 3, 12)

    async def _stub_today(company_id, conn):
        return today

    monkeypatch.setattr(clock, "company_today", _stub_today)
    setup_id = await _create_published_week_setup(client, auth_token, marker)

    branch = await client.post("/settings/branches", headers=_auth(auth_token), json={
        "branch_name": "Onboarding window API " + marker[:8],
        "branch_code": "OWFA" + marker[:6],
    })
    assert branch.status_code == 201, branch.text
    branch_id = branch.json()["branch_id"]

    too_early = await client.post(
        f"/payroll-setup/branches/{branch_id}/assignments", headers=_auth(auth_token), json={
            "setup_id": setup_id,
            "effective_from_date": (today - timedelta(days=21)).isoformat(),
        },
    )
    assert too_early.status_code == 409, too_early.text
    assert too_early.json()["detail"]["code"] == "ONBOARDING_START_TOO_EARLY"

    current = await client.post(
        f"/payroll-setup/branches/{branch_id}/assignments", headers=_auth(auth_token), json={
            "setup_id": setup_id,
            "effective_from_date": today.isoformat(),
        },
    )
    assert current.status_code == 201, current.text


@pytest.mark.asyncio
async def test_api_branch_create_rejects_first_payroll_start_before_onboarding_window(
    client, auth_token, db_conn, monkeypatch,
):
    marker = uuid4().hex
    today = date(2090, 3, 12)

    async def _stub_today(company_id, conn):
        return today

    monkeypatch.setattr(clock, "company_today", _stub_today)
    company_id = (await db_conn.execute(text(
        "SELECT CompanyID FROM core.Companies WHERE CompanyCode = 'DEMO'"
    ))).scalar_one()
    old_default = (await db_conn.execute(text(
        "SELECT DefaultPayrollSetupID FROM core.Companies WHERE CompanyID = :cid"
    ), {"cid": company_id})).scalar_one_or_none()
    setup_id = await _create_published_week_setup(client, auth_token, marker)
    set_default = await client.put("/payroll-setup/default", headers=_auth(auth_token),
                                   json={"setup_id": setup_id})
    assert set_default.status_code == 204, set_default.text
    try:
        branch_code = "OWFB" + marker[:6]
        too_early = await client.post("/settings/branches", headers=_auth(auth_token), json={
            "branch_name": "Onboarding branch-create API " + marker[:8],
            "branch_code": branch_code,
            "first_payroll_start_date": (today - timedelta(days=21)).isoformat(),
        })
        assert too_early.status_code == 409, too_early.text
        assert too_early.json()["detail"]["code"] == "ONBOARDING_START_TOO_EARLY"
        assert (await db_conn.execute(text("""
            SELECT COUNT(*) FROM core.Branches WHERE CompanyID = :cid AND BranchCode = :code
        """), {"cid": company_id, "code": branch_code})).scalar_one() == 0
    finally:
        restored = await client.put("/payroll-setup/default", headers=_auth(auth_token),
                                    json={"setup_id": old_default})
        assert restored.status_code == 204, restored.text
