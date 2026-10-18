"""Focused tests for `is_current` on the published-version read model.
Currentness is classified only when a Branch context is supplied and uses that
Branch's business date; without context the metadata remains unknown (NULL)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.payroll_setup import reads
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
                branch_id = (await conn.execute(text("""
                    SELECT BranchID FROM core.Branches
                    WHERE CompanyID = :cid ORDER BY BranchID LIMIT 1
                """), {"cid": tenant["companyid"]})).scalar_one()
                yield SimpleNamespace(
                    db=conn, company_id=int(tenant["companyid"]),
                    user_id=int(tenant["userid"]), branch_id=int(branch_id), marker=marker,
                )
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


def _stub_today(monkeypatch, today: date) -> None:
    async def _stub(*args, **kwargs):
        return today

    monkeypatch.setattr(reads, "branch_today", _stub)


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

    versions = await reads.list_versions(db.company_id, setup_id, db.db, branch_id=db.branch_id, user_id=db.user_id)

    assert _current_ids(versions) == [v1]


@pytest.mark.asyncio
async def test_future_version_is_not_current_while_past_one_is(payroll_setup_db, monkeypatch):
    db = payroll_setup_db
    setup_id = await _new_setup(db, "b")
    v1 = await _publish(db, setup_id, ANCHOR, mask=1)
    v2 = await _publish(db, setup_id, date(2090, 3, 15), mask=2)
    _stub_today(monkeypatch, date(2090, 3, 10))

    versions = await reads.list_versions(db.company_id, setup_id, db.db, branch_id=db.branch_id, user_id=db.user_id)

    assert _current_ids(versions) == [v1]
    assert v2 not in _current_ids(versions)


@pytest.mark.asyncio
async def test_only_future_version_means_nothing_is_current(payroll_setup_db, monkeypatch):
    db = payroll_setup_db
    setup_id = await _new_setup(db, "c")
    await _publish(db, setup_id, ANCHOR, mask=1)
    _stub_today(monkeypatch, date(2090, 2, 1))

    versions = await reads.list_versions(db.company_id, setup_id, db.db, branch_id=db.branch_id, user_id=db.user_id)

    assert versions
    assert _current_ids(versions) == []


@pytest.mark.asyncio
async def test_later_version_becomes_current_once_it_starts(payroll_setup_db, monkeypatch):
    db = payroll_setup_db
    setup_id = await _new_setup(db, "d")
    await _publish(db, setup_id, ANCHOR, mask=1)
    v2 = await _publish(db, setup_id, date(2090, 3, 15), mask=2)
    _stub_today(monkeypatch, date(2090, 3, 15))

    versions = await reads.list_versions(db.company_id, setup_id, db.db, branch_id=db.branch_id, user_id=db.user_id)

    assert _current_ids(versions) == [v2]


@pytest.mark.asyncio
async def test_same_date_replacement_only_terminal_is_current(payroll_setup_db, monkeypatch):
    db = payroll_setup_db
    setup_id = await _new_setup(db, "e")
    v1 = await _publish(db, setup_id, ANCHOR, mask=1)
    v1b = await _publish(db, setup_id, ANCHOR, mask=2, replaces=v1)
    _stub_today(monkeypatch, date(2090, 3, 10))

    versions = await reads.list_versions(db.company_id, setup_id, db.db, branch_id=db.branch_id, user_id=db.user_id)

    assert _current_ids(versions) == [v1b]
    assert next(v for v in versions if v["version_id"] == v1)["is_current"] is False


@pytest.mark.asyncio
async def test_version_response_schema_carries_is_current(payroll_setup_db, monkeypatch):
    db = payroll_setup_db
    setup_id = await _new_setup(db, "f")
    await _publish(db, setup_id, ANCHOR, mask=1)
    _stub_today(monkeypatch, date(2090, 3, 10))

    versions = await reads.list_versions(db.company_id, setup_id, db.db, branch_id=db.branch_id, user_id=db.user_id)
    payload = VersionResponse.model_validate(versions[0]).model_dump()

    assert payload["is_current"] is True


@pytest.mark.asyncio
async def test_real_branch_context_classifies_shared_setup_on_kiritimati_and_honolulu_dates(
    payroll_setup_db,
):
    db = payroll_setup_db
    await db.db.execute(text(
        "UPDATE core.Branches SET TimeZoneId = 'Pacific/Kiritimati' WHERE BranchID = :branch_id"
    ), {"branch_id": db.branch_id})
    honolulu_branch_id = int((await db.db.execute(text("""
        INSERT INTO core.Branches
            (CompanyID, BranchCode, BranchName, Status, IsDefault, TimeZoneId)
        VALUES (:company_id, :code, 'Clock test Honolulu', 'Active', FALSE, 'Pacific/Honolulu')
        RETURNING BranchID
    """), {"company_id": db.company_id, "code": f"CVH{db.marker}"})).scalar_one())
    dates = (await db.db.execute(text("""
        SELECT core.fn_BranchToday(:company_id, :kiritimati) AS kiritimati,
               core.fn_BranchToday(:company_id, :honolulu) AS honolulu
    """), {
        "company_id": db.company_id,
        "kiritimati": db.branch_id,
        "honolulu": honolulu_branch_id,
    })).mappings().one()
    assert (dates["kiritimati"] - dates["honolulu"]).days == 1

    setup_id = await _new_setup(db, "world-clock")
    async def publish_at(effective: date, mask: int) -> int:
        draft_id = await create_draft(
            db.company_id, db.user_id, setup_id, db.db,
            payroll_frequency="Custom", anchor_start_date=dates["honolulu"],
            custom_interval_days=1, normal_days_off_mask=mask,
        )
        return await publish_version(
            db.company_id, db.user_id, setup_id, draft_id, effective, db.db,
        )

    v1 = await publish_at(dates["honolulu"], 1)
    v2 = await publish_at(dates["kiritimati"], 2)
    honolulu = await reads.list_versions(
        db.company_id, setup_id, db.db,
        branch_id=honolulu_branch_id, user_id=db.user_id,
    )
    kiritimati = await reads.list_versions(
        db.company_id, setup_id, db.db,
        branch_id=db.branch_id, user_id=db.user_id,
    )
    assert _current_ids(honolulu) == [v1]
    assert _current_ids(kiritimati) == [v2]

    without_context = await reads.list_versions(db.company_id, setup_id, db.db)
    assert without_context
    assert all(row["is_current"] is None for row in without_context)
    assert all(row["reference_date"] is None for row in without_context)
    assert all(row["reference_branch_id"] is None for row in without_context)
