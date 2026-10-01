"""HTTP builders for canonical payroll period candidate and creation flows."""

import httpx


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _require_status(response: httpx.Response, expected_statuses: tuple[int, ...]) -> None:
    if response.status_code not in expected_statuses:
        expected = ", ".join(str(status) for status in expected_statuses)
        raise RuntimeError(
            f"Expected HTTP status {expected}; got {response.status_code}: {response.text}"
        )


async def get_period_candidates(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    *,
    mode: str = "OPEN_CREATION",
) -> dict:
    """Fetch payroll period candidates through the authenticated API."""
    response = await client.get(
        f"/payroll/branches/{branch_id}/period-candidates",
        params={"mode": mode},
        headers=_auth(token),
    )
    _require_status(response, (200,))
    return response.json()


async def create_period_from_candidate(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    candidate_key: str,
    *,
    expected_statuses: tuple[int, ...] = (200, 201),
) -> dict:
    """Create a payroll period from a candidate through the authenticated API."""
    response = await client.post(
        f"/payroll/branches/{branch_id}/period-creations",
        json={"candidate_key": candidate_key},
        headers=_auth(token),
    )
    _require_status(response, expected_statuses)
    return response.json()
