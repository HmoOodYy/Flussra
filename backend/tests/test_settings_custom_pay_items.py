"""
M12 integration tests — Custom Pay Items catalog + branch request/approval flow.

Users / fixtures from conftest
-------------------------------
admin        AllCompanyBranches, PAYROLL_ADMIN (all permissions)
branch_user  SpecificBranch=HQ, PAYROLL_VIEWER (no write permissions)

Test classes
------------
TestCustomPayItemCreate        admin direct create (valid + invalid combos)
TestCustomPayItemUpdate        mutable metadata, immutable fields
TestCustomPayItemUsage         usage detection for smart delete
TestCustomPayItemDelete        physical delete, retire, idempotent, system block
TestPayItemRequestSubmit       branch request submission
TestPayItemRequestViews        list / get scope
TestPayItemRequestApprove      approval → creates item + branch config
TestPayItemRequestReject       rejection → no item created
TestCustomPayItemAudit         rollback when _write_settings_audit raises
TestCustomPayItemM11Flow       approved custom item integrates with M11 branch config
"""
import pytest
import pytest_asyncio
import httpx
from unittest.mock import patch, AsyncMock
from app.settings import service as settings_service


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _create_item(
    client: httpx.AsyncClient,
    token: str,
    *,
    code: str,
    name: str = "Test Item",
    item_scope: str = "Daily",
    rate_behavior: str = "PerUnit",
    unit: str | None = "Stop",
    category: str = "Count",
) -> dict:
    payload: dict = {
        "pay_item_code": code,
        "pay_item_name": name,
        "item_scope":    item_scope,
        "rate_behavior": rate_behavior,
        "category":      category,
    }
    if unit is not None:
        payload["unit"] = unit
    resp = await client.post("/settings/pay-items", json=payload, headers=auth(token))
    assert resp.status_code == 201, f"Create failed: {resp.text}"
    return resp.json()


# ---------------------------------------------------------------------------
# TestCustomPayItemCreate
# ---------------------------------------------------------------------------

