"""
Day Entry State — canonical per-driver/per-work-date entry-state behavior.

Extracted from app.payroll.service (Stage B4-13A) as a dependency-closed leaf
module — no behavior change, pure relocation.

Owns the canonical payroll.payrollperioddriverdayentrystate row:
  - _validate_status_key / _resolve_status_key_id: StatusKey lookup/validation
    against payroll.payrollstatuskeys.
  - _upsert_entry_state / _void_entry_state_field: canonical entry-state
    upsert and field-clear behavior, including the STATUS_NOTE immutable
    evidence this mutation emits directly via capture_period_audit_evidence.
  - _enforce_status_key_limits: the four optional StatusKey usage limits
    (per-period, per-driver, across-drivers, per-day) enforced during
    save_day_grid batches.

_canonical_aliases and _parse_quantity were evaluated for this stage and
excluded: both are save_day_grid-only input-normalization helpers (legacy
line-type alias resolution for pay-item DraftLine lookups, and day-grid
quantity-string parsing) with zero coupling to the entry-state table, no
StatusKey involvement, and no audit evidence of their own. They remain
Day-Grid-owned in app.payroll.service pending a future Day Grid ownership
stage, not this one.

Genuinely shared by two domains that both still live in app.payroll.service —
Draft-line CRUD (add_draft_line, update_draft_line, void_draft_line) and Day
Grid (save_day_grid) — neither of which is more entitled to own it than the
other. This module owns only canonical entry-state read/validate/write
behavior; it is distinct from Status Payment Sync
(_sync_status_payment_for_entry_state, _refresh_status_payment_lines,
_resolve_live_status_payment_lines, _calculate_status_payment_amount,
_LiveStatusLine, _STATUS_PAYMENT_PROJECTION_SQL), which derives STATUS_PAYMENT
draft lines FROM the canonical state this module owns and remains in
app.payroll.service. It contains no Day Grid orchestration, no Draft CRUD
line-write logic, and no status-payment derivation.
"""
from datetime import date
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.audit_evidence import capture_period_audit_evidence


async def _validate_status_key(
    key_code: str | None,
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
    *,
    allow_deactivated: bool = False,
) -> dict | None:
    """
    Validate that key_code exists in payrollstatuskeys for the branch.

    - None / blank           → returns None (caller treats as clear)
    - Valid active           → returns the full row dict (including all limit fields)
    - Not found              → raises HTTP 422
    - Inactive key           → raises HTTP 422 unless allow_deactivated=True
      (allow_deactivated is used when the user is re-submitting a status code that
       matches an existing DraftLine selection — key was deactivated after it was
       originally applied, so saving the same value unchanged should not be blocked)
    """
    if not key_code or key_code.strip() == "":
        return None
    result = await db.execute(
        text("""
            SELECT statuskeyid, statuscode, keyname, isoffreason, hoursvalue, isactive,
                   limitusesperperiodenabled, limitusesperperiod,
                   limitusesperdriverenabled, limitusesperdriver,
                   limitusesacrossdriversenabled, limitusesacrossdrivers,
                   limitusesperdayenabled, limitusesperday
            FROM   payroll.payrollstatuskeys
            WHERE  companyid  = :cid
              AND  branchid   = :bid
              AND  statuscode = :code
        """),
        {"cid": company_id, "bid": branch_id, "code": key_code.strip()},
    )
    row = result.mappings().first()
    if row is None or (not allow_deactivated and not row["isactive"]):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Status key '{key_code}' is not a valid active status "
                "for this branch."
            ),
        )
    return dict(row)


async def _resolve_status_key_id(
    company_id: int,
    branch_id: int,
    status_code: str,
    db: AsyncConnection,
) -> "int | None":
    """Return StatusKeyID for status_code — no isactive filter (accepts deactivated)."""
    result = await db.execute(
        text(
            "SELECT statuskeyid FROM payroll.payrollstatuskeys "
            "WHERE companyid = :cid AND branchid = :bid AND statuscode = :code LIMIT 1"
        ),
        {"cid": company_id, "bid": branch_id, "code": status_code},
    )
    row = result.first()
    return row[0] if row else None


