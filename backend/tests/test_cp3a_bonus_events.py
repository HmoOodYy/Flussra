"""
CP-3A: Canonical Bonus Event Domain — full test suite.

Covers:
  - Schema/migration: PayrollBonusEvents table exists, PayrollFinalLines has BonusEventID.
  - API guards: list/create/update/void bonus events; period status gates.
  - Old path blocked: POST /period-pay with linetype=BONUS → 422 with redirect message.
  - Finalization bridge: bonus events appear as BONUS FinalLines with BonusEventID set.
  - Preview bridge: bonus_events field populated; BONUS excluded from lines list.
  - Void a bonus event before finalization: excluded from final lines.
  - Regression: non-BONUS period-pay lines finalize normally through DraftLines.
  - Zero/negative bonus amounts rejected.
  - Optimistic concurrency: data_revision mismatch → 409.
  - Voided bonus event update → 422.

Dates: 2096-* — isolated year (CP-2F uses 2097, CP-2E uses 2099, CP-2D2 uses 2097).

Run from backend/:
    python -B -m pytest tests/test_cp3a_bonus_events.py -v -p no:cacheprovider
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

_COMPANY_ID = 1
_BASE_MONDAY = datetime.date(2096, 6, 2)   # Monday, June 2096
_CTR = itertools.count(0)
# Unique suffix so period codes never collide between pytest sessions.
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
    code = f"CP3A-{_RUN_ID}-{branch_id}-{start.isoformat()}"
    r = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, :status, :code, :name, 'Week', :start, :end)
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "status": status, "code": code,
         "name": f"CP3A {start}", "start": start, "end": end},
    )).mappings().first()
    await db.commit()
    assert r is not None
    return r["payrollperiodid"]


async def _cancel_period_db(db: AsyncConnection, period_id: int) -> None:
    await db.execute(
        _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' "
              "WHERE payrollperiodid = :pid "
              "AND status IN ('Open', 'InReview', 'Approved', 'Returned')"),
        {"pid": period_id},
    )
    await db.commit()


async def _advance_to_approved(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
) -> None:
    """Push period through Open -> InReview -> Approved via the review system."""
    r = await client.patch(
        f"/payroll/periods/{period_id}/status",
        json={"status": "InReview"},
        headers=_auth(token),
    )
    assert r.status_code == 200, f"Submit (InReview) failed: {r.text}"

    # Find the pending review item for this period
    rv = await client.get("/review/items", headers=_auth(token))
    assert rv.status_code == 200, f"GET /review/items failed: {rv.text}"
    items = rv.json()
    review_item = next(
        (
            it for it in items
            if it.get("entity_name") == "PayrollPeriods"
            and str(it.get("entity_id")) == str(period_id)
            and it.get("status") == "Pending"
        ),
        None,
    )
    assert review_item is not None, (
        f"No review item found for period {period_id}. Items: {items}"
    )

    decide = await client.post(
        f"/review/items/{review_item['review_item_id']}/decide",
        headers=_auth(token),
        json={"decision": "Approved"},
    )
    assert decide.status_code == 200, f"Approval failed: {decide.text}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def cp3a_branch_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
) -> int:
    resp = await session_client.get("/core/branches", headers=_auth(auth_token))
    assert resp.status_code == 200, resp.text
    for branch in resp.json():
        if branch["branch_code"] == "PAYTEST":
            return branch["branch_id"]
    raise AssertionError("PAYTEST branch not found")


@pytest_asyncio.fixture
async def cp3a_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    cp3a_branch_id: int,
) -> int:
    driver_code = f"CP3A-BON-{_BASE_MONDAY.isoformat()}"
    # Look up first — driver persists across test runs in the shared DB.
    r_list = await session_client.get("/core/drivers", headers=_auth(auth_token))
    if r_list.status_code == 200:
        for d in r_list.json():
            if d.get("driver_code") == driver_code:
                return d["driver_id"]
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id":      cp3a_branch_id,
            "full_name":      "CP3A Bonus Driver",
            "preferred_name": "BonDrv",
            "driver_code":    driver_code,
            "cdl_number":     f"CDL-CP3A-{_BASE_MONDAY.isoformat()}",
            "email":          f"cp3a.bonus.{_BASE_MONDAY.isoformat()}@example.com",
        },
        headers=_auth(auth_token),
    )
    assert resp.status_code == 201, f"Driver seed failed: {resp.text}"
    return resp.json()["driver_id"]


# ---------------------------------------------------------------------------
# 1. Schema / migration checks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_payrollbonusevents_table_exists(db_conn: AsyncConnection) -> None:
    row = (await db_conn.execute(
        _text("""
            SELECT table_name
            FROM   information_schema.tables
            WHERE  table_schema = 'payroll'
              AND  table_name   = 'payrollbonusevents'
        """)
    )).mappings().first()
    assert row is not None, "payroll.payrollbonusevents does not exist after migration 0058"


@pytest.mark.asyncio
async def test_payrollrunbonuses_table_gone(db_conn: AsyncConnection) -> None:
    row = (await db_conn.execute(
        _text("""
            SELECT table_name
            FROM   information_schema.tables
            WHERE  table_schema = 'payroll'
              AND  table_name   = 'payrollrunbonuses'
        """)
    )).mappings().first()
    assert row is None, "payroll.payrollrunbonuses still exists — migration 0058 not applied"


@pytest.mark.asyncio
async def test_payrollfinallines_has_bonuseventid(db_conn: AsyncConnection) -> None:
    row = (await db_conn.execute(
        _text("""
            SELECT column_name
            FROM   information_schema.columns
            WHERE  table_schema = 'payroll'
              AND  table_name   = 'payrollfinallines'
              AND  column_name  = 'bonuseventid'
        """)
    )).mappings().first()
    assert row is not None, "payroll.payrollfinallines.BonusEventID column missing"


@pytest.mark.asyncio
async def test_bonus_events_canonical_columns(db_conn: AsyncConnection) -> None:
    rows = (await db_conn.execute(
        _text("""
            SELECT column_name
            FROM   information_schema.columns
            WHERE  table_schema = 'payroll'
              AND  table_name   = 'payrollbonusevents'
        """)
    )).scalars().all()
    required = {
        "payrollbonuseventid", "companyid", "branchid", "payrollperiodid", "driverid",
        "amount", "reason", "notes", "status", "datarevision", "sourcedraftlineid",
        "voidedbyuserid", "voidedatutc", "voidreason",
        "createdbyuserid", "createdatutc", "updatedbyuserid", "updatedatutc",
    }
    missing = required - set(rows)
    assert not missing, f"Missing columns in payrollbonusevents: {missing}"


# ---------------------------------------------------------------------------
# 2. API: basic CRUD on an Open period
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_bonus_event(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3a_driver_id, "amount": "150.00", "reason": "Great job"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["amount"] == "150.00"
    assert data["status"] == "Active"
    assert data["driver_id"] == cp3a_driver_id
    assert data["data_revision"] == 1

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_list_bonus_events(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    for amt in ["50.00", "75.00"]:
        await client.post(
            f"/payroll/periods/{period_id}/bonuses",
            json={"driver_id": cp3a_driver_id, "amount": amt},
            headers=_auth(auth_token),
        )

    r = await client.get(
        f"/payroll/periods/{period_id}/bonuses",
        headers=_auth(auth_token),
    )
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 2

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_update_bonus_event_amount(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3a_driver_id, "amount": "200.00"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 201, r.text
    event_id = r.json()["bonus_event_id"]

    r2 = await client.patch(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        json={"amount": "250.00"},
        headers=_auth(auth_token),
    )
    assert r2.status_code == 200, r2.text
    updated = r2.json()
    assert updated["amount"] == "250.00"
    assert updated["data_revision"] == 2

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_void_bonus_event(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3a_driver_id, "amount": "100.00"},
        headers=_auth(auth_token),
    )
    event_id = r.json()["bonus_event_id"]

    r2 = await client.delete(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        headers=_auth(auth_token),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "Voided"

    # Idempotent second void
    r3 = await client.delete(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        headers=_auth(auth_token),
    )
    assert r3.status_code == 200, r3.text

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 3. Validation guards
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zero_amount_rejected(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3a_driver_id, "amount": "0.00"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 422, r.text

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_negative_amount_rejected(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3a_driver_id, "amount": "-50.00"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 422, r.text

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_update_to_zero_rejected(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3a_driver_id, "amount": "100.00"},
        headers=_auth(auth_token),
    )
    event_id = r.json()["bonus_event_id"]

    r2 = await client.patch(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        json={"amount": "0"},
        headers=_auth(auth_token),
    )
    assert r2.status_code == 422, r2.text

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_update_voided_event_rejected(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3a_driver_id, "amount": "100.00"},
        headers=_auth(auth_token),
    )
    event_id = r.json()["bonus_event_id"]
    await client.delete(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        headers=_auth(auth_token),
    )

    r2 = await client.patch(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        json={"amount": "200.00"},
        headers=_auth(auth_token),
    )
    assert r2.status_code == 422, r2.text

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_optimistic_concurrency_conflict(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3a_driver_id, "amount": "100.00"},
        headers=_auth(auth_token),
    )
    event_id = r.json()["bonus_event_id"]

    # Update with wrong revision → 409
    r2 = await client.patch(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        json={"amount": "200.00", "data_revision": 999},
        headers=_auth(auth_token),
    )
    assert r2.status_code == 409, r2.text

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 4. Period-status gates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bonus_blocked_on_submitted_period(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    """CP-3A: bonus creation blocked on InReview (Submitted) period."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end, status="Open")

    # Add a bonus first so the period is non-empty (needed to advance to InReview)
    rb = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3a_driver_id, "amount": "50.00"},
        headers=_auth(auth_token),
    )
    assert rb.status_code == 201, rb.text

    # Advance to InReview
    r_ir = await client.patch(
        f"/payroll/periods/{period_id}/status",
        json={"status": "InReview"},
        headers=_auth(auth_token),
    )
    assert r_ir.status_code == 200, f"InReview transition failed: {r_ir.text}"

    # Now bonus creation must be blocked
    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3a_driver_id, "amount": "100.00"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 422, r.text
    assert "Open or Returned" in r.json()["detail"]

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_bonus_blocked_on_approved_period(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    """CP-3A: bonus creation blocked on Approved period."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end, status="Open")

    rb = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3a_driver_id, "amount": "50.00"},
        headers=_auth(auth_token),
    )
    assert rb.status_code == 201, rb.text

    await _advance_to_approved(client, auth_token, period_id, cp3a_driver_id)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3a_driver_id, "amount": "100.00"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 422, r.text

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 5. Old path blocked: POST /period-pay with linetype=BONUS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bonus_via_period_pay_blocked(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    """POST /period-pay with linetype=BONUS must be 422 with redirect message."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/period-pay",
        json={"driver_id": cp3a_driver_id, "amount": 100, "line_type": "BONUS"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "/bonuses" in detail, f"Expected redirect message, got: {detail}"

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 6. Finalization bridge
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finalization_includes_bonus_events(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    """Bonus events appear as BONUS FinalLines with BonusEventID set after finalization."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3a_driver_id, "amount": "300.00", "reason": "Excellence"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 201, r.text
    bonus_event_id = r.json()["bonus_event_id"]

    await _advance_to_approved(client, auth_token, period_id, cp3a_driver_id)

    r2 = await client.post(
        f"/payroll/periods/{period_id}/finalize",
        headers=_auth(auth_token),
    )
    assert r2.status_code == 200, r2.text

    # Check DB: FinalLine must have BonusEventID set and linetype=BONUS
    fl_row = (await db_conn.execute(
        _text("""
            SELECT finallineid, linetype, finalamount, bonuseventid
            FROM   payroll.payrollfinallines
            WHERE  payrollperiodid = :pid
              AND  linetype        = 'BONUS'
        """),
        {"pid": period_id},
    )).mappings().first()
    assert fl_row is not None, "No BONUS FinalLine found after finalization"
    assert fl_row["bonuseventid"] == bonus_event_id
    assert float(fl_row["finalamount"]) == 300.00

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_voided_bonus_excluded_from_finalization(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    """A voided bonus event is NOT included in finalization."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3a_driver_id, "amount": "500.00"},
        headers=_auth(auth_token),
    )
    event_id = r.json()["bonus_event_id"]

    # Add a second (non-voided) bonus to make the period non-empty after void
    r2 = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3a_driver_id, "amount": "100.00"},
        headers=_auth(auth_token),
    )
    assert r2.status_code == 201, r2.text

    # Void the first
    await client.delete(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        headers=_auth(auth_token),
    )

    await _advance_to_approved(client, auth_token, period_id, cp3a_driver_id)

    r3 = await client.post(
        f"/payroll/periods/{period_id}/finalize",
        headers=_auth(auth_token),
    )
    assert r3.status_code == 200, r3.text

    # Only one BONUS FinalLine — the voided one must not appear
    fl_rows = (await db_conn.execute(
        _text("""
            SELECT finallineid, finalamount, bonuseventid
            FROM   payroll.payrollfinallines
            WHERE  payrollperiodid = :pid
              AND  linetype        = 'BONUS'
        """),
        {"pid": period_id},
    )).mappings().all()
    assert len(fl_rows) == 1, f"Expected 1 BONUS FinalLine, got {len(fl_rows)}"
    assert float(fl_rows[0]["finalamount"]) == 100.00

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 7. Preview bridge
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preview_includes_bonus_events(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    """Preview response has bonus_events list; BONUS not in lines list."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3a_driver_id, "amount": "200.00"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 201, r.text
    bonus_event_id = r.json()["bonus_event_id"]

    await _advance_to_approved(client, auth_token, period_id, cp3a_driver_id)

    r2 = await client.get(
        f"/payroll/periods/{period_id}/finalization-preview",
        headers=_auth(auth_token),
    )
    assert r2.status_code == 200, r2.text
    preview = r2.json()

    # Bonus events list must be populated
    assert preview["bonus_event_count"] == 1
    assert len(preview["bonus_events"]) == 1
    be = preview["bonus_events"][0]
    assert be["bonus_event_id"] == bonus_event_id
    assert Decimal(str(be["amount"])) == Decimal("200.00")

    # CP-4F exposes immutable normalized snapshot lines. BONUS is therefore
    # present as a snapshot line, while still having no synthetic DraftLine ID.
    bonus_in_lines = [l for l in preview["lines"] if l["line_type"] == "BONUS"]
    assert len(bonus_in_lines) == 1
    assert bonus_in_lines[0]["draft_line_id"] is None
    assert Decimal(str(bonus_in_lines[0]["final_amount"])) == Decimal("200.00")

    # final_line_count_estimate must include bonus events
    assert preview["final_line_count_estimate"] >= 1

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 8. Regression: non-BONUS period pay still works
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_bonus_period_pay_unaffected(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    """Non-BONUS period-pay lines are not blocked or altered by CP-3A.

    Inject a non-BONUS DraftLine directly (bypassing branch pay-item activation)
    and confirm it is visible in the period-pay list endpoint — proving CP-3A's
    BONUS block leaves non-BONUS DraftLines untouched.
    """
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    # Direct DB insert of an ADJUSTMENT DraftLine (no branch-activation check).
    await db_conn.execute(
        _text("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid,
                 workdate, linetype, linescope, calculatedamount,
                 sourcetype, status, needsmanagerreview, addedbyuserid)
            VALUES
                (1, :bid, :pid, :did,
                 NULL, 'Adjustment', 'Period', 120.00,
                 'Manual', 'Active', FALSE, 1)
        """),
        {"bid": cp3a_branch_id, "pid": period_id, "did": cp3a_driver_id},
    )
    await db_conn.commit()

    # The ADJUSTMENT line must be visible in the period-pay list
    r = await client.get(
        f"/payroll/periods/{period_id}/period-pay",
        headers=_auth(auth_token),
    )
    assert r.status_code == 200, r.text
    lines = r.json()
    adj_lines = [l for l in lines if l.get("line_type") in ("ADJUSTMENT", "Adjustment")]
    assert len(adj_lines) == 1, f"Expected 1 ADJUSTMENT line, got: {lines}"

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 9. P1 Fix 1 — Bonus create driver eligibility / branch ownership
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_bonus_eligible_driver_succeeds(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    """POST /bonuses with a driver eligible for the period's branch succeeds."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3a_driver_id, "amount": "80.00"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 201, r.text
    assert r.json()["driver_id"] == cp3a_driver_id

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_create_bonus_wrong_branch_driver_rejects(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    session_client: httpx.AsyncClient,
) -> None:
    """POST /bonuses rejects a driver who belongs to a different branch."""
    # Find a branch that is NOT the PAYTEST branch.
    branches_resp = await session_client.get("/core/branches", headers=_auth(auth_token))
    assert branches_resp.status_code == 200, branches_resp.text
    other_branch = next(
        (b for b in branches_resp.json() if b["branch_id"] != cp3a_branch_id),
        None,
    )
    assert other_branch is not None, "Need at least 2 branches for wrong-branch test"
    other_branch_id = other_branch["branch_id"]

    # Create a driver on the other branch.
    driver_code = f"CP3A-WRONGBRANCH-{uuid.uuid4().hex[:6]}"
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id":   other_branch_id,
            "full_name":   "CP3A Wrong Branch Driver",
            "driver_code": driver_code,
        },
        headers=_auth(auth_token),
    )
    assert resp.status_code == 201, resp.text
    wrong_branch_driver_id = resp.json()["driver_id"]

    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": wrong_branch_driver_id, "amount": "80.00"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 422, (
        f"Expected 422 for wrong-branch driver, got {r.status_code}: {r.text}"
    )

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_create_bonus_ineligible_driver_rejects(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    session_client: httpx.AsyncClient,
) -> None:
    """POST /bonuses rejects a driver that is not eligible for the period."""
    # Create a driver then immediately terminate (making them ineligible).
    driver_code = f"CP3A-INELIGIBLE-{uuid.uuid4().hex[:6]}"
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id":   cp3a_branch_id,
            "full_name":   "CP3A Ineligible Driver",
            "driver_code": driver_code,
        },
        headers=_auth(auth_token),
    )
    assert resp.status_code == 201, resp.text
    ineligible_driver_id = resp.json()["driver_id"]

    # Terminate the driver's employment so they are not eligible in 2096.
    await db_conn.execute(
        _text("""
            UPDATE core.employees
            SET    terminationdate = '2090-01-01'
            WHERE  employeeid = (
                SELECT employeeid FROM core.drivers
                WHERE  driverid = :did
            )
        """),
        {"did": ineligible_driver_id},
    )
    await db_conn.commit()

    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": ineligible_driver_id, "amount": "80.00"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 422, (
        f"Expected 422 for ineligible driver, got {r.status_code}: {r.text}"
    )

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 10. P1 Fix 2 — Legacy BONUS DraftLines hidden from / rejected by /period-pay
# ---------------------------------------------------------------------------

async def _inject_bonus_draftline(
    db: AsyncConnection,
    branch_id: int,
    period_id: int,
    driver_id: int,
    amount: str = "99.00",
) -> int:
    """Directly insert a BONUS DraftLine (simulates a pre-migration legacy row)."""
    row = (await db.execute(
        _text("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid,
                 workdate, linetype, linescope, calculatedamount,
                 sourcetype, status, needsmanagerreview, addedbyuserid)
            VALUES
                (1, :bid, :pid, :did,
                 NULL, 'BONUS', 'Period', :amount,
                 'Manual', 'Active', FALSE, 1)
            RETURNING draftlineid
        """),
        {"bid": branch_id, "pid": period_id, "did": driver_id, "amount": amount},
    )).mappings().first()
    await db.commit()
    assert row is not None
    return row["draftlineid"]


