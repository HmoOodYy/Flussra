"""
Phase 4 calculation characterization gate — Slice 1.

Locks the CURRENT (pre-CP-4A) numeric and dispatch behavior of:
  - the ambient Decimal compatibility environment (context precision/rounding);
  - PerUnit (authoritative current daily calculation core);
  - EnteredAmount and Fixed/manual (direct/manual boundaries — not computed methods);
  - None (non-core/retired compatibility value);
  - the legacy/manual COALESCE(calculatedamount, quantity * rateamount) fallback;
  - PostgreSQL NUMERIC(18,4)/NUMERIC(18,2) column coercion;
  - actual HTTP/JSON Decimal serialization.

This is a characterization slice, not a correctness slice: tests lock what the
code currently does, including known inconsistencies (e.g. preview vs.
finalization divergence on the legacy fallback). No production behavior is
changed by this file.

Out of scope for this slice (see the Phase 4 calculation-authority
classification in CURRENT_PAYROLL_BACKEND_MASTER_PLAN.md):
  - Status-derived payment (separate current calculation caller) — a later
    dedicated slice, except where noted below.
  - OrdinalTier / RangeBracket / RangeProgressive / Block (M13c legacy
    compatibility paths) — a later dedicated slice.
  - `Calculated` (future-only reserved behavior) — not implemented or tested
    as a working method here.

Isolation: all periods use year 2091 dates (far future) to avoid conflicts
with other test modules (2026 ledger, 2032/2035 finalize, 2082 rate-calc
boundaries, 2085 preview, 2089 cp5 eligibility).
"""
import contextlib
import datetime
import decimal
import uuid
from decimal import Decimal, ROUND_HALF_EVEN

import pytest
import pytest_asyncio
import httpx
from sqlalchemy import text as _text

from app.payroll.service import _compute_calculated_amount


# ---------------------------------------------------------------------------
# Module-level constants (2091 dates — isolated from other test modules)
# ---------------------------------------------------------------------------

PERUNIT_START = "2091-03-04"
PERUNIT_END = "2091-03-10"
DATE_MAR05 = "2091-03-05"
DATE_MAR06 = "2091-03-06"
DATE_MAR07 = "2091-03-07"
DATE_MAR08 = "2091-03-08"
DATE_MAR09 = "2091-03-09"

FALLBACK_START = "2091-04-01"
FALLBACK_END = "2091-04-07"
DATE_APR02 = "2091-04-02"

PGCOERCE_START = "2091-05-06"
PGCOERCE_END = "2091-05-12"


# ---------------------------------------------------------------------------
# Local helpers (mirrors pattern from test_rate_calc_boundaries.py)
# ---------------------------------------------------------------------------

def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


_PERIOD_CODE_PREFIX = "P4S1-"


async def _cancel_active_periods(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    db=None,
) -> None:
    """
    Narrowed to this module's own uniquely-marked periods only
    (`periodcode` starting with `P4S1-`, matching `_open_period`'s naming
    convention) — never touches periods owned by other tests sharing the
    same PAYTEST branch. This is a correction from an earlier branch-wide
    version of this helper that cancelled every active period in the branch
    regardless of ownership.
    """
    if db is not None:
        await db.execute(
            _text(
                "UPDATE payroll.payrollperiods SET status = 'Cancelled' "
                "WHERE branchid = :bid AND status IN ('InReview', 'Approved') "
                "AND periodcode LIKE :prefix"
            ),
            {"bid": branch_id, "prefix": f"{_PERIOD_CODE_PREFIX}%"},
        )
    headers = auth(token)
    for s in ("Draft", "Open", "InReview", "Approved"):
        resp = await client.get(
            "/payroll/periods",
            params={"branch_id": branch_id, "status": s},
            headers=headers,
        )
        if resp.status_code != 200:
            continue
        for p in resp.json():
            if not p.get("period_code", "").startswith(_PERIOD_CODE_PREFIX):
                continue
            await client.patch(
                f"/payroll/periods/{p['payroll_period_id']}/status",
                json={"status": "Cancelled"},
                headers=headers,
            )


async def _open_period(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    start: str,
    end: str,
    db=None,
) -> int:
    """Insert an Open period directly into DB. Returns period_id."""
    from datetime import date as _date
    if db is None:
        raise RuntimeError("_open_period requires db= since CP-1D B1 guard blocks HTTP POST")
    code = f"P4S1-{branch_id}-{start}"
    row = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": code, "name": f"P4S1 {start}",
         "start": _date.fromisoformat(start), "end": _date.fromisoformat(end)},
    )).mappings().first()
    return row["payrollperiodid"]


async def _create_driver(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    name: str,
) -> int:
    r = await client.post(
        "/core/drivers",
        json={"branch_id": branch_id, "full_name": name},
        headers=auth(token),
    )
    assert r.status_code == 201, f"Create driver failed: {r.text}"
    return r.json()["driver_id"]


async def _get_hourly_rate_type_id(client: httpx.AsyncClient, token: str) -> int:
    rv = await client.get("/payroll/rate-types", headers=auth(token))
    assert rv.status_code == 200
    for rt in rv.json():
        if rt["rate_code"] == "HOURLY":
            return rt["rate_type_id"]
    raise AssertionError("HOURLY rate type not found")


async def _create_and_approve_rate(
    client: httpx.AsyncClient,
    token: str,
    driver_id: int,
    rate_type_id: int,
    amount: str,
    effective_from: str,
) -> int:
    rc = await client.post(
        "/payroll/rates",
        json={
            "driver_id": driver_id,
            "rate_type_id": rate_type_id,
            "amount": amount,
            "effective_from": effective_from,
        },
        headers=auth(token),
    )
    assert rc.status_code == 201, f"Create rate failed: {rc.text}"
    rate_id = rc.json()["driver_rate_id"]
    ra = await client.post(f"/payroll/rates/{rate_id}/approve", headers=auth(token))
    assert ra.status_code == 200, f"Approve rate failed: {ra.text}"
    return rate_id


async def _add_hours_line(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    work_date: str,
    quantity,
) -> dict:
    r = await client.post(
        f"/payroll/periods/{period_id}/lines",
        json={
            "driver_id": driver_id,
            "work_date": work_date,
            "line_type": "HOURS",
            "quantity": str(quantity),
        },
        headers=auth(token),
    )
    assert r.status_code == 201, f"Add HOURS line failed: {r.text}"
    return r.json()


async def _advance_to_approved(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
) -> None:
    """Advance an Open period (with >=1 active non-NMR line) through to Approved."""
    headers = auth(token)
    tr = await client.patch(
        f"/payroll/periods/{period_id}/status",
        json={"status": "InReview"},
        headers=headers,
    )
    assert tr.status_code == 200, f"InReview failed: {tr.text}"
    rv = await client.get("/review/items", headers=headers)
    assert rv.status_code == 200
    item = next(
        (i for i in rv.json()
         if i.get("entity_name") == "PayrollPeriods"
         and i.get("entity_id") == str(period_id)
         and i.get("status") == "Pending"),
        None,
    )
    assert item is not None, f"No pending review item for period {period_id}"
    dec = await client.post(
        f"/review/items/{item['review_item_id']}/decide",
        json={"decision": "Approved"},
        headers=headers,
    )
    assert dec.status_code == 200, f"Review decision failed: {dec.text}"


