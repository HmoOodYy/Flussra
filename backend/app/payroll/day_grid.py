"""
Day Grid — the daily driver/day entry grid for a single work date.

Owns one REST resource end to end, GET and POST on
/payroll/periods/{period_id}/day-grid:

  - get_day_grid       read model: assembles the columns, status keys, driver rows,
                       canonical entry-state values and the summary tallies into a
                       DayGridResponse. Strictly read-only.
  - get_period_entry_overview
                       read model: the same rows and summary for every work date of the
                       period (GET /payroll/periods/{period_id}/entry-overview), built by
                       the same per-date code as get_day_grid. Strictly read-only.
  - save_day_grid      batch write orchestrator: validates the whole payload before
                       any DML (all-or-nothing), then acquires locks, drives source-line
                       upserts canonical entry state, and returns get_day_grid's refreshed
                       response.
  - _parse_quantity    save-payload quantity parser; raises 422 before any DB write so
                       the batch stays atomic.

Columns are the period's ACTIVE definitions (app.payroll.period_definitions) and both
the response values and the save payload are keyed by PayrollPeriodDefinitionID. A
definition's code and name are display metadata only: they never route a save, classify
a total or select a rate. Money shown here is derived live
(app.payroll.live_source_calculation); no stored amount is read.

Status selections and notes are stored only in PayrollPeriodDriverDayEntryState.
Ordinary pay-definition quantities go through add_draft_line/update_draft_line/
void_draft_line.

Transaction ownership is above this module: app.dependencies.get_db opens
`async with engine.begin()`, so every write here shares the caller's ambient
transaction; a failure anywhere unwinds the whole request.
"""
from dataclasses import dataclass
from datetime import date, timedelta
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
    lock_selected_status_keys,
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
from app.payroll.financial_authority import (
    FinancialAuthority,
    frozen_source_lines,
    resolve_financial_authority,
)
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
    PeriodEntryOverviewDay,
    PeriodEntryOverviewResponse,
    PeriodSummary,
)
from app.payroll.status_work_conflict import FinalDayState, assert_no_off_status_work_conflicts
from app.settings.status_keys import get_status_by_id, resolve_status_input

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


def _grid_period(period: PeriodSummary) -> DayGridPeriod:
    return DayGridPeriod(
        period_id=period.payroll_period_id,
        period_name=period.period_name,
        start_date=period.start_date,
        end_date=period.end_date,
        pay_date=period.pay_date,
        status=period.status,
        branch_id=period.branch_id,
        branch_name=period.branch_name,
    )


@dataclass
class _PeriodGridContext:
    """Period-level read inputs shared by every work date of the grid."""
    period: PeriodSummary
    columns: list[DayGridColumn]
    status_keys: list[DayGridStatusKey]
    status_key_id_map: dict[int, DayGridStatusKey]
    snapshot_rows: list[Any]
    authority: FinancialAuthority
    is_finalized: bool
    status_evidence_entries: list[dict[str, Any]] | None
    status_evidence_state: dict[str, str | None] | None
    status_evidence_unavailable: bool


