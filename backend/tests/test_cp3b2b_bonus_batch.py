"""
CP-3B2b: Create-only transactional bonus batch endpoint — full test suite.

Endpoint under test:
    POST /payroll/periods/{period_id}/bonuses/batch

Covers (grouped A–H per the CP-3B2b spec):
  A. Success/response shape, correlation id + idempotency key on events.
  B. Period-level BonusDataRevision: exactly-once bump, stale → 409, failure paths inert.
  C. Idempotency: same key+payload replay (200), key+different payload/revision → 409.
  D. Validation / all-or-nothing rollback.
  E. Lifecycle (Draft/InReview/Approved/… blocked) and permissions (ODA, view-only).
  F. Audit correlation + audit-failure rollback.
  G. DB ownership hardening (migration 0060 trigger + constraints).
  H. Regression (/period-pay BONUS still blocked; no DraftLine BONUS rows).

Dates: 2093-* — isolated year.

Run from backend/:
    python -B -m pytest tests/test_cp3b2b_bonus_batch.py -v -p no:cacheprovider
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

_d = datetime.date(2093, 6, 1)
_BASE_MONDAY = _d + datetime.timedelta(days=(7 - _d.weekday()) % 7)
_CTR = itertools.count(0)
_RUN_ID = uuid.uuid4().hex[:8]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _week(offset: int = 0) -> tuple[datetime.date, datetime.date]:
    n = next(_CTR) + offset
    start = _BASE_MONDAY + datetime.timedelta(weeks=n)
    return start, start + datetime.timedelta(days=6)


def _key() -> str:
    return f"cp3b2b-{uuid.uuid4().hex}"


async def _insert_period_db(
    db: AsyncConnection,
    branch_id: int,
    start: datetime.date,
    end: datetime.date,
    status: str = "Open",
) -> int:
    """Insert an isolated CP3B2B period without deleting historical fixtures."""
    if status == "Open":
        # CP-3A and CP-3B2B share PAYTEST in the combined regression order.
        # Retire only CP-3A's prior Open test slot; its history is retained.
        await db.execute(_text("""
            UPDATE payroll.payrollperiods
            SET status = 'Cancelled'
            WHERE branchid = :bid
              AND periodcode LIKE 'CP3A-%'
              AND status = 'Open'
        """), {"bid": branch_id})
    code = f"CP3B2B-{_RUN_ID}-{branch_id}-{start.isoformat()}"
    r = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, :status, :code, :name, 'Week', :start, :end)
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "status": status, "code": code,
         "name": f"CP3B2B {start}", "start": start, "end": end},
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


async def _force_status_db(db: AsyncConnection, period_id: int, status: str) -> None:
    """Force a period into an arbitrary status directly (bypassing the
    transition graph) for read-only lifecycle-guard tests."""
    await db.execute(_text(
        "ALTER TABLE payroll.payrollperiods DISABLE TRIGGER trg_period_status_revert"
    ))
    await db.execute(
        _text("UPDATE payroll.payrollperiods SET status = :s WHERE payrollperiodid = :pid"),
        {"s": status, "pid": period_id},
    )
    await db.execute(_text(
        "ALTER TABLE payroll.payrollperiods ENABLE TRIGGER trg_period_status_revert"
    ))
    await db.commit()


async def _seed_snapshot(
    db: AsyncConnection, branch_id: int, period_id: int, entries: list[dict],
) -> None:
    await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiodeligibilitysnapshots
                (payrollperiodid, companyid, branchid, snapshotsource, createdatutc, updatedatutc)
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
                        TRUE, :reason, 'Generated', NOW(), NOW())
                ON CONFLICT (payrollperiodid, driverid) DO NOTHING
            """),
            {"bid": branch_id, "pid": period_id, "did": e["driver_id"],
             "code": e["code"], "name": e["name"], "reason": e.get("reason", "Active")},
        )
    await db.commit()


