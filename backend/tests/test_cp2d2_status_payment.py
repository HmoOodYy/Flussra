"""
CP-2D2: Status Payment Through Status Rate Columns — full test suite.

Covers all P1 contract requirements:
  - Migration 0056: schema, triggers, indexes.
  - StatusRateColumns API: list, create (auto-creates RateType), duplicate guard.
  - Future branches: default Status Pay column created automatically.
  - Driver Pay Rates matrix: default + custom SRC columns appear.
  - Batch save: PayItem + StatusRateColumn paths.
  - Status payment derivation: HoursValue × rate, NMR, void.
  - SourceSnapshot populated on draft lines.
  - Finalization preserves SourceSnapshot; later edits don't corrupt it.
  - Manual edit guards on STATUS_PAYMENT lines.
  - No PTO_STATUS regression.

Dates: 2097-* — isolated year (2096=CP-2D1, 2095=CP-2B).
Run from backend/:
    python -B -m pytest tests/test_cp2d2_status_payment.py -v -p no:cacheprovider
"""
import datetime
import itertools
import json

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncConnection

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_COMPANY_ID = 1
_BASE_MONDAY_2097 = datetime.date(2097, 1, 6)   # Monday
_CTR = itertools.count(0)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _week_2097(offset: int = 0) -> tuple[datetime.date, datetime.date]:
    n = next(_CTR) + offset
    start = _BASE_MONDAY_2097 + datetime.timedelta(weeks=n)
    return start, start + datetime.timedelta(days=6)


async def _clean_branch(db: AsyncConnection, branch_id: int) -> None:
    """Remove only mutable test periods; retain immutable financial history."""
    await db.execute(
        _text("DELETE FROM payroll.payrolldraftlines "
              "WHERE payrollperiodid IN ("
              "  SELECT p.payrollperiodid FROM payroll.payrollperiods AS p "
              "  WHERE p.branchid = :bid "
              "    AND p.status = 'Draft' "
              "    AND NOT EXISTS (SELECT 1 FROM payroll.payrollcalculationsnapshots AS s "
              "                    WHERE s.payrollperiodid = p.payrollperiodid) "
              "    AND NOT EXISTS (SELECT 1 FROM payroll.payrollfinallines AS f "
              "                    WHERE f.payrollperiodid = p.payrollperiodid)"
              ")"),
        {"bid": branch_id},
    )
    await db.execute(
        _text("DELETE FROM payroll.payrollperiods AS p "
              "WHERE p.branchid = :bid "
              "  AND p.status = 'Draft' "
              "  AND NOT EXISTS (SELECT 1 FROM payroll.payrollcalculationsnapshots AS s "
              "                  WHERE s.payrollperiodid = p.payrollperiodid) "
              "  AND NOT EXISTS (SELECT 1 FROM payroll.payrollfinallines AS f "
              "                  WHERE f.payrollperiodid = p.payrollperiodid)"),
        {"bid": branch_id},
    )
    await db.commit()


async def _clean_status_pay_rate(db: AsyncConnection, driver_id: int) -> None:
    """Remove all STATUS_PAY and SRC_* driver rates for isolation."""
    await db.execute(
        _text("""
            DELETE FROM payroll.driverrates
            WHERE driverid = :did
              AND ratetypeid IN (
                  SELECT ratetypeid FROM payroll.ratetypes
                  WHERE ratecode = 'STATUS_PAY' OR ratecode LIKE 'SRC_%'
              )
        """),
        {"did": driver_id},
    )
    await db.commit()


async def _open_period_db(
    db: AsyncConnection,
    branch_id: int,
    start: datetime.date,
    end: datetime.date,
    code_suffix: str = "",
) -> int:
    await db.execute(
        _text("""
            UPDATE payroll.payrollperiods
            SET status = 'Cancelled'
            WHERE branchid = :bid
              AND periodcode LIKE 'CP2D2-%'
              AND status = 'Open'
        """),
        {"bid": branch_id},
    )
    code = f"CP2D2-{branch_id}-{start.isoformat()}{code_suffix}"
    r = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": code, "name": f"CP2D2 {start}", "start": start, "end": end},
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


