"""Dashboard pending-rate counts follow readable Driver tenure scope."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import psycopg2
from sqlalchemy import text

from tests.compensation_api_fixtures import (
    _role,
    _user,
    build_tenant,
    create_pending,
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import p3b_database, unique
from tests.payroll_runtime_fixtures import status_column_of
from tests.builders.workforce_db import end_driver_assignment, insert_driver_assignment

pytestmark = pytest.mark.asyncio


@pytest.fixture(name="tenant")
def dashboard_tenant(p3b_dsn):
    return build_tenant(p3b_dsn)


async def _pending_rates(client, tenant, user_id: int) -> int:
    response = await client.get("/dashboard", headers=tenant.headers(user_id))
    assert response.status_code == 200, response.text
    assert "rates_health" in response.json()["sections_available"]
    return response.json()["pending_rates"]


async def test_pending_schedule_dashboard_scope_tracks_current_pending_and_ended_tenures(
    p3c_client, p3c_engine, tenant,
):
    _, rate_definition_id, _ = status_column_of(tenant)
    definition = {"rate_definition_id": rate_definition_id}
    pending_a = await create_pending(
        p3c_client, tenant, definition, driver_id=tenant.driver_a_id,
        effective_from="2030-01-01")
    pending_b = await create_pending(
        p3c_client, tenant, definition, driver_id=tenant.driver_b_id,
        effective_from="2030-01-01")

    conn = psycopg2.connect(client_encoding="utf-8", **tenant.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            role_id = _role(cur, tenant.company_id, unique("DASH_RATE_B_"), ["payrates.view"])
            branch_b_user = _user(cur, tenant.company_id, role_id, "SpecificBranch", tenant.branch_b)
    finally:
        conn.close()

    # Company-wide readers see every pending Driver-owned schedule. A scoped reader sees
    # the schedule because Driver A currently has a tenure in Branch A only.
    assert await _pending_rates(p3c_client, tenant, tenant.owner) == 2
    assert await _pending_rates(p3c_client, tenant, tenant.branch_user) == 1
    assert await _pending_rates(p3c_client, tenant, branch_b_user) == 1

    async with p3c_engine.begin() as db:
        employee_id = (await db.execute(text("""
            SELECT employeeid FROM core.drivers WHERE driverid=:driver_id
        """), {"driver_id": tenant.driver_a_id})).scalar_one()
        # Adjacent half-open windows model a scheduled transfer without overlap.
        await end_driver_assignment(
            db, tenant.driver_a, ended_at="2030-01-01T00:00:00Z")
        await insert_driver_assignment(
            db, company_id=tenant.company_id, branch_id=tenant.branch_b,
            employee_id=employee_id, driver_id=tenant.driver_a_id,
            effective_from_at="2030-01-01T00:00:00Z")

    # The scheduled destination also sees Driver A's pending rate. EXISTS semantics count
    # the schedule once even though the Driver has two tenure rows over time.
    assert await _pending_rates(p3c_client, tenant, tenant.owner) == 2
    assert await _pending_rates(p3c_client, tenant, tenant.branch_user) == 1
    assert await _pending_rates(p3c_client, tenant, branch_b_user) == 2

    async with p3c_engine.begin() as db:
        await end_driver_assignment(
            db, tenant.driver_a, ended_at=datetime.now(timezone.utc) - timedelta(days=1))
    # Once Branch A's tenure is ended, it no longer exposes Driver A's pending schedule.
    assert await _pending_rates(p3c_client, tenant, tenant.branch_user) == 0
    assert await _pending_rates(p3c_client, tenant, branch_b_user) == 2

    assert pending_a["driver_rate_schedule_id"] != pending_b["driver_rate_schedule_id"]