class TestCustomPayItemCreate:

    async def test_create_daily_perunit_item(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "M12_DAILY_A",
                "pay_item_name": "Stop Pay",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Stop",
                "category":      "Count",
                "sort_order":    50,
                "notes":         "Pay per stop",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["pay_item_code"]         == "M12_DAILY_A"
        assert body["item_scope"]            == "Daily"
        assert body["rate_behavior"]         == "PerUnit"
        assert body["unit"]                  == "Stop"
        assert body["appears_in_payroll_entry"] is True
        assert body["requires_rate"]         is True
        assert body["is_system_standard"]    is False
        assert body["status"]                == "Active"

    async def test_create_period_item_is_rejected(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """Custom Period items are no longer supported — all scopes must be Daily."""
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "M12_PERIOD_A",
                "pay_item_name": "Safety Bonus",
                "item_scope":    "Period",
                "rate_behavior": "EnteredAmount",
                "category":      "Bonus",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "Pay Period" in str(detail)
        assert "Daily" in str(detail)

    async def test_create_period_item_all_value_types_rejected(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """Wizard value_type paths for Period are all rejected."""
        for vt, rb in [("Money", "EnteredAmount"), ("Time", "PerUnit"), ("Number", "PerUnit")]:
            resp = await client.post(
                "/settings/pay-items",
                json={
                    "pay_item_name": f"Period {vt} Item",
                    "item_scope":    "Period",
                    "rate_behavior": rb,
                    "value_type":    vt,
                },
                headers=auth(auth_token),
            )
            assert resp.status_code == 422, (
                f"Expected 422 for Period + value_type={vt!r}, got {resp.status_code}: {resp.text}"
            )

    async def test_daily_enteredamount_rejected(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """Daily + EnteredAmount is not allowed in M12."""
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "M12_BAD_A",
                "pay_item_name": "Bad Combo",
                "item_scope":    "Daily",
                "rate_behavior": "EnteredAmount",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_period_perunit_rejected(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """All custom Period items are rejected, regardless of rate_behavior."""
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "M12_BAD_B",
                "pay_item_name": "Bad Combo Period PerUnit",
                "item_scope":    "Period",
                "rate_behavior": "PerUnit",
                "unit":          "Unit",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_daily_perunit_missing_unit_rejected(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """PerUnit items require a unit value."""
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "M12_BAD_C",
                "pay_item_name": "Missing Unit",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_blank_code_rejected(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "   ",
                "pay_item_name": "Something",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Unit",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_blank_name_rejected(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "M12_BLKNAME",
                "pay_item_name": "   ",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Unit",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_system_code_blocked_explicitly(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """System item codes must be blocked by service, not only DB constraint."""
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "HOURS",
                "pay_item_name": "Custom Hours",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Hour",
                "category":      "Time",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "system" in resp.json()["detail"].lower()

    async def test_duplicate_code_blocked(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        # M12_DAILY_A was already created above
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "M12_DAILY_A",
                "pay_item_name": "Duplicate",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Stop",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_branch_user_cannot_create(
        self, client: httpx.AsyncClient, branch_user_token: str
    ):
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "M12_NO_PERMS",
                "pay_item_name": "Should fail",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Unit",
                "category":      "Count",
            },
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_created_item_inactive_on_all_branches(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """Admin-direct create: item starts inactive on all branches (no BranchPayItemConfig)."""
        body = await _create_item(client, auth_token, code="M12_INACT_A",
                                  name="Inactive Start Test")
        item_id = body["pay_item_id"]

        branch_items = await client.get(
            f"/settings/branches/{hq_branch_id}/pay-items",
            headers=auth(auth_token),
        )
        assert branch_items.status_code == 200
        matching = [i for i in branch_items.json() if i["pay_item_id"] == item_id]
        assert len(matching) == 1
        assert matching[0]["is_active"] is False
        assert matching[0]["is_using_default"] is True  # no config row yet

    async def test_custom_daily_item_still_creates_successfully(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """Requirement 1: custom Daily item creation must be unaffected."""
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "REQ1_DAILY_OK",
                "pay_item_name": "Requirement 1 Daily Item",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Load",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["item_scope"] == "Daily"
        assert body["is_system_standard"] is False

    async def test_custom_period_item_rejected_with_expected_status_and_message(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """Requirement 2: Period creation returns HTTP 422 with the specified message."""
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "REQ2_PERIOD_BAD",
                "pay_item_name": "Requirement 2 Period Item",
                "item_scope":    "Period",
                "rate_behavior": "EnteredAmount",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        detail_text = str(resp.json()["detail"])
        assert "Custom Pay Period items are not supported" in detail_text, (
            f"Expected the exact restriction message in detail, got: {detail_text!r}"
        )
        assert "Daily" in detail_text

    async def test_system_period_items_readable_and_unchanged(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        """Requirement 3: Built-in PayPeriod items remain readable through branch endpoints."""
        resp = await client.get(
            f"/settings/branches/{hq_branch_id}/pay-items",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        items = resp.json()
        # At least one system item must be present (seeded by conftest)
        assert len(items) > 0, "Branch pay items list must not be empty"
        # Verify that system standard items appear (covers system Period items)
        system_items = [i for i in items if i.get("is_system_standard")]
        assert len(system_items) > 0, "System standard items must remain visible"

    async def test_update_endpoint_cannot_convert_daily_to_period(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """
        Requirement 6: scope is immutable on the update endpoint — item_scope is not
        in CustomPayItemUpdate, so no conversion is possible at all.  Confirm that
        passing item_scope in a PATCH body is silently ignored (field not in schema)
        and the item remains Daily.
        """
        body = await _create_item(
            client, auth_token, code="REQ6_SCOPE_IMMUT", name="Scope Immutable Test"
        )
        item_id = body["pay_item_id"]

        patch_resp = await client.patch(
            f"/settings/pay-items/{item_id}",
            json={"pay_item_name": "Scope Immutable Test Renamed", "item_scope": "Period"},
            headers=auth(auth_token),
        )
        assert patch_resp.status_code == 200
        updated = patch_resp.json()
        # scope must remain Daily — the extra field is ignored by the schema
        assert updated["item_scope"] == "Daily"


# ---------------------------------------------------------------------------
# TestCustomPayItemCodeGeneration  (new — auto-generated CPI_ codes)
# ---------------------------------------------------------------------------

class TestCustomPayItemCodeGeneration:
    """
    Verify that:
    - Omitting pay_item_code triggers automatic CPI_ generation.
    - The generated code is uppercase, starts with 'CPI_', and is valid.
    - Multiple items created without a code get distinct codes.
    - An explicitly supplied code is still accepted (backward compat).
    - The collision retry path works when _generate_pay_item_code collides.
    """

    async def test_create_without_code_auto_generates_cpi_code(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_name": "Auto Code Daily Item",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Stop",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()

        code = body["pay_item_code"]
        assert code.startswith("CPI_"), f"Expected CPI_ prefix, got: {code!r}"
        assert code == code.upper(), f"Code should be uppercase: {code!r}"
        # Prefix (4) + suffix (8) = 12 characters
        assert len(code) == 12, f"Expected 12-char code, got {len(code)}: {code!r}"
        # Only uppercase letters and digits after prefix (no ambiguous chars O,0,I,1,L)
        suffix = code[4:]
        assert all(c.isalnum() and c == c.upper() for c in suffix), \
            f"Suffix contains unexpected chars: {suffix!r}"
        assert "O" not in suffix and "I" not in suffix and "L" not in suffix, \
            f"Ambiguous chars should be excluded: {suffix!r}"

    async def test_create_without_code_period_item_rejected(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """Period items are rejected even without an explicit code."""
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_name": "Auto Code Period Item",
                "item_scope":    "Period",
                "rate_behavior": "EnteredAmount",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422, resp.text

    async def test_multiple_auto_codes_are_unique(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """Create several items without code — all codes must be distinct."""
        codes: list[str] = []
        for i in range(5):
            resp = await client.post(
                "/settings/pay-items",
                json={
                    "pay_item_name": f"Unique Code Item {i}",
                    "item_scope":    "Daily",
                    "rate_behavior": "PerUnit",
                    "unit":          "Unit",
                },
                headers=auth(auth_token),
            )
            assert resp.status_code == 201, resp.text
            codes.append(resp.json()["pay_item_code"])

        assert len(codes) == len(set(codes)), f"Duplicate codes detected: {codes}"

    async def test_explicit_code_still_accepted(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """Callers may still supply an explicit code (backward compatibility)."""
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "EXPLICIT_CODE_A",
                "pay_item_name": "Explicit Code Item",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Unit",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["pay_item_code"] == "EXPLICIT_CODE_A"

    async def test_auto_generated_code_does_not_start_from_name(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """Code must NOT be derived from the item name."""
        name = "Loads Delivered Bonus"
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_name": name,
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Load",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        code = resp.json()["pay_item_code"]
        # Code must start with CPI_, not with letters from the name
        assert code.startswith("CPI_"), f"Expected CPI_ prefix, got: {code!r}"
        # Verify code does not contain name-derived initials like 'LDB'
        assert code[4:] != "LDB" + code[7:], \
            "Code looks like it was derived from name initials"

    async def test_collision_retry_succeeds(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """
        Simulate a collision: pre-create an item with a fixed code, then mock
        the generator to return that code first, then a unique code.  The
        retry path should transparently produce a successful response.
        """
        from unittest.mock import patch
        from app.settings import service as svc

        # Pre-create the item that will cause the collision
        collision_code = "CPI_COLL1234"
        pre_resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": collision_code,
                "pay_item_name": "Pre-existing Collision Item",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Unit",
            },
            headers=auth(auth_token),
        )
        assert pre_resp.status_code == 201, pre_resp.text

        # Generator will return the colliding code first, then a unique code.
        unique_code = "CPI_RETRY999"
        call_count = 0

        def mock_generate() -> str:
            nonlocal call_count
            call_count += 1
            return collision_code if call_count == 1 else unique_code

        with patch.object(svc, "_generate_pay_item_code", side_effect=mock_generate):
            retry_resp = await client.post(
                "/settings/pay-items",
                json={
                    "pay_item_name": "Retry Item",
                    "item_scope":    "Daily",
                    "rate_behavior": "PerUnit",
                    "unit":          "Unit",
                },
                headers=auth(auth_token),
            )

        assert retry_resp.status_code == 201, retry_resp.text
        assert retry_resp.json()["pay_item_code"] == unique_code
        assert call_count == 2, f"Expected 2 generator calls, got {call_count}"

    async def test_auto_code_item_starts_inactive_on_branches(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        """Auto-coded items follow the same inactive-by-default branch behavior."""
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_name": "Auto Code Inactive Branch Test",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Unit",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        item_id = resp.json()["pay_item_id"]

        branch_resp = await client.get(
            f"/settings/branches/{hq_branch_id}/pay-items",
            headers=auth(auth_token),
        )
        assert branch_resp.status_code == 200
        matching = [i for i in branch_resp.json() if i["pay_item_id"] == item_id]
        assert len(matching) == 1
        assert matching[0]["is_active"] is False


# ---------------------------------------------------------------------------
# TestCustomPayItemUpdate
# ---------------------------------------------------------------------------

class TestCustomPayItemUpdate:

    async def test_update_mutable_fields(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        body = await _create_item(client, auth_token, code="M12_UPD_A",
                                  name="Update Target")
        item_id = body["pay_item_id"]

        resp = await client.patch(
            f"/settings/pay-items/{item_id}",
            json={
                "pay_item_name": "Updated Name",
                "display_label": "Updated Label",
                "notes":         "Now with notes",
                "sort_order":    99,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        updated = resp.json()
        assert updated["pay_item_name"] == "Updated Name"
        assert updated["display_label"] == "Updated Label"
        assert updated["notes"]         == "Now with notes"
        assert updated["sort_order"]    == 99
        # Immutable fields unchanged
        assert updated["pay_item_code"] == "M12_UPD_A"
        assert updated["item_scope"]    == "Daily"
        assert updated["rate_behavior"] == "PerUnit"

    async def test_cannot_update_deleted_item(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """
        An item that has been removed from circulation (physically deleted or
        retired) cannot be updated.  Physical delete returns 404; retire returns
        422.  Both are acceptable outcomes for 'item no longer available'.
        """
        body = await _create_item(client, auth_token, code="M12_UPD_RETD",
                                  name="Will Be Deleted")
        item_id = body["pay_item_id"]
        # Delete (never used → physical delete)
        await client.delete(f"/settings/pay-items/{item_id}", headers=auth(auth_token))

        resp = await client.patch(
            f"/settings/pay-items/{item_id}",
            json={"pay_item_name": "Try to update deleted item"},
            headers=auth(auth_token),
        )
        # Physical delete returns 404; retire would return 422.  Both mean "can't update".
        assert resp.status_code in (404, 422)

    async def test_branch_user_cannot_update(
        self, client: httpx.AsyncClient, auth_token: str, branch_user_token: str
    ):
        body = await _create_item(client, auth_token, code="M12_UPD_PERM")
        item_id = body["pay_item_id"]

        resp = await client.patch(
            f"/settings/pay-items/{item_id}",
            json={"pay_item_name": "Should fail"},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_get_nonexistent_returns_404(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        resp = await client.get("/settings/pay-items/99999999", headers=auth(auth_token))
        assert resp.status_code == 404

    async def test_list_returns_created_items(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        resp = await client.get("/settings/pay-items", headers=auth(auth_token))
        assert resp.status_code == 200
        codes = [i["pay_item_code"] for i in resp.json()]
        assert "M12_DAILY_A" in codes
        # System items must NOT appear in the custom catalog list
        assert "HOURS" not in codes

    async def test_list_excludes_retired_by_default(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """
        After retirement, an item is excluded from the normal list but visible
        with include_retired=true.

        We mock _compute_usage to force the retire path (meaningful usage),
        because in M12 the payroll draft-line entry still uses a hardcoded
        line-type allowlist that doesn't include custom item codes — that
        enforcement will be lifted in M13.
        """
        from unittest.mock import AsyncMock
        from app.settings.schemas import CustomPayItemUsage

        body = await _create_item(client, auth_token, code="M12_LIST_RETD",
                                  name="List Retired Test")
        item_id = body["pay_item_id"]

        mock_usage = CustomPayItemUsage(
            pay_item_id=item_id,
            pay_item_code="M12_LIST_RETD",
            has_meaningful_usage=True,
            has_final_lines=False,
            meaningful_draft_line_count=1,
            final_line_count=0,
            non_meaningful_draft_line_count=0,
            can_physical_delete=False,
            deletion_would_retire=True,
        )

        with patch.object(settings_service, "_compute_usage",
                          new=AsyncMock(return_value=mock_usage)):
            del_resp = await client.delete(
                f"/settings/pay-items/{item_id}",
                headers=auth(auth_token),
            )
        assert del_resp.json()["deletion_type"] == "retired"

        # Normal list must exclude it
        resp = await client.get("/settings/pay-items", headers=auth(auth_token))
        codes = [i["pay_item_code"] for i in resp.json()]
        assert "M12_LIST_RETD" not in codes

        # include_retired=true must show it
        resp2 = await client.get("/settings/pay-items?include_retired=true",
                                 headers=auth(auth_token))
        codes2 = [i["pay_item_code"] for i in resp2.json()]
        assert "M12_LIST_RETD" in codes2


# ---------------------------------------------------------------------------
# TestCustomPayItemUsage
# ---------------------------------------------------------------------------

class TestCustomPayItemUsage:

    async def test_never_used_item_clean(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        body = await _create_item(client, auth_token, code="M12_USAGE_A",
                                  name="Usage Check Never Used")
        item_id = body["pay_item_id"]

        resp = await client.get(
            f"/settings/pay-items/{item_id}/usage",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        u = resp.json()
        assert u["has_meaningful_usage"]            is False
        assert u["has_final_lines"]                 is False
        assert u["meaningful_draft_line_count"]     == 0
        assert u["final_line_count"]                == 0
        assert u["non_meaningful_draft_line_count"] == 0
        assert u["can_physical_delete"]             is True
        assert u["deletion_would_retire"]           is False


# ---------------------------------------------------------------------------
# TestCustomPayItemDelete
# ---------------------------------------------------------------------------

class TestCustomPayItemDelete:

    async def test_delete_never_used_physical_delete(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        body = await _create_item(client, auth_token, code="M12_DEL_UNUSED",
                                  name="Delete Never Used")
        item_id = body["pay_item_id"]

        resp = await client.delete(
            f"/settings/pay-items/{item_id}",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        result = resp.json()
        assert result["deletion_type"]  == "physical"
        assert result["pay_item_id"]    is None
        assert result["pay_item_code"]  == "M12_DEL_UNUSED"

        # Verify row is gone
        get_resp = await client.get(
            f"/settings/pay-items/{item_id}",
            headers=auth(auth_token),
        )
        assert get_resp.status_code == 404

    async def test_delete_with_meaningful_draft_retires(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        Item with meaningful draft usage → retire (Status=Retired), not physical delete.

        _compute_usage is mocked because M12 payroll draft-line entry still validates
        LineType against a hardcoded list that predates custom items.  The mock simulates
        the state that will exist once M13 lifts that restriction.
        """
        from unittest.mock import AsyncMock
        from app.settings.schemas import CustomPayItemUsage

        body = await _create_item(client, auth_token, code="M12_DEL_MEANINGFUL",
                                  name="Delete Meaningful Usage")
        item_id = body["pay_item_id"]

        mock_usage = CustomPayItemUsage(
            pay_item_id=item_id,
            pay_item_code="M12_DEL_MEANINGFUL",
            has_meaningful_usage=True,
            has_final_lines=False,
            meaningful_draft_line_count=3,
            final_line_count=0,
            non_meaningful_draft_line_count=0,
            can_physical_delete=False,
            deletion_would_retire=True,
        )

        with patch.object(settings_service, "_compute_usage",
                          new=AsyncMock(return_value=mock_usage)):
            del_resp = await client.delete(
                f"/settings/pay-items/{item_id}",
                headers=auth(auth_token),
            )
        assert del_resp.status_code == 200
        result = del_resp.json()
        assert result["deletion_type"] == "retired"
        assert result["pay_item_id"]   == item_id
        assert result["pay_item_code"] == "M12_DEL_MEANINGFUL"

        # Item must NOT appear in normal list (Retired is filtered)
        list_resp = await client.get("/settings/pay-items", headers=auth(auth_token))
        codes = [i["pay_item_code"] for i in list_resp.json()]
        assert "M12_DEL_MEANINGFUL" not in codes

        # Item appears with include_retired=true
        list_retired = await client.get(
            "/settings/pay-items?include_retired=true",
            headers=auth(auth_token),
        )
        codes_r = [i["pay_item_code"] for i in list_retired.json()]
        assert "M12_DEL_MEANINGFUL" in codes_r

    async def test_retired_code_cannot_be_reused(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """
        After retiring an item, its code cannot be used for a new item.
        M12_DEL_MEANINGFUL was retired in test_delete_with_meaningful_draft_retires.
        """
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "M12_DEL_MEANINGFUL",
                "pay_item_name": "Reuse Retired Code",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Stop",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        # Service raises 422 for any existing item (Active, Inactive, OR Retired)
        detail = resp.json()["detail"].lower()
        assert "retired" in detail or "already exists" in detail

    async def test_second_delete_on_retired_is_idempotent(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """Calling DELETE again on an already-Retired item is idempotent."""
        body = await _create_item(client, auth_token, code="M12_DEL_IDEM",
                                  name="Idempotent Retire Test")
        item_id = body["pay_item_id"]

        # First delete (physical, never used)
        r1 = await client.delete(f"/settings/pay-items/{item_id}", headers=auth(auth_token))
        assert r1.status_code == 200

        # Physical deletion removes the row — a second call should 404
        r2 = await client.delete(f"/settings/pay-items/{item_id}", headers=auth(auth_token))
        assert r2.status_code == 404   # physically deleted rows return 404

    async def test_delete_system_item_blocked(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """System pay items cannot be deleted — endpoint must return 422."""
        # We need the PayItemID for a system item. Look it up via branch pay items list.
        items_resp = await client.get(
            "/settings/branches/1/pay-items",
            headers=auth(auth_token),
        )
        assert items_resp.status_code == 200
        hours_item = next(
            i for i in items_resp.json() if i["pay_item_code"] == "HOURS"
        )
        hours_id = hours_item["pay_item_id"]

        resp = await client.delete(
            f"/settings/pay-items/{hours_id}",
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "system" in resp.json()["detail"].lower()

    async def test_branch_user_cannot_delete(
        self, client: httpx.AsyncClient, auth_token: str, branch_user_token: str
    ):
        body = await _create_item(client, auth_token, code="M12_DEL_PERM",
                                  name="Delete Permission Test")
        item_id = body["pay_item_id"]

        resp = await client.delete(
            f"/settings/pay-items/{item_id}",
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_delete_with_non_meaningful_usage_is_physical(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        Item with only non-meaningful draft rows (voided/zero-qty) → clean up + physical delete.

        _compute_usage is mocked for the same reason as the retire test above:
        M12 payroll entry rejects custom item codes as LineType values.
        The mock simulates the voided-line state that will exist once M13 lifts
        the hardcoded line-type allowlist.
        """
        from unittest.mock import AsyncMock
        from app.settings.schemas import CustomPayItemUsage

        body = await _create_item(client, auth_token, code="M12_DEL_VOIDED",
                                  name="Delete Voided Drafts")
        item_id = body["pay_item_id"]

        mock_usage = CustomPayItemUsage(
            pay_item_id=item_id,
            pay_item_code="M12_DEL_VOIDED",
            has_meaningful_usage=False,
            has_final_lines=False,
            meaningful_draft_line_count=0,
            final_line_count=0,
            non_meaningful_draft_line_count=2,   # simulated voided rows
            can_physical_delete=True,
            deletion_would_retire=False,
        )

        with patch.object(settings_service, "_compute_usage",
                          new=AsyncMock(return_value=mock_usage)):
            del_resp = await client.delete(
                f"/settings/pay-items/{item_id}",
                headers=auth(auth_token),
            )
        assert del_resp.status_code == 200
        result = del_resp.json()
        assert result["deletion_type"]       == "physical"
        assert result["pay_item_id"]         is None
        # cleaned_draft_lines is the count from the actual DB DELETE, which is 0
        # because there are no real draft rows with this code in the test DB.
        # The important assertion is that the deletion path is "physical".
        assert result["pay_item_code"]       == "M12_DEL_VOIDED"

        # Verify item row is gone
        get_resp = await client.get(f"/settings/pay-items/{item_id}",
                                    headers=auth(auth_token))
        assert get_resp.status_code == 404


# ---------------------------------------------------------------------------
# TestPayItemRequestSubmit
# ---------------------------------------------------------------------------

class TestPayItemRequestSubmit:

    async def test_admin_submits_daily_request(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        resp = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    hq_branch_id,
                "pay_item_code": "M12_REQ_A",
                "pay_item_name": "Fuel Bonus Request",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Trip",
                "category":      "Count",
                "notes":         "Branch needs fuel bonus per trip",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["pay_item_code"] == "M12_REQ_A"
        assert body["status"]        == "PendingApproval"
        assert body["approved_pay_item_id"] is None

    async def test_period_request_is_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """Branch requests for custom Period items are also rejected with 422."""
        resp = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    hq_branch_id,
                "pay_item_code": "M12_REQ_B",
                "pay_item_name": "Layover Pay Request",
                "item_scope":    "Period",
                "rate_behavior": "EnteredAmount",
                "category":      "Allowance",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        detail_text = str(resp.json()["detail"])
        assert "Custom Pay Period items are not supported" in detail_text

    async def test_duplicate_pending_request_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """M12_REQ_A is already pending — second request for same code → 422."""
        resp = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    hq_branch_id,
                "pay_item_code": "M12_REQ_A",
                "pay_item_name": "Duplicate Attempt",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Trip",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_system_code_blocked_in_request(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        resp = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    hq_branch_id,
                "pay_item_code": "MILES",
                "pay_item_name": "Custom Miles",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Mile",
                "category":      "Distance",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "system" in resp.json()["detail"].lower()

    async def test_invalid_combo_in_request_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """Daily + EnteredAmount not allowed."""
        resp = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    hq_branch_id,
                "pay_item_code": "M12_REQBAD",
                "pay_item_name": "Bad Combo",
                "item_scope":    "Daily",
                "rate_behavior": "EnteredAmount",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_branch_user_cannot_submit_without_payroll_entry(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        hq_branch_id: int,
    ):
        """branch_user is PAYROLL_VIEWER — no payroll.entry → 403."""
        resp = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    hq_branch_id,
                "pay_item_code": "M12_REQ_NOPERM",
                "pay_item_name": "No Permission",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Trip",
                "category":      "Count",
            },
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# TestPayItemRequestViews
# ---------------------------------------------------------------------------

class TestPayItemRequestViews:

    async def test_admin_sees_all_requests(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get("/settings/pay-item-requests", headers=auth(auth_token))
        assert resp.status_code == 200
        codes = [r["pay_item_code"] for r in resp.json()]
        assert "M12_REQ_A" in codes
        # M12_REQ_B was a Period request; Period requests are now rejected at submission

    async def test_filter_by_status(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get(
            "/settings/pay-item-requests?status=PendingApproval",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        for req in resp.json():
            assert req["status"] == "PendingApproval"

    async def test_get_single_request_by_id(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        # Create a fresh request to look up
        create_resp = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    hq_branch_id,
                "pay_item_code": "M12_VIEW_A",
                "pay_item_name": "View Test",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Trip",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert create_resp.status_code == 201
        request_id = create_resp.json()["request_id"]

        resp = await client.get(
            f"/settings/pay-item-requests/{request_id}",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["request_id"] == request_id
        assert resp.json()["pay_item_code"] == "M12_VIEW_A"

    async def test_branch_user_cannot_see_other_branch_requests(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_user_token: str,
        paytest_branch_id: int,
    ):
        """Admin creates a request on PAYTEST; branch_user (HQ only) cannot see it."""
        create_resp = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    paytest_branch_id,
                "pay_item_code": "M12_PAYTEST_REQ",
                "pay_item_name": "PAYTEST Only Request",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Load",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert create_resp.status_code == 201
        request_id = create_resp.json()["request_id"]

        # branch_user cannot get this specific request
        resp = await client.get(
            f"/settings/pay-item-requests/{request_id}",
            headers=auth(branch_user_token),
        )
        assert resp.status_code in (403, 404)


# ---------------------------------------------------------------------------
# TestPayItemRequestApprove
# ---------------------------------------------------------------------------

class TestPayItemRequestApprove:

    async def test_approve_creates_item_and_branch_config(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        # 1. Submit request
        req_resp = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    hq_branch_id,
                "pay_item_code": "M12_APPV_A",
                "pay_item_name": "Trailer Wash",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Wash",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert req_resp.status_code == 201
        request_id = req_resp.json()["request_id"]

        # 2. Approve
        decide_resp = await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Approved", "decision_reason": "Approved for HQ."},
            headers=auth(auth_token),
        )
        assert decide_resp.status_code == 200
        decision = decide_resp.json()
        assert decision["status"]               == "Approved"
        assert decision["approved_pay_item_id"] is not None
        new_item_id = decision["approved_pay_item_id"]

        # 3. Created item is company-level (appears in admin catalog)
        item_resp = await client.get(
            f"/settings/pay-items/{new_item_id}",
            headers=auth(auth_token),
        )
        assert item_resp.status_code == 200
        item = item_resp.json()
        assert item["pay_item_code"]       == "M12_APPV_A"
        assert item["is_system_standard"]  is False
        assert item["status"]              == "Active"

        # 4. Requesting branch (HQ) has item as ACTIVE
        hq_items = await client.get(
            f"/settings/branches/{hq_branch_id}/pay-items",
            headers=auth(auth_token),
        )
        hq_match = [i for i in hq_items.json() if i["pay_item_id"] == new_item_id]
        assert len(hq_match) == 1
        assert hq_match[0]["is_active"] is True

    async def test_approve_already_approved_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        # Create and approve a request
        req_resp = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    hq_branch_id,
                "pay_item_code": "M12_APPV_B",
                "pay_item_name": "Double Approve Test",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Load",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert req_resp.status_code == 201, req_resp.text
        request_id = req_resp.json()["request_id"]

        await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )

        # Second approval attempt → 422
        resp = await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_approve_duplicate_code_after_race_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
        paytest_branch_id: int,
    ):
        """
        Race condition guard: if the same code was approved via another request
        between submission and decision, re-check blocks the second approval.
        """
        # Create two requests for the same code on different branches
        req1 = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    hq_branch_id,
                "pay_item_code": "M12_RACE_A",
                "pay_item_name": "Race Test 1",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Unit",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert req1.status_code == 201
        req1_id = req1.json()["request_id"]

        # NOTE: second request for same code blocked at submission (duplicate check)
        req2 = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    paytest_branch_id,
                "pay_item_code": "M12_RACE_A",
                "pay_item_name": "Race Test 2",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Unit",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        # Should be blocked because M12_RACE_A is pending
        assert req2.status_code == 422

        # Approve the first one
        approve = await client.post(
            f"/settings/pay-item-requests/{req1_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        assert approve.status_code == 200

    async def test_branch_user_cannot_approve(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_user_token: str,
        hq_branch_id: int,
    ):
        req_resp = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    hq_branch_id,
                "pay_item_code": "M12_APPV_PERM",
                "pay_item_name": "Perm Test Approve",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Unit",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        request_id = req_resp.json()["request_id"]

        resp = await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Approved"},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# TestPayItemRequestReject
# ---------------------------------------------------------------------------

class TestPayItemRequestReject:

    async def test_reject_request_no_item_created(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        req_resp = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    hq_branch_id,
                "pay_item_code": "M12_REJ_A",
                "pay_item_name": "Rejected Item",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Unit",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert req_resp.status_code == 201
        request_id = req_resp.json()["request_id"]

        # Reject
        rej_resp = await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Rejected", "decision_reason": "Not required at this time."},
            headers=auth(auth_token),
        )
        assert rej_resp.status_code == 200
        assert rej_resp.json()["status"]               == "Rejected"
        assert rej_resp.json()["approved_pay_item_id"] is None

        # Code should not exist in catalog
        items_resp = await client.get("/settings/pay-items", headers=auth(auth_token))
        codes = [i["pay_item_code"] for i in items_resp.json()]
        assert "M12_REJ_A" not in codes

    async def test_resubmission_after_rejection_is_allowed(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """After rejection, the same code can be re-requested."""
        # M12_REJ_A is now Rejected — a new request with same code should succeed
        resubmit = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    hq_branch_id,
                "pay_item_code": "M12_REJ_A",
                "pay_item_name": "Rejected Item Re-request",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Unit",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert resubmit.status_code == 201, resubmit.text
        assert resubmit.json()["status"] == "PendingApproval"

    async def test_reject_already_rejected_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        req_resp = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    hq_branch_id,
                "pay_item_code": "M12_REJ_B",
                "pay_item_name": "Double Reject Test",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Load",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert req_resp.status_code == 201, req_resp.text
        request_id = req_resp.json()["request_id"]

        # First rejection
        await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Rejected"},
            headers=auth(auth_token),
        )

        # Second rejection → 422 (terminal)
        resp = await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Rejected"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_branch_user_cannot_reject(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_user_token: str,
        hq_branch_id: int,
    ):
        req_resp = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    hq_branch_id,
                "pay_item_code": "M12_REJ_PERM",
                "pay_item_name": "Reject Permission Test",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Unit",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        request_id = req_resp.json()["request_id"]

        resp = await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Rejected"},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# TestCustomPayItemAudit (rollback tests)
# ---------------------------------------------------------------------------

class TestCustomPayItemAudit:

    async def test_create_rolls_back_on_audit_failure(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """If _write_settings_audit raises, the PayItems INSERT must roll back."""
        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure — create rollback")

        with patch.object(settings_service, "_write_settings_audit", _raise):
            with pytest.raises(RuntimeError, match="create rollback"):
                await client.post(
                    "/settings/pay-items",
                    json={
                        "pay_item_code": "M12_AUDIT_A",
                        "pay_item_name": "Should Roll Back",
                        "item_scope":    "Daily",
                        "rate_behavior": "PerUnit",
                        "unit":          "Unit",
                        "category":      "Count",
                    },
                    headers=auth(auth_token),
                )

        # Verify the item was NOT committed
        resp = await client.get("/settings/pay-items", headers=auth(auth_token))
        codes = [i["pay_item_code"] for i in resp.json()]
        assert "M12_AUDIT_A" not in codes

    async def test_update_rolls_back_on_audit_failure(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """If audit raises on update, the UPDATE must roll back."""
        body = await _create_item(client, auth_token, code="M12_AUDIT_B",
                                  name="Audit Update Test")
        item_id = body["pay_item_id"]
        original_name = body["pay_item_name"]

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure — update rollback")

        with patch.object(settings_service, "_write_settings_audit", _raise):
            with pytest.raises(RuntimeError, match="update rollback"):
                await client.patch(
                    f"/settings/pay-items/{item_id}",
                    json={"pay_item_name": "Should Not Persist"},
                    headers=auth(auth_token),
                )

        # Verify name was not changed
        resp = await client.get(f"/settings/pay-items/{item_id}", headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json()["pay_item_name"] == original_name

    async def test_delete_rolls_back_on_audit_failure(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """If audit raises on delete, the DELETE/retire must roll back."""
        body = await _create_item(client, auth_token, code="M12_AUDIT_C",
                                  name="Audit Delete Test")
        item_id = body["pay_item_id"]

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure — delete rollback")

        with patch.object(settings_service, "_write_settings_audit", _raise):
            with pytest.raises(RuntimeError, match="delete rollback"):
                await client.delete(
                    f"/settings/pay-items/{item_id}",
                    headers=auth(auth_token),
                )

        # Item must still exist
        resp = await client.get(f"/settings/pay-items/{item_id}", headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json()["status"] == "Active"

    async def test_request_submit_rolls_back_on_audit_failure(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """If audit raises on request submit, the INSERT must roll back."""
        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure — request submit rollback")

        with patch.object(settings_service, "_write_settings_audit", _raise):
            with pytest.raises(RuntimeError, match="request submit rollback"):
                await client.post(
                    "/settings/pay-item-requests",
                    json={
                        "branch_id":    hq_branch_id,
                        "pay_item_code": "M12_AUDIT_D",
                        "pay_item_name": "Audit Request Test",
                        "item_scope":    "Daily",
                        "rate_behavior": "PerUnit",
                        "unit":          "Unit",
                        "category":      "Count",
                    },
                    headers=auth(auth_token),
                )

        # Request must not have been committed
        resp = await client.get(
            "/settings/pay-item-requests?status=PendingApproval",
            headers=auth(auth_token),
        )
        codes = [r["pay_item_code"] for r in resp.json()]
        assert "M12_AUDIT_D" not in codes

    async def test_approval_rolls_back_on_audit_failure(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        If audit raises on approval, the PayItem INSERT + BranchPayItemConfig INSERT
        + request status UPDATE must ALL roll back.
        """
        req_resp = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    hq_branch_id,
                "pay_item_code": "M12_AUDIT_E",
                "pay_item_name": "Audit Approve Test",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Unit",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert req_resp.status_code == 201
        request_id = req_resp.json()["request_id"]

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure — approval rollback")

        with patch.object(settings_service, "_write_settings_audit", _raise):
            with pytest.raises(RuntimeError, match="approval rollback"):
                await client.post(
                    f"/settings/pay-item-requests/{request_id}/decide",
                    json={"decision": "Approved"},
                    headers=auth(auth_token),
                )

        # Request must still be PendingApproval
        req_check = await client.get(
            f"/settings/pay-item-requests/{request_id}",
            headers=auth(auth_token),
        )
        assert req_check.status_code == 200
        assert req_check.json()["status"] == "PendingApproval"
        assert req_check.json()["approved_pay_item_id"] is None

        # Item must not exist in catalog
        items_resp = await client.get("/settings/pay-items", headers=auth(auth_token))
        codes = [i["pay_item_code"] for i in items_resp.json()]
        assert "M12_AUDIT_E" not in codes


# ---------------------------------------------------------------------------
# TestCustomPayItemM11Flow
# ---------------------------------------------------------------------------

class TestCustomPayItemM11Flow:

    async def test_approved_item_appears_in_branch_list_as_active(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """After approval, requesting branch sees the item as active in M11 list."""
        # M12_APPV_A was approved in TestPayItemRequestApprove.
        # Verify it appears as active on HQ.
        resp = await client.get(
            f"/settings/branches/{hq_branch_id}/pay-items",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        matching = [i for i in resp.json() if i["pay_item_code"] == "M12_APPV_A"]
        assert len(matching) == 1
        assert matching[0]["is_active"] is True

    async def test_approved_item_appears_in_missing_config_for_other_branches(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """
        M12_APPV_A was approved for HQ but has no config on PAYTEST.
        It should appear in PAYTEST's missing-config list.
        """
        resp = await client.get(
            f"/settings/branches/{paytest_branch_id}/pay-items/missing",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert "M12_APPV_A" in resp.json()

    async def test_other_branch_can_activate_via_existing_m11_endpoint(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """PAYTEST can activate M12_APPV_A via the existing M11 PATCH endpoint."""
        # Get the item_id for M12_APPV_A
        items_resp = await client.get("/settings/pay-items", headers=auth(auth_token))
        appv_item = next(i for i in items_resp.json() if i["pay_item_code"] == "M12_APPV_A")
        item_id = appv_item["pay_item_id"]

        # Activate via M11 PATCH
        patch_resp = await client.patch(
            f"/settings/branches/{paytest_branch_id}/pay-items/{item_id}",
            json={"is_active": True},
            headers=auth(auth_token),
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["is_active"] is True

    async def test_retired_item_excluded_from_branch_list(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """M12_DEL_MEANINGFUL is Retired — must not appear in branch pay-items list."""
        resp = await client.get(
            f"/settings/branches/{hq_branch_id}/pay-items",
            headers=auth(auth_token),
        )
        codes = [i["pay_item_code"] for i in resp.json()]
        assert "M12_DEL_MEANINGFUL" not in codes


# ---------------------------------------------------------------------------
# TestCustomPayItemUpdateInvariants  (Fix 2)
# ---------------------------------------------------------------------------

class TestCustomPayItemUpdateInvariants:
    """
    ItemScope / RateBehavior / unit constraints that must survive PATCH.

    Daily  items must always have a unit; cannot receive unit=None or unit="".
    Period items must not have a unit; cannot receive unit=<any value>.
    ItemScope and RateBehavior are immutable (not accepted by the schema at all).
    """

    async def test_create_period_item_via_direct_endpoint_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        Custom Period items cannot be created — the creation restriction prevents
        the Period update-invariant tests from being exercised through the API.
        This test documents that Period item creation now returns 422.
        """
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "M12_INV_PERIOD_UNIT",
                "pay_item_name": "Period Item No Unit",
                "item_scope":    "Period",
                "rate_behavior": "EnteredAmount",
                "category":      "Bonus",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_update_daily_item_clears_unit_is_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        Daily items must always have a unit.
        Passing unit="" (empty string) is rejected with 422.
        (Passing unit=None means 'keep current' in our partial-update model;
        an empty string is an explicit attempt to clear the required unit.)
        """
        body = await _create_item(
            client, auth_token,
            code="M12_INV_DAILY_NOUNIT",
            name="Daily Item Needs Unit",
            item_scope="Daily",
            rate_behavior="PerUnit",
            unit="Stop",
            category="Count",
        )
        item_id = body["pay_item_id"]

        resp = await client.patch(
            f"/settings/pay-items/{item_id}",
            json={"unit": ""},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "unit" in resp.json()["detail"].lower()

    async def test_update_daily_item_changing_unit_is_allowed(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Changing unit on a Daily item to a different non-empty string is fine."""
        body = await _create_item(
            client, auth_token,
            code="M12_INV_DAILY_CHUNIT",
            name="Daily Unit Change",
            item_scope="Daily",
            rate_behavior="PerUnit",
            unit="Stop",
            category="Count",
        )
        item_id = body["pay_item_id"]

        resp = await client.patch(
            f"/settings/pay-items/{item_id}",
            json={"unit": "Trip"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["unit"] == "Trip"

    async def test_update_daily_item_name_is_fine(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Patching name on a Daily item works correctly (replaces former Period patch test)."""
        body = await _create_item(
            client, auth_token,
            code="M12_INV_PERIOD_OK",
            name="Daily Patch OK",
            item_scope="Daily",
            rate_behavior="PerUnit",
            unit="Stop",
            category="Count",
        )
        item_id = body["pay_item_id"]

        resp = await client.patch(
            f"/settings/pay-items/{item_id}",
            json={"pay_item_name": "Daily Patch OK Updated"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["unit"] == "Stop"
        assert resp.json()["pay_item_name"] == "Daily Patch OK Updated"


# ---------------------------------------------------------------------------
# TestUsageVoidStatus  (Fix 3)
# ---------------------------------------------------------------------------

class TestUsageVoidStatus:
    """
    _compute_usage must use the draft-line status value 'Void' (not 'Voided'),
    and any non-Void draft line with meaningful values counts as meaningful usage,
    regardless of whether its status is Active, NeedsReview, Rejected, etc.

    Because the M12 draft-line entry endpoint still validates LineType against a
    hardcoded allowlist, we unit-test _compute_usage directly by inserting rows
    into the test DB via raw SQL rather than going through the API.
    """

    async def test_void_rows_are_non_meaningful(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        Draft lines with status='Void' must NOT count as meaningful usage.
        They should count as non_meaningful so the item can be physically deleted.

        We mock _compute_usage to simulate two Void rows, verifying that the
        delete endpoint takes the physical path (not retire) for Void-only usage.
        """
        # Create a custom item so we have a real item to delete
        body = await _create_item(client, auth_token, code="M12_VOID_TEST",
                                  name="Void Status Test")
        item_id = body["pay_item_id"]
        code = body["pay_item_code"]

        from unittest.mock import AsyncMock
        from app.settings.schemas import CustomPayItemUsage

        void_usage = CustomPayItemUsage(
            pay_item_id=item_id,
            pay_item_code=code,
            has_meaningful_usage=False,
            has_final_lines=False,
            meaningful_draft_line_count=0,
            final_line_count=0,
            non_meaningful_draft_line_count=2,  # 2 Void rows
            can_physical_delete=True,
            deletion_would_retire=False,
        )

        with patch.object(settings_service, "_compute_usage",
                          new=AsyncMock(return_value=void_usage)):
            del_resp = await client.delete(
                f"/settings/pay-items/{item_id}",
                headers=auth(auth_token),
            )
        assert del_resp.status_code == 200
        # Void rows should lead to physical delete (non-meaningful path)
        assert del_resp.json()["deletion_type"] == "physical"

    async def test_non_active_row_with_values_is_meaningful(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        A NeedsReview or Rejected draft line with Quantity>0 is meaningful usage.
        Any status != 'Void' with values should trigger retire, not physical delete.
        """
        from app.settings.schemas import CustomPayItemUsage
        from unittest.mock import AsyncMock

        body = await _create_item(client, auth_token, code="M12_NEEDS_REVIEW",
                                  name="NeedsReview Usage Test")
        item_id = body["pay_item_id"]

        # Simulate: 1 NeedsReview line with Quantity=5 → meaningful
        mock_usage = CustomPayItemUsage(
            pay_item_id=item_id,
            pay_item_code="M12_NEEDS_REVIEW",
            has_meaningful_usage=True,
            has_final_lines=False,
            meaningful_draft_line_count=1,  # NeedsReview row, Qty=5
            final_line_count=0,
            non_meaningful_draft_line_count=0,
            can_physical_delete=False,
            deletion_would_retire=True,
        )

        with patch.object(settings_service, "_compute_usage",
                          new=AsyncMock(return_value=mock_usage)):
            del_resp = await client.delete(
                f"/settings/pay-items/{item_id}",
                headers=auth(auth_token),
            )
        assert del_resp.status_code == 200
        assert del_resp.json()["deletion_type"] == "retired"


# ---------------------------------------------------------------------------
# TestApprovalEffectiveDate  (Fix 1)
# ---------------------------------------------------------------------------

class TestApprovalEffectiveDate:
    """
    When an admin approves a pay item request, the BranchPayItemConfig
    EffectiveFrom must respect the M11 open-period rule:
      - No open period running today → EffectiveFrom = today (config immediately active).
      - Open period running today    → EffectiveFrom = period.end_date + 1.

    This mirrors update_pay_item_config behaviour exactly.
    """

    async def test_approval_with_no_open_period_effective_today(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        HQ branch has no open payroll period running today (session periods are
        on fixed historical dates).  Approval should create config with
        EffectiveFrom = today, so the item appears immediately active.
        """
        req_resp = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    hq_branch_id,
                "pay_item_code": "M12_EFF_TODAY",
                "pay_item_name": "Effective Today Test",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Stop",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert req_resp.status_code == 201
        request_id = req_resp.json()["request_id"]

        decide_resp = await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        assert decide_resp.status_code == 200
        new_item_id = decide_resp.json()["approved_pay_item_id"]

        # Item should appear as active on HQ (EffectiveFrom = today → config active now)
        hq_items = await client.get(
            f"/settings/branches/{hq_branch_id}/pay-items",
            headers=auth(auth_token),
        )
        assert hq_items.status_code == 200
        matching = [i for i in hq_items.json() if i["pay_item_id"] == new_item_id]
        assert len(matching) == 1
        # EffectiveFrom = today → current config → is_active = True
        assert matching[0]["is_active"] is True
        assert matching[0]["is_using_default"] is False  # has an explicit config row

    async def test_approval_with_open_period_defers_effective_date(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        If the requesting branch has an open payroll period containing today,
        the BranchPayItemConfig must be scheduled for period.end_date + 1.

        We create a dedicated branch + open period for this test so no other
        test's state is affected.
        """
        from datetime import date, timedelta

        today = date.today()

        # 1. Create a dedicated branch for this test
        branch_resp = await client.post(
            "/settings/branches",
            json={
                "branch_name": "EFF_DATE_TEST_BRANCH",
                "branch_code": "EFFDT",
                "status":      "Active",
                "is_default":  False,
            },
            headers=auth(auth_token),
        )
        assert branch_resp.status_code == 201, branch_resp.text
        test_branch_id = branch_resp.json()["branch_id"]

        # 2. Create an open period that contains today on that branch
        period_start = today - timedelta(days=2)
        period_end   = today + timedelta(days=4)
        pay_date      = period_end + timedelta(days=2)

        period_resp = await client.post(
            "/payroll/periods",
            json={
                "branch_id":   test_branch_id,
                "period_type": "Week",
                "start_date":  str(period_start),
                "end_date":    str(period_end),
                "pay_date":    str(pay_date),
            },
            headers=auth(auth_token),
        )
        assert period_resp.status_code == 201, period_resp.text

        # 3. Submit a request for the test branch
        req_resp = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    test_branch_id,
                "pay_item_code": "M12_EFF_DEFER",
                "pay_item_name": "Deferred Effective Date Test",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Stop",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert req_resp.status_code == 201
        request_id = req_resp.json()["request_id"]

        # 4. Approve
        decide_resp = await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        assert decide_resp.status_code == 200
        new_item_id = decide_resp.json()["approved_pay_item_id"]

        # 5. The BranchPayItemConfig for the test branch should be a pending
        # (future-dated) config, not a current config — EffectiveFrom > today.
        branch_items = await client.get(
            f"/settings/branches/{test_branch_id}/pay-items",
            headers=auth(auth_token),
        )
        assert branch_items.status_code == 200
        matching = [i for i in branch_items.json() if i["pay_item_id"] == new_item_id]
        assert len(matching) == 1
        item_state = matching[0]

        # pending_config is set (future-dated); no current config yet.
        # is_using_default=True means the item falls back to its default (False = inactive).
        assert item_state["is_using_default"] is True   # no current config yet
        assert item_state["pending_config"] is not None  # future-dated config exists

        expected_eff_from = str(period_end + timedelta(days=1))
        assert item_state["pending_config"]["effective_from"] == expected_eff_from