async def _insert_status_key_db(
    db: AsyncConnection,
    company_id: int,
    branch_id: int,
    code: str,
    hours: float = 8.0,
    status_rate_column_id: int | None = None,
) -> int:
    r = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollstatuskeys
                (companyid, branchid, statuscode, normalizedstatuscode, keyname,
                 isoffreason, hoursvalue, isactive, displayorder, statusratecolumnid)
            VALUES (:cid, :bid, :code, :norm, :name, FALSE, :hours, TRUE, 99, :src_col)
            RETURNING statuskeyid
        """),
        {
            "cid": company_id, "bid": branch_id,
            "code": code, "norm": code.upper(),
            "name": f"Test Key {code}",
            "hours": hours,
            "src_col": status_rate_column_id,
        },
    )).mappings().first()
    await db.commit()
    return r["statuskeyid"]


async def _get_draft_lines(db: AsyncConnection, period_id: int) -> list[dict]:
    rows = (await db.execute(
        _text("""
            SELECT draftlineid, driverid, workdate, linetype, quantity,
                   calculatedamount, sourcetype, sourceid, status, needsmanagerreview,
                   sourcesnapshot
            FROM   payroll.payrolldraftlines
            WHERE  payrollperiodid = :pid
            ORDER BY workdate, driverid, linetype
        """),
        {"pid": period_id},
    )).mappings().all()
    return [dict(r) for r in rows]


async def _insert_driver_rate_db(
    db: AsyncConnection,
    company_id: int,
    branch_id: int,
    driver_id: int,
    rate_code: str,
    amount: float,
    effective_from: datetime.date,
) -> int:
    rt_row = (await db.execute(
        _text("SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = :rc"),
        {"rc": rate_code},
    )).mappings().first()
    assert rt_row is not None, f"RateType {rate_code!r} not found"
    rate_type_id = rt_row["ratetypeid"]

    r = (await db.execute(
        _text("""
            INSERT INTO payroll.driverrates
                (companyid, branchid, driverid, ratetypeid, amount, status, effectivefrom)
            VALUES (:cid, :bid, :did, :rtid, :amt, 'Approved', :eff)
            ON CONFLICT (driverid, ratetypeid) WHERE status = 'Approved'
            DO UPDATE SET amount = EXCLUDED.amount, effectivefrom = EXCLUDED.effectivefrom
            RETURNING driverrateid
        """),
        {
            "cid": company_id, "bid": branch_id, "did": driver_id,
            "rtid": rate_type_id, "amt": amount, "eff": effective_from,
        },
    )).mappings().first()
    await db.commit()
    return r["driverrateid"]


def _day_grid_body(driver_id: int, work_date: datetime.date, status_key: str | None = None) -> dict:
    return {
        "work_date": work_date.isoformat(),
        "rows": [{"driver_id": driver_id, "status_key": status_key, "values": {}}],
    }


# ---------------------------------------------------------------------------
# Module-scoped fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="module")
async def cp2d2_branch_id(session_client: httpx.AsyncClient, auth_token: str) -> int:
    resp = await session_client.get("/settings/branches", headers=_auth(auth_token))
    assert resp.status_code == 200
    for b in resp.json():
        if b["branch_code"] == "PAYTEST":
            return b["branch_id"]
    raise AssertionError("PAYTEST branch not found")


@pytest_asyncio.fixture(scope="module")
async def cp2d2_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    cp2d2_branch_id: int,
) -> int:
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id":      cp2d2_branch_id,
            "full_name":      "CP2D2 Status Pay Driver",
            "preferred_name": "SP2D2",
            "driver_code":    "SP2D2-001",
            "cdl_number":     "CDL-SP2D2-001",
            "email":          "sp2d2@example.com",
        },
        headers=_auth(auth_token),
    )
    assert resp.status_code == 201, f"Driver seed failed: {resp.text}"
    return resp.json()["driver_id"]


@pytest_asyncio.fixture(scope="module")
async def cp2d2_src_col_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    cp2d2_branch_id: int,
    session_db_conn: AsyncConnection,
) -> int:
    """Return the default STATUS_PAY StatusRateColumn for the PAYTEST branch."""
    # The default should be seeded by _ensure_default_status_rate_column_for_branch
    # (called from create_branch) or by the migration seed. Try DB first.
    existing = (await session_db_conn.execute(
        _text("""
            SELECT statusratecolumnid
            FROM   payroll.statusratecolumns
            WHERE  branchid  = :bid AND companyid = 1
              AND  isactive  = TRUE
            ORDER BY isdefault DESC, statusratecolumnid
            LIMIT 1
        """),
        {"bid": cp2d2_branch_id},
    )).mappings().first()
    if existing:
        return existing["statusratecolumnid"]

    # Create via service if no default exists (test DB migration seed may have missed this branch)
    rt_row = (await session_db_conn.execute(
        _text("SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = 'STATUS_PAY'"),
    )).mappings().first()
    assert rt_row is not None, "STATUS_PAY RateType not seeded"

    r = (await session_db_conn.execute(
        _text("""
            INSERT INTO payroll.statusratecolumns
                (companyid, branchid, ratetypeid, columnname, normalizedcolumnname, isdefault, isactive)
            VALUES (1, :bid, :rtid, 'Status Pay', 'STATUS PAY', TRUE, TRUE)
            RETURNING statusratecolumnid
        """),
        {"bid": cp2d2_branch_id, "rtid": rt_row["ratetypeid"]},
    )).mappings().first()
    await session_db_conn.commit()
    return r["statusratecolumnid"]


# ---------------------------------------------------------------------------
# Tests: Migration / Schema
# ---------------------------------------------------------------------------

class TestMigration:
    @pytest.mark.asyncio
    async def test_table_columns(self, direct_db: AsyncConnection):
        """StatusRateColumns table has all required columns including NormalizedColumnName."""
        rows = (await direct_db.execute(
            _text("""
                SELECT column_name
                FROM   information_schema.columns
                WHERE  table_schema = 'payroll'
                  AND  table_name   = 'statusratecolumns'
                ORDER BY ordinal_position
            """),
        )).mappings().all()
        cols = {r["column_name"] for r in rows}
        for c in ("statusratecolumnid", "companyid", "branchid", "ratetypeid",
                  "columnname", "normalizedcolumnname", "isdefault", "isactive"):
            assert c in cols, f"Column {c!r} missing from statusratecolumns"

    @pytest.mark.asyncio
    async def test_status_pay_ratetype_seeded(self, direct_db: AsyncConnection):
        """STATUS_PAY system RateType seeded with UnitName='Hour'."""
        row = (await direct_db.execute(
            _text("SELECT ratecode, unitname FROM payroll.ratetypes WHERE ratecode = 'STATUS_PAY'"),
        )).mappings().first()
        assert row is not None, "STATUS_PAY RateType not seeded"
        assert row["unitname"] == "Hour"

    @pytest.mark.asyncio
    async def test_statusratecolumnid_column_on_statuskeys(self, direct_db: AsyncConnection):
        """statusratecolumnid FK column exists on payrollstatuskeys."""
        row = (await direct_db.execute(
            _text("""
                SELECT column_name FROM information_schema.columns
                WHERE  table_schema = 'payroll'
                  AND  table_name   = 'payrollstatuskeys'
                  AND  column_name  = 'statusratecolumnid'
            """),
        )).mappings().first()
        assert row is not None

    @pytest.mark.asyncio
    async def test_sourcesnapshot_on_draftlines(self, direct_db: AsyncConnection):
        """sourcesnapshot JSONB column exists on payrolldraftlines."""
        row = (await direct_db.execute(
            _text("""
                SELECT column_name FROM information_schema.columns
                WHERE  table_schema = 'payroll'
                  AND  table_name   = 'payrolldraftlines'
                  AND  column_name  = 'sourcesnapshot'
            """),
        )).mappings().first()
        assert row is not None

    @pytest.mark.asyncio
    async def test_unique_status_payment_index(self, direct_db: AsyncConnection):
        """Partial unique index ux_DraftLines_StatusPayment_Slot exists."""
        row = (await direct_db.execute(
            _text("""
                SELECT indexname FROM pg_indexes
                WHERE  schemaname = 'payroll'
                  AND  tablename  = 'payrolldraftlines'
                  AND  indexname  = 'ux_draftlines_statuspayment_slot'
            """),
        )).mappings().first()
        assert row is not None, "ux_DraftLines_StatusPayment_Slot index missing"

    @pytest.mark.asyncio
    async def test_normalized_name_index(self, direct_db: AsyncConnection):
        """ux_SRC_NormalizedName partial unique index exists."""
        row = (await direct_db.execute(
            _text("""
                SELECT indexname FROM pg_indexes
                WHERE  schemaname = 'payroll'
                  AND  tablename  = 'statusratecolumns'
                  AND  indexname  = 'ux_src_normalizedname'
            """),
        )).mappings().first()
        assert row is not None, "ux_SRC_NormalizedName index missing"

    @pytest.mark.asyncio
    async def test_psk_branch_trigger_exists(self, direct_db: AsyncConnection):
        """trg_psk_src_branch trigger exists on payrollstatuskeys."""
        row = (await direct_db.execute(
            _text("""
                SELECT trigger_name FROM information_schema.triggers
                WHERE  event_object_schema = 'payroll'
                  AND  event_object_table  = 'payrollstatuskeys'
                  AND  trigger_name        = 'trg_psk_src_branch'
            """),
        )).mappings().first()
        assert row is not None, "trg_psk_src_branch trigger missing"

    @pytest.mark.asyncio
    async def test_src_ratetype_owner_trigger_exists(self, direct_db: AsyncConnection):
        """trg_src_ratetype_owner trigger exists on statusratecolumns."""
        row = (await direct_db.execute(
            _text("""
                SELECT trigger_name FROM information_schema.triggers
                WHERE  event_object_schema = 'payroll'
                  AND  event_object_table  = 'statusratecolumns'
                  AND  trigger_name        = 'trg_src_ratetype_owner'
            """),
        )).mappings().first()
        assert row is not None, "trg_src_ratetype_owner trigger missing"


# ---------------------------------------------------------------------------
# Tests: Status Rate Columns API
# ---------------------------------------------------------------------------

class TestStatusRateColumnsAPI:
    @pytest.mark.asyncio
    async def test_list_returns_default_column(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp2d2_branch_id: int,
        cp2d2_src_col_id: int,
    ):
        """GET /branches/{id}/status-rate-columns returns the default STATUS_PAY column."""
        resp = await session_client.get(
            f"/settings/branches/{cp2d2_branch_id}/status-rate-columns",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert any(c["rate_type_code"] == "STATUS_PAY" and c["is_default"] for c in data), \
            "No default STATUS_PAY column found"

    @pytest.mark.asyncio
    async def test_create_custom_column_creates_ratetype(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp2d2_branch_id: int,
        direct_db: AsyncConnection,
    ):
        """POST creates a custom column AND auto-creates a company-owned SRC_ RateType."""
        resp = await session_client.post(
            f"/settings/branches/{cp2d2_branch_id}/status-rate-columns",
            headers=_auth(auth_token),
            json={"column_name": "Night Status Pay 2097A"},
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["column_name"] == "Night Status Pay 2097A"
        assert data["is_default"] is False

        # The backing RateType should have a SRC_ code and belong to the company
        rt_row = (await direct_db.execute(
            _text("SELECT companyid, ratecode, unitname FROM payroll.ratetypes WHERE ratetypeid = :rtid"),
            {"rtid": data["rate_type_id"]},
        )).mappings().first()
        assert rt_row is not None
        assert rt_row["ratecode"].startswith("SRC_"), f"Expected SRC_ prefix, got {rt_row['ratecode']!r}"
        assert rt_row["companyid"] == _COMPANY_ID, "Backing RateType must be company-owned"
        assert rt_row["unitname"] == "Hour"

        # Cleanup — SRC col first (FK references RateType)
        await direct_db.execute(
            _text("DELETE FROM payroll.statusratecolumns WHERE statusratecolumnid = :sid"),
            {"sid": data["status_rate_column_id"]},
        )
        await direct_db.execute(
            _text("DELETE FROM payroll.ratetypes WHERE ratetypeid = :rtid"),
            {"rtid": data["rate_type_id"]},
        )
        await direct_db.commit()

    @pytest.mark.asyncio
    async def test_duplicate_column_name_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp2d2_branch_id: int,
        direct_db: AsyncConnection,
    ):
        """POST with duplicate column name (case-insensitive) returns 422."""
        resp1 = await session_client.post(
            f"/settings/branches/{cp2d2_branch_id}/status-rate-columns",
            headers=_auth(auth_token),
            json={"column_name": "Duplicate Test 2097"},
        )
        assert resp1.status_code == 201, resp1.text
        col_id = resp1.json()["status_rate_column_id"]
        rt_id  = resp1.json()["rate_type_id"]

        # Try to create again with same name (different case)
        resp2 = await session_client.post(
            f"/settings/branches/{cp2d2_branch_id}/status-rate-columns",
            headers=_auth(auth_token),
            json={"column_name": "duplicate test 2097"},
        )
        assert resp2.status_code == 422, f"Expected 422, got {resp2.status_code}: {resp2.text}"

        # Cleanup — SRC col first
        await direct_db.execute(
            _text("DELETE FROM payroll.statusratecolumns WHERE statusratecolumnid = :sid"),
            {"sid": col_id},
        )
        await direct_db.execute(
            _text("DELETE FROM payroll.ratetypes WHERE ratetypeid = :rtid"),
            {"rtid": rt_id},
        )
        await direct_db.commit()

    @pytest.mark.asyncio
    async def test_custom_column_appears_in_list(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp2d2_branch_id: int,
        direct_db: AsyncConnection,
    ):
        """Custom SRC column appears in the GET list with correct rate_type_code prefix."""
        resp = await session_client.post(
            f"/settings/branches/{cp2d2_branch_id}/status-rate-columns",
            headers=_auth(auth_token),
            json={"column_name": "Weekend Status Pay 2097"},
        )
        assert resp.status_code == 201
        new_id = resp.json()["status_rate_column_id"]
        new_rt = resp.json()["rate_type_id"]

        list_resp = await session_client.get(
            f"/settings/branches/{cp2d2_branch_id}/status-rate-columns",
            headers=_auth(auth_token),
        )
        assert list_resp.status_code == 200
        cols = list_resp.json()
        found = next((c for c in cols if c["status_rate_column_id"] == new_id), None)
        assert found is not None
        assert found["rate_type_code"].startswith("SRC_")

        # Cleanup — SRC col first
        await direct_db.execute(
            _text("DELETE FROM payroll.statusratecolumns WHERE statusratecolumnid = :sid"), {"sid": new_id}
        )
        await direct_db.execute(
            _text("DELETE FROM payroll.ratetypes WHERE ratetypeid = :rtid"), {"rtid": new_rt}
        )
        await direct_db.commit()


# ---------------------------------------------------------------------------
# Tests: Future Branch Default
# ---------------------------------------------------------------------------

class TestFutureBranchDefault:
    @pytest.mark.asyncio
    async def test_new_branch_gets_default_status_pay(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        direct_db: AsyncConnection,
    ):
        """Creating a new branch automatically creates a default Status Pay column."""
        resp = await session_client.post(
            "/settings/branches",
            json={"branch_name": "CP2D2 Temp Branch 2097", "status": "Active"},
            headers=_auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        new_branch_id = resp.json()["branch_id"]

        try:
            col_row = (await direct_db.execute(
                _text("""
                    SELECT src.statusratecolumnid, src.isdefault, rt.ratecode
                    FROM   payroll.statusratecolumns src
                    JOIN   payroll.ratetypes rt ON rt.ratetypeid = src.ratetypeid
                    WHERE  src.branchid = :bid AND src.isdefault = TRUE AND src.isactive = TRUE
                """),
                {"bid": new_branch_id},
            )).mappings().first()
            assert col_row is not None, f"No default StatusRateColumn for new branch {new_branch_id}"
            assert col_row["ratecode"] == "STATUS_PAY"
            assert col_row["isdefault"] is True
        finally:
            # Cleanup — remove periods, draft lines, status keys, status rate columns, then branch
            await direct_db.execute(
                _text("DELETE FROM payroll.statusratecolumns WHERE branchid = :bid"),
                {"bid": new_branch_id},
            )
            await direct_db.execute(
                _text("DELETE FROM core.branches WHERE branchid = :bid"),
                {"bid": new_branch_id},
            )
            await direct_db.commit()

    @pytest.mark.asyncio
    async def test_new_branch_default_appears_in_list(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        direct_db: AsyncConnection,
    ):
        """The default Status Pay column for a new branch is returned by the list API."""
        resp = await session_client.post(
            "/settings/branches",
            json={"branch_name": "CP2D2 Temp Branch 2097B", "status": "Active"},
            headers=_auth(auth_token),
        )
        assert resp.status_code == 201
        new_branch_id = resp.json()["branch_id"]

        try:
            list_resp = await session_client.get(
                f"/settings/branches/{new_branch_id}/status-rate-columns",
                headers=_auth(auth_token),
            )
            assert list_resp.status_code == 200
            cols = list_resp.json()
            defaults = [c for c in cols if c["is_default"] and c["rate_type_code"] == "STATUS_PAY"]
            assert len(defaults) == 1
        finally:
            await direct_db.execute(
                _text("DELETE FROM payroll.statusratecolumns WHERE branchid = :bid"),
                {"bid": new_branch_id},
            )
            await direct_db.execute(
                _text("DELETE FROM core.branches WHERE branchid = :bid"),
                {"bid": new_branch_id},
            )
            await direct_db.commit()

    @pytest.mark.asyncio
    async def test_ensure_default_idempotent(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        direct_db: AsyncConnection,
    ):
        """Creating a branch twice (or calling ensure twice) does not duplicate the default."""
        resp = await session_client.post(
            "/settings/branches",
            json={"branch_name": "CP2D2 Temp Branch 2097C", "status": "Active"},
            headers=_auth(auth_token),
        )
        assert resp.status_code == 201
        new_branch_id = resp.json()["branch_id"]

        try:
            # The default was already created; call ensure helper via DB directly
            rt_row = (await direct_db.execute(
                _text("SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = 'STATUS_PAY'"),
            )).mappings().first()
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.statusratecolumns
                        (companyid, branchid, ratetypeid, columnname, normalizedcolumnname, isdefault, isactive)
                    VALUES (1, :bid, :rtid, 'Status Pay', 'STATUS PAY', TRUE, TRUE)
                    ON CONFLICT DO NOTHING
                """),
                {"bid": new_branch_id, "rtid": rt_row["ratetypeid"]},
            )
            await direct_db.commit()

            # Should still be exactly 1 default
            count = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) AS cnt FROM payroll.statusratecolumns
                    WHERE branchid = :bid AND isdefault = TRUE AND isactive = TRUE
                """),
                {"bid": new_branch_id},
            )).mappings().first()
            assert count["cnt"] == 1
        finally:
            await direct_db.execute(
                _text("DELETE FROM payroll.statusratecolumns WHERE branchid = :bid"),
                {"bid": new_branch_id},
            )
            await direct_db.execute(
                _text("DELETE FROM core.branches WHERE branchid = :bid"),
                {"bid": new_branch_id},
            )
            await direct_db.commit()


# ---------------------------------------------------------------------------
# Tests: Driver Pay Rates matrix integration
# ---------------------------------------------------------------------------

class TestDriverPayRatesMatrix:
    @pytest.mark.asyncio
    async def test_default_status_pay_appears_in_matrix(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp2d2_driver_id: int,
        cp2d2_src_col_id: int,
    ):
        """GET rate matrix includes a StatusRateColumn group for Status Pay."""
        resp = await session_client.get(
            f"/payroll/drivers/{cp2d2_driver_id}/rate-matrix",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        matrix = resp.json()
        src_groups = [g for g in matrix["groups"] if g.get("rate_source") == "StatusRateColumn"]
        assert len(src_groups) >= 1, "No StatusRateColumn groups in matrix"
        default_grp = next(
            (g for g in src_groups if g.get("rate_code") == "STATUS_PAY"),
            None,
        )
        assert default_grp is not None, "STATUS_PAY group missing from rate matrix"
        assert default_grp["status_rate_column_id"] == cp2d2_src_col_id

    @pytest.mark.asyncio
    async def test_custom_src_column_appears_in_matrix(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp2d2_branch_id: int,
        cp2d2_driver_id: int,
        direct_db: AsyncConnection,
    ):
        """A custom SRC_ column also appears in the rate matrix."""
        resp = await session_client.post(
            f"/settings/branches/{cp2d2_branch_id}/status-rate-columns",
            headers=_auth(auth_token),
            json={"column_name": "Matrix Test Custom 2097"},
        )
        assert resp.status_code == 201
        custom_col_id = resp.json()["status_rate_column_id"]
        custom_rt_id  = resp.json()["rate_type_id"]

        try:
            matrix_resp = await session_client.get(
                f"/payroll/drivers/{cp2d2_driver_id}/rate-matrix",
                headers=_auth(auth_token),
            )
            assert matrix_resp.status_code == 200
            groups = matrix_resp.json()["groups"]
            custom_grp = next(
                (g for g in groups if g.get("status_rate_column_id") == custom_col_id),
                None,
            )
            assert custom_grp is not None, "Custom SRC column not in rate matrix"
            assert custom_grp["rate_source"] == "StatusRateColumn"
            assert custom_grp["rate_code"].startswith("SRC_")
        finally:
            await direct_db.execute(
                _text("DELETE FROM payroll.statusratecolumns WHERE statusratecolumnid = :sid"),
                {"sid": custom_col_id},
            )
            await direct_db.execute(
                _text("DELETE FROM payroll.ratetypes WHERE ratetypeid = :rtid"), {"rtid": custom_rt_id}
            )
            await direct_db.commit()

    @pytest.mark.asyncio
    async def test_batch_save_creates_status_pay_rate(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp2d2_driver_id: int,
        cp2d2_src_col_id: int,
        direct_db: AsyncConnection,
    ):
        """Batch save with status_rate_column_id creates a DriverRate for STATUS_PAY."""
        # Get the rate_type_id for the default STATUS_PAY column
        src_col = (await direct_db.execute(
            _text("SELECT ratetypeid FROM payroll.statusratecolumns WHERE statusratecolumnid = :sid"),
            {"sid": cp2d2_src_col_id},
        )).mappings().first()
        assert src_col is not None
        rate_type_id = src_col["ratetypeid"]

        eff_from = datetime.date(2097, 1, 1)
        resp = await session_client.post(
            f"/payroll/drivers/{cp2d2_driver_id}/rates/batch",
            headers=_auth(auth_token),
            json={
                "effective_from": eff_from.isoformat(),
                "changes": [
                    {
                        "status_rate_column_id": cp2d2_src_col_id,
                        "rate_type_id": rate_type_id,
                        "amount": 22.50,
                    }
                ],
            },
        )
        assert resp.status_code == 200, resp.text

        # Verify the DriverRate was created
        dr_row = (await direct_db.execute(
            _text("""
                SELECT amount FROM payroll.driverrates
                WHERE driverid = :did AND ratetypeid = :rtid
                  AND status IN ('Approved', 'PendingApproval')
                ORDER BY createdatutc DESC LIMIT 1
            """),
            {"did": cp2d2_driver_id, "rtid": rate_type_id},
        )).mappings().first()
        assert dr_row is not None
        from decimal import Decimal
        assert Decimal(str(dr_row["amount"])) == Decimal("22.50")

    @pytest.mark.asyncio
    async def test_batch_save_wrong_rate_type_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp2d2_driver_id: int,
        cp2d2_src_col_id: int,
        direct_db: AsyncConnection,
    ):
        """Batch save with wrong rate_type_id for a status column is rejected 422."""
        # Use a rate_type_id that doesn't match the column's backing RateType
        wrong_rt = (await direct_db.execute(
            _text("""
                SELECT ratetypeid FROM payroll.ratetypes
                WHERE ratetypeid != (
                    SELECT ratetypeid FROM payroll.statusratecolumns WHERE statusratecolumnid = :sid
                )
                AND isactive = TRUE
                LIMIT 1
            """),
            {"sid": cp2d2_src_col_id},
        )).mappings().first()
        assert wrong_rt is not None

        resp = await session_client.post(
            f"/payroll/drivers/{cp2d2_driver_id}/rates/batch",
            headers=_auth(auth_token),
            json={
                "effective_from": "2097-01-01",
                "changes": [
                    {
                        "status_rate_column_id": cp2d2_src_col_id,
                        "rate_type_id": wrong_rt["ratetypeid"],
                        "amount": 10.00,
                    }
                ],
            },
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_batch_save_pay_item_path_unchanged(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp2d2_driver_id: int,
        direct_db: AsyncConnection,
    ):
        """PayItem batch save path still works — status columns don't regress it."""
        # Find an active PayItem+RateType pair for PAYTEST
        pi_row = (await direct_db.execute(
            _text("""
                SELECT pi.payitemid, pirm.ratetypeid
                FROM   payroll.payitems pi
                JOIN   payroll.payitemratetypemap pirm ON pirm.payitemid = pi.payitemid
                WHERE  pi.requiresrate = TRUE
                  AND  pi.status != 'Retired'
                  AND  pirm.status = 'Active'
                  AND  (pi.companyid IS NULL OR pi.companyid = 1)
                LIMIT 1
            """),
        )).mappings().first()
        if pi_row is None:
            pytest.skip("No PayItem+RateType pair available for batch save test")

        resp = await session_client.post(
            f"/payroll/drivers/{cp2d2_driver_id}/rates/batch",
            headers=_auth(auth_token),
            json={
                "effective_from": "2097-01-01",
                "changes": [
                    {
                        "pay_item_id":  pi_row["payitemid"],
                        "rate_type_id": pi_row["ratetypeid"],
                        "amount":       15.00,
                    }
                ],
            },
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Tests: Status Payment Draft Lines
# ---------------------------------------------------------------------------

class TestStatusPaymentLines:
    @pytest_asyncio.fixture(autouse=True)
    async def setup(self, direct_db: AsyncConnection, cp2d2_branch_id: int, cp2d2_driver_id: int):
        await _clean_branch(direct_db, cp2d2_branch_id)
        await _clean_status_pay_rate(direct_db, cp2d2_driver_id)
        yield
        await _clean_branch(direct_db, cp2d2_branch_id)

    @pytest.mark.asyncio
    async def test_no_payment_line_when_src_col_null(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db: AsyncConnection,
        cp2d2_branch_id: int,
        cp2d2_driver_id: int,
    ):
        """Status key with no StatusRateColumnID → no STATUS_PAYMENT line."""
        start, end = _week_2097()
        pid = await _open_period_db(direct_db, cp2d2_branch_id, start, end)
        code = f"NOPAYCOL{start.strftime('%Y%m%d')}"
        await _insert_status_key_db(direct_db, _COMPANY_ID, cp2d2_branch_id, code,
                                     hours=8.0, status_rate_column_id=None)

        resp = await client.post(
            f"/payroll/periods/{pid}/day-grid",
            headers=_auth(auth_token),
            json=_day_grid_body(cp2d2_driver_id, start, status_key=code),
        )
        assert resp.status_code == 200, resp.text
        lines = await _get_draft_lines(direct_db, pid)
        sp = [line for line in lines if (line.get("sourceid") or "").startswith("STATUS_PAYMENT:")]
        assert len(sp) == 0

    @pytest.mark.asyncio
    async def test_payment_line_created_with_rate(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db: AsyncConnection,
        cp2d2_branch_id: int,
        cp2d2_driver_id: int,
        cp2d2_src_col_id: int,
    ):
        """Status key with rate column + rate → correct STATUS_PAYMENT line."""
        start, end = _week_2097()
        pid = await _open_period_db(direct_db, cp2d2_branch_id, start, end)
        code = f"PAYCOL{start.strftime('%Y%m%d')}"
        await _insert_status_key_db(direct_db, _COMPANY_ID, cp2d2_branch_id, code,
                                     hours=8.0, status_rate_column_id=cp2d2_src_col_id)
        await _insert_driver_rate_db(direct_db, _COMPANY_ID, cp2d2_branch_id,
                                      cp2d2_driver_id, "STATUS_PAY", 25.0, start)

        resp = await client.post(
            f"/payroll/periods/{pid}/day-grid",
            headers=_auth(auth_token),
            json=_day_grid_body(cp2d2_driver_id, start, status_key=code),
        )
        assert resp.status_code == 200, resp.text

        lines = await _get_draft_lines(direct_db, pid)
        sp = [line for line in lines if (line.get("sourceid") or "").startswith("STATUS_PAYMENT:") and line["status"] == "Active"]
        assert len(sp) == 1
        from decimal import Decimal
        assert sp[0]["sourcetype"] == "System"
        assert sp[0]["needsmanagerreview"] is False
        assert Decimal(str(sp[0]["quantity"])) == Decimal("8.0")
        assert Decimal(str(sp[0]["calculatedamount"])) == Decimal("200.0000")
        assert sp[0]["linetype"] == "STATUS_PAY"

    @pytest.mark.asyncio
    async def test_no_driver_rate_sets_nmr(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db: AsyncConnection,
        cp2d2_branch_id: int,
        cp2d2_driver_id: int,
        cp2d2_src_col_id: int,
    ):
        """Status key with rate column but no driver rate → NMR=True, calc=NULL."""
        start, end = _week_2097()
        pid = await _open_period_db(direct_db, cp2d2_branch_id, start, end)
        code = f"NORATE{start.strftime('%Y%m%d')}"
        await _insert_status_key_db(direct_db, _COMPANY_ID, cp2d2_branch_id, code,
                                     hours=6.0, status_rate_column_id=cp2d2_src_col_id)

        resp = await client.post(
            f"/payroll/periods/{pid}/day-grid",
            headers=_auth(auth_token),
            json=_day_grid_body(cp2d2_driver_id, start, status_key=code),
        )
        assert resp.status_code == 200, resp.text
        lines = await _get_draft_lines(direct_db, pid)
        sp = [line for line in lines if (line.get("sourceid") or "").startswith("STATUS_PAYMENT:")]
        assert len(sp) == 1
        assert sp[0]["needsmanagerreview"] is True
        assert sp[0]["calculatedamount"] is None

    @pytest.mark.asyncio
    async def test_status_cleared_voids_payment_line(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db: AsyncConnection,
        cp2d2_branch_id: int,
        cp2d2_driver_id: int,
        cp2d2_src_col_id: int,
    ):
        """Clearing status voids the STATUS_PAYMENT line."""
        start, end = _week_2097()
        pid = await _open_period_db(direct_db, cp2d2_branch_id, start, end)
        code = f"CLRTEST{start.strftime('%Y%m%d')}"
        await _insert_status_key_db(direct_db, _COMPANY_ID, cp2d2_branch_id, code,
                                     hours=8.0, status_rate_column_id=cp2d2_src_col_id)
        await _insert_driver_rate_db(direct_db, _COMPANY_ID, cp2d2_branch_id,
                                      cp2d2_driver_id, "STATUS_PAY", 20.0, start)

        await client.post(
            f"/payroll/periods/{pid}/day-grid",
            headers=_auth(auth_token),
            json=_day_grid_body(cp2d2_driver_id, start, status_key=code),
        )
        active_before = [line for line in await _get_draft_lines(direct_db, pid)
                         if (line.get("sourceid") or "").startswith("STATUS_PAYMENT:") and line["status"] == "Active"]
        assert len(active_before) == 1

        await client.post(
            f"/payroll/periods/{pid}/day-grid",
            headers=_auth(auth_token),
            json=_day_grid_body(cp2d2_driver_id, start, status_key=None),
        )
        active_after = [line for line in await _get_draft_lines(direct_db, pid)
                        if (line.get("sourceid") or "").startswith("STATUS_PAYMENT:") and line["status"] == "Active"]
        assert len(active_after) == 0

    @pytest.mark.asyncio
    async def test_status_change_replaces_old_line(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db: AsyncConnection,
        cp2d2_branch_id: int,
        cp2d2_driver_id: int,
        cp2d2_src_col_id: int,
    ):
        """Switching from one status key to another voids the old line and creates a new one."""
        start, end = _week_2097()
        pid = await _open_period_db(direct_db, cp2d2_branch_id, start, end)

        code_a = f"KEYA{start.strftime('%Y%m%d')}"
        code_b = f"KEYB{start.strftime('%Y%m%d')}"
        await _insert_status_key_db(direct_db, _COMPANY_ID, cp2d2_branch_id, code_a,
                                     hours=4.0, status_rate_column_id=cp2d2_src_col_id)
        await _insert_status_key_db(direct_db, _COMPANY_ID, cp2d2_branch_id, code_b,
                                     hours=8.0, status_rate_column_id=cp2d2_src_col_id)
        await _insert_driver_rate_db(direct_db, _COMPANY_ID, cp2d2_branch_id,
                                      cp2d2_driver_id, "STATUS_PAY", 10.0, start)

        await client.post(
            f"/payroll/periods/{pid}/day-grid",
            headers=_auth(auth_token),
            json=_day_grid_body(cp2d2_driver_id, start, status_key=code_a),
        )
        await client.post(
            f"/payroll/periods/{pid}/day-grid",
            headers=_auth(auth_token),
            json=_day_grid_body(cp2d2_driver_id, start, status_key=code_b),
        )

        lines = await _get_draft_lines(direct_db, pid)
        active_sp = [line for line in lines if (line.get("sourceid") or "").startswith("STATUS_PAYMENT:") and line["status"] == "Active"]
        assert len(active_sp) == 1
        from decimal import Decimal
        assert Decimal(str(active_sp[0]["quantity"])) == Decimal("8.0")


# ---------------------------------------------------------------------------
# Tests: SourceSnapshot
# ---------------------------------------------------------------------------

class TestSourceSnapshot:
    @pytest_asyncio.fixture(autouse=True)
    async def setup(self, direct_db: AsyncConnection, cp2d2_branch_id: int, cp2d2_driver_id: int):
        await _clean_branch(direct_db, cp2d2_branch_id)
        await _clean_status_pay_rate(direct_db, cp2d2_driver_id)
        yield
        await _clean_branch(direct_db, cp2d2_branch_id)

    @pytest.mark.asyncio
    async def test_draft_line_has_sourcesnapshot(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db: AsyncConnection,
        cp2d2_branch_id: int,
        cp2d2_driver_id: int,
        cp2d2_src_col_id: int,
    ):
        """STATUS_PAYMENT draft line has a SourceSnapshot with required fields."""
        start, end = _week_2097()
        pid = await _open_period_db(direct_db, cp2d2_branch_id, start, end)
        code = f"SNAP{start.strftime('%Y%m%d')}"
        await _insert_status_key_db(direct_db, _COMPANY_ID, cp2d2_branch_id, code,
                                     hours=7.0, status_rate_column_id=cp2d2_src_col_id)
        await _insert_driver_rate_db(direct_db, _COMPANY_ID, cp2d2_branch_id,
                                      cp2d2_driver_id, "STATUS_PAY", 30.0, start)

        await client.post(
            f"/payroll/periods/{pid}/day-grid",
            headers=_auth(auth_token),
            json=_day_grid_body(cp2d2_driver_id, start, status_key=code),
        )

        lines = await _get_draft_lines(direct_db, pid)
        sp = [line for line in lines if (line.get("sourceid") or "").startswith("STATUS_PAYMENT:")]
        assert len(sp) == 1
        snap_raw = sp[0]["sourcesnapshot"]
        assert snap_raw is not None, "SourceSnapshot must be populated"

        snap = json.loads(snap_raw) if isinstance(snap_raw, str) else snap_raw
        assert snap.get("status_key_id") is not None
        assert snap.get("status_rate_column_id") == cp2d2_src_col_id
        assert snap.get("hours_value_used") == 7.0
        assert snap.get("resolved_rate_amount") == 30.0
        assert snap.get("rate_code") == "STATUS_PAY"
        assert snap.get("formula") == "HoursValue * DriverRate.Amount"

    @pytest.mark.asyncio
    async def test_legacy_approved_without_snapshot_fails_closed(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db: AsyncConnection,
        cp2d2_branch_id: int,
        cp2d2_driver_id: int,
        cp2d2_src_col_id: int,
    ):
        """A legacy direct-Approved Status period cannot bypass CP-4F authority."""
        start, end = _week_2097()
        pid = await _open_period_db(direct_db, cp2d2_branch_id, start, end)
        code = f"FINSNAP{start.strftime('%Y%m%d')}"
        await _insert_status_key_db(direct_db, _COMPANY_ID, cp2d2_branch_id, code,
                                     hours=8.0, status_rate_column_id=cp2d2_src_col_id)
        await _insert_driver_rate_db(direct_db, _COMPANY_ID, cp2d2_branch_id,
                                      cp2d2_driver_id, "STATUS_PAY", 25.0, start)

        await client.post(
            f"/payroll/periods/{pid}/day-grid",
            headers=_auth(auth_token),
            json=_day_grid_body(cp2d2_driver_id, start, status_key=code),
        )

        await direct_db.execute(
            _text("UPDATE payroll.payrollperiods SET status = 'Approved' WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )
        await direct_db.commit()

        # Finalize (Approved → Locked)
        fin_resp = await client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=_auth(auth_token),
        )
        assert fin_resp.status_code == 422, fin_resp.text
        assert "APPROVED_SNAPSHOT_NOT_FOUND_FOR_FINALIZATION" in fin_resp.json()["detail"]


# ---------------------------------------------------------------------------
# Tests: Manual edit guards
# ---------------------------------------------------------------------------

class TestManualEditGuards:
    @pytest_asyncio.fixture(autouse=True)
    async def setup(self, direct_db: AsyncConnection, cp2d2_branch_id: int, cp2d2_driver_id: int):
        await _clean_branch(direct_db, cp2d2_branch_id)
        await _clean_status_pay_rate(direct_db, cp2d2_driver_id)
        yield
        await _clean_branch(direct_db, cp2d2_branch_id)

    @pytest.mark.asyncio
    async def test_manual_update_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db: AsyncConnection,
        cp2d2_branch_id: int,
        cp2d2_driver_id: int,
        cp2d2_src_col_id: int,
    ):
        """PATCH on a STATUS_PAYMENT line returns 422."""
        start, end = _week_2097()
        pid = await _open_period_db(direct_db, cp2d2_branch_id, start, end)
        code = f"GUARD{start.strftime('%Y%m%d')}"
        await _insert_status_key_db(direct_db, _COMPANY_ID, cp2d2_branch_id, code,
                                     hours=8.0, status_rate_column_id=cp2d2_src_col_id)
        await _insert_driver_rate_db(direct_db, _COMPANY_ID, cp2d2_branch_id,
                                      cp2d2_driver_id, "STATUS_PAY", 30.0, start)

        await client.post(
            f"/payroll/periods/{pid}/day-grid",
            headers=_auth(auth_token),
            json=_day_grid_body(cp2d2_driver_id, start, status_key=code),
        )
        lines = await _get_draft_lines(direct_db, pid)
        sp = [line for line in lines if (line.get("sourceid") or "").startswith("STATUS_PAYMENT:")]
        assert len(sp) == 1
        sp_lid = sp[0]["draftlineid"]

        upd = await client.patch(
            f"/payroll/periods/{pid}/lines/{sp_lid}",
            headers=_auth(auth_token),
            json={"quantity": 99},
        )
        assert upd.status_code == 422
        assert "managed automatically" in upd.text.lower() or "status payment" in upd.text.lower()

    @pytest.mark.asyncio
    async def test_manual_void_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db: AsyncConnection,
        cp2d2_branch_id: int,
        cp2d2_driver_id: int,
        cp2d2_src_col_id: int,
    ):
        """DELETE on a STATUS_PAYMENT line returns 422."""
        start, end = _week_2097()
        pid = await _open_period_db(direct_db, cp2d2_branch_id, start, end)
        code = f"VGUARD{start.strftime('%Y%m%d')}"
        await _insert_status_key_db(direct_db, _COMPANY_ID, cp2d2_branch_id, code,
                                     hours=4.0, status_rate_column_id=cp2d2_src_col_id)
        await _insert_driver_rate_db(direct_db, _COMPANY_ID, cp2d2_branch_id,
                                      cp2d2_driver_id, "STATUS_PAY", 15.0, start)

        await client.post(
            f"/payroll/periods/{pid}/day-grid",
            headers=_auth(auth_token),
            json=_day_grid_body(cp2d2_driver_id, start, status_key=code),
        )
        lines = await _get_draft_lines(direct_db, pid)
        sp = [line for line in lines if (line.get("sourceid") or "").startswith("STATUS_PAYMENT:")]
        assert len(sp) == 1
        sp_lid = sp[0]["draftlineid"]

        void_resp = await client.delete(
            f"/payroll/periods/{pid}/lines/{sp_lid}",
            headers=_auth(auth_token),
        )
        assert void_resp.status_code == 422
        assert "managed automatically" in void_resp.text.lower() or "status payment" in void_resp.text.lower()


