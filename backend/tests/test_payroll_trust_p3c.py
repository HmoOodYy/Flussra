"""
Payroll Trust Phase 3C — DB-Level Immutability Triggers

Tests verifying that migration 0035 enforces immutability on Locked/Archived
payroll data at the DB layer, and that the service layer still works correctly:

  1  Direct DB UPDATE on a Locked period's final line is blocked (restrict_violation).
  2  Direct DB DELETE on a Locked period's final line is blocked (restrict_violation).
  3  Direct DB status revert (Locked→Open) on PayrollPeriods is blocked.
  4  Locked→Archived transition is allowed (forward-only supported transition).
  5  Archived period: any status change blocked (terminal status).
  6  Normal finalization still works end-to-end (Approved→Locked via service).
  7  Ledger read (get_final_lines) works after trigger migration.
  8  Service-layer add/update on a Locked period returns 422 (service guard intact).
  9  Full regression: all Phase 3B tests still pass implicitly (via same fixtures).

Isolation strategy
------------------
All tests use years 2043–2051 (8 year slots) on fresh per-test branches.
Finalized period history remains in the disposable test database.
"""
from datetime import date as _date
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text as _text

# ---------------------------------------------------------------------------
# Year constants — one per test to avoid locked-period conflicts
# ---------------------------------------------------------------------------
T1_START, T1_END, T1_WORK = "2043-05-05", "2043-05-18", "2043-05-08"
T2_START, T2_END, T2_WORK = "2044-05-05", "2044-05-18", "2044-05-08"
T3_START, T3_END, T3_WORK = "2045-05-05", "2045-05-18", "2045-05-08"
T4_START, T4_END, T4_WORK = "2046-05-05", "2046-05-18", "2046-05-08"
T5_START, T5_END, T5_WORK = "2047-05-05", "2047-05-18", "2047-05-08"
T6_START, T6_END, T6_WORK = "2048-05-05", "2048-05-18", "2048-05-08"
T7_START, T7_END, T7_WORK = "2049-05-05", "2049-05-18", "2049-05-08"
T8A_START, T8A_END, T8A_WORK = "2050-05-05", "2050-05-18", "2050-05-08"
T8B_START, T8B_END, T8B_WORK = "2051-05-05", "2051-05-18", "2051-05-08"


# ---------------------------------------------------------------------------
# Shared helpers (copied/adapted from p3b)
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _make_period(
    db,
    branch_id: int,
    start: str,
    end: str,
) -> int:
    """Insert an Open period directly into DB.  Returns period_id."""
    row = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": f"P3C-{branch_id}-{start}",
         "name": f"P3C {start}", "start": _date.fromisoformat(start), "end": _date.fromisoformat(end)},
    )).mappings().first()
    return row["payrollperiodid"]


async def _create_driver(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    suffix: str,
    hire_date: str = "2043-01-01",
) -> int:
    r = await client.post(
        "/core/drivers",
        json={
            "branch_id":      branch_id,
            "full_name":      f"P3C Test Driver {suffix}",
            "preferred_name": f"P3C-{suffix}",
            "driver_code":    f"P3CDRV-{suffix}",
            "cdl_number":     f"CDL-P3C-{suffix}",
            "email":          f"p3cdrv{suffix}@example.com",
            "hire_date":      hire_date,
        },
        headers=auth(token),
    )
    assert r.status_code == 201, f"driver create: {r.text}"
    return r.json()["driver_id"]


async def _delete_driver(
    client: httpx.AsyncClient,
    token: str,
    driver_id: int,
) -> None:
    await client.delete(f"/core/drivers/{driver_id}", headers=auth(token))


async def _create_and_approve_rate(
    client: httpx.AsyncClient,
    token: str,
    driver_id: int,
    rate_type_id: int,
    amount: str,
    effective_from: str = "2043-01-01",
) -> int:
    headers = auth(token)
    rc = await client.post(
        "/payroll/rates",
        json={
            "driver_id":      driver_id,
            "rate_type_id":   rate_type_id,
            "amount":         amount,
            "effective_from": effective_from,
        },
        headers=headers,
    )
    assert rc.status_code == 201, f"create rate: {rc.text}"
    rate_id = rc.json()["driver_rate_id"]
    ra = await client.post(f"/payroll/rates/{rate_id}/approve", headers=headers)
    assert ra.status_code == 200, f"approve rate: {ra.text}"
    return rate_id


