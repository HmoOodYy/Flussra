"""
Focused regression tests for GET /payroll/periods "mixed authority" users.

A user can hold two simultaneous active assignments:
  - an AllCompanyBranches assignment granting an unrelated permission
    (e.g. users.view), and
  - a SpecificBranch assignment granting payroll.view on exactly one branch.

sec.fn_UserHasPermission only matches a SpecificBranch assignment when a
concrete branch id is supplied. get_periods previously checked the
AllCompanyBranches-implied permission set with branch_id=None whenever
_check_branch_access reported can_see_all=True, which can never see a
SpecificBranch-scoped grant. That incorrectly denied selected-branch
discovery and could not constrain unfiltered discovery to permitted
branches only.
"""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_role_with_perms(
    client: httpx.AsyncClient, token: str, role_name: str, perms: list[str],
) -> int:
    resp = await client.post(
        "/admin/company-roles", json={"role_name": role_name}, headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    role_id = resp.json()["company_role_id"]
    if perms:
        pr = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": perms}, headers=_auth(token),
        )
        assert pr.status_code == 200, pr.text
    return role_id


async def _create_user(client: httpx.AsyncClient, token: str, username: str) -> int:
    resp = await client.post(
        "/admin/users",
        json={
            "username": username, "display_name": username, "password": "TestPass1234!",
            "is_active": True, "can_login": True, "must_change_password": False,
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["user_id"]


async def _assign_company_role(
    client: httpx.AsyncClient, token: str, user_id: int, company_role_id: int,
    scope: str, branch_id: int | None = None,
) -> None:
    payload: dict = {"company_role_id": company_role_id, "scope_type": scope}
    if branch_id is not None:
        payload["branch_id"] = branch_id
    resp = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json=payload, headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text


async def _legacy_role_id(client: httpx.AsyncClient, token: str, role_code: str) -> int:
    resp = await client.get("/admin/roles", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return next(r["role_id"] for r in resp.json() if r["role_code"] == role_code)


async def _assign_legacy_role(
    client: httpx.AsyncClient, token: str, user_id: int, role_id: int,
    scope: str, branch_id: int | None = None,
) -> None:
    payload: dict = {"role_id": role_id, "scope_type": scope}
    if branch_id is not None:
        payload["branch_id"] = branch_id
    resp = await client.post(
        f"/admin/users/{user_id}/roles", json=payload, headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text


async def _login(client: httpx.AsyncClient, username: str) -> str:
    resp = await client.post("/auth/login", json={
        "username": username, "password": "TestPass1234!", "company_code": "DEMO",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _make_mixed_authority_user(
    client: httpx.AsyncClient, admin_token: str, payroll_branch_id: int,
) -> str:
    """Two simultaneously active assignments on one user:
      - a new-path company-role assignment, AllCompanyBranches scope, granting
        an unrelated permission (users.view) — this is what makes
        _check_branch_access report can_see_all=True;
      - a legacy roleid-based assignment, SpecificBranch scope, granting
        payroll.view (via the legacy PAYROLL_ADMIN role) on exactly one branch.

    POST /admin/users/{id}/company-role-assignments revokes any other active
    *company-role* assignment on assign, but never touches legacy roleid-based
    rows, so both assignments coexist — this is the same dual-path shape the
    test-seed admin/branch_user accounts already use (see conftest.py
    _SEED_STMTS), just split across two scopes instead of one.
    """
    marker = uuid4().hex[:12]
    company_wide_role = await _create_role_with_perms(
        client, admin_token, f"LG mixed all-branches {marker}", ["users.view"],
    )
    username = f"lg_mixed_{marker}"
    user_id = await _create_user(client, admin_token, username)
    await _assign_company_role(client, admin_token, user_id, company_wide_role, "AllCompanyBranches")

    legacy_admin_role_id = await _legacy_role_id(client, admin_token, "PAYROLL_ADMIN")
    await _assign_legacy_role(
        client, admin_token, user_id, legacy_admin_role_id, "SpecificBranch", payroll_branch_id,
    )
    return await _login(client, username)


async def _insert_cancelled_period(direct_db, branch_id: int, code_prefix: str) -> int:
    """Cancelled periods carry no workflow-slot uniqueness constraint, so this
    is safe to insert directly regardless of what else exists on the branch."""
    marker = uuid4().hex[:8]
    row = (await direct_db.execute(text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (1, :bid, 'Cancelled', :code, :name, 'Week', '2097-01-06', '2097-01-12')
        RETURNING payrollperiodid
    """), {
        "bid": branch_id, "code": f"{code_prefix}-{marker}", "name": f"{code_prefix} {marker}",
    })).mappings().first()
    return row["payrollperiodid"]


@pytest.mark.asyncio
async def test_mixed_authority_can_list_selected_branch_via_specific_branch_permission(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
):
    """An AllCompanyBranches assignment without payroll.view must not shadow a
    SpecificBranch assignment that does grant it on the requested branch."""
    token = await _make_mixed_authority_user(client, auth_token, paytest_branch_id)

    resp = await client.get(
        "/payroll/periods", params={"branch_id": paytest_branch_id}, headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_mixed_authority_unfiltered_discovery_excludes_branches_without_payroll_permission(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int,
    direct_db,
):
    """Unfiltered discovery must be constrained to branches where the user
    actually holds a payroll read permission — not every company branch just
    because one assignment happens to carry AllCompanyBranches scope."""
    permitted_period_id = await _insert_cancelled_period(direct_db, paytest_branch_id, "LGMIXOK")
    excluded_period_id = await _insert_cancelled_period(direct_db, hq_branch_id, "LGMIXNO")

    token = await _make_mixed_authority_user(client, auth_token, paytest_branch_id)

    resp = await client.get("/payroll/periods", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    returned_ids = {p["payroll_period_id"] for p in resp.json()}
    assert permitted_period_id in returned_ids
    assert excluded_period_id not in returned_ids
