"""Integration tests for the Company Status catalog (keys, categories, lifecycle).

Status keys belong to the Company, so the tests share one Company catalog and give every
Status a unique name (the normalized name is unique among a Company's active Statuses).
"""
import re
from uuid import uuid4

import httpx
import pytest

CATEGORIES = ["Vacation", "Sick", "Bereavement", "Jury Duty", "Personal", "Holiday", "Unpaid", "Other"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def uname(prefix: str = "Status") -> str:
    """A name no other Status of the shared test Company has."""
    return f"{prefix} {uuid4().hex[:10]}"


async def make_key(
    client: httpx.AsyncClient, token: str, **overrides,
) -> dict:
    body = {"key_name": uname(), "category": "Vacation"} | overrides
    resp = await client.post("/settings/status-keys", json=body, headers=auth(token))
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

class TestGetStatusKeys:

    async def test_list_includes_created_key(self, client: httpx.AsyncClient, auth_token: str):
        key = await make_key(client, auth_token)
        resp = await client.get("/settings/status-keys", headers=auth(auth_token))
        assert resp.status_code == 200
        assert key["status_key_id"] in [k["status_key_id"] for k in resp.json()]

    async def test_list_is_ordered_by_display_order_then_name(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        suffix = uuid4().hex[:8]
        created = [
            await make_key(client, auth_token, key_name=f"Zzz {suffix}", display_order=2),
            await make_key(client, auth_token, key_name=f"Aaa {suffix}", display_order=2),
            await make_key(client, auth_token, key_name=f"Mmm {suffix}", display_order=1),
        ]
        resp = await client.get("/settings/status-keys", headers=auth(auth_token))
        mine = [k["key_name"] for k in resp.json()
                if k["status_key_id"] in {c["status_key_id"] for c in created}]
        assert mine == [f"Mmm {suffix}", f"Aaa {suffix}", f"Zzz {suffix}"]

    async def test_include_inactive_flag(self, client: httpx.AsyncClient, auth_token: str):
        key = await make_key(client, auth_token)
        kid = key["status_key_id"]
        assert (await client.delete(f"/settings/status-keys/{kid}", headers=auth(auth_token))).status_code == 200

        active = await client.get("/settings/status-keys", headers=auth(auth_token))
        assert kid not in [k["status_key_id"] for k in active.json()]
        everything = await client.get(
            "/settings/status-keys?include_inactive=true", headers=auth(auth_token))
        assert kid in [k["status_key_id"] for k in everything.json()]

    async def test_branch_user_can_read_the_company_catalog(
        self, client: httpx.AsyncClient, auth_token: str, branch_user_token: str,
    ):
        """The catalog is shared by every Branch: a user scoped to one Branch reads all of it."""
        key = await make_key(client, auth_token)
        resp = await client.get("/settings/status-keys", headers=auth(branch_user_token))
        assert resp.status_code == 200
        assert key["status_key_id"] in [k["status_key_id"] for k in resp.json()]

    async def test_catalog_response_carries_the_catalog_fields(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        key = await make_key(client, auth_token, aliases=["Vac"], hours_value="8")
        assert key["category"] == "Vacation"
        assert key["aliases"] == ["Vac"]
        assert key["is_in_use"] is False
        assert "branch_id" not in key
        assert not any(name.startswith("limit_uses") for name in key)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

class TestCreateStatusKey:

    async def test_create_with_name_and_category_only(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        name = uname("On Leave")
        body = await make_key(client, auth_token, key_name=name, category="Other")
        assert body["key_name"] == name
        assert body["category"] == "Other"
        assert float(body["hours_value"]) == 0.0
        assert body["is_off_reason"] is True
        assert body["deducts_from_yearly_allowance"] is False
        assert body["is_active"] is True
        assert body["status_rate_column_id"] is None
        assert body["aliases"] == []

    async def test_create_generates_sk_code(self, client: httpx.AsyncClient, auth_token: str):
        body = await make_key(client, auth_token)
        assert re.fullmatch(r"SK_[A-Z0-9]{8}", body["status_code"])

    async def test_two_keys_have_different_codes(self, client: httpx.AsyncClient, auth_token: str):
        first = await make_key(client, auth_token)
        second = await make_key(client, auth_token)
        assert first["status_code"] != second["status_code"]

    async def test_status_code_is_not_user_overridable(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        body = await make_key(client, auth_token, status_code="MANUAL_CODE")
        assert re.fullmatch(r"SK_[A-Z0-9]{8}", body["status_code"])

    async def test_create_full_key(self, client: httpx.AsyncClient, auth_token: str):
        body = await make_key(
            client, auth_token, hours_value="8.00", is_off_reason=True, category="Sick")
        assert float(body["hours_value"]) == 8.0
        assert body["is_off_reason"] is True
        assert body["category"] == "Sick"

    @pytest.mark.parametrize("category", CATEGORIES)
    async def test_every_fixed_category_is_accepted(
        self, client: httpx.AsyncClient, auth_token: str, category: str,
    ):
        body = await make_key(client, auth_token, category=category)
        assert body["category"] == category

    async def test_category_is_required(self, client: httpx.AsyncClient, auth_token: str):
        resp = await client.post(
            "/settings/status-keys", json={"key_name": uname()}, headers=auth(auth_token))
        assert resp.status_code == 422

    @pytest.mark.parametrize("category", ["Weekend", "", "vacation", None])
    async def test_category_outside_the_fixed_set_is_rejected(
        self, client: httpx.AsyncClient, auth_token: str, category,
    ):
        resp = await client.post(
            "/settings/status-keys",
            json={"key_name": uname(), "category": category}, headers=auth(auth_token))
        assert resp.status_code == 422

    async def test_hours_above_24_rejected(self, client: httpx.AsyncClient, auth_token: str):
        resp = await client.post(
            "/settings/status-keys",
            json={"key_name": uname(), "category": "Other", "hours_value": "25"},
            headers=auth(auth_token))
        assert resp.status_code == 422

    async def test_hours_zero_allowed(self, client: httpx.AsyncClient, auth_token: str):
        body = await make_key(client, auth_token, hours_value="0")
        assert float(body["hours_value"]) == 0.0

    async def test_deducts_true_rejected_future_guard(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        resp = await client.post(
            "/settings/status-keys",
            json={"key_name": uname(), "category": "Vacation",
                  "deducts_from_yearly_allowance": True},
            headers=auth(auth_token))
        assert resp.status_code == 422
        assert "not available yet" in str(resp.json()["detail"]).lower()

    @pytest.mark.parametrize("name", ["   ", ""])
    async def test_blank_key_name_rejected(
        self, client: httpx.AsyncClient, auth_token: str, name: str,
    ):
        resp = await client.post(
            "/settings/status-keys",
            json={"key_name": name, "category": "Other"}, headers=auth(auth_token))
        assert resp.status_code == 422

    async def test_missing_key_name_rejected(self, client: httpx.AsyncClient, auth_token: str):
        resp = await client.post(
            "/settings/status-keys",
            json={"category": "Other", "hours_value": "8"}, headers=auth(auth_token))
        assert resp.status_code == 422

    async def test_overlong_key_name_rejected(self, client: httpx.AsyncClient, auth_token: str):
        resp = await client.post(
            "/settings/status-keys",
            json={"key_name": "x" * 201, "category": "Other"}, headers=auth(auth_token))
        assert resp.status_code == 422

    async def test_branch_user_denied(
        self, client: httpx.AsyncClient, branch_user_token: str,
    ):
        resp = await client.post(
            "/settings/status-keys",
            json={"key_name": uname(), "category": "Other"}, headers=auth(branch_user_token))
        assert resp.status_code == 403

    async def test_monetary_status_needs_hours(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        column = (await client.get("/settings/status-rate-columns", headers=auth(auth_token))).json()[0]
        resp = await client.post(
            "/settings/status-keys",
            json={"key_name": uname(), "category": "Vacation", "hours_value": "0",
                  "status_rate_column_id": column["status_rate_column_id"]},
            headers=auth(auth_token))
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "STATUS_HOURS_REQUIRED"

    async def test_unknown_rate_column_rejected(self, client: httpx.AsyncClient, auth_token: str):
        resp = await client.post(
            "/settings/status-keys",
            json={"key_name": uname(), "category": "Vacation", "hours_value": "8",
                  "status_rate_column_id": 999999},
            headers=auth(auth_token))
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "STATUS_RATE_COLUMN_NOT_FOUND"

    async def test_create_bound_to_the_default_column(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        columns = (await client.get("/settings/status-rate-columns", headers=auth(auth_token))).json()
        default = next(c for c in columns if c["is_default"])
        body = await make_key(
            client, auth_token, hours_value="8",
            status_rate_column_id=default["status_rate_column_id"])
        assert body["status_rate_column_id"] == default["status_rate_column_id"]


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

class TestUpdateStatusKey:

    async def test_patch_hours_keeps_everything_else(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        before = await make_key(client, auth_token, hours_value="8.00", aliases=["Alias One"])
        resp = await client.patch(
            f"/settings/status-keys/{before['status_key_id']}",
            json={"hours_value": "6.50"}, headers=auth(auth_token))
        assert resp.status_code == 200
        body = resp.json()
        assert float(body["hours_value"]) == 6.5
        for field in ("key_name", "category", "is_off_reason", "is_active", "aliases", "status_code"):
            assert body[field] == before[field]

    async def test_patch_key_name_keeps_the_generated_code(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        created = await make_key(client, auth_token)
        new_name = uname("Renamed")
        resp = await client.patch(
            f"/settings/status-keys/{created['status_key_id']}",
            json={"key_name": new_name}, headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json()["key_name"] == new_name
        assert resp.json()["status_code"] == created["status_code"]

    async def test_patch_not_found(self, client: httpx.AsyncClient, auth_token: str):
        resp = await client.patch(
            "/settings/status-keys/999999", json={"hours_value": "4"}, headers=auth(auth_token))
        assert resp.status_code == 404

    async def test_branch_user_denied(
        self, client: httpx.AsyncClient, auth_token: str, branch_user_token: str,
    ):
        key = await make_key(client, auth_token)
        resp = await client.patch(
            f"/settings/status-keys/{key['status_key_id']}",
            json={"hours_value": "1"}, headers=auth(branch_user_token))
        assert resp.status_code == 403

    async def test_patch_can_clear_the_rate_column(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        columns = (await client.get("/settings/status-rate-columns", headers=auth(auth_token))).json()
        key = await make_key(
            client, auth_token, hours_value="8",
            status_rate_column_id=columns[0]["status_rate_column_id"])
        resp = await client.patch(
            f"/settings/status-keys/{key['status_key_id']}",
            json={"status_rate_column_id": None}, headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json()["status_rate_column_id"] is None

    async def test_patch_unknown_category_rejected(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        key = await make_key(client, auth_token)
        resp = await client.patch(
            f"/settings/status-keys/{key['status_key_id']}",
            json={"category": "Weekend"}, headers=auth(auth_token))
        assert resp.status_code == 422

    async def test_patch_display_order_and_active_flag(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        key = await make_key(client, auth_token)
        resp = await client.patch(
            f"/settings/status-keys/{key['status_key_id']}",
            json={"display_order": 7, "is_active": False}, headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json()["display_order"] == 7
        assert resp.json()["is_active"] is False


# ---------------------------------------------------------------------------
# Deactivate
# ---------------------------------------------------------------------------

class TestDeleteStatusKey:

    async def test_soft_delete(self, client: httpx.AsyncClient, auth_token: str):
        key = await make_key(client, auth_token)
        resp = await client.delete(
            f"/settings/status-keys/{key['status_key_id']}", headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False
        assert resp.json()["status_key_id"] == key["status_key_id"]

    async def test_delete_idempotent(self, client: httpx.AsyncClient, auth_token: str):
        key = await make_key(client, auth_token)
        url = f"/settings/status-keys/{key['status_key_id']}"
        assert (await client.delete(url, headers=auth(auth_token))).status_code == 200
        second = await client.delete(url, headers=auth(auth_token))
        assert second.status_code == 200
        assert second.json()["is_active"] is False

    async def test_delete_not_found(self, client: httpx.AsyncClient, auth_token: str):
        resp = await client.delete("/settings/status-keys/999999", headers=auth(auth_token))
        assert resp.status_code == 404

    async def test_delete_branch_user_denied(
        self, client: httpx.AsyncClient, auth_token: str, branch_user_token: str,
    ):
        key = await make_key(client, auth_token)
        resp = await client.delete(
            f"/settings/status-keys/{key['status_key_id']}", headers=auth(branch_user_token))
        assert resp.status_code == 403

    async def test_deactivated_name_can_be_reused(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        """After a Status is deactivated a new Status may take its name; each has its own code."""
        name = uname("Reusable")
        first = await make_key(client, auth_token, key_name=name, hours_value="4")
        await client.delete(
            f"/settings/status-keys/{first['status_key_id']}", headers=auth(auth_token))
        second = await make_key(client, auth_token, key_name=name, hours_value="4")
        assert second["status_code"] != first["status_code"]
        assert second["key_name"] == name


# ---------------------------------------------------------------------------
# Yearly allowance guard
# ---------------------------------------------------------------------------

class TestYearlyAllowanceGuard:
    """deducts_from_yearly_allowance is a future-only feature: true is rejected, false is fine."""

    async def test_create_with_deducts_false_succeeds(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        body = await make_key(client, auth_token, deducts_from_yearly_allowance=False)
        assert body["deducts_from_yearly_allowance"] is False

    async def test_patch_deducts_true_rejected(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        key = await make_key(client, auth_token)
        resp = await client.patch(
            f"/settings/status-keys/{key['status_key_id']}",
            json={"deducts_from_yearly_allowance": True}, headers=auth(auth_token))
        assert resp.status_code == 422
        assert "not available yet" in str(resp.json()["detail"]).lower()

    async def test_patch_deducts_false_succeeds(
        self, client: httpx.AsyncClient, auth_token: str,
    ):
        key = await make_key(client, auth_token)
        resp = await client.patch(
            f"/settings/status-keys/{key['status_key_id']}",
            json={"deducts_from_yearly_allowance": False}, headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json()["deducts_from_yearly_allowance"] is False
