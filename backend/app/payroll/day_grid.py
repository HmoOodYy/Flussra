"""
Day Grid — the daily driver/day entry grid for a single work date.

Extracted from app.payroll.service (Stage B4-16) as a dependency-closed leaf
module — no behavior change, pure relocation.

Owns one REST resource end to end, GET and POST on
/payroll/periods/{period_id}/day-grid:

  - get_day_grid       read model: assembles columns, status keys, driver
                       rows, canonical entry-state values and the summary
                       tallies into a DayGridResponse. Strictly read-only:
                       no writes, no locks, no mutation of any kind.
  - save_day_grid      batch write orchestrator: validates the whole payload
                       before any DML (all-or-nothing), then acquires locks,
                       drives Draft-line mutation, writes its own DailyStatus/
                       DailyNote rows, upserts canonical entry state, syncs
                       Status Payment, and returns get_day_grid's refreshed
                       response.
  - _canonical_aliases Day-Grid-private canonical/legacy line-type alias
                       lookup for existing-row discovery (save path only).
  - _parse_quantity    Day-Grid-private save-payload quantity parser; raises
                       422 before any DB write so the batch stays atomic.

Read and write are deliberately kept in one module rather than split: they
serve the same REST resource, share the same DayGridResponse contract (the
save path's return value IS the read path's output), and share the same
private policy for the ODA role guard, period loading, WorkDate validation,
canonical line-type vocabulary and snapshot-first daily-column discovery.
Splitting them would add a day_grid_write -> day_grid_read edge that fires on
every save without removing any real coupling.

The DailyStatus/DailyNote INSERT/UPDATE/void SQL inside save_day_grid is
Day-Grid-owned write policy, not Draft-line CRUD: it is deliberately NOT
routed through app.payroll.draft_line_mutation. Only the ordinary pay-item
values go through add_draft_line/update_draft_line/void_draft_line.

This module owns no domain it consumes. Draft-line mutation, Day Entry State,
Status Payment Sync, Period-Day Calendar, pay-item snapshot storage, the two
lock primitives and line audit all remain owned by their own modules and are
imported here. Drivers Off (app.payroll.off_drivers), Calculation
(app.payroll.period_calculation), Lifecycle (app.payroll.period_lifecycle)
and Finalization (app.payroll.finalization) are owned by their own modules
and are not referenced here at all.

Transaction ownership is above this module: app.dependencies.get_db opens
`async with engine.begin()`, so every write here — this module's own SQL, the
nested Draft-line mutations, the entry-state upsert, the Status Payment sync
and every audit row — shares the caller's ambient transaction. This module
starts no transaction and never commits or rolls back; a failure anywhere
unwinds the whole request.

app.payroll.router calls get_day_grid/save_day_grid directly. No
app.payroll.service facade is retained for them: after this stage service.py
has no remaining caller of either.
"""
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import (
    _build_in_clause,
    _check_any_permission,
    _check_permission,
)
from app.payroll import status_evidence
from app.payroll.day_entry_state import (
    _enforce_status_key_limits,
    _upsert_entry_state,
    _validate_status_key,
)
from app.payroll.draft_line_mutation import (
    add_draft_line,
    update_draft_line,
    void_draft_line,
)
from app.payroll.eligibility import (
    _assert_driver_eligible_for_workdate_via_snapshot,
    _is_snapshot_row_eligible_for_workdate,
    _period_has_driver_eligibility_snapshot,
)
from app.payroll.guards import _get_oda_own_driver_id
from app.payroll.line_audit import _write_line_audit
from app.payroll.line_type_vocabulary import (
    _INFORMATIONAL_ONLY,
    _LEGACY_TO_CANONICAL,
)
from app.payroll.mutation_lock import _lock_period_for_mutation
from app.payroll.pay_item_write_lock import _lock_pay_item_for_source_write
from app.payroll.period_day_calendar import _validate_period_work_date
from app.payroll.period_pay_item_snapshot import (
    _get_period_pay_item_snapshot,
    _period_has_pay_item_snapshot,
)
from app.payroll.period_read import get_period_by_id
from app.payroll.schemas import (
    SOURCE_ENTRY_STATUSES,
    DayGridColumn,
    DayGridLineValue,
    DayGridPeriod,
    DayGridResponse,
    DayGridRow,
    DayGridSaveRequest,
    DayGridStatusKey,
    DayGridSummary,
    DraftLineCreate,
    DraftLineUpdate,
)
from app.payroll.status_payment_sync import _sync_status_payment_for_entry_state

# ===========================================================================
# CP-1 — Day Grid
# ===========================================================================

def _canonical_aliases(canonical_code: str) -> list[str]:
    """
    Return the canonical code plus all legacy aliases that map to it.

    Example: _canonical_aliases("HOURS") -> ["HOURS", "Hours"]
    Used in DB lookups so we find legacy rows ("Hours") when the caller sends
    the canonical code ("HOURS").
    """
    legacy = [k for k, v in _LEGACY_TO_CANONICAL.items() if v == canonical_code]
    return [canonical_code] + legacy


def _parse_quantity(raw: str | None, code: str) -> "Decimal | None":
    """
    Parse a quantity string from the day-grid save payload.

    - None or empty string → None (means clear/void the line)
    - Valid numeric string  → Decimal
    - Non-numeric non-empty → raises HTTP 422

    Raises before any DB writes so the caller can validate all rows first.
    """
    from decimal import InvalidOperation as _InvalidOperation

    if raw is None or raw.strip() == "":
        return None  # caller interprets as clear/void
    try:
        return Decimal(raw.strip())
    except _InvalidOperation:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid quantity '{raw}' for pay item {code}. "
                "Must be a number (e.g. '8', '8.5')."
            ),
        )


