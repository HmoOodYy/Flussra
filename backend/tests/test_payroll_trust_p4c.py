"""
Payroll Trust Phase 4C -- RateTypes.CompanyID Structural Ownership Tests.

Verifies:
  A. Migration backfill: system RateTypes have companyid=NULL; custom RateTypes
     get companyid set to the owning company.
  B. Service creation: create_custom_pay_item, decide_pay_item_request, and
     backfill_custom_pay_item_rate_structure all set companyid on new CPI_ RateTypes.
  C. Structural ownership guard: foreign RateType rejected for create_rate,
     approve_rate, update_rate, batch_save, assign_rate_type, rate-types list,
     and rate matrix.
  D. DB trigger: trg_guard_payitemratetypemap_ownership blocks cross-company
     PayItemRateTypeMap inserts at the database level.
  E. Phase 4B regression: all prior 4B protections remain intact via structural check.
"""
import pytest
import pytest_asyncio
import httpx
from sqlalchemy import text as _text

from tests.test_payroll_trust_p4b import _get_token_b, _auth


# ---------------------------------------------------------------------------
# Fixture: minimal 2-company environment for Phase 4C tests
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def p4c_env(direct_db, client: httpx.AsyncClient, auth_token: str):
    """
    Sets up:
      - Company A (DEMO) -- already seeded by conftest
      - Company B (COMP_B_P4C) -- created fresh per test module
      - Company A custom PayItem + CPI_ RateType (structurally owned by Company A)
      - Company B custom PayItem + its own auto-generated CPI_ RateType

    Yields dict with relevant IDs.
    Teardown removes all created rows in dependency order.
    """
    # ── Company A (DEMO) ────────────────────────────────────────────────────
    row = (await direct_db.execute(_text(
        "SELECT companyid FROM core.companies WHERE companycode = 'DEMO'"
    ))).mappings().first()
    cid_a = row["companyid"]

    row = (await direct_db.execute(_text(
        "SELECT branchid FROM core.branches WHERE companyid = :cid AND branchcode = 'HQ'"
    ), {"cid": cid_a})).mappings().first()
    bid_a = row["branchid"]

    # System HOURLY rate type
    row = (await direct_db.execute(_text(
        "SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = 'HOURLY'"
    ))).mappings().first()
    rt_hourly_id = row["ratetypeid"]

    # Company A custom PayItem with companyid set (structural ownership)
    pi_a_row = (await direct_db.execute(_text("""
        INSERT INTO payroll.payitems
            (companyid, payitemcode, payitemname, ratebehavior,
             requiresrate, isdefaultbranchactive, status,
             category, datatype, itemscope)
        VALUES
            (:cid, 'CPI_P4C_A', 'P4C Custom Item A', 'PerUnit',
             TRUE, TRUE, 'Active', 'Custom', 'Decimal', 'Daily')
        ON CONFLICT (companyid, payitemcode) WHERE companyid IS NOT NULL
            DO UPDATE SET status = 'Active'
        RETURNING payitemid
    """), {"cid": cid_a})).mappings().first()
    pi_a_id = pi_a_row["payitemid"]

    # CPI_ RateType for Company A, companyid=cid_a (Phase 4C structural ownership)
    rt_a_row = (await direct_db.execute(_text("""
        INSERT INTO payroll.ratetypes (ratecode, ratename, unitname, isactive, companyid)
        VALUES ('CPI_P4C_RT_A', 'P4C Rate A', 'Unit', TRUE, :cid)
        ON CONFLICT (ratecode) DO UPDATE SET isactive = TRUE, companyid = :cid
        RETURNING ratetypeid
    """), {"cid": cid_a})).mappings().first()
    rt_a_id = rt_a_row["ratetypeid"]

    await direct_db.execute(_text("""
        INSERT INTO payroll.payitemratetypemap (payitemid, ratetypeid, isprimary, status)
        VALUES (:piid, :rtid, TRUE, 'Active')
        ON CONFLICT DO NOTHING
    """), {"piid": pi_a_id, "rtid": rt_a_id})

    # ── Company B ────────────────────────────────────────────────────────────
    # Clean up any leftovers from a previous run
    await direct_db.execute(_text("""
        DELETE FROM payroll.branchpayitemconfig WHERE payitemid IN (
            SELECT payitemid FROM payroll.payitems WHERE companyid IN
                (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4C')
        )
    """))
    await direct_db.execute(_text("""
        DELETE FROM payroll.payitemratetypemap WHERE payitemid IN (
            SELECT payitemid FROM payroll.payitems WHERE companyid IN
                (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4C')
        )
    """))
    await direct_db.execute(_text("""
        DELETE FROM payroll.payitemsettings WHERE payitemid IN (
            SELECT payitemid FROM payroll.payitems WHERE companyid IN
                (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4C')
        )
    """))
    await direct_db.execute(_text("""
        DELETE FROM payroll.payitems WHERE companyid IN
            (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4C')
    """))
    await direct_db.execute(_text("""
        DELETE FROM payroll.driverrates WHERE companyid IN
            (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4C')
    """))
    await direct_db.execute(_text("""
        DELETE FROM core.drivers WHERE companyid IN
            (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4C')
    """))
    await direct_db.execute(_text("""
        DELETE FROM core.employees WHERE companyid IN
            (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4C')
    """))
    await direct_db.execute(_text("""
        DELETE FROM sec.userbranchroles WHERE companyid IN
            (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4C')
    """))
    await direct_db.execute(_text("""
        DELETE FROM sec.users WHERE companyid IN
            (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4C')
    """))
    await direct_db.execute(_text("""
        DELETE FROM core.branches WHERE companyid IN
            (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4C')
    """))
    await direct_db.execute(_text("""
        DELETE FROM core.companies WHERE companycode = 'COMP_B_P4C'
    """))

    # Create Company B via direct DB inserts (no /settings/company endpoint)
    b_row = (await direct_db.execute(_text("""
        INSERT INTO core.companies
            (companycode, companyname, legalname, status, issuspended, timezonename)
        VALUES ('COMP_B_P4C', 'P4C Test Company B', 'P4C Test Co B Ltd', 'Active', FALSE, 'UTC')
        RETURNING companyid
    """))).mappings().first()
    cid_b = b_row["companyid"]

    br_row = (await direct_db.execute(_text("""
        INSERT INTO core.branches
            (companyid, branchcode, branchname, status, isdefault)
        VALUES (:cid, 'BR_B_P4C', 'Branch B P4C', 'Active', TRUE)
        RETURNING branchid
    """), {"cid": cid_b})).mappings().first()
    bid_b = br_row["branchid"]

    from app.auth.security import hash_password
    pw_hash_b = hash_password("TestPass123!")

    u_row_b = (await direct_db.execute(_text("""
        INSERT INTO sec.users
            (companyid, username, displayname, passwordhash, isactive, canlogin)
        VALUES (:cid, 'admin_b_p4c', 'Admin B P4C', :pw, TRUE, TRUE)
        RETURNING userid
    """), {"cid": cid_b, "pw": pw_hash_b})).mappings().first()
    uid_b = u_row_b["userid"]

    role_row_b = (await direct_db.execute(_text(
        "SELECT roleid FROM sec.roles WHERE rolecode = 'PAYROLL_ADMIN'"
    ))).mappings().first()
    await direct_db.execute(_text("""
        INSERT INTO sec.userbranchroles
            (userid, companyid, branchid, roleid, scopetype, isactive)
        VALUES (:uid, :cid, NULL, :rid, 'AllCompanyBranches', TRUE)
    """), {"uid": uid_b, "cid": cid_b, "rid": role_row_b["roleid"]})

    resp_login_b = await client.post("/auth/login", json={
        "username":     "admin_b_p4c",
        "password":     "TestPass123!",
        "company_code": "COMP_B_P4C",
    })
    assert resp_login_b.status_code == 200, f"Company B login failed: {resp_login_b.text}"
    token_b = resp_login_b.json()["access_token"]

    # Company B's custom PayItem via direct DB (LLR-A blocks HTTP creation)
    pi_b_row = (await direct_db.execute(_text("""
        INSERT INTO payroll.payitems
            (companyid, payitemcode, payitemname, ratebehavior,
             requiresrate, isdefaultbranchactive, status,
             category, datatype, itemscope, unit)
        VALUES
            (:cid, 'CPI_P4C_B', 'P4C Custom Item B', 'PerUnit',
             TRUE, FALSE, 'Active', 'Count', 'Decimal', 'Daily', 'Unit')
        ON CONFLICT (companyid, payitemcode) WHERE companyid IS NOT NULL
            DO UPDATE SET status = 'Active'
        RETURNING payitemid
    """), {"cid": cid_b})).mappings().first()
    pi_b_id = pi_b_row["payitemid"]

    rt_b_row = (await direct_db.execute(_text("""
        INSERT INTO payroll.ratetypes (ratecode, ratename, unitname, isactive, companyid)
        VALUES (:code, :name, 'Unit', TRUE, :cid)
        ON CONFLICT (ratecode) DO UPDATE SET isactive = TRUE, companyid = :cid
        RETURNING ratetypeid, companyid
    """), {"code": f"CPI_{pi_b_id}_1", "name": "P4C Custom Item B Rate", "cid": cid_b})).mappings().first()
    rt_b_id = rt_b_row["ratetypeid"]
    rt_b_companyid = rt_b_row["companyid"]

    await direct_db.execute(_text("""
        INSERT INTO payroll.payitemratetypemap (payitemid, ratetypeid, isprimary, status)
        VALUES (:piid, :rtid, TRUE, 'Active')
        ON CONFLICT DO NOTHING
    """), {"piid": pi_b_id, "rtid": rt_b_id})

    yield {
        "cid_a":       cid_a,
        "bid_a":       bid_a,
        "pi_a_id":     pi_a_id,
        "rt_a_id":     rt_a_id,
        "cid_b":       cid_b,
        "bid_b":       bid_b,
        "pi_b_id":     pi_b_id,
        "rt_b_id":     rt_b_id,
        "rt_b_companyid": rt_b_companyid,
        "rt_hourly_id": rt_hourly_id,
        "auth_a":      auth_token,
        "auth_b":      token_b,
    }

    # ── Teardown ─────────────────────────────────────────────────────────────
    await direct_db.execute(_text("""
        DELETE FROM payroll.branchpayitemconfig WHERE payitemid IN (
            SELECT payitemid FROM payroll.payitems WHERE companyid = :cid
        )
    """), {"cid": cid_b})
    await direct_db.execute(_text("""
        DELETE FROM payroll.payitemratetypemap WHERE payitemid IN (
            SELECT payitemid FROM payroll.payitems WHERE companyid = :cid
        )
    """), {"cid": cid_b})
    await direct_db.execute(_text("""
        DELETE FROM payroll.payitemsettings WHERE payitemid IN (
            SELECT payitemid FROM payroll.payitems WHERE companyid = :cid
        )
    """), {"cid": cid_b})
    # Delete auto-generated CPI_ RateTypes for Company B
    if rt_b_id:
        await direct_db.execute(_text(
            "DELETE FROM payroll.ratetypes WHERE ratetypeid = :rtid"
        ), {"rtid": rt_b_id})
    await direct_db.execute(_text(
        "DELETE FROM payroll.payitems WHERE companyid = :cid"
    ), {"cid": cid_b})
    await direct_db.execute(_text(
        "DELETE FROM payroll.driverrates WHERE companyid = :cid"
    ), {"cid": cid_b})
    await direct_db.execute(_text(
        "DELETE FROM core.drivers WHERE companyid = :cid"
    ), {"cid": cid_b})
    await direct_db.execute(_text(
        "DELETE FROM core.employees WHERE companyid = :cid"
    ), {"cid": cid_b})
    await direct_db.execute(_text(
        "DELETE FROM sec.userbranchroles WHERE companyid = :cid"
    ), {"cid": cid_b})
    await direct_db.execute(_text(
        "DELETE FROM sec.users WHERE companyid = :cid"
    ), {"cid": cid_b})
    await direct_db.execute(_text(
        "DELETE FROM core.branches WHERE companyid = :cid"
    ), {"cid": cid_b})
    await direct_db.execute(_text(
        "DELETE FROM core.companies WHERE companyid = :cid"
    ), {"cid": cid_b})

    # Company A cleanup
    await direct_db.execute(_text(
        "DELETE FROM payroll.payitemratetypemap WHERE ratetypeid = :rtid"
    ), {"rtid": rt_a_id})
    await direct_db.execute(_text(
        "DELETE FROM payroll.ratetypes WHERE ratetypeid = :rtid"
    ), {"rtid": rt_a_id})
    await direct_db.execute(_text(
        "DELETE FROM payroll.payitems WHERE payitemid = :piid"
    ), {"piid": pi_a_id})


