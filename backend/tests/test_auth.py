"""
Tests for POST /auth/login and GET /auth/me.

These are integration tests against a real (isolated) PostgreSQL cluster.
The schema and seed user are set up in conftest.py.

Seed credentials:
    username:     admin
    password:     TestPass123!
    company_code: DEMO
"""
from datetime import UTC

import httpx
import pytest

# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------

class TestLogin:

    async def test_valid_credentials_returns_token(self, client: httpx.AsyncClient):
        resp = await client.post("/auth/login", json={
            "username": "admin",
            "password": "TestPass123!",
            "company_code": "DEMO",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert "access_token" in body
        assert body["token_type"] == "bearer"
        assert body["user"]["username"] == "admin"
        assert body["user"]["company_name"] == "Demo Logistics"
        assert isinstance(body["user"]["branches"], list)

    async def test_wrong_password_returns_401(self, client: httpx.AsyncClient):
        resp = await client.post("/auth/login", json={
            "username": "admin",
            "password": "WrongPassword",
            "company_code": "DEMO",
        })
        assert resp.status_code == 401
        assert "Invalid credentials" in resp.json()["detail"]

    async def test_wrong_company_code_returns_401(self, client: httpx.AsyncClient):
        resp = await client.post("/auth/login", json={
            "username": "admin",
            "password": "TestPass123!",
            "company_code": "NOTEXIST",
        })
        assert resp.status_code == 401

    async def test_unknown_username_returns_401(self, client: httpx.AsyncClient):
        resp = await client.post("/auth/login", json={
            "username": "nobody",
            "password": "TestPass123!",
            "company_code": "DEMO",
        })
        assert resp.status_code == 401

    async def test_blank_username_returns_422(self, client: httpx.AsyncClient):
        resp = await client.post("/auth/login", json={
            "username": "   ",
            "password": "TestPass123!",
            "company_code": "DEMO",
        })
        assert resp.status_code == 422

    async def test_blank_password_returns_422(self, client: httpx.AsyncClient):
        resp = await client.post("/auth/login", json={
            "username": "admin",
            "password": "",
            "company_code": "DEMO",
        })
        assert resp.status_code == 422

    async def test_missing_field_returns_422(self, client: httpx.AsyncClient):
        resp = await client.post("/auth/login", json={
            "username": "admin",
            "password": "TestPass123!",
            # company_code missing
        })
        assert resp.status_code == 422

    async def test_login_same_error_for_wrong_user_and_wrong_password(
        self, client: httpx.AsyncClient
    ):
        """
        Both "unknown username" and "wrong password" must return the identical
        detail message so an attacker cannot enumerate valid usernames by
        comparing error text.
        """
        resp_bad_user = await client.post("/auth/login", json={
            "username": "definitely_does_not_exist",
            "password": "TestPass123!",
            "company_code": "DEMO",
        })
        resp_bad_pass = await client.post("/auth/login", json={
            "username": "admin",
            "password": "ThisIsWrong",
            "company_code": "DEMO",
        })
        assert resp_bad_user.status_code == 401
        assert resp_bad_pass.status_code == 401
        assert resp_bad_user.json()["detail"] == resp_bad_pass.json()["detail"], (
            "Enumeration oracle: different error messages for wrong user vs wrong password"
        )

    async def test_response_includes_branch_scope(self, client: httpx.AsyncClient):
        resp = await client.post("/auth/login", json={
            "username": "admin",
            "password": "TestPass123!",
            "company_code": "DEMO",
        })
        assert resp.status_code == 200
        branches = resp.json()["user"]["branches"]
        assert len(branches) == 1
        assert branches[0]["scope"] == "AllCompanyBranches"
        # role_code may be PAYROLL_ADMIN (fresh session) or COMPANY_OWNER (after ownership
        # transfer tests revoke+recreate the row via the new path).  Both represent the
        # admin's full-access assignment — the scope is what matters.
        assert branches[0]["role_code"] in ("PAYROLL_ADMIN", "COMPANY_OWNER")


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------

class TestMe:

    @pytest.fixture
    async def token(self, client: httpx.AsyncClient) -> str:
        resp = await client.post("/auth/login", json={
            "username": "admin",
            "password": "TestPass123!",
            "company_code": "DEMO",
        })
        return resp.json()["access_token"]

    async def test_me_with_valid_token(self, client: httpx.AsyncClient, token: str):
        resp = await client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["username"] == "admin"
        assert body["company_name"] == "Demo Logistics"

    async def test_me_without_token_returns_401(self, client: httpx.AsyncClient):
        # Starlette 1.x HTTPBearer returns 401 (not 403) for missing credentials.
        resp = await client.get("/auth/me")
        assert resp.status_code == 401

    async def test_me_with_invalid_token_returns_401(self, client: httpx.AsyncClient):
        resp = await client.get(
            "/auth/me",
            headers={"Authorization": "Bearer this.is.invalid"},
        )
        assert resp.status_code == 401

    async def test_me_rejects_wrong_company_in_token(self, client: httpx.AsyncClient):
        """
        A valid JWT that carries a non-existent company_id (cid) must be rejected.
        get_me() now validates u.companyid = :company_id in the DB query, so a
        token forged with cid=99999 returns 401 even though the signature is valid.
        """
        from datetime import datetime, timedelta

        import jwt

        from app.config import settings

        forged_payload = {
            "sub": "1",   # real user_id in the test DB
            "cid": 99999, # non-existent company_id
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(hours=1),
        }
        forged_token = jwt.encode(forged_payload, settings.SECRET_KEY, algorithm="HS256")

        resp = await client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {forged_token}"},
        )
        assert resp.status_code == 401

    async def test_me_with_expired_token_returns_401(self, client: httpx.AsyncClient):
        """Fabricate an already-expired token."""
        from datetime import datetime, timedelta

        import jwt

        from app.config import settings

        expired_payload = {
            "sub": "1",  # sub must be a string (RFC 7519 / PyJWT 2.13+)
            "cid": 1,
            "iat": datetime.now(UTC) - timedelta(hours=10),
            "exp": datetime.now(UTC) - timedelta(hours=2),
        }
        expired_token = jwt.encode(expired_payload, settings.SECRET_KEY, algorithm="HS256")

        resp = await client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {expired_token}"},
        )
        assert resp.status_code == 401
        assert "expired" in resp.json()["detail"].lower()
