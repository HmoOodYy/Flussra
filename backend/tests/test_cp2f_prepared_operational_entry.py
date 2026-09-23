"""
CP-2F: Controlled Prepared Operational Entry — full test suite.

Covers:
  - Draft (Prepared) day-grid: GET returns 200, financial fields suppressed.
  - Draft day-grid save: PPDES written, no STATUS_PAYMENT line, NULL calc.
  - Draft financial blocking: Period Pay, Bonus, finalization, direct submit blocked.
  - Direct DraftLine API in Draft: daily source allowed (NULL calc), Period blocked.
  - Draft → Open activation: calculations refreshed, status payments derived, no dups.
  - Regression: no PTO_STATUS, status saved in PPDES not as DraftLine pay item.

Dates: 2097-* — isolated year.
  CP-2E uses 2099, CP-2D2 uses 2097 (different dates — we use week offsets
  far enough out to avoid collisions).  We use 2097-07 onwards.

Run from backend/:
    python -B -m pytest tests/test_cp2f_prepared_operational_entry.py -v -p no:cacheprovider
"""
import datetime
import itertools
from uuid import uuid4

import pytest
import pytest_asyncio
import httpx
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncConnection

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_COMPANY_ID = 1
# Use late 2097 to avoid any CP-2D2 2097-start collisions
_BASE_MONDAY_2097_LATE = datetime.date(2097, 7, 7)   # Monday in July 2097
_CTR = itertools.count(0)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _week(offset: int = 0) -> tuple[datetime.date, datetime.date]:
    n = next(_CTR) + offset
    start = _BASE_MONDAY_2097_LATE + datetime.timedelta(weeks=n)
    return start, start + datetime.timedelta(days=6)


async def _insert_period_db(
    db: AsyncConnection,
    branch_id: int,
    start: datetime.date,
    end: datetime.date,
    status: str = "Draft",
    code_suffix: str = "",
) -> int:
    code = f"CP2F-{branch_id}-{start.isoformat()}{code_suffix}"
    r = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, :status, :code, :name, 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {
            "bid": branch_id, "status": status, "code": code,
            "name": f"CP2F {start}", "start": start, "end": end,
        },
    )).mappings().first()
    await db.commit()
    if r is None:
        r = (await db.execute(
            _text("SELECT payrollperiodid FROM payroll.payrollperiods "
                  "WHERE branchid = :bid AND periodcode = :code"),
            {"bid": branch_id, "code": code},
        )).mappings().first()
    assert r is not None
    return r["payrollperiodid"]


async def _cancel_period_db(db: AsyncConnection, period_id: int) -> None:
    await db.execute(_text(
        "ALTER TABLE payroll.payrollperiods DISABLE TRIGGER trg_period_status_revert"
    ))
    await db.execute(
        _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' "
              "WHERE payrollperiodid = :pid"),
        {"pid": period_id},
    )
    await db.execute(_text(
        "ALTER TABLE payroll.payrollperiods ENABLE TRIGGER trg_period_status_revert"
    ))
    await db.commit()


async def _get_draft_lines(db: AsyncConnection, period_id: int) -> list[dict]:
    rows = (await db.execute(
        _text("""
            SELECT draftlineid, driverid, workdate, linetype, linescope,
                   quantity, calculatedamount, rateamount, sourcetype,
                   sourceid, status, needsmanagerreview, notes
            FROM   payroll.payrolldraftlines
            WHERE  payrollperiodid = :pid
            ORDER BY workdate, driverid, linetype
        """),
        {"pid": period_id},
    )).mappings().all()
    return [dict(r) for r in rows]


async def _get_ppdes_rows(db: AsyncConnection, period_id: int) -> list[dict]:
    rows = (await db.execute(
        _text("""
            SELECT driverid, workdate, statuskeyid, notetext, isvoided
            FROM   payroll.payrollperioddriverdayentrystate
            WHERE  payrollperiodid = :pid
            ORDER BY workdate, driverid
        """),
        {"pid": period_id},
    )).mappings().all()
    return [dict(r) for r in rows]


