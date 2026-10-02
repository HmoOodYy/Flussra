"""Focused tests for the canonical `boundaries` module: publication, assignment,
reassignment, and onboarding legal-date navigation. Company-local "today" is
stubbed via `app.payroll_setup.clock.company_today` and must never influence
whether a *given* date is valid — only the `relation` label, the onboarding
floor, and the default requested/suggested date when no date is supplied.

Reuses the rollback-per-test DEMO-tenant fixture pattern from
tests/test_payroll_setup_onboarding_window.py for direct policy/boundaries
calls, and the client/auth_token/db_conn fixtures from conftest.py for the
handful of true HTTP-level checks (auth, 404/422 mapping, JSON key "date")."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth.security import create_access_token
from app.payroll_setup import boundaries, clock
from app.payroll_setup.chronology import Schedule, is_period_start
from app.payroll_setup.onboarding import onboarding_window_floor
from app.payroll_setup.payroll_policy import (
    archive_setup,
    assign_setup,
    create_draft,
    create_setup,
    publish_version,
    reassign_setup,
)
from app.payroll_setup.validation import terminal_segments

# ---------------------------------------------------------------------------
# Rollback-per-test DB fixture against the shared DEMO tenant (copied from
# tests/test_payroll_setup_onboarding_window.py).
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
                    "code": f"BC_{marker}",
                    "name": f"Boundary choices test branch {marker}",
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
    """), {"cid": db.company_id, "code": f"BC{suffix}_{db.marker}",
           "name": f"Boundary choices {suffix}"})
    return result.scalar_one()


async def _published_setup(
    db, suffix: str, *, frequency: str, anchor: date,
    interval: int | None = None, mask: int = 0, publish_at: date | None = None,
) -> int:
    setup_id = await create_setup(
        db.company_id, db.user_id, f"BC{suffix}_{db.marker}",
        f"Boundary choices {suffix}", db.db,
    )
    draft_id = await create_draft(
        db.company_id, db.user_id, setup_id, db.db,
        payroll_frequency=frequency, anchor_start_date=anchor,
        custom_interval_days=interval, normal_days_off_mask=mask,
    )
    await publish_version(
        db.company_id, db.user_id, setup_id, draft_id, publish_at or anchor, db.db,
    )
    return setup_id


async def _new_draft(
    db, setup_id: int, *, frequency: str, anchor: date,
    interval: int | None = None, mask: int = 0,
) -> int:
    return await create_draft(
        db.company_id, db.user_id, setup_id, db.db,
        payroll_frequency=frequency, anchor_start_date=anchor,
        custom_interval_days=interval, normal_days_off_mask=mask,
    )


def _stub_today(monkeypatch, today: date) -> None:
    async def _stub(company_id, conn):
        return today

    monkeypatch.setattr(clock, "company_today", _stub)


# ---------------------------------------------------------------------------
# (a) Publication: off-grid navigation, publish-at-next, omitted `around`,
#     and same-date correction (replaces_version_id/number).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publication_choices_navigates_off_grid_date_and_publishes_at_next(
    payroll_setup_db, monkeypatch,
):
    db = payroll_setup_db
    week_anchor = date(2090, 1, 1)
    setup_id = await _published_setup(db, "A", frequency="Week", anchor=week_anchor)
    await assign_setup(db.company_id, db.user_id, db.branch_id, setup_id, week_anchor, db.db)

    biweek_anchor = date(2090, 2, 5)  # 35 days after the Week anchor -> a v1 boundary
    draft_id = await _new_draft(db, setup_id, frequency="Biweek", anchor=biweek_anchor)

    off_grid = date(2090, 2, 10)  # mid-period on both grids
    _stub_today(monkeypatch, off_grid)

    result = await boundaries.publication_choices(
        db.company_id, db.user_id, setup_id, draft_id, off_grid, db.db,
    )
    assert result["requested_valid"] is False
    assert result["requested"] is None
    assert any(c["code"] == "SUCCESSOR_BOUNDARY_INVALID" for c in result["conflicts"])
    assert result["previous"]["date"] == date(2090, 2, 5)
    assert result["next"]["date"] == date(2090, 2, 19)
    assert result["previous"]["date"] >= biweek_anchor
    assert result["next"]["payroll_frequency"] == "Biweek"
    assert result["earliest_allowed_date"] is None
    assert result["suggested"] is None  # around was supplied explicitly

    # Publishing at the reported `next` date actually succeeds.
    await publish_version(
        db.company_id, db.user_id, setup_id, draft_id, result["next"]["date"], db.db,
        replaces_version_id=result["next"]["replaces_version_id"],
    )