@contextlib.asynccontextmanager
async def _activated_pay_item_restored(
    client: httpx.AsyncClient,
    token: str,
    direct_db,
    branch_id: int,
    pay_item_code: str,
):
    """
    Temporarily activates a system PayItem for a branch via the real
    settings API, then restores BOTH:

      A. the exact pre-test `payroll.BranchPayItemConfig` row state;
      B. the exact pre-test `audit.AuditLog` state for that activation.

    Audit contract (confirmed by reading `app/settings/service.py`'s
    `_upsert_pay_item_config` -> `_write_settings_audit`, not assumed):
    `entity_name="BranchPayItemConfig"`, `entity_id=f"{branch_id}:{pay_item_id}"`
    — a composite string shared by every create/update/version of that one
    branch+pay-item pair, NOT the config row's surrogate ConfigID. Because
    EntityID can't disambiguate individual audit rows, this uses an AuditID
    watermark instead: the exact set of matching `audit.AuditLog.AuditID`s is
    captured before activation, and only the IDs appearing afterward
    (`after - before`) are deleted as test-created — never a broad delete
    scoped only by EntityName/EntityID/branch/pay-item, and never a
    CreatedAtUtc-range-only condition.

    This mirrors the identical helper added to test_rate_calc_boundaries.py
    for the same underlying settings-audit contract.
    """
    headers = auth(token)
    items_resp = await client.get(f"/settings/branches/{branch_id}/pay-items", headers=headers)
    assert items_resp.status_code == 200, f"List branch pay items failed: {items_resp.text}"
    item = next((i for i in items_resp.json() if i.get("pay_item_code") == pay_item_code), None)
    assert item is not None, f"{pay_item_code} pay item not found for branch {branch_id}"
    pay_item_id = item["pay_item_id"]

    company_row = (await direct_db.execute(
        _text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
        {"bid": branch_id},
    )).mappings().first()
    company_id = company_row["companyid"]
    audit_entity_id = f"{branch_id}:{pay_item_id}"

    async def _config_snapshot():
        rows = (await direct_db.execute(
            _text("""
                SELECT configid, isactive, effectivefrom, effectiveto, notes
                FROM   payroll.branchpayitemconfig
                WHERE  companyid = :cid AND branchid = :bid AND payitemid = :piid
            """),
            {"cid": company_id, "bid": branch_id, "piid": pay_item_id},
        )).mappings().all()
        return {r["configid"]: dict(r) for r in rows}

    async def _audit_id_set():
        rows = (await direct_db.execute(
            _text("""
                SELECT auditid FROM audit.auditlog
                WHERE  entityname = 'BranchPayItemConfig' AND entityid = :eid
            """),
            {"eid": audit_entity_id},
        )).mappings().all()
        return {r["auditid"] for r in rows}

    config_before = await _config_snapshot()
    audit_ids_before = await _audit_id_set()

    try:
        if not item.get("is_active", False):
            patch_resp = await client.patch(
                f"/settings/branches/{branch_id}/pay-items/{pay_item_id}",
                json={"is_active": True},
                headers=headers,
            )
            assert patch_resp.status_code == 200, f"Activate {pay_item_code} failed: {patch_resp.text}"
        yield pay_item_id
    finally:
        config_after = await _config_snapshot()

        for cfg_id in config_after.keys() - config_before.keys():
            await direct_db.execute(
                _text("DELETE FROM payroll.branchpayitemconfig WHERE configid = :id"),
                {"id": cfg_id},
            )
        for cfg_id, original in config_before.items():
            if cfg_id in config_after:
                await direct_db.execute(
                    _text("""
                        UPDATE payroll.branchpayitemconfig
                        SET    isactive = :active, effectivefrom = :eff_from,
                               effectiveto = :eff_to, notes = :notes
                        WHERE  configid = :id
                    """),
                    {"active": original["isactive"], "eff_from": original["effectivefrom"],
                     "eff_to": original["effectiveto"], "notes": original["notes"], "id": cfg_id},
                )
            else:
                await direct_db.execute(
                    _text("""
                        INSERT INTO payroll.branchpayitemconfig
                            (configid, companyid, branchid, payitemid,
                             isactive, effectivefrom, effectiveto, notes)
                        VALUES (:id, :cid, :bid, :piid, :active, :eff_from, :eff_to, :notes)
                    """),
                    {"id": cfg_id, "cid": company_id, "bid": branch_id, "piid": pay_item_id,
                     "active": original["isactive"], "eff_from": original["effectivefrom"],
                     "eff_to": original["effectiveto"], "notes": original["notes"]},
                )

        restored = await _config_snapshot()
        assert restored == config_before, (
            f"BranchPayItemConfig for {pay_item_code}/branch {branch_id} was not "
            f"fully restored after test: before={config_before}, after={restored}"
        )

        audit_ids_after = await _audit_id_set()
        created_audit_ids = audit_ids_after - audit_ids_before
        if created_audit_ids:
            await direct_db.execute(
                _text("DELETE FROM audit.auditlog WHERE auditid = ANY(:ids)"),
                {"ids": list(created_audit_ids)},
            )

        final_audit_ids = await _audit_id_set()
        assert final_audit_ids == audit_ids_before, (
            f"Settings AuditLog for BranchPayItemConfig entity_id={audit_entity_id!r} "
            f"was not fully restored: before={audit_ids_before}, after={final_audit_ids}"
        )


async def _verify_trigger_enabled(direct_db, table: str, trigger: str) -> None:
    """
    Reads pg_trigger directly to confirm a trigger is enabled ('O' = origin,
    i.e. fires normally) after a disable/re-enable cycle. Used as a hard
    guarantee, not an assumption, that cleanup never leaves a trigger off.
    """
    row = (await direct_db.execute(
        _text("""
            SELECT t.tgenabled
            FROM   pg_trigger t
            JOIN   pg_class c ON c.oid = t.tgrelid
            WHERE  c.relname = :table AND t.tgname = :trigger
        """),
        {"table": table, "trigger": trigger},
    )).mappings().first()
    assert row is not None, f"Trigger {trigger} on {table} not found"
    tgenabled = row["tgenabled"]
    tgenabled = tgenabled.decode() if isinstance(tgenabled, bytes) else tgenabled
    assert tgenabled == "O", (
        f"Trigger {trigger} on {table} must be enabled ('O') after cleanup; "
        f"got tgenabled={tgenabled!r} — a disabled trigger here would "
        f"silently remove an immutability guarantee for every later test in "
        f"this session."
    )