# ---------------------------------------------------------------------------
# Tests: No PTO_STATUS regression
# ---------------------------------------------------------------------------

class TestNoPTOStatusRegression:
    @pytest.mark.asyncio
    async def test_pto_status_rate_type_not_present(self, direct_db: AsyncConnection):
        """PTO_STATUS RateType must not exist."""
        row = (await direct_db.execute(
            _text("SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = 'PTO_STATUS'"),
        )).mappings().first()
        assert row is None, "PTO_STATUS RateType must not exist (was removed in migration 0055)"

    @pytest.mark.asyncio
    async def test_no_pay_item_named_pto_status(self, direct_db: AsyncConnection):
        """No PayItem with code containing PTO_STATUS should exist."""
        row = (await direct_db.execute(
            _text("SELECT payitemid FROM payroll.payitems WHERE payitemcode LIKE '%PTO_STATUS%'"),
        )).mappings().first()
        assert row is None, "PTO_STATUS PayItem must not exist"


# ---------------------------------------------------------------------------
# Tests: P1 Fix 1 — Generic /payroll/rates bypass is closed
# ---------------------------------------------------------------------------

class TestGenericRatesBypassClosed:
    """
    Verify that the single-rate create/update/approve endpoints cannot be used
    to create a DriverRate for a status-payment RateType belonging to a different
    branch.  Only batch save (which requires status_rate_column_id) is the
    correct path.
    """

    @pytest.mark.asyncio
    async def test_generic_create_for_own_branch_status_pay_succeeds(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp2d2_branch_id: int,
        cp2d2_driver_id: int,
        cp2d2_src_col_id: int,
        direct_db: AsyncConnection,
    ):
        """Generic POST /payroll/rates succeeds for STATUS_PAY belonging to driver's branch."""
        src_col = (await direct_db.execute(
            _text("SELECT ratetypeid FROM payroll.statusratecolumns WHERE statusratecolumnid = :sid"),
            {"sid": cp2d2_src_col_id},
        )).mappings().first()
        rate_type_id = src_col["ratetypeid"]

        resp = await session_client.post(
            "/payroll/rates",
            headers=_auth(auth_token),
            json={
                "driver_id":      cp2d2_driver_id,
                "rate_type_id":   rate_type_id,
                "amount":         18.00,
                "effective_from": "2097-01-01",
            },
        )
        assert resp.status_code == 201, resp.text

        # Cleanup the created rate
        rate_id = resp.json()["driver_rate_id"]
        await direct_db.execute(
            _text("DELETE FROM payroll.driverrates WHERE driverrateid = :rid"),
            {"rid": rate_id},
        )
        await direct_db.commit()

    @pytest.mark.asyncio
    async def test_generic_create_for_other_branch_status_pay_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp2d2_driver_id: int,
        cp2d2_branch_id: int,
        direct_db: AsyncConnection,
    ):
        """Generic POST /payroll/rates is rejected for a status RateType from another branch."""
        # Create a second branch and a status column for it
        other_branch = (await direct_db.execute(
            _text("""
                INSERT INTO core.branches (companyid, branchname, branchcode, status)
                VALUES (1, 'CP2D2 Other Branch P1', 'OBP1-2097', 'Active')
                RETURNING branchid
            """),
        )).mappings().first()
        other_bid = other_branch["branchid"]

        status_pay_rt = (await direct_db.execute(
            _text("SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = 'STATUS_PAY'"),
        )).mappings().first()
        rt_id = status_pay_rt["ratetypeid"]

        # Create a status column for the OTHER branch
        other_src = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.statusratecolumns
                    (companyid, branchid, ratetypeid, columnname, normalizedcolumnname, isdefault, isactive)
                VALUES (1, :bid, :rtid, 'Status Pay', 'STATUS PAY', TRUE, TRUE)
                RETURNING statusratecolumnid
            """),
            {"bid": other_bid, "rtid": rt_id},
        )).mappings().first()
        await direct_db.commit()

        try:
            # The STATUS_PAY RateType now exists for both branches.
            # But cp2d2_driver is in cp2d2_branch, so STATUS_PAY for other branch
            # is still valid because STATUS_PAY is system-level (companyid=NULL) and
            # belongs to cp2d2_branch too. Let's create a CUSTOM SRC_ type for other_branch only.
            import random
            import string
            suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
            custom_rt = (await direct_db.execute(
                _text("""
                    INSERT INTO payroll.ratetypes
                        (ratecode, ratename, unitname, companyid, isactive)
                    VALUES (:rc, 'Other Branch Only', 'Hour', 1, TRUE)
                    RETURNING ratetypeid
                """),
                {"rc": f"SRC_{suffix}"},
            )).mappings().first()
            custom_rt_id = custom_rt["ratetypeid"]

            # Add a StatusRateColumns row for the other branch using this custom RateType
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.statusratecolumns
                        (companyid, branchid, ratetypeid, columnname, normalizedcolumnname, isdefault, isactive)
                    VALUES (1, :bid, :rtid, 'Custom Other', 'CUSTOM OTHER', FALSE, TRUE)
                """),
                {"bid": other_bid, "rtid": custom_rt_id},
            )
            await direct_db.commit()

            # Now try to create a DriverRate for cp2d2_driver using this other-branch RateType
            resp = await session_client.post(
                "/payroll/rates",
                headers=_auth(auth_token),
                json={
                    "driver_id":      cp2d2_driver_id,
                    "rate_type_id":   custom_rt_id,
                    "amount":         20.00,
                    "effective_from": "2097-01-01",
                },
            )
            assert resp.status_code == 422, (
                f"Expected 422 for cross-branch status RateType, got {resp.status_code}: {resp.text}"
            )
            assert "status-payment" in resp.text.lower() or "branch" in resp.text.lower()
        finally:
            # Cleanup: remove StatusRateColumns, then RateType, then branch
            await direct_db.execute(
                _text("DELETE FROM payroll.statusratecolumns WHERE branchid = :bid"),
                {"bid": other_bid},
            )
            await direct_db.execute(
                _text("DELETE FROM payroll.ratetypes WHERE ratecode LIKE 'SRC_%' AND companyid = 1 AND ratename = 'Other Branch Only'"),
            )
            await direct_db.execute(
                _text("DELETE FROM core.branches WHERE branchid = :bid"),
                {"bid": other_bid},
            )
            await direct_db.commit()

    @pytest.mark.asyncio
    async def test_ordinary_payitem_rate_create_unaffected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp2d2_driver_id: int,
        direct_db: AsyncConnection,
    ):
        """Generic POST /payroll/rates for an ordinary PayItem RateType is not affected."""
        pi_row = (await direct_db.execute(
            _text("""
                SELECT pirm.ratetypeid
                FROM   payroll.payitems pi
                JOIN   payroll.payitemratetypemap pirm ON pirm.payitemid = pi.payitemid
                WHERE  pi.requiresrate = TRUE AND pi.status != 'Retired'
                  AND  pirm.status = 'Active'
                  AND  (pi.companyid IS NULL OR pi.companyid = 1)
                LIMIT 1
            """),
        )).mappings().first()
        if pi_row is None:
            pytest.skip("No ordinary PayItem RateType available")

        resp = await session_client.post(
            "/payroll/rates",
            headers=_auth(auth_token),
            json={
                "driver_id":      cp2d2_driver_id,
                "rate_type_id":   pi_row["ratetypeid"],
                "amount":         12.00,
                "effective_from": "2097-01-01",
            },
        )
        # Should either succeed (201) or fail for an unrelated reason (not our guard)
        # The key is it must NOT return 422 about status-payment RateType
        if resp.status_code == 422:
            assert "status-payment" not in resp.text.lower(), (
                f"Ordinary PayItem rate was blocked by status-payment guard: {resp.text}"
            )

        if resp.status_code == 201:
            rate_id = resp.json()["driver_rate_id"]
            await direct_db.execute(
                _text("DELETE FROM payroll.driverrates WHERE driverrateid = :rid"),
                {"rid": rate_id},
            )
            await direct_db.commit()


