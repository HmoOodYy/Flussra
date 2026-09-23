"""
Phase 4B.2 + 4B.3 — Cross-company RateType scope enforcement (full closure).

4B.2 Covers:
  T1  Direct create_rate with foreign custom RateType → 422
  T2  Two-step mapping exploit blocked at settings endpoint → 422
  T3  Mixed-mapping contaminated DB still blocked at all write endpoints
  T4  System RateType (HOURLY) still works for both companies
  T5  Same-company custom RateType still works
  T6  Unmapped active RateType → create_rate 422
  T7  /payroll/rate-types does not leak foreign custom RateType to Company B
  T8  Rate matrix (real endpoint) does not include foreign custom RateType
  T9  Batch save rejects cross-company custom RateType precisely (422)

4B.3 Covers (orphaned CPI_ exploit):
  T10 Physical PayItem delete deactivates orphaned CPI_ RateType; invisible + unmappable
  T11 assign_rate_type_to_pay_item rejects unmapped/orphaned active CPI_ RateType
  T12 /payroll/rate-types hides unmapped CPI_ types from all companies
  T13 batch_save_rates rejects contaminated mapping via defense-in-depth
  T14 Rate matrix hides contaminated (direct-DB) mapping
  T15 finalize_period refuses contaminated DriverRate using foreign CPI_ RateType

Environment:
  Company A == DEMO (seeded by conftest).
  Company B is created fresh per-test via p4b_env fixture.
  All fixture-created rows are cleaned up in teardown.
"""
import pytest
import pytest_asyncio
import httpx
from decimal import Decimal
from sqlalchemy import text as _text


