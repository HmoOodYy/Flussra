"""Branch applicability of Company PayDefinitions.

A PayDefinition is Company-owned; it is operationally applicable to a Branch
only through an effective-dated payroll.BranchPayItemConfig version keyed by
PayDefinitionID. A missing configuration means not applicable: there is no
implicit default activation and no global fallback.

Versioning is atomic per Company + Branch + PayDefinition:
- same effective date as the open version amends it in place;
- a later date closes the open version at the day before and opens a new one;
- an earlier date than a pending (future) open version replaces it in place.
"""

from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.policy import require_non_driver_subject
from app.compensation.audit import write_audit
from app.compensation.errors import compensation_error
from app.compensation.schemas import (
    BranchConfigTarget,
    BranchConfigUpdate,
    BranchConfigVersion,
    BranchPayDefinitionState,
    BulkBranchConfigBranchResult,
    BulkBranchConfigResult,
    BulkBranchConfigUpdate,
)
from app.core.service import _check_any_permission, _check_branch_access

_CONFIG_EDIT = ["payitems.edit", "setup.manage"]

_CFG_COLUMNS = """
    bpic.configid, bpic.isactive, bpic.notes, bpic.effectivefrom, bpic.effectiveto,
    bpic.createdatutc
"""

_ACTIONS = {
    "BRANCH_PAY_DEFINITION_CONFIG_CREATED": "Created",
    "BRANCH_PAY_DEFINITION_CONFIG_UPDATED": "Updated",
    "BRANCH_PAY_DEFINITION_CONFIG_VERSIONED": "Versioned",
}


def _version(row) -> BranchConfigVersion:
    return BranchConfigVersion(
        config_id=row["configid"], is_active=bool(row["isactive"]), notes=row["notes"],
        effective_from=row["effectivefrom"], effective_to=row["effectiveto"],
        created_at_utc=row["createdatutc"],
    )


async def require_definition_applicable(
    company_id: int, branch_id: int, pay_definition_id: int, on_date: date,
    db: AsyncConnection,
) -> None:
    """Require an active BranchPayItemConfig version for the Company, Branch and
    PayDefinition on ``on_date``.

    A missing version is not active, an explicitly inactive version is not active,
    and a version that starts after ``on_date`` does not apply to it. No other
    state (name, legacy PayItem, default flags) is consulted.
    """
    active = (await db.execute(
        text("""
            SELECT bpic.isactive
            FROM   payroll.branchpayitemconfig bpic
            WHERE  bpic.companyid       = :cid
              AND  bpic.branchid        = :bid
              AND  bpic.paydefinitionid = :pdid
              AND  bpic.effectivefrom  <= :on_date
              AND  (bpic.effectiveto IS NULL OR bpic.effectiveto >= :on_date)
        """),
        {"cid": company_id, "bid": branch_id, "pdid": pay_definition_id, "on_date": on_date},
    )).scalar_one_or_none()
    if not active:
        raise compensation_error(
            "PAY_DEFINITION_NOT_APPLICABLE",
            f"The PayDefinition is not active for this Branch on {on_date.isoformat()}.", 422)


async def _company_today(company_id: int, db: AsyncConnection) -> date:
    return (await db.execute(
        text("SELECT core.fn_CompanyToday(:cid)"), {"cid": company_id},
    )).scalar_one()


async def _open_period_max_end(
    company_id: int, branch_id: int, today: date, db: AsyncConnection,
) -> date | None:
    """End of the latest open period that contains today, or None."""
    return (await db.execute(
        text("""
            SELECT MAX(enddate) FROM payroll.payrollperiods
            WHERE  companyid = :cid AND branchid = :bid
              AND  status IN ('Draft', 'Open', 'InReview', 'Approved')
              AND  startdate <= :today AND enddate >= :today
        """),
        {"cid": company_id, "bid": branch_id, "today": today},
    )).scalar_one()


