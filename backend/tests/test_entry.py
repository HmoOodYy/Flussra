"""
Integration tests for draft-line (entry) endpoints:

  GET    /payroll/periods/{id}/lines
  GET    /payroll/periods/{id}/lines/summary
  POST   /payroll/periods/{id}/lines
  PATCH  /payroll/periods/{id}/lines/{line_id}
  DELETE /payroll/periods/{id}/lines/{line_id}

Test isolation
--------------
All mutable tests use the PAYTEST branch (via the `open_period` fixture).
`open_period` wraps `fresh_period` (from test_payroll.py / conftest) and
transitions the Draft period to Open so that entry is allowed.
`paytest_driver_id` (session-scoped, from conftest) gives us a valid driver
on the PAYTEST branch for every test that needs to POST a line.
"""
import pytest
import pytest_asyncio
import httpx


# ---------------------------------------------------------------------------
# Module-level helpers (copied pattern from test_payroll.py)
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_active_periods(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
) -> None:
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
# Function-scoped fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def paytest_clean(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
):
    """Cancel any active PAYTEST periods before and after the test."""
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
    yield paytest_branch_id
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)


@pytest_asyncio.fixture
async def fresh_period(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_clean: int,
) -> dict:
    """Create a Draft period on PAYTEST; return its response dict."""
    resp = await session_client.post(
        "/payroll/periods",
        json={
            "branch_id":   paytest_clean,
            "period_type": "Week",
            "start_date":  "2031-01-06",
            "end_date":    "2031-01-12",
        },
        headers=auth(auth_token),
    )
    assert resp.status_code == 201, f"fresh_period setup failed: {resp.text}"
    return resp.json()


@pytest_asyncio.fixture
async def open_period(
    session_client: httpx.AsyncClient,
    auth_token: str,
    fresh_period: dict,
) -> dict:
    """
    Transition the fresh Draft period to Open so entry is allowed.
    Returns the updated period dict.
    """
    pid = fresh_period["payroll_period_id"]
    resp = await session_client.patch(
        f"/payroll/periods/{pid}/status",
        json={"status": "Open"},
        headers=auth(auth_token),
    )
    assert resp.status_code == 200, f"open_period setup failed: {resp.text}"
    return resp.json()