@pytest.mark.asyncio
async def test_publication_choices_surfaces_policy_successor_conflict_without_assignments(
    payroll_setup_db,
):
    db = payroll_setup_db
    successor_date = date(2026, 10, 10)
    setup_id = await _published_setup(
        db, "ZERO_BRANCH_SUCCESSOR", frequency="Week", anchor=successor_date,
    )
    candidate_date = date(2026, 9, 27)
    draft_id = await _new_draft(
        db, setup_id, frequency="Week", anchor=candidate_date,
    )

    result = await boundaries.publication_choices(
        db.company_id, db.user_id, setup_id, draft_id, candidate_date, db.db,
    )
    assert result["requested_valid"] is False
    assert result["conflicts"] == [{
        "branch_id": None,
        "code": "SUCCESSOR_BOUNDARY_INVALID",
        "reason": (
            "Existing scheduled update on 2026-10-10 would not start on a valid "
            "boundary under this schedule"
        ),
    }]


@pytest.mark.asyncio
async def test_publication_choices_defaults_around_to_today_and_suggests_next(
    payroll_setup_db, monkeypatch,
):
    db = payroll_setup_db
    week_anchor = date(2090, 1, 1)
    setup_id = await _published_setup(db, "B", frequency="Week", anchor=week_anchor)

    biweek_anchor = date(2090, 2, 5)
    draft_id = await _new_draft(db, setup_id, frequency="Biweek", anchor=biweek_anchor)

    today = date(2090, 2, 10)  # off the Biweek grid
    _stub_today(monkeypatch, today)

    result = await boundaries.publication_choices(
        db.company_id, db.user_id, setup_id, draft_id, None, db.db,
    )
    assert result["reference_date"] == today
    assert result["requested_date"] == today
    assert result["requested_valid"] is False
    assert result["suggested"] == result["next"]
    assert result["suggested"]["date"] == date(2090, 2, 19)


@pytest.mark.asyncio
async def test_publication_choices_same_date_reports_replacement_fields(
    payroll_setup_db, monkeypatch,
):
    db = payroll_setup_db
    anchor = date(2090, 1, 1)
    setup_id = await _published_setup(db, "C", frequency="Week", anchor=anchor)
    versions = await db.db.execute(text("""
        SELECT PayrollSetupVersionID, VersionNumber FROM payroll.PayrollSetupVersions
        WHERE PayrollSetupID = :sid AND LifecycleState = 'Published'
    """), {"sid": setup_id})
    v1 = versions.mappings().one()

    draft_id = await _new_draft(db, setup_id, frequency="Week", anchor=anchor)
    _stub_today(monkeypatch, date(2090, 6, 1))

    result = await boundaries.publication_choices(
        db.company_id, db.user_id, setup_id, draft_id, anchor, db.db,
    )
    assert result["requested_valid"] is True
    assert result["requested"]["replaces_version_id"] == v1["payrollsetupversionid"]
    assert result["requested"]["replaces_version_number"] == v1["versionnumber"]


# ---------------------------------------------------------------------------
# (b) Validity is independent of "today" for an explicit `around`.
# ---------------------------------------------------------------------------


def _without_relation(choice: dict | None) -> dict | None:
    if choice is None:
        return None
    return {k: v for k, v in choice.items() if k != "relation"}


