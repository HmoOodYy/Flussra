"""Migration 0096: settings.manage narrows to company settings, setup.manage is retired.

Every holder keeps the reach the two hidden master keys used to give through the
specific permissions those routes now require.
"""

from __future__ import annotations

from uuid import uuid4

import psycopg2

from tests.schema_helpers import _alembic, _schema_database

_SETTINGS_MANAGE_GRANTS = {
    "users.view", "users.create", "users.edit",
    "roles.view", "roles.create", "roles.edit", "roles.delete",
    "payrates.view", "payrates.edit", "payitems.view",
}
_SETUP_MANAGE_GRANTS = _SETTINGS_MANAGE_GRANTS | {
    "payitems.edit", "settings.manage", "settings.view",
}


def _seed_company(cur) -> int:
    cur.execute(
        "INSERT INTO core.companies (companycode, companyname, status, issuspended) "
        "VALUES (%s, 'Scope Migration', 'Active', FALSE) RETURNING companyid",
        (uuid4().hex[:12],),
    )
    return cur.fetchone()[0]


def _seed_role(cur, company_id: int, role_code: str, codes: list[str]) -> int:
    cur.execute(
        "INSERT INTO sec.companyroles "
        "(companyid, rolecode, rolename, rolelevel, isdefault, isprotected, iscustom, isactive) "
        "VALUES (%s, %s, %s, 50, FALSE, FALSE, TRUE, TRUE) RETURNING companyroleid",
        (company_id, role_code, role_code),
    )
    role_id = cur.fetchone()[0]
    for code in codes:
        cur.execute(
            "INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode) VALUES (%s, %s)",
            (role_id, code),
        )
    return role_id


def _seed_user(cur, company_id: int, name: str) -> int:
    cur.execute(
        "INSERT INTO sec.users (companyid, username, displayname, passwordhash, isactive, canlogin) "
        "VALUES (%s, %s, %s, 'x', TRUE, FALSE) RETURNING userid",
        (company_id, f"{name}_{uuid4().hex[:8]}", name),
    )
    return cur.fetchone()[0]


def _seed_override(cur, company_id: int, user_id: int, code: str, granter_id: int | None) -> None:
    cur.execute(
        "INSERT INTO sec.userpermissionoverrides "
        "(userid, companyid, permissioncode, effect, isactive, grantedbyuserid) "
        "VALUES (%s, %s, %s, 'ALLOW', TRUE, %s)",
        (user_id, company_id, code, granter_id),
    )


def _role_codes(cur, role_id: int) -> set[str]:
    cur.execute(
        "SELECT permissioncode FROM sec.companyrolepermissions WHERE companyroleid = %s",
        (role_id,),
    )
    return {row[0] for row in cur.fetchall()}


def _active_override_codes(cur, user_id: int) -> set[str]:
    cur.execute(
        "SELECT permissioncode FROM sec.userpermissionoverrides "
        "WHERE userid = %s AND isactive AND effect = 'ALLOW'",
        (user_id,),
    )
    return {row[0] for row in cur.fetchall()}


