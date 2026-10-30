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
    assert transfer.ownership_policy == "app.access.policy.resolve_driver_self"


def test_inventory_rejects_missing_auth_policy_classification():
    malformed = RouteAuthorization(
        "AUTHENTICATED", "", (), None, "COMPANY", "DENY", None, None,
    )
    assert "missing or invalid permission/action classification" in " ".join(
        inventory_errors({"GET /example": malformed})
    )


def test_inventory_rejects_allow_own_without_ownership_policy():
    malformed = RouteAuthorization(
        "AUTHENTICATED", "SPECIAL_POLICY", (), "app.access.policy.resolve_driver_self",
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
        "GET /admin/company-roles/{role_id}/permissions": ("PERMISSION", ("roles.view",)),
        "POST /admin/company-roles": ("PERMISSION", ("roles.create",)),
        "DELETE /admin/company-roles/{role_id}": ("PERMISSION", ("roles.delete",)),
        "GET /admin/users/{user_id}/permission-overrides": ("PERMISSION", ("users.view",)),
        "PUT /admin/users/{user_id}/permission-overrides": ("PERMISSION", ("users.edit",)),
        "POST /admin/users/{user_id}/company-role-assignments": ("ANY_OF", ("users.edit", "roles.edit")),
        "PATCH /settings/company": ("PERMISSION", ("settings.manage",)),
        "GET /core/branches": ("NONE", ()),
        "GET /core/driver-assignments": ("PERMISSION", ("drivers.view",)),
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
    assert branch_list.special_policy == "app.access.branch_access.check_branch_access"
    assert branch_list.none_reason
    assert branch_list.driver_self == "DENY"


COMPANY_SETTINGS_ROUTES = {
    "PATCH /settings/branches/{branch_id}",
    "POST /settings/branches/{branch_id}/set-default",
    "PUT /settings/branches/{branch_id}/status-keys/{key_id}/limits",
    "POST /settings/status-keys",
    "PATCH /settings/status-keys/{key_id}",
    "DELETE /settings/status-keys/{key_id}",
    "POST /settings/status-rate-columns",
    "PATCH /settings/status-rate-columns/{column_id}",
    "DELETE /settings/status-rate-columns/{column_id}",
    "GET /settings/currencies",
    "PATCH /settings/company",
}


def test_settings_manage_authorizes_only_company_settings_routes():
    holders = {
        route for route, item in ROUTE_AUTHORIZATION.items()
        if "settings.manage" in item.permission_codes
    }
    assert holders == COMPANY_SETTINGS_ROUTES
    for route in COMPANY_SETTINGS_ROUTES:
        assert ROUTE_AUTHORIZATION[route].permission_codes == ("settings.manage",)


def test_setup_manage_authorizes_no_route():
    holders = [
        route for route, item in ROUTE_AUTHORIZATION.items()
        if "setup.manage" in item.permission_codes
    ]
    assert holders == []


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


def test_p8a_compatibility_routes_are_not_mounted_and_target_roster_remains():
    actual = _application_routes(create_app().router)
    retired = {
        "GET /core/people",
        "POST /core/drivers",
        "PATCH /core/drivers/{driver_id}",
        "GET /core/drivers",
        "GET /core/drivers/{driver_id}",
        "GET /core/drivers/{driver_id}/assignments",
        "GET /admin/users/{user_id}/driver",
        "GET /admin/roles",
        "GET /admin/users/{user_id}/roles",
        "POST /admin/users/{user_id}/roles",
        "DELETE /admin/users/{user_id}/roles/{assignment_id}",
        "GET /payroll/periods/{period_id}/final-lines",
        "GET /payroll/periods/{period_id}/drivers-off",
    }
    assert retired.isdisjoint(actual)
    assert "GET /core/branches" in actual
    assert "GET /core/driver-assignments" in actual
    assert "GET /payroll/finalized/{period_id}/overview" in actual
    assert "GET /payroll/finalized/{period_id}/off-drivers" in actual
