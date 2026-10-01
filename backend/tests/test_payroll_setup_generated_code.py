"""Focused tests for server-generated Payroll Setup references.

Identity model: the true record identity is PayrollSetupID (PK); SetupCode
(explicit or generated) is only a human/system-facing reference — nothing
keys on it. Every server-generated reference is `PPOL-` + 8 characters from
an unambiguous alphabet, drawn by `payroll_policy.generated_setup_code`,
regardless of the Setup's name (no transliteration, no name-derived slug).
Collision safety comes from the DB's `(CompanyID, SetupCode)` unique
constraint (`uq_PayrollSetups_Company_Code`, migrations/sql/0066) via
`INSERT ... ON CONFLICT DO NOTHING RETURNING`, not from the generator.

Environment note: the ephemeral PostgreSQL cluster this test session spins
up (`testing.postgresql`, see conftest.py's `apply_schema`/`pg_instance`)
initializes from the host OS locale, which on this Windows box yields a
database encoding of Windows-1252 — a pre-existing constraint of the test
harness, not something these tests or the task's hard rules (no .env, no
migrations) permit changing. True Arabic text cannot round-trip through
this test database at all (any `INSERT` containing it raises
`UntranslatableCharacterError` from the server, regardless of the code
under test). Wherever the task calls for a "non-Latin"/"Arabic" Setup name
at the HTTP/DB layer, these tests use a cp1252-safe symbol-only name (no
usable Latin characters either way) to exercise the same code path.
"""

from __future__ import annotations

import re
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.payroll_setup import payroll_policy
from app.payroll_setup.payroll_policy import SETUP_CODE_PREFIX, generated_setup_code

GENERATED_CODE_PATTERN = re.compile(r"^PPOL-[A-HJ-NP-Z2-9]{8}$")
SETUP_CODE_SCHEMA_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# A cp1252-safe stand-in for "a name with no usable Latin characters" — see
# the module docstring for why literal Arabic text can't be used here.
SYMBOL_ONLY_NAME = "§±°€"
PUNCTUATION_ONLY_NAME = "!!! ... ---"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _demo_company_id(db_conn) -> int:
    return (await db_conn.execute(text(
        "SELECT CompanyID FROM core.Companies WHERE CompanyCode = 'DEMO'"
    ))).scalar_one()


async def _seed_setup_row(db_conn, company_id: int, code: str, name: str) -> int:
    """Insert a PayrollSetups row directly, bypassing `create_setup` — used to
    occupy a specific SetupCode so a mocked generator's draw collides
    against a row this test controls precisely."""
    return (await db_conn.execute(text("""
        INSERT INTO payroll.PayrollSetups (CompanyID, SetupCode, SetupName)
        VALUES (:cid, :code, :name)
        RETURNING PayrollSetupID
    """), {"cid": company_id, "code": code, "name": name})).scalar_one()


# ---------------------------------------------------------------------------
# Pure `generated_setup_code` cases.
# ---------------------------------------------------------------------------




def test_generated_setup_code_default_matches_pattern_and_alphabet():
    code = generated_setup_code()
    assert GENERATED_CODE_PATTERN.fullmatch(code), code
    assert SETUP_CODE_SCHEMA_PATTERN.fullmatch(code)
    assert len(code) <= 50




# ---------------------------------------------------------------------------
# API: the generated code never depends on the Setup name — Latin,
# symbol-only ("non-Latin" stand-in, see module docstring), and
# punctuation-only names all get a plain PPOL-XXXXXXXX reference.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "Weekly Policy",
    SYMBOL_ONLY_NAME,
    PUNCTUATION_ONLY_NAME,
])
@pytest.mark.asyncio
async def test_create_without_setup_code_generates_ppol_code_regardless_of_name(
    client, auth_token, name,
):
    created = await client.post("/payroll-setup/setups", headers=_auth(auth_token), json={
        "setup_name": name,
    })
    assert created.status_code == 201, created.text
    body = created.json()
    assert GENERATED_CODE_PATTERN.fullmatch(body["setup_code"]), body["setup_code"]
    assert body["setup_name"] == name


@pytest.mark.asyncio
async def test_two_creates_with_the_same_name_get_different_codes_and_ids(
    client, auth_token,
):
    name = f"Duplicate name {uuid4().hex[:8]}"
    first = await client.post("/payroll-setup/setups", headers=_auth(auth_token), json={
        "setup_name": name,
    })
    second = await client.post("/payroll-setup/setups", headers=_auth(auth_token), json={
        "setup_name": name,
    })
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    first_body, second_body = first.json(), second.json()
    assert first_body["setup_code"] != second_body["setup_code"]
    assert first_body["setup_id"] != second_body["setup_id"]
    for body in (first_body, second_body):
        assert GENERATED_CODE_PATTERN.fullmatch(body["setup_code"]), body["setup_code"]