async def _load_period_grid_context(
    period: PeriodSummary, company_id: int, db: AsyncConnection,
) -> _PeriodGridContext:
    """Load everything of the grid that does not depend on the work date (read-only).

    The caller has already authorized the read of the period.
    """
    period_id = period.payroll_period_id
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

    # ── Load the Company's active Statuses (one catalog for every Branch) ── #
    sk_result = await db.execute(
        text("""
            SELECT statuskeyid, statuscode, keyname, isoffreason, hoursvalue,
                   COALESCE((SELECT array_agg(a.aliastext ORDER BY a.statuskeyaliasid)
                             FROM payroll.payrollstatuskeyaliases a
                             WHERE a.statuskeyid = k.statuskeyid), ARRAY[]::varchar[]) AS aliases
            FROM   payroll.payrollstatuskeys k
            WHERE  companyid = :cid
              AND  isactive  = TRUE
            ORDER BY displayorder, keyname
        """),
        {"cid": company_id},
    )
    status_keys: list[DayGridStatusKey] = []
    for row in sk_result.mappings().all():
        sk = DayGridStatusKey(
            status_key_id=row["statuskeyid"],
            key_code=row["statuscode"],
            label=row["keyname"] or row["statuscode"],
            is_off_reason=bool(row["isoffreason"]),
            hours_value=row["hoursvalue"],
            aliases=list(row["aliases"]),
        )
        status_keys.append(sk)

    # Reverse lookup: StatusKeyID → DayGridStatusKey (for editable canonical path).
    status_key_id_map: dict[int, DayGridStatusKey] = {sk.status_key_id: sk for sk in status_keys}

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

    # ── Financial authority of the period ────────────────────────────────── #
    # Open/Returned money is live; InReview/Approved/Locked/Archived money comes only from the
    # verified frozen snapshot of the period; a frozen authority that cannot be resolved or does
    # not verify withholds the money (never a live fallback) and keeps the entry data.
    authority = await resolve_financial_authority(
        db, period_id=period.payroll_period_id, company_id=company_id, branch_id=branch_id,
        withhold=True,
    )

    # ── Immutable Status evidence for Locked/Archived ─────────────────────── #
    # After Submit, the verified snapshot's Status evidence is the historical authority for
    # finalized periods. Locked/Archived rows below must not use the EntryState freeze columns
    # (StatusCodeSnapshot/StatusLabelSnapshot/StatusIsOffReasonSnapshot/FinalizedAtUtc),
    # current mutable PayrollStatusKeys, or legacy DraftLine Status codes to determine
    # historical Status meaning. Other period statuses are unaffected.
    _is_finalized_period = period.status in ("Locked", "Archived")
    status_evidence_entries: list[dict[str, Any]] | None = None
    status_evidence_state: dict[str, str | None] | None = None
    # Set once evidence resolution below concludes UNAVAILABLE -- guards the
    # worked/off tally of every date (see the tally comment for why EMPTY
    # does not need the same guard).
    status_evidence_unavailable = False
    if _is_finalized_period:
        status_evidence_entries, status_evidence_state = status_evidence.status_evidence_of(authority)
        status_evidence_unavailable = status_evidence_state["state"] == "UNAVAILABLE"

    return _PeriodGridContext(
        period=period,
        columns=columns,
        status_keys=status_keys,
        status_key_id_map=status_key_id_map,
        snapshot_rows=snap_rows_all,
        authority=authority,
        is_finalized=_is_finalized_period,
        status_evidence_entries=status_evidence_entries,
        status_evidence_state=status_evidence_state,
        status_evidence_unavailable=status_evidence_unavailable,
    )


