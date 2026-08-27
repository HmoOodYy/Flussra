"""CP-4C immutable calculation snapshot schema and hash foundation tests."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.payroll.snapshot_hash import (
    CURRENT_PAYROLL_CALCULATION_VERSION,
    calculate_snapshot_hash,
    calculate_source_config_hash,
    canonical_json,
)


_HASH_A = "a" * 64
_HASH_B = "b" * 64


@pytest_asyncio.fixture
async def snapshot_db(test_database_url) -> AsyncIterator[SimpleNamespace]:
    """Create all CP-4C test data inside one transaction and roll it back."""
    engine = create_async_engine(test_database_url, echo=False)
    async with engine.connect() as conn:
        outer = await conn.begin()
        try:
            tenant = (await conn.execute(text("""
                SELECT c.companyid, b.branchid, u.userid
                FROM core.companies c
                JOIN core.branches b ON b.companyid = c.companyid
                JOIN sec.users u ON u.companyid = c.companyid
                WHERE c.companycode = 'DEMO' AND b.branchcode = 'HQ'
                  AND u.username = 'admin'
            """))).mappings().one()
            period_id = await _insert_period(conn, tenant["companyid"], tenant["branchid"])
            driver_id = await _insert_driver(
                conn, tenant["companyid"], tenant["branchid"], tenant["userid"]
            )
            yield SimpleNamespace(
                conn=conn,
                company_id=tenant["companyid"],
                branch_id=tenant["branchid"],
                user_id=tenant["userid"],
                period_id=period_id,
                driver_id=driver_id,
            )
        finally:
            await outer.rollback()
    await engine.dispose()


async def _insert_period(
    conn: AsyncConnection, company_id: int, branch_id: int, *, status: str = "Cancelled"
) -> int:
    code = f"CP4C-{uuid4().hex}"
    return (await conn.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (:company_id, :branch_id, :status, :code, 'CP-4C test period',
                'Week', :start_date, :end_date)
        RETURNING payrollperiodid
    """), {
        "company_id": company_id,
        "branch_id": branch_id,
        "status": status,
        "code": code,
        "start_date": date(2040, 1, 1),
        "end_date": date(2040, 1, 7),
    })).scalar_one()


async def _insert_driver(
    conn: AsyncConnection, company_id: int, branch_id: int, user_id: int
) -> int:
    marker = uuid4().hex
    employee_id = (await conn.execute(text("""
        INSERT INTO core.employees
            (companyid, branchid, fullname, employeetype, employmentstatus, createdbyuserid)
        VALUES (:company_id, :branch_id, :name, 'Driver', 'Active', :user_id)
        RETURNING employeeid
    """), {
        "company_id": company_id,
        "branch_id": branch_id,
        "name": f"CP4C Employee {marker}",
        "user_id": user_id,
    })).scalar_one()
    return (await conn.execute(text("""
        INSERT INTO core.drivers (companyid, branchid, employeeid, drivercode, driverstatus)
        VALUES (:company_id, :branch_id, :employee_id, :code, 'Active')
        RETURNING driverid
    """), {
        "company_id": company_id,
        "branch_id": branch_id,
        "employee_id": employee_id,
        "code": f"CP4C-{marker}",
    })).scalar_one()


