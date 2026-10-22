"""
Canonical driver/day Status and note state, Branch usage limits, and mutation evidence.

Status resolution from typed input belongs to the Company Status catalog
(app.settings.status_keys.resolve_status_input); Day Grid orchestrates entry batches.
"""
from datetime import date
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.audit_evidence import capture_period_audit_evidence


async def _upsert_entry_state(
    company_id: int,
    branch_id: int,
    period_id: int,
    driver_assignment_id: int,
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
          AND e.driverassignmentid = :did AND e.workdate = :dt
    """), {"cid": company_id, "pid": period_id, "did": driver_assignment_id, "dt": work_date})).mappings().first()

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
                 workdate, driverassignmentid, statuskeyid, notetext, isvoided,
                 createdbyuserid, updatedbyuserid, createdatutc, updatedatutc)
            VALUES
                (:cid, :bid, :pid, :day_id,
                 :dt, :did, :skid, :note, :voided,
                 :uid, :uid, NOW(), NOW())
            ON CONFLICT (payrollperiodid, driverassignmentid, workdate) DO UPDATE SET
                {conflict_clause}
        """),
        {
            "cid":    company_id,
            "bid":    branch_id,
            "pid":    period_id,
            "day_id": day_id,
            "dt":     work_date,
            "did":    driver_assignment_id,
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
          AND e.driverassignmentid = :did AND e.workdate = :dt
    """), {"cid": company_id, "pid": period_id, "did": driver_assignment_id, "dt": work_date})).mappings().one()

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
            driver_assignment_id=driver_assignment_id, work_date=work_date,
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
            driver_assignment_id=driver_assignment_id, work_date=work_date,
        )


async def lock_selected_status_keys(
    selections: list[tuple[int, bool]], company_id: int, db: AsyncConnection,
) -> None:
    """Lock the Statuses a batch selects FOR SHARE, in key order, and recheck that a Status
    resolved as active still is. ``selections`` are (StatusKeyID, resolved as active).

    A Status that was already inactive when it was resolved is the re-save of a selection the
    day already holds and needs no recheck.
    """
    resolved_active = {key_id for key_id, active in selections if active}
    key_ids = sorted({key_id for key_id, _ in selections})
    if not key_ids:
        return
    rows = (await db.execute(
        text("""
            SELECT statuskeyid, isactive, keyname
            FROM   payroll.payrollstatuskeys
            WHERE  companyid = :cid AND statuskeyid = ANY(:ids)
            ORDER  BY statuskeyid
            FOR SHARE
        """),
        {"cid": company_id, "ids": key_ids},
    )).mappings().all()
    for row in rows:
        if row["statuskeyid"] in resolved_active and not row["isactive"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "STATUS_NOT_FOUND",
                    "message": f"'{row['keyname']}' is no longer an active Status.",
                },
            )


async def _load_branch_limits(
    status_key_id: int, branch_id: int, company_id: int, db: AsyncConnection,
) -> dict | None:
    """The Branch's usage limits of one Status, or None when the Branch has none."""
    row = (await db.execute(
        text("""
            SELECT limitusesperperiodenabled, limitusesperperiod,
                   limitusesperdriverenabled, limitusesperdriver,
                   limitusesacrossdriversenabled, limitusesacrossdrivers,
                   limitusesperdayenabled, limitusesperday
            FROM   payroll.payrollstatuskeybranchlimits
            WHERE  branchid = :bid AND statuskeyid = :skid AND companyid = :cid
        """),
        {"bid": branch_id, "skid": status_key_id, "cid": company_id},
    )).mappings().first()
    return None if row is None else dict(row)


async def _enforce_status_key_limits(
    key_row: dict,
    branch_id: int,
    period_id: int,
    company_id: int,
    work_date: "date",
    batch_driver_assignment_ids: list[int],
    all_batch_pairs: list[tuple],
    db: AsyncConnection,
) -> None:
    """
    Enforce the Branch's four optional usage limits of one Status within save_day_grid.

    Called once per unique Status being SET in the batch, after resolution and the Period
    lock. The limits are the Branch's (a missing row means no limits); a Period belongs to
    one Branch, so the counts are Branch-specific.

    Counting rules:
      - Only canonical, non-voided PayrollPeriodDriverDayEntryState selections of this Status
        in this Period are counted from canonical entry state only.
      - The batch's own (driver_assignment_id, work_date) pairs are excluded from the
        existing counts (those slots are being overwritten, whatever they held).
      - The batch's new contributions are counted separately and added.
      - Per-period:  total row count in period <= LimitUsesPerPeriod
      - Per-driver:  row count per driver in period <= LimitUsesPerDriver
      - Across-all-drivers: distinct driver count in period <= LimitUsesAcrossDrivers
      - Per-day:    row count on this work_date <= LimitUsesPerDay

    Raises HTTP 422 if any enabled limit would be exceeded; does not write.
    """
    limits = await _load_branch_limits(key_row["statuskeyid"], branch_id, company_id, db)
    if limits is None:
        return
    name = key_row["keyname"]
    batch_count = len(batch_driver_assignment_ids)

    # Exclude every (driver_assignment_id, work_date) pair of this batch: those slots are
    # overwritten regardless of what they currently hold.
    pair_params: dict = {}
    if all_batch_pairs:
        pair_conds = " OR ".join(
            f"(driverassignmentid = :ex_did_{i} AND workdate = :ex_wdt_{i})"
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
        "status_key_id": key_row["statuskeyid"],
        **pair_params,
    }
    canonical = """
        FROM payroll.payrollperioddriverdayentrystate
        WHERE payrollperiodid = :period_id
          AND companyid       = :company_id
          AND statuskeyid     = :status_key_id
          AND isvoided        = FALSE
    """

    # ── Per-period limit ─────────────────────────────────────────────── #
    if limits["limitusesperperiodenabled"]:
        limit = limits["limitusesperperiod"]
        existing = (await db.execute(
            text(f"SELECT COUNT(*) {canonical} {excl}"), base_params,
        )).scalar_one()
        if existing + batch_count > limit:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Status key '{name}' has reached its period limit "
                    f"of {limit} use(s). "
                    f"({existing} existing + {batch_count} in this save = {existing + batch_count})"
                ),
            )

    # ── Per-driver limit ─────────────────────────────────────────────── #
    if limits["limitusesperdriverenabled"]:
        limit = limits["limitusesperdriver"]
        for driver_assignment_id in batch_driver_assignment_ids:
            existing = (await db.execute(
                text(f"SELECT COUNT(*) {canonical} AND driverassignmentid = :driver_assignment_id {excl}"),
                {"driver_assignment_id": driver_assignment_id, **base_params},
            )).scalar_one()
            if existing + 1 > limit:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Status key '{name}' has reached its per-driver limit "
                        f"of {limit} use(s) per period. "
                        f"Driver {driver_assignment_id} already has {existing} use(s) in this period."
                    ),
                )

    # ── Across-all-drivers limit (distinct drivers in period) ─────────── #
    if limits["limitusesacrossdriversenabled"]:
        limit = limits["limitusesacrossdrivers"]
        # Distinct drivers after the batch: the existing selections (excluding the batch's
        # slots) unioned with the batch's drivers, so a driver in both counts once.
        drivers_after = {
            int(r) for r in (await db.execute(
                text(f"SELECT DISTINCT driverassignmentid {canonical} {excl}"), base_params,
            )).scalars().all()
        } | set(batch_driver_assignment_ids)
        if len(drivers_after) > limit:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Status key '{name}' has reached its across-all-drivers limit "
                    f"of {limit} distinct driver(s) per period. "
                    f"Would have {len(drivers_after)} distinct driver(s)."
                ),
            )

    # ── Per-day limit ─────────────────────────────────────────────────── #
    if limits["limitusesperdayenabled"]:
        limit = limits["limitusesperday"]
        existing = (await db.execute(
            text(f"SELECT COUNT(*) {canonical} AND workdate = :work_date {excl}"),
            {"work_date": work_date, **base_params},
        )).scalar_one()
        if existing + batch_count > limit:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Status key '{name}' has reached its per-day limit "
                    f"of {limit} use(s) for {work_date}. "
                    f"({existing} existing + {batch_count} in this save = {existing + batch_count})"
                ),
            )