async def _delete_period_and_children(
    direct_db,
    period_id: int,
    *,
    was_locked: bool = False,
) -> None:
    """
    Hard-deletes every test-owned row for one period: audit rows referencing
    the period or its draft lines, review items, final lines, draft lines,
    and the period row itself (PayrollPeriodDriverEligibility/eligibility-
    snapshot rows cascade automatically via ON DELETE CASCADE — see
    migration 0057). Leaves the driver row untouched, matching this
    repository's established test convention (drivers are never hard-deleted
    in cleanup elsewhere; the ephemeral per-session test database is
    discarded afterward).

    `was_locked=True` only when the period actually reached Locked status
    (i.e. finalize_period ran) — `trg_final_line_immutable` blocks both
    UPDATE and DELETE on PayrollFinalLines once Locked, so it must be
    disabled to remove those rows. `trg_period_status_revert` only guards
    `UPDATE OF status`, never `DELETE`, so it is never touched here.

    Review-domain audit rows (`REVIEW_ITEM_CREATED` / `REVIEW_ITEM_DECIDED`,
    both written by `app/review/service.py`'s `_write_review_audit` with
    `EntityName='ManagerReviewItems'` and `EntityID=str(review_item_id)` —
    confirmed by reading that function, not assumed) are captured by exact
    review-item ID BEFORE any review row is deleted, then deleted scoped to
    that exact `EntityName` + exact captured IDs — never a broad delete that
    could reach another entity type's audit rows.

    Reliable even when called from an outer `finally` after a failed
    assertion: the trigger disable/re-enable is wrapped in its own
    try/finally so restoration always runs, and is verified afterward via
    `_verify_trigger_enabled` — never left disabled regardless of what
    happens during the deletes themselves.
    """
    draft_line_ids = [
        r["draftlineid"] for r in (await direct_db.execute(
            _text("SELECT draftlineid FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid"),
            {"pid": period_id},
        )).mappings().all()
    ]
    # Capture exact review-item IDs for this period BEFORE any review-domain
    # delete, so the audit cleanup below can scope by exact ID rather than
    # guessing after the owning rows are gone.
    review_item_ids = [
        r["reviewitemid"] for r in (await direct_db.execute(
            _text(
                "SELECT reviewitemid FROM review.managerreviewitems "
                "WHERE entityname = 'PayrollPeriods' AND entityid = :eid"
            ),
            {"eid": str(period_id)},
        )).mappings().all()
    ]

    if was_locked:
        await direct_db.execute(
            _text("ALTER TABLE payroll.payrollfinallines DISABLE TRIGGER trg_final_line_immutable")
        )
    try:
        await direct_db.execute(
            _text("DELETE FROM audit.auditlog WHERE entityname = 'PayrollPeriods' AND entityid = :eid"),
            {"eid": str(period_id)},
        )
        if draft_line_ids:
            await direct_db.execute(
                _text(
                    "DELETE FROM audit.auditlog "
                    "WHERE entityname = 'PayrollDraftLines' AND entityid = ANY(:ids)"
                ),
                {"ids": [str(i) for i in draft_line_ids]},
            )
        if review_item_ids:
            # Exact EntityName + exact captured IDs only — never a broad
            # numeric-ID condition that could overlap another entity type.
            await direct_db.execute(
                _text(
                    "DELETE FROM audit.auditlog "
                    "WHERE entityname = 'ManagerReviewItems' AND entityid = ANY(:ids)"
                ),
                {"ids": [str(i) for i in review_item_ids]},
            )
            await direct_db.execute(
                _text(
                    "DELETE FROM review.managerreviewdecisions WHERE reviewitemid = ANY(:ids)"
                ),
                {"ids": review_item_ids},
            )
            await direct_db.execute(
                _text(
                    "DELETE FROM review.managerreviewitems WHERE reviewitemid = ANY(:ids)"
                ),
                {"ids": review_item_ids},
            )
        await direct_db.execute(
            _text("DELETE FROM payroll.payrollfinallines WHERE payrollperiodid = :pid"),
            {"pid": period_id},
        )
        await direct_db.execute(
            _text("DELETE FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid"),
            {"pid": period_id},
        )
        await direct_db.execute(
            _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": period_id},
        )
    finally:
        if was_locked:
            await direct_db.execute(
                _text("ALTER TABLE payroll.payrollfinallines ENABLE TRIGGER trg_final_line_immutable")
            )
            await _verify_trigger_enabled(direct_db, "payrollfinallines", "trg_final_line_immutable")

    # Residue verification: confirm no rows remain under this exact period_id
    # marker (and its captured review-item IDs) in any of the tables this
    # helper touches — not an assumption. PayrollPeriodDriverEligibility and
    # the CP-2E eligibility-snapshot table are not separately re-queried here
    # because both declare `ON DELETE CASCADE` back to PayrollPeriods
    # (migration 0057_period_driver_eligibility.sql, fk_PPDE_Period and
    # fk_PPES_Period) — the PayrollPeriods delete above already removed them;
    # asserting `periods == 0` after that delete transitively confirms the
    # cascade ran (a dangling child row would be impossible once the parent
    # is gone under a real FK).
    residue = (await direct_db.execute(
        _text("""
            SELECT
                (SELECT COUNT(*) FROM payroll.payrollperiods    WHERE payrollperiodid = :pid) AS periods,
                (SELECT COUNT(*) FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid) AS draftlines,
                (SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :pid) AS finallines,
                (SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityname = 'PayrollPeriods' AND entityid = :eid) AS period_audit,
                (SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityname = 'PayrollDraftLines' AND entityid = ANY(:dlids)) AS draftline_audit
        """),
        {"pid": period_id, "eid": str(period_id), "dlids": [str(i) for i in draft_line_ids]},
    )).mappings().first()
    assert (
        residue["periods"] == 0 and residue["draftlines"] == 0 and residue["finallines"] == 0
        and residue["period_audit"] == 0 and residue["draftline_audit"] == 0
    ), (
        f"Residue check failed for period {period_id}: {dict(residue)} — "
        f"cleanup must remove every row, not merely cancel the period."
    )
    if review_item_ids:
        review_residue = (await direct_db.execute(
            _text("""
                SELECT
                    (SELECT COUNT(*) FROM review.managerreviewitems
                        WHERE reviewitemid = ANY(:ids)) AS items,
                    (SELECT COUNT(*) FROM review.managerreviewdecisions
                        WHERE reviewitemid = ANY(:ids)) AS decisions,
                    (SELECT COUNT(*) FROM audit.auditlog
                        WHERE entityname = 'ManagerReviewItems'
                          AND entityid = ANY(:str_ids)) AS review_audit
            """),
            {"ids": review_item_ids, "str_ids": [str(i) for i in review_item_ids]},
        )).mappings().first()
        assert (
            review_residue["items"] == 0
            and review_residue["decisions"] == 0
            and review_residue["review_audit"] == 0
        ), (
            f"Review-domain residue check failed for review item IDs "
            f"{review_item_ids}: {dict(review_residue)}"
        )


