import psycopg2
from fastapi.routing import APIRoute

from app.access.route_inventory import (
    ROUTE_AUTHORIZATION,
    RouteAuthorization,
    inventory_errors,
)
from app.main import create_app


def _application_routes(router, prefix: str = "") -> set[str]:
    """Flatten FastAPI's deferred router includes into mounted APIRoutes.

    Recent FastAPI versions keep included routers as internal route entries
    until OpenAPI generation. Walk those router includes structurally so the
    inventory test still checks the actual application route table.
    """
    actual: set[str] = set()
    for route in router.routes:
        if isinstance(route, APIRoute):
            actual.update(
                f"{method.upper()} {prefix}{route.path}"
                for method in route.methods or ()
            )
        elif hasattr(route, "original_router") and hasattr(route, "include_context"):
            actual.update(
                _application_routes(
                    route.original_router,
                    prefix + route.include_context.prefix,
                )
            )
    return actual


def test_every_application_route_has_one_authorization_inventory_entry():
    app = create_app()
    actual = _application_routes(app.router)
    registered = set(ROUTE_AUTHORIZATION)

    assert actual == registered
    assert inventory_errors(ROUTE_AUTHORIZATION, actual) == []
    assert ROUTE_AUTHORIZATION["POST /auth/login"].authentication == "PUBLIC"
    assert all(
        item.authentication == "AUTHENTICATED"
        for key, item in ROUTE_AUTHORIZATION.items()
        if key != "POST /auth/login"
    )


def test_driver_self_disposition_is_limited_to_transfer_create():
    allowed = {
        key for key, item in ROUTE_AUTHORIZATION.items()
        if item.driver_self == "ALLOW_OWN"
    }
    assert allowed == {"POST /driver-transfers"}
    transfer = ROUTE_AUTHORIZATION["POST /driver-transfers"]
    assert transfer.permission_mode == "SPECIAL_POLICY"
    assert transfer.permission_codes == ()
    assert transfer.special_policy == "app.transfer.service.create_driver_transfer_request"
    assert transfer.resource_scope == "RESOURCE_SPECIFIC"
    assert transfer.ownership_policy == "app.access.policy.resolve_driver_self_profile"


def test_inventory_rejects_missing_auth_policy_classification():
    malformed = RouteAuthorization(
        "AUTHENTICATED", "", (), None, "COMPANY", "DENY", None, None,
    )
    assert "missing or invalid permission/action classification" in " ".join(
        inventory_errors({"GET /example": malformed})
    )


def test_inventory_rejects_allow_own_without_ownership_policy():
    malformed = RouteAuthorization(
        "AUTHENTICATED", "SPECIAL_POLICY", (), "app.access.policy.resolve_driver_self_profile",
        "SELF", "ALLOW_OWN", None, None,
    )
    assert "ALLOW_OWN requires ownership policy" in " ".join(
        inventory_errors({"POST /example": malformed})
    )


def test_inventory_rejects_self_resource_with_inconsistent_driver_disposition():
    malformed = RouteAuthorization(
        "AUTHENTICATED", "NONE", (), None, "SELF", "DENY", None,
        "No additional permission is defined.",
    )
    assert "SELF resource scope requires ALLOW_OWN disposition" in " ".join(
        inventory_errors({"GET /example": malformed})
    )


def test_inventory_rejects_stale_registry_routes():
    valid = RouteAuthorization(
        "PUBLIC", "NONE", (), None, "NONE", "NOT_APPLICABLE", None,
        "Public endpoint with no additional permission.",
    )
    assert "stale registry route: GET /old" in inventory_errors(
        {"GET /old": valid}, {"GET /new"},
    )


def test_inventory_permissions_exist_in_catalog(apply_schema):
    conn = psycopg2.connect(client_encoding="utf-8", **apply_schema.dsn())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT permissioncode FROM sec.permissions")
            catalog = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()
    recorded = {code for item in ROUTE_AUTHORIZATION.values() for code in item.permission_codes}
    assert recorded <= catalog, f"unknown permission codes: {sorted(recorded - catalog)}"


