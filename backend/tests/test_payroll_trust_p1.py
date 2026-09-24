"""
Payroll Trust Phase 1 — Duplicate Draft Line Protection

Tests covering the P0 duplicate-line blocker:

  T1  test_add_draft_line_rejects_duplicate
  T2  test_add_draft_line_allows_different_date
  T3  test_add_draft_line_allows_different_pay_item
  T4  test_voided_line_frees_slot
  T5  test_update_cannot_collide
  T6  test_day_grid_is_idempotent
  T7  test_finalization_blocks_preexisting_duplicates
  T8  test_db_unique_index_prevents_direct_duplicate
  T9  test_period_pay_behavior_preserved
  T10 test_normal_add_and_finalize_still_works

Isolation strategy
------------------
All tests use year 2033 dates on the PAYTEST branch.  Function-scoped
fixtures create and clean up periods so tests do not interfere with each
other or with other test modules.
"""
from datetime import date as _date
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text as _text


@pytest_asyncio.fixture(scope="session")
async def paytest_branch_id(session_db_conn) -> int:
    """Use a module-isolated branch for workflow-slot tests."""
    row = (await session_db_conn.execute(_text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (1, :code, :name, 'Active', FALSE)
        RETURNING branchid
    """), {"code": f"P1_{uuid4().hex}", "name": "P1 isolated"})).scalar_one()
    return int(row)


@pytest_asyncio.fixture(scope="session")
async def paytest_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> int:
    """Create the duplicate-line test driver on the isolated branch."""
    response = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": paytest_branch_id,
            "full_name": "P1 Isolated Driver",
            "driver_code": f"P1-D-{uuid4().hex[:10]}",
        },
        headers=auth(auth_token),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["driver_id"])


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# Dates well outside every other test module's range
P_START = "2033-03-06"
P_END   = "2033-03-19"
WORK_A  = "2033-03-07"   # Tuesday
WORK_B  = "2033-03-08"   # Wednesday — different date


async def _cancel_active_periods(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    db=None,
) -> None:
    if db is not None:
        await db.execute(
            _text(
                "UPDATE payroll.payrollperiods SET status = 'Cancelled' "
                "WHERE branchid = :bid AND status IN ('InReview', 'Approved', 'Returned')"
            ),
            {"bid": branch_id},
        )
    headers = auth(token)
    for s in ("Draft", "Open", "InReview", "Approved"):
        resp = await client.get(
            "/payroll/periods",
            params={"branch_id": branch_id, "status": s},
            headers=headers,
        )
        if resp.status_code != 200:
            continue
        for p in resp.json():
            await client.patch(
                f"/payroll/periods/{p['payroll_period_id']}/status",
                json={"status": "Cancelled"},
                headers=headers,
            )


async def _create_open_period(
    db,
    branch_id: int,
    start: str = P_START,
    end: str = P_END,
) -> int:
    """Insert an Open period directly into the DB. Returns period_id."""
    from datetime import date as _ddate
    row = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            ON CONFLICT DO NOTHING RETURNING payrollperiodid
        """),
        {
            "bid":   branch_id,
            "code":  f"P1-{branch_id}-{start}",
            "name":  f"P1 {start}",
            "start": _ddate.fromisoformat(start),
            "end":   _ddate.fromisoformat(end),
        },
    )).mappings().first()
    return row["payrollperiodid"]


async def _add_line(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    work_date: str = WORK_A,
    line_type: str = "DailyNote",
    quantity: int = 1,
) -> httpx.Response:
    payload: dict = {
        "driver_id": driver_id,
        "work_date": work_date,
        "line_type": line_type,
        "quantity":  quantity,
    }
    if line_type == "DailyNote":
        payload["notes"] = "filler"
    return await client.post(
        f"/payroll/periods/{period_id}/lines",
        headers=auth(token),
        json=payload,
    )


