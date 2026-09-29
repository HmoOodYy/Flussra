"""Focused P1a coverage for Driver identity and effective-date integrity."""

import asyncio
from datetime import date, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.payroll.eligibility import _assert_driver_eligible_for_date
from app.workforce.effective import (
    resolve_current_driver_profile,
    resolve_effective_driver_profile,
)


async def _workforce_rows(db, timezone: str = "UTC") -> tuple[int, int, int]:
    suffix = uuid4().hex[:12]
    company_id = (await db.execute(text("""
        INSERT INTO core.companies
            (companycode, companyname, legalname, status, issuspended, timezonename)
        VALUES (:code, :name, :legal, 'Active', FALSE, :timezone)
        RETURNING companyid
    """), {"code": f"P1A-{suffix}", "name": f"P1A {suffix}",
           "legal": f"P1A {suffix} Ltd", "timezone": timezone})).scalar_one()
    branch_id = (await db.execute(text("""
        INSERT INTO core.branches (companyid, branchcode, branchname)
        VALUES (:cid, :code, :name) RETURNING branchid
    """), {"cid": company_id, "code": f"B-{suffix}", "name": f"Branch {suffix}"})).scalar_one()
    employee_id = (await db.execute(text("""
        INSERT INTO core.employees
            (companyid, branchid, employeekey, fullname, employeetype, employmentstatus)
        VALUES (:cid, :bid, :key, 'P1a Test', 'Driver', 'Active')
        RETURNING employeeid
    """), {"cid": company_id, "bid": branch_id, "key": f"E-{suffix}"})).scalar_one()
    return company_id, branch_id, employee_id


async def _driver(db, company_id: int, branch_id: int, employee_id: int,
                  start: date | None, end: date | None, status: str = "Active") -> int:
    return (await db.execute(text("""
        INSERT INTO core.drivers
            (companyid, branchid, employeeid, drivercode, driverstatus, effectivefrom, effectiveto)
        VALUES (:cid, :bid, :eid, :code, :status, :start, :end)
        RETURNING driverid
    """), {"cid": company_id, "bid": branch_id, "eid": employee_id,
           "code": f"D-{uuid4().hex[:12]}", "status": status,
           "start": start, "end": end})).scalar_one()


@pytest.mark.asyncio
async def test_same_company_identity_and_immutable_branch(direct_db):
    company_id, branch_id, employee_id = await _workforce_rows(direct_db)
    other_branch_id = (await direct_db.execute(text("""
        INSERT INTO core.branches (companyid, branchcode, branchname)
        VALUES (:cid, :code, :name) RETURNING branchid
    """), {"cid": company_id, "code": f"B-{uuid4().hex[:8]}",
           "name": f"Branch {uuid4().hex[:8]}"})).scalar_one()
    driver_id = await _driver(direct_db, company_id, branch_id, employee_id, None, None)

    with pytest.raises(DBAPIError):
        await direct_db.execute(
            text("UPDATE core.drivers SET branchid=:branch_id WHERE driverid=:did"),
            {"branch_id": other_branch_id, "did": driver_id},
        )


@pytest.mark.asyncio
async def test_driver_employee_and_branch_company_mismatches_are_rejected(direct_db):
    company_id, branch_id, employee_id = await _workforce_rows(direct_db)
    other_company, other_branch, _ = await _workforce_rows(direct_db)

    with pytest.raises(DBAPIError):
        await _driver(direct_db, other_company, other_branch, employee_id, None, None)
    with pytest.raises(DBAPIError):
        await _driver(direct_db, company_id, other_branch, employee_id, None, None)


@pytest.mark.asyncio
async def test_adjacent_windows_and_historical_pending_resolution(direct_db):
    company_id, branch_id, employee_id = await _workforce_rows(direct_db)
    source_id = await _driver(
        direct_db, company_id, branch_id, employee_id,
        date(2025, 1, 1), date(2025, 6, 30), "Transferred",
    )
    destination_id = await _driver(
        direct_db, company_id, branch_id, employee_id,
        date(2025, 7, 1), None,
    )

    assert (await resolve_effective_driver_profile(
        company_id, employee_id, date(2025, 6, 30), direct_db
    ))["driverid"] == source_id
    assert await resolve_effective_driver_profile(
        company_id, employee_id, date(2024, 12, 31), direct_db
    ) is None
    assert (await resolve_effective_driver_profile(
        company_id, employee_id, date(2025, 6, 30), direct_db
    ))["driverid"] != destination_id
    assert (await resolve_effective_driver_profile(
        company_id, employee_id, date(2025, 7, 1), direct_db
    ))["driverid"] == destination_id
    await _assert_driver_eligible_for_date(
        company_id, source_id, branch_id, date(2025, 6, 30), direct_db
    )
    with pytest.raises(HTTPException):
        await _assert_driver_eligible_for_date(
            company_id, source_id, branch_id, date(2025, 7, 1), direct_db
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["Inactive", "OnLeave"])
async def test_effective_noneligible_driver_status_is_rejected_for_workdate(direct_db, status):
    company_id, branch_id, employee_id = await _workforce_rows(direct_db)
    driver_id = await _driver(
        direct_db, company_id, branch_id, employee_id,
        date(2025, 1, 1), None, status,
    )

    with pytest.raises(HTTPException):
        await _assert_driver_eligible_for_date(
            company_id, driver_id, branch_id, date(2025, 6, 1), direct_db
        )


