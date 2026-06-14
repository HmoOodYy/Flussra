"""
Alembic migration environment.

Uses SQLAlchemy async engine so the same asyncpg driver is used
for both the application and migrations — no second driver required.

Run from Payroll_App_v3/ (the project root):
    alembic upgrade head
    alembic downgrade base
    alembic revision --autogenerate -m "description"  (not useful without ORM models)
    alembic history
"""
import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

# ---------------------------------------------------------------------------
# Add backend/ to sys.path so we can import from app.*
# ---------------------------------------------------------------------------
_backend_dir = Path(__file__).parent.parent / "backend"
sys.path.insert(0, str(_backend_dir))

# ---------------------------------------------------------------------------
# Load .env from backend/ before importing settings
# ---------------------------------------------------------------------------
_env_file = _backend_dir / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file)

from app.config import settings  # noqa: E402 — must come after sys.path insert

# ---------------------------------------------------------------------------
# Alembic boilerplate
# ---------------------------------------------------------------------------
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# We use raw SQL migrations (no ORM metadata), so target_metadata is None.
# Alembic won't autogenerate diffs — migration files are written manually.
target_metadata = None

# Override the URL from settings (ignores the blank sqlalchemy.url in alembic.ini)
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)


# ---------------------------------------------------------------------------
# Migration runners
# ---------------------------------------------------------------------------

def do_run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Store Alembic's version table in the public schema
        version_table_schema="public",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """
    Offline mode: emit SQL to stdout without a live connection.
    Useful for generating a SQL script to review before applying.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """
    Online mode: connect to the database and run migrations.
    Uses the same asyncpg driver as the FastAPI application.
    """
    connectable = create_async_engine(
        config.get_main_option("sqlalchemy.url"),
        echo=False,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
