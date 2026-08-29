"""
Phase 4 calculation characterization gate — Slice 2: canonical Status fact
and Status-derived payment.

Locks the CURRENT (pre-CP-4A) behavior of:
  - the canonical Status source (`PayrollPeriodDriverDayEntryState.StatusKeyID`);
  - the `DailyStatus` compatibility projection (not source of truth);
  - the current derived `STATUS_PAYMENT` financial line (real linetype:
    the resolved Status-rate-column's RateType code, e.g. `STATUS_PAY` —
    see the naming-inconsistency note in `TestStatusNotAPayItem` below);
  - Status-rate resolution (driver/effective-date/tie-break/no-rate);
  - preview vs. finalization parity (or lack thereof) for Status payment;
  - min/max participation;
  - the current architectural separation from PayItems, PTO_STATUS, DAC,
    and any future StatusKeyPayRule.

This is a characterization slice, not a correctness slice: tests lock what
the code currently does, including known inconsistencies, without changing
any production behavior.

Locked architecture boundaries (see CURRENT_PAYROLL_BACKEND_MASTER_PLAN.md):
  - Status is a System Status Entry Channel, not a PayItem.
  - One canonical selected StatusKey per driver/day, at
    `PayrollPeriodDriverDayEntryState.StatusKeyID`.
  - `DailyStatus` DraftLines and derived `STATUS_PAYMENT`/STATUS_PAY lines
    are compatibility projections only, never source truth.
  - `PTO_STATUS` remains removed (migration 0055) and must not return.
  - `StatusKeyPayRule` / `StatusKeyAllowanceRules` remain future-only; no DAC
    or future-rule behavior is implemented or simulated here.

Existing coverage NOT re-tested here (see backend/tests/test_cp2d_canonical_entry_state.py
and backend/tests/test_cp2d2_status_payment.py): basic HoursValue x rate
happy-path multiply-and-quantize, NMR-on-no-rate, void-on-clear/void-on-switch,
canonical-row schema/constraints/indexes, migration/PTO_STATUS-absence checks,
manual-edit guards, SourceSnapshot population. This file adds: ROUND_HALF_EVEN
boundary cases, the hours-value truthiness-vs-zero edge case, DriverRate
tie-break/Pending-exclusion, a genuine (non-synthetic) preview-vs-finalization
divergence, a genuine (non-synthetic) min/max real end-to-end proof (the
existing CP-3C test injects a synthetic line literally typed 'STATUS_PAYMENT',
which is not the real linetype the sync function writes — see that test's
`_inject_status_payment_line` helper), and the PayItem/DAC/future-rule
negative-boundary proofs.

Isolation: all periods use year 2098 dates (far future) to avoid conflicts
with other test modules (2097=CP-2D2, 2096=CP-2D1, 2091=Slice 1, 2087=ledger,
2089=cp5 eligibility, 2082=rate-calc boundaries, 2085=preview).
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


# ---------------------------------------------------------------------------
# Module-level constants (2098 dates — isolated from other test modules)
# ---------------------------------------------------------------------------

_PERIOD_CODE_PREFIX = "P4S2-"

PERIOD_A_START = "2098-02-02"
PERIOD_A_END = "2098-02-08"
DATE_FEB02 = "2098-02-02"
DATE_FEB03 = "2098-02-03"


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_active_periods(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    db=None,
) -> None:
    """
    Narrowed to this module's own uniquely-marked periods only (`periodcode`
    starting with `P4S2-`) — never touches periods owned by other tests
    sharing the same PAYTEST branch (Slice 1 lesson).
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
    db,
    suffix: str = "",
) -> int:
    """Insert an Open period directly into DB. Returns period_id."""
    code = f"{_PERIOD_CODE_PREFIX}{branch_id}-{start}{suffix}"
    row = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": code, "name": f"P4S2 {start}{suffix}",
         "start": datetime.date.fromisoformat(start), "end": datetime.date.fromisoformat(end)},
    )).mappings().first()
    if row is None:
        # Falls back to the pre-existing row with this exact periodcode (the
        # same convention used by test_cp2d2_status_payment.py's
        # `_open_period_db`) — the shared test database persists rows across
        # separate pytest invocations, not just within one session, so a
        # prior interrupted run's row can still be present.
        row = (await db.execute(
            _text("SELECT payrollperiodid FROM payroll.payrollperiods WHERE periodcode = :code"),
            {"code": code},
        )).mappings().first()
    assert row is not None, f"Could not create or find period {code!r}"
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


async def _get_status_pay_rate_type_id(client: httpx.AsyncClient, token: str) -> int:
    rv = await client.get("/payroll/rate-types", headers=auth(token))
    assert rv.status_code == 200
    for rt in rv.json():
        if rt["rate_code"] == "STATUS_PAY":
            return rt["rate_type_id"]
    raise AssertionError("STATUS_PAY rate type not found")


@contextlib.asynccontextmanager
async def _status_rate_column(direct_db, company_id: int, branch_id: int):
    """
    Ownership-aware StatusRateColumns lifecycle (P1 fix).

    Reuses the branch's existing active StatusRateColumn if the migration
    seed already provisioned one (mirrors test_cp2d2_status_payment.py's
    `cp2d2_src_col_id` fixture fallback) — in that case it is never mutated
    or deleted, since it is shared/seeded, not test-owned.

    Only if none exists does this create a test-owned fallback row, marked
    with a unique `P4S2-SRC-<hex>` name so it can never be confused with a
    seeded row, and hard-deletes exactly that row on exit — never a broad
    delete by RateTypeID/branch/company alone. Deletion happens in this
    context manager's own `finally`, which callers must nest OUTSIDE any
    StatusKey creation that references this column (StatusKeys FK-reference
    StatusRateColumnID), so the column is only ever deleted after every
    StatusKey pointing at it has already been removed by the caller.

    Yields the StatusRateColumnID either way — callers do not need to know
    whether it was reused or created.
    """
    existing = (await direct_db.execute(
        _text("""
            SELECT statusratecolumnid
            FROM   payroll.statusratecolumns
            WHERE  branchid = :bid AND companyid = :cid AND isactive = TRUE
            ORDER BY isdefault DESC, statusratecolumnid
            LIMIT 1
        """),
        {"bid": branch_id, "cid": company_id},
    )).mappings().first()
    if existing:
        yield existing["statusratecolumnid"]
        return

    rt_row = (await direct_db.execute(
        _text("SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = 'STATUS_PAY'"),
    )).mappings().first()
    assert rt_row is not None, "STATUS_PAY RateType not seeded"
    marker = f"P4S2-SRC-{uuid.uuid4().hex[:8]}"
    row = (await direct_db.execute(
        _text("""
            INSERT INTO payroll.statusratecolumns
                (companyid, branchid, ratetypeid, columnname, normalizedcolumnname, isdefault, isactive)
            VALUES (:cid, :bid, :rtid, :marker, :norm_marker, TRUE, TRUE)
            RETURNING statusratecolumnid
        """),
        {
            "cid": company_id, "bid": branch_id, "rtid": rt_row["ratetypeid"],
            "marker": marker, "norm_marker": marker.upper(),
        },
    )).mappings().first()
    src_col_id = row["statusratecolumnid"]
    try:
        yield src_col_id
    finally:
        await direct_db.execute(
            _text("DELETE FROM payroll.statusratecolumns WHERE statusratecolumnid = :id"),
            {"id": src_col_id},
        )
        residue = (await direct_db.execute(
            _text(
                "SELECT COUNT(*) AS cnt FROM payroll.statusratecolumns WHERE statusratecolumnid = :id"
            ),
            {"id": src_col_id},
        )).mappings().first()
        assert residue["cnt"] == 0, (
            f"Fallback StatusRateColumns row {src_col_id} ({marker}) was not removed"
        )


@contextlib.asynccontextmanager
async def _forced_owned_status_rate_column(direct_db, company_id: int, branch_id: int):
    """
    Test-only variant of `_status_rate_column` (P2 fix-forward, section 6B):
    unconditionally creates a NEW test-owned fallback row, unlike
    `_status_rate_column` which reuses a shared/seeded row when one already
    exists for the branch. Used only to deterministically exercise the
    fallback-row creation/cleanup branch even when a shared row is present
    -- never touches or reuses any shared/seeded StatusRateColumns row.
    Marked with the same `P4S2-SRC-<hex>` convention so it can never be
    confused with a seeded row.
    """
    rt_row = (await direct_db.execute(
        _text("SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = 'STATUS_PAY'"),
    )).mappings().first()
    assert rt_row is not None, "STATUS_PAY RateType not seeded"
    marker = f"P4S2-SRC-{uuid.uuid4().hex[:8]}"
    row = (await direct_db.execute(
        _text("""
            INSERT INTO payroll.statusratecolumns
                (companyid, branchid, ratetypeid, columnname, normalizedcolumnname, isdefault, isactive)
            VALUES (:cid, :bid, :rtid, :marker, :norm_marker, FALSE, TRUE)
            RETURNING statusratecolumnid
        """),
        {
            "cid": company_id, "bid": branch_id, "rtid": rt_row["ratetypeid"],
            "marker": marker, "norm_marker": marker.upper(),
        },
    )).mappings().first()
    src_col_id = row["statusratecolumnid"]
    try:
        yield src_col_id, marker
    finally:
        await direct_db.execute(
            _text("DELETE FROM payroll.statusratecolumns WHERE statusratecolumnid = :id"),
            {"id": src_col_id},
        )
        residue = (await direct_db.execute(
            _text(
                "SELECT COUNT(*) AS cnt FROM payroll.statusratecolumns WHERE statusratecolumnid = :id"
            ),
            {"id": src_col_id},
        )).mappings().first()
        assert residue["cnt"] == 0, (
            f"Forced test-owned StatusRateColumns row {src_col_id} ({marker}) was not removed"
        )


async def _insert_status_key(
    direct_db,
    company_id: int,
    branch_id: int,
    code: str,
    hours: str,
    status_rate_column_id: int | None,
    allowance_category: str | None = None,
) -> int:
    row = (await direct_db.execute(
        _text("""
            INSERT INTO payroll.payrollstatuskeys
                (companyid, branchid, statuscode, normalizedstatuscode, keyname,
                 isoffreason, hoursvalue, isactive, displayorder, statusratecolumnid,
                 allowancecategory)
            VALUES (:cid, :bid, :code, :norm, :name, FALSE, :hours, TRUE, 99, :src_col, :alcat)
            RETURNING statuskeyid
        """),
        {
            "cid": company_id, "bid": branch_id,
            "code": code, "norm": code.upper(), "name": f"P4S2 {code}",
            "hours": Decimal(hours), "src_col": status_rate_column_id,
            "alcat": allowance_category,
        },
    )).mappings().first()
    return row["statuskeyid"]


async def _delete_status_key(direct_db, status_key_id: int) -> None:
    await direct_db.execute(
        _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
        {"id": status_key_id},
    )


