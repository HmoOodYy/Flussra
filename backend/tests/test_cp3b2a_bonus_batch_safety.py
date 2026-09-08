"""
CP-3B2a: Bonus Batch Safety Foundation — full test suite.

This unit adds NO batch endpoint. It adds the safety scaffolding a future
transactional bonus batch (CP-3B2b) will depend on:

  - payroll.PayrollPeriods.BonusDataRevision (period-level concurrency token)
  - payroll.PayrollBonusBatchRequests (durable idempotency/correlation table,
    foundation only — nothing writes to it yet)
  - a DB-level ownership trigger on PayrollBonusEvents
  - atomic revision predicates on single-event PATCH/DELETE
  - optional audit correlation_id support on _write_line_audit
  - bonus_data_revision exposed on GET /bonuses/summary

Covers:
  - Migration/schema: 0059 objects exist with correct shape.
  - DB ownership hardening: mismatched Company/Branch inserts/updates rejected.
  - Summary revision exposure: starts at 0, present in response.
  - Single-event revision bumps: create/update/void each +1 exactly once.
  - Idempotent void does not double-bump.
  - Atomic update predicate: stale data_revision -> 409, no side effects.
  - Single-event writes invalidate a previously-read summary revision.
  - Audit correlation_id: optional, backward compatible, persisted when supplied.
  - Audit-failure rollback: event mutation and revision bump both roll back.
  - Regression: CP-3A / CP-3B1 tests remain green (run separately, not here).

Dates: 2094-* — isolated year.

Run from backend/:
    python -B -m pytest tests/test_cp3b2a_bonus_batch_safety.py -v -p no:cacheprovider
"""
import datetime
import itertools
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
import httpx
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncConnection

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_d = datetime.date(2094, 6, 1)
_BASE_MONDAY = _d + datetime.timedelta(days=(7 - _d.weekday()) % 7)
_CTR = itertools.count(0)
_RUN_ID = uuid.uuid4().hex[:8]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _week(offset: int = 0) -> tuple[datetime.date, datetime.date]:
    n = next(_CTR) + offset
    start = _BASE_MONDAY + datetime.timedelta(weeks=n)
    return start, start + datetime.timedelta(days=6)


async def _insert_period_db(
    db: AsyncConnection,
    branch_id: int,
    start: datetime.date,
    end: datetime.date,
    status: str = "Open",
) -> int:
    """Insert an isolated CP3B2A period without deleting historical fixtures."""
    if status == "Open":
        await db.execute(_text("""
            UPDATE payroll.payrollperiods
            SET status = 'Cancelled'
            WHERE branchid = :bid
              AND periodcode LIKE :run_prefix
              AND status = 'Open'
        """), {"bid": branch_id, "run_prefix": f"CP3B2A-{_RUN_ID}-%"})
    code = f"CP3B2A-{_RUN_ID}-{branch_id}-{start.isoformat()}"
    r = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, :status, :code, :name, 'Week', :start, :end)
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "status": status, "code": code,
         "name": f"CP3B2A {start}", "start": start, "end": end},
    )).mappings().first()
    await db.commit()
    assert r is not None
    return r["payrollperiodid"]


async def _cancel_period_db(db: AsyncConnection, period_id: int) -> None:
    await db.execute(_text(
        "ALTER TABLE payroll.payrollperiods DISABLE TRIGGER trg_period_status_revert"
    ))
    await db.execute(
        _text("UPDATE payroll.payrollperiods SET status = 'Cancelled', "
              "currentreturnreviewitemid = NULL WHERE payrollperiodid = :pid"),
        {"pid": period_id},
    )
    await db.execute(_text(
        "ALTER TABLE payroll.payrollperiods ENABLE TRIGGER trg_period_status_revert"
    ))
    await db.commit()