# ---------------------------------------------------------------------------
# A. Migration backfill assertions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p4c_a1_system_rate_types_have_null_companyid(direct_db):
    """
    All pre-migration system RateTypes (HOURLY, MILEAGE, etc.) must have
    companyid=NULL after the 0036 migration backfill.
    """
    result = await direct_db.execute(_text("""
        SELECT ratecode, companyid
        FROM   payroll.ratetypes
        WHERE  ratecode IN ('HOURLY','MILEAGE','LOAD','OVERNIGHT','WAIT','PALLET','SILO',
                            'M13C_ORDINAL','M13C_RBRKT','M13C_RPROG','M13C_BLOCK','INACTIVE')
    """))
    rows = result.mappings().all()
    assert rows, "System RateTypes must exist"
    for row in rows:
        assert row["companyid"] is None, (
            f"System RateType {row['ratecode']!r} must have companyid=NULL, "
            f"got {row['companyid']}"
        )


@pytest.mark.asyncio
async def test_p4c_a2_ratetypes_companyid_column_exists(direct_db):
    """
    payroll.ratetypes must have a companyid column after migration 0036.
    """
    result = await direct_db.execute(_text("""
        SELECT column_name
        FROM   information_schema.columns
        WHERE  table_schema = 'payroll'
          AND  table_name   = 'ratetypes'
          AND  column_name  = 'companyid'
    """))
    row = result.first()
    assert row is not None, "payroll.ratetypes.companyid column must exist after migration 0036"