@contextlib.asynccontextmanager
async def _owned_status_key(
    direct_db,
    company_id: int,
    branch_id: int,
    code: str,
    hours: str,
    status_rate_column_id: int | None,
    allowance_category: str | None = None,
):
    """
    Exception-safe StatusKey lifecycle: ownership begins the instant the
    INSERT returns an ID (the `try` wraps the row immediately after
    creation, not after several other resources have already been created
    unprotected) -- a later failure (another resource's creation, or an
    assertion) still triggers this `finally` and removes exactly this row.
    """
    status_key_id = await _insert_status_key(
        direct_db, company_id, branch_id, code, hours, status_rate_column_id,
        allowance_category=allowance_category,
    )
    try:
        yield status_key_id
    finally:
        await _delete_status_key(direct_db, status_key_id)
        residue = (await direct_db.execute(
            _text("SELECT COUNT(*) AS cnt FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
            {"id": status_key_id},
        )).mappings().first()
        assert residue["cnt"] == 0, f"StatusKey {status_key_id} was not removed"


async def _insert_status_pay_rate(
    direct_db,
    company_id: int,
    branch_id: int,
    driver_id: int,
    rate_type_id: int,
    amount: str,
    effective_from: str,
    status: str = "Approved",
    effective_to: str | None = None,
) -> int:
    row = (await direct_db.execute(
        _text("""
            INSERT INTO payroll.driverrates
                (companyid, branchid, driverid, ratetypeid, amount, status, effectivefrom, effectiveto)
            VALUES (:cid, :bid, :did, :rtid, :amt, :status, :eff, :eff_to)
            RETURNING driverrateid
        """),
        {
            "cid": company_id, "bid": branch_id, "did": driver_id, "rtid": rate_type_id,
            "amt": Decimal(amount), "status": status,
            "eff": datetime.date.fromisoformat(effective_from),
            "eff_to": datetime.date.fromisoformat(effective_to) if effective_to else None,
        },
    )).mappings().first()
    return row["driverrateid"]


async def _delete_driver_rate(direct_db, driver_rate_id: int) -> None:
    await direct_db.execute(
        _text("DELETE FROM payroll.driverrates WHERE driverrateid = :id"),
        {"id": driver_rate_id},
    )


@contextlib.asynccontextmanager
async def _owned_driver_rate(
    direct_db,
    company_id: int,
    branch_id: int,
    driver_id: int,
    rate_type_id: int,
    amount: str,
    effective_from: str,
    status: str = "Approved",
    effective_to: str | None = None,
):
    """Exception-safe DriverRate lifecycle -- same ownership guarantee as
    `_owned_status_key`: the `try` begins immediately after the INSERT
    returns an ID."""
    rate_id = await _insert_status_pay_rate(
        direct_db, company_id, branch_id, driver_id, rate_type_id,
        amount, effective_from, status=status, effective_to=effective_to,
    )
    try:
        yield rate_id
    finally:
        await _delete_driver_rate(direct_db, rate_id)
        residue = (await direct_db.execute(
            _text("SELECT COUNT(*) AS cnt FROM payroll.driverrates WHERE driverrateid = :id"),
            {"id": rate_id},
        )).mappings().first()
        assert residue["cnt"] == 0, f"DriverRate {rate_id} was not removed"


async def _save_day_grid(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    work_date: str,
    status_key_code: str | None,
) -> httpx.Response:
    return await client.post(
        f"/payroll/periods/{period_id}/day-grid",
        json={
            "work_date": work_date,
            "rows": [{"driver_id": driver_id, "status_key": status_key_code, "values": {}}],
        },
        headers=auth(token),
    )


async def _get_day_grid(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    work_date: str,
) -> httpx.Response:
    return await client.get(
        f"/payroll/periods/{period_id}/day-grid",
        params={"work_date": work_date},
        headers=auth(token),
    )


async def _get_entry_state_row(direct_db, period_id: int, driver_id: int, work_date: str) -> dict | None:
    row = (await direct_db.execute(
        _text("""
            SELECT payrollperioddriverdayentrystateid, statuskeyid, isvoided,
                   companyid, branchid, payrollperiodid, driverid, workdate
            FROM   payroll.payrollperioddriverdayentrystate
            WHERE  payrollperiodid = :pid AND driverid = :did AND workdate = :wd
        """),
        {"pid": period_id, "did": driver_id, "wd": datetime.date.fromisoformat(work_date)},
    )).mappings().first()
    return dict(row) if row else None


async def _get_status_pay_lines(direct_db, period_id: int, driver_id: int | None = None) -> list[dict]:
    query = """
        SELECT draftlineid, driverid, workdate, linetype, quantity,
               calculatedamount, sourcetype, sourceid, status, needsmanagerreview
        FROM   payroll.payrolldraftlines
        WHERE  payrollperiodid = :pid
          AND  sourceid LIKE 'STATUS_PAYMENT:%'
    """
    params = {"pid": period_id}
    if driver_id is not None:
        query += " AND driverid = :did"
        params["did"] = driver_id
    rows = (await direct_db.execute(_text(query), params)).mappings().all()
    return [dict(r) for r in rows]


async def _advance_to_approved(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
) -> None:
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


async def _delete_period_and_children(
    direct_db,
    period_id: int,
    *,
    was_locked: bool = False,
) -> None:
    """
    Remove mutable test state for one period without bypassing immutable
    financial history. Snapshot-backed finalized periods cannot be deleted by
    design; their exact PPDES/review rows are removed and the test period is
    cancelled so it cannot affect later Status or pay-rule scenarios.
    """
    draft_line_ids = [
        r["draftlineid"] for r in (await direct_db.execute(
            _text("SELECT draftlineid FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid"),
            {"pid": period_id},
        )).mappings().all()
    ]
    review_item_ids = [
        r["reviewitemid"] for r in (await direct_db.execute(
            _text(
                "SELECT reviewitemid FROM review.managerreviewitems "
                "WHERE entityname = 'PayrollPeriods' AND entityid = :eid"
            ),
            {"eid": str(period_id)},
        )).mappings().all()
    ]
    ppdes_ids = [
        r["payrollperioddriverdayentrystateid"] for r in (await direct_db.execute(
            _text(
                "SELECT payrollperioddriverdayentrystateid FROM payroll.payrollperioddriverdayentrystate "
                "WHERE payrollperiodid = :pid"
            ),
            {"pid": period_id},
        )).mappings().all()
    ]

    snapshot_exists = (await direct_db.execute(
        _text("""
            SELECT EXISTS(
                SELECT 1
                FROM payroll.payrollcalculationsnapshots
                WHERE payrollperiodid = :pid
            )
        """),
        {"pid": period_id},
    )).scalar_one()

    if snapshot_exists:
        snapshot_status = (await direct_db.execute(
            _text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": period_id},
        )).scalar_one()
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
            await direct_db.execute(
                _text(
                    "DELETE FROM audit.auditlog "
                    "WHERE entityname = 'ManagerReviewItems' AND entityid = ANY(:ids)"
                ),
                {"ids": [str(i) for i in review_item_ids]},
            )
            await direct_db.execute(
                _text("DELETE FROM review.managerreviewdecisions WHERE reviewitemid = ANY(:ids)"),
                {"ids": review_item_ids},
            )
            await direct_db.execute(
                _text("DELETE FROM review.managerreviewitems WHERE reviewitemid = ANY(:ids)"),
                {"ids": review_item_ids},
            )
        await direct_db.execute(
            _text(
                "DELETE FROM payroll.payrollperioddriverdayentrystate "
                "WHERE payrollperiodid = :pid"
            ),
            {"pid": period_id},
        )
        if snapshot_status not in ("Locked", "Archived"):
            await direct_db.execute(
                _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' WHERE payrollperiodid = :pid"),
                {"pid": period_id},
            )
        residue = (await direct_db.execute(
            _text("""
                SELECT
                    (SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid) AS status,
                    (SELECT COUNT(*) FROM payroll.payrollperioddriverdayentrystate
                        WHERE payrollperiodid = :pid) AS ppdes,
                    (SELECT COUNT(*) FROM review.managerreviewitems
                        WHERE reviewitemid = ANY(:review_ids)) AS review_items
            """),
            {"pid": period_id, "review_ids": review_item_ids or [-1]},
        )).mappings().one()
        assert residue["status"] in ("Cancelled", "Locked", "Archived")
        assert residue["ppdes"] == 0
        assert residue["review_items"] == 0
        return

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
        await direct_db.execute(
                _text(
                    "DELETE FROM audit.auditlog "
                    "WHERE entityname = 'ManagerReviewItems' AND entityid = ANY(:ids)"
                ),
                {"ids": [str(i) for i in review_item_ids]},
        )
        await direct_db.execute(
                _text("DELETE FROM review.managerreviewdecisions WHERE reviewitemid = ANY(:ids)"),
                {"ids": review_item_ids},
        )
        await direct_db.execute(
                _text("DELETE FROM review.managerreviewitems WHERE reviewitemid = ANY(:ids)"),
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
    # Deleting the period cascades PayrollPeriodDriverDayEntryState rows
    # (fk_PPDES_Period ON DELETE CASCADE).
    await direct_db.execute(
            _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": period_id},
    )

    residue = (await direct_db.execute(
        _text("""
            SELECT
                (SELECT COUNT(*) FROM payroll.payrollperiods    WHERE payrollperiodid = :pid) AS periods,
                (SELECT COUNT(*) FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid) AS draftlines,
                (SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :pid) AS finallines,
                (SELECT COUNT(*) FROM payroll.payrollperioddriverdayentrystate
                    WHERE payrollperiodid = :pid) AS ppdes,
                (SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityname = 'PayrollPeriods' AND entityid = :eid) AS period_audit,
                (SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityname = 'PayrollDraftLines' AND entityid = ANY(:dlids)) AS draftline_audit
        """),
        {"pid": period_id, "eid": str(period_id), "dlids": [str(i) for i in draft_line_ids]},
    )).mappings().first()
    assert (
        residue["periods"] == 0 and residue["draftlines"] == 0 and residue["finallines"] == 0
        and residue["ppdes"] == 0 and residue["period_audit"] == 0 and residue["draftline_audit"] == 0
    ), f"Residue check failed for period {period_id}: {dict(residue)}"

    if ppdes_ids:
        # Explicit confirmation the cascade actually ran, not merely that the
        # count-by-period-id query above returned zero for another reason.
        leftover = (await direct_db.execute(
            _text(
                "SELECT COUNT(*) AS cnt FROM payroll.payrollperioddriverdayentrystate "
                "WHERE payrollperioddriverdayentrystateid = ANY(:ids)"
            ),
            {"ids": ppdes_ids},
        )).mappings().first()
        assert leftover["cnt"] == 0, f"PPDES cascade did not remove rows {ppdes_ids}"

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
            review_residue["items"] == 0 and review_residue["decisions"] == 0
            and review_residue["review_audit"] == 0
        ), f"Review-domain residue check failed for {review_item_ids}: {dict(review_residue)}"


@contextlib.asynccontextmanager
async def _owned_period(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    start: str,
    end: str,
    db,
    suffix: str = "",
):
    """
    Exception-safe period lifecycle: ownership begins immediately after
    `_open_period` returns a `payroll_period_id`. Auto-detects whether the
    period reached Locked/Archived status by the time cleanup runs (e.g. a
    test that finalizes inside the block), so callers never need to track
    `was_locked` manually -- `_delete_period_and_children` is always called
    with the correct value.
    """
    pid = await _open_period(client, token, branch_id, start, end, db, suffix=suffix)
    try:
        yield pid
    finally:
        status_row = (await db.execute(
            _text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )).mappings().first()
        was_locked = status_row is not None and status_row["status"] in ("Locked", "Archived")
        await _delete_period_and_children(db, pid, was_locked=was_locked)


@contextlib.asynccontextmanager
async def _owned_driver_pay_rule(
    session_client: httpx.AsyncClient,
    auth_token: str,
    direct_db,
    *,
    driver_id: int,
    branch_id: int,
    rule_type: str,
    amount: str,
    effective_from: str,
    effective_to: str,
):
    """
    Exception-safe DriverPayRule lifecycle (P1 fix — Codex FAIL verdict:
    ownership must begin before response parsing, not depend on it).

    Ownership evidence does NOT rely solely on `resp.json()["driver_pay_rule_id"]`
    -- a successful 201 followed by a JSON-parsing failure, a missing-key
    failure, or a transport failure that arrives after the server already
    committed the row must still be cleaned up. Two independent, schema-real
    ownership signals are snapshotted BEFORE the create request is sent, and
    the `try` begins before that request too:
      1. The exact (DriverID, RuleType) row-ID set for this test's own driver
         (each test uses a freshly-created driver, so this set is inherently
         test-scoped -- no other test/process can add a row to it).
      2. A unique `P4S2-DPR-<hex>` marker written into `DriverPayRules.Notes`
         (a real, schema-supported column -- confirmed by reading
         `DriverPayRuleCreate`/`create_driver_pay_rule` in schemas.py/service.py,
         not assumed) plus an `audit.AuditLog.AuditID` watermark, both taken
         before the request.

    On exit (finally), rows are identified by re-querying with this evidence
    (never by trusting a possibly-unset `rule_id` alone), then for each:
      1. Void is attempted via the real product endpoint (characterizes
         supported behavior) -- wrapped independently; a transport failure or
         422 (e.g. already Voided) here must never block hard cleanup below.
      2. Hard-delete of the exact row(s) and their AuditLog rows.
    Every independent cleanup layer (Void attempt, audit delete, rule delete)
    keeps running even if a prior layer raised -- errors are collected, not
    silently swallowed, and re-raised together at the end. Final assertions
    confirm both the rule-ID set and the audit-ID-above-watermark count are
    back to the pre-test baseline (no Active/Voided/historical residue).
    """
    headers = auth(auth_token)
    marker = f"P4S2-DPR-{uuid.uuid4().hex[:8]}"

    pre_rule_ids = {
        r["driverpayruleid"] for r in (await direct_db.execute(
            _text(
                "SELECT driverpayruleid FROM payroll.driverpayrules "
                "WHERE driverid = :did AND ruletype = :rtype"
            ),
            {"did": driver_id, "rtype": rule_type},
        )).mappings().all()
    }
    audit_watermark = (await direct_db.execute(
        _text("SELECT COALESCE(MAX(auditid), 0) AS wm FROM audit.auditlog"),
    )).mappings().first()["wm"]

    rule_id = None
    try:
        resp = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id": driver_id, "branch_id": branch_id,
                "rule_type": rule_type, "amount": amount,
                "effective_from": effective_from, "effective_to": effective_to,
                "notes": marker,
            },
            headers=headers,
        )
        assert resp.status_code == 201, f"Pay rule creation failed: {resp.text}"
        # Ownership already began above (pre-request snapshot) -- a failure
        # parsing this response body no longer risks an unidentified row.
        rule_id = resp.json()["driver_pay_rule_id"]
        yield rule_id
    finally:
        post_rows = (await direct_db.execute(
            _text(
                "SELECT driverpayruleid FROM payroll.driverpayrules "
                "WHERE driverid = :did AND ruletype = :rtype"
            ),
            {"did": driver_id, "rtype": rule_type},
        )).mappings().all()
        owned_ids = {r["driverpayruleid"] for r in post_rows} - pre_rule_ids
        marker_rows = (await direct_db.execute(
            _text("SELECT driverpayruleid FROM payroll.driverpayrules WHERE notes = :marker"),
            {"marker": marker},
        )).mappings().all()
        owned_ids |= {r["driverpayruleid"] for r in marker_rows}
        if rule_id is not None:
            owned_ids.add(rule_id)

        cleanup_errors: list[Exception] = []

        for rid in owned_ids:
            try:
                await session_client.post(
                    f"/payroll/driver-pay-rules/{rid}/void",
                    json={"reason": "P4S2 test cleanup"},
                    headers=headers,
                )
            except Exception as e:  # noqa: BLE001 -- Void is best-effort; hard cleanup below is authoritative.
                cleanup_errors.append(e)

        for rid in owned_ids:
            try:
                await direct_db.execute(
                    _text("DELETE FROM audit.auditlog WHERE entityname = 'DriverPayRules' AND entityid = :eid"),
                    {"eid": str(rid)},
                )
            except Exception as e:
                cleanup_errors.append(e)
            try:
                await direct_db.execute(
                    _text("DELETE FROM payroll.driverpayrules WHERE driverpayruleid = :id"),
                    {"id": rid},
                )
            except Exception as e:
                cleanup_errors.append(e)

        if cleanup_errors:
            raise ExceptionGroup("DriverPayRule cleanup layer failure(s)", cleanup_errors)

        final_rule_ids = {
            r["driverpayruleid"] for r in (await direct_db.execute(
                _text(
                    "SELECT driverpayruleid FROM payroll.driverpayrules "
                    "WHERE driverid = :did AND ruletype = :rtype"
                ),
                {"did": driver_id, "rtype": rule_type},
            )).mappings().all()
        }
        assert final_rule_ids == pre_rule_ids, (
            f"DriverPayRule scope residue for driver={driver_id} rule_type={rule_type}: "
            f"expected {pre_rule_ids}, got {final_rule_ids} -- "
            f"no Active, Voided, or historical test-owned row may remain"
        )
        final_audit_count = (await direct_db.execute(
            _text(
                "SELECT COUNT(*) AS cnt FROM audit.auditlog "
                "WHERE entityname = 'DriverPayRules' AND auditid > :wm"
            ),
            {"wm": audit_watermark},
        )).mappings().first()["cnt"]
        assert final_audit_count == 0, (
            f"DriverPayRule audit residue: {final_audit_count} row(s) remain above "
            f"watermark {audit_watermark}"
        )