@pytest.mark.asyncio
async def test_legacy_bonus_draftline_hidden_from_period_pay_list(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    """GET /period-pay must not return rows with linetype='BONUS'."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    await _inject_bonus_draftline(db_conn, cp3a_branch_id, period_id, cp3a_driver_id)

    r = await client.get(
        f"/payroll/periods/{period_id}/period-pay",
        headers=_auth(auth_token),
    )
    assert r.status_code == 200, r.text
    bonus_rows = [l for l in r.json() if l.get("line_type", "").upper() == "BONUS"]
    assert bonus_rows == [], (
        f"GET /period-pay must not expose BONUS DraftLines. Found: {bonus_rows}"
    )

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_patch_legacy_bonus_draftline_rejects(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    """PATCH /period-pay/{bonus_draftline_id} must reject with 422 and not mutate the row."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    bonus_dl_id = await _inject_bonus_draftline(
        db_conn, cp3a_branch_id, period_id, cp3a_driver_id, amount="88.00"
    )

    r = await client.patch(
        f"/payroll/periods/{period_id}/period-pay/{bonus_dl_id}",
        json={"amount": "999.00"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 422, (
        f"Expected 422 rejecting BONUS DraftLine PATCH, got {r.status_code}: {r.text}"
    )

    # Row must be unchanged in DB
    row = (await db_conn.execute(
        _text("SELECT calculatedamount FROM payroll.payrolldraftlines WHERE draftlineid = :lid"),
        {"lid": bonus_dl_id},
    )).first()
    assert row is not None and float(row[0]) == 88.00, (
        f"BONUS DraftLine amount must not change after rejected PATCH, got {row}"
    )

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_delete_legacy_bonus_draftline_rejects(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_id: int,
) -> None:
    """DELETE /period-pay/{bonus_draftline_id} must reject with 422; row stays Active."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    bonus_dl_id = await _inject_bonus_draftline(
        db_conn, cp3a_branch_id, period_id, cp3a_driver_id
    )

    r = await client.delete(
        f"/payroll/periods/{period_id}/period-pay/{bonus_dl_id}",
        headers=_auth(auth_token),
    )
    assert r.status_code == 422, (
        f"Expected 422 rejecting BONUS DraftLine DELETE, got {r.status_code}: {r.text}"
    )

    # Row must still be Active (not Void)
    row = (await db_conn.execute(
        _text("SELECT status FROM payroll.payrolldraftlines WHERE draftlineid = :lid"),
        {"lid": bonus_dl_id},
    )).first()
    assert row is not None and row[0] == "Active", (
        f"BONUS DraftLine must remain Active after rejected DELETE, got status={row}"
    )
