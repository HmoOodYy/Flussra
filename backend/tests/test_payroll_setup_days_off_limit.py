"""Three-day Normal Days Off product limit: pure, service, API, and DB coverage."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg2
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.payroll_setup import payroll_policy
from app.payroll_setup.chronology import DaysOffLimitError, Schedule, validate_normal_days_off_mask
from app.payroll_setup.errors import PolicyError
from app.payroll_setup.payroll_policy import create_draft, create_setup, edit_draft, publish_version
from tests.schema_helpers import _alembic, _schema_database

_MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "migrations"
_SQL_FILE = _MIGRATIONS_DIR / "sql" / "0069_payroll_setup_days_off_limit.sql"
_LIMIT_MIGRATION_FILE = _MIGRATIONS_DIR / "versions" / "0095_payroll_setup_three_days_off_limit.py"

# Masks by number of weekdays marked as normal days off.
_THREE_DAY_MASK = 73  # Sun, Wed, Sat
_FOUR_DAY_MASK = 15
_ALL_DAYS_MASK = 127


def _precheck_statement() -> str:
    import sys
    if str(_MIGRATIONS_DIR) not in sys.path:
        sys.path.insert(0, str(_MIGRATIONS_DIR))
    from utils import statements_from_file
    return statements_from_file(_SQL_FILE)[0]


def _downgrade_precheck_statement() -> str:
    import importlib.util
    spec = importlib.util.spec_from_file_location("days_off_limit_migration", _LIMIT_MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._DOWNGRADE_PRECHECK


@pytest_asyncio.fixture
async def days_off_db(test_database_url):
    """Seed the DEMO tenant and use a rollback-only transaction per test."""
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


async def _new_setup(db, suffix: str = "A") -> int:
    return await create_setup(
        db.company_id, db.user_id, f"DOL_{db.marker}_{suffix}",
        f"Days off limit {suffix}", db.db,
    )


async def _draft_count(db, setup_id: int) -> int:
    return (await db.db.execute(text(
        "SELECT COUNT(*) FROM payroll.PayrollSetupVersions WHERE PayrollSetupID = :sid"
    ), {"sid": setup_id})).scalar_one()


async def _stored_mask(db, version_id: int) -> int:
    return (await db.db.execute(text(
        "SELECT NormalDaysOffMask FROM payroll.PayrollSetupVersions "
        "WHERE PayrollSetupVersionID = :vid"
    ), {"vid": version_id})).scalar_one()


# --------------------------------------------------------------------------- #
# (a) Pure: validate_normal_days_off_mask / Schedule
# --------------------------------------------------------------------------- #

def test_validate_normal_days_off_mask_accepts_zero_and_single_day_masks():
    for mask in (0, 1, 2, 4, 8, 16, 32, 64):
        validate_normal_days_off_mask(mask)


def test_validate_normal_days_off_mask_accepts_two_and_three_day_masks():
    for mask in (3, 65, 7, _THREE_DAY_MASK, 112):
        validate_normal_days_off_mask(mask)


def test_validate_normal_days_off_mask_rejects_four_or_more_days():
    for mask in (_FOUR_DAY_MASK, 31, _ALL_DAYS_MASK):
        with pytest.raises(DaysOffLimitError, match="at most three days"):
            validate_normal_days_off_mask(mask)


def test_validate_normal_days_off_mask_rejects_out_of_range_as_plain_value_error():
    for mask in (-1, 128):
        with pytest.raises(ValueError) as error:
            validate_normal_days_off_mask(mask)
        assert not isinstance(error.value, DaysOffLimitError)


def test_schedule_accepts_two_and_three_day_masks():
    Schedule("Week", date(2090, 1, 1), None, 65)
    Schedule("Week", date(2090, 1, 1), None, _THREE_DAY_MASK)


def test_schedule_rejects_masks_over_the_limit():
    with pytest.raises(DaysOffLimitError):
        Schedule("Week", date(2090, 1, 1), None, _FOUR_DAY_MASK)
    with pytest.raises(DaysOffLimitError):
        Schedule("Week", date(2090, 1, 1), None, _ALL_DAYS_MASK)


# --------------------------------------------------------------------------- #
# (b) Service: create_draft
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_create_draft_accepts_masks_with_at_most_three_days(days_off_db):
    db = days_off_db
    setup_id = await _new_setup(db, "CREATE_OK")
    for mask in (0, 1, 65, _THREE_DAY_MASK):
        draft_id = await create_draft(
            db.company_id, db.user_id, setup_id, db.db,
            payroll_frequency="Week", anchor_start_date=date(2090, 1, 1),
            normal_days_off_mask=mask,
        )
        assert await _stored_mask(db, draft_id) == mask


@pytest.mark.asyncio
async def test_create_draft_rejects_masks_over_the_limit_without_inserting(days_off_db):
    db = days_off_db
    setup_id = await _new_setup(db, "CREATE_BAD")
    for mask in (_FOUR_DAY_MASK, _ALL_DAYS_MASK):
        before = await _draft_count(db, setup_id)
        with pytest.raises(PolicyError) as error:
            await create_draft(
                db.company_id, db.user_id, setup_id, db.db,
                payroll_frequency="Week", anchor_start_date=date(2090, 1, 1),
                normal_days_off_mask=mask,
            )
        assert error.value.code == "INVALID_NORMAL_DAYS_OFF"
        assert await _draft_count(db, setup_id) == before


# --------------------------------------------------------------------------- #
# (c) Service: edit_draft
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_edit_draft_rejects_masks_over_the_limit_leaving_row_unchanged(days_off_db):
    db = days_off_db
    setup_id = await _new_setup(db, "EDIT")
    draft_id = await create_draft(
        db.company_id, db.user_id, setup_id, db.db,
        payroll_frequency="Week", anchor_start_date=date(2090, 1, 1),
        normal_days_off_mask=1,
    )
    for mask in (_FOUR_DAY_MASK, _ALL_DAYS_MASK):
        with pytest.raises(PolicyError) as error:
            await edit_draft(
                db.company_id, db.user_id, setup_id, draft_id, db.db,
                payroll_frequency="Week", anchor_start_date=date(2090, 1, 1),
                custom_interval_days=None, normal_days_off_mask=mask,
            )
        assert error.value.code == "INVALID_NORMAL_DAYS_OFF"
        assert await _stored_mask(db, draft_id) == 1

    await edit_draft(
        db.company_id, db.user_id, setup_id, draft_id, db.db,
        payroll_frequency="Week", anchor_start_date=date(2090, 1, 1),
        custom_interval_days=None, normal_days_off_mask=_THREE_DAY_MASK,
    )
    assert await _stored_mask(db, draft_id) == _THREE_DAY_MASK


# --------------------------------------------------------------------------- #
# (d) API
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_api_draft_create_and_update_enforce_days_off_limit(client, auth_token):
    headers = {"Authorization": f"Bearer {auth_token}"}
    created = await client.post("/payroll-setup/setups", headers=headers, json={
        "setup_code": "DOLAPI_" + uuid4().hex[:8], "setup_name": "Days off API",
    })
    assert created.status_code == 201, created.text
    sid = created.json()["setup_id"]
    schedule = {"payroll_frequency": "Week", "anchor_start_date": "2090-01-01",
                "custom_interval_days": None, "normal_days_off_mask": _FOUR_DAY_MASK}

    rejected = await client.post(
        f"/payroll-setup/setups/{sid}/drafts", headers=headers, json=schedule,
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["detail"]["code"] == "INVALID_NORMAL_DAYS_OFF"

    accepted = await client.post(
        f"/payroll-setup/setups/{sid}/drafts", headers=headers,
        json={**schedule, "normal_days_off_mask": _THREE_DAY_MASK},
    )
    assert accepted.status_code == 201, accepted.text
    did = accepted.json()["version_id"]

    put_rejected = await client.put(
        f"/payroll-setup/setups/{sid}/drafts/{did}", headers=headers,
        json={**schedule, "normal_days_off_mask": _ALL_DAYS_MASK},
    )
    assert put_rejected.status_code == 422, put_rejected.text
    assert put_rejected.json()["detail"]["code"] == "INVALID_NORMAL_DAYS_OFF"

    put_accepted = await client.put(
        f"/payroll-setup/setups/{sid}/drafts/{did}", headers=headers,
        json={**schedule, "normal_days_off_mask": 0},
    )
    assert put_accepted.status_code == 200, put_accepted.text


# --------------------------------------------------------------------------- #
# (e) Publication path
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_publish_valid_mask_succeeds_and_schedule_builder_rejects_invalid_mask(
    days_off_db,
):
    db = days_off_db
    setup_id = await _new_setup(db, "PUBLISH")
    draft_id = await create_draft(
        db.company_id, db.user_id, setup_id, db.db,
        payroll_frequency="Week", anchor_start_date=date(2090, 1, 1),
        normal_days_off_mask=65,
    )
    version_id = await publish_version(
        db.company_id, db.user_id, setup_id, draft_id, date(2090, 1, 1), db.db,
    )
    assert await _stored_mask(db, version_id) == 65

    with pytest.raises(PolicyError) as error:
        payroll_policy._schedule("Week", date(2090, 1, 1), None, _FOUR_DAY_MASK)
    assert error.value.code == "INVALID_NORMAL_DAYS_OFF"


# --------------------------------------------------------------------------- #
# (f) Raw persistence protection
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_raw_sql_cannot_bypass_the_check_constraint(direct_db):
    tenant = (await direct_db.execute(text("""
        SELECT c.CompanyID, u.UserID
        FROM core.Companies c
        JOIN sec.Users u ON u.CompanyID = c.CompanyID
        WHERE c.CompanyCode = 'DEMO' AND u.Username = 'admin'
    """))).mappings().one()
    company_id = int(tenant["companyid"])
    user_id = int(tenant["userid"])
    suffix = uuid4().hex[:10]
    setup_id = (await direct_db.execute(text("""
        INSERT INTO payroll.PayrollSetups (CompanyID, SetupCode, SetupName, CreatedByUserID)
        VALUES (:cid, :code, :name, :uid) RETURNING PayrollSetupID
    """), {"cid": company_id, "code": "DOLRAW_" + suffix, "name": "Raw guard",
           "uid": user_id})).scalar_one()
    draft_id = (await direct_db.execute(text("""
        INSERT INTO payroll.PayrollSetupVersions
            (CompanyID, PayrollSetupID, LifecycleState, PayrollFrequency,
             AnchorStartDate, NormalDaysOffMask, CreatedByUserID)
        VALUES (:cid, :sid, 'Draft', 'Week', '2090-01-01', 0, :uid)
        RETURNING PayrollSetupVersionID
    """), {"cid": company_id, "sid": setup_id, "uid": user_id})).scalar_one()

    with pytest.raises(IntegrityError, match="ck_payrollsetupversions_mask"):
        await direct_db.execute(text("""
            UPDATE payroll.PayrollSetupVersions SET NormalDaysOffMask = 15
            WHERE PayrollSetupVersionID = :vid
        """), {"vid": draft_id})

    with pytest.raises(IntegrityError, match="ck_payrollsetupversions_mask"):
        await direct_db.execute(text("""
            INSERT INTO payroll.PayrollSetupVersions
                (CompanyID, PayrollSetupID, LifecycleState, PayrollFrequency,
                 AnchorStartDate, NormalDaysOffMask, CreatedByUserID)
            VALUES (:cid, :sid, 'Draft', 'Week', '2090-01-01', 127, :uid)
        """), {"cid": company_id, "sid": setup_id, "uid": user_id})

    await direct_db.execute(text("""
        UPDATE payroll.PayrollSetupVersions SET NormalDaysOffMask = 73
        WHERE PayrollSetupVersionID = :vid
    """), {"vid": draft_id})
    stored = (await direct_db.execute(text(
        "SELECT NormalDaysOffMask FROM payroll.PayrollSetupVersions "
        "WHERE PayrollSetupVersionID = :vid"
    ), {"vid": draft_id})).scalar_one()
    assert stored == _THREE_DAY_MASK

    # Restore a two-day mask so the shared test database holds no live row above the
    # two-day limit that the historical migration prechecks assert against.
    await direct_db.execute(text("""
        UPDATE payroll.PayrollSetupVersions SET NormalDaysOffMask = 65
        WHERE PayrollSetupVersionID = :vid
    """), {"vid": draft_id})


# --------------------------------------------------------------------------- #
# (g) Migration precheck fail-closed
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_migration_precheck_fails_closed_on_violating_live_drafts(test_database_url):
    precheck_stmt = _precheck_statement()
    engine = create_async_engine(test_database_url, echo=False)
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
                suffix = uuid4().hex[:10]
                setup_id = (await conn.execute(text("""
                    INSERT INTO payroll.PayrollSetups
                        (CompanyID, SetupCode, SetupName, CreatedByUserID)
                    VALUES (:cid, :code, :name, :uid) RETURNING PayrollSetupID
                """), {"cid": company_id, "code": "DOLPRE_" + suffix, "name": "Precheck",
                       "uid": user_id})).scalar_one()
                await conn.execute(text(
                    "ALTER TABLE payroll.PayrollSetupVersions "
                    "DROP CONSTRAINT ck_PayrollSetupVersions_Mask"
                ))
                draft_id = (await conn.execute(text("""
                    INSERT INTO payroll.PayrollSetupVersions
                        (CompanyID, PayrollSetupID, LifecycleState, PayrollFrequency,
                         AnchorStartDate, NormalDaysOffMask, CreatedByUserID)
                    VALUES (:cid, :sid, 'Draft', 'Week', '2090-01-01', 15, :uid)
                    RETURNING PayrollSetupVersionID
                """), {"cid": company_id, "sid": setup_id, "uid": user_id})).scalar_one()

                with pytest.raises(Exception) as error:
                    await conn.execute(text(precheck_stmt))
                message = str(error.value)
                assert "payroll_setup_days_off_limit_violation" in message
                assert f"PayrollSetupVersionID={draft_id}" in message
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_precheck_passes_cleanly_with_no_violations(test_database_url):
    precheck_stmt = _precheck_statement()
    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            try:
                await conn.execute(text(precheck_stmt))
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_precheck_refuses_live_three_day_versions(test_database_url):
    precheck_stmt = _downgrade_precheck_statement()
    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            try:
                await conn.execute(text(precheck_stmt))

                tenant = (await conn.execute(text("""
                    SELECT c.CompanyID, u.UserID
                    FROM core.Companies c
                    JOIN sec.Users u ON u.CompanyID = c.CompanyID
                    WHERE c.CompanyCode = 'DEMO' AND u.Username = 'admin'
                """))).mappings().one()
                company_id = int(tenant["companyid"])
                user_id = int(tenant["userid"])
                suffix = uuid4().hex[:10]
                setup_id = (await conn.execute(text("""
                    INSERT INTO payroll.PayrollSetups
                        (CompanyID, SetupCode, SetupName, CreatedByUserID)
                    VALUES (:cid, :code, :name, :uid) RETURNING PayrollSetupID
                """), {"cid": company_id, "code": "DOLDOWN_" + suffix, "name": "Downgrade",
                       "uid": user_id})).scalar_one()
                draft_id = (await conn.execute(text("""
                    INSERT INTO payroll.PayrollSetupVersions
                        (CompanyID, PayrollSetupID, LifecycleState, PayrollFrequency,
                         AnchorStartDate, NormalDaysOffMask, CreatedByUserID)
                    VALUES (:cid, :sid, 'Draft', 'Week', '2090-01-01', :mask, :uid)
                    RETURNING PayrollSetupVersionID
                """), {"cid": company_id, "sid": setup_id, "mask": _THREE_DAY_MASK,
                       "uid": user_id})).scalar_one()

                nested = await conn.begin_nested()
                with pytest.raises(Exception) as error:
                    await conn.execute(text(precheck_stmt))
                await nested.rollback()
                message = str(error.value)
                assert "payroll_setup_days_off_limit_downgrade_violation" in message
                assert f"PayrollSetupVersionID={draft_id}" in message
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


def _mask_constraint_definition(cur) -> str:
    cur.execute("""
        SELECT pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conname = 'ck_payrollsetupversions_mask'
          AND conrelid = 'payroll.payrollsetupversions'::regclass
    """)
    return cur.fetchone()[0]


def test_alembic_upgrade_allows_three_days_and_downgrade_fails_closed_on_them(schema_databases):
    with _schema_database(schema_databases, "days_off_limit") as (env, dsn):
        with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
            assert "<= 3" in _mask_constraint_definition(cur)

        downgraded = _alembic(env, "downgrade", "0094")
        assert downgraded.returncode == 0, downgraded.stderr
        with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
            definition = _mask_constraint_definition(cur)
            assert "<= 2" in definition and "discardedatutc IS NOT NULL" in definition

        upgraded = _alembic(env, "upgrade", "0095")
        assert upgraded.returncode == 0, upgraded.stderr

        with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
            code = uuid4().hex[:12]
            cur.execute("""INSERT INTO core.companies (companycode, companyname, status, issuspended)
                VALUES (%s, 'Days off limit', 'Active', FALSE) RETURNING companyid""", (code,))
            company_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO payroll.payrollsetups (companyid, setupcode, setupname) "
                "VALUES (%s, %s, 'Days off limit') RETURNING payrollsetupid", (company_id, code),
            )
            setup_id = cur.fetchone()[0]
            cur.execute("""INSERT INTO payroll.payrollsetupversions
                (companyid, payrollsetupid, lifecyclestate, payrollfrequency, anchorstartdate, normaldaysoffmask)
                VALUES (%s, %s, 'Draft', 'Week', '2090-01-01', %s)""",
                (company_id, setup_id, _THREE_DAY_MASK))
            conn.commit()

        blocked = _alembic(env, "downgrade", "0094")
        assert blocked.returncode != 0
        assert "payroll_setup_days_off_limit_downgrade_violation" in blocked.stderr
        with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
            assert "<= 3" in _mask_constraint_definition(cur)
            cur.execute("SELECT version_num FROM public.alembic_version")
            assert cur.fetchone()[0] == "0095"