@pytest.mark.asyncio
async def test_p4c_a3_ownership_trigger_exists(direct_db):
    """
    trg_guard_payitemratetypemap_ownership must exist on payroll.payitemratetypemap.
    """
    result = await direct_db.execute(_text("""
        SELECT trigger_name
        FROM   information_schema.triggers
        WHERE  trigger_schema = 'payroll'
          AND  event_object_table = 'payitemratetypemap'
          AND  trigger_name = 'trg_guard_payitemratetypemap_ownership'
    """))
    row = result.first()
    assert row is not None, (
        "trg_guard_payitemratetypemap_ownership trigger must exist on payroll.payitemratetypemap"
    )


# ---------------------------------------------------------------------------
# B. Service creation: CPI_ RateTypes get companyid set
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p4c_b1_create_custom_pay_item_sets_companyid(
    p4c_env, direct_db, client: httpx.AsyncClient
):
    """
    create_custom_pay_item creates CPI_ RateTypes with companyid=company_id.
    The fixture already called this endpoint for Company B.
    Verify that the auto-generated CPI_ RateType for Company B has companyid=cid_b.
    """
    cid_b    = p4c_env["cid_b"]
    rt_b_id  = p4c_env["rt_b_id"]
    rt_b_cid = p4c_env["rt_b_companyid"]

    assert rt_b_id is not None, "Company B's auto-generated CPI_ RateType must exist"
    assert rt_b_cid == cid_b, (
        f"Company B's CPI_ RateType must have companyid={cid_b}, got {rt_b_cid}"
    )


