"""
CP-6 Integration tests — Review / Approve UI

Tests cover:
  1.  Review list (GET /review/items?status=Pending) shows InReview period items
  2.  Open/Draft/Approved/Locked periods are NOT in the default Pending review list
  3.  Operational reviewer can approve a clean InReview period (→Approved)
  4.  Period becomes Approved after review approval
  5.  Approved period can then be finalized (existing finalize flow)
  6.  Period with NeedsManagerReview lines cannot be approved (422)
  7.  Return (EditRequested) works → period goes to Open
  8.  Driver/ODA user is blocked from GET /review/items (403)
  9.  Branch-scoped user cannot approve another branch's period (403)
  10. AllCompanyBranches user can review periods from any branch
  11. No NMR lines = no blocker (Approve proceeds)
  12. Existing CP-0 through CP-5 payroll/review tests still pass

Isolation: all periods use dates in 2091 to avoid conflicts with other test suites.
"""
import pytest
import pytest_asyncio
import httpx
from sqlalchemy import text as _text


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PERIOD_START = "2095-01-06"
PERIOD_END   = "2095-01-12"
WORK_DATE    = "2095-01-07"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_active_periods(
    client: httpx.AsyncClient, token: str, branch_id: int,
) -> None:
    headers = auth(token)
    for s in ("Draft", "Open", "InReview", "Approved"):
        resp = await client.get(
            "/payroll/periods",
            params={"branch_id": branch_id, "status": s},
            headers=headers,
        )
        if resp.status_code == 200:
            for p in resp.json():
                await client.patch(
                    f"/payroll/periods/{p['payroll_period_id']}/status",
                    json={"status": "Cancelled"},
                    headers=headers,
                )


async def _create_open_period(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    start: str = PERIOD_START,
    end: str = PERIOD_END,
) -> int:
    headers = auth(token)
    r = await client.post(
        "/payroll/periods",
        json={"branch_id": branch_id, "period_type": "Week",
              "start_date": start, "end_date": end},
        headers=headers,
    )
    assert r.status_code == 201, f"Create period: {r.text}"
    pid = r.json()["payroll_period_id"]
    t = await client.patch(
        f"/payroll/periods/{pid}/status",
        json={"status": "Open"}, headers=headers,
    )
    assert t.status_code == 200, f"Open: {t.text}"
    return pid


async def _advance_to_inreview(
    client: httpx.AsyncClient,
    token: str,
    pid: int,
    driver_id: int,
    work_date: str = WORK_DATE,
) -> int:
    """Advance an Open period to InReview. Returns the review_item_id."""
    headers = auth(token)
    # Ensure a non-void line exists (PTO_STATUS is informational, always passes)
    lines = await client.get(
        f"/payroll/periods/{pid}/lines",
        params={"status": "Active"}, headers=headers,
    )
    if lines.status_code == 200 and len(lines.json()) == 0:
        r = await client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": driver_id, "work_date": work_date,
                  "line_type": "PTO_STATUS", "quantity": 1},
            headers=headers,
        )
        assert r.status_code == 201, f"Add line: {r.text}"

    tr = await client.patch(
        f"/payroll/periods/{pid}/status",
        json={"status": "InReview"}, headers=headers,
    )
    assert tr.status_code == 200, f"InReview: {tr.text}"

    # Retrieve the Pending review item created by the InReview transition
    rv = await client.get("/review/items", headers=headers,
                          params={"status": "Pending"})
    assert rv.status_code == 200
    item = next(
        (i for i in rv.json()
         if i.get("entity_name") == "PayrollPeriods"
         and i.get("entity_id") == str(pid)
         and i.get("status") == "Pending"),
        None,
    )
    assert item is not None, f"No Pending review item for period {pid}"
    return item["review_item_id"]


async def _advance_to_approved_via_review(
    client: httpx.AsyncClient,
    token: str,
    pid: int,
    driver_id: int,
    work_date: str = WORK_DATE,
) -> None:
    """Full Open → InReview → Approved flow via review decide."""
    review_id = await _advance_to_inreview(client, token, pid, driver_id, work_date)
    dec = await client.post(
        f"/review/items/{review_id}/decide",
        json={"decision": "Approved"}, headers=auth(token),
    )
    assert dec.status_code == 200, f"Approve failed: {dec.text}"


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
    assert cr.status_code == 201, f"Create role: {cr.text}"
    role_id = cr.json()["company_role_id"]
    if perms:
        pr = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": perms},
            headers=auth(token),
        )
        assert pr.status_code == 200, f"Set perms: {pr.text}"
    return role_id


