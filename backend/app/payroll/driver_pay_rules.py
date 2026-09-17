"""
Driver Pay Rules (Minimum / Maximum Pay) — M15.

Extracted from app.payroll.service (Stage B4-2B) as a dependency-closed
domain module — no behavior change, pure relocation. Depends only on the
neutral app.payroll.guards leaf (Stage B4-2A) for shared access checks, not
on app.payroll.service.
"""
import json
from datetime import date
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import _check_any_permission
from app.payroll.guards import (
    _check_own_driver_only,
    _check_not_in_finalized_period,
    _check_driver_read_access,
)
from app.payroll.schemas import DriverPayRuleCreate, DriverPayRuleSummary


# ===========================================================================
# M15 — Driver Pay Rules (Minimum / Maximum Pay)
# ===========================================================================

_RULE_AUDIT_REASONS: dict[str, str] = {
    "DRIVER_PAY_RULE_CREATED":       "Driver pay rule created",
    "DRIVER_PAY_RULE_ENDED":         "Driver pay rule ended",
    "DRIVER_PAY_RULE_NOTES_UPDATED": "Driver pay rule notes updated",
    "DRIVER_PAY_RULE_VOIDED":        "Driver pay rule voided",
}


async def _write_pay_rule_audit(
    db: AsyncConnection,
    *,
    company_id: int,
    branch_id: int,
    user_id: int,
    rule_id: int,
    action_code: str,
    old_value: dict | None = None,
    new_value: dict | None = None,
) -> None:
    """
    Insert one audit.AuditLog row for a DriverPayRule event.

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
        r.branchid,
        r.driverid,
        r.ruletype,
        r.amount,
        r.effectivefrom,
        r.effectiveto,
        r.status,
        r.createdbyuserid,
        r.createdatutc,
        r.updatedbyuserid,
        r.updatedatutc,
        r.notes
    FROM payroll.driverpayrules r
"""


def _rule_row_to_summary(r: Any) -> DriverPayRuleSummary:
    return DriverPayRuleSummary(
        driver_pay_rule_id=r["driverpayruleid"],
        company_id=r["companyid"],
        branch_id=r["branchid"],
        driver_id=r["driverid"],
        rule_type=r["ruletype"],
        amount=r["amount"],
        effective_from=r["effectivefrom"],
        effective_to=r["effectiveto"],
        status=r["status"],
        created_by_user_id=r["createdbyuserid"],
        created_at_utc=r["createdatutc"],
        updated_by_user_id=r["updatedbyuserid"],
        updated_at_utc=r["updatedatutc"],
        notes=r["notes"],
    )


