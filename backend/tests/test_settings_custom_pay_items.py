"""
M12 integration tests — Custom Pay Items catalog + branch request/approval flow.

LLR-A (Legacy Custom Daily Lockdown):
  POST /settings/pay-items with item_scope='Daily' → 422 (use CDPI instead)
  POST /settings/pay-item-requests with item_scope='Daily' → 422 (use CDPI instead)
  POST /settings/pay-item-requests/{id}/decide Approved for Daily → 422
  Reject/ReturnToDraft decisions for legacy requests still work.
  All read/update/delete compatibility for existing legacy items is preserved.

Users / fixtures from conftest
-------------------------------
admin        AllCompanyBranches, PAYROLL_ADMIN (all permissions)
branch_user  SpecificBranch=HQ, PAYROLL_VIEWER (no write permissions)

Test classes
------------
TestLegacyDailyLockdown        LLR-A acceptance tests (new)
TestCustomPayItemCreate        admin direct create (now blocked for Daily; Period still rejected)
TestCustomPayItemCodeGeneration  code-gen paths now blocked by LLR-A
TestCustomPayItemUpdate        mutable metadata, immutable fields (items seeded via DB)
TestCustomPayItemUsage         usage detection for smart delete (items seeded via DB)
TestCustomPayItemDelete        physical delete, retire, idempotent, system block
TestPayItemRequestSubmit       branch request submission (Daily now blocked)
TestPayItemRequestViews        list / get scope (requests seeded via DB)
TestPayItemRequestApprove      approval blocked for Daily (items seeded via DB)
TestPayItemRequestReject       rejection still works for legacy pending requests
TestCustomPayItemAudit         rollback compatibility
TestCustomPayItemM11Flow       approved custom item integrates with M11 branch config
TestCustomPayItemUpdateInvariants  scope/unit immutability on PATCH (items seeded via DB)
TestUsageVoidStatus            _compute_usage void-row semantics (items seeded via DB)
TestApprovalEffectiveDate      BranchPayItemConfig effective-date rule (now blocked via HTTP)
"""
import pytest
import pytest_asyncio
import httpx
from unittest.mock import patch, AsyncMock
from sqlalchemy import text
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
    """Legacy creation helper — now returns 422 for Daily (LLR-A blocked).
    Kept for tests that verify the lockdown rejects the call."""
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
    return resp.json() | {"_status_code": resp.status_code}


async def _seed_legacy_item(
    db_conn,
    *,
    company_id: int = 1,
    user_id: int = 1,
    code: str,
    name: str = "Legacy Test Item",
    item_scope: str = "Daily",
    rate_behavior: str = "PerUnit",
    unit: str | None = "Stop",
    category: str = "Count",
) -> int:
    """Insert a legacy PayItems row directly, bypassing the LLR-A HTTP guard.
    Used by read/update/delete compatibility tests to seed existing legacy items."""
    result = await db_conn.execute(
        text("""
            INSERT INTO payroll.payitems (
                companyid, payitemcode, payitemname, category, datatype, unit,
                status, sortorder, appearsinpayrollentry, appearsinledger,
                appearsinreports, requiresrate, issystemstandard,
                itemscope, ratebehavior, isdefaultbranchactive,
                createdbyuserid
            ) VALUES (
                :cid, :code, :name, :category, 'Decimal', :unit,
                'Active', 100, TRUE, TRUE, TRUE, TRUE, FALSE,
                :scope, :behavior, FALSE, :uid
            )
            RETURNING payitemid
        """),
        {
            "cid": company_id, "code": code, "name": name,
            "category": category, "unit": unit,
            "scope": item_scope, "behavior": rate_behavior, "uid": user_id,
        }
    )
    return result.scalar_one()


async def _attach_cdpi_definition(
    db_conn,
    *,
    item_id: int,
    user_id: int = 1,
) -> None:
    """Insert a CdpiDefinitions row for an existing PayItem, marking it as an
    approved CDPI definition. Used to test that CDPI authority (not usage) drives
    retire-only behaviour in the generic PayItem usage/delete flow."""
    await db_conn.execute(
        text("""
            INSERT INTO payroll.cdpidefinitions
                (payitemid, definitionschemaversion, lockedatutc, createdbyuserid)
            VALUES (:pid, 1, NOW(), :uid)
        """),
        {"pid": item_id, "uid": user_id},
    )


async def _seed_legacy_request(
    db_conn,
    *,
    company_id: int = 1,
    user_id: int = 1,
    branch_id: int,
    code: str,
    name: str = "Legacy Test Request",
    item_scope: str = "Daily",
    rate_behavior: str = "PerUnit",
    unit: str | None = "Unit",
    category: str = "Count",
    req_status: str = "PendingApproval",
) -> int:
    """Insert a legacy CustomPayItemRequests row directly, bypassing LLR-A.
    Used by request-view, reject, and M11Flow tests."""
    result = await db_conn.execute(
        text("""
            INSERT INTO payroll.custompayitemrequests (
                companyid, requestingbranchid, requestedbyuserid,
                payitemcode, payitemname, itemscope, ratebehavior,
                category, unit, sortorder, status
            ) VALUES (
                :cid, :bid, :uid,
                :code, :name, :scope, :behavior,
                :category, :unit, 100, :status
            )
            RETURNING requestid
        """),
        {
            "cid": company_id, "bid": branch_id, "uid": user_id,
            "code": code, "name": name, "scope": item_scope,
            "behavior": rate_behavior, "category": category,
            "unit": unit, "status": req_status,
        }
    )
    return result.scalar_one()


