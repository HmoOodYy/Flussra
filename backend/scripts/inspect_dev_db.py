"""
Dev-only read-only inspection script.
Prints companies, users, and user roles from the configured dev database.
Makes no writes whatsoever.

Usage (from the backend\ directory):
    .\.venv\Scripts\python.exe scripts\inspect_dev_db.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg

from app.config import settings


async def main() -> None:
    dsn = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)

    try:
        # ── 1. Companies ──────────────────────────────────────────────────────
        companies = await conn.fetch(
            """
            SELECT companyid, companycode, companyname, status, issuspended
              FROM core.companies
             ORDER BY companyid
            """
        )
        print("=" * 60)
        print("COMPANIES")
        print("=" * 60)
        if companies:
            for r in companies:
                print(
                    f"  id={r['companyid']:<4} code={r['companycode']:<12} "
                    f"name={r['companyname']:<30} status={r['status']}  "
                    f"suspended={r['issuspended']}"
                )
        else:
            print("  (none)")

        # ── 2. Users ──────────────────────────────────────────────────────────
        users = await conn.fetch(
            """
            SELECT u.userid, u.username, u.displayname,
                   u.companyid, c.companycode,
                   u.isactive, u.canlogin,
                   u.failedlogincount, u.lockeduntilutc
              FROM sec.users u
              LEFT JOIN core.companies c ON c.companyid = u.companyid
             ORDER BY u.companyid NULLS LAST, u.userid
            """
        )
        print()
        print("=" * 60)
        print("USERS")
        print("=" * 60)
        if users:
            for r in users:
                locked = f"locked_until={r['lockeduntilutc']}" if r['lockeduntilutc'] else ""
                print(
                    f"  id={r['userid']:<4} username={r['username']:<20} "
                    f"display={r['displayname']:<25} company_id={str(r['companyid']):<6} "
                    f"code={str(r['companycode']):<12} active={r['isactive']}  "
                    f"can_login={r['canlogin']}  failed_logins={r['failedlogincount']}  {locked}"
                )
        else:
            print("  (none)")

        # ── 3. User roles ─────────────────────────────────────────────────────
        roles = await conn.fetch(
            """
            SELECT u.username,
                   r.rolecode, r.rolename,
                   ubr.scopetype, ubr.branchid,
                   b.branchname
              FROM sec.userbranchroles ubr
              JOIN sec.users    u ON u.userid   = ubr.userid
              JOIN sec.roles    r ON r.roleid   = ubr.roleid
              LEFT JOIN core.branches b ON b.branchid = ubr.branchid
             ORDER BY u.username, r.rolecode
            """
        )
        print()
        print("=" * 60)
        print("USER ROLES")
        print("=" * 60)
        if roles:
            for r in roles:
                branch = f"branch_id={r['branchid']} ({r['branchname']})" if r['branchid'] else "branch=ALL"
                print(
                    f"  username={r['username']:<20} role={r['rolecode']:<20} "
                    f"scope={r['scopetype']:<22} {branch}"
                )
        else:
            print("  (none)")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
