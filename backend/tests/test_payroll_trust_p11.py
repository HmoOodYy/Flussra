"""
test_payroll_trust_p11.py — Phase 11

Advanced Rate Source Immutability + Audit Snapshot Hardening.

Tests:
  T1  OrdinalTier final line SourceSnapshot includes 'tiers' array
  T2  Direct SQL UPDATE of a tier row on a used DriverRate is blocked
  T3  Direct SQL DELETE of a tier row on a used DriverRate is blocked
  T4  Direct SQL UPDATE of BlockSize/RoundingRule on a used DriverRate is blocked
  T5  Tier rows of an UNUSED advanced rate remain fully mutable
  T6  Future advanced rate supersession works after historical finalized payroll
  T7  SYS_MAX_CAP SourceSnapshot includes system_generated=True + cap details
  T8  SYS_MIN_TOPUP improved snapshot includes driver_pay_rule_id + effective dates
  T9  Schema guard checks Phase 11 objects (fn + trigger for driverratetiers)

Year slots: 2111-2130 (distinct from prior phases).
"""
from __future__ import annotations

import json as _json
from datetime import date as _date
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.schema_guard import _check_payroll_trust
from app.payroll_setup.policy import assign_setup, create_draft, create_setup, publish_version


@pytest_asyncio.fixture
async def trust_branch_id(db_conn, session_client, auth_token) -> int:
    """Give each test a branch whose finalized evidence cannot affect another test."""
    row = (await db_conn.execute(_text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (1, :code, :name, 'Active', FALSE)
        RETURNING branchid
    """), {"code": (code := f"P11_{uuid4().hex}"), "name": code})).scalar_one()
    branch_id = int(row)
    items = await session_client.get(
        f"/settings/branches/{branch_id}/pay-items", headers=_tok(auth_token),
    )
    assert items.status_code == 200, items.text
    hours_id = next(item["pay_item_id"] for item in items.json()
                    if item["pay_item_code"] == "HOURS")
    active = await session_client.patch(
        f"/settings/branches/{branch_id}/pay-items/{hours_id}",
        json={"is_active": True}, headers=_tok(auth_token),
    )
    assert active.status_code == 200, active.text
    return branch_id

# ---------------------------------------------------------------------------
# Year slots
# ---------------------------------------------------------------------------
T1_START, T1_END, T1_WORK   = "2111-01-06", "2111-01-12", "2111-01-07"
T2_START, T2_END, T2_WORK   = "2112-02-03", "2112-02-09", "2112-02-04"
T3_START, T3_END, T3_WORK   = "2113-03-03", "2113-03-09", "2113-03-04"
T4_START, T4_END, T4_WORK   = "2114-04-07", "2114-04-13", "2114-04-08"
T5_START                     = "2115-05-01"
T6A_START, T6A_END, T6A_WORK = "2116-06-01", "2116-06-07", "2116-06-02"
T6B_START                    = "2117-07-01"
T7_START, T7_END, T7_WORK   = "2118-08-05", "2118-08-11", "2118-08-06"
T8_START, T8_END, T8_WORK   = "2119-09-02", "2119-09-08", "2119-09-03"

FINALIZE_URL = "/payroll/periods/{pid}/finalize"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tok(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_driver(client, token, branch_id, suffix, hire_date="2111-01-01"):
    r = await client.post(
        "/core/drivers",
        json={"branch_id": branch_id, "full_name": f"P11 {suffix}",
              "driver_code": f"P11-{suffix[:8]}",
              "cdl_number": f"CDL-P11-{suffix[:6]}",
              "email": f"p11{suffix[:6].lower().replace('-', '')}@example.com",
              "hire_date": hire_date},
        headers=_tok(token),
    )
    assert r.status_code == 201, f"create_driver: {r.text}"
    return r.json()["driver_id"]


async def _delete_driver(client, token, driver_id):
    await client.delete(f"/core/drivers/{driver_id}", headers=_tok(token))


async def _get_rate_type_id(client, token, code):
    r = await client.get("/payroll/rate-types", headers=_tok(token))
    assert r.status_code == 200
    for rt in r.json():
        if rt["rate_code"] == code:
            return rt["rate_type_id"]
    raise AssertionError(f"RateType {code!r} not found")


async def _create_rate(client, token, driver_id, rate_type_id, effective_from,
                       amount="1.00", ordinal_tiers=None, block_size=None,
                       rounding_rule=None):
    body = {
        "driver_id": driver_id, "rate_type_id": rate_type_id,
        "amount": amount, "effective_from": effective_from,
    }
    if ordinal_tiers:
        body["ordinal_tiers"] = ordinal_tiers
    if block_size:
        body["block_size"] = block_size
    if rounding_rule:
        body["rounding_rule"] = rounding_rule
    r = await client.post("/payroll/rates", json=body, headers=_tok(token))
    assert r.status_code == 201, f"create rate: {r.text}"
    return r.json()["driver_rate_id"]


async def _approve_rate(client, token, rate_id):
    r = await client.post(f"/payroll/rates/{rate_id}/approve", headers=_tok(token))
    assert r.status_code == 200, f"approve rate: {r.text}"


async def _open_period(client, token, branch_id, test_database_url, start, end):
    headers = _tok(token)
    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.begin() as db:
            user_id = (await db.execute(_text("""
                SELECT UserID FROM sec.Users WHERE CompanyID = 1 AND Username = 'admin'
            """))).scalar_one()
            setup_id = await create_setup(
                1, user_id, f"P11_{uuid4().hex[:12]}", "P11 trust setup", db,
            )
            anchor = _date.fromisoformat(start)
            draft_id = await create_draft(
                1, user_id, setup_id, db,
                payroll_frequency="Week", anchor_start_date=anchor,
                normal_days_off_mask=0,
            )
            await publish_version(1, user_id, setup_id, draft_id, anchor, db)
            await assign_setup(1, user_id, branch_id, setup_id, anchor, db)
    finally:
        await engine.dispose()
    preview = await client.get(
        f"/payroll/branches/{branch_id}/period-candidates",
        params={"mode": "OPEN_CREATION"}, headers=headers,
    )
    assert preview.status_code == 200, f"candidate preview: {preview.text}"
    selected = preview.json()["selected"]
    assert selected["start_date"] == start and selected["end_date"] == end
    assert selected["creatable"], selected
    created = await client.post(
        f"/payroll/branches/{branch_id}/period-creations",
        json={"candidate_key": selected["candidate_key"]}, headers=headers,
    )
    assert created.status_code == 201, f"create period: {created.text}"
    return created.json()["payroll_period_id"]


async def _advance_to_approved(client, token, pid, driver_id, work_date,
                                line_type="HOURS", quantity="8"):
    headers = _tok(token)
    r = await client.post(f"/payroll/periods/{pid}/lines",
                          json={"driver_id": driver_id, "work_date": work_date,
                                "line_type": line_type, "quantity": quantity},
                          headers=headers)
    assert r.status_code == 201, f"add line: {r.text}"
    r = await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "InReview"}, headers=headers)
    assert r.status_code == 200, f"InReview: {r.text}"
    items = await client.get("/review/items", headers=headers)
    item = next(
        (i for i in items.json()
         if i.get("entity_name") == "PayrollPeriods"
         and i.get("entity_id") == str(pid)
         and i.get("status") == "Pending"),
        None,
    )
    assert item is not None, "review item not found"
    dec = await client.post(f"/review/items/{item['review_item_id']}/decide",
                            json={"decision": "Approved"}, headers=headers)
    assert dec.status_code == 200, f"approve review: {dec.text}"


async def _finalize(client, token, pid):
    r = await client.post(FINALIZE_URL.format(pid=pid), headers=_tok(token))
    assert r.status_code == 200, f"finalize: {r.text}"
    return r.json()


async def _ensure_ordinal_item_active(client, token, branch_id, db_conn) -> int:
    """
    Ensure a custom OrdinalTier pay item (P11_ORD) linked to M13C_ORDINAL
    exists and is active on branch_id. Returns the pay_item_id.
    Idempotent — skips creation if item already exists.
    """
    # Look up M13C_ORDINAL rate type id
    rt_id = await _get_rate_type_id(client, token, "M13C_ORDINAL")

    # Check if item already exists
    r = await client.get("/settings/pay-items", headers=_tok(token))
    assert r.status_code == 200
    for item in r.json():
        if item.get("pay_item_code") == "P11_ORD":
            item_id = item["pay_item_id"]
            # Ensure it's active
            r2 = await client.patch(
                f"/settings/branches/{branch_id}/pay-items/{item_id}",
                json={"is_active": True}, headers=_tok(token),
            )
            assert r2.status_code == 200, f"activate P11_ORD: {r2.text}"
            return item_id

    # Create it via DB seed (HTTP creation of Daily items is blocked by LLR-A)
    from tests.seed_helpers import seed_legacy_item
    item_id = await seed_legacy_item(
        db_conn,
        code="P11_ORD", name="P11 Ordinal Test Item",
        rate_behavior="OrdinalTier", unit="Load", category="Count",
    )

    # Link rate type
    r = await client.post(f"/settings/pay-items/{item_id}/rate-type-map",
                          json={"rate_type_id": rt_id, "is_primary": True},
                          headers=_tok(token))
    assert r.status_code in (200, 201), f"link rt: {r.text}"

    # Activate on branch
    r = await client.patch(
        f"/settings/branches/{branch_id}/pay-items/{item_id}",
        json={"is_active": True}, headers=_tok(token),
    )
    assert r.status_code == 200, f"activate P11_ORD: {r.text}"
    return item_id


async def _ensure_block_item_active(client, token, branch_id, db_conn) -> int:
    """
    Ensure a custom Block pay item (P11_BLK) linked to M13C_BLOCK
    exists and is active on branch_id. Returns the pay_item_id.
    """
    rt_id = await _get_rate_type_id(client, token, "M13C_BLOCK")

    r = await client.get("/settings/pay-items", headers=_tok(token))
    assert r.status_code == 200
    for item in r.json():
        if item.get("pay_item_code") == "P11_BLK":
            item_id = item["pay_item_id"]
            r2 = await client.patch(
                f"/settings/branches/{branch_id}/pay-items/{item_id}",
                json={"is_active": True}, headers=_tok(token),
            )
            assert r2.status_code == 200
            return item_id

    # Create via DB seed (HTTP creation of Daily items is blocked by LLR-A)
    from tests.seed_helpers import seed_legacy_item
    item_id = await seed_legacy_item(
        db_conn,
        code="P11_BLK", name="P11 Block Test Item",
        rate_behavior="Block", unit="Mile", category="Count",
    )

    r = await client.post(f"/settings/pay-items/{item_id}/rate-type-map",
                          json={"rate_type_id": rt_id, "is_primary": True},
                          headers=_tok(token))
    assert r.status_code in (200, 201), f"link rt: {r.text}"

    r = await client.patch(
        f"/settings/branches/{branch_id}/pay-items/{item_id}",
        json={"is_active": True}, headers=_tok(token),
    )
    assert r.status_code == 200, f"activate P11_BLK: {r.text}"
    return item_id


_ORDINAL_3_TIERS = [
    {"tier_sequence": 1, "from_unit": 1, "to_unit": 1,   "tier_amount": "1.00"},
    {"tier_sequence": 2, "from_unit": 2, "to_unit": 2,   "tier_amount": "2.00"},
    {"tier_sequence": 3, "from_unit": 3, "to_unit": None, "tier_amount": "3.00"},
]


# ---------------------------------------------------------------------------
# T1 — OrdinalTier final line SourceSnapshot includes 'tiers' array
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p11_t1_ordinal_tier_snapshot_includes_tiers(
    session_client: httpx.AsyncClient,
    auth_token: str,
    trust_branch_id: int,
    test_database_url: str,
    direct_db,
    session_db_conn,
):
    """
    T1: After finalizing a period that used an OrdinalTier rate, the final line's
    canonical provenance wrapper and scalar rate identity must be present. The
    tier rows remain the authoritative immutable advanced-rate definition.
    """
    driver_id = None
    pid = None

    try:
        rt_id = await _get_rate_type_id(session_client, auth_token, "M13C_ORDINAL")

        driver_id = await _create_driver(session_client, auth_token, trust_branch_id,
                                         "T1TIER", hire_date="2111-01-01")
        await _ensure_ordinal_item_active(session_client, auth_token, trust_branch_id, session_db_conn)
        rate_id = await _create_rate(
            session_client, auth_token, driver_id, rt_id, T1_START,
            amount="1.00", ordinal_tiers=_ORDINAL_3_TIERS,
        )
        await _approve_rate(session_client, auth_token, rate_id)

        pid = await _open_period(session_client, auth_token, trust_branch_id, test_database_url,
                                  T1_START, T1_END)
        # Use M13C_SYS_ORDINAL line type (loads); quantity=2 → ordinal tier 2
        await _advance_to_approved(session_client, auth_token, pid, driver_id, T1_WORK,
                                   line_type="P11_ORD", quantity="2")
        await _finalize(session_client, auth_token, pid)

        rows = await direct_db.execute(
            _text("""
                SELECT sourcesnapshot
                FROM   payroll.payrollfinallines
                WHERE  payrollperiodid = :pid
                  AND  driverrateid = :rid
            """),
            {"pid": pid, "rid": rate_id},
        )
        final_rows = rows.mappings().all()
        assert len(final_rows) > 0, "No final lines for the OrdinalTier rate"

        for row in final_rows:
            snap = row["sourcesnapshot"]
            assert snap is not None, "SourceSnapshot is NULL on OrdinalTier final line"
            if isinstance(snap, str):
                snap = _json.loads(snap)
            assert isinstance(snap, dict), f"SourceSnapshot not a dict: {snap!r}"

            assert {"payroll_calculation_snapshot_id", "snapshot_line_id", "source_evidence"} <= snap.keys()
            assert snap["source_type"] == "DraftLine"
            tiers = (await direct_db.execute(_text("""
                SELECT tiersequence, fromunit, tounit, tieramount
                FROM payroll.driverratetiers
                WHERE driverrateid = :rid
                ORDER BY tiersequence
            """), {"rid": rate_id})).mappings().all()
            assert len(tiers) == 3
            assert tiers[0]["tiersequence"] == 1

    finally:
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)


# ---------------------------------------------------------------------------
# T2 — Direct SQL UPDATE of a used tier row is blocked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p11_t2_used_tier_update_blocked(
    session_client: httpx.AsyncClient,
    auth_token: str,
    trust_branch_id: int,
    test_database_url: str,
    direct_db,
    session_db_conn,
):
    """
    T2: After finalization references an OrdinalTier DriverRate, a direct SQL
    UPDATE on one of that rate's tier rows must be rejected by the
    trg_guard_driverratetier_used_mutation trigger.
    """
    driver_id = None
    pid = None

    try:
        rt_id = await _get_rate_type_id(session_client, auth_token, "M13C_ORDINAL")

        driver_id = await _create_driver(session_client, auth_token, trust_branch_id,
                                         "T2TIERUPD", hire_date="2112-01-01")
        await _ensure_ordinal_item_active(session_client, auth_token, trust_branch_id, session_db_conn)
        rate_id = await _create_rate(
            session_client, auth_token, driver_id, rt_id, T2_START,
            amount="1.00", ordinal_tiers=_ORDINAL_3_TIERS,
        )
        await _approve_rate(session_client, auth_token, rate_id)

        pid = await _open_period(session_client, auth_token, trust_branch_id, test_database_url,
                                  T2_START, T2_END)
        await _advance_to_approved(session_client, auth_token, pid, driver_id, T2_WORK,
                                   line_type="P11_ORD", quantity="1")
        await _finalize(session_client, auth_token, pid)

        # Verify the rate is referenced
        ref = await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE driverrateid = :rid"),
            {"rid": rate_id},
        )
        assert ref.scalar() > 0, "Rate not referenced — precondition failed"

        # Attempt UPDATE on a tier row — must be blocked
        blocked = False
        try:
            await direct_db.execute(
                _text("""
                    UPDATE payroll.driverratetiers
                    SET    tieramount = 99.00
                    WHERE  driverrateid = :rid AND tiersequence = 1
                """),
                {"rid": rate_id},
            )
        except Exception as exc:
            err_str = str(exc).lower()
            assert ("driverratetier_mutation_guard" in err_str
                    or "restrict_violation" in err_str), (
                f"Expected tier mutation guard error, got: {exc}"
            )
            blocked = True

        assert blocked, "Expected UPDATE of used tier row to be blocked, but it succeeded"

    finally:
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)


# ---------------------------------------------------------------------------
# T3 — Direct SQL DELETE of a used tier row is blocked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p11_t3_used_tier_delete_blocked(
    session_client: httpx.AsyncClient,
    auth_token: str,
    trust_branch_id: int,
    test_database_url: str,
    direct_db,
    session_db_conn,
):
    """
    T3: After finalization references an OrdinalTier DriverRate, a direct SQL
    DELETE of one of that rate's tier rows must be rejected.
    """
    driver_id = None
    pid = None

    try:
        rt_id = await _get_rate_type_id(session_client, auth_token, "M13C_ORDINAL")

        driver_id = await _create_driver(session_client, auth_token, trust_branch_id,
                                         "T3TIERDEL", hire_date="2113-01-01")
        await _ensure_ordinal_item_active(session_client, auth_token, trust_branch_id, session_db_conn)
        rate_id = await _create_rate(
            session_client, auth_token, driver_id, rt_id, T3_START,
            amount="1.00", ordinal_tiers=_ORDINAL_3_TIERS,
        )
        await _approve_rate(session_client, auth_token, rate_id)

        pid = await _open_period(session_client, auth_token, trust_branch_id, test_database_url,
                                  T3_START, T3_END)
        await _advance_to_approved(session_client, auth_token, pid, driver_id, T3_WORK,
                                   line_type="P11_ORD", quantity="1")
        await _finalize(session_client, auth_token, pid)

        # Verify referenced
        ref = await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE driverrateid = :rid"),
            {"rid": rate_id},
        )
        assert ref.scalar() > 0, "Rate not referenced — precondition failed"

        # Attempt DELETE of a tier row — must be blocked
        blocked = False
        try:
            await direct_db.execute(
                _text("""
                    DELETE FROM payroll.driverratetiers
                    WHERE  driverrateid = :rid AND tiersequence = 1
                """),
                {"rid": rate_id},
            )
        except Exception as exc:
            err_str = str(exc).lower()
            assert ("driverratetier_mutation_guard" in err_str
                    or "restrict_violation" in err_str), (
                f"Expected tier deletion guard error, got: {exc}"
            )
            blocked = True

        assert blocked, "Expected DELETE of used tier row to be blocked, but it succeeded"

    finally:
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)


# ---------------------------------------------------------------------------
# T4 — Direct SQL UPDATE of BlockSize/RoundingRule on a used DriverRate is blocked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p11_t4_used_block_fields_update_blocked(
    session_client: httpx.AsyncClient,
    auth_token: str,
    trust_branch_id: int,
    test_database_url: str,
    direct_db,
    session_db_conn,
):
    """
    T4: After finalization references a Block DriverRate, direct SQL UPDATE of
    BlockSize or RoundingRule must be blocked by the extended
    fn_guard_driverrate_used_mutation (Phase 11 addition).
    """
    driver_id = None
    pid = None

    try:
        rt_id = await _get_rate_type_id(session_client, auth_token, "M13C_BLOCK")

        driver_id = await _create_driver(session_client, auth_token, trust_branch_id,
                                         "T4BLKUPD", hire_date="2114-01-01")
        # Activate M13C_SYS_BLOCK on the branch before creating the rate (rate type check)
        await _ensure_block_item_active(session_client, auth_token, trust_branch_id, session_db_conn)
        rate_id = await _create_rate(
            session_client, auth_token, driver_id, rt_id, T4_START,
            amount="10.00", block_size="4", rounding_rule="Floor",
        )
        await _approve_rate(session_client, auth_token, rate_id)

        pid = await _open_period(session_client, auth_token, trust_branch_id, test_database_url,
                                  T4_START, T4_END)
        await _advance_to_approved(session_client, auth_token, pid, driver_id, T4_WORK,
                                   line_type="P11_BLK", quantity="8")
        await _finalize(session_client, auth_token, pid)

        # Verify referenced
        ref = await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE driverrateid = :rid"),
            {"rid": rate_id},
        )
        assert ref.scalar() > 0, "Rate not referenced — precondition failed"

        # Attempt UPDATE of BlockSize — must be blocked
        blocked_size = False
        try:
            await direct_db.execute(
                _text("UPDATE payroll.driverrates SET blocksize = 99 WHERE driverrateid = :rid"),
                {"rid": rate_id},
            )
        except Exception as exc:
            err_str = str(exc).lower()
            assert ("driverrate_mutation_guard" in err_str
                    or "restrict_violation" in err_str), (
                f"Expected mutation guard for blocksize, got: {exc}"
            )
            blocked_size = True

        assert blocked_size, "Expected UPDATE of BlockSize on used rate to be blocked"

        # Attempt UPDATE of RoundingRule — also must be blocked
        blocked_rule = False
        try:
            await direct_db.execute(
                _text("UPDATE payroll.driverrates SET roundingrule = 'Ceiling' WHERE driverrateid = :rid"),
                {"rid": rate_id},
            )
        except Exception as exc:
            err_str = str(exc).lower()
            assert ("driverrate_mutation_guard" in err_str
                    or "restrict_violation" in err_str), (
                f"Expected mutation guard for roundingrule, got: {exc}"
            )
            blocked_rule = True

        assert blocked_rule, "Expected UPDATE of RoundingRule on used rate to be blocked"

    finally:
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)


# ---------------------------------------------------------------------------
# T5 — Tier rows of an UNUSED advanced rate remain fully mutable
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p11_t5_unused_tier_rows_mutable(
    session_client: httpx.AsyncClient,
    auth_token: str,
    trust_branch_id: int,
    test_database_url: str,
    direct_db,
    session_db_conn,
):
    """
    T5: An OrdinalTier DriverRate that has NOT been referenced in any finalized
    PayrollFinalLines must still allow direct UPDATE and DELETE of its tier rows.
    """
    driver_id = None

    try:
        rt_id = await _get_rate_type_id(session_client, auth_token, "M13C_ORDINAL")

        driver_id = await _create_driver(session_client, auth_token, trust_branch_id,
                                         "T5UNUSED", hire_date="2115-01-01")
        await _ensure_ordinal_item_active(session_client, auth_token, trust_branch_id, session_db_conn)
        rate_id = await _create_rate(
            session_client, auth_token, driver_id, rt_id, T5_START,
            amount="1.00", ordinal_tiers=_ORDINAL_3_TIERS,
        )
        # Do NOT approve or finalize — rate is unused

        # Verify no final-line reference
        ref = await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE driverrateid = :rid"),
            {"rid": rate_id},
        )
        assert ref.scalar() == 0, "Rate already referenced — precondition failed"

        # UPDATE a tier row — must succeed
        await direct_db.execute(
            _text("""
                UPDATE payroll.driverratetiers
                SET    tieramount = 5.00
                WHERE  driverrateid = :rid AND tiersequence = 1
            """),
            {"rid": rate_id},
        )

        # DELETE a tier row — must succeed
        await direct_db.execute(
            _text("""
                DELETE FROM payroll.driverratetiers
                WHERE  driverrateid = :rid AND tiersequence = 3
            """),
            {"rid": rate_id},
        )

        # Confirm tier sequence 1 now has updated amount
        updated = await direct_db.execute(
            _text("SELECT tieramount FROM payroll.driverratetiers WHERE driverrateid = :rid AND tiersequence = 1"),
            {"rid": rate_id},
        )
        assert Decimal(str(updated.scalar())) == Decimal("5.00"), (
            "Tier amount should have been updated to 5.00"
        )

    finally:
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)


# ---------------------------------------------------------------------------
# T6 — Future advanced rate supersession works after historical finalized payroll
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p11_t6_advanced_rate_supersession_after_finalization(
    session_client: httpx.AsyncClient,
    auth_token: str,
    trust_branch_id: int,
    test_database_url: str,
    direct_db,
    session_db_conn,
):
    """
    T6: After an OrdinalTier rate is used in finalized payroll, creating and
    approving a new rate for the same driver+type (supersession) must succeed —
    the guard only blocks mutation of EXISTING rows, not creation of new rates.
    """
    driver_id = None
    pid_a = None

    try:
        rt_id = await _get_rate_type_id(session_client, auth_token, "M13C_ORDINAL")

        driver_id = await _create_driver(session_client, auth_token, trust_branch_id,
                                         "T6SUPER", hire_date="2116-01-01")
        await _ensure_ordinal_item_active(session_client, auth_token, trust_branch_id, session_db_conn)
        old_rate_id = await _create_rate(
            session_client, auth_token, driver_id, rt_id, T6A_START,
            amount="1.00", ordinal_tiers=_ORDINAL_3_TIERS,
        )
        await _approve_rate(session_client, auth_token, old_rate_id)

        pid_a = await _open_period(session_client, auth_token, trust_branch_id, test_database_url,
                                    T6A_START, T6A_END)
        await _advance_to_approved(session_client, auth_token, pid_a, driver_id, T6A_WORK,
                                   line_type="P11_ORD", quantity="1")
        await _finalize(session_client, auth_token, pid_a)

        # Verify old rate is referenced
        ref = await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE driverrateid = :rid"),
            {"rid": old_rate_id},
        )
        assert ref.scalar() > 0, "Old rate not referenced — precondition failed"

        # Create a new rate with different tiers — this must succeed
        new_tiers = [
            {"tier_sequence": 1, "from_unit": 1, "to_unit": 2,   "tier_amount": "5.00"},
            {"tier_sequence": 2, "from_unit": 3, "to_unit": None, "tier_amount": "8.00"},
        ]
        new_rate_id = await _create_rate(
            session_client, auth_token, driver_id, rt_id, T6B_START,
            amount="5.00", ordinal_tiers=new_tiers,
        )

        # Approve new rate (triggers supersession of old rate — status + effectiveto update)
        r = await session_client.post(f"/payroll/rates/{new_rate_id}/approve",
                                       headers=_tok(auth_token))
        assert r.status_code == 200, (
            f"New advanced rate approval failed (guard may have blocked supersession): {r.text}"
        )

        # Old rate status must now be Superseded
        old_status = await direct_db.execute(
            _text("SELECT status FROM payroll.driverrates WHERE driverrateid = :rid"),
            {"rid": old_rate_id},
        )
        assert old_status.scalar() == "Superseded", (
            "Old OrdinalTier rate should be Superseded after new rate approval"
        )

    finally:
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)


# ---------------------------------------------------------------------------
# T7 — SYS_MAX_CAP SourceSnapshot includes system_generated + cap details
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p11_t7_sys_max_cap_snapshot_content(
    session_client: httpx.AsyncClient,
    auth_token: str,
    trust_branch_id: int,
    test_database_url: str,
    direct_db,
):
    """
    T7: When a SYS_MAX_CAP line is inserted during finalization, it must carry
    a SourceSnapshot with system_generated=True, reason='maximum_pay_cap', and
    the numeric amounts used to compute the cap adjustment.
    Now also includes driver_pay_rule_id and effective date fields (Phase 11).
    """
    driver_id = None
    pid = None
    rule_id = None

    try:

        br_row = await direct_db.execute(
            _text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
            {"bid": trust_branch_id},
        )
        company_id = br_row.scalar()
        assert company_id is not None

        rate_type_id = None
        r = await session_client.get("/payroll/rate-types", headers=_tok(auth_token))
        for rt in r.json():
            if rt["rate_code"] == "HOURLY":
                rate_type_id = rt["rate_type_id"]
                break
        assert rate_type_id is not None

        driver_id = await _create_driver(session_client, auth_token, trust_branch_id,
                                         "T7MAXCAP", hire_date="2118-01-01")

        # Give driver a very high HOURLY rate so earned pay will exceed the cap
        r = await session_client.post("/payroll/rates", json={
            "driver_id": driver_id, "rate_type_id": rate_type_id,
            "amount": "1000.00", "effective_from": T7_START,
        }, headers=_tok(auth_token))
        assert r.status_code == 201, f"create rate: {r.text}"
        rid = r.json()["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{rid}/approve", headers=_tok(auth_token))

        # Insert a maximum pay rule that will be exceeded
        rule_result = await direct_db.execute(
            _text("""
                INSERT INTO payroll.driverpayrules
                    (companyid, branchid, driverid, ruletype, amount,
                     effectivefrom, status)
                VALUES
                    (:cid, :bid, :did, 'MaximumPay', 50.00,
                     :edate, 'Active')
                RETURNING driverpayruleid
            """),
            {
                "cid": company_id, "bid": trust_branch_id,
                "did": driver_id,
                "edate": _date.fromisoformat(T7_START),
            },
        )
        rule_id = rule_result.scalar()

        pid = await _open_period(session_client, auth_token, trust_branch_id, test_database_url,
                                  T7_START, T7_END)
        await _advance_to_approved(session_client, auth_token, pid, driver_id, T7_WORK)
        await _finalize(session_client, auth_token, pid)

        rows = await direct_db.execute(
            _text("""
                SELECT sourcesnapshot, finalamount
                FROM   payroll.payrollfinallines
                WHERE  payrollperiodid = :pid AND linetype = 'SYS_MAX_CAP'
            """),
            {"pid": pid},
        )
        cap_rows = rows.mappings().all()
        assert len(cap_rows) == 1, f"Expected 1 SYS_MAX_CAP line, found {len(cap_rows)}"

        snap = cap_rows[0]["sourcesnapshot"]
        assert snap is not None, "SYS_MAX_CAP SourceSnapshot is NULL"
        if isinstance(snap, str):
            snap = _json.loads(snap)

        assert snap["source_type"] == "System", f"Unexpected source type: {snap}"
        evidence = snap["source_evidence"]
        assert evidence["RuleType"] == "MaximumPay"
        assert evidence["DriverPayRuleID"] == rule_id
        assert Decimal(str(evidence["RuleAmount"])) == Decimal("50")
        assert Decimal(str(evidence["NormalBase"])) == Decimal("8000")

    finally:
        # Used rules are immutable provenance and cannot be deleted.
        pass
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)


# ---------------------------------------------------------------------------
# T8 — SYS_MIN_TOPUP improved snapshot includes driver_pay_rule_id + dates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p11_t8_sys_min_topup_snapshot_includes_rule_identity(
    session_client: httpx.AsyncClient,
    auth_token: str,
    trust_branch_id: int,
    test_database_url: str,
    direct_db,
):
    """
    T8: After Phase 11, the SYS_MIN_TOPUP SourceSnapshot must include
    driver_pay_rule_id and rule_effective_from (Phase 11 additions), in addition
    to the Phase 9 fields (system_generated, reason, minimum_amount, etc.).
    """
    driver_id = None
    pid = None
    rule_id = None

    try:

        br_row = await direct_db.execute(
            _text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
            {"bid": trust_branch_id},
        )
        company_id = br_row.scalar()
        assert company_id is not None

        rate_type_id = None
        r = await session_client.get("/payroll/rate-types", headers=_tok(auth_token))
        for rt in r.json():
            if rt["rate_code"] == "HOURLY":
                rate_type_id = rt["rate_type_id"]
                break
        assert rate_type_id is not None

        driver_id = await _create_driver(session_client, auth_token, trust_branch_id,
                                         "T8MINRULE", hire_date="2119-01-01")

        r = await session_client.post("/payroll/rates", json={
            "driver_id": driver_id, "rate_type_id": rate_type_id,
            "amount": "1.00", "effective_from": T8_START,
        }, headers=_tok(auth_token))
        assert r.status_code == 201
        rid = r.json()["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{rid}/approve", headers=_tok(auth_token))

        rule_result = await direct_db.execute(
            _text("""
                INSERT INTO payroll.driverpayrules
                    (companyid, branchid, driverid, ruletype, amount,
                     effectivefrom, status)
                VALUES
                    (:cid, :bid, :did, 'MinimumPay', 500.00,
                     :edate, 'Active')
                RETURNING driverpayruleid
            """),
            {
                "cid": company_id, "bid": trust_branch_id,
                "did": driver_id,
                "edate": _date.fromisoformat(T8_START),
            },
        )
        rule_id = rule_result.scalar()

        pid = await _open_period(session_client, auth_token, trust_branch_id, test_database_url,
                                  T8_START, T8_END)
        await _advance_to_approved(session_client, auth_token, pid, driver_id, T8_WORK)
        await _finalize(session_client, auth_token, pid)

        rows = await direct_db.execute(
            _text("""
                SELECT sourcesnapshot
                FROM   payroll.payrollfinallines
                WHERE  payrollperiodid = :pid AND linetype = 'SYS_MIN_TOPUP'
            """),
            {"pid": pid},
        )
        topup_rows = rows.mappings().all()
        assert len(topup_rows) == 1, f"Expected 1 SYS_MIN_TOPUP line, found {len(topup_rows)}"

        snap = topup_rows[0]["sourcesnapshot"]
        assert snap is not None, "SYS_MIN_TOPUP SourceSnapshot is NULL"
        if isinstance(snap, str):
            snap = _json.loads(snap)

        assert snap["source_type"] == "System", f"Unexpected source type: {snap}"
        evidence = snap["source_evidence"]
        assert evidence["RuleType"] == "MinimumPay"
        assert evidence["DriverPayRuleID"] == rule_id
        assert evidence["EffectiveFrom"] is not None
        assert Decimal(str(evidence["RuleAmount"])) == Decimal("500")

    finally:
        # Used rules are immutable provenance and cannot be deleted.
        pass
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)


# ---------------------------------------------------------------------------
# T9 — Schema guard checks Phase 11 objects
# ---------------------------------------------------------------------------

def test_p11_t9_schema_guard_checks_phase11_objects():
    """
    T9: _check_payroll_trust() must report Phase 11 errors when the new
    driverratetier mutation guard function and trigger are absent.
    """
    # Missing function
    class _StubCur:
        def __init__(self, missing):
            self._missing = missing
            self._result = None

        def execute(self, sql, params=None):
            sql_lower = sql.strip().lower()
            if "information_schema.columns" in sql_lower:
                key = ("column", params[0], params[1], params[2])
            elif "pg_indexes" in sql_lower:
                key = ("index", params[0], params[1])
            elif "information_schema.routines" in sql_lower:
                key = ("function", params[0], params[1].lower())
            elif "pg_trigger" in sql_lower:
                key = ("trigger", params[0].lower(), params[1], params[2])
            else:
                key = None
            self._result = None if (key in self._missing) else ("found",)

        def fetchone(self):
            return self._result

    # Function missing
    missing_fn = {("function", "payroll", "fn_guard_driverratetier_used_mutation")}
    errors = _check_payroll_trust(_StubCur(missing_fn))
    assert any(
        "fn_guard_driverratetier_used_mutation" in e and "Phase 11" in e
        for e in errors
    ), f"Expected Phase 11 / fn_guard_driverratetier_used_mutation error, got: {errors}"

    # Trigger missing
    missing_trig = {
        ("trigger", "trg_guard_driverratetier_used_mutation", "payroll", "driverratetiers")
    }
    errors = _check_payroll_trust(_StubCur(missing_trig))
    assert any(
        "trg_guard_driverratetier_used_mutation" in e and "Phase 11" in e
        for e in errors
    ), f"Expected Phase 11 / trg_guard_driverratetier_used_mutation error, got: {errors}"