async def _upsert_entry_state(
    company_id: int,
    branch_id: int,
    period_id: int,
    driver_id: int,
    work_date: "date",
    user_id: int,
    db: AsyncConnection,
    *,
    status_key_id: "int | None" = None,
    note_text: "str | None" = None,
    set_status: bool = True,
    set_note: bool = True,
) -> None:
    """
    Upsert canonical PayrollPeriodDriverDayEntryState row.

    set_status / set_note control which fields are updated on conflict.
    When both are True (default, save_day_grid path), both fields are set and
    IsVoided is derived from whether both would be empty.
    When only one is True (direct DraftLine API path), the other field is
    not touched by the ON CONFLICT UPDATE — only the INSERT uses NULL as default.
    """
    if not set_status and not set_note:
        return

    before_row = (await db.execute(text("""
        SELECT e.payrollperioddriverdayentrystateid, e.statuskeyid, e.notetext,
               sk.statuscode, sk.keyname AS statuslabel
        FROM payroll.payrollperioddriverdayentrystate e
        LEFT JOIN payroll.payrollstatuskeys sk ON sk.statuskeyid = e.statuskeyid
        WHERE e.companyid = :cid AND e.payrollperiodid = :pid
          AND e.driverid = :did AND e.workdate = :dt
    """), {"cid": company_id, "pid": period_id, "did": driver_id, "dt": work_date})).mappings().first()

    day_id_result = await db.execute(
        text(
            "SELECT payrollperioddayid FROM payroll.payrollperioddays "
            "WHERE payrollperiodid = :pid AND workdate = :dt LIMIT 1"
        ),
        {"pid": period_id, "dt": work_date},
    )
    day_id = day_id_result.scalar_one_or_none()

    # IsVoided: only deterministic when setting both fields simultaneously.
    # Single-field updates use FALSE (we are setting something, so not voided).
    if set_status and set_note:
        is_voided = status_key_id is None and not note_text
    else:
        is_voided = False

    # Build ON CONFLICT SET clause — only include fields being updated.
    conflict_parts = []
    if set_status:
        conflict_parts.append("statuskeyid = EXCLUDED.statuskeyid")
    if set_note:
        conflict_parts.append("notetext = EXCLUDED.notetext")
    if set_status and set_note:
        conflict_parts.append("isvoided = EXCLUDED.isvoided")
    else:
        conflict_parts.append("isvoided = FALSE")
    conflict_parts += [
        "updatedbyuserid = EXCLUDED.updatedbyuserid",
        "updatedatutc    = NOW()",
    ]
    conflict_clause = ",\n                    ".join(conflict_parts)

    await db.execute(
        text(f"""
            INSERT INTO payroll.payrollperioddriverdayentrystate
                (companyid, branchid, payrollperiodid, payrollperioddayid,
                 workdate, driverid, statuskeyid, notetext, isvoided,
                 createdbyuserid, updatedbyuserid, createdatutc, updatedatutc)
            VALUES
                (:cid, :bid, :pid, :day_id,
                 :dt, :did, :skid, :note, :voided,
                 :uid, :uid, NOW(), NOW())
            ON CONFLICT (payrollperiodid, driverid, workdate) DO UPDATE SET
                {conflict_clause}
        """),
        {
            "cid":    company_id,
            "bid":    branch_id,
            "pid":    period_id,
            "day_id": day_id,
            "dt":     work_date,
            "did":    driver_id,
            "skid":   status_key_id,
            "note":   note_text or None,
            "voided": is_voided,
            "uid":    user_id,
        },
    )
    after_row = (await db.execute(text("""
        SELECT e.payrollperioddriverdayentrystateid, e.statuskeyid, e.notetext,
               sk.statuscode, sk.keyname AS statuslabel
        FROM payroll.payrollperioddriverdayentrystate e
        LEFT JOIN payroll.payrollstatuskeys sk ON sk.statuskeyid = e.statuskeyid
        WHERE e.companyid = :cid AND e.payrollperiodid = :pid
          AND e.driverid = :did AND e.workdate = :dt
    """), {"cid": company_id, "pid": period_id, "did": driver_id, "dt": work_date})).mappings().one()

    def status_payload(row: Any | None) -> dict[str, Any] | None:
        if row is None or row["statuskeyid"] is None:
            return None
        return {
            "status_key_id": row["statuskeyid"], "status_code": row["statuscode"],
            "status_label": row["statuslabel"],
        }

    before_status = status_payload(before_row)
    after_status = status_payload(after_row)
    entry_id = int(after_row["payrollperioddriverdayentrystateid"])
    if set_status and before_status != after_status:
        action = "STATUS_CLEARED" if after_status is None else (
            "STATUS_SET" if before_status is None else "STATUS_CHANGED"
        )
        await capture_period_audit_evidence(
            company_id=company_id, branch_id=branch_id, period_id=period_id,
            domain="STATUS_NOTE", action_code=action,
            source_entity_type="PayrollPeriodDriverDayEntryState", source_entity_id=entry_id,
            user_id=user_id, required_permission_code="payroll.entry", db=db,
            before_state=before_status, after_state=after_status,
            driver_id=driver_id, work_date=work_date,
        )
    before_note = None if before_row is None else before_row["notetext"]
    after_note = after_row["notetext"]
    if set_note and before_note != after_note:
        action = "NOTE_CLEARED" if not after_note else (
            "NOTE_SET" if not before_note else "NOTE_CHANGED"
        )
        await capture_period_audit_evidence(
            company_id=company_id, branch_id=branch_id, period_id=period_id,
            domain="STATUS_NOTE", action_code=action,
            source_entity_type="PayrollPeriodDriverDayEntryState", source_entity_id=entry_id,
            user_id=user_id, required_permission_code="payroll.entry", db=db,
            before_state=None if before_note is None else {"note": before_note},
            after_state=None if after_note is None else {"note": after_note},
            driver_id=driver_id, work_date=work_date,
        )


