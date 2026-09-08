"""
CP-3B1: Zero-inclusive canonical bonus summary — full test suite.

Covers:
  - Zero-inclusive roster: all CP-2E snapshot-eligible drivers appear, even with
    zero bonus events.
  - Exact eligibility roster: non-snapshot drivers never appear, even with a
    contaminated PayrollBonusEvents row.
  - TerminatedHistorical / Transferred / IncludedByExistingData snapshot rows
    remain visible with their reason codes.
  - Active vs voided aggregation: voided events listed but excluded from totals.
  - Multiple events per driver aggregate correctly.
  - Sorting: nonzero totals first (descending), then name/code/id.
  - Status behavior: Draft rejected; Open/Returned editable; InReview/Approved/
    Cancelled read-only.
  - Permissions: view-only capabilities false; ODA denied; branch isolation.
  - No-marker periods: controlled 422, no live-roster fallback.
  - Existing GET /bonuses event list unchanged; /period-pay BONUS still blocked.

Dates: 2095-* — isolated year (CP-3A uses 2096, CP-2F 2097, CP-2E 2099).

Run from backend/:
    python -B -m pytest tests/test_cp3b1_bonus_summary.py -v -p no:cacheprovider
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
_d = datetime.date(2095, 6, 1)
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
    """Insert an isolated CP3B1 period without deleting historical fixtures."""
    await db.execute(_text("""
        UPDATE payroll.payrollperiods
        SET status = 'Cancelled'
        WHERE branchid = :bid
          AND periodcode LIKE :run_prefix
          AND status = 'Open'
    """), {"bid": branch_id, "run_prefix": f"CP3B1-{_RUN_ID}-%"})
    code = f"CP3B1-{_RUN_ID}-{branch_id}-{start.isoformat()}"
    r = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, :status, :code, :name, 'Week', :start, :end)
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "status": status, "code": code,
         "name": f"CP3B1 {start}", "start": start, "end": end},
    )).mappings().first()
    await db.commit()
    assert r is not None
    return r["payrollperiodid"]


async def _cancel_period_db(db: AsyncConnection, period_id: int) -> None:
    await db.execute(_text(
        "ALTER TABLE payroll.payrollperiods DISABLE TRIGGER trg_period_status_revert"
    ))
    # Clear the Returned pointer too — ck_PayrollPeriods_ReturnedPointerConsistency
    # forbids a non-Returned status with CurrentReturnReviewItemID set.
    await db.execute(
        _text("UPDATE payroll.payrollperiods "
              "SET status = 'Cancelled', currentreturnreviewitemid = NULL "
              "WHERE payrollperiodid = :pid"),
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
    """Seed a CP-2E eligibility snapshot: marker row + one detail row per entry.

    Each entry: {"driver_id", "code", "name", "reason"}.
    """
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
                        TRUE, :reason, 'Generated', NOW(), NOW())
                ON CONFLICT (payrollperiodid, driverid) DO NOTHING
            """),
            {
                "bid":    branch_id,
                "pid":    period_id,
                "did":    e["driver_id"],
                "code":   e["code"],
                "name":   e["name"],
                "reason": e.get("reason", "Active"),
            },
        )
    await db.commit()


async def _post_bonus(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    amount: str,
) -> int:
    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": driver_id, "amount": amount},
        headers=_auth(token),
    )
    assert r.status_code == 201, f"Bonus create failed: {r.text}"
    return r.json()["bonus_event_id"]


async def _get_summary(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
) -> httpx.Response:
    return await client.get(
        f"/payroll/periods/{period_id}/bonuses/summary",
        headers=_auth(token),
    )


def _driver_row(summary: dict, driver_id: int) -> dict | None:
    return next((d for d in summary["drivers"] if d["driver_id"] == driver_id), None)


