from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_supported_time_zone_catalog_is_authenticated_and_database_backed(client, auth_token):
    unauthorized = await client.get("/settings/time-zones")
    assert unauthorized.status_code == 401

    response = await client.get("/settings/time-zones", headers=_auth(auth_token))
    assert response.status_code == 200
    values = response.json()
    assert "UTC" in values
    assert "America/New_York" in values
    assert "Pacific/Kiritimati" in values
    assert "EST" not in values
    assert "+05:00" not in values


@pytest.mark.parametrize("payload", [
    {"branch_name": "Missing zone"},
    {"branch_name": "Null zone", "time_zone_id": None},
    {"branch_name": "Blank zone", "time_zone_id": "   "},
    {"branch_name": "Unsupported zone", "time_zone_id": "EST"},
    {"branch_name": "Offset zone", "time_zone_id": "+05:00"},
])
async def test_branch_creation_requires_a_supported_time_zone(client, auth_token, payload):
    response = await client.post("/settings/branches", json=payload, headers=_auth(auth_token))
    assert response.status_code == 422


async def test_branch_creation_persists_exact_supported_time_zone(client, auth_token):
    response = await client.post(
        "/settings/branches",
        json={"branch_name": "Explicit Phoenix zone", "time_zone_id": "America/Phoenix"},
        headers=_auth(auth_token),
    )
    assert response.status_code == 201, response.text
    assert response.json()["time_zone_id"] == "America/Phoenix"

    branch_id = response.json()["branch_id"]
    omitted = await client.patch(
        f"/settings/branches/{branch_id}",
        json={"branch_name": "Explicit Phoenix zone renamed"},
        headers=_auth(auth_token),
    )
    assert omitted.status_code == 200, omitted.text
    assert omitted.json()["time_zone_id"] == "America/Phoenix"


@pytest.mark.parametrize("time_zone_id", [None, "", "  ", "EST"])
async def test_branch_timezone_patch_rejects_null_blank_and_unsupported_values(
    client, auth_token, time_zone_id,
):
    created = await client.post(
        "/settings/branches",
        json={"branch_name": f"Patch timezone validation {uuid4().hex[:8]}", "time_zone_id": "UTC"},
        headers=_auth(auth_token),
    )
    assert created.status_code == 201, created.text
    branch_id = created.json()["branch_id"]

    rejected = await client.patch(
        f"/settings/branches/{branch_id}",
        json={"time_zone_id": time_zone_id},
        headers=_auth(auth_token),
    )
    assert rejected.status_code == 422, rejected.text

    unchanged = await client.get(f"/settings/branches/{branch_id}", headers=_auth(auth_token))
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["time_zone_id"] == "UTC"
