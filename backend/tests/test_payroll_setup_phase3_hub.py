"""Current Payroll readiness resolves only persisted setup assignments."""

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.payroll import current_hub
from app.payroll_setup.policy import (
    assign_setup,
    create_draft,
    create_setup,
    publish_version,
    set_default_setup,
)


@pytest_asyncio.fixture
async def setup_hub_db(test_database_url):
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
                yield SimpleNamespace(
                    db=conn,
                    company_id=int(tenant["companyid"]),
                    user_id=int(tenant["userid"]),
                    marker=marker,
                )
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


async def _branch(db, suffix: str) -> int:
    return int((await db.db.execute(text("""
        INSERT INTO core.Branches
            (CompanyID, BranchCode, BranchName, Status, IsDefault)
        VALUES (:cid, :code, :name, 'Active', FALSE)
        RETURNING BranchID
    """), {
        "cid": db.company_id,
        "code": f"P3H_{db.marker}_{suffix}",
        "name": f"Phase 3 hub {suffix}",
    })).scalar_one())


async def _published_setup(db) -> int:
    setup_id = await create_setup(
        db.company_id, db.user_id, f"P3H_{db.marker}", "Phase 3 hub setup", db.db,
    )
    draft_id = await create_draft(
        db.company_id, db.user_id, setup_id, db.db,
        payroll_frequency="Week", anchor_start_date=date(2090, 1, 1),
        normal_days_off_mask=0,
    )
    await publish_version(
        db.company_id, db.user_id, setup_id, draft_id, date(2090, 1, 1), db.db,
    )
    return setup_id


async def _entry(db, branch_id: int) -> dict:
    response = await current_hub.get_current_workflow(
        db.company_id, db.user_id, branch_id, db.db,
    )
    return response.branches[0].model_dump()


@pytest.mark.asyncio
async def test_readiness_uses_assignment_and_latest_non_cancelled_period_date(
    setup_hub_db, monkeypatch,
):
    db = setup_hub_db

    async def not_driver(*args, **kwargs):
        return None

    async def company_access(*args, **kwargs):
        return True, []

    async def has_permission(*args, **kwargs):
        return True

    monkeypatch.setattr(current_hub, "_require_not_driver_role", not_driver)
    monkeypatch.setattr(current_hub, "_get_oda_own_driver_id", not_driver)
    monkeypatch.setattr(current_hub, "_check_branch_access", company_access)
    monkeypatch.setattr(current_hub, "_has_any_permission", has_permission)

    setup_id = await _published_setup(db)
    await set_default_setup(db.company_id, db.user_id, setup_id, db.db)

    default_only_branch = await _branch(db, "DEFAULT")
    assert (await _entry(db, default_only_branch))["setup_status"] == "missing"

    assigned_branch = await _branch(db, "ASSIGNED")
    await assign_setup(
        db.company_id, db.user_id, assigned_branch, setup_id, date(2090, 1, 1), db.db,
    )
    await db.db.execute(text("""
        INSERT INTO payroll.PayrollPeriods
            (CompanyID, BranchID, PeriodCode, PeriodName, PeriodType,
             StartDate, EndDate, Status)
        VALUES
            (:cid, :bid, :archived_code, 'Prior period', 'Week',
             '2090-01-01', '2090-01-07', 'Archived'),
            (:cid, :bid, :cancelled_code, 'Cancelled later period', 'Week',
             '2090-01-08', '2090-01-14', 'Cancelled')
    """), {
        "cid": db.company_id,
        "bid": assigned_branch,
        "archived_code": f"P3H_A_{db.marker}",
        "cancelled_code": f"P3H_C_{db.marker}",
    })

    resolved_dates = []
    original_resolver = current_hub.resolve_payroll_setup_version

    async def capture_resolution_date(company_id, branch_id, start_date, conn):
        resolved_dates.append(start_date)
        return await original_resolver(company_id, branch_id, start_date, conn)

    monkeypatch.setattr(current_hub, "resolve_payroll_setup_version", capture_resolution_date)
    assert (await _entry(db, assigned_branch))["setup_status"] == "complete"
    assert resolved_dates == [date(2090, 1, 8)]
