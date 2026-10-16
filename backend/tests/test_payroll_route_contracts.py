import importlib

from fastapi.routing import APIRoute
from starlette.routing import Match

from app.payroll.router import router


def _first_full_match(path: str, method: str) -> APIRoute:
    scope = {
        "type": "http",
        "path": path,
        "root_path": "",
        "method": method,
        "headers": [],
        "query_string": b"",
    }
    for route in router.routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route
    raise AssertionError(f"no route matched {method} {path}")


def test_public_payroll_router_is_flat_and_handlers_live_in_owner_modules():
    assert len(router.routes) == 61
    assert all(isinstance(route, APIRoute) for route in router.routes)
    assert router.routes[0].path == "/finalized"
    assert router.routes[0].name == "list_finalized_periods"

    for route in router.routes:
        owner = importlib.import_module(route.endpoint.__module__)
        assert getattr(owner, route.endpoint.__name__) is route.endpoint
        assert route.endpoint.__module__.startswith("app.payroll.routes.")


def test_static_payroll_paths_resolve_to_their_static_handlers():
    lookup = _first_full_match("/rates/lookup", "GET")
    candidates = _first_full_match("/branches/5/period-candidates", "GET")

    assert lookup.path == "/rates/lookup"
    assert lookup.name == "lookup_rate_for_date"
    assert candidates.path == "/branches/{branch_id}/period-candidates"
    assert candidates.name == "get_period_candidates"