@pytest.mark.asyncio
async def test_overlapping_effective_windows_are_rejected(direct_db):
    company_id, branch_id, employee_id = await _workforce_rows(direct_db)
    await _driver(direct_db, company_id, branch_id, employee_id,
                  date(2025, 1, 1), date(2025, 7, 1))
    with pytest.raises(DBAPIError):
        await _driver(direct_db, company_id, branch_id, employee_id,
                      date(2025, 7, 1), None)


@pytest.mark.asyncio
async def test_invalid_effective_window_and_nonterminated_sentinel_are_rejected(direct_db):
    company_id, branch_id, employee_id = await _workforce_rows(direct_db)
    with pytest.raises(DBAPIError):
        await _driver(direct_db, company_id, branch_id, employee_id,
                      date(2025, 7, 2), date(2025, 7, 1), "Active")
    with pytest.raises(DBAPIError):
        await _driver(direct_db, company_id, branch_id, employee_id,
                      date(2025, 7, 2), date(2025, 7, 1), "Transferred")


@pytest.mark.asyncio
async def test_never_effective_terminated_profile_is_empty(direct_db):
    company_id, branch_id, employee_id = await _workforce_rows(direct_db)
    effective_from = date(2025, 7, 1)
    driver_id = await _driver(
        direct_db, company_id, branch_id, employee_id,
        effective_from, effective_from - timedelta(days=1), "Terminated",
    )
    assert (await direct_db.execute(text("""
        SELECT isempty(core.fn_DriverEffectiveRange(:start, :end))
    """), {"start": effective_from, "end": effective_from - timedelta(days=1)})).scalar_one()
    assert await resolve_effective_driver_profile(
        company_id, employee_id, effective_from, direct_db
    ) is None
    assert driver_id is not None


@pytest.mark.asyncio
async def test_closed_history_rejects_status_window_and_lineage_mutation(direct_db):
    company_id, branch_id, employee_id = await _workforce_rows(direct_db)
    driver_id = await _driver(direct_db, company_id, branch_id, employee_id,
                              date(2025, 1, 1), date(2025, 6, 30), "Transferred")
    await direct_db.execute(text("UPDATE core.drivers SET cdlnumber='LEGACY-EDIT' WHERE driverid=:did"),
                            {"did": driver_id})
    for update in (
        "UPDATE core.drivers SET driverstatus='Active' WHERE driverid=:did",
        "UPDATE core.drivers SET effectiveto='2025-07-01' WHERE driverid=:did",
        "UPDATE core.drivers SET transferredtodriverid=driverid WHERE driverid=:did",
    ):
        with pytest.raises(DBAPIError):
            await direct_db.execute(text(update), {"did": driver_id})


