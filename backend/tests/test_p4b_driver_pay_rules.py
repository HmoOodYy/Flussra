"""DriverPayRule (Minimum/Maximum pay) API contract.

DriverPayRules are independent of the PayItem/PayDefinition runtime and stay live across
the P4b cutover, so the CRUD, validation, overlap, audit-atomicity, access-boundary and
finalized-period guard contracts of the retired Phase-2C suites are covered here against
the disposable tenant.
"""
from __future__ import annotations

import pytest

from tests.p3b_fixtures import p3b_cursor, p3b_database  # noqa: F401 - register fixtures
from tests.p3c_fixtures import (  # noqa: F401 - register fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.p4b_fixtures import (
    build_payroll_tenant,
    make_driver_self_user,
    make_user,
    query,
)

pytestmark = pytest.mark.asyncio

RULES = "/payroll/driver-pay-rules"


@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


def _rule(tenant, rule_type="MinimumPay", amount="100.00", start="2200-01-01", end=None,
          driver_id=None, **extra) -> dict:
    return {"driver_id": driver_id or tenant.driver_a, "rule_type": rule_type,
            "amount": amount, "effective_from": start, "effective_to": end, **extra}


async def _create(client, tenant, **kwargs) -> int:
    response = await client.post(RULES, json=_rule(tenant, **kwargs), headers=tenant.admin)
    assert response.status_code == 201, response.text
    return response.json()["driver_pay_rule_id"]


# ---------------------------------------------------------------------------
# Create, read, end, void
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rule_type", ["MinimumPay", "MaximumPay"])
async def test_minimum_and_maximum_rules_are_created_active_and_can_be_ended_and_voided(
    p3c_client, tenant, rule_type,
):
    rule_id = await _create(p3c_client, tenant, rule_type=rule_type, amount="450.00",
                            start="2201-01-01", end="2201-01-31")
    detail = (await p3c_client.get(f"{RULES}/{rule_id}", headers=tenant.admin)).json()
    assert detail["status"] == "Active" and detail["rule_type"] == rule_type
    ended = await p3c_client.post(f"{RULES}/{rule_id}/end", json={"effective_to": "2201-01-15"},
                                  headers=tenant.admin)
    assert ended.status_code == 200 and ended.json()["effective_to"] == "2201-01-15"
    voided = await p3c_client.post(f"{RULES}/{rule_id}/void", headers=tenant.admin)
    assert voided.status_code == 200 and voided.json()["status"] == "Voided"


async def test_unknown_drivers_and_rules_are_not_found(p3c_client, tenant):
    unknown = await p3c_client.post(
        RULES, json=_rule(tenant, driver_id=999999999), headers=tenant.admin)
    assert unknown.status_code == 404
    assert (await p3c_client.get(f"{RULES}/999999", headers=tenant.admin)).status_code == 404
    assert (await p3c_client.post(f"{RULES}/999999/void", headers=tenant.admin)).status_code == 404


async def test_a_rule_of_another_company_driver_cannot_be_created_or_read(
    p3c_client, tenant,
):
    other = build_payroll_tenant(tenant.dsn)
    response = await p3c_client.post(
        RULES, json=_rule(tenant, driver_id=other.driver_a), headers=tenant.admin)
    assert response.status_code == 404
    rule_id = await _create(p3c_client, tenant)
    assert (await p3c_client.get(f"{RULES}/{rule_id}", headers=other.admin)).status_code == 404
    assert (await p3c_client.post(f"{RULES}/{rule_id}/void", headers=other.admin)
            ).status_code == 404


# ---------------------------------------------------------------------------
# Validation and overlap
# ---------------------------------------------------------------------------

async def test_amounts_must_be_positive_and_ranges_ordered(p3c_client, tenant):
    for amount in ("0", "-1.00"):
        response = await p3c_client.post(
            RULES, json=_rule(tenant, amount=amount), headers=tenant.admin)
        assert response.status_code == 422, amount
    reverse = await p3c_client.post(
        RULES, json=_rule(tenant, start="2200-02-01", end="2200-01-31"), headers=tenant.admin)
    assert reverse.status_code == 422
    bad_type = await p3c_client.post(
        RULES, json=_rule(tenant, rule_type="MiddlePay"), headers=tenant.admin)
    assert bad_type.status_code == 422


