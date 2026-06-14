"""
Database connection pool.

Uses SQLAlchemy 2.0 async Core with the asyncpg driver.
No ORM models — all queries are written as raw parameterised SQL
via sqlalchemy.text().

The engine is created once at application startup (via FastAPI lifespan)
and disposed at shutdown. Every request gets a connection from the pool
via the get_db() FastAPI dependency.
"""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncConnection, AsyncEngine
from app.config import settings

# ---------------------------------------------------------------------------
# Engine — created at module import time, reused for the lifetime of the app
# ---------------------------------------------------------------------------

engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL,
    # Pool settings — sensible defaults for a single-user payroll app
    pool_size=10,           # max persistent connections in pool
    max_overflow=5,         # extra connections allowed above pool_size
    pool_pre_ping=True,     # test connection aliveness before use (handles idle disconnects)
    pool_recycle=1800,      # recycle connections older than 30 min
    echo=settings.is_dev,   # log SQL to stdout in development
)


async def dispose_engine() -> None:
    """Called at app shutdown to cleanly close all pool connections."""
    await engine.dispose()