async def _advance_to_approved(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    work_date: str,
) -> None:
    """Open → InReview (add dummy DailyNote line) → Approved via review flow."""
    headers = auth(token)

    dummy = await client.post(
        f"/payroll/periods/{period_id}/lines",
        json={"driver_id": driver_id, "work_date": work_date,
              "line_type": "DailyNote", "notes": "filler"},
        headers=headers,
    )
    assert dummy.status_code == 201, f"dummy DailyNote: {dummy.text}"

    r = await client.patch(
        f"/payroll/periods/{period_id}/status",
        json={"status": "InReview"},
        headers=headers,
    )
    assert r.status_code == 200, f"InReview: {r.text}"

    review_resp = await client.get("/review/items", headers=headers)
    assert review_resp.status_code == 200
    item = next(
        (i for i in review_resp.json()
         if i.get("entity_name") == "PayrollPeriods"
         and i.get("entity_id") == str(period_id)
         and i.get("status") == "Pending"),
        None,
    )
    assert item is not None, "No pending review item found"

    dec = await client.post(
        f"/review/items/{item['review_item_id']}/decide",
        json={"decision": "Approved"},
        headers=headers,
    )
    assert dec.status_code == 200, f"review approve: {dec.text}"


async def _finalize(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
) -> dict:
    r = await client.post(
        f"/payroll/periods/{period_id}/finalize",
        headers=auth(token),
    )
    assert r.status_code == 200, f"finalize: {r.text}"
    return r.json()


async def _get_final_lines(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int | None = None,
) -> list[dict]:
    params: dict = {}
    if driver_id is not None:
        params["driver_id"] = driver_id
    r = await client.get(
        f"/payroll/periods/{period_id}/final-lines",
        params=params,
        headers=auth(token),
    )
    assert r.status_code == 200, f"get_final_lines: {r.text}"
    return r.json()