def _line_payload(driver_id: int, **overrides) -> dict:
    """Return a minimal valid DraftLineCreate payload."""
    base = {
        "driver_id":  driver_id,
        "work_date":  "2031-01-07",
        "line_type":  "Hours",
        "quantity":   "8.00",
        "notes":      "Test line",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# GET /payroll/periods/{id}/lines
# ---------------------------------------------------------------------------

class TestListLines:

    async def test_requires_auth(self, client: httpx.AsyncClient, created_period_id: int):
        resp = await client.get(f"/payroll/periods/{created_period_id}/lines")
        assert resp.status_code == 401

    async def test_returns_empty_list_for_new_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
    ):
        pid = open_period["payroll_period_id"]
        resp = await client.get(
            f"/payroll/periods/{pid}/lines",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_404_for_unknown_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get(
            "/payroll/periods/999999/lines",
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_lists_added_line(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        paytest_driver_id: int,
    ):
        pid = open_period["payroll_period_id"]
        # Add one line first
        post_resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id),
            headers=auth(auth_token),
        )
        assert post_resp.status_code == 201

        resp = await client.get(
            f"/payroll/periods/{pid}/lines",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        lines = resp.json()
        assert len(lines) == 1
        assert lines[0]["line_type"] == "HOURS"
        assert lines[0]["driver_id"] == paytest_driver_id

    async def test_filter_by_driver_id(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        paytest_driver_id: int,
    ):
        pid = open_period["payroll_period_id"]
        await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id, line_type="Miles", quantity="100.00"),
            headers=auth(auth_token),
        )
        # Filter by the correct driver — should find it
        resp = await client.get(
            f"/payroll/periods/{pid}/lines",
            params={"driver_id": paytest_driver_id},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

        # Filter by a bogus driver — should return empty
        resp2 = await client.get(
            f"/payroll/periods/{pid}/lines",
            params={"driver_id": 999999},
            headers=auth(auth_token),
        )
        assert resp2.status_code == 200
        assert resp2.json() == []

    async def test_filter_by_line_status(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        paytest_driver_id: int,
    ):
        pid = open_period["payroll_period_id"]
        post_resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id, line_type="Loads", quantity="3.00"),
            headers=auth(auth_token),
        )
        assert post_resp.status_code == 201
        line_id = post_resp.json()["draft_line_id"]

        # Active filter should return the line
        resp = await client.get(
            f"/payroll/periods/{pid}/lines",
            params={"status": "Active"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        ids = [l["draft_line_id"] for l in resp.json()]
        assert line_id in ids

        # Void filter should not return it yet
        resp2 = await client.get(
            f"/payroll/periods/{pid}/lines",
            params={"status": "Void"},
            headers=auth(auth_token),
        )
        assert resp2.status_code == 200
        ids2 = [l["draft_line_id"] for l in resp2.json()]
        assert line_id not in ids2


# ---------------------------------------------------------------------------
# GET /payroll/periods/{id}/lines/summary
# ---------------------------------------------------------------------------

class TestLinesSummary:

    async def test_requires_auth(self, client: httpx.AsyncClient, created_period_id: int):
        resp = await client.get(f"/payroll/periods/{created_period_id}/lines/summary")
        assert resp.status_code == 401

    async def test_returns_empty_list_for_no_lines(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
    ):
        pid = open_period["payroll_period_id"]
        resp = await client.get(
            f"/payroll/periods/{pid}/lines/summary",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_summary_aggregates_correctly(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        paytest_driver_id: int,
    ):
        pid = open_period["payroll_period_id"]
        # Add two Hours lines on different dates (duplicate guard requires unique
        # business key: driver + date + line_type must not collide).
        for qty, wdate in (("8.00", "2031-01-07"), ("9.50", "2031-01-08")):
            await client.post(
                f"/payroll/periods/{pid}/lines",
                json=_line_payload(paytest_driver_id, quantity=qty, work_date=wdate),
                headers=auth(auth_token),
            )

        resp = await client.get(
            f"/payroll/periods/{pid}/lines/summary",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        row = rows[0]
        assert row["driver_id"] == paytest_driver_id
        assert row["line_type"] == "HOURS"
        assert row["line_count"] == 2
        # 8.00 + 9.50 = 17.50
        from decimal import Decimal
        assert Decimal(str(row["total_quantity"])) == Decimal("17.50")

    async def test_404_for_unknown_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get(
            "/payroll/periods/999999/lines/summary",
            headers=auth(auth_token),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /payroll/periods/{id}/lines
# ---------------------------------------------------------------------------

class TestAddLine:

    async def test_requires_auth(
        self,
        client: httpx.AsyncClient,
        open_period: dict,
        paytest_driver_id: int,
    ):
        pid = open_period["payroll_period_id"]
        resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id),
        )
        assert resp.status_code == 401

    async def test_add_line_success(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        paytest_driver_id: int,
    ):
        pid = open_period["payroll_period_id"]
        resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id, line_type="Miles", quantity="250.00"),
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["draft_line_id"] > 0
        assert body["line_type"] == "MILES"
        assert body["driver_id"] == paytest_driver_id
        assert body["status"] == "Active"
        assert body["period_id"] == pid

    async def test_add_line_all_optional_fields(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        paytest_driver_id: int,
    ):
        pid = open_period["payroll_period_id"]
        resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json={
                "driver_id":             paytest_driver_id,
                "work_date":             "2031-01-08",
                "line_type":             "Overnight",
                "quantity":              "1.00",
                "notes":                 "Stayed over",
                "source_type":           "Manual",
                "needs_manager_review":  True,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["needs_manager_review"] is True
        assert body["notes"] == "Stayed over"

    async def test_rejects_draft_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        fresh_period: dict,
        paytest_driver_id: int,
    ):
        """POST to a period still in Draft status must return 422."""
        pid = fresh_period["payroll_period_id"]
        resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id),
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_rejects_invalid_line_type(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        paytest_driver_id: int,
    ):
        pid = open_period["payroll_period_id"]
        resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id, line_type="Invalid"),
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_rejects_negative_quantity(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        paytest_driver_id: int,
    ):
        pid = open_period["payroll_period_id"]
        resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id, quantity="-1.00"),
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_rejects_driver_wrong_branch(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        created_driver_id: int,   # driver is on HQ branch, not PAYTEST
    ):
        pid = open_period["payroll_period_id"]
        resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(created_driver_id),
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "branch" in resp.json()["detail"].lower()

    async def test_404_for_unknown_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        resp = await client.post(
            "/payroll/periods/999999/lines",
            json=_line_payload(paytest_driver_id),
            headers=auth(auth_token),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /payroll/periods/{id}/lines/{line_id}
# ---------------------------------------------------------------------------

class TestUpdateLine:

    async def test_requires_auth(
        self,
        client: httpx.AsyncClient,
        open_period: dict,
        auth_token: str,
        paytest_driver_id: int,
    ):
        # First add a line so we have a real line_id
        pid = open_period["payroll_period_id"]
        post_resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id),
            headers=auth(auth_token),
        )
        lid = post_resp.json()["draft_line_id"]
        resp = await client.patch(
            f"/payroll/periods/{pid}/lines/{lid}",
            json={"quantity": "5.00"},
        )
        assert resp.status_code == 401

    async def test_update_quantity(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        paytest_driver_id: int,
    ):
        pid = open_period["payroll_period_id"]
        post_resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id, quantity="8.00"),
            headers=auth(auth_token),
        )
        assert post_resp.status_code == 201
        lid = post_resp.json()["draft_line_id"]

        resp = await client.patch(
            f"/payroll/periods/{pid}/lines/{lid}",
            json={"quantity": "10.50"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        from decimal import Decimal
        assert Decimal(str(body["quantity"])) == Decimal("10.50")

    async def test_update_notes_and_status(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        paytest_driver_id: int,
    ):
        pid = open_period["payroll_period_id"]
        post_resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id),
            headers=auth(auth_token),
        )
        lid = post_resp.json()["draft_line_id"]

        resp = await client.patch(
            f"/payroll/periods/{pid}/lines/{lid}",
            json={"notes": "Revised", "status": "NeedsReview"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["notes"] == "Revised"
        assert body["status"] == "NeedsReview"

    async def test_cannot_update_on_draft_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        fresh_period: dict,
        paytest_driver_id: int,
    ):
        """
        We add a line while the period is Open, then transition it back to
        Draft via cancel + new period — but it's simpler to test by checking
        the Approved/Locked/Cancelled scenario.  For Draft: we can't add a
        line while Draft anyway, so test the Cancelled scenario instead.
        """
        # Add a line to the open period
        pid = open_period["payroll_period_id"]
        post_resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id),
            headers=auth(auth_token),
        )
        lid = post_resp.json()["draft_line_id"]

        # Cancel the period → lines become frozen
        await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

        resp = await client.patch(
            f"/payroll/periods/{pid}/lines/{lid}",
            json={"quantity": "1.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "Cancelled" in resp.json()["detail"]

    async def test_rejects_negative_quantity(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        paytest_driver_id: int,
    ):
        pid = open_period["payroll_period_id"]
        post_resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id),
            headers=auth(auth_token),
        )
        lid = post_resp.json()["draft_line_id"]

        resp = await client.patch(
            f"/payroll/periods/{pid}/lines/{lid}",
            json={"quantity": "-5.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_rejects_invalid_status(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        paytest_driver_id: int,
    ):
        pid = open_period["payroll_period_id"]
        post_resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id),
            headers=auth(auth_token),
        )
        lid = post_resp.json()["draft_line_id"]

        resp = await client.patch(
            f"/payroll/periods/{pid}/lines/{lid}",
            json={"status": "BadStatus"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_404_unknown_line(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
    ):
        pid = open_period["payroll_period_id"]
        resp = await client.patch(
            f"/payroll/periods/{pid}/lines/999999",
            json={"quantity": "5.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_cannot_update_voided_line(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        paytest_driver_id: int,
    ):
        pid = open_period["payroll_period_id"]
        post_resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id),
            headers=auth(auth_token),
        )
        lid = post_resp.json()["draft_line_id"]

        # Void it
        await client.delete(
            f"/payroll/periods/{pid}/lines/{lid}",
            headers=auth(auth_token),
        )

        # Try to update a voided line
        resp = await client.patch(
            f"/payroll/periods/{pid}/lines/{lid}",
            json={"quantity": "5.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "void" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# DELETE /payroll/periods/{id}/lines/{line_id}
# ---------------------------------------------------------------------------

class TestVoidLine:

    async def test_requires_auth(
        self,
        client: httpx.AsyncClient,
        open_period: dict,
        auth_token: str,
        paytest_driver_id: int,
    ):
        pid = open_period["payroll_period_id"]
        post_resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id),
            headers=auth(auth_token),
        )
        lid = post_resp.json()["draft_line_id"]
        resp = await client.delete(f"/payroll/periods/{pid}/lines/{lid}")
        assert resp.status_code == 401

    async def test_void_returns_204(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        paytest_driver_id: int,
    ):
        pid = open_period["payroll_period_id"]
        post_resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id),
            headers=auth(auth_token),
        )
        lid = post_resp.json()["draft_line_id"]

        resp = await client.delete(
            f"/payroll/periods/{pid}/lines/{lid}",
            headers=auth(auth_token),
        )
        assert resp.status_code == 204

    async def test_void_sets_status(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        paytest_driver_id: int,
    ):
        pid = open_period["payroll_period_id"]
        post_resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id, line_type="Wait", quantity="2.00"),
            headers=auth(auth_token),
        )
        lid = post_resp.json()["draft_line_id"]

        await client.delete(
            f"/payroll/periods/{pid}/lines/{lid}",
            headers=auth(auth_token),
        )

        # Confirm status = Void via the list endpoint
        resp = await client.get(
            f"/payroll/periods/{pid}/lines",
            params={"status": "Void"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        ids = [l["draft_line_id"] for l in resp.json()]
        assert lid in ids

    async def test_void_is_idempotent(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        paytest_driver_id: int,
    ):
        pid = open_period["payroll_period_id"]
        post_resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id),
            headers=auth(auth_token),
        )
        lid = post_resp.json()["draft_line_id"]

        r1 = await client.delete(
            f"/payroll/periods/{pid}/lines/{lid}",
            headers=auth(auth_token),
        )
        r2 = await client.delete(
            f"/payroll/periods/{pid}/lines/{lid}",
            headers=auth(auth_token),
        )
        assert r1.status_code == 204
        assert r2.status_code == 204  # idempotent

    async def test_void_blocked_on_frozen_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        paytest_driver_id: int,
    ):
        pid = open_period["payroll_period_id"]
        post_resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id),
            headers=auth(auth_token),
        )
        lid = post_resp.json()["draft_line_id"]

        # Cancel the period
        await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

        resp = await client.delete(
            f"/payroll/periods/{pid}/lines/{lid}",
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_404_unknown_line(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
    ):
        pid = open_period["payroll_period_id"]
        resp = await client.delete(
            f"/payroll/periods/{pid}/lines/999999",
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_voided_line_excluded_from_summary(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        open_period: dict,
        paytest_driver_id: int,
    ):
        """Voided lines must not appear in the aggregated summary."""
        pid = open_period["payroll_period_id"]
        post_resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json=_line_payload(paytest_driver_id, line_type="PTO", quantity="8.00"),
            headers=auth(auth_token),
        )
        lid = post_resp.json()["draft_line_id"]

        # Void it
        await client.delete(
            f"/payroll/periods/{pid}/lines/{lid}",
            headers=auth(auth_token),
        )

        # Summary should be empty (no active lines)
        resp = await client.get(
            f"/payroll/periods/{pid}/lines/summary",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        # The voided line should not appear in the aggregated view
        rows = resp.json()
        assert all(r["line_type"] != "PTO" or r["line_count"] == 0 for r in rows)
