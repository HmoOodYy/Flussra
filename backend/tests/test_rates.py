"""
tests/test_rates.py — Milestone 6: Pay Rates (driver rate matrix)

Covers:
  GET  /payroll/rate-types
  GET  /payroll/rates
  GET  /payroll/rates/{rate_id}
  POST /payroll/rates
  PATCH /payroll/rates/{rate_id}
  POST /payroll/rates/{rate_id}/approve
  DELETE /payroll/rates/{rate_id}

TestRateSafety covers:
  - Atomic claim prevents double-approval
  - effective_to set on superseded open-ended rate
  - explicit effective_to preserved on supersede
  - future-dated rate scenario
  - overlapping pending rates allowed
  - finalized payroll unaffected by rate change
  - full rollback when audit write fails (create, approve, approve-with-supersession)
"""
import pytest
import pytest_asyncio
import httpx
from decimal import Decimal
from unittest.mock import patch, AsyncMock
from app.payroll import service as payroll_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_rate(
    client: httpx.AsyncClient,
    token: str,
    driver_id: int,
    rate_type_id: int,
    *,
    amount: str = "18.50",
    effective_from: str = "2030-01-01",
    effective_to: str | None = None,
    notes: str | None = None,
) -> dict:
    """Helper: create a rate and assert 201."""
    body: dict = {
        "driver_id":      driver_id,
        "rate_type_id":   rate_type_id,
        "amount":         amount,
        "effective_from": effective_from,
    }
    if effective_to is not None:
        body["effective_to"] = effective_to
    if notes is not None:
        body["notes"] = notes
    resp = await client.post("/payroll/rates", json=body, headers=auth(token))
    assert resp.status_code == 201, f"Rate creation failed: {resp.text}"
    return resp.json()


# ---------------------------------------------------------------------------
# Function-scoped fixture: void all non-Voided/Superseded rates created during
# a test to keep tests from polluting each other.
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def rates_clean(session_client: httpx.AsyncClient, auth_token: str):
    """
    Void all PendingApproval and Approved rates before AND after each test.

    Pre-test cleanup prevents leftover Approved rates from a prior test from
    colliding with the partial unique index (one Approved per driver × rate_type)
    when the current test tries to approve a new rate.
    Post-test cleanup keeps subsequent tests isolated.
    """
    async def _void_active() -> None:
        # Fetch each active status separately so Voided rows (which accumulate
        # over the test session) do not fill the limit=100 window and hide
        # the non-Voided rows that need cleanup.
        for status_filter in ("PendingApproval", "Approved", "Superseded"):
            resp = await session_client.get(
                f"/payroll/rates?status={status_filter}", headers=auth(auth_token)
            )
            if resp.status_code == 200:
                for rate in resp.json():
                    await session_client.delete(
                        f"/payroll/rates/{rate['driver_rate_id']}",
                        headers=auth(auth_token),
                    )

    await _void_active()   # pre-test: clear any state left by previous tests
    yield
    await _void_active()   # post-test: clean up what this test created


# ===========================================================================
# TestRateTypes — GET /payroll/rate-types
# ===========================================================================

class TestRateTypes:
    async def test_requires_auth(self, client: httpx.AsyncClient):
        resp = await client.get("/payroll/rate-types")
        assert resp.status_code == 401

    async def test_returns_list(
        self, session_client: httpx.AsyncClient, auth_token: str
    ):
        resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
        assert len(resp.json()) >= 7  # at least the 7 active seeds

    async def test_active_only_by_default(
        self, session_client: httpx.AsyncClient, auth_token: str
    ):
        resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        assert resp.status_code == 200
        for rt in resp.json():
            assert rt["is_active"] is True

    async def test_include_inactive(
        self, session_client: httpx.AsyncClient, auth_token: str
    ):
        resp = await session_client.get(
            "/payroll/rate-types?include_inactive=true", headers=auth(auth_token)
        )
        assert resp.status_code == 200
        codes = {rt["rate_code"] for rt in resp.json()}
        assert "INACTIVE" in codes  # seeded inactive type

    async def test_schema(self, session_client: httpx.AsyncClient, auth_token: str):
        resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        assert resp.status_code == 200
        rt = resp.json()[0]
        assert "rate_type_id" in rt
        assert "rate_code"    in rt
        assert "rate_name"    in rt
        assert "unit_name"    in rt
        assert "is_active"    in rt

    async def test_hourly_seed_present(
        self, session_client: httpx.AsyncClient, auth_token: str, paytest_rate_type_id: int
    ):
        resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        ids = [rt["rate_type_id"] for rt in resp.json()]
        assert paytest_rate_type_id in ids


# ===========================================================================
# TestListRates — GET /payroll/rates
# ===========================================================================

