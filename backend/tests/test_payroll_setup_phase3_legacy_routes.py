"""Contract tests for disabled Phase 3 legacy payroll routes."""

import httpx
import pytest
from fastapi import FastAPI

from app.dependencies import get_current_user, get_db
from app.payroll import router as payroll_router
from app.settings import router as settings_router


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body", "expected_code"),
    [
        ("GET", "/settings/branches/17/payroll-setup", None,
         "LEGACY_PAYROLL_SETUP_ROUTE_DISABLED"),
        ("PUT", "/settings/branches/17/payroll-setup",
         {"payroll_frequency": "Week", "anchor_start_date": "2026-01-05"},
         "LEGACY_PAYROLL_SETUP_ROUTE_DISABLED"),
        ("GET", "/payroll/periods/next-period-dates?branch_id=17", None,
         "LEGACY_NEXT_PERIOD_DATES_ROUTE_DISABLED"),
        ("POST", "/payroll/periods",
         {"branch_id": 17, "start_date": "2026-01-05", "end_date": "2026-01-11"},
         "LEGACY_DIRECT_PERIOD_CREATION_ROUTE_DISABLED"),
    ],
)
async def test_legacy_payroll_routes_return_410_without_service_calls(
    monkeypatch, method, path, body, expected_code
):
    async def fail_if_called(**kwargs):
        raise AssertionError("disabled route invoked a legacy service")

    monkeypatch.setattr(payroll_router.period_creation, "get_next_period_dates", fail_if_called)
    monkeypatch.setattr(payroll_router.period_creation, "create_period", fail_if_called)
    monkeypatch.setattr(settings_router.service, "get_payroll_setup", fail_if_called)
    monkeypatch.setattr(settings_router.service, "upsert_payroll_setup", fail_if_called)

    app = FastAPI()
    app.include_router(payroll_router.router, prefix="/payroll")
    app.include_router(settings_router.router, prefix="/settings")

    async def current_user():
        return {"sub": "1", "cid": "1"}

    async def db_connection():
        return None

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = db_connection

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.request(method, path, json=body)

    assert response.status_code == 410
    assert response.json()["detail"]["code"] == expected_code
    assert response.json()["detail"]["message"]