async def _create_user_with_role(
    client: httpx.AsyncClient,
    admin_token: str,
    username: str,
    role_id: int,
    scope_type: str = "AllCompanyBranches",
    branch_id: int | None = None,
    password: str = "TestPass123!",
) -> str:
    resp = await client.post(
        "/admin/users",
        json={"username": username, "display_name": username,
              "password": password, "is_active": True,
              "can_login": True, "must_change_password": False},
        headers=auth(admin_token),
    )
    assert resp.status_code == 201, f"Create user: {resp.text}"
    user_id = resp.json()["user_id"]
    body: dict = {"company_role_id": role_id, "scope_type": scope_type}
    if branch_id is not None:
        body["branch_id"] = branch_id
    assign = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json=body, headers=auth(admin_token),
    )
    assert assign.status_code in (200, 201), f"Assign role: {assign.text}"
    login = await client.post("/auth/login", json={
        "username": username, "password": password, "company_code": "DEMO",
    })
    assert login.status_code == 200, f"Login: {login.text}"
    return login.json()["access_token"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def hq_branch_id(session_client: httpx.AsyncClient, auth_token: str) -> int:
    resp = await session_client.get("/core/branches", headers=auth(auth_token))
    assert resp.status_code == 200
    branch = next((b for b in resp.json() if b["branch_code"] == "HQ"), None)
    assert branch is not None, "HQ branch not found"
    return branch["branch_id"]


@pytest_asyncio.fixture
async def hq_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    hq_branch_id: int,
) -> int:
    """Return an existing HQ driver or create one for per-branch filter tests."""
    resp = await session_client.get(
        "/core/drivers",
        params={"branch_id": hq_branch_id},
        headers=auth(auth_token),
    )
    assert resp.status_code == 200
    drivers = resp.json()
    if drivers:
        return drivers[0]["driver_id"]
    new_d = await session_client.post(
        "/core/drivers",
        json={"branch_id": hq_branch_id, "full_name": "CP6 HQ Driver", "driver_code": "CP6HQ01"},
        headers=auth(auth_token),
    )
    assert new_d.status_code == 201, f"Create HQ driver: {new_d.text}"
    return new_d.json()["driver_id"]


async def _force_cancel_locked_periods(direct_db, branch_id: int) -> None:
    """Cancel Locked/Archived periods by temporarily disabling immutability triggers."""
    await direct_db.execute(
        _text("ALTER TABLE payroll.payrollfinallines DISABLE TRIGGER trg_final_line_immutable")
    )
    await direct_db.execute(
        _text("ALTER TABLE payroll.payrollperiods DISABLE TRIGGER trg_period_status_revert")
    )
    await direct_db.execute(
        _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' "
              "WHERE branchid = :bid AND status IN ('Locked', 'Archived')"),
        {"bid": branch_id},
    )
    await direct_db.execute(
        _text("ALTER TABLE payroll.payrollfinallines ENABLE TRIGGER trg_final_line_immutable")
    )
    await direct_db.execute(
        _text("ALTER TABLE payroll.payrollperiods ENABLE TRIGGER trg_period_status_revert")
    )