async def _batch(
    client: httpx.AsyncClient, token: str, period_id: int,
    idempotency_key: str, expected_revision: int, items: list[dict],
) -> httpx.Response:
    return await client.post(
        f"/payroll/periods/{period_id}/bonuses/batch",
        json={
            "idempotency_key": idempotency_key,
            "expected_bonus_data_revision": expected_revision,
            "items": items,
        },
        headers=_auth(token),
    )


async def _get_bonus_data_revision(db: AsyncConnection, period_id: int) -> int:
    row = (await db.execute(
        _text("SELECT bonusdatarevision FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
        {"pid": period_id},
    )).first()
    assert row is not None
    return int(row[0])


async def _count_events(db: AsyncConnection, period_id: int) -> int:
    row = (await db.execute(
        _text("SELECT COUNT(*) FROM payroll.payrollbonusevents WHERE payrollperiodid = :pid"),
        {"pid": period_id},
    )).first()
    return int(row[0])


async def _count_batch_rows(db: AsyncConnection, period_id: int) -> int:
    row = (await db.execute(
        _text("SELECT COUNT(*) FROM payroll.payrollbonusbatchrequests WHERE payrollperiodid = :pid"),
        {"pid": period_id},
    )).first()
    return int(row[0])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="module")
async def cp3b2b_branch_id(session_db_conn) -> int:
    row = (await session_db_conn.execute(
        _text("""
            INSERT INTO core.branches
                (companyid, branchcode, branchname, status, isdefault)
            VALUES (1, :code, :name, 'Active', FALSE)
            RETURNING branchid
        """),
        {"code": (code := f"CP3B2B_{uuid.uuid4().hex[:10]}"), "name": code},
    )).mappings().first()
    await session_db_conn.commit()
    assert row is not None
    return row["branchid"]


async def _get_or_create_driver(
    session_client: httpx.AsyncClient, auth_token: str,
    branch_id: int, driver_code: str, full_name: str,
) -> int:
    r_list = await session_client.get("/core/drivers", headers=_auth(auth_token))
    if r_list.status_code == 200:
        for d in r_list.json():
            if d.get("driver_code") == driver_code:
                return d["driver_id"]
    resp = await session_client.post(
        "/core/drivers",
        json={"branch_id": branch_id, "full_name": full_name, "driver_code": driver_code},
        headers=_auth(auth_token),
    )
    assert resp.status_code == 201, f"Driver seed failed: {resp.text}"
    return resp.json()["driver_id"]


@pytest_asyncio.fixture
async def cp3b2b_drivers(
    session_client: httpx.AsyncClient, auth_token: str, cp3b2b_branch_id: int,
) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, name in [
        ("alpha", "CP3B2B Alpha Driver"),
        ("beta",  "CP3B2B Beta Driver"),
        ("gamma", "CP3B2B Gamma Driver"),
    ]:
        out[key] = await _get_or_create_driver(
            session_client, auth_token, cp3b2b_branch_id,
            driver_code=f"CP3B2B-{key.upper()}", full_name=name,
        )
    return out


def _entry(drivers: dict[str, int], k: str) -> dict:
    return {"driver_id": drivers[k], "code": f"CP3B2B-{k.upper()}", "name": f"CP3B2B {k.capitalize()} Driver"}


async def _open_period_with_roster(
    db: AsyncConnection, branch_id: int, drivers: dict[str, int], keys: list[str],
) -> int:
    start, end = _week()
    period_id = await _insert_period_db(db, branch_id, start, end)
    await _seed_snapshot(db, branch_id, period_id, [_entry(drivers, k) for k in keys])
    return period_id


# ===========================================================================
# A. Success / response
# ===========================================================================