@contextlib.asynccontextmanager
async def _perunit_period(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
    *,
    driver_name: str,
    rate_amount: str,
    effective_from: str,
):
    """
    Shared setup/teardown for the common "fresh driver + one approved HOURLY
    rate + one Open period" scenario used by every PerUnit characterization
    test. Reliable cleanup: the rate is voided via the API and the period
    (plus all its children) is hard-deleted via `_delete_period_and_children`
    inside a `finally`, so a failed assertion mid-test still leaves the
    database clean.
    """
    headers = auth(auth_token)
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
    hourly_rt_id = await _get_hourly_rate_type_id(session_client, auth_token)
    driver_id = await _create_driver(session_client, auth_token, paytest_branch_id, driver_name)
    rate_id = await _create_and_approve_rate(
        session_client, auth_token, driver_id, hourly_rt_id,
        amount=rate_amount, effective_from=effective_from,
    )
    pid = await _open_period(
        session_client, auth_token, paytest_branch_id,
        start=PERUNIT_START, end=PERUNIT_END, db=direct_db,
    )
    try:
        yield driver_id, pid
    finally:
        await session_client.delete(f"/payroll/rates/{rate_id}", headers=headers)
        await _delete_period_and_children(direct_db, pid)
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)


# ---------------------------------------------------------------------------
# 1. Decimal compatibility environment (read-only — no global mutation)
# ---------------------------------------------------------------------------

class TestDecimalContextCharacterization:
    """
    Locks the ambient `decimal` module context that all current calculation
    code silently relies on. Nothing in the codebase calls getcontext()/
    setcontext()/localcontext() — every bare .quantize() uses whatever the
    interpreter's default/thread context happens to be. This test proves
    that default is currently precision=28, rounding=ROUND_HALF_EVEN, and
    does so read-only so it cannot leak state into other test modules.
    """

    def test_ambient_default_context_is_precision_28_round_half_even(self):
        ctx = decimal.getcontext()
        assert ctx.prec == 28, (
            f"Current calculation code relies on the ambient default Decimal "
            f"precision being 28; got {ctx.prec}. If this ever changes, every "
            f".quantize() call in _compute_calculated_amount and friends is "
            f"operating under a different compatibility contract than characterized here."
        )
        assert ctx.rounding == ROUND_HALF_EVEN, (
            f"Current calculation code relies on the ambient default Decimal "
            f"rounding mode being ROUND_HALF_EVEN (Python's implicit default); "
            f"got {ctx.rounding}."
        )

    def test_ambient_default_context_trap_configuration(self):
        """
        These are compatibility CHARACTERISTICS of the current environment —
        this test does not implement or propose a new production Decimal
        policy. `InvalidOperation`/`DivisionByZero`/`Overflow` trapping
        (raising) is Python's implicit default; a silent Underflow/Subnormal/
        Inexact/Rounded/Clamped would currently pass through uncaught in any
        calculation path, since nothing in the codebase configures traps
        explicitly (confirmed via source inspection: no getcontext()/
        setcontext()/localcontext() call exists anywhere in app/).
        """
        ctx = decimal.getcontext()
        enabled_traps = {
            decimal.InvalidOperation: True,
            decimal.DivisionByZero: True,
            decimal.Overflow: True,
        }
        disabled_traps = {
            decimal.Underflow: False,
            decimal.Subnormal: False,
            decimal.Inexact: False,
            decimal.Rounded: False,
            decimal.Clamped: False,
        }
        for trap, expected in {**enabled_traps, **disabled_traps}.items():
            assert ctx.traps[trap] is expected, (
                f"Expected trap {trap.__name__} to be {expected} in the "
                f"ambient default context; got {ctx.traps[trap]}. Current "
                f"calculation code relies on this exact trap configuration — "
                f"e.g. an Inexact/Rounded trap being disabled is what lets "
                f"every bare .quantize() call silently round instead of "
                f"raising."
            )

    def test_isolated_context_reproduces_current_quantize_behavior(self):
        """
        Proves the *behavior* the ambient context produces (not just its
        settings), using an isolated `localcontext()` so no state leaks to
        other tests even if this one is reordered.
        """
        with decimal.localcontext() as ctx:
            ctx.prec = 28
            ctx.rounding = ROUND_HALF_EVEN
            # A value whose 5th decimal digit is exactly 5 with a 4th-decimal
            # digit that is odd (3) must round up to the even neighbor (4)
            # under ROUND_HALF_EVEN — this is the exact rule PerUnit relies on.
            value = Decimal("21.37035")
            assert value.quantize(Decimal("0.0001")) == Decimal("21.3704")


# ---------------------------------------------------------------------------
# 2. PerUnit characterization (authoritative current daily calculation core)
# ---------------------------------------------------------------------------

