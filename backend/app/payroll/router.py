"""
Payroll route composition; responsibility-owned handlers live in app.payroll.routes.

All endpoints require a valid JWT.
Company and user identity come from the token; branch-scoping is enforced
inside the service layer.
"""

from fastapi import APIRouter

from .routes import bonus, calculation, entry, finalized, pay_rules, periods, reporting

# Preserve the original flat route sequence; payroll families occupy noncontiguous segments.
router = APIRouter(
    routes=[
        *finalized.list_router.routes,
        *periods.router.routes,
        *entry.lines_router.routes,
        *calculation.router.routes,
        *reporting.router.routes,
        *finalized.router.routes,
        *bonus.router.routes,
        *pay_rules.router.routes,
        *entry.off_drivers_router.routes,
        *entry.day_grid_router.routes,
        *periods.creation_router.routes,
        *periods.workflow_router.routes,
    ],
)
