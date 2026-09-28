"""Route-surface assertions for the physically retired Phase 3 endpoints."""

from app.payroll import router as payroll_router
from app.settings import router as settings_router


def _route_methods(router) -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in router.router.routes
        for method in (route.methods or ())
    }


def test_legacy_settings_payroll_setup_routes_are_not_registered():
    methods = _route_methods(settings_router)
    assert ("GET", "/branches/{branch_id}/payroll-setup") not in methods
    assert ("PUT", "/branches/{branch_id}/payroll-setup") not in methods


def test_legacy_period_creation_routes_are_not_registered():
    methods = _route_methods(payroll_router)
    assert ("GET", "/periods/next-period-dates") not in methods
    assert ("POST", "/periods") not in methods
