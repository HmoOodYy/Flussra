"""
Permission catalogue integrity tests — Step 1 alignment.

Verifies that every permission code the backend actively enforces is present in
sec.Permissions, has the correct module code, and is accessible/assignable.

Covers:
  • payroll.entry        — Enter Payroll Data           (modulecode='payroll')
  • payroll.period.create — Open/Create Payroll Period  (modulecode='payroll')
  • review.decide        — Approve/Reject Review Items  (modulecode='review')
  • Legacy codes not removed: setup.manage, settings.manage, drivers.manage,
    payroll.approve_rate

Also verifies:
  • Company Owner passes payroll.entry and review.decide backend checks
  • A custom role granted only payroll.entry can read payroll (entry ∈ _PAYROLL_READ_PERMS)
  • A custom role granted only review.decide can read review items
  • dispatch permissions untouched
  • settings.manage / setup.manage fallback behaviour unchanged
"""
import pytest
import pytest_asyncio
import httpx


# ---------------------------------------------------------------------------
# Helpers (same pattern as test_granular_auth.py)
# ---------------------------------------------------------------------------

def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _login(
    client: httpx.AsyncClient,
    username: str,
    password: str = "TestPass1234!",
    company_code: str = "DEMO",
) -> str:
    resp = await client.post("/auth/login", json={
        "username": username,
        "password": password,
        "company_code": company_code,
    })
    assert resp.status_code == 200, f"Login failed for {username!r}: {resp.text}"
    return resp.json()["access_token"]


async def _create_user(
    client: httpx.AsyncClient,
    token: str,
    username: str,
    display_name: str = "Catalog Test User",
) -> dict:
    resp = await client.post(
        "/admin/users",
        json={
            "username": username,
            "display_name": display_name,
            "password": "TestPass1234!",
            "is_active": True,
            "can_login": True,
            "must_change_password": False,
        },
        headers=_hdr(token),
    )
    assert resp.status_code == 201, f"Create user failed: {resp.text}"
    return resp.json()


async def _create_role_with_perms(
    client: httpx.AsyncClient,
    token: str,
    role_name: str,
    perms: list[str],
) -> int:
    cr = await client.post(
        "/admin/company-roles",
        json={"role_name": role_name},
        headers=_hdr(token),
    )
    assert cr.status_code == 201, f"Create role failed: {cr.text}"
    role_id = cr.json()["company_role_id"]
    if perms:
        pr = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": perms},
            headers=_hdr(token),
        )
        assert pr.status_code == 200, f"Set permissions failed: {pr.text}"
    return role_id


async def _assign_role(
    client: httpx.AsyncClient,
    token: str,
    user_id: int,
    role_id: int,
    scope: str = "AllCompanyBranches",
    branch_id: int | None = None,
) -> None:
    payload: dict = {"company_role_id": role_id, "scope_type": scope}
    if branch_id is not None:
        payload["branch_id"] = branch_id
    resp = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json=payload,
        headers=_hdr(token),
    )
    assert resp.status_code == 201, f"Assign role failed: {resp.text}"