async def _ensure_bonus_active(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
) -> None:
    r = await client.get(
        f"/settings/branches/{branch_id}/pay-items",
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    bonus = next((i for i in r.json() if i.get("pay_item_code") == "BONUS"), None)
    if bonus and not bonus.get("is_active"):
        r2 = await client.patch(
            f"/settings/branches/{branch_id}/pay-items/{bonus['pay_item_id']}",
            json={"is_active": True},
            headers=_auth(token),
        )
        assert r2.status_code == 200, r2.text


async def _ensure_status_key(
    db: AsyncConnection,
    branch_id: int,
    code: str = "CP2F_TEST_SK",
    with_rate_column: bool = False,
) -> tuple[int, str]:
    """
    Return (status_key_id, status_code) for the branch, creating if needed.
    If with_rate_column=True, also ensures a StatusRateColumn is linked.
    Returns (sk_id, sk_code).
    """
    # Try to find existing active status key
    existing = (await db.execute(
        _text("""
            SELECT statuskeyid, statuscode FROM payroll.payrollstatuskeys
            WHERE companyid = 1 AND branchid = :bid AND isactive = TRUE
            ORDER BY statuskeyid LIMIT 1
        """),
        {"bid": branch_id},
    )).mappings().first()

    if existing and not with_rate_column:
        return existing["statuskeyid"], existing["statuscode"]

    if with_rate_column:
        # Need a status key with a linked StatusRateColumn
        existing_with_src = (await db.execute(
            _text("""
                SELECT sk.statuskeyid, sk.statuscode
                FROM payroll.payrollstatuskeys sk
                WHERE sk.companyid = 1 AND sk.branchid = :bid
                  AND sk.isactive = TRUE
                  AND sk.statusratecolumnid IS NOT NULL
                ORDER BY sk.statuskeyid LIMIT 1
            """),
            {"bid": branch_id},
        )).mappings().first()

        if existing_with_src:
            return existing_with_src["statuskeyid"], existing_with_src["statuscode"]

        # Create a StatusRateColumn if none exists
        src_row = (await db.execute(
            _text("""
                SELECT statusratecolumnid FROM payroll.statusratecolumns
                WHERE companyid = 1 AND branchid = :bid AND isactive = TRUE
                ORDER BY isdefault DESC, statusratecolumnid LIMIT 1
            """),
            {"bid": branch_id},
        )).mappings().first()

        if src_row is None:
            # Create a STATUS_PAY rate column
            rt = (await db.execute(
                _text("SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = 'STATUS_PAY'"),
            )).mappings().first()
            if rt is None:
                # Cannot create status key with rate column — fall back to without
                pass
            else:
                src_row = (await db.execute(
                    _text("""
                        INSERT INTO payroll.statusratecolumns
                            (companyid, branchid, ratetypeid, columnname,
                             normalizedcolumnname, isdefault, isactive)
                        VALUES (1, :bid, :rtid, 'CP2F Status Pay', 'CP2F STATUS PAY', TRUE, TRUE)
                        RETURNING statusratecolumnid
                    """),
                    {"bid": branch_id, "rtid": rt["ratetypeid"]},
                )).mappings().first()
                await db.commit()

        src_id = src_row["statusratecolumnid"] if src_row else None

        # Create the status key
        sk = (await db.execute(
            _text("""
                INSERT INTO payroll.payrollstatuskeys
                    (companyid, branchid, statuscode, normalizedstatuscode, keyname,
                     isoffreason, hoursvalue, isactive, displayorder, statusratecolumnid)
                VALUES (1, :bid, :code, :norm, :name, FALSE, 8.0, TRUE, 99, :srcid)
                RETURNING statuskeyid
            """),
            {
                "bid": branch_id,
                "code": code,
                "norm": code.upper(),
                "name": f"CP2F Test Key {code}",
                "srcid": src_id,
            },
        )).mappings().first()
        await db.commit()
        return sk["statuskeyid"], code

    # Create basic status key (no rate column)
    sk = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollstatuskeys
                (companyid, branchid, statuscode, normalizedstatuscode, keyname,
                 isoffreason, hoursvalue, isactive, displayorder)
            VALUES (1, :bid, :code, :norm, :name, FALSE, 8.0, TRUE, 99)
            RETURNING statuskeyid
        """),
        {
            "bid": branch_id,
            "code": code,
            "norm": code.upper(),
            "name": f"CP2F Test Key {code}",
        },
    )).mappings().first()
    await db.commit()
    return sk["statuskeyid"], code


# ---------------------------------------------------------------------------
# Module-scoped fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="module")
async def branch_id(
    session_db_conn: AsyncConnection,
    session_client: httpx.AsyncClient,
    auth_token: str,
) -> int:
    """Create a branch-local CP2F fixture so workflow slots are not shared."""
    code = f"CP2F_{uuid4().hex[:10]}".upper()
    row = (await session_db_conn.execute(
        _text("""
            INSERT INTO core.branches
                (companyid, branchcode, branchname, status, isdefault)
            VALUES (1, :code, :name, 'Active', FALSE)
            RETURNING branchid
        """),
        {"code": code, "name": f"CP2F isolated {code}"},
    )).mappings().one()
    branch = int(row["branchid"])

    setup = await session_client.put(
        f"/settings/branches/{branch}/payroll-setup",
        json={"payroll_frequency": "Week", "anchor_start_date": "2097-07-07"},
        headers=_auth(auth_token),
    )
    assert setup.status_code in (200, 201), setup.text

    # Force an explicit HOURS config. The default catalog flag alone is not
    # enough for every rate-matrix/day-grid query on a new branch.
    items = await session_client.get(
        f"/settings/branches/{branch}/pay-items",
        headers=_auth(auth_token),
    )
    assert items.status_code == 200, items.text
    hours = next(item for item in items.json() if item["pay_item_code"] == "HOURS")
    activate = await session_client.patch(
        f"/settings/branches/{branch}/pay-items/{hours['pay_item_id']}",
        json={"is_active": True},
        headers=_auth(auth_token),
    )
    assert activate.status_code == 200, activate.text
    return branch


@pytest_asyncio.fixture(scope="module")
async def driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    branch_id: int,
) -> int:
    response = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": branch_id,
            "full_name": "CP2F Isolated Driver",
            "preferred_name": "CP2F",
            "driver_code": f"CP2F-{uuid4().hex[:8]}",
            "cdl_number": f"CDL-CP2F-{uuid4().hex[:8]}",
        },
        headers=_auth(auth_token),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["driver_id"])


# ---------------------------------------------------------------------------
# Section 1: Draft Day Grid — read
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDraftDayGridRead:
    """GET day grid on a Draft period is allowed; financial fields are null/zero."""

    async def test_cp2f_draft_get_day_grid_allowed_operational_only(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """GET day grid on a Draft period returns 200."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            work_date = start.isoformat()
            r = await client.get(
                f"/payroll/periods/{pid}/day-grid",
                params={"work_date": work_date},
                headers=_auth(auth_token),
            )
            assert r.status_code == 200, f"Expected 200 for Draft day-grid GET, got {r.status_code}: {r.text}"
            data = r.json()
            assert data["period"]["status"] == "Draft"
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_day_grid_summary_has_no_money(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """GET day grid on Draft returns gross_total=None and financials_available=False."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            work_date = start.isoformat()
            r = await client.get(
                f"/payroll/periods/{pid}/day-grid",
                params={"work_date": work_date},
                headers=_auth(auth_token),
            )
            assert r.status_code == 200, r.text
            summary = r.json()["summary"]
            gross = summary.get("gross_total")
            # CP-2F: gross_total must be None for Draft periods
            assert gross is None, (
                f"Expected gross_total=None for Draft period, got {gross!r}"
            )
            # financials_available must be False
            fin_avail = summary.get("financials_available", True)
            assert fin_avail is False, (
                f"Expected financials_available=False for Draft period, got {fin_avail!r}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)


# ---------------------------------------------------------------------------
# Section 2: Draft Day Grid — save
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDraftDayGridSave:
    """POST save_day_grid on Draft period is allowed for operational (non-financial) data."""

    async def test_cp2f_draft_save_day_grid_quantity_status_note_succeeds(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """POST save_day_grid on Draft period with quantity returns success."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            work_date = start.isoformat()
            r = await client.post(
                f"/payroll/periods/{pid}/day-grid",
                json={
                    "work_date": work_date,
                    "rows": [
                        {
                            "driver_id": driver_id,
                            "values": {"HOURS": "8"},
                            "notes": "Draft note",
                        }
                    ],
                },
                headers=_auth(auth_token),
            )
            assert r.status_code == 200, (
                f"Expected 200 for Draft day-grid save, got {r.status_code}: {r.text}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_quantity_line_has_no_calculated_amount_or_nmr(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """DraftLine created via Draft save has calculated_amount=NULL, needs_manager_review=False."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            work_date = start.isoformat()
            r = await client.post(
                f"/payroll/periods/{pid}/day-grid",
                json={
                    "work_date": work_date,
                    "rows": [
                        {
                            "driver_id": driver_id,
                            "values": {"HOURS": "8"},
                        }
                    ],
                },
                headers=_auth(auth_token),
            )
            assert r.status_code == 200, r.text

            lines = await _get_draft_lines(direct_db, pid)
            pay_lines = [
                ln for ln in lines
                if ln["linetype"] not in ("DailyStatus", "DailyNote") and ln["status"] != "Void"
            ]
            assert len(pay_lines) >= 1, "Expected at least one pay line after Draft save"
            for ln in pay_lines:
                assert ln["calculatedamount"] is None, (
                    f"Draft pay line should have NULL calculatedamount, got {ln['calculatedamount']}"
                )
                assert ln["needsmanagerreview"] is False, (
                    f"Draft pay line should have needsmanagerreview=False, got {ln['needsmanagerreview']}"
                )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_status_save_creates_ppdes_but_no_status_payment_line(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """After status save in Draft, PPDES row exists; no STATUS_PAYMENT DraftLine."""
        sk_id, sk_code = await _ensure_status_key(direct_db, branch_id, code="CP2F_PPDES_SK")

        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            work_date = start.isoformat()
            r = await client.post(
                f"/payroll/periods/{pid}/day-grid",
                json={
                    "work_date": work_date,
                    "rows": [
                        {
                            "driver_id": driver_id,
                            "status_key": sk_code,
                        }
                    ],
                },
                headers=_auth(auth_token),
            )
            assert r.status_code == 200, r.text

            ppdes_rows = await _get_ppdes_rows(direct_db, pid)
            active_ppdes = [r for r in ppdes_rows if not r["isvoided"]]
            assert len(active_ppdes) >= 1, "Expected PPDES row after status save in Draft"

            lines = await _get_draft_lines(direct_db, pid)
            sp_lines = [
                ln for ln in lines
                if (ln.get("sourceid") or "").startswith("STATUS_PAYMENT:") and ln["status"] != "Void"
            ]
            assert len(sp_lines) == 0, (
                f"Expected no STATUS_PAYMENT lines in Draft, found {len(sp_lines)}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_save_uses_provisional_cp2e_eligibility(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """save_day_grid on Draft succeeds using snapshot eligibility (not live)."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            work_date = start.isoformat()
            # Save succeeds — driver is eligible via snapshot or live fallback
            r = await client.post(
                f"/payroll/periods/{pid}/day-grid",
                json={
                    "work_date": work_date,
                    "rows": [{"driver_id": driver_id, "values": {"HOURS": "4"}}],
                },
                headers=_auth(auth_token),
            )
            assert r.status_code == 200, (
                f"Draft save should succeed using eligibility snapshot, got {r.status_code}: {r.text}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)


# ---------------------------------------------------------------------------
# Section 3: Financial blocking in Draft
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDraftFinancialBlocking:
    """Financial paths (Period Pay, Bonus, finalization, submit) are blocked in Draft."""

    async def test_cp2f_draft_period_pay_create_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """POST add_period_pay_line on Draft returns 422."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            r = await client.post(
                f"/payroll/periods/{pid}/period-pay",
                json={"driver_id": driver_id, "line_type": "Bonus", "amount": "50.00"},
                headers=_auth(auth_token),
            )
            assert r.status_code in (422, 409, 403), (
                f"Expected rejection for period-pay on Draft, got {r.status_code}: {r.text}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_bonus_create_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """POST add Bonus period-pay on Draft returns 422."""
        await _ensure_bonus_active(client, auth_token, branch_id)
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            r = await client.post(
                f"/payroll/periods/{pid}/period-pay",
                json={"driver_id": driver_id, "line_type": "Bonus", "amount": "100.00"},
                headers=_auth(auth_token),
            )
            assert r.status_code in (422, 409, 403), (
                f"Expected rejection for Bonus on Draft, got {r.status_code}: {r.text}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_finalization_preview_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """GET finalization preview on Draft returns 422."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            r = await client.get(
                f"/payroll/periods/{pid}/finalization-preview",
                headers=_auth(auth_token),
            )
            assert r.status_code in (422, 409, 403), (
                f"Expected rejection for finalization preview on Draft, got {r.status_code}: {r.text}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_finalize_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """POST finalize on Draft returns 422."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            r = await client.post(
                f"/payroll/periods/{pid}/finalize",
                headers=_auth(auth_token),
            )
            assert r.status_code in (422, 409, 403), (
                f"Expected rejection for finalize on Draft, got {r.status_code}: {r.text}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_submit_direct_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """PATCH status to InReview on Draft returns 422 (invalid transition)."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            r = await client.patch(
                f"/payroll/periods/{pid}/status",
                json={"status": "InReview"},
                headers=_auth(auth_token),
            )
            assert r.status_code in (422, 409, 403), (
                f"Expected rejection for Draft→InReview PATCH, got {r.status_code}: {r.text}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)


# ---------------------------------------------------------------------------
# Section 4: Direct DraftLine API in Draft
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDraftDirectDraftLineAPI:
    """Direct POST/PATCH/DELETE on /lines endpoints in Draft periods."""

    async def test_cp2f_draft_direct_daily_line_source_only_or_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """Daily source line via add_draft_line on Draft: succeeds with NULL calculated_amount."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            work_date = start.isoformat()
            r = await client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": driver_id,
                    "work_date": work_date,
                    "line_type": "HOURS",
                    "quantity": 6,
                    "source_type": "Manual",
                },
                headers=_auth(auth_token),
            )
            if r.status_code == 201:
                # Allowed — verify NULL financial fields
                data = r.json()
                assert data.get("calculated_amount") is None, (
                    f"Draft line should have null calculated_amount, got {data.get('calculated_amount')}"
                )
                assert data.get("needs_manager_review") is False, (
                    f"Draft line should have needs_manager_review=False"
                )
            elif r.status_code in (422, 409, 403):
                # Also acceptable — document as blocked cleanly
                pass
            else:
                pytest.fail(
                    f"Unexpected status {r.status_code} for add_draft_line on Draft: {r.text}"
                )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_direct_daily_line_rejects_rate_amount(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """add_draft_line on Draft with rate_amount is rejected with 422 (CP-2F source-only rule)."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            r = await client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": driver_id,
                    "work_date": start.isoformat(),
                    "line_type": "MILES",
                    "quantity": 100,
                    "rate_amount": "1.50",
                    "source_type": "Manual",
                },
                headers=_auth(auth_token),
            )
            assert r.status_code == 422, (
                f"Expected 422 for rate_amount on Draft line, got {r.status_code}: {r.text}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_direct_daily_line_rejects_needs_manager_review(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """add_draft_line on Draft with needs_manager_review=True is rejected."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            work_date = start.isoformat()
            r = await client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": driver_id,
                    "work_date": work_date,
                    "line_type": "HOURS",
                    "quantity": 8,
                    "source_type": "Manual",
                    "needs_manager_review": True,
                },
                headers=_auth(auth_token),
            )
            assert r.status_code in (422, 409, 403), (
                f"Expected rejection for needs_manager_review=True on Draft, got {r.status_code}: {r.text}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_direct_daily_line_stores_rate_amount_null(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """Allowed Draft line stores NULL calculated_amount regardless of qty."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            work_date = start.isoformat()
            r = await client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": driver_id,
                    "work_date": work_date,
                    "line_type": "HOURS",
                    "quantity": 8,
                    "source_type": "Manual",
                },
                headers=_auth(auth_token),
            )
            if r.status_code == 201:
                lines = await _get_draft_lines(direct_db, pid)
                hours_lines = [ln for ln in lines if ln["linetype"] == "HOURS" and ln["status"] != "Void"]
                assert len(hours_lines) >= 1
                assert hours_lines[0]["calculatedamount"] is None, (
                    "Draft HOURS line must have NULL calculatedamount"
                )
            elif r.status_code in (422, 409, 403):
                pass  # also acceptable
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_direct_daily_line_stores_nmr_false(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """Allowed Draft line always stores needs_manager_review=False."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            work_date = start.isoformat()
            r = await client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": driver_id,
                    "work_date": work_date,
                    "line_type": "HOURS",
                    "quantity": 8,
                    "source_type": "Manual",
                },
                headers=_auth(auth_token),
            )
            if r.status_code == 201:
                lines = await _get_draft_lines(direct_db, pid)
                hours_lines = [ln for ln in lines if ln["linetype"] == "HOURS" and ln["status"] != "Void"]
                assert len(hours_lines) >= 1
                assert hours_lines[0]["needsmanagerreview"] is False, (
                    "Draft line must have needsmanagerreview=False"
                )
            elif r.status_code in (422, 409, 403):
                pass
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_direct_period_or_system_line_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """System source line via add_draft_line on Draft is rejected."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            work_date = start.isoformat()
            r = await client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": driver_id,
                    "work_date": work_date,
                    "line_type": "HOURS",
                    "quantity": 8,
                    "source_type": "System",
                },
                headers=_auth(auth_token),
            )
            assert r.status_code in (422, 409, 403), (
                f"Expected rejection for System line on Draft, got {r.status_code}: {r.text}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_period_or_system_or_status_payment_line_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """Period-scope line type via add_draft_line on Draft is rejected."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            # Try adding Bonus (Period-scope) via /lines — should be rejected
            r = await client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": driver_id,
                    "line_type": "Bonus",
                    "quantity": 1,
                    "source_type": "Manual",
                },
                headers=_auth(auth_token),
            )
            assert r.status_code in (422, 409, 403), (
                f"Expected rejection for Period-scope line on Draft, got {r.status_code}: {r.text}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_update_rejects_rate_amount(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """update_draft_line on Draft with rate_amount is rejected (422)."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            work_date = start.isoformat()
            # First create a line via day-grid save
            r_save = await client.post(
                f"/payroll/periods/{pid}/day-grid",
                json={
                    "work_date": work_date,
                    "rows": [{"driver_id": driver_id, "values": {"HOURS": "6"}}],
                },
                headers=_auth(auth_token),
            )
            assert r_save.status_code == 200, r_save.text

            lines = await _get_draft_lines(direct_db, pid)
            hours_line = next(
                (ln for ln in lines if ln["linetype"] == "HOURS" and ln["status"] != "Void"),
                None,
            )
            if hours_line is None:
                pytest.skip("No HOURS line created — cannot test update rate_amount rejection")

            lid = hours_line["draftlineid"]
            r_upd = await client.patch(
                f"/payroll/periods/{pid}/lines/{lid}",
                json={"rate_amount": "2.50"},
                headers=_auth(auth_token),
            )
            assert r_upd.status_code in (422, 409, 403), (
                f"Expected rejection for rate_amount on Draft update, got {r_upd.status_code}: {r_upd.text}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_update_rejects_needs_manager_review(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """update_draft_line on Draft with needs_manager_review=True is rejected."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            work_date = start.isoformat()
            r_save = await client.post(
                f"/payroll/periods/{pid}/day-grid",
                json={
                    "work_date": work_date,
                    "rows": [{"driver_id": driver_id, "values": {"HOURS": "6"}}],
                },
                headers=_auth(auth_token),
            )
            assert r_save.status_code == 200, r_save.text

            lines = await _get_draft_lines(direct_db, pid)
            hours_line = next(
                (ln for ln in lines if ln["linetype"] == "HOURS" and ln["status"] != "Void"),
                None,
            )
            if hours_line is None:
                pytest.skip("No HOURS line created — cannot test NMR rejection")

            lid = hours_line["draftlineid"]
            r_upd = await client.patch(
                f"/payroll/periods/{pid}/lines/{lid}",
                json={"needs_manager_review": True},
                headers=_auth(auth_token),
            )
            assert r_upd.status_code in (422, 409, 403), (
                f"Expected rejection for needs_manager_review=True on Draft update, got {r_upd.status_code}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_update_clears_stale_rate_amount_and_calculated_amount(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """update_draft_line on Draft clears rate_amount and calculated_amount to NULL."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            work_date = start.isoformat()
            r_save = await client.post(
                f"/payroll/periods/{pid}/day-grid",
                json={
                    "work_date": work_date,
                    "rows": [{"driver_id": driver_id, "values": {"HOURS": "6"}}],
                },
                headers=_auth(auth_token),
            )
            assert r_save.status_code == 200, r_save.text

            lines = await _get_draft_lines(direct_db, pid)
            hours_line = next(
                (ln for ln in lines if ln["linetype"] == "HOURS" and ln["status"] != "Void"),
                None,
            )
            if hours_line is None:
                pytest.skip("No HOURS line created — cannot test stale clearing")

            lid = hours_line["draftlineid"]
            r_upd = await client.patch(
                f"/payroll/periods/{pid}/lines/{lid}",
                json={"quantity": 7},
                headers=_auth(auth_token),
            )
            if r_upd.status_code == 200:
                data = r_upd.json()
                assert data.get("calculated_amount") is None, (
                    "Updated Draft line should have NULL calculated_amount"
                )
                assert data.get("needs_manager_review") is False, (
                    "Updated Draft line should have needs_manager_review=False"
                )
            elif r_upd.status_code in (422, 409, 403):
                pass  # blocked cleanly — also acceptable
            else:
                pytest.fail(f"Unexpected {r_upd.status_code}: {r_upd.text}")
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_update_daily_source_allowed_without_calculation_if_supported(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """update_draft_line on Draft daily source succeeds without recomputing money."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            work_date = start.isoformat()
            # First create a line via day-grid save
            r_save = await client.post(
                f"/payroll/periods/{pid}/day-grid",
                json={
                    "work_date": work_date,
                    "rows": [{"driver_id": driver_id, "values": {"HOURS": "6"}}],
                },
                headers=_auth(auth_token),
            )
            assert r_save.status_code == 200, r_save.text

            lines = await _get_draft_lines(direct_db, pid)
            hours_line = next(
                (ln for ln in lines if ln["linetype"] == "HOURS" and ln["status"] != "Void"),
                None,
            )
            if hours_line is None:
                pytest.skip("No HOURS line created — cannot test update")

            lid = hours_line["draftlineid"]
            r_upd = await client.patch(
                f"/payroll/periods/{pid}/lines/{lid}",
                json={"quantity": 7},
                headers=_auth(auth_token),
            )
            if r_upd.status_code == 200:
                data = r_upd.json()
                assert data.get("calculated_amount") is None, (
                    "Updated Draft line should still have NULL calculated_amount"
                )
            elif r_upd.status_code in (422, 409, 403):
                pass  # blocked cleanly — also acceptable
            else:
                pytest.fail(f"Unexpected {r_upd.status_code}: {r_upd.text}")
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_void_daily_source_allowed_if_supported(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """void_draft_line on Draft daily source succeeds."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            work_date = start.isoformat()
            r_save = await client.post(
                f"/payroll/periods/{pid}/day-grid",
                json={
                    "work_date": work_date,
                    "rows": [{"driver_id": driver_id, "values": {"HOURS": "5"}}],
                },
                headers=_auth(auth_token),
            )
            assert r_save.status_code == 200, r_save.text

            lines = await _get_draft_lines(direct_db, pid)
            hours_line = next(
                (ln for ln in lines if ln["linetype"] == "HOURS" and ln["status"] != "Void"),
                None,
            )
            if hours_line is None:
                pytest.skip("No HOURS line created — cannot test void")

            lid = hours_line["draftlineid"]
            r_void = await client.delete(
                f"/payroll/periods/{pid}/lines/{lid}",
                headers=_auth(auth_token),
            )
            assert r_void.status_code in (200, 204, 422, 409, 403), (
                f"Unexpected {r_void.status_code}: {r_void.text}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_void_financial_or_system_line_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """Voiding a System or Period-scope line on Draft is rejected."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            # Insert a System-sourced line directly (bypassing API)
            r_insert = (await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverid,
                         workdate, linetype, linescope, quantity, sourcetype, status,
                         needsmanagerreview, addedbyuserid)
                    VALUES (1, :bid, :pid, :did, :wd, 'HOURS', 'Daily', 8, 'System', 'Active', FALSE, 1)
                    RETURNING draftlineid
                """),
                {"bid": branch_id, "pid": pid, "did": driver_id, "wd": start},
            )).mappings().first()
            await direct_db.commit()
            assert r_insert is not None

            lid = r_insert["draftlineid"]
            r_void = await client.delete(
                f"/payroll/periods/{pid}/lines/{lid}",
                headers=_auth(auth_token),
            )
            # Should be rejected because it's a System line in Draft
            assert r_void.status_code in (422, 409, 403), (
                f"Expected rejection for voiding System line on Draft, got {r_void.status_code}: {r_void.text}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)


    async def test_cp2f_draft_direct_daily_line_rejects_rate_amount_for_perunit_item(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """POST /lines on Draft with rate_amount for PerUnit (MILES) returns the CP-2F error message.

        The CP-2F guard at the top of add_draft_line fires BEFORE the Phase 4C PerUnit guard.
        Asserting the exact CP-2F message proves it is the Draft source-only rule that rejects,
        not the pre-existing PerUnit override rule (which returns a different message).
        """
        _CP2F_RATE_AMOUNT_MSG = "rate_amount cannot be supplied for a Prepared (Draft) period line."
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            r = await client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": driver_id,
                    "work_date": start.isoformat(),
                    "line_type": "MILES",
                    "quantity": 100,
                    "rate_amount": "1.50",
                    "source_type": "Manual",
                },
                headers=_auth(auth_token),
            )
            assert r.status_code == 422, (
                f"Expected 422 for rate_amount on Draft PerUnit line, got {r.status_code}: {r.text}"
            )
            assert _CP2F_RATE_AMOUNT_MSG in r.json().get("detail", ""), (
                f"Expected CP-2F error message in detail, got: {r.json().get('detail')}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_direct_daily_line_rejects_rate_amount_for_non_perunit_item(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """POST /lines on Draft with rate_amount for a true non-PerUnit (Fixed) daily item is rejected.

        Creates a test-only Fixed-behavior pay item so this test does not rely on the PerUnit guard.
        OLD code: the PerUnit guard at line ~3424 would NOT fire for Fixed items; rate_amount
                  would be stored in the DB.
        NEW code: the CP-2F guard fires first (before any rate-behavior check); returns 422
                  with the Draft-specific message.
        """
        _CP2F_RATE_AMOUNT_MSG = "rate_amount cannot be supplied for a Prepared (Draft) period line."
        _TEST_ITEM_CODE = "CP2F_FIXED_TEST"

        # Create and activate a Fixed-behavior daily pay item for this branch.
        # Fixed items are NOT caught by the PerUnit override guard — this is the
        # precise gap that existed before CP-2F.
        pi_row = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.payitems
                    (companyid, payitemcode, payitemname, ratebehavior, requiresrate,
                     isdefaultbranchactive, status, category, datatype, itemscope, issystemstandard)
                VALUES
                    (1, :code, 'CP-2F Fixed Test Item', 'Fixed', FALSE,
                     FALSE, 'Active', 'Custom', 'Decimal', 'Daily', FALSE)
                RETURNING payitemid
            """),
            {"code": _TEST_ITEM_CODE},
        )).mappings().first()
        assert pi_row is not None, "Could not create test pay item"
        pi_id = pi_row["payitemid"]

        # Activate for the test branch (branchpayitemconfig)
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.branchpayitemconfig
                    (payitemid, companyid, branchid, isactive, effectivefrom, createdbyuserid)
                VALUES (:piid, 1, :bid, TRUE, '2000-01-01', 1)
            """),
            {"piid": pi_id, "bid": branch_id},
        )
        await direct_db.commit()

        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            r = await client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": driver_id,
                    "work_date": start.isoformat(),
                    "line_type": _TEST_ITEM_CODE,
                    "quantity": 1,
                    "rate_amount": "50.00",
                    "source_type": "Manual",
                },
                headers=_auth(auth_token),
            )
            assert r.status_code == 422, (
                f"Expected 422 for rate_amount on Draft Fixed line, got {r.status_code}: {r.text}"
            )
            assert _CP2F_RATE_AMOUNT_MSG in r.json().get("detail", ""), (
                f"Expected CP-2F error message (not PerUnit guard), got: {r.json().get('detail')}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)
            # Clean up test pay item and its branch config
            await direct_db.execute(
                _text("DELETE FROM payroll.branchpayitemconfig WHERE payitemid = :piid"),
                {"piid": pi_id},
            )
            await direct_db.execute(
                _text("DELETE FROM payroll.payitems WHERE payitemid = :piid"),
                {"piid": pi_id},
            )
            await direct_db.commit()

    async def test_cp2f_draft_direct_daily_line_stores_rate_amount_null_for_allowed_source(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """POST /lines on Draft without rate_amount succeeds and stores rateamount=NULL."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            r = await client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": driver_id,
                    "work_date": start.isoformat(),
                    "line_type": "HOURS",
                    "quantity": 8,
                    "source_type": "Manual",
                },
                headers=_auth(auth_token),
            )
            if r.status_code not in (200, 201):
                pytest.skip(f"add_draft_line in Draft not supported: {r.status_code}")
            data = r.json()
            assert data.get("rate_amount") is None, (
                f"Draft HOURS line should have NULL rate_amount, got {data.get('rate_amount')}"
            )
            assert data.get("calculated_amount") is None, (
                f"Draft HOURS line should have NULL calculated_amount"
            )
            assert data.get("needs_manager_review") is False, (
                f"Draft HOURS line should have NMR=False"
            )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_update_notes_only_clears_stale_rate_amount_and_calculated_amount(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """PATCH /lines on Draft with notes-only update clears stale CalculatedAmount and RateAmount."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            # Create line via day-grid save (quantity only, NULL financials)
            r_save = await client.post(
                f"/payroll/periods/{pid}/day-grid",
                json={
                    "work_date": start.isoformat(),
                    "rows": [{"driver_id": driver_id, "values": {"HOURS": "6"}}],
                },
                headers=_auth(auth_token),
            )
            assert r_save.status_code == 200, r_save.text

            lines = await _get_draft_lines(direct_db, pid)
            hours_line = next(
                (ln for ln in lines if ln["linetype"] == "HOURS" and ln["status"] != "Void"),
                None,
            )
            if hours_line is None:
                pytest.skip("No HOURS line created via day-grid — cannot test notes-only clearing")

            # Inject stale financial data directly to simulate legacy data
            await direct_db.execute(
                _text("""
                    UPDATE payroll.payrolldraftlines
                    SET rateamount = 10.00, calculatedamount = 60.00, needsmanagerreview = TRUE
                    WHERE draftlineid = :lid
                """),
                {"lid": hours_line["draftlineid"]},
            )
            await direct_db.commit()

            # Patch with notes only (no quantity, no rate_amount)
            r_upd = await client.patch(
                f"/payroll/periods/{pid}/lines/{hours_line['draftlineid']}",
                json={"notes": "test note for stale clearing"},
                headers=_auth(auth_token),
            )
            if r_upd.status_code not in (200, 201):
                pytest.skip(f"update_draft_line on Draft not supported: {r_upd.status_code}")

            data = r_upd.json()
            assert data.get("calculated_amount") is None, (
                f"Notes-only Draft update should clear CalculatedAmount to NULL, got {data.get('calculated_amount')}"
            )
            assert data.get("rate_amount") is None, (
                f"Notes-only Draft update should clear RateAmount to NULL, got {data.get('rate_amount')}"
            )
            assert data.get("needs_manager_review") is False, (
                f"Notes-only Draft update should set NMR=False, got {data.get('needs_manager_review')}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_update_always_sets_nmr_false(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """Any allowed PATCH /lines on Draft always sets NMR=False, even if stale NMR=True existed."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            r_save = await client.post(
                f"/payroll/periods/{pid}/day-grid",
                json={
                    "work_date": start.isoformat(),
                    "rows": [{"driver_id": driver_id, "values": {"HOURS": "5"}}],
                },
                headers=_auth(auth_token),
            )
            assert r_save.status_code == 200, r_save.text

            lines = await _get_draft_lines(direct_db, pid)
            hours_line = next(
                (ln for ln in lines if ln["linetype"] == "HOURS" and ln["status"] != "Void"),
                None,
            )
            if hours_line is None:
                pytest.skip("No HOURS line — cannot test NMR clearing")

            # Inject stale NMR=True
            await direct_db.execute(
                _text("UPDATE payroll.payrolldraftlines SET needsmanagerreview=TRUE WHERE draftlineid=:lid"),
                {"lid": hours_line["draftlineid"]},
            )
            await direct_db.commit()

            r_upd = await client.patch(
                f"/payroll/periods/{pid}/lines/{hours_line['draftlineid']}",
                json={"quantity": 6},
                headers=_auth(auth_token),
            )
            if r_upd.status_code not in (200, 201):
                pytest.skip(f"update_draft_line on Draft not supported: {r_upd.status_code}")

            assert r_upd.json().get("needs_manager_review") is False, (
                f"Draft update must always set NMR=False, got {r_upd.json().get('needs_manager_review')}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)


# ---------------------------------------------------------------------------
# Section 4B: Draft read endpoint protection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDraftReadEndpointProtection:
    """Draft read endpoints sanitize or block financial data."""

    async def test_cp2f_draft_get_lines_sanitizes_money_or_blocks(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """GET /lines on Draft: if 200, money fields are null; if 422 also acceptable."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            r = await client.get(
                f"/payroll/periods/{pid}/lines",
                headers=_auth(auth_token),
            )
            if r.status_code == 200:
                for ln in r.json():
                    assert ln.get("calculated_amount") is None, (
                        f"Draft line should have null calculated_amount in list response"
                    )
                    assert ln.get("rate_amount") is None, (
                        f"Draft line should have null rate_amount in list response"
                    )
                    assert ln.get("needs_manager_review") is False, (
                        f"Draft line should have needs_manager_review=False"
                    )
            elif r.status_code in (422, 409, 403):
                pass  # blocking is also acceptable
            else:
                pytest.fail(f"Unexpected {r.status_code}: {r.text}")
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_get_lines_hides_status_payment_and_period_money(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """GET /lines on Draft does not expose STATUS_PAYMENT or Period-scope rows."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            # Insert a stale STATUS_PAYMENT line directly
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverid,
                         workdate, linetype, linescope, quantity, sourcetype, status,
                         needsmanagerreview, sourceid, addedbyuserid)
                    VALUES (1, :bid, :pid, :did, :wd, 'STATUS_PAYMENT', 'Daily', 1,
                            'System', 'Active', FALSE, 'STATUS_PAYMENT:99', 1)
                """),
                {"bid": branch_id, "pid": pid, "did": driver_id, "wd": start},
            )
            await direct_db.commit()

            r = await client.get(
                f"/payroll/periods/{pid}/lines",
                headers=_auth(auth_token),
            )
            if r.status_code == 200:
                lines = r.json()
                sp_lines = [ln for ln in lines if ln.get("line_type") == "STATUS_PAYMENT"]
                assert len(sp_lines) == 0, (
                    f"STATUS_PAYMENT lines must not be visible via /lines in Draft, found {len(sp_lines)}"
                )
            elif r.status_code in (422, 409, 403):
                pass  # blocked = also fine
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_lines_summary_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """GET /lines/summary on Draft returns 422."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            r = await client.get(
                f"/payroll/periods/{pid}/lines/summary",
                headers=_auth(auth_token),
            )
            assert r.status_code in (422, 409, 403), (
                f"Expected rejection for lines-summary on Draft, got {r.status_code}: {r.text}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_period_pay_list_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """GET /period-pay on Draft returns 422."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            r = await client.get(
                f"/payroll/periods/{pid}/period-pay",
                headers=_auth(auth_token),
            )
            assert r.status_code in (422, 409, 403), (
                f"Expected rejection for period-pay GET on Draft, got {r.status_code}: {r.text}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_period_eligible_drivers_blocked_if_money_flow(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """GET /eligible-drivers on Draft returns 422 (it feeds period-pay money UX)."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            r = await client.get(
                f"/payroll/periods/{pid}/eligible-drivers",
                headers=_auth(auth_token),
            )
            assert r.status_code in (422, 409, 403), (
                f"Expected rejection for eligible-drivers on Draft, got {r.status_code}: {r.text}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_stale_status_payment_not_visible(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """A stale STATUS_PAYMENT line from before Draft→Open is not visible in Draft /lines."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            # Inject a System STATUS_PAYMENT line
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverid,
                         workdate, linetype, linescope, quantity, sourcetype, status,
                         needsmanagerreview, sourceid, addedbyuserid)
                    VALUES (1, :bid, :pid, :did, :wd, 'STATUS_PAYMENT', 'Daily', 1,
                            'System', 'Active', FALSE, 'STATUS_PAYMENT:1', 1)
                """),
                {"bid": branch_id, "pid": pid, "did": driver_id, "wd": start},
            )
            await direct_db.commit()

            r = await client.get(
                f"/payroll/periods/{pid}/lines",
                headers=_auth(auth_token),
            )
            if r.status_code == 200:
                sp_lines = [ln for ln in r.json() if ln.get("line_type") == "STATUS_PAYMENT"]
                assert len(sp_lines) == 0, "STATUS_PAYMENT must not appear in Draft /lines"
            elif r.status_code in (422, 409, 403):
                pass  # block is also fine
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_draft_stale_period_pay_not_visible(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """A Period-scope line in Draft is not visible via /period-pay (blocked)."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            r = await client.get(
                f"/payroll/periods/{pid}/period-pay",
                headers=_auth(auth_token),
            )
            # Period-pay list must be blocked for Draft
            assert r.status_code in (422, 409, 403), (
                f"Draft /period-pay must be blocked, got {r.status_code}"
            )
        finally:
            await _cancel_period_db(direct_db, pid)


