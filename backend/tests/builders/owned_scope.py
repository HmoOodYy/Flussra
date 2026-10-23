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


async def create_owned_branch(db, code_prefix: str, name: str) -> int:
    marker = uuid4().hex
    return int((await db.execute(text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault, timezoneid)
        VALUES (1, :code, :name, 'Active', FALSE, 'UTC')
        RETURNING branchid
    """), {"code": f"{code_prefix}_{marker[:12]}", "name": f"{name} {marker[:8]}"})).scalar_one())


async def create_owned_driver(
    client: httpx.AsyncClient, token: str, branch_id: int, label: str,
    *, full_name: str | None = None,
) -> int:
    marker = uuid4().hex
    resp = await client.post(
        "/workforce/employees",
        json={
            "branch_id": branch_id,
            "full_name": full_name or f"{label} owned driver {marker}",
            "hire_date": "2020-01-01",
            "driver": {
                "driver_number": f"{label[:3].upper()}-{marker[:10]}",
                "effective_from_at": "2020-01-01T00:00:00Z",
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, f"{label} owned driver create failed: {resp.text}"
    return resp.json()["driver"]["driver_id"]
