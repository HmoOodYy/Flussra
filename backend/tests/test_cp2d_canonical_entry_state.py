"""
CP-2D1: Canonical daily driver/day entry-state tests.

Product contracts verified:
  - Migration 0054: PayrollPeriodDriverDayEntryState table exists with required
    columns, unique constraint, FKs, and indexes.
  - Live dropdown: newly-created active Status Key appears in get_day_grid
    without period reopen (not snapshotted at creation time).
  - save_day_grid creates/upserts a canonical entry-state row for each driver row.
  - Canonical row has correct StatusKeyID, NoteText, IsVoided, CompanyID, BranchID.
  - get_day_grid canonical-first read: canonical row takes precedence over DraftLines
    for drivers that have a canonical row; others fall back to DraftLines.
  - Deactivated key bypass: re-submitting the same status code when the key has
    since been deactivated → 200 (no change to status).
  - Voided entry state: IsVoided=TRUE when both StatusKeyID and NoteText are cleared.
  - Finalization snapshot: finalize_period fills StatusCodeSnapshot,
    StatusLabelSnapshot, StatusIsOffReasonSnapshot, FinalizedAtUtc on canonical rows.
  - Locked period display: get_day_grid uses snapshot values (not live lookup) for
    Locked periods.
  - Legacy fallback: periods with no canonical rows read status/note from DraftLines.
  - Legacy canonicalization: finalize_period creates canonical rows for periods that
    had only DraftLines (legacy).
  - Dual-write: save_day_grid writes both DraftLine AND canonical row.
  - Tenant isolation: canonical rows are scoped by CompanyID; cross-company
    rows are not visible.
  - direct add_draft_line / update_draft_line / void_draft_line for DailyStatus and
    DailyNote write / update / clear the canonical row.


Dates: 2096-* — isolated year (2094=CP-2C, 2095=CP-2B).
Run from backend/:
    python -m pytest tests/test_cp2d_canonical_entry_state.py -v
"""
import datetime
import itertools

import pytest
import pytest_asyncio
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncConnection

# ---------------------------------------------------------------------------
# Shared helpers / constants
# ---------------------------------------------------------------------------

_COMPANY_ID = 1
_BASE_MONDAY_2096 = datetime.date(2096, 1, 7)

_CTR = itertools.count(0)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _week_2096(offset: int = 0) -> tuple[datetime.date, datetime.date]:
    n = next(_CTR) + offset
    start = _BASE_MONDAY_2096 + datetime.timedelta(weeks=n)
    return start, start + datetime.timedelta(days=6)


def _sk_code(prefix: str, date: datetime.date) -> str:
    """Build a status-key code safe for the normalizedstatuscode constraint (no hyphens)."""
    return f"{prefix}_{date.strftime('%Y%m%d')}"


async def _clean_branch(db: AsyncConnection, branch_id: int) -> None:
    """
    Remove all periods and related rows for the branch, including locked/finalized ones.
    Disables immutability triggers so locked period records can be deleted.
    Must be called BEFORE deleting any status keys that may be referenced by canonical rows.
    """
    # Disable immutability guard triggers so locked periods can be cleaned up.
    await db.execute(_text(
        "ALTER TABLE payroll.payrollfinallines DISABLE TRIGGER trg_final_line_immutable"
    ))
    await db.execute(_text(
        "ALTER TABLE payroll.payrollperiods DISABLE TRIGGER trg_period_status_revert"
    ))
    # Cancel locked/archived periods before deleting them.
    await db.execute(
        _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' "
              "WHERE branchid = :bid AND status IN ('Locked', 'Archived')"),
        {"bid": branch_id},
    )
    await db.execute(
        _text("DELETE FROM payroll.payrollfinallines "
              "WHERE payrollperiodid IN "
              "(SELECT payrollperiodid FROM payroll.payrollperiods WHERE branchid = :bid)"),
        {"bid": branch_id},
    )
    await db.execute(
        _text("DELETE FROM payroll.payrolldraftlines "
              "WHERE payrollperiodid IN "
              "(SELECT payrollperiodid FROM payroll.payrollperiods WHERE branchid = :bid)"),
        {"bid": branch_id},
    )
    await db.execute(
        _text("DELETE FROM payroll.payrollperiods WHERE branchid = :bid"),
        {"bid": branch_id},
    )
    await db.execute(_text(
        "ALTER TABLE payroll.payrollfinallines ENABLE TRIGGER trg_final_line_immutable"
    ))
    await db.execute(_text(
        "ALTER TABLE payroll.payrollperiods ENABLE TRIGGER trg_period_status_revert"
    ))
    await db.commit()


async def _open_period(
    db: AsyncConnection,
    branch_id: int,
    start: datetime.date,
    end: datetime.date,
    code_suffix: str = "",
) -> int:
    code = f"CP2D-{branch_id}-{start.isoformat()}{code_suffix}"
    r = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": code, "name": f"CP2D {start}", "start": start, "end": end},
    )).mappings().first()
    await db.commit()
    if r is None:
        r = (await db.execute(
            _text("SELECT payrollperiodid FROM payroll.payrollperiods "
                  "WHERE branchid = :bid AND periodcode = :code"),
            {"bid": branch_id, "code": code},
        )).mappings().first()
    assert r is not None, f"Failed to create or find period with code {code!r}"
    return r["payrollperiodid"]