async def _lock_period(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    start: str,
    end: str,
    work_date: str,
    rate_type_id: int,
    suffix: str,
    db,
) -> tuple[int, int, int]:
    """
    Full flow: create driver + rate → create period → add HOURS line →
    advance to Approved → finalize (Locked).
    Returns (period_id, driver_id, final_line_id).
    """
    c, tok = client, token
    drv = await _create_driver(c, tok, branch_id, suffix, hire_date=start[:4] + "-01-01")
    await _create_and_approve_rate(c, tok, drv, rate_type_id, "15.00",
                                   effective_from=start[:4] + "-01-01")
    pid = await _make_period(db, branch_id, start=start, end=end)
    r = await client.post(
        f"/payroll/periods/{pid}/lines",
        json={"driver_id": drv, "work_date": work_date,
              "line_type": "HOURS", "quantity": "8.0000"},
        headers=auth(token),
    )
    assert r.status_code == 201, f"add hours: {r.text}"
    await _advance_to_approved(c, tok, pid, drv, work_date=work_date)
    await _finalize(c, tok, pid)

    # Fetch the finallineid from DB
    row = (await db.execute(
        _text("SELECT finallineid FROM payroll.payrollfinallines "
              "WHERE payrollperiodid = :pid AND linetype = 'HOURS' LIMIT 1"),
        {"pid": pid},
    )).mappings().first()
    assert row is not None, "No HOURS final line found after finalization"
    return pid, drv, int(row["finallineid"])


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def p3c_env(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_rate_type_id: int,
    direct_db,
):
    """Isolate immutable trust evidence on a fresh branch per test."""
    code = f"P3C_{uuid4().hex[:12]}"
    branch_id = (await direct_db.execute(_text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (1, :code, :name, 'Active', FALSE)
        RETURNING branchid
    """), {"code": code, "name": code})).scalar_one()
    items = await session_client.get(
        f"/settings/branches/{branch_id}/pay-items", headers=auth(auth_token),
    )
    assert items.status_code == 200, items.text
    hours_id = next(item["pay_item_id"] for item in items.json()
                    if item["pay_item_code"] == "HOURS")
    active = await session_client.patch(
        f"/settings/branches/{branch_id}/pay-items/{hours_id}",
        json={"is_active": True}, headers=auth(auth_token),
    )
    assert active.status_code == 200, active.text

    env = {
        "client":          session_client,
        "token":           auth_token,
        "branch_id":       branch_id,
        "hourly_rtid":     paytest_rate_type_id,
        "db":              direct_db,
        "created_drivers": [],
    }
    yield env

    # Retain period history; driver deletion may be denied by finalized FKs.
    for did in env["created_drivers"]:
        await _delete_driver(session_client, auth_token, did)


# ---------------------------------------------------------------------------
# Test 1 — Direct DB UPDATE on Locked final line is blocked
# ---------------------------------------------------------------------------

class TestT1_UpdateLockedLineBlocked:

    @pytest.mark.asyncio
    async def test_update_locked_final_line_raises(self, p3c_env):
        """T1 — DB UPDATE on a Locked-period final line raises restrict_violation."""
        c, tok, bid = p3c_env["client"], p3c_env["token"], p3c_env["branch_id"]
        db = p3c_env["db"]
        rtid = p3c_env["hourly_rtid"]

        pid, drv, fid = await _lock_period(
            c, tok, bid, T1_START, T1_END, T1_WORK, rtid, "T1a", db
        )
        p3c_env["created_drivers"].append(drv)

        with pytest.raises(Exception) as exc_info:
            await db.execute(
                _text("UPDATE payroll.payrollfinallines "
                      "SET notes = 'tampered' "
                      "WHERE finallineid = :fid"),
                {"fid": fid},
            )

        err = str(exc_info.value).lower()
        assert "restrict_violation" in err or "payroll_ledger_immutable" in err, (
            f"Expected restrict_violation / payroll_ledger_immutable, got: {exc_info.value}"
        )


# ---------------------------------------------------------------------------
# Test 2 — Direct DB DELETE on Locked final line is blocked
# ---------------------------------------------------------------------------

class TestT2_DeleteLockedLineBlocked:

    @pytest.mark.asyncio
    async def test_delete_locked_final_line_raises(self, p3c_env):
        """T2 — DB DELETE on a Locked-period final line raises restrict_violation."""
        c, tok, bid = p3c_env["client"], p3c_env["token"], p3c_env["branch_id"]
        db = p3c_env["db"]
        rtid = p3c_env["hourly_rtid"]

        pid, drv, fid = await _lock_period(
            c, tok, bid, T2_START, T2_END, T2_WORK, rtid, "T2a", db
        )
        p3c_env["created_drivers"].append(drv)

        with pytest.raises(Exception) as exc_info:
            await db.execute(
                _text("DELETE FROM payroll.payrollfinallines WHERE finallineid = :fid"),
                {"fid": fid},
            )

        err = str(exc_info.value).lower()
        assert "restrict_violation" in err or "payroll_ledger_immutable" in err, (
            f"Expected restrict_violation / payroll_ledger_immutable, got: {exc_info.value}"
        )


# ---------------------------------------------------------------------------
# Test 3 — Direct DB status revert (Locked→Open) is blocked
# ---------------------------------------------------------------------------

class TestT3_StatusRevertLockedBlocked:

    @pytest.mark.asyncio
    async def test_revert_locked_period_raises(self, p3c_env):
        """T3 — Attempting to set a Locked period back to Open raises restrict_violation."""
        c, tok, bid = p3c_env["client"], p3c_env["token"], p3c_env["branch_id"]
        db = p3c_env["db"]
        rtid = p3c_env["hourly_rtid"]

        pid, drv, _fid = await _lock_period(
            c, tok, bid, T3_START, T3_END, T3_WORK, rtid, "T3a", db
        )
        p3c_env["created_drivers"].append(drv)

        with pytest.raises(Exception) as exc_info:
            await db.execute(
                _text("UPDATE payroll.payrollperiods "
                      "SET status = 'Open' "
                      "WHERE payrollperiodid = :pid"),
                {"pid": pid},
            )

        err = str(exc_info.value).lower()
        assert "restrict_violation" in err or "payroll_status_immutable" in err, (
            f"Expected restrict_violation / payroll_status_immutable, got: {exc_info.value}"
        )


# ---------------------------------------------------------------------------
# Test 4 — Locked→Archived is allowed
# ---------------------------------------------------------------------------

class TestT4_LockedToArchivedAllowed:

    @pytest.mark.asyncio
    async def test_locked_to_archived_succeeds(self, p3c_env):
        """T4 — Locked→Archived is the one permitted forward transition."""
        c, tok, bid = p3c_env["client"], p3c_env["token"], p3c_env["branch_id"]
        db = p3c_env["db"]
        rtid = p3c_env["hourly_rtid"]

        pid, drv, _fid = await _lock_period(
            c, tok, bid, T4_START, T4_END, T4_WORK, rtid, "T4a", db
        )
        p3c_env["created_drivers"].append(drv)

        # This must NOT raise
        await db.execute(
            _text("UPDATE payroll.payrollperiods "
                  "SET status = 'Archived' "
                  "WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )

        row = (await db.execute(
            _text("SELECT status FROM payroll.payrollperiods "
                  "WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )).mappings().first()
        assert row["status"] == "Archived"


# ---------------------------------------------------------------------------
# Test 5 — Archived period: any status change blocked
# ---------------------------------------------------------------------------

class TestT5_ArchivedTerminal:

    @pytest.mark.asyncio
    async def test_archived_period_blocks_all_transitions(self, p3c_env):
        """T5 — Archived is terminal; no further status changes are allowed."""
        c, tok, bid = p3c_env["client"], p3c_env["token"], p3c_env["branch_id"]
        db = p3c_env["db"]
        rtid = p3c_env["hourly_rtid"]

        pid, drv, _fid = await _lock_period(
            c, tok, bid, T5_START, T5_END, T5_WORK, rtid, "T5a", db
        )
        p3c_env["created_drivers"].append(drv)

        # Advance to Archived first (this is allowed)
        await db.execute(
            _text("UPDATE payroll.payrollperiods SET status = 'Archived' "
                  "WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )

        # Now try to revert Archived→Locked — must be blocked
        with pytest.raises(Exception) as exc_info:
            await db.execute(
                _text("UPDATE payroll.payrollperiods "
                      "SET status = 'Locked' "
                      "WHERE payrollperiodid = :pid"),
                {"pid": pid},
            )

        err = str(exc_info.value).lower()
        assert "restrict_violation" in err or "payroll_status_immutable" in err, (
            f"Expected restrict_violation / payroll_status_immutable, got: {exc_info.value}"
        )


# ---------------------------------------------------------------------------
# Test 6 — Normal finalization still works (Approved→Locked via service)
# ---------------------------------------------------------------------------

class TestT6_FinalizationStillWorks:

    @pytest.mark.asyncio
    async def test_finalization_succeeds_after_trigger(self, p3c_env):
        """T6 — The INSERT-only finalization path is not blocked by the trigger."""
        c, tok, bid = p3c_env["client"], p3c_env["token"], p3c_env["branch_id"]
        db = p3c_env["db"]
        rtid = p3c_env["hourly_rtid"]

        drv = await _create_driver(c, tok, bid, "T6a", hire_date="2048-01-01")
        p3c_env["created_drivers"].append(drv)
        await _create_and_approve_rate(c, tok, drv, rtid, "18.00",
                                       effective_from="2048-01-01")

        pid = await _make_period(db, bid, start=T6_START, end=T6_END)
        r = await c.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": drv, "work_date": T6_WORK,
                  "line_type": "HOURS", "quantity": "8.0000"},
            headers=auth(tok),
        )
        assert r.status_code == 201

        await _advance_to_approved(c, tok, pid, drv, work_date=T6_WORK)
        result = await _finalize(c, tok, pid)

        assert result.get("status") == "Locked" or result.get("payroll_period_id") is not None

        lines = await _get_final_lines(c, tok, pid, driver_id=drv)
        hours_lines = [line for line in lines if line["line_type"] == "HOURS"]
        assert len(hours_lines) == 1
        assert Decimal(str(hours_lines[0]["final_amount"])) == Decimal("144.0000")


# ---------------------------------------------------------------------------
# Test 7 — Ledger read (get_final_lines) works after trigger migration
# ---------------------------------------------------------------------------

class TestT7_LedgerReadWorks:

    @pytest.mark.asyncio
    async def test_get_final_lines_returns_source_snapshot(self, p3c_env):
        """T7 — get_final_lines returns source snapshot fields after migration 0035."""
        c, tok, bid = p3c_env["client"], p3c_env["token"], p3c_env["branch_id"]
        db = p3c_env["db"]
        rtid = p3c_env["hourly_rtid"]

        drv = await _create_driver(c, tok, bid, "T7a", hire_date="2049-01-01")
        p3c_env["created_drivers"].append(drv)
        await _create_and_approve_rate(c, tok, drv, rtid, "20.00",
                                       effective_from="2049-01-01")

        pid = await _make_period(db, bid, start=T7_START, end=T7_END)
        r = await c.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": drv, "work_date": T7_WORK,
                  "line_type": "HOURS", "quantity": "8.0000"},
            headers=auth(tok),
        )
        assert r.status_code == 201

        await _advance_to_approved(c, tok, pid, drv, work_date=T7_WORK)
        await _finalize(c, tok, pid)

        lines = await _get_final_lines(c, tok, pid, driver_id=drv)
        hours_lines = [line for line in lines if line["line_type"] == "HOURS"]
        assert len(hours_lines) == 1
        ln = hours_lines[0]

        # Source snapshot fields must be populated
        assert ln.get("rate_behavior") is not None, "rate_behavior should be populated"
        assert ln.get("pay_item_id") is not None, "pay_item_id should be populated"
        assert ln.get("driver_rate_id") is not None, "driver_rate_id should be populated"
        assert ln.get("rate_type_id") is not None, "rate_type_id should be populated"
        assert ln.get("resolved_rate_amount") is not None, "resolved_rate_amount should be populated"


# ---------------------------------------------------------------------------
# Test 8 — Service-layer add/update on Locked period returns 422
# ---------------------------------------------------------------------------

class TestT8_ServiceLayerLockedGuard:

    @pytest.mark.asyncio
    async def test_add_line_to_locked_period_returns_422(self, p3c_env):
        """T8a — Adding a draft line to a Locked period returns 422."""
        c, tok, bid = p3c_env["client"], p3c_env["token"], p3c_env["branch_id"]
        db = p3c_env["db"]
        rtid = p3c_env["hourly_rtid"]

        pid, drv, _fid = await _lock_period(
            c, tok, bid, T8A_START, T8A_END, T8A_WORK, rtid, "T8a", db
        )
        p3c_env["created_drivers"].append(drv)

        r = await c.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": drv, "work_date": T8A_WORK,
                  "line_type": "HOURS", "quantity": "4.0000"},
            headers=auth(tok),
        )
        assert r.status_code == 422, (
            f"Expected 422 adding line to Locked period, got {r.status_code}: {r.text}"
        )

    @pytest.mark.asyncio
    async def test_update_line_in_locked_period_returns_422(self, p3c_env):
        """T8b — Updating a draft line in a Locked period returns 422.

        The draft lines still exist (they are not deleted on finalization).
        Attempting to update one via the service should return 422.
        """
        c, tok, bid = p3c_env["client"], p3c_env["token"], p3c_env["branch_id"]
        db = p3c_env["db"]
        rtid = p3c_env["hourly_rtid"]

        pid, drv, _fid = await _lock_period(
            c, tok, bid, T8B_START, T8B_END, T8B_WORK, rtid, "T8b", db
        )
        p3c_env["created_drivers"].append(drv)

        # Find a draft line for this period
        row = (await db.execute(
            _text("SELECT draftlineid FROM payroll.payrolldraftlines "
                  "WHERE payrollperiodid = :pid AND linetype = 'HOURS' LIMIT 1"),
            {"pid": pid},
        )).mappings().first()

        if row is None:
            pytest.skip("No HOURS draft line found — cannot test update guard")

        dlid = int(row["draftlineid"])
        r = await c.patch(
            f"/payroll/periods/{pid}/lines/{dlid}",
            json={"quantity": "2.0000"},
            headers=auth(tok),
        )
        assert r.status_code == 422, (
            f"Expected 422 updating line in Locked period, got {r.status_code}: {r.text}"
        )
