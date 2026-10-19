"""
Driver MinimumPay/MaximumPay rule administration and audit.

A pay rule is owned by the permanent Driver (Durable Driver Stage 2), not by a Branch or a
DriverAssignment. It records the home pay calendar it was set for (frequency, anchor,
custom interval) as a compatibility key; the server captures it from the Payroll Setup of
the Branch of the rule's context tenure and no client ever supplies it. The calendar is
never a rescaling basis: at calculation a rule applies to a period only when the period is
exactly one period of the rule's calendar (``MINMAX_CALENDAR_MISMATCH`` otherwise).

Authority, visibility and lock order follow ``app.compensation.driver_context``:

* create uses the rule's start date, end uses the first date whose resolved rule changes
  (the day after the earlier of the old and new end), void uses the rule's start date; each
  needs the rate-edit permission on the context tenure's Branch or company-wide, and is
  refused with 409 ``DRIVER_COMPENSATION_HISTORICAL`` when that tenure has ended;
* a notes edit has no payroll effect and needs only authority over the Driver now;
* every payroll-effective writer takes the Company currency, the Driver's assignments, the
  Branch workflow locks, then the rule row FOR UPDATE, reads one decision instant, and only
  then applies the canonical finalized guard (``PeriodStart`` mode: a Locked or Archived
  period of ANY of the Driver's assignments, in any Branch, whose start date is inside the
  footprint) and the DML. Pay rules have no RateDefinition, so that lock is skipped.
"""
import json
from datetime import date, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.compensation.driver_context import (
    ReadScope,
    driver_compensation_read_scope,
    load_driver_tenures,
    lock_driver_compensation_write,
    require_compensation_authority,
)
from app.payroll.guards import require_no_finalized_payroll
from app.payroll.schemas import DriverPayRuleCreate, DriverPayRuleSummary
from app.payroll_setup.chronology import (
    Schedule,
    period_end,
    period_matches_schedule,
    previous_period_start,
)
from app.payroll_setup.errors import PolicyError
from app.payroll_setup.resolver import resolve_effective_setup_version
from app.workforce.ops import decision_instant

# ===========================================================================
# M15 — Driver Pay Rules (Minimum / Maximum Pay)
# ===========================================================================

_RULE_AUDIT_REASONS: dict[str, str] = {
    "DRIVER_PAY_RULE_CREATED":       "Driver pay rule created",
    "DRIVER_PAY_RULE_ENDED":         "Driver pay rule ended",
    "DRIVER_PAY_RULE_NOTES_UPDATED": "Driver pay rule notes updated",
    "DRIVER_PAY_RULE_VOIDED":        "Driver pay rule voided",
}

_OVERLAP_CONSTRAINTS = ("excl_driverpayrules_no_date_overlap", "ux_driverpayrules_driver_type_openended")


async def _write_pay_rule_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    branch_id: int | None,
    user_id: int,
    rule_id: int,
    action_code: str,
    old_value: dict | None = None,
    new_value: dict | None = None,
) -> None:
    """
    Insert one audit.AuditLog row for a DriverPayRule event.

    ``branch_id`` is the Branch of the context tenure of the change, when it has one: a rule
    belongs to the Driver, so a Branch is context, not ownership.

    Extracted as a module-level function so tests can monkeypatch it to verify
    that all preceding writes roll back when this raises.
    """
    await db.execute(
        text("""
            INSERT INTO audit.auditlog
                (companyid, branchid, actoruserid, actioncode,
                 entityschema, entityname, entityid,
                 oldvaluejson, newvaluejson, reason, sourcetype)
            VALUES
                (:company_id, :branch_id, :actor_id, :action_code,
                 'payroll', 'DriverPayRules', :entity_id,
                 :old_val, :new_val, :reason, 'Application')
        """),
        {
            "company_id":  company_id,
            "branch_id":   branch_id,
            "actor_id":    user_id,
            "action_code": action_code,
            "entity_id":   str(rule_id),
            "old_val":     json.dumps(old_value) if old_value is not None else None,
            "new_val":     json.dumps(new_value) if new_value is not None else None,
            "reason":      _RULE_AUDIT_REASONS.get(action_code, action_code),
        },
    )


