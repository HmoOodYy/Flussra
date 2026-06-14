"""
Dev-only script: reset the local dev admin password to TestPass123!

Uses the same bcrypt hashing the backend uses (cost=12, via app.auth.security).
Safe to run multiple times — does a targeted UPDATE by username + company code.

Usage (from the backend\ directory):
    .\.venv\Scripts\python.exe scripts\reset_dev_password.py
"""
import asyncio
import sys
from pathlib import Path

# Make sure backend app is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg
from app.config import settings
from app.auth.security import hash_password

USERNAME = "admin"
COMPANY_CODE = "DEMO"
NEW_PASSWORD = "TestPass123!"


async def main() -> None:
    # Strip the asyncpg driver prefix so we get a plain postgres:// DSN
    dsn = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

    conn = await asyncpg.connect(dsn)
    try:
        new_hash = hash_password(NEW_PASSWORD)

        result = await conn.execute(
            """
            UPDATE sec.users u
               SET passwordhash = $1
              FROM core.companies c
             WHERE u.companyid = c.companyid
               AND LOWER(u.username)    = LOWER($2)
               AND LOWER(c.companycode) = LOWER($3)
            """,
            new_hash,
            USERNAME,
            COMPANY_CODE,
        )

        rows_updated = int(result.split()[-1])  # "UPDATE N"
        if rows_updated == 0:
            print(f"[ERROR] No user found: username='{USERNAME}' company_code='{COMPANY_CODE}'")
            print("        Check that the dev database has been seeded.")
            sys.exit(1)

        print(f"[OK] Password reset for '{USERNAME}' (company: {COMPANY_CODE})")
        print(f"     New password: {NEW_PASSWORD}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
