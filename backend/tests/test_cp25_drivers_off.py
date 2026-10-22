"""The superseded period-wide Drivers Off API is not mounted.

Selected-day and Fully-Off behavior is covered by test_cp5b_off_drivers.py.
"""

import pytest

pytestmark = pytest.mark.asyncio


async def test_period_wide_drivers_off_api_is_not_mounted(session_client):
    response = await session_client.get("/openapi.json")
    assert response.status_code == 200, response.text
    assert "/payroll/periods/{period_id}/drivers-off" not in response.json()["paths"]
