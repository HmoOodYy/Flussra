"""Focused Phase 4 regressions for Payroll Setup default locking and authorization."""

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

from app.payroll_setup.policy import create_draft, create_setup, publish_version, set_default_setup

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS = _ROOT / "migrations" / "sql"
_ANCHOR = date(2090, 1, 1)


@pytest_asyncio.fixture
async def phase4_policy_db(pg_instance):
    """Create a disposable migrated PostgreSQL database for these regressions."""
    database = "p4_default_" + uuid4().hex[:12]
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
            """, ("P4_" + uuid4().hex[:10], "Payroll Setup Phase 4"))
            company_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO sec.Users (CompanyID, Username, DisplayName, IsActive, CanLogin)
                VALUES (%s, %s, 'Phase 4 Admin', TRUE, TRUE) RETURNING UserID
            """, (company_id, "p4_" + uuid4().hex[:10]))
            user_id = cursor.fetchone()[0]
            cursor.execute(
                "UPDATE core.Companies SET OwnerUserID = %s WHERE CompanyID = %s",
                (user_id, company_id),
            )
            cursor.execute("""
                INSERT INTO sec.Roles (RoleCode, RoleName, RoleLevel, IsSystemRole)
                VALUES (%s, 'Phase 4 Admin', 100, FALSE) RETURNING RoleID
            """, ("P4R_" + uuid4().hex[:10],))
            role_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO sec.CompanyRoles
                    (CompanyID, RoleCode, RoleName, RoleLevel, IsDefault,
                     IsProtected, IsCustom, IsActive)
                VALUES (%s, %s, 'Phase 4 Admin', 100, FALSE, FALSE, TRUE, TRUE)
                RETURNING CompanyRoleID
            """, (company_id, "P4C_" + uuid4().hex[:10]))
            company_role_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO sec.CompanyRolePermissions (CompanyRoleID, PermissionCode)
                SELECT %s, PermissionCode FROM sec.Permissions
                ON CONFLICT (CompanyRoleID, PermissionCode) DO NOTHING
            """, (company_role_id,))
            cursor.execute("""
                INSERT INTO sec.UserBranchRoles
                    (UserID, CompanyID, BranchID, RoleID, CompanyRoleID,
                     ScopeType, IsActive)
                VALUES (%s, %s, NULL, %s, %s, 'AllCompanyBranches', TRUE)
            """, (user_id, company_id, role_id, company_role_id))
            cursor.execute("""
                INSERT INTO core.Branches (CompanyID, BranchCode, BranchName, Status, IsDefault)
                VALUES (%s, 'P4BRANCH', 'Phase 4 Branch', 'Active', FALSE)
                RETURNING BranchID
            """, (company_id,))
            branch_id = cursor.fetchone()[0]
    finally:
        isolated.close()

    url = (
        f"postgresql+asyncpg://{isolated_dsn['user']}@{isolated_dsn['host']}"
        f":{isolated_dsn['port']}/{database}"
    )
    engine = create_async_engine(url, echo=False)
    try:
        yield SimpleNamespace(
            engine=engine, company_id=int(company_id), user_id=int(user_id),
            branch_id=int(branch_id), admin_dsn=admin_dsn, database=database,
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


async def _set_lock_timeout(conn, timeout: str = "5s") -> None:
    await conn.execute(
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": timeout},
    )


async def _wait_until_blocked(observer, backend_pid: int) -> None:
    async def poll():
        while True:
            blocked = await observer.execute(text("""
                SELECT cardinality(pg_blocking_pids(:pid)) > 0
            """), {"pid": backend_pid})
            if blocked.scalar_one():
                return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout=5)


@pytest.mark.asyncio
async def test_default_change_company_lock_does_not_deadlock_with_publish_audit(phase4_policy_db):
    db = phase4_policy_db
    async with db.engine.begin() as conn:
        setup_id = await create_setup(
            db.company_id, db.user_id, "LOCKCYCLE", "Lock cycle", conn,
        )
        draft_id = await create_draft(
            db.company_id, db.user_id, setup_id, conn,
            payroll_frequency="Week", anchor_start_date=_ANCHOR,
            normal_days_off_mask=0,
        )

    async with db.engine.connect() as publisher, db.engine.connect() as default_setter:
        publish_tx = await publisher.begin()
        default_tx = await default_setter.begin()
        try:
            await _set_lock_timeout(publisher)
            await _set_lock_timeout(default_setter)
            await publisher.execute(text("""
                SELECT payrollsetupid FROM payroll.payrollsetups
                WHERE companyid = :cid AND payrollsetupid = :sid
                FOR NO KEY UPDATE
            """), {"cid": db.company_id, "sid": setup_id})
            setter_pid = int((await default_setter.execute(
                text("SELECT pg_backend_pid()")
            )).scalar_one())
            set_default_task = asyncio.create_task(set_default_setup(
                db.company_id, db.user_id, setup_id, default_setter,
            ))
            async with db.engine.connect() as observer:
                await _wait_until_blocked(observer, setter_pid)

            published_id = await asyncio.wait_for(publish_version(
                db.company_id, db.user_id, setup_id, draft_id, _ANCHOR, publisher,
            ), timeout=6)
            await asyncio.wait_for(publish_tx.commit(), timeout=6)
            await asyncio.wait_for(set_default_task, timeout=6)
            await asyncio.wait_for(default_tx.commit(), timeout=6)
        except BaseException:
            if publish_tx.is_active:
                await publish_tx.rollback()
            if default_tx.is_active:
                await default_tx.rollback()
            if not set_default_task.done():
                set_default_task.cancel()
            await asyncio.gather(set_default_task, return_exceptions=True)
            raise

    async with db.engine.connect() as conn:
        final_state = (await conn.execute(text("""
            SELECT c.DefaultPayrollSetupID, v.LifecycleState, v.VersionNumber
            FROM core.Companies c
            JOIN payroll.PayrollSetupVersions v ON v.PayrollSetupID = :sid
            WHERE c.CompanyID = :cid AND v.PayrollSetupVersionID = :vid
        """), {"cid": db.company_id, "sid": setup_id, "vid": published_id})).one()
        events = (await conn.execute(text("""
            SELECT EventType, PayrollSetupVersionID, OldPayrollSetupID, NewPayrollSetupID
            FROM payroll.PayrollSetupPolicyAuditEvents
            WHERE CompanyID = :cid AND EventType IN ('VersionPublished', 'DefaultChanged')
            ORDER BY PayrollSetupPolicyAuditEventID
        """), {"cid": db.company_id})).all()
    assert tuple(final_state) == (setup_id, "Published", 1)
    assert [tuple(row) for row in events] == [
        ("VersionPublished", published_id, None, setup_id),
        ("DefaultChanged", None, None, setup_id),
    ]