async def _void_entry_state_field(
    period_id: int,
    company_id: int,
    driver_id: int,
    work_date: "date",
    user_id: int,
    db: AsyncConnection,
    *,
    clear_status: bool = False,
    clear_note: bool = False,
) -> None:
    """
    Update canonical entry-state row when a DailyStatus or DailyNote DraftLine is voided.

    Sets the corresponding field to NULL and recomputes IsVoided (TRUE only when
    both fields would then be empty).  No-op if the canonical row does not exist.
    """
    if not clear_status and not clear_note:
        return

    before_row = (await db.execute(text("""
        SELECT e.payrollperioddriverdayentrystateid, e.statuskeyid, e.notetext,
               sk.statuscode, sk.keyname AS statuslabel
        FROM payroll.payrollperioddriverdayentrystate e
        LEFT JOIN payroll.payrollstatuskeys sk ON sk.statuskeyid = e.statuskeyid
        WHERE e.companyid = :cid AND e.payrollperiodid = :pid
          AND e.driverid = :did AND e.workdate = :dt AND e.isvoided = FALSE
    """), {"cid": company_id, "pid": period_id, "did": driver_id, "dt": work_date})).mappings().first()

    if clear_status and clear_note:
        set_clause = "statuskeyid = NULL, notetext = NULL, isvoided = TRUE"
    elif clear_status:
        set_clause = "statuskeyid = NULL, isvoided = (notetext IS NULL OR notetext = '')"
    else:
        set_clause = "notetext = NULL, isvoided = (statuskeyid IS NULL)"

    await db.execute(
        text(f"""
            UPDATE payroll.payrollperioddriverdayentrystate
            SET    {set_clause},
                   updatedbyuserid = :uid,
                   updatedatutc    = NOW()
            WHERE  payrollperiodid = :pid
              AND  companyid       = :cid
              AND  driverid        = :did
              AND  workdate        = :dt
              AND  isvoided          = FALSE
        """),
        {"pid": period_id, "cid": company_id, "did": driver_id, "dt": work_date, "uid": user_id},
    )
    if before_row is None:
        return
    entry_id = int(before_row["payrollperioddriverdayentrystateid"])
    branch_id = int((await db.execute(text("""
        SELECT branchid FROM payroll.payrollperiods WHERE payrollperiodid = :pid
    """), {"pid": period_id})).scalar_one())
    if clear_status and before_row["statuskeyid"] is not None:
        await capture_period_audit_evidence(
            company_id=company_id, branch_id=branch_id, period_id=period_id,
            domain="STATUS_NOTE", action_code="STATUS_CLEARED",
            source_entity_type="PayrollPeriodDriverDayEntryState", source_entity_id=entry_id,
            user_id=user_id, required_permission_code="payroll.entry", db=db,
            before_state={
                "status_key_id": before_row["statuskeyid"], "status_code": before_row["statuscode"],
                "status_label": before_row["statuslabel"],
            }, after_state=None, driver_id=driver_id, work_date=work_date,
        )
    if clear_note and before_row["notetext"]:
        await capture_period_audit_evidence(
            company_id=company_id, branch_id=branch_id, period_id=period_id,
            domain="STATUS_NOTE", action_code="NOTE_CLEARED",
            source_entity_type="PayrollPeriodDriverDayEntryState", source_entity_id=entry_id,
            user_id=user_id, required_permission_code="payroll.entry", db=db,
            before_state={"note": before_row["notetext"]}, after_state=None,
            driver_id=driver_id, work_date=work_date,
        )


