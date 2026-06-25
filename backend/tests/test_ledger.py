"""
CP-4 Integration tests for the Ledger — finalized payroll periods and final lines.

  GET /payroll/periods               (with status filter)
  GET /payroll/periods/{id}/final-lines

Isolation
---------
Tests use dates in the year 2087 to avoid conflicts with other test modules.
A session-scoped `locked_period` fixture creates one period, advances it to
Approved, seeds lines, and finalizes it once for the whole module.

Covered:
- Ledger lists Locked periods via status=Locked filter
- Ledger lists Archived periods via status=Archived filter
- Draft/Open/InReview/Approved periods do NOT appear in Locked ledger
- Final lines endpoint returns PayrollFinalLines data (final_line_id, final_amount)
- Bonus (BONUS line_scope=Period) appears in final lines
- SYS_MIN_TOPUP appears in final lines when minimum pay rule triggered
- ODA/Driver users are blocked with 403 from final-lines endpoint
- Branch-scoped user cannot see another branch's final lines
- AllCompanyBranches user can see finalized period in scope
- Empty final lines for a non-finalized (Approved) period
- PeriodSummary includes final_gross and final_driver_count after finalization
"""
import datetime
import pytest
import pytest_asyncio
import httpx
from decimal import Decimal
from sqlalchemy import text as _sqla_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _activate_bonus(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
) -> None:
    """Activate the BONUS pay item for a branch if not already active."""
    items = await client.get(
        f"/settings/branches/{branch_id}/pay-items",
        headers=auth(token),
    )
    if items.status_code != 200:
        return
    for item in items.json():
        if item["pay_item_code"] == "BONUS":
            if not item.get("is_active", False):
                await client.patch(
                    f"/settings/branches/{branch_id}/pay-items/{item['pay_item_id']}",
                    json={"is_active": True},
                    headers=auth(token),
                )
            return
    # Try global items list
    all_items = await client.get("/settings/pay-items", headers=auth(token))
    if all_items.status_code == 200:
        for item in all_items.json():
            if item.get("pay_item_code") == "BONUS":
                await client.patch(
                    f"/settings/branches/{branch_id}/pay-items/{item['pay_item_id']}",
                    json={"is_active": True},
                    headers=auth(token),
                )
                return


async def _cancel_active_periods(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
) -> None:
    # CP-1A: only Draft and Open can be cancelled via PATCH.
    headers = auth(token)
    for s in ("Draft", "Open"):
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


async def _advance_to_approved(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    start_date: str = "2087-01-07",
) -> None:
    """Draft→Open→InReview→Approved via review flow."""
    headers = auth(token)
    lines_resp = await client.get(
        f"/payroll/periods/{period_id}/lines",
        headers=headers,
        params={"status": "Active"},
    )
    if lines_resp.status_code == 200 and len(lines_resp.json()) == 0:
        await client.post(
            f"/payroll/periods/{period_id}/lines",
            headers=headers,
            json={
                "driver_id": driver_id,
                "work_date": start_date,
                "line_type": "DailyNote",
                "quantity": 1,
                "notes": "filler",
            },
        )
    r = await client.patch(
        f"/payroll/periods/{period_id}/status",
        headers=headers,
        json={"status": "InReview"},
    )
    assert r.status_code == 200, f"InReview failed: {r.text}"

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
    decide = await client.post(
        f"/review/items/{review_item['review_item_id']}/decide",
        headers=headers,
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
        headers=auth(token),
    )
    assert cr.status_code == 201
    role_id = cr.json()["company_role_id"]
    if perms:
        pr = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": perms},
            headers=auth(token),
        )
        assert pr.status_code == 200
    return role_id


async def _create_user_with_role(
    client: httpx.AsyncClient,
    admin_token: str,
    username: str,
    role_id: int,
    scope_type: str = "AllCompanyBranches",
    branch_id=None,
) -> str:
    resp = await client.post(
        "/admin/users",
        json={
            "username": username,
            "display_name": username,
            "password": "TestPass123!",
            "is_active": True,
            "can_login": True,
            "must_change_password": False,
        },
        headers=auth(admin_token),
    )
    assert resp.status_code == 201, f"Create user failed: {resp.text}"
    user_id = resp.json()["user_id"]
    assign_body: dict = {"company_role_id": role_id, "scope_type": scope_type}
    if branch_id is not None:
        assign_body["branch_id"] = branch_id
    assign = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json=assign_body,
        headers=auth(admin_token),
    )
    assert assign.status_code in (200, 201)
    login = await client.post("/auth/login", json={
        "username": username,
        "password": "TestPass123!",
        "company_code": "DEMO",
    })
    assert login.status_code == 200
    return login.json()["access_token"]