class TestPerUnitCharacterization:
    """
    PerUnit: qty x approved DriverRate.amount, quantized ONCE to
    Decimal("0.0001") after the full-precision multiply
    (service.py: `calculated = (quantity * resolved_amt).quantize(Decimal("0.0001"))`).

    All HTTP-level tests share the `_perunit_period` context manager for
    setup/reliable teardown (see its docstring above).
    """

    @pytest.mark.asyncio
    async def test_ordinary_exact_multiplication(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        """Case A: product already exact within 4dp — no rounding needed."""
        async with _perunit_period(
            session_client, auth_token, paytest_branch_id, direct_db,
            driver_name="P4S1 PerUnit A", rate_amount="12.3400", effective_from=DATE_MAR05,
        ) as (driver_id, pid):
            line = await _add_hours_line(
                session_client, auth_token, pid, driver_id,
                work_date=DATE_MAR05, quantity="6.5000",
            )
            # 6.5000 * 12.3400 = 80.21 exactly (already within 4dp)
            assert Decimal(str(line["calculated_amount"])) == Decimal("80.2100")
            assert line["needs_manager_review"] is False

    @pytest.mark.asyncio
    async def test_result_with_more_than_4dp_quantized_once(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        """
        Case B: quantity (5dp) x rate (4dp) => a 5-fractional-digit raw
        product. Asserts the full-precision multiply happens first and the
        completed line is quantized exactly once to 4dp — no per-input or
        intermediate 2dp rounding.

        7.12345 * 3.0000 = 21.370350000 exact -> quantize once -> 21.3704
        (4th retained decimal digit is 3, odd -> rounds up to even 4 under
        ROUND_HALF_EVEN; also covered directly in the parametrized boundary
        test below).
        """
        async with _perunit_period(
            session_client, auth_token, paytest_branch_id, direct_db,
            driver_name="P4S1 PerUnit B", rate_amount="3.0000", effective_from=DATE_MAR05,
        ) as (driver_id, pid):
            line = await _add_hours_line(
                session_client, auth_token, pid, driver_id,
                work_date=DATE_MAR05, quantity="7.12345",
            )
            assert Decimal(str(line["calculated_amount"])) == Decimal("21.3704"), (
                f"Expected full-precision multiply (7.12345 x 3.0000 = 21.370350000) "
                f"quantized once to 21.3704; got {line['calculated_amount']}"
            )
            assert line["needs_manager_review"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "case_name,quantity,rate,expected",
        [
            # Retained 4th decimal digit ODD (3) -> ROUND_HALF_EVEN rounds UP
            # to the nearest even digit (4). 7.12345 x 3.0000 = 21.370350000 exact.
            pytest.param("odd_digit_rounds_up", "7.12345", "3.0000", "21.3704", id="odd_digit_rounds_up"),
            # Retained 4th decimal digit EVEN (2) -> ROUND_HALF_EVEN leaves it
            # unchanged (the tie "stays"). 4.0961 x 2.5000 = 10.24025000 exact.
            pytest.param("even_digit_tie_stays", "4.0961", "2.5000", "10.2402", id="even_digit_tie_stays"),
        ],
    )
    async def test_round_half_even_boundary(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
        case_name: str, quantity: str, rate: str, expected: str,
    ):
        """
        Case C: exact 5th-decimal halfway boundaries, both directions of
        ROUND_HALF_EVEN's "round to even" tie-breaking rule.
        """
        async with _perunit_period(
            session_client, auth_token, paytest_branch_id, direct_db,
            driver_name=f"P4S1 PerUnit {case_name}", rate_amount=rate, effective_from=DATE_MAR06,
        ) as (driver_id, pid):
            line = await _add_hours_line(
                session_client, auth_token, pid, driver_id,
                work_date=DATE_MAR06, quantity=quantity,
            )
            assert Decimal(str(line["calculated_amount"])) == Decimal(expected), (
                f"Expected {quantity} x {rate} exact product, ROUND_HALF_EVEN "
                f"-> {expected}; got {line['calculated_amount']}"
            )

    @pytest.mark.asyncio
    async def test_zero_quantity_boundary(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        """
        Case D: zero quantity is currently permitted (DraftLineCreate's
        validator only rejects negative quantity, not zero). Characterizes
        current behavior only — no new validation rule is introduced.
        """
        async with _perunit_period(
            session_client, auth_token, paytest_branch_id, direct_db,
            driver_name="P4S1 PerUnit Zero", rate_amount="99.0000", effective_from=DATE_MAR08,
        ) as (driver_id, pid):
            line = await _add_hours_line(
                session_client, auth_token, pid, driver_id,
                work_date=DATE_MAR08, quantity="0",
            )
            assert Decimal(str(line["calculated_amount"])) == Decimal("0.0000")
            assert line["needs_manager_review"] is False

    @pytest.mark.asyncio
    async def test_successful_dispatch_result_type_is_decimal_never_float(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        """
        Case E (corrected): exercises the actual PerUnit dispatch path
        (`_compute_calculated_amount`, not an isolated Decimal multiplication)
        with a REAL resolved rate, so the assertion is anchored to a
        successful result rather than the `None` no-rate path. Proves the
        dispatch's calculated_amount is Decimal, is not float, is not None,
        and matches the exact expected value.
        """
        headers = auth(auth_token)
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        hourly_rt_id = await _get_hourly_rate_type_id(session_client, auth_token)
        driver_id = await _create_driver(session_client, auth_token, paytest_branch_id, "P4S1 PerUnit CaseE")

        driver_row = (await direct_db.execute(
            _text("SELECT companyid FROM core.drivers WHERE driverid = :did"),
            {"did": driver_id},
        )).mappings().first()
        rate_row = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.driverrates
                    (companyid, branchid, driverid, ratetypeid, amount, effectivefrom, status)
                VALUES (:cid, :bid, :did, :rtid, :amt, :eff, 'Approved')
                RETURNING driverrateid
            """),
            {
                "cid": driver_row["companyid"], "bid": paytest_branch_id, "did": driver_id,
                "rtid": hourly_rt_id, "amt": Decimal("9.2500"),
                "eff": datetime.date.fromisoformat(DATE_MAR09),
            },
        )).mappings().first()
        try:
            result = await _compute_calculated_amount(
                rate_behavior="PerUnit",
                rate_code="HOURLY",
                quantity=Decimal("6"),
                rate_amount_override=None,
                driver_id=driver_id,
                company_id=driver_row["companyid"],
                as_of_date=datetime.date.fromisoformat(DATE_MAR09),
                db=direct_db,
            )
            assert result.calculated_amount is not None, (
                "Expected a successful PerUnit resolution — a real Approved "
                "DriverRate was seeded for this exact driver/date/rate_code."
            )
            assert isinstance(result.calculated_amount, Decimal)
            assert not isinstance(result.calculated_amount, float)
            # 6 x 9.2500 = 55.5000 exactly (already within 4dp).
            assert result.calculated_amount == Decimal("55.5000")
            assert result.needs_manager_review is False
        finally:
            await direct_db.execute(
                _text("DELETE FROM payroll.driverrates WHERE driverrateid = :id"),
                {"id": rate_row["driverrateid"]},
            )


# ---------------------------------------------------------------------------
# 3. EnteredAmount characterization (direct/manual boundary, not a formula)
# ---------------------------------------------------------------------------

class TestEnteredAmountCharacterization:
    """
    EnteredAmount: `_compute_calculated_amount` returns
    `(rate_amount_override, False)` immediately — the supplied direct amount
    passes through verbatim. The PerUnit 0.0001-quantize rule is NOT applied
    by the dispatch for this behavior.

    Live-reachability note: as of this slice, there is no supported public
    creation path for a NEW custom Period + EnteredAmount PayItem — admin
    direct creation of custom Period items is rejected by the settings API
    (`test_create_period_item_is_rejected` in test_settings_custom_pay_items.py),
    and CDPI is a Daily-only domain. The only live system items using the
    M14 direct-amount storage contract (`CalculatedAmount = amount`, no
    dispatch through `_compute_calculated_amount` at all) are the Period-
    scope system items BONUS (blocked from `/period-pay`; use the canonical
    Bonus Events API) and ADJUSTMENT (RateBehavior='Fixed' at the catalog
    level). See TestDirectManualBoundary below for the live HTTP/DB
    characterization of that shared direct-amount storage contract.
    """

    @pytest.mark.asyncio
    async def test_dispatch_preserves_override_exact_no_quantization(
        self, direct_db,
    ):
        """
        Unit-level dispatch characterization: an override with more than
        four fractional digits passes through completely unchanged — the
        dispatch does not quantize it to 0.0001 (that rule is PerUnit-only).
        """
        override = Decimal("42.123456789")
        result = await _compute_calculated_amount(
            rate_behavior="EnteredAmount",
            rate_code=None,
            quantity=Decimal("1"),
            rate_amount_override=override,
            driver_id=1,
            company_id=1,
            as_of_date=datetime.date(2091, 3, 5),
            db=direct_db,
        )
        assert result.calculated_amount == override, (
            "EnteredAmount dispatch must return the override completely "
            "unchanged — no quantization, no rounding."
        )
        assert result.calculated_amount == Decimal("42.123456789"), (
            "Confirms no 0.0001 quantization occurred (would have produced "
            "42.1235 under ROUND_HALF_EVEN)."
        )
        assert result.needs_manager_review is False
        assert result.rate_behavior == "EnteredAmount"

    @pytest.mark.asyncio
    async def test_no_live_creation_path_for_new_period_entered_amount_item(
        self, session_client: httpx.AsyncClient, auth_token: str,
    ):
        """
        Documents (re-confirms) the current reachability gap: admin direct
        creation of a NEW custom Period + EnteredAmount PayItem is rejected
        today. This is why sections 1-2 of the required output report this
        as a live-reachability gap rather than a full HTTP round-trip.
        """
        resp = await session_client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "P4S1_PERIOD_EA",
                "pay_item_name": "P4S1 Period EnteredAmount Probe",
                "item_scope": "Period",
                "rate_behavior": "EnteredAmount",
                "category": "Bonus",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422, (
            f"Expected current guard to reject new custom Period item creation; "
            f"got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# 4. Fixed and None characterization (dispatch-only — see class docstring)
# ---------------------------------------------------------------------------

class TestFixedNoneCharacterization:
    """
    Fixed / None: `_compute_calculated_amount` falls through to the generic
    `if rate_behavior != "PerUnit": return (None, False)` branch — no
    computation is ever performed for either value.

    Live-reachability note: the only historical Daily/Fixed system item
    (OVERNIGHT) was migrated to PerUnit by migration 0022
    (`0022_overnight_rate_repair`); there is currently no live, creatable
    Daily-scope Fixed PayItem to exercise end-to-end (custom Daily creation
    is CDPI-only, and CDPI implements only PerUnit). Per this slice's
    instructions, a focused unit-level dispatch characterization is used
    instead. `DailyStatus`/`DailyNote` (None behavior, informational-only)
    remain live-reachable and are characterized via the dispatch call below;
    their storage/serialization behavior for informational lines is out of
    scope for this numeric-focused slice.
    """

    @pytest.mark.asyncio
    async def test_fixed_dispatch_returns_no_computed_amount(self, direct_db):
        result = await _compute_calculated_amount(
            rate_behavior="Fixed",
            rate_code=None,
            quantity=Decimal("3"),
            rate_amount_override=Decimal("55.5555"),
            driver_id=1,
            company_id=1,
            as_of_date=datetime.date(2091, 3, 5),
            db=direct_db,
        )
        assert result.calculated_amount is None, (
            "Fixed dispatch must return no computed amount even when a "
            "rate_amount_override is supplied — Fixed is not auto-processed "
            "through PerUnit quantization or any formula."
        )
        assert result.needs_manager_review is False
        assert result.rate_behavior == "Fixed"

    @pytest.mark.asyncio
    async def test_none_dispatch_returns_no_computed_amount(self, direct_db):
        result = await _compute_calculated_amount(
            rate_behavior="None",
            rate_code=None,
            quantity=Decimal("1"),
            rate_amount_override=None,
            driver_id=1,
            company_id=1,
            as_of_date=datetime.date(2091, 3, 5),
            db=direct_db,
        )
        assert result.calculated_amount is None
        assert result.needs_manager_review is False
        assert result.rate_behavior == "None"


# ---------------------------------------------------------------------------
# 5. Direct/manual boundary — live HTTP/DB round trip (Period Pay, M14 contract)
# ---------------------------------------------------------------------------

class TestDirectManualBoundary:
    """
    Period Pay lines (BONUS/ADJUSTMENT and any future Period item) never
    call `_compute_calculated_amount` at all: `add_period_pay_line` stores
    `CalculatedAmount = data.amount` directly at INSERT time (the "M14
    storage contract"). This is the live-reachable proof that EnteredAmount
    and Fixed period behaviors share one direct-amount storage path, distinct
    from PerUnit's computed-formula path.

    ADJUSTMENT is used here (RateBehavior='Fixed' at the catalog level) —
    BONUS is blocked from `/period-pay` since CP-3A (canonical Bonus Events
    API only); no live EnteredAmount Period item can currently be created
    (see TestEnteredAmountCharacterization above).
    """

    @pytest.mark.asyncio
    async def test_period_pay_direct_amount_bypasses_perunit_quantization(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, paytest_driver_id: int, direct_db,
    ):
        headers = auth(auth_token)
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)

        # ADJUSTMENT activation/restoration (BranchPayItemConfig + its
        # settings-audit rows) is independent from period cleanup below: the
        # activation context's own `finally` runs on the way out of this
        # `async with` block regardless of whether the inner period try/finally
        # succeeded or raised, so one cleanup failure cannot silently prevent
        # the other from running.
        async with _activated_pay_item_restored(
            session_client, auth_token, direct_db, paytest_branch_id, "ADJUSTMENT",
        ):
            pid = await _open_period(
                session_client, auth_token, paytest_branch_id,
                start=FALLBACK_START, end=FALLBACK_END, db=direct_db,
            )
            try:
                supplied_amount = "77.123456"  # 6 fractional digits — supported by amount: Decimal
                resp = await session_client.post(
                    f"/payroll/periods/{pid}/period-pay",
                    json={
                        "driver_id": paytest_driver_id,
                        "line_type": "ADJUSTMENT",
                        "amount": supplied_amount,
                        "notes": "P4S1 direct/manual boundary characterization",
                    },
                    headers=headers,
                )
                assert resp.status_code == 201, f"Add ADJUSTMENT line failed: {resp.text}"
                body = resp.json()

                # (2) value after DB storage, read back via the API response
                #     (PayrollDraftLines.CalculatedAmount is NUMERIC(18,4) — the
                #     supplied 6dp value is coerced by PostgreSQL at INSERT time).
                stored = Decimal(str(body["calculated_amount"]))
                assert stored == Decimal("77.1235"), (
                    f"Expected NUMERIC(18,4) coercion of {supplied_amount} (no PerUnit "
                    f"0.0001-quantize business rule — this is raw column-scale "
                    f"truncation/rounding by Postgres, characterized separately in "
                    f"TestPostgresNumericCoercion); got {stored}"
                )
                # EVIDENCE BOUNDARY: this proves generic direct/manual persistence
                # (ADJUSTMENT, RateBehavior='Fixed' at the catalog level) — it does
                # NOT prove EnteredAmount-specific persistence, since no live
                # EnteredAmount Period item can currently be created (see
                # TestEnteredAmountCharacterization above). The two behaviors
                # share this one storage code path (`add_period_pay_line` never
                # calls `_compute_calculated_amount`), which is why this is valid
                # evidence for the shared mechanism, not an EnteredAmount-specific
                # claim.
                assert body["needs_manager_review"] is False
            finally:
                await _delete_period_and_children(direct_db, pid)
                await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)


# ---------------------------------------------------------------------------
# 6. Legacy/manual fallback characterization — THE P1 GATE
#    CalculatedAmount IS NULL -> Quantity * RateAmount
# ---------------------------------------------------------------------------

class TestLegacyManualFallbackCharacterization:
    """
    This is the Codex-discovered P1 characterization gate.

    There is no LIVE, currently-creatable path that produces a PerUnit Daily
    line with CalculatedAmount=NULL and a non-NULL RateAmount: Phase 4C
    blocks manual `rate_amount` for PerUnit lines at both add and update
    (`test_manual_rate_amount_rejected_for_daily_line` /
    `_on_update` in test_rate_calc_boundaries.py). That guard is an
    API-layer validation, not a database constraint — historical rows that
    predate the guard (or were written directly) can still exist with this
    exact shape. This test simulates that historical shape via a direct DB
    insert (bypassing the API, not production code) to characterize what the
    CURRENT preview and finalization code paths actually do with it today.

    Fixture: quantity=3.0001, rateamount=2.0001 (both at the column's
    native NUMERIC(18,4) scale — DriverRates/DraftLines columns cannot
    themselves hold more than 4dp, so the >4dp raw value can only arise from
    MULTIPLYING two already-4dp-scaled numbers) -> exact raw product
    6.00050001 (8 fractional digits; unambiguously above the 4dp rounding
    point under any rounding rule, so this is not conflated with the
    dedicated ROUND_HALF_EVEN boundary tests above).

    Preview (service.py get_finalization_preview): Python Decimal
    multiplication, NO explicit 0.0001 quantization in the fallback branch
    (`final_amt = qty * (rate if rate is not None else Decimal("0"))`).

    Finalization (service.py finalize_period Step 3): SQL multiplication
    written directly into PayrollFinalLines.FinalAmount NUMERIC(18,4); the
    PostgreSQL column coerces the excess fractional scale at INSERT time.

    This test intentionally proves these two do NOT reach the same exact
    Decimal value — that divergence is the characterized compatibility debt
    CP-4A must preserve (not silently unify) until a product decision is made.
    """

    @pytest.mark.asyncio
    async def test_preview_and_finalization_diverge_on_uncoerced_legacy_fallback(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        headers = auth(auth_token)
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        driver_id = await _create_driver(
            session_client, auth_token, paytest_branch_id, "P4S1 Fallback Driver",
        )

        pid = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start=FALLBACK_START, end=FALLBACK_END, db=direct_db,
        )
        try:
            # Simulate a historical PerUnit row that predates the Phase 4C
            # manual-rate-amount guard: CalculatedAmount=NULL,
            # NeedsManagerReview=FALSE, RateAmount supplied directly.
            # (No approved DriverRate exists for this driver, matching the
            # documented `_compute_calculated_amount` PerUnit branch
            # `rate_amount_override is not None -> (None, False)` shape.)
            insert_result = await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverid,
                         workdate, linetype, linescope, quantity, rateamount,
                         calculatedamount, sourcetype, status, needsmanagerreview)
                    VALUES
                        (1, :bid, :pid, :did,
                         :wd, 'HOURS', 'Daily', :qty, :rate,
                         NULL, 'Manual', 'Active', FALSE)
                    RETURNING draftlineid
                """),
                {
                    "bid": paytest_branch_id, "pid": pid, "did": driver_id,
                    "wd": datetime.date.fromisoformat(DATE_APR02),
                    "qty": Decimal("3.0001"), "rate": Decimal("2.0001"),
                },
            )
            draft_line_id = insert_result.scalar_one()

            # finalization-preview requires Approved status.
            await _advance_to_approved(session_client, auth_token, pid)

            # --- Preview: unrounded Python Decimal fallback, asserted BEFORE
            # any DB final-line coercion happens (finalize hasn't run yet). ---
            preview_resp = await session_client.get(
                f"/payroll/periods/{pid}/finalization-preview",
                headers=headers,
            )
            assert preview_resp.status_code == 200, f"Preview failed: {preview_resp.text}"
            preview_body = preview_resp.json()
            preview_line = next(
                (l for l in preview_body["lines"] if l["draft_line_id"] == draft_line_id),
                None,
            )
            assert preview_line is not None, "Fallback line not found in preview lines"
            preview_final = Decimal(str(preview_line["final_amount"]))
            assert preview_final == Decimal("6.00050001"), (
                f"Preview's fallback is Python Decimal qty*rate with NO explicit "
                f"quantize(); expected exact 6.00050001, got {preview_final}"
            )

            # --- Finalization: SQL fallback coerced into NUMERIC(18,4), on
            # the SAME historical draft row that drove the preview above. ---
            fin = await session_client.post(f"/payroll/periods/{pid}/finalize", headers=headers)
            assert fin.status_code == 200, f"Finalize failed: {fin.text}"

            # --- Independent read of the actual Ledger/final-lines endpoint
            # used by the product (the same GET /final-lines that
            # test_ledger.py's TestFinalLines suite exercises) — a live HTTP
            # request through the real FinalLineSummary-typed response, not a
            # direct SQL read presented as "ledger evidence". ---
            fl = await session_client.get(f"/payroll/periods/{pid}/final-lines", headers=headers)
            assert fl.status_code == 200
            final_line = next(
                (l for l in fl.json() if l["draft_line_id"] == draft_line_id),
                None,
            )
            assert final_line is not None, "Fallback line not found in the final-lines ledger endpoint"
            # This endpoint returns individual persisted final lines (one row
            # per PayrollFinalLines record), not an aggregated driver total.
            finalized_amount = Decimal(str(final_line["final_amount"]))
            assert finalized_amount == Decimal("6.0005"), (
                f"Finalization writes the SQL product directly into a "
                f"NUMERIC(18,4) column; expected Postgres-coerced 6.0005, "
                f"got {finalized_amount}"
            )

            # --- THE CHARACTERIZED DIVERGENCE — compared directly, no
            # normalization of either value. ---
            assert preview_final != finalized_amount, (
                "This slice intentionally characterizes that preview "
                "(6.00050001, full Python Decimal precision) and "
                "finalization (6.0005, Postgres NUMERIC(18,4)-coerced, read "
                "back through the actual final-lines ledger endpoint) do NOT "
                "reach the same exact value on this legacy fallback path "
                "today. This is pre-existing compatibility debt, not a bug "
                "introduced by this test, and CP-4A must preserve — not "
                "silently unify — this divergence pending a product decision."
            )
        finally:
            # This period was finalized (Locked) above, so PayrollFinalLines
            # rows exist and trg_final_line_immutable must be disabled to
            # remove them; the helper guarantees restoration and verifies it.
            await _delete_period_and_children(direct_db, pid, was_locked=True)


# ---------------------------------------------------------------------------
# 7. PostgreSQL NUMERIC coercion characterization
# ---------------------------------------------------------------------------

class TestPostgresNumericCoercion:
    """
    Characterizes actual PostgreSQL behavior (not Python/SQLAlchemy
    assumptions) for NUMERIC(18,4) and NUMERIC(18,2) column-scale coercion.
    """

    @pytest.mark.asyncio
    async def test_numeric_18_4_coercion_via_raw_cast(self, direct_db):
        """Distinguishes pure PostgreSQL CAST behavior from any app-layer rounding."""
        row = (await direct_db.execute(
            _text("SELECT CAST(:v AS NUMERIC(18,4)) AS coerced"),
            {"v": "6.0000500001"},
        )).mappings().first()
        assert Decimal(str(row["coerced"])) == Decimal("6.0001"), (
            f"Expected PostgreSQL to coerce 6.0000500001 to NUMERIC(18,4) as "
            f"6.0001; got {row['coerced']}"
        )

    @pytest.mark.asyncio
    async def test_numeric_18_2_coercion_via_raw_cast_for_comparison(self, direct_db):
        """
        Comparison point only (no production 2dp column is exercised here):
        PostgreSQL's NUMERIC(18,2) coercion of the same style of excess-scale
        input, for contrast against the 18,4 case above.
        """
        row = (await direct_db.execute(
            _text("SELECT CAST(:v AS NUMERIC(18,2)) AS coerced"),
            {"v": "6.005"},
        )).mappings().first()
        assert Decimal(str(row["coerced"])) == Decimal("6.01"), (
            f"Expected PostgreSQL to coerce 6.005 to NUMERIC(18,2) as 6.01; "
            f"got {row['coerced']}"
        )

    @pytest.mark.asyncio
    async def test_numeric_18_4_coercion_via_real_draftlines_insert(
        self, direct_db, paytest_branch_id: int,
    ):
        """
        Same coercion, but through the real model-persistence path
        (an actual INSERT into payroll.payrolldraftlines), not just a bare
        CAST expression — distinguishes SQLAlchemy binding from PostgreSQL
        column coercion by reading the value back after a real round trip.

        Setup and assertions run inside `try`; the created period (and its
        one draft line) is hard-deleted in `finally` regardless of whether
        the assertion passes or fails, using a period code unique to this
        test run so cleanup cannot affect any other test.
        """
        driver_row = (await direct_db.execute(
            _text("SELECT driverid FROM core.drivers WHERE branchid = :bid LIMIT 1"),
            {"bid": paytest_branch_id},
        )).mappings().first()
        assert driver_row is not None, "No driver found on PAYTEST branch for coercion test"

        period_row = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (1, :bid, 'Open', :code, 'P4S1 Coercion Test', 'Week', :start, :end)
                RETURNING payrollperiodid
            """),
            {"bid": paytest_branch_id, "code": f"P4S1-COERCE-{paytest_branch_id}-{uuid.uuid4().hex[:8]}",
             "start": datetime.date.fromisoformat(PGCOERCE_START),
             "end": datetime.date.fromisoformat(PGCOERCE_END)},
        )).mappings().first()
        pid = period_row["payrollperiodid"]

        try:
            insert_result = await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverid,
                         workdate, linetype, linescope, quantity, rateamount,
                         calculatedamount, sourcetype, status, needsmanagerreview)
                    VALUES
                        (1, :bid, :pid, :did,
                         :wd, 'DailyNote', 'Daily', :qty, NULL,
                         NULL, 'Manual', 'Active', FALSE)
                    RETURNING draftlineid, quantity
                """),
                {
                    "bid": paytest_branch_id, "pid": pid, "did": driver_row["driverid"],
                    "wd": datetime.date.fromisoformat(PGCOERCE_START),
                    "qty": Decimal("12.123456"),  # 6 fractional digits -> NUMERIC(18,4) column
                },
            )
            row = insert_result.mappings().first()
            assert Decimal(str(row["quantity"])) == Decimal("12.1235"), (
                f"Expected the real PayrollDraftLines.Quantity NUMERIC(18,4) column "
                f"to coerce 12.123456 to 12.1235 (matching the CAST-based test "
                f"above); got {row['quantity']}"
            )
        finally:
            await _delete_period_and_children(direct_db, pid)


# ---------------------------------------------------------------------------
# 8. HTTP Decimal serialization characterization
# ---------------------------------------------------------------------------

class TestHttpDecimalSerialization:
    """
    Characterizes the ACTUAL FastAPI/Pydantic JSON contract for Decimal
    fields via the real test client — not an isolated model-level assumption.
    """

    @pytest.mark.asyncio
    async def test_calculated_amount_exposes_json_string_at_4dp(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        """
        PerUnit calculated_amount (NUMERIC(18,4)-scale) is exposed as a JSON
        STRING (not a JSON number) by the real HTTP response — this is
        Pydantic v2's default Decimal-field JSON serialization behavior,
        confirmed here through the actual endpoint rather than assumed.
        """
        async with _perunit_period(
            session_client, auth_token, paytest_branch_id, direct_db,
            driver_name="P4S1 HTTP Serialization", rate_amount="10.5000", effective_from=DATE_MAR09,
        ) as (driver_id, pid):
            resp = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": driver_id,
                    "work_date": DATE_MAR09,
                    "line_type": "HOURS",
                    "quantity": "4.0000",
                },
                headers=auth(auth_token),
            )
            assert resp.status_code == 201, f"Add HOURS line failed: {resp.text}"

            # Raw JSON text check — proves the wire format directly, not just
            # the parsed Python object's type.
            assert '"calculated_amount":"42.0000"' in resp.text, (
                f"Expected calculated_amount to be a quoted JSON STRING "
                f"\"42.0000\" in the raw response body; got: {resp.text}"
            )
            parsed = resp.json()["calculated_amount"]
            assert isinstance(parsed, str), (
                f"json.loads() must yield a Python str for calculated_amount "
                f"(Pydantic v2 default Decimal->JSON-string behavior), not a "
                f"float/int; got {type(parsed).__name__}"
            )
            assert Decimal(parsed) == Decimal("42.0000")

    def test_isolated_pydantic_decimal_serialization_matches_2dp_case(self):
        """
        Supplementary evidence only (per this slice's instructions, an
        isolated model-level test does not by itself prove the API
        contract — see the live HTTP test above for that). There is no
        currently-reachable live 2dp-scale response field in the Daily/
        PerUnit/direct-manual paths covered by this slice (PayrollDraftLines
        and PayrollFinalLines are uniformly NUMERIC(18,4); the 2dp-scale
        PayrollBonusEvents.Amount field belongs to the separate bonus lane,
        explicitly out of scope here). This isolated check documents that
        Pydantic v2's Decimal->JSON-string behavior is scale-independent,
        deferring the LIVE 2dp HTTP proof to a bonus-focused characterization
        slice.
        """
        from pydantic import BaseModel

        class _Probe(BaseModel):
            amount: Decimal

        dumped = _Probe(amount=Decimal("6.01")).model_dump(mode="json")
        assert dumped == {"amount": "6.01"}
        assert isinstance(dumped["amount"], str)