@pytest.mark.asyncio
async def test_terminate_operation_is_narrow_and_preserves_identity_and_lineage(
    direct_db, test_database_url
):
    company_id, branch_id, employee_id = await _workforce_rows(direct_db)
    source_id = await _driver(direct_db, company_id, branch_id, employee_id,
                              date(2025, 1, 1), date(2025, 6, 30), "Transferred")
    other_branch_id = (await direct_db.execute(text("""
        INSERT INTO core.branches (companyid, branchcode, branchname)
        VALUES (:cid, :code, :name) RETURNING branchid
    """), {"cid": company_id, "code": f"B-{uuid4().hex[:8]}",
           "name": f"Branch {uuid4().hex[:8]}"})).scalar_one()
    engine = create_async_engine(test_database_url)
    try:
        for operation in (None, "custom_operation"):
            async with engine.connect() as db:
                tx = await db.begin()
                if operation is not None:
                    await db.execute(text(
                        "SELECT set_config('flussra.workforce_op', :operation, true)"
                    ), {"operation": operation})
                with pytest.raises(DBAPIError):
                    await db.execute(text("""
                        UPDATE core.drivers
                        SET driverstatus='Terminated', effectiveto='2025-06-01'
                        WHERE driverid=:did
                    """), {"did": source_id})
                await tx.rollback()

        async with engine.begin() as db:
            await db.execute(text("""
                UPDATE core.employees
                SET employmentstatus='Terminated', terminationdate='2025-06-01'
                WHERE employeeid=:eid
            """), {"eid": employee_id})
            await db.execute(text(
                "SELECT set_config('flussra.workforce_op', 'terminate', true)"
            ))
            await db.execute(text("""
                UPDATE core.drivers
                SET driverstatus='Terminated', effectiveto='2025-06-01'
                WHERE driverid=:did
            """), {"did": source_id})

        for mutation, parameters in (
            ("UPDATE core.drivers SET branchid=:branch WHERE driverid=:did",
             {"branch": other_branch_id, "did": source_id}),
            ("UPDATE core.drivers SET employeeid=:employee WHERE driverid=:did",
             {"employee": employee_id + 100000, "did": source_id}),
            ("UPDATE core.drivers SET companyid=:company WHERE driverid=:did",
             {"company": company_id + 100000, "did": source_id}),
            ("UPDATE core.drivers SET effectivefrom='2025-01-02' WHERE driverid=:did",
             {"did": source_id}),
            ("UPDATE core.drivers SET transferredtodriverid=driverid WHERE driverid=:did",
             {"did": source_id}),
        ):
            async with engine.connect() as db:
                tx = await db.begin()
                await db.execute(text(
                    "SELECT set_config('flussra.workforce_op', 'terminate', true)"
                ))
                with pytest.raises(DBAPIError):
                    await db.execute(text(mutation), parameters)
                await tx.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_deferred_overlap_still_makes_resolver_fail_closed(test_database_url):
    engine = create_async_engine(test_database_url)
    try:
        async with engine.begin() as db:
            company_id, branch_id, employee_id = await _workforce_rows(db)
            await _driver(db, company_id, branch_id, employee_id,
                          date(2025, 1, 1), date(2025, 12, 31))
            await _driver(db, company_id, branch_id, employee_id,
                          date(2025, 6, 1), date(2025, 12, 31), "Transferred")
            with pytest.raises(DBAPIError, match="Multiple effective Driver profiles"):
                await resolve_effective_driver_profile(
                    company_id, employee_id, date(2025, 7, 1), db
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_overlapping_concurrent_transactions_cannot_both_commit(test_database_url):
    engine = create_async_engine(test_database_url)
    try:
        async with engine.connect() as setup:
            company_id, branch_id, employee_id = await _workforce_rows(setup)
            await setup.commit()

        async with engine.connect() as first, engine.connect() as second:
            tx1 = await first.begin()
            tx2 = await second.begin()
            await _driver(first, company_id, branch_id, employee_id,
                          date(2025, 1, 1), date(2025, 12, 31))
            pending_insert = asyncio.create_task(_driver(
                second, company_id, branch_id, employee_id,
                date(2025, 6, 1), date(2025, 12, 31), "Transferred",
            ))
            await asyncio.sleep(0.1)
            await tx1.commit()
            try:
                await pending_insert
                with pytest.raises(DBAPIError):
                    await tx2.commit()
            except DBAPIError:
                await tx2.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_current_profile_uses_company_business_date(direct_db):
    company_id, branch_id, employee_id = await _workforce_rows(direct_db, "Pacific/Auckland")
    driver_id = await _driver(direct_db, company_id, branch_id, employee_id,
                              date(2000, 1, 1), None)
    database_today = (await direct_db.execute(text("SELECT core.fn_CompanyToday(:cid)"),
                                               {"cid": company_id})).scalar_one()
    expected_company_date = (await direct_db.execute(text("""
        SELECT (NOW() AT TIME ZONE TimeZoneName)::date
        FROM core.companies WHERE companyid=:cid
    """), {"cid": company_id})).scalar_one()
    resolved = await resolve_current_driver_profile(company_id, employee_id, direct_db)
    assert database_today == expected_company_date
    assert resolved["driverid"] == driver_id


@pytest.mark.asyncio
async def test_core_driver_patch_is_driver_owned_and_rejects_workforce_fields(
    client, auth_token, created_driver_id, direct_db
):
    headers = {"Authorization": f"Bearer {auth_token}"}
    ordinary = await client.patch(
        f"/core/drivers/{created_driver_id}",
        json={"cdl_number": "P1A-LEGACY-EDIT"},
        headers=headers,
    )
    assert ordinary.status_code == 200
    assert ordinary.json()["cdl_number"] == "P1A-LEGACY-EDIT"

    for payload in (
        {"driver_status": "Transferred"},
        {"driver_status": "Terminated"},
        {"employment_status": "Inactive"},
        {"termination_date": "2026-01-01"},
        {"full_name": "Employee-owned field"},
    ):
        response = await client.patch(
            f"/core/drivers/{created_driver_id}", json=payload, headers=headers
        )
        assert response.status_code == 422