async def _insert_other_company_driver(
    conn: AsyncConnection, user_id: int
) -> tuple[int, int, int]:
    marker = uuid4().hex
    company_id = (await conn.execute(text("""
        INSERT INTO core.companies (companycode, companyname, status, issuspended, timezonename)
        VALUES (:code, :name, 'Active', FALSE, 'UTC')
        RETURNING companyid
    """), {"code": f"CP4C{marker[:18]}", "name": f"CP4C Other {marker}"})).scalar_one()
    branch_id = (await conn.execute(text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (:company_id, :code, :name, 'Active', TRUE)
        RETURNING branchid
    """), {
        "company_id": company_id,
        "code": f"C{marker[:12]}",
        "name": f"CP4C Branch {marker}",
    })).scalar_one()
    driver_id = await _insert_driver(conn, company_id, branch_id, user_id)
    return company_id, branch_id, driver_id


async def _insert_snapshot(
    db: SimpleNamespace, *, revision: int = 1, source_hash: str = _HASH_A, snapshot_hash: str = _HASH_B
) -> int:
    return (await db.conn.execute(text("""
        INSERT INTO payroll.payrollcalculationsnapshots
            (companyid, branchid, payrollperiodid, revisionnumber, calculationversion,
             sourceconfighash, snapshothash, createdbyuserid, totalexpectedpay)
        VALUES
            (:company_id, :branch_id, :period_id, :revision, :version,
             :source_hash, :snapshot_hash, :user_id, :total)
        RETURNING payrollcalculationsnapshotid
    """), {
        "company_id": db.company_id,
        "branch_id": db.branch_id,
        "period_id": db.period_id,
        "revision": revision,
        "version": CURRENT_PAYROLL_CALCULATION_VERSION,
        "source_hash": source_hash,
        "snapshot_hash": snapshot_hash,
        "user_id": db.user_id,
        "total": Decimal("123.4567"),
    })).scalar_one()


async def _insert_driver_total(
    db: SimpleNamespace,
    snapshot_id: int,
    *,
    driver_id: int | None = None,
    company_id: int | None = None,
    branch_id: int | None = None,
) -> int:
    return (await db.conn.execute(text("""
        INSERT INTO payroll.payrollcalculationdrivertotals
            (payrollcalculationsnapshotid, companyid, branchid, driverid,
             drivercodesnapshot, drivernamesnapshot, dailypay, statuspay, periodpay,
             minimumadjustment, maximumadjustment, bonustotal, expectedpay)
        VALUES
            (:snapshot_id, :company_id, :branch_id, :driver_id,
             'CP4C', 'CP4C Driver', :daily, :status, :period, :minimum, :maximum,
             :bonus, :expected)
        RETURNING payrollcalculationdrivertotalid
    """), {
        "snapshot_id": snapshot_id,
        "company_id": db.company_id if company_id is None else company_id,
        "branch_id": db.branch_id if branch_id is None else branch_id,
        "driver_id": db.driver_id if driver_id is None else driver_id,
        "daily": Decimal("100.0000"),
        "status": Decimal("10.0000"),
        "period": Decimal("5.0000"),
        "minimum": Decimal("8.0000"),
        "maximum": Decimal("-2.0000"),
        "bonus": Decimal("3.0000"),
        "expected": Decimal("124.0000"),
    })).scalar_one()


async def _insert_line(
    db: SimpleNamespace,
    driver_total_id: int,
    *,
    line_type: str = "HOURS",
    source_type: str = "Manual",
    source_id: str | None = None,
    bonus_event_id: int | None = None,
) -> int:
    return (await db.conn.execute(text("""
        INSERT INTO payroll.payrollcalculationsnapshotlines
            (payrollcalculationdrivertotalid, sourcetype, sourceid, linetype, linescope,
             workdate, payitemid, ratetypeid, driverrateid, bonuseventid, quantity,
             resolvedrateamount, calculatedamount, sourceevidencejsonb)
        VALUES
            (:driver_total_id, :source_type, :source_id, :line_type, 'Daily',
             :work_date, 101, 102, 103, :bonus_event_id, :quantity, :rate, :amount,
             CAST(:evidence AS jsonb))
        RETURNING payrollcalculationsnapshotlineid
    """), {
        "driver_total_id": driver_total_id,
        "source_type": source_type,
        "source_id": source_id,
        "line_type": line_type,
        "work_date": date(2040, 1, 2),
        "bonus_event_id": bonus_event_id,
        "quantity": Decimal("1.2345"),
        "rate": Decimal("100.0000"),
        "amount": Decimal("123.4500"),
        "evidence": json.dumps({"source": "CP4C", "nested": {"status_key_id": 7}}),
    })).scalar_one()


async def _expect_integrity_error(conn: AsyncConnection, statement, params: dict) -> IntegrityError:
    savepoint = await conn.begin_nested()
    try:
        with pytest.raises(IntegrityError) as captured:
            await conn.execute(statement, params)
        return captured.value
    finally:
        if savepoint.is_active:
            await savepoint.rollback()


def _load_migration_module():
    path = Path(__file__).parents[2] / "migrations" / "versions" / "0061_cp4c_calculation_snapshots.py"
    spec = importlib.util.spec_from_file_location("cp4c_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_snapshot_tables_columns_and_numeric_contract_exist(snapshot_db):
    rows = (await snapshot_db.conn.execute(text("""
        SELECT table_name, column_name, data_type, numeric_precision, numeric_scale, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'payroll'
          AND table_name IN (
              'payrollcalculationsnapshots',
              'payrollcalculationdrivertotals',
              'payrollcalculationsnapshotlines'
          )
    """))).mappings().all()
    columns = {(row["table_name"], row["column_name"]): row for row in rows}
    assert ("payrollcalculationsnapshots", "payrollcalculationsnapshotid") in columns
    assert ("payrollcalculationdrivertotals", "companyid") in columns
    assert ("payrollcalculationdrivertotals", "branchid") in columns
    assert ("payrollcalculationsnapshotlines", "payrollcalculationsnapshotid") not in columns
    assert ("payrollcalculationsnapshotlines", "companyid") not in columns
    assert ("payrollcalculationsnapshotlines", "branchid") not in columns
    for table, column in (
        ("payrollcalculationsnapshots", "totalexpectedpay"),
        ("payrollcalculationdrivertotals", "dailypay"),
        ("payrollcalculationsnapshotlines", "quantity"),
        ("payrollcalculationsnapshotlines", "resolvedrateamount"),
        ("payrollcalculationsnapshotlines", "calculatedamount"),
    ):
        assert columns[(table, column)]["data_type"] == "numeric"
        assert columns[(table, column)]["numeric_precision"] == 18
        assert columns[(table, column)]["numeric_scale"] == 4


async def test_snapshot_constraints_and_indexes_exist(snapshot_db):
    constraints = (await snapshot_db.conn.execute(text("""
        SELECT conname
        FROM pg_constraint
        WHERE connamespace = 'payroll'::regnamespace
          AND conname IN (
              'uq_payrollcalculationsnapshots_period_revision',
              'uq_payrollcalculationsnapshots_id_company_branch',
              'uq_payrollcalculationdrivertotals_snapshot_driver',
              'fk_payrollcalculationdrivertotals_snapshot_company_branch',
              'fk_payrollcalculationdrivertotals_driver_company_branch',
              'fk_payrollcalculationsnapshotlines_drivertotal'
          )
    """))).scalars().all()
    assert len(constraints) == 6
    indexes = (await snapshot_db.conn.execute(text("""
        SELECT indexname FROM pg_indexes
        WHERE schemaname = 'payroll'
          AND indexname IN (
              'ix_payrollcalculationsnapshots_company_branch_period_revision',
              'ix_payrollcalculationsnapshotlines_drivertotal',
              'ix_payrollcalculationsnapshotlines_sourceidentity'
          )
    """))).scalars().all()
    assert len(indexes) == 3


@pytest.mark.parametrize("revision", [0, -1])
async def test_snapshot_revision_must_be_positive(snapshot_db, revision):
    error = await _expect_integrity_error(snapshot_db.conn, text("""
        INSERT INTO payroll.payrollcalculationsnapshots
            (companyid, branchid, payrollperiodid, revisionnumber, calculationversion,
             sourceconfighash, snapshothash, createdbyuserid, totalexpectedpay)
        VALUES (:company_id, :branch_id, :period_id, :revision, :version,
                :source_hash, :snapshot_hash, :user_id, 0)
    """), {
        "company_id": snapshot_db.company_id,
        "branch_id": snapshot_db.branch_id,
        "period_id": snapshot_db.period_id,
        "revision": revision,
        "version": CURRENT_PAYROLL_CALCULATION_VERSION,
        "source_hash": _HASH_A,
        "snapshot_hash": _HASH_B,
        "user_id": snapshot_db.user_id,
    })
    assert "revisionpositive" in str(error).lower()


async def test_snapshot_revisions_are_unique_per_period_and_incrementable(snapshot_db):
    first = await _insert_snapshot(snapshot_db, revision=1)
    second = await _insert_snapshot(snapshot_db, revision=2)
    assert second != first
    await _expect_integrity_error(snapshot_db.conn, text("""
        INSERT INTO payroll.payrollcalculationsnapshots
            (companyid, branchid, payrollperiodid, revisionnumber, calculationversion,
             sourceconfighash, snapshothash, createdbyuserid, totalexpectedpay)
        VALUES (:company_id, :branch_id, :period_id, 1, :version,
                :source_hash, :snapshot_hash, :user_id, 0)
    """), {
        "company_id": snapshot_db.company_id,
        "branch_id": snapshot_db.branch_id,
        "period_id": snapshot_db.period_id,
        "version": CURRENT_PAYROLL_CALCULATION_VERSION,
        "source_hash": _HASH_A,
        "snapshot_hash": _HASH_B,
        "user_id": snapshot_db.user_id,
    })


@pytest.mark.parametrize("hash_column", ["sourceconfighash", "snapshothash"])
@pytest.mark.parametrize("bad_hash", ["a" * 63, "A" * 64, "g" * 64])
async def test_snapshot_hash_columns_require_lowercase_sha256_hex(snapshot_db, hash_column, bad_hash):
    error = await _expect_integrity_error(snapshot_db.conn, text("""
        INSERT INTO payroll.payrollcalculationsnapshots
            (companyid, branchid, payrollperiodid, revisionnumber, calculationversion,
             sourceconfighash, snapshothash, createdbyuserid, totalexpectedpay)
        VALUES (:company_id, :branch_id, :period_id, 1, :version,
                :source_hash, :snapshot_hash, :user_id, 0)
    """), {
        "company_id": snapshot_db.company_id,
        "branch_id": snapshot_db.branch_id,
        "period_id": snapshot_db.period_id,
        "version": CURRENT_PAYROLL_CALCULATION_VERSION,
        "source_hash": bad_hash if hash_column == "sourceconfighash" else _HASH_A,
        "snapshot_hash": bad_hash if hash_column == "snapshothash" else _HASH_B,
        "user_id": snapshot_db.user_id,
    })
    assert "hashhex" in str(error).lower()


async def test_driver_total_requires_matching_snapshot_and_driver_tenant(snapshot_db):
    snapshot_id = await _insert_snapshot(snapshot_db)
    total_id = await _insert_driver_total(snapshot_db, snapshot_id)
    assert total_id > 0

    _, _, other_driver_id = await _insert_other_company_driver(snapshot_db.conn, snapshot_db.user_id)
    await _expect_integrity_error(snapshot_db.conn, text("""
        INSERT INTO payroll.payrollcalculationdrivertotals
            (payrollcalculationsnapshotid, companyid, branchid, driverid,
             dailypay, statuspay, periodpay, minimumadjustment, maximumadjustment,
             bonustotal, expectedpay)
        VALUES (:snapshot_id, :company_id, :branch_id, :driver_id,
                0, 0, 0, 0, 0, 0, 0)
    """), {
        "snapshot_id": snapshot_id,
        "company_id": snapshot_db.company_id,
        "branch_id": snapshot_db.branch_id,
        "driver_id": other_driver_id,
    })

    other_branch = (await snapshot_db.conn.execute(text("""
        SELECT branchid FROM core.branches
        WHERE companyid = :company_id AND branchcode = 'PAYTEST'
    """), {"company_id": snapshot_db.company_id})).scalar_one()
    other_branch_driver = await _insert_driver(
        snapshot_db.conn, snapshot_db.company_id, other_branch, snapshot_db.user_id
    )
    await _expect_integrity_error(snapshot_db.conn, text("""
        INSERT INTO payroll.payrollcalculationdrivertotals
            (payrollcalculationsnapshotid, companyid, branchid, driverid,
             dailypay, statuspay, periodpay, minimumadjustment, maximumadjustment,
             bonustotal, expectedpay)
        VALUES (:snapshot_id, :company_id, :branch_id, :driver_id,
                0, 0, 0, 0, 0, 0, 0)
    """), {
        "snapshot_id": snapshot_id,
        "company_id": snapshot_db.company_id,
        "branch_id": snapshot_db.branch_id,
        "driver_id": other_branch_driver,
    })


async def test_driver_total_is_unique_per_snapshot_and_driver(snapshot_db):
    snapshot_id = await _insert_snapshot(snapshot_db)
    await _insert_driver_total(snapshot_db, snapshot_id)
    await _expect_integrity_error(snapshot_db.conn, text("""
        INSERT INTO payroll.payrollcalculationdrivertotals
            (payrollcalculationsnapshotid, companyid, branchid, driverid,
             dailypay, statuspay, periodpay, minimumadjustment, maximumadjustment,
             bonustotal, expectedpay)
        VALUES (:snapshot_id, :company_id, :branch_id, :driver_id,
                0, 0, 0, 0, 0, 0, 0)
    """), {
        "snapshot_id": snapshot_id,
        "company_id": snapshot_db.company_id,
        "branch_id": snapshot_db.branch_id,
        "driver_id": snapshot_db.driver_id,
    })


async def test_snapshot_lines_inherit_ownership_and_preserve_evidence_and_decimals(snapshot_db):
    snapshot_id = await _insert_snapshot(snapshot_db)
    total_id = await _insert_driver_total(snapshot_db, snapshot_id)
    line_id = await _insert_line(snapshot_db, total_id, bonus_event_id=999999999)
    row = (await snapshot_db.conn.execute(text("""
        SELECT calculatedamount, quantity, resolvedrateamount, sourceevidencejsonb::text
        FROM payroll.payrollcalculationsnapshotlines
        WHERE payrollcalculationsnapshotlineid = :line_id
    """), {"line_id": line_id})).mappings().one()
    assert row["calculatedamount"] == Decimal("123.4500")
    assert row["quantity"] == Decimal("1.2345")
    assert row["resolvedrateamount"] == Decimal("100.0000")
    assert json.loads(row["sourceevidencejsonb"]) == {
        "nested": {"status_key_id": 7}, "source": "CP4C"
    }
    total = (await snapshot_db.conn.execute(text("""
        SELECT minimumadjustment, maximumadjustment
        FROM payroll.payrollcalculationdrivertotals
        WHERE payrollcalculationdrivertotalid = :total_id
    """), {"total_id": total_id})).mappings().one()
    assert total["minimumadjustment"] == Decimal("8.0000")
    assert total["maximumadjustment"] == Decimal("-2.0000")


@pytest.mark.parametrize("line_type", ["SYS_MIN_TOPUP", "SYS_MAX_CAP"])
async def test_system_adjustment_lines_are_representable(snapshot_db, line_type):
    snapshot_id = await _insert_snapshot(snapshot_db)
    total_id = await _insert_driver_total(snapshot_db, snapshot_id)
    line_id = await _insert_line(snapshot_db, total_id, line_type=line_type, source_type="System")
    stored = (await snapshot_db.conn.execute(text("""
        SELECT linetype FROM payroll.payrollcalculationsnapshotlines
        WHERE payrollcalculationsnapshotlineid = :line_id
    """), {"line_id": line_id})).scalar_one()
    assert stored == line_type


@pytest.mark.parametrize(
    ("table", "id_column", "mutation"),
    [
        ("payroll.payrollcalculationsnapshots", "payrollcalculationsnapshotid", "UPDATE {table} SET totalexpectedpay = 0 WHERE {id_column} = :id"),
        ("payroll.payrollcalculationsnapshots", "payrollcalculationsnapshotid", "DELETE FROM {table} WHERE {id_column} = :id"),
        ("payroll.payrollcalculationdrivertotals", "payrollcalculationdrivertotalid", "UPDATE {table} SET expectedpay = 0 WHERE {id_column} = :id"),
        ("payroll.payrollcalculationdrivertotals", "payrollcalculationdrivertotalid", "DELETE FROM {table} WHERE {id_column} = :id"),
        ("payroll.payrollcalculationsnapshotlines", "payrollcalculationsnapshotlineid", "UPDATE {table} SET calculatedamount = 0 WHERE {id_column} = :id"),
        ("payroll.payrollcalculationsnapshotlines", "payrollcalculationsnapshotlineid", "DELETE FROM {table} WHERE {id_column} = :id"),
    ],
)
async def test_snapshot_rows_reject_updates_and_deletes(snapshot_db, table, id_column, mutation):
    snapshot_id = await _insert_snapshot(snapshot_db)
    total_id = await _insert_driver_total(snapshot_db, snapshot_id)
    line_id = await _insert_line(snapshot_db, total_id)
    row_id = {
        "payrollcalculationsnapshotid": snapshot_id,
        "payrollcalculationdrivertotalid": total_id,
        "payrollcalculationsnapshotlineid": line_id,
    }[id_column]
    error = await _expect_integrity_error(
        snapshot_db.conn,
        text(mutation.format(table=table, id_column=id_column)),
        {"id": row_id},
    )
    assert "payroll_calculation_snapshot_immutable" in str(error).lower()


async def test_parent_period_delete_is_restricted_and_rollback_removes_snapshot_rows(snapshot_db):
    snapshot_id = await _insert_snapshot(snapshot_db)
    total_id = await _insert_driver_total(snapshot_db, snapshot_id)
    await _insert_line(snapshot_db, total_id)
    await _expect_integrity_error(snapshot_db.conn, text("""
        DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :period_id
    """), {"period_id": snapshot_db.period_id})
    count = (await snapshot_db.conn.execute(text("""
        SELECT COUNT(*) FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid = :snapshot_id
    """), {"snapshot_id": snapshot_id})).scalar_one()
    assert count == 1


async def test_outer_transaction_rollback_removes_immutable_snapshot_rows(test_database_url):
    """Rollback, not DELETE or trigger bypass, is the isolation mechanism."""
    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            try:
                tenant = (await conn.execute(text("""
                    SELECT c.companyid, b.branchid, u.userid
                    FROM core.companies c
                    JOIN core.branches b ON b.companyid = c.companyid
                    JOIN sec.users u ON u.companyid = c.companyid
                    WHERE c.companycode = 'DEMO' AND b.branchcode = 'HQ' AND u.username = 'admin'
                """))).mappings().one()
                period_id = await _insert_period(conn, tenant["companyid"], tenant["branchid"])
                db = SimpleNamespace(
                    conn=conn,
                    company_id=tenant["companyid"],
                    branch_id=tenant["branchid"],
                    user_id=tenant["userid"],
                    period_id=period_id,
                )
                snapshot_id = await _insert_snapshot(db)
            finally:
                if outer.is_active:
                    await outer.rollback()

        async with engine.connect() as verification_conn:
            count = (await verification_conn.execute(text("""
                SELECT COUNT(*) FROM payroll.payrollcalculationsnapshots
                WHERE payrollcalculationsnapshotid = :snapshot_id
            """), {"snapshot_id": snapshot_id})).scalar_one()
    finally:
        await engine.dispose()
    assert count == 0


async def test_existing_periods_receive_no_snapshot_backfill(snapshot_db):
    in_review_id = await _insert_period(
        snapshot_db.conn, snapshot_db.company_id, snapshot_db.branch_id, status="InReview"
    )
    approved_id = await _insert_period(
        snapshot_db.conn, snapshot_db.company_id, snapshot_db.branch_id, status="Approved"
    )
    count = (await snapshot_db.conn.execute(text("""
        SELECT COUNT(*) FROM payroll.payrollcalculationsnapshots
        WHERE payrollperiodid IN (:in_review_id, :approved_id)
    """), {"in_review_id": in_review_id, "approved_id": approved_id})).scalar_one()
    assert count == 0
    pointers = (await snapshot_db.conn.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'payroll' AND table_name = 'payrollperiods'
          AND column_name ILIKE '%calculationsnapshot%'
    """))).scalars().all()
    assert pointers == []