@contextlib.asynccontextmanager
async def _status_pay_scenario(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
    *,
    period_start: str,
    period_end: str,
    driver_name: str,
    status_code: str,
    hours: str,
    rate_amount: str | None,
    rate_effective_from: str | None,
    allowance_category: str | None = None,
    period_suffix: str = "",
):
    """
    Shared setup/teardown: fresh driver + Open period + a StatusKey wired to
    the branch's default (or test-owned fallback) StatusRateColumn +
    (optionally) an Approved DriverRate for STATUS_PAY. Yields
    (driver_id, pid, status_key_id).

    Exception safety (P1 fix): every resource is wrapped in its own owning
    context (`_owned_status_key`, `_owned_driver_rate`, `_owned_period`)
    composed via `AsyncExitStack`, so ownership of each resource begins the
    instant it is created -- a later resource's creation failing (or a
    later assertion failing) still unwinds and cleans up everything created
    so far, in reverse order. `_status_rate_column` remains the OUTERMOST
    context so its fallback-row cleanup (if any) only runs after every
    dependent StatusKey has already been removed (FK RESTRICT).
    """
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
    driver_id = await _create_driver(session_client, auth_token, paytest_branch_id, driver_name)
    async with _status_rate_column(direct_db, 1, paytest_branch_id) as src_col_id:
        async with contextlib.AsyncExitStack() as stack:
            status_key_id = await stack.enter_async_context(
                _owned_status_key(
                    direct_db, 1, paytest_branch_id, status_code, hours, src_col_id,
                    allowance_category=allowance_category,
                )
            )
            if rate_amount is not None:
                status_pay_rt_id = await _get_status_pay_rate_type_id(session_client, auth_token)
                await stack.enter_async_context(
                    _owned_driver_rate(
                        direct_db, 1, paytest_branch_id, driver_id, status_pay_rt_id,
                        rate_amount, rate_effective_from,
                    )
                )
            pid = await stack.enter_async_context(
                _owned_period(
                    session_client, auth_token, paytest_branch_id,
                    period_start, period_end, direct_db, suffix=period_suffix,
                )
            )
            yield driver_id, pid, status_key_id
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)


# ---------------------------------------------------------------------------
# 1. Canonical Status source
# ---------------------------------------------------------------------------