@pytest_asyncio.fixture
async def cp6_clean(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """Cancel any leftover active periods, yield branch_id, clean up after."""
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
    await _force_cancel_locked_periods(direct_db, paytest_branch_id)
    yield paytest_branch_id
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
    await _force_cancel_locked_periods(direct_db, paytest_branch_id)


# ---------------------------------------------------------------------------
# 1 & 2. Review list — what appears
# ---------------------------------------------------------------------------

class TestReviewList:

    @pytest.mark.asyncio
    async def test_inreview_period_appears_in_pending_list(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
    ):
        """A period submitted to InReview creates a Pending review item in the list."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        rv = await session_client.get(
            "/review/items",
            params={"status": "Pending"},
            headers=auth(auth_token),
        )
        assert rv.status_code == 200
        ids = [i["review_item_id"] for i in rv.json()]
        assert review_id in ids

    @pytest.mark.asyncio
    async def test_open_period_not_in_pending_list(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
    ):
        """An Open period has no Pending review item."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean)

        rv = await session_client.get(
            "/review/items",
            params={"status": "Pending"},
            headers=auth(auth_token),
        )
        assert rv.status_code == 200
        period_ids = {
            int(i["entity_id"])
            for i in rv.json()
            if i.get("entity_name") == "PayrollPeriods" and i.get("entity_id")
        }
        assert pid not in period_ids, "Open period should have no Pending review item"

    @pytest.mark.asyncio
    async def test_payroll_periods_endpoint_shows_inreview_status(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
    ):
        """GET /payroll/periods?status=InReview returns the period for the review page."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean)
        await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        periods = await session_client.get(
            "/payroll/periods",
            params={"status": "InReview", "branch_id": cp6_clean},
            headers=auth(auth_token),
        )
        assert periods.status_code == 200
        pids = [p["payroll_period_id"] for p in periods.json()]
        assert pid in pids

    @pytest.mark.asyncio
    async def test_open_draft_locked_not_in_inreview_filter(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
    ):
        """GET /payroll/periods?status=InReview does not include Open/Draft periods."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean)

        periods = await session_client.get(
            "/payroll/periods",
            params={"status": "InReview", "branch_id": cp6_clean},
            headers=auth(auth_token),
        )
        assert periods.status_code == 200
        pids = [p["payroll_period_id"] for p in periods.json()]
        assert pid not in pids, "Open period must not appear in InReview filter"


# ---------------------------------------------------------------------------
# 3 & 4. Approve workflow
# ---------------------------------------------------------------------------

class TestReviewApprove:

    @pytest.mark.asyncio
    async def test_reviewer_can_approve_clean_period(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
    ):
        """Operational user approves InReview → period status becomes Approved."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        dec = await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        assert dec.status_code == 200, f"Approve failed: {dec.text}"
        assert dec.json()["status"] == "Approved"

        # Period must be Approved
        period_resp = await session_client.get(
            f"/payroll/periods/{pid}", headers=auth(auth_token)
        )
        assert period_resp.status_code == 200
        assert period_resp.json()["status"] == "Approved"

    @pytest.mark.asyncio
    async def test_approved_period_disappears_from_pending_review(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
    ):
        """After approval the review item is no longer Pending."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )

        rv = await session_client.get(
            "/review/items",
            params={"status": "Pending"},
            headers=auth(auth_token),
        )
        pending_ids = [i["review_item_id"] for i in rv.json()]
        assert review_id not in pending_ids

    @pytest.mark.asyncio
    async def test_approved_period_can_be_finalized(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        direct_db,
    ):
        """After review approval the period can be finalized (existing finalize flow).

        Uses a fresh one-off driver so the resulting Locked period does NOT
        affect the paytest_driver_id used by other test suites' pay-rule tests.
        """
        headers = auth(auth_token)

        # Create a fresh one-off driver for this test only
        drv = await session_client.post(
            "/core/drivers",
            json={
                "branch_id": cp6_clean,
                "full_name": "CP6 Finalize Driver",
                "driver_code": "CP6-FIN-001",
            },
            headers=headers,
        )
        assert drv.status_code == 201, f"Create driver: {drv.text}"
        fresh_driver_id = drv.json()["driver_id"]

        pid = await _create_open_period(session_client, auth_token, cp6_clean)
        await _advance_to_approved_via_review(
            session_client, auth_token, pid, fresh_driver_id
        )

        fin = await session_client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=headers,
        )
        assert fin.status_code == 200, f"Finalize failed: {fin.text}"
        assert fin.json()["status"] == "Locked"

        # Cleanup: period is now Locked; bypass immutability triggers to cancel.
        await _force_cancel_locked_periods(direct_db, cp6_clean)


# ---------------------------------------------------------------------------
# 6. NMR blocker
# ---------------------------------------------------------------------------