def test_inventory_records_match_tricky_route_authority():
    expected = {
        "GET /admin/company-roles/{role_id}/permissions": ("ANY_OF", ("roles.view", "settings.manage", "setup.manage")),
        "GET /admin/users/{user_id}/permission-overrides": ("ANY_OF", ("users.view", "settings.manage", "setup.manage")),
        "GET /admin/users/{user_id}/roles": ("ANY_OF", ("users.view", "settings.manage", "setup.manage")),
        "PUT /admin/users/{user_id}/permission-overrides": ("ANY_OF", ("users.edit", "settings.manage", "setup.manage")),
        "GET /admin/users/{user_id}/driver": ("ANY_OF", ("users.view", "payrates.view", "payrates.edit", "settings.manage", "setup.manage")),
        "GET /core/branches": ("NONE", ()),
        "GET /core/people": ("PERMISSION", ("employees.view",)),
        "GET /driver-transfers": ("ANY_OF", ("drivers.view", "drivers.edit")),
        "GET /driver-transfers/{transfer_request_id}": ("ANY_OF", ("drivers.view", "drivers.edit")),
        "GET /payroll-setup/branches/{branch_id}/history": ("PERMISSION", ("payroll.view",)),
        "POST /payroll-setup/setups/{setup_id}/publish": ("SPECIAL_POLICY", ()),
        "GET /payroll/periods/{period_id}/bonuses": ("PERMISSION", ("payroll.view",)),
        "GET /review/items": ("ANY_OF", ("payroll.view", "payroll.entry", "payroll.finalize", "review.decide")),
        "GET /review/items/{item_id}": ("ANY_OF", ("payroll.view", "payroll.entry", "payroll.finalize", "review.decide")),
        "POST /review/items/{item_id}/decide": ("PERMISSION", ("review.decide",)),
        "PATCH /payroll/periods/{period_id}/status": ("SPECIAL_POLICY", ()),
    }
    for route, (mode, codes) in expected.items():
        item = ROUTE_AUTHORIZATION[route]
        assert (item.permission_mode, item.permission_codes) == (mode, codes)

    branch_list = ROUTE_AUTHORIZATION["GET /core/branches"]
    assert branch_list.special_policy == "app.core.service._check_branch_access"
    assert branch_list.none_reason
    assert branch_list.driver_self == "DENY"


def test_inventory_rejects_special_policy_without_explicit_identifier():
    malformed = RouteAuthorization(
        "AUTHENTICATED", "SPECIAL_POLICY", (), None, "COMPANY", "DENY", None, None,
    )
    assert "SPECIAL_POLICY requires an identifier" in " ".join(
        inventory_errors({"POST /example": malformed})
    )


def test_inventory_allows_resource_specific_allow_own_with_ownership_policy():
    valid = RouteAuthorization(
        "AUTHENTICATED", "SPECIAL_POLICY", (), "app.policy.example",
        "RESOURCE_SPECIFIC", "ALLOW_OWN", "app.policy.ownership", None,
    )
    assert inventory_errors({"POST /example": valid}) == []


def test_core_compatibility_routes_report_delegated_workforce_authority():
    expected = {
        "POST /core/drivers": ("employees.manage", "BRANCH"),
        "PATCH /core/drivers/{driver_id}": ("employees.manage", "RESOURCE_SPECIFIC"),
        "GET /core/drivers": ("drivers.view", "BRANCH"),
        "GET /core/people": ("employees.view", "BRANCH"),
    }
    for route, (permission_code, resource_scope) in expected.items():
        item = ROUTE_AUTHORIZATION[route]
        assert item.permission_mode == "PERMISSION"
        assert item.permission_codes == (permission_code,)
        assert item.resource_scope == resource_scope
