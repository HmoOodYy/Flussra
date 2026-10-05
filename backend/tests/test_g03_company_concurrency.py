"""G0.3 Company row-lock ownership, proven on real PostgreSQL.

Ordinary monetary writers take a shared Company row guard (FOR SHARE); Company
mutation takes the conflicting row lock first. Blocking is observed through
pg_blocking_pids and lock_timeout (never sleeps), so the proofs do not depend on
timing.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

import app.company_currency as company_currency
from app.company_concurrency import lock_company_for_monetary_use, lock_company_for_mutation
from app.company_currency import lock_and_get_company_currency_for_monetary_write
from app.db.transaction_retry import retryable_sqlstate
from app.settings import service as settings_service
from app.settings.schemas import CompanyUpdate

_LOCK_NOT_AVAILABLE = "55P03"


async def _make_company(engine, currency: str | None = "USD") -> int:
    code = "G03_" + uuid4().hex[:16]
    async with engine.begin() as db:
        return int((await db.execute(text("""
            INSERT INTO core.companies(companycode, companyname, currencycode)
            VALUES (:code, 'G0.3 company', :cur) RETURNING companyid
        """), {"code": code, "cur": currency})).scalar_one())


async def _drop_company(engine, company_id: int) -> None:
    async with engine.begin() as db:
        await db.execute(text("DELETE FROM core.companies WHERE companyid = :cid"), {"cid": company_id})


@asynccontextmanager
async def _tx(engine, *, lock_timeout_ms: int | None = None):
    async with engine.connect() as conn:
        tx = await conn.begin()
        try:
            if lock_timeout_ms is not None:
                await conn.execute(text(f"SET LOCAL lock_timeout = {int(lock_timeout_ms)}"))
            yield conn
        finally:
            if tx.is_active:
                await tx.rollback()


async def _backend_pid(conn) -> int:
    return int((await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one())


async def _wait_until_blocked(engine, backend_pid: int) -> None:
    async def poll():
        async with engine.connect() as observer:
            while True:
                blocked = (await observer.execute(
                    text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": backend_pid},
                )).scalar_one()
                if blocked:
                    return
                await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout=10)


async def _assert_lock_unavailable(coro) -> None:
    with pytest.raises(DBAPIError) as exc:
        await coro
    assert retryable_sqlstate(exc.value) == _LOCK_NOT_AVAILABLE


# --------------------------------------------------------------------------- #
# Test A / B — shared guard coexistence and Company independence
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_same_company_monetary_writers_coexist(test_engine):
    company_id = await _make_company(test_engine)
    try:
        async with _tx(test_engine) as a, _tx(test_engine, lock_timeout_ms=3000) as b:
            await lock_company_for_monetary_use(company_id, a)
            # A is still open and holds FOR SHARE: B must get the same guard now.
            currency = await asyncio.wait_for(
                lock_and_get_company_currency_for_monetary_write(company_id, b), 5,
            )
            assert currency.code == "USD"
            holders = (await a.execute(text("""
                SELECT COUNT(*) FROM pg_locks WHERE locktype = 'relation'
                  AND relation = 'core.companies'::regclass AND pid = ANY(:pids)
            """), {"pids": [await _backend_pid(a), await _backend_pid(b)]})).scalar_one()
            assert holders >= 2
    finally:
        await _drop_company(test_engine, company_id)


@pytest.mark.asyncio
async def test_different_companies_do_not_block_each_other(test_engine):
    first = await _make_company(test_engine)
    second = await _make_company(test_engine)
    try:
        async with _tx(test_engine) as a, _tx(test_engine, lock_timeout_ms=3000) as b:
            await lock_company_for_mutation(first, a)
            currency = await asyncio.wait_for(
                lock_and_get_company_currency_for_monetary_write(second, b), 5,
            )
            assert currency.code == "USD"
    finally:
        await _drop_company(test_engine, first)
        await _drop_company(test_engine, second)


# --------------------------------------------------------------------------- #
# Test C — writer first, direct SQL currency change second (Bonus domain)
# --------------------------------------------------------------------------- #

async def _seed_bonus_graph(engine) -> dict:
    """Isolated Company/Branch/User/Driver/Period able to hold a Bonus event."""
    marker = uuid4().hex[:12]
    async with engine.begin() as db:
        cid = int((await db.execute(text("""
            INSERT INTO core.companies(companycode, companyname, currencycode)
            VALUES (:code, 'G0.3 bonus', 'USD') RETURNING companyid
        """), {"code": f"G03B_{marker}"})).scalar_one())
        bid = int((await db.execute(text("""
            INSERT INTO core.branches(companyid, branchcode, branchname, status, isdefault)
            VALUES (:cid, 'G03B', 'G0.3 branch', 'Active', FALSE) RETURNING branchid
        """), {"cid": cid})).scalar_one())
        uid = int((await db.execute(text("""
            INSERT INTO sec.users(companyid, username, displayname, passwordhash, isactive, canlogin)
            VALUES (:cid, :uname, 'G0.3 user', 'placeholder-not-for-auth', TRUE, FALSE)
            RETURNING userid
        """), {"cid": cid, "uname": f"g03_{marker}"})).scalar_one())
        eid = int((await db.execute(text("""
            INSERT INTO core.employees(companyid, branchid, fullname, employeetype)
            VALUES (:cid, :bid, 'G0.3 driver', 'Driver') RETURNING employeeid
        """), {"cid": cid, "bid": bid})).scalar_one())
        did = int((await db.execute(text("""
            INSERT INTO core.drivers(companyid, branchid, employeeid, drivercode)
            VALUES (:cid, :bid, :eid, :code) RETURNING driverid
        """), {"cid": cid, "bid": bid, "eid": eid, "code": f"G03-{marker}"})).scalar_one())
        pid = int((await db.execute(text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (:cid, :bid, 'Open', :code, 'G0.3 period', 'Week', DATE '2097-03-03', DATE '2097-03-09')
            RETURNING payrollperiodid
        """), {"cid": cid, "bid": bid, "code": f"G03P-{marker}"})).scalar_one())
    return {"company_id": cid, "branch_id": bid, "user_id": uid, "driver_id": did, "period_id": pid}