async def get_day_grid(
    period_id: int,
    company_id: int,
    user_id: int,
    work_date: date | None,
    db: AsyncConnection,
) -> DayGridResponse:
    """
    Return the full daily entry grid for a single work_date within a period.

    If work_date is omitted (None), the backend resolves it:
      - today's date if today falls within the period
      - period.start_date otherwise

    - Columns: active Daily-scope pay items for the branch.
    - Status keys: active PayrollStatusKeys for the branch.
    - Rows: all eligible active drivers; populated with existing draft lines.
    """
    # ── ODA / Driver-role guard ───────────────────────────────────────────── #
    # Current Payroll is a manager/dispatcher screen, not a driver self-service
    # screen.  OwnDriverDataOnly users are blocked unconditionally — they must
    # not receive any payroll data (driver names, quantities, amounts, summary
    # counts, etc.).  This is a belt-and-suspenders guard on top of the
    # payroll.view / payroll.entry permission check below.
    own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)
    if own_driver_id is not None:
        raise HTTPException(
            status_code=403,
            detail="Current Payroll is not accessible to driver-role users.",
        )

    # ── Load and access-check the period ─────────────────────────────────── #
    period = await get_period_by_id(company_id, user_id, period_id, db)

    # ── P1 #1: resolve work_date when omitted ────────────────────────────── #
    if work_date is None:
        today = date.today()
        if period.start_date <= today <= period.end_date:
            work_date = today
        else:
            work_date = period.start_date

    # ── work_date bounds check (CP-2B: snapshot-aware) ───────────────────── #
    await _validate_period_work_date(
        period.payroll_period_id, work_date,
        period.start_date, period.end_date, db,
    )

    # ── Permission: payroll.view OR payroll.entry ─────────────────────────── #
    await _check_any_permission(
        company_id, user_id, period.branch_id, ["payroll.view", "payroll.entry"], db
    )

    branch_id = period.branch_id

    # ── Load active Daily columns for the branch ─────────────────────────── #
    # CP-2C: use snapshot when period has PayrollPeriodPayItems rows (post-0053
    # periods); fall back to live BranchPayItemConfig query for legacy periods.
    # Use _period_has_pay_item_snapshot to distinguish "post-0053 period with
    # zero active Daily items" from "legacy period with no snapshot" — both
    # would produce an empty snap_cols list, but only the latter should fall back.
    columns: list[DayGridColumn] = []
    if await _period_has_pay_item_snapshot(period.payroll_period_id, db):
        snap_cols = await _get_period_pay_item_snapshot(
            period.payroll_period_id, company_id, db, scope="Daily", active_only=True,
        )
        for row in snap_cols:
            code = row["payitemcode"]
            if code in _INFORMATIONAL_ONLY:
                continue
            columns.append(DayGridColumn(
                pay_item_code=code,
                label=row["displaylabel"] or row["payitemname"] or code,
                rate_behavior=row["ratebehavior"] or "None",
                is_time=(code in ("HOURS", "WAIT_TIME")) or (row["datatype"] == "Time"),
            ))
    else:
        cols_result = await db.execute(
            text("""
                SELECT pi.payitemcode, pi.payitemname, pi.ratebehavior, pi.datatype
                FROM   payroll.payitems pi
                LEFT JOIN payroll.branchpayitemconfig bpic
                       ON bpic.payitemid  = pi.payitemid
                      AND bpic.companyid  = :cid
                      AND bpic.branchid   = :bid
                      AND bpic.effectivefrom <= :dt
                      AND (bpic.effectiveto IS NULL OR bpic.effectiveto >= :dt)
                WHERE  (pi.companyid IS NULL OR pi.companyid = :cid)
                  AND  pi.itemscope  = 'Daily'
                  AND  pi.status    != 'Retired'
                  AND  COALESCE(bpic.isactive, pi.isdefaultbranchactive) = TRUE
                ORDER BY pi.sortorder NULLS LAST, pi.payitemcode
            """),
            {"cid": company_id, "bid": branch_id, "dt": work_date},
        )
        for row in cols_result.mappings().all():
            code = row["payitemcode"]
            if code in _INFORMATIONAL_ONLY:
                continue
            columns.append(DayGridColumn(
                pay_item_code=code,
                label=row["payitemname"] or code,
                rate_behavior=row["ratebehavior"] or "None",
                is_time=(code in ("HOURS", "WAIT_TIME")) or (row["datatype"] == "Time"),
            ))

    # ── Load status keys for the branch ──────────────────────────────────── #
    sk_result = await db.execute(
        text("""
            SELECT statuskeyid, statuscode, keyname, isoffreason, hoursvalue
            FROM   payroll.payrollstatuskeys
            WHERE  companyid = :cid
              AND  branchid  = :bid
              AND  isactive  = TRUE
            ORDER BY displayorder, keyname
        """),
        {"cid": company_id, "bid": branch_id},
    )
    status_keys: list[DayGridStatusKey] = []
    status_key_map: dict[str, DayGridStatusKey] = {}  # statuscode -> key
    for row in sk_result.mappings().all():
        sk = DayGridStatusKey(
            status_key_id=row["statuskeyid"],
            key_code=row["statuscode"],
            label=row["keyname"] or row["statuscode"],
            is_off_reason=bool(row["isoffreason"]),
            hours_value=row["hoursvalue"],
        )
        status_keys.append(sk)
        status_key_map[row["statuscode"]] = sk

    # ── Load eligible drivers (CP-2E: snapshot-aware) ────────────────────── #
    # For snapshotted periods use the canonical eligibility snapshot roster.
    # Legacy periods (no marker) fall back to the live EmploymentStatus query.
    _has_snapshot = await _period_has_driver_eligibility_snapshot(period_id, db)
    if _has_snapshot:
        # Load all snapshot rows for this period
        snap_rows_result = await db.execute(
            text("""
                SELECT driverid,
                       COALESCE(drivernamesnapshot, '') AS drivername,
                       COALESCE(drivercodesnapshot, '') AS drivercode,
                       eligibilityreasoncode,
                       hiredatesnapshot,
                       terminationdatesnapshot,
                       drivereffectivefromsnapshot,
                       drivereffectivetosnapshot,
                       iseligibleforperiod
                FROM   payroll.payrollperioddrivereligibility
                WHERE  payrollperiodid = :pid
                  AND  companyid       = :cid
                  AND  branchid        = :bid
                ORDER BY drivername
            """),
            {"pid": period_id, "cid": company_id, "bid": branch_id},
        )
        snap_rows_all = list(snap_rows_result.mappings().all())

        # Existing-source rescue: for ANY reason code, a driver out of their
        # date window is still shown if they have existing daily source on this
        # exact work_date (DraftLine or EntryState).  Collect the out-of-window
        # candidates first, then batch-check them.
        out_of_window_candidates = [
            r["driverid"] for r in snap_rows_all
            if not _is_snapshot_row_eligible_for_workdate(r, work_date)
        ]
        rescue_driver_ids_on_date: set[int] = set()
        if out_of_window_candidates:
            in_clause_rescue, in_params_rescue = _build_in_clause(
                out_of_window_candidates, "rescue"
            )
            rescue_result = await db.execute(
                text(f"""
                    SELECT DISTINCT driverid FROM (
                        SELECT driverid FROM payroll.payrolldraftlines
                        WHERE payrollperiodid = :pid AND workdate = :dt
                          AND status != 'Void'
                          AND driverid IN ({in_clause_rescue})
                        UNION
                        SELECT driverid FROM payroll.payrollperioddriverdayentrystate
                        WHERE payrollperiodid = :pid AND workdate = :dt
                          AND isvoided = FALSE
                          AND driverid IN ({in_clause_rescue})
                    ) src
                """),
                {"pid": period_id, "dt": work_date, **in_params_rescue},
            )
            rescue_driver_ids_on_date = {
                r["driverid"] for r in rescue_result.mappings().all()
            }

        # Filter snapshot rows to those eligible for this work_date
        # (primary: date-window check; secondary: existing-source rescue)
        drivers_raw = []
        for snap in snap_rows_all:
            if _is_snapshot_row_eligible_for_workdate(snap, work_date):
                pass  # window eligible — include
            elif snap["driverid"] in rescue_driver_ids_on_date:
                pass  # existing source rescue — include
            else:
                continue
            drivers_raw.append({
                "driverid":   snap["driverid"],
                "drivername": snap["drivername"],
                "drivercode": snap["drivercode"],
            })
        drivers = drivers_raw
    else:
        # Legacy fallback: live roster query
        drv_result = await db.execute(
            text("""
                SELECT d.driverid, e.fullname AS drivername, d.drivercode
                FROM   core.drivers   d
                JOIN   core.employees e ON e.employeeid = d.employeeid
                WHERE  d.companyid          = :cid
                  AND  d.branchid           = :bid
                  AND  e.employmentstatus   = 'Active'
                  AND  (
                           d.driverstatus = 'Active'
                        OR (d.driverstatus = 'Transferred'
                            AND d.effectiveto IS NOT NULL
                            AND d.effectiveto >= :dt)
                       )
                  AND  (e.hiredate IS NULL OR e.hiredate <= :dt)
                  AND  (e.terminationdate IS NULL OR e.terminationdate >= :dt)
                  AND  (d.effectivefrom IS NULL OR d.effectivefrom <= :dt)
                  AND  (d.effectiveto   IS NULL OR d.effectiveto   >= :dt)
                ORDER BY e.fullname
            """),
            {"cid": company_id, "bid": branch_id, "dt": work_date},
        )
        drivers = list(drv_result.mappings().all())
    driver_ids = [d["driverid"] for d in drivers]

    # ── Load existing draft lines for these drivers on this date ─────────── #
    # driver_id -> list of line rows
    lines_by_driver: dict[int, list[Any]] = {did: [] for did in driver_ids}

    if driver_ids:
        in_clause, in_params = _build_in_clause(driver_ids, "drv")
        dl_result = await db.execute(
            text(f"""
                SELECT dl.draftlineid, dl.driverid, dl.linetype,
                       dl.quantity, dl.calculatedamount, dl.needsmanagerreview,
                       dl.notes, dl.status
                FROM   payroll.payrolldraftlines dl
                WHERE  dl.payrollperiodid = :pid
                  AND  dl.workdate        = :dt
                  AND  dl.driverid        IN ({in_clause})
                  AND  dl.status         != 'Void'
            """),
            {"pid": period_id, "dt": work_date, **in_params},
        )
        for row in dl_result.mappings().all():
            did = row["driverid"]
            if did in lines_by_driver:
                lines_by_driver[did].append(dict(row))

    # ── CP-2D1: batch-load canonical entry-state rows for this date ───────── #
    canonical_by_driver: dict[int, dict] = {}
    if driver_ids:
        in_clause_ces, in_params_ces = _build_in_clause(driver_ids, "ces_drv")
        ces_result = await db.execute(
            text(f"""
                SELECT driverid, statuskeyid, notetext,
                       statuscodesnapshot, statuslabelsnapshot,
                       statusisoffreasonsnapshot, finalizedatutc
                FROM   payroll.payrollperioddriverdayentrystate
                WHERE  payrollperiodid = :pid
                  AND  workdate        = :dt
                  AND  driverid        IN ({in_clause_ces})
                  AND  isvoided          = FALSE
            """),
            {"pid": period_id, "dt": work_date, **in_params_ces},
        )
        for row in ces_result.mappings().all():
            canonical_by_driver[row["driverid"]] = dict(row)

    # Reverse lookup: StatusKeyID → DayGridStatusKey (for editable canonical path).
    status_key_id_map: dict[int, DayGridStatusKey] = {sk.status_key_id: sk for sk in status_keys}

    # Pre-load any deactivated keys referenced in canonical rows for editable periods.
    # (Deactivated keys are absent from status_key_id_map; fetch them in one batch.)
    deactivated_key_map: dict[int, dict] = {}
    deactivated_sk_ids = {
        row["statuskeyid"]
        for row in canonical_by_driver.values()
        if row.get("finalizedatutc") is None
        and row.get("statuskeyid") is not None
        and row["statuskeyid"] not in status_key_id_map
    }
    if deactivated_sk_ids:
        in_clause_dk, in_params_dk = _build_in_clause(list(deactivated_sk_ids), "dkid")
        dk_result = await db.execute(
            text(f"""
                SELECT statuskeyid, statuscode, keyname, isoffreason
                FROM   payroll.payrollstatuskeys
                WHERE  statuskeyid IN ({in_clause_dk})
            """),
            in_params_dk,
        )
        for row in dk_result.mappings().all():
            deactivated_key_map[row["statuskeyid"]] = dict(row)

    # ── Stage B3 Unit 8C-3: immutable Status evidence for Locked/Archived ──── #
    # After Submit, immutable calculation-snapshot Status evidence is the
    # historical authority for finalized periods. Locked/Archived rows below
    # must not use the EntryState freeze columns (StatusCodeSnapshot/
    # StatusLabelSnapshot/StatusIsOffReasonSnapshot/FinalizedAtUtc), current
    # mutable PayrollStatusKeys, or legacy DraftLine Status codes to determine
    # historical Status meaning. Draft/Open/InReview/Returned/Approved periods
    # are unaffected -- this block only runs for Locked/Archived.
    _is_finalized_period = period.status in ("Locked", "Archived")
    _status_evidence_by_driver: dict[int, dict[str, Any]] = {}
    _status_evidence_state: dict[str, str | None] | None = None
    # Set once evidence resolution below concludes UNAVAILABLE -- guards the
    # worked/off tally further down so an unreadable historical Status never
    # gets silently counted as "worked" (see the tally comment for why EMPTY
    # does not need the same guard).
    _status_evidence_unavailable = False
    if _is_finalized_period:
        _fin_snapshot, _fin_snapshot_avail = await status_evidence.resolve_finalized_snapshot(
            db, period_id=period.payroll_period_id, company_id=company_id, branch_id=branch_id,
        )
        if _fin_snapshot is None:
            # No usable snapshot provenance -- never fall back to another
            # snapshot or to mutable current state; evidence is unavailable.
            _status_evidence_state = _fin_snapshot_avail
        else:
            _fin_status_entries = await status_evidence.read_status_entries(
                db,
                snapshot_id=_fin_snapshot["payrollcalculationsnapshotid"],
                company_id=company_id,
                branch_id=branch_id,
                period_id=period.payroll_period_id,
            )
            _status_evidence_state = status_evidence.status_evidence_availability(
                _fin_snapshot, _fin_status_entries,
            )
            _status_evidence_by_driver = {
                entry["driver_id"]: entry
                for entry in _fin_status_entries
                if entry["work_date"] == work_date
            }
        _status_evidence_unavailable = _status_evidence_state["state"] == "UNAVAILABLE"

    # ── Build rows ────────────────────────────────────────────────────────── #
    col_codes = {c.pay_item_code for c in columns}
    rows: list[DayGridRow] = []
    total_hours = Decimal("0")
    total_miles = Decimal("0")
    gross_total = Decimal("0")
    needs_attention = 0
    worked_count = 0
    pto_count = 0
    off_count = 0

    for drv in drivers:
        did = drv["driverid"]
        drv_lines = lines_by_driver.get(did, [])

        values: dict[str, DayGridLineValue] = {}
        status_key_code: str | None = None
        notes_text: str | None = None
        sk_label: str | None = None
        is_off: bool = False

        # CP-2D1: canonical-first per-driver status/note read.
        # Falls back to DraftLines when no canonical row exists (legacy periods).
        ces_row = canonical_by_driver.get(did)
        if ces_row is not None:
            notes_text = ces_row.get("notetext")
            # Stage B3 Unit 8C-3: for Locked/Archived, Status meaning comes
            # only from immutable snapshot evidence (applied further below) --
            # never from these EntryState freeze columns or the live StatusKey.
            if not _is_finalized_period:
                if ces_row["finalizedatutc"] is not None:
                    # Locked period (pre-Solution-B path): use frozen snapshot values.
                    status_key_code = ces_row["statuscodesnapshot"]
                    sk_label = ces_row["statuslabelsnapshot"]
                    snap_off = ces_row["statusisoffreasonsnapshot"]
                    is_off = bool(snap_off) if snap_off is not None else False
                else:
                    # Editable period: live label/flags via StatusKeyID.
                    sk_id = ces_row.get("statuskeyid")
                    if sk_id is not None:
                        live_sk = status_key_id_map.get(sk_id)
                        if live_sk is not None:
                            status_key_code = live_sk.key_code
                            sk_label = live_sk.label
                            is_off = bool(live_sk.is_off_reason)
                        else:
                            dk = deactivated_key_map.get(sk_id)
                            if dk:
                                status_key_code = dk["statuscode"]
                                sk_label = dk["keyname"]
                                is_off = bool(dk["isoffreason"])

        for line in drv_lines:
            lt = line["linetype"]
            canonical = _LEGACY_TO_CANONICAL.get(lt, lt)

            if lt == "DailyStatus":
                if ces_row is None and not _is_finalized_period:
                    # Legacy fallback: status code stored in DraftLine notes.
                    # Not used for Locked/Archived -- see Stage B3 Unit 8C-3
                    # note above: legacy DraftLine Status must not be used to
                    # fabricate historical meaning.
                    status_key_code = line["notes"]
                continue
            if lt == "DailyNote":
                if ces_row is None:
                    # Legacy fallback: note text stored in DraftLine notes.
                    notes_text = line["notes"]
                continue

            if canonical in col_codes:
                qty = line["quantity"]
                calc = line["calculatedamount"]
                nmr = bool(line["needsmanagerreview"])
                values[canonical] = DayGridLineValue(
                    line_id=line["draftlineid"],
                    quantity=str(qty) if qty is not None else None,
                    calculated_amount=str(calc) if calc is not None else None,
                    needs_manager_review=nmr,
                )
                if nmr:
                    needs_attention += 1
                if canonical == "HOURS" and qty:
                    total_hours += Decimal(str(qty))
                if canonical == "MILES" and qty:
                    total_miles += Decimal(str(qty))
                if calc:
                    gross_total += Decimal(str(calc))

        # Legacy path: resolve label/is_off from status_key_map when no canonical row.
        if not _is_finalized_period and ces_row is None and status_key_code:
            sk_obj = status_key_map.get(status_key_code)
            if sk_obj is not None:
                sk_label = sk_obj.label
                is_off = bool(sk_obj.is_off_reason)

        # Stage B3 Unit 8C-3: for Locked/Archived, override whatever the
        # blocks above computed (they are guarded off above, but this stays
        # authoritative even if that guarding is ever loosened) -- Status
        # meaning comes only from immutable snapshot evidence for this
        # specific work_date, or is left unknown (None/False) when no
        # evidence entry exists for this driver/day, regardless of whether
        # the overall evidence state is AVAILABLE, EMPTY, or UNAVAILABLE.
        if _is_finalized_period:
            evidence_row = _status_evidence_by_driver.get(did)
            if evidence_row is not None:
                status_key_code = evidence_row["status_code"]
                sk_label = evidence_row["status_label"]
                is_off = bool(evidence_row["is_off_reason"])
            else:
                status_key_code = None
                sk_label = None
                is_off = False

        # Stage B3 Unit 8C-3 follow-up: when the overall Status evidence for
        # this Locked/Archived period is UNAVAILABLE, every row's is_off
        # above is a default (False), not a historical fact -- tallying it
        # into worked/off would silently present an unknown historical
        # Status as a confirmed "worked" day. Skip the tally entirely in
        # that case (leaves worked=off=0, distinguishable from a real
        # all-worked day via the top-level status_evidence field). EMPTY
        # does not need this guard: a captured snapshot with zero Status
        # rows is a positive historical fact that nobody had an off/PTO
        # Status that period, matching the same "no Status recorded means
        # worked" rule already applied to every other period status here.
        if _status_evidence_unavailable:
            pass
        elif is_off:
            off_count += 1
        else:
            worked_count += 1

        rows.append(DayGridRow(
            driver_id=did,
            driver_name=drv["drivername"],
            driver_code=drv.get("drivercode"),
            status_key=status_key_code,
            status_label=sk_label,
            is_off=is_off,
            notes=notes_text,
            values=values,
        ))

    # CP-2F: suppress gross_total for Draft (Prepared) periods — financials not available.
    _is_draft = period.status == "Draft"
    summary = DayGridSummary(
        total_drivers=len(drivers),
        worked=worked_count,
        pto=pto_count,
        off=off_count,
        total_hours=str(total_hours.quantize(Decimal("0.01"))),
        total_miles=str(total_miles.quantize(Decimal("0.01"))),
        gross_total=None if _is_draft else str(gross_total.quantize(Decimal("0.01"))),
        needs_attention=needs_attention,
        financials_available=not _is_draft,
    )

    grid_period = DayGridPeriod(
        period_id=period.payroll_period_id,
        period_name=period.period_name,
        start_date=period.start_date,
        end_date=period.end_date,
        pay_date=period.pay_date,
        status=period.status,
        branch_id=period.branch_id,
        branch_name=period.branch_name,
    )

    return DayGridResponse(
        period=grid_period,
        work_date=work_date,
        columns=columns,
        status_keys=status_keys,
        rows=rows,
        summary=summary,
        status_evidence=_status_evidence_state,
    )