@pytest.mark.asyncio
async def test_publication_and_reassignment_validity_independent_of_today(
    payroll_setup_db, monkeypatch,
):
    db = payroll_setup_db
    anchor = date(2090, 1, 1)
    setup_id = await _published_setup(db, "D", frequency="Week", anchor=anchor)
    draft_id = await _new_draft(db, setup_id, frequency="Biweek", anchor=anchor)
    around = date(2090, 1, 8)  # a Week boundary, off the Biweek grid

    _stub_today(monkeypatch, date(2089, 1, 1))
    first = await boundaries.publication_choices(
        db.company_id, db.user_id, setup_id, draft_id, around, db.db,
    )
    _stub_today(monkeypatch, date(2095, 6, 15))
    second = await boundaries.publication_choices(
        db.company_id, db.user_id, setup_id, draft_id, around, db.db,
    )

    assert first["requested_valid"] == second["requested_valid"]
    assert first["conflicts"] == second["conflicts"]
    assert first["requested_date"] == second["requested_date"]
    assert _without_relation(first["previous"]) == _without_relation(second["previous"])
    assert _without_relation(first["next"]) == _without_relation(second["next"])
    assert first["suggested"] is None and second["suggested"] is None
    assert first["reference_date"] != second["reference_date"]

    # Reassignment: source Week setup, destination the Biweek draft published.
    # Reset the onboarding-window clock stub to the anchor itself so this
    # setup assignment (an unrelated fixture step) is not itself rejected.
    _stub_today(monkeypatch, anchor)
    await assign_setup(db.company_id, db.user_id, db.branch_id, setup_id, anchor, db.db)
    dest_setup_id = await _published_setup(db, "D2", frequency="Biweek", anchor=anchor)

    _stub_today(monkeypatch, date(2089, 1, 1))
    first_r = await boundaries.reassignment_choices(
        db.company_id, db.user_id, db.branch_id, dest_setup_id, around, db.db,
    )
    _stub_today(monkeypatch, date(2095, 6, 15))
    second_r = await boundaries.reassignment_choices(
        db.company_id, db.user_id, db.branch_id, dest_setup_id, around, db.db,
    )
    assert first_r["requested_valid"] == second_r["requested_valid"]
    assert first_r["conflicts"] == second_r["conflicts"]
    assert _without_relation(first_r["previous"]) == _without_relation(second_r["previous"])
    assert _without_relation(first_r["next"]) == _without_relation(second_r["next"])
    assert first_r["suggested"] is None and second_r["suggested"] is None


# ---------------------------------------------------------------------------
# (c) Reassignment: both-sides validity, predecessor fields, sparse
#     Week -> Month boundary, and an unreachable Custom -> Custom grid.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reassignment_choices_navigates_and_reports_predecessor(
    payroll_setup_db, monkeypatch,
):
    db = payroll_setup_db
    anchor = date(2090, 1, 1)
    source_setup_id = await _published_setup(db, "E1", frequency="Week", anchor=anchor)
    await assign_setup(db.company_id, db.user_id, db.branch_id, source_setup_id, anchor, db.db)
    dest_setup_id = await _published_setup(db, "E2", frequency="Biweek", anchor=anchor)

    _stub_today(monkeypatch, date(2090, 1, 20))
    around = date(2090, 1, 22)  # Week-grid date, off the destination Biweek grid

    result = await boundaries.reassignment_choices(
        db.company_id, db.user_id, db.branch_id, dest_setup_id, around, db.db,
    )
    assert result["requested_valid"] is False
    assert any(c["code"] == "SUCCESSOR_BOUNDARY_INVALID" and c["branch_id"] == db.branch_id
               for c in result["conflicts"])
    assert result["previous"]["date"] == date(2090, 1, 15)
    assert result["next"]["date"] == date(2090, 1, 29)
    for choice in (result["previous"], result["next"]):
        assert (choice["date"] - anchor).days % 7 == 0  # valid on the Week source grid too
        assert choice["predecessor_payroll_frequency"] == "Week"
        assert choice["predecessor_period_end_date"] == choice["date"] - timedelta(days=1)

    new_assignment_id = await reassign_setup(
        db.company_id, db.user_id, db.branch_id, dest_setup_id, result["next"]["date"], db.db,
    )
    assert new_assignment_id is not None