async def test_overlapping_rules_are_rejected_and_contiguous_ranges_allowed(
    p3c_client, tenant,
):
    await _create(p3c_client, tenant, amount="500", start="2201-01-01", end="2201-01-31")
    overlap = await p3c_client.post(
        RULES, json=_rule(tenant, amount="600", start="2201-01-15", end="2201-02-15"),
        headers=tenant.admin)
    assert overlap.status_code == 422
    await _create(p3c_client, tenant, amount="600", start="2201-02-01", end="2201-02-28")
    # A different rule type or another driver may cover the same dates.
    await _create(p3c_client, tenant, rule_type="MaximumPay", amount="900",
                  start="2201-01-01", end="2201-01-31")
    await _create(p3c_client, tenant, amount="500", start="2201-01-01", end="2201-01-31",
                  driver_id=tenant.driver_a2)

    ended = await _create(p3c_client, tenant, amount="700", start="2201-03-01", end="2201-03-31")
    assert (await p3c_client.post(f"{RULES}/{ended}/end", json={"effective_to": "2201-03-15"},
                                  headers=tenant.admin)).status_code == 200
    still_covered = await p3c_client.post(
        RULES, json=_rule(tenant, amount="800", start="2201-03-10", end="2201-04-01"),
        headers=tenant.admin)
    assert still_covered.status_code == 422
    after_end = await p3c_client.post(
        RULES, json=_rule(tenant, amount="800", start="2201-03-16", end="2201-04-01"),
        headers=tenant.admin)
    assert after_end.status_code == 201


async def test_list_filters_notes_update_and_lifecycle_guards(p3c_client, tenant):
    rule_id = await _create(p3c_client, tenant, rule_type="MaximumPay", amount="2200",
                            start="2203-01-01", end="2203-01-31")
    listed = await p3c_client.get(
        f"/payroll/drivers/{tenant.driver_a}/pay-rules",
        params={"rule_type": "MaximumPay", "status": "Active"}, headers=tenant.admin)
    assert [r["driver_pay_rule_id"] for r in listed.json()] == [rule_id]
    empty = await p3c_client.get(
        f"/payroll/drivers/{tenant.driver_a}/pay-rules", params={"rule_type": "MinimumPay"},
        headers=tenant.admin)
    assert empty.json() == []

    before = (await p3c_client.get(f"{RULES}/{rule_id}", headers=tenant.admin)).json()
    patched = await p3c_client.patch(f"{RULES}/{rule_id}", json={"notes": "note"},
                                     headers=tenant.admin)
    assert patched.status_code == 200 and patched.json()["notes"] == "note"
    assert patched.json()["amount"] == before["amount"]
    assert patched.json()["effective_from"] == before["effective_from"]

    assert (await p3c_client.post(f"{RULES}/{rule_id}/end", json={"effective_to": "2203-01-15"},
                                  headers=tenant.admin)).status_code == 200
    assert (await p3c_client.post(f"{RULES}/{rule_id}/end", json={"effective_to": "2203-01-20"},
                                  headers=tenant.admin)).status_code == 422
    assert (await p3c_client.patch(f"{RULES}/{rule_id}", json={"notes": "after end"},
                                   headers=tenant.admin)).status_code == 200
    assert (await p3c_client.post(f"{RULES}/{rule_id}/void", headers=tenant.admin)
            ).status_code == 200
    assert (await p3c_client.post(f"{RULES}/{rule_id}/void", headers=tenant.admin)
            ).status_code == 422
    assert (await p3c_client.patch(f"{RULES}/{rule_id}", json={"notes": "after void"},
                                   headers=tenant.admin)).status_code == 422


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------

async def test_every_pay_rule_write_rolls_back_when_its_audit_fails(
    p3c_client, tenant, monkeypatch,
):
    first = await _create(p3c_client, tenant, amount="200", start="2210-02-01", end="2210-02-28")
    second = await _create(p3c_client, tenant, rule_type="MaximumPay", amount="3000",
                           start="2210-03-01", end="2210-03-31")
    third = await _create(p3c_client, tenant, amount="250", start="2210-04-01", end="2210-04-30")

    async def failing_audit(*_args, **_kwargs):
        raise RuntimeError("simulated pay-rule audit failure")

    monkeypatch.setattr("app.payroll.driver_pay_rules._write_pay_rule_audit", failing_audit)
    with pytest.raises(RuntimeError):
        await p3c_client.post(
            RULES, json=_rule(tenant, amount="100", start="2210-01-01", end="2210-01-31",
                              notes="audit-failure-create"), headers=tenant.admin)
    with pytest.raises(RuntimeError):
        await p3c_client.post(f"{RULES}/{first}/end", json={"effective_to": "2210-02-15"},
                              headers=tenant.admin)
    with pytest.raises(RuntimeError):
        await p3c_client.post(f"{RULES}/{second}/void", headers=tenant.admin)
    with pytest.raises(RuntimeError):
        await p3c_client.patch(f"{RULES}/{third}", json={"notes": "must not commit"},
                               headers=tenant.admin)

    assert query(tenant, "SELECT count(*) FROM payroll.driverpayrules "
                         "WHERE notes = 'audit-failure-create'") == [(0,)]
    states = query(tenant, "SELECT status, effectiveto, notes FROM payroll.driverpayrules "
                           "WHERE driverpayruleid IN (%s, %s, %s) ORDER BY driverpayruleid",
                   (first, second, third))
    assert [row[0] for row in states] == ["Active", "Active", "Active"]
    assert states[0][1].isoformat() == "2210-02-28" and states[2][2] is None