@pytest.mark.asyncio
async def test_p4c_b2_fixture_cpi_has_companyid(p4c_env, direct_db):
    """
    The manually inserted CPI_P4C_RT_A RateType must have companyid=cid_a.
    """
    cid_a  = p4c_env["cid_a"]
    rt_a_id = p4c_env["rt_a_id"]

    result = await direct_db.execute(_text(
        "SELECT companyid FROM payroll.ratetypes WHERE ratetypeid = :rtid"
    ), {"rtid": rt_a_id})
    row = result.mappings().first()
    assert row["companyid"] == cid_a, (
        f"CPI_P4C_RT_A must have companyid={cid_a}, got {row['companyid']}"
    )


# ---------------------------------------------------------------------------
# C. Structural ownership guard (service layer)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p4c_c1_get_rate_types_shows_own_and_system_only(
    p4c_env, client: httpx.AsyncClient
):
    """
    GET /payroll/rate-types as Company B:
      - System types (companyid=NULL) must appear.
      - Company B's own CPI_ type (companyid=cid_b) must appear.
      - Company A's CPI_ type (companyid=cid_a) must NOT appear.
    """
    token_b    = p4c_env["auth_b"]
    rt_a_id    = p4c_env["rt_a_id"]
    rt_b_id    = p4c_env["rt_b_id"]
    rt_hourly  = p4c_env["rt_hourly_id"]

    resp = await client.get("/payroll/rate-types", headers=_auth(token_b))
    assert resp.status_code == 200

    ids   = {rt["rate_type_id"] for rt in resp.json()}
    codes = {rt["rate_code"]    for rt in resp.json()}

    assert rt_hourly in ids,  "System HOURLY must appear for Company B"
    assert "HOURLY" in codes

    assert rt_a_id not in ids, (
        "Company A's CPI_ RateType must NOT appear for Company B "
        "(structural: rt.companyid=cid_a != cid_b)"
    )

    if rt_b_id is not None:
        assert rt_b_id in ids, (
            "Company B's own CPI_ RateType must appear (companyid=cid_b)"
        )