# ---------------------------------------------------------------------------
# 9. Float-boundary findings (documented — no production code to test against)
# ---------------------------------------------------------------------------

class TestFloatProhibitionCharacterization:
    """
    CP-4A's pure/versioned calculation core does not exist yet, so there is
    no production boundary to assert a float-rejection contract against
    without inventing production abstractions purely to test them (explicitly
    forbidden by this slice's instructions). This class instead re-confirms,
    for every CURRENT path exercised in this slice, that the returned
    authoritative financial value is a Decimal, never a float.

    The missing future test (CP-4A core rejects/raises on float input) is
    recorded as a gap in the required output report, to be added once the
    core module exists.
    """

    @pytest.mark.asyncio
    async def test_all_current_dispatch_branches_return_decimal_not_float(self, direct_db):
        cases = [
            ("EnteredAmount", None, Decimal("1"), Decimal("9.99")),
            ("Fixed", None, Decimal("1"), None),
            ("None", None, Decimal("1"), None),
        ]
        for rate_behavior, rate_code, qty, override in cases:
            result = await _compute_calculated_amount(
                rate_behavior=rate_behavior,
                rate_code=rate_code,
                quantity=qty,
                rate_amount_override=override,
                driver_id=1,
                company_id=1,
                as_of_date=datetime.date(2091, 3, 5),
                db=direct_db,
            )
            if result.calculated_amount is not None:
                assert isinstance(result.calculated_amount, Decimal)
            assert not isinstance(result.calculated_amount, float)