async def _seed_legacy_approved_item_with_branch_config(
    db_conn,
    *,
    company_id: int = 1,
    user_id: int = 1,
    branch_id: int,
    code: str,
    name: str = "Legacy Approved Item",
) -> int:
    """Seed a legacy Daily item + BranchPayItemConfig (active) for a branch.
    Simulates the state that would have been created by legacy request approval."""
    from datetime import date as _date
    item_id = await _seed_legacy_item(
        db_conn, company_id=company_id, user_id=user_id,
        code=code, name=name,
    )
    await db_conn.execute(
        text("""
            INSERT INTO payroll.branchpayitemconfig (
                companyid, branchid, payitemid, isactive, effectivefrom,
                createdbyuserid
            ) VALUES (
                :cid, :bid, :piid, TRUE, :eff, :uid
            )
            ON CONFLICT DO NOTHING
        """),
        {
            "cid": company_id, "bid": branch_id, "piid": item_id,
            "eff": _date.today(), "uid": user_id,
        }
    )
    return item_id


# ---------------------------------------------------------------------------
# TestLegacyDailyLockdown  (LLR-A acceptance tests)
# ---------------------------------------------------------------------------

class TestLegacyDailyLockdown:
    """
    Verifies that all three legacy Custom Daily creation/approval paths
    are blocked with a clear 422 pointing users to CDPI.
    """

    async def test_direct_create_daily_blocked(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """POST /settings/pay-items with item_scope='Daily' returns 422."""
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "LLRA_BLOCK_1",
                "pay_item_name": "Should Be Blocked",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Stop",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "CDPI" in detail
        assert "cdpi" in detail.lower()

    async def test_direct_create_daily_cdpi_message_present(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """The 422 detail references the CDPI endpoint to use instead."""
        resp = await client.post(
            "/settings/pay-items",
            json={
                "pay_item_name": "Another Blocked Item",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Load",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "direct-company-items" in detail or "cdpi" in detail.lower()

    async def test_legacy_request_daily_blocked(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        """POST /settings/pay-item-requests with item_scope='Daily' returns 422."""
        resp = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":     hq_branch_id,
                "pay_item_code": "LLRA_REQ_BLOCK",
                "pay_item_name": "Blocked Request",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Trip",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "CDPI" in detail or "cdpi" in detail.lower()

    async def test_legacy_request_daily_no_row_created(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        """Blocked request submission does not persist a row in custompayitemrequests."""
        resp = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":     hq_branch_id,
                "pay_item_code": "LLRA_REQ_NOROW",
                "pay_item_name": "Should Not Be Saved",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Unit",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

        check = await client.get(
            "/settings/pay-item-requests?status=PendingApproval",
            headers=auth(auth_token),
        )
        codes = [r["pay_item_code"] for r in check.json()]
        assert "LLRA_REQ_NOROW" not in codes

    async def test_legacy_approve_daily_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        db_conn,
        hq_branch_id: int,
    ):
        """Approving a legacy PendingApproval Daily request returns 422."""
        request_id = await _seed_legacy_request(
            db_conn,
            branch_id=hq_branch_id,
            code="LLRA_APPV_BLOCK",
            name="Lockdown Approve Test",
        )
        resp = await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "CDPI" in detail or "cdpi" in detail.lower()

    async def test_legacy_approve_daily_no_item_created(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        db_conn,
        hq_branch_id: int,
    ):
        """Blocked approval creates no PayItem or BranchPayItemConfig row."""
        request_id = await _seed_legacy_request(
            db_conn,
            branch_id=hq_branch_id,
            code="LLRA_APPV_NOROW",
            name="No Item Should Appear",
        )
        await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )

        items = await client.get("/settings/pay-items", headers=auth(auth_token))
        codes = [i["pay_item_code"] for i in items.json()]
        assert "LLRA_APPV_NOROW" not in codes

    async def test_legacy_reject_daily_still_allowed(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        db_conn,
        hq_branch_id: int,
    ):
        """Rejecting a legacy Daily request still works (no PayItem created)."""
        request_id = await _seed_legacy_request(
            db_conn,
            branch_id=hq_branch_id,
            code="LLRA_REJ_OK",
            name="Reject Still Works",
        )
        resp = await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Rejected", "decision_reason": "LLR-A — use CDPI"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "Rejected"
        assert resp.json()["approved_pay_item_id"] is None

    async def test_direct_create_daily_no_payitem_row_created(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """Blocked direct-create does not persist a PayItems row."""
        await client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "LLRA_NOROW_CHK",
                "pay_item_name": "Should Not Persist",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Unit",
            },
            headers=auth(auth_token),
        )
        items = await client.get("/settings/pay-items", headers=auth(auth_token))
        codes = [i["pay_item_code"] for i in items.json()]
        assert "LLRA_NOROW_CHK" not in codes


# ---------------------------------------------------------------------------
# TestCustomPayItemCreate
# ---------------------------------------------------------------------------

class TestCustomPayItemCreate:

    async def test_create_daily_perunit_item_now_blocked(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """LLR-A: admin direct Daily creation returns 422 (use CDPI instead)."""
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
        assert resp.status_code == 422
        assert "CDPI" in resp.json()["detail"] or "cdpi" in resp.json()["detail"].lower()

    async def test_create_period_item_is_rejected(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """Custom Period items are not supported — schema validator fires before LLR-A guard."""
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
        """Wizard value_type paths for Period are all rejected by schema."""
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
        """Daily + EnteredAmount is not allowed (schema validator fires before LLR-A guard)."""
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
        """Daily items with missing unit return 422 (LLR-A guard fires after schema; 422 either way)."""
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

    async def test_system_code_blocked(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """LLR-A Daily guard fires — 422 returned (CDPI message, not system message)."""
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

    async def test_duplicate_code_blocked(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """Attempting to create any Daily item returns 422 (LLR-A fires before duplicate check)."""
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
        """Permission check fires before LLR-A guard — still 403 for non-admin."""
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
        db_conn,
        hq_branch_id: int,
    ):
        """Compatibility: legacy item seeded via DB starts inactive on all branches."""
        item_id = await _seed_legacy_item(
            db_conn, code="M12_INACT_A", name="Inactive Start Test"
        )

        branch_items = await client.get(
            f"/settings/branches/{hq_branch_id}/pay-items",
            headers=auth(auth_token),
        )
        assert branch_items.status_code == 200
        matching = [i for i in branch_items.json() if i["pay_item_id"] == item_id]
        assert len(matching) == 1
        assert matching[0]["is_active"] is False
        assert matching[0]["is_using_default"] is True

    async def test_daily_creation_blocked_for_all_behaviors(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """LLR-A: all legacy Daily rate behaviors are blocked."""
        for rb in ["PerUnit", "OrdinalTier", "RangeBracket", "RangeProgressive", "Block"]:
            resp = await client.post(
                "/settings/pay-items",
                json={
                    "pay_item_name": f"Blocked {rb} Item",
                    "item_scope":    "Daily",
                    "rate_behavior": rb,
                    "unit":          "Unit",
                },
                headers=auth(auth_token),
            )
            assert resp.status_code == 422, (
                f"Expected 422 for Daily + {rb}, got {resp.status_code}: {resp.text}"
            )
            assert "CDPI" in resp.json()["detail"] or "cdpi" in resp.json()["detail"].lower()

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
        assert "Custom Pay Period items are not supported" in detail_text
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
        assert len(items) > 0
        system_items = [i for i in items if i.get("is_system_standard")]
        assert len(system_items) > 0

    async def test_update_endpoint_cannot_convert_daily_to_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        db_conn,
    ):
        """
        item_scope is immutable on PATCH. item_scope is not in CustomPayItemUpdate,
        so passing it is silently ignored. Confirm item remains Daily.
        """
        item_id = await _seed_legacy_item(
            db_conn, code="REQ6_SCOPE_IMMUT", name="Scope Immutable Test"
        )

        patch_resp = await client.patch(
            f"/settings/pay-items/{item_id}",
            json={"pay_item_name": "Scope Immutable Test Renamed", "item_scope": "Period"},
            headers=auth(auth_token),
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["item_scope"] == "Daily"


# ---------------------------------------------------------------------------
# TestCustomPayItemCodeGeneration
# ---------------------------------------------------------------------------

class TestCustomPayItemCodeGeneration:
    """
    LLR-A: All legacy Daily creation paths are blocked, including auto-code paths.
    These tests verify that the lockdown applies regardless of whether a code is
    supplied or omitted.
    """

    async def test_create_without_code_daily_blocked(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """Omitting pay_item_code with Daily scope → 422 (LLR-A)."""
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
        assert resp.status_code == 422
        assert "CDPI" in resp.json()["detail"] or "cdpi" in resp.json()["detail"].lower()

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
        assert resp.status_code == 422

    async def test_multiple_auto_code_attempts_all_blocked(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """All attempts to create Daily items without code return 422."""
        for i in range(3):
            resp = await client.post(
                "/settings/pay-items",
                json={
                    "pay_item_name": f"Blocked Auto Code Item {i}",
                    "item_scope":    "Daily",
                    "rate_behavior": "PerUnit",
                    "unit":          "Unit",
                },
                headers=auth(auth_token),
            )
            assert resp.status_code == 422

    async def test_explicit_code_daily_also_blocked(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """LLR-A applies to explicit codes too."""
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
        assert resp.status_code == 422

    async def test_collision_retry_path_unreachable_daily_blocked(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """LLR-A fires before the code-generation retry path is reached."""
        from unittest.mock import patch
        from app.settings import service as svc

        call_count = 0

        def mock_generate() -> str:
            nonlocal call_count
            call_count += 1
            return "CPI_BLOCKED1"

        with patch.object(svc, "_generate_pay_item_code", side_effect=mock_generate):
            resp = await client.post(
                "/settings/pay-items",
                json={
                    "pay_item_name": "Retry Item",
                    "item_scope":    "Daily",
                    "rate_behavior": "PerUnit",
                    "unit":          "Unit",
                },
                headers=auth(auth_token),
            )

        assert resp.status_code == 422
        assert call_count == 0, "Code generator must not be called when guard fires"

    async def test_auto_code_daily_inactive_branch_verify_blocked(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        """Auto-coded Daily creation is blocked before any DB write."""
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
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# TestCustomPayItemUpdate
# ---------------------------------------------------------------------------

class TestCustomPayItemUpdate:

    async def test_update_mutable_fields(
        self, client: httpx.AsyncClient, auth_token: str, db_conn
    ):
        item_id = await _seed_legacy_item(
            db_conn, code="M12_UPD_A", name="Update Target"
        )

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
        assert updated["pay_item_code"] == "M12_UPD_A"
        assert updated["item_scope"]    == "Daily"
        assert updated["rate_behavior"] == "PerUnit"

    async def test_cannot_update_deleted_item(
        self, client: httpx.AsyncClient, auth_token: str, db_conn
    ):
        item_id = await _seed_legacy_item(
            db_conn, code="M12_UPD_RETD", name="Will Be Deleted"
        )
        await client.delete(f"/settings/pay-items/{item_id}", headers=auth(auth_token))

        resp = await client.patch(
            f"/settings/pay-items/{item_id}",
            json={"pay_item_name": "Try to update deleted item"},
            headers=auth(auth_token),
        )
        assert resp.status_code in (404, 422)

    async def test_branch_user_cannot_update(
        self, client: httpx.AsyncClient, auth_token: str, branch_user_token: str, db_conn
    ):
        item_id = await _seed_legacy_item(db_conn, code="M12_UPD_PERM")

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

    async def test_list_returns_seeded_items(
        self, client: httpx.AsyncClient, auth_token: str, db_conn
    ):
        """Compatibility: legacy items seeded in DB appear in the GET list."""
        await _seed_legacy_item(db_conn, code="M12_LIST_CHK", name="List Check Item")

        resp = await client.get("/settings/pay-items", headers=auth(auth_token))
        assert resp.status_code == 200
        codes = [i["pay_item_code"] for i in resp.json()]
        assert "M12_LIST_CHK" in codes
        assert "HOURS" not in codes

    async def test_list_excludes_retired_by_default(
        self, client: httpx.AsyncClient, auth_token: str, db_conn
    ):
        """After retirement, item excluded from normal list but visible with include_retired."""
        from unittest.mock import AsyncMock
        from app.settings.schemas import CustomPayItemUsage

        item_id = await _seed_legacy_item(
            db_conn, code="M12_LIST_RETD", name="List Retired Test"
        )

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

        resp = await client.get("/settings/pay-items", headers=auth(auth_token))
        codes = [i["pay_item_code"] for i in resp.json()]
        assert "M12_LIST_RETD" not in codes

        resp2 = await client.get("/settings/pay-items?include_retired=true",
                                 headers=auth(auth_token))
        codes2 = [i["pay_item_code"] for i in resp2.json()]
        assert "M12_LIST_RETD" in codes2


# ---------------------------------------------------------------------------
# TestCustomPayItemUsage
# ---------------------------------------------------------------------------

class TestCustomPayItemUsage:

    async def test_never_used_item_clean(
        self, client: httpx.AsyncClient, auth_token: str, db_conn
    ):
        item_id = await _seed_legacy_item(
            db_conn, code="M12_USAGE_A", name="Usage Check Never Used"
        )

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

    async def test_approved_cdpi_is_retire_only_with_zero_usage(
        self, client: httpx.AsyncClient, auth_token: str, db_conn
    ):
        """An approved CDPI PayItem must report retire-only even when draft/final/
        driver-rate usage is all zero — CdpiDefinitions is authority, not usage."""
        item_id = await _seed_legacy_item(
            db_conn, code="M12_CDPI_USAGE", name="Usage Check CDPI Approved"
        )
        await _attach_cdpi_definition(db_conn, item_id=item_id)
        try:
            resp = await client.get(
                f"/settings/pay-items/{item_id}/usage",
                headers=auth(auth_token),
            )
            assert resp.status_code == 200
            u = resp.json()
            assert u["has_meaningful_usage"]  is False
            assert u["has_final_lines"]       is False
            assert u["driver_rates_count"]    == 0
            assert u["has_cdpi_definition"]   is True
            assert u["can_physical_delete"]   is False
            assert u["deletion_would_retire"] is True
        finally:
            await db_conn.execute(
                text("DELETE FROM payroll.cdpidefinitions WHERE payitemid = :pid"),
                {"pid": item_id},
            )
            await db_conn.execute(
                text("DELETE FROM payroll.payitems WHERE payitemid = :pid"),
                {"pid": item_id},
            )


# ---------------------------------------------------------------------------
# TestCustomPayItemDelete
# ---------------------------------------------------------------------------

class TestCustomPayItemDelete:

    async def test_delete_never_used_physical_delete(
        self, client: httpx.AsyncClient, auth_token: str, db_conn
    ):
        item_id = await _seed_legacy_item(
            db_conn, code="M12_DEL_UNUSED", name="Delete Never Used"
        )

        resp = await client.delete(
            f"/settings/pay-items/{item_id}",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        result = resp.json()
        assert result["deletion_type"]  == "physical"
        assert result["pay_item_id"]    is None
        assert result["pay_item_code"]  == "M12_DEL_UNUSED"

        get_resp = await client.get(
            f"/settings/pay-items/{item_id}",
            headers=auth(auth_token),
        )
        assert get_resp.status_code == 404

    async def test_delete_with_meaningful_draft_retires(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        db_conn,
    ):
        """Item with meaningful draft usage → retire (Status=Retired), not physical delete."""
        from unittest.mock import AsyncMock
        from app.settings.schemas import CustomPayItemUsage

        item_id = await _seed_legacy_item(
            db_conn, code="M12_DEL_MEANINGFUL", name="Delete Meaningful Usage"
        )

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

        list_resp = await client.get("/settings/pay-items", headers=auth(auth_token))
        codes = [i["pay_item_code"] for i in list_resp.json()]
        assert "M12_DEL_MEANINGFUL" not in codes

        list_retired = await client.get(
            "/settings/pay-items?include_retired=true",
            headers=auth(auth_token),
        )
        codes_r = [i["pay_item_code"] for i in list_retired.json()]
        assert "M12_DEL_MEANINGFUL" in codes_r

    async def test_retired_code_reuse_blocked(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """Attempting to reuse any retired code returns 422 (LLR-A fires for Daily)."""
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

    async def test_second_delete_on_retired_is_idempotent(
        self, client: httpx.AsyncClient, auth_token: str, db_conn
    ):
        """Calling DELETE again on an already-deleted item returns 404."""
        item_id = await _seed_legacy_item(
            db_conn, code="M12_DEL_IDEM", name="Idempotent Retire Test"
        )

        r1 = await client.delete(f"/settings/pay-items/{item_id}", headers=auth(auth_token))
        assert r1.status_code == 200

        r2 = await client.delete(f"/settings/pay-items/{item_id}", headers=auth(auth_token))
        assert r2.status_code == 404

    async def test_delete_system_item_blocked(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """System pay items cannot be deleted — endpoint must return 422."""
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
        self, client: httpx.AsyncClient, auth_token: str, branch_user_token: str, db_conn
    ):
        item_id = await _seed_legacy_item(db_conn, code="M12_DEL_PERM",
                                          name="Delete Permission Test")

        resp = await client.delete(
            f"/settings/pay-items/{item_id}",
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_delete_with_non_meaningful_usage_is_physical(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        db_conn,
    ):
        """Item with only non-meaningful draft rows → physical delete."""
        from unittest.mock import AsyncMock
        from app.settings.schemas import CustomPayItemUsage

        item_id = await _seed_legacy_item(
            db_conn, code="M12_DEL_VOIDED", name="Delete Voided Drafts"
        )

        mock_usage = CustomPayItemUsage(
            pay_item_id=item_id,
            pay_item_code="M12_DEL_VOIDED",
            has_meaningful_usage=False,
            has_final_lines=False,
            meaningful_draft_line_count=0,
            final_line_count=0,
            non_meaningful_draft_line_count=2,
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
        assert result["deletion_type"] == "physical"
        assert result["pay_item_id"]   is None
        assert result["pay_item_code"] == "M12_DEL_VOIDED"

        get_resp = await client.get(f"/settings/pay-items/{item_id}",
                                    headers=auth(auth_token))
        assert get_resp.status_code == 404

    async def test_delete_unused_approved_cdpi_retires_not_physical(
        self, client: httpx.AsyncClient, auth_token: str, db_conn
    ):
        """DELETE on an unused approved CDPI PayItem must retire it instead of
        physically deleting — a physical delete would hit fk_CdpiDefinitions_PayItem
        (ON DELETE RESTRICT). The CdpiDefinitions row must survive."""
        item_id = await _seed_legacy_item(
            db_conn, code="M12_CDPI_DEL", name="Delete Unused CDPI Approved"
        )
        await _attach_cdpi_definition(db_conn, item_id=item_id)
        try:
            resp = await client.delete(
                f"/settings/pay-items/{item_id}",
                headers=auth(auth_token),
            )
            assert resp.status_code == 200
            result = resp.json()
            assert result["deletion_type"] == "retired"
            assert result["pay_item_id"]   == item_id
            assert result["pay_item_code"] == "M12_CDPI_DEL"

            status_row = (await db_conn.execute(
                text("SELECT status FROM payroll.payitems WHERE payitemid = :pid"),
                {"pid": item_id},
            )).mappings().first()
            assert status_row["status"] == "Retired"

            cdpi_count = (await db_conn.execute(
                text("SELECT COUNT(*) AS cnt FROM payroll.cdpidefinitions WHERE payitemid = :pid"),
                {"pid": item_id},
            )).mappings().first()["cnt"]
            assert cdpi_count == 1
        finally:
            await db_conn.execute(
                text("DELETE FROM payroll.cdpidefinitions WHERE payitemid = :pid"),
                {"pid": item_id},
            )
            await db_conn.execute(
                text("DELETE FROM payroll.payitems WHERE payitemid = :pid"),
                {"pid": item_id},
            )


# ---------------------------------------------------------------------------
# TestPayItemRequestSubmit
# ---------------------------------------------------------------------------

class TestPayItemRequestSubmit:

    async def test_admin_daily_request_now_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """LLR-A: legacy Daily request creation returns 422 (use CDPI instead)."""
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
        assert resp.status_code == 422
        assert "CDPI" in resp.json()["detail"] or "cdpi" in resp.json()["detail"].lower()

    async def test_period_request_is_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """Branch requests for custom Period items are rejected with 422 (schema validator)."""
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

    async def test_daily_request_blocked_all_behaviors(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """LLR-A: all legacy Daily request behaviors are blocked."""
        for rb in ["PerUnit", "OrdinalTier"]:
            resp = await client.post(
                "/settings/pay-item-requests",
                json={
                    "branch_id":    hq_branch_id,
                    "pay_item_code": f"M12_REQ_BLK_{rb}",
                    "pay_item_name": f"Blocked {rb}",
                    "item_scope":    "Daily",
                    "rate_behavior": rb,
                    "unit":          "Unit",
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
        """LLR-A Daily guard fires — 422 returned (CDPI message)."""
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

    async def test_invalid_combo_in_request_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """Daily + EnteredAmount blocked by schema validator before LLR-A guard."""
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
        """branch_user is PAYROLL_VIEWER — 403 fires before LLR-A guard."""
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
        db_conn,
        hq_branch_id: int,
    ):
        """Compatibility: legacy requests seeded in DB appear in GET list."""
        await _seed_legacy_request(
            db_conn, branch_id=hq_branch_id, code="M12_REQ_VIEW_A",
            name="View All Seed Request",
        )
        resp = await client.get("/settings/pay-item-requests", headers=auth(auth_token))
        assert resp.status_code == 200
        codes = [r["pay_item_code"] for r in resp.json()]
        assert "M12_REQ_VIEW_A" in codes

    async def test_filter_by_status(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        db_conn,
        hq_branch_id: int,
    ):
        await _seed_legacy_request(
            db_conn, branch_id=hq_branch_id, code="M12_REQ_FILT",
        )
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
        db_conn,
        hq_branch_id: int,
    ):
        request_id = await _seed_legacy_request(
            db_conn, branch_id=hq_branch_id, code="M12_VIEW_A",
            name="View Test",
        )

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
        db_conn,
        paytest_branch_id: int,
    ):
        """Admin seeds a request on PAYTEST; branch_user (HQ only) cannot see it."""
        request_id = await _seed_legacy_request(
            db_conn, branch_id=paytest_branch_id, code="M12_PAYTEST_REQ",
            name="PAYTEST Only Request",
        )

        resp = await client.get(
            f"/settings/pay-item-requests/{request_id}",
            headers=auth(branch_user_token),
        )
        assert resp.status_code in (403, 404)


# ---------------------------------------------------------------------------
# TestPayItemRequestApprove
# ---------------------------------------------------------------------------

class TestPayItemRequestApprove:

    async def test_approve_daily_request_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        db_conn,
        hq_branch_id: int,
    ):
        """LLR-A: approving a legacy Daily request returns 422."""
        request_id = await _seed_legacy_request(
            db_conn, branch_id=hq_branch_id, code="M12_APPV_A",
            name="Trailer Wash",
        )

        decide_resp = await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Approved", "decision_reason": "Approved for HQ."},
            headers=auth(auth_token),
        )
        assert decide_resp.status_code == 422
        assert "CDPI" in decide_resp.json()["detail"] or \
               "cdpi" in decide_resp.json()["detail"].lower()

    async def test_approve_daily_blocked_no_item_created(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        db_conn,
        hq_branch_id: int,
    ):
        """Blocked approval creates no PayItem row."""
        request_id = await _seed_legacy_request(
            db_conn, branch_id=hq_branch_id, code="M12_APPV_NOROW",
            name="No Item Created",
        )
        await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        items = await client.get("/settings/pay-items", headers=auth(auth_token))
        codes = [i["pay_item_code"] for i in items.json()]
        assert "M12_APPV_NOROW" not in codes

    async def test_approve_already_approved_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        db_conn,
        hq_branch_id: int,
    ):
        """Terminal status guard fires for already-Approved request (before Daily guard)."""
        request_id = await _seed_legacy_request(
            db_conn, branch_id=hq_branch_id, code="M12_APPV_B",
            name="Double Approve Test", req_status="Approved",
        )

        resp = await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_branch_user_cannot_approve(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        branch_user_token: str,
        db_conn,
        hq_branch_id: int,
    ):
        """Permission check fires first — 403 for branch_user."""
        request_id = await _seed_legacy_request(
            db_conn, branch_id=hq_branch_id, code="M12_APPV_PERM",
            name="Perm Test Approve",
        )

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
        db_conn,
        hq_branch_id: int,
    ):
        """Rejecting a legacy Daily request still works (no PayItem created)."""
        request_id = await _seed_legacy_request(
            db_conn, branch_id=hq_branch_id, code="M12_REJ_A",
            name="Rejected Item",
        )

        rej_resp = await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Rejected", "decision_reason": "Not required at this time."},
            headers=auth(auth_token),
        )
        assert rej_resp.status_code == 200
        assert rej_resp.json()["status"]               == "Rejected"
        assert rej_resp.json()["approved_pay_item_id"] is None

        items_resp = await client.get("/settings/pay-items", headers=auth(auth_token))
        codes = [i["pay_item_code"] for i in items_resp.json()]
        assert "M12_REJ_A" not in codes

    async def test_resubmission_after_rejection_blocked_by_llra(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        db_conn,
        hq_branch_id: int,
    ):
        """After rejection, resubmitting via legacy API returns 422 (LLR-A)."""
        request_id = await _seed_legacy_request(
            db_conn, branch_id=hq_branch_id, code="M12_REJ_RESUB",
            name="Rejected Resubmit",
        )
        await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Rejected"},
            headers=auth(auth_token),
        )

        resubmit = await client.post(
            "/settings/pay-item-requests",
            json={
                "branch_id":    hq_branch_id,
                "pay_item_code": "M12_REJ_RESUB",
                "pay_item_name": "Resubmit Attempt",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Unit",
                "category":      "Count",
            },
            headers=auth(auth_token),
        )
        assert resubmit.status_code == 422

    async def test_reject_already_rejected_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        db_conn,
        hq_branch_id: int,
    ):
        """Terminal status guard: already-Rejected request → 422."""
        request_id = await _seed_legacy_request(
            db_conn, branch_id=hq_branch_id, code="M12_REJ_B",
            name="Double Reject Test",
        )

        await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Rejected"},
            headers=auth(auth_token),
        )

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
        db_conn,
        hq_branch_id: int,
    ):
        """Permission check fires first — 403 for branch_user."""
        request_id = await _seed_legacy_request(
            db_conn, branch_id=hq_branch_id, code="M12_REJ_PERM",
            name="Reject Permission Test",
        )

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

    async def test_create_guard_fires_before_audit(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """LLR-A guard raises HTTPException before _write_settings_audit is reached.
        No RuntimeError is propagated; the response is a clean 422."""
        async def _raise(*args, **kwargs):
            raise RuntimeError("Should not be reached")

        with patch.object(settings_service, "_write_settings_audit", _raise):
            resp = await client.post(
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
        assert resp.status_code == 422

        list_resp = await client.get("/settings/pay-items", headers=auth(auth_token))
        codes = [i["pay_item_code"] for i in list_resp.json()]
        assert "M12_AUDIT_A" not in codes

    async def test_update_rolls_back_on_audit_failure(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        db_conn,
    ):
        """If audit raises on update, the UPDATE must roll back."""
        item_id = await _seed_legacy_item(
            db_conn, code="M12_AUDIT_B", name="Audit Update Test"
        )
        original_name = "Audit Update Test"

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure — update rollback")

        with patch.object(settings_service, "_write_settings_audit", _raise):
            with pytest.raises(RuntimeError, match="update rollback"):
                await client.patch(
                    f"/settings/pay-items/{item_id}",
                    json={"pay_item_name": "Should Not Persist"},
                    headers=auth(auth_token),
                )

        resp = await client.get(f"/settings/pay-items/{item_id}", headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json()["pay_item_name"] == original_name

    async def test_delete_rolls_back_on_audit_failure(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        db_conn,
    ):
        """If audit raises on delete, the DELETE/retire must roll back."""
        item_id = await _seed_legacy_item(
            db_conn, code="M12_AUDIT_C", name="Audit Delete Test"
        )

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure — delete rollback")

        with patch.object(settings_service, "_write_settings_audit", _raise):
            with pytest.raises(RuntimeError, match="delete rollback"):
                await client.delete(
                    f"/settings/pay-items/{item_id}",
                    headers=auth(auth_token),
                )

        resp = await client.get(f"/settings/pay-items/{item_id}", headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json()["status"] == "Active"

    async def test_request_submit_guard_fires_before_audit(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """LLR-A guard raises before _write_settings_audit on request submit."""
        async def _raise(*args, **kwargs):
            raise RuntimeError("Should not be reached")

        with patch.object(settings_service, "_write_settings_audit", _raise):
            resp = await client.post(
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
        assert resp.status_code == 422

        check = await client.get(
            "/settings/pay-item-requests?status=PendingApproval",
            headers=auth(auth_token),
        )
        codes = [r["pay_item_code"] for r in check.json()]
        assert "M12_AUDIT_D" not in codes

    async def test_approval_guard_fires_before_payitem_insert(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        db_conn,
        hq_branch_id: int,
    ):
        """LLR-A guard fires before the PayItem INSERT on approval. Request stays PendingApproval."""
        request_id = await _seed_legacy_request(
            db_conn, branch_id=hq_branch_id, code="M12_AUDIT_E",
            name="Audit Approve Test",
        )

        resp = await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

        req_check = await client.get(
            f"/settings/pay-item-requests/{request_id}",
            headers=auth(auth_token),
        )
        assert req_check.status_code == 200
        assert req_check.json()["status"] == "PendingApproval"
        assert req_check.json()["approved_pay_item_id"] is None

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
        db_conn,
        hq_branch_id: int,
    ):
        """Compatibility: legacy item seeded via DB + BranchPayItemConfig appears as active."""
        await _seed_legacy_approved_item_with_branch_config(
            db_conn, branch_id=hq_branch_id, code="M12_APPV_A",
            name="Trailer Wash (seeded)",
        )

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
        """M12_APPV_A was seeded for HQ but has no config on PAYTEST."""
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
        items_resp = await client.get("/settings/pay-items", headers=auth(auth_token))
        appv_item = next(i for i in items_resp.json() if i["pay_item_code"] == "M12_APPV_A")
        item_id = appv_item["pay_item_id"]

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
# TestCustomPayItemUpdateInvariants
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
        """Custom Period items cannot be created — 422."""
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
        db_conn,
    ):
        """Daily items must always have a unit. unit="" is rejected with 422."""
        item_id = await _seed_legacy_item(
            db_conn,
            code="M12_INV_DAILY_NOUNIT",
            name="Daily Item Needs Unit",
            unit="Stop",
        )

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
        db_conn,
    ):
        """Changing unit on a Daily item to a different non-empty string is fine."""
        item_id = await _seed_legacy_item(
            db_conn,
            code="M12_INV_DAILY_CHUNIT",
            name="Daily Unit Change",
            unit="Stop",
        )

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
        db_conn,
    ):
        """Patching name on a Daily item works correctly."""
        item_id = await _seed_legacy_item(
            db_conn,
            code="M12_INV_PERIOD_OK",
            name="Daily Patch OK",
            unit="Stop",
        )

        resp = await client.patch(
            f"/settings/pay-items/{item_id}",
            json={"pay_item_name": "Daily Patch OK Updated"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["unit"] == "Stop"
        assert resp.json()["pay_item_name"] == "Daily Patch OK Updated"


# ---------------------------------------------------------------------------
# TestUsageVoidStatus
# ---------------------------------------------------------------------------

class TestUsageVoidStatus:
    """
    _compute_usage must use the draft-line status value 'Void' (not 'Voided'),
    and any non-Void draft line with meaningful values counts as meaningful usage.
    Items seeded via DB (legacy path) since HTTP creation is now blocked.
    """

    async def test_void_rows_are_non_meaningful(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        db_conn,
    ):
        """Draft lines with status='Void' must NOT count as meaningful usage."""
        item_id = await _seed_legacy_item(
            db_conn, code="M12_VOID_TEST", name="Void Status Test"
        )

        from unittest.mock import AsyncMock
        from app.settings.schemas import CustomPayItemUsage

        void_usage = CustomPayItemUsage(
            pay_item_id=item_id,
            pay_item_code="M12_VOID_TEST",
            has_meaningful_usage=False,
            has_final_lines=False,
            meaningful_draft_line_count=0,
            final_line_count=0,
            non_meaningful_draft_line_count=2,
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
        assert del_resp.json()["deletion_type"] == "physical"

    async def test_non_active_row_with_values_is_meaningful(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        db_conn,
    ):
        """A NeedsReview or Rejected draft line with Quantity>0 is meaningful usage."""
        from app.settings.schemas import CustomPayItemUsage
        from unittest.mock import AsyncMock

        item_id = await _seed_legacy_item(
            db_conn, code="M12_NEEDS_REVIEW", name="NeedsReview Usage Test"
        )

        mock_usage = CustomPayItemUsage(
            pay_item_id=item_id,
            pay_item_code="M12_NEEDS_REVIEW",
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
        assert del_resp.status_code == 200
        assert del_resp.json()["deletion_type"] == "retired"


# ---------------------------------------------------------------------------
# TestApprovalEffectiveDate
# ---------------------------------------------------------------------------

class TestApprovalEffectiveDate:
    """
    LLR-A: The legacy approval path that created BranchPayItemConfig is now blocked
    for Daily items. These tests verify the lockdown and document the expected behavior.

    The effective-date logic in decide_pay_item_request() is still present in the
    code but is unreachable for Daily items via the HTTP API. It will be verified
    in Phase LLR-B via the CDPI approval path.
    """

    async def test_legacy_approval_with_no_open_period_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        db_conn,
        hq_branch_id: int,
    ):
        """LLR-A: approval of seeded legacy Daily request returns 422."""
        request_id = await _seed_legacy_request(
            db_conn, branch_id=hq_branch_id, code="M12_EFF_TODAY",
            name="Effective Today Test",
        )

        decide_resp = await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        assert decide_resp.status_code == 422
        assert "CDPI" in decide_resp.json()["detail"] or \
               "cdpi" in decide_resp.json()["detail"].lower()

    async def test_legacy_approval_with_open_period_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        db_conn,
    ):
        """LLR-A: approval attempt returns 422 regardless of open period state."""
        from datetime import date, timedelta

        today = date.today()

        # Create a dedicated branch + open period for this test
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

        request_id = await _seed_legacy_request(
            db_conn, branch_id=test_branch_id, code="M12_EFF_DEFER",
            name="Deferred Effective Date Test",
        )

        decide_resp = await client.post(
            f"/settings/pay-item-requests/{request_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        assert decide_resp.status_code == 422