async def test_downgrade_refuses_when_immutable_snapshot_rows_exist(snapshot_db):
    await _insert_snapshot(snapshot_db)
    migration = _load_migration_module()
    with pytest.raises(RuntimeError, match="Downgrade of 0061 refused"):
        await snapshot_db.conn.run_sync(migration._assert_snapshot_tables_empty)


def _line(**overrides):
    line = {
        "SourceType": "Manual",
        "SourceID": None,
        "LineType": "ADJUSTMENT",
        "LineScope": "Period",
        "WorkDate": None,
        "PayItemID": 7,
        "RateTypeID": None,
        "DriverRateID": None,
        "BonusEventID": None,
        "Quantity": Decimal("1.0000"),
        "ResolvedRateAmount": None,
        "CalculatedAmount": Decimal("5.0000"),
        "SourceEvidenceJSONB": {"manual": True},
    }
    line.update(overrides)
    return line


def _total(driver_id: int, *lines):
    return {
        "DriverID": driver_id,
        "DriverCodeSnapshot": f"D{driver_id}",
        "DriverNameSnapshot": f"Driver {driver_id}",
        "DailyPay": Decimal("1.0000"),
        "StatusPay": Decimal("2.0000"),
        "PeriodPay": Decimal("3.0000"),
        "MinimumAdjustment": Decimal("0"),
        "MaximumAdjustment": Decimal("0"),
        "BonusTotal": Decimal("0"),
        "ExpectedPay": Decimal("6.0000"),
        "Lines": list(lines),
    }


