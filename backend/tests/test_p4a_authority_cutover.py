"""Legacy PayDefinition, CDPI and ordinary PerUnit rate authorities are closed.

Status pay still uses the temporary Status-only rate path until Status
compensation is cut over; these tests pin both sides of that boundary.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text


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


async def _status_rate_column(session_client, token: str, branch_id: int) -> dict:
    """Create a Status rate column for the Branch (the Status-only RateType owner)."""
    created = await session_client.post(
        f"/settings/branches/{branch_id}/status-rate-columns", headers=_auth(token),
        json={"column_name": f"P4A {uuid4().hex[:8]}"})
    assert created.status_code in (200, 201), created.text
    return created.json()


async def test_ordinary_legacy_rate_writers_are_closed(
    session_client, auth_token, paytest_rate_type_id, created_driver_id, direct_db,
):
    headers = _auth(auth_token)
    create = await session_client.post("/payroll/rates", headers=headers, json={
        "driver_id": created_driver_id, "rate_type_id": paytest_rate_type_id,
        "amount": "10", "effective_from": "2088-01-01"})
    assert create.status_code == 409, create.text
    assert create.json()["detail"]["code"] == "ORDINARY_RATE_AUTHORING_RETIRED"

    batch = await session_client.post(
        f"/payroll/drivers/{created_driver_id}/rates/batch", headers=headers,
        json={"effective_from": "2088-01-01",
              "changes": [{"pay_item_id": 1, "rate_type_id": paytest_rate_type_id, "amount": "5"}]})
    assert batch.status_code == 409, batch.text
    assert batch.json()["detail"]["code"] == "ORDINARY_RATE_AUTHORING_RETIRED"

    # A pre-existing ordinary Pending rate cannot be edited, approved or voided either.
    branch_id = (await direct_db.execute(
        text("SELECT branchid FROM core.drivers WHERE driverid = :d"),
        {"d": created_driver_id})).scalar_one()
    rate_id = (await direct_db.execute(text("""
        INSERT INTO payroll.driverrates
            (companyid, branchid, driverid, ratetypeid, amount, effectivefrom, status)
        VALUES (1, :b, :d, :r, 3, DATE '2088-02-01', 'PendingApproval') RETURNING driverrateid
    """), {"b": branch_id, "d": created_driver_id, "r": paytest_rate_type_id})).scalar_one()
    try:
        for call in (
            session_client.patch(f"/payroll/rates/{rate_id}", headers=headers, json={"amount": "4"}),
            session_client.post(f"/payroll/rates/{rate_id}/approve", headers=headers),
            session_client.delete(f"/payroll/rates/{rate_id}", headers=headers),
        ):
            response = await call
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["code"] == "ORDINARY_RATE_AUTHORING_RETIRED"
        status = (await direct_db.execute(
            text("SELECT status FROM payroll.driverrates WHERE driverrateid = :r"),
            {"r": rate_id})).scalar_one()
        assert status == "PendingApproval"
    finally:
        await direct_db.execute(text("DELETE FROM payroll.driverrates WHERE driverrateid = :r"),
                                {"r": rate_id})


async def test_copy_rates_is_unavailable(session_client, auth_token, created_driver_id):
    response = await session_client.post(
        f"/payroll/drivers/{created_driver_id}/rates/copy-from/{created_driver_id}",
        headers=_auth(auth_token), json={"effective_from": "2088-01-01"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "RATE_COPY_UNAVAILABLE"


async def test_rate_matrix_exposes_only_status_rate_columns(
    session_client, auth_token, created_driver_id, direct_db,
):
    branch_id = (await direct_db.execute(
        text("SELECT branchid FROM core.drivers WHERE driverid = :d"),
        {"d": created_driver_id})).scalar_one()
    await _status_rate_column(session_client, auth_token, branch_id)
    response = await session_client.get(
        f"/payroll/drivers/{created_driver_id}/rate-matrix", headers=_auth(auth_token))
    assert response.status_code == 200, response.text
    groups = response.json()["groups"]
    assert groups, "the Branch has a default Status rate column"
    assert {g["rate_source"] for g in groups} == {"StatusRateColumn"}
    assert all(g["pay_item_id"] is None for g in groups)


async def test_status_pay_rates_still_use_the_temporary_status_only_path(
    session_client, auth_token, created_driver_id, direct_db,
):
    headers = _auth(auth_token)
    branch_id = (await direct_db.execute(
        text("SELECT branchid FROM core.drivers WHERE driverid = :d"),
        {"d": created_driver_id})).scalar_one()
    rate_type_id = (await _status_rate_column(session_client, auth_token, branch_id))["rate_type_id"]

    created = await session_client.post("/payroll/rates", headers=headers, json={
        "driver_id": created_driver_id, "rate_type_id": rate_type_id,
        "amount": "12.5", "effective_from": "2088-03-01"})
    assert created.status_code == 201, created.text
    rate_id = created.json()["driver_rate_id"]
    approved = await session_client.post(f"/payroll/rates/{rate_id}/approve", headers=headers)
    assert approved.status_code == 200, approved.text
    voided = await session_client.delete(f"/payroll/rates/{rate_id}", headers=headers)
    assert voided.status_code in (200, 204), voided.text


async def test_ordinary_authoring_still_works_on_the_target_path(
    session_client, auth_token, hq_branch_id, created_driver_id,
):
    headers = _auth(auth_token)
    definition = await session_client.post("/compensation/pay-definitions", headers=headers, json={
        "definition_name": "Cutover item", "input_type": "Decimal", "calculation_method": "PerUnit"})
    assert definition.status_code == 201, definition.text
    body = definition.json()
    configured = await session_client.patch(
        f"/compensation/branches/{hq_branch_id}/pay-definitions/{body['pay_definition_id']}",
        headers=headers, json={"is_active": True})
    assert configured.status_code == 200, configured.text
    rows = await session_client.get(
        f"/compensation/drivers/{created_driver_id}/pay-rates", headers=headers)
    assert body["pay_definition_id"] in {r["pay_definition_id"] for r in rows.json()}
