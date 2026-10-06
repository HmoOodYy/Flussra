"""
Company custom PayItem catalog reads and retirement lifecycle.

Custom Daily PayItem definition and governance authority is CDPI; the generic
Settings catalog only reads company items and retires them. Items are seeded
directly with the canonical current structure (PayItems row + CdpiDefinitions
owner marker) from tests.seed_helpers.

Users / fixtures from conftest
-------------------------------
admin        AllCompanyBranches, PAYROLL_ADMIN (all permissions)
branch_user  SpecificBranch=HQ, PAYROLL_VIEWER (no write permissions)

Test classes
------------
TestCustomPayItemReads         catalog list / get / system items
TestCustomPayItemUsage         history counts shown before retirement
TestCustomPayItemRetire        DELETE retires; idempotent; system block; permissions; audit
TestCustomPayItemBranchConfig  retired / active items in branch configuration
"""
from datetime import date
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import text

from app.settings import service as settings_service
from tests.seed_helpers import seed_cdpi_item


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_item_with_branch_config(db_conn, *, branch_id: int, code: str, name: str) -> int:
    """CDPI-owned Daily item with an active BranchPayItemConfig for one branch."""
    item_id = await seed_cdpi_item(db_conn, code=code, name=name)
    await db_conn.execute(
        text("""
            INSERT INTO payroll.branchpayitemconfig (
                companyid, branchid, payitemid, isactive, effectivefrom, createdbyuserid
            ) VALUES (1, :bid, :piid, TRUE, :eff, 1)
            ON CONFLICT DO NOTHING
        """),
        {"bid": branch_id, "piid": item_id, "eff": date.today()},
    )
    return item_id


# ---------------------------------------------------------------------------
# Catalog reads
# ---------------------------------------------------------------------------

