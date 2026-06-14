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
import pytest
import pytest_asyncio
import httpx


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_active_periods(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
) -> None:
    """
    Cancel every Draft / Open / InReview / Approved period on `branch_id`.
    Silently skips statuses that produce no results or periods that are
    already in a terminal state.
    """
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


# ---------------------------------------------------------------------------
# Function-scoped fixtures for PAYTEST branch isolation
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def paytest_clean(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
):
    """
    Cancels any active periods on PAYTEST before the test (safety net),
    yields the PAYTEST branch_id, then cancels again after the test.
    This guarantees every test that uses it starts and ends with a clean
    Draft/Open slate on PAYTEST.
    """
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
    yield paytest_branch_id
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)


@pytest_asyncio.fixture
async def fresh_period(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_clean: int,          # int = PAYTEST branch_id; also handles cleanup
) -> dict:
    """
    Creates one Draft period on PAYTEST and yields its response dict.
    Cleanup is handled by paytest_clean (which runs after this fixture tears down).
    """
    resp = await session_client.post(
        "/payroll/periods",
        json={
            "branch_id": paytest_clean,
            "period_type": "Week",
            "start_date": "2030-01-06",
            "end_date":   "2030-01-12",
        },
        headers=auth(auth_token),
    )
    assert resp.status_code == 201, f"fresh_period setup failed: {resp.text}"
    return resp.json()


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
        paytest_clean: int,
    ):
        resp = await client.post(
            "/payroll/periods",
            json={
                "branch_id":   paytest_clean,
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
        paytest_clean: int,
    ):
        resp = await client.post(
            "/payroll/periods",
            json={
                "branch_id":   paytest_clean,
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
        paytest_clean: int,
    ):
        resp = await client.post(
            "/payroll/periods",
            json={
                "branch_id":   paytest_clean,
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
        paytest_clean: int,
    ):
        resp = await client.post(
            "/payroll/periods",
            json={
                "branch_id":   paytest_clean,
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
        paytest_clean: int,
    ):
        resp = await client.post(
            "/payroll/periods",
            json={
                "branch_id":   paytest_clean,
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
        assert resp.status_code == 422

    async def test_response_includes_branch_name(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_clean: int,
    ):
        resp = await client.post(
            "/payroll/periods",
            json={"branch_id": paytest_clean, "period_type": "Week",
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
        pid = fresh_period["payroll_period_id"]
        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Open"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "Open"

    async def test_open_to_inreview(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_period: dict,
        paytest_driver_id: int,
    ):
        pid = fresh_period["payroll_period_id"]
        headers = auth(auth_token)
        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "Open"}, headers=headers)
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
        fresh_period: dict,
        paytest_driver_id: int,
    ):
        pid = fresh_period["payroll_period_id"]
        headers = auth(auth_token)

        # Draft → Open
        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Open"},
            headers=headers,
        )
        assert resp.status_code == 200, f"Failed at Open: {resp.text}"
        assert resp.json()["status"] == "Open"

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
        fresh_period: dict,
    ):
        pid = fresh_period["payroll_period_id"]
        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "Open"}, headers=auth(auth_token))
        resp = await client.patch(f"/payroll/periods/{pid}/status",
                                  json={"status": "Cancelled"}, headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json()["status"] == "Cancelled"

    async def test_send_back_inreview_to_open(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_period: dict,
        paytest_driver_id: int,
    ):
        pid = fresh_period["payroll_period_id"]
        headers = auth(auth_token)
        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "Open"}, headers=headers)
        # Add a line so InReview is not blocked by empty-period guard
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
        assert resp.status_code == 200
        assert resp.json()["status"] == "Open"

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
        fresh_period: dict,
        paytest_driver_id: int,
    ):
        """Approved → Locked must go through /finalize, not this endpoint."""
        pid = fresh_period["payroll_period_id"]
        headers = auth(auth_token)
        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "Open"}, headers=headers)
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
        fresh_period: dict,
    ):
        pid = fresh_period["payroll_period_id"]
        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Open", "notes": "Ready for data entry"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["notes"] == "Ready for data entry"

    async def test_approved_status_reflected_in_get(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_period: dict,
        paytest_driver_id: int,
    ):
        pid = fresh_period["payroll_period_id"]
        headers = auth(auth_token)
        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "Open"}, headers=headers)
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
        fresh_period: dict,
    ):
        """Advancing a period to Open must not raise — audit write is in-transaction."""
        pid = fresh_period["payroll_period_id"]
        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Open"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        # Idempotent re-read to confirm the change persisted
        get = await client.get(f"/payroll/periods/{pid}", headers=auth(auth_token))
        assert get.json()["status"] == "Open"


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
        fresh_period: dict,
        paytest_driver_id: int,
    ):
        """PATCH InReview → Approved returns 422 — removed from valid transitions."""
        pid = fresh_period["payroll_period_id"]
        headers = auth(auth_token)
        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "Open"}, headers=headers)
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

    async def test_duplicate_pending_item_blocks_open_to_inreview(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_period: dict,
        paytest_driver_id: int,
    ):
        """Open → InReview is blocked when a Pending review item already exists."""
        pid = fresh_period["payroll_period_id"]
        headers = auth(auth_token)
        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "Open"}, headers=headers)
        await client.post(
            f"/payroll/periods/{pid}/lines", headers=headers,
            json={"driver_id": paytest_driver_id, "work_date": "2030-01-07",
                  "line_type": "PTO_STATUS", "quantity": 1},
        )
        # First submit → creates a Pending review item
        r1 = await client.patch(f"/payroll/periods/{pid}/status",
                                json={"status": "InReview"}, headers=headers)
        assert r1.status_code == 200

        # Return to Open manually
        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "Open"}, headers=headers)

        # Second submit → blocked by duplicate Pending item
        r2 = await client.patch(f"/payroll/periods/{pid}/status",
                                json={"status": "InReview"}, headers=headers)
        assert r2.status_code == 422
        assert "pending" in r2.json()["detail"].lower()