# ---------------------------------------------------------------------------
# Session-scoped locked period fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="module")
async def ledger_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> int:
    """Create an isolated driver for ledger tests with no approved rates."""
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": paytest_branch_id,
            "full_name": "Ledger Isolated Driver",
            "driver_code": "LID-0001",
        },
        headers=auth(auth_token),
    )
    assert resp.status_code == 201, f"Create ledger driver failed: {resp.text}"
    return resp.json()["driver_id"]


@pytest_asyncio.fixture(scope="module")
async def locked_period_data(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    ledger_driver_id: int,
    paytest_rate_type_id: int,
    session_db_conn,
) -> dict:
    """
    Create a period on the PAYTEST branch (dates 2087-01-06 to 2087-01-12),
    seed an HOURS line + a BONUS line, finalize it.  Returns a dict with
    'period', 'hours_line', 'bonus_line'.

    The fixture cancels any active periods on PAYTEST before creating
    the ledger period, then restores state at teardown.

    Phase 4C: rate_amount is no longer accepted for PerUnit lines.
    An approved HOURLY DriverRate ($25) is created for ledger_driver_id.
    """
    headers = auth(auth_token)
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
    await _activate_bonus(session_client, auth_token, paytest_branch_id)

    # Create and approve an HOURLY rate ($25) for ledger_driver_id
    rate_resp = await session_client.post(
        "/payroll/rates",
        json={
            "driver_id":      ledger_driver_id,
            "rate_type_id":   paytest_rate_type_id,
            "amount":         "25.00",
            "effective_from": "2087-01-01",
        },
        headers=headers,
    )
    assert rate_resp.status_code == 201, f"Create rate failed: {rate_resp.text}"
    ledger_rate_id = rate_resp.json()["driver_rate_id"]
    approve_resp = await session_client.post(
        f"/payroll/rates/{ledger_rate_id}/approve",
        headers=headers,
    )
    assert approve_resp.status_code == 200, f"Approve rate failed: {approve_resp.text}"

    # Insert Open period directly (CP-1D: POST requires existing Open; PATCH Draft→Open blocked).
    row = (await session_db_conn.execute(
        _sqla_text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', 'LEDGER-2087-0106', 'Ledger Test 2087-W01', 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": paytest_branch_id,
         "start": datetime.date(2087, 1, 6),
         "end": datetime.date(2087, 1, 12)},
    )).mappings().first()
    if row is None:
        row = (await session_db_conn.execute(
            _sqla_text(
                "SELECT payrollperiodid FROM payroll.payrollperiods "
                "WHERE branchid = :bid AND periodcode = 'LEDGER-2087-0106'"
            ),
            {"bid": paytest_branch_id},
        )).mappings().first()
    pid = row["payrollperiodid"]

    # Seed HOURS line (PerUnit, 8h × $25 = $200 from approved DriverRate)
    hl = await session_client.post(
        f"/payroll/periods/{pid}/lines",
        json={
            "driver_id": ledger_driver_id,
            "work_date": "2087-01-07",
            "line_type": "Hours",
            "quantity": "8.00",
        },
        headers=headers,
    )
    assert hl.status_code == 201, f"Add hours line failed: {hl.text}"
    hours_line = hl.json()

    # Seed BONUS period-pay line ($50 bonus)
    bl = await session_client.post(
        f"/payroll/periods/{pid}/period-pay",
        json={
            "driver_id": ledger_driver_id,
            "line_type": "BONUS",
            "amount": "50.00",
            "notes": "Performance bonus",
        },
        headers=headers,
    )
    assert bl.status_code == 201, f"Add bonus line failed: {bl.text}"
    bonus_line = bl.json()

    # Open → InReview → Approved via review flow
    await _advance_to_approved(session_client, auth_token, pid, ledger_driver_id)

    # Finalize
    fin = await session_client.post(
        f"/payroll/periods/{pid}/finalize",
        headers=headers,
    )
    assert fin.status_code == 200, f"Finalize failed: {fin.text}"
    locked = fin.json()
    assert locked["status"] == "Locked"

    yield {
        "period": locked,
        "period_id": pid,
        "hours_line": hours_line,
        "bonus_line": bonus_line,
    }

    # Teardown: nothing to cancel (Locked periods cannot be cancelled)