class TestReviewNMRBlocker:

    @pytest.mark.asyncio
    async def test_approve_blocked_when_nmr_lines_exist(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """Period with NeedsManagerReview lines → approve returns 422."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean)

        # Add a line before InReview
        lr = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": WORK_DATE,
                  "line_type": "PTO_STATUS", "quantity": 1},
            headers=auth(auth_token),
        )
        assert lr.status_code == 201
        line_id = lr.json()["draft_line_id"]

        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        # Force NMR=True on the line (as if a rate was voided after submission)
        await direct_db.execute(
            _text("UPDATE payroll.payrolldraftlines SET needsmanagerreview = TRUE "
                  "WHERE draftlineid = :lid"),
            {"lid": line_id},
        )

        dec = await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        assert dec.status_code == 422, f"Expected 422 (NMR blocker), got {dec.status_code}"
        assert "manager review" in dec.json()["detail"].lower()

        # Period must still be InReview
        period_resp = await session_client.get(
            f"/payroll/periods/{pid}", headers=auth(auth_token)
        )
        assert period_resp.json()["status"] == "InReview"

    @pytest.mark.asyncio
    async def test_approve_succeeds_when_no_nmr_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
    ):
        """Period with no NMR lines → approve succeeds."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )
        dec = await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        assert dec.status_code == 200
        assert dec.json()["status"] == "Approved"


# ---------------------------------------------------------------------------
# 7. Return / reject
# ---------------------------------------------------------------------------

class TestReviewReturn:

    @pytest.mark.asyncio
    async def test_edit_requested_returns_period_to_open(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
    ):
        """EditRequested decision → period transitions from InReview to Open."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        dec = await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "EditRequested", "decision_reason": "Missing data"},
            headers=auth(auth_token),
        )
        assert dec.status_code == 200, f"EditRequested failed: {dec.text}"

        period_resp = await session_client.get(
            f"/payroll/periods/{pid}", headers=auth(auth_token)
        )
        assert period_resp.json()["status"] == "Open"

    @pytest.mark.asyncio
    async def test_rejected_returns_period_to_open(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
    ):
        """Rejected decision → period transitions from InReview to Open."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        dec = await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "Rejected", "decision_reason": "Period is incorrect"},
            headers=auth(auth_token),
        )
        assert dec.status_code == 200, f"Rejected failed: {dec.text}"

        period_resp = await session_client.get(
            f"/payroll/periods/{pid}", headers=auth(auth_token)
        )
        assert period_resp.json()["status"] == "Open"

    @pytest.mark.asyncio
    async def test_return_blocked_by_nmr_still_allowed(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """EditRequested is NOT blocked by NMR lines — reviewer can always return."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean)
        lr = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": WORK_DATE,
                  "line_type": "PTO_STATUS", "quantity": 1},
            headers=auth(auth_token),
        )
        line_id = lr.json()["draft_line_id"]
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )
        # Force NMR=True
        await direct_db.execute(
            _text("UPDATE payroll.payrolldraftlines SET needsmanagerreview = TRUE "
                  "WHERE draftlineid = :lid"),
            {"lid": line_id},
        )

        dec = await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "EditRequested", "decision_reason": "NMR line present"},
            headers=auth(auth_token),
        )
        assert dec.status_code == 200, f"Return should succeed even with NMR: {dec.text}"
        period_resp = await session_client.get(
            f"/payroll/periods/{pid}", headers=auth(auth_token)
        )
        assert period_resp.json()["status"] == "Open"


# ---------------------------------------------------------------------------
# 8. Driver/ODA security
# ---------------------------------------------------------------------------

class TestReviewODASecurity:

    @pytest.mark.asyncio
    async def test_oda_user_blocked_from_review_list(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """ODA (driver-role) user receives 403 on GET /review/items."""
        role_id = await _create_role_with_perms(
            session_client, auth_token,
            "CP6_ODA_Review_Role",
            ["payroll.view", "review.decide"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token,
            "cp6_oda_review_user",
            role_id,
            scope_type="OwnDriverDataOnly",
            branch_id=paytest_branch_id,
        )

        rv = await session_client.get(
            "/review/items",
            params={"status": "Pending"},
            headers=auth(oda_token),
        )
        assert rv.status_code == 403

    @pytest.mark.asyncio
    async def test_oda_user_blocked_from_review_decide(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        cp6_clean: int,
        paytest_driver_id: int,
    ):
        """ODA user cannot decide (approve/reject) a review item — 403 before data access."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        role_id = await _create_role_with_perms(
            session_client, auth_token,
            "CP6_ODA_Decide_Role",
            ["payroll.view", "review.decide"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token,
            "cp6_oda_decide_user",
            role_id,
            scope_type="OwnDriverDataOnly",
            branch_id=paytest_branch_id,
        )

        dec = await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "Approved"},
            headers=auth(oda_token),
        )
        assert dec.status_code == 403

        # Period must still be InReview
        period_resp = await session_client.get(
            f"/payroll/periods/{pid}", headers=auth(auth_token)
        )
        assert period_resp.json()["status"] == "InReview"