# ---------------------------------------------------------------------------
# p4b_env fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def p4b_env(direct_db, client: httpx.AsyncClient, auth_token: str):
    """
    Sets up:
      - Company A (DEMO) custom PayItem + custom RateType + PayItemRateTypeMap
      - Company A custom PayItem activated on HQ branch
      - Company A driver (P4B-DRV-A) via API
      - Company B (COMP_B_P4B): company + branch + admin user + driver
      - Company B custom PayItem (CPI_P4B_B) + its own auto-generated RateType mapping
      - BranchPayItemConfig activating Company B's PayItem on Branch B

    Yields a dict with all relevant IDs.
    Teardown removes all created rows in dependency order.
    """
    # ── Look up Company A ────────────────────────────────────────────────────
    row = (await direct_db.execute(_text(
        "SELECT companyid FROM core.companies WHERE companycode = 'DEMO'"
    ))).mappings().first()
    cid_a = row["companyid"]

    row = (await direct_db.execute(_text(
        "SELECT branchid FROM core.branches WHERE companyid = :cid AND branchcode = 'HQ'"
    ), {"cid": cid_a})).mappings().first()
    bid_a = row["branchid"]

    # ── System HOURLY rate type ──────────────────────────────────────────────
    row = (await direct_db.execute(_text(
        "SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = 'HOURLY'"
    ))).mappings().first()
    rt_hourly_id = row["ratetypeid"]

    # ── Company A custom PayItem + RateType ──────────────────────────────────
    pi_a_row = (await direct_db.execute(_text("""
        INSERT INTO payroll.payitems
            (companyid, payitemcode, payitemname, ratebehavior,
             requiresrate, isdefaultbranchactive, status,
             category, datatype, itemscope)
        VALUES
            (:cid, 'CPI_P4B', 'P4B Custom Item A', 'PerUnit',
             TRUE, TRUE, 'Active', 'Custom', 'Decimal', 'Daily')
        ON CONFLICT (companyid, payitemcode) WHERE companyid IS NOT NULL
            DO UPDATE SET status = 'Active'
        RETURNING payitemid
    """), {"cid": cid_a})).mappings().first()
    pi_a_id = pi_a_row["payitemid"]

    # Phase 4C: set companyid so CPI_P4B_RT is structurally owned by Company A.
    # Without this, companyid=NULL would make it a system type and any company could use it.
    rt_a_row = (await direct_db.execute(_text("""
        INSERT INTO payroll.ratetypes (ratecode, ratename, unitname, isactive, companyid)
        VALUES ('CPI_P4B_RT', 'P4B Custom Rate Type A', 'Unit', TRUE, :cid)
        ON CONFLICT (ratecode) DO UPDATE SET isactive = TRUE, companyid = :cid
        RETURNING ratetypeid
    """), {"cid": cid_a})).mappings().first()
    rt_a_custom_id = rt_a_row["ratetypeid"]

    await direct_db.execute(_text("""
        INSERT INTO payroll.payitemratetypemap (payitemid, ratetypeid, isprimary, status)
        VALUES (:piid, :rtid, TRUE, 'Active')
        ON CONFLICT DO NOTHING
    """), {"piid": pi_a_id, "rtid": rt_a_custom_id})

    await direct_db.execute(_text("""
        INSERT INTO payroll.branchpayitemconfig
            (companyid, branchid, payitemid, isactive, effectivefrom)
        VALUES (:cid, :bid, :piid, TRUE, '2000-01-01')
        ON CONFLICT DO NOTHING
    """), {"cid": cid_a, "bid": bid_a, "piid": pi_a_id})

    # ── Company A driver ─────────────────────────────────────────────────────
    await direct_db.execute(_text(
        "DELETE FROM payroll.driverrates WHERE driverid IN "
        "(SELECT driverid FROM core.drivers WHERE drivercode = 'P4B-DRV-A')"
    ))
    await direct_db.execute(_text(
        "DELETE FROM core.drivers WHERE drivercode = 'P4B-DRV-A'"
    ))
    resp = await client.post(
        "/core/drivers",
        json={
            "branch_id": bid_a,
            "full_name": "P4B Driver A",
            "preferred_name": "P4B-A",
            "driver_code": "P4B-DRV-A",
            "cdl_number": "CDL-P4B-A",
            "email": "p4bdrva@example.com",
        },
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 201, f"Driver A creation failed: {resp.text}"
    driver_a_id = resp.json()["driver_id"]

    # ── Company B — clean up any leftover from a previous run ────────────────
    await direct_db.execute(_text("""
        DELETE FROM payroll.driverrates WHERE companyid IN
            (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4B')
    """))
    await direct_db.execute(_text("""
        DELETE FROM payroll.branchpayitemconfig WHERE payitemid IN (
            SELECT payitemid FROM payroll.payitems WHERE companyid IN
                (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4B')
        )
    """))
    await direct_db.execute(_text("""
        DELETE FROM payroll.payitemsettings WHERE payitemid IN (
            SELECT payitemid FROM payroll.payitems WHERE companyid IN
                (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4B')
        )
    """))
    await direct_db.execute(_text("""
        DELETE FROM payroll.payitemratetypemap WHERE payitemid IN (
            SELECT payitemid FROM payroll.payitems WHERE companyid IN
                (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4B')
        )
    """))
    await direct_db.execute(_text("""
        DELETE FROM payroll.payitems WHERE companyid IN
            (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4B')
    """))
    await direct_db.execute(_text("""
        DELETE FROM core.drivers WHERE companyid IN
            (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4B')
    """))
    await direct_db.execute(_text("""
        DELETE FROM core.employees WHERE companyid IN
            (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4B')
    """))
    await direct_db.execute(_text("""
        DELETE FROM sec.userbranchroles WHERE companyid IN
            (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4B')
    """))
    await direct_db.execute(_text("""
        DELETE FROM sec.users WHERE companyid IN
            (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4B')
    """))
    await direct_db.execute(_text("""
        DELETE FROM core.branches WHERE companyid IN
            (SELECT companyid FROM core.companies WHERE companycode = 'COMP_B_P4B')
    """))
    await direct_db.execute(_text(
        "DELETE FROM core.companies WHERE companycode = 'COMP_B_P4B'"
    ))

    # ── Create Company B ─────────────────────────────────────────────────────
    b_row = (await direct_db.execute(_text("""
        INSERT INTO core.companies
            (companycode, companyname, legalname, status, issuspended, timezonename)
        VALUES ('COMP_B_P4B', 'Company B P4B', 'Company B P4B Ltd', 'Active', FALSE, 'UTC')
        RETURNING companyid
    """))).mappings().first()
    cid_b = b_row["companyid"]

    br_row = (await direct_db.execute(_text("""
        INSERT INTO core.branches
            (companyid, branchcode, branchname, status, isdefault)
        VALUES (:cid, 'HQ_B', 'HQ Branch B', 'Active', TRUE)
        RETURNING branchid
    """), {"cid": cid_b})).mappings().first()
    bid_b = br_row["branchid"]

    from app.auth.security import hash_password
    pw_hash = hash_password("TestPass123!")

    u_row = (await direct_db.execute(_text("""
        INSERT INTO sec.users
            (companyid, username, displayname, passwordhash, isactive, canlogin)
        VALUES (:cid, 'admin_b_p4b', 'Admin B P4B', :pw, TRUE, TRUE)
        RETURNING userid
    """), {"cid": cid_b, "pw": pw_hash})).mappings().first()
    uid_b = u_row["userid"]

    role_row = (await direct_db.execute(_text(
        "SELECT roleid FROM sec.roles WHERE rolecode = 'PAYROLL_ADMIN'"
    ))).mappings().first()
    await direct_db.execute(_text("""
        INSERT INTO sec.userbranchroles
            (userid, companyid, branchid, roleid, scopetype, isactive)
        VALUES (:uid, :cid, NULL, :rid, 'AllCompanyBranches', TRUE)
    """), {"uid": uid_b, "cid": cid_b, "rid": role_row["roleid"]})

    # ── Company B driver (via API after login) ───────────────────────────────
    token_b_setup = await _get_token_b(client)
    resp_b_drv = await client.post(
        "/core/drivers",
        json={
            "branch_id": bid_b,
            "full_name": "P4B Driver B",
            "preferred_name": "P4B-B",
            "driver_code": "P4B-DRV-B",
            "cdl_number": "CDL-P4B-B",
            "email": "p4bdrvb@example.com",
        },
        headers={"Authorization": f"Bearer {token_b_setup}"},
    )
    assert resp_b_drv.status_code == 201, f"Driver B creation failed: {resp_b_drv.text}"
    driver_b_id = resp_b_drv.json()["driver_id"]

    # ── Company B custom PayItem via direct DB (LLR-A blocks HTTP creation) ──
    pi_b_row = (await direct_db.execute(_text("""
        INSERT INTO payroll.payitems
            (companyid, payitemcode, payitemname, ratebehavior,
             requiresrate, isdefaultbranchactive, status,
             category, datatype, itemscope, unit)
        VALUES
            (:cid, 'CPI_P4B_B', 'P4B Custom Item B', 'PerUnit',
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
        RETURNING ratetypeid
    """), {"code": f"CPI_{pi_b_id}_1", "name": "P4B Custom Item B Rate", "cid": cid_b})).mappings().first()
    rt_b_own_id = rt_b_row["ratetypeid"]

    await direct_db.execute(_text("""
        INSERT INTO payroll.payitemratetypemap (payitemid, ratetypeid, isprimary, status)
        VALUES (:piid, :rtid, TRUE, 'Active')
        ON CONFLICT DO NOTHING
    """), {"piid": pi_b_id, "rtid": rt_b_own_id})

    # Activate Company B's PayItem on Branch B
    await direct_db.execute(_text("""
        INSERT INTO payroll.branchpayitemconfig
            (companyid, branchid, payitemid, isactive, effectivefrom)
        VALUES (:cid, :bid, :piid, TRUE, '2000-01-01')
        ON CONFLICT DO NOTHING
    """), {"cid": cid_b, "bid": bid_b, "piid": pi_b_id})

    yield {
        "cid_a":          cid_a,
        "bid_a":          bid_a,
        "driver_a_id":    driver_a_id,
        "pi_a_id":        pi_a_id,
        "rt_a_custom_id": rt_a_custom_id,
        "cid_b":          cid_b,
        "bid_b":          bid_b,
        "uid_b":          uid_b,
        "driver_b_id":    driver_b_id,
        "pi_b_id":        pi_b_id,
        "rt_b_own_id":    rt_b_own_id,
        "rt_hourly_id":   rt_hourly_id,
        "auth_a":         auth_token,
    }

    # ── Teardown ─────────────────────────────────────────────────────────────
    await direct_db.execute(_text(
        "DELETE FROM payroll.driverrates WHERE companyid = :cid"
    ), {"cid": cid_b})
    # Capture auto-generated RateType IDs before deleting mappings
    rt_b_auto_ids = (await direct_db.execute(_text("""
        SELECT DISTINCT rt.ratetypeid
        FROM payroll.ratetypes rt
        JOIN payroll.payitemratetypemap pirm ON pirm.ratetypeid = rt.ratetypeid
        JOIN payroll.payitems pi ON pi.payitemid = pirm.payitemid
        WHERE pi.companyid = :cid
    """), {"cid": cid_b})).scalars().all()
    await direct_db.execute(_text("""
        DELETE FROM payroll.branchpayitemconfig WHERE payitemid IN
            (SELECT payitemid FROM payroll.payitems WHERE companyid = :cid)
    """), {"cid": cid_b})
    await direct_db.execute(_text("""
        DELETE FROM payroll.payitemsettings WHERE payitemid IN
            (SELECT payitemid FROM payroll.payitems WHERE companyid = :cid)
    """), {"cid": cid_b})
    await direct_db.execute(_text("""
        DELETE FROM payroll.payitemratetypemap WHERE payitemid IN
            (SELECT payitemid FROM payroll.payitems WHERE companyid = :cid)
    """), {"cid": cid_b})
    await direct_db.execute(_text(
        "DELETE FROM payroll.payitems WHERE companyid = :cid"
    ), {"cid": cid_b})
    # Delete auto-generated RateTypes that are now unmapped
    for rt_id in rt_b_auto_ids:
        await direct_db.execute(_text("""
            DELETE FROM payroll.ratetypes WHERE ratetypeid = :rtid
              AND NOT EXISTS (
                SELECT 1 FROM payroll.payitemratetypemap WHERE ratetypeid = :rtid
              )
        """), {"rtid": rt_id})
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

    # Company A custom data cleanup
    await direct_db.execute(_text(
        "DELETE FROM payroll.driverrates WHERE ratetypeid = :rtid"
    ), {"rtid": rt_a_custom_id})
    await direct_db.execute(_text(
        "DELETE FROM payroll.branchpayitemconfig WHERE payitemid = :piid"
    ), {"piid": pi_a_id})
    await direct_db.execute(_text(
        "DELETE FROM payroll.payitemsettings WHERE payitemid = :piid"
    ), {"piid": pi_a_id})
    await direct_db.execute(_text(
        "DELETE FROM payroll.payitemratetypemap WHERE ratetypeid = :rtid"
    ), {"rtid": rt_a_custom_id})
    await direct_db.execute(_text(
        "DELETE FROM payroll.ratetypes WHERE ratetypeid = :rtid"
    ), {"rtid": rt_a_custom_id})
    await direct_db.execute(_text(
        "DELETE FROM payroll.payitems WHERE payitemid = :piid"
    ), {"piid": pi_a_id})
    await direct_db.execute(_text(
        "DELETE FROM core.drivers WHERE drivercode = 'P4B-DRV-A'"
    ))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_token_b(client: httpx.AsyncClient) -> str:
    resp = await client.post("/auth/login", json={
        "username":     "admin_b_p4b",
        "password":     "TestPass123!",
        "company_code": "COMP_B_P4B",
    })
    assert resp.status_code == 200, f"Company B auth failed: {resp.text}"
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _insert_contaminated_rate(
    direct_db,
    cid: int, bid: int, did: int, rtid: int,
    amount: str, effective_from: str,
) -> int:
    """Bypass service to insert a DriverRate with a foreign RateType directly."""
    from datetime import date as _date
    ef_date = _date.fromisoformat(effective_from)
    row = (await direct_db.execute(_text("""
        INSERT INTO payroll.driverrates
            (companyid, branchid, driverid, ratetypeid, amount, effectivefrom, status)
        VALUES (:cid, :bid, :did, :rtid, :amt, :ef, 'PendingApproval')
        RETURNING driverrateid
    """), {"cid": cid, "bid": bid, "did": did, "rtid": rtid,
           "amt": amount, "ef": ef_date})).mappings().first()
    return row["driverrateid"]


async def _bypass_trigger_insert_map(direct_db, piid: int, rtid: int) -> None:
    """
    Insert a PayItemRateTypeMap bypassing the Phase 4C ownership trigger.

    Phase 4C adds trg_guard_payitemratetypemap_ownership which blocks cross-company
    PayItemRateTypeMap inserts at the DB level.  Some regression tests simulate a
    DBA-level attack (direct INSERT into the DB bypassing all application guards),
    which is equivalent to what a superuser could do.  This helper disables the
    trigger for the insert then re-enables it, preserving the trigger for all other
    operations.

    The service-layer guards (structural CompanyID check) are still exercised after
    this setup, proving defence-in-depth remains intact even if the DB trigger is
    somehow bypassed.
    """
    await direct_db.execute(_text(
        "ALTER TABLE payroll.payitemratetypemap "
        "DISABLE TRIGGER trg_guard_payitemratetypemap_ownership"
    ))
    try:
        await direct_db.execute(_text("""
            INSERT INTO payroll.payitemratetypemap (payitemid, ratetypeid, isprimary, status)
            VALUES (:piid, :rtid, FALSE, 'Active')
            ON CONFLICT (payitemid, ratetypeid) DO NOTHING
        """), {"piid": piid, "rtid": rtid})
    finally:
        await direct_db.execute(_text(
            "ALTER TABLE payroll.payitemratetypemap "
            "ENABLE TRIGGER trg_guard_payitemratetypemap_ownership"
        ))


# ===========================================================================
# T1 — Direct create_rate with foreign custom RateType → 422, no DB row
# ===========================================================================

@pytest.mark.asyncio
async def test_t1_create_rate_rejects_foreign_custom_rate_type(
    p4b_env, client: httpx.AsyncClient, direct_db
):
    """
    Company B directly calls create_rate with Company A's custom CPI_P4B_RT.
    Must be rejected 422.  No DriverRate row must be inserted.
    """
    token_b  = await _get_token_b(client)
    rt_a_id  = p4b_env["rt_a_custom_id"]
    drv_b_id = p4b_env["driver_b_id"]

    resp = await client.post(
        "/payroll/rates",
        json={
            "driver_id":      drv_b_id,
            "rate_type_id":   rt_a_id,
            "amount":         "10.00",
            "effective_from": "2055-01-01",
        },
        headers=_auth(token_b),
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
    assert "does not belong to this company" in resp.json()["detail"].lower()

    row = (await direct_db.execute(_text("""
        SELECT 1 FROM payroll.driverrates
        WHERE driverid = :did AND ratetypeid = :rtid
    """), {"did": drv_b_id, "rtid": rt_a_id})).first()
    assert row is None, "No DriverRate row must exist after rejected create_rate"


# ===========================================================================
# T2 — Two-step mapping exploit blocked at settings endpoint
# ===========================================================================

@pytest.mark.asyncio
async def test_t2_two_step_mapping_exploit_blocked(
    p4b_env, client: httpx.AsyncClient, direct_db
):
    """
    Company B tries the two-step exploit:
      Step 1 — Company B creates own PayItem (done in fixture as CPI_P4B_B).
      Step 2 — Company B maps CPI_P4B_B to Company A's CPI_P4B_RT via settings endpoint.

    The settings endpoint must return 422.
    No PayItemRateTypeMap row for (CPI_P4B_B, CPI_P4B_RT) must be created.
    Subsequent create_rate with CPI_P4B_RT + Company B driver must still fail 422.
    """
    token_b  = await _get_token_b(client)
    rt_a_id  = p4b_env["rt_a_custom_id"]
    pi_b_id  = p4b_env["pi_b_id"]
    drv_b_id = p4b_env["driver_b_id"]

    # Step 2: attempt to map Company B PayItem → Company A RateType
    resp_map = await client.post(
        f"/settings/pay-items/{pi_b_id}/rate-type-map",
        json={"rate_type_id": rt_a_id, "is_primary": True},
        headers=_auth(token_b),
    )
    assert resp_map.status_code == 422, (
        f"Mapping exploit must be blocked 422, got {resp_map.status_code}: {resp_map.text}"
    )
    assert "does not belong to this company" in resp_map.json()["detail"].lower()

    # Confirm no row was inserted
    map_row = (await direct_db.execute(_text("""
        SELECT 1 FROM payroll.payitemratetypemap
        WHERE payitemid = :piid AND ratetypeid = :rtid
    """), {"piid": pi_b_id, "rtid": rt_a_id})).first()
    assert map_row is None, "PayItemRateTypeMap must NOT be created by the blocked exploit"

    # Confirm create_rate is still rejected for Company B with the foreign RateType
    resp_rate = await client.post(
        "/payroll/rates",
        json={
            "driver_id":      drv_b_id,
            "rate_type_id":   rt_a_id,
            "amount":         "10.00",
            "effective_from": "2056-01-01",
        },
        headers=_auth(token_b),
    )
    assert resp_rate.status_code == 422, (
        f"create_rate must still be rejected after blocked mapping, "
        f"got {resp_rate.status_code}: {resp_rate.text}"
    )


# ===========================================================================
# T3 — Mixed contaminated DB blocked at all write endpoints
# ===========================================================================

@pytest.mark.asyncio
async def test_t3_mixed_mapping_contaminated_db_blocked(
    p4b_env, client: httpx.AsyncClient, direct_db
):
    """
    Simulate the contaminated state by:
      1. Directly inserting PayItemRateTypeMap: Company B PayItem → Company A RateType
         (bypassing service guard to test the helper itself, not just the guard).
      2. Directly inserting a contaminated DriverRate for Company B.
    Then verify:
      - create_rate with that RateType for Company B → 422
      - approve_rate contaminated row → 422, status unchanged
      - update_rate contaminated row → 422, amount unchanged
      - batch_save with Company B PayItem + Company A RateType → 422, no partial write
    """
    token_b  = await _get_token_b(client)
    rt_a_id  = p4b_env["rt_a_custom_id"]
    pi_b_id  = p4b_env["pi_b_id"]
    drv_b_id = p4b_env["driver_b_id"]
    cid_b    = p4b_env["cid_b"]
    bid_b    = p4b_env["bid_b"]

    # --- Insert mixed mapping directly (simulating DBA-level attack) ---
    # Phase 4C: the ownership trigger blocks this normally; bypass it to simulate
    # a superuser inserting bad data directly, then verify service still rejects.
    await _bypass_trigger_insert_map(direct_db, pi_b_id, rt_a_id)

    contaminated_rate_id = await _insert_contaminated_rate(
        direct_db, cid_b, bid_b, drv_b_id, rt_a_id, "50.00", "2057-01-01"
    )

    try:
        # (a) create_rate — must reject
        resp = await client.post(
            "/payroll/rates",
            json={
                "driver_id": drv_b_id, "rate_type_id": rt_a_id,
                "amount": "10.00", "effective_from": "2057-06-01",
            },
            headers=_auth(token_b),
        )
        assert resp.status_code == 422, (
            f"create_rate must reject mixed-mapped RateType, got {resp.status_code}: {resp.text}"
        )

        # (b) approve_rate — must reject, status must not change
        resp = await client.post(
            f"/payroll/rates/{contaminated_rate_id}/approve",
            headers=_auth(token_b),
        )
        assert resp.status_code == 422, (
            f"approve_rate must reject contaminated row, got {resp.status_code}: {resp.text}"
        )
        status_row = (await direct_db.execute(_text(
            "SELECT status FROM payroll.driverrates WHERE driverrateid = :rid"
        ), {"rid": contaminated_rate_id})).mappings().first()
        assert status_row["status"] == "PendingApproval"

        # (c) update_rate — must reject, amount unchanged
        resp = await client.patch(
            f"/payroll/rates/{contaminated_rate_id}",
            json={"amount": "999.99"},
            headers=_auth(token_b),
        )
        assert resp.status_code == 422, (
            f"update_rate must reject contaminated row, got {resp.status_code}: {resp.text}"
        )
        amt_row = (await direct_db.execute(_text(
            "SELECT amount FROM payroll.driverrates WHERE driverrateid = :rid"
        ), {"rid": contaminated_rate_id})).mappings().first()
        assert Decimal(str(amt_row["amount"])) == Decimal("50.00")

        # (d) batch_save — must reject, no partial write
        resp = await client.post(
            f"/payroll/drivers/{drv_b_id}/rates/batch",
            json={
                "effective_from": "2057-07-01",
                "changes": [
                    {"pay_item_id": pi_b_id, "rate_type_id": rt_a_id, "amount": "8.00"}
                ],
            },
            headers=_auth(token_b),
        )
        assert resp.status_code == 422, (
            f"batch_save must reject cross-company custom RateType, "
            f"got {resp.status_code}: {resp.text}"
        )
        batch_row = (await direct_db.execute(_text("""
            SELECT 1 FROM payroll.driverrates
            WHERE driverid = :did AND ratetypeid = :rtid
              AND effectivefrom = '2057-07-01'
        """), {"did": drv_b_id, "rtid": rt_a_id})).first()
        assert batch_row is None, "Batch must not have written a new rate row"

    finally:
        # Remove contaminated mapping and rate
        await direct_db.execute(_text("""
            DELETE FROM payroll.payitemratetypemap
            WHERE payitemid = :piid AND ratetypeid = :rtid
        """), {"piid": pi_b_id, "rtid": rt_a_id})
        await direct_db.execute(_text(
            "DELETE FROM payroll.driverrates WHERE driverrateid = :rid"
        ), {"rid": contaminated_rate_id})


# ===========================================================================
# T4 — System RateType (HOURLY) still works for both companies
# ===========================================================================

@pytest.mark.asyncio
async def test_t4_system_rate_type_works_for_both_companies(
    p4b_env, client: httpx.AsyncClient, direct_db
):
    """
    HOURLY is a true system rate type mapped to a system PayItem (CompanyID IS NULL).
    Both Company A and Company B must be able to create DriverRates with it.
    """
    token_a      = p4b_env["auth_a"]
    token_b      = await _get_token_b(client)
    rt_hourly_id = p4b_env["rt_hourly_id"]
    drv_a_id     = p4b_env["driver_a_id"]
    drv_b_id     = p4b_env["driver_b_id"]

    ids_to_clean: list[int] = []
    try:
        # Company A
        resp_a = await client.post(
            "/payroll/rates",
            json={"driver_id": drv_a_id, "rate_type_id": rt_hourly_id,
                  "amount": "20.00", "effective_from": "2058-01-01"},
            headers=_auth(token_a),
        )
        assert resp_a.status_code == 201, (
            f"Company A must create HOURLY rate, got {resp_a.status_code}: {resp_a.text}"
        )
        ids_to_clean.append(resp_a.json()["driver_rate_id"])

        # Company B
        resp_b = await client.post(
            "/payroll/rates",
            json={"driver_id": drv_b_id, "rate_type_id": rt_hourly_id,
                  "amount": "18.00", "effective_from": "2058-01-01"},
            headers=_auth(token_b),
        )
        assert resp_b.status_code == 201, (
            f"Company B must create HOURLY rate, got {resp_b.status_code}: {resp_b.text}"
        )
        ids_to_clean.append(resp_b.json()["driver_rate_id"])

    finally:
        for rid in ids_to_clean:
            await direct_db.execute(_text(
                "DELETE FROM payroll.driverrates WHERE driverrateid = :rid"
            ), {"rid": rid})


# ===========================================================================
# T5 — Same-company custom RateType still works
# ===========================================================================

@pytest.mark.asyncio
async def test_t5_same_company_custom_rate_type_works(
    p4b_env, client: httpx.AsyncClient, direct_db
):
    """
    Company A admin creates a DriverRate using Company A's own custom RateType (CPI_P4B_RT).
    Must succeed 201.
    The rate matrix for Company A's driver must include the custom rate type.
    """
    token_a      = p4b_env["auth_a"]
    rt_a_id      = p4b_env["rt_a_custom_id"]
    drv_a_id     = p4b_env["driver_a_id"]

    resp = await client.post(
        "/payroll/rates",
        json={"driver_id": drv_a_id, "rate_type_id": rt_a_id,
              "amount": "12.50", "effective_from": "2059-01-01"},
        headers=_auth(token_a),
    )
    assert resp.status_code == 201, (
        f"Company A must create its own custom rate, got {resp.status_code}: {resp.text}"
    )
    rate_id = resp.json()["driver_rate_id"]
    await direct_db.execute(_text(
        "DELETE FROM payroll.driverrates WHERE driverrateid = :rid"
    ), {"rid": rate_id})

    # Rate matrix for Company A's driver must include CPI_P4B_RT
    from datetime import date
    resp_matrix = await client.get(
        f"/payroll/drivers/{drv_a_id}/rate-matrix",
        params={"as_of": "2059-06-01"},
        headers=_auth(token_a),
    )
    assert resp_matrix.status_code == 200, (
        f"Rate matrix must return 200, got {resp_matrix.status_code}: {resp_matrix.text}"
    )
    groups = resp_matrix.json().get("groups", [])
    rt_ids_in_matrix = {g["rate_type_id"] for g in groups}
    assert rt_a_id in rt_ids_in_matrix, (
        f"Company A's custom RateType {rt_a_id} must appear in Company A's rate matrix"
    )


# ===========================================================================
# T6 — Unmapped active RateType is rejected by create_rate
# ===========================================================================

@pytest.mark.asyncio
async def test_t6_unmapped_active_rate_type_rejected(
    p4b_env, client: httpx.AsyncClient, direct_db
):
    """
    Phase 4C: create_rate must reject a RateType that belongs to a different company.

    An active RateType owned by Company B (companyid=cid_b) cannot be used by
    Company A (structural ownership check: rt.companyid != cid_a -> 422).

    Previously (Phase 4B) the rejection was due to the unmapped/orphaned check;
    Phase 4C makes the check structural via RateTypes.CompanyID.
    """
    token_a  = p4b_env["auth_a"]
    drv_a_id = p4b_env["driver_a_id"]
    cid_b    = p4b_env["cid_b"]

    # Insert an active RateType owned by Company B (foreign from Company A's perspective)
    unmapped_row = (await direct_db.execute(_text("""
        INSERT INTO payroll.ratetypes (ratecode, ratename, unitname, isactive, companyid)
        VALUES ('P4B_UNMAPPED', 'P4B Unmapped Test', 'Unit', TRUE, :cid_b)
        ON CONFLICT (ratecode) DO UPDATE SET isactive = TRUE, companyid = :cid_b
        RETURNING ratetypeid
    """), {"cid_b": cid_b})).mappings().first()
    unmapped_rt_id = unmapped_row["ratetypeid"]

    try:
        # Company A tries to create a rate using Company B's RateType -> 422
        resp = await client.post(
            "/payroll/rates",
            json={"driver_id": drv_a_id, "rate_type_id": unmapped_rt_id,
                  "amount": "5.00", "effective_from": "2060-01-01"},
            headers=_auth(token_a),
        )
        assert resp.status_code == 422, (
            f"Foreign RateType (owned by Company B) must be rejected 422 for Company A, "
            f"got {resp.status_code}: {resp.text}"
        )

        # No DB row inserted
        row = (await direct_db.execute(_text("""
            SELECT 1 FROM payroll.driverrates
            WHERE driverid = :did AND ratetypeid = :rtid
        """), {"did": drv_a_id, "rtid": unmapped_rt_id})).first()
        assert row is None, "No DriverRate row must exist for foreign-company RateType"

    finally:
        await direct_db.execute(_text(
            "DELETE FROM payroll.ratetypes WHERE ratetypeid = :rtid"
        ), {"rtid": unmapped_rt_id})


# ===========================================================================
# T7 — /payroll/rate-types hides foreign custom CPI RateTypes
# ===========================================================================

@pytest.mark.asyncio
async def test_t7_rate_types_endpoint_hides_foreign_custom_types(
    p4b_env, client: httpx.AsyncClient
):
    """
    Company B calls GET /payroll/rate-types.
    Must NOT contain Company A's CPI_P4B_RT (foreign custom).
    Must contain system rate types (HOURLY, MILEAGE, etc.).
    Company B's own custom RateType (CPI_P4B_B's auto-generated RT) must appear.
    """
    token_b      = await _get_token_b(client)
    rt_a_id      = p4b_env["rt_a_custom_id"]
    rt_b_own_id  = p4b_env["rt_b_own_id"]
    rt_hourly_id = p4b_env["rt_hourly_id"]

    resp = await client.get("/payroll/rate-types", headers=_auth(token_b))
    assert resp.status_code == 200, f"rate-types must return 200: {resp.text}"

    rate_types = resp.json()
    rate_type_ids  = {rt["rate_type_id"] for rt in rate_types}
    rate_type_codes = {rt["rate_code"] for rt in rate_types}

    # Foreign CPI_ must be absent
    assert rt_a_id not in rate_type_ids, (
        "Company A's custom CPI_P4B_RT must NOT appear in Company B's rate-types list"
    )
    assert "CPI_P4B_RT" not in rate_type_codes, (
        "CPI_P4B_RT code must not appear for Company B"
    )

    # System types must be present
    assert rt_hourly_id in rate_type_ids, "System HOURLY must appear for Company B"
    assert "HOURLY" in rate_type_codes

    # Company B's own custom RateType must be present (if found in fixture)
    if rt_b_own_id is not None:
        assert rt_b_own_id in rate_type_ids, (
            f"Company B's own custom RateType {rt_b_own_id} must appear for Company B"
        )


# ===========================================================================
# T8 — Rate matrix (real endpoint) does not include foreign custom RateType
# ===========================================================================

@pytest.mark.asyncio
async def test_t8_rate_matrix_does_not_include_foreign_custom_rate_type(
    p4b_env, client: httpx.AsyncClient
):
    """
    Company B calls GET /payroll/drivers/{driver_b_id}/rate-matrix.
    The response must return 200 (Company B's driver has its own custom pay item
    activated on Branch B, so the matrix is non-empty).
    Company A's custom CPI_P4B_RT must NOT appear in the matrix.
    """
    token_b      = await _get_token_b(client)
    drv_b_id     = p4b_env["driver_b_id"]
    rt_a_id      = p4b_env["rt_a_custom_id"]

    resp = await client.get(
        f"/payroll/drivers/{drv_b_id}/rate-matrix",
        params={"as_of": "2061-06-01"},
        headers=_auth(token_b),
    )
    assert resp.status_code == 200, (
        f"Rate matrix must return 200 for Company B driver, "
        f"got {resp.status_code}: {resp.text}"
    )

    groups = resp.json().get("groups", [])
    rt_ids_in_matrix = {g["rate_type_id"] for g in groups}
    assert rt_a_id not in rt_ids_in_matrix, (
        f"Company A's custom RateType {rt_a_id} must NOT appear in Company B's rate matrix"
    )


# ===========================================================================
# T9 — Batch save rejects cross-company custom RateType precisely (422)
# ===========================================================================

@pytest.mark.asyncio
async def test_t9_batch_save_rejects_cross_company_rate_type_precisely(
    p4b_env, client: httpx.AsyncClient, direct_db
):
    """
    Company B calls POST /payroll/drivers/{driver_b_id}/rates/batch
    with Company A's custom PayItem + Company A's custom RateType.
    Must return exactly 422 (not 404, not 200).
    No DriverRate row must be created.
    """
    token_b  = await _get_token_b(client)
    rt_a_id  = p4b_env["rt_a_custom_id"]
    pi_a_id  = p4b_env["pi_a_id"]
    drv_b_id = p4b_env["driver_b_id"]

    resp = await client.post(
        f"/payroll/drivers/{drv_b_id}/rates/batch",
        json={
            "effective_from": "2062-01-01",
            "changes": [
                {"pay_item_id": pi_a_id, "rate_type_id": rt_a_id, "amount": "8.00"}
            ],
        },
        headers=_auth(token_b),
    )
    assert resp.status_code == 422, (
        f"Batch save must return 422 for cross-company custom RateType, "
        f"got {resp.status_code}: {resp.text}"
    )

    # No rate row must be created
    row = (await direct_db.execute(_text("""
        SELECT 1 FROM payroll.driverrates
        WHERE driverid = :did AND ratetypeid = :rtid
    """), {"did": drv_b_id, "rtid": rt_a_id})).first()
    assert row is None, "No DriverRate must exist after rejected batch save"


# ===========================================================================
# Phase 4B.3 tests — Orphaned CPI_ RateType exploit
# ===========================================================================

# ===========================================================================
# T10 — Physical delete deactivates orphaned CPI_ RateType; it is invisible
#       and unmappable by another company
# ===========================================================================

@pytest.mark.asyncio
async def test_t10_orphaned_cpi_after_physical_delete(
    p4b_env, client: httpx.AsyncClient, direct_db
):
    """
    Phase 4B.3 exploit path:
      1. Company A creates a custom PayItem via API → CPI_ RateType auto-created.
      2. Company A physically deletes the PayItem via API.
      3. The generated CPI_ RateType must be deactivated (not claimable).
      4. Company B must NOT see it in /payroll/rate-types.
      5. Company B must NOT be able to map its PayItem to that RateType.
      6. Company B must NOT be able to create_rate with that RateType.
    """
    token_a = p4b_env["auth_a"]
    token_b = await _get_token_b(client)
    pi_b_id = p4b_env["pi_b_id"]

    # Step 1: Company A seeds a throw-away custom PayItem via direct DB (LLR-A blocks HTTP)
    cid_a = p4b_env["cid_a"]
    pi_throw_row = (await direct_db.execute(_text("""
        INSERT INTO payroll.payitems
            (companyid, payitemcode, payitemname, ratebehavior,
             requiresrate, isdefaultbranchactive, status,
             category, datatype, itemscope, unit)
        VALUES (:cid, 'CPI_P4B_T10', 'T10 Throwaway Item', 'PerUnit',
                TRUE, FALSE, 'Active', 'Count', 'Decimal', 'Daily', 'Unit')
        ON CONFLICT (companyid, payitemcode) WHERE companyid IS NOT NULL
            DO UPDATE SET status = 'Active'
        RETURNING payitemid
    """), {"cid": cid_a})).mappings().first()
    throwaway_pi_id = pi_throw_row["payitemid"]

    rt_throw_ins = (await direct_db.execute(_text("""
        INSERT INTO payroll.ratetypes (ratecode, ratename, unitname, isactive, companyid)
        VALUES (:code, 'T10 Throwaway Rate', 'Unit', TRUE, :cid)
        ON CONFLICT (ratecode) DO UPDATE SET isactive = TRUE, companyid = :cid
        RETURNING ratetypeid, ratecode
    """), {"code": f"CPI_{throwaway_pi_id}_1", "cid": cid_a})).mappings().first()
    rt_throwaway_id   = rt_throw_ins["ratetypeid"]
    rt_throwaway_code = rt_throw_ins["ratecode"]

    await direct_db.execute(_text("""
        INSERT INTO payroll.payitemratetypemap (payitemid, ratetypeid, isprimary, status)
        VALUES (:piid, :rtid, TRUE, 'Active')
        ON CONFLICT DO NOTHING
    """), {"piid": throwaway_pi_id, "rtid": rt_throwaway_id})
    assert rt_throwaway_code.startswith("CPI_"), (
        f"Seeded rate code must start with CPI_, got {rt_throwaway_code!r}"
    )

    # Step 2: Company A physically deletes the PayItem (no DriverRates → physical delete)
    resp_delete = await client.delete(
        f"/settings/pay-items/{throwaway_pi_id}",
        headers=_auth(token_a),
    )
    assert resp_delete.status_code == 200, f"PayItem delete failed: {resp_delete.text}"
    result_data = resp_delete.json()
    assert result_data["deletion_type"] == "physical", (
        f"Expected physical delete, got {result_data['deletion_type']!r}"
    )

    # Step 3: Verify the CPI_ RateType is now inactive
    rt_active_row = (await direct_db.execute(_text("""
        SELECT isactive FROM payroll.ratetypes WHERE ratetypeid = :rtid
    """), {"rtid": rt_throwaway_id})).mappings().first()
    if rt_active_row is not None:
        assert not rt_active_row["isactive"], (
            "Orphaned CPI_ RateType must be deactivated after PayItem physical delete"
        )
    # (if rt_active_row is None, the RateType was actually deleted — also correct)

    # Step 4: Company B must NOT see this rate type in /payroll/rate-types
    resp_list = await client.get("/payroll/rate-types", headers=_auth(token_b))
    assert resp_list.status_code == 200
    rate_type_ids = {rt["rate_type_id"] for rt in resp_list.json()}
    assert rt_throwaway_id not in rate_type_ids, (
        "Orphaned/deactivated CPI_ RateType must NOT appear in Company B's rate-types list"
    )

    # Step 5: Company B must NOT be able to map its PayItem to the orphaned RateType
    resp_map = await client.post(
        f"/settings/pay-items/{pi_b_id}/rate-type-map",
        json={"rate_type_id": rt_throwaway_id, "is_primary": False},
        headers=_auth(token_b),
    )
    assert resp_map.status_code == 422, (
        f"Mapping to orphaned/deactivated CPI_ RateType must return 422, "
        f"got {resp_map.status_code}: {resp_map.text}"
    )

    # Step 6: Company B must NOT be able to create_rate with the orphaned RateType
    drv_b_id = p4b_env["driver_b_id"]
    resp_rate = await client.post(
        "/payroll/rates",
        json={"driver_id": drv_b_id, "rate_type_id": rt_throwaway_id,
              "amount": "5.00", "effective_from": "2063-01-01"},
        headers=_auth(token_b),
    )
    assert resp_rate.status_code == 422, (
        f"create_rate with orphaned CPI_ RateType must return 422, "
        f"got {resp_rate.status_code}: {resp_rate.text}"
    )

    # Cleanup (throwaway PayItem already deleted; clean up residual RateType row if present)
    await direct_db.execute(_text(
        "DELETE FROM payroll.driverrates WHERE ratetypeid = :rtid"
    ), {"rtid": rt_throwaway_id})
    await direct_db.execute(_text(
        "DELETE FROM payroll.payitemratetypemap WHERE ratetypeid = :rtid"
    ), {"rtid": rt_throwaway_id})
    await direct_db.execute(_text(
        "DELETE FROM payroll.ratetypes WHERE ratetypeid = :rtid"
    ), {"rtid": rt_throwaway_id})


# ===========================================================================
# T11 — assign_rate_type_to_pay_item rejects unmapped active custom RateType
# ===========================================================================

@pytest.mark.asyncio
async def test_t11_assign_rejects_unmapped_active_raw_rate_type(
    p4b_env, client: httpx.AsyncClient, direct_db
):
    """
    Phase 4C: assign_rate_type_to_pay_item must reject a RateType that belongs to
    a different company (structural ownership: rt.companyid != cid_b -> 422).

    The RateType is owned by Company A (companyid=cid_a).
    Company B tries to map its PayItem to it -> must be rejected.
    No PayItemRateTypeMap row must be created (trigger also blocks it at DB level).
    """
    token_b = await _get_token_b(client)
    pi_b_id = p4b_env["pi_b_id"]
    cid_a   = p4b_env["cid_a"]

    # Insert a RateType owned by Company A (foreign from Company B's perspective)
    orphan_row = (await direct_db.execute(_text("""
        INSERT INTO payroll.ratetypes (ratecode, ratename, unitname, isactive, companyid)
        VALUES ('CPI_TEST_ORPHAN_T11', 'T11 Orphan Test', 'Unit', TRUE, :cid_a)
        ON CONFLICT (ratecode) DO UPDATE SET isactive = TRUE, companyid = :cid_a
        RETURNING ratetypeid
    """), {"cid_a": cid_a})).mappings().first()
    orphan_rt_id = orphan_row["ratetypeid"]

    try:
        resp = await client.post(
            f"/settings/pay-items/{pi_b_id}/rate-type-map",
            json={"rate_type_id": orphan_rt_id, "is_primary": False},
            headers=_auth(token_b),
        )
        assert resp.status_code == 422, (
            f"Mapping to unmapped/orphaned CPI_ RateType must return 422, "
            f"got {resp.status_code}: {resp.text}"
        )
        assert "does not belong to this company" in resp.json()["detail"].lower()

        # No PayItemRateTypeMap row must exist
        map_row = (await direct_db.execute(_text("""
            SELECT 1 FROM payroll.payitemratetypemap
            WHERE payitemid = :piid AND ratetypeid = :rtid
        """), {"piid": pi_b_id, "rtid": orphan_rt_id})).first()
        assert map_row is None, "No PayItemRateTypeMap must be created for orphaned RateType"

    finally:
        await direct_db.execute(_text(
            "DELETE FROM payroll.payitemratetypemap WHERE ratetypeid = :rtid"
        ), {"rtid": orphan_rt_id})
        await direct_db.execute(_text(
            "DELETE FROM payroll.ratetypes WHERE ratetypeid = :rtid"
        ), {"rtid": orphan_rt_id})


# ===========================================================================
# T12 — /payroll/rate-types hides unmapped non-system CPI_ RateTypes
# ===========================================================================

@pytest.mark.asyncio
async def test_t12_rate_types_endpoint_hides_unmapped_cpi_types(
    p4b_env, client: httpx.AsyncClient, direct_db
):
    """
    Phase 4C: GET /payroll/rate-types uses structural RateTypes.CompanyID.

    A CPI_ RateType owned by Company A (companyid=cid_a) must:
      - Appear in Company A's rate-types list (own-company type)
      - NOT appear in Company B's rate-types list (foreign company)

    System types (companyid=NULL) must appear for both companies.
    """
    token_a = p4b_env["auth_a"]
    token_b = await _get_token_b(client)
    cid_a   = p4b_env["cid_a"]

    # Insert a CPI_ RateType owned by Company A (structurally, no PayItemRateTypeMap needed)
    raw_row = (await direct_db.execute(_text("""
        INSERT INTO payroll.ratetypes (ratecode, ratename, unitname, isactive, companyid)
        VALUES ('CPI_T12_ORPHAN', 'T12 Orphan', 'Unit', TRUE, :cid_a)
        ON CONFLICT (ratecode) DO UPDATE SET isactive = TRUE, companyid = :cid_a
        RETURNING ratetypeid
    """), {"cid_a": cid_a})).mappings().first()
    raw_rt_id = raw_row["ratetypeid"]

    try:
        # Company A DOES see it (it's their own type)
        resp_a = await client.get("/payroll/rate-types", headers=_auth(token_a))
        assert resp_a.status_code == 200
        ids_a = {rt["rate_type_id"] for rt in resp_a.json()}
        assert raw_rt_id in ids_a, (
            "Company A's own CPI_ RateType must appear in Company A's rate-types list"
        )

        # Company B must NOT see it (foreign company's type)
        resp_b = await client.get("/payroll/rate-types", headers=_auth(token_b))
        assert resp_b.status_code == 200
        ids_b = {rt["rate_type_id"] for rt in resp_b.json()}
        assert raw_rt_id not in ids_b, (
            "Company A's CPI_ RateType must NOT appear in Company B's rate-types list"
        )

        # System types must still be present for both companies
        assert "HOURLY" in {rt["rate_code"] for rt in resp_a.json()}
        assert "HOURLY" in {rt["rate_code"] for rt in resp_b.json()}

    finally:
        await direct_db.execute(_text(
            "DELETE FROM payroll.ratetypes WHERE ratetypeid = :rtid"
        ), {"rtid": raw_rt_id})


# ===========================================================================
# T13 — batch_save_rates rejects contaminated mapping (AllowSelfApproval=false)
# ===========================================================================

@pytest.mark.asyncio
async def test_t13_batch_save_rejects_contaminated_mapping(
    p4b_env, client: httpx.AsyncClient, direct_db
):
    """
    Direct-DB inserts a contaminated PayItemRateTypeMap:
        Company B's PayItem → Company A's CPI_ RateType.
    batch_save_rates must reject the request with 422 via defense-in-depth
    (_assert_rate_type_allowed_for_company called per change).
    No DriverRate row must be created.
    """
    cid_b    = p4b_env["cid_b"]
    bid_b    = p4b_env["bid_b"]
    pi_b_id  = p4b_env["pi_b_id"]
    rt_a_id  = p4b_env["rt_a_custom_id"]
    drv_b_id = p4b_env["driver_b_id"]
    token_b  = await _get_token_b(client)

    # Force AllowSelfApproval=FALSE so we test the batch in a stricter mode
    await direct_db.execute(_text(
        "UPDATE core.companies SET allowselfapproval = FALSE WHERE companyid = :cid"
    ), {"cid": cid_b})

    # Insert the contaminated mapping bypassing the Phase 4C DB trigger (simulates DBA attack).
    # Service-layer defense-in-depth (structural CompanyID check) must still catch this.
    await _bypass_trigger_insert_map(direct_db, pi_b_id, rt_a_id)

    try:
        resp = await client.post(
            f"/payroll/drivers/{drv_b_id}/rates/batch",
            json={
                "effective_from": "2064-01-01",
                "changes": [
                    {"pay_item_id": pi_b_id, "rate_type_id": rt_a_id, "amount": "9.00"}
                ],
            },
            headers=_auth(token_b),
        )
        assert resp.status_code == 422, (
            f"batch_save must return 422 for contaminated mapping, "
            f"got {resp.status_code}: {resp.text}"
        )

        # No DriverRate row must be written
        row = (await direct_db.execute(_text("""
            SELECT 1 FROM payroll.driverrates
            WHERE driverid = :did AND ratetypeid = :rtid
        """), {"did": drv_b_id, "rtid": rt_a_id})).first()
        assert row is None, "No DriverRate must exist after contaminated batch_save rejection"

    finally:
        # Restore AllowSelfApproval and remove contaminated mapping
        await direct_db.execute(_text(
            "UPDATE core.companies SET allowselfapproval = TRUE WHERE companyid = :cid"
        ), {"cid": cid_b})
        await direct_db.execute(_text("""
            DELETE FROM payroll.payitemratetypemap
            WHERE payitemid = :piid AND ratetypeid = :rtid
        """), {"piid": pi_b_id, "rtid": rt_a_id})
        await direct_db.execute(_text("""
            DELETE FROM payroll.driverrates WHERE driverid = :did AND ratetypeid = :rtid
        """), {"did": drv_b_id, "rtid": rt_a_id})


# ===========================================================================
# T14 — Rate matrix hides contaminated mixed mapping
# ===========================================================================

@pytest.mark.asyncio
async def test_t14_matrix_hides_contaminated_mapping(
    p4b_env, client: httpx.AsyncClient, direct_db
):
    """
    Direct-DB inserts a contaminated PayItemRateTypeMap:
        Company B's PayItem → Company A's CPI_ RateType.
    GET /payroll/drivers/{driver_b_id}/rate-matrix must return 200 but
    must NOT include Company A's CPI_ RateType in any group.
    """
    pi_b_id  = p4b_env["pi_b_id"]
    rt_a_id  = p4b_env["rt_a_custom_id"]
    drv_b_id = p4b_env["driver_b_id"]
    token_b  = await _get_token_b(client)

    # Insert contaminated mapping bypassing the Phase 4C DB trigger (simulates DBA attack).
    # Matrix query must still exclude the foreign type via structural CompanyID check.
    await _bypass_trigger_insert_map(direct_db, pi_b_id, rt_a_id)

    try:
        resp = await client.get(
            f"/payroll/drivers/{drv_b_id}/rate-matrix",
            params={"as_of": "2065-06-01"},
            headers=_auth(token_b),
        )
        assert resp.status_code == 200, (
            f"Rate matrix must return 200, got {resp.status_code}: {resp.text}"
        )
        groups = resp.json().get("groups", [])
        rt_ids_in_matrix = {g["rate_type_id"] for g in groups}
        assert rt_a_id not in rt_ids_in_matrix, (
            "Company A's contaminated CPI_ RateType must NOT appear in Company B's matrix"
        )

    finally:
        await direct_db.execute(_text("""
            DELETE FROM payroll.payitemratetypemap
            WHERE payitemid = :piid AND ratetypeid = :rtid
        """), {"piid": pi_b_id, "rtid": rt_a_id})


# ===========================================================================
# T15 — finalize_period refuses contaminated DriverRate
# ===========================================================================

@pytest.mark.asyncio
async def test_t15_finalization_refuses_contaminated_driver_rate(
    p4b_env, client: httpx.AsyncClient, direct_db
):
    """
    Setup (all via direct_db to bypass service guards):
      - Company B PayItem mapped to Company A's CPI_ RateType (contaminated)
      - Payroll period for Company B (Approved status)
      - Draft line for Company B driver using Company B's PayItem
      - Approved DriverRate for Company B driver using Company A's CPI_ RateType
    Call POST /payroll/periods/{period_id}/finalize.
    Finalization must be rejected 422 (contaminated rate type detected).
    No PayrollFinalLines must be written.
    """
    from datetime import date as _date
    cid_b    = p4b_env["cid_b"]
    bid_b    = p4b_env["bid_b"]
    pi_b_id  = p4b_env["pi_b_id"]
    pi_b_code_row = (await direct_db.execute(_text(
        "SELECT payitemcode FROM payroll.payitems WHERE payitemid = :id"
    ), {"id": pi_b_id})).mappings().first()
    pi_b_code = pi_b_code_row["payitemcode"]
    rt_a_id   = p4b_env["rt_a_custom_id"]
    drv_b_id  = p4b_env["driver_b_id"]
    uid_b     = p4b_env["uid_b"]
    token_b   = await _get_token_b(client)

    # Insert contaminated PayItemRateTypeMap (bypass Phase 4C DB trigger, simulates DBA attack)
    await _bypass_trigger_insert_map(direct_db, pi_b_id, rt_a_id)

    # Insert an approved DriverRate for Company B driver using Company A's CPI_ RateType
    rate_row = (await direct_db.execute(_text("""
        INSERT INTO payroll.driverrates
            (companyid, branchid, driverid, ratetypeid, amount, effectivefrom, status)
        VALUES (:cid, :bid, :did, :rtid, '10.00', :ef, 'Approved')
        RETURNING driverrateid
    """), {"cid": cid_b, "bid": bid_b, "did": drv_b_id, "rtid": rt_a_id,
           "ef": _date(2065, 1, 1)})).mappings().first()
    contaminated_rate_id = rate_row["driverrateid"]

    # Insert a payroll period in 'Approved' status for Company B
    period_row = (await direct_db.execute(_text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, periodcode, periodname, periodtype,
             startdate, enddate, status, createdbyuserid)
        VALUES (:cid, :bid, 'T15-2065-01', 'T15 Test Period Jan 2065', 'Month',
                '2065-01-01', '2065-01-31', 'Approved', :uid)
        RETURNING payrollperiodid
    """), {"cid": cid_b, "bid": bid_b, "uid": uid_b})).mappings().first()
    test_period_id = period_row["payrollperiodid"]

    # Insert a non-void draft line for Company B driver using Company B's custom PayItem
    draft_row = (await direct_db.execute(_text("""
        INSERT INTO payroll.payrolldraftlines
            (companyid, branchid, payrollperiodid, driverid,
             workdate, linetype, linescope, quantity, rateamount,
             status, sourcetype, needsmanagerreview)
        VALUES (:cid, :bid, :pid, :did,
                '2065-01-15', :linetype, 'Daily', 1, '10.00',
                'Approved', 'Manual', FALSE)
        RETURNING draftlineid
    """), {"cid": cid_b, "bid": bid_b, "pid": test_period_id,
           "did": drv_b_id, "linetype": pi_b_code})).mappings().first()
    draft_line_id = draft_row["draftlineid"]

    try:
        resp = await client.post(
            f"/payroll/periods/{test_period_id}/finalize",
            headers=_auth(token_b),
        )
        assert resp.status_code == 422, (
            f"finalize_period must return 422 for contaminated rate type, "
            f"got {resp.status_code}: {resp.text}"
        )
        detail = resp.json().get("detail", "")
        assert ("rate type" in detail.lower() or "contaminated" in detail.lower() or
                "not valid" in detail.lower() or
                "approved_snapshot_not_found_for_finalization" in detail.lower()), (
            f"Error detail must mention rate type issue: {detail!r}"
        )

        # No PayrollFinalLines must be written
        final_count = (await direct_db.execute(_text("""
            SELECT COUNT(*) FROM payroll.payrollfinallines
            WHERE payrollperiodid = :pid
        """), {"pid": test_period_id})).scalar()
        assert final_count == 0, (
            "No PayrollFinalLines must be written after contaminated finalization rejection"
        )

    finally:
        # Cleanup in dependency order
        await direct_db.execute(_text(
            "DELETE FROM payroll.payrollfinallines WHERE payrollperiodid = :pid"
        ), {"pid": test_period_id})
        await direct_db.execute(_text(
            "DELETE FROM payroll.payrolldraftlines WHERE draftlineid = :id"
        ), {"id": draft_line_id})
        await direct_db.execute(_text(
            "DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"
        ), {"pid": test_period_id})
        await direct_db.execute(_text(
            "DELETE FROM payroll.driverrates WHERE driverrateid = :id"
        ), {"id": contaminated_rate_id})
        await direct_db.execute(_text("""
            DELETE FROM payroll.payitemratetypemap
            WHERE payitemid = :piid AND ratetypeid = :rtid
        """), {"piid": pi_b_id, "rtid": rt_a_id})


# ===========================================================================
# T16 -- get_rate_types hides mixed-contaminated mapping (Phase 4B.4)
# ===========================================================================

@pytest.mark.asyncio
async def test_t16_rate_types_endpoint_hides_mixed_contaminated_mapping(
    p4b_env, client: httpx.AsyncClient, direct_db
):
    """
    Phase 4B.4 regression: when a contaminated PayItemRateTypeMap row exists
    (Company B PayItem -> Company A CPI_ RateType, inserted via direct-DB),
    GET /payroll/rate-types as Company B must NOT expose Company A's CPI_ type.

    Correct rule: has_system OR (has_own AND NOT has_foreign)
    The contaminated row makes has_own=True for Company B, but also
    has_foreign=True (from Company A's PayItem mapping) -- so the type must
    be hidden.
    """
    rt_a_id   = p4b_env["rt_a_custom_id"]
    pi_b_id   = p4b_env["pi_b_id"]
    rt_b_id   = p4b_env["rt_b_own_id"]
    rt_h_id   = p4b_env["rt_hourly_id"]

    # Insert contaminated mapping bypassing Phase 4C trigger (simulates DBA attack).
    # Phase 4C structural check (rt.companyid=cid_a != cid_b) hides rt_a from Company B.
    await _bypass_trigger_insert_map(direct_db, pi_b_id, rt_a_id)

    try:
        # Authenticate as Company B user
        token_b = await _get_token_b(client)
        headers_b = _auth(token_b)

        resp = await client.get("/payroll/rate-types", headers=headers_b)
        assert resp.status_code == 200, f"Expected 200: {resp.text}"

        data = resp.json()
        rate_type_ids   = [rt["rate_type_id"] for rt in data]
        rate_type_codes = [rt["rate_code"]     for rt in data]

        # Company A's CPI_ type must be absent (structural: rt.companyid=cid_a != cid_b)
        assert rt_a_id not in rate_type_ids, (
            "Company A's CPI_ RateType must be hidden from Company B "
            "(Phase 4C structural check: rt.companyid=cid_a != cid_b)"
        )
        assert not any(c.startswith("CPI_P4B_RT") for c in rate_type_codes), (
            "Company A's CPI_P4B_RT code must not appear in Company B's rate types"
        )

        # System type (HOURLY) must still be visible
        assert rt_h_id in rate_type_ids, (
            "System rate type (HOURLY) must remain visible to Company B"
        )

        # Company B's own pure CPI_ type must still be visible (has_own AND NOT has_foreign)
        if rt_b_id is not None:
            assert rt_b_id in rate_type_ids, (
                "Company B's own pure CPI_ type must still be visible"
            )

    finally:
        # Remove the contaminated mapping
        await direct_db.execute(_text("""
            DELETE FROM payroll.payitemratetypemap
            WHERE payitemid = :piid AND ratetypeid = :rtid_t16
        """), {"piid": pi_b_id, "rtid_t16": rt_a_id})