# ---------------------------------------------------------------------------
# Access boundary
# ---------------------------------------------------------------------------

async def test_pay_rules_need_the_rate_permissions_and_deny_driver_self_accounts(
    p3c_client, tenant,
):
    rule_id = await _create(p3c_client, tenant)
    viewer = make_user(tenant, ["payrates.view"])
    editor = make_user(tenant, ["payrates.edit"])
    nobody = make_user(tenant, [])
    entry_only = make_user(tenant, ["payroll.entry", "payroll.view"])
    driver = make_driver_self_user(tenant, ["payrates.view", "payrates.edit"])

    assert (await p3c_client.get(f"/payroll/drivers/{tenant.driver_a}/pay-rules",
                                 headers=viewer)).status_code == 200
    assert (await p3c_client.post(RULES, json=_rule(tenant, start="2300-01-01"),
                                  headers=viewer)).status_code == 403
    assert (await p3c_client.post(
        RULES, json=_rule(tenant, driver_id=tenant.driver_a2, start="2300-01-01"),
        headers=editor)).status_code == 201
    for headers in (nobody, entry_only, driver):
        assert (await p3c_client.get(f"/payroll/drivers/{tenant.driver_a}/pay-rules",
                                     headers=headers)).status_code == 403
        assert (await p3c_client.get(f"{RULES}/{rule_id}", headers=headers)).status_code == 403
        assert (await p3c_client.post(RULES, json=_rule(tenant, start="2400-01-01"),
                                      headers=headers)).status_code == 403
    for headers in (viewer, nobody, driver):
        assert (await p3c_client.post(f"{RULES}/{rule_id}/end",
                                      json={"effective_to": "2200-01-10"},
                                      headers=headers)).status_code == 403
        assert (await p3c_client.post(f"{RULES}/{rule_id}/void", headers=headers)
                ).status_code == 403


async def test_a_branch_scoped_editor_cannot_manage_rules_of_another_branch(
    p3c_client, tenant,
):
    editor = make_user(tenant, ["payrates.edit", "payrates.view"], scope="SpecificBranch",
                       branch_id=tenant.branch_a)
    own = await p3c_client.post(RULES, json=_rule(tenant, start="2500-01-01"), headers=editor)
    assert own.status_code == 201
    foreign = await p3c_client.post(
        RULES, json=_rule(tenant, driver_id=tenant.driver_b, start="2500-01-01"),
        headers=editor)
    assert foreign.status_code in (403, 404)


# ---------------------------------------------------------------------------
# Finalized periods
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["Locked", "Archived"])
async def test_a_rule_cannot_take_effect_inside_a_finalized_period(
    p3c_client, tenant, status,
):
    query(tenant, """
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, periodcode, periodname, periodtype,
             startdate, enddate, paydate, status, createdbyuserid)
        SELECT %s, %s, 'FINAL-1', 'Finalized', 'Month', DATE '2070-01-01', DATE '2070-01-31',
               DATE '2070-01-31', 'Locked', %s
    """, (tenant.company_id, tenant.branch_a, tenant.owner))
    if status == "Archived":
        query(tenant, "UPDATE payroll.payrollperiods SET status = 'Archived' "
                      "WHERE periodcode = 'FINAL-1' AND companyid = %s", (tenant.company_id,))
    for rule_type in ("MinimumPay", "MaximumPay"):
        inside = await p3c_client.post(
            RULES, json=_rule(tenant, rule_type=rule_type, start="2070-01-15"),
            headers=tenant.admin)
        assert inside.status_code == 422, (rule_type, inside.text)
    outside = await p3c_client.post(
        RULES, json=_rule(tenant, start="2070-02-01"), headers=tenant.admin)
    assert outside.status_code == 201
    assert query(tenant, "SELECT count(*) FROM payroll.driverpayrules "
                         "WHERE driverid = %s AND effectivefrom = DATE '2070-01-15'",
                 (tenant.driver_a,)) == [(0,)]