# ---------------------------------------------------------------------------
# 9 & 10. Branch scoping
# ---------------------------------------------------------------------------

class TestReviewBranchScope:

    @pytest.mark.asyncio
    async def test_specific_branch_user_cannot_decide_other_branch_item(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        hq_branch_id: int,
        cp6_clean: int,
        paytest_driver_id: int,
    ):
        """SpecificBranch user scoped to HQ cannot approve a PAYTEST period."""
        # Create and submit period on PAYTEST
        pid = await _create_open_period(session_client, auth_token, cp6_clean)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        # Create a user scoped ONLY to HQ (not PAYTEST)
        role_id = await _create_role_with_perms(
            session_client, auth_token,
            "CP6_HQ_Only_Role",
            ["payroll.view", "payroll.entry", "review.decide"],
        )
        hq_only_token = await _create_user_with_role(
            session_client, auth_token,
            "cp6_hq_only_user",
            role_id,
            scope_type="SpecificBranch",
            branch_id=hq_branch_id,
        )

        dec = await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "Approved"},
            headers=auth(hq_only_token),
        )
        assert dec.status_code == 403, (
            f"Cross-branch approval must be 403, got {dec.status_code}"
        )

    @pytest.mark.asyncio
    async def test_all_company_branches_user_can_approve_any_branch(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
    ):
        """AllCompanyBranches reviewer can approve a period on any branch."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        # admin has AllCompanyBranches scope
        dec = await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        assert dec.status_code == 200

    @pytest.mark.asyncio
    async def test_specific_branch_user_can_see_own_branch_items(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        cp6_clean: int,
        paytest_driver_id: int,
    ):
        """SpecificBranch user scoped to PAYTEST CAN see PAYTEST review items."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        role_id = await _create_role_with_perms(
            session_client, auth_token,
            "CP6_PAYTEST_Reviewer_Role",
            ["payroll.view", "payroll.entry", "review.decide"],
        )
        paytest_token = await _create_user_with_role(
            session_client, auth_token,
            "cp6_paytest_reviewer",
            role_id,
            scope_type="SpecificBranch",
            branch_id=paytest_branch_id,
        )

        rv = await session_client.get(
            "/review/items",
            params={"status": "Pending"},
            headers=auth(paytest_token),
        )
        assert rv.status_code == 200
        ids = [i["review_item_id"] for i in rv.json()]
        assert review_id in ids


# ---------------------------------------------------------------------------
# 11. No manual Min/Max/Adjustment controls implied
# ---------------------------------------------------------------------------