async def _enforce_status_key_limits(
    status_code: str,
    key_row: dict,
    period_id: int,
    company_id: int,
    work_date: "date",
    batch_driver_ids: list[int],
    all_batch_pairs: list[tuple],
    db: AsyncConnection,
) -> None:
    """
    Enforce the four optional usage limits for a status key within save_day_grid.

    Called once per unique status_code being SET in the batch, after all
    existence/active checks have passed.

    Counting rules:
      - Only active (non-Void) DailyStatus lines are counted.
      - The batch's own (driver_id, work_date) pairs are excluded from
        existing counts (those slots are being overwritten).
      - The batch's new contributions are counted separately and added.
      - Per-period:  total row count in period ≤ LimitUsesPerPeriod
      - Per-driver:  row count per driver in period ≤ LimitUsesPerDriver
      - Across-all-drivers: distinct driver count in period ≤ LimitUsesAcrossDrivers
      - Per-day:    row count on this work_date ≤ LimitUsesPerDay

    Raises HTTP 422 if any enabled limit would be exceeded; does not write.
    """
    any_limit = (
        key_row["limitusesperperiodenabled"]
        or key_row["limitusesperdriverenabled"]
        or key_row["limitusesacrossdriversenabled"]
        or key_row["limitusesperdayenabled"]
    )
    if not any_limit:
        return

    batch_count = len(batch_driver_ids)

    # Build exclusion clause: exclude all (driver_id, work_date) pairs in
    # this batch because those slots will be overwritten regardless.
    pair_params: dict = {}
    if all_batch_pairs:
        pair_conds = " OR ".join(
            f"(driverid = :ex_did_{i} AND workdate = :ex_wdt_{i})"
            for i in range(len(all_batch_pairs))
        )
        for i, (did, wdt) in enumerate(all_batch_pairs):
            pair_params[f"ex_did_{i}"] = did
            pair_params[f"ex_wdt_{i}"] = wdt
        excl = f"AND NOT ({pair_conds})"
    else:
        excl = ""

    base_params = {
        "period_id": period_id,
        "company_id": company_id,
        "status_code": status_code,
        **pair_params,
    }

    # ── Per-period limit ─────────────────────────────────────────────── #
    if key_row["limitusesperperiodenabled"]:
        limit = key_row["limitusesperperiod"]
        r = await db.execute(
            text(f"""
                SELECT COUNT(*) FROM payroll.payrolldraftlines
                WHERE payrollperiodid = :period_id
                  AND companyid      = :company_id
                  AND linetype       = 'DailyStatus'
                  AND status        != 'Void'
                  AND notes          = :status_code
                  {excl}
            """),
            base_params,
        )
        existing = r.scalar_one()
        if existing + batch_count > limit:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Status key '{status_code}' has reached its period limit "
                    f"of {limit} use(s). "
                    f"({existing} existing + {batch_count} in this save = {existing + batch_count})"
                ),
            )

    # ── Per-driver limit ─────────────────────────────────────────────── #
    if key_row["limitusesperdriverenabled"]:
        limit = key_row["limitusesperdriver"]
        for driver_id in batch_driver_ids:
            r = await db.execute(
                text(f"""
                    SELECT COUNT(*) FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :period_id
                      AND companyid      = :company_id
                      AND driverid       = :driver_id
                      AND linetype       = 'DailyStatus'
                      AND status        != 'Void'
                      AND notes          = :status_code
                      {excl}
                """),
                {"driver_id": driver_id, **base_params},
            )
            existing = r.scalar_one()
            if existing + 1 > limit:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Status key '{status_code}' has reached its per-driver limit "
                        f"of {limit} use(s) per period. "
                        f"Driver {driver_id} already has {existing} use(s) in this period."
                    ),
                )

    # ── Across-all-drivers limit (distinct drivers in period) ─────────── #
    if key_row["limitusesacrossdriversenabled"]:
        limit = key_row["limitusesacrossdrivers"]
        # Compute total distinct drivers after the batch by unioning:
        #   existing rows (excl. batch pairs) UNION batch drivers setting this key.
        # This correctly handles drivers who appear in both (counted once).
        if batch_driver_ids:
            new_selects = " UNION ALL ".join(
                f"SELECT :new_did_{i} AS driverid"
                for i in range(len(batch_driver_ids))
            )
            new_params = {f"new_did_{i}": d for i, d in enumerate(batch_driver_ids)}
            combined_query = f"""
                SELECT COUNT(DISTINCT driverid) FROM (
                    SELECT driverid FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :period_id
                      AND companyid      = :company_id
                      AND linetype       = 'DailyStatus'
                      AND status        != 'Void'
                      AND notes          = :status_code
                      {excl}
                    UNION ALL
                    {new_selects}
                ) combined
            """
        else:
            new_params = {}
            combined_query = f"""
                SELECT COUNT(DISTINCT driverid) FROM (
                    SELECT driverid FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :period_id
                      AND companyid      = :company_id
                      AND linetype       = 'DailyStatus'
                      AND status        != 'Void'
                      AND notes          = :status_code
                      {excl}
                ) combined
            """
        r = await db.execute(
            text(combined_query),
            {**base_params, **new_params},
        )
        total_distinct = r.scalar_one()
        if total_distinct > limit:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Status key '{status_code}' has reached its across-all-drivers limit "
                    f"of {limit} distinct driver(s) per period. "
                    f"Would have {total_distinct} distinct driver(s)."
                ),
            )

    # ── Per-day limit ─────────────────────────────────────────────────── #
    if key_row["limitusesperdayenabled"]:
        limit = key_row["limitusesperday"]
        r = await db.execute(
            text(f"""
                SELECT COUNT(*) FROM payroll.payrolldraftlines
                WHERE payrollperiodid = :period_id
                  AND companyid      = :company_id
                  AND workdate       = :work_date
                  AND linetype       = 'DailyStatus'
                  AND status        != 'Void'
                  AND notes          = :status_code
                  {excl}
            """),
            {"work_date": work_date, **base_params},
        )
        existing = r.scalar_one()
        if existing + batch_count > limit:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Status key '{status_code}' has reached its per-day limit "
                    f"of {limit} use(s) for {work_date}. "
                    f"({existing} existing + {batch_count} in this save = {existing + batch_count})"
                ),
            )
