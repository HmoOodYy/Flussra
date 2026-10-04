"""G0.1 precision contract for DriverPayRule source amounts."""
import random
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.payroll.schemas import DriverPayRuleCreate


def _create_payload(amount: str) -> dict:
    return {
        "driver_id": 1,
        "rule_type": "MinimumPay",
        "amount": amount,
        "effective_from": "2191-01-01",
    }


def test_driver_pay_rule_create_accepts_four_decimals_and_exact_trailing_zero_scale():
    assert DriverPayRuleCreate.model_validate(_create_payload("1.2345")).amount == Decimal("1.2345")
    assert DriverPayRuleCreate.model_validate(_create_payload("1.23000")).amount == Decimal("1.23000")


@pytest.mark.parametrize("amount", [
    "1.23456", "NaN", "Infinity", "-Infinity", "0", "-1", "100000000000000",
])
def test_driver_pay_rule_create_rejects_values_outside_numeric_18_4(amount: str):
    with pytest.raises(ValueError):
        DriverPayRuleCreate.model_validate(_create_payload(amount))


@pytest.mark.asyncio
async def test_four_decimal_driver_pay_rule_persists_and_audits_canonical_amount(
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
            "driver_code": f"G01-{suffix}",
        },
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert driver_response.status_code == 201, driver_response.text
    driver_id = driver_response.json()["driver_id"]

    response = await session_client.post(
        "/payroll/driver-pay-rules",
        json={**_create_payload("1.2345"), "driver_id": driver_id},
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert response.status_code == 201, response.text
    rule_id = response.json()["driver_pay_rule_id"]
    assert Decimal(str(response.json()["amount"])) == Decimal("1.2345")

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
    assert Decimal(str(persisted["amount"])) == Decimal("1.2345")
    assert Decimal(persisted["audit_amount"]) == Decimal(str(persisted["amount"]))

    cleanup = await session_client.post(
        f"/payroll/driver-pay-rules/{rule_id}/void",
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert cleanup.status_code == 200, cleanup.text


@pytest.mark.asyncio
async def test_five_decimal_driver_pay_rule_request_creates_no_row_or_audit(
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
            "driver_code": f"G01R-{suffix}",
        },
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert driver_response.status_code == 201, driver_response.text
    driver_id = driver_response.json()["driver_id"]
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
        json={**_create_payload("1.23456"), "driver_id": driver_id,
              "effective_from": effective_from.isoformat()},
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert response.status_code == 422, response.text

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
