"""
Tests for the backend-owned, branch-aware permission authority object
returned by POST /auth/login and GET /auth/me.

The authority object is separate from (and does not replace) the existing
flat `active_permissions` union.  Every permission list in it must be
produced by sec.fn_UserHasPermission — never reconstructed in Python/TS.

Seed data (see conftest.py):
    admin        — AllCompanyBranches, COMPANY_OWNER (all permissions)
    branch_user  — SpecificBranch HQ, PAYROLL_VIEWER_CO (payroll.view only)
    Branches     — HQ, PAYTEST (both company DEMO)
"""
import httpx
import psycopg2
import pytest


async def _login(client, username="admin", password="TestPass123!", company_code="DEMO"):
    return await client.post("/auth/login", json={
        "username":     username,
        "password":     password,
        "company_code": company_code,
    })


def _pg_cursor(apply_schema):
    conn = psycopg2.connect(**apply_schema.dsn())
    conn.autocommit = True
    return conn, conn.cursor()


def _branch_id(cur, branch_code: str) -> int:
    cur.execute("""
        SELECT b.branchid FROM core.branches b
        JOIN core.companies c ON c.companyid = b.companyid
        WHERE c.companycode = 'DEMO' AND b.branchcode = %s
    """, (branch_code,))
    row = cur.fetchone()
    assert row, f"Seed branch {branch_code} not found"
    return row[0]