async def _drop_bonus_graph(engine, g: dict) -> None:
    async with engine.begin() as db:
        for sql in (
            "DELETE FROM payroll.payrollbonusevents WHERE companyid = :cid",
            "DELETE FROM payroll.payrollperiods WHERE companyid = :cid",
            "DELETE FROM core.drivers WHERE companyid = :cid",
            "DELETE FROM core.employees WHERE companyid = :cid",
            "DELETE FROM sec.users WHERE companyid = :cid",
            "DELETE FROM core.branches WHERE companyid = :cid",
            "DELETE FROM core.companies WHERE companyid = :cid",
        ):
            await db.execute(text(sql), {"cid": g["company_id"]})


@pytest.mark.asyncio
async def test_direct_sql_currency_change_waits_for_writer_then_is_blocked(test_engine):
    g = await _seed_bonus_graph(test_engine)
    try:
        writer_cm = _tx(test_engine)
        changer_cm = _tx(test_engine, lock_timeout_ms=20000)
        async with writer_cm as writer, changer_cm as changer:
            currency = await lock_and_get_company_currency_for_monetary_write(g["company_id"], writer)
            assert currency.code == "USD"
            await writer.execute(text("""
                INSERT INTO payroll.payrollbonusevents
                    (companyid, branchid, payrollperiodid, driverid,
                     amount, status, createdbyuserid, createdatutc, datarevision)
                VALUES (:cid, :bid, :pid, :did, 50.00, 'Active', :uid, NOW(), 1)
            """), {"cid": g["company_id"], "bid": g["branch_id"], "pid": g["period_id"],
                   "did": g["driver_id"], "uid": g["user_id"]})

            # Direct SQL, no application helper: must queue behind the FOR SHARE guard.
            changer_pid = await _backend_pid(changer)
            change = asyncio.create_task(changer.execute(
                text("UPDATE core.companies SET currencycode = 'EUR' WHERE companyid = :cid"),
                {"cid": g["company_id"]},
            ))
            try:
                await _wait_until_blocked(test_engine, changer_pid)
                assert not change.done(), "currency UPDATE must wait while the writer holds FOR SHARE"
                await writer.commit()
                with pytest.raises(DBAPIError) as exc:
                    await asyncio.wait_for(change, 10)
                assert "COMPANY_CURRENCY_CHANGE_BLOCKED" in str(exc.value)
            finally:
                if not change.done():
                    change.cancel()
                    await asyncio.gather(change, return_exceptions=True)

        async with test_engine.connect() as db:
            code = (await db.execute(
                text("SELECT currencycode FROM core.companies WHERE companyid = :cid"),
                {"cid": g["company_id"]},
            )).scalar_one()
        assert code == "USD"
    finally:
        await _drop_bonus_graph(test_engine, g)


