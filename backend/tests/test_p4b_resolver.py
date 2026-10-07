"""resolve_many and the method-owned calculation boundary."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.compensation import resolver
from app.compensation.errors import CompensationOwnershipError
from app.compensation.resolver import ResolutionKey
from app.compensation.schemas import ResolvedCompensation, ResolvedComponent
from app.payroll.definition_calculation import (
    CalculationStatus,
    PeriodDefinition,
    calculate_definition_input,
    is_method_operational,
    quantity_error,
)
from tests.p3b_fixtures import p3b_cursor, p3b_database  # noqa: F401
from tests.p3c_fixtures import (  # noqa: F401 - register fixtures
    authored_assignment,
    create_definition,
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
    p3c_tenant,
)
from tests.p4b_fixtures import add_driver
from tests.test_p3c_compensation_resolver import _Rows

pytestmark = pytest.mark.asyncio


class _CountingConnection:
    """Wraps a real connection and counts the statements the resolver issues."""

    def __init__(self, inner):
        self._inner = inner
        self.statements = 0

    async def execute(self, *args, **kwargs):
        self.statements += 1
        return await self._inner.execute(*args, **kwargs)


# ---------------------------------------------------------------------------
# resolve_many
# ---------------------------------------------------------------------------

async def test_resolve_many_is_set_wise_and_deduplicates_keys(p3c_client, p3c_engine, tenant):
    definition = await create_definition(p3c_client, tenant, definition_code="ITEM_ALPHA")
    first = await authored_assignment(p3c_client, tenant, definition, "25",
                                      effective_from="2026-01-01", effective_to="2026-05-31")
    second = await authored_assignment(p3c_client, tenant, definition, "30",
                                       effective_from="2026-06-01")
    rd = definition["rate_definition_id"]
    keys = [
        ResolutionKey(tenant.driver_a, rd, date(2026, 2, 1)),
        ResolutionKey(tenant.driver_a, rd, date(2026, 2, 1)),     # duplicate
        ResolutionKey(tenant.driver_a, rd, date(2026, 7, 1)),
        ResolutionKey(tenant.driver_a, rd, date(2025, 12, 31)),   # before any schedule
    ]
    async with p3c_engine.connect() as raw:
        db = _CountingConnection(raw)
        resolved = await resolver.resolve_many(tenant.company_id, keys, db)

    # One round of statements regardless of the number of keys.
    assert db.statements == 4
    assert set(resolved) == set(keys)
    assert resolved[keys[0]].driver_rate_assignment_id == first["driver_rate_assignment_id"]
    assert resolved[keys[2]].driver_rate_assignment_id == second["driver_rate_assignment_id"]
    assert resolved[keys[3]] is None
    assert resolved[keys[0]].components[0].amount == Decimal("25.0000")
    assert resolved[keys[2]].components[0].amount == Decimal("30.0000")


async def test_resolve_many_of_nothing_issues_no_statement(p3c_engine):
    async with p3c_engine.connect() as raw:
        db = _CountingConnection(raw)
        assert await resolver.resolve_many(1, [], db) == {}
    assert db.statements == 0


async def test_resolve_many_matches_the_single_key_form(p3c_client, p3c_engine, tenant):
    definition = await create_definition(p3c_client, tenant, definition_code="ITEM_ALPHA")
    await authored_assignment(p3c_client, tenant, definition, "12.5", effective_from="2026-01-01")
    key = ResolutionKey(tenant.driver_a, definition["rate_definition_id"], date(2026, 3, 3))
    async with p3c_engine.connect() as db:
        many = (await resolver.resolve_many(tenant.company_id, [key], db))[key]
        single = await resolver.resolve(tenant.company_id, *key, db)
    assert many == single and many is not None


async def test_resolve_many_returns_each_drivers_own_schedule(p3c_client, p3c_engine, tenant):
    definition = await create_definition(p3c_client, tenant, definition_code="ITEM_ALPHA")
    await authored_assignment(p3c_client, tenant, definition, "25", effective_from="2026-01-01")
    other = add_driver(tenant)
    await authored_assignment(p3c_client, tenant, definition, "40",
                              effective_from="2026-01-01", driver_id=other)
    rd = definition["rate_definition_id"]
    keys = [ResolutionKey(tenant.driver_a, rd, date(2026, 2, 1)),
            ResolutionKey(other, rd, date(2026, 2, 1))]
    async with p3c_engine.connect() as db:
        resolved = await resolver.resolve_many(tenant.company_id, keys, db)
    assert [resolved[k].components[0].amount for k in keys] == [Decimal("25.0000"),
                                                                Decimal("40.0000")]


async def test_resolve_many_checks_ownership_of_every_key(p3c_client, p3c_engine, tenant):
    definition = await create_definition(p3c_client, tenant, definition_code="ITEM_ALPHA")
    rd = definition["rate_definition_id"]
    async with p3c_engine.connect() as db:
        with pytest.raises(CompensationOwnershipError):
            await resolver.resolve_many(
                tenant.company_id,
                [ResolutionKey(tenant.driver_a, rd, date(2026, 2, 1)),
                 ResolutionKey(tenant.driver_a + 10_000, rd, date(2026, 2, 1))], db)
        with pytest.raises(CompensationOwnershipError):
            await resolver.resolve_many(
                tenant.company_id,
                [ResolutionKey(tenant.driver_a, rd + 10_000, date(2026, 2, 1))], db)


async def test_an_ordinal_tier_schedule_is_carried_whole_in_sequence_order():
    """Three tier components resolve as ONE schedule; nothing calculates them yet."""
    day = date(2026, 2, 1)
    db = _ScriptedResolverConnection(
        [{"driverid": 1, "branchid": 7}],
        [{"ratedefinitionid": 9, "shape": "OrdinalTierSchedule", "paydefinitionid": 5,
          "calculationmethod": "OrdinalTier"}],
        [{"driverid": 1, "ratedefinitionid": 9, "workdate": day,
          "driverrateassignmentid": 3, "status": "Approved",
          "effectivefrom": date(2026, 1, 1), "effectiveto": None}],
        [{"assignment_id": 3, "rate_component_definition_id": 21, "shape": "OrdinalRange",
          "sequence_no": 1, "ordinal_from": 1, "ordinal_to": 5, "amount": Decimal("10")},
         {"assignment_id": 3, "rate_component_definition_id": 22, "shape": "OrdinalRange",
          "sequence_no": 2, "ordinal_from": 6, "ordinal_to": 10, "amount": Decimal("0")},
         {"assignment_id": 3, "rate_component_definition_id": 23, "shape": "OrdinalRange",
          "sequence_no": 3, "ordinal_from": 11, "ordinal_to": None, "amount": Decimal("7.5")}],
    )
    resolved = await resolver.resolve(1, 1, 9, day, db)
    assert resolved.rate_shape == "OrdinalTierSchedule"
    assert resolved.calculation_method == "OrdinalTier"
    assert [(c.sequence_no, c.ordinal_from, c.ordinal_to, c.amount) for c in resolved.components] \
        == [(1, 1, 5, Decimal("10")), (2, 6, 10, Decimal("0")), (3, 11, None, Decimal("7.5"))]
    assert {c.shape for c in resolved.components} == {"OrdinalRange"}
    # A multi-component schedule has no scalar projection: the contract is not scalar-only.
    with pytest.raises(ValueError):
        resolved.scalar_amount  # noqa: B018


class _ScriptedResolverConnection:
    def __init__(self, *results):
        self._results = list(results)

    async def execute(self, *_args, **_kwargs):
        return _Rows(self._results.pop(0))


# ---------------------------------------------------------------------------
# The calculation boundary
# ---------------------------------------------------------------------------

def _definition(method="PerUnit", shape="Scalar", input_type="Decimal") -> PeriodDefinition:
    return PeriodDefinition(
        payroll_period_definition_id=1, pay_definition_id=2, rate_definition_id=3,
        code="ITEM_ALPHA", name="Item alpha", input_type=input_type, unit="unit",
        calculation_method=method, calculation_method_version=1, rate_shape=shape,
        is_active=True)


def _resolved(*amounts, shape="Scalar", method="PerUnit") -> ResolvedCompensation:
    return ResolvedCompensation(
        company_id=1, driver_id=1, branch_id=1, pay_definition_id=2, rate_definition_id=3,
        rate_shape=shape, calculation_method=method, driver_rate_assignment_id=11,
        assignment_status="Approved", effective_from=date(2026, 1, 1),
        components=[ResolvedComponent(
            rate_component_definition_id=100 + i, shape="Scalar" if shape == "Scalar"
            else "OrdinalRange", sequence_no=i + 1, amount=amount)
            for i, amount in enumerate(amounts)])


async def test_per_unit_multiplies_and_quantizes_half_even():
    result = calculate_definition_input(_definition(), _resolved(Decimal("25")), Decimal("8"))
    assert result.status is CalculationStatus.CALCULATED
    assert result.amount == Decimal("200")
    assert result.driver_rate_assignment_id == 11
    # 0.0001 grid, banker's rounding at the half.
    half = calculate_definition_input(
        _definition(), _resolved(Decimal("0.00005")), Decimal("1"))
    assert half.amount == Decimal("0.0000")
    up = calculate_definition_input(
        _definition(), _resolved(Decimal("0.00015")), Decimal("1"))
    assert up.amount == Decimal("0.0002")


async def test_zero_rate_and_zero_quantity_are_valid_results():
    zero_rate = calculate_definition_input(_definition(), _resolved(Decimal("0")), Decimal("9"))
    assert (zero_rate.status, zero_rate.amount) == (CalculationStatus.CALCULATED, Decimal("0"))
    zero_quantity = calculate_definition_input(
        _definition(), _resolved(Decimal("25")), Decimal("0"))
    assert (zero_quantity.status, zero_quantity.amount) == (
        CalculationStatus.CALCULATED, Decimal("0"))


async def test_no_assignment_is_missing_never_zero():
    result = calculate_definition_input(_definition(), None, Decimal("8"))
    assert result.status is CalculationStatus.MISSING_RATE and result.amount is None
    assert result.needs_attention


async def test_a_missing_component_value_is_an_incomplete_rate():
    result = calculate_definition_input(_definition(), _resolved(None), Decimal("8"))
    assert result.status is CalculationStatus.INCOMPLETE_RATE and result.amount is None
    assert result.driver_rate_assignment_id == 11
    wrong_count = calculate_definition_input(
        _definition(), _resolved(Decimal("1"), Decimal("2")), Decimal("8"))
    assert wrong_count.status is CalculationStatus.INCOMPLETE_RATE


async def test_ordinal_tier_fails_closed_as_method_not_ready():
    assert is_method_operational("PerUnit") and not is_method_operational("OrdinalTier")
    definition = _definition("OrdinalTier", "OrdinalTierSchedule")
    resolved = _resolved(Decimal("10"), Decimal("0"), Decimal("7.5"),
                         shape="OrdinalTierSchedule", method="OrdinalTier")
    result = calculate_definition_input(definition, resolved, Decimal("12"))
    assert result.status is CalculationStatus.METHOD_NOT_READY
    assert result.amount is None and result.needs_attention
    # Not-ready is independent of whether a schedule exists.
    assert calculate_definition_input(definition, None, Decimal("12")).status \
        is CalculationStatus.METHOD_NOT_READY


async def test_quantity_validation_follows_the_frozen_input_type():
    assert quantity_error("WholeNumber", Decimal("2")) is None
    assert quantity_error("WholeNumber", Decimal("0")) is None
    assert quantity_error("WholeNumber", Decimal("2.0")) is None
    assert "whole number" in quantity_error("WholeNumber", Decimal("2.5"))
    assert quantity_error("Decimal", Decimal("2.5")) is None
    assert quantity_error("Decimal", Decimal("-1")) is not None
