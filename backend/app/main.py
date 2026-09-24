"""
FastAPI application factory.

Responsibilities:
  - Create the FastAPI app instance with metadata
  - Register all domain routers
  - Manage the database engine lifecycle via lifespan
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.admin.router import router as admin_router
from app.auth.router import router as auth_router
from app.cdpi.router import router as cdpi_router
from app.config import settings
from app.core.router import router as core_router
from app.dashboard.router import router as dashboard_router
from app.db.schema_guard import run_schema_guard
from app.db.session import dispose_engine
from app.payroll.router import router as payroll_router
from app.review.router import router as review_router
from app.settings.router import router as settings_router
from app.transfer.router import router as transfer_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan: runs startup code before yield,
    shutdown code after yield.

    Startup: run the schema guard (dev: aborts if DB is behind head),
             then let the engine pool warm lazily on first use.
    Shutdown: dispose the connection pool cleanly.
    """
    # Run synchronously before accepting requests.
    # In development: raises RuntimeError (crashes the process with a clear message)
    # so a missing migration never silently causes a 500 on the first login.
    import asyncio
    await asyncio.get_event_loop().run_in_executor(
        None,
        run_schema_guard,
        settings.DATABASE_URL,
        settings.is_dev,
    )
    yield
    await dispose_engine()


app = FastAPI(
    title="Payroll App V3 API",
    version="0.1.0",
    description=(
        "Backend API for Payroll App V3. "
        "Handles authentication, payroll periods, driver rates, "
        "approvals, and settings administration."
    ),
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS
#
# Development: explicit local origins + a regex that matches any
#   *.trycloudflare.com origin (Quick Tunnel URLs change every session so
#   we cannot whitelist them by name).
#
# Production: must be set explicitly — no wildcard, no trycloudflare.
#   Set ALLOWED_ORIGINS in your production environment / settings.
# ---------------------------------------------------------------------------
DEV_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://192.168.100.2:5173",
    "http://192.168.100.2:5174",
    "http://192.168.100.2:5175",
]

# Matches any Cloudflare Quick Tunnel frontend origin — dev only.
# Quick Tunnel URLs rotate every session; regex avoids manual config edits.
_DEV_TUNNEL_REGEX = r"^https://[a-z0-9-]+\.trycloudflare\.com$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=DEV_CORS_ORIGINS if settings.is_dev else [],
    allow_origin_regex=_DEV_TUNNEL_REGEX if settings.is_dev else None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(auth_router,      prefix="/auth",      tags=["Auth"])
app.include_router(core_router,      prefix="/core",      tags=["Core"])
app.include_router(payroll_router,   prefix="/payroll",   tags=["Payroll"])
app.include_router(review_router,    prefix="/review",    tags=["Review"])
app.include_router(settings_router,  prefix="/settings",  tags=["Settings"])
app.include_router(admin_router,     prefix="/admin",     tags=["Admin"])
app.include_router(dashboard_router, prefix="/dashboard", tags=["Dashboard"])
app.include_router(transfer_router,  prefix="/driver-transfers", tags=["DriverTransfer"])
app.include_router(cdpi_router,      prefix="/settings/cdpi",    tags=["CDPI"])
