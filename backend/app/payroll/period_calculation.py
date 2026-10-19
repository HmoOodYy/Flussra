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

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.permissions import check_any_permission
from app.access.policy import require_non_driver_subject
from app.company_currency import get_company_currency
from app.payroll import bonus as payroll_bonus
from app.payroll.definition_calculation import CalculationStatus
from app.payroll.eligibility import require_period_eligibility_snapshot
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

    # ── Ordinary daily lines: live, derived from the effective DriverRateSchedule
    # (never from a stored column). The persisted STATUS_PAYMENT/STATUS_PAY
    # compatibility projection is not a target ordinary line and is excluded.
    driver_names: dict[int, str | None] = {}
    driver_numbers: dict[int, str | None] = {}
    driver_permanent_ids: dict[int, int] = {}
    driver_daily: dict[int, Decimal] = {}
    driver_status: dict[int, Decimal] = {}
    driver_bonus: dict[int, Decimal] = {}
    driver_line_nmr: dict[int, bool] = {}
    driver_lines: dict[int, list[_CalculationPacketLine]] = {}

    for line in await load_live_source_lines(period_id, company_id, db):
        drv = line.driver_assignment_id
        driver_lines.setdefault(drv, [])
        calc = line.calculation
        if calc.needs_attention:
            driver_line_nmr[drv] = True
        amount = calc.amount if calc.amount is not None else Decimal("0")
        driver_daily[drv] = driver_daily.get(drv, Decimal("0")) + amount

        definition = line.definition
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

        amt = sl.calculated_amount if sl.calculated_amount is not None else Decimal("0")
        driver_status[drv] = driver_status.get(drv, Decimal("0")) + amt

        if sl.needs_manager_review:
            driver_line_nmr[drv] = True

        driver_lines[drv].append(_CalculationPacketLine(
            source_type="StatusEntryState",
            source_id=f"STATUS_LIVE:{drv}:{sl.work_date}:{sl.status_key_id}",
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
        driver_bonus[drv] = driver_bonus.get(drv, Decimal("0")) + amt
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
        set(driver_daily) | set(driver_status) | set(driver_bonus)
    )

    if not all_driver_assignment_ids:
        warnings.append("No current financial source lines for this period.")
    else:
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

    # ── Minimum/maximum: same as-of-period-start rule and ordering as
    # get_finalization_preview — normal base excludes bonus by construction;
    # bonus is added back in only after minimum/maximum is applied (CP-3C).
    period_start = period.start_date
    driver_blockers: dict[int, list[str]] = {}
    driver_min_adj: dict[int, Decimal] = {}
    driver_max_adj: dict[int, Decimal] = {}
    driver_min_rule: dict[int, Any] = {}
    driver_max_rule: dict[int, Any] = {}

    for drv_id in all_driver_assignment_ids:
        normal_base = (
            driver_daily.get(drv_id, Decimal("0"))
            + driver_status.get(drv_id, Decimal("0"))
        )

        min_row = (await db.execute(
            text("""
                SELECT driverpayruleid, amount, status, effectivefrom, effectiveto,
                       calendarfrequency, calendaranchorstartdate, calendarcustomintervaldays
                FROM payroll.driverpayrules
                WHERE  driverid = (SELECT da.driverid FROM core.driverassignments da
                                   WHERE da.driverassignmentid = :did)
                  AND  companyid     = :cid
                  AND  ruletype      = 'MinimumPay'
                  AND  status        IN ('Active', 'Ended')
                  AND  effectivefrom <= :as_of
                  AND  (effectiveto IS NULL OR effectiveto >= :as_of)
                ORDER BY effectivefrom DESC
                LIMIT 1
            """),
            {"did": drv_id, "cid": company_id, "as_of": period_start},
        )).mappings().first()
        max_row = (await db.execute(
            text("""
                SELECT driverpayruleid, amount, status, effectivefrom, effectiveto,
                       calendarfrequency, calendaranchorstartdate, calendarcustomintervaldays
                FROM payroll.driverpayrules
                WHERE  driverid = (SELECT da.driverid FROM core.driverassignments da
                                   WHERE da.driverassignmentid = :did)
                  AND  companyid     = :cid
                  AND  ruletype      = 'MaximumPay'
                  AND  status        IN ('Active', 'Ended')
                  AND  effectivefrom <= :as_of
                  AND  (effectiveto IS NULL OR effectiveto >= :as_of)
                ORDER BY effectivefrom DESC
                LIMIT 1
            """),
            {"did": drv_id, "cid": company_id, "as_of": period_start},
        )).mappings().first()

        # A rule applies to a period only when the period is exactly one period of the
        # calendar the rule recorded (the home pay calendar it was set for). Anything else is
        # a blocker for this driver: never rescaled, never silently dropped or duplicated.
        mismatched = [
            (label, row) for label, row in (("Minimum", min_row), ("Maximum", max_row))
            if row is not None and not period_matches_schedule(
                Schedule(row["calendarfrequency"], row["calendaranchorstartdate"],
                         row["calendarcustomintervaldays"], 0),
                period.start_date, period.end_date)
        ]
        if mismatched:
            for label, row in mismatched:
                driver_blockers.setdefault(drv_id, []).append(
                    f"MINMAX_CALENDAR_MISMATCH: the {label} pay rule {int(row['driverpayruleid'])} "
                    f"was set for a {row['calendarfrequency']} pay calendar that does not "
                    f"match this period ({period.start_date} to {period.end_date}). "
                    f"End the rule and create a new one for the Driver's current calendar.")
            driver_min_adj[drv_id] = Decimal("0")
            driver_max_adj[drv_id] = Decimal("0")
            continue

        min_amount = Decimal(str(min_row["amount"])) if min_row else None
        max_amount = Decimal(str(max_row["amount"])) if max_row else None
        if min_row is not None:
            driver_min_rule[drv_id] = min_row
        if max_row is not None:
            driver_max_rule[drv_id] = max_row

        if min_amount is not None and max_amount is not None and min_amount > max_amount:
            driver_blockers.setdefault(drv_id, []).append(
                f"Minimum pay ({min_amount}) exceeds maximum pay ({max_amount}). "
                f"Correct the pay rules before this driver's total can be trusted."
            )
            driver_min_adj[drv_id] = Decimal("0")
            driver_max_adj[drv_id] = Decimal("0")
            continue

        if min_amount is not None and normal_base < min_amount:
            driver_min_adj[drv_id] = min_amount - normal_base
        else:
            driver_min_adj[drv_id] = Decimal("0")

        if max_amount is not None and normal_base > max_amount:
            driver_max_adj[drv_id] = max_amount - normal_base
        else:
            driver_max_adj[drv_id] = Decimal("0")

    # ── Assemble driver totals.
    driver_totals: list[_CalculationPacketDriverTotal] = []
    for drv_id in sorted(all_driver_assignment_ids):
        daily = driver_daily.get(drv_id, Decimal("0"))
        status_pay = driver_status.get(drv_id, Decimal("0"))
        normal_base = daily + status_pay
        min_adj = driver_min_adj.get(drv_id, Decimal("0"))
        max_adj = driver_max_adj.get(drv_id, Decimal("0"))
        bonus = driver_bonus.get(drv_id, Decimal("0"))
        expected_pay = normal_base + min_adj + max_adj + bonus
        drv_blockers = driver_blockers.get(drv_id, [])
        drv_nmr = driver_line_nmr.get(drv_id, False)

        if min_adj != 0:
            min_rule = driver_min_rule[drv_id]
            driver_lines[drv_id].append(_CalculationPacketLine(
                source_type="System",
                source_id=str(min_rule["driverpayruleid"]),
                line_type="SYS_MIN_TOPUP",
                line_scope="Period",
                work_date=None,
                driver_assignment_id=drv_id,
                quantity=Decimal("1"),
                resolved_rate_amount=None,
                calculated_amount=min_adj,
                needs_manager_review=False,
                blocker_reason=None,
                source_evidence={
                    "DriverPayRuleID": int(min_rule["driverpayruleid"]),
                    "RuleType": "MinimumPay",
                    "RuleAmount": Decimal(str(min_rule["amount"])),
                    "RuleStatus": min_rule["status"],
                    "EffectiveFrom": min_rule["effectivefrom"],
                    "EffectiveTo": min_rule["effectiveto"],
                    "NormalBase": normal_base,
                },
            ))
        if max_adj != 0:
            max_rule = driver_max_rule[drv_id]
            driver_lines[drv_id].append(_CalculationPacketLine(
                source_type="System",
                source_id=str(max_rule["driverpayruleid"]),
                line_type="SYS_MAX_CAP",
                line_scope="Period",
                work_date=None,
                driver_assignment_id=drv_id,
                quantity=Decimal("1"),
                resolved_rate_amount=None,
                calculated_amount=max_adj,
                needs_manager_review=False,
                blocker_reason=None,
                source_evidence={
                    "DriverPayRuleID": int(max_rule["driverpayruleid"]),
                    "RuleType": "MaximumPay",
                    "RuleAmount": Decimal(str(max_rule["amount"])),
                    "RuleStatus": max_rule["status"],
                    "EffectiveFrom": max_rule["effectivefrom"],
                    "EffectiveTo": max_rule["effectiveto"],
                    "NormalBase": normal_base,
                },
            ))

        driver_totals.append(_CalculationPacketDriverTotal(
            driver_assignment_id=drv_id,
            driver_id=driver_permanent_ids[drv_id],
            driver_number=driver_numbers.get(drv_id),
            driver_name=driver_names.get(drv_id),
            daily_pay=daily,
            status_pay=status_pay,
            minimum_adjustment=min_adj,
            maximum_adjustment=max_adj,
            bonus_total=bonus,
            expected_pay=expected_pay,
            needs_manager_review=drv_nmr,
            blockers=drv_blockers,
            lines=driver_lines.get(drv_id, []),
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
    currency = await get_company_currency(company_id, db)
    return CalculationPreviewResponse(
        currency_code=None if currency is None else currency.code,
        currency_minor_unit_digits=None if currency is None else currency.minor_unit_digits,
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