def test_migration_converts_the_two_legacy_codes_into_the_specific_permissions(schema_databases):
    with _schema_database(schema_databases, "settings_scope") as (env, dsn):
        downgraded = _alembic(env, "downgrade", "0095")
        assert downgraded.returncode == 0, downgraded.stderr

        with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT permissionname FROM sec.permissions WHERE permissioncode = 'settings.manage'")
            assert cur.fetchone()[0] == "Manage Settings"
            cur.execute(
                "INSERT INTO sec.permissions (permissioncode, permissionname, modulecode) "
                "VALUES ('setup.manage', 'Manage Settings', 'settings') ON CONFLICT DO NOTHING"
            )
            company_id = _seed_company(cur)
            settings_role = _seed_role(cur, company_id, "SETTINGS_ROLE", ["settings.manage"])
            setup_role = _seed_role(cur, company_id, "SETUP_ROLE", ["setup.manage", "payroll.view"])
            specific_role = _seed_role(cur, company_id, "SPECIFIC_ROLE", ["users.view", "payroll.view"])
            driver_role = _seed_role(cur, company_id, "DRIVER", ["settings.manage", "drivers.view"])
            granter = _seed_user(cur, company_id, "granter")
            settings_member = _seed_user(cur, company_id, "settings_member")
            setup_member = _seed_user(cur, company_id, "setup_member")
            specific_member = _seed_user(cur, company_id, "specific_member")
            _seed_override(cur, company_id, settings_member, "settings.manage", granter)
            _seed_override(cur, company_id, setup_member, "setup.manage", granter)
            _seed_override(cur, company_id, setup_member, "users.view", granter)
            _seed_override(cur, company_id, specific_member, "users.view", granter)
            conn.commit()

        upgraded = _alembic(env, "upgrade", "0096")
        assert upgraded.returncode == 0, upgraded.stderr

        with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
            # settings.manage keeps its row and gains what it used to stand in for.
            assert _role_codes(cur, settings_role) == _SETTINGS_MANAGE_GRANTS | {"settings.manage"}
            # setup.manage is converted: its row goes, company settings and the specific codes arrive.
            assert _role_codes(cur, setup_role) == _SETUP_MANAGE_GRANTS | {"payroll.view"}
            # Roles that never held either code are untouched.
            assert _role_codes(cur, specific_role) == {"users.view", "payroll.view"}
            # Generic permissions are never effective for a DRIVER role, so none are added.
            assert _role_codes(cur, driver_role) == {"settings.manage", "drivers.view"}

            assert _active_override_codes(cur, settings_member) == _SETTINGS_MANAGE_GRANTS | {"settings.manage"}
            # setup.manage override is revoked, not deleted, and the existing override is not duplicated.
            assert _active_override_codes(cur, setup_member) == _SETUP_MANAGE_GRANTS
            cur.execute(
                "SELECT isactive, revokedatutc IS NOT NULL FROM sec.userpermissionoverrides "
                "WHERE userid = %s AND permissioncode = 'setup.manage'", (setup_member,),
            )
            assert cur.fetchall() == [(False, True)]
            cur.execute(
                "SELECT count(*) FROM sec.userpermissionoverrides "
                "WHERE userid = %s AND permissioncode = 'users.view'", (setup_member,),
            )
            assert cur.fetchone()[0] == 1
            assert _active_override_codes(cur, specific_member) == {"users.view"}

            # Converted overrides keep their original granter.
            cur.execute(
                "SELECT DISTINCT grantedbyuserid FROM sec.userpermissionoverrides "
                "WHERE userid = %s AND isactive AND permissioncode = 'payrates.edit'", (setup_member,),
            )
            assert cur.fetchall() == [(granter,)]

            cur.execute("SELECT 1 FROM sec.permissions WHERE permissioncode = 'setup.manage'")
            assert cur.fetchone() is None
            cur.execute("SELECT 1 FROM sec.companyrolepermissions WHERE permissioncode = 'setup.manage'")
            assert cur.fetchone() is None
            cur.execute("SELECT permissionname FROM sec.permissions WHERE permissioncode = 'settings.manage'")
            assert cur.fetchone()[0] == "Manage Company Settings"


def test_migration_is_repeatable_and_downgrade_keeps_granted_permissions(schema_databases):
    with _schema_database(schema_databases, "settings_scope_repeat") as (env, dsn):
        downgraded = _alembic(env, "downgrade", "0095")
        assert downgraded.returncode == 0, downgraded.stderr

        with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
            company_id = _seed_company(cur)
            role_id = _seed_role(cur, company_id, "SETTINGS_ROLE", ["settings.manage"])
            conn.commit()

        assert _alembic(env, "upgrade", "0096").returncode == 0
        assert _alembic(env, "downgrade", "0095").returncode == 0
        with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
            assert _role_codes(cur, role_id) == _SETTINGS_MANAGE_GRANTS | {"settings.manage"}
            cur.execute("SELECT permissionname FROM sec.permissions WHERE permissioncode = 'settings.manage'")
            assert cur.fetchone()[0] == "Manage Settings"

        again = _alembic(env, "upgrade", "0096")
        assert again.returncode == 0, again.stderr
        with psycopg2.connect(**dsn) as conn, conn.cursor() as cur:
            assert _role_codes(cur, role_id) == _SETTINGS_MANAGE_GRANTS | {"settings.manage"}