async def _get_rule_by_id_internal(
    rule_id: int,
    company_id: int,
    db: AsyncConnection,
) -> DriverPayRuleSummary:
    """Internal fetch — no branch-access check. Used by other service functions."""
    result = await db.execute(
        text(f"{_RULE_SELECT} WHERE r.driverpayruleid = :rid AND r.companyid = :cid"),
        {"rid": rule_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Driver pay rule not found.")
    return _rule_row_to_summary(row)


async def _count_finalized_periods_in_rule_range(
    driver_id: int,
    company_id: int,
    effective_from: date,
    effective_to: "date | None",
    db: AsyncConnection,
) -> int:
    """
    Count Locked or Archived periods for this driver whose start_date falls
    within the rule's effective range [effective_from, effective_to].

    Both Locked and Archived are treated as finalized: Archived periods have
    already been finalized and locked; their history must be preserved.

    Used by both void (block if > 0) and end (derive minimum closure date).
    A period 'relied on' the rule if it is Locked/Archived AND has at least one
    PayrollFinalLine for this driver (proof that finalization ran).
    """
    result = await db.execute(
        text("""
            SELECT COUNT(*) AS cnt
            FROM   payroll.payrollperiods pp
            WHERE  pp.companyid  = :company_id
              AND  pp.status     IN ('Locked', 'Archived')
              AND  pp.startdate >= :eff_from
              AND  (CAST(:eff_to AS date) IS NULL OR pp.startdate <= CAST(:eff_to AS date))
              AND  EXISTS (
                  SELECT 1
                  FROM   payroll.payrollfinallines fl
                  WHERE  fl.payrollperiodid = pp.payrollperiodid
                    AND  fl.driverid        = :driver_id
              )
        """),
        {
            "company_id": company_id,
            "driver_id":  driver_id,
            "eff_from":   effective_from,
            "eff_to":     effective_to,
        },
    )
    return int(result.scalar_one())


async def _latest_finalized_period_start_in_range(
    driver_id: int,
    company_id: int,
    effective_from: date,
    effective_to: "date | None",
    db: AsyncConnection,
) -> "date | None":
    """
    Return the latest period.start_date among Locked or Archived periods for
    this driver whose start_date falls within the rule's effective range.
    Returns None if no such periods exist.

    Archived is treated as finalized for the same reasons as Locked.
    """
    result = await db.execute(
        text("""
            SELECT MAX(pp.startdate) AS latest
            FROM   payroll.payrollperiods pp
            WHERE  pp.companyid  = :company_id
              AND  pp.status     IN ('Locked', 'Archived')
              AND  pp.startdate >= :eff_from
              AND  (CAST(:eff_to AS date) IS NULL OR pp.startdate <= CAST(:eff_to AS date))
              AND  EXISTS (
                  SELECT 1
                  FROM   payroll.payrollfinallines fl
                  WHERE  fl.payrollperiodid = pp.payrollperiodid
                    AND  fl.driverid        = :driver_id
              )
        """),
        {
            "company_id": company_id,
            "driver_id":  driver_id,
            "eff_from":   effective_from,
            "eff_to":     effective_to,
        },
    )
    row = result.first()
    return row[0] if row and row[0] is not None else None


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
    Create a new Active DriverPayRule.

    Guards:
      - Driver must exist in this company; user must have branch access.
      - Permission: setup.manage
      - Overlap: enforced by DB EXCLUDE constraint (also checked at service level).
      - If both MinimumPay and MaximumPay rules exist for the same period range,
        min <= max is checked at finalization time, not here.
    """
    drv_result = await db.execute(
        text("SELECT driverid, branchid FROM core.drivers WHERE driverid = :did AND companyid = :cid"),
        {"did": data.driver_id, "cid": company_id},
    )
    drv_row = drv_result.mappings().first()
    if drv_row is None:
        raise HTTPException(status_code=404, detail="Driver not found in this company.")

    driver_branch_id: int = drv_row["branchid"]
    await _check_any_permission(
        company_id, user_id, driver_branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )
    await _check_own_driver_only(company_id, user_id, data.driver_id, db)

    # Service-level overlap check (belt-and-suspenders on top of DB EXCLUDE constraint).
    # Check for any Active or Ended rule for same driver+type whose date range overlaps.
    overlap_result = await db.execute(
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
        {
            "cid":     company_id,
            "did":     data.driver_id,
            "rtype":   data.rule_type,
            "eff_from": data.effective_from,
            "eff_to":   data.effective_to,
        },
    )
    if overlap_result.first() is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"A {data.rule_type} rule already exists for this driver that overlaps "
                f"the requested date range [{data.effective_from}, {data.effective_to or 'open'}]. "
                "End the existing rule first, or choose a non-overlapping date range."
            ),
        )

    # Finalized-period guard: a pay rule must not be created with effective_from
    # inside a Locked or Archived payroll period (same as rates backdating guard).
    await _check_not_in_finalized_period(
        company_id, driver_branch_id, data.effective_from, db, label="pay rule"
    )

    insert_result = await db.execute(
        text("""
            INSERT INTO payroll.driverpayrules
                (companyid, branchid, driverid, ruletype, amount,
                 effectivefrom, effectiveto, status, createdbyuserid, notes)
            VALUES
                (:cid, :bid, :did, :rtype, :amount,
                 :eff_from, :eff_to, 'Active', :creator, :notes)
            RETURNING driverpayruleid
        """),
        {
            "cid":      company_id,
            "bid":      driver_branch_id,
            "did":      data.driver_id,
            "rtype":    data.rule_type,
            "amount":   data.amount,
            "eff_from": data.effective_from,
            "eff_to":   data.effective_to,
            "creator":  user_id,
            "notes":    data.notes,
        },
    )
    rule_id: int = insert_result.scalar_one()

    await _write_pay_rule_audit(
        db,
        company_id=company_id,
        branch_id=driver_branch_id,
        user_id=user_id,
        rule_id=rule_id,
        action_code="DRIVER_PAY_RULE_CREATED",
        new_value={
            "driver_id":      data.driver_id,
            "rule_type":      data.rule_type,
            "amount":         str(data.amount),
            "effective_from": str(data.effective_from),
            "effective_to":   str(data.effective_to) if data.effective_to else None,
            "status":         "Active",
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
    """Return all pay rules for a driver (branch-access + ODA checked)."""
    await _check_driver_read_access(driver_id, company_id, user_id, db)

    conditions = ["r.companyid = :cid", "r.driverid = :did"]
    params: dict[str, Any] = {"cid": company_id, "did": driver_id}

    if rule_type is not None:
        conditions.append("r.ruletype = :rtype")
        params["rtype"] = rule_type
    if rule_status is not None:
        conditions.append("r.status = :rstatus")
        params["rstatus"] = rule_status

    where = " AND ".join(conditions)
    result = await db.execute(
        text(f"{_RULE_SELECT} WHERE {where} ORDER BY r.ruletype, r.effectivefrom DESC"),
        params,
    )
    return [_rule_row_to_summary(r) for r in result.mappings().all()]


async def get_driver_pay_rule_by_id(
    rule_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> DriverPayRuleSummary:
    """Return a single pay rule (branch-access + ODA checked)."""
    rule = await _get_rule_by_id_internal(rule_id, company_id, db)
    await _check_any_permission(
        company_id, user_id, rule.branch_id,
        ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
    )
    await _check_own_driver_only(company_id, user_id, rule.driver_id, db)
    return rule


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
      - effective_to must not precede the latest finalized period's start_date
        that falls within the rule's current effective range (that would make
        historical final lines inconsistent).
    """
    rule = await get_driver_pay_rule_by_id(rule_id, company_id, user_id, db)

    if rule.status != "Active":
        raise HTTPException(
            status_code=422,
            detail=f"Only Active rules can be ended (current status: '{rule.status}').",
        )

    await _check_any_permission(
        company_id, user_id, rule.branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    if effective_to < rule.effective_from:
        raise HTTPException(
            status_code=422,
            detail=f"effective_to ({effective_to}) cannot be before effective_from ({rule.effective_from}).",
        )

    # Guard: cannot close the rule before the latest finalized period that relied on it.
    latest = await _latest_finalized_period_start_in_range(
        rule.driver_id, company_id, rule.effective_from, rule.effective_to, db
    )
    if latest is not None and effective_to < latest:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot end this rule on {effective_to}: a finalized period with "
                f"start date {latest} already relied on it. "
                f"The closure date must be on or after {latest}."
            ),
        )

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
        branch_id=rule.branch_id,
        user_id=user_id,
        rule_id=rule_id,
        action_code="DRIVER_PAY_RULE_ENDED",
        old_value={"status": "Active", "effective_to": str(rule.effective_to) if rule.effective_to else None},
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
    Update notes on a pay rule. Notes-only — amount/dates cannot be changed in-place.
    Rule must not be Voided.
    """
    rule = await get_driver_pay_rule_by_id(rule_id, company_id, user_id, db)

    if rule.status == "Voided":
        raise HTTPException(status_code=422, detail="Cannot update a Voided rule.")

    await _check_any_permission(
        company_id, user_id, rule.branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

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
        branch_id=rule.branch_id,
        user_id=user_id,
        rule_id=rule_id,
        action_code="DRIVER_PAY_RULE_NOTES_UPDATED",
        old_value={"notes": rule.notes},
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
      - No finalized period must have been governed by this rule.
        (Any Locked period for this driver whose start_date falls within the
        rule's effective range counts as 'governed'.)
    """
    rule = await get_driver_pay_rule_by_id(rule_id, company_id, user_id, db)

    if rule.status == "Voided":
        raise HTTPException(status_code=422, detail="Rule is already Voided.")

    await _check_any_permission(
        company_id, user_id, rule.branch_id,
        ["payrates.edit", "settings.manage", "setup.manage"], db,
    )

    governed = await _count_finalized_periods_in_rule_range(
        rule.driver_id, company_id, rule.effective_from, rule.effective_to, db
    )
    if governed > 0:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot void this rule: {governed} finalized payroll period(s) were governed by it. "
                "Voiding is only allowed when no finalized period has relied on the rule. "
                "Use 'end' to close the rule going forward while preserving history."
            ),
        )

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
        branch_id=rule.branch_id,
        user_id=user_id,
        rule_id=rule_id,
        action_code="DRIVER_PAY_RULE_VOIDED",
        old_value={"status": rule.status},
        new_value={"status": "Voided"},
    )

    return await _get_rule_by_id_internal(rule_id, company_id, db)