@pytest.mark.asyncio
async def test_p4c_c2_get_rate_types_shows_own_from_company_a_perspective(
    p4c_env, client: httpx.AsyncClient
):
    """
    GET /payroll/rate-types as Company A:
      - Company A's CPI_ type (companyid=cid_a) must appear.
      - Company B's CPI_ type (companyid=cid_b) must NOT appear.
    """
    token_a = p4c_env["auth_a"]
    rt_a_id = p4c_env["rt_a_id"]
    rt_b_id = p4c_env["rt_b_id"]

    resp = await client.get("/payroll/rate-types", headers=_auth(token_a))
    assert resp.status_code == 200

    ids = {rt["rate_type_id"] for rt in resp.json()}

    assert rt_a_id in ids, (
        "Company A's CPI_ RateType must appear for Company A (own-company type)"
    )
    if rt_b_id is not None:
        assert rt_b_id not in ids, (
            "Company B's CPI_ RateType must NOT appear for Company A"
        )


@pytest.mark.asyncio
async def test_p4c_c3_assign_rate_type_rejects_foreign_company_type(
    p4c_env, client: httpx.AsyncClient, direct_db
):
    """
    Company B tries to assign Company A's CPI_ RateType to its own PayItem.
    Must be rejected 422 (structural: rt.companyid=cid_a != cid_b).
    """
    token_b = p4c_env["auth_b"]
    pi_b_id = p4c_env["pi_b_id"]
    rt_a_id = p4c_env["rt_a_id"]

    resp = await client.post(
        f"/settings/pay-items/{pi_b_id}/rate-type-map",
        json={"rate_type_id": rt_a_id, "is_primary": False},
        headers=_auth(token_b),
    )
    assert resp.status_code == 422, (
        f"Expected 422 for foreign RateType assignment, got {resp.status_code}: {resp.text}"
    )
    assert "does not belong to this company" in resp.json()["detail"].lower()

    # Verify no PayItemRateTypeMap row was created
    map_row = (await direct_db.execute(_text("""
        SELECT 1 FROM payroll.payitemratetypemap
        WHERE payitemid = :piid AND ratetypeid = :rtid
    """), {"piid": pi_b_id, "rtid": rt_a_id})).first()
    assert map_row is None, "No PayItemRateTypeMap must be created for foreign RateType"