@pytest.mark.asyncio
async def test_reassignment_choices_finds_sparse_week_to_month_boundary(
    payroll_setup_db, monkeypatch,
):
    db = payroll_setup_db
    anchor = date(2090, 1, 1)
    source_setup_id = await _published_setup(db, "F1", frequency="Week", anchor=anchor)
    await assign_setup(db.company_id, db.user_id, db.branch_id, source_setup_id, anchor, db.db)
    dest_setup_id = await _published_setup(db, "F2", frequency="Month", anchor=anchor)

    _stub_today(monkeypatch, anchor)
    result = await boundaries.reassignment_choices(
        db.company_id, db.user_id, db.branch_id, dest_setup_id, anchor, db.db,
    )
    assert result["next"] is not None
    next_date = result["next"]["date"]
    assert (next_date - anchor).days % 7 == 0
    month_schedule = Schedule("Month", anchor, None, 0)
    assert is_period_start(month_schedule, next_date) is True

    await reassign_setup(
        db.company_id, db.user_id, db.branch_id, dest_setup_id, next_date, db.db,
    )


@pytest.mark.asyncio
async def test_reassignment_choices_reports_no_next_when_grids_never_align(
    payroll_setup_db, monkeypatch,
):
    db = payroll_setup_db
    source_anchor = date(2090, 1, 1)
    dest_anchor = date(2090, 1, 6)  # source_anchor + 5: never a multiple of 20 away
    source_setup_id = await _published_setup(
        db, "G1", frequency="Custom", anchor=source_anchor, interval=20,
    )
    await assign_setup(
        db.company_id, db.user_id, db.branch_id, source_setup_id, source_anchor, db.db,
    )
    dest_setup_id = await _published_setup(
        db, "G2", frequency="Custom", anchor=dest_anchor, interval=10,
    )

    _stub_today(monkeypatch, dest_anchor)
    result = await boundaries.reassignment_choices(
        db.company_id, db.user_id, db.branch_id, dest_setup_id, dest_anchor, db.db,
    )
    assert result["next"] is None


# ---------------------------------------------------------------------------
# (d) Initial assignment onboarding window: suggestion, floor, and rejection.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assignment_choices_suggests_current_period_and_enforces_floor(
    payroll_setup_db, monkeypatch,
):
    db = payroll_setup_db
    anchor = date(2090, 1, 1)
    setup_id = await _published_setup(db, "H", frequency="Week", anchor=anchor)
    today = date(2090, 3, 15)  # mid-period
    _stub_today(monkeypatch, today)

    segments = await terminal_segments(db.db, setup_id)
    floor = onboarding_window_floor(segments, today)

    result = await boundaries.assignment_choices(
        db.company_id, db.user_id, db.branch_id, setup_id, None, db.db,
    )
    assert result["earliest_allowed_date"] == floor
    assert result["suggested"] is not None
    assert result["suggested"]["relation"] == "current"
    assert result["suggested"]["date"] < today
    assert result["previous"] is None or result["previous"]["date"] >= floor

    too_early = today - timedelta(days=21)  # three periods back
    blocked = await boundaries.assignment_choices(
        db.company_id, db.user_id, db.branch_id, setup_id, too_early, db.db,
    )
    assert blocked["requested_valid"] is False
    assert any(c["code"] == "ONBOARDING_START_TOO_EARLY" for c in blocked["conflicts"])

    await assign_setup(
        db.company_id, db.user_id, db.branch_id, setup_id, result["suggested"]["date"], db.db,
    )


@pytest.mark.asyncio
async def test_assignment_choices_floor_is_none_once_branch_has_a_timeline(
    payroll_setup_db, monkeypatch,
):
    db = payroll_setup_db
    anchor = date(2090, 1, 1)
    setup_id = await _published_setup(db, "I", frequency="Week", anchor=anchor)
    today = date(2090, 3, 15)
    _stub_today(monkeypatch, today)

    other_branch = await _new_branch(db, "IB")
    # Reset the onboarding-window clock stub to the anchor itself so this
    # setup assignment (an unrelated fixture step) is not itself rejected.
    _stub_today(monkeypatch, anchor)
    await assign_setup(db.company_id, db.user_id, other_branch, setup_id, anchor, db.db)
    _stub_today(monkeypatch, today)

    result = await boundaries.assignment_choices(
        db.company_id, db.user_id, other_branch, setup_id, None, db.db,
    )
    assert result["earliest_allowed_date"] is None