# --------------------------------------------------------------------------- #
# Test E — Company mutation: conflicts with SHARE, no shared->exclusive upgrade
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_company_mutation_lock_conflicts_with_monetary_guard_both_ways(test_engine):
    company_id = await _make_company(test_engine)
    try:
        async with _tx(test_engine) as writer, _tx(test_engine, lock_timeout_ms=300) as mutator:
            await lock_company_for_monetary_use(company_id, writer)
            await _assert_lock_unavailable(lock_company_for_mutation(company_id, mutator))

        async with _tx(test_engine) as mutator, _tx(test_engine, lock_timeout_ms=300) as writer:
            await lock_company_for_mutation(company_id, mutator)
            await _assert_lock_unavailable(
                lock_and_get_company_currency_for_monetary_write(company_id, writer)
            )
    finally:
        await _drop_company(test_engine, company_id)


@pytest.mark.asyncio
async def test_update_company_profile_takes_mutation_lock_first_and_never_the_shared_guard(
    client, auth_token, test_engine, monkeypatch,
):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("update_company_profile must not take the shared monetary guard")

    monkeypatch.setattr(company_currency, "lock_company_for_monetary_use", forbidden)

    statements: list[str] = []

    def record(_conn, _cursor, statement, *_rest):
        flat = " ".join(statement.split()).lower()
        if "core.companies" in flat:
            statements.append(flat)

    headers = {"Authorization": f"Bearer {auth_token}"}
    current = (await client.get("/settings/company", headers=headers)).json()
    event.listen(test_engine.sync_engine, "before_cursor_execute", record)
    try:
        resp = await client.patch("/settings/company", headers=headers, json={
            "company_name": current["company_name"], "legal_name": current["legal_name"],
        })
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", record)
    assert resp.status_code == 200, resp.text

    assert not any("for share" in s for s in statements)
    first_lock = next(i for i, s in enumerate(statements) if "for no key update" in s)
    first_update = next(i for i, s in enumerate(statements) if s.startswith("update core.companies"))
    assert first_lock < first_update


@pytest.mark.asyncio
async def test_update_company_profile_waits_behind_a_monetary_writer_without_deadlock(test_engine):
    async with test_engine.connect() as db:
        user_id, company_id = (await db.execute(text(
            "SELECT userid, companyid FROM sec.users WHERE username = 'admin'"
        ))).one()
        profile = await settings_service.get_company_profile(company_id, user_id, db)

    async with _tx(test_engine) as writer, _tx(test_engine) as profile_tx:
        await lock_company_for_monetary_use(company_id, writer)
        pid = await _backend_pid(profile_tx)
        update = asyncio.create_task(settings_service.update_company_profile(
            company_id, user_id,
            CompanyUpdate(company_name=profile.company_name, legal_name=profile.legal_name),
            profile_tx,
        ))
        try:
            await _wait_until_blocked(test_engine, pid)
            assert not update.done()
            await writer.commit()
            await asyncio.wait_for(update, 10)
        finally:
            if not update.done():
                update.cancel()
                await asyncio.gather(update, return_exceptions=True)