def _create_user_with_assignments(cur, username: str, assignments: list[tuple[str | None, str]]) -> None:
    """
    Create (or reuse) a DEMO-company user (password TestPass123!) and attach one
    active sec.UserBranchRoles row per (branch_code, company_rolecode) pair.
    branch_code=None -> AllCompanyBranches (company-wide); otherwise SpecificBranch.
    Uses the CompanyRoleID (new) path only — RoleID left NULL, same pattern as
    the existing override_perm_user fixture in test_auth_regression.py.
    """
    from app.auth.security import hash_password
    pw = hash_password("TestPass123!")

    cur.execute("""
        INSERT INTO sec.users (companyid, username, displayname, passwordhash, isactive, canlogin)
        SELECT c.companyid, %s, %s, %s, TRUE, TRUE
        FROM core.companies c WHERE c.companycode = 'DEMO'
        ON CONFLICT (companyid, username) DO NOTHING
    """, (username, username, pw))

    for branch_code, rolecode in assignments:
        if branch_code is None:
            cur.execute("""
                INSERT INTO sec.userbranchroles
                    (userid, companyid, branchid, roleid, companyroleId, scopetype, isactive)
                SELECT u.userid, u.companyid, NULL, NULL,
                       cr.companyroleid, 'AllCompanyBranches', TRUE
                FROM sec.users u
                JOIN core.companies c  ON c.companyid = u.companyid
                JOIN sec.companyroles cr ON cr.companyid = c.companyid AND cr.rolecode = %s
                WHERE u.username = %s
            """, (rolecode, username))
        else:
            cur.execute("""
                INSERT INTO sec.userbranchroles
                    (userid, companyid, branchid, roleid, companyroleId, scopetype, isactive)
                SELECT u.userid, u.companyid, b.branchid, NULL,
                       cr.companyroleid, 'SpecificBranch', TRUE
                FROM sec.users u
                JOIN core.companies c  ON c.companyid = u.companyid
                JOIN core.branches b   ON b.companyid = c.companyid AND b.branchcode = %s
                JOIN sec.companyroles cr ON cr.companyid = c.companyid AND cr.rolecode = %s
                WHERE u.username = %s
            """, (branch_code, rolecode, username))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AllCompanyBranches
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_all_company_branches_grants_company_permissions_and_no_branch_entries(
    client: httpx.AsyncClient,
):
    """admin holds AllCompanyBranches only -> company_permissions non-empty, branch_permissions empty."""
    r = await _login(client)
    assert r.status_code == 200, r.text
    authority = r.json()["user"]["authority"]
    assert isinstance(authority["company_permissions"], list)
    assert "payroll.entry" in authority["company_permissions"]
    assert "setup.manage" in authority["company_permissions"]
    assert authority["branch_permissions"] == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SpecificBranch only — proves company-scope denial without a company-wide grant
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_specific_branch_only_denies_company_scope_and_scopes_branch_permissions(
    client: httpx.AsyncClient, apply_schema,
):
    conn, cur = _pg_cursor(apply_schema)
    hq_id = _branch_id(cur, "HQ")
    cur.close()
    conn.close()

    r = await _login(client, username="branch_user")
    assert r.status_code == 200, r.text
    authority = r.json()["user"]["authority"]

    assert authority["company_permissions"] == [], (
        "branch_user has no AllCompanyBranches assignment — company_permissions must be empty"
    )
    assert authority["branch_permissions"] == [
        {"branch_id": hq_id, "permissions": ["payroll.view"]}
    ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Mixed AllCompanyBranches + SpecificBranch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_mixed_all_company_and_specific_branch_authority(
    client: httpx.AsyncClient, apply_schema,
):
    """
    A company-wide DRIVER grant (drivers.view) plus a PAYTEST-only PAYROLL_VIEWER_CO
    grant (payroll.view).  The company-wide permission must also appear in the
    branch's own effective list (fn_UserHasPermission unions across all active
    assignments for that user/branch pair).
    """
    conn, cur = _pg_cursor(apply_schema)
    _create_user_with_assignments(
        cur, "authority_mixed_user",
        [(None, "DRIVER"), ("PAYTEST", "PAYROLL_VIEWER_CO")],
    )
    paytest_id = _branch_id(cur, "PAYTEST")
    cur.close()
    conn.close()

    r = await _login(client, username="authority_mixed_user")
    assert r.status_code == 200, r.text
    authority = r.json()["user"]["authority"]

    assert authority["company_permissions"] == ["drivers.view"]
    assert authority["branch_permissions"] == [
        {"branch_id": paytest_id, "permissions": ["drivers.view", "payroll.view"]}
    ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Multiple distinct SpecificBranch assignments
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_multiple_specific_branch_assignments_each_scoped_independently(
    client: httpx.AsyncClient, apply_schema,
):
    conn, cur = _pg_cursor(apply_schema)
    _create_user_with_assignments(
        cur, "authority_multibranch_user",
        [("HQ", "PAYROLL_VIEWER_CO"), ("PAYTEST", "DRIVER")],
    )
    hq_id = _branch_id(cur, "HQ")
    paytest_id = _branch_id(cur, "PAYTEST")
    cur.close()
    conn.close()

    r = await _login(client, username="authority_multibranch_user")
    assert r.status_code == 200, r.text
    authority = r.json()["user"]["authority"]

    assert authority["company_permissions"] == []
    by_branch = {row["branch_id"]: row["permissions"] for row in authority["branch_permissions"]}
    assert by_branch[hq_id] == ["payroll.view"]
    assert by_branch[paytest_id] == ["drivers.view"]
    assert [row["branch_id"] for row in authority["branch_permissions"]] == sorted([hq_id, paytest_id]), (
        "branch_permissions ordering must be deterministic (ascending branch_id)"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Overlapping assignments on the same concrete branch — dedupe + union
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_overlapping_specific_branch_assignments_dedupe_to_one_entry(
    client: httpx.AsyncClient, apply_schema,
):
    conn, cur = _pg_cursor(apply_schema)
    _create_user_with_assignments(
        cur, "authority_overlap_user",
        [("HQ", "PAYROLL_VIEWER_CO"), ("HQ", "DRIVER")],
    )
    hq_id = _branch_id(cur, "HQ")
    cur.close()
    conn.close()

    r = await _login(client, username="authority_overlap_user")
    assert r.status_code == 200, r.text
    authority = r.json()["user"]["authority"]

    assert authority["branch_permissions"] == [
        {"branch_id": hq_id, "permissions": ["drivers.view", "payroll.view"]}
    ], "Two active assignments on the same branch must collapse into one entry"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# active_permissions preserved unchanged
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_active_permissions_field_unchanged_alongside_authority(client: httpx.AsyncClient):
    r = await _login(client)
    assert r.status_code == 200, r.text
    user = r.json()["user"]
    assert isinstance(user["active_permissions"], list)
    assert len(user["active_permissions"]) > 0
    assert "authority" in user


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GET /auth/me returns the same authority contract
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_get_me_returns_authority(client: httpx.AsyncClient):
    login_r = await _login(client)
    assert login_r.status_code == 200
    token = login_r.json()["access_token"]

    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    body = me.json()
    assert "authority" in body
    assert body["authority"]["company_permissions"] == login_r.json()["user"]["authority"]["company_permissions"]
    assert body["authority"]["branch_permissions"] == []
