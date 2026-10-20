"""
Live Period Calculation — the payroll domain's owner of:

  A. Live authoritative Calculation packet construction
     (_build_live_calculation_packet) and its packet types
     (_CalculationPacketLine, _CalculationPacketDriverTotal, _LiveCalculationPacket)
  B. The CP-4B calculation preview API (get_calculation_preview)
  C. Structural source validation (_validate_period_can_finalize,
     _validate_legacy_status_canonicalized)

Ordinary PayDefinition earnings are derived LIVE: each source line is resolved and
calculated through app.payroll.live_source_calculation and the method-owned boundary
in app.payroll.definition_calculation. No ordinary amount is read from a stored column.
Status pay remains on its temporary Status-only path; Bonus is BonusEvent; minimum/
maximum is DriverPayRule.

Preview is read-only by construction: get_calculation_preview ->
_build_live_calculation_packet emits no INSERT/UPDATE/DELETE.

Transaction ownership is above this module: every function receives an ambient
AsyncConnection, opens no transaction, commits nothing and rolls back nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.permissions import check_any_permission
from app.access.policy import require_non_driver_subject
from app.company_currency import CompanyCurrency, currency_error, get_company_currency
from app.payroll import bonus as payroll_bonus
from app.payroll.calculation import minmax_proration as minmax
from app.payroll.calculation.minmax_proration import MINMAX_PRORATION_VERSION
from app.payroll.calculation.payable_rounding import (
    PAYABLE_ROUNDING_VERSION,
    is_payable_precision,
    round_payable,
)
from app.payroll.definition_calculation import CalculationStatus
from app.payroll.eligibility import (
    _is_snapshot_row_eligible_for_workdate,
    require_period_eligibility_snapshot,
)
from app.payroll.live_source_calculation import load_live_source_lines
from app.payroll.period_read import get_period_by_id
from app.payroll.schemas import ENTRY_ALLOWED_STATUSES, PeriodSummary
from app.payroll.status_payment_sync import (
    _resolve_live_status_payment_lines,
)
from app.payroll_setup.chronology import Schedule, period_matches_schedule

if TYPE_CHECKING:
    from app.payroll.schemas import CalculationPreviewResponse

# ---------------------------------------------------------------------------
# Phase 7: Shared finalization validator
# Used by both finalize_period and get_finalization_preview so that preview
# surfaces exactly the same blockers that finalize_period would enforce.
# Does NOT check: period status, permissions, empty-period, NMR/zero-calc,
# or min/max cross-rule guards (those depend on per-path state).
# ---------------------------------------------------------------------------

async def _validate_period_can_finalize(
    period_id: int,
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> list[str]:
    """
    Structural source blockers shared by the live calculation packet and the
    finalization paths. Returns human-readable blocker strings (empty = none).

    Checks:
      1. Duplicate active source lines for one (driver, date, period definition).
      2. Driver eligibility for the line's work date.

    Rate and calculation problems are NOT checked here: they are derived live per
    line and surface as that line's calculation status.
    """
    blockers: list[str] = []

    # ── 1. Duplicate active source lines ──────────────────────────────────────
    dup_rows = (await db.execute(
        text("""
            SELECT driverassignmentid, workdate, payrollperioddefinitionid, COUNT(*) AS cnt
            FROM   payroll.payrolldraftlines
            WHERE  payrollperiodid = :period_id
              AND  companyid       = :company_id
              AND  status         != 'Void'
              AND  payrollperioddefinitionid IS NOT NULL
            GROUP BY driverassignmentid, workdate, payrollperioddefinitionid
            HAVING COUNT(*) > 1
            LIMIT 5
        """),
        {"period_id": period_id, "company_id": company_id},
    )).mappings().all()
    if dup_rows:
        examples = "; ".join(
            f"driver {r['driverassignmentid']} {r['workdate']} definition "
            f"{r['payrollperioddefinitionid']} x{r['cnt']}"
            for r in dup_rows
        )
        blockers.append(
            f"Cannot finalize: duplicate active source lines detected "
            f"({examples}). Void the extra lines before finalizing."
        )

    # ── 2. Driver eligibility ─────────────────────────────────────────────────
    # Every operational period must have its tenant-scoped frozen roster marker.
    # Saved source is rescued only on its exact date; no live-roster fallback exists.
    await require_period_eligibility_snapshot(period_id, company_id, branch_id, db)
    elig_rows = (await db.execute(
        text("""
            SELECT dl.draftlineid, dl.driverassignmentid, dl.workdate
            FROM payroll.payrolldraftlines dl
            WHERE dl.payrollperiodid = :period_id AND dl.companyid = :company_id
              AND dl.status != 'Void' AND dl.payrollperioddefinitionid IS NOT NULL
              AND NOT EXISTS (
                    SELECT 1 FROM payroll.payrollperioddrivereligibility ppde
                    WHERE ppde.payrollperiodid = dl.payrollperiodid
                      AND ppde.driverassignmentid = dl.driverassignmentid AND ppde.iseligibleforperiod = TRUE
              )
            LIMIT 5
        """), {"period_id": period_id, "company_id": company_id},
    )).mappings().all()
    if elig_rows:
        examples = "; ".join(f"driver {r['driverassignmentid']} {r['workdate']}" for r in elig_rows)
        blockers.append(
            f"Cannot finalize: {len(elig_rows)} source line(s) reference "
            f"driver/date combinations that are no longer eligible "
            f"({examples}). Void these lines before finalizing."
        )

    return blockers


# ---------------------------------------------------------------------------
# CP-4B — Open/Returned live read-only calculation preview
# ---------------------------------------------------------------------------


_CALCULATION_BLOCKER_REASONS: dict[CalculationStatus, str] = {
    CalculationStatus.MISSING_RATE: (
        "No effective rate is assigned to this driver for this pay definition on the "
        "work date."
    ),
    CalculationStatus.INCOMPLETE_RATE: (
        "The effective rate assignment is missing a required value."
    ),
    CalculationStatus.METHOD_NOT_READY: (
        "This pay definition uses a calculation method that is not operational yet."
    ),
}


@dataclass(frozen=True)
class _CalculationPacketLine:
    """Persistence-grade result from the live CP-4B calculation assembly."""

    source_type: str
    source_id: str | None
    line_type: str | None
    line_scope: str | None
    work_date: date | None
    driver_assignment_id: int
    quantity: Decimal | None
    resolved_rate_amount: Decimal | None
    calculated_amount: Decimal | None
    needs_manager_review: bool
    blocker_reason: str | None
    payroll_period_definition_id: int | None = None
    pay_definition_id: int | None = None
    definition_name: str | None = None
    calculation_status: str | None = None
    pay_item_id: int | None = None
    rate_column_id: int | None = None
    rate_type_id: int | None = None
    driver_rate_id: int | None = None
    bonus_event_id: int | None = None
    source_evidence: dict[str, Any] | None = None
    snapshot_source_type: str | None = None
    snapshot_source_id: str | None = None
    snapshot_calculated_amount: Decimal | None = None


@dataclass(frozen=True)
class _OfficialComponent:
    """One official payable amount of a DriverAssignment in the period.

    ``pre_rounding_amount`` is the exact sum of its raw contributions at internal
    precision; ``official_amount`` is that sum rounded once with ``round_payable``.
    Identity: ORDINARY is keyed by PayrollPeriodDefinitionID + PayDefinitionID + the frozen
    calculation method/version, STATUS by the status rate column, BONUS by the
    DriverAssignment alone, and MINIMUM_ADJUSTMENT / MAXIMUM_ADJUSTMENT by the DriverPayRule.
    ``source_ids`` are the contributing line source ids; ``unresolved_line_count`` counts
    contributing lines whose amount is missing (a blocker is raised separately).
    """

    kind: str
    driver_assignment_id: int
    pre_rounding_amount: Decimal
    official_amount: Decimal
    payroll_period_definition_id: int | None = None
    pay_definition_id: int | None = None
    calculation_method: str | None = None
    calculation_method_version: int | None = None
    status_rate_column_id: int | None = None
    driver_pay_rule_id: int | None = None
    source_ids: tuple[str, ...] = ()
    unresolved_line_count: int = 0


@dataclass(frozen=True)
class _MinMaxRule:
    """A DriverPayRule as selected for the period, with the home pay calendar it recorded."""

    driver_pay_rule_id: int
    rule_type: str
    amount: Decimal
    status: str
    effective_from: date
    effective_to: date | None
    calendar_frequency: str
    calendar_anchor_start_date: date
    calendar_custom_interval_days: int | None
    calendar_source_branch_id: int
    calendar_source_payroll_setup_version_id: int
    prorated_amount: Decimal | None = None


@dataclass(frozen=True)
class _MinMaxProration:
    """How one DriverAssignment's Minimum/Maximum were evaluated for the Branch period.

    ``status`` is APPLIED or the reason neither rule applied. ``numerator`` / ``denominator``
    are the eligible and the scheduled whole days of the period (None when the evaluation
    stopped before the days were counted). ``base_amount`` is the official ordinary plus
    status pay the rules were compared with (Bonus excluded).
    """

    driver_assignment_id: int
    driver_id: int
    payroll_period_id: int
    branch_id: int
    status: str
    base_amount: Decimal
    numerator: int | None = None
    denominator: int | None = None
    minimum_rule: _MinMaxRule | None = None
    maximum_rule: _MinMaxRule | None = None
    version: str = MINMAX_PRORATION_VERSION


@dataclass(frozen=True)
class _CalculationPacketDriverTotal:
    driver_assignment_id: int
    driver_id: int
    driver_number: str | None
    driver_name: str | None
    daily_pay: Decimal
    status_pay: Decimal
    minimum_adjustment: Decimal
    maximum_adjustment: Decimal
    bonus_total: Decimal
    expected_pay: Decimal
    needs_manager_review: bool
    blockers: list[str]
    lines: list[_CalculationPacketLine]
    components: list[_OfficialComponent] = field(default_factory=list)
    minmax: _MinMaxProration | None = None


@dataclass(frozen=True)
class _LiveCalculationPacket:
    payroll_period_id: int
    company_id: int
    branch_id: int
    status: str
    blockers: list[str]
    warnings: list[str]
    drivers: list[_CalculationPacketDriverTotal]
    total_expected_pay: Decimal
    currency_code: str | None = None
    currency_minor_unit_digits: int | None = None
    payable_rounding_version: str = PAYABLE_ROUNDING_VERSION
    minmax_proration_version: str = MINMAX_PRORATION_VERSION


async def _validate_legacy_status_canonicalized(
    period: PeriodSummary,
    company_id: int,
    db: AsyncConnection,
) -> list[str]:
    """
    CP-4D completeness guard: reject Submit/Resubmit if a legacy DailyStatus
    DraftLine exists for a driver/day with no corresponding live-selected
    canonical PayrollPeriodDriverDayEntryState row.

    `calculation_snapshot._load_report_evidence` reads only canonical
    entry-state rows with a non-voided StatusKeyID; it never falls back to
    DraftLines. Without this guard, such a day would silently capture zero
    Status evidence while the snapshot still reports a versioned, "complete"
    ReportEvidenceVersion.

    A canonical row that exists but only carries NoteText (StatusKeyID NULL --
    e.g. a note-only save on an old period whose legacy Status code was never
    re-entered) does not satisfy this check; the legacy Status is still
    unrepresented. DailyNote-only legacy lines are out of scope: NoteText is
    never part of the immutable Status evidence contract (the CP-4D snapshot
    evidence reader requires `StatusKeyID IS NOT NULL`), so a missing canonical
    row can never cause a note to disappear from that evidence.

    Read-only. Returns a blocker list (empty = no gap found).
    """
    rows = (await db.execute(
        text("""
            SELECT DISTINCT ds.driverassignmentid, ds.workdate
            FROM   payroll.payrolldraftlines ds
            WHERE  ds.payrollperiodid = :pid
              AND  ds.companyid       = :cid
              AND  ds.linetype        = 'DailyStatus'
              AND  ds.status         != 'Void'
              AND  ds.workdate BETWEEN :period_start AND :period_end
              AND  NOT EXISTS (
                       SELECT 1
                       FROM   payroll.payrollperioddriverdayentrystate e
                       WHERE  e.payrollperiodid = ds.payrollperiodid
                         AND  e.companyid       = ds.companyid
                         AND  e.driverassignmentid        = ds.driverassignmentid
                         AND  e.workdate        = ds.workdate
                         AND  e.statuskeyid    IS NOT NULL
                         AND  e.isvoided        = FALSE
                   )
            ORDER BY ds.driverassignmentid, ds.workdate
            LIMIT 5
        """),
        {
            "pid": period.payroll_period_id,
            "cid": company_id,
            "period_start": period.start_date,
            "period_end": period.end_date,
        },
    )).mappings().all()
    if not rows:
        return []

    examples = "; ".join(f"driver {r['driverassignmentid']} on {r['workdate']}" for r in rows)
    return [
        "LEGACY_STATUS_NOT_CANONICAL: one or more days have a Status set only "
        "through the legacy Daily Status representation, with no matching "
        f"entry in the current Day Grid entry state ({examples}). Open the "
        "Day Grid for the affected day(s), re-select the Status, and save "
        "before this period can be submitted."
    ]




@dataclass
class _Accumulator:
    """Raw contributions to one official component, summed before the one rounding."""

    kind: str
    driver_assignment_id: int
    identity: dict[str, Any] = field(default_factory=dict)
    raw: Decimal = field(default_factory=lambda: Decimal("0"))
    source_ids: list[str] = field(default_factory=list)
    unresolved: int = 0

    def add(self, amount: Decimal | None, source_id: str | None, *, unresolved: bool = False) -> None:
        self.raw += amount if amount is not None else Decimal("0")
        if source_id is not None:
            self.source_ids.append(source_id)
        if unresolved:
            self.unresolved += 1

    def official(self, round_amount) -> _OfficialComponent:
        return _OfficialComponent(
            kind=self.kind,
            driver_assignment_id=self.driver_assignment_id,
            pre_rounding_amount=self.raw,
            official_amount=round_amount(self.raw),
            source_ids=tuple(self.source_ids),
            unresolved_line_count=self.unresolved,
            **self.identity,
        )


@dataclass
class _MinMaxOutcome:
    proration: _MinMaxProration
    raw_minimum: Decimal = field(default_factory=lambda: Decimal("0"))
    raw_maximum: Decimal = field(default_factory=lambda: Decimal("0"))
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_MINIMUM = "MinimumPay"
_MAXIMUM = "MaximumPay"


def _minmax_rule_from_row(row: Any) -> _MinMaxRule:
    return _MinMaxRule(
        driver_pay_rule_id=int(row["driverpayruleid"]),
        rule_type=row["ruletype"],
        amount=Decimal(str(row["amount"])),
        status=row["status"],
        effective_from=row["effectivefrom"],
        effective_to=row["effectiveto"],
        calendar_frequency=row["calendarfrequency"],
        calendar_anchor_start_date=row["calendaranchorstartdate"],
        calendar_custom_interval_days=row["calendarcustomintervaldays"],
        calendar_source_branch_id=int(row["calendarsourcebranchid"]),
        calendar_source_payroll_setup_version_id=int(row["calendarsourcepayrollsetupversionid"]),
    )


async def _load_effective_rules(
    company_id: int, driver_ids: list[int], as_of: date, db: AsyncConnection,
) -> dict[tuple[int, str], _MinMaxRule]:
    """The Minimum and Maximum rule of each Driver effective at ``as_of`` (v1 selection).

    One set-based read for all Drivers: per (Driver, rule type) the Active or Ended rule whose
    window contains the period start, the latest start winning. Rules are owned by the
    permanent Driver, never by a DriverAssignment.
    """
    if not driver_ids:
        return {}
    rows = (await db.execute(
        text("""
            SELECT DISTINCT ON (driverid, ruletype)
                   driverpayruleid, driverid, ruletype, amount, status,
                   effectivefrom, effectiveto,
                   calendarfrequency, calendaranchorstartdate, calendarcustomintervaldays,
                   calendarsourcebranchid, calendarsourcepayrollsetupversionid
            FROM payroll.driverpayrules
            WHERE  companyid     = :cid
              AND  driverid      = ANY(:driver_ids)
              AND  ruletype      IN ('MinimumPay', 'MaximumPay')
              AND  status        IN ('Active', 'Ended')
              AND  effectivefrom <= :as_of
              AND  (effectiveto IS NULL OR effectiveto >= :as_of)
            ORDER BY driverid, ruletype, effectivefrom DESC
        """),
        {"cid": company_id, "driver_ids": sorted(set(driver_ids)), "as_of": as_of},
    )).mappings().all()
    return {(int(row["driverid"]), row["ruletype"]): _minmax_rule_from_row(row) for row in rows}


async def _load_coverage_inputs(
    period: PeriodSummary, company_id: int, assignment_drivers: dict[int, int],
    db: AsyncConnection,
) -> tuple[bool, set[date], dict[int, set[date]], dict[int, set[date]]]:
    """(has day rows, scheduled days, per DriverAssignment the eligible work dates it owns,
    per DriverAssignment its days with an off-reason Status).

    Scheduled days are read from the frozen ``PayrollPeriodDays`` only; a period without day
    rows reports ``has day rows`` False (the caller blocks, nothing is inferred). A date belongs to
    exactly one DriverAssignment of a Driver: the latest-starting one whose window overlaps
    that Branch-local day (rule D1), decided from captured eligibility where it exists, so a
    transfer day shared by two tenures counts for one of them only. At most four reads for
    all assignments, however many there are.
    """
    day_rows = (await db.execute(
        text("""
            SELECT workdate, (isdefaultworkday OR isaddedworkday) AS scheduled
            FROM payroll.payrollperioddays
            WHERE payrollperiodid = :pid AND companyid = :cid
        """),
        {"pid": period.payroll_period_id, "cid": company_id},
    )).mappings().all()
    scheduled = {row["workdate"] for row in day_rows if row["scheduled"]}
    if not scheduled:
        return bool(day_rows), scheduled, {}, {}
    ids = sorted(assignment_drivers)
    snapshot_rows = (await db.execute(
        text("""
            SELECT driverassignmentid, iseligibleforperiod, eligibilityreasoncode,
                   eligibleworkdates
            FROM   payroll.payrollperioddrivereligibility
            WHERE  payrollperiodid = :pid AND companyid = :cid
              AND  driverassignmentid = ANY(:ids)
        """),
        {"pid": period.payroll_period_id, "cid": company_id, "ids": ids},
    )).mappings().all()
    # Ownership by captured facts. A date of X is taken away only by a later-ranking tenure Y of
    # the same Driver (rule D1 order: later start, then higher id) that claims it: (a) through
    # its frozen eligibility in a live period, or (b) only when nothing of Y is frozen for that
    # date, through the live D1 window. A Branch timezone edit therefore never moves a date
    # that has been captured.
    owned_rows = (await db.execute(
        text("""
            SELECT x.driverassignmentid AS assignmentid, d.workdate
            FROM   core.driverassignments x
            CROSS JOIN unnest(CAST(:days AS DATE[])) AS d(workdate)
            WHERE  x.companyid = :cid AND x.driverassignmentid = ANY(:ids)
              AND  NOT EXISTS (
                SELECT 1 FROM core.driverassignments y
                WHERE  y.companyid = x.companyid AND y.driverid = x.driverid
                  AND  y.driverassignmentid <> x.driverassignmentid
                  AND  (y.effectivefromat > x.effectivefromat
                        OR (y.effectivefromat = x.effectivefromat
                            AND y.driverassignmentid > x.driverassignmentid))
                  AND  (
                    EXISTS (
                      SELECT 1
                      FROM   payroll.payrollperioddrivereligibility e
                      JOIN   payroll.payrollperiods p ON p.payrollperiodid = e.payrollperiodid
                      WHERE  e.driverassignmentid = y.driverassignmentid
                        AND  p.status <> 'Cancelled'
                        AND  e.iseligibleforperiod
                        AND  e.eligibilityreasoncode IS DISTINCT FROM 'IncludedByExistingData'
                        AND  d.workdate = ANY(e.eligibleworkdates))
                    OR (
                      NOT EXISTS (
                        SELECT 1
                        FROM   payroll.payrollperioddrivereligibility e
                        JOIN   payroll.payrollperiods p ON p.payrollperiodid = e.payrollperiodid
                        WHERE  e.driverassignmentid = y.driverassignmentid
                          AND  p.status <> 'Cancelled'
                          AND  p.startdate <= d.workdate AND d.workdate <= p.enddate)
                      AND core.fn_DriverEffectiveRange(y.effectivefromat, y.effectivetoat)
                          && core.fn_BranchWorkDateRange(y.companyid, y.branchid,
                                                         d.workdate, d.workdate)))
              )
        """),
        {"cid": company_id, "ids": ids, "days": sorted(scheduled)},
    )).mappings().all()
    owned: dict[int, set[date]] = {}
    for row in owned_rows:
        owned.setdefault(int(row["assignmentid"]), set()).add(row["workdate"])
    eligible: dict[int, set[date]] = {}
    for row in snapshot_rows:
        snapshot = SimpleNamespace(**dict(row))
        assignment_id = int(row["driverassignmentid"])
        eligible[assignment_id] = {
            day for day in owned.get(assignment_id, ())
            if _is_snapshot_row_eligible_for_workdate(snapshot, day)
        }
    # The explicit IsOffReason flag, never a status name: the frozen snapshot once the entry
    # state is finalized, otherwise the live Status key (the pattern of off_drivers).
    off_rows = (await db.execute(
        text("""
            SELECT es.driverassignmentid, es.workdate
            FROM   payroll.payrollperioddriverdayentrystate es
            JOIN   payroll.payrollstatuskeys sk ON sk.statuskeyid = es.statuskeyid
            WHERE  es.payrollperiodid = :pid AND es.companyid = :cid
              AND  es.driverassignmentid = ANY(:ids)
              AND  es.isvoided = FALSE
              AND  es.statuskeyid IS NOT NULL
              AND  (CASE WHEN es.finalizedatutc IS NOT NULL
                         THEN es.statusisoffreasonsnapshot
                         ELSE sk.isoffreason END) IS TRUE
        """),
        {"pid": period.payroll_period_id, "cid": company_id, "ids": ids},
    )).mappings().all()
    off_days: dict[int, set[date]] = {}
    for row in off_rows:
        off_days.setdefault(int(row["driverassignmentid"]), set()).add(row["workdate"])
    return True, scheduled, eligible, off_days


def _proration(
    period: PeriodSummary, assignment_id: int, driver_id: int, base: Decimal,
    status: str, **kwargs: Any,
) -> _MinMaxProration:
    return _MinMaxProration(
        driver_assignment_id=assignment_id, driver_id=driver_id,
        payroll_period_id=period.payroll_period_id, branch_id=period.branch_id,
        status=status, base_amount=base, **kwargs)


async def _evaluate_minmax(
    period: PeriodSummary,
    company_id: int,
    assignment_driver: dict[int, int],
    bases: dict[int, Decimal],
    currency: CompanyCurrency | None,
    db: AsyncConnection,
) -> dict[int, _MinMaxOutcome]:
    """Evaluate every DriverAssignment's Minimum/Maximum for this Branch period.

    Selection is v1: the rule of the permanent Driver effective at the period start. A rule
    applies only when the period is exactly one period of the calendar it recorded
    (MINMAX_CALENDAR_MISMATCH otherwise), and, for a DriverAssignment covering only part of
    the period, both amounts are prorated by the same whole-day fraction. When no scheduled
    day is eligible, or the period has none, neither rule applies.
    """
    digits = None if currency is None else currency.minor_unit_digits
    rules = await _load_effective_rules(
        company_id, list(assignment_driver.values()), period.start_date, db)
    ruled = {
        assignment: driver for assignment, driver in sorted(assignment_driver.items())
        if (driver, _MINIMUM) in rules or (driver, _MAXIMUM) in rules
    }
    has_days = False
    scheduled: set[date] = set()
    eligible: dict[int, set[date]] = {}
    off_days: dict[int, set[date]] = {}
    if ruled:
        has_days, scheduled, eligible, off_days = await _load_coverage_inputs(
            period, company_id, ruled, db)

    outcomes: dict[int, _MinMaxOutcome] = {}
    for assignment_id in sorted(assignment_driver):
        driver_id = assignment_driver[assignment_id]
        base = bases[assignment_id]
        minimum = rules.get((driver_id, _MINIMUM))
        maximum = rules.get((driver_id, _MAXIMUM))

        proration = partial(_proration, period, assignment_id, driver_id, base)

        if minimum is None and maximum is None:
            outcomes[assignment_id] = _MinMaxOutcome(proration(minmax.NO_RULE))
            continue

        # A rule applies to a period only when the period is exactly one period of the
        # calendar the rule recorded (the home pay calendar it was set for). Anything else is
        # a blocker for this driver: never rescaled, never silently dropped or duplicated.
        mismatched = [
            (label, rule) for label, rule in (("Minimum", minimum), ("Maximum", maximum))
            if rule is not None and not period_matches_schedule(
                Schedule(rule.calendar_frequency, rule.calendar_anchor_start_date,
                         rule.calendar_custom_interval_days, 0),
                period.start_date, period.end_date)
        ]
        if mismatched:
            outcomes[assignment_id] = _MinMaxOutcome(
                proration(minmax.CALENDAR_MISMATCH, minimum_rule=minimum, maximum_rule=maximum),
                blockers=[
                    f"MINMAX_CALENDAR_MISMATCH: the {label} pay rule {rule.driver_pay_rule_id} "
                    f"was set for a {rule.calendar_frequency} pay calendar that does not "
                    f"match this period ({period.start_date} to {period.end_date}). "
                    f"End the rule and create a new one for the Driver's current calendar."
                    for label, rule in mismatched
                ])
            continue

        blockers: list[str] = []
        reason: str | None = None
        if digits is not None:
            for label, rule in (("Minimum", minimum), ("Maximum", maximum)):
                if rule is not None and not is_payable_precision(rule.amount, digits):
                    blockers.append(
                        f"SOURCE_AMOUNT_PRECISION: the {label} pay rule {rule.driver_pay_rule_id} "
                        f"amount {rule.amount} has more decimals than the Company currency "
                        f"allows ({digits}). End the rule and create a new one with a "
                        f"correctly rounded amount.")
                    reason = minmax.SOURCE_AMOUNT_PRECISION
        if minimum is not None and maximum is not None and minimum.amount > maximum.amount:
            blockers.append(
                f"Minimum pay ({minimum.amount}) exceeds maximum pay ({maximum.amount}). "
                f"Correct the pay rules before this driver's total can be trusted.")
            reason = reason or minmax.MIN_EXCEEDS_MAX
        if blockers:
            outcomes[assignment_id] = _MinMaxOutcome(
                proration(reason or minmax.MIN_EXCEEDS_MAX,
                          minimum_rule=minimum, maximum_rule=maximum),
                blockers=blockers)
            continue

        if not has_days:
            # No frozen day calendar: the days are never inferred, so nothing can be prorated.
            outcomes[assignment_id] = _MinMaxOutcome(
                proration(minmax.MINMAX_PERIOD_DAYS_MISSING,
                          minimum_rule=minimum, maximum_rule=maximum),
                blockers=[
                    "MINMAX_PERIOD_DAYS_MISSING: this period has no frozen day calendar, so "
                    "the Minimum/Maximum pay rule(s) "
                    + ", ".join(
                        str(rule.driver_pay_rule_id) for rule in (minimum, maximum)
                        if rule is not None)
                    + " cannot be prorated."])
            continue

        numerator, denominator = minmax.coverage(
            scheduled, eligible.get(assignment_id, ()), off_days.get(assignment_id, ()))
        if denominator == 0 or numerator == 0:
            # Neither rule applies: no top-up, no cap, the Driver is paid exactly what was
            # earned. Only a period with no scheduled day at all is worth a warning.
            outcome = _MinMaxOutcome(proration(
                minmax.NO_SCHEDULED_DAYS if denominator == 0 else minmax.NO_ELIGIBLE_SCHEDULED_DAYS,
                numerator=numerator, denominator=denominator,
                minimum_rule=minimum, maximum_rule=maximum))
            if denominator == 0:
                ids = ", ".join(
                    f"{label} {rule.driver_pay_rule_id}"
                    for label, rule in (("Minimum", minimum), ("Maximum", maximum))
                    if rule is not None)
                outcome.warnings.append(
                    f"Driver {assignment_id} (DriverID {driver_id}): this period has no scheduled "
                    f"days, so the pay rule(s) {ids} were not applied.")
            outcomes[assignment_id] = outcome
            continue

        if minimum is not None:
            minimum = replace(
                minimum, prorated_amount=minmax.prorate(minimum.amount, numerator, denominator))
        if maximum is not None:
            maximum = replace(
                maximum, prorated_amount=minmax.prorate(maximum.amount, numerator, denominator))
        outcomes[assignment_id] = _MinMaxOutcome(
            proration(minmax.APPLIED, numerator=numerator, denominator=denominator,
                      minimum_rule=minimum, maximum_rule=maximum),
            raw_minimum=minmax.raw_minimum_adjustment(
                base, None if minimum is None else minimum.prorated_amount),
            raw_maximum=minmax.raw_maximum_adjustment(
                base, None if maximum is None else maximum.prorated_amount),
        )
    return outcomes


def _minmax_line(
    kind_line_type: str, rule: _MinMaxRule, proration: _MinMaxProration,
    raw: Decimal, official: Decimal,
) -> _CalculationPacketLine:
    return _CalculationPacketLine(
        source_type="System",
        source_id=str(rule.driver_pay_rule_id),
        line_type=kind_line_type,
        line_scope="Period",
        work_date=None,
        driver_assignment_id=proration.driver_assignment_id,
        quantity=Decimal("1"),
        resolved_rate_amount=None,
        calculated_amount=official,
        needs_manager_review=False,
        blocker_reason=None,
        source_evidence={
            "DriverPayRuleID": rule.driver_pay_rule_id,
            "RuleType": rule.rule_type,
            "RuleAmount": rule.amount,
            "RuleStatus": rule.status,
            "EffectiveFrom": rule.effective_from,
            "EffectiveTo": rule.effective_to,
            "NormalBase": proration.base_amount,
            "ProrationVersion": proration.version,
            "ProrationNumerator": proration.numerator,
            "ProrationDenominator": proration.denominator,
            "ProratedAmount": rule.prorated_amount,
            "PreRoundingAdjustment": raw,
            "OfficialAdjustment": official,
        },
    )


async def _build_live_calculation_packet(
    period: PeriodSummary,
    company_id: int,
    db: AsyncConnection,
) -> _LiveCalculationPacket:
    """
    Assemble live financial results without writes, audit, or projection refresh.

    Ordinary source money comes from load_live_source_lines and its frozen-method
    calculation boundary. Status pay resolves current rates from canonical day
    selections, never the stored compatibility projection, to avoid stale money
    and double counting. Minimum/maximum adjustments exclude Active Bonus events
    from their base; Bonus is added afterward.

    Official amounts (payable-rounding-v1): raw line contributions stay at internal
    precision and are summed per component (ordinary: DriverAssignment x period
    definition; Status: DriverAssignment x status rate column; Bonus: DriverAssignment;
    Min/Max: the adjustment), then rounded once with ``round_payable`` to the Company
    currency. A driver total is the exact sum of its official components and is never
    re-rounded.

    Include only Drivers with ordinary source, live Status pay, or Active Bonus
    events, not the full eligible roster. Lifecycle/read callers select when
    this live packet is appropriate; it is not immutable approval evidence.
    """
    period_id = period.payroll_period_id

    blockers: list[str] = []
    warnings: list[str] = []

    # Surface source, eligibility, and rate-mapping blockers in the live packet
    # so an incomplete calculation cannot appear financially ready.
    blockers.extend(await _validate_period_can_finalize(
        period_id=period_id,
        company_id=company_id,
        branch_id=period.branch_id,
        db=db,
    ))

    # ── CP-4D completeness guard: a legacy DailyStatus DraftLine with no
    # canonical entry-state row would silently vanish from the immutable
    # Status evidence captured at Submit/Resubmit (the snapshot evidence reader
    # reads canonical rows only). Block until the day is re-saved through
    # the Day Grid so the Status is canonically represented.
    blockers.extend(await _validate_legacy_status_canonicalized(
        period=period,
        company_id=company_id,
        db=db,
    ))

    # The Company currency decides the one payable rounding. It is read here, once; a
    # Company without a currency can hold no monetary state (every monetary writer fails
    # closed), so every raw amount below is zero and nothing needs rounding.
    currency = await get_company_currency(company_id, db)
    digits = None if currency is None else currency.minor_unit_digits

    def official(amount: Decimal) -> Decimal:
        if digits is None:
            if amount != 0:
                raise currency_error(
                    "COMPANY_CURRENCY_INVARIANT_VIOLATION",
                    "A monetary amount exists for a Company without a configured currency.",
                    409,
                )
            return Decimal("0")
        return round_payable(amount, digits)

    # ── Ordinary daily lines: live, derived from the effective DriverRateSchedule
    # (never from a stored column). The persisted STATUS_PAYMENT/STATUS_PAY
    # compatibility projection is not a target ordinary line and is excluded.
    driver_names: dict[int, str | None] = {}
    driver_numbers: dict[int, str | None] = {}
    driver_permanent_ids: dict[int, int] = {}
    driver_line_nmr: dict[int, bool] = {}
    driver_lines: dict[int, list[_CalculationPacketLine]] = {}
    ordinary: dict[tuple[int, int], _Accumulator] = {}
    status_pay: dict[tuple[int, int | None], _Accumulator] = {}
    bonus: dict[int, _Accumulator] = {}
    driver_bonus_events: dict[int, list[tuple[int, Decimal]]] = {}

    for line in await load_live_source_lines(period_id, company_id, db):
        drv = line.driver_assignment_id
        driver_lines.setdefault(drv, [])
        calc = line.calculation
        if calc.needs_attention:
            driver_line_nmr[drv] = True
        amount = calc.amount if calc.amount is not None else Decimal("0")

        definition = line.definition
        component = ordinary.setdefault(
            (drv, definition.payroll_period_definition_id),
            _Accumulator("ORDINARY", drv, {
                "payroll_period_definition_id": definition.payroll_period_definition_id,
                "pay_definition_id": definition.pay_definition_id,
                "calculation_method": definition.calculation_method,
                "calculation_method_version": definition.calculation_method_version,
            }))
        component.add(calc.amount, str(line.draft_line_id), unresolved=calc.needs_attention)
        driver_lines[drv].append(_CalculationPacketLine(
            source_type="DraftLine",
            source_id=str(line.draft_line_id),
            line_type=None,
            line_scope="Daily",
            work_date=line.work_date,
            driver_assignment_id=drv,
            quantity=line.quantity,
            resolved_rate_amount=None,
            calculated_amount=calc.amount,
            needs_manager_review=calc.needs_attention,
            blocker_reason=_CALCULATION_BLOCKER_REASONS.get(calc.status),
            payroll_period_definition_id=definition.payroll_period_definition_id,
            pay_definition_id=definition.pay_definition_id,
            definition_name=definition.name,
            calculation_status=calc.status.value,
            source_evidence={
                "DraftLineID": line.draft_line_id,
                "PayrollPeriodDefinitionID": definition.payroll_period_definition_id,
                "PayDefinitionID": definition.pay_definition_id,
                "RateDefinitionID": definition.rate_definition_id,
                "CalculationMethod": definition.calculation_method,
                "CalculationMethodVersion": definition.calculation_method_version,
                "CalculationStatus": calc.status.value,
                "DriverRateScheduleID": calc.driver_rate_schedule_id,
                "CalculationVersion": calc.calculation_version,
            },
            snapshot_source_type="DraftLine",
            snapshot_source_id=str(line.draft_line_id),
            snapshot_calculated_amount=amount,
        ))

    # ── Canonical live Status-derived pay (CP-4B) — never the stored
    # STATUS_PAYMENT/STATUS_PAY projection, which was already excluded above.
    live_status_lines = await _resolve_live_status_payment_lines(
        period_id, company_id, period.branch_id, db,
    )
    for sl in live_status_lines:
        drv = sl.driver_assignment_id
        driver_names.setdefault(drv, None)
        driver_lines.setdefault(drv, [])

        if sl.needs_manager_review:
            driver_line_nmr[drv] = True

        source_id = f"STATUS_LIVE:{drv}:{sl.work_date}:{sl.status_key_id}"
        status_pay.setdefault(
            (drv, sl.status_rate_column_id),
            _Accumulator("STATUS", drv, {"status_rate_column_id": sl.status_rate_column_id}),
        ).add(sl.calculated_amount, source_id, unresolved=sl.needs_manager_review)
        driver_lines[drv].append(_CalculationPacketLine(
            source_type="StatusEntryState",
            source_id=source_id,
            line_type=sl.line_type,
            line_scope="Daily",
            work_date=sl.work_date,
            driver_assignment_id=drv,
            quantity=sl.hours_value,
            resolved_rate_amount=sl.resolved_rate_amount,
            calculated_amount=sl.calculated_amount,
            needs_manager_review=sl.needs_manager_review,
            blocker_reason=(
                "No applicable approved DriverRate found for this driver's "
                "selected Status as of its work date."
                if sl.needs_manager_review else None
            ),
            rate_type_id=sl.rate_type_id,
            rate_column_id=sl.status_rate_column_id,
            driver_rate_id=sl.driver_rate_id,
            source_evidence={
                "PayrollPeriodDriverDayEntryStateID": sl.entry_state_id,
                "StatusKeyID": sl.status_key_id,
                "StatusCode": sl.status_code,
                "StatusRateColumnID": sl.status_rate_column_id,
                "HoursValue": sl.hours_value,
                "WorkDate": sl.work_date,
                "RateTypeID": sl.rate_type_id,
                "DriverRateID": sl.driver_rate_id,
            },
            snapshot_source_type="StatusEntryState",
            snapshot_source_id=str(sl.entry_state_id),
        ))

    # ── Canonical Active bonus (never Voided).
    for b in await payroll_bonus._load_active_bonus_events(period_id, company_id, db):
        drv = int(b["driverassignmentid"])
        driver_names.setdefault(drv, b["drivername"])
        driver_numbers.setdefault(drv, b["drivernumber"])
        driver_permanent_ids.setdefault(drv, int(b["driverid"]))
        driver_lines.setdefault(drv, [])
        amt = Decimal(str(b["amount"]))
        bonus.setdefault(drv, _Accumulator("BONUS", drv)).add(amt, str(b["payrollbonuseventid"]))
        driver_bonus_events.setdefault(drv, []).append((int(b["payrollbonuseventid"]), amt))
        driver_lines[drv].append(_CalculationPacketLine(
            source_type="BonusEvent",
            source_id=str(b["payrollbonuseventid"]),
            line_type="BONUS",
            line_scope="Period",
            work_date=None,
            driver_assignment_id=drv,
            quantity=None,
            resolved_rate_amount=None,
            calculated_amount=amt,
            needs_manager_review=False,
            blocker_reason=None,
            bonus_event_id=int(b["payrollbonuseventid"]),
            source_evidence={
                "PayrollBonusEventID": int(b["payrollbonuseventid"]),
                "Amount": amt,
                "Reason": b["reason"],
                "Notes": b["notes"],
                "DataRevision": b["datarevision"],
                "Status": "Active",
            },
        ))

    # ── Financial-source-driven driver union (CP-4B: not a full roster).
    all_driver_assignment_ids = (
        {drv for drv, _ in ordinary} | {drv for drv, _ in status_pay} | set(bonus)
    )

    if not all_driver_assignment_ids:
        warnings.append("No current financial source lines for this period.")
    else:
        if currency is None:
            blockers.append(
                "COMPANY_CURRENCY_REQUIRED: configure the Company currency before payroll "
                "amounts can be made official.")
        driver_assignment_identity_result = await db.execute(
            text("""
                SELECT a.driverassignmentid, a.driverid, d.drivernumber, e.fullname
                FROM core.driverassignments a
                JOIN core.drivers d ON d.driverid = a.driverid
                JOIN core.employees e ON e.employeeid = a.employeeid
                WHERE a.companyid = :cid
                  AND a.driverassignmentid = ANY(:driver_assignment_ids)
            """),
            {"cid": company_id, "driver_assignment_ids": sorted(all_driver_assignment_ids)},
        )
        for identity in driver_assignment_identity_result.mappings().all():
            driver_names.setdefault(int(identity["driverassignmentid"]), identity["fullname"])
            driver_numbers.setdefault(int(identity["driverassignmentid"]), identity["drivernumber"])
            driver_permanent_ids.setdefault(int(identity["driverassignmentid"]), int(identity["driverid"]))

    # ── Official ordinary, Status and Bonus components: each raw sum rounded once.
    driver_components: dict[int, list[_OfficialComponent]] = {
        drv: [] for drv in all_driver_assignment_ids}
    driver_daily: dict[int, Decimal] = {}
    driver_status: dict[int, Decimal] = {}
    driver_bonus: dict[int, Decimal] = {}
    driver_blockers: dict[int, list[str]] = {}
    for (drv, _), accumulator in sorted(ordinary.items(), key=lambda item: item[0]):
        component = accumulator.official(official)
        driver_components[drv].append(component)
        driver_daily[drv] = driver_daily.get(drv, Decimal("0")) + component.official_amount
    for (drv, _), accumulator in sorted(
        status_pay.items(), key=lambda item: (item[0][0], item[0][1] or 0),
    ):
        component = accumulator.official(official)
        driver_components[drv].append(component)
        driver_status[drv] = driver_status.get(drv, Decimal("0")) + component.official_amount
    for drv, accumulator in sorted(bonus.items()):
        component = accumulator.official(official)
        driver_components[drv].append(component)
        driver_bonus[drv] = component.official_amount
        if digits is not None:
            for event_id, amt in driver_bonus_events[drv]:
                if not is_payable_precision(amt, digits):
                    driver_blockers.setdefault(drv, []).append(
                        f"SOURCE_AMOUNT_PRECISION: bonus event {event_id} amount {amt} has more "
                        f"decimals than the Company currency allows ({digits}). Edit the "
                        f"bonus and enter the amount again.")

    # ── Minimum/maximum: same as-of-period-start rule and ordering as
    # get_finalization_preview — normal base excludes bonus by construction;
    # bonus is added back in only after minimum/maximum is applied (CP-3C).
    assignment_driver = {drv: driver_permanent_ids[drv] for drv in all_driver_assignment_ids}
    bases = {
        drv: driver_daily.get(drv, Decimal("0")) + driver_status.get(drv, Decimal("0"))
        for drv in all_driver_assignment_ids
    }
    outcomes = (
        await _evaluate_minmax(period, company_id, assignment_driver, bases, currency, db)
        if all_driver_assignment_ids else {}
    )

    # ── Assemble driver totals.
    driver_totals: list[_CalculationPacketDriverTotal] = []
    for drv_id in sorted(all_driver_assignment_ids):
        daily = driver_daily.get(drv_id, Decimal("0"))
        status_total = driver_status.get(drv_id, Decimal("0"))
        bonus_total = driver_bonus.get(drv_id, Decimal("0"))
        outcome = outcomes[drv_id]
        proration = outcome.proration
        driver_blockers.setdefault(drv_id, []).extend(outcome.blockers)
        warnings.extend(outcome.warnings)

        components = driver_components[drv_id]
        min_adj = max_adj = Decimal("0")
        for raw, rule, component_kind, line_type in (
            (outcome.raw_minimum, proration.minimum_rule, "MINIMUM_ADJUSTMENT", "SYS_MIN_TOPUP"),
            (outcome.raw_maximum, proration.maximum_rule, "MAXIMUM_ADJUSTMENT", "SYS_MAX_CAP"),
        ):
            if raw == 0 or rule is None:
                continue
            adjustment = official(raw)
            components.append(_OfficialComponent(
                kind=component_kind,
                driver_assignment_id=drv_id,
                pre_rounding_amount=raw,
                official_amount=adjustment,
                driver_pay_rule_id=rule.driver_pay_rule_id,
            ))
            if component_kind == "MINIMUM_ADJUSTMENT":
                min_adj = adjustment
            else:
                max_adj = adjustment
            if adjustment != 0:
                driver_lines[drv_id].append(_minmax_line(line_type, rule, proration, raw, adjustment))

        expected_pay = sum((c.official_amount for c in components), Decimal("0"))
        drv_blockers = driver_blockers.get(drv_id, [])
        drv_nmr = driver_line_nmr.get(drv_id, False)

        driver_totals.append(_CalculationPacketDriverTotal(
            driver_assignment_id=drv_id,
            driver_id=driver_permanent_ids[drv_id],
            driver_number=driver_numbers.get(drv_id),
            driver_name=driver_names.get(drv_id),
            daily_pay=daily,
            status_pay=status_total,
            minimum_adjustment=min_adj,
            maximum_adjustment=max_adj,
            bonus_total=bonus_total,
            expected_pay=expected_pay,
            needs_manager_review=drv_nmr,
            blockers=drv_blockers,
            lines=driver_lines.get(drv_id, []),
            components=components,
            minmax=proration,
        ))
        if drv_blockers:
            blockers.extend(f"Driver {drv_id}: {b}" for b in drv_blockers)
        if drv_nmr:
            blockers.append(
                f"Driver {drv_id}: one or more lines require manager review "
                f"(calculation unresolved or manually flagged)."
            )

    total_expected_pay = sum((dt.expected_pay for dt in driver_totals), Decimal("0"))

    return _LiveCalculationPacket(
        payroll_period_id=period_id,
        company_id=company_id,
        branch_id=period.branch_id,
        status=period.status,
        blockers=blockers,
        warnings=warnings,
        drivers=driver_totals,
        total_expected_pay=total_expected_pay,
        currency_code=None if currency is None else currency.code,
        currency_minor_unit_digits=digits,
    )


def _preview_component(component: _OfficialComponent):
    from app.payroll.schemas import CalculationPreviewComponent

    return CalculationPreviewComponent(
        kind=component.kind,
        payroll_period_definition_id=component.payroll_period_definition_id,
        pay_definition_id=component.pay_definition_id,
        status_rate_column_id=component.status_rate_column_id,
        driver_pay_rule_id=component.driver_pay_rule_id,
        pre_rounding_amount=component.pre_rounding_amount,
        official_amount=component.official_amount,
        line_count=len(component.source_ids),
        unresolved_line_count=component.unresolved_line_count,
    )


def _preview_minmax(proration: _MinMaxProration | None):
    from app.payroll.schemas import CalculationPreviewMinMax

    if proration is None:
        return None
    minimum, maximum = proration.minimum_rule, proration.maximum_rule
    return CalculationPreviewMinMax(
        status=proration.status,
        proration_version=proration.version,
        numerator=proration.numerator,
        denominator=proration.denominator,
        base_amount=proration.base_amount,
        minimum_rule_id=None if minimum is None else minimum.driver_pay_rule_id,
        minimum_rule_amount=None if minimum is None else minimum.amount,
        prorated_minimum=None if minimum is None else minimum.prorated_amount,
        maximum_rule_id=None if maximum is None else maximum.driver_pay_rule_id,
        maximum_rule_amount=None if maximum is None else maximum.amount,
        prorated_maximum=None if maximum is None else maximum.prorated_amount,
    )


async def get_calculation_preview(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> CalculationPreviewResponse:
    """Adapt the shared live packet to CP-4B's unchanged public contract."""
    from app.payroll.schemas import (
        CalculationPreviewDriverTotal,
        CalculationPreviewLine,
        CalculationPreviewResponse,
    )

    await require_non_driver_subject(company_id, user_id, db)
    period = await get_period_by_id(company_id, user_id, period_id, db)
    if period.status not in ENTRY_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                "Calculation preview requires an Open or Returned period. "
                f"Current status: '{period.status}'."
            ),
        )
    await check_any_permission(
        company_id, user_id, period.branch_id, ["payroll.view", "payroll.entry"], db,
    )
    packet = await _build_live_calculation_packet(period, company_id, db)
    return CalculationPreviewResponse(
        currency_code=packet.currency_code,
        currency_minor_unit_digits=packet.currency_minor_unit_digits,
        payable_rounding_version=packet.payable_rounding_version,
        minmax_proration_version=packet.minmax_proration_version,
        payroll_period_id=packet.payroll_period_id,
        company_id=packet.company_id,
        branch_id=packet.branch_id,
        branch_name=period.branch_name,
        status=packet.status,
        provisional=True,
        financials_available=True,
        has_blockers=bool(packet.blockers),
        blockers=packet.blockers,
        warnings=packet.warnings,
        drivers=[
            CalculationPreviewDriverTotal(
                driver_assignment_id=driver.driver_assignment_id,
                driver_id=driver.driver_id,
                driver_number=driver.driver_number,
                driver_name=driver.driver_name,
                daily_pay=driver.daily_pay,
                status_pay=driver.status_pay,
                normal_base=driver.daily_pay + driver.status_pay,
                minimum_adjustment=driver.minimum_adjustment,
                maximum_adjustment=driver.maximum_adjustment,
                bonus_total=driver.bonus_total,
                expected_pay=driver.expected_pay,
                needs_manager_review=driver.needs_manager_review,
                blockers=driver.blockers,
                components=[_preview_component(c) for c in driver.components],
                minmax=_preview_minmax(driver.minmax),
                lines=[
                    CalculationPreviewLine(
                        source_type=line.source_type,
                        source_id=line.source_id,
                        line_type=line.line_type,
                        work_date=line.work_date,
                        payroll_period_definition_id=line.payroll_period_definition_id,
                        pay_definition_id=line.pay_definition_id,
                        definition_name=line.definition_name,
                        calculation_status=line.calculation_status,
                        rate_column_id=line.rate_column_id,
                        driver_assignment_id=line.driver_assignment_id,
                        quantity=line.quantity,
                        resolved_rate=line.resolved_rate_amount,
                        calculated_amount=line.calculated_amount,
                        needs_manager_review=line.needs_manager_review,
                        blocker_reason=line.blocker_reason,
                    )
                    for line in driver.lines
                ],
            )
            for driver in packet.drivers
        ],
        total_expected_pay=packet.total_expected_pay,
    )