async def _advance_to_approved(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
) -> None:
    """Push period through Open -> InReview -> Approved via the review system."""
    r = await client.patch(
        f"/payroll/periods/{period_id}/status",
        json={"status": "InReview"},
        headers=_auth(token),
    )
    assert r.status_code == 200, f"Submit (InReview) failed: {r.text}"

    rv = await client.get("/review/items", headers=_auth(token))
    assert rv.status_code == 200, f"GET /review/items failed: {rv.text}"
    review_item = next(
        (
            it for it in rv.json()
            if it.get("entity_name") == "PayrollPeriods"
            and str(it.get("entity_id")) == str(period_id)
            and it.get("status") == "Pending"
        ),
        None,
    )
    assert review_item is not None, f"No review item found for period {period_id}"

    decide = await client.post(
        f"/review/items/{review_item['review_item_id']}/decide",
        headers=_auth(token),
        json={"decision": "Approved"},
    )
    assert decide.status_code == 200, f"Approval failed: {decide.text}"


async def _create_role_with_perms(
    client: httpx.AsyncClient,
    token: str,
    role_name: str,
    perms: list,
) -> int:
    cr = await client.post(
        "/admin/company-roles",
        json={"role_name": role_name},
        headers=_auth(token),
    )
    assert cr.status_code == 201, f"Create role failed: {cr.text}"
    role_id = cr.json()["company_role_id"]
    if perms:
        pr = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": perms},
            headers=_auth(token),
        )
        assert pr.status_code == 200, f"Set permissions failed: {pr.text}"
    return role_id


async def _create_user_with_role(
    client: httpx.AsyncClient,
    admin_token: str,
    username: str,
    role_id: int,
    scope_type: str = "AllCompanyBranches",
    branch_id=None,
    password: str = "TestPass123!",
) -> str:
    resp = await client.post(
        "/admin/users",
        json={
            "username": username,
            "display_name": username,
            "password": password,
            "is_active": True,
            "can_login": True,
            "must_change_password": False,
        },
        headers=_auth(admin_token),
    )
    assert resp.status_code == 201, f"Create user failed: {resp.text}"
    user_id = resp.json()["user_id"]

    assign_body: dict = {"company_role_id": role_id, "scope_type": scope_type}
    if branch_id is not None:
        assign_body["branch_id"] = branch_id

    assign_resp = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json=assign_body,
        headers=_auth(admin_token),
    )
    assert assign_resp.status_code in (200, 201), f"Assign role failed: {assign_resp.text}"

    login = await client.post("/auth/login", json={
        "username": username,
        "password": password,
        "company_code": "DEMO",
    })
    assert login.status_code == 200, f"Login failed: {login.text}"
    return login.json()["access_token"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def cp3b1_branch_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
) -> int:
    resp = await session_client.get("/core/branches", headers=_auth(auth_token))
    assert resp.status_code == 200, resp.text
    for branch in resp.json():
        if branch["branch_code"] == "PAYTEST":
            return branch["branch_id"]
    raise AssertionError("PAYTEST branch not found")


async def _get_or_create_driver(
    session_client: httpx.AsyncClient,
    auth_token: str,
    branch_id: int,
    driver_code: str,
    full_name: str,
) -> int:
    r_list = await session_client.get("/core/drivers", headers=_auth(auth_token))
    if r_list.status_code == 200:
        for d in r_list.json():
            if d.get("driver_code") == driver_code:
                return d["driver_id"]
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id":   branch_id,
            "full_name":   full_name,
            "driver_code": driver_code,
        },
        headers=_auth(auth_token),
    )
    assert resp.status_code == 201, f"Driver seed failed: {resp.text}"
    return resp.json()["driver_id"]


@pytest_asyncio.fixture
async def cp3b1_drivers(
    session_client: httpx.AsyncClient,
    auth_token: str,
    cp3b1_branch_id: int,
) -> dict[str, int]:
    """Four PAYTEST drivers with alphabetically ordered names for sort tests."""
    out: dict[str, int] = {}
    for key, name in [
        ("alpha", "CP3B1 Alpha Driver"),
        ("beta",  "CP3B1 Beta Driver"),
        ("gamma", "CP3B1 Gamma Driver"),
        ("zeta",  "CP3B1 Zeta Driver"),
    ]:
        out[key] = await _get_or_create_driver(
            session_client, auth_token, cp3b1_branch_id,
            driver_code=f"CP3B1-{key.upper()}", full_name=name,
        )
    return out