class TestListRates:
    async def test_requires_auth(self, client: httpx.AsyncClient):
        resp = await client.get("/payroll/rates")
        assert resp.status_code == 401

    async def test_returns_empty_initially(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        rates_clean,
    ):
        resp = await session_client.get(
            "/payroll/rates?status=PendingApproval", headers=auth(auth_token)
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_shows_created_rate(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        resp = await session_client.get(
            "/payroll/rates?status=PendingApproval", headers=auth(auth_token)
        )
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    async def test_filter_by_driver(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        resp = await session_client.get(
            f"/payroll/rates?driver_id={paytest_driver_id}",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert all(r["driver_id"] == paytest_driver_id for r in resp.json())

    async def test_filter_by_status(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        # Filter for Approved — should not include the PendingApproval we just created
        resp = await session_client.get(
            "/payroll/rates?status=Approved", headers=auth(auth_token)
        )
        assert resp.status_code == 200
        assert all(r["status"] == "Approved" for r in resp.json())

    async def test_pagination(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        paytest_mileage_rate_type_id: int,
        rates_clean,
    ):
        # Create two rates on different types
        await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            effective_from="2030-02-01",
        )
        await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_mileage_rate_type_id,
            amount="1.25", effective_from="2030-02-01",
        )
        resp_one = await session_client.get(
            f"/payroll/rates?driver_id={paytest_driver_id}&limit=1&offset=0",
            headers=auth(auth_token),
        )
        resp_two = await session_client.get(
            f"/payroll/rates?driver_id={paytest_driver_id}&limit=1&offset=1",
            headers=auth(auth_token),
        )
        assert resp_one.status_code == 200
        assert resp_two.status_code == 200
        assert len(resp_one.json()) == 1
        assert len(resp_two.json()) == 1
        # The two pages return different records
        assert resp_one.json()[0]["driver_rate_id"] != resp_two.json()[0]["driver_rate_id"]


# ===========================================================================
# TestGetRate — GET /payroll/rates/{rate_id}
# ===========================================================================

class TestGetRate:
    async def test_requires_auth(
        self,
        client: httpx.AsyncClient,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        resp = await client.get(f"/payroll/rates/{rate['driver_rate_id']}")
        assert resp.status_code == 401

    async def test_returns_correct_record(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="22.00",
        )
        rid = rate["driver_rate_id"]
        resp = await session_client.get(f"/payroll/rates/{rid}", headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json()["driver_rate_id"] == rid
        assert Decimal(resp.json()["amount"]) == Decimal("22.00")

    async def test_not_found(self, session_client: httpx.AsyncClient, auth_token: str):
        resp = await session_client.get("/payroll/rates/9999999", headers=auth(auth_token))
        assert resp.status_code == 404

    async def test_schema(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        rid = rate["driver_rate_id"]
        resp = await session_client.get(f"/payroll/rates/{rid}", headers=auth(auth_token))
        data = resp.json()
        for field in (
            "driver_rate_id", "company_id", "branch_id", "driver_id", "driver_name",
            "rate_type_id", "rate_code", "rate_name", "unit_name",
            "amount", "effective_from", "status",
            "created_by_user_id", "created_at_utc",
        ):
            assert field in data, f"Missing field: {field}"


# ===========================================================================
# TestCreateRate — POST /payroll/rates
# ===========================================================================

class TestCreateRate:
    async def test_requires_auth(
        self,
        client: httpx.AsyncClient,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
    ):
        resp = await client.post("/payroll/rates", json={
            "driver_id":      paytest_driver_id,
            "rate_type_id":   paytest_rate_type_id,
            "amount":         "15.00",
            "effective_from": "2030-01-01",
        })
        assert resp.status_code == 401

    async def test_create_minimal(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id":      paytest_driver_id,
                "rate_type_id":   paytest_rate_type_id,
                "amount":         "20.00",
                "effective_from": "2030-01-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"]         == "PendingApproval"
        assert Decimal(data["amount"]) == Decimal("20.00")
        assert data["driver_id"]      == paytest_driver_id
        assert data["rate_type_id"]   == paytest_rate_type_id

    async def test_create_with_all_fields(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id":      paytest_driver_id,
                "rate_type_id":   paytest_rate_type_id,
                "amount":         "17.75",
                "effective_from": "2030-03-01",
                "effective_to":   "2030-12-31",
                "notes":          "Contracted rate",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["effective_to"]  == "2030-12-31"
        assert data["notes"]         == "Contracted rate"

    async def test_populates_driver_name_and_rate_type_fields(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        assert rate["driver_name"] == "Paytest Driver"
        assert rate["rate_code"]   == "HOURLY"
        assert rate["unit_name"]   == "Hour"

    async def test_bad_driver_returns_422(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id":      9999999,
                "rate_type_id":   paytest_rate_type_id,
                "amount":         "15.00",
                "effective_from": "2030-01-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_bad_rate_type_returns_422(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        rates_clean,
    ):
        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id":      paytest_driver_id,
                "rate_type_id":   9999999,
                "amount":         "15.00",
                "effective_from": "2030-01-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_inactive_rate_type_returns_422(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        rates_clean,
    ):
        # Fetch the INACTIVE type's ID
        rt_resp = await session_client.get(
            "/payroll/rate-types?include_inactive=true",
            headers=auth(auth_token),
        )
        inactive_id = next(
            rt["rate_type_id"] for rt in rt_resp.json() if rt["rate_code"] == "INACTIVE"
        )
        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id":      paytest_driver_id,
                "rate_type_id":   inactive_id,
                "amount":         "15.00",
                "effective_from": "2030-01-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_zero_amount_returns_422(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
    ):
        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id":      paytest_driver_id,
                "rate_type_id":   paytest_rate_type_id,
                "amount":         "0",
                "effective_from": "2030-01-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_negative_amount_returns_422(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
    ):
        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id":      paytest_driver_id,
                "rate_type_id":   paytest_rate_type_id,
                "amount":         "-5.00",
                "effective_from": "2030-01-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_effective_to_before_from_returns_422(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
    ):
        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id":      paytest_driver_id,
                "rate_type_id":   paytest_rate_type_id,
                "amount":         "15.00",
                "effective_from": "2030-06-01",
                "effective_to":   "2030-01-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_effective_to_same_as_from_returns_422(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
    ):
        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id":      paytest_driver_id,
                "rate_type_id":   paytest_rate_type_id,
                "amount":         "15.00",
                "effective_from": "2030-06-01",
                "effective_to":   "2030-06-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422


# ===========================================================================
# TestUpdateRate — PATCH /payroll/rates/{rate_id}
# ===========================================================================

class TestUpdateRate:
    async def test_requires_auth(
        self,
        client: httpx.AsyncClient,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        resp = await client.patch(
            f"/payroll/rates/{rate['driver_rate_id']}", json={"amount": "25.00"}
        )
        assert resp.status_code == 401

    async def test_update_amount(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="15.00",
        )
        rid = rate["driver_rate_id"]
        resp = await session_client.patch(
            f"/payroll/rates/{rid}",
            json={"amount": "19.50"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert Decimal(resp.json()["amount"]) == Decimal("19.50")

    async def test_update_notes_and_dates(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            effective_from="2030-01-01",
        )
        rid = rate["driver_rate_id"]
        resp = await session_client.patch(
            f"/payroll/rates/{rid}",
            json={
                "effective_from": "2030-02-01",
                "effective_to":   "2030-12-31",
                "notes":          "Updated note",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["effective_from"] == "2030-02-01"
        assert data["effective_to"]   == "2030-12-31"
        assert data["notes"]          == "Updated note"

    async def test_empty_body_is_noop(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="18.50",
        )
        rid = rate["driver_rate_id"]
        resp = await session_client.patch(
            f"/payroll/rates/{rid}",
            json={},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert Decimal(resp.json()["amount"]) == Decimal("18.50")  # unchanged

    async def test_cannot_update_approved_rate(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        rid = rate["driver_rate_id"]
        # Approve it
        await session_client.post(f"/payroll/rates/{rid}/approve", headers=auth(auth_token))
        # Now try to update
        resp = await session_client.patch(
            f"/payroll/rates/{rid}",
            json={"amount": "99.99"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_not_found_returns_404(
        self, session_client: httpx.AsyncClient, auth_token: str
    ):
        resp = await session_client.patch(
            "/payroll/rates/9999999",
            json={"amount": "10.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_negative_amount_returns_422(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        resp = await session_client.patch(
            f"/payroll/rates/{rate['driver_rate_id']}",
            json={"amount": "-1.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_effective_to_before_from_returns_422(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            effective_from="2030-06-01",
        )
        resp = await session_client.patch(
            f"/payroll/rates/{rate['driver_rate_id']}",
            json={"effective_to": "2030-05-01"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422


# ===========================================================================
# TestApproveRate — POST /payroll/rates/{rate_id}/approve
# ===========================================================================

class TestApproveRate:
    async def test_requires_auth(
        self,
        client: httpx.AsyncClient,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        resp = await client.post(f"/payroll/rates/{rate['driver_rate_id']}/approve")
        assert resp.status_code == 401

    async def test_approve_sets_status_and_approver(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        rid = rate["driver_rate_id"]
        resp = await session_client.post(
            f"/payroll/rates/{rid}/approve", headers=auth(auth_token)
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"]              == "Approved"
        assert data["approved_by_user_id"] is not None
        assert data["approved_at_utc"]     is not None

    async def test_approve_supersedes_previous_approved(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        """Approving a new rate for the same driver+type supersedes the old Approved one."""
        # First rate — approve it
        rate1 = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="15.00", effective_from="2030-01-01",
        )
        rid1 = rate1["driver_rate_id"]
        await session_client.post(
            f"/payroll/rates/{rid1}/approve", headers=auth(auth_token)
        )

        # Second rate for same driver+type — approve it
        rate2 = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="18.00", effective_from="2031-01-01",
        )
        rid2 = rate2["driver_rate_id"]
        await session_client.post(
            f"/payroll/rates/{rid2}/approve", headers=auth(auth_token)
        )

        # rate1 should now be Superseded
        r1_resp = await session_client.get(f"/payroll/rates/{rid1}", headers=auth(auth_token))
        assert r1_resp.json()["status"] == "Superseded"

        # rate2 should be Approved
        r2_resp = await session_client.get(f"/payroll/rates/{rid2}", headers=auth(auth_token))
        assert r2_resp.json()["status"] == "Approved"

    async def test_cannot_approve_already_approved(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        rid = rate["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{rid}/approve", headers=auth(auth_token))
        # Try to approve again
        resp = await session_client.post(
            f"/payroll/rates/{rid}/approve", headers=auth(auth_token)
        )
        assert resp.status_code == 422

    async def test_cannot_approve_voided(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        rid = rate["driver_rate_id"]
        await session_client.delete(f"/payroll/rates/{rid}", headers=auth(auth_token))
        resp = await session_client.post(
            f"/payroll/rates/{rid}/approve", headers=auth(auth_token)
        )
        assert resp.status_code == 422

    async def test_not_found(self, session_client: httpx.AsyncClient, auth_token: str):
        resp = await session_client.post(
            "/payroll/rates/9999999/approve", headers=auth(auth_token)
        )
        assert resp.status_code == 404


# ===========================================================================
# TestVoidRate — DELETE /payroll/rates/{rate_id}
# ===========================================================================

class TestVoidRate:
    async def test_requires_auth(
        self,
        client: httpx.AsyncClient,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        resp = await client.delete(f"/payroll/rates/{rate['driver_rate_id']}")
        assert resp.status_code == 401

    async def test_void_pending_returns_204(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        resp = await session_client.delete(
            f"/payroll/rates/{rate['driver_rate_id']}", headers=auth(auth_token)
        )
        assert resp.status_code == 204

    async def test_void_sets_status(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        rid = rate["driver_rate_id"]
        await session_client.delete(f"/payroll/rates/{rid}", headers=auth(auth_token))
        resp = await session_client.get(f"/payroll/rates/{rid}", headers=auth(auth_token))
        assert resp.json()["status"] == "Voided"

    async def test_void_approved_rate(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        rid = rate["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{rid}/approve", headers=auth(auth_token))
        resp = await session_client.delete(f"/payroll/rates/{rid}", headers=auth(auth_token))
        assert resp.status_code == 204
        check = await session_client.get(f"/payroll/rates/{rid}", headers=auth(auth_token))
        assert check.json()["status"] == "Voided"

    async def test_cannot_void_already_voided(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        """An already-Voided rate cannot be voided again."""
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="10.00", effective_from="2030-01-01",
        )
        rid = rate["driver_rate_id"]

        # First void succeeds
        r1 = await session_client.delete(f"/payroll/rates/{rid}", headers=auth(auth_token))
        assert r1.status_code == 204

        # Second void fails — already Voided
        r2 = await session_client.delete(f"/payroll/rates/{rid}", headers=auth(auth_token))
        assert r2.status_code == 422
        assert "already Voided" in r2.json()["detail"]

    async def test_not_found_returns_404(
        self, session_client: httpx.AsyncClient, auth_token: str
    ):
        resp = await session_client.delete(
            "/payroll/rates/9999999", headers=auth(auth_token)
        )
        assert resp.status_code == 404

    async def test_void_excluded_from_active_list(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        rid = rate["driver_rate_id"]
        await session_client.delete(f"/payroll/rates/{rid}", headers=auth(auth_token))

        resp = await session_client.get(
            f"/payroll/rates?driver_id={paytest_driver_id}&status=PendingApproval",
            headers=auth(auth_token),
        )
        ids = [r["driver_rate_id"] for r in resp.json()]
        assert rid not in ids


# ===========================================================================
# TestRateSafety — atomic claims, audit, effective-date, historical isolation
# ===========================================================================

class TestRateSafety:
    """
    Verifies the 5 safety properties fixed in the M6 review:
      1. Atomic claim prevents double-approval (TOCTOU fix)
      2. effective_to closed on superseded open-ended rate
      3. explicit effective_to preserved on supersede
      4. Full rollback when audit write fails (create)
      5. Full rollback when audit write fails (approval — no prior Approved)
      6. Full rollback reverts supersession when audit write fails
      7. Future-dated rate coexists with current Approved until explicitly approved
      8. Two PendingApproval rates for the same driver+type are allowed (resolved on approval)
      9. Finalized payroll is completely isolated from subsequent rate changes
    """

    # ---- 1: Atomic claim ----------------------------------------------------

    async def test_atomic_claim_rejects_already_approved(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        """
        The atomic UPDATE WHERE status='PendingApproval' RETURNING means a second
        attempt to approve the same rate (after it's already Approved) gets 422.
        This also validates the uniqueness invariant in steady-state.
        """
        rate = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        rid = rate["driver_rate_id"]
        # First approval succeeds
        r1 = await session_client.post(
            f"/payroll/rates/{rid}/approve", headers=auth(auth_token)
        )
        assert r1.status_code == 200
        assert r1.json()["status"] == "Approved"

        # Second attempt on the same rate — already Approved, must fail
        r2 = await session_client.post(
            f"/payroll/rates/{rid}/approve", headers=auth(auth_token)
        )
        assert r2.status_code == 422

    # ---- 2: effective_to closed on open-ended superseded rate ---------------

    async def test_superseded_open_rate_effective_to_set(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        """
        When a new rate is approved, the prior Approved rate's effective_to
        (if NULL / open-ended) should be set to new_rate.effective_from - 1 day.
        """
        # rate1: open-ended
        rate1 = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="15.00", effective_from="2030-01-01",
            # effective_to deliberately omitted → NULL
        )
        rid1 = rate1["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{rid1}/approve", headers=auth(auth_token))

        # rate2: starts 2031-01-01
        rate2 = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="18.00", effective_from="2031-01-01",
        )
        rid2 = rate2["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{rid2}/approve", headers=auth(auth_token))

        # rate1 should now have effective_to = 2030-12-31 (2031-01-01 minus 1 day)
        r1 = await session_client.get(f"/payroll/rates/{rid1}", headers=auth(auth_token))
        assert r1.json()["effective_to"] == "2030-12-31"

    # ---- 3: explicit effective_to preserved ---------------------------------

    async def test_superseded_explicit_effective_to_preserved(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        """
        If the superseded rate already had an explicit effective_to, it must
        NOT be overwritten by the supersession logic.
        """
        # rate1: explicit effective_to
        rate1 = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="15.00", effective_from="2030-01-01", effective_to="2030-06-30",
        )
        rid1 = rate1["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{rid1}/approve", headers=auth(auth_token))

        # rate2
        rate2 = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="18.00", effective_from="2031-01-01",
        )
        rid2 = rate2["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{rid2}/approve", headers=auth(auth_token))

        # rate1's explicit effective_to must remain unchanged
        r1 = await session_client.get(f"/payroll/rates/{rid1}", headers=auth(auth_token))
        assert r1.json()["effective_to"] == "2030-06-30"

    # ---- 4: rollback when audit write fails on create -----------------------

    async def test_rollback_when_audit_fails_on_create(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
    ):
        """
        If _write_rate_audit raises during create_rate(), the INSERT must roll back:
        no rate row should exist after the exception.
        """
        headers = auth(auth_token)

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure on create — rollback expected")

        with patch.object(payroll_service, "_write_rate_audit", _raise):
            with pytest.raises(RuntimeError, match="Simulated audit failure on create"):
                await client.post(
                    "/payroll/rates",
                    json={
                        "driver_id":      paytest_driver_id,
                        "rate_type_id":   paytest_rate_type_id,
                        "amount":         "20.00",
                        "effective_from": "2034-01-01",
                    },
                    headers=headers,
                )

        # No rate should have been committed
        resp = await client.get(
            f"/payroll/rates?driver_id={paytest_driver_id}&status=PendingApproval",
            headers=headers,
        )
        # Verify the specific amount isn't there (other tests may have left rates)
        amounts = [r["amount"] for r in resp.json()]
        assert "20.0000" not in amounts and "20.00" not in amounts

    # ---- 5: rollback when audit fails on approval (no prior Approved) -------

    async def test_rollback_when_audit_fails_on_approval(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        """
        If _write_rate_audit raises during approve_rate(), the atomic claim
        (UPDATE to Approved) must roll back — rate stays PendingApproval.
        """
        headers = auth(auth_token)
        rate = await _create_rate(
            client, auth_token, paytest_driver_id, paytest_rate_type_id
        )
        rid = rate["driver_rate_id"]

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure on approval — rollback expected")

        with patch.object(payroll_service, "_write_rate_audit", _raise):
            with pytest.raises(RuntimeError, match="Simulated audit failure on approval"):
                await client.post(f"/payroll/rates/{rid}/approve", headers=headers)

        # Rate must still be PendingApproval (not Approved)
        resp = await client.get(f"/payroll/rates/{rid}", headers=headers)
        assert resp.json()["status"] == "PendingApproval"

    # ---- 6: rollback reverts supersession when audit fails ------------------

    async def test_rollback_reverts_supersession_when_audit_fails(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        """
        When approving rate2 supersedes rate1, and the audit write then fails,
        the entire transaction rolls back:
          - rate2 reverts from Approved → PendingApproval
          - rate1 reverts from Superseded → Approved
        """
        headers = auth(auth_token)

        # Approve rate1 first
        rate1 = await _create_rate(
            client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="15.00", effective_from="2030-01-01",
        )
        rid1 = rate1["driver_rate_id"]
        # This approve succeeds (no monkeypatch yet)
        await client.post(f"/payroll/rates/{rid1}/approve", headers=headers)

        # Create rate2 (will supersede rate1 when approved)
        rate2 = await _create_rate(
            client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="18.00", effective_from="2031-01-01",
        )
        rid2 = rate2["driver_rate_id"]

        # Now patch audit to fail — the RATE_SUPERSEDED write for rate1 fires first
        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure — supersession rollback expected")

        with patch.object(payroll_service, "_write_rate_audit", _raise):
            with pytest.raises(RuntimeError, match="Simulated audit failure"):
                await client.post(f"/payroll/rates/{rid2}/approve", headers=headers)

        # Both rates must be in their original pre-approve-call state
        r1 = await client.get(f"/payroll/rates/{rid1}", headers=headers)
        r2 = await client.get(f"/payroll/rates/{rid2}", headers=headers)
        assert r1.json()["status"] == "Approved"       # not Superseded
        assert r2.json()["status"] == "PendingApproval"  # not Approved

    # ---- 7: future-dated rate coexists with current Approved ----------------

    async def test_future_dated_rate_does_not_affect_current(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        """
        Creating a PendingApproval rate with a future effective_from does not
        disturb the currently Approved rate.  Only the explicit /approve call
        transitions the current rate to Superseded.
        """
        headers = auth(auth_token)

        # Approve a current rate
        rate1 = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="15.00", effective_from="2025-01-01",
        )
        rid1 = rate1["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{rid1}/approve", headers=headers)

        # Create a future rate (PendingApproval) — must not touch rate1
        await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="18.00", effective_from="2040-01-01",
        )

        # rate1 must still be Approved
        r1 = await session_client.get(f"/payroll/rates/{rid1}", headers=headers)
        assert r1.json()["status"] == "Approved"

    async def test_approving_future_rate_supersedes_current(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        """
        Approving the future rate supersedes the current Approved rate.
        """
        headers = auth(auth_token)

        rate1 = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="15.00", effective_from="2025-01-01",
        )
        rid1 = rate1["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{rid1}/approve", headers=headers)

        # Use 2055-01-01 — no test creates a finalized period that far out,
        # so the backdating guard will not fire.
        rate2 = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="18.00", effective_from="2055-01-01",
        )
        rid2 = rate2["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{rid2}/approve", headers=headers)

        r1 = await session_client.get(f"/payroll/rates/{rid1}", headers=headers)
        r2 = await session_client.get(f"/payroll/rates/{rid2}", headers=headers)
        assert r1.json()["status"] == "Superseded"
        assert r2.json()["status"] == "Approved"

    # ---- 8: overlapping pending rates are allowed ---------------------------

    async def test_two_pending_rates_same_driver_type_allowed(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        """
        Two PendingApproval rates for the same driver+type are valid.
        They are resolved when one is approved (which supersedes any Approved rate,
        not other Pending ones — those remain pending and require separate decisions).
        """
        rate1 = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="15.00", effective_from="2030-01-01",
        )
        rate2 = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="18.00", effective_from="2031-01-01",
        )
        assert rate1["driver_rate_id"] != rate2["driver_rate_id"]
        assert rate1["status"] == "PendingApproval"
        assert rate2["status"] == "PendingApproval"

        # Approving rate1 does not affect the PendingApproval state of rate2
        await session_client.post(
            f"/payroll/rates/{rate1['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )
        r2 = await session_client.get(
            f"/payroll/rates/{rate2['driver_rate_id']}", headers=auth(auth_token)
        )
        assert r2.json()["status"] == "PendingApproval"

    # ---- 9: finalized payroll is isolated from rate changes -----------------

    async def test_finalized_payroll_unaffected_by_rate_change(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_rate_type_id: int,
        paytest_branch_id: int,
        rates_clean,
    ):
        """
        PayrollFinalLines snapshot the rate amount at finalization time.
        A subsequent rate change does not alter the finalized payroll record.

        This confirms the architectural isolation: DriverRates is a reference
        table; PayrollFinalLines stores the locked amount independently.

        Phase 5 note: uses an isolated driver so that rate15 (which stays
        referenced in PayrollFinalLines after finalization) does not contaminate
        paytest_driver_id-based tests.  rates_clean silently ignores the 422
        when it tries to void rate15 (Phase 5 guard), and since the rate belongs
        to an isolated driver the stuck Superseded status causes no cascade.
        """
        headers = auth(auth_token)

        # Isolated driver for this test: keeps finalized DriverRate off
        # the shared paytest_driver_id so rates_clean 422s are harmless.
        drv_resp = await session_client.post(
            "/core/drivers",
            json={
                "branch_id": paytest_branch_id,
                "full_name": "RateSafety Isolated Driver",
                "driver_code": "RSI-0001",
            },
            headers=headers,
        )
        assert drv_resp.status_code == 201, f"Create isolated driver failed: {drv_resp.text}"
        isolated_driver_id = drv_resp.json()["driver_id"]

        # Create a payroll period on the PAYTEST branch (unique future dates)
        period_resp = await session_client.post(
            "/payroll/periods",
            json={
                "branch_id":   paytest_branch_id,
                "period_type": "Week",
                "start_date":  "2038-06-07",
                "end_date":    "2038-06-13",
            },
            headers=headers,
        )
        assert period_resp.status_code == 201
        pid = period_resp.json()["payroll_period_id"]

        # Open → create and approve an HOURLY rate ($15), then add the line
        # Phase 4C: rate_amount is no longer accepted for PerUnit lines.
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Open"},
            headers=headers,
        )
        rate15 = await _create_rate(
            session_client, auth_token, isolated_driver_id, paytest_rate_type_id,
            amount="15.00", effective_from="2038-01-01",
        )
        await session_client.post(
            f"/payroll/rates/{rate15['driver_rate_id']}/approve",
            headers=headers,
        )
        line_resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={
                "driver_id":   isolated_driver_id,
                "line_type":   "Hours",
                "quantity":    "10.00",
                "work_date":   "2038-06-10",
            },
            headers=headers,
        )
        assert line_resp.status_code == 201
        # Approved DriverRate ($15) provides the rate; NMR must be False.
        assert not line_resp.json().get("needs_manager_review"), (
            "Hours line with approved rate should not be flagged for review"
        )

        # Advance to Approved via review flow then Finalize
        r_ir = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "InReview"}, headers=headers,
        )
        assert r_ir.status_code == 200, f"transition to InReview failed: {r_ir.text}"
        review_resp = await session_client.get("/review/items", headers=headers)
        review_item = next(
            i for i in review_resp.json()
            if i.get("entity_name") == "PayrollPeriods"
            and i.get("entity_id") == str(pid)
            and i.get("status") == "Pending"
        )
        r_approve = await session_client.post(
            f"/review/items/{review_item['review_item_id']}/decide",
            headers=headers, json={"decision": "Approved"},
        )
        assert r_approve.status_code == 200, f"Approval failed: {r_approve.text}"
        fin = await session_client.post(
            f"/payroll/periods/{pid}/finalize", headers=headers
        )
        assert fin.status_code == 200, fin.text

        # Verify final amount = 10 * 15.00 = 150.00
        fl = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=headers
        )
        assert len(fl.json()) == 1
        assert Decimal(fl.json()[0]["final_amount"]) == Decimal("150.00")

        # Now create and approve a very different rate — supersedes rate15
        new_rate = await _create_rate(
            session_client, auth_token, isolated_driver_id, paytest_rate_type_id,
            amount="999.99", effective_from="2038-01-01",
        )
        await session_client.post(
            f"/payroll/rates/{new_rate['driver_rate_id']}/approve",
            headers=headers,
        )

        # Final lines must be completely unchanged
        fl2 = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=headers
        )
        assert len(fl2.json()) == 1
        assert Decimal(fl2.json()[0]["final_amount"]) == Decimal("150.00")

    # ---- 10: explicit effective_to is trimmed when the new rate's range overlaps ----

    async def test_explicit_effective_to_trimmed_on_overlap(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        """
        Q2/Q4 coverage.

        When an existing Approved rate has an explicit effective_to that OVERLAPS
        with the incoming rate's range, the supersede step must TRIM the old
        effective_to to (new_from - 1 day), not leave it unchanged.

        Example:
          rate1: Approved  Jan 1 → Dec 31  $25   (explicit effective_to)
          rate2: approved  Jun 1 → ∞       $30

        After approving rate2, rate1 must become:
          rate1: Superseded  Jan 1 → May 31  $25   ← trimmed, not Dec 31
        """
        headers = auth(auth_token)

        # rate1: explicit effective_to well into the future (would overlap rate2's range)
        rate1 = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="25.00", effective_from="2030-01-01", effective_to="2030-12-31",
        )
        rid1 = rate1["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{rid1}/approve", headers=headers)

        # rate2 starts mid-year — its range OVERLAPS rate1's Dec 31 end date
        rate2 = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="30.00", effective_from="2030-06-01",
        )
        rid2 = rate2["driver_rate_id"]
        r = await session_client.post(f"/payroll/rates/{rid2}/approve", headers=headers)
        assert r.status_code == 200, r.text

        # rate1 must be closed at May 31 (2030-06-01 − 1), NOT at Dec 31
        r1 = await session_client.get(f"/payroll/rates/{rid1}", headers=headers)
        assert r1.json()["status"]       == "Superseded"
        assert r1.json()["effective_to"] == "2030-05-31"

        # rate2 is the new open-ended approved rate
        r2 = await session_client.get(f"/payroll/rates/{rid2}", headers=headers)
        assert r2.json()["status"]       == "Approved"
        assert r2.json()["effective_to"] is None

    # ---- 11: approval rejected when a later Approved rate already exists ----------

    async def test_approval_rejected_when_later_approved_rate_exists(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        """
        Q1 / conflict-guard coverage.

        Cannot approve a rate whose effective_from is on or after an already-Approved
        rate's effective_from.  Doing so would require superseding that rate with an
        effective_to BEFORE its own effective_from (invalid dates).

        The correct workflow: void the later Approved rate first, then approve.
        """
        headers = auth(auth_token)

        # First approve a rate that starts Oct 1
        rate_late = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="30.00", effective_from="2030-10-01",
        )
        await session_client.post(
            f"/payroll/rates/{rate_late['driver_rate_id']}/approve", headers=headers
        )

        # Now try to approve a rate that starts Jul 1 — BEFORE the Oct 1 rate
        rate_early = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="25.00", effective_from="2030-07-01",
        )
        resp = await session_client.post(
            f"/payroll/rates/{rate_early['driver_rate_id']}/approve", headers=headers
        )
        # Must be rejected — the Oct 1 rate starts after Jul 1
        assert resp.status_code == 422
        assert "2030-10-01" in resp.json()["detail"] or "already exists" in resp.json()["detail"]

    # ---- 12: superseded rate is usable for historical work dates (Q3 / Q5) ---------

    async def test_superseded_rate_usable_for_historical_work_date(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        """
        Q3 / Q5 coverage.

        A Superseded rate must still be returned by the rate-lookup endpoint for
        work dates that fall inside its historical effective range.  'Superseded'
        means the rate is no longer the *current* rate — it does NOT mean the rate
        is unusable for historical payroll calculations.
        """
        headers = auth(auth_token)

        # Approve rate1 ($25, Jan 1 open-ended)
        rate1 = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="25.00", effective_from="2030-01-01",
        )
        rid1 = rate1["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{rid1}/approve", headers=headers)

        # Approve rate2 ($30, May 4 open-ended) — supersedes rate1
        rate2 = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="30.00", effective_from="2030-05-04",
        )
        rid2 = rate2["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{rid2}/approve", headers=headers)

        # rate1 is now Superseded with effective_to = May 3
        r1 = await session_client.get(f"/payroll/rates/{rid1}", headers=headers)
        assert r1.json()["status"]       == "Superseded"
        assert r1.json()["effective_to"] == "2030-05-03"

        # Lookup for a work date INSIDE rate1's historical range → must return rate1
        resp = await session_client.get(
            f"/payroll/rates/lookup"
            f"?driver_id={paytest_driver_id}"
            f"&rate_type_id={paytest_rate_type_id}"
            f"&work_date=2030-03-15",
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["found"] is True
        assert data["rate"]["driver_rate_id"] == rid1
        assert data["rate"]["status"]         == "Superseded"
        assert Decimal(data["rate"]["amount"]) == Decimal("25.00")

        # Lookup for a work date INSIDE rate2's range → must return rate2
        resp2 = await session_client.get(
            f"/payroll/rates/lookup"
            f"?driver_id={paytest_driver_id}"
            f"&rate_type_id={paytest_rate_type_id}"
            f"&work_date=2030-06-01",
            headers=headers,
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["found"] is True
        assert data2["rate"]["driver_rate_id"] == rid2
        assert data2["rate"]["status"]         == "Approved"
        assert Decimal(data2["rate"]["amount"]) == Decimal("30.00")

        # Lookup before any rate → not found
        resp3 = await session_client.get(
            f"/payroll/rates/lookup"
            f"?driver_id={paytest_driver_id}"
            f"&rate_type_id={paytest_rate_type_id}"
            f"&work_date=2029-12-31",
            headers=headers,
        )
        assert resp3.status_code == 200
        assert resp3.json()["found"] is False

    # ---- 13: mid-period rate change — correct rate selected per work date (Q9) -----

    async def test_mid_period_rate_change_correct_rate_by_work_date(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        rates_clean,
    ):
        """
        Q9 coverage — the core mid-period scenario.

        Payroll period: May 1 → May 7 (illustrative; not a real period here)
        Rate change:
          rate1  $25/hr  May 1 → May 3   (closed when rate2 is approved)
          rate2  $30/hr  May 4 → ∞

        The rate-lookup endpoint must return:
          work_date May 1 → $25  (rate1, Superseded)
          work_date May 3 → $25  (rate1, Superseded — last day of its range)
          work_date May 4 → $30  (rate2, Approved — first day of new range)
          work_date May 7 → $30  (rate2)
          work_date Apr 30 → not found
        """
        headers = auth(auth_token)

        base = "2031-05"  # use 2031 to avoid collision with other tests

        rate1 = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="25.00", effective_from=f"{base}-01",
        )
        rid1 = rate1["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{rid1}/approve", headers=headers)

        rate2 = await _create_rate(
            session_client, auth_token, paytest_driver_id, paytest_rate_type_id,
            amount="30.00", effective_from=f"{base}-04",
        )
        rid2 = rate2["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{rid2}/approve", headers=headers)

        # Confirm date-range closure
        r1 = await session_client.get(f"/payroll/rates/{rid1}", headers=headers)
        assert r1.json()["effective_to"] == f"{base}-03"
        assert r1.json()["status"]       == "Superseded"

        async def lookup(work_date: str) -> dict:
            resp = await session_client.get(
                f"/payroll/rates/lookup"
                f"?driver_id={paytest_driver_id}"
                f"&rate_type_id={paytest_rate_type_id}"
                f"&work_date={work_date}",
                headers=headers,
            )
            assert resp.status_code == 200
            return resp.json()

        # May 1 — first day of period, rate1 applies
        d = await lookup(f"{base}-01")
        assert d["found"] is True
        assert d["rate"]["driver_rate_id"] == rid1
        assert Decimal(d["rate"]["amount"]) == Decimal("25.00")

        # May 3 — last day rate1 applies
        d = await lookup(f"{base}-03")
        assert d["found"] is True
        assert d["rate"]["driver_rate_id"] == rid1
        assert Decimal(d["rate"]["amount"]) == Decimal("25.00")

        # May 4 — first day rate2 applies (rate change day)
        d = await lookup(f"{base}-04")
        assert d["found"] is True
        assert d["rate"]["driver_rate_id"] == rid2
        assert Decimal(d["rate"]["amount"]) == Decimal("30.00")

        # May 7 — last day of period, rate2 still applies
        d = await lookup(f"{base}-07")
        assert d["found"] is True
        assert d["rate"]["driver_rate_id"] == rid2
        assert Decimal(d["rate"]["amount"]) == Decimal("30.00")

        # Apr 30 — before any rate
        d = await lookup("2031-04-30")
        assert d["found"] is False