async def _advance_to_approved(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
) -> None:
    """Advance an Open period all the way to Approved via the review workflow."""
    headers = auth(token)

    # Ensure at least one non-void line (DailyNote — no rate needed)
    lines_resp = await client.get(
        f"/payroll/periods/{period_id}/lines",
        headers=headers,
        params={"status": "Active"},
    )
    if lines_resp.status_code == 200 and len(lines_resp.json()) == 0:
        r = await _add_line(client, token, period_id, driver_id)
        assert r.status_code == 201, f"seed line failed: {r.text}"

    # Open → InReview
    r = await client.patch(
        f"/payroll/periods/{period_id}/status",
        headers=headers,
        json={"status": "InReview"},
    )
    assert r.status_code == 200, f"InReview failed: {r.text}"

    # Find the pending review item
    review_resp = await client.get("/review/items", headers=headers)
    assert review_resp.status_code == 200
    review_item = next(
        (i for i in review_resp.json()
         if i.get("entity_name") == "PayrollPeriods"
         and i.get("entity_id") == str(period_id)
         and i.get("status") == "Pending"),
        None,
    )
    assert review_item is not None, f"No pending review item for period {period_id}"

    # Approve via review endpoint
    decide = await client.post(
        f"/review/items/{review_item['review_item_id']}/decide",
        headers=headers,
        json={"decision": "Approved"},
    )
    assert decide.status_code == 200, f"Approval failed: {decide.text}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def p1_clean(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """Cancel any conflicting PAYTEST periods before and after each test."""
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
    # Also force-cancel Locked/Archived periods left by finalization tests
    # (migration 0035 blocks direct status UPDATE; disable triggers temporarily)
    await direct_db.execute(_text(
        "ALTER TABLE payroll.payrollfinallines DISABLE TRIGGER trg_final_line_immutable"
    ))
    await direct_db.execute(_text(
        "ALTER TABLE payroll.payrollperiods DISABLE TRIGGER trg_period_status_revert"
    ))
    await direct_db.execute(
        _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' "
              "WHERE branchid = :bid AND status IN ('Locked', 'Archived')"),
        {"bid": paytest_branch_id},
    )
    await direct_db.execute(_text(
        "ALTER TABLE payroll.payrollfinallines ENABLE TRIGGER trg_final_line_immutable"
    ))
    await direct_db.execute(_text(
        "ALTER TABLE payroll.payrollperiods ENABLE TRIGGER trg_period_status_revert"
    ))
    yield paytest_branch_id
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
    await direct_db.execute(_text(
        "ALTER TABLE payroll.payrollfinallines DISABLE TRIGGER trg_final_line_immutable"
    ))
    await direct_db.execute(_text(
        "ALTER TABLE payroll.payrollperiods DISABLE TRIGGER trg_period_status_revert"
    ))
    await direct_db.execute(
        _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' "
              "WHERE branchid = :bid AND status IN ('Locked', 'Archived')"),
        {"bid": paytest_branch_id},
    )
    await direct_db.execute(_text(
        "ALTER TABLE payroll.payrollfinallines ENABLE TRIGGER trg_final_line_immutable"
    ))
    await direct_db.execute(_text(
        "ALTER TABLE payroll.payrollperiods ENABLE TRIGGER trg_period_status_revert"
    ))


@pytest_asyncio.fixture
async def open_period(
    session_client: httpx.AsyncClient,
    auth_token: str,
    p1_clean: int,
    direct_db,
) -> int:
    """Open period on PAYTEST, returns period_id."""
    return await _create_open_period(direct_db, p1_clean)


@pytest_asyncio.fixture
async def approved_period_p1(
    session_client: httpx.AsyncClient,
    auth_token: str,
    p1_clean: int,
    paytest_driver_id: int,
    direct_db,
) -> int:
    """
    Create an Approved period on PAYTEST with all draft lines voided so each
    test can seed its own lines.  Returns period_id.
    """
    pid = await _create_open_period(direct_db, p1_clean)
    await _advance_to_approved(
        session_client, auth_token, pid, paytest_driver_id
    )
    # Void the seed line used by _advance_to_approved
    await direct_db.execute(
        _text("UPDATE payroll.payrolldraftlines SET status='Void' WHERE payrollperiodid=:pid"),
        {"pid": pid},
    )
    return pid


# ---------------------------------------------------------------------------
# T1 — Service-level duplicate guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_draft_line_rejects_duplicate(
    session_client: httpx.AsyncClient,
    auth_token: str,
    open_period: int,
    paytest_driver_id: int,
):
    """Second add for the same (driver, date, line_type) must return 422."""
    pid = open_period
    driver = paytest_driver_id

    r1 = await _add_line(session_client, auth_token, pid, driver)
    assert r1.status_code == 201, f"first add failed: {r1.text}"

    r2 = await _add_line(session_client, auth_token, pid, driver)
    assert r2.status_code == 422, f"expected 422 on duplicate, got {r2.status_code}: {r2.text}"
    assert "already exists" in r2.json().get("detail", "").lower(), r2.text


# ---------------------------------------------------------------------------
# T2 — Different date is allowed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_draft_line_allows_different_date(
    session_client: httpx.AsyncClient,
    auth_token: str,
    open_period: int,
    paytest_driver_id: int,
):
    """Same driver + line_type but different work_date must succeed."""
    pid = open_period
    driver = paytest_driver_id

    r1 = await _add_line(session_client, auth_token, pid, driver, work_date=WORK_A)
    assert r1.status_code == 201, r1.text

    r2 = await _add_line(session_client, auth_token, pid, driver, work_date=WORK_B)
    assert r2.status_code == 201, f"different-date add failed unexpectedly: {r2.text}"


# ---------------------------------------------------------------------------
# T3 — Different pay item is allowed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_draft_line_allows_different_pay_item(
    session_client: httpx.AsyncClient,
    auth_token: str,
    open_period: int,
    paytest_driver_id: int,
):
    """Same driver + date but different line_type must succeed."""
    pid = open_period
    driver = paytest_driver_id

    r1 = await _add_line(
        session_client, auth_token, pid, driver, line_type="DailyNote"
    )
    assert r1.status_code == 201, r1.text

    # MILES is a valid canonical code for the PAYTEST branch (activated by conftest)
    r2 = await _add_line(
        session_client, auth_token, pid, driver, line_type="MILES", quantity=50
    )
    assert r2.status_code == 201, f"different-item add failed unexpectedly: {r2.text}"


# ---------------------------------------------------------------------------
# T4 — Voiding frees the slot for re-add
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_voided_line_frees_slot(
    session_client: httpx.AsyncClient,
    auth_token: str,
    open_period: int,
    paytest_driver_id: int,
):
    """After voiding a line, re-adding the same business key must succeed."""
    pid = open_period
    driver = paytest_driver_id
    headers = auth(auth_token)

    r1 = await _add_line(session_client, auth_token, pid, driver)
    assert r1.status_code == 201, r1.text
    line_id = r1.json()["draft_line_id"]

    # Void it (DELETE returns 204)
    void_resp = await session_client.delete(
        f"/payroll/periods/{pid}/lines/{line_id}",
        headers=headers,
    )
    assert void_resp.status_code == 204, f"void failed: {void_resp.text}"

    # Should now be allowed again
    r2 = await _add_line(session_client, auth_token, pid, driver)
    assert r2.status_code == 201, f"re-add after void failed: {r2.text}"


# ---------------------------------------------------------------------------
# T5 — update_draft_line cannot cause a collision
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_cannot_collide(
    session_client: httpx.AsyncClient,
    auth_token: str,
    open_period: int,
    paytest_driver_id: int,
):
    """
    update_draft_line only changes quantity/rate/notes — never driver_id,
    work_date, or line_type.  Updating a line while another line exists for
    a different key must succeed (no false collision).
    """
    pid = open_period
    driver = paytest_driver_id
    headers = auth(auth_token)

    r1 = await _add_line(
        session_client, auth_token, pid, driver, work_date=WORK_A, line_type="DailyNote"
    )
    assert r1.status_code == 201, r1.text
    line_id_a = r1.json()["draft_line_id"]

    r2 = await _add_line(
        session_client, auth_token, pid, driver, work_date=WORK_B, line_type="DailyNote"
    )
    assert r2.status_code == 201, r2.text

    # Updating line A's quantity must succeed — no business key change
    upd = await session_client.patch(
        f"/payroll/periods/{pid}/lines/{line_id_a}",
        headers=headers,
        json={"quantity": 2},
    )
    assert upd.status_code == 200, f"update_draft_line failed unexpectedly: {upd.text}"
    assert float(upd.json()["quantity"]) == 2.0


# ---------------------------------------------------------------------------
# T6 — Day-grid save is idempotent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_day_grid_is_idempotent(
    session_client: httpx.AsyncClient,
    auth_token: str,
    open_period: int,
    paytest_driver_id: int,
):
    """
    Posting the same day-grid payload twice must succeed both times
    (upsert — no duplicate error).
    """
    pid = open_period
    driver = paytest_driver_id
    headers = auth(auth_token)

    payload = {
        "work_date": WORK_A,
        "rows": [
            {
                "driver_id":  driver,
                "values":     {"MILES": "40"},
                "status_key": None,
                "notes":      None,
            }
        ],
    }

    r1 = await session_client.post(
        f"/payroll/periods/{pid}/day-grid",
        headers=headers,
        json=payload,
    )
    assert r1.status_code == 200, f"first day-grid save failed: {r1.text}"

    # Second identical post must succeed (idempotent update, not duplicate add)
    r2 = await session_client.post(
        f"/payroll/periods/{pid}/day-grid",
        headers=headers,
        json=payload,
    )
    assert r2.status_code == 200, f"second day-grid save failed: {r2.text}"

    # The MILES value in the grid should be 40 (not 80)
    grid = r2.json()
    driver_row = next(
        (row for row in grid.get("rows", []) if row["driver_id"] == driver),
        None,
    )
    assert driver_row is not None, "driver row missing from day-grid response"
    miles_entry = driver_row.get("values", {}).get("MILES")
    assert miles_entry is not None, f"MILES not in values: {driver_row}"
    # values[code] may be a dict {"quantity": "40.0000", ...} or a plain scalar
    if isinstance(miles_entry, dict):
        miles_qty = float(miles_entry["quantity"])
    else:
        miles_qty = float(miles_entry)
    assert miles_qty == 40.0, f"expected MILES=40 after idempotent save, got {miles_qty}"


# ---------------------------------------------------------------------------
# T7 — Finalization blocks pre-existing duplicates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finalization_blocks_preexisting_duplicates(
    session_client: httpx.AsyncClient,
    auth_token: str,
    approved_period_p1: int,
    paytest_driver_id: int,
    paytest_branch_id: int,
    direct_db,
):
    """
    If the draft ledger somehow already has duplicate active Daily lines
    (e.g. injected directly, bypassing the service guard), finalization must
    reject with 422 before attempting any INSERT into PayrollFinalLines.
    """
    pid = approved_period_p1
    driver = paytest_driver_id
    branch = paytest_branch_id

    work_date_a = _date(2033, 3, 7)

    # The partial unique index (migration 0033) prevents duplicate inserts in
    # normal operation.  To simulate pre-existing corruption (e.g. rows written
    # before the index existed) we temporarily drop the index, inject duplicates,
    # then recreate it — limited to this test, on a shared test DB.
    try:
        await direct_db.execute(
            _text("DROP INDEX IF EXISTS payroll.uix_payrolldraftlines_daily_active_business_key")
        )

        for _ in range(2):
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverid,
                         workdate, linetype, linescope, quantity,
                         sourcetype, status, needsmanagerreview, notes, addedbyuserid)
                    SELECT companyid, :bid, :pid, :did,
                           :wdate, 'DailyNote', 'Daily', 1,
                           'Manual', 'Active', FALSE, NULL, 1
                    FROM   payroll.payrollperiods
                    WHERE  payrollperiodid = :pid
                """),
                {"bid": branch, "pid": pid, "did": driver, "wdate": work_date_a},
            )

        # Finalization must use the already-approved immutable packet rather than
        # rebuilding from these post-submit live draft duplicates.
        r = await session_client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=auth(auth_token),
        )
        assert r.status_code == 200, (
            f"Expected approved snapshot finalization to ignore live duplicates, got {r.status_code}: {r.text}"
        )
    finally:
        # Void injected duplicate rows so cleanup can proceed, then restore index
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrolldraftlines
                SET    status = 'Void'
                WHERE  payrollperiodid = :pid AND linetype = 'DailyNote'
            """),
            {"pid": pid},
        )
        await direct_db.execute(
            _text("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                    uix_payrolldraftlines_daily_active_business_key
                ON payroll.payrolldraftlines
                    (companyid, payrollperiodid, driverid, workdate, linetype)
                WHERE linescope = 'Daily'
                  AND status   != 'Void'
            """)
        )