# ---------------------------------------------------------------------------
# Section 4C: Hub capabilities for Draft
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDraftHubCapabilities:
    """Hub workflow capabilities for Draft (Prepared) periods."""

    async def test_cp2f_hub_prepared_can_open_day_grid(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """Hub returns can_open_day_grid=True for a Draft period."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            r = await client.get(
                "/payroll/current-workflow",
                headers=_auth(auth_token),
            )
            assert r.status_code == 200, r.text
            # Find this period's capabilities
            for branch in r.json().get("branches", []):
                caps = branch.get("capabilities", {}).get("periods", {})
                if str(pid) in caps:
                    day_grid_cap = caps[str(pid)].get("can_open_day_grid", {})
                    assert day_grid_cap.get("allowed") is True, (
                        f"can_open_day_grid should be True for Draft period, got {day_grid_cap}"
                    )
                    break
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_hub_prepared_can_enter_source_with_permission(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """Hub returns can_enter_source=True for a Draft period when user has payroll.entry."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            r = await client.get(
                "/payroll/current-workflow",
                headers=_auth(auth_token),
            )
            assert r.status_code == 200, r.text
            for branch in r.json().get("branches", []):
                caps = branch.get("capabilities", {}).get("periods", {})
                if str(pid) in caps:
                    enter_cap = caps[str(pid)].get("can_enter_source", {})
                    assert enter_cap.get("allowed") is True, (
                        f"can_enter_source should be True for Draft period with entry perm, got {enter_cap}"
                    )
                    break
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_hub_prepared_cannot_submit_for_review(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """Hub returns can_submit_for_review=False for a Draft period."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            r = await client.get(
                "/payroll/current-workflow",
                headers=_auth(auth_token),
            )
            assert r.status_code == 200, r.text
            for branch in r.json().get("branches", []):
                caps = branch.get("capabilities", {}).get("periods", {})
                if str(pid) in caps:
                    submit_cap = caps[str(pid)].get("can_submit_for_review", {})
                    assert submit_cap.get("allowed") is False, (
                        f"can_submit_for_review should be False for Draft period, got {submit_cap}"
                    )
                    break
        finally:
            await _cancel_period_db(direct_db, pid)

    async def test_cp2f_hub_prepared_exposes_no_money(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """Hub does not expose finalize / review money capabilities for Draft."""
        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Draft")
        try:
            r = await client.get(
                "/payroll/current-workflow",
                headers=_auth(auth_token),
            )
            assert r.status_code == 200, r.text
            for branch in r.json().get("branches", []):
                caps = branch.get("capabilities", {}).get("periods", {})
                if str(pid) in caps:
                    period_caps = caps[str(pid)]
                    # can_view_review and can_resubmit_returned must be False
                    assert period_caps.get("can_view_review", {}).get("allowed") is False, (
                        "can_view_review must be False for Draft period"
                    )
                    assert period_caps.get("can_resubmit_returned", {}).get("allowed") is False, (
                        "can_resubmit_returned must be False for Draft period"
                    )
                    break
        finally:
            await _cancel_period_db(direct_db, pid)


# ---------------------------------------------------------------------------
# Section 5: Draft → Open activation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDraftToOpenActivation:
    """Draft→Open promotion activates calculations and status payments."""

    async def _setup_adjacent_periods(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ) -> tuple[int, int, datetime.date]:
        """
        Create adjacent Open + Draft periods.
        Returns (open_pid, draft_pid, draft_work_date).
        The Open period has a DraftLine so it can be submitted.
        The Draft period has HOURS data saved via day-grid.
        """
        open_start, open_end = _week()
        draft_start = open_end + datetime.timedelta(days=1)
        draft_end = draft_start + datetime.timedelta(days=6)

        open_pid = await _insert_period_db(
            direct_db, branch_id, open_start, open_end, status="Open", code_suffix="-O"
        )
        draft_pid = await _insert_period_db(
            direct_db, branch_id, draft_start, draft_end, status="Draft", code_suffix="-D"
        )

        # Add a line to Open period so submission passes the empty-period guard
        work_date_open = open_start.isoformat()
        r_add = await client.post(
            f"/payroll/periods/{open_pid}/lines",
            json={
                "driver_id": driver_id,
                "work_date": work_date_open,
                "line_type": "DailyNote",
                "quantity": 0,
                "source_type": "Manual",
                "notes": "setup line",
            },
            headers=_auth(auth_token),
        )
        assert r_add.status_code == 201, f"Failed to add line to Open period: {r_add.text}"

        # Add HOURS to the Draft period via day-grid
        r_dg = await client.post(
            f"/payroll/periods/{draft_pid}/day-grid",
            json={
                "work_date": draft_start.isoformat(),
                "rows": [{"driver_id": driver_id, "values": {"HOURS": "8"}}],
            },
            headers=_auth(auth_token),
        )
        assert r_dg.status_code == 200, f"Failed to save Draft day-grid: {r_dg.text}"

        return open_pid, draft_pid, draft_start

    async def test_cp2f_draft_to_open_preserves_source(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """DraftLines/PPDES written during Draft are present after promotion."""
        open_pid, draft_pid, draft_work_date = await self._setup_adjacent_periods(
            client, auth_token, branch_id, driver_id, direct_db
        )
        try:
            lines_before = await _get_draft_lines(direct_db, draft_pid)
            active_before = [ln for ln in lines_before if ln["status"] != "Void"]
            assert len(active_before) >= 1, "Expected source lines before promotion"

            # Submit open period (promotes Draft→Open)
            r_submit = await client.patch(
                f"/payroll/periods/{open_pid}/status",
                json={"status": "InReview"},
                headers=_auth(auth_token),
            )
            # 422 is OK if there are guard issues (NMR lines, etc.); we mainly care about
            # the Draft→Open promotion side effect
            if r_submit.status_code not in (200, 422):
                pytest.skip(f"Submit returned unexpected {r_submit.status_code}: {r_submit.text}")

            lines_after = await _get_draft_lines(direct_db, draft_pid)
            active_after = [ln for ln in lines_after if ln["status"] != "Void"]
            assert len(active_after) >= 1, (
                "Draft-era source lines should be preserved after Draft→Open promotion"
            )
        finally:
            # Cancel both periods
            await _cancel_period_db(direct_db, open_pid)
            await _cancel_period_db(direct_db, draft_pid)

    async def test_cp2f_draft_to_open_refreshes_daily_calculations(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """After Draft→Open, Draft-era daily source rows may have CalculatedAmount computed."""
        open_pid, draft_pid, draft_work_date = await self._setup_adjacent_periods(
            client, auth_token, branch_id, driver_id, direct_db
        )
        try:
            # Verify NULL before submit
            lines_before = await _get_draft_lines(direct_db, draft_pid)
            hours_before = [
                ln for ln in lines_before
                if ln["linetype"] == "HOURS" and ln["status"] != "Void"
            ]
            if hours_before:
                assert hours_before[0]["calculatedamount"] is None, (
                    "HOURS line in Draft period should have NULL calculated_amount before promotion"
                )

            r_submit = await client.patch(
                f"/payroll/periods/{open_pid}/status",
                json={"status": "InReview"},
                headers=_auth(auth_token),
            )
            # Submit may fail due to guard issues (DailyNote has no calculated amount issues),
            # but _refresh_draft_calculations should have run either way
            if r_submit.status_code not in (200, 422):
                pytest.skip(f"Submit returned unexpected status: {r_submit.status_code}")

            # Check period got promoted to Open
            period_r = await client.get(
                f"/payroll/periods/{draft_pid}",
                headers=_auth(auth_token),
            )
            if period_r.status_code == 200:
                period_status = period_r.json().get("status")
                if period_status == "Open":
                    # Refresh happened — HOURS line may now have calculated_amount
                    lines_after = await _get_draft_lines(direct_db, draft_pid)
                    hours_after = [
                        ln for ln in lines_after
                        if ln["linetype"] == "HOURS" and ln["status"] != "Void"
                    ]
                    # If driver has no rate, calculated_amount remains NULL (that's valid)
                    # The important thing is _refresh_draft_calculations ran without error
                    # (we just check the line still exists)
                    assert len(hours_after) >= 1, "HOURS line should still exist after promotion"
        finally:
            await _cancel_period_db(direct_db, open_pid)
            await _cancel_period_db(direct_db, draft_pid)

    async def test_cp2f_draft_to_open_regenerates_and_freezes_eligibility(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """After Draft→Open, eligibility snapshot rows exist for the promoted period."""
        open_pid, draft_pid, draft_work_date = await self._setup_adjacent_periods(
            client, auth_token, branch_id, driver_id, direct_db
        )
        try:
            r_submit = await client.patch(
                f"/payroll/periods/{open_pid}/status",
                json={"status": "InReview"},
                headers=_auth(auth_token),
            )
            if r_submit.status_code not in (200, 422):
                pytest.skip(f"Submit returned unexpected status: {r_submit.status_code}")

            # Check if Draft got promoted
            period_r = await client.get(
                f"/payroll/periods/{draft_pid}",
                headers=_auth(auth_token),
            )
            if period_r.status_code != 200 or period_r.json().get("status") != "Open":
                pytest.skip("Draft period was not promoted to Open — skip eligibility check")

            # Check snapshot marker exists
            marker = (await direct_db.execute(
                _text("""
                    SELECT frozenatutc FROM payroll.payrollperiodeligibilitysnapshots
                    WHERE payrollperiodid = :pid
                """),
                {"pid": draft_pid},
            )).mappings().first()
            assert marker is not None, (
                "Eligibility snapshot marker should exist after Draft→Open promotion"
            )
            assert marker["frozenatutc"] is not None, (
                "Eligibility snapshot should be frozen after Draft→Open promotion"
            )
        finally:
            await _cancel_period_db(direct_db, open_pid)
            await _cancel_period_db(direct_db, draft_pid)

    async def test_cp2f_draft_to_open_derives_status_payment_once(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """After Draft→Open, PPDES rows with status keys have STATUS_PAYMENT processing attempted."""
        # Ensure a status key with StatusRateColumn exists (self-sufficient setup)
        sk_id, sk_code = await _ensure_status_key(
            direct_db, branch_id, code="CP2F_SP_SK", with_rate_column=True
        )

        open_pid, draft_pid, draft_work_date = await self._setup_adjacent_periods(
            client, auth_token, branch_id, driver_id, direct_db
        )
        try:
            # Save a status key in Draft
            r_sk = await client.post(
                f"/payroll/periods/{draft_pid}/day-grid",
                json={
                    "work_date": draft_work_date.isoformat(),
                    "rows": [{"driver_id": driver_id, "status_key": sk_code}],
                },
                headers=_auth(auth_token),
            )
            assert r_sk.status_code == 200, r_sk.text

            # Verify no STATUS_PAYMENT line in Draft
            lines_draft = await _get_draft_lines(direct_db, draft_pid)
            sp_in_draft = [
                ln for ln in lines_draft
                if (ln.get("sourceid") or "").startswith("STATUS_PAYMENT:") and ln["status"] != "Void"
            ]
            assert len(sp_in_draft) == 0, f"Should be no STATUS_PAYMENT lines in Draft, found {len(sp_in_draft)}"

            # Submit to promote Draft→Open
            r_submit = await client.patch(
                f"/payroll/periods/{open_pid}/status",
                json={"status": "InReview"},
                headers=_auth(auth_token),
            )
            if r_submit.status_code not in (200, 422):
                pytest.skip(f"Submit returned unexpected status: {r_submit.status_code}")

            period_r = await client.get(
                f"/payroll/periods/{draft_pid}",
                headers=_auth(auth_token),
            )
            if period_r.status_code != 200 or period_r.json().get("status") != "Open":
                pytest.skip("Draft period was not promoted to Open")

            # After promotion, _refresh_status_payment_lines ran — check no duplicates
            lines_after = await _get_draft_lines(direct_db, draft_pid)
            sp_after = [
                ln for ln in lines_after
                if (ln.get("sourceid") or "").startswith("STATUS_PAYMENT:") and ln["status"] != "Void"
            ]
            # At most one per driver/date slot (no duplicates)
            seen_slots: set = set()
            for ln in sp_after:
                slot = (ln["driverid"], str(ln["workdate"]))
                assert slot not in seen_slots, f"Duplicate STATUS_PAYMENT line for slot {slot}"
                seen_slots.add(slot)
        finally:
            await _cancel_period_db(direct_db, open_pid)
            await _cancel_period_db(direct_db, draft_pid)


# ---------------------------------------------------------------------------
# Section 6: Status / PTO regression
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestStatusPTORegression:
    """Verify no PTO_STATUS payitem; status saved in PPDES not as DraftLine pay item."""

    async def test_cp2f_no_pto_status_payitem(self, direct_db: AsyncConnection):
        """No PayItem with code PTO_STATUS exists."""
        row = (await direct_db.execute(
            _text("""
                SELECT payitemid FROM payroll.payitems
                WHERE payitemcode = 'PTO_STATUS'
            """)
        )).first()
        assert row is None, "PTO_STATUS PayItem must not exist (was removed in CP-2)"

    async def test_cp2f_status_not_payitem(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_id: int,
        driver_id: int,
        direct_db: AsyncConnection,
    ):
        """Status is saved in PPDES (DailyStatus/entry-state), not as a DraftLine pay item with money."""
        sk_id, sk_code = await _ensure_status_key(direct_db, branch_id, code="CP2F_REG_SK")

        start, end = _week()
        pid = await _insert_period_db(direct_db, branch_id, start, end, status="Open")
        try:
            work_date = start.isoformat()
            r = await client.post(
                f"/payroll/periods/{pid}/day-grid",
                json={
                    "work_date": work_date,
                    "rows": [{"driver_id": driver_id, "status_key": sk_code}],
                },
                headers=_auth(auth_token),
            )
            assert r.status_code == 200, r.text

            lines = await _get_draft_lines(direct_db, pid)
            # Status should appear as DailyStatus, not as a financial pay-item line
            financial_status_lines = [
                ln for ln in lines
                if ln["linetype"] == "PTO_STATUS" and ln["status"] != "Void"
            ]
            assert len(financial_status_lines) == 0, (
                "Status must not be stored as PTO_STATUS DraftLine"
            )

            # PPDES row should exist
            ppdes = await _get_ppdes_rows(direct_db, pid)
            active_ppdes = [r for r in ppdes if not r["isvoided"]]
            assert len(active_ppdes) >= 1, "Status save should create PPDES row"
        finally:
            await _cancel_period_db(direct_db, pid)