_RULE_SELECT = """
    SELECT
        r.driverpayruleid,
        r.companyid,
        r.driverid,
        r.ruletype,
        r.amount,
        r.effectivefrom,
        r.effectiveto,
        r.status,
        r.calendarfrequency,
        r.calendaranchorstartdate,
        r.calendarcustomintervaldays,
        r.calendarsourcebranchid,
        r.calendarsourcepayrollsetupversionid,
        r.createdbyuserid,
        r.createdatutc,
        r.updatedbyuserid,
        r.updatedatutc,
        r.notes
    FROM payroll.driverpayrules r
"""


def _rule_row_to_summary(r: Any, reconciliation: date | None = None) -> DriverPayRuleSummary:
    return DriverPayRuleSummary(
        driver_pay_rule_id=r["driverpayruleid"],
        company_id=r["companyid"],
        driver_id=r["driverid"],
        rule_type=r["ruletype"],
        amount=r["amount"],
        effective_from=r["effectivefrom"],
        effective_to=r["effectiveto"],
        status=r["status"],
        calendar_frequency=r["calendarfrequency"],
        calendar_anchor_start_date=r["calendaranchorstartdate"],
        calendar_custom_interval_days=r["calendarcustomintervaldays"],
        calendar_source_branch_id=r["calendarsourcebranchid"],
        calendar_source_payroll_setup_version_id=r["calendarsourcepayrollsetupversionid"],
        calendar_reconciliation_needed_from=reconciliation,
        created_by_user_id=r["createdbyuserid"],
        created_at_utc=r["createdatutc"],
        updated_by_user_id=r["updatedbyuserid"],
        updated_at_utc=r["updatedatutc"],
        notes=r["notes"],
    )


def _rule_schedule(row: Any) -> Schedule:
    """The rule's recorded calendar as a schedule (the days-off mask is not part of it)."""
    return Schedule(
        row["calendarfrequency"], row["calendaranchorstartdate"],
        row["calendarcustomintervaldays"], 0,
    )


async def _reconciliation_dates(
    rows: list[Any], company_id: int, db: AsyncConnection,
) -> dict[int, date | None]:
    """Per Active/Ended rule: the earliest start of a current or pending tenure, inside the
    rule's window, whose Branch's Payroll Setup calendar is not compatible with the rule's.

    The tenure's calendar is the setup period (of the Branch's schedule in force that day)
    that contains the tenure's first day; it is compatible when it is exactly one period of the
    rule's calendar. Read-only and advisory: nothing blocks, ends or reschedules anything.
    A Branch with no resolvable setup that day has no calendar to compare and is skipped.
    """
    result: dict[int, date | None] = {}
    tenures_by_driver: dict[int, list] = {}
    instant = None
    for row in rows:
        rule_id = int(row["driverpayruleid"])
        result[rule_id] = None
        if row["status"] not in ("Active", "Ended"):
            continue
        if instant is None:
            instant = await decision_instant(db)
        driver_id = int(row["driverid"])
        if driver_id not in tenures_by_driver:
            tenures_by_driver[driver_id] = await load_driver_tenures(company_id, driver_id, db)
        rule_schedule = _rule_schedule(row)
        for tenure in tenures_by_driver[driver_id]:
            first_day = tenure.first_day
            if (not tenure.is_authority(instant) or first_day < row["effectivefrom"]
                    or (row["effectiveto"] is not None and first_day > row["effectiveto"])):
                continue
            try:
                effective = await resolve_effective_setup_version(
                    company_id, tenure.branch_id, first_day, db)
            except PolicyError:
                continue
            start = previous_period_start(effective.schedule, first_day + timedelta(days=1))
            if start is None:
                continue
            if not period_matches_schedule(
                    rule_schedule, start, period_end(effective.schedule, start)):
                result[rule_id] = first_day
                break
    return result


