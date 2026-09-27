"""Focused tests for `is_current` on the published-version read model
(`reads.list_versions` / `VersionResponse`): the one published Version in
effect on company-local today. Display/read-model only — `clock.company_today`
never decides payroll authority; it is stubbed here as in
tests/test_payroll_setup_branch_summaries.py."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.payroll_setup import clock, reads
from app.payroll_setup.payroll_policy import create_draft, create_setup, publish_version
from app.payroll_setup.schemas import VersionResponse


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


def _stub_today(monkeypatch, today: date) -> None:
    async def _stub(company_id, conn):
        return today

    monkeypatch.setattr(clock, "company_today", _stub)


ANCHOR = date(2090, 3, 1)  # weekly boundaries: 03-01, 03-08, 03-15, ...


async def _new_setup(db, suffix: str) -> int:
    return await create_setup(
        db.company_id, db.user_id, f"CV{suffix}_{db.marker}", f"Current version {suffix}", db.db,
    )


async def _publish(db, setup_id: int, effective: date, *, mask: int, replaces: int | None = None) -> int:
    draft_id = await create_draft(
        db.company_id, db.user_id, setup_id, db.db,
        payroll_frequency="Week", anchor_start_date=ANCHOR,
        custom_interval_days=None, normal_days_off_mask=mask,
    )
    return await publish_version(
        db.company_id, db.user_id, setup_id, draft_id, effective, db.db,
        replaces_version_id=replaces,
    )


def _current_ids(versions: list[dict]) -> list[int]:
    return [v["version_id"] for v in versions if v["is_current"]]


@pytest.mark.asyncio
async def test_single_past_version_is_current(payroll_setup_db, monkeypatch):
    db = payroll_setup_db
    setup_id = await _new_setup(db, "a")
    v1 = await _publish(db, setup_id, ANCHOR, mask=1)
    _stub_today(monkeypatch, date(2090, 3, 20))

    versions = await reads.list_versions(db.company_id, setup_id, db.db)

    assert _current_ids(versions) == [v1]


@pytest.mark.asyncio
async def test_future_version_is_not_current_while_past_one_is(payroll_setup_db, monkeypatch):
    db = payroll_setup_db
    setup_id = await _new_setup(db, "b")
    v1 = await _publish(db, setup_id, ANCHOR, mask=1)
    v2 = await _publish(db, setup_id, date(2090, 3, 15), mask=2)
    _stub_today(monkeypatch, date(2090, 3, 10))

    versions = await reads.list_versions(db.company_id, setup_id, db.db)

    assert _current_ids(versions) == [v1]
    assert v2 not in _current_ids(versions)


@pytest.mark.asyncio
async def test_only_future_version_means_nothing_is_current(payroll_setup_db, monkeypatch):
    db = payroll_setup_db
    setup_id = await _new_setup(db, "c")
    await _publish(db, setup_id, ANCHOR, mask=1)
    _stub_today(monkeypatch, date(2090, 2, 1))

    versions = await reads.list_versions(db.company_id, setup_id, db.db)

    assert versions
    assert _current_ids(versions) == []


@pytest.mark.asyncio
async def test_later_version_becomes_current_once_it_starts(payroll_setup_db, monkeypatch):
    db = payroll_setup_db
    setup_id = await _new_setup(db, "d")
    await _publish(db, setup_id, ANCHOR, mask=1)
    v2 = await _publish(db, setup_id, date(2090, 3, 15), mask=2)
    _stub_today(monkeypatch, date(2090, 3, 15))

    versions = await reads.list_versions(db.company_id, setup_id, db.db)

    assert _current_ids(versions) == [v2]


@pytest.mark.asyncio
async def test_same_date_replacement_only_terminal_is_current(payroll_setup_db, monkeypatch):
    db = payroll_setup_db
    setup_id = await _new_setup(db, "e")
    v1 = await _publish(db, setup_id, ANCHOR, mask=1)
    v1b = await _publish(db, setup_id, ANCHOR, mask=2, replaces=v1)
    _stub_today(monkeypatch, date(2090, 3, 10))

    versions = await reads.list_versions(db.company_id, setup_id, db.db)

    assert _current_ids(versions) == [v1b]
    assert next(v for v in versions if v["version_id"] == v1)["is_current"] is False


@pytest.mark.asyncio
async def test_version_response_schema_carries_is_current(payroll_setup_db, monkeypatch):
    db = payroll_setup_db
    setup_id = await _new_setup(db, "f")
    await _publish(db, setup_id, ANCHOR, mask=1)
    _stub_today(monkeypatch, date(2090, 3, 10))

    versions = await reads.list_versions(db.company_id, setup_id, db.db)
    payload = VersionResponse.model_validate(versions[0]).model_dump()

    assert payload["is_current"] is True