@pytest.mark.asyncio
async def test_generated_code_collision_retries_with_a_fresh_suffix(
    client, auth_token, db_conn, monkeypatch,
):
    marker = uuid4().hex[:6].upper()
    existing_code = f"{SETUP_CODE_PREFIX}TAKEN{marker}"
    fresh_code = f"{SETUP_CODE_PREFIX}FRESH{marker}"
    company_id = await _demo_company_id(db_conn)
    await _seed_setup_row(db_conn, company_id, existing_code, "Occupies the generated slot")

    codes = iter([existing_code, fresh_code])
    monkeypatch.setattr(payroll_policy, "generated_setup_code", lambda: next(codes))

    created = await client.post("/payroll-setup/setups", headers=_auth(auth_token), json={
        "setup_name": "Retries after a collision",
    })
    assert created.status_code == 201, created.text
    assert created.json()["setup_code"] == fresh_code


@pytest.mark.asyncio
async def test_generated_code_exhausted_attempts_is_conflict(
    client, auth_token, db_conn, monkeypatch,
):
    marker = uuid4().hex[:6].upper()
    stuck_code = f"{SETUP_CODE_PREFIX}STUCK{marker}"
    company_id = await _demo_company_id(db_conn)
    await _seed_setup_row(db_conn, company_id, stuck_code, "Occupies the generated slot")

    monkeypatch.setattr(payroll_policy, "generated_setup_code", lambda: stuck_code)

    response = await client.post("/payroll-setup/setups", headers=_auth(auth_token), json={
        "setup_name": "Never finds a free slot",
    })
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "SETUP_CODE_CONFLICT"


# ---------------------------------------------------------------------------
# API: the PPOL- namespace is reserved — an explicit code using it (any
# case) is rejected, while ordinary explicit codes behave exactly as before.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["ppol-abc", "PPOL-X", "Ppol-mixedCase1"])
@pytest.mark.asyncio
async def test_explicit_code_in_reserved_namespace_is_rejected(client, auth_token, code):
    response = await client.post("/payroll-setup/setups", headers=_auth(auth_token), json={
        "setup_code": code, "setup_name": "Reserved namespace probe",
    })
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "INVALID_SETUP_CODE"


@pytest.mark.asyncio
async def test_explicit_code_still_works_and_duplicate_is_conflict(client, auth_token):
    code = "GC_EXPLICIT_" + uuid4().hex[:8]
    first = await client.post("/payroll-setup/setups", headers=_auth(auth_token), json={
        "setup_code": code, "setup_name": "Explicit code setup",
    })
    assert first.status_code == 201, first.text
    assert first.json()["setup_code"] == code

    duplicate = await client.post("/payroll-setup/setups", headers=_auth(auth_token), json={
        "setup_code": code, "setup_name": "Explicit code setup, again",
    })
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["detail"]["code"] == "SETUP_CODE_CONFLICT"


@pytest.mark.asyncio
async def test_audit_row_code_equals_stored_code_for_generated_and_explicit(
    client, auth_token, db_conn,
):
    marker = uuid4().hex[:10]

    generated = await client.post("/payroll-setup/setups", headers=_auth(auth_token), json={
        "setup_name": f"Audit generated code {marker}",
    })
    assert generated.status_code == 201, generated.text
    generated_body = generated.json()

    explicit_code = "GC_AUDIT_" + marker
    explicit = await client.post("/payroll-setup/setups", headers=_auth(auth_token), json={
        "setup_code": explicit_code, "setup_name": f"Audit explicit code {marker}",
    })
    assert explicit.status_code == 201, explicit.text
    explicit_body = explicit.json()

    for body in (generated_body, explicit_body):
        audit_code = (await db_conn.execute(text("""
            SELECT NewStateJSON->>'code' FROM payroll.PayrollSetupPolicyAuditEvents
            WHERE PayrollSetupID = :sid AND EventType = 'SetupCreated'
        """), {"sid": body["setup_id"]})).scalar_one()
        assert audit_code == body["setup_code"]


# ---------------------------------------------------------------------------
# DB: the unique constraint, not application logic, is the collision guard.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_duplicate_insert_violates_the_unique_constraint(
    client, auth_token, db_conn,
):
    code = "RAWDUP_" + uuid4().hex[:8]
    created = await client.post("/payroll-setup/setups", headers=_auth(auth_token), json={
        "setup_code": code, "setup_name": "Raw duplicate probe",
    })
    assert created.status_code == 201, created.text
    company_id = await _demo_company_id(db_conn)

    with pytest.raises(IntegrityError) as excinfo:
        await db_conn.execute(text("""
            INSERT INTO payroll.PayrollSetups (CompanyID, SetupCode, SetupName)
            VALUES (:cid, :code, 'Raw duplicate attempt')
        """), {"cid": company_id, "code": code})
    assert "uq_payrollsetups_company_code" in str(excinfo.value).lower()
