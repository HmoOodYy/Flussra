"""Impact previews warn about Driver pay rules a calendar change would stop fitting.

A Minimum/Maximum pay rule applies only to a period that is exactly one period of the calendar
it recorded. Publishing a Payroll Setup version, or moving a Branch to another Setup, can leave
rules that no longer fit; the previews list them (read-only, no amounts) so the change can be
reviewed. Nothing here ends, rewrites or blocks a rule.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth.security import create_access_token
from app.payroll_setup.chronology import Schedule
from app.payroll_setup.payroll_policy import (
    assign_setup,
    create_draft,
    create_setup,
    preview_policy_impact,
    preview_reassignment_impact,
    publish_version,
)
from tests.builders.company import create_branch
from tests.builders.pay_rules import insert_pay_rule
from tests.builders.workforce_db import insert_driver_assignment

ANCHOR = date(2090, 1, 1)
CHANGE = date(2090, 1, 15)  # a weekly period start of the ANCHOR grid (14 days on)


@pytest_asyncio.fixture
async def world(test_database_url):
    """A rollback-only transaction with two fresh Branches in the DEMO company."""
    engine = create_async_engine(test_database_url, echo=False)
    marker = uuid4().hex[:10]
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            try:
                tenant = (await conn.execute(text("""
                    SELECT c.CompanyID, u.UserID FROM core.Companies c
                    JOIN sec.Users u ON u.CompanyID = c.CompanyID
                    WHERE c.CompanyCode = 'DEMO' AND u.Username = 'admin'
                """))).mappings().one()
                company_id, user_id = int(tenant["companyid"]), int(tenant["userid"])
                branch_id = int(await create_branch(
                    conn, company_id, user_id, branch_code=f"PI_{marker}",
                    branch_name=f"Impact branch {marker}"))
                other_branch_id = int(await create_branch(
                    conn, company_id, user_id, branch_code=f"PO_{marker}",
                    branch_name=f"Impact other {marker}"))
                yield SimpleNamespace(
                    db=conn, company_id=company_id, user_id=user_id, branch_id=branch_id,
                    other_branch_id=other_branch_id, marker=marker)
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


async def _setup(w, suffix: str, *, frequency: str = "Week", anchor: date = ANCHOR) -> int:
    setup_id = await create_setup(
        w.company_id, w.user_id, f"PI_{w.marker}_{suffix}", f"Impact setup {suffix}", w.db)
    draft_id = await create_draft(
        w.company_id, w.user_id, setup_id, w.db, payroll_frequency=frequency,
        anchor_start_date=anchor, custom_interval_days=None, normal_days_off_mask=0)
    await publish_version(w.company_id, w.user_id, setup_id, draft_id, anchor, w.db)
    return setup_id


async def _driver(w, branch_id: int, name: str, *, number: str | None = None,
                  to_at: str | None = None) -> int:
    employee_id = int((await w.db.execute(text("""
        INSERT INTO core.employees (companyid, branchid, fullname, employmentstatus)
        VALUES (:c, :b, :n, 'Active') RETURNING employeeid
    """), {"c": w.company_id, "b": branch_id, "n": name})).scalar_one())
    driver_id = int((await w.db.execute(text("""
        INSERT INTO core.drivers (companyid, employeeid, drivernumber, driverstatus)
        VALUES (:c, :e, :num, 'Active') RETURNING driverid
    """), {"c": w.company_id, "e": employee_id,
           "num": number or f"D{uuid4().hex[:8]}"})).scalar_one())
    await insert_driver_assignment(
        w.db, company_id=w.company_id, branch_id=branch_id, employee_id=employee_id,
        driver_id=driver_id, effective_from_at="2089-01-01T00:00:00Z",
        effective_to_at=to_at, end_reason=None if to_at is None else "Terminated")
    return driver_id


async def _rule(w, driver_id: int, **kwargs) -> int:
    kwargs.setdefault("start", "2089-06-01")
    return await insert_pay_rule(
        w.db, company_id=w.company_id, driver_id=driver_id, branch_id=w.branch_id,
        anchor=ANCHOR, **kwargs)


async def _publication(w, setup_id: int, schedule: Schedule, on: date = CHANGE) -> dict:
    return await preview_policy_impact(
        w.company_id, w.user_id, setup_id, on, schedule, w.db)


def _rule_ids(impact: dict) -> list[int]:
    return [item["rule_id"] for item in impact["affected_pay_rules"]]


@pytest.mark.asyncio
async def test_publication_to_a_different_calendar_lists_the_rules_that_stop_fitting(world):
    w = world
    setup_id = await _setup(w, "A")
    await assign_setup(w.company_id, w.user_id, w.branch_id, setup_id, ANCHOR, w.db)
    driver_id = await _driver(w, w.branch_id, "Ada Weekly", number="D-ADA")
    minimum = await _rule(w, driver_id, rule_type="MinimumPay", amount="50")
    maximum = await _rule(w, driver_id, rule_type="MaximumPay", amount="500", end="2090-03-01")

    impact = await _publication(w, setup_id, Schedule("Month", CHANGE, None, 0))

    assert impact["allowed"] is True
    assert impact["affected_branch_ids"] == [w.branch_id]
    assert _rule_ids(impact) == [maximum, minimum]  # by rule type, then id
    first = impact["affected_pay_rules"][1]
    assert first == {
        "driver_id": driver_id, "driver_name": "Ada Weekly", "driver_number": "D-ADA",
        "branch_id": w.branch_id, "branch_name": f"Impact branch {w.marker}",
        "rule_id": minimum, "rule_type": "MinimumPay", "calendar_frequency": "Week",
        "calendar_custom_interval_days": None, "calendar_anchor_start_date": ANCHOR,
        "effective_from": date(2089, 6, 1), "effective_to": None,
    }
    assert impact["affected_pay_rules"][0]["effective_to"] == date(2090, 3, 1)
    assert "amount" not in first


@pytest.mark.asyncio
async def test_publication_that_keeps_the_calendar_grid_lists_nothing(world):
    w = world
    setup_id = await _setup(w, "A")
    await assign_setup(w.company_id, w.user_id, w.branch_id, setup_id, ANCHOR, w.db)
    await _rule(w, await _driver(w, w.branch_id, "Ada Weekly"))

    # Same weekly grid under a different anchor, and a days-off-only difference.
    shifted = await _publication(w, setup_id, Schedule("Week", date(2090, 1, 8), None, 0))
    days_off = await _publication(w, setup_id, Schedule("Week", ANCHOR, None, 5))

    assert shifted["allowed"] is True and shifted["affected_pay_rules"] == []
    assert days_off["allowed"] is True and days_off["affected_pay_rules"] == []


@pytest.mark.asyncio
async def test_voided_and_already_ended_rules_are_not_listed(world):
    w = world
    setup_id = await _setup(w, "A")
    await assign_setup(w.company_id, w.user_id, w.branch_id, setup_id, ANCHOR, w.db)
    driver_id = await _driver(w, w.branch_id, "Ada Weekly")
    await _rule(w, driver_id, rule_type="MinimumPay", status="Voided")
    await _rule(w, driver_id, rule_type="MinimumPay", status="Ended",
                start="2089-06-01", end="2090-01-14")
    ends_on_change_date = await _rule(
        w, driver_id, rule_type="MaximumPay", status="Ended", end="2090-01-15")

    impact = await _publication(w, setup_id, Schedule("Month", CHANGE, None, 0))

    assert _rule_ids(impact) == [ends_on_change_date]


@pytest.mark.asyncio
async def test_only_drivers_still_in_the_affected_branch_are_listed(world):
    w = world
    setup_id = await _setup(w, "A")
    await assign_setup(w.company_id, w.user_id, w.branch_id, setup_id, ANCHOR, w.db)
    stays = await _driver(w, w.branch_id, "Stays Here")
    left = await _driver(w, w.branch_id, "Left Before", to_at="2090-01-10T00:00:00Z")
    elsewhere = await _driver(w, w.other_branch_id, "Other Branch")
    stays_rule = await _rule(w, stays)
    await _rule(w, left)
    await _rule(w, elsewhere)

    impact = await _publication(w, setup_id, Schedule("Month", CHANGE, None, 0))

    assert _rule_ids(impact) == [stays_rule]
    assert impact["affected_pay_rules"][0]["branch_id"] == w.branch_id


@pytest.mark.asyncio
async def test_a_tenure_that_starts_after_the_next_scheduled_version_is_not_listed(world):
    w = world
    setup_id = await _setup(w, "A")
    await assign_setup(w.company_id, w.user_id, w.branch_id, setup_id, ANCHOR, w.db)
    draft_id = await create_draft(
        w.company_id, w.user_id, setup_id, w.db, payroll_frequency="Week",
        anchor_start_date=ANCHOR, custom_interval_days=None, normal_days_off_mask=0)
    await publish_version(w.company_id, w.user_id, setup_id, draft_id, date(2090, 2, 5), w.db)
    late = await _driver(w, w.branch_id, "Starts Later")
    await w.db.execute(text("""
        UPDATE core.driverassignments SET effectivefromat = TIMESTAMPTZ '2090-03-01 00:00+00'
        WHERE driverid = :d"""), {"d": late})
    await _rule(w, late, start="2090-03-01")

    impact = await _publication(w, setup_id, Schedule("Week", CHANGE, None, 0))

    assert impact["next_version_boundary"] == date(2090, 2, 5)
    assert impact["affected_pay_rules"] == []


@pytest.mark.asyncio
async def test_reassignment_lists_rules_only_when_the_destination_calendar_differs(world):
    w = world
    source = await _setup(w, "SRC")
    monthly = await _setup(w, "MON", frequency="Month", anchor=CHANGE)
    same_grid = await _setup(w, "SAME", anchor=date(2090, 1, 8))
    await assign_setup(w.company_id, w.user_id, w.branch_id, source, ANCHOR, w.db)
    driver_id = await _driver(w, w.branch_id, "Ada Weekly")
    rule_id = await _rule(w, driver_id)
    await _rule(w, await _driver(w, w.other_branch_id, "Other Branch"))

    to_monthly = await preview_reassignment_impact(
        w.company_id, w.user_id, w.branch_id, monthly, CHANGE, w.db)
    to_same_grid = await preview_reassignment_impact(
        w.company_id, w.user_id, w.branch_id, same_grid, CHANGE, w.db)

    assert to_monthly["allowed"] is True
    assert _rule_ids(to_monthly) == [rule_id]
    assert to_monthly["affected_pay_rules"][0]["driver_name"] == "Ada Weekly"
    assert to_same_grid["allowed"] is True
    assert to_same_grid["affected_pay_rules"] == []


@pytest.mark.asyncio
async def test_reassignment_without_a_source_assignment_lists_nothing(world):
    w = world
    monthly = await _setup(w, "MON", frequency="Month", anchor=CHANGE)
    await _rule(w, await _driver(w, w.branch_id, "Ada Weekly"))

    impact = await preview_reassignment_impact(
        w.company_id, w.user_id, w.branch_id, monthly, CHANGE, w.db)

    assert impact["allowed"] is False
    assert impact["affected_pay_rules"] == []


# ---------------------------------------------------------------------------
# HTTP: response shape, all three previews, and the existing scope rules.
# ---------------------------------------------------------------------------

def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_http_previews_return_affected_pay_rules_and_keep_company_wide_scope(
    client, auth_token, db_conn,
):
    suffix = uuid4().hex[:8]
    company_id = (await db_conn.execute(text(
        "SELECT CompanyID FROM core.Companies WHERE CompanyCode = 'DEMO'"))).scalar_one()
    branch_id = int((await db_conn.execute(text("""
        INSERT INTO core.Branches
            (CompanyID, BranchCode, BranchName, Status, IsDefault, timezoneid)
        VALUES (:c, :code, :name, 'Active', FALSE, 'UTC') RETURNING BranchID"""),
        {"c": company_id, "code": f"PH_{suffix}",
         "name": f"Http impact {suffix}"})).scalar_one())
    headers = _auth(auth_token)

    async def make_setup(code: str, frequency: str, anchor: str) -> int:
        created = await client.post("/payroll-setup/setups", headers=headers, json={
            "setup_code": f"PH_{suffix}_{code}", "setup_name": f"Http {code}"})
        assert created.status_code == 201, created.text
        sid = created.json()["setup_id"]
        draft = await client.post(f"/payroll-setup/setups/{sid}/drafts", headers=headers, json={
            "payroll_frequency": frequency, "anchor_start_date": anchor,
            "custom_interval_days": None, "normal_days_off_mask": 0})
        assert draft.status_code == 201, draft.text
        published = await client.post(
            f"/payroll-setup/setups/{sid}/drafts/{draft.json()['version_id']}/publish",
            headers=headers, json={"effective_from_date": anchor})
        assert published.status_code == 201, published.text
        return sid

    weekly = await make_setup("W", "Week", "2090-01-01")
    monthly = await make_setup("M", "Month", "2090-01-15")
    assigned = await client.post(
        f"/payroll-setup/branches/{branch_id}/assignments", headers=headers,
        json={"setup_id": weekly, "effective_from_date": "2090-01-01"})
    assert assigned.status_code == 201, assigned.text

    w = SimpleNamespace(db=db_conn, company_id=int(company_id), branch_id=branch_id)
    driver_id = await _driver(w, branch_id, f"Http Driver {suffix}", number=f"H{suffix}")
    rule_id = await _rule(w, driver_id)

    draft = await client.post(f"/payroll-setup/setups/{weekly}/drafts", headers=headers, json={
        "payroll_frequency": "Month", "anchor_start_date": "2090-01-15",
        "custom_interval_days": None, "normal_days_off_mask": 0})
    assert draft.status_code == 201, draft.text
    draft_impact = await client.post(
        f"/payroll-setup/setups/{weekly}/drafts/{draft.json()['version_id']}"
        "/publication-impact", headers=headers, json={"effective_from_date": "2090-01-15"})
    inline_impact = await client.post(
        f"/payroll-setup/setups/{weekly}/publication-impact", headers=headers, json={
            "payroll_frequency": "Month", "anchor_start_date": "2090-01-15",
            "custom_interval_days": None, "normal_days_off_mask": 0,
            "effective_from_date": "2090-01-15"})
    reassignment = await client.post(
        f"/payroll-setup/branches/{branch_id}/reassignment-impact", headers=headers,
        json={"destination_setup_id": monthly, "effective_from_date": "2090-01-15"})

    for response in (draft_impact, inline_impact, reassignment):
        assert response.status_code == 200, response.text
        body = response.json()
        assert [item["rule_id"] for item in body["affected_pay_rules"]] == [rule_id]
        item = body["affected_pay_rules"][0]
        assert item["driver_name"] == f"Http Driver {suffix}"
        assert item["branch_name"] == f"Http impact {suffix}"
        assert item["rule_type"] == "MinimumPay"
        assert item["calendar_frequency"] == "Week"
        assert item["calendar_anchor_start_date"] == "2090-01-01"

    # Existing scope rules are unchanged: a Branch-scoped principal gets no preview at all.
    role_id = (await db_conn.execute(text("""
        INSERT INTO sec.CompanyRoles
            (CompanyID, RoleCode, RoleName, RoleLevel, IsDefault, IsProtected, IsCustom, IsActive)
        VALUES (:c, :code, 'Impact viewer', 20, FALSE, FALSE, TRUE, TRUE)
        RETURNING CompanyRoleID"""), {"c": company_id, "code": f"PHV_{suffix}"})).scalar_one()
    await db_conn.execute(text("""
        INSERT INTO sec.CompanyRolePermissions (CompanyRoleID, PermissionCode)
        VALUES (:r, 'payroll_setup.view')"""), {"r": role_id})
    scoped_user = (await db_conn.execute(text("""
        INSERT INTO sec.Users (CompanyID, Username, DisplayName, IsActive, CanLogin)
        VALUES (:c, :u, 'Impact scoped', TRUE, TRUE) RETURNING UserID"""),
        {"c": company_id, "u": f"phs_{suffix}"})).scalar_one()
    await db_conn.execute(text("""
        INSERT INTO sec.UserBranchRoles
            (UserID, CompanyID, BranchID, CompanyRoleID, ScopeType, IsActive)
        VALUES (:u, :c, :b, :r, 'SpecificBranch', TRUE)"""),
        {"u": scoped_user, "c": company_id, "b": branch_id, "r": role_id})
    scoped = _auth(create_access_token(int(scoped_user), int(company_id)))
    denied = await client.post(
        f"/payroll-setup/branches/{branch_id}/reassignment-impact", headers=scoped,
        json={"destination_setup_id": monthly, "effective_from_date": "2090-01-15"})
    assert denied.status_code == 403, denied.text