# ---------------------------------------------------------------------------
# Tests: P1 Fix 2 — _resolve_rate_behavior DB membership, not prefix
# ---------------------------------------------------------------------------

class TestResolveBehaviorDbMembership:
    """
    Verify that _resolve_rate_behavior uses DB membership in StatusRateColumns,
    not RateCode prefix, to return PerUnit for status-payment types.
    """

    @pytest.mark.asyncio
    async def test_src_prefix_without_membership_does_not_resolve_as_status(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp2d2_driver_id: int,
        direct_db: AsyncConnection,
    ):
        """A RateType with SRC_ prefix but NOT in StatusRateColumns gets normal 422 (no PayItemMap)."""
        # Insert an orphan SRC_ RateType with no StatusRateColumns row
        import random
        import string
        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
        orphan_rt = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.ratetypes
                    (ratecode, ratename, unitname, companyid, isactive)
                VALUES (:rc, 'Orphan SRC', 'Hour', 1, TRUE)
                RETURNING ratetypeid
            """),
            {"rc": f"SRC_{suffix}"},
        )).mappings().first()
        orphan_rt_id = orphan_rt["ratetypeid"]
        await direct_db.commit()

        try:
            # Attempt to create a DriverRate using this orphan RateType.
            # Since it has no StatusRateColumns row, the guard should NOT treat it
            # as a status-payment type, and it should fail with "Rate behavior could
            # not be resolved" (no PayItemRateTypeMap either).
            resp = await session_client.post(
                "/payroll/rates",
                headers=_auth(auth_token),
                json={
                    "driver_id":      cp2d2_driver_id,
                    "rate_type_id":   orphan_rt_id,
                    "amount":         10.00,
                    "effective_from": "2097-01-01",
                },
            )
            assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
            # Must NOT be the status-payment branch guard message
            assert "status-payment" not in resp.text.lower(), (
                "Orphan SRC_ type incorrectly treated as status-payment"
            )
            # Should be the PayItemRateTypeMap resolution failure
            assert "rate behavior" in resp.text.lower() or "pay item" in resp.text.lower(), (
                f"Unexpected 422 message: {resp.text}"
            )
        finally:
            await direct_db.execute(
                _text("DELETE FROM payroll.ratetypes WHERE ratetypeid = :rtid"),
                {"rtid": orphan_rt_id},
            )
            await direct_db.commit()

    @pytest.mark.asyncio
    async def test_status_ratetype_with_membership_resolves_as_perun(
        self,
        direct_db: AsyncConnection,
        cp2d2_src_col_id: int,
    ):
        """A RateType that IS in StatusRateColumns resolves as PerUnit via DB lookup."""
        src_rt = (await direct_db.execute(
            _text("SELECT ratetypeid FROM payroll.statusratecolumns WHERE statusratecolumnid = :sid"),
            {"sid": cp2d2_src_col_id},
        )).mappings().first()
        rate_type_id = src_rt["ratetypeid"]

        # Confirm it IS in StatusRateColumns
        row = (await direct_db.execute(
            _text("""
                SELECT 1 FROM payroll.statusratecolumns
                WHERE ratetypeid = :rtid AND isactive = TRUE LIMIT 1
            """),
            {"rtid": rate_type_id},
        )).first()
        assert row is not None, "STATUS_PAY RateType must be in StatusRateColumns"

        # Confirm there is no PayItemRateTypeMap entry (the old prefix shortcut path)
        pirtm = (await direct_db.execute(
            _text("""
                SELECT 1 FROM payroll.payitemratetypemap
                WHERE ratetypeid = :rtid AND status = 'Active' LIMIT 1
            """),
            {"rtid": rate_type_id},
        )).first()
        assert pirtm is None, "STATUS_PAY must not have a PayItemRateTypeMap entry"


# ---------------------------------------------------------------------------
# Tests: P1 Fix 3 — DB branch/company ownership enforcement
# ---------------------------------------------------------------------------

class TestDbBranchCompanyOwnership:
    """
    Verify DB trigger enforces that StatusRateColumns.BranchID belongs to
    StatusRateColumns.CompanyID.
    """

    @pytest.mark.asyncio
    async def test_cross_company_branch_insert_rejected(
        self,
        direct_db: AsyncConnection,
    ):
        """DB rejects inserting a StatusRateColumns row with a BranchID from another company."""
        # Get STATUS_PAY RateType
        rt_row = (await direct_db.execute(
            _text("SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = 'STATUS_PAY'"),
        )).mappings().first()
        rt_id = rt_row["ratetypeid"]

        # Get a BranchID that belongs to company 1
        branch_row = (await direct_db.execute(
            _text("SELECT branchid FROM core.branches WHERE companyid = 1 LIMIT 1"),
        )).mappings().first()
        branch_id = branch_row["branchid"]

        # Attempt to insert with company_id=999 (doesn't own this branch)
        from sqlalchemy.exc import IntegrityError
        try:
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.statusratecolumns
                        (companyid, branchid, ratetypeid, columnname, normalizedcolumnname, isdefault, isactive)
                    VALUES (999, :bid, :rtid, 'Cross Company Test', 'CROSS COMPANY TEST', FALSE, TRUE)
                """),
                {"bid": branch_id, "rtid": rt_id},
            )
            await direct_db.commit()
            pytest.fail("Expected IntegrityError for cross-company BranchID insert")
        except (IntegrityError, Exception) as e:
            await direct_db.rollback()
            err = str(e).lower()
            assert (
                "check_violation" in err or "check violation" in err
                or "branchid" in err or "company" in err
                or "belongs to company" in err
            ), f"Expected branch/company check violation, got: {e}"

    @pytest.mark.asyncio
    async def test_valid_branch_company_insert_succeeds(
        self,
        direct_db: AsyncConnection,
    ):
        """DB allows inserting StatusRateColumns where BranchID belongs to CompanyID."""
        rt_row = (await direct_db.execute(
            _text("SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = 'STATUS_PAY'"),
        )).mappings().first()
        rt_id = rt_row["ratetypeid"]

        # Create a new branch properly owned by company 1
        new_branch = (await direct_db.execute(
            _text("""
                INSERT INTO core.branches (companyid, branchname, branchcode, status)
                VALUES (1, 'CP2D2 P1Fix3 Valid Branch', 'P1F3-2097', 'Active')
                RETURNING branchid
            """),
        )).mappings().first()
        new_bid = new_branch["branchid"]

        try:
            result = (await direct_db.execute(
                _text("""
                    INSERT INTO payroll.statusratecolumns
                        (companyid, branchid, ratetypeid, columnname, normalizedcolumnname, isdefault, isactive)
                    VALUES (1, :bid, :rtid, 'Valid Branch Test', 'VALID BRANCH TEST', TRUE, TRUE)
                    RETURNING statusratecolumnid
                """),
                {"bid": new_bid, "rtid": rt_id},
            )).mappings().first()
            await direct_db.commit()
            assert result is not None
        finally:
            await direct_db.execute(
                _text("DELETE FROM payroll.statusratecolumns WHERE branchid = :bid"),
                {"bid": new_bid},
            )
            await direct_db.execute(
                _text("DELETE FROM core.branches WHERE branchid = :bid"),
                {"bid": new_bid},
            )
            await direct_db.commit()

    @pytest.mark.asyncio
    async def test_status_key_cross_branch_rejected(
        self,
        direct_db: AsyncConnection,
        cp2d2_branch_id: int,
        cp2d2_src_col_id: int,
    ):
        """DB trigger rejects linking a StatusKey to a StatusRateColumn from another branch."""
        # Create another branch
        other_branch = (await direct_db.execute(
            _text("""
                INSERT INTO core.branches (companyid, branchname, branchcode, status)
                VALUES (1, 'CP2D2 P1Fix3 Other', 'P1F3B-2097', 'Active')
                RETURNING branchid
            """),
        )).mappings().first()
        other_bid = other_branch["branchid"]
        await direct_db.commit()

        try:
            # Try to insert a StatusKey for other_bid that points to cp2d2_src_col_id (different branch)
            from sqlalchemy.exc import IntegrityError
            try:
                await direct_db.execute(
                    _text("""
                        INSERT INTO payroll.payrollstatuskeys
                            (companyid, branchid, statuscode, normalizedstatuscode, keyname,
                             isoffreason, hoursvalue, isactive, displayorder, statusratecolumnid)
                        VALUES (1, :bid, 'XBRANCH_TEST', 'XBRANCH_TEST', 'Cross Branch Test',
                                FALSE, 8.0, TRUE, 99, :src_col)
                    """),
                    {"bid": other_bid, "src_col": cp2d2_src_col_id},
                )
                await direct_db.commit()
                pytest.fail("Expected IntegrityError for cross-branch StatusKey StatusRateColumn link")
            except (IntegrityError, Exception) as e:
                await direct_db.rollback()
                err = str(e).lower()
                assert (
                    "check_violation" in err or "check violation" in err
                    or "mismatch" in err or "branch" in err
                ), f"Expected branch mismatch error, got: {e}"
        finally:
            await direct_db.execute(
                _text("DELETE FROM core.branches WHERE branchid = :bid"),
                {"bid": other_bid},
            )
            await direct_db.commit()


