"""Legacy PayDefinition, CDPI, ordinary PerUnit and assignment-owned rate authorities are gone.

Every rate is a Driver-owned schedule: ordinary rates target a PayDefinition and Status rates
target a Company Status rate column. These tests pin that no assignment-level rate route is
mounted any more and that ordinary authoring stays on the compensation path.
"""
from __future__ import annotations

import pytest


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


RETIRED_ROUTES = [
    ("post", "/settings/cdpi/requests"),
    ("get", "/settings/cdpi/requests"),
    ("post", "/settings/cdpi/direct-company-items"),
    ("get", "/settings/cdpi/branches/1/items"),
    ("patch", "/settings/cdpi/branches/1/items/1"),
    ("get", "/settings/pay-items"),
    ("get", "/settings/pay-items/1"),
    ("delete", "/settings/pay-items/1"),
    ("patch", "/settings/pay-items/order"),
    ("get", "/settings/branches/1/pay-items"),
    ("patch", "/settings/branches/1/pay-items/1"),
    ("get", "/settings/branches/1/pay-items/1/history"),
    ("get", "/settings/branches/1/pay-items/missing"),
    ("patch", "/settings/branches/pay-items/1/bulk-config"),
]


@pytest.mark.parametrize("method,path", RETIRED_ROUTES)
async def test_legacy_definition_and_configuration_writers_are_not_mounted(
    session_client, auth_token, method, path,
):
    response = await session_client.request(
        method.upper(), path, headers=_auth(auth_token),
        json={} if method in ("post", "patch") else None)
    assert response.status_code == 404, (method, path, response.status_code, response.text)


RETIRED_RATE_ROUTES = [
    ("post", "/payroll/rates"),
    ("get", "/payroll/rates"),
    ("get", "/payroll/rates/1"),
    ("patch", "/payroll/rates/1"),
    ("post", "/payroll/rates/1/approve"),
    ("delete", "/payroll/rates/1"),
    ("post", "/payroll/driver-assignments/1/rates/batch"),
    ("get", "/payroll/driver-assignments/1/rates"),
    ("get", "/payroll/driver-assignments/1/rate-matrix"),
    ("post", "/payroll/driver-assignments/1/rates/copy-from/2"),
]


@pytest.mark.parametrize("method,path", RETIRED_RATE_ROUTES)
async def test_assignment_owned_rate_routes_are_not_mounted(
    session_client, auth_token, method, path,
):
    response = await session_client.request(
        method.upper(), path, headers=_auth(auth_token),
        json={} if method in ("post", "patch") else None)
    assert response.status_code in (404, 405), (method, path, response.status_code, response.text)


async def test_no_mounted_route_names_assignment_rates():
    from app.main import create_app

    mounted = set()

    def walk(router, prefix=""):
        for route in router.routes:
            if hasattr(route, "methods") and hasattr(route, "path"):
                mounted.update(f"{m} {prefix}{route.path}" for m in route.methods)
            elif hasattr(route, "original_router") and hasattr(route, "include_context"):
                walk(route.original_router, prefix + route.include_context.prefix)

    walk(create_app().router)
    assert not [r for r in mounted if "copy-from" in r or "/payroll/rates" in r
                or "/rate-matrix" in r or ("driver-assignments" in r and "/rates" in r)]


async def test_ordinary_authoring_still_works_on_the_target_path(
    client, auth_token, hq_branch_id, created_driver_id,
):
    headers = _auth(auth_token)
    definition = await client.post("/compensation/pay-definitions", headers=headers, json={
        "definition_name": "Cutover item", "input_type": "Decimal", "calculation_method": "PerUnit"})
    assert definition.status_code == 201, definition.text
    body = definition.json()
    configured = await client.patch(
        f"/compensation/branches/{hq_branch_id}/pay-definitions/{body['pay_definition_id']}",
        headers=headers, json={"is_active": True})
    assert configured.status_code == 200, configured.text
    rows = await client.get(
        f"/compensation/drivers/{created_driver_id}/pay-rates", headers=headers)
    ordinary = [r for r in rows.json() if r["kind"] == "PayDefinition"]
    assert body["pay_definition_id"] in {r["pay_definition_id"] for r in ordinary}