async def _summaries(
    rows: list[Any], company_id: int, db: AsyncConnection,
) -> list[DriverPayRuleSummary]:
    reconciliation = await _reconciliation_dates(rows, company_id, db)
    return [_rule_row_to_summary(r, reconciliation[int(r["driverpayruleid"])]) for r in rows]


async def _load_rule_row(
    rule_id: int, company_id: int, db: AsyncConnection, *, for_update: bool = False,
) -> Any:
    result = await db.execute(
        text(f"{_RULE_SELECT} WHERE r.driverpayruleid = :rid AND r.companyid = :cid"
             f"{' FOR UPDATE OF r' if for_update else ''}"),
        {"rid": rule_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Driver pay rule not found.")
    return row


async def _get_rule_by_id_internal(
    rule_id: int,
    company_id: int,
    db: AsyncConnection,
) -> DriverPayRuleSummary:
    """Internal fetch — no access check. Used by the writers for their response."""
    row = await _load_rule_row(rule_id, company_id, db)
    return (await _summaries([row], company_id, db))[0]


def _sees(scope: ReadScope, row: Any) -> bool:
    return scope.sees({
        "status": row["status"], "effective_from": row["effectivefrom"],
        "effective_to": row["effectiveto"],
    })


def _require_status(rule: Any, allowed: tuple[str, ...], message: str) -> None:
    if rule["status"] not in allowed:
        raise HTTPException(
            status_code=422, detail=f"{message} (current status: '{rule['status']}').")


def _overlap_error(rule_type: str, effective_from: date, effective_to: date | None) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=(
            f"A {rule_type} rule already exists for this driver that overlaps "
            f"the requested date range [{effective_from}, {effective_to or 'open'}]. "
            "End the existing rule first, or choose a non-overlapping date range."
        ),
    )


# ---------------------------------------------------------------------------
# Create driver pay rule
# ---------------------------------------------------------------------------

async def create_driver_pay_rule(
    company_id: int,
    user_id: int,
    data: DriverPayRuleCreate,
    db: AsyncConnection,
) -> DriverPayRuleSummary:
    """
    Create a new Active DriverPayRule for the permanent Driver.

    Guards:
      - Authority over the context tenure of ``effective_from`` (see module docstring).
      - The Payroll Setup of that tenure's Branch must define a pay calendar on that date;
        it is captured on the rule (422 ``PAY_RULE_CALENDAR_UNAVAILABLE`` otherwise).
      - Overlap: a friendly pre-check keyed by the Driver, enforced by the DB exclusion.
      - Finalized payroll: no Locked/Archived period of the Driver, in any Branch, may start
        inside ``[effective_from, effective_to]`` (the rule is selected at period start).
      - If both MinimumPay and MaximumPay rules exist for the same period range,
        min <= max is checked at calculation time, not here.
    """
    await require_compensation_authority(
        company_id, user_id, data.driver_id, data.effective_from, db)

    await lock_driver_compensation_write(
        company_id, data.driver_id, None, db, monetary=True, lock_from=data.effective_from)
    instant = await decision_instant(db)
    context = await require_compensation_authority(
        company_id, user_id, data.driver_id, data.effective_from, db, at=instant)

    overlap = await db.execute(
        text("""
            SELECT driverpayruleid
            FROM   payroll.driverpayrules
            WHERE  companyid     = :cid
              AND  driverid      = :did
              AND  ruletype      = :rtype
              AND  status        IN ('Active', 'Ended')
              AND  effectivefrom <= COALESCE(:eff_to, '9999-12-31'::date)
              AND  (effectiveto IS NULL OR effectiveto >= :eff_from)
            LIMIT 1
        """),
        {"cid": company_id, "did": data.driver_id, "rtype": data.rule_type,
         "eff_from": data.effective_from, "eff_to": data.effective_to},
    )
    if overlap.first() is not None:
        raise _overlap_error(data.rule_type, data.effective_from, data.effective_to)

    try:
        setup = await resolve_effective_setup_version(
            company_id, context.branch_id, data.effective_from, db)
    except PolicyError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "PAY_RULE_CALENDAR_UNAVAILABLE",
                "message": (
                    "The pay rule's home pay calendar comes from the Payroll Setup of the "
                    "Branch the Driver belongs to on the start date, and none is in force "
                    f"({exc})."),
            },
        ) from exc
    schedule = setup.schedule

    await require_no_finalized_payroll(
        company_id, data.driver_id, data.effective_from, data.effective_to, "PeriodStart", db)

    try:
        insert_result = await db.execute(
            text("""
                INSERT INTO payroll.driverpayrules
                    (companyid, driverid, ruletype, amount, effectivefrom, effectiveto,
                     status, createdbyuserid, notes,
                     calendarfrequency, calendaranchorstartdate, calendarcustomintervaldays,
                     calendarsourcebranchid, calendarsourcepayrollsetupversionid)
                VALUES
                    (:cid, :did, :rtype, :amount, :eff_from, :eff_to,
                     'Active', :creator, :notes,
                     :cal_freq, :cal_anchor, :cal_interval, :cal_branch, :cal_version)
                RETURNING driverpayruleid, amount
            """),
            {
                "cid":      company_id,
                "did":      data.driver_id,
                "rtype":    data.rule_type,
                "amount":   data.amount,
                "eff_from": data.effective_from,
                "eff_to":   data.effective_to,
                "creator":  user_id,
                "notes":    data.notes,
                "cal_freq": schedule.frequency,
                "cal_anchor": schedule.anchor_start_date,
                "cal_interval": schedule.custom_interval_days,
                "cal_branch": context.branch_id,
                "cal_version": setup.version_id,
            },
        )
    except DBAPIError as exc:
        if any(name in str(exc.orig) for name in _OVERLAP_CONSTRAINTS):
            raise _overlap_error(data.rule_type, data.effective_from, data.effective_to) from exc
        raise
    inserted_rule = insert_result.mappings().one()
    rule_id: int = inserted_rule["driverpayruleid"]

    await _write_pay_rule_audit(
        db,
        company_id=company_id,
        branch_id=context.branch_id,
        user_id=user_id,
        rule_id=rule_id,
        action_code="DRIVER_PAY_RULE_CREATED",
        new_value={
            "driver_id":      data.driver_id,
            "rule_type":      data.rule_type,
            "amount":         str(inserted_rule["amount"]),
            "effective_from": str(data.effective_from),
            "effective_to":   str(data.effective_to) if data.effective_to else None,
            "status":         "Active",
            "calendar": {
                "frequency":     schedule.frequency,
                "anchor_start_date": str(schedule.anchor_start_date),
                "custom_interval_days": schedule.custom_interval_days,
                "source_branch_id": context.branch_id,
                "source_payroll_setup_version_id": setup.version_id,
            },
        },
    )

    return await _get_rule_by_id_internal(rule_id, company_id, db)


