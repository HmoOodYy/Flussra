"""Module-owned Branch/Driver scope for workflow tests that need the PAYTEST shape.

Tests that create, submit, approve or finalize periods used to share the seeded
PAYTEST branch and rely on a later module sweeping what they left behind. A
module that shadows `paytest_branch_id` / `paytest_driver_id` with these builders
owns its workflow slot instead: nothing it leaves can reach another module, and
nothing another module leaves can reach it.
"""

from uuid import uuid4

import httpx
from sqlalchemy import text

# Mirrors conftest.activate_paytest_system_items: the shared fixture activates
# these on whichever `paytest_branch_id` the FIRST requesting module resolves, so
# an owned branch can never rely on it and activates them itself.
PAYTEST_EQUIVALENT_ITEM_CODES = frozenset(
    {"OVERNIGHT", "WAIT_TIME", "PALLETS", "SILOS", "HOURS", "MILES"}
)


async def create_owned_branch(db, code_prefix: str, name: str) -> int:
    marker = uuid4().hex
    return int((await db.execute(text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (1, :code, :name, 'Active', FALSE)
        RETURNING branchid
    """), {"code": f"{code_prefix}_{marker[:12]}", "name": f"{name} {marker[:8]}"})).scalar_one())


async def activate_paytest_equivalent_items(
    client: httpx.AsyncClient, token: str, branch_id: int,
    codes: frozenset[str] = PAYTEST_EQUIVALENT_ITEM_CODES,
) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    items = await client.get(f"/settings/branches/{branch_id}/pay-items", headers=headers)
    assert items.status_code == 200, items.text
    for item in items.json():
        if item["pay_item_code"] in codes:
            activated = await client.patch(
                f"/settings/branches/{branch_id}/pay-items/{item['pay_item_id']}",
                json={"is_active": True}, headers=headers,
            )
            assert activated.status_code == 200, (
                f"activating {item['pay_item_code']} on branch {branch_id} failed: {activated.text}"
            )


async def create_owned_driver(
    client: httpx.AsyncClient, token: str, branch_id: int, label: str,
    *, full_name: str | None = None,
) -> int:
    marker = uuid4().hex
    resp = await client.post(
        "/core/drivers",
        json={
            "branch_id": branch_id,
            "full_name": full_name or f"{label} owned driver {marker}",
            "driver_code": f"{label[:3].upper()}-{marker[:10]}",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, f"{label} owned driver create failed: {resp.text}"
    return resp.json()["driver_id"]