def resolve_effective_from(
    requested: date | None, period_max_end: date | None, today: date,
) -> date:
    """Apply open-period protection to a requested effective date."""
    if period_max_end is None:
        return requested or today
    earliest = period_max_end + timedelta(days=1)
    if requested is None:
        return earliest
    if requested <= period_max_end:
        raise compensation_error(
            "OPEN_PERIOD_BOUNDARY",
            f"A payroll period is open until {period_max_end}. Configuration changes cannot "
            f"take effect during an open period. The earliest allowed effective_from is "
            f"{earliest}.", 422)
    return requested


async def _require_branch_in_company(
    company_id: int, branch_id: int, db: AsyncConnection,
) -> None:
    found = (await db.execute(
        text("SELECT 1 FROM core.branches WHERE branchid = :bid AND companyid = :cid"),
        {"bid": branch_id, "cid": company_id},
    )).scalar_one_or_none()
    if found is None:
        raise compensation_error("BRANCH_NOT_FOUND", "Branch not found.", 404)


async def _lock_definition(
    company_id: int, pay_definition_id: int, db: AsyncConnection,
) -> dict:
    """Read the definition under a shared row lock so retirement cannot interleave."""
    row = (await db.execute(
        text("""
            SELECT paydefinitionid, definitioncode, status
            FROM   payroll.paydefinitions
            WHERE  paydefinitionid = :pid AND companyid = :cid
            FOR SHARE
        """),
        {"pid": pay_definition_id, "cid": company_id},
    )).mappings().first()
    if row is None:
        raise compensation_error("DEFINITION_NOT_FOUND", "PayDefinition not found.", 404)
    return dict(row)


