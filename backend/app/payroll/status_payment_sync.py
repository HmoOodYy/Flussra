"""
Status Payment Sync — derives and synchronizes STATUS_PAYMENT source-line
behavior from canonical driver/day entry-state status data.

Extracted from app.payroll.service (Stage B4-15) as a dependency-closed leaf
module — no behavior change, pure relocation.

Distinct from app.payroll.day_entry_state (Stage B4-13A), which owns the
canonical per-driver/day status + note state itself, StatusKey validation/
resolution, entry-state mutation, and STATUS_NOTE evidence. This module
consumes that canonical state (by reading
PayrollPeriodDriverDayEntryState/PayrollStatusKeys directly) and derives
STATUS_PAYMENT source-line/payment behavior from it — it does not import or
duplicate any Day Entry State function.

Owns two views of the same canonical Status Payment policy, both sharing the
same rate-resolution rule and the same arithmetic
(_calculate_status_payment_amount), so they can never independently drift:

  A. Persisted synchronization/write path:
     _sync_status_payment_for_entry_state, _refresh_status_payment_lines
  B. Live/read-only projection path:
     _LiveStatusLine, _resolve_live_status_payment_lines

Plus _STATUS_PAYMENT_PROJECTION_SQL: the exact identity predicate for the
persisted STATUS_PAYMENT compatibility-projection DraftLine written by
_sync_status_payment_for_entry_state, used by Calculation
(_build_live_calculation_packet, in app.payroll.period_calculation) to
exclude those rows from its own live-line scan since the canonical live
status pay is sourced from _resolve_live_status_payment_lines instead.

Current consumers, each in its own module:
  - Day Grid (save_day_grid, in app.payroll.day_grid) calls
    _sync_status_payment_for_entry_state.
  - Lifecycle (change_period_status, resubmit_period, in
    app.payroll.period_lifecycle) calls _refresh_status_payment_lines.
  - Calculation (_build_live_calculation_packet, in
    app.payroll.period_calculation) calls _resolve_live_status_payment_lines
    and references _STATUS_PAYMENT_PROJECTION_SQL.
"""
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, NamedTuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.eligibility import (
    _driver_has_existing_daily_source_on_date,
    _is_snapshot_row_eligible_for_workdate,
    _period_has_driver_eligibility_snapshot,
)

# CP-4B fix (Codex P2): exact identity predicate for the persisted Status-
# payment compatibility projection DraftLine written by
# `_sync_status_payment_for_entry_state`. LineType cannot be hardcoded here
# (it is the mapped RateType's RateCode, which is data-driven per company/
# StatusRateColumn configuration) — the projection is instead uniquely
# identified by the combination of SourceType='System' and a SourceID that
# matches the exact 'STATUS_PAYMENT:{entry_state_id}:{status_key_id}:
# {status_rate_column_id}' format (three integer segments), not merely a
# SourceID text prefix. A prefix-only match could incorrectly exclude an
# unrelated line whose SourceID happens to start with the same text but has
# a different SourceType or a malformed/foreign suffix.
_STATUS_PAYMENT_PROJECTION_SQL = (
    "(dl.sourcetype = 'System' "
    "AND dl.sourceid ~ '^STATUS_PAYMENT:[0-9]+:[0-9]+:[0-9]+$')"
)


# =============================================================================
# CP-2D2: Status payment sync helpers
# =============================================================================

def _calculate_status_payment_amount(
    hours_value: "Decimal | None",
    resolved_rate: "Decimal | None",
) -> "Decimal | None":
    """
    Pure Status-payment arithmetic (CP-4B fix-forward): the exact single
    operation shared by the write-based synchronizer
    (`_sync_status_payment_for_entry_state`) and the CP-4B read-only live
    Status resolver (`_resolve_live_status_payment_lines`), so the two can
    never independently drift.

    `HoursValue x DriverRate.Amount`, quantized once to `Decimal("0.0001")`
    with explicit `ROUND_HALF_EVEN` -- identical to the ambient-context
    behavior this replaces (Python's implicit default rounding mode is
    already `ROUND_HALF_EVEN`; making it explicit here does not change any
    result). Returns `None` under the same truthiness guard the original
    inline expression used: no resolved rate, or a falsy (zero) hours value.

    Contains no SQL, no persistence, no workflow -- callers remain
    responsible for resolving `hours_value`/`resolved_rate` and for any
    write/void/upsert behavior.
    """
    if resolved_rate is None or not hours_value:
        return None
    return (hours_value * resolved_rate).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)


