"""
Integration tests for the /core domain endpoints:
  GET  /core/branches
  GET  /core/driver-assignments

All tests run against the same isolated PostgreSQL cluster used by test_auth.py.
Seed data (company DEMO + branch HQ + admin user) is applied in conftest.py.

The `created_driver_id` session fixture creates one driver record at session
start; tests that read or mutate a specific driver rely on it.
"""
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
# Retired Core compatibility routes
# Workforce owns employee and permanent Driver lifecycle. Core retains only Branch
# and assignment-roster reads, which are consumed by current target workflows.

async def test_workforce_compatibility_routes_are_not_mounted(client, auth_token):
    retired = [
        ("GET", "/core/people"),
        ("POST", "/core/drivers"),
        ("PATCH", "/core/drivers/123"),
        ("GET", "/core/drivers"),
        ("GET", "/core/drivers/123"),
        ("GET", "/core/drivers/123/assignments"),
    ]
    for method, path in retired:
        response = await client.request(method, path, headers=auth(auth_token), json={})
        assert response.status_code == 404, f"{method} {path} remains mounted"