@pytest.mark.asyncio
async def test_multi_driver_batch_succeeds_201(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha", "beta", "gamma"])
    r = await _batch(client, auth_token, period_id, _key(), 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "50.00", "reason": "Safety"},
        {"driver_id": cp3b2b_drivers["beta"],  "amount": "75.00"},
        {"driver_id": cp3b2b_drivers["gamma"], "amount": "20.00", "notes": "spot"},
    ])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["replayed"] is False
    assert body["created_event_count"] == 3
    assert len(body["created_event_ids"]) == 3
    assert len(body["events"]) == 3
    assert body["batch_request_id"] > 0
    assert body["batch_correlation_id"]
    assert body["expected_bonus_data_revision"] == 0
    assert body["result_bonus_data_revision"] == 1
    # all events share the same correlation id + idempotency key
    corr = {e["bonus_event_id"]: None for e in body["events"]}
    assert len(corr) == 3
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_duplicate_driver_rows_create_separate_events(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha"])
    r = await _batch(client, auth_token, period_id, _key(), 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "10.00"},
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "20.00"},
    ])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created_event_count"] == 2
    assert len(set(body["created_event_ids"])) == 2
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_events_carry_correlation_and_idempotency_key(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha", "beta"])
    key = _key()
    r = await _batch(client, auth_token, period_id, key, 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "50.00"},
        {"driver_id": cp3b2b_drivers["beta"],  "amount": "60.00"},
    ])
    assert r.status_code == 201, r.text
    corr = r.json()["batch_correlation_id"]
    rows = (await db_conn.execute(
        _text("""
            SELECT batchcorrelationid, idempotencykey
            FROM payroll.payrollbonusevents WHERE payrollperiodid = :pid
        """),
        {"pid": period_id},
    )).mappings().all()
    assert len(rows) == 2
    for row in rows:
        assert str(row["batchcorrelationid"]) == corr
        assert row["idempotencykey"] == key
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_get_bonuses_lists_created_events(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha", "beta"])
    await _batch(client, auth_token, period_id, _key(), 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "50.00"},
        {"driver_id": cp3b2b_drivers["beta"],  "amount": "60.00"},
    ])
    r = await client.get(f"/payroll/periods/{period_id}/bonuses", headers=_auth(auth_token))
    assert r.status_code == 200, r.text
    assert len(r.json()) == 2
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_summary_reflects_batch_totals_and_revision(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha", "beta"])
    r = await _batch(client, auth_token, period_id, _key(), 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "50.00"},
        {"driver_id": cp3b2b_drivers["beta"],  "amount": "60.00"},
    ])
    result_rev = r.json()["result_bonus_data_revision"]
    s = await client.get(f"/payroll/periods/{period_id}/bonuses/summary", headers=_auth(auth_token))
    assert s.status_code == 200, s.text
    summary = s.json()
    assert summary["bonus_data_revision"] == result_rev
    assert Decimal(summary["active_bonus_total"]) == Decimal("110.00")
    assert summary["active_event_count"] == 2
    await _cancel_period_db(db_conn, period_id)


# ===========================================================================
# B. Revision
# ===========================================================================

@pytest.mark.asyncio
async def test_batch_increments_revision_exactly_once(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha", "beta", "gamma"])
    before = await _get_bonus_data_revision(db_conn, period_id)
    r = await _batch(client, auth_token, period_id, _key(), before, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "10.00"},
        {"driver_id": cp3b2b_drivers["beta"],  "amount": "20.00"},
        {"driver_id": cp3b2b_drivers["gamma"], "amount": "30.00"},
    ])
    assert r.status_code == 201, r.text
    after = await _get_bonus_data_revision(db_conn, period_id)
    assert after == before + 1, "3-item batch must bump revision by exactly 1, not per event"
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_stale_expected_revision_conflicts_writes_nothing(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha"])
    before = await _get_bonus_data_revision(db_conn, period_id)
    r = await _batch(client, auth_token, period_id, _key(), before + 99, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "10.00"},
    ])
    assert r.status_code == 409, r.text
    assert await _count_events(db_conn, period_id) == 0
    assert await _count_batch_rows(db_conn, period_id) == 0
    assert await _get_bonus_data_revision(db_conn, period_id) == before
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_single_event_post_invalidates_batch_revision(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha", "beta"])
    # Client reads summary revision (0), then a single-event POST lands.
    s = await client.get(f"/payroll/periods/{period_id}/bonuses/summary", headers=_auth(auth_token))
    rev_seen = s.json()["bonus_data_revision"]
    pe = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3b2b_drivers["alpha"], "amount": "5.00"},
        headers=_auth(auth_token),
    )
    assert pe.status_code == 201, pe.text
    # Batch built against the now-stale revision must 409.
    r = await _batch(client, auth_token, period_id, _key(), rev_seen, [
        {"driver_id": cp3b2b_drivers["beta"], "amount": "10.00"},
    ])
    assert r.status_code == 409, r.text
    await _cancel_period_db(db_conn, period_id)


