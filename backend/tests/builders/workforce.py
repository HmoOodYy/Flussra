"""Shared Workforce state builders for HTTP integration tests."""

import httpx


async def create_driver_employee(
    client: httpx.AsyncClient,
    token: str,
    *,
    branch_id: int,
    full_name: str,
    driver_code: str,
    hire_date: str | None = None,
    preferred_name: str | None = None,
    cdl_number: str | None = None,
    email: str | None = None,
) -> int:
    """Create an employee and its first Driver profile through Workforce."""
    payload = {
        "branch_id": branch_id,
        "full_name": full_name,
        "driver_profile": {
            "driver_code": driver_code,
            "cdl_number": cdl_number,
        },
    }
    if hire_date is not None:
        payload["hire_date"] = hire_date
    if preferred_name is not None:
        payload["preferred_name"] = preferred_name
    if email is not None:
        payload["email"] = email

    response = await client.post(
        "/workforce/employees",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    return response.json()["current_or_pending_driver"]["driver_id"]
