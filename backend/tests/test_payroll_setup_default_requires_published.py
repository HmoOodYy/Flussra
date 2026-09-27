"""A Payroll Setup can only become the Company Default once it has at least one
Published Version. Drafts do not count; a Published Version that starts in the
future does. Company Default stays onboarding-only (no branch reassignment)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.payroll_setup.errors import PolicyError
from app.payroll_setup.payroll_policy import (
    create_draft,
    create_setup,
    publish_version,
    set_default_setup,
)


@pytest_asyncio.fixture
async def payroll_setup_db(test_database_url):
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
                    db=conn, company_id=int(tenant["companyid"]),
                    user_id=int(tenant["userid"]), marker=marker,
                )
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


ANCHOR = date(2090, 3, 1)


async def _new_setup(db, suffix: str) -> int:
    return await create_setup(
        db.company_id, db.user_id, f"DP{suffix}_{db.marker}", f"Default prerequisite {suffix}", db.db,
    )


async def _draft(db, setup_id: int) -> int:
    return await create_draft(
        db.company_id, db.user_id, setup_id, db.db,
        payroll_frequency="Week", anchor_start_date=ANCHOR,
        custom_interval_days=None, normal_days_off_mask=0,
    )


async def _current_default(db) -> int | None:
    return (await db.db.execute(text("""
        SELECT DefaultPayrollSetupID FROM core.Companies WHERE CompanyID = :cid
    """), {"cid": db.company_id})).scalar_one_or_none()


@pytest.mark.asyncio
async def test_setup_with_no_versions_cannot_become_default(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db, "a")
    before = await _current_default(db)

    with pytest.raises(PolicyError) as error:
        await set_default_setup(db.company_id, db.user_id, setup_id, db.db)

    assert error.value.code == "DEFAULT_SETUP_NOT_PUBLISHED"
    assert "Publish a payroll schedule" in str(error.value)
    assert await _current_default(db) == before


@pytest.mark.asyncio
async def test_draft_only_setup_cannot_become_default(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db, "b")
    await _draft(db, setup_id)
    before = await _current_default(db)

    with pytest.raises(PolicyError) as error:
        await set_default_setup(db.company_id, db.user_id, setup_id, db.db)

    assert error.value.code == "DEFAULT_SETUP_NOT_PUBLISHED"
    assert await _current_default(db) == before


@pytest.mark.asyncio
async def test_setup_with_a_published_version_can_become_default(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db, "c")
    await publish_version(db.company_id, db.user_id, setup_id, await _draft(db, setup_id), ANCHOR, db.db)

    await set_default_setup(db.company_id, db.user_id, setup_id, db.db)

    assert await _current_default(db) == setup_id


@pytest.mark.asyncio
async def test_future_only_published_version_satisfies_the_prerequisite(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db, "d")
    # 2090 is far in the future of any real company-local today.
    await publish_version(db.company_id, db.user_id, setup_id, await _draft(db, setup_id), ANCHOR, db.db)

    await set_default_setup(db.company_id, db.user_id, setup_id, db.db)

    assert await _current_default(db) == setup_id


@pytest.mark.asyncio
async def test_clearing_the_default_needs_no_published_version(payroll_setup_db):
    db = payroll_setup_db
    setup_id = await _new_setup(db, "e")
    await publish_version(db.company_id, db.user_id, setup_id, await _draft(db, setup_id), ANCHOR, db.db)
    await set_default_setup(db.company_id, db.user_id, setup_id, db.db)

    await set_default_setup(db.company_id, db.user_id, None, db.db)

    assert await _current_default(db) is None


@pytest.mark.asyncio
async def test_http_put_default_rejects_unpublished_setup(client, auth_token):
    headers = {"Authorization": f"Bearer {auth_token}"}
    created = await client.post("/payroll-setup/setups", headers=headers, json={
        "setup_code": "DPHTTP_" + uuid4().hex[:8], "setup_name": "Unpublished default attempt",
    })
    assert created.status_code == 201, created.text

    response = await client.put(
        "/payroll-setup/default", headers=headers,
        json={"setup_id": created.json()["setup_id"]},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "DEFAULT_SETUP_NOT_PUBLISHED"