class TestCustomPayItemReads:

    async def test_system_items_readable_through_branch_endpoint(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        resp = await client.get(
            f"/settings/branches/{hq_branch_id}/pay-items", headers=auth(auth_token)
        )
        assert resp.status_code == 200
        assert any(i.get("is_system_standard") for i in resp.json())

    async def test_get_nonexistent_returns_404(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        resp = await client.get("/settings/pay-items/99999999", headers=auth(auth_token))
        assert resp.status_code == 404

    async def test_list_returns_company_items_only(
        self, client: httpx.AsyncClient, auth_token: str, db_conn
    ):
        await seed_cdpi_item(db_conn, code="G05_LIST_CHK", name="List Check Item")

        resp = await client.get("/settings/pay-items", headers=auth(auth_token))
        assert resp.status_code == 200
        codes = [i["pay_item_code"] for i in resp.json()]
        assert "G05_LIST_CHK" in codes
        assert "HOURS" not in codes

    async def test_get_single_company_item(
        self, client: httpx.AsyncClient, auth_token: str, db_conn
    ):
        item_id = await seed_cdpi_item(db_conn, code="G05_GET_ONE", name="Get One")
        resp = await client.get(f"/settings/pay-items/{item_id}", headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json()["pay_item_code"] == "G05_GET_ONE"

    async def test_branch_user_cannot_list(
        self, client: httpx.AsyncClient, branch_user_token: str
    ):
        resp = await client.get("/settings/pay-items", headers=auth(branch_user_token))
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

class TestCustomPayItemUsage:

    async def test_never_used_item_has_no_history(
        self, client: httpx.AsyncClient, auth_token: str, db_conn
    ):
        item_id = await seed_cdpi_item(db_conn, code="G05_USAGE_A", name="Usage Never Used")

        resp = await client.get(
            f"/settings/pay-items/{item_id}/usage", headers=auth(auth_token)
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "pay_item_id": item_id,
            "pay_item_code": "G05_USAGE_A",
            "meaningful_draft_line_count": 0,
            "final_line_count": 0,
            "driver_rates_count": 0,
        }


# ---------------------------------------------------------------------------
# Retirement lifecycle
# ---------------------------------------------------------------------------

class TestCustomPayItemRetire:

    async def test_delete_retires_item_and_preserves_definition(
        self, client: httpx.AsyncClient, auth_token: str, db_conn
    ):
        item_id = await seed_cdpi_item(db_conn, code="G05_RETIRE_A", name="Retire A")

        resp = await client.delete(f"/settings/pay-items/{item_id}", headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json() == {
            "pay_item_id": item_id,
            "pay_item_code": "G05_RETIRE_A",
            "status": "Retired",
        }

        row = (await db_conn.execute(
            text("SELECT status FROM payroll.payitems WHERE payitemid = :pid"),
            {"pid": item_id},
        )).mappings().first()
        assert row["status"] == "Retired"
        definitions = (await db_conn.execute(
            text("SELECT COUNT(*) FROM payroll.cdpidefinitions WHERE payitemid = :pid"),
            {"pid": item_id},
        )).scalar_one()
        assert definitions == 1

    async def test_retired_item_hidden_by_default_and_listed_on_request(
        self, client: httpx.AsyncClient, auth_token: str, db_conn
    ):
        item_id = await seed_cdpi_item(db_conn, code="G05_RETIRE_LIST", name="Retire List")
        assert (await client.delete(
            f"/settings/pay-items/{item_id}", headers=auth(auth_token)
        )).status_code == 200

        default = await client.get("/settings/pay-items", headers=auth(auth_token))
        assert "G05_RETIRE_LIST" not in [i["pay_item_code"] for i in default.json()]
        full = await client.get("/settings/pay-items?include_retired=true", headers=auth(auth_token))
        assert "G05_RETIRE_LIST" in [i["pay_item_code"] for i in full.json()]

    async def test_second_delete_on_retired_item_is_idempotent(
        self, client: httpx.AsyncClient, auth_token: str, db_conn
    ):
        item_id = await seed_cdpi_item(db_conn, code="G05_RETIRE_IDEM", name="Retire Idempotent")

        first = await client.delete(f"/settings/pay-items/{item_id}", headers=auth(auth_token))
        second = await client.delete(f"/settings/pay-items/{item_id}", headers=auth(auth_token))
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()

    async def test_system_item_cannot_be_retired(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        items = await client.get("/settings/branches/1/pay-items", headers=auth(auth_token))
        hours_id = next(i for i in items.json() if i["pay_item_code"] == "HOURS")["pay_item_id"]

        resp = await client.delete(f"/settings/pay-items/{hours_id}", headers=auth(auth_token))
        assert resp.status_code == 422
        assert "system" in resp.json()["detail"].lower()

    async def test_unknown_item_returns_404(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        resp = await client.delete("/settings/pay-items/99999999", headers=auth(auth_token))
        assert resp.status_code == 404

    async def test_branch_user_cannot_retire(
        self, client: httpx.AsyncClient, branch_user_token: str, db_conn
    ):
        item_id = await seed_cdpi_item(db_conn, code="G05_RETIRE_PERM", name="Retire Permission")
        resp = await client.delete(
            f"/settings/pay-items/{item_id}", headers=auth(branch_user_token)
        )
        assert resp.status_code == 403

    async def test_retire_rolls_back_on_audit_failure(
        self, client: httpx.AsyncClient, auth_token: str, db_conn
    ):
        item_id = await seed_cdpi_item(db_conn, code="G05_RETIRE_AUDIT", name="Retire Audit")

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure — retire rollback")

        with patch.object(settings_service, "_write_settings_audit", _raise):
            with pytest.raises(RuntimeError, match="retire rollback"):
                await client.delete(
                    f"/settings/pay-items/{item_id}", headers=auth(auth_token)
                )

        resp = await client.get(f"/settings/pay-items/{item_id}", headers=auth(auth_token))
        assert resp.status_code == 200
        assert resp.json()["status"] == "Active"


# ---------------------------------------------------------------------------
# Branch configuration interplay
# ---------------------------------------------------------------------------

class TestCustomPayItemBranchConfig:

    async def test_configured_item_is_active_in_branch_list(
        self, client: httpx.AsyncClient, auth_token: str, db_conn, hq_branch_id: int
    ):
        await _seed_item_with_branch_config(
            db_conn, branch_id=hq_branch_id, code="G05_CFG_A", name="Configured Item"
        )
        resp = await client.get(
            f"/settings/branches/{hq_branch_id}/pay-items", headers=auth(auth_token)
        )
        assert resp.status_code == 200
        matching = [i for i in resp.json() if i["pay_item_code"] == "G05_CFG_A"]
        assert len(matching) == 1
        assert matching[0]["is_active"] is True

    async def test_item_missing_config_on_other_branch_and_activatable(
        self, client: httpx.AsyncClient, auth_token: str, db_conn,
        hq_branch_id: int, paytest_branch_id: int,
    ):
        item_id = await _seed_item_with_branch_config(
            db_conn, branch_id=hq_branch_id, code="G05_CFG_B", name="Configured B"
        )
        missing = await client.get(
            f"/settings/branches/{paytest_branch_id}/pay-items/missing",
            headers=auth(auth_token),
        )
        assert missing.status_code == 200
        assert "G05_CFG_B" in missing.json()

        activated = await client.patch(
            f"/settings/branches/{paytest_branch_id}/pay-items/{item_id}",
            json={"is_active": True},
            headers=auth(auth_token),
        )
        assert activated.status_code == 200
        assert activated.json()["is_active"] is True

    async def test_retired_item_excluded_from_branch_list(
        self, client: httpx.AsyncClient, auth_token: str, db_conn, hq_branch_id: int
    ):
        item_id = await _seed_item_with_branch_config(
            db_conn, branch_id=hq_branch_id, code="G05_CFG_RET", name="Configured Retired"
        )
        assert (await client.delete(
            f"/settings/pay-items/{item_id}", headers=auth(auth_token)
        )).status_code == 200

        resp = await client.get(
            f"/settings/branches/{hq_branch_id}/pay-items", headers=auth(auth_token)
        )
        assert "G05_CFG_RET" not in [i["pay_item_code"] for i in resp.json()]