# ---------------------------------------------------------------------------
# 1. Period list — Ledger (Locked/Archived status filter)
# ---------------------------------------------------------------------------

class TestLedgerPeriodList:

    @pytest.mark.asyncio
    async def test_locked_period_appears_with_status_filter(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        locked_period_data: dict,
    ):
        """GET /payroll/periods?status=Locked must include the locked period."""
        pid = locked_period_data["period_id"]
        resp = await session_client.get(
            "/payroll/periods",
            params={"status": "Locked"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        ids = [p["payroll_period_id"] for p in resp.json()]
        assert pid in ids

    @pytest.mark.asyncio
    async def test_draft_period_absent_from_locked_filter(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """GET /payroll/periods?status=Locked must not return Draft periods."""
        # Insert a Draft period directly (CP-1D: POST requires an existing Open)
        _r = (await direct_db.execute(
            _sqla_text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (1, :bid, 'Draft', 'LEDGER-DRAFT-2087-07', 'Ledger Draft Filter Test', 'Week', :start, :end)
                ON CONFLICT DO NOTHING
                RETURNING payrollperiodid
            """),
            {"bid": paytest_branch_id,
             "start": datetime.date(2087, 7, 7),
             "end": datetime.date(2087, 7, 13)},
        )).mappings().first()
        if _r is None:
            _r = (await direct_db.execute(
                _sqla_text(
                    "SELECT payrollperiodid FROM payroll.payrollperiods "
                    "WHERE branchid = :bid AND periodcode = 'LEDGER-DRAFT-2087-07'"
                ),
                {"bid": paytest_branch_id},
            )).mappings().first()
        draft_pid = _r["payrollperiodid"]

        try:
            resp = await session_client.get(
                "/payroll/periods",
                params={"status": "Locked"},
                headers=auth(auth_token),
            )
            assert resp.status_code == 200
            ids = [p["payroll_period_id"] for p in resp.json()]
            assert draft_pid not in ids
        finally:
            await session_client.patch(
                f"/payroll/periods/{draft_pid}/status",
                json={"status": "Cancelled"},
                headers=auth(auth_token),
            )

    @pytest.mark.asyncio
    async def test_period_summary_includes_final_gross(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        locked_period_data: dict,
    ):
        """PeriodSummary for a Locked period must include final_gross > 0."""
        pid = locked_period_data["period_id"]
        resp = await session_client.get(
            f"/payroll/periods/{pid}",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "final_gross" in body
        gross = Decimal(str(body["final_gross"]))
        # Hours: 8 × $25 = $200; Bonus: $50 → total $250
        assert gross >= Decimal("200"), f"Expected final_gross >= 200, got {gross}"

    @pytest.mark.asyncio
    async def test_period_summary_includes_final_driver_count(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        locked_period_data: dict,
    ):
        """PeriodSummary for a Locked period must include final_driver_count >= 1."""
        pid = locked_period_data["period_id"]
        resp = await session_client.get(
            f"/payroll/periods/{pid}",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("final_driver_count", 0) >= 1

    @pytest.mark.asyncio
    async def test_non_locked_period_has_zero_final_gross(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """A Draft period must have final_gross=0 (no final lines)."""
        _r = (await direct_db.execute(
            _sqla_text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (1, :bid, 'Draft', 'LEDGER-DRAFT-2087-08', 'Ledger Zero Gross Test', 'Week', :start, :end)
                ON CONFLICT DO NOTHING
                RETURNING payrollperiodid
            """),
            {"bid": paytest_branch_id,
             "start": datetime.date(2087, 8, 4),
             "end": datetime.date(2087, 8, 10)},
        )).mappings().first()
        if _r is None:
            _r = (await direct_db.execute(
                _sqla_text(
                    "SELECT payrollperiodid FROM payroll.payrollperiods "
                    "WHERE branchid = :bid AND periodcode = 'LEDGER-DRAFT-2087-08'"
                ),
                {"bid": paytest_branch_id},
            )).mappings().first()
        pid = _r["payrollperiodid"]
        try:
            resp = await session_client.get(
                f"/payroll/periods/{pid}",
                headers=auth(auth_token),
            )
            assert resp.status_code == 200
            assert Decimal(str(resp.json().get("final_gross", "0"))) == Decimal("0")
        finally:
            await session_client.patch(
                f"/payroll/periods/{pid}/status",
                json={"status": "Cancelled"},
                headers=auth(auth_token),
            )


# ---------------------------------------------------------------------------
# 2. Final lines endpoint — data correctness
# ---------------------------------------------------------------------------

class TestFinalLines:

    @pytest.mark.asyncio
    async def test_requires_auth(
        self,
        client: httpx.AsyncClient,
        locked_period_data: dict,
    ):
        pid = locked_period_data["period_id"]
        resp = await client.get(f"/payroll/periods/{pid}/final-lines")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_final_lines_returns_200(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        locked_period_data: dict,
    ):
        pid = locked_period_data["period_id"]
        resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        lines = resp.json()
        assert isinstance(lines, list)
        assert len(lines) >= 1

    @pytest.mark.asyncio
    async def test_final_lines_have_final_line_id(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        locked_period_data: dict,
    ):
        """Lines come from PayrollFinalLines — they must have final_line_id."""
        pid = locked_period_data["period_id"]
        resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        for line in resp.json():
            assert "final_line_id" in line
            assert line["final_line_id"] is not None

    @pytest.mark.asyncio
    async def test_final_lines_have_final_amount(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        locked_period_data: dict,
    ):
        """Every final line must have a non-null final_amount."""
        pid = locked_period_data["period_id"]
        resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        for line in resp.json():
            assert "final_amount" in line
            assert line["final_amount"] is not None

    @pytest.mark.asyncio
    async def test_hours_final_line_appears(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        locked_period_data: dict,
    ):
        """HOURS line seeded before finalization must appear in final lines."""
        pid = locked_period_data["period_id"]
        resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        types = {l["line_type"] for l in resp.json()}
        assert "HOURS" in types

    @pytest.mark.asyncio
    async def test_bonus_final_line_appears(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        locked_period_data: dict,
    ):
        """BONUS period-pay line seeded before finalization must appear in final lines."""
        pid = locked_period_data["period_id"]
        resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        bonus_lines = [l for l in resp.json() if l["line_type"] == "BONUS"]
        assert len(bonus_lines) >= 1, "BONUS final line not found"
        # Bonus final_amount should be $50
        assert Decimal(str(bonus_lines[0]["final_amount"])) == Decimal("50.00")

    @pytest.mark.asyncio
    async def test_hours_final_amount_correct(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        locked_period_data: dict,
    ):
        """HOURS final_amount must be 8 × $25 = $200."""
        pid = locked_period_data["period_id"]
        resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        hours_lines = [l for l in resp.json() if l["line_type"] == "HOURS"]
        assert hours_lines, "HOURS final line not found"
        assert Decimal(str(hours_lines[0]["final_amount"])) == Decimal("200.00")

    @pytest.mark.asyncio
    async def test_final_lines_empty_for_approved_period(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """GET final-lines for an Approved (not yet finalized) period returns 422.

        Final lines are only available for Locked or Archived periods.
        Calling final-lines on an Approved period is rejected — draft data must
        not be exposed through the ledger path.
        """
        headers = auth(auth_token)
        # Insert Open period directly (CP-1D: POST requires existing Open; PATCH Draft→Open blocked).
        _r = (await direct_db.execute(
            _sqla_text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (1, :bid, 'Open', 'LEDGER-APPR-2087-09', 'Ledger Approved Test', 'Week', :start, :end)
                ON CONFLICT DO NOTHING
                RETURNING payrollperiodid
            """),
            {"bid": paytest_branch_id,
             "start": datetime.date(2087, 9, 8),
             "end": datetime.date(2087, 9, 14)},
        )).mappings().first()
        if _r is None:
            _r = (await direct_db.execute(
                _sqla_text(
                    "SELECT payrollperiodid FROM payroll.payrollperiods "
                    "WHERE branchid = :bid AND periodcode = 'LEDGER-APPR-2087-09'"
                ),
                {"bid": paytest_branch_id},
            )).mappings().first()
        pid = _r["payrollperiodid"]
        try:
            # Open → InReview → Approved
            await _advance_to_approved(
                session_client, auth_token, pid, paytest_driver_id, "2087-09-09"
            )
            # Final-lines on an Approved (not Locked) period must be rejected.
            resp = await session_client.get(
                f"/payroll/periods/{pid}/final-lines",
                headers=headers,
            )
            assert resp.status_code == 422, (
                "final-lines must reject non-Locked/non-Archived periods; "
                f"got {resp.status_code}: {resp.text}"
            )
        finally:
            # CP-0C: Approved→InReview is now blocked. Force directly to Cancelled.
            from sqlalchemy import text as _text
            await direct_db.execute(
                _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' WHERE payrollperiodid = :pid"),
                {"pid": pid},
            )

    @pytest.mark.asyncio
    async def test_final_lines_driver_filter(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        locked_period_data: dict,
        ledger_driver_id: int,
    ):
        """driver_id query param filters to a specific driver."""
        pid = locked_period_data["period_id"]
        resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines",
            params={"driver_id": ledger_driver_id},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        for line in resp.json():
            assert line["driver_id"] == ledger_driver_id


# ---------------------------------------------------------------------------
# 3. Security — ODA / Driver block
# ---------------------------------------------------------------------------

class TestFinalLinesODABlock:
    """
    Driver-role (ODA) users must be blocked from GET /payroll/periods/{id}/final-lines
    with HTTP 403.  They must not receive any finalized payroll data.
    """

    @pytest.mark.asyncio
    async def test_oda_user_blocked_from_final_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        locked_period_data: dict,
        paytest_branch_id: int,
        paytest_driver_id: int,
    ):
        """ODA user gets 403 on GET final-lines."""
        # Create a role with payroll.view + oda scope (OwnDriverDataOnly)
        role_id = await _create_role_with_perms(
            session_client, auth_token,
            "LGR_ODARole_2087",
            ["payroll.view"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token,
            "lgr_oda_user_2087", role_id,
            scope_type="OwnDriverDataOnly",
            branch_id=paytest_branch_id,
        )
        pid = locked_period_data["period_id"]
        resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines",
            headers=auth(oda_token),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_driver_only_user_blocked_from_final_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        locked_period_data: dict,
        paytest_branch_id: int,
    ):
        """Driver-role user with no payroll permissions gets 403 on final-lines."""
        role_id = await _create_role_with_perms(
            session_client, auth_token,
            "LGR_DriverRole_2087",
            [],  # no payroll permissions
        )
        driver_token = await _create_user_with_role(
            session_client, auth_token,
            "lgr_driver_user_2087", role_id,
            scope_type="OwnDriverDataOnly",
            branch_id=paytest_branch_id,
        )
        pid = locked_period_data["period_id"]
        resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines",
            headers=auth(driver_token),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 4. Security — branch scope
# ---------------------------------------------------------------------------

class TestFinalLinesBranchScope:

    @pytest.mark.asyncio
    async def test_branch_scoped_user_cannot_see_other_branch_final_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        locked_period_data: dict,
        paytest_branch_id: int,
    ):
        """
        A user scoped to a different branch must get 403 on final-lines for
        the PAYTEST branch's locked period.
        """
        # Find a branch that is NOT paytest_branch_id
        branches_resp = await session_client.get(
            "/core/branches",
            headers=auth(auth_token),
        )
        assert branches_resp.status_code == 200
        other_branch = next(
            (b for b in branches_resp.json() if b["branch_id"] != paytest_branch_id),
            None,
        )
        if other_branch is None:
            pytest.skip("Need at least 2 branches for this test")

        other_branch_id = other_branch["branch_id"]
        role_id = await _create_role_with_perms(
            session_client, auth_token,
            "LGR_OtherBranchRole_2087",
            ["payroll.view"],
        )
        other_token = await _create_user_with_role(
            session_client, auth_token,
            "lgr_other_branch_2087", role_id,
            scope_type="SpecificBranch",
            branch_id=other_branch_id,
        )
        pid = locked_period_data["period_id"]
        resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines",
            headers=auth(other_token),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_allcompanybranches_user_can_see_final_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        locked_period_data: dict,
    ):
        """
        AllCompanyBranches user with payroll.view can see any branch's final lines.
        The admin user (auth_token) has AllCompanyBranches scope.
        """
        pid = locked_period_data["period_id"]
        resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# 5. SYS adjustment final lines
# ---------------------------------------------------------------------------

class TestSysAdjFinalLines:

    @pytest.mark.asyncio
    async def test_sys_min_topup_appears_in_final_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        ledger_driver_id: int,
        paytest_rate_type_id: int,
        direct_db,
    ):
        """
        When a MinimumPay rule causes a top-up, SYS_MIN_TOPUP must appear
        in PayrollFinalLines with line_type='SYS_MIN_TOPUP'.

        Uses 2087-10 dates (safe from locked_period fixture which uses 2087-01).
        MinimumPay rule effective only in Oct 2087 to avoid leaking into other tests.

        Phase 5 note: uses ledger_driver_id (isolated module driver) instead of
        paytest_driver_id.  The finalized DriverRate stays referenced in
        PayrollFinalLines after the test (Phase 5 void guard rejects a void
        attempt), but ledger_driver_id is not shared with test_rates.py so there
        is no cascade to approval-conflict checks in other modules.
        """
        headers = auth(auth_token)
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)

        # Create MinimumPay rule for ledger_driver_id: $500 minimum, Oct 2087 only
        rule_resp = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id": ledger_driver_id,
                "branch_id": paytest_branch_id,
                "rule_type": "MinimumPay",
                "amount": "500.00",
                "effective_from": "2087-10-01",
                "effective_to": "2087-10-31",
            },
            headers=headers,
        )
        assert rule_resp.status_code == 201, f"Create pay rule failed: {rule_resp.text}"
        rule_id = rule_resp.json()["driver_pay_rule_id"]

        # Insert Open period directly (CP-1D: POST requires existing Open; PATCH Draft→Open blocked).
        _r = (await direct_db.execute(
            _sqla_text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (1, :bid, 'Open', 'LEDGER-TOPUP-2087-10', 'Ledger TopUp Test', 'Week', :start, :end)
                ON CONFLICT DO NOTHING
                RETURNING payrollperiodid
            """),
            {"bid": paytest_branch_id,
             "start": datetime.date(2087, 10, 6),
             "end": datetime.date(2087, 10, 12)},
        )).mappings().first()
        if _r is None:
            _r = (await direct_db.execute(
                _sqla_text(
                    "SELECT payrollperiodid FROM payroll.payrollperiods "
                    "WHERE branchid = :bid AND periodcode = 'LEDGER-TOPUP-2087-10'"
                ),
                {"bid": paytest_branch_id},
            )).mappings().first()
        pid = _r["payrollperiodid"]

        # Create and approve an HOURLY rate ($25) for ledger_driver_id.
        # A rate for ledger_driver_id at 2087-01-01 already exists (locked_period_data
        # fixture); the new 2087-10-01 rate supersedes it, which is fine.
        rate_resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id":      ledger_driver_id,
                "rate_type_id":   paytest_rate_type_id,
                "amount":         "25.00",
                "effective_from": "2087-10-01",
            },
            headers=headers,
        )
        assert rate_resp.status_code == 201, f"Create rate failed: {rate_resp.text}"
        topup_rate_id = rate_resp.json()["driver_rate_id"]
        approve_resp = await session_client.post(
            f"/payroll/rates/{topup_rate_id}/approve",
            headers=headers,
        )
        assert approve_resp.status_code == 200, f"Approve rate failed: {approve_resp.text}"

        # Add a small HOURS line (2h × $25 = $50) so minimum ($500) triggers a top-up.
        # Phase 4C: rate_amount removed; approved DriverRate provides the rate.
        line_resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={
                "driver_id": ledger_driver_id,
                "work_date": "2087-10-07",
                "line_type": "Hours",
                "quantity": "2.00",
            },
            headers=headers,
        )
        assert line_resp.status_code == 201

        await _advance_to_approved(
            session_client, auth_token, pid, ledger_driver_id, "2087-10-07"
        )

        fin = await session_client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=headers,
        )
        assert fin.status_code == 200, f"Finalize failed: {fin.text}"

        # Verify SYS_MIN_TOPUP in final lines
        fl_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines",
            headers=headers,
        )
        assert fl_resp.status_code == 200
        types = [l["line_type"] for l in fl_resp.json()]
        assert "SYS_MIN_TOPUP" in types, f"SYS_MIN_TOPUP missing; got types: {types}"

        # Cleanup: void the pay rule via API.
        # topup_rate_id stays referenced in PayrollFinalLines (Phase 5 guard would
        # reject a void attempt), but ledger_driver_id isolation prevents contamination.
        await session_client.delete(
            f"/payroll/driver-pay-rules/{rule_id}",
            headers=headers,
        )