# ---------------------------------------------------------------------------
# (e) Onboarding options: no default, with default, and the write it feeds.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_onboarding_choices_suggests_current_period(payroll_setup_db, monkeypatch):
    db = payroll_setup_db
    anchor = date(2090, 1, 1)
    setup_id = await _published_setup(db, "J", frequency="Week", anchor=anchor)
    today = date(2090, 3, 15)
    _stub_today(monkeypatch, today)

    result = await boundaries.onboarding_choices(db.company_id, setup_id, None, db.db)
    assert result["suggested"] is not None
    assert result["suggested"]["relation"] == "current"
    assert result["suggested"]["date"] < today
    assert result["earliest_allowed_date"] is not None


@pytest.mark.asyncio
async def test_onboarding_choices_reports_setup_not_active_as_a_conflict_not_a_special_case(
    payroll_setup_db, monkeypatch,
):
    db = payroll_setup_db
    anchor = date(2090, 1, 1)
    setup_id = await _published_setup(db, "K", frequency="Week", anchor=anchor)
    await archive_setup(db.company_id, db.user_id, setup_id, db.db)
    _stub_today(monkeypatch, anchor)

    result = await boundaries.onboarding_choices(db.company_id, setup_id, anchor, db.db)
    assert result["requested_valid"] is False
    assert any(c["code"] == "SETUP_NOT_ACTIVE" for c in result["conflicts"])


# ---------------------------------------------------------------------------
# (e cont'd) / (f) HTTP: onboarding-options endpoint, and the choices
# endpoints' auth / 404 / 422 mapping and JSON "date" key.
# ---------------------------------------------------------------------------


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _make_actor(db_conn, *, permissions: tuple[str, ...], scope: str,
                      branch_id: int | None = None) -> str:
    suffix = uuid4().hex[:10]
    company_id = (await db_conn.execute(text(
        "SELECT CompanyID FROM core.Companies WHERE CompanyCode = 'DEMO'"
    ))).scalar_one()
    user_id = (await db_conn.execute(text("""
        INSERT INTO sec.Users (CompanyID, Username, DisplayName, IsActive, CanLogin)
        VALUES (:cid, :name, 'Boundary choices actor', TRUE, TRUE)
        RETURNING UserID
    """), {"cid": company_id, "name": "bcactor_" + suffix})).scalar_one()
    role_id = (await db_conn.execute(text("""
        INSERT INTO sec.Roles (RoleCode, RoleName, RoleLevel, IsSystemRole)
        VALUES (:code, 'Boundary choices actor', 20, FALSE) RETURNING RoleID
    """), {"code": "BCR_" + suffix})).scalar_one()
    company_role_id = (await db_conn.execute(text("""
        INSERT INTO sec.CompanyRoles
            (CompanyID, RoleCode, RoleName, RoleLevel, IsDefault,
             IsProtected, IsCustom, IsActive)
        VALUES (:cid, :code, 'Boundary choices actor', 20, FALSE, FALSE, TRUE, TRUE)
        RETURNING CompanyRoleID
    """), {"cid": company_id, "code": "BCC_" + suffix})).scalar_one()
    for permission in permissions:
        await db_conn.execute(text("""
            INSERT INTO sec.CompanyRolePermissions (CompanyRoleID, PermissionCode)
            VALUES (:rid, :permission)
        """), {"rid": company_role_id, "permission": permission})
    await db_conn.execute(text("""
        INSERT INTO sec.UserBranchRoles
            (UserID, CompanyID, BranchID, RoleID, CompanyRoleID, ScopeType, IsActive)
        VALUES (:uid, :cid, :bid, :rid, :crid, :scope, TRUE)
    """), {
        "uid": user_id, "cid": company_id,
        "bid": branch_id if scope == "SpecificBranch" else None,
        "rid": role_id, "crid": company_role_id, "scope": scope,
    })
    return create_access_token(int(user_id), int(company_id))