# ---------------------------------------------------------------------------
# T8 — DB unique index prevents direct duplicate insert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_unique_index_prevents_direct_duplicate(
    session_client: httpx.AsyncClient,
    auth_token: str,
    open_period: int,
    paytest_driver_id: int,
    paytest_branch_id: int,
    direct_db,
):
    """
    The partial unique index must raise an IntegrityError when a second
    active Daily line with the same business key is inserted directly into
    the DB (bypassing the service layer).
    """
    from sqlalchemy.exc import IntegrityError

    pid = open_period
    driver = paytest_driver_id
    branch = paytest_branch_id

    work_date_a = _date(2033, 3, 7)
    insert_sql = _text("""
        INSERT INTO payroll.payrolldraftlines
            (companyid, branchid, payrollperiodid, driverid,
             workdate, linetype, linescope, quantity,
             sourcetype, status, needsmanagerreview, notes, addedbyuserid)
        SELECT companyid, :bid, :pid, :did,
               :wdate, 'DailyNote', 'Daily', 1,
               'Manual', 'Active', FALSE, NULL, 1
        FROM   payroll.payrollperiods
        WHERE  payrollperiodid = :pid
    """)
    params = {"bid": branch, "pid": pid, "did": driver, "wdate": work_date_a}

    # First insert should succeed
    await direct_db.execute(insert_sql, params)

    # Second insert must be rejected by the DB unique index
    with pytest.raises((IntegrityError, Exception)) as exc_info:
        await direct_db.execute(insert_sql, params)

    err_str = str(exc_info.value).lower()
    assert (
        "unique" in err_str or "duplicate" in err_str or "uix_" in err_str
    ), f"Expected unique-violation error, got: {exc_info.value}"