class TestReviewNoManualAdjustment:

    @pytest.mark.asyncio
    async def test_approve_does_not_create_sys_adjustments(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """Review approval does not insert any SYS_MIN_TOPUP/SYS_MAX_CAP lines.
        Those are only created by finalize_period.  This confirms review does not
        trigger or expose any manual adjustment UI/logic.

        Note: period is left in Approved (not finalized) to avoid creating a
        Locked period on paytest_driver_id that could block pay-rule tests."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean)
        await _advance_to_approved_via_review(
            session_client, auth_token, pid, paytest_driver_id
        )

        # No final lines should exist (finalize not called)
        agg = await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )
        assert agg.scalar_one() == 0, "Review approval must not insert final lines"

        # No SYS lines in draft (review does not add adjustments)
        sys_agg = await direct_db.execute(
            _text("""
                SELECT COUNT(*)
                FROM payroll.payrolldraftlines
                WHERE payrollperiodid = :pid
                  AND linetype LIKE 'SYS_%'
            """),
            {"pid": pid},
        )
        assert sys_agg.scalar_one() == 0, "Review must not add SYS_* draft lines"


# ---------------------------------------------------------------------------
# P1 — per-branch permission filtering: mixed-branch user sees only permitted branches
# ---------------------------------------------------------------------------

class TestReviewPerBranchPermissionFilter:
    """
    A user with payroll.view permission on PAYTEST (Branch A) only must NOT see
    review items for HQ (Branch B).  The permitted_branches filter in
    get_review_items must scope the DB query to only the branches the user
    actually has permission on, not all accessible branches.
    """

    @pytest.mark.asyncio
    async def test_single_branch_permission_does_not_leak_other_branch_items(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        hq_branch_id: int,
        direct_db,
    ):
        """
        User has payroll.view on PAYTEST (SpecificBranch scope) only.
        HQ has an InReview period; PAYTEST has an InReview period.
        The user must see the PAYTEST review item and must NOT see the HQ item.
        """
        # Create an InReview period on HQ (Branch B, no permission for this user)
        # Use direct status patch — no need to add lines since HQ may not have
        # PTO_STATUS activated; we only need the review item to exist.
        await _cancel_active_periods(session_client, auth_token, hq_branch_id)
        hq_pid = await _create_open_period(
            session_client, auth_token, hq_branch_id,
            "2095-07-07", "2095-07-13",
        )
        # Inject a DailyStatus line directly (informational; no pay item activation needed)
        hq_driver_resp = await session_client.get(
            "/core/drivers",
            params={"branch_id": hq_branch_id},
            headers=auth(auth_token),
        )
        assert hq_driver_resp.status_code == 200
        hq_drivers = hq_driver_resp.json()
        if not hq_drivers:
            new_d = await session_client.post(
                "/core/drivers",
                json={"branch_id": hq_branch_id, "full_name": "CP6 HQ Temp", "driver_code": "CP6HQT"},
                headers=auth(auth_token),
            )
            assert new_d.status_code == 201, f"Create HQ driver: {new_d.text}"
            hq_driver_for_line = new_d.json()["driver_id"]
        else:
            hq_driver_for_line = hq_drivers[0]["driver_id"]

        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverid,
                     workdate, linetype, linescope, quantity, sourcetype,
                     status, needsmanagerreview, addedbyuserid)
                VALUES
                    ((SELECT companyid FROM core.branches WHERE branchid = :bid),
                     :bid, :pid, :did,
                     '2095-07-08', 'DailyStatus', 'Daily', 0, 'Manual', 'Active', FALSE,
                     (SELECT userid FROM sec.users WHERE username = 'admin' LIMIT 1))
            """),
            {"bid": hq_branch_id, "pid": hq_pid, "did": hq_driver_for_line},
        )
        hq_inreview = await session_client.patch(
            f"/payroll/periods/{hq_pid}/status",
            json={"status": "InReview"},
            headers=auth(auth_token),
        )
        assert hq_inreview.status_code == 200, f"HQ period to InReview failed: {hq_inreview.text}"

        # Create an InReview period on PAYTEST (Branch A, permitted)
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        pt_pid = await _create_open_period(
            session_client, auth_token, paytest_branch_id,
            "2095-07-07", "2095-07-13",
        )
        await _advance_to_inreview(
            session_client, auth_token, pt_pid, paytest_driver_id, "2095-07-08"
        )

        # Create a role with payroll.view only on PAYTEST
        view_role_id = await _create_role_with_perms(
            session_client, auth_token,
            "CP6_VIEW_PAYTEST_ONLY",
            ["payroll.view"],
        )

        # Create user with SpecificBranch=PAYTEST scope
        tok = await _create_user_with_role(
            session_client, auth_token,
            f"cp6_view_pt_{hq_pid}",
            view_role_id,
            scope_type="SpecificBranch",
            branch_id=paytest_branch_id,
        )

        # GET /review/items — must return only PAYTEST items, not HQ items
        rv = await session_client.get("/review/items", headers=auth(tok))
        assert rv.status_code == 200, f"Permitted user must be able to list review items: {rv.text}"

        item_branch_ids = {i["branch_id"] for i in rv.json()}

        # Primary assertion: HQ items must NOT leak to a PAYTEST-only user
        assert hq_branch_id not in item_branch_ids, (
            f"User with permission on PAYTEST only must not see HQ review items; "
            f"got branch_ids={item_branch_ids}"
        )

        # Secondary assertion: PAYTEST items must be visible (test setup created one)
        assert paytest_branch_id in item_branch_ids, (
            f"User with permission on PAYTEST must see PAYTEST review items; "
            f"got branch_ids={item_branch_ids}"
        )

        # Cleanup
        await _cancel_active_periods(session_client, auth_token, hq_branch_id)
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
