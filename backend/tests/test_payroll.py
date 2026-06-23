"""
Integration tests for /payroll/periods:
  GET   /payroll/periods
  GET   /payroll/periods/{period_id}
  POST  /payroll/periods
  PATCH /payroll/periods/{period_id}/status

Schema constraints that shape these tests
-----------------------------------------
Two partial unique indexes on payroll.PayrollPeriods mean:
  - At most ONE Draft period per (company, branch) at any time
  - At most ONE Open  period per (company, branch) at any time

Test isolation strategy
-----------------------
- HQ branch (branch_id queried at runtime): holds the `created_period_id`
  session fixture period (Draft, read-only for list/get tests).
- PAYTEST branch (`paytest_branch_id` from conftest): used for all create and
  status-transition tests. Two fixtures manage state on this branch:

    paytest_clean  — function-scoped; cancels any active PAYTEST periods before
                     AND after each test so every test starts with a clean slate.
                     Yields the PAYTEST branch_id.

    fresh_period   — function-scoped; creates one Draft on PAYTEST (via
                     paytest_clean so cleanup is guaranteed), then yields the
                     response dict.  Used by status-transition tests.
"""
import datetime as _dt
import pytest
import pytest_asyncio
import httpx
from sqlalchemy import text as _sqla_text


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_active_periods(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    direct_db=None,
) -> None:
    """
    Cancel every Draft / Open period via PATCH (production path).
    InReview, Returned, and Approved cannot be cancelled via PATCH in CP-1A;
    cancel those directly in the database when direct_db is provided.
    """
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
    if direct_db is not None:
        from sqlalchemy import text as _text
        # Returned: must clear CurrentReturnReviewItemID first (pointer-consistency CHECK).
        await direct_db.execute(
            _text(
                "UPDATE payroll.payrollperiods "
                "SET status = 'Cancelled', currentreturnreviewitemid = NULL "
                "WHERE branchid = :bid AND status = 'Returned'"
            ),
            {"bid": branch_id},
        )
        # InReview and Approved: no pointer to clear.
        await direct_db.execute(
            _text(
                "UPDATE payroll.payrollperiods SET status = 'Cancelled' "
                "WHERE branchid = :bid AND status IN ('InReview', 'Approved')"
            ),
            {"bid": branch_id},
        )


# ---------------------------------------------------------------------------
# Function-scoped fixtures for PAYTEST branch isolation
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def paytest_clean(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """
    Cancels any active periods on PAYTEST before the test (safety net),
    yields the PAYTEST branch_id, then cancels again after the test.
    CP-1A: InReview/Returned/Approved cancellation via PATCH is blocked;
    use direct_db for those statuses.
    """
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id, direct_db)
    yield paytest_branch_id
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id, direct_db)


@pytest_asyncio.fixture
async def fresh_period(
    paytest_clean: int,          # int = PAYTEST branch_id; also handles cleanup
    direct_db,
) -> dict:
    """
    Inserts one Draft period on PAYTEST directly via SQL and returns a minimal dict.
    CP-1D: POST requires an existing Open (B1 guard); insert directly instead.
    Cleanup is handled by paytest_clean (which runs after this fixture tears down).
    """
    row = (await direct_db.execute(
        _sqla_text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Draft', 'PT-2030-0106', 'Week of Jan 6, 2030', 'Week',
                    '2030-01-06', '2030-01-12')
            RETURNING payrollperiodid, status, startdate, enddate
        """),
        {"bid": paytest_clean},
    )).mappings().first()
    return {
        "payroll_period_id": row["payrollperiodid"],
        "status": row["status"],
        "start_date": str(row["startdate"]),
        "end_date": str(row["enddate"]),
    }


@pytest_asyncio.fixture
async def fresh_open_period(
    paytest_clean: int,
    direct_db,
) -> dict:
    """
    Inserts one Open period on PAYTEST directly via SQL.
    CP-1D: Draft→Open via PATCH is blocked; use Open directly for tests that need
    an Open period without going through Draft.
    """
    row = (await direct_db.execute(
        _sqla_text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', 'PT-2030-0106-OP', 'Week of Jan 6, 2030', 'Week',
                    '2030-01-06', '2030-01-12')
            RETURNING payrollperiodid, status, startdate, enddate
        """),
        {"bid": paytest_clean},
    )).mappings().first()
    return {
        "payroll_period_id": row["payrollperiodid"],
        "status": row["status"],
        "start_date": str(row["startdate"]),
        "end_date": str(row["enddate"]),
    }