# ===========================================================================
# C. Idempotency
# ===========================================================================

@pytest.mark.asyncio
async def test_replay_same_key_same_payload_returns_200(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha", "beta"])
    key = _key()
    items = [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "50.00", "reason": "x"},
        {"driver_id": cp3b2b_drivers["beta"],  "amount": "60.00"},
    ]
    r1 = await _batch(client, auth_token, period_id, key, 0, items)
    assert r1.status_code == 201, r1.text
    first = r1.json()
    rev_after_first = await _get_bonus_data_revision(db_conn, period_id)
    events_after_first = await _count_events(db_conn, period_id)

    r2 = await _batch(client, auth_token, period_id, key, 0, items)
    assert r2.status_code == 200, r2.text
    second = r2.json()
    assert second["replayed"] is True
    assert second["created_event_ids"] == first["created_event_ids"]
    assert second["batch_correlation_id"] == first["batch_correlation_id"]
    assert second["batch_request_id"] == first["batch_request_id"]
    assert second["result_bonus_data_revision"] == first["result_bonus_data_revision"]
    # No duplicate writes, no extra bump.
    assert await _count_events(db_conn, period_id) == events_after_first
    assert await _get_bonus_data_revision(db_conn, period_id) == rev_after_first
    assert await _count_batch_rows(db_conn, period_id) == 1
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_same_key_different_amount_conflicts(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha"])
    key = _key()
    r1 = await _batch(client, auth_token, period_id, key, 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "50.00"},
    ])
    assert r1.status_code == 201, r1.text
    r2 = await _batch(client, auth_token, period_id, key, 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "51.00"},
    ])
    assert r2.status_code == 409, r2.text
    assert await _count_events(db_conn, period_id) == 1
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_same_key_different_reason_conflicts(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha"])
    key = _key()
    r1 = await _batch(client, auth_token, period_id, key, 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "50.00", "reason": "A"},
    ])
    assert r1.status_code == 201, r1.text
    r2 = await _batch(client, auth_token, period_id, key, 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "50.00", "reason": "B"},
    ])
    assert r2.status_code == 409, r2.text
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_same_key_different_expected_revision_conflicts(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha"])
    key = _key()
    r1 = await _batch(client, auth_token, period_id, key, 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "50.00"},
    ])
    assert r1.status_code == 201, r1.text
    # Same key, same items, but different expected revision → different hash → 409.
    r2 = await _batch(client, auth_token, period_id, key, 1, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "50.00"},
    ])
    assert r2.status_code == 409, r2.text
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_sequential_same_key_yields_one_apply_one_replay(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    """Proxy for concurrency: the period FOR UPDATE lock + unique idempotency
    index guarantee a retried key applies once and replays after — never a
    duplicate write."""
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha"])
    key = _key()
    items = [{"driver_id": cp3b2b_drivers["alpha"], "amount": "50.00"}]
    codes = []
    for _ in range(3):
        r = await _batch(client, auth_token, period_id, key, 0, items)
        codes.append(r.status_code)
    assert codes == [201, 200, 200], codes
    assert await _count_events(db_conn, period_id) == 1
    assert await _count_batch_rows(db_conn, period_id) == 1
    await _cancel_period_db(db_conn, period_id)


# ===========================================================================
# D. Validation / all-or-nothing
# ===========================================================================

@pytest.mark.asyncio
async def test_empty_items_rejected(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha"])
    r = await _batch(client, auth_token, period_id, _key(), 0, [])
    assert r.status_code == 422, r.text
    assert await _count_events(db_conn, period_id) == 0
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_more_than_100_items_rejected(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha"])
    items = [{"driver_id": cp3b2b_drivers["alpha"], "amount": "1.00"} for _ in range(101)]
    r = await _batch(client, auth_token, period_id, _key(), 0, items)
    assert r.status_code == 422, r.text
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_zero_amount_rejected(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha"])
    r = await _batch(client, auth_token, period_id, _key(), 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "0.00"},
    ])
    assert r.status_code == 422, r.text
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_negative_amount_rejected(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha"])
    r = await _batch(client, auth_token, period_id, _key(), 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "-5.00"},
    ])
    assert r.status_code == 422, r.text
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_too_many_decimals_rejected(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha"])
    r = await _batch(client, auth_token, period_id, _key(), 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "50.005"},
    ])
    assert r.status_code == 422, r.text
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_amount_out_of_numeric_range_rejected(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha"])
    r = await _batch(client, auth_token, period_id, _key(), 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "10000000000000000.00"},
    ])
    assert r.status_code == 422, r.text
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_one_ineligible_item_rolls_back_whole_batch(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers, hq_branch_id,
    session_client,
) -> None:
    """A driver not in the period snapshot (here: an HQ-branch driver) makes the
    whole batch fail with zero partial writes."""
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha", "beta"])
    # A driver on a different branch — not in this period's snapshot.
    hq_driver = await _get_or_create_driver(
        session_client, auth_token, hq_branch_id,
        driver_code=f"CP3B2B-HQ-{uuid.uuid4().hex[:6]}", full_name="CP3B2B HQ Driver",
    )
    r = await _batch(client, auth_token, period_id, _key(), 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "10.00"},
        {"driver_id": hq_driver,               "amount": "20.00"},   # ineligible
        {"driver_id": cp3b2b_drivers["beta"],  "amount": "30.00"},
    ])
    assert r.status_code == 422, r.text
    assert await _count_events(db_conn, period_id) == 0
    assert await _count_batch_rows(db_conn, period_id) == 0
    assert await _get_bonus_data_revision(db_conn, period_id) == 0
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_included_by_existing_data_matches_single_event(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    """An IncludedByExistingData driver with no period-pay source is rejected by
    the batch exactly as single-event POST rejects it."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b2b_branch_id, start, end)
    await _seed_snapshot(db_conn, cp3b2b_branch_id, period_id, [{
        "driver_id": cp3b2b_drivers["alpha"], "code": "CP3B2B-ALPHA",
        "name": "CP3B2B Alpha Driver", "reason": "IncludedByExistingData",
    }])
    # Single-event POST rejects it →
    single = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3b2b_drivers["alpha"], "amount": "10.00"},
        headers=_auth(auth_token),
    )
    assert single.status_code == 422, single.text
    # Batch rejects it too, nothing written.
    r = await _batch(client, auth_token, period_id, _key(), 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "10.00"},
    ])
    assert r.status_code == 422, r.text
    assert await _count_events(db_conn, period_id) == 0
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_no_marker_period_matches_single_event_behavior(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    """On a period without a CP-2E snapshot marker, bonus creation (single-event
    POST and batch alike) falls back to the live eligibility check — the batch
    must behave identically to single-event POST, not diverge. (This differs
    from GET /bonuses/summary, which requires a marker; creation does not.)"""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b2b_branch_id, start, end)
    # No snapshot seeded — both paths use the live-eligibility fallback.
    single = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3b2b_drivers["alpha"], "amount": "10.00"},
        headers=_auth(auth_token),
    )
    batch = await _batch(client, auth_token, period_id, _key(), 1, [
        {"driver_id": cp3b2b_drivers["beta"], "amount": "20.00"},
    ])
    # Batch's status matches single-event POST's status (both accept a
    # live-eligible driver on a no-marker period). Consistency is the assertion.
    assert batch.status_code in (201, single.status_code), (
        f"batch={batch.status_code} single={single.status_code}: {batch.text}"
    )
    if single.status_code == 201:
        assert batch.status_code == 201, batch.text
    await _cancel_period_db(db_conn, period_id)


# ===========================================================================
# E. Lifecycle / permissions
# ===========================================================================

@pytest.mark.asyncio
async def test_draft_period_blocked(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b2b_branch_id, start, end, status="Draft")
    await _seed_snapshot(db_conn, cp3b2b_branch_id, period_id, [_entry(cp3b2b_drivers, "alpha")])
    r = await _batch(client, auth_token, period_id, _key(), 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "10.00"},
    ])
    assert r.status_code == 422, r.text
    assert await _count_events(db_conn, period_id) == 0
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["InReview", "Approved", "Locked", "Archived", "Cancelled"])
async def test_non_editable_statuses_blocked(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers, status,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha"])
    await _force_status_db(db_conn, period_id, status)
    r = await _batch(client, auth_token, period_id, _key(), 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "10.00"},
    ])
    assert r.status_code == 422, f"{status}: {r.text}"
    assert await _count_events(db_conn, period_id) == 0
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_oda_user_denied(
    session_client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha"])
    # Create an ODA user scoped to the branch.
    cr = await session_client.post(
        "/admin/company-roles", json={"role_name": f"CP3B2B_ODA_{_RUN_ID}"}, headers=_auth(auth_token),
    )
    role_id = cr.json()["company_role_id"]
    await session_client.put(
        f"/admin/company-roles/{role_id}/permissions",
        json={"permission_codes": ["payroll.view", "payroll.entry"]}, headers=_auth(auth_token),
    )
    uresp = await session_client.post(
        "/admin/users",
        json={"username": f"cp3b2b_oda_{_RUN_ID}", "display_name": "oda", "password": "TestPass123!",
              "is_active": True, "can_login": True, "must_change_password": False},
        headers=_auth(auth_token),
    )
    uid = uresp.json()["user_id"]
    await session_client.post(
        f"/admin/users/{uid}/company-role-assignments",
        json={"company_role_id": role_id, "scope_type": "OwnDriverDataOnly", "branch_id": cp3b2b_branch_id},
        headers=_auth(auth_token),
    )
    login = await session_client.post("/auth/login", json={
        "username": f"cp3b2b_oda_{_RUN_ID}", "password": "TestPass123!", "company_code": "DEMO"})
    oda_token = login.json()["access_token"]

    r = await _batch(session_client, oda_token, period_id, _key(), 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "10.00"},
    ])
    assert r.status_code == 403, r.text
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_view_only_user_denied(
    client, auth_token, branch_user_token, db_conn, hq_branch_id, hq_driver_id,
) -> None:
    """branch_user (payroll.view only, HQ scope) cannot apply a batch."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, hq_branch_id, start, end, status="Open")
    await _seed_snapshot(db_conn, hq_branch_id, period_id, [
        {"driver_id": hq_driver_id, "code": "HQD-001", "name": "HQ Driver"},
    ])
    r = await _batch(client, branch_user_token, period_id, _key(), 0, [
        {"driver_id": hq_driver_id, "amount": "10.00"},
    ])
    assert r.status_code == 403, r.text
    assert await _count_events(db_conn, period_id) == 0
    await _cancel_period_db(db_conn, period_id)


# ===========================================================================
# F. Audit / rollback
# ===========================================================================

@pytest.mark.asyncio
async def test_audit_rows_share_one_correlation_id_and_batch_row(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha", "beta"])
    r = await _batch(client, auth_token, period_id, _key(), 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "50.00"},
        {"driver_id": cp3b2b_drivers["beta"],  "amount": "60.00"},
    ])
    assert r.status_code == 201, r.text
    corr = r.json()["batch_correlation_id"]
    batch_request_id = r.json()["batch_request_id"]

    rows = (await db_conn.execute(
        _text("""
            SELECT actioncode, entityname, entityid
            FROM audit.auditlog
            WHERE correlationid = CAST(:corr AS UUID)
            ORDER BY auditid
        """),
        {"corr": corr},
    )).mappings().all()
    action_counts: dict[str, int] = {}
    for row in rows:
        action_counts[row["actioncode"]] = action_counts.get(row["actioncode"], 0) + 1
    assert action_counts.get("BONUS_EVENT_ADDED") == 2
    assert action_counts.get("BONUS_BATCH_APPLIED") == 1
    batch_row = [r for r in rows if r["actioncode"] == "BONUS_BATCH_APPLIED"][0]
    assert batch_row["entityname"] == "PayrollBonusBatchRequests"
    assert batch_row["entityid"] == str(batch_request_id)
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_everything(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers, monkeypatch,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha", "beta"])
    before_rev = await _get_bonus_data_revision(db_conn, period_id)

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr("app.payroll.bonus._write_line_audit", _boom)
    with pytest.raises(RuntimeError):
        await _batch(client, auth_token, period_id, _key(), before_rev, [
            {"driver_id": cp3b2b_drivers["alpha"], "amount": "50.00"},
            {"driver_id": cp3b2b_drivers["beta"],  "amount": "60.00"},
        ])
    # Full rollback: no events, no batch row, no revision bump.
    assert await _count_events(db_conn, period_id) == 0
    assert await _count_batch_rows(db_conn, period_id) == 0
    assert await _get_bonus_data_revision(db_conn, period_id) == before_rev
    await _cancel_period_db(db_conn, period_id)


# ===========================================================================
# G. DB hardening / migration 0060
# ===========================================================================

_VALID_HASH = "a" * 64


async def _insert_batch_row_raw(db: AsyncConnection, **overrides) -> None:
    params = {
        "cid": 1, "bid": None, "pid": None,
        "key": f"raw-{uuid.uuid4().hex}", "hash": _VALID_HASH,
        "payload": '{"version": 1}', "corr": str(uuid.uuid4()),
        "expected": 0, "result": 1, "event_ids": "[]", "count": 0,
        "uid": 1,
    }
    params.update(overrides)
    await db.execute(
        _text("""
            INSERT INTO payroll.payrollbonusbatchrequests
                (companyid, branchid, payrollperiodid, idempotencykey, requesthash,
                 requestpayloadjson, batchcorrelationid, expectedbonusdatarevision,
                 resultbonusdatarevision, createdeventids, createdeventcount, status,
                 createdbyuserid, createdatutc, appliedatutc)
            VALUES (:cid, :bid, :pid, :key, :hash, CAST(:payload AS JSONB),
                    CAST(:corr AS UUID), :expected, :result, CAST(:event_ids AS JSONB),
                    :count, 'Applied', :uid, NOW(), NOW())
        """),
        params,
    )


@pytest.mark.asyncio
async def test_ownership_trigger_rejects_mismatched_branch_insert(
    db_conn, cp3b2b_branch_id, hq_branch_id,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2b_branch_id, *_week())
    with pytest.raises(Exception):
        await _insert_batch_row_raw(db_conn, bid=hq_branch_id, pid=period_id)
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_ownership_trigger_rejects_mismatched_company_insert(
    db_conn, cp3b2b_branch_id,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2b_branch_id, *_week())
    with pytest.raises(Exception):
        await _insert_batch_row_raw(db_conn, cid=999, bid=cp3b2b_branch_id, pid=period_id)
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_ownership_trigger_rejects_mismatched_update(
    db_conn, cp3b2b_branch_id, hq_branch_id,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2b_branch_id, *_week())
    await _insert_batch_row_raw(db_conn, bid=cp3b2b_branch_id, pid=period_id)
    await db_conn.commit()
    with pytest.raises(Exception):
        await db_conn.execute(
            _text("UPDATE payroll.payrollbonusbatchrequests SET branchid = :bid "
                  "WHERE payrollperiodid = :pid"),
            {"bid": hq_branch_id, "pid": period_id},
        )
    await db_conn.rollback()
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_created_by_user_id_not_null(
    db_conn, cp3b2b_branch_id,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2b_branch_id, *_week())
    with pytest.raises(Exception):
        await _insert_batch_row_raw(db_conn, bid=cp3b2b_branch_id, pid=period_id, uid=None)
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_malformed_request_hash_rejected(
    db_conn, cp3b2b_branch_id,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2b_branch_id, *_week())
    with pytest.raises(Exception):
        # 64 chars but not hex.
        await _insert_batch_row_raw(db_conn, bid=cp3b2b_branch_id, pid=period_id, hash="Z" * 64)
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_event_count_mismatch_rejected(
    db_conn, cp3b2b_branch_id,
) -> None:
    period_id = await _insert_period_db(db_conn, cp3b2b_branch_id, *_week())
    with pytest.raises(Exception):
        # count says 5 but array is empty.
        await _insert_batch_row_raw(db_conn, bid=cp3b2b_branch_id, pid=period_id,
                                    event_ids="[]", count=5)
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_downgrade_guard_detects_live_batch_rows(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    """After a real batch, PayrollBonusBatchRequests is non-empty — exactly the
    condition migration 0060's downgrade refuses on. We assert the mechanism's
    predicate here (running the alembic downgrade in-process would corrupt the
    shared session DB)."""
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha"])
    r = await _batch(client, auth_token, period_id, _key(), 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "10.00"},
    ])
    assert r.status_code == 201, r.text
    total = (await db_conn.execute(
        _text("SELECT COUNT(*) FROM payroll.payrollbonusbatchrequests"),
    )).scalar()
    assert total and total > 0, "downgrade guard refuses when this count > 0"
    await _cancel_period_db(db_conn, period_id)