async def _seed_snapshot(
    db: AsyncConnection,
    branch_id: int,
    period_id: int,
    entries: list[dict],
) -> None:
    await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiodeligibilitysnapshots
                (payrollperiodid, companyid, branchid, snapshotsource,
                 createdatutc, updatedatutc)
            VALUES (:pid, 1, :bid, 'Generated', NOW(), NOW())
            ON CONFLICT (payrollperiodid) DO NOTHING
        """),
        {"pid": period_id, "bid": branch_id},
    )
    for e in entries:
        await db.execute(
            _text("""
                INSERT INTO payroll.payrollperioddrivereligibility
                    (companyid, branchid, payrollperiodid, driverid,
                     drivercodesnapshot, drivernamesnapshot,
                     iseligibleforperiod, eligibilityreasoncode, snapshotsource,
                     createdatutc, updatedatutc)
                VALUES (1, :bid, :pid, :did, :code, :name,
                        TRUE, 'Active', 'Generated', NOW(), NOW())
                ON CONFLICT (payrollperiodid, driverid) DO NOTHING
            """),
            {"bid": branch_id, "pid": period_id, "did": e["driver_id"],
             "code": e["code"], "name": e["name"]},
        )
    await db.commit()


async def _post_bonus(
    client: httpx.AsyncClient, token: str, period_id: int, driver_id: int, amount: str,
) -> int:
    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": driver_id, "amount": amount},
        headers=_auth(token),
    )
    assert r.status_code == 201, f"Bonus create failed: {r.text}"
    return r.json()["bonus_event_id"]


async def _get_summary(client: httpx.AsyncClient, token: str, period_id: int) -> httpx.Response:
    return await client.get(
        f"/payroll/periods/{period_id}/bonuses/summary",
        headers=_auth(token),
    )


async def _get_bonus_data_revision(db: AsyncConnection, period_id: int) -> int:
    row = (await db.execute(
        _text("SELECT bonusdatarevision FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
        {"pid": period_id},
    )).first()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def cp3b2a_branch_id(session_client: httpx.AsyncClient, auth_token: str) -> int:
    resp = await session_client.get("/core/branches", headers=_auth(auth_token))
    assert resp.status_code == 200, resp.text
    for branch in resp.json():
        if branch["branch_code"] == "PAYTEST":
            return branch["branch_id"]
    raise AssertionError("PAYTEST branch not found")


@pytest_asyncio.fixture
async def cp3b2a_driver_id(
    session_client: httpx.AsyncClient, auth_token: str, cp3b2a_branch_id: int,
) -> int:
    driver_code = f"CP3B2A-DRV-{_BASE_MONDAY.isoformat()}"
    r_list = await session_client.get("/core/drivers", headers=_auth(auth_token))
    if r_list.status_code == 200:
        for d in r_list.json():
            if d.get("driver_code") == driver_code:
                return d["driver_id"]
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": cp3b2a_branch_id,
            "full_name": "CP3B2A Bonus Driver",
            "driver_code": driver_code,
        },
        headers=_auth(auth_token),
    )
    assert resp.status_code == 201, f"Driver seed failed: {resp.text}"
    return resp.json()["driver_id"]


async def _snapshot_entry(driver_id: int) -> dict:
    return {"driver_id": driver_id, "code": "CP3B2A-DRV", "name": "CP3B2A Bonus Driver"}


# ---------------------------------------------------------------------------
# 1. Migration / schema
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_alembic_head_is_current() -> None:
    # CP-3B2a introduced migration 0059; CP-3B2b later added 0060, which is now
    # the head. This test only asserts the chain is linear and that 0059 is
    # applied (i.e. head is 0059 or a later revision that builds on it).
    import subprocess, sys, pathlib
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        capture_output=True, text=True,
        cwd=str(pathlib.Path(__file__).parent.parent.parent),
    )
    lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, f"Expected exactly one alembic head, got {len(lines)}: {result.stdout}"
    head_rev = lines[0].split()[0]
    assert head_rev >= "0059", f"Expected head >= 0059, got: {lines[0]}"


@pytest.mark.asyncio
async def test_bonus_data_revision_column_exists_defaults_zero(db_conn: AsyncConnection) -> None:
    row = (await db_conn.execute(
        _text("""
            SELECT column_name, column_default, is_nullable
            FROM   information_schema.columns
            WHERE  table_schema = 'payroll'
              AND  table_name   = 'payrollperiods'
              AND  column_name  = 'bonusdatarevision'
        """)
    )).mappings().first()
    assert row is not None, "PayrollPeriods.BonusDataRevision column missing"
    assert row["is_nullable"] == "NO"
    assert "0" in (row["column_default"] or "")


@pytest.mark.asyncio
async def test_bonus_batch_requests_table_exists(db_conn: AsyncConnection) -> None:
    rows = (await db_conn.execute(
        _text("""
            SELECT column_name
            FROM   information_schema.columns
            WHERE  table_schema = 'payroll'
              AND  table_name   = 'payrollbonusbatchrequests'
        """)
    )).scalars().all()
    required = {
        "payrollbonusbatchrequestid", "companyid", "branchid", "payrollperiodid",
        "idempotencykey", "requesthash", "requestpayloadjson", "batchcorrelationid",
        "expectedbonusdatarevision", "resultbonusdatarevision",
        "createdeventids", "createdeventcount", "status",
        "createdbyuserid", "createdatutc", "appliedatutc",
    }
    missing = required - set(rows)
    assert not missing, f"Missing columns in payrollbonusbatchrequests: {missing}"


@pytest.mark.asyncio
async def test_bonus_batch_requests_unique_idempotency_index(db_conn: AsyncConnection) -> None:
    row = (await db_conn.execute(
        _text("""
            SELECT indexname FROM pg_indexes
            WHERE  schemaname = 'payroll'
              AND  tablename  = 'payrollbonusbatchrequests'
              AND  indexname  = 'ux_payrollbonusbatchrequests_idempotency'
        """)
    )).first()
    assert row is not None, "Unique idempotency index missing"


@pytest.mark.asyncio
async def test_bonus_batch_requests_unique_correlation_index(db_conn: AsyncConnection) -> None:
    row = (await db_conn.execute(
        _text("""
            SELECT indexname FROM pg_indexes
            WHERE  schemaname = 'payroll'
              AND  tablename  = 'payrollbonusbatchrequests'
              AND  indexname  = 'ux_payrollbonusbatchrequests_correlation'
        """)
    )).first()
    assert row is not None, "Unique BatchCorrelationID index missing"


@pytest.mark.asyncio
async def test_bonus_events_batch_correlation_index_exists(db_conn: AsyncConnection) -> None:
    row = (await db_conn.execute(
        _text("""
            SELECT indexname FROM pg_indexes
            WHERE  schemaname = 'payroll'
              AND  tablename  = 'payrollbonusevents'
              AND  indexname  = 'ix_payrollbonusevents_batchcorrelation'
        """)
    )).first()
    assert row is not None, "PayrollBonusEvents.BatchCorrelationID index missing"


# ---------------------------------------------------------------------------
# 2. DB ownership hardening
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mismatched_branch_insert_rejected(
    db_conn: AsyncConnection,
    cp3b2a_branch_id: int,
    cp3b2a_driver_id: int,
    hq_branch_id: int,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2a_branch_id, *_week())
    with pytest.raises(Exception) as exc_info:
        await db_conn.execute(
            _text("""
                INSERT INTO payroll.payrollbonusevents
                    (companyid, branchid, payrollperiodid, driverid,
                     amount, status, createdbyuserid, createdatutc, datarevision)
                VALUES (1, :bid, :pid, :did, 50.00, 'Active', 1, NOW(), 1)
            """),
            {"bid": hq_branch_id, "pid": period_id, "did": cp3b2a_driver_id},
        )
    assert "PayrollBonusEvents" in str(exc_info.value) or "check_violation" in str(exc_info.value).lower() \
        or "belongs to company/branch" in str(exc_info.value)

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_mismatched_company_insert_rejected(
    db_conn: AsyncConnection,
    cp3b2a_branch_id: int,
    cp3b2a_driver_id: int,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2a_branch_id, *_week())
    with pytest.raises(Exception):
        await db_conn.execute(
            _text("""
                INSERT INTO payroll.payrollbonusevents
                    (companyid, branchid, payrollperiodid, driverid,
                     amount, status, createdbyuserid, createdatutc, datarevision)
                VALUES (999, :bid, :pid, :did, 50.00, 'Active', 1, NOW(), 1)
            """),
            {"bid": cp3b2a_branch_id, "pid": period_id, "did": cp3b2a_driver_id},
        )

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_valid_insert_still_succeeds(
    db_conn: AsyncConnection,
    cp3b2a_branch_id: int,
    cp3b2a_driver_id: int,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2a_branch_id, *_week())
    await db_conn.execute(
        _text("""
            INSERT INTO payroll.payrollbonusevents
                (companyid, branchid, payrollperiodid, driverid,
                 amount, status, createdbyuserid, createdatutc, datarevision)
            VALUES (1, :bid, :pid, :did, 50.00, 'Active', 1, NOW(), 1)
        """),
        {"bid": cp3b2a_branch_id, "pid": period_id, "did": cp3b2a_driver_id},
    )
    await db_conn.commit()
    row = (await db_conn.execute(
        _text("SELECT COUNT(*) FROM payroll.payrollbonusevents WHERE payrollperiodid = :pid"),
        {"pid": period_id},
    )).first()
    assert row[0] == 1

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 3. Summary revision exposure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_includes_bonus_data_revision_initially_zero(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b2a_branch_id: int,
    cp3b2a_driver_id: int,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2a_branch_id, *_week())
    await _seed_snapshot(db_conn, cp3b2a_branch_id, period_id, [await _snapshot_entry(cp3b2a_driver_id)])

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["bonus_data_revision"] == 0
    # Existing zero-inclusive/branch-scoped behavior must remain unchanged.
    assert len(summary["drivers"]) == 1
    assert summary["eligibility_source"] == "PeriodEligibilitySnapshot"

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 4. Single-event revision bumps
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_bumps_revision_by_one(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b2a_branch_id: int,
    cp3b2a_driver_id: int,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2a_branch_id, *_week())
    before = await _get_bonus_data_revision(db_conn, period_id)
    await _post_bonus(client, auth_token, period_id, cp3b2a_driver_id, "50.00")
    after = await _get_bonus_data_revision(db_conn, period_id)
    assert after == before + 1

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_update_bumps_revision_by_one(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b2a_branch_id: int,
    cp3b2a_driver_id: int,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2a_branch_id, *_week())
    event_id = await _post_bonus(client, auth_token, period_id, cp3b2a_driver_id, "50.00")
    before = await _get_bonus_data_revision(db_conn, period_id)

    r = await client.patch(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        json={"amount": "75.00"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 200, r.text
    after = await _get_bonus_data_revision(db_conn, period_id)
    assert after == before + 1

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_void_active_bumps_revision_by_one(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b2a_branch_id: int,
    cp3b2a_driver_id: int,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2a_branch_id, *_week())
    event_id = await _post_bonus(client, auth_token, period_id, cp3b2a_driver_id, "50.00")
    before = await _get_bonus_data_revision(db_conn, period_id)

    r = await client.delete(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        headers=_auth(auth_token),
    )
    assert r.status_code == 200, r.text
    after = await _get_bonus_data_revision(db_conn, period_id)
    assert after == before + 1

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_idempotent_void_does_not_double_bump(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b2a_branch_id: int,
    cp3b2a_driver_id: int,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2a_branch_id, *_week())
    event_id = await _post_bonus(client, auth_token, period_id, cp3b2a_driver_id, "50.00")

    r1 = await client.delete(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        headers=_auth(auth_token),
    )
    assert r1.status_code == 200, r1.text
    after_first = await _get_bonus_data_revision(db_conn, period_id)

    r2 = await client.delete(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        headers=_auth(auth_token),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "Voided"
    after_second = await _get_bonus_data_revision(db_conn, period_id)
    assert after_second == after_first, "Idempotent void must not bump revision again"

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_failed_create_does_not_bump_revision(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b2a_branch_id: int,
    cp3b2a_driver_id: int,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2a_branch_id, *_week())
    before = await _get_bonus_data_revision(db_conn, period_id)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3b2a_driver_id, "amount": "0.00"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 422, r.text
    after = await _get_bonus_data_revision(db_conn, period_id)
    assert after == before

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_failed_update_does_not_bump_revision(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b2a_branch_id: int,
    cp3b2a_driver_id: int,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2a_branch_id, *_week())
    event_id = await _post_bonus(client, auth_token, period_id, cp3b2a_driver_id, "50.00")
    before = await _get_bonus_data_revision(db_conn, period_id)

    r = await client.patch(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        json={"amount": "75.00", "data_revision": 999},
        headers=_auth(auth_token),
    )
    assert r.status_code == 409, r.text
    after = await _get_bonus_data_revision(db_conn, period_id)
    assert after == before

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_failed_void_wrong_period_does_not_bump_revision(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b2a_branch_id: int,
    cp3b2a_driver_id: int,
) -> None:
    """Voiding a bonus event through a period ID it doesn't belong to must be
    rejected (here via the Draft-period status gate, since only one Open
    period per branch is allowed) and must not bump either period's revision."""
    period_id = await _insert_period_db(db_conn, cp3b2a_branch_id, *_week())
    other_period_id = await _insert_period_db(
        db_conn, cp3b2a_branch_id, *_week(), status="Draft"
    )
    event_id = await _post_bonus(client, auth_token, period_id, cp3b2a_driver_id, "50.00")
    before = await _get_bonus_data_revision(db_conn, other_period_id)

    r = await client.delete(
        f"/payroll/periods/{other_period_id}/bonuses/{event_id}",
        headers=_auth(auth_token),
    )
    assert r.status_code == 422, r.text
    after = await _get_bonus_data_revision(db_conn, other_period_id)
    assert after == before

    await _cancel_period_db(db_conn, period_id)
    await _cancel_period_db(db_conn, other_period_id)


