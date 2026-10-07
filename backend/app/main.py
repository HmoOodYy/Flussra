"""FastAPI application factory and application-owned resource lifecycle."""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.admin.router import router as admin_router
from app.auth.router import router as auth_router
from app.compensation.router import router as compensation_router
from app.config import get_settings
from app.core.router import router as core_router
from app.dashboard.router import router as dashboard_router
from app.db.schema_guard import run_schema_guard
from app.db.session import create_engine, dispose_engine
from app.payroll.router import router as payroll_router
from app.payroll_setup.errors import PolicyError
from app.payroll_setup.router import router as payroll_setup_router
from app.review.router import router as review_router
from app.settings.router import router as settings_router
from app.transfer.router import router as transfer_router
from app.workforce.router import router as workforce_router

DEV_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://192.168.100.2:5173",
    "http://192.168.100.2:5174",
    "http://192.168.100.2:5175",
]

# Quick Tunnel URLs rotate every session; regex avoids manual config edits.
_DEV_TUNNEL_REGEX = r"^https://[a-z0-9-]+\.trycloudflare\.com$"


def create_app() -> FastAPI:
    """Create the configured application; required settings are validated here."""
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = create_engine(settings.DATABASE_URL, echo=settings.is_dev)
        app.state.engine = engine
        try:
            # Keep the existing startup guard and fail before serving requests.
            await asyncio.get_running_loop().run_in_executor(
                None,
                run_schema_guard,
                settings.DATABASE_URL,
                settings.is_dev,
            )
            yield
        finally:
            try:
                await dispose_engine(engine)
            finally:
                app.state.engine = None

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
    app.state.settings = settings

    @app.exception_handler(PolicyError)
    async def payroll_setup_policy_error_handler(
        request: Request, exc: PolicyError
    ) -> JSONResponse:
        del request
        code = exc.code
        if code.endswith("_NOT_FOUND") or code == "COMPANY_NOT_FOUND":
            status_code = 404
        elif code.startswith("INVALID_"):
            status_code = 422
        else:
            status_code = 409
        return JSONResponse(
            status_code=status_code,
            content={"detail": {"code": code, "message": str(exc)}},
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_CORS_ORIGINS if settings.is_dev else [],
        allow_origin_regex=_DEV_TUNNEL_REGEX if settings.is_dev else None,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_router, prefix="/auth", tags=["Auth"])
    app.include_router(core_router, prefix="/core", tags=["Core"])
    app.include_router(payroll_router, prefix="/payroll", tags=["Payroll"])
    app.include_router(
        payroll_setup_router, prefix="/payroll-setup", tags=["Payroll Setup"]
    )
    app.include_router(review_router, prefix="/review", tags=["Review"])
    app.include_router(settings_router, prefix="/settings", tags=["Settings"])
    app.include_router(admin_router, prefix="/admin", tags=["Admin"])
    app.include_router(dashboard_router, prefix="/dashboard", tags=["Dashboard"])
    app.include_router(
        transfer_router, prefix="/driver-transfers", tags=["DriverTransfer"]
    )
    app.include_router(workforce_router, prefix="/workforce", tags=["Workforce"])
    app.include_router(compensation_router, prefix="/compensation", tags=["Compensation"])
    return app
