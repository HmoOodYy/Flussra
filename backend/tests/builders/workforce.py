"""Target Workforce setup helpers for tests that need a permanent Driver."""

from uuid import uuid4

import httpx


async def create_driver_employee_record(
    client: httpx.AsyncClient,
    token: str,
    *,
    branch_id: int,
    full_name: str,
    driver_number: str,
    hire_date: str | None = None,
    preferred_name: str | None = None,
    cdl_number: str | None = None,
    email: str | None = None,
    effective_from_at: str | None = None,
) -> dict:
    """Create an Employee, permanent Driver and first assignment via Workforce."""
    body = {"branch_id": branch_id, "full_name": full_name, "driver": {
        "driver_number": driver_number, "cdl_number": cdl_number,
    }}
    if hire_date is not None:
        body["hire_date"] = hire_date
    if preferred_name is not None:
        body["preferred_name"] = preferred_name
    if email is not None:
        body["email"] = email
    if effective_from_at is not None:
        body["driver"]["effective_from_at"] = effective_from_at
    response = await client.post(
        "/workforce/employees", json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    if response.status_code != 201:
        raise AssertionError(f"Workforce Driver setup failed: {response.text}")
    return response.json()


async def create_workforce_driver(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    full_name: str,
    driver_number: str,
    *,
    preferred_name: str | None = None,
    cdl_number: str | None = None,
    email: str | None = None,
    hire_date: str = "2020-01-01",
    effective_from_at: str = "2020-01-01T00:00:00Z",
) -> dict:
    """Create an Employee and its first DriverAssignment through Workforce."""
    marker = uuid4().hex[:12]
    response = await client.post(
        "/workforce/employees",
        json={
            "branch_id": branch_id,
            "full_name": full_name,
            "preferred_name": preferred_name,
            "employee_key": f"TEST-{marker}",
            "email": email,
            "hire_date": hire_date,
            "driver": {
                "driver_number": driver_number,
                "cdl_number": cdl_number,
                "effective_from_at": effective_from_at,
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201, f"Workforce Driver setup failed: {response.text}"
    body = response.json()
    driver = body["driver"]
    assignment = body["current_or_pending_assignment"]
    return {
        "employee_id": body["employee_id"],
        "driver_id": driver["driver_id"],
        "driver_assignment_id": assignment["driver_assignment_id"],
        "full_name": body["full_name"],
        "driver_number": driver["driver_number"],
    }