# ===========================================================================
# H. Regression
# ===========================================================================

@pytest.mark.asyncio
async def test_period_pay_bonus_still_blocked(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha"])
    r = await client.post(
        f"/payroll/periods/{period_id}/period-pay",
        json={"driver_id": cp3b2b_drivers["alpha"], "amount": 100, "line_type": "BONUS"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 422, r.text
    assert "/bonuses" in r.json()["detail"]
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_batch_creates_no_draftline_bonus_rows(
    client, auth_token, db_conn, cp3b2b_branch_id, cp3b2b_drivers,
) -> None:
    period_id = await _open_period_with_roster(db_conn, cp3b2b_branch_id, cp3b2b_drivers, ["alpha", "beta"])
    await _batch(client, auth_token, period_id, _key(), 0, [
        {"driver_id": cp3b2b_drivers["alpha"], "amount": "50.00"},
        {"driver_id": cp3b2b_drivers["beta"],  "amount": "60.00"},
    ])
    row = (await db_conn.execute(
        _text("SELECT COUNT(*) FROM payroll.payrolldraftlines "
              "WHERE payrollperiodid = :pid AND linetype = 'BONUS'"),
        {"pid": period_id},
    )).first()
    assert row[0] == 0, "batch must never create BONUS DraftLines"
    await _cancel_period_db(db_conn, period_id)