def test_canonical_decimal_datetime_dict_and_unicode_contract():
    assert CURRENT_PAYROLL_CALCULATION_VERSION == "current-payroll-v1"
    assert canonical_json({"b": Decimal("-0.0000"), "a": Decimal("12.3400")}) == '{"a":"12.34","b":"0"}'
    assert canonical_json({"when": datetime(2040, 1, 1, 7, tzinfo=timezone(timedelta(hours=2)))}) == '{"when":"2040-01-01T05:00:00.000000Z"}'
    assert canonical_json({"text": "cafe\u0301"}) == '{"text":"café"}'


@pytest.mark.parametrize("value", [1.0, {"nested": 1.0}])
def test_canonical_serialization_rejects_floats(value):
    with pytest.raises(TypeError, match="float"):
        canonical_json(value)


def test_canonical_serialization_rejects_naive_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        canonical_json(datetime(2040, 1, 1))


def test_hashes_are_deterministic_and_change_with_semantic_values():
    source = {"rate": Decimal("10.0000"), "date": date(2040, 1, 1)}
    assert calculate_source_config_hash(source) == calculate_source_config_hash(dict(reversed(list(source.items()))))
    assert calculate_source_config_hash(source) != calculate_source_config_hash({**source, "rate": Decimal("11")})

    base = dict(
        company_id=1,
        branch_id=2,
        payroll_period_id=3,
        revision_number=1,
        calculation_version=CURRENT_PAYROLL_CALCULATION_VERSION,
        source_config_hash=_HASH_A,
        driver_totals=[_total(2, _line()), _total(1, _line(SourceID="same"))],
    )
    first = calculate_snapshot_hash(**base)
    reordered = calculate_snapshot_hash(**{**base, "driver_totals": list(reversed(base["driver_totals"]))})
    changed = calculate_snapshot_hash(**{**base, "driver_totals": [_total(2, _line(CalculatedAmount=Decimal("6"))), _total(1, _line(SourceID="same"))]})
    assert first == reordered
    assert first != changed


