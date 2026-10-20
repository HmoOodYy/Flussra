"""
CP-3A: Canonical Bonus Event Domain — full test suite.

Covers:
  - Schema/migration: PayrollBonusEvents table exists, PayrollFinalLines has BonusEventID.
  - API guards: list/create/update/void bonus events; period status gates.
  - Finalization bridge: bonus events appear as BONUS FinalLines with BonusEventID set.
  - Preview bridge: bonus_events field populated; BONUS excluded from lines list.
  - Void a bonus event before finalization: excluded from final lines.
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

import httpx
import pytest
import pytest_asyncio
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
    from app.payroll.eligibility import _create_period_driver_eligibility_rows
    await _create_period_driver_eligibility_rows(
        r["payrollperiodid"], 1, branch_id, db,
        snapshot_source="Generated",
        freeze=status in {"Open", "Returned"},
    )
    await db.commit()
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
    driver_assignment_id: int,
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

@pytest_asyncio.fixture(scope="module")
async def cp3a_branch_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    session_db_conn,
) -> int:
    row = (await session_db_conn.execute(
        _text("""
            INSERT INTO core.branches
                (companyid, branchcode, branchname, status, isdefault, timezoneid)
            VALUES (1, :code, :name, 'Active', FALSE, 'UTC')
            RETURNING branchid
        """),
        {"code": (code := f"CP3A_{uuid.uuid4().hex[:10]}"), "name": code},
    )).mappings().first()
    await session_db_conn.commit()
    return row["branchid"]


@pytest_asyncio.fixture(scope="module")
async def cp3a_driver_assignment_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    cp3a_branch_id: int,
) -> int:
    driver_number = f"CP3A-BON-{_BASE_MONDAY.isoformat()}-{_RUN_ID}"
    resp = await session_client.post(
        "/workforce/employees",
        json={
            "branch_id": cp3a_branch_id,
            "full_name": "CP3A Bonus Driver",
            "hire_date": "1990-01-01",
            "driver": {
                "driver_number": driver_number,
                "effective_from_at": "2000-01-01T00:00:00Z",
            },
        },
        headers=_auth(auth_token),
    )
    assert resp.status_code == 201, f"Driver seed failed: {resp.text}"
    return resp.json()["current_or_pending_assignment"]["driver_assignment_id"]


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
        "payrollbonuseventid", "companyid", "branchid", "payrollperiodid", "driverassignmentid",
        "amount", "reason", "notes", "status", "datarevision",
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
    cp3a_driver_assignment_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_assignment_id": cp3a_driver_assignment_id, "amount": "150.1234", "reason": "Great job"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 201, r.text
    data = r.json()
    # The Company currency is USD: typed-in precision is normalized to two decimals on entry
    # (payable-rounding-v1) and the normalized value is what is returned and stored.
    assert Decimal(str(data["amount"])) == Decimal("150.12")
    assert data["status"] == "Active"
    assert data["driver_assignment_id"] == cp3a_driver_assignment_id
    assert data["data_revision"] == 1
    stored_amount = (await db_conn.execute(_text("""
        SELECT amount FROM payroll.payrollbonusevents WHERE payrollbonuseventid = :event_id
    """), {"event_id": data["bonus_event_id"]})).scalar_one()
    assert Decimal(str(stored_amount)) == Decimal("150.12")

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_list_bonus_events(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_assignment_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    for amt in ["50.00", "75.00"]:
        await client.post(
            f"/payroll/periods/{period_id}/bonuses",
            json={"driver_assignment_id": cp3a_driver_assignment_id, "amount": amt},
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
    cp3a_driver_assignment_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_assignment_id": cp3a_driver_assignment_id, "amount": "200.00"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 201, r.text
    event_id = r.json()["bonus_event_id"]

    r2 = await client.patch(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        json={"amount": "250.1234"},
        headers=_auth(auth_token),
    )
    assert r2.status_code == 200, r2.text
    updated = r2.json()
    assert Decimal(str(updated["amount"])) == Decimal("250.12")  # normalized to USD decimals
    assert updated["data_revision"] == 2

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_void_bonus_event(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_assignment_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_assignment_id": cp3a_driver_assignment_id, "amount": "100.00"},
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
    cp3a_driver_assignment_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_assignment_id": cp3a_driver_assignment_id, "amount": "0.00"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 422, r.text

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_single_create_normalizes_extra_precision_and_rejects_absurd_scale(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_assignment_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)
    # Extra precision is accepted and rounded half-even to the USD decimals (1.23456 -> 1.23).
    response = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_assignment_id": cp3a_driver_assignment_id, "amount": "1.23456"},
        headers=_auth(auth_token),
    )
    assert response.status_code == 201, response.text
    assert Decimal(str(response.json()["amount"])) == Decimal("1.23")
    # More than ten decimals is refused outright, and creates nothing.
    absurd = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_assignment_id": cp3a_driver_assignment_id, "amount": "1.12345678901"},
        headers=_auth(auth_token),
    )
    assert absurd.status_code == 422, absurd.text
    count = (await db_conn.execute(_text("""
        SELECT COUNT(*) FROM payroll.payrollbonusevents WHERE payrollperiodid = :pid
    """), {"pid": period_id})).scalar_one()
    assert count == 1
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_single_create_rejects_an_amount_that_rounds_to_zero(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_assignment_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)
    response = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_assignment_id": cp3a_driver_assignment_id, "amount": "0.004"},
        headers=_auth(auth_token),
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "AMOUNT_ROUNDS_TO_ZERO"
    count = (await db_conn.execute(_text("""
        SELECT COUNT(*) FROM payroll.payrollbonusevents WHERE payrollperiodid = :pid
    """), {"pid": period_id})).scalar_one()
    assert count == 0
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_single_update_normalizes_extra_precision_and_rejects_zero_rounding(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_assignment_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)
    created = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_assignment_id": cp3a_driver_assignment_id, "amount": "1.00"},
        headers=_auth(auth_token),
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["bonus_event_id"]
    rejected = await client.patch(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        json={"amount": "0.004"},
        headers=_auth(auth_token),
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["detail"]["code"] == "AMOUNT_ROUNDS_TO_ZERO"
    stored = (await db_conn.execute(_text("""
        SELECT amount FROM payroll.payrollbonusevents WHERE payrollbonuseventid = :event_id
    """), {"event_id": event_id})).scalar_one()
    assert Decimal(str(stored)) == Decimal("1.00")
    # 1.125 is a tie: half-even keeps the even neighbour (1.12); 1.135 goes up to 1.14.
    for typed, saved in (("1.125", "1.12"), ("1.135", "1.14")):
        updated = await client.patch(
            f"/payroll/periods/{period_id}/bonuses/{event_id}",
            json={"amount": typed},
            headers=_auth(auth_token),
        )
        assert updated.status_code == 200, updated.text
        assert Decimal(str(updated.json()["amount"])) == Decimal(saved)
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_negative_amount_rejected(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_assignment_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_assignment_id": cp3a_driver_assignment_id, "amount": "-50.00"},
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
    cp3a_driver_assignment_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_assignment_id": cp3a_driver_assignment_id, "amount": "100.00"},
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
    cp3a_driver_assignment_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_assignment_id": cp3a_driver_assignment_id, "amount": "100.00"},
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
    cp3a_driver_assignment_id: int,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_assignment_id": cp3a_driver_assignment_id, "amount": "100.00"},
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

# ---------------------------------------------------------------------------
# 6. Finalization bridge
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 7. Preview bridge
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 9. P1 Fix 1 — Bonus create driver eligibility / branch ownership
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_bonus_eligible_driver_succeeds(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3a_branch_id: int,
    cp3a_driver_assignment_id: int,
) -> None:
    """POST /bonuses with a driver eligible for the period's branch succeeds."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_assignment_id": cp3a_driver_assignment_id, "amount": "80.00"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 201, r.text
    assert r.json()["driver_assignment_id"] == cp3a_driver_assignment_id

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
    driver_number = f"CP3A-WRONGBRANCH-{uuid.uuid4().hex[:6]}"
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id":   other_branch_id,
            "full_name":   "CP3A Wrong Branch Driver",
            "driver_number": driver_number,
        },
        headers=_auth(auth_token),
    )
    assert resp.status_code == 201, resp.text
    wrong_branch_driver_assignment_id = resp.json()["driver_assignment_id"]

    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_assignment_id": wrong_branch_driver_assignment_id, "amount": "80.00"},
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
    driver_number = f"CP3A-INELIGIBLE-{uuid.uuid4().hex[:6]}"
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id":   cp3a_branch_id,
            "full_name":   "CP3A Ineligible Driver",
            "driver_number": driver_number,
        },
        headers=_auth(auth_token),
    )
    assert resp.status_code == 201, resp.text
    ineligible_driver_assignment_id = resp.json()["driver_assignment_id"]

    # Terminate the driver's employment so they are not eligible in 2096.
    await db_conn.execute(
        _text("""
            UPDATE core.employees
            SET    terminationdate = '2090-01-01'
            WHERE  employeeid = (
                SELECT employeeid FROM core.driverassignments
                WHERE  driverassignmentid = :did
            )
        """),
        {"did": ineligible_driver_assignment_id},
    )
    await db_conn.commit()

    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3a_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_assignment_id": ineligible_driver_assignment_id, "amount": "80.00"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 422, (
        f"Expected 422 for ineligible driver, got {r.status_code}: {r.text}"
    )

    await _cancel_period_db(db_conn, period_id)