async def apply_config(
    db: AsyncConnection, *, company_id: int, branch_id: int, pay_definition_id: int,
    user_id: int, is_active: bool, notes: str | None, effective_from: date,
) -> tuple[str, int]:
    """Write one branch's configuration for a PayDefinition with full versioning."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"branch-pay-definition-config:{company_id}:{branch_id}:{pay_definition_id}"},
    )
    open_row = (await db.execute(
        text(f"""
            SELECT {_CFG_COLUMNS}
            FROM   payroll.branchpayitemconfig bpic
            WHERE  bpic.paydefinitionid = :pid AND bpic.companyid = :cid
              AND  bpic.branchid = :bid AND bpic.effectiveto IS NULL
            FOR UPDATE
        """),
        {"pid": pay_definition_id, "cid": company_id, "bid": branch_id},
    )).mappings().first()

    insert_sql = text("""
        INSERT INTO payroll.branchpayitemconfig
            (companyid, branchid, paydefinitionid, isactive, effectivefrom, notes,
             createdbyuserid)
        VALUES (:cid, :bid, :pid, :active, :eff_from, :notes, :uid)
        RETURNING configid
    """)
    params = {"cid": company_id, "bid": branch_id, "pid": pay_definition_id,
              "active": is_active, "eff_from": effective_from, "notes": notes, "uid": user_id}

    old_value: dict | None = None
    try:
        if open_row is None:
            action = "BRANCH_PAY_DEFINITION_CONFIG_CREATED"
            config_id = (await db.execute(insert_sql, params)).scalar_one()
        elif open_row["effectivefrom"] == effective_from or open_row["effectivefrom"] > effective_from:
            action = "BRANCH_PAY_DEFINITION_CONFIG_UPDATED"
            config_id = open_row["configid"]
            old_value = {"is_active": bool(open_row["isactive"]), "notes": open_row["notes"],
                         "effective_from": str(open_row["effectivefrom"])}
            if effective_from < open_row["effectivefrom"]:
                # Moving the open version earlier must not leave the closed version before it
                # overlapping: re-close that version at the day before the new start.
                previous = (await db.execute(
                    text("""
                        SELECT configid, effectivefrom FROM payroll.branchpayitemconfig
                        WHERE  paydefinitionid = :pid AND companyid = :cid AND branchid = :bid
                          AND  effectiveto = :closed_on
                        FOR UPDATE
                    """),
                    {"pid": pay_definition_id, "cid": company_id, "bid": branch_id,
                     "closed_on": open_row["effectivefrom"] - timedelta(days=1)},
                )).mappings().first()
                if previous is not None:
                    if effective_from <= previous["effectivefrom"]:
                        raise compensation_error(
                            "CONFIG_VERSION_OVERLAP",
                            "The effective date overlaps an existing configuration version.", 409)
                    await db.execute(
                        text("UPDATE payroll.branchpayitemconfig SET effectiveto = :close_on "
                             "WHERE configid = :config_id"),
                        {"close_on": effective_from - timedelta(days=1),
                         "config_id": previous["configid"]})
            await db.execute(
                text("""
                    UPDATE payroll.branchpayitemconfig
                    SET    isactive = :active, notes = :notes, effectivefrom = :eff_from
                    WHERE  configid = :config_id
                """),
                {"active": is_active, "notes": notes, "eff_from": effective_from,
                 "config_id": config_id})
        else:
            action = "BRANCH_PAY_DEFINITION_CONFIG_VERSIONED"
            old_value = {"is_active": bool(open_row["isactive"]), "notes": open_row["notes"],
                         "effective_from": str(open_row["effectivefrom"]),
                         "closed_effective_to": str(effective_from - timedelta(days=1))}
            await db.execute(
                text("UPDATE payroll.branchpayitemconfig SET effectiveto = :close_on "
                     "WHERE configid = :config_id"),
                {"close_on": effective_from - timedelta(days=1),
                 "config_id": open_row["configid"]})
            config_id = (await db.execute(insert_sql, params)).scalar_one()
    except DBAPIError as exc:
        if "excl_branchpayitemconfig_definitionversionoverlap" in str(exc).lower():
            raise compensation_error(
                "CONFIG_VERSION_OVERLAP",
                "The effective date overlaps an existing configuration version.", 409) from exc
        raise

    await write_audit(
        db, company_id=company_id, branch_id=branch_id, user_id=user_id,
        action_code=action, entity_name="BranchPayItemConfig",
        entity_id=f"{branch_id}:{pay_definition_id}", old_value=old_value,
        new_value={"is_active": is_active, "notes": notes,
                   "effective_from": str(effective_from)})
    return action, int(config_id)


async def _config_pair(
    company_id: int, branch_id: int, today: date, db: AsyncConnection,
) -> tuple[dict[int, BranchConfigVersion], dict[int, BranchConfigVersion]]:
    current = (await db.execute(
        text(f"""
            SELECT DISTINCT ON (bpic.paydefinitionid) bpic.paydefinitionid, {_CFG_COLUMNS}
            FROM   payroll.branchpayitemconfig bpic
            WHERE  bpic.companyid = :cid AND bpic.branchid = :bid
              AND  bpic.effectivefrom <= :today
              AND  (bpic.effectiveto IS NULL OR bpic.effectiveto >= :today)
            ORDER  BY bpic.paydefinitionid, bpic.effectivefrom DESC
        """),
        {"cid": company_id, "bid": branch_id, "today": today},
    )).mappings().all()
    pending = (await db.execute(
        text(f"""
            SELECT bpic.paydefinitionid, {_CFG_COLUMNS}
            FROM   payroll.branchpayitemconfig bpic
            WHERE  bpic.companyid = :cid AND bpic.branchid = :bid
              AND  bpic.effectiveto IS NULL AND bpic.effectivefrom > :today
        """),
        {"cid": company_id, "bid": branch_id, "today": today},
    )).mappings().all()
    return ({r["paydefinitionid"]: _version(r) for r in current},
            {r["paydefinitionid"]: _version(r) for r in pending})


def _state(row, current, pending, *, has_open_periods: bool = False) -> BranchPayDefinitionState:
    return BranchPayDefinitionState(
        pay_definition_id=row["paydefinitionid"],
        definition_code=row["definitioncode"],
        definition_name=row["definitionname"],
        input_type=row["inputtype"],
        unit=row["unit"],
        calculation_method=row["calculationmethod"],
        definition_status=row["status"],
        rate_definition_id=row["ratedefinitionid"],
        is_configured=current is not None,
        is_active=bool(current.is_active) if current is not None else False,
        notes=current.notes if current is not None else None,
        current_config=current,
        pending_config=pending,
        has_open_periods=has_open_periods,
    )


_DEFINITION_COLUMNS = """
    pd.paydefinitionid, pd.definitioncode, pd.definitionname, pd.inputtype, pd.unit,
    pd.calculationmethod, pd.status, rd.ratedefinitionid