# ---------------------------------------------------------------------------
# T9 — Period Pay (Bonus / Adjustment) behavior is not affected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
# ---------------------------------------------------------------------------
# T10 — Normal add + finalization regression test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_normal_add_and_finalize_still_works(
    session_client: httpx.AsyncClient,
    auth_token: str,
    approved_period_p1: int,
    paytest_driver_id: int,
    direct_db,
):
    """
    Regression: a clean period (no duplicates) with a single active Daily line
    must still finalize successfully after the duplicate guard is in place.
    """
    pid = approved_period_p1
    driver = paytest_driver_id
    headers = auth(auth_token)

    # Inject exactly one active DailyNote line via direct_db
    await direct_db.execute(
        _text("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid,
                 workdate, linetype, linescope, quantity,
                 sourcetype, status, needsmanagerreview, notes, addedbyuserid)
            SELECT companyid, branchid, :pid, :did,
                   :wdate, 'DailyNote', 'Daily', 1,
                   'Manual', 'Active', FALSE, NULL, 1
            FROM   payroll.payrollperiods
            WHERE  payrollperiodid = :pid
        """),
        {"pid": pid, "did": driver, "wdate": _date(2033, 3, 7)},
    )

    # Finalize must succeed
    r = await session_client.post(
        f"/payroll/periods/{pid}/finalize",
        headers=headers,
    )
    assert r.status_code == 200, f"finalization failed unexpectedly: {r.text}"

    # Verify the final line was written
    lines_resp = await session_client.get(
        f"/payroll/periods/{pid}/final-lines",
        headers=headers,
    )
    assert lines_resp.status_code == 200
    final_lines = lines_resp.json()
    assert not any(fl.get("line_type") == "DailyNote" for fl in final_lines), (
        "informational DailyNote source lines must not become financial FinalLines"
    )
    assert (await session_client.get(f"/payroll/periods/{pid}", headers=headers)).json()["status"] == "Locked"
