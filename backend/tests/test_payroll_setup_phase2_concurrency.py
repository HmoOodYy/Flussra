"""Real PostgreSQL concurrency checks for Phase 2 Payroll Setup policy writes."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg2
import pytest
import pytest_asyncio
from psycopg2 import sql as pg_sql
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.payroll.period_creation import create_period_from_candidate, get_period_candidates
from app.payroll.schemas import PeriodCreationRequest
from app.payroll.workflow_lock import _acquire_branch_workflow_lock
from app.payroll_setup.errors import PolicyError
from app.payroll_setup.locks import lock_branches
from app.payroll_setup.policy import (
    assign_setup,
    create_draft,
    create_setup,
    publish_version,
    reassign_setup,
)


_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS = _ROOT / "migrations" / "sql"
_ANCHOR = date(2090, 1, 1)


@pytest_asyncio.fixture
async def isolated_policy_database(pg_instance):
    """Create, migrate, seed, and destroy a database dedicated to this test."""
    database = "p2_concurrency_" + uuid4().hex[:12]
    admin_dsn = dict(pg_instance.dsn())
    admin = psycopg2.connect(**admin_dsn)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(pg_sql.SQL("CREATE DATABASE {}").format(pg_sql.Identifier(database)))
    finally:
        admin.close()

    isolated_dsn = dict(pg_instance.dsn())
    isolated_dsn["database"] = database
    isolated = psycopg2.connect(**isolated_dsn)
    isolated.autocommit = True
    try:
        with isolated.cursor() as cursor:
            for migration in sorted(_MIGRATIONS.glob("*.sql")):
                cursor.execute(migration.read_text(encoding="utf-8"))
            cursor.execute("""
                INSERT INTO core.Companies (CompanyCode, CompanyName, Status, IsSuspended)
                VALUES (%s, %s, 'Active', FALSE) RETURNING CompanyID
            """, ("P2C_" + uuid4().hex[:10], "Payroll Setup concurrency"))
            company_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO sec.Users (CompanyID, Username, DisplayName, IsActive, CanLogin)
                VALUES (%s, %s, 'Concurrency Admin', TRUE, TRUE) RETURNING UserID
            """, (company_id, "p2c_" + uuid4().hex[:10]))
            user_id = cursor.fetchone()[0]
            cursor.execute(
                "UPDATE core.Companies SET OwnerUserID = %s WHERE CompanyID = %s",
                (user_id, company_id),
            )
            cursor.execute("""
                INSERT INTO sec.Roles (RoleCode, RoleName, RoleLevel, IsSystemRole)
                VALUES (%s, 'Concurrency Admin', 100, FALSE) RETURNING RoleID
            """, ("P2C_" + uuid4().hex[:10],))
            role_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO sec.CompanyRoles
                    (CompanyID, RoleCode, RoleName, RoleLevel, IsDefault,
                     IsProtected, IsCustom, IsActive)
                VALUES (%s, %s, 'Concurrency Admin', 100, FALSE, FALSE, TRUE, TRUE)
                RETURNING CompanyRoleID
            """, (company_id, "P2C_" + uuid4().hex[:10]))
            company_role_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO sec.CompanyRolePermissions (CompanyRoleID, PermissionCode)
                SELECT %s, PermissionCode FROM sec.Permissions
                ON CONFLICT (CompanyRoleID, PermissionCode) DO NOTHING
            """, (company_role_id,))
            branch_ids = []
            for index in range(1, 5):
                cursor.execute("""
                    INSERT INTO core.Branches
                        (CompanyID, BranchCode, BranchName, Status, IsDefault)
                    VALUES (%s, %s, %s, 'Active', FALSE) RETURNING BranchID
                """, (company_id, f"B{index}", f"Concurrency Branch {index}"))
                branch_ids.append(cursor.fetchone()[0])
            cursor.execute("""
                INSERT INTO sec.UserBranchRoles
                    (UserID, CompanyID, BranchID, RoleID, CompanyRoleID,
                     ScopeType, IsActive)
                VALUES (%s, %s, NULL, %s, %s, 'AllCompanyBranches', TRUE)
            """, (user_id, company_id, role_id, company_role_id))
    finally:
        isolated.close()

    url = (
        f"postgresql+asyncpg://{isolated_dsn['user']}@{isolated_dsn['host']}"
        f":{isolated_dsn['port']}/{database}"
    )
    try:
        yield SimpleNamespace(
            url=url, company_id=int(company_id), user_id=int(user_id),
            branch_ids=tuple(int(branch_id) for branch_id in branch_ids),
        )
    finally:
        cleanup = psycopg2.connect(**admin_dsn)
        cleanup.autocommit = True
        try:
            with cleanup.cursor() as cursor:
                cursor.execute(
                    pg_sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                    .format(pg_sql.Identifier(database))
                )
        finally:
            cleanup.close()


