"""Behavioral coverage for the 0074 -> 0075 Access authority cutover.

Legacy ODA rows are inserted directly only in this migration test because the
current application and post-0075 schema intentionally cannot create them.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import psycopg2
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def access_migration_db(pg_instance, monkeypatch):
    """Create a disposable database in the test-owned PostgreSQL cluster."""
    dbname = f"p2b_{uuid.uuid4().hex[:12]}"
    cluster_dsn = pg_instance.dsn()
    admin = psycopg2.connect(**cluster_dsn)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        admin.close()

    url = (
        f"postgresql+asyncpg://{cluster_dsn['user']}@{cluster_dsn['host']}"
        f":{cluster_dsn['port']}/{dbname}"
    )
    monkeypatch.setenv("DATABASE_URL", url)
    from app.config import get_settings

    get_settings.cache_clear()
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    dsn = {**cluster_dsn, "database": dbname}
    try:
        yield config, dsn
    finally:
        cleanup = psycopg2.connect(**cluster_dsn)
        cleanup.autocommit = True
        try:
            with cleanup.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (dbname,),
                )
                cursor.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        finally:
            cleanup.close()
        get_settings.cache_clear()


def _db(dsn):
    conn = psycopg2.connect(**dsn)
    conn.autocommit = True
    return conn


def _upgrade(config: Config, revision: str) -> None:
    """Run Alembic without leaking asyncio.run's event-loop reset to other tests."""
    try:
        previous_loop = asyncio.get_event_loop()
    except RuntimeError:
        previous_loop = None
    try:
        command.upgrade(config, revision)
    finally:
        if previous_loop is not None and not previous_loop.is_closed():
            asyncio.set_event_loop(previous_loop)
        else:
            asyncio.set_event_loop(asyncio.new_event_loop())


