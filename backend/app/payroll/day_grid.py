"""
Day Grid — the daily driver/day entry grid for a single work date.

Owns one REST resource end to end, GET and POST on
/payroll/periods/{period_id}/day-grid:

  - get_day_grid       read model: assembles the columns, status keys, driver rows,
                       canonical entry-state values and the summary tallies into a
                       DayGridResponse. Strictly read-only.
  - save_day_grid      batch write orchestrator: validates the whole payload before
                       any DML (all-or-nothing), then acquires locks, drives source-line
                       mutation, writes its own DailyStatus/DailyNote rows, upserts
                       canonical entry state, syncs Status Payment, and returns
                       get_day_grid's refreshed response.
  - _parse_quantity    save-payload quantity parser; raises 422 before any DB write so
                       the batch stays atomic.

Columns are the period's ACTIVE definitions (app.payroll.period_definitions) and both
the response values and the save payload are keyed by PayrollPeriodDefinitionID. A
definition's code and name are display metadata only: they never route a save, classify
a total or select a rate. Money shown here is derived live
(app.payroll.live_source_calculation); no stored amount is read.

The DailyStatus/DailyNote INSERT/UPDATE/void SQL inside save_day_grid is Day-Grid-owned
write policy, deliberately NOT routed through app.payroll.draft_line_mutation. Only the
ordinary pay-definition quantities go through add_draft_line/update_draft_line/
void_draft_line.

Transaction ownership is above this module: app.dependencies.get_db opens
`async with engine.begin()`, so every write here shares the caller's ambient
transaction; a failure anywhere unwinds the whole request.
"""
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.permissions import check_any_permission, check_permission
from app.access.policy import require_non_driver_subject
from app.company_currency import lock_and_get_company_currency_for_monetary_write
from app.db.sql import build_in_clause
from app.payroll import status_evidence
from app.payroll.day_entry_state import (
    _enforce_status_key_limits,
    _upsert_entry_state,
    _validate_status_key,
)
from app.payroll.definition_calculation import is_method_operational, quantity_error
from app.payroll.draft_line_mutation import (
    add_draft_line,
    update_draft_line,
    void_draft_line,
)
from app.payroll.eligibility import (
    _assert_driver_eligible_for_workdate_via_snapshot,
    _is_snapshot_row_eligible_for_workdate,
    _lifecycle_eligible_assignment_ids,
    require_period_eligibility_snapshot,
)
from app.payroll.line_audit import _write_line_audit
from app.payroll.live_source_calculation import load_live_source_lines
from app.payroll.mutation_lock import _lock_period_for_mutation
from app.payroll.period_day_calendar import _validate_period_work_date, resolve_period_work_date
from app.payroll.period_definitions import list_period_definitions
from app.payroll.period_read import get_period_by_id
from app.payroll.schemas import (
    SOURCE_ENTRY_STATUSES,
    DayGridColumn,
    DayGridLineValue,
    DayGridPeriod,
    DayGridQuantityTotal,
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

def _parse_quantity(raw: str | None, label: str) -> "Decimal | None":
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
                f"Invalid quantity '{raw}' for {label}. "
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
    await require_non_driver_subject(company_id, user_id, db)

    # ── Load and access-check the period ─────────────────────────────────── #
    period = await get_period_by_id(company_id, user_id, period_id, db)

    # ── P1 #1: resolve work_date when omitted ────────────────────────────── #
    work_date = await resolve_period_work_date(
        company_id, period.branch_id, period.start_date, period.end_date, work_date, db,
    )

    # ── work_date bounds check (CP-2B: snapshot-aware) ───────────────────── #
    await _validate_period_work_date(
        period.payroll_period_id, work_date,
        period.start_date, period.end_date, db,
    )

    # ── Permission: payroll.view OR payroll.entry ─────────────────────────── #
    await check_any_permission(
        company_id, user_id, period.branch_id, ["payroll.view", "payroll.entry"], db
    )

    branch_id = period.branch_id

    # ── Columns: the period's ACTIVE definitions, in their frozen order ───── #
    period_definitions = await list_period_definitions(
        period.payroll_period_id, company_id, db, active_only=True,
    )
    columns: list[DayGridColumn] = [
        DayGridColumn(
            payroll_period_definition_id=d.payroll_period_definition_id,
            pay_definition_id=d.pay_definition_id,
            definition_code=d.code,
            label=d.name,
            input_type=d.input_type,
            unit=d.unit,
            calculation_method=d.calculation_method,
        )
        for d in period_definitions
    ]

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

    # ── Load eligible drivers from the required canonical snapshot ───────── #
    await require_period_eligibility_snapshot(period_id, company_id, branch_id, db)
    # Load all snapshot rows for this period
    snap_rows_result = await db.execute(
        text("""
            SELECT pde.driverassignmentid,
                   (SELECT a.driverid FROM core.driverassignments a
                     WHERE a.driverassignmentid = pde.driverassignmentid) AS permanentdriverid,
                   COALESCE(drivernamesnapshot, '') AS drivername,
                   COALESCE(drivernumbersnapshot, '') AS drivernumber,
                   eligibilityreasoncode,
                   hiredatesnapshot,
                   terminationdatesnapshot,
                   eligibleworkdates,
                   iseligibleforperiod
            FROM   payroll.payrollperioddrivereligibility pde
            WHERE  payrollperiodid = :pid
              AND  companyid       = :cid
              AND  branchid        = :bid
            ORDER BY drivername
        """),
        {"pid": period_id, "cid": company_id, "bid": branch_id},
    )
    snap_rows_all = list(snap_rows_result.mappings().all())
    snapshot_driver_assignment_ids = [r["driverassignmentid"] for r in snap_rows_all]
    lifecycle_eligible_ids = await _lifecycle_eligible_assignment_ids(
        period_id, company_id, branch_id, work_date, snapshot_driver_assignment_ids, db,
    )

    # Existing-source rescue: for ANY reason code, a driver out of their
    # date window is still shown if they have existing daily source on this
    # exact work_date (DraftLine or EntryState).  Collect the out-of-window
    # candidates first, then batch-check them.
    out_of_window_candidates = [
        r["driverassignmentid"] for r in snap_rows_all
        if (
            not _is_snapshot_row_eligible_for_workdate(r, work_date)
            or r["driverassignmentid"] not in lifecycle_eligible_ids
        )
    ]
    rescue_driver_assignment_ids_on_date: set[int] = set()
    if out_of_window_candidates:
        in_clause_rescue, in_params_rescue = build_in_clause(
            out_of_window_candidates, "rescue"
        )
        rescue_result = await db.execute(
            text(f"""
                SELECT DISTINCT driverassignmentid FROM (
                    SELECT driverassignmentid FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :pid AND workdate = :dt
                      AND status != 'Void'
                      AND driverassignmentid IN ({in_clause_rescue})
                    UNION
                    SELECT driverassignmentid FROM payroll.payrollperioddriverdayentrystate
                    WHERE payrollperiodid = :pid AND workdate = :dt
                      AND isvoided = FALSE
                      AND driverassignmentid IN ({in_clause_rescue})
                ) src
            """),
            {"pid": period_id, "dt": work_date, **in_params_rescue},
        )
        rescue_driver_assignment_ids_on_date = {
            r["driverassignmentid"] for r in rescue_result.mappings().all()
        }

    # A frozen window must still fit current lifecycle boundaries.
    # Existing exact-date source remains visible through the rescue path.
    drivers = [
        dict(snap)
        for snap in snap_rows_all
        if (
            _is_snapshot_row_eligible_for_workdate(snap, work_date)
            and snap["driverassignmentid"] in lifecycle_eligible_ids
        ) or snap["driverassignmentid"] in rescue_driver_assignment_ids_on_date
    ]
    driver_assignment_ids = [d["driverassignmentid"] for d in drivers]

    # ── Load existing draft lines for these drivers on this date ─────────── #
    # driver_assignment_id -> list of line rows
    lines_by_driver: dict[int, list[Any]] = {did: [] for did in driver_assignment_ids}

    if driver_assignment_ids:
        in_clause, in_params = build_in_clause(driver_assignment_ids, "drv")
        dl_result = await db.execute(
            text(f"""
                SELECT dl.draftlineid, dl.driverassignmentid, dl.linetype,
                       dl.payrollperioddefinitionid,
                       dl.quantity, dl.notes, dl.status
                FROM   payroll.payrolldraftlines dl
                WHERE  dl.payrollperiodid = :pid
                  AND  dl.workdate        = :dt
                  AND  dl.driverassignmentid        IN ({in_clause})
                  AND  dl.status         != 'Void'
            """),
            {"pid": period_id, "dt": work_date, **in_params},
        )
        for row in dl_result.mappings().all():
            did = row["driverassignmentid"]
            if did in lines_by_driver:
                lines_by_driver[did].append(dict(row))

    # ── CP-2D1: batch-load canonical entry-state rows for this date ───────── #
    canonical_by_driver: dict[int, dict] = {}
    if driver_assignment_ids:
        in_clause_ces, in_params_ces = build_in_clause(driver_assignment_ids, "ces_drv")
        ces_result = await db.execute(
            text(f"""
                SELECT driverassignmentid, statuskeyid, notetext,
                       statuscodesnapshot, statuslabelsnapshot,
                       statusisoffreasonsnapshot, finalizedatutc
                FROM   payroll.payrollperioddriverdayentrystate
                WHERE  payrollperiodid = :pid
                  AND  workdate        = :dt
                  AND  driverassignmentid        IN ({in_clause_ces})
                  AND  isvoided          = FALSE
            """),
            {"pid": period_id, "dt": work_date, **in_params_ces},
        )
        for row in ces_result.mappings().all():
            canonical_by_driver[row["driverassignmentid"]] = dict(row)

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
        in_clause_dk, in_params_dk = build_in_clause(list(deactivated_sk_ids), "dkid")
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
                entry["driver_assignment_id"]: entry
                for entry in _fin_status_entries
                if entry["work_date"] == work_date
            }
        _status_evidence_unavailable = _status_evidence_state["state"] == "UNAVAILABLE"

    # ── Live money for the ordinary lines of this date ────────────────────── #
    # Draft (Prepared) periods are source-only: no money is derived or shown.
    column_ids = {c.payroll_period_definition_id for c in columns}
    live_by_line: dict[int, Any] = {}
    if period.status != "Draft" and driver_assignment_ids:
        live_by_line = {
            line.draft_line_id: line
            for line in await load_live_source_lines(
                period_id, company_id, db, driver_assignment_ids=driver_assignment_ids, work_date=work_date,
            )
        }

    # ── Build rows ────────────────────────────────────────────────────────── #
    rows: list[DayGridRow] = []
    quantity_totals: dict[int, Decimal] = {}
    gross_total = Decimal("0")
    needs_attention = 0
    worked_count = 0
    pto_count = 0
    off_count = 0

    for drv in drivers:
        did = drv["driverassignmentid"]
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

            ppd_id = line["payrollperioddefinitionid"]
            if ppd_id is not None and ppd_id in column_ids:
                qty = line["quantity"]
                live = live_by_line.get(line["draftlineid"])
                calc = live.calculation if live is not None else None
                attention = calc.needs_attention if calc is not None else False
                values[str(ppd_id)] = DayGridLineValue(
                    line_id=line["draftlineid"],
                    quantity=str(qty) if qty is not None else None,
                    calculated_amount=(
                        str(calc.amount) if calc is not None and calc.amount is not None else None
                    ),
                    needs_manager_review=attention,
                    calculation_status=calc.status.value if calc is not None else None,
                )
                if attention:
                    needs_attention += 1
                if qty:
                    quantity_totals[ppd_id] = (
                        quantity_totals.get(ppd_id, Decimal("0")) + Decimal(str(qty))
                    )
                if calc is not None and calc.amount is not None:
                    gross_total += calc.amount

        # Legacy path: resolve label/is_off from status_key_map when no canonical row.
        if not _is_finalized_period and ces_row is None and status_key_code:
            sk_obj = status_key_map.get(status_key_code)
            if sk_obj is not None:
                sk_label = sk_obj.label
                is_off = bool(sk_obj.is_off_reason)

        # For Locked/Archived, override whatever the
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

        # When the overall Status evidence for
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
            driver_assignment_id=did,
            driver_id=drv["permanentdriverid"],
            driver_name=drv["drivername"],
            driver_number=drv.get("drivernumber"),
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
        quantity_totals=[
            DayGridQuantityTotal(
                payroll_period_definition_id=column.payroll_period_definition_id,
                quantity=str(quantity_totals.get(
                    column.payroll_period_definition_id, Decimal("0"))),
            )
            for column in columns
        ],
        gross_total=None if _is_draft else str(gross_total),
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
    await require_non_driver_subject(company_id, user_id, db)

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
    await check_permission(company_id, user_id, period.branch_id, "payroll.entry", db)

    currency = await lock_and_get_company_currency_for_monetary_write(company_id, db, required=False)
    branch_id = period.branch_id

    # ── Active definitions: the only identities a save may name ──────────── #
    active_definitions = {
        d.payroll_period_definition_id: d
        for d in await list_period_definitions(
            period.payroll_period_id, company_id, db, active_only=True)
    }

    # ── Phase 1: validate ALL inputs before any DB writes ────────────────── #
    # P1 #2: strict quantity parsing (non-numeric → 422 before writes)
    # P1 #3: status key validation (invalid/inactive → 422 before writes)
    # This preserves all-or-nothing atomicity.

    async def _assert_row_eligible(driver_assignment_id: int) -> None:
        try:
            await _assert_driver_eligible_for_workdate_via_snapshot(
                company_id, branch_id, period.payroll_period_id, driver_assignment_id, work_date, db
            )
        except HTTPException:
            raise HTTPException(
                status_code=422,
                detail=f"Driver {driver_assignment_id} is not eligible for this branch or work date.",
            )

    # Each row: (save_row, driver_assignment_id, parsed_values, validated_status_key, key_row)
    parsed_rows: list[tuple] = []

    for save_row in data.rows:
        driver_assignment_id = save_row.driver_assignment_id

        # CP-2E: snapshot-based eligibility, evaluated early for a fast 422. The write is
        # authorized again under the period lock below.
        await _assert_row_eligible(driver_assignment_id)

        # Parse and validate every quantity before any write so the batch is
        # rejected atomically. A key is a PayrollPeriodDefinitionID, never a code.
        parsed_values: dict[int, Decimal | None] = {}
        for key, raw_val in save_row.values.items():
            try:
                definition = active_definitions.get(int(key))
            except (TypeError, ValueError):
                definition = None
            if definition is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"'{key}' is not an active pay definition column for this period."
                    ),
                )
            if not is_method_operational(
                definition.calculation_method, definition.calculation_method_version):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "CALCULATION_METHOD_NOT_READY",
                        "message": f"'{definition.name}' uses a calculation method that "
                                   "is not operational yet.",
                    },
                )
            quantity = _parse_quantity(raw_val, definition.name)
            if quantity is not None:
                problem = quantity_error(definition.input_type, quantity)
                if problem is not None:
                    raise HTTPException(
                        status_code=422, detail=f"{definition.name}: {problem}")
            parsed_values[definition.payroll_period_definition_id] = quantity

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
                    WHERE  payrollperiodid = :pid AND workdate = :dt AND driverassignmentid = :did
                      AND  linetype = 'DailyStatus' AND status != 'Void'
                    LIMIT 1
                """),
                {"pid": period_id, "dt": work_date, "did": driver_assignment_id},
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

        parsed_rows.append((save_row, driver_assignment_id, parsed_values, validated_status_key, key_row))

    # ── Phase 2: execute DB writes (all validations passed) ──────────────── #
    # Period source-mutation lock (also rechecks the Period is still editable).
    # It is taken before the Status limit check, so
    # the count-then-write below is serialized per Period and no longer relies
    # on any Company-level lock.
    await _lock_period_for_mutation(period_id, company_id, db)

    # Re-authorize every row against the roster and assignment windows as they are under
    # the period lock: a transfer reschedule/cancel or termination holds the same lock, so
    # a boundary change either waited for this save or committed before it and is seen
    # here. (The currency lock above precedes the period lock in the global lock order,
    # so the early check cannot simply move below it.)
    for save_row in data.rows:
        await _assert_row_eligible(save_row.driver_assignment_id)

    # ── Status key usage limit enforcement (under the Period lock) ─────── #
    # All existence/active checks passed. Enforce configured usage limits
    # batch-wide so that intra-batch writes that would collectively exceed a
    # limit are caught before any DB write happens (all-or-nothing atomicity).
    # The counts must be read only after the Period lock is held, so a
    # concurrent Status save for this Period has already committed or waits.
    all_batch_pairs: list[tuple] = [(r.driver_assignment_id, work_date) for r in data.rows]
    batch_by_code: dict[str, list[int]] = {}
    for _, drv_id, _, vsk, _ in parsed_rows:
        if vsk:
            batch_by_code.setdefault(vsk, []).append(drv_id)

    for status_code, batch_driver_assignment_ids in batch_by_code.items():
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
            batch_driver_assignment_ids=batch_driver_assignment_ids,
            all_batch_pairs=all_batch_pairs,
            db=db,
        )

    for save_row, driver_assignment_id, parsed_values, validated_status_key, key_row in parsed_rows:

        # ── Ordinary pay lines, by period definition ─────────────────────── #
        for ppd_id, qty in parsed_values.items():
            # Treat None (empty/blank) as zero (clear)
            effective_qty: Decimal = qty if qty is not None else Decimal("0")

            existing_result = await db.execute(
                text("""
                    SELECT draftlineid FROM payroll.payrolldraftlines
                    WHERE  payrollperiodid = :pid
                      AND  workdate        = :dt
                      AND  driverassignmentid        = :did
                      AND  payrollperioddefinitionid = :ppd
                      AND  status         != 'Void'
                """),
                {"pid": period_id, "dt": work_date, "did": driver_assignment_id, "ppd": ppd_id},
            )
            existing = existing_result.mappings().first()

            if effective_qty == 0 and existing:
                await void_draft_line(
                    period_id=period_id,
                    draft_line_id=existing["draftlineid"],
                    company_id=company_id,
                    user_id=user_id,
                    db=db,
                )
            elif effective_qty == 0 and not existing:
                continue  # don't create zero rows
            elif not existing:
                await add_draft_line(
                    period_id=period_id,
                    company_id=company_id,
                    user_id=user_id,
                    data=DraftLineCreate(
                        driver_assignment_id=driver_assignment_id,
                        work_date=work_date,
                        payroll_period_definition_id=ppd_id,
                        quantity=effective_qty,
                        source_type="Manual",
                    ),
                    db=db,
                )
            else:
                await update_draft_line(
                    period_id=period_id,
                    draft_line_id=existing["draftlineid"],
                    company_id=company_id,
                    user_id=user_id,
                    data=DraftLineUpdate(quantity=effective_qty),
                    db=db,
                )

        # ── DailyStatus upsert ────────────────────────────────────────────── #
        # P1 #4: write audit via _write_line_audit for all DailyStatus mutations
        status_val = validated_status_key  # None = clear
        existing_status = await db.execute(
            text("""
                SELECT draftlineid FROM payroll.payrolldraftlines
                WHERE  payrollperiodid = :pid
                  AND  workdate        = :dt
                  AND  driverassignmentid        = :did
                  AND  linetype        = 'DailyStatus'
                  AND  status         != 'Void'
                LIMIT 1
            """),
            {"pid": period_id, "dt": work_date, "did": driver_assignment_id},
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
                            (companyid, branchid, payrollperiodid, driverassignmentid,
                             workdate, linetype, quantity,
                             sourcetype, status, needsmanagerreview, notes, addedbyuserid)
                        VALUES
                            (:cid, :bid, :pid, :did,
                             :dt, 'DailyStatus', 0,
                             'Manual', 'Active', FALSE, :n, :uid)
                        RETURNING draftlineid
                    """),
                    {
                        "cid": company_id, "bid": branch_id, "pid": period_id,
                        "did": driver_assignment_id, "dt": work_date,
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
                  AND  driverassignmentid        = :did
                  AND  linetype        = 'DailyNote'
                  AND  status         != 'Void'
                LIMIT 1
            """),
            {"pid": period_id, "dt": work_date, "did": driver_assignment_id},
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
                            (companyid, branchid, payrollperiodid, driverassignmentid,
                             workdate, linetype, quantity,
                             sourcetype, status, needsmanagerreview, notes, addedbyuserid)
                        VALUES
                            (:cid, :bid, :pid, :did,
                             :dt, 'DailyNote', 0,
                             'Manual', 'Active', FALSE, :n, :uid)
                        RETURNING draftlineid
                    """),
                    {
                        "cid": company_id, "bid": branch_id, "pid": period_id,
                        "did": driver_assignment_id, "dt": work_date,
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
            company_id, branch_id, period_id, driver_assignment_id, work_date, user_id, db,
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
            driver_assignment_id=driver_assignment_id,
            work_date=work_date,
            status_key_id=key_row["statuskeyid"] if key_row else None,
            user_id=user_id,
            db=db,
            currency=currency,
        )

    # Return the refreshed grid
    return await get_day_grid(
        period_id=period_id,
        company_id=company_id,
        user_id=user_id,
        work_date=data.work_date,
        db=db,
    )