async def _insert_status_key(
    db: AsyncConnection,
    company_id: int,
    branch_id: int,
    code: str,
    *,
    is_off_reason: bool = False,
    is_active: bool = True,
) -> int:
    r = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollstatuskeys
                (companyid, branchid, statuscode, normalizedstatuscode, keyname,
                 isoffreason, hoursvalue, isactive, displayorder)
            VALUES (:cid, :bid, :code, :norm, :name, :off, 8.0, :active, 99)
            RETURNING statuskeyid
        """),
        {
            "cid": company_id, "bid": branch_id,
            "code": code, "norm": code,
            "name": f"Test Key {code}", "off": is_off_reason, "active": is_active,
        },
    )).mappings().first()
    await db.commit()
    return r["statuskeyid"]


async def _canonical_rows(db: AsyncConnection, period_id: int) -> list[dict]:
    rows = (await db.execute(
        _text("""
            SELECT payrollperioddriverdayentrystateid, payrollperiodid,
                   companyid, branchid, driverid, workdate,
                   statuskeyid, notetext, isvoided,
                   statuscodesnapshot, statuslabelsnapshot,
                   statusisoffreasonsnapshot, finalizedatutc
            FROM   payroll.payrollperioddriverdayentrystate
            WHERE  payrollperiodid = :pid
            ORDER BY driverid, workdate
        """),
        {"pid": period_id},
    )).mappings().all()
    return [dict(r) for r in rows]


async def _advance_to_approved(
    client, token: str, period_id: int, driver_id: int, work_date: str,
) -> None:
    headers = _auth(token)
    lines_resp = await client.get(f"/payroll/periods/{period_id}/lines",
                                  headers=headers, params={"status": "Active"})
    if lines_resp.status_code == 200 and len(lines_resp.json()) == 0:
        await client.post(
            f"/payroll/periods/{period_id}/lines",
            headers=headers,
            json={"driver_id": driver_id, "work_date": work_date,
                  "line_type": "DailyNote", "notes": "filler"},
        )
    r = await client.patch(f"/payroll/periods/{period_id}/status",
                           headers=headers, json={"status": "InReview"})
    assert r.status_code == 200, f"InReview failed: {r.text}"
    review_resp = await client.get("/review/items", headers=headers)
    assert review_resp.status_code == 200
    item = next(
        (i for i in review_resp.json()
         if i.get("entity_name") == "PayrollPeriods"
         and i.get("entity_id") == str(period_id)
         and i.get("status") == "Pending"),
        None,
    )
    assert item is not None, f"No pending review item for period {period_id}"
    decide = await client.post(
        f"/review/items/{item['review_item_id']}/decide",
        headers=headers,
        json={"decision": "Approved"},
    )
    assert decide.status_code == 200, f"Approval failed: {decide.text}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="class")
async def ces_branch_id(session_client, auth_token: str) -> int:
    resp = await session_client.get("/core/branches", headers=_auth(auth_token))
    assert resp.status_code == 200
    for b in resp.json():
        if b["branch_code"] == "PAYTEST":
            return b["branch_id"]
    raise AssertionError("PAYTEST branch not found")


@pytest_asyncio.fixture(scope="class")
async def ces_driver_id(session_client, auth_token: str, ces_branch_id: int) -> int:
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id":      ces_branch_id,
            "full_name":      "CES Driver Alpha",
            "preferred_name": "CESA",
            "driver_code":    "CESA-2096",
            "cdl_number":     "CDL-CESA-2096",
            "email":          "cesa2096@example.com",
        },
        headers=_auth(auth_token),
    )
    assert resp.status_code in (200, 201), f"driver seed failed: {resp.text}"
    return resp.json()["driver_id"]


@pytest_asyncio.fixture(scope="class")
async def ces_driver_id_b(session_client, auth_token: str, ces_branch_id: int) -> int:
    """Second driver for per-driver precedence tests."""
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id":      ces_branch_id,
            "full_name":      "CES Driver Beta",
            "preferred_name": "CESB",
            "driver_code":    "CESB-2096",
            "cdl_number":     "CDL-CESB-2096",
            "email":          "cesb2096@example.com",
        },
        headers=_auth(auth_token),
    )
    assert resp.status_code in (200, 201), f"driver B seed failed: {resp.text}"
    return resp.json()["driver_id"]


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestCp2dCanonicalEntryState:

    # ------------------------------------------------------------------ #
    # E01 — Migration schema
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e01_migration_schema(self, direct_db):
        """E01: PayrollPeriodDriverDayEntryState table exists with required columns."""
        r = await direct_db.execute(
            _text("""
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'payroll'
                  AND table_name   = 'payrollperioddriverdayentrystate'
            """)
        )
        assert r.first() is not None, "payroll.PayrollPeriodDriverDayEntryState table missing"

        cols_r = await direct_db.execute(
            _text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'payroll'
                  AND table_name   = 'payrollperioddriverdayentrystate'
            """)
        )
        cols = {row["column_name"] for row in cols_r.mappings().all()}
        required = {
            "payrollperioddriverdayentrystateid", "companyid", "branchid",
            "payrollperiodid", "payrollperioddayid", "workdate", "driverid",
            "statuskeyid", "notetext",
            "statuscodesnapshot", "statuslabelsnapshot",
            "statusisoffreasonsnapshot", "statushoursvaluesnapshot",
            "finalizedatutc", "isvoided",
            "createdbyuserid", "updatedbyuserid", "createdatutc", "updatedatutc",
        }
        missing = required - cols
        assert not missing, f"Missing columns: {sorted(missing)}"

    @pytest.mark.asyncio
    async def test_e01b_unique_constraint(self, direct_db, ces_branch_id: int):
        """E01b: Unique constraint rejects duplicate (PayrollPeriodID, DriverID, WorkDate)."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e01b")
        try:
            shared_params = {
                "cid": _COMPANY_ID, "bid": ces_branch_id,
                "pid": pid, "dt": start, "did": 999991, "uid": 1,
            }
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrollperioddriverdayentrystate
                        (companyid, branchid, payrollperiodid, workdate, driverid,
                         isvoided, createdbyuserid, updatedbyuserid, createdatutc, updatedatutc)
                    VALUES (:cid, :bid, :pid, :dt, :did, FALSE, :uid, :uid, NOW(), NOW())
                """),
                shared_params,
            )
            with pytest.raises(Exception):
                await direct_db.execute(
                    _text("""
                        INSERT INTO payroll.payrollperioddriverdayentrystate
                            (companyid, branchid, payrollperiodid, workdate, driverid,
                             isvoided, createdbyuserid, updatedbyuserid, createdatutc, updatedatutc)
                        VALUES (:cid, :bid, :pid, :dt, :did, FALSE, :uid, :uid, NOW(), NOW())
                    """),
                    shared_params,
                )
        finally:
            await direct_db.rollback()
            await _clean_branch(direct_db, ces_branch_id)

    @pytest.mark.asyncio
    async def test_e01c_indexes_exist(self, direct_db):
        """E01c: Required indexes exist on the canonical table."""
        idx_r = await direct_db.execute(
            _text("""
                SELECT indexname FROM pg_indexes
                WHERE schemaname = 'payroll'
                  AND tablename  = 'payrollperioddriverdayentrystate'
            """)
        )
        indexes = {row["indexname"] for row in idx_r.mappings().all()}
        required_indexes = {
            "ix_ppdes_period_date", "ix_ppdes_period_driver",
            "ix_ppdes_company_branch", "ix_ppdes_statuskey", "ix_ppdes_period_finalized",
        }
        missing = required_indexes - {i.lower() for i in indexes}
        assert not missing, f"Missing indexes: {sorted(missing)}"

    # ------------------------------------------------------------------ #
    # E02 — Live dropdown (not snapshotted at period creation)
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e02_live_status_key_dropdown(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_id: int,
    ):
        """E02: A Status Key added AFTER period creation appears in get_day_grid."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e02")
        new_code = _sk_code("LIVE2096", start)
        try:
            headers = _auth(auth_token)
            wdate = str(start)

            # Confirm new_code not in dropdown before insert
            r1 = await session_client.get(
                f"/payroll/periods/{pid}/day-grid",
                params={"work_date": wdate},
                headers=headers,
            )
            assert r1.status_code == 200
            before_codes = {sk["key_code"] for sk in r1.json().get("status_keys", [])}
            assert new_code not in before_codes, "Key should not exist yet"

            # Insert the new key
            await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, new_code)

            # Dropdown now includes new key — no period reopen needed
            r2 = await session_client.get(
                f"/payroll/periods/{pid}/day-grid",
                params={"work_date": wdate},
                headers=headers,
            )
            assert r2.status_code == 200
            after_codes = {sk["key_code"] for sk in r2.json().get("status_keys", [])}
            assert new_code in after_codes, "Newly-added Status Key must appear in dropdown"
        finally:
            await direct_db.execute(
                _text("DELETE FROM payroll.payrollstatuskeys "
                      "WHERE statuscode = :code AND branchid = :bid"),
                {"code": new_code, "bid": ces_branch_id},
            )
            await direct_db.commit()
            await _clean_branch(direct_db, ces_branch_id)

    # ------------------------------------------------------------------ #
    # E03 — Canonical storage via save_day_grid
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e03_save_day_grid_creates_canonical_row(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_id: int,
    ):
        """E03: save_day_grid writes a canonical entry-state row with correct fields."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e03")
        code = _sk_code("E03KEY", start)
        try:
            sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)
            wdate = str(start)
            headers = _auth(auth_token)

            r = await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={
                    "work_date": wdate,
                    "rows": [{"driver_id": ces_driver_id, "values": {},
                               "status_key": code, "notes": "Test note"}],
                },
            )
            assert r.status_code == 200, f"save_day_grid failed: {r.text}"

            # Verify canonical row in DB
            rows = await _canonical_rows(direct_db, pid)
            ces = [row for row in rows if row["driverid"] == ces_driver_id]
            assert len(ces) == 1, f"Expected 1 canonical row, got {len(ces)}"
            assert ces[0]["statuskeyid"] == sk_id
            assert ces[0]["notetext"] == "Test note"
            assert ces[0]["isvoided"] is False
            assert ces[0]["companyid"] == _COMPANY_ID
            assert ces[0]["branchid"] == ces_branch_id
            assert ces[0]["finalizedatutc"] is None, "Should not be finalized yet"

            # Also verify DraftLine still written (dual-write)
            dl = (await direct_db.execute(
                _text("""
                    SELECT notes FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :pid AND driverid = :did
                      AND linetype = 'DailyStatus' AND status != 'Void'
                    LIMIT 1
                """),
                {"pid": pid, "did": ces_driver_id},
            )).mappings().first()
            assert dl is not None, "DraftLine must also be written (dual-write)"
            assert dl["notes"] == code
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id is not None:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E04 — Clearing sets IsVoided=TRUE when both fields empty
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e04_clear_both_sets_isvoided(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_id: int,
    ):
        """E04: Clearing both status and note sets IsVoided=TRUE on canonical row."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e04")
        code = _sk_code("E04KEY", start)
        sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)
        wdate = str(start)
        headers = _auth(auth_token)
        try:
            # Set status + note
            await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_id": ces_driver_id, "values": {},
                                 "status_key": code, "notes": "A note"}]},
            )
            # Clear both
            r = await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_id": ces_driver_id, "values": {},
                                 "status_key": None, "notes": None}]},
            )
            assert r.status_code == 200

            rows = await _canonical_rows(direct_db, pid)
            ces = [row for row in rows if row["driverid"] == ces_driver_id]
            assert len(ces) == 1
            assert ces[0]["isvoided"] is True
            assert ces[0]["statuskeyid"] is None
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id is not None:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E05 — get_day_grid canonical-first read (editable period)
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e05_get_day_grid_canonical_first_read(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_id: int,
    ):
        """E05: get_day_grid returns canonical row values when canonical row exists."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e05")
        code = _sk_code("E05KEY", start)
        sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)
        wdate = str(start)
        headers = _auth(auth_token)
        try:
            await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_id": ces_driver_id, "values": {},
                                 "status_key": code, "notes": "E05 note"}]},
            )
            r = await session_client.get(
                f"/payroll/periods/{pid}/day-grid",
                params={"work_date": wdate},
                headers=headers,
            )
            assert r.status_code == 200
            data = r.json()
            drv_row = next(
                (d for d in data["rows"] if d["driver_id"] == ces_driver_id), None
            )
            assert drv_row is not None
            assert drv_row["status_key"] == code
            assert drv_row["notes"] == "E05 note"
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id is not None:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E06 — Legacy fallback (no canonical rows → read from DraftLines)
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e06_legacy_fallback_reads_from_draftlines(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_id: int,
    ):
        """E06: Period with no canonical rows reads status/note from DraftLines."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e06")
        code = _sk_code("E06KEY", start)
        sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)
        wdate = str(start)
        headers = _auth(auth_token)
        try:
            # Insert DraftLine directly — bypasses canonical write
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverid,
                         workdate, linetype, linescope, quantity,
                         sourcetype, status, needsmanagerreview, notes, addedbyuserid)
                    VALUES (1, :bid, :pid, :did, :dt,
                            'DailyStatus', 'Daily', 0,
                            'Manual', 'Active', FALSE, :code, 1)
                """),
                {"bid": ces_branch_id, "pid": pid, "did": ces_driver_id, "dt": start, "code": code},
            )
            await direct_db.commit()

            # No canonical rows should exist
            rows = await _canonical_rows(direct_db, pid)
            assert not rows, "Test setup error: canonical rows should not exist"

            # get_day_grid must fall back to DraftLine and return the status key code
            r = await session_client.get(
                f"/payroll/periods/{pid}/day-grid",
                params={"work_date": wdate},
                headers=headers,
            )
            assert r.status_code == 200
            drv_row = next(
                (d for d in r.json()["rows"] if d["driver_id"] == ces_driver_id), None
            )
            assert drv_row is not None
            assert drv_row["status_key"] == code, "Legacy fallback must return DraftLine status"
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id is not None:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E07 — Per-driver canonical-first: only drivers WITH canonical row use it
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e07_per_driver_canonical_precedence(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_id: int,
        ces_driver_id_b: int,
    ):
        """E07: Driver A has canonical row; Driver B has only DraftLine.
        get_day_grid uses canonical for A and DraftLine fallback for B."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e07")
        code_a = _sk_code("E07AKEY", start)
        code_b = _sk_code("E07BKEY", start)
        sk_a = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code_a)
        sk_b = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code_b)
        wdate = str(start)
        headers = _auth(auth_token)
        try:
            # Driver A: write via save_day_grid (creates canonical row)
            await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_id": ces_driver_id, "values": {}, "status_key": code_a}]},
            )

            # Driver B: write DraftLine directly (no canonical row)
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverid,
                         workdate, linetype, linescope, quantity,
                         sourcetype, status, needsmanagerreview, notes, addedbyuserid)
                    VALUES (1, :bid, :pid, :did, :dt,
                            'DailyStatus', 'Daily', 0, 'Manual', 'Active', FALSE, :code, 1)
                """),
                {"bid": ces_branch_id, "pid": pid, "did": ces_driver_id_b, "dt": start, "code": code_b},
            )
            await direct_db.commit()

            # get_day_grid: Driver A from canonical, Driver B from DraftLine
            r = await session_client.get(
                f"/payroll/periods/{pid}/day-grid",
                params={"work_date": wdate},
                headers=headers,
            )
            assert r.status_code == 200
            drv_rows = {d["driver_id"]: d for d in r.json()["rows"]}

            assert drv_rows[ces_driver_id]["status_key"] == code_a, "A: canonical row"
            assert drv_rows[ces_driver_id_b]["status_key"] == code_b, "B: DraftLine fallback"
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            for sid in (sk_a, sk_b):
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sid},
                )
            await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E08 — Deactivated key: bypass active-only check on unchanged re-submit
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e08_deactivated_key_bypass_unchanged(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_id: int,
    ):
        """E08: Re-submitting the same code after the key is deactivated → 200."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e08")
        code = _sk_code("E08DEACT", start)
        wdate = str(start)
        headers = _auth(auth_token)
        sk_id = None
        try:
            sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)

            # Set the status while key is active
            r1 = await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_id": ces_driver_id, "values": {}, "status_key": code}]},
            )
            assert r1.status_code == 200, f"initial save failed: {r1.text}"

            # Deactivate the key
            await direct_db.execute(
                _text("UPDATE payroll.payrollstatuskeys SET isactive = FALSE WHERE statuskeyid = :sid"),
                {"sid": sk_id},
            )
            await direct_db.commit()

            # Re-submit the same code (unchanged) → must succeed (bypass active-only check)
            r2 = await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_id": ces_driver_id, "values": {}, "status_key": code}]},
            )
            assert r2.status_code == 200, f"Re-submit of deactivated unchanged key failed: {r2.text}"

            # Submitting a DIFFERENT code while key is inactive → 422
            r3 = await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_id": ces_driver_id, "values": {},
                                 "status_key": code + "_NEW"}]},
            )
            assert r3.status_code == 422, "A new deactivated code must be rejected"
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E09 — Finalization snapshot
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e09_finalization_snapshot(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_id: int,
    ):
        """E09: finalize_period fills snapshot columns on canonical rows."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e09")
        code = _sk_code("E09SNAP", start)
        wdate = str(start)
        headers = _auth(auth_token)
        sk_id = None
        try:
            sk_id = await _insert_status_key(
                direct_db, _COMPANY_ID, ces_branch_id, code, is_off_reason=True
            )

            # Save status + note
            await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_id": ces_driver_id, "values": {},
                                 "status_key": code, "notes": "snap note"}]},
            )

            # Verify snapshot fields are NULL before finalization
            rows_pre = await _canonical_rows(direct_db, pid)
            ces_pre = next(r for r in rows_pre if r["driverid"] == ces_driver_id)
            assert ces_pre["finalizedatutc"] is None
            assert ces_pre["statuscodesnapshot"] is None

            # Advance to Approved then finalize
            await _advance_to_approved(session_client, auth_token, pid, ces_driver_id, wdate)
            fin = await session_client.post(
                f"/payroll/periods/{pid}/finalize",
                headers=headers,
            )
            assert fin.status_code == 200, f"finalize failed: {fin.text}"

            # Snapshot fields must now be filled
            rows_post = await _canonical_rows(direct_db, pid)
            ces_post = next(r for r in rows_post if r["driverid"] == ces_driver_id)
            assert ces_post["finalizedatutc"] is not None, "FinalizedAtUtc must be set"
            assert ces_post["statuscodesnapshot"] == code
            assert "E09" in (ces_post["statuslabelsnapshot"] or "")
            assert ces_post["statusisoffreasonsnapshot"] is True
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E10 — Locked period: get_day_grid uses snapshot values
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e10_locked_period_uses_snapshot(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_id: int,
    ):
        """E10: get_day_grid returns snapshot label/code for Locked periods."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e10")
        code = _sk_code("E10LOCK", start)
        wdate = str(start)
        headers = _auth(auth_token)
        sk_id = None
        try:
            sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)

            await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_id": ces_driver_id, "values": {}, "status_key": code}]},
            )

            await _advance_to_approved(session_client, auth_token, pid, ces_driver_id, wdate)
            fin = await session_client.post(f"/payroll/periods/{pid}/finalize", headers=headers)
            assert fin.status_code == 200

            # get_day_grid on Locked period must return the status code from snapshot
            r = await session_client.get(
                f"/payroll/periods/{pid}/day-grid",
                params={"work_date": wdate},
                headers=headers,
            )
            assert r.status_code == 200
            drv_row = next(
                (d for d in r.json()["rows"] if d["driver_id"] == ces_driver_id), None
            )
            assert drv_row is not None
            assert drv_row["status_key"] == code, "Locked period must show snapshot status code"
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E11 — Legacy canonicalization at finalization
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e11_finalization_canonicalizes_legacy_draftlines(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_id: int,
    ):
        """E11: finalize_period creates canonical rows for legacy periods (DraftLine-only)."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e11")
        code = _sk_code("E11LEG", start)
        wdate = str(start)
        headers = _auth(auth_token)
        sk_id = None
        try:
            sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)

            # Insert DraftLine directly — bypasses canonical write
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverid,
                         workdate, linetype, linescope, quantity,
                         sourcetype, status, needsmanagerreview, notes, addedbyuserid)
                    VALUES (1, :bid, :pid, :did, :dt,
                            'DailyStatus', 'Daily', 0, 'Manual', 'Active', FALSE, :code, 1)
                """),
                {"bid": ces_branch_id, "pid": pid, "did": ces_driver_id, "dt": start, "code": code},
            )
            # Also add a DailyNote so finalization won't reject for empty period
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverid,
                         workdate, linetype, linescope, quantity,
                         sourcetype, status, needsmanagerreview, addedbyuserid)
                    VALUES (1, :bid, :pid, :did, :dt,
                            'DailyNote', 'Daily', 1, 'Manual', 'Active', FALSE, 1)
                """),
                {"bid": ces_branch_id, "pid": pid, "did": ces_driver_id, "dt": start},
            )
            await direct_db.commit()

            # No canonical row should exist yet
            assert not await _canonical_rows(direct_db, pid)

            await _advance_to_approved(session_client, auth_token, pid, ces_driver_id, wdate)
            fin = await session_client.post(f"/payroll/periods/{pid}/finalize", headers=headers)
            assert fin.status_code == 200

            # finalize_period must have created a canonical row from the DraftLine
            rows = await _canonical_rows(direct_db, pid)
            ces = [r for r in rows if r["driverid"] == ces_driver_id]
            assert ces, "Finalization must create canonical row from legacy DraftLine"
            assert ces[0]["statuscodesnapshot"] == code, "Snapshot must use DraftLine status code"
            assert ces[0]["finalizedatutc"] is not None
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E12 — Direct DraftLine API: add_draft_line writes canonical row
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e12_add_draft_line_writes_canonical_status(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_id: int,
    ):
        """E12: Adding a DailyStatus DraftLine via direct API also writes canonical row."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e12")
        code = _sk_code("E12KEY", start)
        wdate = str(start)
        headers = _auth(auth_token)
        sk_id = None
        try:
            sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)

            r = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                headers=headers,
                json={
                    "driver_id": ces_driver_id,
                    "work_date": wdate,
                    "line_type": "DailyStatus",
                    "quantity": "0",
                    "notes": code,
                },
            )
            assert r.status_code in (200, 201), f"add_draft_line failed: {r.text}"

            rows = await _canonical_rows(direct_db, pid)
            ces = [row for row in rows if row["driverid"] == ces_driver_id]
            assert len(ces) == 1, "Canonical row must be created by add_draft_line"
            assert ces[0]["statuskeyid"] == sk_id
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E13 — ON DELETE CASCADE: deleting period removes canonical rows
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e13_cascade_delete(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_id: int,
    ):
        """E13: Deleting a payroll period removes its canonical entry-state rows."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e13")
        code = _sk_code("E13KEY", start)
        sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)
        wdate = str(start)
        headers = _auth(auth_token)
        try:
            await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_id": ces_driver_id, "values": {}, "status_key": code}]},
            )
            assert await _canonical_rows(direct_db, pid), "Row must exist before delete"

            await direct_db.execute(
                _text("DELETE FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid"),
                {"pid": pid},
            )
            await direct_db.execute(
                _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": pid},
            )
            await direct_db.commit()

            rows_after = await _canonical_rows(direct_db, pid)
            assert not rows_after, "Canonical rows must be removed via CASCADE"
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            await direct_db.execute(
                _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                {"sid": sk_id},
            )
            await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E14 — Tenant isolation: CompanyID scoping
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e14_canonical_row_company_id_scoped(
        self,
        direct_db,
        ces_branch_id: int,
    ):
        """E14: Canonical rows carry the period's CompanyID — cross-company rows absent."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e14")
        try:
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrollperioddriverdayentrystate
                        (companyid, branchid, payrollperiodid, workdate, driverid,
                         isvoided, createdbyuserid, updatedbyuserid, createdatutc, updatedatutc)
                    VALUES (1, :bid, :pid, :dt, 999992, FALSE, 1, 1, NOW(), NOW())
                """),
                {"bid": ces_branch_id, "pid": pid, "dt": start},
            )
            await direct_db.commit()

            # Row is scoped to company 1
            rows = (await direct_db.execute(
                _text("SELECT companyid FROM payroll.payrollperioddriverdayentrystate "
                      "WHERE payrollperiodid = :pid AND driverid = 999992"),
                {"pid": pid},
            )).mappings().all()
            assert all(r["companyid"] == 1 for r in rows)

            # Company 2 sees zero rows for this period
            rows_c2 = (await direct_db.execute(
                _text("SELECT 1 FROM payroll.payrollperioddriverdayentrystate "
                      "WHERE payrollperiodid = :pid AND companyid = 2"),
                {"pid": pid},
            )).all()
            assert not rows_c2, "Cross-company canonical rows must not exist"
        finally:
            await _clean_branch(direct_db, ces_branch_id)

    # ------------------------------------------------------------------ #
    # E16 — Direct add_draft_line: invalid status code → 422, no mutations
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e16_direct_add_dailystatus_invalid_code_rejected(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_id: int,
    ):
        """E16: add_draft_line with DailyStatus and a non-existent code → 422.
        No DraftLine and no canonical row must be created."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e16")
        wdate = str(start)
        headers = _auth(auth_token)
        try:
            r = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                headers=headers,
                json={
                    "driver_id": ces_driver_id,
                    "work_date": wdate,
                    "line_type": "DailyStatus",
                    "quantity": "0",
                    "notes": "NONEXISTENT_CODE_E16",
                },
            )
            assert r.status_code == 422, f"Expected 422 for invalid status code; got {r.status_code}"

            # No DraftLine should have been inserted
            dl_count = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :pid AND driverid = :did AND linetype = 'DailyStatus'
                """),
                {"pid": pid, "did": ces_driver_id},
            )).scalar()
            assert dl_count == 0, "No DraftLine must be inserted on validation failure"

            # No canonical row should exist
            assert not await _canonical_rows(direct_db, pid), \
                "No canonical row must be created on validation failure"
        finally:
            await _clean_branch(direct_db, ces_branch_id)

    # ------------------------------------------------------------------ #
    # E17 — Direct add_draft_line: inactive status code → 422, no mutations
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e17_direct_add_dailystatus_inactive_code_rejected(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_id: int,
    ):
        """E17: add_draft_line with DailyStatus and an inactive code → 422.
        No DraftLine and no canonical row must be created."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e17")
        code = _sk_code("E17INACT", start)
        wdate = str(start)
        headers = _auth(auth_token)
        sk_id = None
        try:
            sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)
            # Deactivate immediately
            await direct_db.execute(
                _text("UPDATE payroll.payrollstatuskeys SET isactive = FALSE WHERE statuskeyid = :sid"),
                {"sid": sk_id},
            )
            await direct_db.commit()

            r = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                headers=headers,
                json={
                    "driver_id": ces_driver_id,
                    "work_date": wdate,
                    "line_type": "DailyStatus",
                    "quantity": "0",
                    "notes": code,
                },
            )
            assert r.status_code == 422, f"Expected 422 for inactive status code; got {r.status_code}"

            dl_count = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :pid AND driverid = :did AND linetype = 'DailyStatus'
                """),
                {"pid": pid, "did": ces_driver_id},
            )).scalar()
            assert dl_count == 0, "No DraftLine must be inserted for inactive code"
            assert not await _canonical_rows(direct_db, pid), \
                "No canonical row must be created for inactive code"
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id is not None:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E18 — Direct add_draft_line: blank notes for DailyStatus → 422
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e18_direct_add_dailystatus_blank_notes_rejected(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_id: int,
    ):
        """E18: add_draft_line with DailyStatus and missing/blank notes → 422."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e18")
        wdate = str(start)
        headers = _auth(auth_token)
        try:
            r = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                headers=headers,
                json={
                    "driver_id": ces_driver_id,
                    "work_date": wdate,
                    "line_type": "DailyStatus",
                    "quantity": "0",
                },
            )
            assert r.status_code == 422, f"Expected 422 for blank notes; got {r.status_code}"

            dl_count = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :pid AND driverid = :did AND linetype = 'DailyStatus'
                """),
                {"pid": pid, "did": ces_driver_id},
            )).scalar()
            assert dl_count == 0, "No DraftLine must be inserted for blank notes"
        finally:
            await _clean_branch(direct_db, ces_branch_id)

    # ------------------------------------------------------------------ #
    # E19 — Direct update_draft_line: invalid code → 422, state unchanged
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e19_direct_update_dailystatus_invalid_code_rejected(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_id: int,
    ):
        """E19: update_draft_line changing DailyStatus to invalid code → 422.
        Existing DraftLine and canonical row must remain unchanged."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e19")
        code = _sk_code("E19VALID", start)
        wdate = str(start)
        headers = _auth(auth_token)
        sk_id = None
        try:
            sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)

            # Add a valid DailyStatus line first
            add_r = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                headers=headers,
                json={
                    "driver_id": ces_driver_id,
                    "work_date": wdate,
                    "line_type": "DailyStatus",
                    "quantity": "0",
                    "notes": code,
                },
            )
            assert add_r.status_code in (200, 201), f"add failed: {add_r.text}"
            line_id = add_r.json()["draft_line_id"]

            # Canonical row must exist with the correct statuskeyid
            rows_before = await _canonical_rows(direct_db, pid)
            assert rows_before, "Canonical row must exist after valid add"
            canonical_sk_id_before = rows_before[0]["statuskeyid"]
            assert canonical_sk_id_before == sk_id

            # Attempt update to invalid code
            upd_r = await session_client.patch(
                f"/payroll/periods/{pid}/lines/{line_id}",
                headers=headers,
                json={"notes": "NONEXISTENT_E19"},
            )
            assert upd_r.status_code == 422, f"Expected 422 for invalid update; got {upd_r.status_code}"

            # DraftLine notes must still be the original code
            dl = (await direct_db.execute(
                _text("SELECT notes FROM payroll.payrolldraftlines WHERE draftlineid = :lid"),
                {"lid": line_id},
            )).mappings().first()
            assert dl["notes"] == code, "DraftLine notes must not change on failed update"

            # Canonical row must still hold the original statuskeyid
            rows_after = await _canonical_rows(direct_db, pid)
            assert rows_after[0]["statuskeyid"] == sk_id, \
                "Canonical statuskeyid must not change on failed update"
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id is not None:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E20 — Direct update_draft_line: inactive code → 422 (no bypass on direct path)
    # E20b — Same code after deactivation (unchanged resave) → also 422
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e20_direct_update_dailystatus_inactive_rejected_no_bypass(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_id: int,
    ):
        """E20: update_draft_line to inactive code → 422; even unchanged resave → 422.

        Policy (documented): direct update_draft_line with DailyStatus always
        requires an active StatusKey regardless of whether the submitted code
        matches the existing selection. The deactivated-bypass is only available
        via save_day_grid (which has explicit unchanged-resave detection).
        """
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e20")
        code = _sk_code("E20DEACT", start)
        wdate = str(start)
        headers = _auth(auth_token)
        sk_id = None
        try:
            sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)

            # Add a valid DailyStatus line while key is active
            add_r = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                headers=headers,
                json={
                    "driver_id": ces_driver_id,
                    "work_date": wdate,
                    "line_type": "DailyStatus",
                    "quantity": "0",
                    "notes": code,
                },
            )
            assert add_r.status_code in (200, 201), f"add failed: {add_r.text}"
            line_id = add_r.json()["draft_line_id"]

            # Deactivate the key
            await direct_db.execute(
                _text("UPDATE payroll.payrollstatuskeys SET isactive = FALSE WHERE statuskeyid = :sid"),
                {"sid": sk_id},
            )
            await direct_db.commit()

            # Update to a DIFFERENT inactive code → 422
            upd_r1 = await session_client.patch(
                f"/payroll/periods/{pid}/lines/{line_id}",
                headers=headers,
                json={"notes": code},  # same code but now inactive
            )
            assert upd_r1.status_code == 422, \
                "Unchanged-but-deactivated code must be rejected on direct update path"

            # DraftLine unchanged
            dl = (await direct_db.execute(
                _text("SELECT notes FROM payroll.payrolldraftlines WHERE draftlineid = :lid"),
                {"lid": line_id},
            )).mappings().first()
            assert dl["notes"] == code, "DraftLine must not change on failed update"
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id is not None:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E21 — Direct DailyNote add / update / void dual-writes canonical NoteText
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e21_direct_dailynote_dual_writes_canonical(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_id: int,
    ):
        """E21: add/update/void of DailyNote via direct API correctly dual-writes
        canonical NoteText.  No StatusKey validation is required for DailyNote."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e21")
        wdate = str(start)
        headers = _auth(auth_token)
        try:
            # --- Add ---
            add_r = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                headers=headers,
                json={
                    "driver_id": ces_driver_id,
                    "work_date": wdate,
                    "line_type": "DailyNote",
                    "quantity": "0",
                    "notes": "e21 initial note",
                },
            )
            assert add_r.status_code in (200, 201), f"add failed: {add_r.text}"
            line_id = add_r.json()["draft_line_id"]

            rows = await _canonical_rows(direct_db, pid)
            ces = [r for r in rows if r["driverid"] == ces_driver_id]
            assert ces, "Canonical row must be created by DailyNote add"
            assert ces[0]["notetext"] == "e21 initial note", "NoteText must match"
            assert ces[0]["statuskeyid"] is None, "StatusKeyID must remain NULL for DailyNote"

            # --- Update ---
            upd_r = await session_client.patch(
                f"/payroll/periods/{pid}/lines/{line_id}",
                headers=headers,
                json={"notes": "e21 updated note"},
            )
            assert upd_r.status_code == 200, f"update failed: {upd_r.text}"

            rows2 = await _canonical_rows(direct_db, pid)
            ces2 = [r for r in rows2 if r["driverid"] == ces_driver_id]
            assert ces2[0]["notetext"] == "e21 updated note", "NoteText must be updated"

            # --- Void ---
            void_r = await session_client.delete(
                f"/payroll/periods/{pid}/lines/{line_id}",
                headers=headers,
            )
            assert void_r.status_code in (200, 204), f"void failed: {void_r.text}"

            # Canonical row should be soft-voided (isvoided=TRUE) or notetext cleared
            rows3 = (await direct_db.execute(
                _text("""
                    SELECT isvoided, notetext, statuskeyid
                    FROM payroll.payrollperioddriverdayentrystate
                    WHERE payrollperiodid = :pid AND driverid = :did
                    LIMIT 1
                """),
                {"pid": pid, "did": ces_driver_id},
            )).mappings().first()
            assert rows3 is not None, "Canonical row must still exist (soft-void)"
            # After voiding the only informational line, notetext must be cleared
            # and (since statuskeyid was already NULL) the row must be soft-voided
            assert rows3["notetext"] is None, "NoteText must be cleared after void"
            assert rows3["isvoided"] is True, "Row must be soft-voided when both fields NULL"
        finally:
            await _clean_branch(direct_db, ces_branch_id)