def _seed_legacy_access_state(dsn, *, invalid_non_driver_oda: bool = False):
    """Seed historical rows while the database is still at migration 0074."""
    conn = _db(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO core.companies (CompanyCode, CompanyName, Status, IsSuspended)
                VALUES ('MIG', 'Migration Test', 'Active', FALSE)
                RETURNING CompanyID
            """)
            company_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO core.branches (CompanyID, BranchCode, BranchName, Status)
                VALUES (%s, 'A', 'Branch A', 'Active'),
                       (%s, 'B', 'Branch B', 'Active')
                RETURNING BranchID
            """, (company_id, company_id))
            branch_a, branch_b = [row[0] for row in cur.fetchall()]
            cur.execute("""
                INSERT INTO sec.Roles (RoleCode, RoleName, RoleLevel)
                VALUES ('DRIVER', 'Legacy Driver', 10), ('PAYROLL_VIEWER', 'Viewer', 20)
                RETURNING RoleID
            """)
            role_ids = [row[0] for row in cur.fetchall()]
            legacy_driver_role_id, viewer_role_id = role_ids
            cur.execute("""
                INSERT INTO sec.CompanyRoles
                    (CompanyID, RoleCode, RoleName, RoleLevel, IsProtected, IsActive)
                VALUES (%s, 'DRIVER', 'Driver', 10, TRUE, TRUE),
                       (%s, 'PAYROLL_VIEWER_CO', 'Viewer', 20, FALSE, TRUE),
                       (%s, 'COMPANY_OWNER', 'Company Owner', 100, TRUE, TRUE)
                RETURNING CompanyRoleID
            """, (company_id, company_id, company_id))
            driver_cr, viewer_cr, owner_cr = [row[0] for row in cur.fetchall()]
            cur.execute("""
                INSERT INTO sec.Permissions (PermissionCode, PermissionName, ModuleCode)
                VALUES ('drivers.view', 'View drivers', 'core'),
                       ('payroll.view', 'View payroll', 'payroll')
                ON CONFLICT (PermissionCode) DO NOTHING
            """)
            cur.execute("""
                INSERT INTO sec.CompanyRolePermissions (CompanyRoleID, PermissionCode)
                VALUES (%s, 'drivers.view'), (%s, 'payroll.view')
            """, (driver_cr, viewer_cr))
            cur.execute("""
                INSERT INTO sec.RolePermissions (RoleID, PermissionID)
                SELECT %s, PermissionID FROM sec.Permissions WHERE PermissionCode = 'drivers.view'
            """, (legacy_driver_role_id,))
            cur.execute("""
                INSERT INTO sec.Users (CompanyID, Username, DisplayName, IsActive, CanLogin)
                VALUES (%s, 'driver_cr_oda', 'Driver CR ODA', TRUE, TRUE),
                       (%s, 'driver_legacy_oda', 'Driver Legacy ODA', TRUE, TRUE),
                       (%s, 'driver_specific', 'Driver Specific', TRUE, TRUE),
                       (%s, 'driver_company', 'Driver Company', TRUE, TRUE),
                       (%s, 'viewer_specific', 'Viewer Specific', TRUE, TRUE),
                       (%s, 'viewer_company', 'Viewer Company', TRUE, TRUE),
                       (%s, 'override_none', 'Override None', TRUE, TRUE),
                       (%s, 'owner_specific', 'Owner Specific', TRUE, TRUE)
                RETURNING UserID
            """, (company_id,) * 8)
            user_ids = [row[0] for row in cur.fetchall()]
            (driver_cr_user, driver_legacy_user, driver_specific_user,
             driver_company_user, viewer_specific_user, viewer_company_user,
             override_none_user, owner_user) = user_ids
            cur.execute("""
                INSERT INTO sec.UserBranchRoles
                    (UserID, CompanyID, BranchID, RoleID, CompanyRoleID, ScopeType, IsActive)
                VALUES
                    (%s, %s, %s, NULL, %s, 'OwnDriverDataOnly', TRUE),
                    (%s, %s, %s, %s, NULL, 'OwnDriverDataOnly', TRUE),
                    (%s, %s, %s, NULL, %s, 'SpecificBranch', TRUE),
                    (%s, %s, NULL, NULL, %s, 'AllCompanyBranches', TRUE),
                    (%s, %s, %s, NULL, %s, 'SpecificBranch', TRUE),
                    (%s, %s, NULL, NULL, %s, 'AllCompanyBranches', TRUE),
                    (%s, %s, %s, NULL, %s, 'SpecificBranch', TRUE)
            """, (
                driver_cr_user, company_id, branch_a, driver_cr,
                driver_legacy_user, company_id, branch_a, legacy_driver_role_id,
                driver_specific_user, company_id, branch_a, driver_cr,
                driver_company_user, company_id, driver_cr,
                viewer_specific_user, company_id, branch_a, viewer_cr,
                viewer_company_user, company_id, viewer_cr,
                owner_user, company_id, branch_a, owner_cr,
            ))
            cur.execute("""
                INSERT INTO sec.UserPermissionOverrides
                    (UserID, CompanyID, PermissionCode, Effect, IsActive)
                VALUES (%s, %s, 'payroll.view', 'ALLOW', TRUE)
            """, (driver_cr_user, company_id))
            if invalid_non_driver_oda:
                cur.execute("""
                    INSERT INTO sec.UserBranchRoles
                        (UserID, CompanyID, BranchID, RoleID, CompanyRoleID, ScopeType, IsActive)
                    VALUES (%s, %s, %s, NULL, %s, 'OwnDriverDataOnly', TRUE)
                """, (viewer_specific_user, company_id, branch_a, viewer_cr))
        return {
            "company": company_id,
            "branch_a": branch_a,
            "branch_b": branch_b,
            "driver_cr": driver_cr,
            "viewer_cr": viewer_cr,
            "owner_cr": owner_cr,
            "driver_cr_user": driver_cr_user,
            "driver_legacy_user": driver_legacy_user,
            "driver_specific_user": driver_specific_user,
            "driver_company_user": driver_company_user,
            "viewer_specific_user": viewer_specific_user,
            "viewer_company_user": viewer_company_user,
            "override_none_user": override_none_user,
            "owner_user": owner_user,
        }
    finally:
        conn.close()


