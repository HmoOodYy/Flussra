"""PostgreSQL concurrency coverage for Phase 3 period authority operations."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg2
import pytest
import pytest_asyncio
from fastapi import HTTPException
from psycopg2 import sql as pg_sql
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.payroll.period_creation import (
    create_period_from_candidate,
    get_period_candidates,
)
from app.payroll.schemas import PeriodCreationRequest
from app.payroll.workflow_lock import _acquire_branch_workflow_lock
from app.payroll_setup.errors import PolicyError
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
async def phase3_concurrency_db(pg_instance):
    """Give each concurrency test a fresh database on the disposable PG cluster."""
    database = "p3_concurrency_" + uuid4().hex[:12]
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
            """, ("P3C_" + uuid4().hex[:10], "Phase 3 concurrency"))
            company_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO sec.Users (CompanyID, Username, DisplayName, IsActive, CanLogin)
                VALUES (%s, %s, 'Concurrency Admin', TRUE, TRUE) RETURNING UserID
            """, (company_id, "p3c_" + uuid4().hex[:10]))
            user_id = cursor.fetchone()[0]
            cursor.execute(
                "UPDATE core.Companies SET OwnerUserID = %s WHERE CompanyID = %s",
                (user_id, company_id),
            )
            role_code = "P3C_" + uuid4().hex[:10]
            cursor.execute("""
                INSERT INTO sec.Roles (RoleCode, RoleName, RoleLevel, IsSystemRole)
                VALUES (%s, 'Concurrency Admin', 100, FALSE) RETURNING RoleID
            """, (role_code,))
            role_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO sec.CompanyRoles
                    (CompanyID, RoleCode, RoleName, RoleLevel, IsDefault,
                     IsProtected, IsCustom, IsActive)
                VALUES (%s, %s, 'Concurrency Admin', 100, FALSE, FALSE, TRUE, TRUE)
                RETURNING CompanyRoleID
            """, (company_id, role_code))
            company_role_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO sec.CompanyRolePermissions (CompanyRoleID, PermissionCode)
                SELECT %s, PermissionCode FROM sec.Permissions
                ON CONFLICT (CompanyRoleID, PermissionCode) DO NOTHING
            """, (company_role_id,))
            cursor.execute("""
                INSERT INTO core.Branches
                    (CompanyID, BranchCode, BranchName, Status, IsDefault)
                VALUES (%s, 'P3C_BRANCH', 'Concurrency Branch', 'Active', FALSE)
                RETURNING BranchID
            """, (company_id,))
            branch_id = cursor.fetchone()[0]
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
    engine = create_async_engine(url, echo=False)
    try:
        yield SimpleNamespace(
            engine=engine,
            company_id=int(company_id),
            user_id=int(user_id),
            branch_id=int(branch_id),
            marker=uuid4().hex[:10],
        )
    finally:
        await engine.dispose()
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


async def _new_setup(db, conn, suffix: str, *, mask: int = 0) -> tuple[int, int]:
    setup_id = await create_setup(
        db.company_id, db.user_id, f"P3C_{db.marker}_{suffix}", suffix, conn,
    )
    draft_id = await create_draft(
        db.company_id, db.user_id, setup_id, conn,
        payroll_frequency="Week", anchor_start_date=_ANCHOR,
        normal_days_off_mask=mask,
    )
    version_id = await publish_version(
        db.company_id, db.user_id, setup_id, draft_id, _ANCHOR, conn,
    )
    return setup_id, version_id


async def _preview(db, conn):
    return await get_period_candidates(
        db.company_id, db.user_id, db.branch_id,
        "OPEN_CREATION", None, conn,
    )


async def _confirm(db, conn, candidate_key: str):
    return await create_period_from_candidate(
        db.company_id, db.user_id, db.branch_id,
        PeriodCreationRequest(candidate_key=candidate_key), conn,
    )


async def _set_lock_timeout(conn, timeout: str = "8s") -> None:
    await conn.execute(text("SELECT set_config('lock_timeout', :timeout, true)"),
                       {"timeout": timeout})


async def _wait_for_lock_wait(db, backend_pid: int) -> None:
    async def poll():
        while True:
            async with db.engine.connect() as observer:
                wait_type = (await observer.execute(text("""
                    SELECT wait_event_type FROM pg_stat_activity WHERE PID = :pid
                """), {"pid": backend_pid})).scalar_one_or_none()
            if wait_type == "Lock":
                return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout=5)


@pytest.mark.asyncio
async def test_concurrent_confirmation_of_one_candidate_creates_once(phase3_concurrency_db):
    db = phase3_concurrency_db
    async with db.engine.begin() as conn:
        setup_id, _ = await _new_setup(db, conn, "SAME")
        await assign_setup(db.company_id, db.user_id, db.branch_id, setup_id, _ANCHOR, conn)
        candidate = (await _preview(db, conn)).selected.candidate_key

    barrier = asyncio.Barrier(2)

    async def confirm_in_own_transaction():
        async with db.engine.connect() as conn:
            transaction = await conn.begin()
            try:
                await _set_lock_timeout(conn)
                await barrier.wait()
                result = await asyncio.wait_for(_confirm(db, conn, candidate), timeout=12)
                await asyncio.wait_for(transaction.commit(), timeout=5)
                return result
            except BaseException:
                if transaction.is_active:
                    await transaction.rollback()
                raise

    results = await asyncio.wait_for(
        asyncio.gather(confirm_in_own_transaction(), confirm_in_own_transaction(),
                       return_exceptions=True),
        timeout=15,
    )
    errors = [result for result in results if isinstance(result, BaseException)]
    responses = [result for result in results if not isinstance(result, BaseException)]
    assert not errors, errors
    assert sorted(result.result for result in responses) == ["ALREADY_EXISTS", "CREATED"]
    assert len({result.payroll_period_id for result in responses}) == 1

    async with db.engine.connect() as conn:
        count = (await conn.execute(text("""
            SELECT COUNT(*) FROM payroll.PayrollPeriods
            WHERE CompanyID = :cid AND BranchID = :bid
              AND CreationCandidateKeyHash IS NOT NULL
        """), {"cid": db.company_id, "bid": db.branch_id})).scalar_one()
    assert count == 1


@pytest.mark.parametrize("operation", ["publish", "reassign"])
@pytest.mark.parametrize("mutation_first", [False, True])
@pytest.mark.asyncio
async def test_policy_mutation_and_candidate_confirmation_obey_lock_order(
    phase3_concurrency_db, operation: str, mutation_first: bool,
):
    db = phase3_concurrency_db
    async with db.engine.begin() as conn:
        source_setup_id, source_version_id = await _new_setup(
            db, conn, "SRC", mask=0,
        )
        await assign_setup(
            db.company_id, db.user_id, db.branch_id, source_setup_id, _ANCHOR, conn,
        )
        if operation == "publish":
            draft_id = await create_draft(
                db.company_id, db.user_id, source_setup_id, conn,
                payroll_frequency="Week", anchor_start_date=_ANCHOR,
                normal_days_off_mask=1,
            )
            mutation_setup_id = source_setup_id
        else:
            mutation_setup_id, _ = await _new_setup(db, conn, "DST", mask=0)
            draft_id = None
        candidate = (await _preview(db, conn)).selected.candidate_key

    async def mutation(conn):
        if operation == "publish":
            return await publish_version(
                db.company_id, db.user_id, mutation_setup_id, draft_id, _ANCHOR,
                conn, replaces_version_id=source_version_id,
            )
        return await reassign_setup(
            db.company_id, db.user_id, db.branch_id, mutation_setup_id,
            date(2090, 1, 8), conn,
        )

    if mutation_first:
        async with db.engine.connect() as policy_conn:
            policy_tx = await policy_conn.begin()
            await _set_lock_timeout(policy_conn)
            mutation_result = await mutation(policy_conn)
            policy_pid = int((await policy_conn.execute(
                text("SELECT pg_backend_pid()")
            )).scalar_one())

            async with db.engine.connect() as confirm_conn:
                confirm_tx = await confirm_conn.begin()
                await _set_lock_timeout(confirm_conn)
                confirm_pid = int((await confirm_conn.execute(
                    text("SELECT pg_backend_pid()")
                )).scalar_one())
                confirmation = asyncio.create_task(_confirm(db, confirm_conn, candidate))
                await _wait_for_lock_wait(db, confirm_pid)
                await asyncio.wait_for(policy_tx.commit(), timeout=5)
                try:
                    with pytest.raises(HTTPException) as error:
                        await asyncio.wait_for(confirmation, timeout=8)
                    assert error.value.status_code == 409
                    assert error.value.detail["code"] == "CANDIDATE_STALE"
                    await confirm_tx.rollback()
                finally:
                    if not confirmation.done():
                        confirmation.cancel()
                        await asyncio.gather(confirmation, return_exceptions=True)
                    if confirm_tx.is_active:
                        await confirm_tx.rollback()
            assert mutation_result is not None
            assert policy_pid != confirm_pid
    else:
        async with db.engine.connect() as confirm_conn:
            confirm_tx = await confirm_conn.begin()
            await _set_lock_timeout(confirm_conn)
            await _acquire_branch_workflow_lock(
                db.company_id, db.branch_id, confirm_conn,
            )
            confirm_pid = int((await confirm_conn.execute(
                text("SELECT pg_backend_pid()")
            )).scalar_one())

            async with db.engine.connect() as policy_conn:
                policy_tx = await policy_conn.begin()
                await _set_lock_timeout(policy_conn)
                policy_pid = int((await policy_conn.execute(
                    text("SELECT pg_backend_pid()")
                )).scalar_one())
                policy_task = asyncio.create_task(mutation(policy_conn))
                await _wait_for_lock_wait(db, policy_pid)
                created = await asyncio.wait_for(_confirm(db, confirm_conn, candidate), timeout=8)
                assert created.result == "CREATED"
                await asyncio.wait_for(confirm_tx.commit(), timeout=5)
                try:
                    if operation == "publish":
                        with pytest.raises(PolicyError):
                            await asyncio.wait_for(policy_task, timeout=8)
                        await policy_tx.rollback()
                    else:
                        reassignment_id = await asyncio.wait_for(policy_task, timeout=8)
                        assert reassignment_id > 0
                        await asyncio.wait_for(policy_tx.commit(), timeout=5)
                finally:
                    if not policy_task.done():
                        policy_task.cancel()
                        await asyncio.gather(policy_task, return_exceptions=True)
                    if policy_tx.is_active:
                        await policy_tx.rollback()
            assert confirm_pid != policy_pid
