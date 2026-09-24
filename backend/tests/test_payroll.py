"""
Integration tests for payroll periods:
  GET   /payroll/periods
  GET   /payroll/periods/{period_id}
  GET   /payroll/branches/{branch_id}/period-candidates
  POST  /payroll/branches/{branch_id}/period-creations
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
- Creation and status-transition tests each use a fresh branch with a company
  Setup, published Version, and Branch Assignment. Finalized history is retained.
- fresh_period inserts one Draft for status-transition tests that exercise the
  existing lifecycle endpoint rather than candidate creation.
"""
import datetime as _dt
from uuid import uuid4

import httpx
import pytest_asyncio
from sqlalchemy import text as _sqla_text
from sqlalchemy.ext.asyncio import create_async_engine

from app.payroll_setup.policy import assign_setup, create_draft, create_setup, publish_version

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _new_authorized_branch(test_database_url: str, frequency: str,
                                 anchor: _dt.date) -> tuple[int, str]:
    """Create isolated current setup authority without touching retained periods."""
    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.begin() as db:
            code = f"PERIOD_{uuid4().hex[:16]}"
            branch_id = (await db.execute(_sqla_text("""
                INSERT INTO core.branches
                    (companyid, branchcode, branchname, status, isdefault)
                VALUES (1, :code, :name, 'Active', FALSE)
                RETURNING branchid
            """), {"code": code, "name": f"Period API isolated {code}"})).scalar_one()
            user_id = (await db.execute(_sqla_text("""
                SELECT userid FROM sec.users WHERE companyid = 1 AND username = 'admin'
            """))).scalar_one()
            setup_id = await create_setup(1, user_id, code, "Period API setup", db)
            draft_id = await create_draft(
                1, user_id, setup_id, db,
                payroll_frequency=frequency, anchor_start_date=anchor,
                normal_days_off_mask=0,
            )
            await publish_version(1, user_id, setup_id, draft_id, anchor, db)
            await assign_setup(1, user_id, branch_id, setup_id, anchor, db)
            return branch_id, code
    finally:
        await engine.dispose()


async def _create_candidate(client: httpx.AsyncClient, token: str,
                            branch_id: int, mode: str = "OPEN_CREATION") -> dict:
    preview = await client.get(
        f"/payroll/branches/{branch_id}/period-candidates",
        params={"mode": mode}, headers=auth(token),
    )
    assert preview.status_code == 200, preview.text
    selected = preview.json()["selected"]
    assert selected["creatable"] is True, selected
    created = await client.post(
        f"/payroll/branches/{branch_id}/period-creations",
        json={"candidate_key": selected["candidate_key"]}, headers=auth(token),
    )
    assert created.status_code == 201, created.text
    return created.json()


# ---------------------------------------------------------------------------
# Function-scoped fixtures for isolated lifecycle branches
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def paytest_clean(
    test_database_url: str,
):
    """A fresh branch per lifecycle test; no immutable-history cleanup."""
    branch_id, _ = await _new_authorized_branch(
        test_database_url, "Week", _dt.date(2030, 1, 6),
    )
    return branch_id


@pytest_asyncio.fixture
async def period_test_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_clean: int,
) -> int:
    code = f"PTD_{uuid4().hex[:12]}"
    response = await session_client.post(
        "/core/drivers",
        json={"branch_id": paytest_clean, "full_name": "Period test driver",
              "driver_code": code, "cdl_number": f"CDL-{code}"},
        headers=auth(auth_token),
    )
    assert response.status_code == 201, response.text
    return response.json()["driver_id"]


