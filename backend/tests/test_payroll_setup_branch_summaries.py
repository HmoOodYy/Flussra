"""Focused tests for the `GET /payroll-setup/branch-summaries` read model
(`reads.list_branch_policy_summaries`): one compact row per Branch showing
the current and next-scheduled non-withdrawn assignment plus readiness.

`today` (clock.company_today) is stubbed for the direct-call tests, matching
the pattern in tests/test_payroll_setup_boundary_choices.py; it decides only
which non-withdrawn assignment is "current" vs "scheduled_change" here, never
payroll authority itself."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth.security import create_access_token
from app.payroll_setup import clock, reads
from app.payroll_setup.payroll_policy import (
    assign_setup,
    create_draft,
    create_setup,
    publish_version,
    reassign_setup,
    withdraw_assignment,
)
from tests.builders.company import create_branch

# ---------------------------------------------------------------------------
# Rollback-per-test DB fixture against the shared DEMO tenant (copied from
# tests/test_payroll_setup_boundary_choices.py).
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
                branch_id = await create_branch(
                    conn, company_id, user_id,
                    branch_code=f"BS_{marker}",
                    branch_name=f"Branch summaries test branch {marker}",
                )
                yield SimpleNamespace(
                    db=conn, company_id=company_id, user_id=user_id,
                    branch_id=int(branch_id), marker=marker,
                )
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


async def _published_setup(
    db, suffix: str, *, frequency: str, anchor: date,
    interval: int | None = None, mask: int = 0,
) -> int:
    setup_id = await create_setup(
        db.company_id, db.user_id, f"BS{suffix}_{db.marker}",
        f"Branch summaries {suffix}", db.db,
    )
    draft_id = await create_draft(
        db.company_id, db.user_id, setup_id, db.db,
        payroll_frequency=frequency, anchor_start_date=anchor,
        custom_interval_days=interval, normal_days_off_mask=mask,
    )
    await publish_version(db.company_id, db.user_id, setup_id, draft_id, anchor, db.db)
    return setup_id


def _stub_today(monkeypatch, today: date) -> None:
    async def _stub(company_id, conn):
        return today

    monkeypatch.setattr(clock, "company_today", _stub)


def _row_for(summaries: list[dict], branch_id: int) -> dict:
    return next(row for row in summaries if row["branch_id"] == branch_id)


# ---------------------------------------------------------------------------
# Core summary rules.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unassigned_branch_reports_no_assignment(payroll_setup_db, monkeypatch):
    db = payroll_setup_db
    _stub_today(monkeypatch, date(2090, 3, 15))

    summaries = await reads.list_branch_policy_summaries(db.company_id, db.db)
    row = _row_for(summaries, db.branch_id)

    assert row["payroll_set_up"] is False
    assert row["current"] is None
    assert row["scheduled_change"] is None
    assert row["readiness_reason"] == "NO_ASSIGNMENT"
    assert row["readiness_date"] is None


@pytest.mark.asyncio
async def test_weekly_assignment_from_before_today_is_current(payroll_setup_db, monkeypatch):
    db = payroll_setup_db
    anchor = date(2090, 1, 1)
    today = date(2090, 3, 15)
    _stub_today(monkeypatch, anchor)
    setup_id = await _published_setup(db, "A", frequency="Week", anchor=anchor)
    await assign_setup(db.company_id, db.user_id, db.branch_id, setup_id, anchor, db.db)
    _stub_today(monkeypatch, today)

    summaries = await reads.list_branch_policy_summaries(db.company_id, db.db)
    row = _row_for(summaries, db.branch_id)

    assert row["payroll_set_up"] is True
    assert row["current"] is not None
    assert row["current"]["setup_id"] == setup_id
    assert row["current"]["payroll_frequency"] == "Week"
    assert row["current"]["effective_from_date"] == anchor
    assert row["current"]["effective_to_date"] is None
    assert row["scheduled_change"] is None
    assert row["reference_date"] == today


@pytest.mark.asyncio
async def test_reassignment_scheduled_after_today_splits_current_and_scheduled(
    payroll_setup_db, monkeypatch,
):
    db = payroll_setup_db
    anchor = date(2090, 1, 1)
    today = date(2090, 3, 15)
    reassign_date = date(2090, 4, 2)  # a Week boundary after `today`
    _stub_today(monkeypatch, anchor)
    source_id = await _published_setup(db, "B1", frequency="Week", anchor=anchor)
    await assign_setup(db.company_id, db.user_id, db.branch_id, source_id, anchor, db.db)
    dest_id = await _published_setup(db, "B2", frequency="Biweek", anchor=reassign_date)
    _stub_today(monkeypatch, today)
    await reassign_setup(
        db.company_id, db.user_id, db.branch_id, dest_id, reassign_date, db.db,
    )

    summaries = await reads.list_branch_policy_summaries(db.company_id, db.db)
    row = _row_for(summaries, db.branch_id)

    assert row["payroll_set_up"] is True
    assert row["current"]["setup_id"] == source_id
    assert row["current"]["payroll_frequency"] == "Week"
    assert row["current"]["effective_to_date"] == reassign_date
    assert row["scheduled_change"]["setup_id"] == dest_id
    assert row["scheduled_change"]["payroll_frequency"] == "Biweek"
    assert row["scheduled_change"]["effective_from_date"] == reassign_date


@pytest.mark.asyncio
async def test_second_future_assignment_appears_in_upcoming_but_not_scheduled_change(
    payroll_setup_db, monkeypatch,
):
    db = payroll_setup_db
    anchor = date(2090, 1, 1)
    today = date(2090, 3, 15)
    q_date = date(2090, 4, 2)  # a Week boundary after `today`
    r_date = date(2090, 4, 16)  # a Biweek boundary after `q_date`
    _stub_today(monkeypatch, anchor)
    p_id = await _published_setup(db, "E1", frequency="Week", anchor=anchor)
    await assign_setup(db.company_id, db.user_id, db.branch_id, p_id, anchor, db.db)
    q_id = await _published_setup(db, "E2", frequency="Biweek", anchor=q_date)
    _stub_today(monkeypatch, today)
    await reassign_setup(db.company_id, db.user_id, db.branch_id, q_id, q_date, db.db)
    r_id = await _published_setup(db, "E3", frequency="Biweek", anchor=r_date)
    await reassign_setup(db.company_id, db.user_id, db.branch_id, r_id, r_date, db.db)

    summaries = await reads.list_branch_policy_summaries(db.company_id, db.db)
    row = _row_for(summaries, db.branch_id)

    assert row["current"]["setup_id"] == p_id
    assert row["scheduled_change"]["setup_id"] == q_id
    assert row["scheduled_change"] == row["upcoming_assignments"][0]
    assert [item["setup_id"] for item in row["upcoming_assignments"]] == [q_id, r_id]
    assert row["upcoming_assignments"][0]["effective_from_date"] == q_date
    assert row["upcoming_assignments"][1]["effective_from_date"] == r_date


@pytest.mark.asyncio
async def test_onboarding_starting_after_today_has_no_current_but_is_scheduled(
    payroll_setup_db, monkeypatch,
):
    db = payroll_setup_db
    today = date(2090, 3, 15)
    onboarding_start = date(2090, 3, 22)  # future first assignment, a Week boundary
    _stub_today(monkeypatch, today)
    setup_id = await _published_setup(db, "C", frequency="Week", anchor=today)
    await assign_setup(
        db.company_id, db.user_id, db.branch_id, setup_id, onboarding_start, db.db,
    )

    summaries = await reads.list_branch_policy_summaries(db.company_id, db.db)
    row = _row_for(summaries, db.branch_id)

    assert row["payroll_set_up"] is True
    assert row["current"] is None
    assert row["scheduled_change"] is not None
    assert row["scheduled_change"]["setup_id"] == setup_id
    assert row["scheduled_change"]["effective_from_date"] == onboarding_start
    assert row["scheduled_change"]["payroll_frequency"] == "Week"


@pytest.mark.asyncio
async def test_withdrawn_future_reassignment_excluded_from_scheduled_change(
    payroll_setup_db, monkeypatch,
):
    db = payroll_setup_db
    anchor = date(2090, 1, 1)
    today = date(2090, 3, 15)
    reassign_date = date(2090, 4, 2)
    _stub_today(monkeypatch, anchor)
    source_id = await _published_setup(db, "D1", frequency="Week", anchor=anchor)
    await assign_setup(db.company_id, db.user_id, db.branch_id, source_id, anchor, db.db)
    dest_id = await _published_setup(db, "D2", frequency="Biweek", anchor=reassign_date)
    _stub_today(monkeypatch, today)
    new_assignment_id = await reassign_setup(
        db.company_id, db.user_id, db.branch_id, dest_id, reassign_date, db.db,
    )
    await withdraw_assignment(db.company_id, db.user_id, new_assignment_id, db.db)

    summaries = await reads.list_branch_policy_summaries(db.company_id, db.db)
    row = _row_for(summaries, db.branch_id)

    assert row["payroll_set_up"] is True
    assert row["scheduled_change"] is None
    assert row["current"] is not None
    assert row["current"]["setup_id"] == source_id
    assert row["current"]["effective_to_date"] is None


@pytest.mark.asyncio
async def test_response_ordered_by_branch_name(payroll_setup_db, monkeypatch):
    db = payroll_setup_db
    _stub_today(monkeypatch, date(2090, 3, 15))
    marker = db.marker
    charlie = await create_branch(
        db.db, db.company_id, db.user_id,
        branch_code=f"BSord1_{marker}", branch_name=f"ZZBS Charlie {marker}",
    )
    alpha = await create_branch(
        db.db, db.company_id, db.user_id,
        branch_code=f"BSord2_{marker}", branch_name=f"ZZBS Alpha {marker}",
    )
    bravo = await create_branch(
        db.db, db.company_id, db.user_id,
        branch_code=f"BSord3_{marker}", branch_name=f"ZZBS Bravo {marker}",
    )

    summaries = await reads.list_branch_policy_summaries(db.company_id, db.db)
    ours = [row for row in summaries if row["branch_id"] in (charlie, alpha, bravo)]
    assert [row["branch_id"] for row in ours] == [alpha, bravo, charlie]
    assert [row["branch_name"] for row in ours] == sorted(row["branch_name"] for row in ours)


# ---------------------------------------------------------------------------
# HTTP: permission gate.
# ---------------------------------------------------------------------------


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _make_actor(db_conn, *, permissions: tuple[str, ...], scope: str) -> str:
    suffix = uuid4().hex[:10]
    company_id = (await db_conn.execute(text(
        "SELECT CompanyID FROM core.Companies WHERE CompanyCode = 'DEMO'"
    ))).scalar_one()
    user_id = (await db_conn.execute(text("""
        INSERT INTO sec.Users (CompanyID, Username, DisplayName, IsActive, CanLogin)
        VALUES (:cid, :name, 'Branch summaries actor', TRUE, TRUE)
        RETURNING UserID
    """), {"cid": company_id, "name": "bsactor_" + suffix})).scalar_one()
    role_id = (await db_conn.execute(text("""
        INSERT INTO sec.Roles (RoleCode, RoleName, RoleLevel, IsSystemRole)
        VALUES (:code, 'Branch summaries actor', 20, FALSE) RETURNING RoleID
    """), {"code": "BSR_" + suffix})).scalar_one()
    company_role_id = (await db_conn.execute(text("""
        INSERT INTO sec.CompanyRoles
            (CompanyID, RoleCode, RoleName, RoleLevel, IsDefault,
             IsProtected, IsCustom, IsActive)
        VALUES (:cid, :code, 'Branch summaries actor', 20, FALSE, FALSE, TRUE, TRUE)
        RETURNING CompanyRoleID
    """), {"cid": company_id, "code": "BSC_" + suffix})).scalar_one()
    for permission in permissions:
        await db_conn.execute(text("""
            INSERT INTO sec.CompanyRolePermissions (CompanyRoleID, PermissionCode)
            VALUES (:rid, :permission)
        """), {"rid": company_role_id, "permission": permission})
    await db_conn.execute(text("""
        INSERT INTO sec.UserBranchRoles
            (UserID, CompanyID, BranchID, RoleID, CompanyRoleID, ScopeType, IsActive)
        VALUES (:uid, :cid, NULL, :rid, :crid, :scope, TRUE)
    """), {"uid": user_id, "cid": company_id, "rid": role_id,
           "crid": company_role_id, "scope": scope})
    return create_access_token(int(user_id), int(company_id))


@pytest.mark.asyncio
async def test_http_branch_summaries_200_and_403(client, auth_token, db_conn):
    ok = await client.get("/payroll-setup/branch-summaries", headers=_auth(auth_token))
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert isinstance(body, list)
    if body:
        row = body[0]
        assert set(row) == {
            "branch_id", "branch_code", "branch_name", "branch_status",
            "reference_date", "payroll_set_up", "current", "scheduled_change",
            "upcoming_assignments", "readiness_reason", "readiness_date",
        }

    no_view = await _make_actor(db_conn, permissions=(), scope="AllCompanyBranches")
    denied = await client.get("/payroll-setup/branch-summaries", headers=_auth(no_view))
    assert denied.status_code == 403, denied.text