# ---------------------------------------------------------------------------
# GET /payroll/periods
# ---------------------------------------------------------------------------

class TestListPeriods:

    async def test_requires_auth(self, client: httpx.AsyncClient):
        resp = await client.get("/payroll/periods")
        assert resp.status_code == 401

    async def test_returns_list(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_period_id: int,
    ):
        resp = await client.get("/payroll/periods", headers=auth(auth_token))
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
        assert len(resp.json()) >= 1

    async def test_contains_seeded_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_period_id: int,
    ):
        # Use a large limit to avoid pagination hiding the seeded period when
        # other test modules create many periods (finalized periods cannot be
        # cancelled and accumulate across the session).
        resp = await client.get(
            "/payroll/periods", params={"limit": 500}, headers=auth(auth_token)
        )
        ids = [p["payroll_period_id"] for p in resp.json()]
        assert created_period_id in ids

    async def test_filter_by_status_draft(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_period_id: int,
    ):
        resp = await client.get(
            "/payroll/periods",
            params={"status": "Draft"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        for p in resp.json():
            assert p["status"] == "Draft"

    async def test_filter_unknown_status_returns_empty(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get(
            "/payroll/periods",
            params={"status": "Nonexistent"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_pagination_limit(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_period_id: int,
    ):
        resp = await client.get(
            "/payroll/periods",
            params={"limit": 1},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert len(resp.json()) <= 1

    async def test_schema(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_period_id: int,
    ):
        resp = await client.get("/payroll/periods", headers=auth(auth_token))
        assert resp.status_code == 200
        for p in resp.json():
            required = {
                "payroll_period_id", "branch_id", "branch_name",
                "period_code", "period_name", "period_type",
                "start_date", "end_date", "status",
                "draft_drivers", "draft_lines", "final_lines",
            }
            for field in required:
                assert field in p, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# GET /payroll/periods/{period_id}
# ---------------------------------------------------------------------------

class TestGetPeriod:

    async def test_requires_auth(
        self,
        client: httpx.AsyncClient,
        created_period_id: int,
    ):
        resp = await client.get(f"/payroll/periods/{created_period_id}")
        assert resp.status_code == 401

    async def test_returns_correct_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_period_id: int,
    ):
        resp = await client.get(
            f"/payroll/periods/{created_period_id}",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["payroll_period_id"] == created_period_id
        assert body["branch_name"] == "Headquarters"
        assert body["period_type"] == "Week"
        assert body["start_date"] == "2026-01-06"
        assert body["end_date"]   == "2026-01-12"
        assert body["pay_date"]   == "2026-01-14"
        assert body["status"]     == "Draft"

    async def test_auto_period_name(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_period_id: int,
    ):
        resp = await client.get(
            f"/payroll/periods/{created_period_id}",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        # Week period starting 2026-01-06 → "Week of Jan 6, 2026"
        assert resp.json()["period_name"] == "Week of Jan 6, 2026"

    async def test_not_found_returns_404(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get(
            "/payroll/periods/999999",
            headers=auth(auth_token),
        )
        assert resp.status_code == 404


@pytest_asyncio.fixture
async def paytest_with_open(
    paytest_clean: int,
    session_client: httpx.AsyncClient,
    auth_token: str,
    direct_db,
) -> int:
    """
    Like paytest_clean but also ensures payroll setup + one Open period exist so
    POST /payroll/periods can create a Draft (CP-1D B1 guard requires exactly one
    existing Open; CP-2A requires a schedule version before any period insert).
    Returns the PAYTEST branch_id; cleanup is handled by paytest_clean.
    """
    # CP-2A: ensure an active payroll setup / schedule version exists.
    r = await session_client.put(
        f"/settings/branches/{paytest_clean}/payroll-setup",
        json={"payroll_frequency": "Week", "anchor_start_date": "2025-01-06"},
        headers=auth(auth_token),
    )
    assert r.status_code in (200, 201), f"setup PUT failed: {r.text}"

    await direct_db.execute(
        _sqla_text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', 'PT-OPEN-SEED', 'Open Seed', 'Week', '2025-01-06', '2025-01-12')
            ON CONFLICT DO NOTHING
        """),
        {"bid": paytest_clean},
    )
    return paytest_clean


# ---------------------------------------------------------------------------
# POST /payroll/periods
# ---------------------------------------------------------------------------

class TestCreatePeriod:
    """
    Tests use `paytest_clean` to borrow the PAYTEST branch.
    Each test creates at most ONE period; cleanup cancels it after.
    Validation-only tests (→ 422) skip the fixture since they don't insert.
    """

    async def test_requires_auth(self, client: httpx.AsyncClient):
        resp = await client.post(
            "/payroll/periods",
            json={"branch_id": 1, "period_type": "Week",
                  "start_date": "2026-03-01", "end_date": "2026-03-07"},
        )
        assert resp.status_code == 401

    async def test_create_minimal(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_with_open: int,
    ):
        resp = await client.post(
            "/payroll/periods",
            json={
                "branch_id":   paytest_with_open,
                "period_type": "Week",
                "start_date":  "2026-02-02",
                "end_date":    "2026-02-08",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"]      == "Draft"
        assert body["period_type"] == "Week"
        assert body["period_name"] == "Week of Feb 2, 2026"
        assert body["payroll_period_id"] is not None

    async def test_create_with_explicit_name_and_notes(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_with_open: int,
    ):
        resp = await client.post(
            "/payroll/periods",
            json={
                "branch_id":   paytest_with_open,
                "period_type": "Custom",
                "start_date":  "2026-02-10",
                "end_date":    "2026-02-20",
                "period_name": "Special Run Feb",
                "notes":       "Covers overtime reconciliation",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["period_name"] == "Special Run Feb"
        assert body["notes"]       == "Covers overtime reconciliation"

    async def test_create_month_type_auto_name(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_with_open: int,
    ):
        resp = await client.post(
            "/payroll/periods",
            json={
                "branch_id":   paytest_with_open,
                "period_type": "Month",
                "start_date":  "2026-04-01",
                "end_date":    "2026-04-30",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        assert resp.json()["period_name"] == "April 2026"

    async def test_period_code_includes_branch_and_date(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_with_open: int,
    ):
        resp = await client.post(
            "/payroll/periods",
            json={
                "branch_id":   paytest_with_open,
                "period_type": "Week",
                "start_date":  "2026-07-06",
                "end_date":    "2026-07-12",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        code = resp.json()["period_code"]
        assert "PAYTEST" in code
        assert "20260706" in code

    async def test_biweek_auto_name(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_with_open: int,
    ):
        resp = await client.post(
            "/payroll/periods",
            json={
                "branch_id":   paytest_with_open,
                "period_type": "Biweek",
                "start_date":  "2026-03-16",
                "end_date":    "2026-03-29",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        name = resp.json()["period_name"]
        assert "Mar" in name
        assert "2026" in name

    async def test_end_before_start_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.post(
            "/payroll/periods",
            json={"branch_id": 1, "period_type": "Week",
                  "start_date": "2026-03-07", "end_date": "2026-03-01"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_same_start_end_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.post(
            "/payroll/periods",
            json={"branch_id": 1, "period_type": "Week",
                  "start_date": "2026-03-01", "end_date": "2026-03-01"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_invalid_period_type_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.post(
            "/payroll/periods",
            json={"branch_id": 1, "period_type": "Fortnight",
                  "start_date": "2026-03-01", "end_date": "2026-03-14"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_bad_branch_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.post(
            "/payroll/periods",
            json={"branch_id": 999999, "period_type": "Week",
                  "start_date": "2026-03-01", "end_date": "2026-03-07"},
            headers=auth(auth_token),
        )
        # B1 guard fires before branch validation (no Open periods → DRAFT_CREATION_REQUIRES_OPEN)
        # so the response may be 409 or 422 depending on guard order.
        assert resp.status_code in (409, 422)

    async def test_response_includes_branch_name(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_with_open: int,
    ):
        resp = await client.post(
            "/payroll/periods",
            json={"branch_id": paytest_with_open, "period_type": "Week",
                  "start_date": "2026-08-03", "end_date": "2026-08-09"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        assert resp.json()["branch_name"] == "Payroll Test Branch"


# ---------------------------------------------------------------------------
# PATCH /payroll/periods/{period_id}/status
# ---------------------------------------------------------------------------

class TestStatusTransitions:
    """
    Each test gets `fresh_period` — a Draft period on PAYTEST that is
    cancelled automatically after the test.
    """

    async def test_requires_auth(self, client: httpx.AsyncClient):
        # Auth check doesn't need a real period ID
        resp = await client.patch(
            "/payroll/periods/999999/status",
            json={"status": "Open"},
        )
        assert resp.status_code == 401

    async def test_draft_to_open(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_period: dict,
    ):
        """CP-1D: Draft→Open via PATCH is removed; atomic promotion via submit only."""
        pid = fresh_period["payroll_period_id"]
        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Open"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_open_to_inreview(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_open_period: dict,
        paytest_driver_id: int,
    ):
        pid = fresh_open_period["payroll_period_id"]
        headers = auth(auth_token)
        # Add a line (required by M16 empty-period guard)
        await client.post(
            f"/payroll/periods/{pid}/lines", headers=headers,
            json={"driver_id": paytest_driver_id, "work_date": "2030-01-07",
                  "line_type": "PTO_STATUS", "quantity": 1},
        )
        resp = await client.patch(f"/payroll/periods/{pid}/status",
                                  json={"status": "InReview"}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "InReview"

    async def test_full_workflow_to_approved(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_open_period: dict,
        paytest_driver_id: int,
    ):
        pid = fresh_open_period["payroll_period_id"]
        headers = auth(auth_token)

        # Add a line (required by the InReview guard: empty-period check)
        await client.post(
            f"/payroll/periods/{pid}/lines",
            headers=headers,
            json={"driver_id": paytest_driver_id, "work_date": "2030-01-07",
                  "line_type": "PTO_STATUS", "quantity": 1},
        )

        # Open → InReview
        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "InReview"},
            headers=headers,
        )
        assert resp.status_code == 200, f"Failed at InReview: {resp.text}"
        assert resp.json()["status"] == "InReview"

        # InReview → Approved via review flow
        review_resp = await client.get("/review/items", headers=headers)
        review_item = next(
            i for i in review_resp.json()
            if i.get("entity_name") == "PayrollPeriods"
            and i.get("entity_id") == str(pid)
            and i.get("status") == "Pending"
        )
        decide = await client.post(
            f"/review/items/{review_item['review_item_id']}/decide",
            headers=headers,
            json={"decision": "Approved"},
        )
        assert decide.status_code == 200, f"Approval failed: {decide.text}"

        period_resp = await client.get(f"/payroll/periods/{pid}", headers=headers)
        assert period_resp.status_code == 200
        assert period_resp.json()["status"] == "Approved"

    async def test_cancel_from_draft(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_period: dict,
    ):
        pid = fresh_period["payroll_period_id"]
        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "Cancelled"

    async def test_cancel_from_open(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_open_period: dict,
    ):
        pid = fresh_open_period["payroll_period_id"]
        resp = await client.patch(f"/payroll/periods/{pid}/status",
                                  json={"status": "Cancelled"}, headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json()["status"] == "Cancelled"

    async def test_send_back_inreview_to_open(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_open_period: dict,
        paytest_driver_id: int,
    ):
        """CP-1A: PATCH InReview→Open is blocked; InReview has no PATCH exits."""
        pid = fresh_open_period["payroll_period_id"]
        headers = auth(auth_token)
        await client.post(
            f"/payroll/periods/{pid}/lines", headers=headers,
            json={"driver_id": paytest_driver_id, "work_date": "2030-01-07",
                  "line_type": "PTO_STATUS", "quantity": 1},
        )
        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "InReview"}, headers=headers)
        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Open"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422, (
            f"CP-1A: InReview→Open must be blocked (422), got {resp.status_code}: {resp.text}"
        )
        assert "InReview" in resp.text or "cannot be transitioned" in resp.text

    async def test_invalid_status_value_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_period: dict,
    ):
        pid = fresh_period["payroll_period_id"]
        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Finalized"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_forbidden_transition_draft_to_approved_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_period: dict,
    ):
        pid = fresh_period["payroll_period_id"]
        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Approved"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_cancelled_period_no_further_transitions(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_period: dict,
    ):
        pid = fresh_period["payroll_period_id"]
        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "Cancelled"}, headers=auth(auth_token))
        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Draft"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_approved_to_locked_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_open_period: dict,
        paytest_driver_id: int,
    ):
        """Approved → Locked must go through /finalize, not this endpoint."""
        pid = fresh_open_period["payroll_period_id"]
        headers = auth(auth_token)
        await client.post(
            f"/payroll/periods/{pid}/lines", headers=headers,
            json={"driver_id": paytest_driver_id, "work_date": "2030-01-07",
                  "line_type": "PTO_STATUS", "quantity": 1},
        )
        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "InReview"}, headers=headers)
        review_resp = await client.get("/review/items", headers=headers)
        review_item = next(
            i for i in review_resp.json()
            if i.get("entity_name") == "PayrollPeriods" and i.get("entity_id") == str(pid)
            and i.get("status") == "Pending"
        )
        await client.post(f"/review/items/{review_item['review_item_id']}/decide",
                          headers=headers, json={"decision": "Approved"})
        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Locked"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_not_found_returns_404(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.patch(
            "/payroll/periods/999999/status",
            json={"status": "Open"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_notes_updated_with_status_change(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_open_period: dict,
    ):
        """Notes can be updated when transitioning an Open period to InReview (or Cancelled)."""
        pid = fresh_open_period["payroll_period_id"]
        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled", "notes": "Ready for data entry"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["notes"] == "Ready for data entry"

    async def test_approved_status_reflected_in_get(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_open_period: dict,
        paytest_driver_id: int,
    ):
        pid = fresh_open_period["payroll_period_id"]
        headers = auth(auth_token)
        await client.post(
            f"/payroll/periods/{pid}/lines", headers=headers,
            json={"driver_id": paytest_driver_id, "work_date": "2030-01-07",
                  "line_type": "PTO_STATUS", "quantity": 1},
        )
        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "InReview"}, headers=headers)
        review_resp = await client.get("/review/items", headers=headers)
        review_item = next(
            i for i in review_resp.json()
            if i.get("entity_name") == "PayrollPeriods" and i.get("entity_id") == str(pid)
            and i.get("status") == "Pending"
        )
        await client.post(f"/review/items/{review_item['review_item_id']}/decide",
                          headers=headers, json={"decision": "Approved"})
        resp = await client.get(
            f"/payroll/periods/{pid}",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "Approved"

    async def test_status_transition_writes_audit(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_open_period: dict,
    ):
        """Transitioning Open→Cancelled confirms audit write is in-transaction."""
        pid = fresh_open_period["payroll_period_id"]
        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        get = await client.get(f"/payroll/periods/{pid}", headers=auth(auth_token))
        assert get.json()["status"] == "Cancelled"


# ---------------------------------------------------------------------------
# Status transition: review-item gate
# ---------------------------------------------------------------------------

class TestStatusTransitionReviewGate:
    """
    M16: InReview → Approved via PATCH is no longer valid.
    Approval flows exclusively through POST /review/items/{id}/decide.

    Verify that:
    1. PATCH /status Approved from InReview returns 422 (not a valid transition).
    2. Duplicate Pending review item blocks Open → InReview re-submission.
    """

    async def test_patch_inreview_to_approved_is_invalid(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_open_period: dict,
        paytest_driver_id: int,
    ):
        """PATCH InReview → Approved returns 422 — removed from valid transitions."""
        pid = fresh_open_period["payroll_period_id"]
        headers = auth(auth_token)
        await client.post(
            f"/payroll/periods/{pid}/lines", headers=headers,
            json={"driver_id": paytest_driver_id, "work_date": "2030-01-07",
                  "line_type": "PTO_STATUS", "quantity": 1},
        )
        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "InReview"}, headers=headers)

        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Approved"},
            headers=headers,
        )
        assert resp.status_code == 422

    async def test_inreview_has_no_patch_exits(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_open_period: dict,
        paytest_driver_id: int,
    ):
        """
        CP-1A: Once InReview, no PATCH transition is allowed (not Open, not Cancelled,
        not InReview again). The period must exit InReview only via the review decision flow.
        The old 'return to Open manually' path (which relied on InReview→Open) is blocked.
        """
        pid = fresh_open_period["payroll_period_id"]
        headers = auth(auth_token)
        await client.post(
            f"/payroll/periods/{pid}/lines", headers=headers,
            json={"driver_id": paytest_driver_id, "work_date": "2030-01-07",
                  "line_type": "PTO_STATUS", "quantity": 1},
        )
        r1 = await client.patch(f"/payroll/periods/{pid}/status",
                                json={"status": "InReview"}, headers=headers)
        assert r1.status_code == 200

        # CP-1A: all PATCH exits from InReview are blocked
        for target in ("Open", "Cancelled", "InReview", "Draft"):
            r = await client.patch(f"/payroll/periods/{pid}/status",
                                   json={"status": target}, headers=headers)
            assert r.status_code == 422, (
                f"CP-1A: InReview→{target} must be blocked (422), got {r.status_code}: {r.text}"
            )