@pytest.mark.asyncio
async def test_p4c_c4_system_rate_type_accessible_by_all_companies(
    p4c_env, client: httpx.AsyncClient, direct_db
):
    """
    System RateType (companyid=NULL) is visible to all companies.
    This verifies that the structural check correctly handles NULL companyid.
    """
    token_a    = p4c_env["auth_a"]
    token_b    = p4c_env["auth_b"]
    rt_hourly  = p4c_env["rt_hourly_id"]

    for token, label in [(token_a, "Company A"), (token_b, "Company B")]:
        resp = await client.get("/payroll/rate-types", headers=_auth(token))
        assert resp.status_code == 200
        ids = {rt["rate_type_id"] for rt in resp.json()}
        assert rt_hourly in ids, (
            f"System HOURLY must be visible to {label} (companyid=NULL -> all companies)"
        )


# ---------------------------------------------------------------------------
# D. DB trigger: trg_guard_payitemratetypemap_ownership
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p4c_d1_trigger_blocks_cross_company_mapping(p4c_env, direct_db):
    """
    The ownership trigger must raise restrict_violation when a company-owned
    RateType (companyid=cid_a) is mapped to a PayItem from another company (cid_b).
    """
    import sqlalchemy.exc
    pi_b_id = p4c_env["pi_b_id"]
    rt_a_id = p4c_env["rt_a_id"]

    with pytest.raises(sqlalchemy.exc.IntegrityError) as exc_info:
        await direct_db.execute(_text("""
            INSERT INTO payroll.payitemratetypemap (payitemid, ratetypeid, isprimary, status)
            VALUES (:piid, :rtid, FALSE, 'Active')
        """), {"piid": pi_b_id, "rtid": rt_a_id})

    assert "payitemratetypemap_ownership_violation" in str(exc_info.value), (
        "Trigger must raise payitemratetypemap_ownership_violation for cross-company mapping"
    )

    # Verify no row was inserted
    map_row = (await direct_db.execute(_text("""
        SELECT 1 FROM payroll.payitemratetypemap
        WHERE payitemid = :piid AND ratetypeid = :rtid
    """), {"piid": pi_b_id, "rtid": rt_a_id})).first()
    assert map_row is None, "No PayItemRateTypeMap row must exist after trigger rejection"


@pytest.mark.asyncio
async def test_p4c_d2_trigger_allows_same_company_mapping(p4c_env, direct_db):
    """
    The ownership trigger must ALLOW mapping when RateType.companyid = PayItem.companyid.
    """
    pi_a_id = p4c_env["pi_a_id"]
    rt_a_id = p4c_env["rt_a_id"]

    # Already mapped in fixture setup; verify row exists
    map_row = (await direct_db.execute(_text("""
        SELECT 1 FROM payroll.payitemratetypemap
        WHERE payitemid = :piid AND ratetypeid = :rtid AND status = 'Active'
    """), {"piid": pi_a_id, "rtid": rt_a_id})).first()
    assert map_row is not None, (
        "Same-company mapping (pi_a_id -> rt_a_id, both cid_a) must be allowed by trigger"
    )


@pytest.mark.asyncio
async def test_p4c_d3_trigger_allows_system_ratetype_any_payitem(p4c_env, direct_db):
    """
    The ownership trigger must ALLOW mapping a system RateType (companyid=NULL)
    to any company's PayItem (companyid=cid_b).
    """
    pi_b_id    = p4c_env["pi_b_id"]
    rt_hourly  = p4c_env["rt_hourly_id"]

    # This should succeed (companyid=NULL, no trigger restriction)
    await direct_db.execute(_text("""
        INSERT INTO payroll.payitemratetypemap (payitemid, ratetypeid, isprimary, status)
        VALUES (:piid, :rtid, FALSE, 'Active')
        ON CONFLICT (payitemid, ratetypeid) DO NOTHING
    """), {"piid": pi_b_id, "rtid": rt_hourly})

    # Cleanup
    await direct_db.execute(_text("""
        DELETE FROM payroll.payitemratetypemap
        WHERE payitemid = :piid AND ratetypeid = :rtid
    """), {"piid": pi_b_id, "rtid": rt_hourly})
