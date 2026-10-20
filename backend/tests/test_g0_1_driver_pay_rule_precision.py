"""G0.1 precision contract for DriverPayRule source amounts."""
import random
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.payroll.schemas import DriverPayRuleCreate
from tests.builders.pay_rules import ensure_branch_pay_calendar


def _create_payload(amount: str) -> dict:
    return {
        "driver_id": 1,
        "rule_type": "MinimumPay",
        "amount": amount,
        "effective_from": "2191-01-01",
    }


def test_driver_pay_rule_create_accepts_extra_typed_precision_for_entry_rounding():
    """DD3 amends G0.1: the schema accepts typed precision; the service rounds it on entry."""
    assert DriverPayRuleCreate.model_validate(_create_payload("1.2345")).amount == Decimal("1.2345")
    assert DriverPayRuleCreate.model_validate(_create_payload("1.23000")).amount == Decimal("1.23000")
    assert DriverPayRuleCreate.model_validate(_create_payload("1.23456")).amount == Decimal("1.23456")


@pytest.mark.parametrize("amount", [
    "1.12345678901", "NaN", "Infinity", "-Infinity", "0", "-1", "100000000000000",
])
def test_driver_pay_rule_create_rejects_values_outside_the_typed_amount_limits(amount: str):
    with pytest.raises(ValueError):
        DriverPayRuleCreate.model_validate(_create_payload(amount))


@pytest.mark.asyncio
async def test_typed_driver_pay_rule_amount_is_normalized_persisted_and_audited(
    session_client,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    suffix = random.randint(100000, 999999)
    driver_response = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": paytest_branch_id,
            "full_name": f"G0.1 precision {suffix}",
            "driver_number": f"G01-{suffix}",
        },
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert driver_response.status_code == 201, driver_response.text
    driver_id = driver_response.json()["driver_id"]
    await ensure_branch_pay_calendar(direct_db, 1, paytest_branch_id)

    response = await session_client.post(
        "/payroll/driver-pay-rules",
        json={**_create_payload("1.2345"), "driver_id": driver_id},
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert response.status_code == 201, response.text
    rule_id = response.json()["driver_pay_rule_id"]
    # The Company currency is USD: 1.2345 is normalized to two decimals (half-even) on entry.
    assert Decimal(str(response.json()["amount"])) == Decimal("1.23")

    persisted = (await direct_db.execute(text("""
        SELECT r.amount, (a.newvaluejson::jsonb)->>'amount' AS audit_amount
        FROM payroll.driverpayrules r
        JOIN audit.auditlog a
          ON a.entityschema = 'payroll'
         AND a.entityname = 'DriverPayRules'
         AND a.entityid = r.driverpayruleid::text
         AND a.actioncode = 'DRIVER_PAY_RULE_CREATED'
        WHERE r.driverpayruleid = :rule_id
    """), {"rule_id": rule_id})).mappings().one()
    assert Decimal(str(persisted["amount"])) == Decimal("1.23")
    assert Decimal(persisted["audit_amount"]) == Decimal(str(persisted["amount"]))

    cleanup = await session_client.post(
        f"/payroll/driver-pay-rules/{rule_id}/void",
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert cleanup.status_code == 200, cleanup.text


@pytest.mark.asyncio
async def test_pay_rule_amount_that_rounds_to_zero_creates_no_row_or_audit(
    session_client,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    suffix = random.randint(100000, 999999)
    driver_response = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": paytest_branch_id,
            "full_name": f"G0.1 rejected precision {suffix}",
            "driver_number": f"G01R-{suffix}",
        },
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert driver_response.status_code == 201, driver_response.text
    driver_id = driver_response.json()["driver_id"]
    await ensure_branch_pay_calendar(direct_db, 1, paytest_branch_id)
    effective_from = date(2192, 1, 1)

    before_rows = (await direct_db.execute(text("""
        SELECT COUNT(*) FROM payroll.driverpayrules
        WHERE driverid = :driver_id AND effectivefrom = :effective_from
    """), {"driver_id": driver_id, "effective_from": effective_from})).scalar_one()
    before_audits = (await direct_db.execute(text("""
        SELECT COUNT(*) FROM audit.auditlog
        WHERE actioncode = 'DRIVER_PAY_RULE_CREATED'
          AND entityname = 'DriverPayRules'
          AND newvaluejson::jsonb->>'driver_id' = :driver_id
    """), {"driver_id": str(driver_id)})).scalar_one()

    response = await session_client.post(
        "/payroll/driver-pay-rules",
        json={**_create_payload("0.004"), "driver_id": driver_id,
              "effective_from": effective_from.isoformat()},
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "AMOUNT_ROUNDS_TO_ZERO"

    after_rows = (await direct_db.execute(text("""
        SELECT COUNT(*) FROM payroll.driverpayrules
        WHERE driverid = :driver_id AND effectivefrom = :effective_from
    """), {"driver_id": driver_id, "effective_from": effective_from})).scalar_one()
    after_audits = (await direct_db.execute(text("""
        SELECT COUNT(*) FROM audit.auditlog
        WHERE actioncode = 'DRIVER_PAY_RULE_CREATED'
          AND entityname = 'DriverPayRules'
          AND newvaluejson::jsonb->>'driver_id' = :driver_id
    """), {"driver_id": str(driver_id)})).scalar_one()
    assert after_rows == before_rows == 0
    assert after_audits == before_audits == 0