async def _new_setup(db, conn, code: str) -> int:
    return await create_setup(
        db.company_id, db.user_id, code, f"Setup {code}", conn,
    )


async def _draft(db, conn, setup_id: int, *, frequency="Week", anchor=_ANCHOR):
    return await create_draft(
        db.company_id, db.user_id, setup_id, conn,
        payroll_frequency=frequency, anchor_start_date=anchor,
        normal_days_off_mask=0,
    )


async def _publish(db, conn, setup_id: int, draft_id: int, effective: date, *, replaces=None):
    return await publish_version(
        db.company_id, db.user_id, setup_id, draft_id, effective, conn,
        replaces_version_id=replaces,
    )


async def _set_lock_timeout(conn, timeout="5s"):
    await conn.execute(
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": timeout},
    )


async def _concurrent_calls(db, callables):
    """Run each operation in its own connection and commit successful results."""
    barrier = asyncio.Barrier(len(callables))

    async def run(call):
        async with db.engine.connect() as conn:
            transaction = await conn.begin()
            try:
                await _set_lock_timeout(conn)
                await barrier.wait()
                result = await call(conn)
                await asyncio.wait_for(transaction.commit(), timeout=8)
                return result
            except BaseException:
                if transaction.is_active:
                    await transaction.rollback()
                raise

    return await asyncio.wait_for(
        asyncio.gather(*(run(call) for call in callables), return_exceptions=True),
        timeout=12,
    )