# ---------------------------------------------------------------------------
# 5. Atomic update predicate / stale revision handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_with_correct_revision_succeeds(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b2a_branch_id: int,
    cp3b2a_driver_id: int,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2a_branch_id, *_week())
    event_id = await _post_bonus(client, auth_token, period_id, cp3b2a_driver_id, "50.00")

    r = await client.patch(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        json={"amount": "60.00", "data_revision": 1},
        headers=_auth(auth_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["amount"] == "60.00"
    assert r.json()["data_revision"] == 2

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_patch_with_stale_revision_conflicts_no_side_effects(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b2a_branch_id: int,
    cp3b2a_driver_id: int,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2a_branch_id, *_week())
    event_id = await _post_bonus(client, auth_token, period_id, cp3b2a_driver_id, "50.00")
    before_period_rev = await _get_bonus_data_revision(db_conn, period_id)

    r = await client.patch(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        json={"amount": "999.00", "data_revision": 5},
        headers=_auth(auth_token),
    )
    assert r.status_code == 409, r.text

    # Neither the event nor the period revision changed.
    row = (await db_conn.execute(
        _text("SELECT amount, datarevision FROM payroll.payrollbonusevents WHERE payrollbonuseventid = :id"),
        {"id": event_id},
    )).mappings().first()
    assert float(row["amount"]) == 50.00
    assert int(row["datarevision"]) == 1
    after_period_rev = await _get_bonus_data_revision(db_conn, period_id)
    assert after_period_rev == before_period_rev

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 6. Single-event writes invalidate a previously-read summary revision
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_create_invalidates_summary_revision(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b2a_branch_id: int,
    cp3b2a_driver_id: int,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2a_branch_id, *_week())
    await _seed_snapshot(db_conn, cp3b2a_branch_id, period_id, [await _snapshot_entry(cp3b2a_driver_id)])

    r1 = await _get_summary(client, auth_token, period_id)
    rev_before = r1.json()["bonus_data_revision"]

    await _post_bonus(client, auth_token, period_id, cp3b2a_driver_id, "20.00")

    r2 = await _get_summary(client, auth_token, period_id)
    rev_after = r2.json()["bonus_data_revision"]
    assert rev_after == rev_before + 1

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_single_update_invalidates_summary_revision(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b2a_branch_id: int,
    cp3b2a_driver_id: int,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2a_branch_id, *_week())
    await _seed_snapshot(db_conn, cp3b2a_branch_id, period_id, [await _snapshot_entry(cp3b2a_driver_id)])
    event_id = await _post_bonus(client, auth_token, period_id, cp3b2a_driver_id, "20.00")

    r1 = await _get_summary(client, auth_token, period_id)
    rev_before = r1.json()["bonus_data_revision"]

    await client.patch(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        json={"amount": "30.00"},
        headers=_auth(auth_token),
    )

    r2 = await _get_summary(client, auth_token, period_id)
    rev_after = r2.json()["bonus_data_revision"]
    assert rev_after == rev_before + 1

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_single_void_invalidates_summary_revision(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b2a_branch_id: int,
    cp3b2a_driver_id: int,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2a_branch_id, *_week())
    await _seed_snapshot(db_conn, cp3b2a_branch_id, period_id, [await _snapshot_entry(cp3b2a_driver_id)])
    event_id = await _post_bonus(client, auth_token, period_id, cp3b2a_driver_id, "20.00")

    r1 = await _get_summary(client, auth_token, period_id)
    rev_before = r1.json()["bonus_data_revision"]

    await client.delete(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        headers=_auth(auth_token),
    )

    r2 = await _get_summary(client, auth_token, period_id)
    rev_after = r2.json()["bonus_data_revision"]
    assert rev_after == rev_before + 1

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 7. Audit correlation_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_line_audit_without_correlation_id_unchanged(db_conn: AsyncConnection) -> None:
    """Existing calls (no correlation_id) still work exactly as before."""
    from app.payroll.service import _write_line_audit

    await _write_line_audit(
        db_conn,
        company_id=1,
        branch_id=1,
        user_id=1,
        line_id=999999,
        action_code="BONUS_EVENT_ADDED",
        old_value=None,
        new_value={"probe": "no_correlation"},
        entity_name="PayrollBonusEvents",
    )
    await db_conn.commit()

    row = (await db_conn.execute(
        _text("""
            SELECT correlationid FROM audit.auditlog
            WHERE entityname = 'PayrollBonusEvents' AND entityid = '999999'
            ORDER BY auditid DESC LIMIT 1
        """)
    )).mappings().first()
    assert row is not None
    assert row["correlationid"] is not None, "Table default must still populate CorrelationID"


@pytest.mark.asyncio
async def test_write_line_audit_with_correlation_id_persisted(db_conn: AsyncConnection) -> None:
    """Supplying correlation_id stores exactly that value."""
    from app.payroll.service import _write_line_audit

    correlation_id = str(uuid.uuid4())
    await _write_line_audit(
        db_conn,
        company_id=1,
        branch_id=1,
        user_id=1,
        line_id=999998,
        action_code="BONUS_EVENT_ADDED",
        old_value=None,
        new_value={"probe": "with_correlation"},
        entity_name="PayrollBonusEvents",
        correlation_id=correlation_id,
    )
    await db_conn.commit()

    row = (await db_conn.execute(
        _text("""
            SELECT correlationid FROM audit.auditlog
            WHERE entityname = 'PayrollBonusEvents' AND entityid = '999998'
            ORDER BY auditid DESC LIMIT 1
        """)
    )).mappings().first()
    assert row is not None
    assert str(row["correlationid"]) == correlation_id


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_event_and_revision(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b2a_branch_id: int,
    cp3b2a_driver_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If _write_line_audit raises, both the event insert and the
    BonusDataRevision bump must roll back (same request transaction)."""
    period_id = await _insert_period_db(db_conn, cp3b2a_branch_id, *_week())
    before_rev = await _get_bonus_data_revision(db_conn, period_id)

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr("app.payroll.service._write_line_audit", _boom)

    with pytest.raises(RuntimeError):
        await client.post(
            f"/payroll/periods/{period_id}/bonuses",
            json={"driver_id": cp3b2a_driver_id, "amount": "77.00"},
            headers=_auth(auth_token),
        )

    row = (await db_conn.execute(
        _text("SELECT COUNT(*) FROM payroll.payrollbonusevents WHERE payrollperiodid = :pid"),
        {"pid": period_id},
    )).first()
    assert row[0] == 0, "Event must not exist after audit failure"
    after_rev = await _get_bonus_data_revision(db_conn, period_id)
    assert after_rev == before_rev, "Revision bump must roll back with the failed audit write"

    await _cancel_period_db(db_conn, period_id)