def test_snapshot_hash_sorts_null_source_ids_and_preserves_identical_line_multiplicity():
    first = calculate_snapshot_hash(
        company_id=1, branch_id=1, payroll_period_id=1, revision_number=1,
        calculation_version=CURRENT_PAYROLL_CALCULATION_VERSION, source_config_hash=_HASH_A,
        driver_totals=[_total(1, _line(), _line(CalculatedAmount=Decimal("7")))],
    )
    reordered = calculate_snapshot_hash(
        company_id=1, branch_id=1, payroll_period_id=1, revision_number=1,
        calculation_version=CURRENT_PAYROLL_CALCULATION_VERSION, source_config_hash=_HASH_A,
        driver_totals=[_total(1, _line(CalculatedAmount=Decimal("7")), _line())],
    )
    one_line = calculate_snapshot_hash(
        company_id=1, branch_id=1, payroll_period_id=1, revision_number=1,
        calculation_version=CURRENT_PAYROLL_CALCULATION_VERSION, source_config_hash=_HASH_A,
        driver_totals=[_total(1, _line())],
    )
    duplicate_lines = calculate_snapshot_hash(
        company_id=1, branch_id=1, payroll_period_id=1, revision_number=1,
        calculation_version=CURRENT_PAYROLL_CALCULATION_VERSION, source_config_hash=_HASH_A,
        driver_totals=[_total(1, _line(), _line())],
    )
    assert first == reordered
    assert one_line != duplicate_lines


def test_snapshot_hash_excludes_generated_snapshot_surrogate_ids():
    total = _total(1, _line())
    hash_without_ids = calculate_snapshot_hash(
        company_id=1, branch_id=1, payroll_period_id=1, revision_number=1,
        calculation_version=CURRENT_PAYROLL_CALCULATION_VERSION, source_config_hash=_HASH_A,
        driver_totals=[total],
    )
    total_with_ids = {
        **total,
        "PayrollCalculationDriverTotalID": 999,
        "Lines": [{**total["Lines"][0], "PayrollCalculationSnapshotLineID": 123}],
    }
    hash_with_ids = calculate_snapshot_hash(
        company_id=1, branch_id=1, payroll_period_id=1, revision_number=1,
        calculation_version=CURRENT_PAYROLL_CALCULATION_VERSION, source_config_hash=_HASH_A,
        driver_totals=[total_with_ids],
    )
    assert hash_without_ids == hash_with_ids
