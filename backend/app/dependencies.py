"""
Shared FastAPI dependencies.

Every route that needs a DB connection or the current authenticated user
injects these via Depends(). Nothing else in the codebase imports from
here except routers.
"""
from collections.abc import AsyncGenerator

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.auth.security import decode_token

# Bearer token extractor — requires "Authorization: Bearer <token>" header
_bearer = HTTPBearer()


# ---------------------------------------------------------------------------
# Database connection dependency
# ---------------------------------------------------------------------------

def get_engine(request: Request) -> AsyncEngine:
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise RuntimeError("Database engine is not available outside the application lifespan.")
    return engine


async def get_db(request: Request) -> AsyncGenerator[AsyncConnection, None]:
    """
    Yield an async database connection from the shared pool.

    Uses engine.begin() which:
    - Opens a transaction automatically
    - Commits on successful yield exit
    - Rolls back on exception
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise RuntimeError("Database engine is not available outside the application lifespan.")

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
