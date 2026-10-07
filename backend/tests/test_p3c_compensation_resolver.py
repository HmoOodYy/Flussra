"""Canonical target assignment resolver."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.compensation import resolver
from app.compensation.errors import CompensationIntegrityError, CompensationOwnershipError
from tests.p3b_fixtures import p3b_cursor, p3b_database  # noqa: F401 - register fixtures
from tests.p3c_fixtures import (  # noqa: F401 - register fixtures
    authored_assignment,
    build_tenant,
    create_definition,
    create_pending,
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
    p3c_tenant,
    set_scalar_value,
)

pytestmark = pytest.mark.asyncio

BASE = "/compensation/driver-rate-assignments"


async def _resolve(engine, tenant, definition, work_date, driver_id=None, company_id=None):
    async with engine.connect() as db:
        return await resolver.resolve(
            company_id or tenant.company_id, driver_id or tenant.driver_a,
            definition["rate_definition_id"], date.fromisoformat(work_date), db)


async def _two_schedules(client, tenant):
    definition = await create_definition(client, tenant, definition_code="ITEM_ALPHA")
    first = await authored_assignment(client, tenant, definition, "25",
                                      effective_from="2026-01-01")
    second = await authored_assignment(client, tenant, definition, "30",
                                       effective_from="2026-06-01")
    return definition, first, second


async def test_boundary_dates_select_exactly_one_whole_schedule(p3c_client, p3c_engine, tenant):
    definition, first, second = await _two_schedules(p3c_client, tenant)
    first_id = first["driver_rate_assignment_id"]
    second_id = second["driver_rate_assignment_id"]
    expected = {
        "2025-12-31": None,
        "2026-01-01": (first_id, "Superseded", "25.0000"),
        "2026-03-15": (first_id, "Superseded", "25.0000"),
        "2026-05-31": (first_id, "Superseded", "25.0000"),
        "2026-06-01": (second_id, "Approved", "30.0000"),
        "2030-01-01": (second_id, "Approved", "30.0000"),
    }
    for work_date, outcome in expected.items():
        resolved = await _resolve(p3c_engine, tenant, definition, work_date)
        if outcome is None:
            assert resolved is None, work_date
            continue
        assignment_id, status, amount = outcome
        assert resolved.driver_rate_assignment_id == assignment_id, work_date
        assert resolved.assignment_status == status
        assert resolved.scalar_amount == Decimal(amount)
        assert len(resolved.components) == 1


async def test_exact_effective_to_is_inclusive(p3c_client, p3c_engine, tenant):
    definition = await create_definition(p3c_client, tenant)
    await authored_assignment(p3c_client, tenant, definition, "9",
                              effective_from="2026-01-01", effective_to="2026-01-31")
    assert (await _resolve(p3c_engine, tenant, definition, "2026-01-31")) is not None
    assert (await _resolve(p3c_engine, tenant, definition, "2026-02-01")) is None


async def test_resolution_exposes_target_identity_without_legacy_routing(
    p3c_client, p3c_engine, tenant,
):
    definition, first, _ = await _two_schedules(p3c_client, tenant)
    resolved = await _resolve(p3c_engine, tenant, definition, "2026-02-01")
    assert resolved.company_id == tenant.company_id
    assert resolved.driver_id == tenant.driver_a
    assert resolved.branch_id == tenant.branch_a
    assert resolved.pay_definition_id == definition["pay_definition_id"]
    assert resolved.rate_definition_id == definition["rate_definition_id"]
    assert resolved.rate_shape == "Scalar"
    assert resolved.calculation_method == "PerUnit"
    assert resolved.effective_from == date(2026, 1, 1)
    assert resolved.effective_to == date(2026, 5, 31)
    component = resolved.components[0]
    assert component.rate_component_definition_id == \
        definition["components"][0]["rate_component_definition_id"]
    assert not any("ratetype" in name for name in resolved.model_dump())


async def test_zero_is_returned_as_zero(p3c_client, p3c_engine, tenant):
    definition = await create_definition(p3c_client, tenant)
    await authored_assignment(p3c_client, tenant, definition, "0")
    resolved = await _resolve(p3c_engine, tenant, definition, "2026-02-01")
    assert resolved.scalar_amount == Decimal("0")
    assert resolved.scalar_amount is not None


async def test_decimal_precision_is_preserved(p3c_client, p3c_engine, tenant):
    definition = await create_definition(p3c_client, tenant)
    await authored_assignment(p3c_client, tenant, definition, "12.3456")
    resolved = await _resolve(p3c_engine, tenant, definition, "2026-02-01")
    assert resolved.scalar_amount == Decimal("12.3456")
    assert isinstance(resolved.scalar_amount, Decimal)


async def test_pending_assignments_are_never_resolved(p3c_client, p3c_engine, tenant):
    definition = await create_definition(p3c_client, tenant)
    pending = await create_pending(p3c_client, tenant, definition)
    await set_scalar_value(p3c_client, tenant, pending, definition, "99")
    assert await _resolve(p3c_engine, tenant, definition, "2026-02-01") is None


async def test_voided_assignments_are_never_resolved(p3c_client, p3c_engine, tenant):
    definition, first, second = await _two_schedules(p3c_client, tenant)
    await p3c_client.post(f"{BASE}/{second['driver_rate_assignment_id']}/void",
                          headers=tenant.admin, json={"reason": "wrong"})
    assert await _resolve(p3c_engine, tenant, definition, "2026-07-01") is None
    still = await _resolve(p3c_engine, tenant, definition, "2026-02-01")
    assert still.driver_rate_assignment_id == first["driver_rate_assignment_id"]
    await p3c_client.post(f"{BASE}/{first['driver_rate_assignment_id']}/void",
                          headers=tenant.admin, json={"reason": "wrong"})
    assert await _resolve(p3c_engine, tenant, definition, "2026-02-01") is None


async def test_values_always_come_from_the_selected_assignment_only(
    p3c_client, p3c_engine, tenant, cur,
):
    definition, first, second = await _two_schedules(p3c_client, tenant)
    for work_date, amount in (("2026-02-01", "25.0000"), ("2026-08-01", "30.0000")):
        resolved = await _resolve(p3c_engine, tenant, definition, work_date)
        cur.execute("""
            SELECT count(DISTINCT driverrateassignmentid) FROM payroll.driverratevalues
            WHERE driverrateassignmentid = %s AND ratecomponentdefinitionid = ANY(%s)
        """, (resolved.driver_rate_assignment_id,
              [c.rate_component_definition_id for c in resolved.components]))
        assert cur.fetchone()[0] == 1
        assert str(resolved.scalar_amount) == amount


async def test_ownership_is_verified_for_company_driver_and_definition(
    p3c_client, p3c_engine, tenant, p3b_dsn,
):
    other = build_tenant(p3b_dsn)
    definition = await create_definition(p3c_client, tenant)
    await authored_assignment(p3c_client, tenant, definition, "5")
    other_definition = await create_definition(p3c_client, other)

    with pytest.raises(CompensationOwnershipError):  # Driver from another Company
        await _resolve(p3c_engine, tenant, definition, "2026-02-01", driver_id=other.driver_a)
    with pytest.raises(CompensationOwnershipError):  # RateDefinition from another Company
        await _resolve(p3c_engine, tenant, other_definition, "2026-02-01")
    with pytest.raises(CompensationOwnershipError):  # Company mismatch
        await _resolve(p3c_engine, tenant, definition, "2026-02-01", company_id=other.company_id)
    with pytest.raises(CompensationOwnershipError):  # nonexistent identifiers
        await _resolve(p3c_engine, tenant, {"rate_definition_id": 0}, "2026-02-01")


async def test_other_drivers_schedules_are_not_resolved(p3c_client, p3c_engine, tenant):
    definition = await create_definition(p3c_client, tenant)
    await authored_assignment(p3c_client, tenant, definition, "5", driver_id=tenant.driver_a2)
    assert await _resolve(p3c_engine, tenant, definition, "2026-02-01") is None
    assert (await _resolve(
        p3c_engine, tenant, definition, "2026-02-01", driver_id=tenant.driver_a2)) is not None


async def test_two_companies_resolve_arbitrary_definitions_independently(
    p3c_client, p3c_engine, tenant, p3b_dsn,
):
    other = build_tenant(p3b_dsn)
    alpha = await create_definition(p3c_client, tenant, definition_code="ITEM_ALPHA")
    beta = await create_definition(p3c_client, other, definition_code="ITEM_BETA")
    await authored_assignment(p3c_client, tenant, alpha, "25")
    await authored_assignment(p3c_client, other, beta, "7.5")
    assert (await _resolve(p3c_engine, tenant, alpha, "2026-02-01")).scalar_amount == Decimal("25")
    assert (await _resolve(p3c_engine, other, beta, "2026-02-01", driver_id=other.driver_a)
            ).scalar_amount == Decimal("7.5")


async def test_the_resolution_endpoint_returns_the_whole_assignment(p3c_client, tenant):
    definition, first, _ = await _two_schedules(p3c_client, tenant)
    url = (f"/compensation/drivers/{tenant.driver_a}/rate-definitions/"
           f"{definition['rate_definition_id']}/resolution")
    found = await p3c_client.get(url, params={"work_date": "2026-02-01"}, headers=tenant.admin)
    assert found.status_code == 200
    assert found.json()["driver_rate_assignment_id"] == first["driver_rate_assignment_id"]
    assert found.json()["components"][0]["amount"] == "25.0000"
    missing = await p3c_client.get(url, params={"work_date": "2025-01-01"}, headers=tenant.admin)
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "NO_EFFECTIVE_ASSIGNMENT"
    denied = await p3c_client.get(
        url, params={"work_date": "2026-02-01"}, headers=tenant.headers(tenant.no_permissions))
    assert denied.status_code == 403


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _ScriptedConnection:
    def __init__(self, *results):
        self._results = list(results)

    async def execute(self, *_args, **_kwargs):
        return _Rows(self._results.pop(0))


async def test_an_impossible_multi_match_fails_closed():
    db = _ScriptedConnection(
        [{"branchid": 1}],
        [{"shape": "Scalar", "paydefinitionid": 1, "calculationmethod": "PerUnit"}],
        [{"driverrateassignmentid": 1, "status": "Approved",
          "effectivefrom": date(2026, 1, 1), "effectiveto": None},
         {"driverrateassignmentid": 2, "status": "Superseded",
          "effectivefrom": date(2026, 1, 1), "effectiveto": date(2026, 12, 31)}],
    )
    with pytest.raises(CompensationIntegrityError):
        await resolver.resolve(1, 1, 1, date(2026, 2, 1), db)
