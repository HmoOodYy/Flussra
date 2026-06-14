"""
Integration tests for the /core domain endpoints:
  GET  /core/branches
  GET  /core/people
  GET  /core/drivers
  GET  /core/drivers/{driver_id}
  POST /core/drivers
  PATCH /core/drivers/{driver_id}

All tests run against the same isolated PostgreSQL cluster used by test_auth.py.
Seed data (company DEMO + branch HQ + admin user) is applied in conftest.py.

The `created_driver_id` session fixture creates one driver record at session
start; tests that read or mutate a specific driver rely on it.
"""
import pytest
import httpx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# GET /core/branches
# ---------------------------------------------------------------------------

class TestBranches:

    async def test_list_branches_requires_auth(self, client: httpx.AsyncClient):
        resp = await client.get("/core/branches")
        assert resp.status_code == 401

    async def test_list_branches_returns_hq(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get("/core/branches", headers=auth(auth_token))
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) >= 1

        hq = next((b for b in body if b["branch_code"] == "HQ"), None)
        assert hq is not None, "HQ branch not found in response"
        assert hq["branch_name"] == "Headquarters"
        assert hq["is_default"] is True
        assert hq["status"] == "Active"

    async def test_list_branches_schema(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get("/core/branches", headers=auth(auth_token))
        assert resp.status_code == 200
        for branch in resp.json():
            assert "branch_id" in branch
            assert "branch_code" in branch
            assert "branch_name" in branch
            assert "status" in branch
            assert "is_default" in branch


# ---------------------------------------------------------------------------
# GET /core/people
# ---------------------------------------------------------------------------

class TestPeople:

    async def test_list_people_requires_auth(self, client: httpx.AsyncClient):
        resp = await client.get("/core/people")
        assert resp.status_code == 401

    async def test_list_people_returns_list(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,   # ensure at least one driver exists
    ):
        resp = await client.get("/core/people", headers=auth(auth_token))
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) >= 1

    async def test_list_people_driver_fields_populated(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        resp = await client.get(
            "/core/people",
            params={"employee_type": "Driver"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        drivers = resp.json()
        assert len(drivers) >= 1
        for p in drivers:
            assert p["employee_type"] == "Driver"
            assert p["driver_id"] is not None
            assert p["driver_status"] is not None

    async def test_list_people_filter_by_employment_status(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get(
            "/core/people",
            params={"employment_status": "Active"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        for p in resp.json():
            assert p["employment_status"] == "Active"

    async def test_list_people_search_by_name(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        resp = await client.get(
            "/core/people",
            params={"q": "test driver"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        results = resp.json()
        assert any("Test Driver" in p["full_name"] for p in results)

    async def test_list_people_schema(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get("/core/people", headers=auth(auth_token))
        assert resp.status_code == 200
        for p in resp.json():
            assert "employee_id" in p
            assert "branch_id" in p
            assert "branch_name" in p
            assert "full_name" in p
            assert "employee_type" in p
            assert "employment_status" in p


# ---------------------------------------------------------------------------
# GET /core/drivers
# ---------------------------------------------------------------------------

class TestDriversList:

    async def test_list_drivers_requires_auth(self, client: httpx.AsyncClient):
        resp = await client.get("/core/drivers")
        assert resp.status_code == 401

    async def test_list_drivers_returns_list(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        resp = await client.get("/core/drivers", headers=auth(auth_token))
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) >= 1

    async def test_list_drivers_filter_by_status(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        resp = await client.get(
            "/core/drivers",
            params={"driver_status": "Active"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        for d in resp.json():
            assert d["driver_status"] == "Active"

    async def test_list_drivers_filter_no_results(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get(
            "/core/drivers",
            params={"driver_status": "OnLeave"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_drivers_search(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        resp = await client.get(
            "/core/drivers",
            params={"q": "td-001"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        results = resp.json()
        assert any(
            (d.get("driver_code") or "").upper() == "TD-001"
            for d in results
        )

    async def test_list_drivers_schema(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        resp = await client.get("/core/drivers", headers=auth(auth_token))
        assert resp.status_code == 200
        for d in resp.json():
            assert "driver_id" in d
            assert "employee_id" in d
            assert "branch_id" in d
            assert "branch_name" in d
            assert "full_name" in d
            assert "driver_status" in d
            assert "employment_status" in d


# ---------------------------------------------------------------------------
# GET /core/drivers/{driver_id}
# ---------------------------------------------------------------------------

class TestDriverGet:

    async def test_get_driver_requires_auth(
        self,
        client: httpx.AsyncClient,
        created_driver_id: int,
    ):
        resp = await client.get(f"/core/drivers/{created_driver_id}")
        assert resp.status_code == 401

    async def test_get_driver_returns_correct_record(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        resp = await client.get(
            f"/core/drivers/{created_driver_id}",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["driver_id"] == created_driver_id
        assert body["full_name"] == "Test Driver One"
        assert body["driver_code"] == "TD-001"
        assert body["cdl_number"] == "CDL-TEST-001"
        assert body["driver_status"] == "Active"
        assert body["employment_status"] == "Active"
        assert body["branch_name"] == "Headquarters"

    async def test_get_driver_not_found_returns_404(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get(
            "/core/drivers/999999",
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_get_driver_schema(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        resp = await client.get(
            f"/core/drivers/{created_driver_id}",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        required = {
            "driver_id", "employee_id", "branch_id", "branch_name",
            "full_name", "driver_status", "employment_status",
        }
        for field in required:
            assert field in body, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# POST /core/drivers
# ---------------------------------------------------------------------------

class TestDriverCreate:

    async def test_create_driver_requires_auth(self, client: httpx.AsyncClient):
        resp = await client.post(
            "/core/drivers",
            json={"branch_id": 1, "full_name": "No Auth Driver"},
        )
        assert resp.status_code == 401

    async def test_create_driver_minimal_fields(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.post(
            "/core/drivers",
            json={"branch_id": 1, "full_name": "Minimal Driver"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["full_name"] == "Minimal Driver"
        assert body["driver_status"] == "Active"
        assert body["employment_status"] == "Active"
        assert body["driver_id"] is not None

    async def test_create_driver_all_fields(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.post(
            "/core/drivers",
            json={
                "branch_id": 1,
                "full_name": "Full Fields Driver",
                "preferred_name": "FFD",
                "employee_key": "EK-FULL-01",
                "email": "ffd@example.com",
                "primary_phone": "555-0001",
                "hire_date": "2024-03-15",
                "driver_code": "FFD-001",
                "cdl_number": "CDL-FULL-01",
                "external_driver_id": "EXT-FULL-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["full_name"] == "Full Fields Driver"
        assert body["preferred_name"] == "FFD"
        assert body["employee_key"] == "EK-FULL-01"
        assert body["email"] == "ffd@example.com"
        assert body["driver_code"] == "FFD-001"
        assert body["cdl_number"] == "CDL-FULL-01"
        assert body["external_driver_id"] == "EXT-FULL-01"
        assert body["hire_date"] == "2024-03-15"

    async def test_create_driver_blank_name_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.post(
            "/core/drivers",
            json={"branch_id": 1, "full_name": "   "},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_create_driver_missing_name_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.post(
            "/core/drivers",
            json={"branch_id": 1},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_create_driver_bad_branch_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.post(
            "/core/drivers",
            json={"branch_id": 999999, "full_name": "Bad Branch Driver"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_create_driver_returns_201_with_location_fields(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.post(
            "/core/drivers",
            json={"branch_id": 1, "full_name": "Status Check Driver"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["branch_id"] == 1
        assert body["branch_name"] == "Headquarters"


# ---------------------------------------------------------------------------
# PATCH /core/drivers/{driver_id}
# ---------------------------------------------------------------------------

class TestDriverPatch:

    async def test_patch_driver_requires_auth(
        self,
        client: httpx.AsyncClient,
        created_driver_id: int,
    ):
        resp = await client.patch(
            f"/core/drivers/{created_driver_id}",
            json={"full_name": "No Auth"},
        )
        assert resp.status_code == 401

    async def test_patch_driver_updates_name(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        resp = await client.patch(
            f"/core/drivers/{created_driver_id}",
            json={"preferred_name": "Patched-TD1"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["preferred_name"] == "Patched-TD1"

    async def test_patch_driver_updates_status(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        # Set to Inactive, then restore to Active so other tests aren't affected
        resp = await client.patch(
            f"/core/drivers/{created_driver_id}",
            json={"driver_status": "Inactive"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["driver_status"] == "Inactive"

        resp2 = await client.patch(
            f"/core/drivers/{created_driver_id}",
            json={"driver_status": "Active"},
            headers=auth(auth_token),
        )
        assert resp2.status_code == 200
        assert resp2.json()["driver_status"] == "Active"

    async def test_patch_driver_invalid_status_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        resp = await client.patch(
            f"/core/drivers/{created_driver_id}",
            json={"driver_status": "NOTVALID"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_patch_driver_not_found_returns_404(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.patch(
            "/core/drivers/999999",
            json={"preferred_name": "Ghost"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_patch_driver_blank_name_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        resp = await client.patch(
            f"/core/drivers/{created_driver_id}",
            json={"full_name": "   "},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_patch_driver_empty_body_is_noop(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        # Fetching first to get baseline
        before = (
            await client.get(
                f"/core/drivers/{created_driver_id}",
                headers=auth(auth_token),
            )
        ).json()

        resp = await client.patch(
            f"/core/drivers/{created_driver_id}",
            json={},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        after = resp.json()
        # Core fields must be unchanged
        assert after["full_name"] == before["full_name"]
        assert after["driver_status"] == before["driver_status"]
