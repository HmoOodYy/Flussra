from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.builders.access import create_user_with_role_token


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def driver_user(
    client: httpx.AsyncClient,
    admin_token: str,
    branch_id: int,
    db: AsyncConnection,
) -> tuple[str, int]:
    roles = await client.get("/admin/company-roles", headers=auth(admin_token))
    assert roles.status_code == 200, roles.text
    role_id = next(role["company_role_id"] for role in roles.json() if role["role_code"] == "DRIVER")
    username = f"p2b_self_{uuid4().hex[:12]}"
    token = await create_user_with_role_token(
        client, admin_token, username, role_id,
        scope_type="Self", driver_branch_id=branch_id,
    )
    account = await db.execute(
        text("SELECT EmployeeID FROM sec.Users WHERE Username = :username"),
        {"username": username},
    )
    employee_id = account.scalar_one()
    driver = await db.execute(
        text("SELECT DriverID FROM core.Drivers WHERE EmployeeID = :employee_id ORDER BY EffectiveFrom DESC LIMIT 1"),
        {"employee_id": employee_id},
    )
    return token, int(driver.scalar_one())


@pytest.mark.asyncio
async def test_driver_self_transfer_is_limited_to_current_owned_driver(
    client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    hq_branch_id: int,
    db_conn: AsyncConnection,
):
    token, own_driver_id = await driver_user(client, auth_token, paytest_branch_id, db_conn)
    other_token, other_driver_id = await driver_user(client, auth_token, paytest_branch_id, db_conn)
    assert token != other_token

    me = await client.get("/auth/me", headers=auth(token))
    assert me.status_code == 200, me.text
    assert me.json()["branches"] == []
    assert len(me.json()["self_assignments"]) == 1
    assert me.json()["self_assignments"][0]["role_code"] == "DRIVER"
    assert me.json()["self_assignments"][0]["scope"] == "Self"

    own_request = await client.post(
        "/driver-transfers",
        json={
            "driver_id": own_driver_id,
            "target_branch_id": hq_branch_id,
            "effective_date": "2099-01-01",
            "initiated_by": "Driver",
        },
        headers=auth(token),
    )
    assert own_request.status_code == 201, own_request.text

    other_request = await client.post(
        "/driver-transfers",
        json={
            "driver_id": other_driver_id,
            "target_branch_id": hq_branch_id,
            "effective_date": "2099-01-01",
            "initiated_by": "Driver",
        },
        headers=auth(token),
    )
    assert other_request.status_code == 403

    wrong_initiator = await client.post(
        "/driver-transfers",
        json={
            "driver_id": own_driver_id,
            "target_branch_id": hq_branch_id,
            "effective_date": "2099-01-01",
            "initiated_by": "SourceBranch",
        },
        headers=auth(token),
    )
    assert wrong_initiator.status_code == 422

    for path in ("/driver-transfers", "/workforce/employees", "/payroll/current-workflow"):
        response = await client.get(path, headers=auth(token))
        assert response.status_code == 403, f"{path}: {response.status_code} {response.text}"
