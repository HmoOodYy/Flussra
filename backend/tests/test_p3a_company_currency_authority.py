"""P3a Company currency catalog, configuration and direct database invariants."""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.company_currency import (
    CompanyCurrency,
    company_has_durable_monetary_state,
    frozen_currency,
    lock_and_get_company_currency,
)
from app.settings import service as settings_service
from app.settings.schemas import CompanyUpdate


@pytest.mark.asyncio
async def test_supported_catalog_and_auth_context(session_client, auth_token):
    headers = {"Authorization": f"Bearer {auth_token}"}
    response = await session_client.get("/settings/currencies", headers=headers)
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) >= 160
    assert [row["currency_code"] for row in rows] == sorted(row["currency_code"] for row in rows)
    by_code = {row["currency_code"]: row for row in rows}
    assert {code: by_code[code]["minor_unit_digits"] for code in ("JPY", "USD", "KWD", "CLF")} == {
        "JPY": 0, "USD": 2, "KWD": 3, "CLF": 4,
    }
    assert "BGN" not in by_code
    profile = await session_client.get("/settings/company", headers=headers)
    assert profile.status_code == 200, profile.text
    assert profile.json()["currency_code"] == "USD"
    assert profile.json()["currency_minor_unit_digits"] == 2
    me = await session_client.get("/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["currency_code"] == "USD"
    assert me.json()["currency_minor_unit_digits"] == 2


@pytest.mark.asyncio
async def test_company_currency_configuration_and_permanent_lock(test_engine, monkeypatch):
    async def allow(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings_service, "_ensure_company_admin", allow)
    monkeypatch.setattr(settings_service, "_check_branch_access", allow)
    monkeypatch.setattr(settings_service, "_write_settings_audit", allow)

    async with test_engine.connect() as db:
        transaction = await db.begin()
        try:
            code = "P3A_" + uuid4().hex[:16]
            company_id = (await db.execute(text("""
                INSERT INTO core.companies(companycode, companyname)
                VALUES (:code, 'P3a isolation') RETURNING companyid
            """), {"code": code})).scalar_one()
            def update(currency):
                return CompanyUpdate(company_name="P3a isolation", currency_code=currency)
            profile = await settings_service.get_company_profile(company_id, 1, db)
            assert profile.currency_code is None and not profile.currency_change_locked
            with pytest.raises(HTTPException) as missing:
                await lock_and_get_company_currency(company_id, db)
            assert missing.value.detail["code"] == "COMPANY_CURRENCY_REQUIRED"
            with pytest.raises(HTTPException) as unsupported:
                await settings_service.update_company_profile(company_id, 1, update("ZZZ"), db)
            assert unsupported.value.detail["code"] == "UNSUPPORTED_CURRENCY_CODE"
            with pytest.raises(HTTPException) as null:
                await settings_service.update_company_profile(company_id, 1, update(None), db)
            assert null.value.detail["code"] == "COMPANY_CURRENCY_REQUIRED"
            profile = await settings_service.update_company_profile(company_id, 1, update(" usd "), db)
            assert profile.currency_code == "USD" and not profile.currency_change_locked
            profile = await settings_service.update_company_profile(company_id, 1, update("USD"), db)
            assert profile.currency_code == "USD"
            profile = await settings_service.update_company_profile(company_id, 1, update("JPY"), db)
            assert profile.currency_code == "JPY" and profile.currency_minor_unit_digits == 0

            profile_id = (await db.execute(text("""
                INSERT INTO payroll.payprofiles(companyid, profilecode, profilename, effectivefrom)
                VALUES (:cid, :code, 'P3a profile', DATE '2099-01-01')
                RETURNING payprofileid
            """), {"cid": company_id, "code": code})).scalar_one()
            rate_type_id = (await db.execute(text("SELECT ratetypeid FROM payroll.ratetypes ORDER BY ratetypeid LIMIT 1"))).scalar_one()
            await db.execute(text("""
                INSERT INTO payroll.payprofilerates(payprofileid, ratetypeid, rateamount, effectivefrom)
                VALUES (:pid, :rid, 1.2345, DATE '2099-01-01')
            """), {"pid": profile_id, "rid": rate_type_id})
            assert await company_has_durable_monetary_state(company_id, db)
            profile = await settings_service.get_company_profile(company_id, 1, db)
            assert profile.currency_change_locked
            with pytest.raises(HTTPException) as changed:
                await settings_service.update_company_profile(company_id, 1, update("USD"), db)
            assert changed.value.detail["code"] == "COMPANY_CURRENCY_CHANGE_BLOCKED"
            # Cleanup of an active row must not be used to unlock the Company. A
            # lifecycle change (to Voided) retains the durable row.
            await db.execute(text("UPDATE payroll.payprofilerates SET status = 'Voided' WHERE payprofileid = :pid"), {"pid": profile_id})
            assert await company_has_durable_monetary_state(company_id, db)
            with pytest.raises(DBAPIError) as direct:
                await db.execute(text("UPDATE core.companies SET currencycode = 'USD' WHERE companyid = :cid"), {"cid": company_id})
            assert "COMPANY_CURRENCY_CHANGE_BLOCKED" in str(direct.value)
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_missing_frozen_currency_fails_closed():
    with pytest.raises(HTTPException) as missing:
        frozen_currency(None, None)
    assert missing.value.detail["code"] == "SNAPSHOT_CURRENCY_REQUIRED"
    assert frozen_currency("KWD", 3) == CompanyCurrency("KWD", 3)
