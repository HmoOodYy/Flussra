"""Real PostgreSQL serialization orders for Company currency and retry runner."""
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.company_currency import lock_and_get_company_currency_for_monetary_write
from app.db.transaction_retry import run_retryable_transaction
from tests.p3a_currency_fixtures import (
    INSERT_DRIVER_RATE,
    create_branch_and_driver,
    first_rate_type_id,
)


async def _company_and_driver(engine):
    code = "P3ARACE_" + uuid4().hex[:14]
    async with engine.begin() as db:
        company_id = (await db.execute(text("""
            INSERT INTO core.companies(companycode, companyname, currencycode)
            VALUES (:code, 'P3a race', 'USD') RETURNING companyid
        """), {"code": code})).scalar_one()
        branch_id, driver_id = await create_branch_and_driver(db, company_id, code[-12:])
        rate_type_id = await first_rate_type_id(db)
    return {"cid": company_id, "bid": branch_id, "did": driver_id,
            "rid": rate_type_id, "amount": "1.2345"}


async def _cleanup(engine, params):
    cid = params["cid"]
    async with engine.begin() as db:
        # A monetary row permanently locks the Company currency; remove the
        # whole disposable test company (rates, driver, employee, branch).
        await db.execute(text("DELETE FROM payroll.driverrates WHERE companyid=:cid"), {"cid": cid})
        await db.execute(text("DELETE FROM core.drivers WHERE companyid=:cid"), {"cid": cid})
        await db.execute(text("DELETE FROM core.employees WHERE companyid=:cid"), {"cid": cid})
        await db.execute(text("DELETE FROM core.branches WHERE companyid=:cid"), {"cid": cid})
        await db.execute(text("DELETE FROM core.companies WHERE companyid=:cid"), {"cid": cid})


@pytest.mark.asyncio
@pytest.mark.parametrize("operation_name", ["submit", "resubmit"])
async def test_change_first_retries_a_real_repeatable_read_transaction(test_engine, operation_name):
    rate_params = await _company_and_driver(test_engine)
    company_id = rate_params["cid"]
    change_held = asyncio.Event()
    snapshot_taken = asyncio.Event()
    change_committed = asyncio.Event()
    observations = []
    async def change():
        async with test_engine.begin() as db:
            await db.execute(text("UPDATE core.companies SET currencycode='EUR' WHERE companyid=:cid"), {"cid": company_id})
            change_held.set()
            await asyncio.wait_for(snapshot_taken.wait(), 5)
        change_committed.set()

    async def operation(db):
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        seen = (await db.execute(text("SELECT currencycode FROM core.companies WHERE companyid=:cid"), {"cid": company_id})).scalar_one()
        observations.append(seen)
        if len(observations) == 1:
            snapshot_taken.set()
            await asyncio.wait_for(change_committed.wait(), 5)
        currency = await lock_and_get_company_currency_for_monetary_write(company_id, db)
        await db.execute(INSERT_DRIVER_RATE, rate_params)
        return currency.code

    async def no_delay(_seconds):
        return None

    task = asyncio.create_task(change())
    try:
        await asyncio.wait_for(change_held.wait(), 5)
        result = await asyncio.wait_for(
            run_retryable_transaction(test_engine, operation, operation_name=operation_name, sleep=no_delay),
            10,
        )
        await task
        assert result == "EUR"
        assert observations == ["USD", "EUR"]
        async with test_engine.connect() as db:
            row = (await db.execute(text("""
                SELECT c.currencycode, COUNT(dr.driverrateid) AS rate_count
                FROM core.companies c
                LEFT JOIN payroll.driverrates dr ON dr.companyid = c.companyid
                WHERE c.companyid=:cid GROUP BY c.currencycode
            """), {"cid": company_id})).mappings().one()
            assert row["currencycode"] == "EUR" and row["rate_count"] == 1
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await _cleanup(test_engine, rate_params)

@pytest.mark.asyncio
async def test_writer_first_locks_currency_before_monetary_commit(test_engine):
    rate_params = await _company_and_driver(test_engine)
    company_id = rate_params["cid"]
    writer_locked = asyncio.Event()
    change_started = asyncio.Event()
    async def writer():
        async with test_engine.begin() as db:
            await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            currency = await lock_and_get_company_currency_for_monetary_write(company_id, db)
            assert currency.code == "USD"
            await db.execute(INSERT_DRIVER_RATE, rate_params)
            writer_locked.set()
            await asyncio.wait_for(change_started.wait(), 5)

    async def change():
        await asyncio.wait_for(writer_locked.wait(), 5)
        change_started.set()
        async with test_engine.begin() as db:
            await db.execute(text("UPDATE core.companies SET currencycode='EUR' WHERE companyid=:cid"), {"cid": company_id})

    try:
        results = await asyncio.wait_for(asyncio.gather(writer(), change(), return_exceptions=True), 10)
        assert results[0] is None
        assert isinstance(results[1], DBAPIError)
        assert "COMPANY_CURRENCY_CHANGE_BLOCKED" in str(results[1])
        async with test_engine.connect() as db:
            code = (await db.execute(text("SELECT currencycode FROM core.companies WHERE companyid=:cid"), {"cid": company_id})).scalar_one()
            assert code == "USD"
    finally:
        await _cleanup(test_engine, rate_params)
