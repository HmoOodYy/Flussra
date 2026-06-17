"""
test_cdpi_http_token.py — CDPI-HTTP-1 regression tests

Verifies that the CDPI router correctly casts token["sub"] (a JWT string) and
token["cid"] (an int) to Python int before passing them to SQL permission checks.

Before the fix, all CDPI endpoints returned HTTP 500 because asyncpg rejected
string "1" for an int4 SQL parameter in fn_UserHasPermission.

These tests exercise the three endpoints confirmed broken during E2E-SMOKE-1
via a real authenticated token through TestClient, not direct service calls.
"""
import pytest


# ---------------------------------------------------------------------------
# 1. GET /settings/cdpi/branches/{branch_id}/items
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_branch_items_no_500(client, auth_token, hq_branch_id):
    """Route returns 200 (not 500) — token cast works end-to-end."""
    resp = await client.get(
        f"/settings/cdpi/branches/{hq_branch_id}/items",
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 200, (
        f"Expected 200 from list_branch_items; got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_list_branch_items_returns_list(client, auth_token, hq_branch_id):
    """Response body is a JSON array (may be empty when no CDPI items exist)."""
    resp = await client.get(
        f"/settings/cdpi/branches/{hq_branch_id}/items",
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# 2. POST /settings/cdpi/direct-company-items
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_direct_create_no_500(client, auth_token):
    """Route returns 201 (not 500) — token cast works end-to-end."""
    resp = await client.post(
        "/settings/cdpi/direct-company-items",
        json={
            "item_name":        "HTTP Token Test Number Item",
            "input_type":       "Number",
            "calc_method_key":  "PerUnit",
            "notes":            "CDPI-HTTP-1 regression test",
        },
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 201, (
        f"Expected 201 from direct-company-items; got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_direct_create_response_shape(client, auth_token):
    """Successful creation returns the expected summary fields."""
    resp = await client.post(
        "/settings/cdpi/direct-company-items",
        json={
            "item_name":       "HTTP Token Test Shape Check",
            "input_type":      "Number",
            "calc_method_key": "PerUnit",
        },
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "pay_item_id" in data
    assert isinstance(data["pay_item_id"], int)
    assert data["input_type"] == "Number"
    assert data["calc_method_key"] == "PerUnit"
    assert isinstance(data["created_by_user_id"], int)


# ---------------------------------------------------------------------------
# 3. POST /settings/cdpi/requests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_request_no_500(client, auth_token, hq_branch_id):
    """Route returns 201 (not 500) — token cast works end-to-end."""
    resp = await client.post(
        "/settings/cdpi/requests",
        json={
            "requesting_branch_id": hq_branch_id,
            "item_name":            "HTTP Token Test Time Request",
            "input_type":           "Time",
            "calc_method_key":      "PerUnit",
            "notes":                "CDPI-HTTP-1 regression test",
        },
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 201, (
        f"Expected 201 from create_request; got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_create_request_response_shape(client, auth_token, hq_branch_id):
    """Successful request creation returns a Draft summary with expected fields."""
    resp = await client.post(
        "/settings/cdpi/requests",
        json={
            "requesting_branch_id": hq_branch_id,
            "item_name":            "HTTP Token Test Request Shape",
            "input_type":           "Number",
            "calc_method_key":      "PerUnit",
        },
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "request_id" in data
    assert data["status"] == "Draft"
    assert data["requesting_branch_id"] == hq_branch_id
    assert isinstance(data["created_by_user_id"], int)


# ---------------------------------------------------------------------------
# 4. GET /settings/cdpi/requests  (list — also exercises token extraction)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_requests_no_500(client, auth_token):
    """GET list endpoint returns 200 after token-cast fix."""
    resp = await client.get(
        "/settings/cdpi/requests",
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 200, (
        f"Expected 200 from list_requests; got {resp.status_code}: {resp.text}"
    )
    assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# 5. Unauthenticated requests return 401/403 (not 500 from token cast)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_branch_items_no_auth(client, hq_branch_id):
    """Missing token → 401 or 403, never 500."""
    resp = await client.get(f"/settings/cdpi/branches/{hq_branch_id}/items")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_direct_create_no_auth(client):
    """Missing token → 401 or 403, never 500."""
    resp = await client.post(
        "/settings/cdpi/direct-company-items",
        json={"item_name": "x", "input_type": "Number", "calc_method_key": "PerUnit"},
    )
    assert resp.status_code in (401, 403)