def test_fresh_database_upgrades_through_0083(access_migration_db):
    config, dsn = access_migration_db
    _upgrade(config, "head")
    conn = _db(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM public.alembic_version")
            assert cur.fetchone()[0] == "0083"
            cur.execute("""
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = 'sec.userbranchroles'::regclass
                  AND conname = 'ck_userbranchroles_scopetype'
            """)
            assert "Self" in cur.fetchone()[0]
            cur.execute("""
                SELECT pg_get_viewdef('app.vw_userbranchaccess'::regclass, TRUE)
            """)
            view_body = cur.fetchone()[0].lower()
            assert "allcompanybranches" in view_body
            assert "specificbranch" in view_body
            assert "self" not in view_body
            cur.execute("""
                SELECT pg_get_functiondef('sec.fn_UserHasPermission(integer,integer,integer,character varying)'::regprocedure)
            """)
            body = cur.fetchone()[0].lower()
            assert "self" in body and "userpermissionoverrides" in body
    finally:
        conn.close()


def test_upgrade_0074_to_0075_migrates_and_enforces_runtime_authority(access_migration_db):
    config, dsn = access_migration_db
    _upgrade(config, "0074")
    ids = _seed_legacy_access_state(dsn)
    _upgrade(config, "head")
    conn = _db(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ubr.ScopeType, ubr.BranchID
                FROM sec.UserBranchRoles ubr
                WHERE ubr.UserID = %s
            """, (ids["driver_cr_user"],))
            assert cur.fetchone() == ("Self", None)
            cur.execute("""
                SELECT ubr.ScopeType, ubr.BranchID
                FROM sec.UserBranchRoles ubr
                WHERE ubr.UserID = %s
            """, (ids["driver_legacy_user"],))
            assert cur.fetchone() == ("Self", None)
            cur.execute("""
                SELECT ubr.ScopeType, ubr.BranchID FROM sec.UserBranchRoles ubr
                WHERE ubr.UserID = %s
            """, (ids["driver_specific_user"],))
            assert cur.fetchone() == ("Self", None)
            cur.execute("""
                SELECT ubr.ScopeType, ubr.BranchID FROM sec.UserBranchRoles ubr
                WHERE ubr.UserID = %s
            """, (ids["driver_company_user"],))
            assert cur.fetchone() == ("Self", None)
            cur.execute("""
                SELECT ubr.ScopeType, ubr.BranchID FROM sec.UserBranchRoles ubr
                WHERE ubr.UserID = %s
            """, (ids["owner_user"],))
            assert cur.fetchone() == ("AllCompanyBranches", None)
            cur.execute("""
                SELECT COUNT(*) FROM sec.CompanyRolePermissions
                WHERE CompanyRoleID = %s
            """, (ids["driver_cr"],))
            assert cur.fetchone()[0] == 0
            cur.execute("""
                SELECT COUNT(*) FROM sec.RolePermissions rp
                JOIN sec.Roles r ON r.RoleID = rp.RoleID WHERE r.RoleCode = 'DRIVER'
            """)
            assert cur.fetchone()[0] == 0
            cur.execute("""
                SELECT IsActive, RevokedAtUtc IS NOT NULL FROM sec.UserPermissionOverrides
                WHERE UserID = %s
            """, (ids["driver_cr_user"],))
            assert cur.fetchone() == (False, True)

            # A stale permission cannot bypass the generic DRIVER/Self ceiling.
            cur.execute("""
                INSERT INTO sec.CompanyRolePermissions (CompanyRoleID, PermissionCode)
                VALUES (%s, 'drivers.view') ON CONFLICT DO NOTHING
            """, (ids["driver_cr"],))
            cur.execute("""
                SELECT sec.fn_UserHasPermission(%s, %s, %s, 'drivers.view')
            """, (ids["driver_cr_user"], ids["company"], ids["branch_a"]))
            assert cur.fetchone()[0] is False

            # Self assignments never enter the branch-access projection.
            cur.execute("""
                SELECT COUNT(*) FROM app.vw_UserBranchAccess WHERE UserID IN (%s, %s, %s, %s)
            """, (ids["driver_cr_user"], ids["driver_legacy_user"], ids["driver_specific_user"], ids["driver_company_user"]))
            assert cur.fetchone()[0] == 0

            # Resource-bounded ALLOW overrides still work for non-DRIVER users.
            for username, branch_id, company_id, role_id, scope in (
                ("override_all", None, ids["company"], ids["viewer_cr"], "AllCompanyBranches"),
                ("override_branch", ids["branch_a"], ids["company"], ids["viewer_cr"], "SpecificBranch"),
            ):
                cur.execute("""
                    INSERT INTO sec.Users (CompanyID, Username, DisplayName, IsActive, CanLogin)
                    VALUES (%s, %s, %s, TRUE, TRUE) RETURNING UserID
                """, (company_id, username, username))
                user_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO sec.UserBranchRoles
                        (UserID, CompanyID, BranchID, RoleID, CompanyRoleID, ScopeType, IsActive)
                    VALUES (%s, %s, %s, NULL, %s, %s, TRUE)
                """, (user_id, company_id, branch_id, role_id, scope))
                cur.execute("""
                    INSERT INTO sec.UserPermissionOverrides (UserID, CompanyID, PermissionCode, Effect, IsActive)
                    VALUES (%s, %s, 'drivers.view', 'ALLOW', TRUE)
                """, (user_id, company_id))
                if scope == "AllCompanyBranches":
                    for target_branch in (ids["branch_a"], ids["branch_b"]):
                        cur.execute("SELECT sec.fn_UserHasPermission(%s, %s, %s, 'drivers.view')", (user_id, company_id, target_branch))
                        assert cur.fetchone()[0] is True
                    cur.execute("SELECT sec.fn_UserHasPermission(%s, %s, NULL, 'drivers.view')", (user_id, company_id))
                    assert cur.fetchone()[0] is True
                else:
                    cur.execute("SELECT sec.fn_UserHasPermission(%s, %s, %s, 'drivers.view')", (user_id, company_id, ids["branch_a"]))
                    assert cur.fetchone()[0] is True
                    for target_branch in (None, ids["branch_b"]):
                        cur.execute("SELECT sec.fn_UserHasPermission(%s, %s, %s, 'drivers.view')", (user_id, company_id, target_branch))
                        assert cur.fetchone()[0] is False

            cur.execute("""
                INSERT INTO sec.UserPermissionOverrides (UserID, CompanyID, PermissionCode, Effect, IsActive)
                VALUES (%s, %s, 'drivers.view', 'ALLOW', TRUE)
            """, (ids["override_none_user"], ids["company"]))
            cur.execute("SELECT sec.fn_UserHasPermission(%s, %s, NULL, 'drivers.view')", (ids["override_none_user"], ids["company"]))
            assert cur.fetchone()[0] is False

            # The trigger accepts valid Self and ordinary branch/company rows.
            cur.execute("""
                INSERT INTO sec.Users (CompanyID, Username, DisplayName, IsActive, CanLogin)
                VALUES (%s, 'valid_driver', 'Valid Driver', TRUE, TRUE) RETURNING UserID
            """, (ids["company"],))
            valid_driver = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO sec.UserBranchRoles (UserID, CompanyID, BranchID, RoleID, CompanyRoleID, ScopeType, IsActive)
                VALUES (%s, %s, NULL, NULL, %s, 'Self', TRUE)
            """, (valid_driver, ids["company"], ids["driver_cr"]))
            for suffix, branch_id, scope in (("specific", ids["branch_a"], "SpecificBranch"), ("all", None, "AllCompanyBranches")):
                cur.execute("""
                    INSERT INTO sec.Users (CompanyID, Username, DisplayName, IsActive, CanLogin)
                    VALUES (%s, %s, %s, TRUE, TRUE) RETURNING UserID
                """, (ids["company"], f"valid_{suffix}", f"Valid {suffix}"))
                user_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO sec.UserBranchRoles (UserID, CompanyID, BranchID, RoleID, CompanyRoleID, ScopeType, IsActive)
                    VALUES (%s, %s, %s, NULL, %s, %s, TRUE)
                """, (user_id, ids["company"], branch_id, ids["viewer_cr"], scope))

            # Invalid authority shapes are rejected by the trigger/constraint.
            for role_id, scope, branch_id, message in (
                (ids["driver_cr"], "SpecificBranch", ids["branch_a"], "driver_self_required"),
                (ids["driver_cr"], "AllCompanyBranches", None, "driver_self_required"),
                (ids["viewer_cr"], "Self", None, "self_driver_only"),
                (ids["driver_cr"], "Self", ids["branch_a"], "driver_self_required"),
            ):
                cur.execute("""
                    INSERT INTO sec.Users (CompanyID, Username, DisplayName, IsActive, CanLogin)
                    VALUES (%s, %s, %s, TRUE, TRUE) RETURNING UserID
                """, (ids["company"], f"reject_{uuid.uuid4().hex[:10]}", "Rejected"))
                bad_user = cur.fetchone()[0]
                with pytest.raises(psycopg2.Error) as error:
                    cur.execute("""
                        INSERT INTO sec.UserBranchRoles
                            (UserID, CompanyID, BranchID, RoleID, CompanyRoleID, ScopeType, IsActive)
                        VALUES (%s, %s, %s, NULL, %s, %s, TRUE)
                    """, (bad_user, ids["company"], branch_id, role_id, scope))
                assert message in str(error.value).lower()

            with pytest.raises(psycopg2.Error, match="company_owner_scope_invalid"):
                cur.execute("""
                    UPDATE sec.UserBranchRoles SET ScopeType = 'SpecificBranch', BranchID = %s
                    WHERE UserID = %s
                """, (ids["branch_a"], ids["owner_user"]))
    finally:
        conn.close()


def test_upgrade_refuses_non_driver_oda_rows(access_migration_db):
    config, dsn = access_migration_db
    _upgrade(config, "0074")
    _seed_legacy_access_state(dsn, invalid_non_driver_oda=True)
    with pytest.raises(DBAPIError, match="0075 refused: non-DRIVER OwnDriverDataOnly"):
        _upgrade(config, "head")