async def save_day_grid(
    period_id: int,
    company_id: int,
    user_id: int,
    data: DayGridSaveRequest,
    db: AsyncConnection,
) -> DayGridResponse:
    """
    Batch-save a day grid.  For each row:
      - Upsert pay item lines (create / update / void-zero)
      - Upsert DailyStatus line (status_key stored in Notes)
      - Upsert DailyNote line  (notes text)

    All operations share the caller's transaction (no nested BEGIN).
    Returns the refreshed day-grid response.
    """
    # ── ODA / Driver-role guard ───────────────────────────────────────────── #
    # Same as get_day_grid: OwnDriverDataOnly users are unconditionally blocked
    # from writing to the day grid.  They must not be able to modify any payroll
    # data — not their own row, not anyone else's.
    own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)
    if own_driver_id is not None:
        raise HTTPException(
            status_code=403,
            detail="Current Payroll is not accessible to driver-role users.",
        )

    # ── Load and access-check the period ─────────────────────────────────── #
    period = await get_period_by_id(company_id, user_id, period_id, db)

    # CP-2F: Draft (Prepared) periods support operational day-grid entry.
    if period.status not in SOURCE_ENTRY_STATUSES:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Period is not editable (status: '{period.status}'). "
                "Only Open, Returned, or Prepared (Draft) periods accept entry."
            ),
        )

    work_date = data.work_date

    # CP-2B: snapshot-aware date validation
    await _validate_period_work_date(
        period.payroll_period_id, work_date,
        period.start_date, period.end_date, db,
    )

    # ── Permission: payroll.entry ─────────────────────────────────────────── #
    await _check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    branch_id = period.branch_id

    # ── Load active Daily columns for this branch/date ───────────────────── #
    # CP-2C: snapshot-first. Post-0053 periods use PayrollPeriodPayItems;
    # legacy periods fall back to live BranchPayItemConfig.
    # Use _period_has_pay_item_snapshot so a post-0053 period with zero active
    # Daily rows doesn't fall back to live config (an empty active set is the
    # correct answer — no codes should pass the validate step).
    if await _period_has_pay_item_snapshot(period.payroll_period_id, db):
        snap_active = await _get_period_pay_item_snapshot(
            period.payroll_period_id, company_id, db, scope="Daily", active_only=True,
        )
        active_col_codes: set[str] = {
            _LEGACY_TO_CANONICAL.get(r["payitemcode"], r["payitemcode"])
            for r in snap_active
        }
    else:
        _active_cols_result = await db.execute(
            text("""
                SELECT pi.payitemcode
                FROM   payroll.payitems pi
                LEFT JOIN payroll.branchpayitemconfig bpic
                       ON bpic.payitemid  = pi.payitemid
                      AND bpic.companyid  = :cid
                      AND bpic.branchid   = :bid
                      AND bpic.effectivefrom <= :dt
                      AND (bpic.effectiveto IS NULL OR bpic.effectiveto >= :dt)
                WHERE  (pi.companyid IS NULL OR pi.companyid = :cid)
                  AND  pi.itemscope  = 'Daily'
                  AND  pi.status    != 'Retired'
                  AND  COALESCE(bpic.isactive, pi.isdefaultbranchactive) = TRUE
            """),
            {"cid": company_id, "bid": branch_id, "dt": work_date},
        )
        active_col_codes: set[str] = {
            _LEGACY_TO_CANONICAL.get(r["payitemcode"], r["payitemcode"])
            for r in _active_cols_result.mappings().all()
        }

    # ── Phase 1: validate ALL inputs before any DB writes ────────────────── #
    # P1 #2: strict quantity parsing (non-numeric → 422 before writes)
    # P1 #3: status key validation (invalid/inactive → 422 before writes)
    # This preserves all-or-nothing atomicity.

    # Each row: (save_row, driver_id, parsed_values, validated_status_key, key_row)
    parsed_rows: list[tuple] = []

    for save_row in data.rows:
        driver_id = save_row.driver_id

        # CP-2E: use snapshot-based eligibility when available; legacy fallback otherwise.
        try:
            await _assert_driver_eligible_for_workdate_via_snapshot(
                company_id, branch_id, period.payroll_period_id, driver_id, work_date, db
            )
        except HTTPException:
            raise HTTPException(
                status_code=422,
                detail=f"Driver {driver_id} is not eligible for this branch or work date.",
            )

        # P1 #2: parse all quantities; reject unknown or branch-inactive codes
        # before any writes so the batch is rejected atomically.
        parsed_values: dict[str, Decimal | None] = {}
        for pay_item_code, raw_val in save_row.values.items():
            canonical = _LEGACY_TO_CANONICAL.get(pay_item_code, pay_item_code)
            if canonical not in active_col_codes:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Pay item '{canonical}' is not an active daily column "
                        "for this period branch and date."
                    ),
                )
            parsed_values[canonical] = _parse_quantity(raw_val, canonical)

        # P1 #3: validate status key — raises 422 for invalid/inactive.
        # Returns the full key row (with limit fields) or None when clearing.
        # CP-2D1: if the submitted code matches the existing DraftLine value,
        # the user is not changing the status — allow saves of deactivated keys
        # that were applied before the key was deactivated.
        allow_deactivated = False
        if save_row.status_key:
            existing_sk_q = await db.execute(
                text("""
                    SELECT notes FROM payroll.payrolldraftlines
                    WHERE  payrollperiodid = :pid AND workdate = :dt AND driverid = :did
                      AND  linetype = 'DailyStatus' AND status != 'Void'
                    LIMIT 1
                """),
                {"pid": period_id, "dt": work_date, "did": driver_id},
            )
            existing_sk_row = existing_sk_q.mappings().first()
            if (
                existing_sk_row is not None
                and existing_sk_row["notes"] == save_row.status_key.strip()
            ):
                allow_deactivated = True
        key_row = await _validate_status_key(
            save_row.status_key, company_id, branch_id, db,
            allow_deactivated=allow_deactivated,
        )
        validated_status_key = save_row.status_key  # None/blank = clear

        parsed_rows.append((save_row, driver_id, parsed_values, validated_status_key, key_row))

    # ── Phase 1b: status key usage limit enforcement ──────────────────── #
    # All existence/active checks passed. Now enforce configured usage limits
    # batch-wide so that intra-batch writes that would collectively exceed a
    # limit are caught before any DB write happens (all-or-nothing atomicity).
    all_batch_pairs: list[tuple] = [(r.driver_id, work_date) for r in data.rows]
    batch_by_code: dict[str, list[int]] = {}
    for _, drv_id, _, vsk, _ in parsed_rows:
        if vsk:
            batch_by_code.setdefault(vsk, []).append(drv_id)

    for status_code, batch_driver_ids in batch_by_code.items():
        code_key_row = next(
            krow
            for _, _, _, vsk, krow in parsed_rows
            if vsk == status_code and krow is not None
        )
        await _enforce_status_key_limits(
            status_code=status_code,
            key_row=code_key_row,
            period_id=period_id,
            company_id=company_id,
            work_date=work_date,
            batch_driver_ids=batch_driver_ids,
            all_batch_pairs=all_batch_pairs,
            db=db,
        )

    # ── Phase 2: execute DB writes (all validations passed) ──────────────── #
    # CP-0A lock ordering: pre-lock ALL distinct custom PayItems in the batch in
    # sorted (deterministic) order BEFORE acquiring the Period lock.
    #
    # Without batch pre-locking, a multi-item transaction can interleave:
    #   save_day_grid: PayItem A → Period → (tries) PayItem B
    #   deletion:      PayItem B → (tries) Period
    # → deadlock.
    #
    # With batch pre-locking the order is always:
    #   PayItem codes (sorted) → Period
    # which matches the deletion path (PayItem → Period), so no deadlock is possible.
    #
    # Re-locking an already-held row in the same transaction is a no-op in
    # PostgreSQL, so the per-line calls inside add_draft_line / update_draft_line
    # are safe duplicates of these batch locks.
    all_canonical_codes: set[str] = set()
    for _, _, pv, _, _ in parsed_rows:
        all_canonical_codes.update(pv.keys())
    all_canonical_codes.discard("DailyStatus")
    all_canonical_codes.discard("DailyNote")
    for code in sorted(all_canonical_codes):
        await _lock_pay_item_for_source_write(code, company_id, db, period_id=period.payroll_period_id)

    for save_row, driver_id, parsed_values, validated_status_key, key_row in parsed_rows:

        # ── Pay item lines ────────────────────────────────────────────────── #
        for canonical, qty in parsed_values.items():
            # Treat None (empty/blank) as zero (clear)
            effective_qty: Decimal = qty if qty is not None else Decimal("0")

            # P1 #5: look for existing line using canonical + all legacy aliases
            aliases = _canonical_aliases(canonical)
            lt_keys = {f"lt{i}": v for i, v in enumerate(aliases)}
            lt_in_clause = ", ".join(f":{k}" for k in lt_keys)
            existing_result = await db.execute(
                text(f"""
                    SELECT draftlineid, linetype FROM payroll.payrolldraftlines
                    WHERE  payrollperiodid = :pid
                      AND  workdate        = :dt
                      AND  driverid        = :did
                      AND  linetype        IN ({lt_in_clause})
                      AND  status         != 'Void'
                """),
                {"pid": period_id, "dt": work_date, "did": driver_id, **lt_keys},
            )
            existing_rows = existing_result.mappings().all()

            # P1 #5: detect duplicates (both "Hours" and "HOURS" rows exist)
            if len(existing_rows) > 1:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Duplicate payroll lines found for '{canonical}' "
                        f"(driver {driver_id}, {work_date}). Contact admin to resolve."
                    ),
                )
            existing = existing_rows[0] if existing_rows else None

            if effective_qty == 0 and existing:
                # P1 #4: route void through void_draft_line to write audit
                await void_draft_line(
                    period_id=period_id,
                    draft_line_id=existing["draftlineid"],
                    company_id=company_id,
                    user_id=user_id,
                    db=db,
                )
            elif effective_qty == 0 and not existing:
                # Skip: don't create zero rows
                continue
            elif effective_qty != 0 and not existing:
                # Create new line via add_draft_line (writes audit)
                await add_draft_line(
                    period_id=period_id,
                    company_id=company_id,
                    user_id=user_id,
                    data=DraftLineCreate(
                        driver_id=driver_id,
                        work_date=work_date,
                        line_type=canonical,
                        quantity=effective_qty,
                        source_type="Manual",
                    ),
                    db=db,
                )
            else:
                # Update existing line qty (writes audit)
                await update_draft_line(
                    period_id=period_id,
                    draft_line_id=existing["draftlineid"],
                    company_id=company_id,
                    user_id=user_id,
                    data=DraftLineUpdate(quantity=effective_qty),
                    db=db,
                )

        # ── Period lock for DailyStatus / DailyNote writes ───────────────── #
        # CP-0A: If this row had no pay-item writes (empty parsed_values or all
        # zeroes/voids), add_draft_line was never called so the period lock has
        # not been acquired yet.  Lock now before any DML.  If the period lock
        # was already acquired by a preceding add_draft_line call this is a
        # no-op (same transaction already holds the lock).
        await _lock_period_for_mutation(period_id, company_id, db)

        # ── DailyStatus upsert ────────────────────────────────────────────── #
        # P1 #4: write audit via _write_line_audit for all DailyStatus mutations
        status_val = validated_status_key  # None = clear
        existing_status = await db.execute(
            text("""
                SELECT draftlineid FROM payroll.payrolldraftlines
                WHERE  payrollperiodid = :pid
                  AND  workdate        = :dt
                  AND  driverid        = :did
                  AND  linetype        = 'DailyStatus'
                  AND  status         != 'Void'
                LIMIT 1
            """),
            {"pid": period_id, "dt": work_date, "did": driver_id},
        )
        es_row = existing_status.mappings().first()

        if status_val:
            if es_row:
                # Update notes (status code stored in notes)
                upd = await db.execute(
                    text(
                        "UPDATE payroll.payrolldraftlines SET notes = :n "
                        "WHERE draftlineid = :lid "
                        "  AND payrollperiodid = :period_id AND companyid = :company_id"
                    ),
                    {
                        "n": status_val,
                        "lid": es_row["draftlineid"],
                        "period_id": period_id,
                        "company_id": company_id,
                    },
                )
                if upd.rowcount:
                    await _write_line_audit(
                        db,
                        company_id=company_id,
                        branch_id=branch_id,
                        user_id=user_id,
                        line_id=es_row["draftlineid"],
                        action_code="DRAFT_LINE_UPDATED",
                        new_value={"status_key": status_val},
                    )
            else:
                # Insert new DailyStatus line
                ins_result = await db.execute(
                    text("""
                        INSERT INTO payroll.payrolldraftlines
                            (companyid, branchid, payrollperiodid, driverid,
                             workdate, linetype, linescope, quantity,
                             sourcetype, status, needsmanagerreview, notes, addedbyuserid)
                        VALUES
                            (:cid, :bid, :pid, :did,
                             :dt, 'DailyStatus', 'Daily', 0,
                             'Manual', 'Active', FALSE, :n, :uid)
                        RETURNING draftlineid
                    """),
                    {
                        "cid": company_id, "bid": branch_id, "pid": period_id,
                        "did": driver_id, "dt": work_date,
                        "n": status_val, "uid": user_id,
                    },
                )
                new_lid = ins_result.scalar_one()
                await _write_line_audit(
                    db,
                    company_id=company_id,
                    branch_id=branch_id,
                    user_id=user_id,
                    line_id=new_lid,
                    action_code="DRAFT_LINE_ADDED",
                    new_value={"line_type": "DailyStatus", "status_key": status_val},
                )
        elif es_row:
            # Clear status: void the line (with audit)
            upd = await db.execute(
                text(
                    "UPDATE payroll.payrolldraftlines SET status = 'Void' "
                    "WHERE draftlineid = :lid "
                    "  AND payrollperiodid = :period_id AND companyid = :company_id"
                ),
                {
                    "lid": es_row["draftlineid"],
                    "period_id": period_id,
                    "company_id": company_id,
                },
            )
            if upd.rowcount:
                await _write_line_audit(
                    db,
                    company_id=company_id,
                    branch_id=branch_id,
                    user_id=user_id,
                    line_id=es_row["draftlineid"],
                    action_code="DRAFT_LINE_VOIDED",
                    old_value={"line_type": "DailyStatus"},
                    new_value={"status": "Void"},
                )

        # ── DailyNote upsert ──────────────────────────────────────────────── #
        # P1 #4: write audit via _write_line_audit for all DailyNote mutations
        notes_val = save_row.notes  # may be None or ''
        existing_note = await db.execute(
            text("""
                SELECT draftlineid FROM payroll.payrolldraftlines
                WHERE  payrollperiodid = :pid
                  AND  workdate        = :dt
                  AND  driverid        = :did
                  AND  linetype        = 'DailyNote'
                  AND  status         != 'Void'
                LIMIT 1
            """),
            {"pid": period_id, "dt": work_date, "did": driver_id},
        )
        en_row = existing_note.mappings().first()

        if notes_val:
            if en_row:
                upd = await db.execute(
                    text(
                        "UPDATE payroll.payrolldraftlines SET notes = :n "
                        "WHERE draftlineid = :lid "
                        "  AND payrollperiodid = :period_id AND companyid = :company_id"
                    ),
                    {
                        "n": notes_val,
                        "lid": en_row["draftlineid"],
                        "period_id": period_id,
                        "company_id": company_id,
                    },
                )
                if upd.rowcount:
                    await _write_line_audit(
                        db,
                        company_id=company_id,
                        branch_id=branch_id,
                        user_id=user_id,
                        line_id=en_row["draftlineid"],
                        action_code="DRAFT_LINE_UPDATED",
                        new_value={"notes": notes_val},
                    )
            else:
                ins_result2 = await db.execute(
                    text("""
                        INSERT INTO payroll.payrolldraftlines
                            (companyid, branchid, payrollperiodid, driverid,
                             workdate, linetype, linescope, quantity,
                             sourcetype, status, needsmanagerreview, notes, addedbyuserid)
                        VALUES
                            (:cid, :bid, :pid, :did,
                             :dt, 'DailyNote', 'Daily', 0,
                             'Manual', 'Active', FALSE, :n, :uid)
                        RETURNING draftlineid
                    """),
                    {
                        "cid": company_id, "bid": branch_id, "pid": period_id,
                        "did": driver_id, "dt": work_date,
                        "n": notes_val, "uid": user_id,
                    },
                )
                new_lid2 = ins_result2.scalar_one()
                await _write_line_audit(
                    db,
                    company_id=company_id,
                    branch_id=branch_id,
                    user_id=user_id,
                    line_id=new_lid2,
                    action_code="DRAFT_LINE_ADDED",
                    new_value={"line_type": "DailyNote", "notes": notes_val},
                )
        elif en_row:
            upd = await db.execute(
                text(
                    "UPDATE payroll.payrolldraftlines SET status = 'Void' "
                    "WHERE draftlineid = :lid "
                    "  AND payrollperiodid = :period_id AND companyid = :company_id"
                ),
                {
                    "lid": en_row["draftlineid"],
                    "period_id": period_id,
                    "company_id": company_id,
                },
            )
            if upd.rowcount:
                await _write_line_audit(
                    db,
                    company_id=company_id,
                    branch_id=branch_id,
                    user_id=user_id,
                    line_id=en_row["draftlineid"],
                    action_code="DRAFT_LINE_VOIDED",
                    old_value={"line_type": "DailyNote"},
                    new_value={"status": "Void"},
                )

        # CP-2D1: upsert canonical entry-state after both DailyStatus/DailyNote writes.
        await _upsert_entry_state(
            company_id, branch_id, period_id, driver_id, work_date, user_id, db,
            status_key_id=key_row["statuskeyid"] if key_row else None,
            note_text=notes_val if notes_val else None,
        )

        # CP-2D2: sync STATUS_PAYMENT draft line from the updated entry state.
        # CP-2F: Draft (Prepared) is source-only — skip money derivation.
        if period.status == "Draft":
            continue
        await _sync_status_payment_for_entry_state(
            company_id=company_id,
            branch_id=branch_id,
            period_id=period_id,
            driver_id=driver_id,
            work_date=work_date,
            status_key_id=key_row["statuskeyid"] if key_row else None,
            user_id=user_id,
            db=db,
        )

    # Return the refreshed grid
    return await get_day_grid(
        period_id=period_id,
        company_id=company_id,
        user_id=user_id,
        work_date=data.work_date,
        db=db,
    )
