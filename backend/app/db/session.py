"""Application-owned async database engine factory and disposal boundary."""
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def create_engine(database_url: str, *, echo: bool) -> AsyncEngine:
    """Create the async pool for one FastAPI application instance."""
    return create_async_engine(
        database_url,
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=echo,
    )


async def dispose_engine(engine: AsyncEngine) -> None:
    """Dispose the engine owned by the application that is shutting down."""
    await engine.dispose()