class TestCanonicalStatusSource:
    """
    Direct proof: the canonical selected Status source is
    `PayrollPeriodDriverDayEntryState.StatusKeyID`
    (migrations/sql/0054_canonical_daily_entry_state.sql), written via
    `_upsert_entry_state` from the real `POST /periods/{id}/day-grid`
    endpoint (service.py `save_day_grid`). A `DailyStatus` DraftLine is never
    accepted as proof of canonical persistence in this class.
    """

    @pytest.mark.asyncio
    async def test_selection_stored_in_entry_state_statuskeyid(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        async with _status_pay_scenario(
            session_client, auth_token, paytest_branch_id, direct_db,
            period_start=PERIOD_A_START, period_end=PERIOD_A_END,
            driver_name="P4S2 Canonical A", status_code=f"P4S2CANON{id(self)}",
            hours="8.00", rate_amount=None, rate_effective_from=None,
        ) as (driver_id, pid, status_key_id):
            resp = await _save_day_grid(
                session_client, auth_token, pid, driver_id, DATE_FEB02,
                status_key_code=None,
            )
            # Re-fetch the exact status_code (server-generated, unknown to us
            # until insert) to drive the save.
            code_row = (await direct_db.execute(
                _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                {"id": status_key_id},
            )).mappings().first()
            status_code = code_row["statuscode"]

            resp = await _save_day_grid(
                session_client, auth_token, pid, driver_id, DATE_FEB02,
                status_key_code=status_code,
            )
            assert resp.status_code == 200, f"Day grid save failed: {resp.text}"

            row = await _get_entry_state_row(direct_db, pid, driver_id, DATE_FEB02)
            assert row is not None, "PayrollPeriodDriverDayEntryState row must exist after save"
            assert row["statuskeyid"] == status_key_id, (
                f"Canonical StatusKeyID must be {status_key_id}; got {row['statuskeyid']}"
            )
            assert row["isvoided"] is False

    @pytest.mark.asyncio
    async def test_replacing_statuskey_updates_canonical_row_not_a_new_one(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        """One driver/day has ONE canonical row — replacing the StatusKey
        updates that same row (enforced by `uq_PPDES_Period_Driver_Date`
        + the ON CONFLICT DO UPDATE in `_upsert_entry_state`), it does not
        create a second competing row. Canonical-side only — the
        DailyStatus compatibility PROJECTION's own write-side refresh is
        proven separately by
        `TestDailyStatusProjectionNotSourceOfTruth::test_write_side_projection_refreshes_on_statuskey_replacement`
        below, which is a distinct contract from this canonical-row proof."""
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        driver_id = await _create_driver(session_client, auth_token, paytest_branch_id, "P4S2 Replace")
        async with _status_rate_column(direct_db, 1, paytest_branch_id) as src_col_id:
            async with contextlib.AsyncExitStack() as stack:
                key_a = await stack.enter_async_context(
                    _owned_status_key(direct_db, 1, paytest_branch_id, f"P4S2REPA{id(self)}", "8.00", src_col_id)
                )
                key_b = await stack.enter_async_context(
                    _owned_status_key(direct_db, 1, paytest_branch_id, f"P4S2REPB{id(self)}", "4.00", src_col_id)
                )
                pid = await stack.enter_async_context(
                    _owned_period(
                        session_client, auth_token, paytest_branch_id,
                        PERIOD_A_START, PERIOD_A_END, direct_db, suffix="-rep",
                    )
                )
                code_a = (await direct_db.execute(
                    _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                    {"id": key_a},
                )).mappings().first()["statuscode"]
                code_b = (await direct_db.execute(
                    _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                    {"id": key_b},
                )).mappings().first()["statuscode"]

                r1 = await _save_day_grid(session_client, auth_token, pid, driver_id, DATE_FEB02, code_a)
                assert r1.status_code == 200
                row1 = await _get_entry_state_row(direct_db, pid, driver_id, DATE_FEB02)
                assert row1["statuskeyid"] == key_a
                first_row_id = row1["payrollperioddriverdayentrystateid"]

                r2 = await _save_day_grid(session_client, auth_token, pid, driver_id, DATE_FEB02, code_b)
                assert r2.status_code == 200
                row2 = await _get_entry_state_row(direct_db, pid, driver_id, DATE_FEB02)
                assert row2["statuskeyid"] == key_b, "Canonical row must now reflect key_b"
                assert row2["payrollperioddriverdayentrystateid"] == first_row_id, (
                    "Replacing the StatusKey must update the SAME canonical row "
                    "(one row per driver/day), not create a second one"
                )

                count_row = (await direct_db.execute(
                    _text(
                        "SELECT COUNT(*) AS cnt FROM payroll.payrollperioddriverdayentrystate "
                        "WHERE payrollperiodid = :pid AND driverid = :did AND workdate = :wd"
                    ),
                    {"pid": pid, "did": driver_id, "wd": datetime.date.fromisoformat(DATE_FEB02)},
                )).mappings().first()
                assert count_row["cnt"] == 1, "Exactly one canonical row must exist for this driver/day"
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)

    @pytest.mark.asyncio
    async def test_clearing_status_sets_statuskeyid_null_and_voids(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        """The current API supports fully clearing the canonical selection
        (status_key=None) — asserts the actual current behavior
        (`StatusKeyID` set to NULL, `IsVoided` set per current logic), not
        an invented clearing semantic."""
        async with _status_pay_scenario(
            session_client, auth_token, paytest_branch_id, direct_db,
            period_start=PERIOD_A_START, period_end=PERIOD_A_END,
            driver_name="P4S2 Clear", status_code=f"P4S2CLR{id(self)}",
            hours="8.00", rate_amount=None, rate_effective_from=None,
            period_suffix="-clr",
        ) as (driver_id, pid, status_key_id):
            code_row = (await direct_db.execute(
                _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                {"id": status_key_id},
            )).mappings().first()
            status_code = code_row["statuscode"]

            r1 = await _save_day_grid(session_client, auth_token, pid, driver_id, DATE_FEB02, status_code)
            assert r1.status_code == 200
            row1 = await _get_entry_state_row(direct_db, pid, driver_id, DATE_FEB02)
            assert row1["statuskeyid"] == status_key_id
            assert row1["isvoided"] is False

            r2 = await _save_day_grid(session_client, auth_token, pid, driver_id, DATE_FEB02, None)
            assert r2.status_code == 200
            row2 = await _get_entry_state_row(direct_db, pid, driver_id, DATE_FEB02)
            assert row2["statuskeyid"] is None, "Clearing must set StatusKeyID to NULL"
            assert row2["isvoided"] is True, (
                "Current behavior: clearing status (with no note) sets IsVoided=TRUE"
            )

    @pytest.mark.asyncio
    async def test_scope_isolation_one_drivers_status_does_not_affect_another(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        driver_1 = await _create_driver(session_client, auth_token, paytest_branch_id, "P4S2 Scope D1")
        driver_2 = await _create_driver(session_client, auth_token, paytest_branch_id, "P4S2 Scope D2")
        async with _status_rate_column(direct_db, 1, paytest_branch_id) as src_col_id:
            async with contextlib.AsyncExitStack() as stack:
                key_id = await stack.enter_async_context(
                    _owned_status_key(direct_db, 1, paytest_branch_id, f"P4S2SCOPE{id(self)}", "8.00", src_col_id)
                )
                pid = await stack.enter_async_context(
                    _owned_period(
                        session_client, auth_token, paytest_branch_id,
                        PERIOD_A_START, PERIOD_A_END, direct_db, suffix="-scope",
                    )
                )
                code_row = (await direct_db.execute(
                    _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                    {"id": key_id},
                )).mappings().first()
                status_code = code_row["statuscode"]

                r1 = await _save_day_grid(session_client, auth_token, pid, driver_1, DATE_FEB03, status_code)
                assert r1.status_code == 200

                row_1 = await _get_entry_state_row(direct_db, pid, driver_1, DATE_FEB03)
                row_2 = await _get_entry_state_row(direct_db, pid, driver_2, DATE_FEB03)
                assert row_1 is not None and row_1["statuskeyid"] == key_id
                assert row_2 is None, "Driver 2 must have no canonical row from driver 1's save"
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)


# ---------------------------------------------------------------------------
# 2. DailyStatus compatibility projection (not source of truth)
# ---------------------------------------------------------------------------

class TestDailyStatusProjectionNotSourceOfTruth:
    """
    `DailyStatus` DraftLines are a compatibility projection/output only.
    This class proves two DISTINCT contracts, deliberately kept as separate
    tests so neither is mistaken for the other:

    1. `test_write_side_projection_refreshes_on_statuskey_replacement` —
       the WRITE side: the supported `day-grid` save flow itself refreshes
       the DailyStatus projection to reflect a replacement StatusKey, in the
       same request/transaction as the canonical entry-state write. No
       separate refresh/repair endpoint is invoked.

    2. `test_canonical_row_wins_over_corrupted_dailystatus_projection` —
       the READ side: `get_day_grid`'s canonical-first precedence means a
       (deliberately, DB-only) corrupted DailyStatus projection is ignored
       on read once a canonical row exists — legacy fallback applies ONLY
       when no canonical row exists at all.
    """

    @pytest.mark.asyncio
    async def test_write_side_projection_refreshes_on_statuskey_replacement(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        """
        P1 write-side proof (Codex fix-forward): confirmed by reading
        `save_day_grid` (service.py ~13255-13353) that a StatusKey
        replacement UPDATES the existing DailyStatus DraftLine IN PLACE
        (same DraftLineID, `notes` column rewritten to the new status code)
        — it does not void the old line and insert a new one; that
        void+replace path is reserved for CLEARING status (status_key=None),
        not replacing one key with another. This test proves that exact
        current behavior end-to-end through the real save flow, reading the
        database immediately after the second save with no separate refresh
        endpoint, `_refresh_status_payment_lines` call, or period
        resubmission in between.
        """
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        driver_id = await _create_driver(session_client, auth_token, paytest_branch_id, "P4S2 WriteRefresh")
        async with _status_rate_column(direct_db, 1, paytest_branch_id) as src_col_id:
            async with contextlib.AsyncExitStack() as stack:
                key_a = await stack.enter_async_context(
                    _owned_status_key(direct_db, 1, paytest_branch_id, f"P4S2WRA{id(self)}", "8.00", src_col_id)
                )
                key_b = await stack.enter_async_context(
                    _owned_status_key(direct_db, 1, paytest_branch_id, f"P4S2WRB{id(self)}", "4.00", src_col_id)
                )
                pid = await stack.enter_async_context(
                    _owned_period(
                        session_client, auth_token, paytest_branch_id,
                        PERIOD_A_START, PERIOD_A_END, direct_db, suffix="-writerefresh",
                    )
                )
                code_a = (await direct_db.execute(
                    _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                    {"id": key_a},
                )).mappings().first()["statuscode"]
                code_b = (await direct_db.execute(
                    _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                    {"id": key_b},
                )).mappings().first()["statuscode"]

                # 1. Save Status A through the supported day-grid endpoint.
                r1 = await _save_day_grid(session_client, auth_token, pid, driver_id, DATE_FEB02, code_a)
                assert r1.status_code == 200, f"Save Status A failed: {r1.text}"

                # 2. Capture the canonical row and the DailyStatus projection
                #    line's exact identity for Status A.
                canonical_a = await _get_entry_state_row(direct_db, pid, driver_id, DATE_FEB02)
                assert canonical_a is not None and canonical_a["statuskeyid"] == key_a
                daily_status_a = (await direct_db.execute(
                    _text("""
                        SELECT draftlineid, linetype, sourcetype, status, notes
                        FROM   payroll.payrolldraftlines
                        WHERE  payrollperiodid = :pid AND driverid = :did
                          AND  workdate = :wd AND linetype = 'DailyStatus'
                    """),
                    {"pid": pid, "did": driver_id, "wd": datetime.date.fromisoformat(DATE_FEB02)},
                )).mappings().first()
                assert daily_status_a is not None, "DailyStatus projection line must exist after save"
                assert daily_status_a["notes"] == code_a, "Projection notes must carry Status A's code"
                assert daily_status_a["status"] == "Active"
                assert daily_status_a["linetype"] == "DailyStatus"
                assert daily_status_a["sourcetype"] == "Manual"
                projection_line_id = daily_status_a["draftlineid"]

                # 3. Replace Status A with Status B through the SAME
                #    supported day-grid endpoint -- one request.
                r2 = await _save_day_grid(session_client, auth_token, pid, driver_id, DATE_FEB02, code_b)
                assert r2.status_code == 200, f"Save Status B failed: {r2.text}"

                # 4. Read the database immediately -- no separate refresh
                #    endpoint, no _refresh_status_payment_lines, no
                #    resubmission. "The supported save flow returns only
                #    after both canonical state and compatibility
                #    projection reflect the replacement Status."
                canonical_b = await _get_entry_state_row(direct_db, pid, driver_id, DATE_FEB02)
                assert canonical_b is not None
                assert canonical_b["statuskeyid"] == key_b, (
                    "Canonical entry-state must reflect Status B immediately after the save"
                )
                assert canonical_b["payrollperioddriverdayentrystateid"] == (
                    canonical_a["payrollperioddriverdayentrystateid"]
                ), "Same canonical row, updated in place -- not a new row"

                daily_status_b = (await direct_db.execute(
                    _text("""
                        SELECT draftlineid, linetype, sourcetype, status, notes
                        FROM   payroll.payrolldraftlines
                        WHERE  payrollperiodid = :pid AND driverid = :did
                          AND  workdate = :wd AND linetype = 'DailyStatus'
                    """),
                    {"pid": pid, "did": driver_id, "wd": datetime.date.fromisoformat(DATE_FEB02)},
                )).mappings().first()
                assert daily_status_b is not None, "DailyStatus projection line must still exist"
                assert daily_status_b["draftlineid"] == projection_line_id, (
                    "The projection is updated IN PLACE (same DraftLineID) on a StatusKey "
                    "replacement -- it is not voided and re-inserted (that path is reserved "
                    "for clearing status to None, not replacing one key with another)"
                )
                assert daily_status_b["notes"] == code_b, (
                    "Projection notes must now carry Status B's code, refreshed by the "
                    "same day-grid save call that updated the canonical row"
                )
                assert daily_status_b["status"] == "Active", (
                    "The projection remains Active (not voided) after a same-flow replacement"
                )

                # No active projection still represents Status A: exactly
                # one Active DailyStatus line exists for this driver/day,
                # and its content is Status B's, not Status A's.
                active_count = (await direct_db.execute(
                    _text("""
                        SELECT COUNT(*) AS cnt FROM payroll.payrolldraftlines
                        WHERE  payrollperiodid = :pid AND driverid = :did
                          AND  workdate = :wd AND linetype = 'DailyStatus' AND status = 'Active'
                    """),
                    {"pid": pid, "did": driver_id, "wd": datetime.date.fromisoformat(DATE_FEB02)},
                )).mappings().first()
                assert active_count["cnt"] == 1, (
                    f"Exactly one Active DailyStatus projection must exist for this "
                    f"driver/day; got {active_count['cnt']}"
                )
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)

    @pytest.mark.asyncio
    async def test_canonical_row_wins_over_corrupted_dailystatus_projection(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        """
        Confirmed via `get_day_grid`'s read-side precedence (service.py):
        once a canonical `PayrollPeriodDriverDayEntryState` row exists for a
        driver/day, the `DailyStatus` DraftLine's `notes` content is
        IGNORED entirely for read purposes — it is legacy fallback ONLY
        when no canonical row exists. This test proves that precedence
        directly: it corrupts the legacy DailyStatus DraftLine's `notes` to
        a wrong value via direct DB write (not a supported API — there is
        no "edit DailyStatus notes directly to an arbitrary string"
        endpoint; this is DB evidence of what the read path actually keys
        off, not a claim that this corruption is reachable through the
        API) and proves `GET .../day-grid` still returns the CANONICAL
        StatusKey's code, unaffected by the corrupted projection. This is a
        READ-precedence contract, distinct from the WRITE-refresh contract
        proven by `test_write_side_projection_refreshes_on_statuskey_replacement`
        above.
        """
        async with _status_pay_scenario(
            session_client, auth_token, paytest_branch_id, direct_db,
            period_start=PERIOD_A_START, period_end=PERIOD_A_END,
            driver_name="P4S2 Projection", status_code=f"P4S2PROJ{id(self)}",
            hours="8.00", rate_amount=None, rate_effective_from=None,
            period_suffix="-proj",
        ) as (driver_id, pid, status_key_id):
            code_row = (await direct_db.execute(
                _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                {"id": status_key_id},
            )).mappings().first()
            status_code = code_row["statuscode"]

            save_resp = await _save_day_grid(
                session_client, auth_token, pid, driver_id, DATE_FEB02, status_code,
            )
            assert save_resp.status_code == 200

            # Corrupt the legacy DailyStatus projection directly in the DB —
            # not through any supported endpoint.
            await direct_db.execute(
                _text("""
                    UPDATE payroll.payrolldraftlines
                    SET notes = 'CORRUPTED_NOT_A_REAL_CODE'
                    WHERE payrollperiodid = :pid AND driverid = :did
                      AND workdate = :wd AND linetype = 'DailyStatus'
                """),
                {"pid": pid, "did": driver_id, "wd": datetime.date.fromisoformat(DATE_FEB02)},
            )

            grid_resp = await _get_day_grid(session_client, auth_token, pid, DATE_FEB02)
            assert grid_resp.status_code == 200, f"Get day grid failed: {grid_resp.text}"
            row = next(
                (r for r in grid_resp.json()["rows"] if r["driver_id"] == driver_id), None,
            )
            assert row is not None
            assert row["status_key"] == status_code, (
                "The read path must reflect the CANONICAL PayrollPeriodDriverDayEntryState "
                "row, not the corrupted DailyStatus projection — DailyStatus is a "
                "compatibility projection, never source truth."
            )


# ---------------------------------------------------------------------------
# 3. STATUS_PAYMENT formula characterization
# ---------------------------------------------------------------------------

class TestStatusPaymentFormula:
    """
    Formula confirmed by reading `_sync_status_payment_for_entry_state`
    (service.py): `calc_amount = (hours_value * resolved_rate).quantize(Decimal("0.0001"))`
    — quantized exactly once, no explicit rounding mode passed (ambient
    default ROUND_HALF_EVEN, same as PerUnit in Slice 1), guarded by
    `if (resolved_rate is not None and hours_value)` — a TRUTHINESS check,
    not an `is not None` check, so `hours_value == Decimal("0")` produces
    `calc_amount = None` even with a resolved rate.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "case_name,hours,rate,expected",
        [
            # Retained 4th decimal digit EVEN (6) -> ROUND_HALF_EVEN stays.
            # 3.25 x 2.0002 = 6.500650 exact.
            pytest.param("even_digit_stays", "3.25", "2.0002", "6.5006", id="even_digit_stays"),
            # Retained 4th decimal digit ODD (5) -> ROUND_HALF_EVEN rounds up.
            # 2.75 x 2.0002 = 5.500550 exact.
            pytest.param("odd_digit_rounds_up", "2.75", "2.0002", "5.5006", id="odd_digit_rounds_up"),
        ],
    )
    async def test_round_half_even_boundary(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
        case_name: str, hours: str, rate: str, expected: str,
    ):
        async with _status_pay_scenario(
            session_client, auth_token, paytest_branch_id, direct_db,
            period_start=PERIOD_A_START, period_end=PERIOD_A_END,
            driver_name=f"P4S2 Formula {case_name}", status_code=f"P4S2F{case_name[:6]}{id(self)}",
            hours=hours, rate_amount=rate, rate_effective_from=DATE_FEB02,
            period_suffix=f"-{case_name}",
        ) as (driver_id, pid, status_key_id):
            code_row = (await direct_db.execute(
                _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                {"id": status_key_id},
            )).mappings().first()
            status_code = code_row["statuscode"]

            resp = await _save_day_grid(
                session_client, auth_token, pid, driver_id, DATE_FEB02, status_code,
            )
            assert resp.status_code == 200, f"Day grid save failed: {resp.text}"

            lines = await _get_status_pay_lines(direct_db, pid, driver_id)
            assert len(lines) == 1
            assert Decimal(str(lines[0]["calculatedamount"])) == Decimal(expected), (
                f"Expected {hours} x {rate} exact product, ROUND_HALF_EVEN -> {expected}; "
                f"got {lines[0]['calculatedamount']}"
            )
            assert lines[0]["needsmanagerreview"] is False

    @pytest.mark.asyncio
    async def test_zero_hours_value_currently_unreachable_via_valid_data(
        self, direct_db, paytest_branch_id: int,
    ):
        """
        The code's dispatch guard `if (resolved_rate is not None and
        hours_value)` (service.py `_sync_status_payment_for_entry_state`) is
        a TRUTHINESS check, not an `is not None` check -- `Decimal("0")` is
        falsy, so if a StatusKey ever had `HoursValue == 0` AND a resolved
        rate, the formula would currently produce `calculatedamount = NULL`
        rather than `Decimal("0.0000")`.

        This test characterizes that the scenario is NOT currently
        reachable through any valid StatusKey configuration: the DB trigger
        `trg_psk_src_branch` (migration 0056_status_rate_columns.sql)
        enforces `HoursValue > 0` whenever `StatusRateColumnID` is set,
        confirmed here directly against the real schema/trigger rather than
        assumed. Recorded as a genuine (currently dead) edge case in the
        Python guard, not exercised end-to-end because no valid data can
        reach it -- a gap for a future slice/decision, not a Slice 2 failure.
        """
        async with _status_rate_column(direct_db, 1, paytest_branch_id) as src_col_id:
            with pytest.raises(Exception) as exc_info:
                await direct_db.execute(
                    _text("""
                        INSERT INTO payroll.payrollstatuskeys
                            (companyid, branchid, statuscode, normalizedstatuscode, keyname,
                             isoffreason, hoursvalue, isactive, displayorder, statusratecolumnid)
                        VALUES (1, :bid, :code, :norm, :name, FALSE, 0, TRUE, 99, :src_col)
                    """),
                    {
                        "bid": paytest_branch_id, "code": f"P4S2ZEROBLOCKED{id(self)}",
                        "norm": f"P4S2ZEROBLOCKED{id(self)}".upper(), "name": "P4S2 zero-hours probe",
                        "src_col": src_col_id,
                    },
                )
            assert "HoursValue must be > 0" in str(exc_info.value), (
                f"Expected the trg_psk_src_branch CHECK to block HoursValue=0 with "
                f"StatusRateColumnID set; got a different error: {exc_info.value}"
            )


# ---------------------------------------------------------------------------
# 4. Status-rate resolution
# ---------------------------------------------------------------------------

class TestStatusRateResolution:
    """
    Rate resolution query (service.py `_sync_status_payment_for_entry_state`):
    `WHERE driverid=:did AND ratetypeid=:rtid AND companyid=:cid
       AND status IN ('Approved','Superseded')
       AND effectivefrom <= :dt AND (effectiveto IS NULL OR effectiveto >= :dt)
     ORDER BY effectivefrom DESC LIMIT 1`
    — no generic/cross-branch/cross-company fallback exists in this function.

    Note on "multiple candidate rates" (section 10.E): the DB's
    `excl_driverrates_no_date_overlap` exclusion constraint (confirmed by
    attempting it) prevents two DriverRate rows for the same
    (company, driver, ratetype) from ever having overlapping effective date
    ranges, REGARDLESS of Approved/Superseded status. So the resolution
    query's `ORDER BY effectivefrom DESC LIMIT 1` can never actually need to
    break a tie between two simultaneously-valid candidates for the same
    work date in practice -- at most one row's range ever covers a given
    date. The test below instead characterizes the real, reachable scenario:
    a superseded historical rate whose range has been closed (EffectiveTo
    set) is correctly excluded once a later rate's range begins.

    Note on branch isolation (section 10.F, P2 evidence gap -- deliberately
    NOT force-tested here): the resolution query above filters ONLY by
    `driverid`, `ratetypeid`, and `companyid` -- it has NO `BranchID`
    predicate at all (confirmed by reading the exact query, not assumed).
    Branch ownership is therefore entirely INDIRECT: `core.Drivers.BranchID`
    is `NOT NULL` and a driver permanently belongs to exactly one branch, so
    a query scoped to `driverid=:did` can never match another branch's
    DriverRate row for a DIFFERENT driver. A "meaningful direct cross-branch
    candidate" (per this fix-forward's instructions) is not constructible
    through valid schema state here: the only way to make a Branch-B rate
    row visible to this query would be to insert it with the Branch-A
    driver's own DriverID -- which is not "another driver's rate in a
    different branch" at all, just the same driver's rate, and proves
    nothing about branch isolation. Manufacturing a DriverRates row with a
    BranchID column value that mismatches the driver's actual branch would
    also prove nothing, since the query never reads DriverRates.BranchID.
    Existing tenant-isolation coverage for DriverRates generally lives in
    test_rate_calc_boundaries.py and the CP-2D1 canonical-row company
    scoping test (`test_e14_canonical_row_company_id_scoped` in
    test_cp2d_canonical_entry_state.py). This is left as a documented P2
    evidence gap, not fabricated.
    """

    @pytest.mark.asyncio
    async def test_superseded_rate_excluded_once_later_rate_effective(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        driver_id = await _create_driver(session_client, auth_token, paytest_branch_id, "P4S2 TieBreak")
        async with _status_rate_column(direct_db, 1, paytest_branch_id) as src_col_id:
            async with contextlib.AsyncExitStack() as stack:
                status_key_id = await stack.enter_async_context(
                    _owned_status_key(direct_db, 1, paytest_branch_id, f"P4S2TIE{id(self)}", "5.00", src_col_id)
                )
                status_pay_rt_id = await _get_status_pay_rate_type_id(session_client, auth_token)
                await stack.enter_async_context(
                    _owned_driver_rate(
                        direct_db, 1, paytest_branch_id, driver_id, status_pay_rt_id,
                        "10.0000", "2098-01-01", status="Superseded", effective_to="2098-01-31",
                    )
                )
                await stack.enter_async_context(
                    _owned_driver_rate(
                        direct_db, 1, paytest_branch_id, driver_id, status_pay_rt_id,
                        "30.0000", "2098-02-01", status="Approved",
                    )
                )
                pid = await stack.enter_async_context(
                    _owned_period(
                        session_client, auth_token, paytest_branch_id,
                        PERIOD_A_START, PERIOD_A_END, direct_db, suffix="-tiebreak",
                    )
                )
                code_row = (await direct_db.execute(
                    _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                    {"id": status_key_id},
                )).mappings().first()
                status_code = code_row["statuscode"]

                resp = await _save_day_grid(
                    session_client, auth_token, pid, driver_id, DATE_FEB02, status_code,
                )
                assert resp.status_code == 200, f"Day grid save failed: {resp.text}"

                lines = await _get_status_pay_lines(direct_db, pid, driver_id)
                assert len(lines) == 1
                # 5.00 x 30.0000 = 150.0000 (rate B, the later effectivefrom).
                assert Decimal(str(lines[0]["calculatedamount"])) == Decimal("150.0000"), (
                    f"Expected the most-recent-effectivefrom rate ($30) to win; "
                    f"got {lines[0]['calculatedamount']}"
                )
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)

    @pytest.mark.asyncio
    async def test_pending_approval_rate_not_resolved(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        """A PendingApproval DriverRate must NOT be used — the resolution
        query filters `status IN ('Approved', 'Superseded')` only."""
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        driver_id = await _create_driver(session_client, auth_token, paytest_branch_id, "P4S2 Pending")
        async with _status_rate_column(direct_db, 1, paytest_branch_id) as src_col_id:
            async with contextlib.AsyncExitStack() as stack:
                status_key_id = await stack.enter_async_context(
                    _owned_status_key(direct_db, 1, paytest_branch_id, f"P4S2PEND{id(self)}", "6.00", src_col_id)
                )
                status_pay_rt_id = await _get_status_pay_rate_type_id(session_client, auth_token)
                await stack.enter_async_context(
                    _owned_driver_rate(
                        direct_db, 1, paytest_branch_id, driver_id, status_pay_rt_id,
                        "99.0000", "2098-01-01", status="PendingApproval",
                    )
                )
                pid = await stack.enter_async_context(
                    _owned_period(
                        session_client, auth_token, paytest_branch_id,
                        PERIOD_A_START, PERIOD_A_END, direct_db, suffix="-pending",
                    )
                )
                code_row = (await direct_db.execute(
                    _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                    {"id": status_key_id},
                )).mappings().first()
                status_code = code_row["statuscode"]

                resp = await _save_day_grid(
                    session_client, auth_token, pid, driver_id, DATE_FEB02, status_code,
                )
                assert resp.status_code == 200, f"Day grid save failed: {resp.text}"

                lines = await _get_status_pay_lines(direct_db, pid, driver_id)
                assert len(lines) == 1
                assert lines[0]["calculatedamount"] is None, (
                    "A PendingApproval rate must not be resolved -> no calculated amount"
                )
                assert lines[0]["needsmanagerreview"] is True
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)


# ---------------------------------------------------------------------------
# 5. Preview vs. finalization parity for STATUS_PAY
# ---------------------------------------------------------------------------

class TestPreviewFinalizationDivergence:
    """
    `get_finalization_preview` (service.py) does NOT call
    `_refresh_status_payment_lines` — only the Open->InReview submit
    transition and `finalize_period` (Step 1.6a) do. This test proves a
    genuine, currently-reachable divergence: a driver-rate change made AFTER
    submit (while the period is Approved, before finalize) is NOT reflected
    by a subsequent finalization-preview read (stale, submit-time amount),
    but IS reflected by finalize's own final-lines result (fresh, re-synced
    amount) — exactly the kind of "preview vs finalization parity" gap this
    Slice is required to characterize, not fix.
    """

    @pytest.mark.asyncio
    async def test_rate_change_after_submit_diverges_preview_from_finalization(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        headers = auth(auth_token)
        period_start = "2099-02-02"
        period_end = "2099-02-08"
        work_date = "2099-02-02"
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        driver_id = await _create_driver(session_client, auth_token, paytest_branch_id, "P4S2 Divergence")
        async with _status_rate_column(direct_db, 1, paytest_branch_id) as src_col_id:
            async with contextlib.AsyncExitStack() as stack:
                status_key_id = await stack.enter_async_context(
                    _owned_status_key(direct_db, 1, paytest_branch_id, f"P4S2DIV{id(self)}", "8.00", src_col_id)
                )
                status_pay_rt_id = await _get_status_pay_rate_type_id(session_client, auth_token)
                rate_a_id = await stack.enter_async_context(
                    _owned_driver_rate(
                        direct_db, 1, paytest_branch_id, driver_id, status_pay_rt_id,
                        "20.0000", "2099-01-01",
                    )
                )
                pid = await stack.enter_async_context(
                    _owned_period(
                        session_client, auth_token, paytest_branch_id,
                        period_start, period_end, direct_db,
                        suffix=f"-divergence-{uuid.uuid4().hex[:8]}",
                    )
                )
                code_row = (await direct_db.execute(
                    _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                    {"id": status_key_id},
                )).mappings().first()
                status_code = code_row["statuscode"]

                save_resp = await _save_day_grid(
                    session_client, auth_token, pid, driver_id, work_date, status_code,
                )
                assert save_resp.status_code == 200

                # Capture the exact Status source identity BEFORE finalization
                # -- this is the strong selector used to locate the final line
                # later, not driver+amount (Codex P2 strengthening note).
                entry_state = await _get_entry_state_row(direct_db, pid, driver_id, work_date)
                assert entry_state is not None
                entry_state_id = entry_state["payrollperioddriverdayentrystateid"]

                lines_before = await _get_status_pay_lines(direct_db, pid, driver_id)
                assert len(lines_before) == 1
                draft_line_id = lines_before[0]["draftlineid"]
                expected_source_id = f"STATUS_PAYMENT:{entry_state_id}:{status_key_id}:{src_col_id}"
                assert lines_before[0]["sourceid"] == expected_source_id, (
                    f"Exact production SourceID format is "
                    f"'STATUS_PAYMENT:{{entry_state_id}}:{{status_key_id}}:{{status_rate_column_id}}' "
                    f"(service.py _sync_status_payment_for_entry_state); expected "
                    f"{expected_source_id!r}, got {lines_before[0]['sourceid']!r}"
                )
                assert lines_before[0]["linetype"] == "STATUS_PAY", (
                    "The persisted financial LineType is the resolved RateType code "
                    "(seeded as 'STATUS_PAY') -- NOT the literal string 'STATUS_PAYMENT', "
                    "which is only the SourceID prefix. These are deliberately not "
                    "interchangeable names (see TestStatusNotAPayItem's naming-"
                    "inconsistency note)."
                )
                assert lines_before[0]["sourcetype"] == "System"
                assert Decimal(str(lines_before[0]["calculatedamount"])) == Decimal("160.0000"), (
                    "Initial STATUS_PAY amount must be 8.00 x $20 = $160.0000"
                )

                # Submit (re-syncs status payment at Rate A -- unchanged so far).
                await _advance_to_approved(session_client, auth_token, pid)

                # Change the driver's STATUS_PAY rate AFTER submit: void Rate A,
                # approve Rate B ($30) at an earlier effective date so it's the
                # only Approved candidate.
                await direct_db.execute(
                    _text("UPDATE payroll.driverrates SET status = 'Voided' WHERE driverrateid = :id"),
                    {"id": rate_a_id},
                )
                # Entered into the SAME stack mid-flow: ownership begins the
                # instant this rate is created, even though it's created
                # partway through the test body -- a later assertion failure
                # still unwinds and removes it along with everything else.
                rate_b_id = await stack.enter_async_context(
                    _owned_driver_rate(
                        direct_db, 1, paytest_branch_id, driver_id, status_pay_rt_id,
                        "30.0000", "2099-01-01",
                    )
                )

                # Preview does NOT re-sync status payment -- must show the STALE
                # submit-time amount ($160), not the new rate's $240. Selected
                # by the exact draft_line_id captured above, not by amount.
                preview_resp = await session_client.get(
                    f"/payroll/periods/{pid}/finalization-preview", headers=headers,
                )
                assert preview_resp.status_code == 200, f"Preview failed: {preview_resp.text}"
                preview_line = next(
                    (l for l in preview_resp.json()["lines"] if l["draft_line_id"] == draft_line_id),
                    None,
                )
                assert preview_line is not None, "STATUS_PAY line not found in preview lines"
                assert Decimal(str(preview_line["calculated_amount"])) == Decimal("160.0000"), (
                    "Preview must show the STALE submit-time amount ($160) -- it does "
                    "not call _refresh_status_payment_lines, unlike submit/finalize"
                )

                # Finalize DOES re-sync status payment (Step 1.6a) -- must show
                # the FRESH amount using rate B ($240).
                fin_resp = await session_client.post(f"/payroll/periods/{pid}/finalize", headers=headers)
                assert fin_resp.status_code == 200, f"Finalize failed: {fin_resp.text}"

                fl_resp = await session_client.get(f"/payroll/periods/{pid}/final-lines", headers=headers)
                assert fl_resp.status_code == 200
                # Selected by the exact persisted source linkage
                # (PayrollFinalLines.DraftLineID == the same DraftLineID
                # captured before finalization) -- not by driver+amount, which
                # would not distinguish this line from another coincidentally
                # sharing the same driver and dollar amount.
                final_line = next(
                    (l for l in fl_resp.json() if l.get("draft_line_id") == draft_line_id),
                    None,
                )
                assert final_line is not None, (
                    f"STATUS_PAY final line not found by exact draft_line_id={draft_line_id}"
                )
                assert final_line["line_type"] == "STATUS_PAY"
                assert final_line["source_type"] == "System"
                assert Decimal(str(final_line["final_amount"])) == Decimal("240.0000"), (
                    "Finalization re-syncs status payment before locking -- must reflect "
                    "the fresh rate B (8.00 x $30 = $240.0000), diverging from the "
                    "stale preview value ($160.0000) read just before finalize. The "
                    "difference is caused by refresh timing (preview never re-syncs, "
                    "finalize always does), not by selecting a different line -- both "
                    "reads used the exact same draft_line_id identity."
                )
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)


# ---------------------------------------------------------------------------
# 6. Min/max participation (real end-to-end, not a synthetic injected line)
# ---------------------------------------------------------------------------

class TestMinMaxRealParticipation:
    """
    `_MINMAX_BASE_EXCLUDED_LINETYPES = ("BONUS", "SYS_MIN_TOPUP", "SYS_MAX_CAP")`
    (service.py) does not include the real STATUS_PAY linetype, so
    STATUS_PAY amounts ARE included in the normal min/max comparison base.
    The existing regression in test_cp3c_minmax_bonus.py
    (`test_status_payment_remains_in_minmax_base`) already asserts this
    conclusion, but does so using a SYNTHETIC injected DraftLine literally
    typed `linetype='STATUS_PAYMENT'` (via `_inject_status_payment_line` ->
    `_inject_adjustment_line`) -- a string the real
    `_sync_status_payment_for_entry_state` function never actually writes
    (it writes the resolved rate-type code, e.g. `STATUS_PAY`). This test
    closes that authenticity gap: it drives the REAL day-grid -> canonical
    entry-state -> STATUS_PAY sync flow end-to-end and proves the genuine
    STATUS_PAY line is what participates in min/max, not an injected stand-in.
    """

    @pytest.mark.asyncio
    async def test_real_status_pay_line_counts_toward_minimum_pay(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        async with _status_pay_scenario(
            session_client, auth_token, paytest_branch_id, direct_db,
            period_start=PERIOD_A_START, period_end=PERIOD_A_END,
            driver_name="P4S2 MinMax", status_code=f"P4S2MM{id(self)}",
            hours="9.00", rate_amount="20.0000", rate_effective_from=DATE_FEB02,
            period_suffix="-minmax",
        ) as (driver_id, pid, status_key_id):
            code_row = (await direct_db.execute(
                _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                {"id": status_key_id},
            )).mappings().first()
            status_code = code_row["statuscode"]

            save_resp = await _save_day_grid(
                session_client, auth_token, pid, driver_id, DATE_FEB02, status_code,
            )
            assert save_resp.status_code == 200
            lines = await _get_status_pay_lines(direct_db, pid, driver_id)
            assert Decimal(str(lines[0]["calculatedamount"])) == Decimal("180.0000"), (
                "9.00 x $20 = $180.0000 real STATUS_PAY line"
            )

            async with _owned_driver_pay_rule(
                session_client, auth_token, direct_db,
                driver_id=driver_id, branch_id=paytest_branch_id,
                rule_type="MinimumPay", amount="200.00",
                effective_from=PERIOD_A_START, effective_to=PERIOD_A_END,
            ) as rule_id:
                await _advance_to_approved(session_client, auth_token, pid)
                preview_resp = await session_client.get(
                    f"/payroll/periods/{pid}/finalization-preview", headers=auth(auth_token),
                )
                assert preview_resp.status_code == 200
                driver_totals = preview_resp.json()["driver_totals"]
                row = next((d for d in driver_totals if d["driver_id"] == driver_id), None)
                assert row is not None, "Driver not found in preview driver_totals"
                assert Decimal(str(row["gross_pay"])) == Decimal("180.0000"), (
                    "The real STATUS_PAY amount ($180) must count as normal gross pay"
                )
                sys_adjustments = preview_resp.json()["sys_adjustments"]
                adj_row = next((a for a in sys_adjustments if a["driver_id"] == driver_id), None)
                assert adj_row is not None, "Expected a min top-up sys_adjustment row"
                assert Decimal(str(adj_row["adjustment_amount"])) == Decimal("20.0000"), (
                    "Min top-up = $200 minimum - $180 real STATUS_PAY gross = $20.0000"
                )


# ---------------------------------------------------------------------------
# 7. PTO_STATUS prohibition
# ---------------------------------------------------------------------------

class TestPtoStatusProhibitionSlice2:
    """
    Evidence-precise, focused re-confirmation (existing broader coverage:
    test_cp2d2_status_payment.py::TestNoPTOStatusRegression). This class
    additionally proves PTO_STATUS cannot be reintroduced through THIS
    Slice's own supported entry path (day-grid save with a status_key code
    of 'PTO_STATUS', which does not exist as an active key). Evidence is
    real-database/API state in this test run only -- not a claim about
    production-wide absence.
    """

    @pytest.mark.asyncio
    async def test_pto_status_payitem_absent_from_local_test_schema(self, direct_db):
        row = (await direct_db.execute(
            _text("SELECT payitemid FROM payroll.payitems WHERE payitemcode LIKE '%PTO_STATUS%'"),
        )).mappings().first()
        assert row is None, (
            "No PayItem referencing PTO_STATUS exists in this test database "
            "(migration 0055_retire_pto_status.sql hard-deleted it)"
        )

    @pytest.mark.asyncio
    async def test_day_grid_rejects_pto_status_as_unknown_status_key(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        driver_id = await _create_driver(session_client, auth_token, paytest_branch_id, "P4S2 PTO Probe")
        async with _owned_period(
            session_client, auth_token, paytest_branch_id,
            PERIOD_A_START, PERIOD_A_END, direct_db, suffix="-pto",
        ) as pid:
            resp = await _save_day_grid(
                session_client, auth_token, pid, driver_id, DATE_FEB02, "PTO_STATUS",
            )
            assert resp.status_code == 422, (
                f"'PTO_STATUS' is not an active StatusKey code in this test database, "
                f"so the current API must reject it as unknown; got {resp.status_code}: {resp.text}"
            )
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)


# ---------------------------------------------------------------------------
# 8. Status is not a PayItem
# ---------------------------------------------------------------------------

class TestStatusNotAPayItem:
    """
    Confirms the current domain separation directly from schema/data:
      - PayrollStatusKeys has no PayItemID column at all (Status selection
        is a Status Key concept, never a PayItem reference).
      - No PayItem row exists with PayItemCode='STATUS_PAY' -- only a
        payroll.RateTypes row (migration 0056_status_rate_columns.sql) --
        confirming STATUS_PAY is a RateType/RateCode used by the derived
        financial line, not a user-selectable custom Daily PayItem.
      - The generic manual draft-line creation endpoint does not accept
        'STATUS_PAY' as a line_type (no matching PayItem catalog row to
        validate against), confirming Status payment is not reachable as an
        ordinary custom PayItem entry.

    Naming-inconsistency note (see module docstring / Slice 1 precedent):
    several other code paths (Draft-period read filter, add_draft_line's
    Draft guard) compare `linetype`/`payitemcode` against the literal string
    'STATUS_PAYMENT' -- which is actually the `SourceID` PREFIX the sync
    function writes, not the LineType. Since the real linetype written is the
    resolved rate-type code, these particular string comparisons never match
    a real STATUS_PAY row today (masked because status-payment sync is
    skipped entirely for Draft periods) -- documented here as characterized
    compatibility debt, not fixed.
    """

    @pytest.mark.asyncio
    async def test_payrollstatuskeys_has_no_payitemid_column(self, direct_db):
        row = (await direct_db.execute(
            _text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'payroll' AND table_name = 'payrollstatuskeys'
                  AND column_name ILIKE '%payitem%'
            """),
        )).mappings().first()
        assert row is None, (
            "PayrollStatusKeys must have no PayItemID (or similarly named) column -- "
            "Status selection is not, and does not reference, a PayItem"
        )

    @pytest.mark.asyncio
    async def test_no_payitem_row_for_status_pay_ratetype_code(self, direct_db):
        row = (await direct_db.execute(
            _text("SELECT payitemid FROM payroll.payitems WHERE payitemcode = 'STATUS_PAY'"),
        )).mappings().first()
        assert row is None, (
            "STATUS_PAY exists only as a payroll.RateTypes row (migration 0056), "
            "never as a payroll.PayItems row -- STATUS_PAY is not a selectable PayItem"
        )

    @pytest.mark.asyncio
    async def test_generic_draft_line_endpoint_rejects_status_pay_as_line_type(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, paytest_driver_id: int, direct_db,
    ):
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        async with _owned_period(
            session_client, auth_token, paytest_branch_id,
            PERIOD_A_START, PERIOD_A_END, direct_db, suffix="-notpayitem",
        ) as pid:
            resp = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": paytest_driver_id,
                    "work_date": DATE_FEB02,
                    "line_type": "STATUS_PAY",
                    "quantity": "8",
                },
                headers=auth(auth_token),
            )
            assert resp.status_code == 422, (
                f"'STATUS_PAY' must not be creatable as an ordinary manual draft "
                f"line (no PayItem catalog row backs it); got {resp.status_code}: {resp.text}"
            )
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)


# ---------------------------------------------------------------------------
# 9. DAC and future StatusKeyPayRule boundary (no implementation, negative proof)
# ---------------------------------------------------------------------------

class TestDacAndFutureRuleBoundary:
    """
    No DAC implementation is added here. This class proves, narrowly, that
    the CURRENT STATUS_PAY amount does not depend on `AllowanceCategory`
    (two otherwise-identical StatusKeys, differing only in
    AllowanceCategory, produce the identical payment) and documents (via
    schema absence, not a fake table) that no StatusKeyPayRule /
    StatusKeyAllowanceRules table exists yet to depend on.
    """

    @pytest.mark.asyncio
    async def test_status_payment_amount_independent_of_allowance_category(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        driver_id = await _create_driver(session_client, auth_token, paytest_branch_id, "P4S2 DAC Boundary")
        async with _status_rate_column(direct_db, 1, paytest_branch_id) as src_col_id:
            async with contextlib.AsyncExitStack() as stack:
                status_pay_rt_id = await _get_status_pay_rate_type_id(session_client, auth_token)
                await stack.enter_async_context(
                    _owned_driver_rate(
                        direct_db, 1, paytest_branch_id, driver_id, status_pay_rt_id,
                        "15.0000", "2098-01-01",
                    )
                )
                key_no_allowance = await stack.enter_async_context(
                    _owned_status_key(
                        direct_db, 1, paytest_branch_id, f"P4S2DACNO{id(self)}", "8.00", src_col_id,
                        allowance_category=None,
                    )
                )
                key_with_allowance = await stack.enter_async_context(
                    _owned_status_key(
                        direct_db, 1, paytest_branch_id, f"P4S2DACYES{id(self)}", "8.00", src_col_id,
                        allowance_category="Vacation",
                    )
                )
                # One period only: a branch may have only one Open period at a time
                # (`ux_PayrollPeriods_OneOpenPerBranch`) -- two different work dates
                # in the same period isolate the two StatusKey scenarios instead.
                pid = await stack.enter_async_context(
                    _owned_period(
                        session_client, auth_token, paytest_branch_id,
                        PERIOD_A_START, PERIOD_A_END, direct_db, suffix="-dac",
                    )
                )
                code_no = (await direct_db.execute(
                    _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                    {"id": key_no_allowance},
                )).mappings().first()["statuscode"]
                code_yes = (await direct_db.execute(
                    _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                    {"id": key_with_allowance},
                )).mappings().first()["statuscode"]

                r1 = await _save_day_grid(session_client, auth_token, pid, driver_id, DATE_FEB02, code_no)
                assert r1.status_code == 200
                r2 = await _save_day_grid(session_client, auth_token, pid, driver_id, DATE_FEB03, code_yes)
                assert r2.status_code == 200

                lines_1 = await _get_status_pay_lines(direct_db, pid, driver_id)
                lines_1 = [l for l in lines_1 if l["workdate"] == datetime.date.fromisoformat(DATE_FEB02)]
                lines_2 = await _get_status_pay_lines(direct_db, pid, driver_id)
                lines_2 = [l for l in lines_2 if l["workdate"] == datetime.date.fromisoformat(DATE_FEB03)]
                assert len(lines_1) == 1 and len(lines_2) == 1
                amt_1 = Decimal(str(lines_1[0]["calculatedamount"]))
                amt_2 = Decimal(str(lines_2[0]["calculatedamount"]))
                assert amt_1 == Decimal("120.0000") and amt_2 == Decimal("120.0000"), (
                    "Both keys must produce the identical 8.00 x $15 = $120.0000 amount"
                )
                assert amt_1 == amt_2, (
                    "AllowanceCategory ('Vacation' vs NULL) must have zero effect on the "
                    "current STATUS_PAY financial amount -- payment resolution never "
                    "reads AllowanceCategory (confirmed by reading "
                    "_sync_status_payment_for_entry_state, which has no such column reference)"
                )
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)

    @pytest.mark.asyncio
    async def test_no_statuskeypayrule_or_allowancerules_table_exists(self, direct_db):
        """
        Schema-absence proof only -- StatusKeyPayRule/StatusKeyAllowanceRules
        remain future-only architecture; this Slice does not create, fake,
        or simulate either. A future Phase 4/Status-architecture slice must
        re-run this exact check and expect it to start failing once (and
        only once) those tables are deliberately introduced.
        """
        rows = (await direct_db.execute(
            _text("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'payroll'
                  AND (table_name ILIKE '%statuskeypayrule%'
                       OR table_name ILIKE '%statuskeyallowancerule%')
            """),
        )).mappings().all()
        assert len(rows) == 0, (
            f"Found unexpected future-architecture table(s) already present: {[r['table_name'] for r in rows]} "
            f"-- StatusKeyPayRule/StatusKeyAllowanceRules must remain unimplemented in this Slice"
        )


# ---------------------------------------------------------------------------
# 8. Failure-path cleanup regression coverage
# ---------------------------------------------------------------------------

class TestCleanupIsExceptionSafe:
    """
    Proves the ownership helpers above actually clean up when a test fails
    partway through setup, rather than only when the happy path completes.
    Does not exercise or characterize any new financial behavior.
    """

    @pytest.mark.asyncio
    async def test_owned_status_key_and_rate_cleanup_survive_a_setup_failure(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        class _DeliberateTestFailure(Exception):
            pass

        driver_id = await _create_driver(
            session_client, auth_token, paytest_branch_id, "P4S2 Cleanup Failure",
        )
        status_key_id = None
        rate_id = None
        with pytest.raises(_DeliberateTestFailure):
            async with _status_rate_column(direct_db, 1, paytest_branch_id) as src_col_id:
                async with contextlib.AsyncExitStack() as stack:
                    status_key_id = await stack.enter_async_context(
                        _owned_status_key(
                            direct_db, 1, paytest_branch_id,
                            f"P4S2FAIL{id(self)}", "8.00", src_col_id,
                        )
                    )
                    status_pay_rt_id = await _get_status_pay_rate_type_id(session_client, auth_token)
                    rate_id = await stack.enter_async_context(
                        _owned_driver_rate(
                            direct_db, 1, paytest_branch_id, driver_id, status_pay_rt_id,
                            "15.0000", "2098-01-01",
                        )
                    )
                    raise _DeliberateTestFailure("simulated setup failure after partial resource creation")

        assert status_key_id is not None and rate_id is not None, (
            "test bug: failure must be raised after both resources were created"
        )
        key_residue = (await direct_db.execute(
            _text("SELECT COUNT(*) AS cnt FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
            {"id": status_key_id},
        )).mappings().first()
        rate_residue = (await direct_db.execute(
            _text("SELECT COUNT(*) AS cnt FROM payroll.driverrates WHERE driverrateid = :id"),
            {"id": rate_id},
        )).mappings().first()
        assert key_residue["cnt"] == 0, (
            f"StatusKey {status_key_id} leaked after a deliberate setup failure"
        )
        assert rate_residue["cnt"] == 0, (
            f"DriverRate {rate_id} leaked after a deliberate setup failure"
        )

    @pytest.mark.asyncio
    async def test_owned_driver_pay_rule_cleanup_survives_a_setup_failure(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        class _DeliberateTestFailure(Exception):
            pass

        driver_id = await _create_driver(
            session_client, auth_token, paytest_branch_id, "P4S2 PayRule Cleanup Failure",
        )
        rule_id = None
        with pytest.raises(_DeliberateTestFailure):
            async with _owned_driver_pay_rule(
                session_client, auth_token, direct_db,
                driver_id=driver_id, branch_id=paytest_branch_id,
                rule_type="MinimumPay", amount="200.00",
                effective_from=PERIOD_A_START, effective_to=PERIOD_A_END,
            ) as rule_id:
                raise _DeliberateTestFailure("simulated failure before rule assertions complete")

        assert rule_id is not None, "test bug: failure must be raised after rule creation"
        residue = (await direct_db.execute(
            _text("""
                SELECT
                    (SELECT COUNT(*) FROM payroll.driverpayrules
                        WHERE driverpayruleid = :id) AS rule_rows,
                    (SELECT COUNT(*) FROM audit.auditlog
                        WHERE entityname = 'DriverPayRules' AND entityid = :eid) AS audit_rows
            """),
            {"id": rule_id, "eid": str(rule_id)},
        )).mappings().first()
        assert residue["rule_rows"] == 0 and residue["audit_rows"] == 0, (
            f"DriverPayRule {rule_id} residue after a deliberate mid-test failure: {dict(residue)}"
        )

    @pytest.mark.asyncio
    async def test_created_period_cleanup_survives_a_post_creation_failure(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        """
        P2 fix-forward (section 6A): the existing failure-path coverage above
        only proves StatusKey/DriverRate cleanup; this proves Period cleanup
        specifically. Enters StatusKey, DriverRate, and Period owners (in
        that order, via the same `AsyncExitStack` composition
        `_status_pay_scenario` uses), drives one real day-grid save so a
        canonical entry-state row and a real STATUS_PAY DraftLine both exist,
        then deliberately fails AFTER the Period exists. All resource-owner
        `finally` blocks must still unwind and leave zero residue for the
        Period itself, its DraftLines/FinalLines, its canonical entry-state
        rows, and its review/audit rows -- not only the StatusKey/DriverRate.
        """
        class _DeliberateTestFailure(Exception):
            pass

        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        driver_id = await _create_driver(
            session_client, auth_token, paytest_branch_id, "P4S2 Period Cleanup Failure",
        )
        status_key_id = None
        rate_id = None
        pid = None
        with pytest.raises(_DeliberateTestFailure):
            async with _status_rate_column(direct_db, 1, paytest_branch_id) as src_col_id:
                async with contextlib.AsyncExitStack() as stack:
                    status_key_id = await stack.enter_async_context(
                        _owned_status_key(
                            direct_db, 1, paytest_branch_id,
                            f"P4S2PFAIL{id(self)}", "8.00", src_col_id,
                        )
                    )
                    status_pay_rt_id = await _get_status_pay_rate_type_id(session_client, auth_token)
                    rate_id = await stack.enter_async_context(
                        _owned_driver_rate(
                            direct_db, 1, paytest_branch_id, driver_id, status_pay_rt_id,
                            "20.0000", "2098-01-01",
                        )
                    )
                    pid = await stack.enter_async_context(
                        _owned_period(
                            session_client, auth_token, paytest_branch_id,
                            PERIOD_A_START, PERIOD_A_END, direct_db, suffix="-pfail",
                        )
                    )
                    code_row = (await direct_db.execute(
                        _text("SELECT statuscode FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
                        {"id": status_key_id},
                    )).mappings().first()
                    save_resp = await _save_day_grid(
                        session_client, auth_token, pid, driver_id, DATE_FEB02, code_row["statuscode"],
                    )
                    assert save_resp.status_code == 200, f"Day grid save failed: {save_resp.text}"
                    entry_state = await _get_entry_state_row(direct_db, pid, driver_id, DATE_FEB02)
                    assert entry_state is not None, "Canonical entry-state row must exist before the failure"
                    lines = await _get_status_pay_lines(direct_db, pid, driver_id)
                    assert len(lines) == 1, "Real STATUS_PAY DraftLine must exist before the failure"

                    raise _DeliberateTestFailure("simulated failure after Period + entry-state exist")

        assert status_key_id is not None and rate_id is not None and pid is not None, (
            "test bug: failure must be raised after StatusKey, DriverRate, and Period all exist"
        )

        residue = (await direct_db.execute(
            _text("""
                SELECT
                    (SELECT COUNT(*) FROM payroll.payrollperiods WHERE payrollperiodid = :pid) AS periods,
                    (SELECT COUNT(*) FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid) AS draftlines,
                    (SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :pid) AS finallines,
                    (SELECT COUNT(*) FROM payroll.payrollperioddriverdayentrystate
                        WHERE payrollperiodid = :pid) AS ppdes,
                    (SELECT COUNT(*) FROM payroll.payrollstatuskeys WHERE statuskeyid = :skid) AS statuskeys,
                    (SELECT COUNT(*) FROM payroll.driverrates WHERE driverrateid = :rid) AS driverrates,
                    (SELECT COUNT(*) FROM audit.auditlog
                        WHERE entityname = 'PayrollPeriods' AND entityid = :eid) AS period_audit,
                    (SELECT COUNT(*) FROM review.managerreviewitems
                        WHERE entityname = 'PayrollPeriods' AND entityid = :eid) AS review_items
            """),
            {"pid": pid, "skid": status_key_id, "rid": rate_id, "eid": str(pid)},
        )).mappings().first()
        assert residue["periods"] == 0, f"Period {pid} leaked after a deliberate post-creation failure"
        assert residue["draftlines"] == 0, f"DraftLines for period {pid} leaked"
        assert residue["finallines"] == 0, f"FinalLines for period {pid} leaked"
        assert residue["ppdes"] == 0, f"Canonical entry-state rows for period {pid} leaked"
        assert residue["statuskeys"] == 0, f"StatusKey {status_key_id} leaked"
        assert residue["driverrates"] == 0, f"DriverRate {rate_id} leaked"
        assert residue["period_audit"] == 0, f"AuditLog rows for period {pid} leaked"
        assert residue["review_items"] == 0, f"Review items for period {pid} leaked"

    @pytest.mark.asyncio
    async def test_forced_fallback_status_rate_column_cleanup_survives_a_setup_failure(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, direct_db,
    ):
        """
        P2 fix-forward (section 6B): deterministically exercises the
        test-owned fallback StatusRateColumns creation branch (via
        `_forced_owned_status_rate_column`, which always creates a new row
        rather than reusing a shared one), creates a dependent StatusKey
        against it, then deliberately fails. Proves: the dependent StatusKey
        is removed first (required by the FK from PayrollStatusKeys to
        StatusRateColumns -- `_owned_status_key`'s own `finally` runs before
        `_forced_owned_status_rate_column`'s, since the StatusKey context is
        entered AFTER and therefore exits BEFORE, standard nested
        context-manager unwind order), the exact forced fallback row is
        removed, and any pre-existing shared/seeded StatusRateColumns rows
        for this branch are completely unaffected (row count for THOSE rows
        unchanged before/after).
        """
        class _DeliberateTestFailure(Exception):
            pass

        shared_ids_before = {
            r["statusratecolumnid"] for r in (await direct_db.execute(
                _text(
                    "SELECT statusratecolumnid FROM payroll.statusratecolumns "
                    "WHERE branchid = :bid AND companyid = 1"
                ),
                {"bid": paytest_branch_id},
            )).mappings().all()
        }

        src_col_id = None
        status_key_id = None
        with pytest.raises(_DeliberateTestFailure):
            async with _forced_owned_status_rate_column(direct_db, 1, paytest_branch_id) as (col_id, marker):
                src_col_id = col_id
                async with _owned_status_key(
                    direct_db, 1, paytest_branch_id,
                    f"P4S2FSRC{id(self)}", "8.00", src_col_id,
                ) as key_id:
                    status_key_id = key_id
                    raise _DeliberateTestFailure("simulated failure after forced fallback column + StatusKey exist")

        assert src_col_id is not None and status_key_id is not None, (
            "test bug: failure must be raised after both the forced StatusRateColumns "
            "row and its dependent StatusKey exist"
        )

        key_residue = (await direct_db.execute(
            _text("SELECT COUNT(*) AS cnt FROM payroll.payrollstatuskeys WHERE statuskeyid = :id"),
            {"id": status_key_id},
        )).mappings().first()
        assert key_residue["cnt"] == 0, (
            f"Dependent StatusKey {status_key_id} leaked after a deliberate setup failure -- "
            f"must be removed before the fallback StatusRateColumns row (FK ordering)"
        )
        col_residue = (await direct_db.execute(
            _text("SELECT COUNT(*) AS cnt FROM payroll.statusratecolumns WHERE statusratecolumnid = :id"),
            {"id": src_col_id},
        )).mappings().first()
        assert col_residue["cnt"] == 0, (
            f"Forced fallback StatusRateColumns row {src_col_id} leaked after a deliberate setup failure"
        )

        shared_ids_after = {
            r["statusratecolumnid"] for r in (await direct_db.execute(
                _text(
                    "SELECT statusratecolumnid FROM payroll.statusratecolumns "
                    "WHERE branchid = :bid AND companyid = 1"
                ),
                {"bid": paytest_branch_id},
            )).mappings().all()
        }
        assert shared_ids_after == shared_ids_before, (
            f"Pre-existing shared/seeded StatusRateColumns rows for this branch must be "
            f"completely unaffected: before={shared_ids_before}, after={shared_ids_after}"
        )