# ---------------------------------------------------------------------------
# Catalogue presence tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCataloguePresence:
    """All enforced permission codes must exist in sec.Permissions."""

    async def test_payroll_entry_in_catalogue(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """payroll.entry must be in sec.Permissions (added by migration 0041)."""
        resp = await client.get(
            "/admin/permissions?ui_only=false", headers=_hdr(auth_token)
        )
        assert resp.status_code == 200
        codes = {p["permission_code"] for p in resp.json()}
        assert "payroll.entry" in codes, (
            "payroll.entry is enforced by the backend but was absent from the "
            "production catalogue. Migration 0041 must add it."
        )

    async def test_payroll_period_create_in_catalogue(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """payroll.period.create must be in sec.Permissions (added by migration 0030)."""
        resp = await client.get(
            "/admin/permissions?ui_only=false", headers=_hdr(auth_token)
        )
        assert resp.status_code == 200
        codes = {p["permission_code"] for p in resp.json()}
        assert "payroll.period.create" in codes, (
            "payroll.period.create must be in the catalogue (migration 0030)."
        )

    async def test_review_decide_in_catalogue(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """review.decide must be in sec.Permissions (added by migration 0041)."""
        resp = await client.get(
            "/admin/permissions?ui_only=false", headers=_hdr(auth_token)
        )
        assert resp.status_code == 200
        codes = {p["permission_code"] for p in resp.json()}
        assert "review.decide" in codes, (
            "review.decide is enforced by the backend but was absent from the "
            "production catalogue. Migration 0041 must add it."
        )

    async def test_payroll_entry_has_correct_module(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """payroll.entry must have modulecode='payroll' so it appears in the UI."""
        resp = await client.get(
            "/admin/permissions?ui_only=false", headers=_hdr(auth_token)
        )
        assert resp.status_code == 200
        perm = next(
            (p for p in resp.json() if p["permission_code"] == "payroll.entry"), None
        )
        assert perm is not None
        assert perm["module_code"] == "payroll", (
            f"payroll.entry must have module_code='payroll', got {perm['module_code']!r}. "
            "Uppercase 'Payroll' from ensure_dev_admin.py breaks the ui_only filter."
        )

    async def test_review_decide_has_correct_module(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """review.decide must have modulecode='review' (lowercase)."""
        resp = await client.get(
            "/admin/permissions?ui_only=false", headers=_hdr(auth_token)
        )
        assert resp.status_code == 200
        perm = next(
            (p for p in resp.json() if p["permission_code"] == "review.decide"), None
        )
        assert perm is not None
        assert perm["module_code"] == "review", (
            f"review.decide must have module_code='review' (lowercase), "
            f"got {perm['module_code']!r}."
        )


# ---------------------------------------------------------------------------
# UI visibility tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestUiVisibility:
    """
    payroll.entry and payroll.period.create must appear in the default
    (ui_only=true) response so they are assignable in the Roles & Permissions UI.
    review.decide uses modulecode='review' which is excluded until the UI step
    adds 'review' to the allowed-modules list.
    """

    async def test_payroll_entry_visible_in_ui_filter(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        resp = await client.get("/admin/permissions", headers=_hdr(auth_token))
        assert resp.status_code == 200
        codes = {p["permission_code"] for p in resp.json()}
        assert "payroll.entry" in codes, (
            "payroll.entry (modulecode='payroll') must appear in the default "
            "ui_only=true permission list so admins can assign it to custom roles."
        )

    async def test_payroll_period_create_visible_in_ui_filter(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        resp = await client.get("/admin/permissions", headers=_hdr(auth_token))
        assert resp.status_code == 200
        codes = {p["permission_code"] for p in resp.json()}
        assert "payroll.period.create" in codes, (
            "payroll.period.create must appear in the ui_only=true list."
        )

    async def test_review_decide_visible_in_ui_filter(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """review.decide must appear in the default ui_only=true list.
        'review' was added to the module filter so the Roles UI can assign it."""
        resp = await client.get("/admin/permissions", headers=_hdr(auth_token))
        assert resp.status_code == 200
        codes = {p["permission_code"] for p in resp.json()}
        assert "review.decide" in codes, (
            "review.decide (modulecode='review') must be visible to the Roles UI. "
            "The 'review' module was added to the ui_only filter in admin/service.py."
        )


# ---------------------------------------------------------------------------
# Legacy permission preservation tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestLegacyPermissionsNotRemoved:
    """
    Step 1 must not remove any existing permission codes.
    All legacy codes that existed before this step must still be present.
    """

    async def test_legacy_codes_still_present(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        resp = await client.get(
            "/admin/permissions?ui_only=false", headers=_hdr(auth_token)
        )
        assert resp.status_code == 200
        codes = {p["permission_code"] for p in resp.json()}

        legacy = [
            "setup.manage",
            "settings.manage",
            "drivers.manage",
            "payroll.approve_rate",
        ]
        for code in legacy:
            assert code in codes, (
                f"Legacy permission {code!r} must not be removed — "
                "it may still be used by existing role assignments or backend fallbacks."
            )

    async def test_dispatch_permissions_untouched(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """dispatch.view and dispatch.edit must remain in the catalogue (Step 4 scope)."""
        resp = await client.get(
            "/admin/permissions?ui_only=false", headers=_hdr(auth_token)
        )
        assert resp.status_code == 200
        codes = {p["permission_code"] for p in resp.json()}
        assert "dispatch.view" in codes, "dispatch.view must not be removed in Step 1"
        assert "dispatch.edit" in codes, "dispatch.edit must not be removed in Step 1"


# ---------------------------------------------------------------------------
# Company Owner access tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCompanyOwnerCatalogAccess:
    """
    Company Owner uses fn_UserHasPermission Path A which checks the catalogue.
    If a permission is missing from sec.Permissions, Company Owner returns FALSE.
    These tests verify Company Owner correctly passes backend checks that use
    the newly-catalogued permission codes.
    """

    async def test_company_owner_accesses_payroll_periods(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        """Company Owner passes the payroll read gate (payroll.entry ∈ _PAYROLL_READ_PERMS)."""
        resp = await client.get(
            f"/payroll/periods?branch_id={hq_branch_id}",
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 200, (
            f"Company Owner must pass payroll.entry/view check. "
            f"If payroll.entry is missing from the catalogue, Path A returns FALSE. "
            f"Got {resp.status_code}: {resp.text}"
        )

    async def test_company_owner_accesses_review_items(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        """Company Owner passes the review read gate (review.decide ∈ _REVIEW_READ_PERMS)."""
        resp = await client.get(
            f"/review/items?branch_id={hq_branch_id}",
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 200, (
            f"Company Owner must pass review.decide/view check. "
            f"If review.decide is missing from the catalogue, Path A returns FALSE. "
            f"Got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# Custom role assignment tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCustomRoleAssignment:
    """
    Verify that payroll.entry and review.decide can be assigned to custom roles
    and that those roles correctly gain the expected access.
    """

    async def test_role_with_payroll_entry_passes_read_gate(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        """
        A custom role with only payroll.entry passes the payroll read gate.
        _PAYROLL_READ_PERMS = [payroll.view, payroll.entry, payroll.finalize]
        """
        role_id = await _create_role_with_perms(
            client, auth_token, "PCT_PayrollEntry", ["payroll.entry"]
        )
        user = await _create_user(client, auth_token, "pct_pe_user")
        await _assign_role(
            client, auth_token, user["user_id"], role_id,
            scope="SpecificBranch", branch_id=hq_branch_id,
        )
        tok = await _login(client, "pct_pe_user")
        resp = await client.get(
            f"/payroll/periods?branch_id={hq_branch_id}", headers=_hdr(tok)
        )
        assert resp.status_code == 200, (
            f"Role with payroll.entry must pass the payroll read gate. "
            f"Got {resp.status_code}: {resp.text}"
        )

    async def test_role_with_review_decide_passes_review_read(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        """
        A custom role with only review.decide passes the review read gate.
        _REVIEW_READ_PERMS = [payroll.view, payroll.entry, payroll.finalize, review.decide]
        """
        role_id = await _create_role_with_perms(
            client, auth_token, "PCT_ReviewDecide", ["review.decide"]
        )
        user = await _create_user(client, auth_token, "pct_rd_user")
        await _assign_role(
            client, auth_token, user["user_id"], role_id,
            scope="SpecificBranch", branch_id=hq_branch_id,
        )
        tok = await _login(client, "pct_rd_user")
        resp = await client.get(
            f"/review/items?branch_id={hq_branch_id}", headers=_hdr(tok)
        )
        assert resp.status_code == 200, (
            f"Role with review.decide must pass the review read gate. "
            f"Got {resp.status_code}: {resp.text}"
        )

    async def test_role_without_payroll_entry_blocked_on_entry_gate(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        """
        A role with payroll.view but NOT payroll.entry must be blocked on
        endpoints that specifically require payroll.entry (not just read access).
        This verifies the gate separation is intact post-migration.
        """
        role_id = await _create_role_with_perms(
            client, auth_token, "PCT_ViewOnly", ["payroll.view"]
        )
        user = await _create_user(client, auth_token, "pct_view_only")
        await _assign_role(
            client, auth_token, user["user_id"], role_id,
            scope="SpecificBranch", branch_id=hq_branch_id,
        )
        tok = await _login(client, "pct_view_only")

        # GET periods is allowed (payroll.view is sufficient for read)
        resp_read = await client.get(
            f"/payroll/periods?branch_id={hq_branch_id}", headers=_hdr(tok)
        )
        assert resp_read.status_code == 200, (
            f"payroll.view must allow reading periods. Got {resp_read.status_code}"
        )

        # Candidate preview requires payroll.period.create — not payroll.view.
        resp_create = await client.get(
            f"/payroll/branches/{hq_branch_id}/period-candidates",
            params={"mode": "OPEN_CREATION"},
            headers=_hdr(tok),
        )
        assert resp_create.status_code == 403, (
            f"payroll.view alone must NOT allow period candidate creation "
            f"(needs payroll.period.create). Got {resp_create.status_code}"
        )