async def _sync_status_payment_for_entry_state(
    company_id: int,
    branch_id: int,
    period_id: int,
    driver_id: int,
    work_date: "date",
    status_key_id: "int | None",
    user_id: int,
    db: AsyncConnection,
) -> None:
    """
    Create, update, or void the STATUS_PAYMENT draft line for a driver/day.

    Called from save_day_grid after _upsert_entry_state.
    Called from _refresh_status_payment_lines for each PPDES row at submit/finalize.

    Logic:
      - If status_key_id is None or StatusKey has no StatusRateColumnID → void any existing line.
      - Otherwise → look up driver rate for StatusRateColumns.RateTypeID as-of work_date,
        compute HoursValue × rate, and upsert the draft line.

    SOURCE_ID format: 'STATUS_PAYMENT:{entry_state_id}:{status_key_id}:{status_rate_column_id}'
    Line type: the RateType's RateCode (e.g., 'STATUS_PAY').
    """
    # 1. Get canonical entry state row ID
    es_result = await db.execute(
        text("""
            SELECT payrollperioddriverdayentrystateid
            FROM   payroll.payrollperioddriverdayentrystate
            WHERE  payrollperiodid = :pid AND driverid = :did AND workdate = :dt
        """),
        {"pid": period_id, "did": driver_id, "dt": work_date},
    )
    es_row = es_result.mappings().first()
    entry_state_id: int | None = (
        es_row["payrollperioddriverdayentrystateid"] if es_row else None
    )

    # 2. Look up StatusKey payment config if status is set
    src_col_id: int | None = None
    src_rate_type_id: int | None = None
    rate_code: str | None = None
    hours_value: Decimal | None = None
    status_code_val: str | None = None
    key_name_val: str | None = None
    col_name_val: str | None = None

    if status_key_id is not None:
        sk_result = await db.execute(
            text("""
                SELECT sk.statuskeyid, sk.statuscode, sk.keyname,
                       sk.hoursvalue, sk.statusratecolumnid,
                       src.columnname AS src_col_name,
                       src.ratetypeid, rt.ratecode
                FROM   payroll.payrollstatuskeys sk
                LEFT JOIN payroll.statusratecolumns src
                       ON src.statusratecolumnid = sk.statusratecolumnid
                LEFT JOIN payroll.ratetypes rt
                       ON rt.ratetypeid = src.ratetypeid
                WHERE  sk.statuskeyid = :skid
            """),
            {"skid": status_key_id},
        )
        sk_row = sk_result.mappings().first()
        if sk_row:
            src_col_id       = sk_row["statusratecolumnid"]
            src_rate_type_id = sk_row["ratetypeid"]
            rate_code        = sk_row["ratecode"]
            hv               = sk_row["hoursvalue"]
            hours_value      = Decimal(str(hv)) if hv is not None else Decimal("0")
            status_code_val  = sk_row["statuscode"]
            key_name_val     = sk_row["keyname"]
            col_name_val     = sk_row["src_col_name"]

    # 3. Find any existing non-void STATUS_PAYMENT line for this slot
    existing_result = await db.execute(
        text("""
            SELECT draftlineid, sourceid
            FROM   payroll.payrolldraftlines
            WHERE  payrollperiodid = :pid
              AND  driverid        = :did
              AND  workdate        = :dt
              AND  companyid       = :cid
              AND  sourceid        LIKE 'STATUS_PAYMENT:%'
              AND  status         != 'Void'
            LIMIT 1
        """),
        {"pid": period_id, "did": driver_id, "dt": work_date, "cid": company_id},
    )
    existing = existing_result.mappings().first()

    # 4. If no status, no rate column, or no entry state → void existing and return
    if status_key_id is None or src_col_id is None or entry_state_id is None:
        if existing:
            await db.execute(
                text(
                    "UPDATE payroll.payrolldraftlines SET status = 'Void' "
                    "WHERE draftlineid = :lid"
                ),
                {"lid": existing["draftlineid"]},
            )
        return

    # 5. Compute amount
    import json as _json
    rate_result = await db.execute(
        text("""
            SELECT driverrateid, amount
            FROM   payroll.driverrates
            WHERE  driverid     = :did
              AND  ratetypeid   = :rtid
              AND  companyid    = :cid
              AND  status       IN ('Approved', 'Superseded')
              AND  effectivefrom <= :dt
              AND  (effectiveto IS NULL OR effectiveto >= :dt)
            ORDER BY effectivefrom DESC
            LIMIT 1
        """),
        {"did": driver_id, "rtid": src_rate_type_id, "cid": company_id, "dt": work_date},
    )
    rate_row = rate_result.mappings().first()
    resolved_rate    = Decimal(str(rate_row["amount"])) if rate_row else None
    resolved_rate_id = rate_row["driverrateid"] if rate_row else None
    calc_amount = _calculate_status_payment_amount(hours_value, resolved_rate)
    needs_review = resolved_rate is None

    new_source_id = (
        f"STATUS_PAYMENT:{entry_state_id}:{status_key_id}:{src_col_id}"
    )

    # Build SourceSnapshot — immutable audit record of inputs at draft time.
    snapshot_dict: dict = {
        "entry_state_id":           entry_state_id,
        "payroll_period_id":        period_id,
        "driver_id":                driver_id,
        "work_date":                str(work_date),
        "status_key_id":            status_key_id,
        "status_code":              status_code_val,
        "status_key_name":          key_name_val,
        "hours_value_used":         float(hours_value) if hours_value is not None else None,
        "status_rate_column_id":    src_col_id,
        "status_rate_column_name":  col_name_val,
        "rate_type_id":             src_rate_type_id,
        "rate_code":                rate_code,
        "driver_rate_id":           resolved_rate_id,
        "resolved_rate_amount":     float(resolved_rate) if resolved_rate is not None else None,
        "calculated_amount":        float(calc_amount) if calc_amount is not None else None,
        "formula":                  "HoursValue * DriverRate.Amount",
    }
    # Remove None values to keep snapshot lean
    source_snapshot = _json.dumps(
        {k: v for k, v in snapshot_dict.items() if v is not None}
    )

    # 6. Upsert draft line
    if existing:
        if existing["sourceid"] != new_source_id:
            # Status key or column changed — void old, fall through to insert
            await db.execute(
                text(
                    "UPDATE payroll.payrolldraftlines SET status = 'Void' "
                    "WHERE draftlineid = :lid"
                ),
                {"lid": existing["draftlineid"]},
            )
            existing = None
        else:
            # In-place update (also refresh SourceSnapshot in case rate changed)
            await db.execute(
                text("""
                    UPDATE payroll.payrolldraftlines
                    SET    quantity           = :qty,
                           calculatedamount   = :calc,
                           needsmanagerreview = :review,
                           sourcesnapshot     = CAST(:snap AS JSONB),
                           status             = 'Active'
                    WHERE  draftlineid = :lid
                """),
                {
                    "qty":    hours_value,
                    "calc":   calc_amount,
                    "review": needs_review,
                    "snap":   source_snapshot,
                    "lid":    existing["draftlineid"],
                },
            )
            return

    # Insert new STATUS_PAYMENT line
    await db.execute(
        text("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid,
                 workdate, linetype, linescope, quantity,
                 calculatedamount, sourcetype, sourceid,
                 status, needsmanagerreview, addedbyuserid, sourcesnapshot)
            VALUES
                (:cid, :bid, :pid, :did,
                 :dt, :lt, 'Daily', :qty,
                 :calc, 'System', :sid,
                 'Active', :review, :uid, CAST(:snap AS JSONB))
        """),
        {
            "cid":    company_id,
            "bid":    branch_id,
            "pid":    period_id,
            "did":    driver_id,
            "dt":     work_date,
            "lt":     rate_code,
            "qty":    hours_value,
            "calc":   calc_amount,
            "sid":    new_source_id,
            "review": needs_review,
            "uid":    user_id,
            "snap":   source_snapshot,
        },
    )


class _LiveStatusLine(NamedTuple):
    """One canonically-resolved, read-only, live Status-derived pay line
    (CP-4B). Distinct from the persisted STATUS_PAYMENT compatibility
    projection DraftLine, which may be stale."""
    driver_id: int
    entry_state_id: int
    work_date: "date"
    status_key_id: int
    status_code: str
    status_rate_column_id: "int | None"
    rate_type_id: int | None
    driver_rate_id: int | None
    line_type: str
    hours_value: Decimal
    resolved_rate_amount: "Decimal | None"
    calculated_amount: "Decimal | None"
    needs_manager_review: bool


async def _resolve_live_status_payment_lines(
    period_id: int,
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> "list[_LiveStatusLine]":
    """
    CP-4B read-only resolver: computes current, live Status-derived pay for
    every canonical selected Status in this period, directly from
    `PayrollPeriodDriverDayEntryState.StatusKeyID` -- never from the
    persisted STATUS_PAYMENT/STATUS_PAY compatibility-projection DraftLine,
    which is a write-time snapshot that can go stale after an effective-
    dated rate change (see `_sync_status_payment_for_entry_state`, which
    only re-runs on save/submit/finalize, not on every read).

    Uses the exact same rate-resolution rule as the synchronizer (Approved
    or Superseded `DriverRates`, effective-dated as-of the entry's own
    `WorkDate`, most-recent `EffectiveFrom` wins -- Pending rates are
    excluded by the `status IN ('Approved','Superseded')` filter) and
    shares its arithmetic via `_calculate_status_payment_amount`, so this
    can never independently drift from the persisted-write formula.

    Guarantees: no INSERT/UPDATE/DELETE; no audit write; no mutation of the
    canonical entry-state rows or any StatusKey/StatusRateColumns row.
    A StatusKey with no configured `StatusRateColumnID` (a non-payment
    status, e.g. an off-reason with no rate) is not a blocker and is simply
    omitted -- it never expected a payment line.
    """
    rows = (await db.execute(
        text("""
            SELECT
                ppdes.driverid,
                ppdes.payrollperioddriverdayentrystateid,
                ppdes.workdate,
                sk.statuskeyid,
                sk.statuscode,
                sk.hoursvalue,
                sk.statusratecolumnid,
                src.ratetypeid,
                rt.ratecode
            FROM   payroll.payrollperioddriverdayentrystate ppdes
            JOIN   payroll.payrollstatuskeys sk ON sk.statuskeyid = ppdes.statuskeyid
            LEFT JOIN payroll.statusratecolumns src ON src.statusratecolumnid = sk.statusratecolumnid
            LEFT JOIN payroll.ratetypes rt ON rt.ratetypeid = src.ratetypeid
            WHERE  ppdes.payrollperiodid = :pid
              AND  ppdes.companyid       = :cid
              AND  ppdes.branchid        = :bid
              AND  ppdes.isvoided        = FALSE
              AND  ppdes.statuskeyid IS NOT NULL
            ORDER BY ppdes.driverid, ppdes.workdate
        """),
        {"pid": period_id, "cid": company_id, "bid": branch_id},
    )).mappings().all()

    results: list[_LiveStatusLine] = []
    for row in rows:
        src_rate_type_id = row["ratetypeid"]
        if src_rate_type_id is None:
            # No configured Status rate column -- this status never expects
            # a payment line; not a blocker, simply not applicable.
            continue

        hv = row["hoursvalue"]
        hours_value = Decimal(str(hv)) if hv is not None else Decimal("0")

        rate_row = (await db.execute(
            text("""
                SELECT driverrateid, amount
                FROM   payroll.driverrates
                WHERE  driverid      = :did
                  AND  ratetypeid    = :rtid
                  AND  companyid     = :cid
                  AND  status        IN ('Approved', 'Superseded')
                  AND  effectivefrom <= :dt
                  AND  (effectiveto IS NULL OR effectiveto >= :dt)
                ORDER BY effectivefrom DESC
                LIMIT 1
            """),
            {
                "did": row["driverid"], "rtid": src_rate_type_id,
                "cid": company_id, "dt": row["workdate"],
            },
        )).mappings().first()
        resolved_rate = Decimal(str(rate_row["amount"])) if rate_row else None

        results.append(_LiveStatusLine(
            driver_id=row["driverid"],
            entry_state_id=row["payrollperioddriverdayentrystateid"],
            work_date=row["workdate"],
            status_key_id=row["statuskeyid"],
            status_code=row["statuscode"],
            status_rate_column_id=row["statusratecolumnid"],
            rate_type_id=src_rate_type_id,
            driver_rate_id=(int(rate_row["driverrateid"]) if rate_row else None),
            line_type=row["ratecode"] or "STATUS_PAY",
            hours_value=hours_value,
            resolved_rate_amount=resolved_rate,
            calculated_amount=_calculate_status_payment_amount(hours_value, resolved_rate),
            needs_manager_review=(resolved_rate is None),
        ))

    return results


async def _refresh_status_payment_lines(
    period_id: int,
    company_id: int,
    branch_id: int,
    user_id: int,
    db: AsyncConnection,
) -> int:
    """
    Re-sync all STATUS_PAYMENT draft lines for a period from PPDES state.

    Called before _refresh_draft_calculations at submit and finalize so that
    status-payment lines are current before the NMR guard runs.

    Returns the count of PPDES rows processed.
    """
    # Fetch all non-voided PPDES rows for this period
    ppdes_result = await db.execute(
        text("""
            SELECT payrollperioddriverdayentrystateid,
                   driverid, workdate, statuskeyid
            FROM   payroll.payrollperioddriverdayentrystate
            WHERE  payrollperiodid = :pid
              AND  companyid       = :cid
              AND  isvoided        = FALSE
        """),
        {"pid": period_id, "cid": company_id},
    )
    rows = list(ppdes_result.mappings().all())

    # Also void any orphaned STATUS_PAYMENT lines for slots with no PPDES row
    # (e.g., status was cleared and PPDES was voided after a prior sync)
    await db.execute(
        text("""
            UPDATE payroll.payrolldraftlines
            SET    status = 'Void'
            WHERE  payrollperiodid = :pid
              AND  companyid       = :cid
              AND  sourceid        LIKE 'STATUS_PAYMENT:%'
              AND  status         != 'Void'
              AND  NOT EXISTS (
                SELECT 1 FROM payroll.payrollperioddriverdayentrystate ppdes
                WHERE  ppdes.payrollperiodid = payrolldraftlines.payrollperiodid
                  AND  ppdes.driverid        = payrolldraftlines.driverid
                  AND  ppdes.workdate        = payrolldraftlines.workdate
                  AND  ppdes.isvoided        = FALSE
                  AND  ppdes.statuskeyid     IS NOT NULL
              )
        """),
        {"pid": period_id, "cid": company_id},
    )

    # CP-2E: pre-load snapshot rows for this period (one query, not per-row)
    _refresh_has_snap = await _period_has_driver_eligibility_snapshot(period_id, db)
    _snap_row_by_driver: dict[int, Any] = {}
    if _refresh_has_snap:
        snap_res = await db.execute(
            text("""
                SELECT driverid, eligibilityreasoncode,
                       hiredatesnapshot, terminationdatesnapshot,
                       drivereffectivefromsnapshot, drivereffectivetosnapshot,
                       iseligibleforperiod
                FROM   payroll.payrollperioddrivereligibility
                WHERE  payrollperiodid = :pid AND companyid = :cid
            """),
            {"pid": period_id, "cid": company_id},
        )
        for snap in snap_res.mappings().all():
            _snap_row_by_driver[snap["driverid"]] = snap

    for row in rows:
        driver_id = row["driverid"]
        work_date = row["workdate"]

        # CP-2E: eligibility guard — skip ineligible dates in snapshotted periods
        if _refresh_has_snap:
            snap = _snap_row_by_driver.get(driver_id)
            if snap is None:
                # Driver not in snapshot → skip
                continue
            # Primary: date-window check. Secondary rescue: existing source on
            # exact date (any reason code — covers generated-row drivers with
            # PPDES outside their eligibility window from before the snapshot).
            if not _is_snapshot_row_eligible_for_workdate(snap, work_date):
                has_src = await _driver_has_existing_daily_source_on_date(
                    period_id, driver_id, work_date, db
                )
                if not has_src:
                    continue

        await _sync_status_payment_for_entry_state(
            company_id=company_id,
            branch_id=branch_id,
            period_id=period_id,
            driver_id=driver_id,
            work_date=work_date,
            status_key_id=row["statuskeyid"],
            user_id=user_id,
            db=db,
        )

    return len(rows)