# ---------------------------------------------------------------------------
# List / get driver pay rules
# ---------------------------------------------------------------------------

async def get_driver_pay_rules(
    company_id: int,
    user_id: int,
    driver_id: int,
    db: AsyncConnection,
    *,
    rule_type: str | None = None,
    rule_status: str | None = None,
) -> list[DriverPayRuleSummary]:
    """Return the pay rules of a Driver that the caller may read (see ``ReadScope``)."""
    scope = await driver_compensation_read_scope(company_id, user_id, driver_id, db)

    params: dict[str, Any] = {"cid": company_id, "did": driver_id}
    conditions = ["r.companyid = :cid", "r.driverid = :did"]
    if rule_type is not None:
        conditions.append("r.ruletype = :rtype")
        params["rtype"] = rule_type
    if rule_status is not None:
        conditions.append("r.status = :rstatus")
        params["rstatus"] = rule_status

    where = " AND ".join(conditions) + scope.row_filter("r", params)
    result = await db.execute(
        text(f"{_RULE_SELECT} WHERE {where} ORDER BY r.ruletype, r.effectivefrom DESC"),
        params,
    )
    return await _summaries(list(result.mappings().all()), company_id, db)


async def get_driver_pay_rule_by_id(
    rule_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> DriverPayRuleSummary:
    """Return a single pay rule if the caller may read its Driver's history at that window."""
    row = await _load_rule_row(rule_id, company_id, db)
    scope = await driver_compensation_read_scope(company_id, user_id, int(row["driverid"]), db)
    if not _sees(scope, row):
        raise HTTPException(status_code=404, detail="Driver pay rule not found.")
    return (await _summaries([row], company_id, db))[0]


# ---------------------------------------------------------------------------
# End driver pay rule
# ---------------------------------------------------------------------------

async def end_driver_pay_rule(
    rule_id: int,
    company_id: int,
    user_id: int,
    effective_to: date,
    db: AsyncConnection,
) -> DriverPayRuleSummary:
    """
    Close a rule by setting Status='Ended' and EffectiveTo.

    Guards:
      - Rule must be Active (cannot end an already-Ended or Voided rule).
      - effective_to must be >= rule.effective_from.
      - Authority over the context tenure of the first date whose resolved rule changes.
      - No Locked/Archived period of the Driver, in any Branch, may start inside
        ``[effective_to + 1, old effective_to]`` (that would change a finalized selection).
    """
    rule = await _load_rule_row(rule_id, company_id, db)
    _require_status(rule, ("Active",), "Only Active rules can be ended")
    if effective_to < rule["effectivefrom"]:
        raise HTTPException(
            status_code=422,
            detail=f"effective_to ({effective_to}) cannot be before effective_from ({rule['effectivefrom']}).",
        )
    driver_id = int(rule["driverid"])
    old_to = rule["effectiveto"]
    first_changed = (min(effective_to, old_to) if old_to is not None else effective_to) + timedelta(days=1)

    await require_compensation_authority(company_id, user_id, driver_id, first_changed, db)
    await lock_driver_compensation_write(
        company_id, driver_id, None, db, monetary=True, lock_from=first_changed)
    rule = await _load_rule_row(rule_id, company_id, db, for_update=True)
    _require_status(rule, ("Active",), "Only Active rules can be ended")
    instant = await decision_instant(db)
    context = await require_compensation_authority(
        company_id, user_id, driver_id, first_changed, db, at=instant)
    await require_no_finalized_payroll(
        company_id, driver_id, effective_to + timedelta(days=1), rule["effectiveto"],
        "PeriodStart", db)

    await db.execute(
        text("""
            UPDATE payroll.driverpayrules
            SET    status        = 'Ended',
                   effectiveto   = :eff_to,
                   updatedbyuserid = :uid,
                   updatedatutc  = NOW()
            WHERE  driverpayruleid = :rid
              AND  companyid       = :cid
        """),
        {"eff_to": effective_to, "uid": user_id, "rid": rule_id, "cid": company_id},
    )

    await _write_pay_rule_audit(
        db,
        company_id=company_id,
        branch_id=context.branch_id,
        user_id=user_id,
        rule_id=rule_id,
        action_code="DRIVER_PAY_RULE_ENDED",
        old_value={"status": "Active", "effective_to": str(rule["effectiveto"]) if rule["effectiveto"] else None},
        new_value={"status": "Ended", "effective_to": str(effective_to)},
    )

    return await _get_rule_by_id_internal(rule_id, company_id, db)


# ---------------------------------------------------------------------------
# Update notes (notes-only PATCH)
# ---------------------------------------------------------------------------

async def update_driver_pay_rule_notes(
    rule_id: int,
    company_id: int,
    user_id: int,
    notes: "str | None",
    db: AsyncConnection,
) -> DriverPayRuleSummary:
    """
    Update notes on a pay rule. Notes-only — amount/dates/calendar cannot change in place.
    Rule must not be Voided. A notes edit has no payroll effect, so it needs only authority
    over the Driver now (never a historical-tenure refusal).
    """
    rule = await _load_rule_row(rule_id, company_id, db)
    if rule["status"] == "Voided":
        raise HTTPException(status_code=422, detail="Cannot update a Voided rule.")
    driver_id = int(rule["driverid"])
    await require_compensation_authority(company_id, user_id, driver_id, None, db, mode="current")

    await lock_driver_compensation_write(
        company_id, driver_id, None, db, monetary=False, lock_from=None)
    rule = await _load_rule_row(rule_id, company_id, db, for_update=True)
    if rule["status"] == "Voided":
        raise HTTPException(status_code=422, detail="Cannot update a Voided rule.")
    await require_compensation_authority(
        company_id, user_id, driver_id, None, db, mode="current", at=await decision_instant(db))

    await db.execute(
        text("""
            UPDATE payroll.driverpayrules
            SET    notes           = :notes,
                   updatedbyuserid = :uid,
                   updatedatutc    = NOW()
            WHERE  driverpayruleid = :rid
              AND  companyid       = :cid
        """),
        {"notes": notes, "uid": user_id, "rid": rule_id, "cid": company_id},
    )

    await _write_pay_rule_audit(
        db,
        company_id=company_id,
        branch_id=None,
        user_id=user_id,
        rule_id=rule_id,
        action_code="DRIVER_PAY_RULE_NOTES_UPDATED",
        old_value={"notes": rule["notes"]},
        new_value={"notes": notes},
    )

    return await _get_rule_by_id_internal(rule_id, company_id, db)


# ---------------------------------------------------------------------------
# Void driver pay rule
# ---------------------------------------------------------------------------

async def void_driver_pay_rule(
    rule_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> DriverPayRuleSummary:
    """
    Void a rule that was created by mistake.

    Guards:
      - Rule must be Active or Ended (not already Voided).
      - Authority over the context tenure of the rule's start date.
      - No Locked/Archived period of the Driver, in any Branch, may start inside the rule's
        window: a rule that governed a finalized period cannot be voided. Use 'end' to close
        it going forward while preserving history.
    """
    rule = await _load_rule_row(rule_id, company_id, db)
    if rule["status"] == "Voided":
        raise HTTPException(status_code=422, detail="Rule is already Voided.")
    driver_id = int(rule["driverid"])

    await require_compensation_authority(
        company_id, user_id, driver_id, rule["effectivefrom"], db)
    await lock_driver_compensation_write(
        company_id, driver_id, None, db, monetary=True, lock_from=rule["effectivefrom"])
    rule = await _load_rule_row(rule_id, company_id, db, for_update=True)
    if rule["status"] == "Voided":
        raise HTTPException(status_code=422, detail="Rule is already Voided.")
    instant = await decision_instant(db)
    context = await require_compensation_authority(
        company_id, user_id, driver_id, rule["effectivefrom"], db, at=instant)
    await require_no_finalized_payroll(
        company_id, driver_id, rule["effectivefrom"], rule["effectiveto"], "PeriodStart", db)

    await db.execute(
        text("""
            UPDATE payroll.driverpayrules
            SET    status           = 'Voided',
                   updatedbyuserid  = :uid,
                   updatedatutc     = NOW()
            WHERE  driverpayruleid  = :rid
              AND  companyid        = :cid
        """),
        {"uid": user_id, "rid": rule_id, "cid": company_id},
    )

    await _write_pay_rule_audit(
        db,
        company_id=company_id,
        branch_id=context.branch_id,
        user_id=user_id,
        rule_id=rule_id,
        action_code="DRIVER_PAY_RULE_VOIDED",
        old_value={"status": rule["status"]},
        new_value={"status": "Voided"},
    )

    return await _get_rule_by_id_internal(rule_id, company_id, db)