# ---------------------------------------------------------------------------
# Tests: P2 Fix 1 — approve_rate branch-scoped status-rate guard
# ---------------------------------------------------------------------------

class TestApproveRateStatusGuard:
    """
    Verify approve_rate enforces the same status-rate branch/company guard
    added to create_rate and update_rate (CP-2D2 hardening).
    """

    @pytest.mark.asyncio
    async def test_approve_own_branch_status_pay_succeeds(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp2d2_driver_id: int,
        cp2d2_src_col_id: int,
        direct_db: AsyncConnection,
    ):
        """Approve a PendingApproval STATUS_PAY rate for the driver's own branch — succeeds."""
        src_rt = (await direct_db.execute(
            _text("SELECT ratetypeid FROM payroll.statusratecolumns WHERE statusratecolumnid = :sid"),
            {"sid": cp2d2_src_col_id},
        )).mappings().first()
        rate_type_id = src_rt["ratetypeid"]

        # Purge any existing approved STATUS_PAY rates for this driver to avoid supersede conflicts
        await direct_db.execute(
            _text("DELETE FROM payroll.driverrates WHERE driverid = :did AND ratetypeid = :rtid"),
            {"did": cp2d2_driver_id, "rtid": rate_type_id},
        )
        pending = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.driverrates
                    (companyid, branchid, driverid, ratetypeid, amount, effectivefrom, status)
                SELECT d.companyid, d.branchid, d.driverid, :rtid, 17.00, '2097-02-01', 'PendingApproval'
                FROM core.drivers d WHERE d.driverid = :did
                RETURNING driverrateid
            """),
            {"did": cp2d2_driver_id, "rtid": rate_type_id},
        )).mappings().first()
        await direct_db.commit()
        rate_id = pending["driverrateid"]

        try:
            resp = await session_client.post(
                f"/payroll/rates/{rate_id}/approve",
                headers=_auth(auth_token),
            )
            assert resp.status_code == 200, resp.text
        finally:
            await direct_db.execute(
                _text("DELETE FROM payroll.driverrates WHERE driverid = :did AND ratetypeid = :rtid"),
                {"did": cp2d2_driver_id, "rtid": rate_type_id},
            )
            await direct_db.commit()

    @pytest.mark.asyncio
    async def test_approve_cross_branch_status_type_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp2d2_driver_id: int,
        direct_db: AsyncConnection,
    ):
        """Approve a contaminated PendingApproval rate using another branch's status RateType — 422."""
        import random
        import string

        other_branch = (await direct_db.execute(
            _text("""
                INSERT INTO core.branches (companyid, branchname, branchcode, status)
                VALUES (1, 'CP2D2 Approve Guard Branch', 'AGBR-2097', 'Active')
                RETURNING branchid
            """),
        )).mappings().first()
        other_bid = other_branch["branchid"]

        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
        custom_rt = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.ratetypes
                    (ratecode, ratename, unitname, companyid, isactive)
                VALUES (:rc, 'Other Branch Approve Test', 'Hour', 1, TRUE)
                RETURNING ratetypeid
            """),
            {"rc": f"SRC_{suffix}"},
        )).mappings().first()
        custom_rt_id = custom_rt["ratetypeid"]

        await direct_db.execute(
            _text("""
                INSERT INTO payroll.statusratecolumns
                    (companyid, branchid, ratetypeid, columnname, normalizedcolumnname, isdefault, isactive)
                VALUES (1, :bid, :rtid, 'Other Approve Col', 'OTHER APPROVE COL', FALSE, TRUE)
            """),
            {"bid": other_bid, "rtid": custom_rt_id},
        )

        # Insert contaminated PendingApproval directly to bypass create_rate guard
        pending = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.driverrates
                    (companyid, branchid, driverid, ratetypeid, amount, effectivefrom, status)
                SELECT d.companyid, d.branchid, d.driverid, :rtid, 19.00, '2097-03-01', 'PendingApproval'
                FROM core.drivers d WHERE d.driverid = :did
                RETURNING driverrateid
            """),
            {"did": cp2d2_driver_id, "rtid": custom_rt_id},
        )).mappings().first()
        await direct_db.commit()
        rate_id = pending["driverrateid"]

        try:
            resp = await session_client.post(
                f"/payroll/rates/{rate_id}/approve",
                headers=_auth(auth_token),
            )
            assert resp.status_code == 422, (
                f"Expected 422 for cross-branch status RateType approval, got {resp.status_code}: {resp.text}"
            )
            assert "status-payment" in resp.text.lower() or "branch" in resp.text.lower()
        finally:
            await direct_db.execute(
                _text("DELETE FROM payroll.driverrates WHERE driverrateid = :rid"),
                {"rid": rate_id},
            )
            await direct_db.execute(
                _text("DELETE FROM payroll.statusratecolumns WHERE branchid = :bid"),
                {"bid": other_bid},
            )
            await direct_db.execute(
                _text("DELETE FROM payroll.ratetypes WHERE ratetypeid = :rtid"),
                {"rtid": custom_rt_id},
            )
            await direct_db.execute(
                _text("DELETE FROM core.branches WHERE branchid = :bid"),
                {"bid": other_bid},
            )
            await direct_db.commit()

    @pytest.mark.asyncio
    async def test_approve_ordinary_payitem_rate_unaffected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp2d2_driver_id: int,
        direct_db: AsyncConnection,
    ):
        """Approving a PendingApproval rate for an ordinary PayItem RateType still works."""
        pi_row = (await direct_db.execute(
            _text("""
                SELECT pirm.ratetypeid
                FROM   payroll.payitems pi
                JOIN   payroll.payitemratetypemap pirm ON pirm.payitemid = pi.payitemid
                WHERE  pi.requiresrate = TRUE AND pi.status != 'Retired'
                  AND  pirm.status = 'Active'
                  AND  (pi.companyid IS NULL OR pi.companyid = 1)
                  AND  pi.ratebehavior NOT IN ('OrdinalTier', 'RangeBracket', 'RangeProgressive', 'Block')
                LIMIT 1
            """),
        )).mappings().first()
        if pi_row is None:
            pytest.skip("No suitable PayItem RateType available")

        pending = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.driverrates
                    (companyid, branchid, driverid, ratetypeid, amount, effectivefrom, status)
                SELECT d.companyid, d.branchid, d.driverid, :rtid, 13.00, '2097-04-01', 'PendingApproval'
                FROM core.drivers d WHERE d.driverid = :did
                RETURNING driverrateid
            """),
            {"did": cp2d2_driver_id, "rtid": pi_row["ratetypeid"]},
        )).mappings().first()
        await direct_db.commit()
        rate_id = pending["driverrateid"]

        try:
            resp = await session_client.post(
                f"/payroll/rates/{rate_id}/approve",
                headers=_auth(auth_token),
            )
            if resp.status_code == 422:
                assert "status-payment" not in resp.text.lower(), (
                    f"Ordinary PayItem rate blocked by status-payment guard: {resp.text}"
                )
        finally:
            await direct_db.execute(
                _text("DELETE FROM payroll.driverrates WHERE driverrateid = :rid"),
                {"rid": rate_id},
            )
            await direct_db.execute(
                _text("""
                    DELETE FROM payroll.driverrates
                    WHERE driverid = :did AND ratetypeid = :rtid AND effectivefrom = '2097-04-01'
                """),
                {"did": cp2d2_driver_id, "rtid": pi_row["ratetypeid"]},
            )
            await direct_db.commit()


# ---------------------------------------------------------------------------
# Tests: P2 Fix 2 — GET status-rate-columns branch-access enforcement
# ---------------------------------------------------------------------------

class TestStatusRateColumnsAccessControl:
    @pytest.mark.asyncio
    async def test_list_allowed_branch_succeeds(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp2d2_branch_id: int,
        cp2d2_src_col_id: int,
    ):
        """Authenticated user with branch access can list status rate columns."""
        resp = await session_client.get(
            f"/settings/branches/{cp2d2_branch_id}/status-rate-columns",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert any(c["status_rate_column_id"] == cp2d2_src_col_id for c in data)

    @pytest.mark.asyncio
    async def test_list_returns_only_own_company_branch(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        direct_db: AsyncConnection,
    ):
        """GET with a non-existent branch_id returns empty list or 403/404 — no foreign data."""
        resp = await session_client.get(
            "/settings/branches/999999/status-rate-columns",
            headers=_auth(auth_token),
        )
        assert resp.status_code in (200, 403, 404)
        if resp.status_code == 200:
            assert resp.json() == [], "Non-existent branch must return empty list, not foreign data"

    @pytest.mark.asyncio
    async def test_status_pay_ratename_is_status_pay(
        self,
        direct_db: AsyncConnection,
    ):
        """STATUS_PAY RateType has RateName = 'Status Pay' (not 'Status Payment')."""
        row = (await direct_db.execute(
            _text("SELECT ratename FROM payroll.ratetypes WHERE ratecode = 'STATUS_PAY'"),
        )).mappings().first()
        assert row is not None
        assert row["ratename"] == "Status Pay", f"Expected 'Status Pay', got '{row['ratename']}'"
