"""Builders for the current operational legacy DriverRate API in tests."""

import httpx


def _require_status(response: httpx.Response, expected_status: int, operation: str) -> None:
    if response.status_code != expected_status:
        raise RuntimeError(
            f"{operation} expected HTTP {expected_status}; "
            f"got {response.status_code}: {response.text}"
        )


async def create_approved_rate(
    client: httpx.AsyncClient,
    token: str,
    *,
    driver_id: int,
    rate_type_id: int,
    amount: str,
    effective_from: str,
) -> int:
    """Create and approve one operational legacy DriverRate through the API."""
    headers = {"Authorization": f"Bearer {token}"}
    created = await client.post(
        "/payroll/rates",
        json={
            "driver_id": driver_id,
            "rate_type_id": rate_type_id,
            "amount": amount,
            "effective_from": effective_from,
        },
        headers=headers,
    )
    _require_status(created, 201, "Create rate")
    rate_id = created.json()["driver_rate_id"]
    approved = await client.post(
        f"/payroll/rates/{rate_id}/approve",
        headers=headers,
    )
    _require_status(approved, 200, "Approve rate")
    return rate_id