async def _build_day_grid(
    ctx: _PeriodGridContext, company_id: int, work_date: date, db: AsyncConnection,
) -> tuple[list[DayGridRow], DayGridSummary]:
    """Build the rows and summary of one work date from the shared period context (read-only)."""
    period = ctx.period
    period_id = period.payroll_period_id
    branch_id = period.branch_id
    columns = ctx.columns
    snap_rows_all = ctx.snapshot_rows
    status_key_id_map = ctx.status_key_id_map
    authority = ctx.authority
    _is_finalized_period = ctx.is_finalized
    _status_evidence_unavailable = ctx.status_evidence_unavailable
    _status_evidence_by_driver: dict[int, dict[str, Any]] = {
        entry["driver_assignment_id"]: entry
        for entry in ctx.status_evidence_entries or []
        if entry["work_date"] == work_date
    }

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
                SELECT dl.draftlineid, dl.driverassignmentid,
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

    # ── Money for the ordinary lines of this date, from the period's authority ─── #
    column_ids = {c.payroll_period_definition_id for c in columns}
    live_by_line: dict[int, Any] = {}
    frozen_by_line: dict[int, dict[str, Any]] = {}
    if authority.is_live and driver_assignment_ids:
        live_by_line = {
            line.draft_line_id: line
            for line in await load_live_source_lines(
                period_id, company_id, db, driver_assignment_ids=driver_assignment_ids, work_date=work_date,
            )
        }
    elif authority.is_frozen:
        frozen_by_line = frozen_source_lines(authority.content)

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
        status_key_id_held: int | None = None
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
                            status_key_id_held = sk_id
                            status_key_code = live_sk.key_code
                            sk_label = live_sk.label
                            is_off = bool(live_sk.is_off_reason)
                        else:
                            dk = deactivated_key_map.get(sk_id)
                            if dk:
                                status_key_id_held = sk_id
                                status_key_code = dk["statuscode"]
                                sk_label = dk["keyname"]
                                is_off = bool(dk["isoffreason"])

        for line in drv_lines:
            ppd_id = line["payrollperioddefinitionid"]
            if ppd_id is not None and ppd_id in column_ids:
                qty = line["quantity"]
                live = live_by_line.get(line["draftlineid"])
                calc = live.calculation if live is not None else None
                frozen_line = frozen_by_line.get(line["draftlineid"])
                attention = calc.needs_attention if calc is not None else False
                if frozen_line is not None:
                    # A frozen source: its exact snapshot line is the only source of money.
                    line_amount: Decimal | None = frozen_line["CalculatedAmount"]
                    line_status: str | None = frozen_line["CalculationStatus"]
                elif calc is not None:
                    line_amount = calc.amount
                    line_status = calc.status.value
                else:
                    line_amount, line_status = None, None
                values[str(ppd_id)] = DayGridLineValue(
                    line_id=line["draftlineid"],
                    quantity=str(qty) if qty is not None else None,
                    calculated_amount=str(line_amount) if line_amount is not None else None,
                    needs_manager_review=attention,
                    calculation_status=line_status,
                )
                if attention:
                    needs_attention += 1
                if qty:
                    quantity_totals[ppd_id] = (
                        quantity_totals.get(ppd_id, Decimal("0")) + Decimal(str(qty))
                    )
                if line_amount is not None:
                    gross_total += line_amount

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
            status_key_id=status_key_id_held,
            status_key=status_key_code,
            status_label=sk_label,
            is_off=is_off,
            notes=notes_text,
            values=values,
        ))

    # No money is shown without an authority: a Draft (Prepared) period has none, and a frozen
    # authority that is unavailable withholds it.
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
        gross_total=str(gross_total) if authority.financials_available else None,
        needs_attention=needs_attention,
        financials_available=authority.financials_available,
        financial_authority=authority.financial_authority,
        financial_authority_reason=authority.financial_authority_reason,
    )

    return rows, summary


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
    - Status keys: the Company's active Statuses (names and aliases), shared by every Branch.
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

    context = await _load_period_grid_context(period, company_id, db)
    rows, summary = await _build_day_grid(context, company_id, work_date, db)

    return DayGridResponse(
        period=_grid_period(period),
        work_date=work_date,
        columns=context.columns,
        status_keys=context.status_keys,
        rows=rows,
        summary=summary,
        status_evidence=context.status_evidence_state,
    )


