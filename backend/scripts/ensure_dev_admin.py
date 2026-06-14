"""
Local-dev idempotent seed script.
Creates or repairs the DEMO company, Headquarters branch, admin user,
legacy PAYROLL_ADMIN global role (for internal auth compat), COMPANY_OWNER
company role, and the role assignment linking the admin to COMPANY_OWNER.

Safe to run multiple times — every step uses INSERT ... ON CONFLICT DO NOTHING
or ON CONFLICT DO UPDATE (upsert) so nothing is duplicated.

Usage (from the backend\ directory):
    .\.venv\Scripts\python.exe scripts\ensure_dev_admin.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg
from app.config import settings
from app.auth.security import hash_password

# ── Target credentials ────────────────────────────────────────────────────────
COMPANY_CODE = "DEMO"
COMPANY_NAME = "Demo Logistics"
BRANCH_CODE  = "HQ"
BRANCH_NAME  = "Headquarters"
USERNAME     = "admin"
DISPLAY_NAME = "Admin User"
PASSWORD     = "TestPass123!"
ROLE_CODE    = "PAYROLL_ADMIN"
ROLE_NAME    = "Payroll Admin"

# All permissions required for full frontend testing.
# Module codes must be lowercase to match the ui_only filter in list_permissions.
# Names and codes must match migration 0041 (which uses ON CONFLICT DO UPDATE
# to correct any rows this script inserts with wrong case).
PERMISSIONS = [
    ("payroll.entry",        "Enter Payroll Data",           "payroll"),
    ("payroll.finalize",     "Finalize Payroll",             "payroll"),
    ("payroll.approve_rate", "Approve Pay Rates",            "payroll"),
    ("review.decide",        "Approve / Reject Review Items","review"),
    ("setup.manage",         "Manage Settings",              "settings"),
    ("drivers.manage",       "Manage Drivers",               "core"),
]


async def main() -> None:
    dsn = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)

    try:
        # ── 1. Company ────────────────────────────────────────────────────────
        company_id = await conn.fetchval(
            """
            INSERT INTO core.companies (companycode, companyname, status, issuspended)
            VALUES ($1, $2, 'Active', FALSE)
            ON CONFLICT (companycode) DO UPDATE
                SET companyname  = EXCLUDED.companyname,
                    status       = 'Active',
                    issuspended  = FALSE,
                    updatedatutc = NOW()
            RETURNING companyid
            """,
            COMPANY_CODE, COMPANY_NAME,
        )
        print(f"[OK] Company   id={company_id}  code={COMPANY_CODE}  name={COMPANY_NAME}")

        # ── 2. Branch ─────────────────────────────────────────────────────────
        branch_id = await conn.fetchval(
            """
            INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
            VALUES ($1, $2, $3, 'Active', TRUE)
            ON CONFLICT (companyid, branchcode) DO UPDATE
                SET branchname   = EXCLUDED.branchname,
                    status       = 'Active',
                    isdefault    = TRUE,
                    updatedatutc = NOW()
            RETURNING branchid
            """,
            company_id, BRANCH_CODE, BRANCH_NAME,
        )
        print(f"[OK] Branch    id={branch_id}  code={BRANCH_CODE}  name={BRANCH_NAME}  isdefault=True")

        # ── 3. User ───────────────────────────────────────────────────────────
        pw_hash = hash_password(PASSWORD)
        user_id = await conn.fetchval(
            """
            INSERT INTO sec.users
                (companyid, username, displayname, passwordhash, isactive, canlogin)
            VALUES ($1, $2, $3, $4, TRUE, TRUE)
            ON CONFLICT DO NOTHING
            RETURNING userid
            """,
            company_id, USERNAME, DISPLAY_NAME, pw_hash,
        )

        if user_id is None:
            # Already existed — update password + ensure active/can_login
            user_id = await conn.fetchval(
                """
                UPDATE sec.users
                   SET passwordhash  = $1,
                       isactive      = TRUE,
                       canlogin      = TRUE,
                       failedlogincount = 0,
                       lockeduntilutc   = NULL,
                       updatedatutc     = NOW()
                 WHERE companyid = $2
                   AND LOWER(username) = LOWER($3)
                RETURNING userid
                """,
                pw_hash, company_id, USERNAME,
            )
        print(f"[OK] User      id={user_id}  username={USERNAME}  active=True  can_login=True")

        # ── 4. Role ───────────────────────────────────────────────────────────
        role_id = await conn.fetchval(
            """
            INSERT INTO sec.roles (rolecode, rolename, rolelevel, issystemrole)
            VALUES ($1, $2, 100, TRUE)
            ON CONFLICT (rolecode) DO UPDATE
                SET rolename = EXCLUDED.rolename
            RETURNING roleid
            """,
            ROLE_CODE, ROLE_NAME,
        )
        print(f"[OK] Role      id={role_id}  code={ROLE_CODE}")

        # ── 5. Permissions + link to role ─────────────────────────────────────
        for pcode, pname, module in PERMISSIONS:
            perm_id = await conn.fetchval(
                """
                INSERT INTO sec.permissions (permissioncode, permissionname, modulecode)
                VALUES ($1, $2, $3)
                ON CONFLICT (permissioncode) DO UPDATE
                    SET permissionname = EXCLUDED.permissionname
                RETURNING permissionid
                """,
                pcode, pname, module,
            )
            await conn.execute(
                """
                INSERT INTO sec.rolepermissions (roleid, permissionid)
                VALUES ($1, $2)
                ON CONFLICT (roleid, permissionid) DO NOTHING
                """,
                role_id, perm_id,
            )
            print(f"[OK] Permission  {pcode}")

        # ── 6. Company roles (new system) ─────────────────────────────────────
        # COMPANY_OWNER — default protected, all permissions
        co_role_id = await conn.fetchval(
            """
            INSERT INTO sec.companyroles
                (companyid, rolecode, rolename, rolelevel, isdefault, isprotected, iscustom, isactive)
            VALUES ($1, 'COMPANY_OWNER', 'Company Owner', 100, TRUE, TRUE, FALSE, TRUE)
            ON CONFLICT (companyid, rolecode) DO UPDATE
                SET rolename = EXCLUDED.rolename
            RETURNING companyroleid
            """,
            company_id,
        )
        await conn.execute(
            """
            INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
            SELECT $1, permissioncode FROM sec.permissions
            ON CONFLICT (companyroleid, permissioncode) DO NOTHING
            """,
            co_role_id,
        )
        print(f"[OK] CompanyRole  COMPANY_OWNER  id={co_role_id}")

        # DRIVER — default protected, minimal permissions
        driver_role_id = await conn.fetchval(
            """
            INSERT INTO sec.companyroles
                (companyid, rolecode, rolename, rolelevel, isdefault, isprotected, iscustom, isactive)
            VALUES ($1, 'DRIVER', 'Driver', 10, TRUE, TRUE, FALSE, TRUE)
            ON CONFLICT (companyid, rolecode) DO UPDATE
                SET rolename = EXCLUDED.rolename
            RETURNING companyroleid
            """,
            company_id,
        )
        await conn.execute(
            """
            INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
            VALUES ($1, 'drivers.view')
            ON CONFLICT (companyroleid, permissioncode) DO NOTHING
            """,
            driver_role_id,
        )
        print(f"[OK] CompanyRole  DRIVER         id={driver_role_id}")

        # Remove any lingering auto-mirrored legacy company roles (from migration 0016).
        # These are custom roles whose rolecode matches a global sec.roles entry.
        await conn.execute(
            """
            UPDATE sec.userbranchroles
            SET    companyroleId = NULL
            WHERE  companyroleId IN (
                SELECT companyroleid FROM sec.companyroles
                WHERE  companyid    = $1
                  AND  iscustom     = TRUE
                  AND  isprotected  = FALSE
                  AND  isdefault    = FALSE
                  AND  rolecode IN (SELECT rolecode FROM sec.roles)
            )
            """,
            company_id,
        )
        deleted = await conn.fetchval(
            """
            WITH deleted AS (
                DELETE FROM sec.companyroles
                WHERE  companyid    = $1
                  AND  iscustom     = TRUE
                  AND  isprotected  = FALSE
                  AND  isdefault    = FALSE
                  AND  rolecode IN (SELECT rolecode FROM sec.roles)
                RETURNING companyroleid
            )
            SELECT COUNT(*) FROM deleted
            """,
            company_id,
        )
        if deleted:
            print(f"[OK] Removed {deleted} auto-mirrored legacy role(s) from CompanyRoles")

        # ── 7. Role assignment (AllCompanyBranches) — both old and new path ──
        # Admin user is assigned the legacy PAYROLL_ADMIN global role (for backward compat)
        # AND the COMPANY_OWNER company role (for the new permissions system).
        existing = await conn.fetchval(
            """
            SELECT userbranchroleid
              FROM sec.userbranchroles
             WHERE userid    = $1
               AND companyid = $2
               AND roleid    = $3
               AND scopetype = 'AllCompanyBranches'
            """,
            user_id, company_id, role_id,
        )

        if existing is None:
            await conn.execute(
                """
                INSERT INTO sec.userbranchroles
                    (userid, companyid, branchid, roleid, companyroleId, scopetype, isactive)
                VALUES ($1, $2, NULL, $3, $4, 'AllCompanyBranches', TRUE)
                """,
                user_id, company_id, role_id, co_role_id,
            )
            print(f"[OK] Role assigned  {ROLE_CODE} -> {USERNAME}  scope=AllCompanyBranches  companyRole=COMPANY_OWNER")
        else:
            await conn.execute(
                """
                UPDATE sec.userbranchroles
                   SET isactive = TRUE, revokedatutc = NULL, companyroleId = $2
                 WHERE userbranchroleid = $1
                """,
                existing, co_role_id,
            )
            print(f"[OK] Role confirmed  {ROLE_CODE} -> {USERNAME}  scope=AllCompanyBranches  companyRole=COMPANY_OWNER")

        # ── Summary ───────────────────────────────────────────────────────────
        print()
        print("=" * 50)
        print("Dev admin ready. Login credentials:")
        print(f"  Company Code : {COMPANY_CODE}")
        print(f"  Username     : {USERNAME}")
        print(f"  Password     : {PASSWORD}")
        print("=" * 50)

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