def _roster(drivers: dict[str, int], keys: list[str], reason: str = "Active") -> list[dict]:
    return [
        {
            "driver_id": drivers[k],
            "code":      f"CP3B1-{k.upper()}",
            "name":      f"CP3B1 {k.capitalize()} Driver",
            "reason":    reason,
        }
        for k in keys
    ]


# ---------------------------------------------------------------------------
# 1. Zero-inclusive roster
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zero_inclusive_roster(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    """3 snapshot drivers, 1 with a bonus — all 3 appear; zero drivers show 0."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end)
    await _seed_snapshot(
        db_conn, cp3b1_branch_id, period_id,
        _roster(cp3b1_drivers, ["alpha", "beta", "gamma"]),
    )
    await _post_bonus(client, auth_token, period_id, cp3b1_drivers["alpha"], "100.00")

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    summary = r.json()
    assert len(summary["drivers"]) == 3
    assert Decimal(_driver_row(summary, cp3b1_drivers["alpha"])["total_bonus"]) == Decimal("100.00")
    for key in ("beta", "gamma"):
        row = _driver_row(summary, cp3b1_drivers[key])
        assert row is not None, f"Zero-bonus driver {key} missing from summary"
        assert Decimal(row["total_bonus"]) == Decimal("0")
        assert row["active_event_count"] == 0
        assert row["events"] == []
    assert summary["eligibility_source"] == "PeriodEligibilitySnapshot"

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 2. Exact eligibility roster — no expansion from BonusEvents or live data
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_only_snapshot_drivers_appear(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    """A live-eligible driver outside the snapshot never appears — even with a
    contaminated PayrollBonusEvents row — and their events don't pollute totals."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end)
    # Snapshot contains only alpha; gamma (a real Active PAYTEST driver) is excluded.
    await _seed_snapshot(
        db_conn, cp3b1_branch_id, period_id,
        _roster(cp3b1_drivers, ["alpha"]),
    )
    # Contaminated bonus event for the non-snapshot driver, inserted directly.
    await db_conn.execute(
        _text("""
            INSERT INTO payroll.payrollbonusevents
                (companyid, branchid, payrollperiodid, driverid,
                 amount, status, createdbyuserid, createdatutc, datarevision)
            VALUES (1, :bid, :pid, :did, 500.00, 'Active', 1, NOW(), 1)
        """),
        {"bid": cp3b1_branch_id, "pid": period_id, "did": cp3b1_drivers["gamma"]},
    )
    await db_conn.commit()

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    summary = r.json()
    driver_ids = [d["driver_id"] for d in summary["drivers"]]
    assert driver_ids == [cp3b1_drivers["alpha"]], (
        f"Only snapshot drivers may appear; got {driver_ids}"
    )
    # The contaminated event must not leak into the top-level totals either.
    assert Decimal(summary["active_bonus_total"]) == Decimal("0")
    assert summary["active_event_count"] == 0

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 3. Mid-period removed/terminated driver remains visible
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_terminated_historical_driver_visible(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end)
    await _seed_snapshot(db_conn, cp3b1_branch_id, period_id, [
        *_roster(cp3b1_drivers, ["alpha"]),
        {
            "driver_id": cp3b1_drivers["beta"],
            "code":      "CP3B1-BETA",
            "name":      "CP3B1 Beta Driver",
            "reason":    "TerminatedHistorical",
        },
        {
            "driver_id": cp3b1_drivers["gamma"],
            "code":      "CP3B1-GAMMA",
            "name":      "CP3B1 Gamma Driver",
            "reason":    "Transferred",
        },
    ])

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    summary = r.json()

    beta = _driver_row(summary, cp3b1_drivers["beta"])
    assert beta is not None, "TerminatedHistorical driver must stay visible"
    assert beta["eligibility_reason_code"] == "TerminatedHistorical"
    assert Decimal(beta["total_bonus"]) == Decimal("0")

    gamma = _driver_row(summary, cp3b1_drivers["gamma"])
    assert gamma is not None, "Transferred driver must stay visible"
    assert gamma["eligibility_reason_code"] == "Transferred"

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 4. IncludedByExistingData reason preserved
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_included_by_existing_data_reason_preserved(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end)
    await _seed_snapshot(db_conn, cp3b1_branch_id, period_id, [
        {
            "driver_id": cp3b1_drivers["alpha"],
            "code":      "CP3B1-ALPHA",
            "name":      "CP3B1 Alpha Driver",
            "reason":    "IncludedByExistingData",
        },
    ])

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    row = _driver_row(r.json(), cp3b1_drivers["alpha"])
    assert row is not None
    assert row["eligibility_reason_code"] == "IncludedByExistingData"

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 5. Active vs voided totals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_active_vs_voided_totals(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end)
    await _seed_snapshot(
        db_conn, cp3b1_branch_id, period_id, _roster(cp3b1_drivers, ["alpha"])
    )
    keep_id = await _post_bonus(client, auth_token, period_id, cp3b1_drivers["alpha"], "150.00")
    void_id = await _post_bonus(client, auth_token, period_id, cp3b1_drivers["alpha"], "999.00")
    rv = await client.delete(
        f"/payroll/periods/{period_id}/bonuses/{void_id}",
        headers=_auth(auth_token),
    )
    assert rv.status_code == 200, rv.text

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    row = _driver_row(r.json(), cp3b1_drivers["alpha"])
    assert Decimal(row["total_bonus"]) == Decimal("150.00"), (
        "Voided event must not contribute to total_bonus"
    )
    assert row["active_event_count"] == 1
    assert row["voided_event_count"] == 1
    assert len(row["events"]) == 2, "Voided events stay visible in the events array"
    statuses = {e["bonus_event_id"]: e["status"] for e in row["events"]}
    assert statuses[keep_id] == "Active"
    assert statuses[void_id] == "Voided"

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 6. Multiple events per driver
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiple_events_aggregate(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end)
    await _seed_snapshot(
        db_conn, cp3b1_branch_id, period_id, _roster(cp3b1_drivers, ["alpha"])
    )
    for amt in ("50.00", "75.00", "25.00"):
        await _post_bonus(client, auth_token, period_id, cp3b1_drivers["alpha"], amt)

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    summary = r.json()
    row = _driver_row(summary, cp3b1_drivers["alpha"])
    assert Decimal(row["total_bonus"]) == Decimal("150.00")
    assert row["active_event_count"] == 3
    assert len(row["events"]) == 3
    assert Decimal(summary["active_bonus_total"]) == Decimal("150.00")
    assert summary["active_event_count"] == 3

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 7. Sorting
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sorting(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    """Nonzero totals first (desc), then zero-total drivers by name/code/id."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end)
    await _seed_snapshot(
        db_conn, cp3b1_branch_id, period_id,
        _roster(cp3b1_drivers, ["alpha", "beta", "gamma", "zeta"]),
    )
    await _post_bonus(client, auth_token, period_id, cp3b1_drivers["zeta"], "250.00")
    await _post_bonus(client, auth_token, period_id, cp3b1_drivers["alpha"], "100.00")

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    order = [d["driver_id"] for d in r.json()["drivers"]]
    assert order == [
        cp3b1_drivers["zeta"],    # 250.00 — highest total first
        cp3b1_drivers["alpha"],   # 100.00
        cp3b1_drivers["beta"],    # 0 — 'Beta' sorts before 'Gamma'
        cp3b1_drivers["gamma"],   # 0
    ], f"Unexpected sort order: {order}"

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 8. Draft blocked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_draft_blocked(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end, status="Draft")
    await _seed_snapshot(
        db_conn, cp3b1_branch_id, period_id, _roster(cp3b1_drivers, ["alpha"])
    )

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 422, (
        f"Draft period must reject summary, got {r.status_code}: {r.text}"
    )

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 9. Open / Returned editable capabilities
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_editable_capabilities(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    """A normal Active-eligible driver with an active event gets full capabilities."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end, status="Open")
    await _seed_snapshot(
        db_conn, cp3b1_branch_id, period_id, _roster(cp3b1_drivers, ["alpha"])
    )
    await _post_bonus(client, auth_token, period_id, cp3b1_drivers["alpha"], "45.00")

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    caps = r.json()["drivers"][0]["capabilities"]
    assert caps["can_create"] is True
    assert caps["can_update"] is True
    assert caps["can_void"] is True
    assert caps["reason_codes"] == []

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_returned_editable_capabilities(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    """Reach Returned via the real review flow (Open -> InReview -> Returned)."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end, status="Open")
    await _seed_snapshot(
        db_conn, cp3b1_branch_id, period_id, _roster(cp3b1_drivers, ["alpha"])
    )
    await _post_bonus(client, auth_token, period_id, cp3b1_drivers["alpha"], "70.00")

    r_ir = await client.patch(
        f"/payroll/periods/{period_id}/status",
        json={"status": "InReview"},
        headers=_auth(auth_token),
    )
    assert r_ir.status_code == 200, f"InReview transition failed: {r_ir.text}"

    rv = await client.get("/review/items", headers=_auth(auth_token))
    assert rv.status_code == 200, rv.text
    review_item = next(
        (
            it for it in rv.json()
            if it.get("entity_name") == "PayrollPeriods"
            and str(it.get("entity_id")) == str(period_id)
            and it.get("status") == "Pending"
        ),
        None,
    )
    assert review_item is not None, f"No review item found for period {period_id}"
    decide = await client.post(
        f"/review/items/{review_item['review_item_id']}/decide",
        headers=_auth(auth_token),
        json={"decision": "EditRequested", "decision_reason": "CP3B1 capabilities test"},
    )
    assert decide.status_code == 200, f"Return decision failed: {decide.text}"

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["period_status"] == "Returned"
    caps = summary["drivers"][0]["capabilities"]
    assert caps["can_create"] is True and caps["can_update"] is True and caps["can_void"] is True

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 10. InReview / Approved / Cancelled read-only capabilities
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inreview_readonly(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end)
    await _seed_snapshot(
        db_conn, cp3b1_branch_id, period_id, _roster(cp3b1_drivers, ["alpha"])
    )
    await _post_bonus(client, auth_token, period_id, cp3b1_drivers["alpha"], "80.00")

    r_ir = await client.patch(
        f"/payroll/periods/{period_id}/status",
        json={"status": "InReview"},
        headers=_auth(auth_token),
    )
    assert r_ir.status_code == 200, f"InReview transition failed: {r_ir.text}"

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["period_status"] == "InReview"
    caps = summary["drivers"][0]["capabilities"]
    assert caps["can_create"] is False
    assert caps["can_update"] is False
    assert caps["can_void"] is False
    assert "status_read_only" in caps["reason_codes"]

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_approved_readonly(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end)
    await _seed_snapshot(
        db_conn, cp3b1_branch_id, period_id, _roster(cp3b1_drivers, ["alpha"])
    )
    await _post_bonus(client, auth_token, period_id, cp3b1_drivers["alpha"], "80.00")
    await _advance_to_approved(client, auth_token, period_id)

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["period_status"] == "Approved"
    caps = summary["drivers"][0]["capabilities"]
    assert caps["can_create"] is False
    assert "status_read_only" in caps["reason_codes"]
    # Bonus data remains visible read-only
    assert Decimal(summary["active_bonus_total"]) == Decimal("80.00")

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_cancelled_readonly_historical(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end)
    await _seed_snapshot(
        db_conn, cp3b1_branch_id, period_id, _roster(cp3b1_drivers, ["alpha"])
    )
    await _post_bonus(client, auth_token, period_id, cp3b1_drivers["alpha"], "60.00")
    await _cancel_period_db(db_conn, period_id)

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["period_status"] == "Cancelled"
    caps = summary["drivers"][0]["capabilities"]
    assert caps["can_create"] is False
    assert "status_read_only" in caps["reason_codes"]


# ---------------------------------------------------------------------------
# 11. View-only permission
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_view_only_user_reads_but_cannot_mutate(
    client: httpx.AsyncClient,
    auth_token: str,
    branch_user_token: str,
    db_conn: AsyncConnection,
    hq_branch_id: int,
    hq_driver_id: int,
) -> None:
    """branch_user (payroll.view only, HQ scope) reads the summary with all
    mutation capabilities false and a permission reason code."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, hq_branch_id, start, end, status="Open")
    await _seed_snapshot(db_conn, hq_branch_id, period_id, [
        {"driver_id": hq_driver_id, "code": "HQD-001", "name": "HQ Driver", "reason": "Active"},
    ])

    r = await _get_summary(client, branch_user_token, period_id)
    assert r.status_code == 200, r.text
    caps = r.json()["drivers"][0]["capabilities"]
    assert caps["can_create"] is False
    assert caps["can_update"] is False
    assert caps["can_void"] is False
    assert "permission_entry_required" in caps["reason_codes"]

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 12. ODA / driver denial
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_oda_user_denied(
    session_client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end)
    await _seed_snapshot(
        db_conn, cp3b1_branch_id, period_id, _roster(cp3b1_drivers, ["alpha"])
    )

    role_id = await _create_role_with_perms(
        session_client, auth_token,
        f"CP3B1_ODA_Role_{_RUN_ID}",
        ["payroll.view", "payroll.entry"],
    )
    oda_token = await _create_user_with_role(
        session_client, auth_token,
        f"cp3b1_oda_user_{_RUN_ID}",
        role_id,
        scope_type="OwnDriverDataOnly",
        branch_id=cp3b1_branch_id,
    )

    r = await _get_summary(session_client, oda_token, period_id)
    assert r.status_code == 403, (
        f"ODA user must be denied the bonus summary, got {r.status_code}: {r.text}"
    )

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 13. Branch / company isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_branch_isolation(
    client: httpx.AsyncClient,
    auth_token: str,
    branch_user_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    """branch_user is scoped to HQ only — a PAYTEST period summary must be denied."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end)
    await _seed_snapshot(
        db_conn, cp3b1_branch_id, period_id, _roster(cp3b1_drivers, ["alpha"])
    )

    r = await _get_summary(client, branch_user_token, period_id)
    assert r.status_code in (403, 404), (
        f"Cross-branch summary must be denied, got {r.status_code}: {r.text}"
    )

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 14. No-marker behavior — controlled unavailable, no live fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_marker_returns_controlled_unavailable(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    """A period without a CP-2E marker gets 422 with an explicit code — the
    live roster (which WOULD contain the active CP3B1 drivers) is never used."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end)
    # No snapshot seeded — live PAYTEST roster has active drivers that a
    # fallback would have returned.

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 422, (
        f"No-marker period must return controlled 422, got {r.status_code}: {r.text}"
    )
    detail = r.json()["detail"]
    assert "BONUS_SUMMARY_UNAVAILABLE_NO_ELIGIBILITY_SNAPSHOT" in detail
    assert "drivers" not in r.json(), "No roster data may leak on the unavailable path"

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 15. Existing GET /bonuses unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_event_list_endpoint_unchanged(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end)
    await _seed_snapshot(
        db_conn, cp3b1_branch_id, period_id, _roster(cp3b1_drivers, ["alpha"])
    )
    await _post_bonus(client, auth_token, period_id, cp3b1_drivers["alpha"], "40.00")

    r = await client.get(
        f"/payroll/periods/{period_id}/bonuses",
        headers=_auth(auth_token),
    )
    assert r.status_code == 200, r.text
    items = r.json()
    assert isinstance(items, list), "GET /bonuses must remain a flat event list"
    assert len(items) == 1
    event = items[0]
    assert event["bonus_event_id"] > 0
    assert event["amount"] == "40.00"
    assert "capabilities" not in event, "Event list shape must not gain summary fields"
    assert "total_bonus" not in event

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 16. CP-3A regression — /period-pay still rejects BONUS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_period_pay_bonus_still_blocked(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end)

    r = await client.post(
        f"/payroll/periods/{period_id}/period-pay",
        json={"driver_id": cp3b1_drivers["alpha"], "amount": 100, "line_type": "BONUS"},
        headers=_auth(auth_token),
    )
    assert r.status_code == 422, r.text
    assert "/bonuses" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 17. P1 Fix 1 — Branch-scoped event aggregation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cross_branch_contaminated_event_excluded(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    hq_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    """A same-company PayrollBonusEvents row with the correct PayrollPeriodID
    but a WRONG BranchID must not contribute to the driver's total, event
    list, or the top-level aggregates.

    This is the exact scenario Codex P1 Fix 1 flagged: aggregation filtered
    by PeriodID + CompanyID only, missing BranchID. The read-side BranchID
    filter added for that fix is exercised here directly (bypassing the ORM
    layer with a raw UPDATE) as a defense-in-depth check.

    As of CP-3B2a, a second, independent defense also exists: a DB-level
    ownership trigger on PayrollBonusEvents rejects any INSERT/UPDATE whose
    CompanyID/BranchID does not match the owning PayrollPeriods row — so the
    contaminated row can no longer even be inserted through normal SQL. That
    trigger is asserted separately in
    test_cp3b2a_bonus_batch_safety.py::test_mismatched_branch_insert_rejected.
    To keep exercising this file's own read-side BranchID filter, the
    contaminated row is produced here via UPDATE while the trigger is
    disabled — simulating pre-CP-3B2a contamination or a future bulk-load
    path that bypasses the trigger.
    """
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end)
    await _seed_snapshot(
        db_conn, cp3b1_branch_id, period_id, _roster(cp3b1_drivers, ["alpha"])
    )

    # Legitimate same-branch bonus.
    good_event_id = await _post_bonus(client, auth_token, period_id, cp3b1_drivers["alpha"], "50.00")

    # Contaminated row: insert a second Active event normally (passes the
    # CP-3B2a ownership trigger because BranchID is correct at insert time),
    # then flip its BranchID to HQ via a direct UPDATE with the trigger
    # temporarily disabled — simulating contamination that predates the
    # trigger or a bypass path, which is exactly what the read-side BranchID
    # filter defends against independently of the trigger.
    bad_event_id = await _post_bonus(client, auth_token, period_id, cp3b1_drivers["alpha"], "9999.00")
    await db_conn.execute(_text(
        "ALTER TABLE payroll.payrollbonusevents DISABLE TRIGGER trg_bonusevents_ownership"
    ))
    await db_conn.execute(
        _text("""
            UPDATE payroll.payrollbonusevents
            SET    branchid = :bid
            WHERE  payrollbonuseventid = :beid
        """),
        {"bid": hq_branch_id, "beid": bad_event_id},
    )
    await db_conn.execute(_text(
        "ALTER TABLE payroll.payrollbonusevents ENABLE TRIGGER trg_bonusevents_ownership"
    ))
    await db_conn.commit()

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    summary = r.json()
    row = _driver_row(summary, cp3b1_drivers["alpha"])
    assert row is not None

    assert Decimal(row["total_bonus"]) == Decimal("50.00"), (
        "Cross-branch contaminated event must not inflate total_bonus — "
        f"got {row['total_bonus']}"
    )
    assert row["active_event_count"] == 1
    assert row["voided_event_count"] == 0
    assert len(row["events"]) == 1, "Contaminated cross-branch row must not appear in events[]"
    assert row["events"][0]["bonus_event_id"] == good_event_id
    assert all(Decimal(e["amount"]) != Decimal("9999.00") for e in row["events"])

    assert Decimal(summary["active_bonus_total"]) == Decimal("50.00")
    assert summary["active_event_count"] == 1

    await _cancel_period_db(db_conn, period_id)


# ---------------------------------------------------------------------------
# 18. P1 Fix 2 — Per-driver capability consistency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_included_by_existing_data_not_creatable(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    """A. IncludedByExistingData driver with NO existing period-pay source is
    visible in the summary but can_create must be false, matching the fact
    that POST /bonuses would reject the same driver."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end)
    await _seed_snapshot(db_conn, cp3b1_branch_id, period_id, [
        {
            "driver_id": cp3b1_drivers["alpha"],
            "code":      "CP3B1-ALPHA",
            "name":      "CP3B1 Alpha Driver",
            "reason":    "IncludedByExistingData",
        },
    ])
    # No DraftLine/PPDES period-pay source exists for this driver — the
    # eligibility guard used by create_bonus_event must reject creation.

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    row = _driver_row(r.json(), cp3b1_drivers["alpha"])
    assert row is not None, "IncludedByExistingData driver must remain visible"
    assert row["eligibility_reason_code"] == "IncludedByExistingData"
    assert Decimal(row["total_bonus"]) == Decimal("0")
    assert row["capabilities"]["can_create"] is False, (
        "can_create must be false when POST /bonuses would reject this driver"
    )
    assert "eligibility_existing_data_only" in row["capabilities"]["reason_codes"]

    # Prove consistency: the actual create endpoint rejects the same driver.
    create_resp = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": cp3b1_drivers["alpha"], "amount": "10.00"},
        headers=_auth(auth_token),
    )
    assert create_resp.status_code == 422, (
        f"Expected create to reject IncludedByExistingData driver with no "
        f"existing source, got {create_resp.status_code}: {create_resp.text}"
    )

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_normal_eligible_driver_creatable(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    """B. A normal Active-eligible driver in an Open period with payroll.entry
    has can_create = true."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end, status="Open")
    await _seed_snapshot(
        db_conn, cp3b1_branch_id, period_id, _roster(cp3b1_drivers, ["alpha"])
    )

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    row = _driver_row(r.json(), cp3b1_drivers["alpha"])
    assert row["capabilities"]["can_create"] is True

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_zero_event_driver_update_void_false(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    """C. Eligible driver with no active bonus events: can_create may be true,
    but can_update/can_void must be false (nothing to update or void)."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end, status="Open")
    await _seed_snapshot(
        db_conn, cp3b1_branch_id, period_id, _roster(cp3b1_drivers, ["alpha"])
    )

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    row = _driver_row(r.json(), cp3b1_drivers["alpha"])
    assert row["active_event_count"] == 0
    caps = row["capabilities"]
    assert caps["can_create"] is True
    assert caps["can_update"] is False
    assert caps["can_void"] is False
    assert "no_active_bonus_event" in caps["reason_codes"]

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_active_event_driver_update_void_true(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    """D. Eligible driver with an active event in Open/Returned + payroll.entry:
    can_update = true, can_void = true."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end, status="Open")
    await _seed_snapshot(
        db_conn, cp3b1_branch_id, period_id, _roster(cp3b1_drivers, ["alpha"])
    )
    await _post_bonus(client, auth_token, period_id, cp3b1_drivers["alpha"], "35.00")

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    row = _driver_row(r.json(), cp3b1_drivers["alpha"])
    caps = row["capabilities"]
    assert caps["can_update"] is True
    assert caps["can_void"] is True
    assert "no_active_bonus_event" not in caps["reason_codes"]

    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_only_voided_events_update_void_false(
    client: httpx.AsyncClient,
    auth_token: str,
    db_conn: AsyncConnection,
    cp3b1_branch_id: int,
    cp3b1_drivers: dict[str, int],
) -> None:
    """E. Driver with only Voided events: event stays visible, total_bonus is 0,
    can_update/can_void must be false (no active event to act on)."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3b1_branch_id, start, end, status="Open")
    await _seed_snapshot(
        db_conn, cp3b1_branch_id, period_id, _roster(cp3b1_drivers, ["alpha"])
    )
    event_id = await _post_bonus(client, auth_token, period_id, cp3b1_drivers["alpha"], "88.00")
    void_resp = await client.delete(
        f"/payroll/periods/{period_id}/bonuses/{event_id}",
        headers=_auth(auth_token),
    )
    assert void_resp.status_code == 200, void_resp.text

    r = await _get_summary(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    row = _driver_row(r.json(), cp3b1_drivers["alpha"])
    assert Decimal(row["total_bonus"]) == Decimal("0")
    assert row["active_event_count"] == 0
    assert row["voided_event_count"] == 1
    assert len(row["events"]) == 1, "Voided event must remain visible in events[]"
    caps = row["capabilities"]
    assert caps["can_update"] is False
    assert caps["can_void"] is False
    assert "no_active_bonus_event" in caps["reason_codes"]

    await _cancel_period_db(db_conn, period_id)

    await _cancel_period_db(db_conn, period_id)