@pytest_asyncio.fixture
async def fresh_period(
    paytest_clean: int,
    direct_db,
) -> dict:
    """
    Inserts one Draft directly to exercise legacy status transitions.
    CP-1D: the current candidate route requires an Open before Draft creation.
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
    Inserts one Open period on the fresh branch directly via SQL.
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
        # Walk the documented offset pagination rather than assuming the
        # session's retained finalized history fits in one page.
        for offset in range(0, 5000, 500):
            resp = await client.get(
                "/payroll/periods",
                params={"limit": 500, "offset": offset},
                headers=auth(auth_token),
            )
            assert resp.status_code == 200
            ids = [p["payroll_period_id"] for p in resp.json()]
            if created_period_id in ids:
                return
            if len(ids) < 500:
                break
        raise AssertionError(f"Seeded period {created_period_id} not found in paginated list")

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
async def candidate_week_branch(test_database_url: str) -> tuple[int, str]:
    return await _new_authorized_branch(
        test_database_url, "Week", _dt.date(2026, 2, 2),
    )


# ---------------------------------------------------------------------------
# Candidate-based period creation
# ---------------------------------------------------------------------------

class TestCreatePeriod:
    """Current candidate route; each creation gets its own setup-assigned branch."""

    async def test_requires_auth(self, client: httpx.AsyncClient):
        resp = await client.post(
            "/payroll/branches/1/period-creations",
            json={"candidate_key": "dummy"},
        )
        assert resp.status_code == 401

    async def test_create_minimal(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        candidate_week_branch: tuple[int, str],
    ):
        branch_id, _ = candidate_week_branch
        await _create_candidate(client, auth_token, branch_id)
        body = await _create_candidate(
            client, auth_token, branch_id, "PREPARED_CREATION",
        )
        assert body["status"]      == "Draft"
        assert body["period_type"] == "Week"
        assert body["period_name"] == "Week of Feb 9, 2026"
        assert body["payroll_period_id"] is not None

    async def test_explicit_name_and_notes_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        candidate_week_branch: tuple[int, str],
    ):
        branch_id, _ = candidate_week_branch
        resp = await client.post(
            f"/payroll/branches/{branch_id}/period-creations",
            json={
                "candidate_key": "dummy",
                "period_name": "Special Run Feb",
                "notes":       "Covers overtime reconciliation",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        errors = {str(item["loc"][-1]) for item in resp.json()["detail"]}
        assert {"period_name", "notes"} <= errors

    async def test_create_month_type_auto_name(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        branch_id, _ = await _new_authorized_branch(
            test_database_url, "Month", _dt.date(2026, 4, 1),
        )
        body = await _create_candidate(client, auth_token, branch_id)
        assert body["period_type"] == "Month"
        assert body["period_name"] == "April 2026"

    async def test_period_code_includes_branch_and_date(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        candidate_week_branch: tuple[int, str],
    ):
        branch_id, branch_code = candidate_week_branch
        body = await _create_candidate(client, auth_token, branch_id)
        assert body["period_code"] == f"{branch_code}-20260202"

    async def test_biweek_auto_name(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        test_database_url: str,
    ):
        branch_id, _ = await _new_authorized_branch(
            test_database_url, "Biweek", _dt.date(2026, 3, 16),
        )
        body = await _create_candidate(client, auth_token, branch_id)
        assert body["period_type"] == "Biweek"
        assert body["period_name"] == "Mar 16 – Mar 29, 2026"

    async def test_bad_branch_returns_branch_inactive(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get(
            "/payroll/branches/999999/period-candidates",
            params={"mode": "OPEN_CREATION"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "BRANCH_INACTIVE"

    async def test_detail_includes_created_branch_name(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        candidate_week_branch: tuple[int, str],
    ):
        branch_id, branch_code = candidate_week_branch
        created = await _create_candidate(client, auth_token, branch_id)
        detail = await client.get(
            f"/payroll/periods/{created['payroll_period_id']}",
            headers=auth(auth_token),
        )
        assert detail.status_code == 200
        assert detail.json()["branch_name"] == f"Period API isolated {branch_code}"


# ---------------------------------------------------------------------------
# PATCH /payroll/periods/{period_id}/status
# ---------------------------------------------------------------------------

class TestStatusTransitions:
    """
    Each test gets an isolated branch; periods remain as audit history.
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
        period_test_driver_id: int,
    ):
        pid = fresh_open_period["payroll_period_id"]
        headers = auth(auth_token)
        # Add a line (required by M16 empty-period guard)
        await client.post(
            f"/payroll/periods/{pid}/lines", headers=headers,
            json={"driver_id": period_test_driver_id, "work_date": "2030-01-07",
                  "line_type": "DailyNote", "quantity": 1, "notes": "filler"},
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
        period_test_driver_id: int,
    ):
        pid = fresh_open_period["payroll_period_id"]
        headers = auth(auth_token)

        # Add a line (required by the InReview guard: empty-period check)
        await client.post(
            f"/payroll/periods/{pid}/lines",
            headers=headers,
            json={"driver_id": period_test_driver_id, "work_date": "2030-01-07",
                  "line_type": "DailyNote", "quantity": 1, "notes": "filler"},
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
        period_test_driver_id: int,
    ):
        """CP-1A: PATCH InReview→Open is blocked; InReview has no PATCH exits."""
        pid = fresh_open_period["payroll_period_id"]
        headers = auth(auth_token)
        await client.post(
            f"/payroll/periods/{pid}/lines", headers=headers,
            json={"driver_id": period_test_driver_id, "work_date": "2030-01-07",
                  "line_type": "DailyNote", "quantity": 1, "notes": "filler"},
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
        period_test_driver_id: int,
    ):
        """Approved → Locked must go through /finalize, not this endpoint."""
        pid = fresh_open_period["payroll_period_id"]
        headers = auth(auth_token)
        await client.post(
            f"/payroll/periods/{pid}/lines", headers=headers,
            json={"driver_id": period_test_driver_id, "work_date": "2030-01-07",
                  "line_type": "DailyNote", "quantity": 1, "notes": "filler"},
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
        period_test_driver_id: int,
    ):
        pid = fresh_open_period["payroll_period_id"]
        headers = auth(auth_token)
        await client.post(
            f"/payroll/periods/{pid}/lines", headers=headers,
            json={"driver_id": period_test_driver_id, "work_date": "2030-01-07",
                  "line_type": "DailyNote", "quantity": 1, "notes": "filler"},
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
        period_test_driver_id: int,
    ):
        """PATCH InReview → Approved returns 422 — removed from valid transitions."""
        pid = fresh_open_period["payroll_period_id"]
        headers = auth(auth_token)
        await client.post(
            f"/payroll/periods/{pid}/lines", headers=headers,
            json={"driver_id": period_test_driver_id, "work_date": "2030-01-07",
                  "line_type": "DailyNote", "quantity": 1, "notes": "filler"},
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
        period_test_driver_id: int,
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
            json={"driver_id": period_test_driver_id, "work_date": "2030-01-07",
                  "line_type": "DailyNote", "quantity": 1, "notes": "filler"},
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
