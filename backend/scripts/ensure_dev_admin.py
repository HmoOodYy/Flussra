"""
Local-dev idempotent seed script.
Creates or repairs the DEMO company (with its default Status rate column), Headquarters
branch, admin user, and the CompanyRole assignment linking the admin to COMPANY_OWNER.

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

from app.auth.security import hash_password
from app.config import get_settings

# ── Target credentials ────────────────────────────────────────────────────────
COMPANY_CODE = "DEMO"
COMPANY_NAME = "Demo Logistics"
BRANCH_CODE  = "HQ"
BRANCH_NAME  = "Headquarters"
BRANCH_TIME_ZONE_ID = "America/New_York"
USERNAME     = "admin"
DISPLAY_NAME = "Admin User"
PASSWORD     = "TestPass123!"
async def main() -> None:
    settings = get_settings()
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

        # Every Company starts with its one default Status rate column (and the rate
        # definition it owns). Idempotent.
        status_column_id = await conn.fetchval(
            "SELECT payroll.fn_EnsureDefaultStatusRateColumn($1)", company_id,
        )
        print(f"[OK] Status rate column id={status_column_id}  (Company default)")

        # ── 2. Branch ─────────────────────────────────────────────────────────
        branch_id = await conn.fetchval(
            """
            INSERT INTO core.branches
                (companyid, branchcode, branchname, status, isdefault, timezoneid)
            VALUES ($1, $2, $3, 'Active', TRUE, $4)
            ON CONFLICT (companyid, branchcode) DO UPDATE
                SET branchname   = EXCLUDED.branchname,
                    status       = 'Active',
                    isdefault    = TRUE,
                    updatedatutc = NOW()
            RETURNING branchid
            """,
            company_id, BRANCH_CODE, BRANCH_NAME, BRANCH_TIME_ZONE_ID,
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

        # ── 4. Company roles ──────────────────────────────────────────────────

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

        # ── 5. CompanyRole assignment (AllCompanyBranches) ────────────────────
        existing = await conn.fetchval(
            """
            SELECT userbranchroleid
              FROM sec.userbranchroles
             WHERE userid = $1
               AND companyid = $2
               AND companyroleId = $3
               AND scopetype = 'AllCompanyBranches'
            """,
            user_id, company_id, co_role_id,
        )

        if existing is None:
            await conn.execute(
                """
                INSERT INTO sec.userbranchroles
                    (userid, companyid, branchid, companyroleId, scopetype, isactive)
                VALUES ($1, $2, NULL, $3, 'AllCompanyBranches', TRUE)
                """,
                user_id, company_id, co_role_id,
            )
            print(f"[OK] CompanyRole assigned  COMPANY_OWNER -> {USERNAME}  scope=AllCompanyBranches")
        else:
            await conn.execute(
                """
                UPDATE sec.userbranchroles
                   SET isactive = TRUE, revokedatutc = NULL, companyroleId = $2
                 WHERE userbranchroleid = $1
                """,
                existing, co_role_id,
            )
            print(f"[OK] CompanyRole confirmed  COMPANY_OWNER -> {USERNAME}  scope=AllCompanyBranches")
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
