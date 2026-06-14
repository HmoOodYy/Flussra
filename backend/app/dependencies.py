"""
Shared FastAPI dependencies.

Every route that needs a DB connection or the current authenticated user
injects these via Depends(). Nothing else in the codebase imports from
here except routers.
"""
from typing import AsyncGenerator

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.session import engine
from app.auth.security import decode_token

# Bearer token extractor — requires "Authorization: Bearer <token>" header
_bearer = HTTPBearer()


# ---------------------------------------------------------------------------
# Database connection dependency
# ---------------------------------------------------------------------------

async def get_db() -> AsyncGenerator[AsyncConnection, None]:
    """
    Yield an async database connection from the shared pool.

    Uses engine.begin() which:
    - Opens a transaction automatically
    - Commits on successful yield exit
    - Rolls back on exception
    """
    async with engine.begin() as conn:
        yield conn


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    """
    Validate the JWT in the Authorization header.

    Returns the decoded token payload dict:
        { "sub": user_id, "cid": company_id, "exp": ..., "iat": ... }

    Raises 401 if the token is missing, expired, or invalid.
    """
    return decode_token(credentials.credentials)