@pytest_asyncio.fixture
async def database_engine(isolated_policy_database):
    engine = create_async_engine(isolated_policy_database.url, echo=False)
    isolated_policy_database.engine = engine
    try:
        yield isolated_policy_database
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_publish_serializes_version_numbers(database_engine):
    db = database_engine
    async with db.engine.begin() as conn:
        setup_id = await _new_setup(db, conn, "PUBLISH")
        first = await _draft(db, conn, setup_id)
        second = await _draft(db, conn, setup_id)

    dates = (_ANCHOR, date(2090, 1, 8))
    results = await _concurrent_calls(db, [
        lambda conn: _publish(db, conn, setup_id, first, dates[0]),
        lambda conn: _publish(db, conn, setup_id, second, dates[1]),
    ])
    assert results == [first, second]

    async with db.engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT VersionNumber, EffectiveFromDate
            FROM payroll.PayrollSetupVersions
            WHERE PayrollSetupID = :sid AND LifecycleState = 'Published'
        """), {"sid": setup_id})).all()
    assert len(rows) == 2
    version_numbers = [row[0] for row in rows]
    assert set(version_numbers) == {1, 2}
    assert len(set(version_numbers)) == 2
    assert {row[1] for row in rows} == set(dates)


@pytest.mark.asyncio
async def test_concurrent_same_date_replacement_cannot_create_a_fork(database_engine):
    db = database_engine
    async with db.engine.begin() as conn:
        setup_id = await _new_setup(db, conn, "REPLACE")
        original_draft = await _draft(db, conn, setup_id)
        original = await _publish(db, conn, setup_id, original_draft, _ANCHOR)
        drafts = [await _draft(db, conn, setup_id) for _ in range(2)]

    outcomes = await _concurrent_calls(db, [
        lambda conn, draft=draft: _publish(
            db, conn, setup_id, draft, _ANCHOR, replaces=original,
        )
        for draft in drafts
    ])
    successes = [result for result in outcomes if isinstance(result, int)]
    failures = [result for result in outcomes if isinstance(result, PolicyError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code == "REPLACEMENT_NOT_TERMINAL"

    async with db.engine.connect() as conn:
        versions = (await conn.execute(text("""
            SELECT PayrollSetupVersionID, ReplacesVersionID
            FROM payroll.PayrollSetupVersions
            WHERE PayrollSetupID = :sid AND LifecycleState = 'Published'
            ORDER BY VersionNumber
        """), {"sid": setup_id})).all()
    assert versions[0][0] == original and versions[0][1] is None
    assert len(versions) == 2
    assert versions[1][1] == original
    assert sum(1 for row in versions if row[1] == original) == 1


@pytest.mark.asyncio
async def test_setup_timeline_lock_serializes_publication_with_assignment(database_engine):
    db = database_engine
    branch_id = db.branch_ids[0]
    effective = date(2090, 1, 8)
    async with db.engine.begin() as conn:
        setup_id = await _new_setup(db, conn, "TIMELINE")
        initial_draft = await _draft(db, conn, setup_id)
        await _publish(db, conn, setup_id, initial_draft, _ANCHOR)
        future_draft = await _draft(db, conn, setup_id)

    outcomes = await _concurrent_calls(db, [
        lambda conn: _publish(db, conn, setup_id, future_draft, effective),
        lambda conn: assign_setup(
            db.company_id, db.user_id, branch_id, setup_id, effective, conn,
        ),
    ])
    assert all(not isinstance(result, BaseException) for result in outcomes), outcomes

    async with db.engine.connect() as conn:
        assignment = (await conn.execute(text("""
            SELECT BranchPayrollSetupAssignmentID FROM payroll.BranchPayrollSetupAssignments
            WHERE CompanyID = :cid AND BranchID = :bid AND EffectiveFromDate = :effective
              AND WithdrawnAtUtc IS NULL
        """), {"cid": db.company_id, "bid": branch_id, "effective": effective})).scalar_one()
        effective_version = (await conn.execute(text("""
            SELECT PayrollSetupVersionID FROM payroll.PayrollSetupVersions
            WHERE PayrollSetupID = :sid AND LifecycleState = 'Published'
              AND EffectiveFromDate = :effective
        """), {"sid": setup_id, "effective": effective})).scalar_one()
        publication_events = (await conn.execute(text("""
            SELECT EventType FROM payroll.PayrollSetupPolicyAuditEvents
            WHERE CompanyID = :cid AND PayrollSetupID = :sid
              AND EffectiveDate = :effective
              AND EventType IN ('VersionPublished', 'FutureVersionScheduled', 'VersionReplaced')
            ORDER BY PayrollSetupPolicyAuditEventID
        """), {"cid": db.company_id, "sid": setup_id,
               "effective": effective})).scalars().all()
        assignment_audit_count = (await conn.execute(text("""
            SELECT COUNT(*) FROM payroll.PayrollSetupPolicyAuditEvents
            WHERE CompanyID = :cid AND BranchID = :bid
              AND EffectiveDate = :effective AND EventType = 'BranchAssigned'
        """), {"cid": db.company_id, "bid": branch_id,
               "effective": effective})).scalar_one()
    assert assignment
    assert effective_version
    assert publication_events == ["FutureVersionScheduled"], publication_events
    assert assignment_audit_count == 1


async def _wait_until_backend_is_lock_waiting(conn, backend_pid: int) -> None:
    async def poll_database_state():
        while True:
            state = (await conn.execute(text("""
                SELECT wait_event_type FROM pg_stat_activity WHERE PID = :pid
            """), {"pid": backend_pid})).scalar_one_or_none()
            if state == "Lock":
                return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll_database_state(), timeout=5)


@pytest.mark.asyncio
async def test_reassignment_waits_for_candidate_period_creation_branch_lock(database_engine):
    db = database_engine
    branch_id = db.branch_ids[0]
    async with db.engine.begin() as conn:
        source = await _new_setup(db, conn, "LOCKSRC")
        destination = await _new_setup(db, conn, "LOCKDST")
        source_draft = await _draft(db, conn, source)
        destination_draft = await _draft(db, conn, destination)
        await _publish(db, conn, source, source_draft, _ANCHOR)
        await _publish(db, conn, destination, destination_draft, _ANCHOR)
        await assign_setup(db.company_id, db.user_id, branch_id, source, _ANCHOR, conn)
        first = await get_period_candidates(
            db.company_id, db.user_id, branch_id, "OPEN_CREATION", None, conn,
        )
        assert first.selected.creatable
        opened = await create_period_from_candidate(
            db.company_id, db.user_id, branch_id,
            PeriodCreationRequest(candidate_key=first.selected.candidate_key), conn,
        )
        assert opened.result == "CREATED"

        prepared = await get_period_candidates(
            db.company_id, db.user_id, branch_id, "PREPARED_CREATION", None, conn,
        )
        assert prepared.selected.creatable
        assert (prepared.selected.start_date, prepared.selected.end_date) == (
            date(2090, 1, 8), date(2090, 1, 14),
        )

    async with db.engine.connect() as creator:
        creator_tx = await creator.begin()
        await _set_lock_timeout(creator)
        await _acquire_branch_workflow_lock(db.company_id, branch_id, creator)

        async with db.engine.connect() as reassigner:
            reassign_tx = await reassigner.begin()
            await _set_lock_timeout(reassigner)
            backend_pid = int((await reassigner.execute(text("SELECT pg_backend_pid()"))).scalar_one())
            reassignment = asyncio.create_task(reassign_setup(
                db.company_id, db.user_id, branch_id, destination,
                date(2090, 1, 8), reassigner,
            ))
            await _wait_until_backend_is_lock_waiting(creator, backend_pid)
            try:
                created = await asyncio.wait_for(create_period_from_candidate(
                    db.company_id, db.user_id, branch_id,
                    PeriodCreationRequest(candidate_key=prepared.selected.candidate_key),
                    creator,
                ), timeout=5)
                assert created.result == "CREATED"
                assert created.start_date == date(2090, 1, 8)
                await asyncio.wait_for(creator_tx.commit(), timeout=5)
            except BaseException:
                if creator_tx.is_active:
                    await creator_tx.rollback()
                if not reassignment.done():
                    reassignment.cancel()
                await asyncio.gather(reassignment, return_exceptions=True)
                if reassign_tx.is_active:
                    await reassign_tx.rollback()
                raise

            with pytest.raises(PolicyError) as error:
                await asyncio.wait_for(reassignment, timeout=5)
            assert error.value.code == "PERIOD_HISTORY_CONFLICT"
            await reassign_tx.rollback()

    async with db.engine.connect() as conn:
        period_count = (await conn.execute(text("""
            SELECT COUNT(*) FROM payroll.PayrollPeriods
            WHERE CompanyID = :cid AND BranchID = :bid AND Status = 'Draft'
              AND StartDate = DATE '2090-01-08' AND EndDate = DATE '2090-01-14'
        """), {"cid": db.company_id, "bid": branch_id})).scalar_one()
        assignments = (await conn.execute(text("""
            SELECT COUNT(*) FROM payroll.BranchPayrollSetupAssignments
            WHERE CompanyID = :cid AND BranchID = :bid AND PayrollSetupID = :sid
              AND EffectiveFromDate = DATE '2090-01-08' AND WithdrawnAtUtc IS NULL
        """), {"cid": db.company_id, "bid": branch_id, "sid": destination})).scalar_one()
    assert period_count == 1
    assert assignments == 0


@pytest.mark.asyncio
async def test_reverse_branch_lock_requests_hold_same_database_lock_set(database_engine):
    db = database_engine
    first_branch, second_branch = db.branch_ids[:2]
    barrier = asyncio.Barrier(2)

    async def lock_both(branch_order):
        async with db.engine.connect() as conn:
            transaction = await conn.begin()
            try:
                await _set_lock_timeout(conn)
                await barrier.wait()
                await lock_branches(db.company_id, branch_order, conn)
                held = (await conn.execute(text("""
                    SELECT COUNT(*) FROM pg_locks
                    WHERE PID = pg_backend_pid() AND LockType = 'advisory' AND Granted
                """))).scalar_one()
                await transaction.commit()
                return held
            except BaseException:
                if transaction.is_active:
                    await transaction.rollback()
                raise

    branch_lock_results = await asyncio.wait_for(asyncio.gather(
        lock_both([first_branch, second_branch]),
        lock_both([second_branch, first_branch]),
    ), timeout=8)
    assert branch_lock_results == [2, 2]


@pytest.mark.asyncio
async def test_opposite_reassignments_lock_source_and_destination_without_deadlock(
    database_engine,
):
    db = database_engine
    first_branch, second_branch = db.branch_ids[:2]
    async with db.engine.begin() as conn:
        setup_a = await _new_setup(db, conn, "ORDERA")
        setup_b = await _new_setup(db, conn, "ORDERB")
        draft_a = await _draft(db, conn, setup_a)
        draft_b = await _draft(db, conn, setup_b)
        await _publish(db, conn, setup_a, draft_a, _ANCHOR)
        await _publish(db, conn, setup_b, draft_b, _ANCHOR)
        await assign_setup(db.company_id, db.user_id, first_branch, setup_a, _ANCHOR, conn)
        await assign_setup(db.company_id, db.user_id, second_branch, setup_b, _ANCHOR, conn)

    reassign_results = await _concurrent_calls(db, [
        lambda conn: reassign_setup(
            db.company_id, db.user_id, first_branch, setup_b,
            date(2090, 1, 8), conn,
        ),
        lambda conn: reassign_setup(
            db.company_id, db.user_id, second_branch, setup_a,
            date(2090, 1, 8), conn,
        ),
    ])
    assert all(isinstance(result, int) for result in reassign_results), reassign_results

    async with db.engine.connect() as conn:
        final_setups = (await conn.execute(text("""
            SELECT BranchID, PayrollSetupID
            FROM payroll.BranchPayrollSetupAssignments
            WHERE CompanyID = :cid AND BranchID IN (:first, :second)
              AND EffectiveFromDate = DATE '2090-01-08' AND WithdrawnAtUtc IS NULL
            ORDER BY BranchID
        """), {"cid": db.company_id, "first": first_branch,
               "second": second_branch})).all()
    assert [(row[0], row[1]) for row in final_setups] == [
        (first_branch, setup_b), (second_branch, setup_a),
    ]