"""


async def list_branch_definitions(
    company_id: int, user_id: int, branch_id: int, db: AsyncConnection,
) -> list[BranchPayDefinitionState]:
    await require_non_driver_subject(company_id, user_id, db)
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise compensation_error(
            "BRANCH_ACCESS_DENIED", "You do not have access to this branch.", 403)
    await _require_branch_in_company(company_id, branch_id, db)

    today = await _company_today(company_id, db)
    rows = (await db.execute(
        text(f"""
            SELECT {_DEFINITION_COLUMNS}
            FROM   payroll.paydefinitions pd
            LEFT JOIN payroll.ratedefinitions rd ON rd.paydefinitionid = pd.paydefinitionid
            WHERE  pd.companyid = :cid AND pd.status <> 'Retired'
            ORDER  BY lower(pd.definitionname), pd.definitioncode, pd.paydefinitionid
        """),
        {"cid": company_id},
    )).mappings().all()
    current, pending = await _config_pair(company_id, branch_id, today, db)
    return [_state(r, current.get(r["paydefinitionid"]), pending.get(r["paydefinitionid"]))
            for r in rows]


async def _state_for(
    company_id: int, branch_id: int, pay_definition_id: int, today: date,
    db: AsyncConnection, *, has_open_periods: bool = False,
) -> BranchPayDefinitionState:
    row = (await db.execute(
        text(f"""
            SELECT {_DEFINITION_COLUMNS}
            FROM   payroll.paydefinitions pd
            LEFT JOIN payroll.ratedefinitions rd ON rd.paydefinitionid = pd.paydefinitionid
            WHERE  pd.paydefinitionid = :pid AND pd.companyid = :cid
        """),
        {"pid": pay_definition_id, "cid": company_id},
    )).mappings().first()
    current, pending = await _config_pair(company_id, branch_id, today, db)
    return _state(row, current.get(pay_definition_id), pending.get(pay_definition_id),
                  has_open_periods=has_open_periods)


def _require_activatable(definition: dict, is_active: bool) -> None:
    if is_active and definition["status"] != "Active":
        raise compensation_error(
            "PAY_DEFINITION_NOT_ACTIVE",
            "A retired PayDefinition cannot be activated for a Branch.", 422)


async def update_branch_config(
    company_id: int, user_id: int, branch_id: int, pay_definition_id: int,
    data: BranchConfigUpdate, db: AsyncConnection,
) -> BranchPayDefinitionState:
    await _check_any_permission(company_id, user_id, branch_id, _CONFIG_EDIT, db)
    await _require_branch_in_company(company_id, branch_id, db)
    definition = await _lock_definition(company_id, pay_definition_id, db)
    _require_activatable(definition, data.is_active)

    today = await _company_today(company_id, db)
    period_end = await _open_period_max_end(company_id, branch_id, today, db)
    effective_from = resolve_effective_from(data.effective_from, period_end, today)
    await apply_config(
        db, company_id=company_id, branch_id=branch_id, pay_definition_id=pay_definition_id,
        user_id=user_id, is_active=data.is_active, notes=data.notes,
        effective_from=effective_from)
    return await _state_for(
        company_id, branch_id, pay_definition_id, today, db,
        has_open_periods=period_end is not None)


async def bulk_update_branch_config(
    company_id: int, user_id: int, pay_definition_id: int, data: BulkBranchConfigUpdate,
    db: AsyncConnection,
) -> BulkBranchConfigResult:
    await _check_any_permission(company_id, user_id, None, _CONFIG_EDIT, db)
    definition = await _lock_definition(company_id, pay_definition_id, db)
    _require_activatable(definition, data.is_active)

    if data.target == BranchConfigTarget.AllBranches:
        rows = (await db.execute(
            text("SELECT branchid, branchname FROM core.branches "
                 "WHERE companyid = :cid AND status = 'Active' ORDER BY branchid"),
            {"cid": company_id},
        )).mappings().all()
        if not rows:
            raise compensation_error(
                "NO_ACTIVE_BRANCHES", "No active branches found for this company.", 422)
    else:
        rows = (await db.execute(
            text("SELECT branchid, branchname FROM core.branches "
                 "WHERE companyid = :cid AND branchid = ANY(:bids) ORDER BY branchid"),
            {"cid": company_id, "bids": data.branch_ids},
        )).mappings().all()
        missing = sorted(set(data.branch_ids or []) - {r["branchid"] for r in rows})
        if missing:
            raise compensation_error(
                "BRANCH_NOT_FOUND", f"Branch ID(s) not found in this company: {missing}", 422)

    today = await _company_today(company_id, db)
    validated: list[tuple[dict, date]] = []
    errors: list[dict] = []
    for row in rows:
        period_end = await _open_period_max_end(company_id, row["branchid"], today, db)
        try:
            validated.append((dict(row), resolve_effective_from(
                data.effective_from, period_end, today)))
        except HTTPException as exc:
            errors.append({"branch_id": row["branchid"], "branch_name": row["branchname"],
                           "error": exc.detail["message"]})
    if errors:
        raise HTTPException(status_code=422, detail={
            "code": "BULK_VALIDATION_FAILED",
            "message": "Validation failed for one or more target branches. "
                       "No changes were applied.",
            "branch_errors": errors})

    results: list[BulkBranchConfigBranchResult] = []
    for row, effective_from in validated:
        action, config_id = await apply_config(
            db, company_id=company_id, branch_id=row["branchid"],
            pay_definition_id=pay_definition_id, user_id=user_id,
            is_active=data.is_active, notes=data.notes, effective_from=effective_from)
        results.append(BulkBranchConfigBranchResult(
            branch_id=row["branchid"], branch_name=row["branchname"],
            status=_ACTIONS[action], config_id=config_id, effective_from=effective_from))
    await write_audit(
        db, company_id=company_id, branch_id=None, user_id=user_id,
        action_code="BRANCH_PAY_DEFINITION_BULK_CONFIG", entity_name="BranchPayItemConfig",
        entity_id=f"bulk:{pay_definition_id}",
        new_value={"is_active": data.is_active, "notes": data.notes,
                   "target": str(data.target), "branch_count": len(results)})
    return BulkBranchConfigResult(
        pay_definition_id=pay_definition_id, target=data.target,
        requested_branch_count=len(rows), updated_branch_count=len(results),
        results=results)


async def config_history(
    company_id: int, user_id: int, branch_id: int, pay_definition_id: int, db: AsyncConnection,
) -> list[BranchConfigVersion]:
    await require_non_driver_subject(company_id, user_id, db)
    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise compensation_error(
            "BRANCH_ACCESS_DENIED", "You do not have access to this branch.", 403)
    await _require_branch_in_company(company_id, branch_id, db)
    exists = (await db.execute(
        text("SELECT 1 FROM payroll.paydefinitions WHERE paydefinitionid = :pid AND companyid = :cid"),
        {"pid": pay_definition_id, "cid": company_id},
    )).scalar_one_or_none()
    if exists is None:
        raise compensation_error("DEFINITION_NOT_FOUND", "PayDefinition not found.", 404)
    rows = (await db.execute(
        text(f"""
            SELECT {_CFG_COLUMNS}
            FROM   payroll.branchpayitemconfig bpic
            WHERE  bpic.paydefinitionid = :pid AND bpic.companyid = :cid AND bpic.branchid = :bid
            ORDER  BY bpic.effectivefrom DESC, bpic.configid DESC
        """),
        {"pid": pay_definition_id, "cid": company_id, "bid": branch_id},
    )).mappings().all()
    return [_version(r) for r in rows]