async def _create_published_setup_via_api(client, token: str, marker: str, *,
                                          frequency: str = "Week") -> tuple[int, int]:
    created = await client.post("/payroll-setup/setups", headers=_auth(token), json={
        "setup_code": "BCH_" + marker[:10], "setup_name": "Boundary choices HTTP",
    })
    assert created.status_code == 201, created.text
    setup_id = created.json()["setup_id"]
    draft = await client.post(
        f"/payroll-setup/setups/{setup_id}/drafts", headers=_auth(token), json={
            "payroll_frequency": frequency, "anchor_start_date": "2090-01-01",
            "normal_days_off_mask": 0,
        },
    )
    assert draft.status_code == 201, draft.text
    draft_id = draft.json()["version_id"]
    published = await client.post(
        f"/payroll-setup/setups/{setup_id}/drafts/{draft_id}/publish",
        headers=_auth(token), json={"effective_from_date": "2090-01-01"},
    )
    assert published.status_code == 201, published.text
    return setup_id, draft_id


@pytest.mark.asyncio
async def test_http_publication_choices_200_403_404_422(client, auth_token, db_conn):
    marker = uuid4().hex
    setup_id, _ = await _create_published_setup_via_api(client, auth_token, marker)

    # A second (not-yet-published) Draft on the same Setup — publication-choices
    # is queried against a live Draft, not one already Published.
    correction_draft = await client.post(
        f"/payroll-setup/setups/{setup_id}/drafts", headers=_auth(auth_token), json={
            "payroll_frequency": "Week", "anchor_start_date": "2090-01-01",
            "normal_days_off_mask": 0,
        },
    )
    assert correction_draft.status_code == 201, correction_draft.text
    draft_id = correction_draft.json()["version_id"]

    ok = await client.get(
        f"/payroll-setup/setups/{setup_id}/drafts/{draft_id}/publication-choices",
        headers=_auth(auth_token), params={"around": "2090-01-01"},
    )
    assert ok.status_code == 200, ok.text
    assert '"date"' in ok.text
    assert ok.json()["requested"]["date"] == "2090-01-01"

    no_view = await _make_actor(db_conn, permissions=(), scope="AllCompanyBranches")
    denied = await client.get(
        f"/payroll-setup/setups/{setup_id}/drafts/{draft_id}/publication-choices",
        headers=_auth(no_view),
    )
    assert denied.status_code == 403, denied.text

    missing = await client.get(
        "/payroll-setup/setups/999999999/drafts/1/publication-choices",
        headers=_auth(auth_token),
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"]["code"] == "SETUP_NOT_FOUND"

    empty_draft = await client.post(
        f"/payroll-setup/setups/{setup_id}/drafts", headers=_auth(auth_token), json={},
    )
    assert empty_draft.status_code == 201, empty_draft.text
    incomplete = await client.get(
        f"/payroll-setup/setups/{setup_id}/drafts/{empty_draft.json()['version_id']}"
        "/publication-choices",
        headers=_auth(auth_token),
    )
    assert incomplete.status_code == 422, incomplete.text
    assert incomplete.json()["detail"]["code"] == "INVALID_SCHEDULE"


@pytest.mark.asyncio
async def test_http_assignment_choices_200_and_403(client, auth_token, db_conn):
    marker = uuid4().hex
    setup_id, _ = await _create_published_setup_via_api(client, auth_token, marker)
    branch = await client.post("/settings/branches", headers=_auth(auth_token), json={
        "branch_name": "Boundary choices HTTP " + marker[:8],
        "branch_code": "BCHA" + marker[:6],
    })
    assert branch.status_code == 201, branch.text
    branch_id = branch.json()["branch_id"]

    ok = await client.get(
        f"/payroll-setup/branches/{branch_id}/assignment-choices",
        headers=_auth(auth_token), params={"setup_id": setup_id, "around": "2090-01-01"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["requested"]["date"] == "2090-01-01"

    no_view = await _make_actor(db_conn, permissions=(), scope="AllCompanyBranches")
    denied = await client.get(
        f"/payroll-setup/branches/{branch_id}/assignment-choices",
        headers=_auth(no_view), params={"setup_id": setup_id},
    )
    assert denied.status_code == 403, denied.text


@pytest.mark.asyncio
async def test_http_assignment_choices_404_unknown_branch_unknown_setup_and_cross_tenant(
    client, auth_token, db_conn,
):
    """Review fix: an unknown Branch or Setup on `assignment-choices` must 404,
    not report a conflict with HTTP 200 — matching every other endpoint's
    existence-check contract. Inactive Branch / archived Setup remain
    conflicts (unchanged) and are covered by the domain-level test above."""
    marker = uuid4().hex
    setup_id, _ = await _create_published_setup_via_api(client, auth_token, marker)
    branch = await client.post("/settings/branches", headers=_auth(auth_token), json={
        "branch_name": "Boundary choices 404 " + marker[:8],
        "branch_code": "BC404" + marker[:5],
    })
    assert branch.status_code == 201, branch.text
    branch_id = branch.json()["branch_id"]

    unknown_branch = await client.get(
        "/payroll-setup/branches/999999999/assignment-choices",
        headers=_auth(auth_token), params={"setup_id": setup_id},
    )
    assert unknown_branch.status_code == 404, unknown_branch.text
    assert unknown_branch.json()["detail"]["code"] == "BRANCH_NOT_FOUND"

    unknown_setup = await client.get(
        f"/payroll-setup/branches/{branch_id}/assignment-choices",
        headers=_auth(auth_token), params={"setup_id": 999999999},
    )
    assert unknown_setup.status_code == 404, unknown_setup.text
    assert unknown_setup.json()["detail"]["code"] == "SETUP_NOT_FOUND"

    foreign_company = (await db_conn.execute(text("""
        INSERT INTO core.Companies (CompanyCode, CompanyName, Status, IsSuspended)
        VALUES (:code, 'Boundary choices foreign tenant', 'Active', FALSE)
        RETURNING CompanyID
    """), {"code": "BCX_" + marker[:8]})).scalar_one()
    foreign_branch = (await db_conn.execute(text("""
        INSERT INTO core.Branches (CompanyID, BranchCode, BranchName, Status, IsDefault)
        VALUES (:cid, 'FOREIGN', 'Foreign branch', 'Active', FALSE) RETURNING BranchID
    """), {"cid": foreign_company})).scalar_one()

    cross_tenant = await client.get(
        f"/payroll-setup/branches/{foreign_branch}/assignment-choices",
        headers=_auth(auth_token), params={"setup_id": setup_id},
    )
    assert cross_tenant.status_code == 404, cross_tenant.text
    assert cross_tenant.json()["detail"]["code"] == "BRANCH_NOT_FOUND"


@pytest.mark.asyncio
async def test_http_reassignment_choices_200_403_404(client, auth_token, db_conn):
    marker = uuid4().hex
    source_id, _ = await _create_published_setup_via_api(client, auth_token, marker)
    branch = await client.post("/settings/branches", headers=_auth(auth_token), json={
        "branch_name": "Boundary choices HTTP R" + marker[:8],
        "branch_code": "BCHR" + marker[:6],
    })
    assert branch.status_code == 201, branch.text
    branch_id = branch.json()["branch_id"]
    assigned = await client.post(
        f"/payroll-setup/branches/{branch_id}/assignments", headers=_auth(auth_token), json={
            "setup_id": source_id, "effective_from_date": "2090-01-01",
        },
    )
    assert assigned.status_code == 201, assigned.text

    dest_id, _ = await _create_published_setup_via_api(client, auth_token, uuid4().hex,
                                                        frequency="Biweek")
    ok = await client.get(
        f"/payroll-setup/branches/{branch_id}/reassignment-choices",
        headers=_auth(auth_token),
        params={"destination_setup_id": dest_id, "around": "2090-01-15"},
    )
    assert ok.status_code == 200, ok.text
    assert '"date"' in ok.text

    no_view = await _make_actor(db_conn, permissions=(), scope="AllCompanyBranches")
    denied = await client.get(
        f"/payroll-setup/branches/{branch_id}/reassignment-choices",
        headers=_auth(no_view), params={"destination_setup_id": dest_id},
    )
    assert denied.status_code == 403, denied.text

    missing = await client.get(
        f"/payroll-setup/branches/{branch_id}/reassignment-choices",
        headers=_auth(auth_token), params={"destination_setup_id": 999999999},
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"]["code"] == "SETUP_NOT_FOUND"


@pytest.mark.asyncio
async def test_http_onboarding_options_no_default_and_with_default(
    client, auth_token, db_conn,
):
    marker = uuid4().hex
    company_id = (await db_conn.execute(text(
        "SELECT CompanyID FROM core.Companies WHERE CompanyCode = 'DEMO'"
    ))).scalar_one()
    old_default = (await db_conn.execute(text(
        "SELECT DefaultPayrollSetupID FROM core.Companies WHERE CompanyID = :cid"
    ), {"cid": company_id})).scalar_one_or_none()

    try:
        cleared = await client.put("/payroll-setup/default", headers=_auth(auth_token),
                                   json={"setup_id": None})
        assert cleared.status_code == 204, cleared.text

        empty = await client.get("/settings/branches/onboarding-options",
                                 headers=_auth(auth_token))
        assert empty.status_code == 200, empty.text
        assert empty.json() == {"default_setup": None, "choices": None}

        setup_id, _ = await _create_published_setup_via_api(client, auth_token, marker)
        set_default = await client.put("/payroll-setup/default", headers=_auth(auth_token),
                                       json={"setup_id": setup_id})
        assert set_default.status_code == 204, set_default.text

        with_default = await client.get("/settings/branches/onboarding-options",
                                        headers=_auth(auth_token),
                                        params={"around": "2090-01-01"})
        assert with_default.status_code == 200, with_default.text
        body = with_default.json()
        assert body["default_setup"]["setup_id"] == setup_id
        assert body["choices"]["requested"]["date"] == "2090-01-01"

        no_around = await client.get("/settings/branches/onboarding-options",
                                     headers=_auth(auth_token))
        assert no_around.status_code == 200, no_around.text
        suggested = no_around.json()["choices"]["suggested"]
        assert suggested is not None

        branch_code = "BCHO" + marker[:6]
        created = await client.post("/settings/branches", headers=_auth(auth_token), json={
            "branch_name": "Boundary choices onboarding " + marker[:8],
            "branch_code": branch_code,
            "first_payroll_start_date": suggested["date"],
        })
        assert created.status_code == 201, created.text
        branch_id = created.json()["branch_id"]
        assignment = (await db_conn.execute(text("""
            SELECT EffectiveFromDate FROM payroll.BranchPayrollSetupAssignments
            WHERE CompanyID = :cid AND BranchID = :bid AND WithdrawnAtUtc IS NULL
        """), {"cid": company_id, "bid": branch_id})).scalar_one()
        assert assignment.isoformat() == suggested["date"]

        missing_assign = await _make_actor(
            db_conn, permissions=("branches.create",), scope="AllCompanyBranches",
        )
        denied1 = await client.get("/settings/branches/onboarding-options",
                                   headers=_auth(missing_assign))
        assert denied1.status_code == 403, denied1.text

        missing_create = await _make_actor(
            db_conn, permissions=("payroll_setup.assign",), scope="AllCompanyBranches",
        )
        denied2 = await client.get("/settings/branches/onboarding-options",
                                   headers=_auth(missing_create))
        assert denied2.status_code == 403, denied2.text
    finally:
        restore = await client.put("/payroll-setup/default", headers=_auth(auth_token),
                                   json={"setup_id": old_default})
        assert restore.status_code == 204, restore.text