async def _create_actor(db, *, permissions: tuple[str, ...], scope: str, driver=False) -> int:
    suffix = uuid4().hex[:10]
    role_code = "DRIVER" if driver else "P4C_" + suffix
    async with db.engine.begin() as conn:
        user_id = (await conn.execute(text("""
            INSERT INTO sec.Users (CompanyID, Username, DisplayName, IsActive, CanLogin)
            VALUES (:cid, :username, 'Phase 4 Permission Actor', TRUE, TRUE)
            RETURNING UserID
        """), {"cid": db.company_id, "username": "p4u_" + suffix})).scalar_one()
        role_id = (await conn.execute(text("""
            INSERT INTO sec.Roles (RoleCode, RoleName, RoleLevel, IsSystemRole)
            VALUES (:code, 'Phase 4 Permission Actor', 20, FALSE) RETURNING RoleID
        """), {"code": "P4R_" + suffix})).scalar_one()
        company_role_id = (await conn.execute(text("""
            INSERT INTO sec.CompanyRoles
                (CompanyID, RoleCode, RoleName, RoleLevel, IsDefault,
                 IsProtected, IsCustom, IsActive)
            VALUES (:cid, :code, 'Phase 4 Permission Actor', 20, FALSE, FALSE, TRUE, TRUE)
            RETURNING CompanyRoleID
        """), {"cid": db.company_id, "code": role_code})).scalar_one()
        for permission in permissions:
            await conn.execute(text("""
                INSERT INTO sec.CompanyRolePermissions (CompanyRoleID, PermissionCode)
                VALUES (:rid, :permission)
            """), {"rid": company_role_id, "permission": permission})
        await conn.execute(text("""
            INSERT INTO sec.UserBranchRoles
                (UserID, CompanyID, BranchID, RoleID, CompanyRoleID, ScopeType, IsActive)
            VALUES (:uid, :cid, :bid, :role_id, :company_role_id, :scope, TRUE)
        """), {
            "uid": user_id, "cid": db.company_id,
            "bid": db.branch_id if scope == "SpecificBranch" else None,
            "role_id": role_id, "company_role_id": company_role_id, "scope": scope,
        })
    return int(user_id)


@pytest.mark.asyncio
async def test_default_mutation_requires_assign_company_scope_and_non_driver(phase4_policy_db):
    db = phase4_policy_db
    async with db.engine.begin() as conn:
        setup_id = await create_setup(
            db.company_id, db.user_id, "AUTHZ", "Authorization", conn,
        )

    manage_only = await _create_actor(
        db, permissions=("payroll_setup.manage",), scope="AllCompanyBranches",
    )
    assign_only = await _create_actor(
        db, permissions=("payroll_setup.assign",), scope="AllCompanyBranches",
    )
    branch_scoped = await _create_actor(
        db, permissions=("payroll_setup.assign",), scope="SpecificBranch",
    )
    driver = await _create_actor(
        db, permissions=("payroll_setup.assign",),
        scope="AllCompanyBranches", driver=True,
    )

    async with db.engine.begin() as conn:
        with pytest.raises(HTTPException) as error:
            await set_default_setup(db.company_id, manage_only, setup_id, conn)
        assert error.value.status_code == 403

    async with db.engine.begin() as conn:
        await set_default_setup(db.company_id, assign_only, setup_id, conn)
    async with db.engine.begin() as conn:
        with pytest.raises(HTTPException) as error:
            await set_default_setup(db.company_id, manage_only, None, conn)
        assert error.value.status_code == 403

    for denied_user in (branch_scoped, driver):
        async with db.engine.begin() as conn:
            with pytest.raises(HTTPException) as error:
                await set_default_setup(db.company_id, denied_user, None, conn)
            assert error.value.status_code == 403

    async with db.engine.begin() as conn:
        await set_default_setup(db.company_id, assign_only, None, conn)
    async with db.engine.connect() as conn:
        default_id = (await conn.execute(text(
            "SELECT DefaultPayrollSetupID FROM core.Companies WHERE CompanyID = :cid"
        ), {"cid": db.company_id})).scalar_one_or_none()
    assert default_id is None