async def get_period_entry_overview(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> PeriodEntryOverviewResponse:
    """
    Return the day grid of every work date of a period in one read.

    Authorization is the one of get_day_grid. Each day carries exactly the rows and summary
    get_day_grid returns for that date, plus whether the frozen period day is a configured
    (normal) day off. The dates are the period's frozen days when it has them, otherwise every
    date from start_date to end_date. Strictly read-only.
    """
    await require_non_driver_subject(company_id, user_id, db)

    # ── Load and access-check the period ─────────────────────────────────── #
    period = await get_period_by_id(company_id, user_id, period_id, db)

    # ── Permission: payroll.view OR payroll.entry ─────────────────────────── #
    await check_any_permission(
        company_id, user_id, period.branch_id, ["payroll.view", "payroll.entry"], db
    )

    # ── The valid work dates (CP-2B: snapshot-aware) ──────────────────────── #
    day_rows = (await db.execute(
        text("""
            SELECT workdate, isconfiguredoffday
            FROM   payroll.PayrollPeriodDays
            WHERE  payrollperiodid = :pid
            ORDER BY workdate
        """),
        {"pid": period.payroll_period_id},
    )).all()
    if day_rows:
        calendar = [
            (r.workdate, bool(r.isconfiguredoffday)) for r in day_rows
            if period.start_date <= r.workdate <= period.end_date
        ]
    else:
        calendar = [
            (period.start_date + timedelta(days=offset), False)
            for offset in range((period.end_date - period.start_date).days + 1)
        ]

    context = await _load_period_grid_context(period, company_id, db)
    days: list[PeriodEntryOverviewDay] = []
    for work_date, is_configured_off_day in calendar:
        rows, summary = await _build_day_grid(context, company_id, work_date, db)
        days.append(PeriodEntryOverviewDay(
            work_date=work_date,
            is_configured_off_day=is_configured_off_day,
            rows=rows,
            summary=summary,
        ))

    return PeriodEntryOverviewResponse(
        period=_grid_period(period),
        columns=context.columns,
        status_keys=context.status_keys,
        days=days,
        status_evidence=context.status_evidence_state,
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
      - Upsert canonical Status/note entry state

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

    await lock_and_get_company_currency_for_monetary_write(company_id, db, required=False)
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

    # Each row: (save_row, driver_assignment_id, parsed_values, key_row)
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

        # The Status of the row is either a canonical identity (`status_key_id`: the Status the
        # row already holds, used as is) or newly typed text (`status_key`: a name, an alias or
        # a code, resolved Company-wide to exactly one Status). Keeping a held Status and
        # choosing one by text are different operations: an unrelated edit of the row, such as
        # its note, must never re-read the held Status as free text, where another Status's
        # alias could take the day over. Any resolution failure rejects the whole batch before
        # a write (422). Re-saving the held Status is allowed even when it was deactivated since.
        key_row = None
        has_status_id = save_row.status_key_id is not None
        has_status_text = bool(save_row.status_key and save_row.status_key.strip())
        if has_status_id or has_status_text:
            held_key_id = (await db.execute(
                text("""
                    SELECT statuskeyid FROM payroll.payrollperioddriverdayentrystate
                    WHERE  payrollperiodid = :pid AND workdate = :dt
                      AND  driverassignmentid = :did AND isvoided = FALSE
                """),
                {"pid": period_id, "dt": work_date, "did": driver_assignment_id},
            )).scalar_one_or_none()
            if has_status_id:
                key_row = await get_status_by_id(
                    company_id, save_row.status_key_id, db, held_key_id=held_key_id)
            else:
                try:
                    key_row = await resolve_status_input(
                        company_id, save_row.status_key, db, held_key_id=held_key_id)
                except HTTPException:
                    if held_key_id is None:
                        raise
                    key_row = await resolve_status_input(
                        company_id, save_row.status_key, db,
                        allow_deactivated=True, key_id=held_key_id,
                    )
        parsed_rows.append((save_row, driver_assignment_id, parsed_values, key_row))

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

    # The Statuses this batch selects are locked FOR SHARE until the transaction ends, so a
    # concurrent catalog edit of their hours, off-day semantic, Category or rate column either
    # committed before this save (and is seen) or waits and is then refused as in use. A Status
    # deactivated since it was resolved cannot be newly selected.
    await lock_selected_status_keys(
        [(row["statuskeyid"], bool(row["isactive"])) for _, _, _, row in parsed_rows
         if row is not None],
        company_id, db,
    )

    # ── Full-day-off integrity (under the Period lock) ──────────────────── #
    # The final state of every row of the batch (its resolved Status and the ordinary
    # quantities left after the save) must not combine a full-day-off Status with ordinary
    # work. A whole batch with one such row is refused before any write.
    await assert_no_off_status_work_conflicts(
        period_id=period_id, company_id=company_id, work_date=work_date,
        rows=[
            FinalDayState(
                driver_assignment_id=driver_assignment_id,
                status_is_off=bool(key_row["isoffreason"]) if key_row is not None else False,
                quantities=parsed_values,
            )
            for _, driver_assignment_id, parsed_values, key_row in parsed_rows
        ],
        db=db,
    )

    # ── Status key usage limit enforcement (under the Period lock) ─────── #
    # All existence/active checks passed. Enforce configured usage limits
    # batch-wide so that intra-batch writes that would collectively exceed a
    # limit are caught before any DB write happens (all-or-nothing atomicity).
    # The counts must be read only after the Period lock is held, so a
    # concurrent Status save for this Period has already committed or waits.
    all_batch_pairs: list[tuple] = [(r.driver_assignment_id, work_date) for r in data.rows]
    batch_by_key: dict[int, list[int]] = {}
    key_by_id: dict[int, dict] = {}
    for _, drv_id, _, krow in parsed_rows:
        if krow is not None:
            batch_by_key.setdefault(krow["statuskeyid"], []).append(drv_id)
            key_by_id[krow["statuskeyid"]] = krow

    for status_key_id, batch_driver_assignment_ids in batch_by_key.items():
        await _enforce_status_key_limits(
            key_row=key_by_id[status_key_id],
            branch_id=branch_id,
            period_id=period_id,
            company_id=company_id,
            work_date=work_date,
            batch_driver_assignment_ids=batch_driver_assignment_ids,
            all_batch_pairs=all_batch_pairs,
            db=db,
        )

    for save_row, driver_assignment_id, parsed_values, key_row in parsed_rows:

        # ── Ordinary pay lines, by period definition ─────────────────────── #
        # Two passes around the Status write keep every intermediate database state free of a
        # full-day-off Status beside ordinary work: lines being cleared go first, lines being
        # set go after the Status and entry state. The final state was validated above.
        async def apply_quantities(
            *, clearing: bool, driver_assignment_id=driver_assignment_id,
            parsed_values=parsed_values,
        ) -> None:
            for ppd_id, qty in parsed_values.items():
                # Treat None (empty/blank) as zero (clear)
                effective_qty: Decimal = qty if qty is not None else Decimal("0")
                if (effective_qty == 0) != clearing:
                    continue

                existing_result = await db.execute(
                    text("""
                        SELECT draftlineid FROM payroll.payrolldraftlines
                        WHERE  payrollperiodid = :pid
                          AND  workdate        = :dt
                          AND  driverassignmentid        = :did
                          AND  payrollperioddefinitionid = :ppd
                          AND  status         != 'Void'
                    """),
                    {"pid": period_id, "dt": work_date, "did": driver_assignment_id,
                     "ppd": ppd_id},
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
                        enforce_status_conflict=False,
                    )
                else:
                    await update_draft_line(
                        period_id=period_id,
                        draft_line_id=existing["draftlineid"],
                        company_id=company_id,
                        user_id=user_id,
                        data=DraftLineUpdate(quantity=effective_qty),
                        db=db,
                        enforce_status_conflict=False,
                    )

        await apply_quantities(clearing=True)

        notes_val = save_row.notes
        await _upsert_entry_state(
            company_id, branch_id, period_id, driver_assignment_id, work_date, user_id, db,
            status_key_id=key_row["statuskeyid"] if key_row else None,
            note_text=notes_val if notes_val else None,
        )

        await apply_quantities(clearing=False)

    # Return the refreshed grid
    return await get_day_grid(
        period_id=period_id,
        company_id=company_id,
        user_id=user_id,
        work_date=data.work_date,
        db=db,
    )
