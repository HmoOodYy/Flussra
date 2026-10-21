"""Branch usage limits of Company Statuses.

The four optional hard limits of a Status (uses per period, per driver, across drivers and per
day) are a Branch x Status fact: the catalog is shared by every Branch, while how often a
Branch lets a Status be used is the Branch's own. A Status without a row has every limit
disabled; a row always has at least one limit enabled, so disabling the last one removes it.
Enforcement is in the Day Grid, from the canonical day-entry selections of the period.
"""
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.branch_access import check_branch_access
from app.settings import audit, security
from app.settings.schemas import StatusKeyBranchLimits, StatusKeyBranchLimitsUpdate

_LIMIT_PAIRS = (
    ("limit_uses_per_period", "limitusesperperiod"),
    ("limit_uses_per_driver", "limitusesperdriver"),
    ("limit_uses_across_drivers", "limitusesacrossdrivers"),
    ("limit_uses_per_day", "limitusesperday"),
)


def _coded(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


async def _require_branch(company_id: int, branch_id: int, db: AsyncConnection) -> None:
    found = (await db.execute(
        text("SELECT 1 FROM core.branches WHERE branchid = :bid AND companyid = :cid"),
        {"bid": branch_id, "cid": company_id},
    )).first()
    if found is None:
        raise _coded(404, "BRANCH_NOT_FOUND", f"Branch {branch_id} not found.")


def _limits_from_row(row) -> StatusKeyBranchLimits:
    values: dict = {
        "branch_id": row["branchid"],
        "status_key_id": row["statuskeyid"],
        "key_name": row["keyname"],
    }
    for field, column in _LIMIT_PAIRS:
        values[f"{field}_enabled"] = bool(row[f"{column}enabled"] or False)
        values[field] = row[column]
    return StatusKeyBranchLimits(**values)


_LIMITS_SELECT = """
    SELECT CAST(:bid AS integer) AS branchid, k.statuskeyid, k.keyname,
           l.limitusesperperiodenabled, l.limitusesperperiod,
           l.limitusesperdriverenabled, l.limitusesperdriver,
           l.limitusesacrossdriversenabled, l.limitusesacrossdrivers,
           l.limitusesperdayenabled, l.limitusesperday
    FROM   payroll.payrollstatuskeys k
    LEFT JOIN payroll.payrollstatuskeybranchlimits l
           ON l.statuskeyid = k.statuskeyid AND l.branchid = :bid AND l.companyid = k.companyid
"""


async def list_branch_limits(
    branch_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> list[StatusKeyBranchLimits]:
    """One entry per active Status of the Company with this Branch's limits (all disabled
    when the Branch has none). Readable by any user with access to the Branch."""
    can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise _coded(403, "BRANCH_ACCESS_DENIED", "You do not have access to this branch.")
    await _require_branch(company_id, branch_id, db)
    rows = (await db.execute(
        text(f"""
            {_LIMITS_SELECT}
            WHERE  k.companyid = :cid AND k.isactive
            ORDER  BY k.displayorder, k.normalizedkeyname, k.statuskeyid
        """),
        {"bid": branch_id, "cid": company_id},
    )).mappings().all()
    return [_limits_from_row(r) for r in rows]


async def put_branch_limits(
    branch_id: int,
    key_id: int,
    company_id: int,
    user_id: int,
    data: StatusKeyBranchLimitsUpdate,
    db: AsyncConnection,
) -> StatusKeyBranchLimits:
    """Replace the limits of one Status in one Branch. Requires AllCompanyBranches scope.
    Every limit disabled removes the row."""
    await security.ensure_company_admin(company_id, user_id, db)
    await _require_branch(company_id, branch_id, db)
    key = (await db.execute(
        text("SELECT statuskeyid FROM payroll.payrollstatuskeys "
             "WHERE statuskeyid = :kid AND companyid = :cid FOR SHARE"),
        {"kid": key_id, "cid": company_id},
    )).first()
    if key is None:
        raise _coded(404, "STATUS_KEY_NOT_FOUND", f"Status key {key_id} not found.")

    params: dict = {"bid": branch_id, "kid": key_id, "cid": company_id, "uid": user_id}
    for field, _ in _LIMIT_PAIRS:
        enabled = bool(getattr(data, f"{field}_enabled"))
        value = getattr(data, field)
        if enabled and value is None:
            raise _coded(422, "STATUS_LIMIT_VALUE_REQUIRED",
                         f"{field} must be a positive integer when enabled.")
        params[f"{field}_enabled"] = enabled
        params[field] = value if enabled else None

    old = (await db.execute(
        text("""
            SELECT limitusesperperiodenabled, limitusesperperiod,
                   limitusesperdriverenabled, limitusesperdriver,
                   limitusesacrossdriversenabled, limitusesacrossdrivers,
                   limitusesperdayenabled, limitusesperday
            FROM   payroll.payrollstatuskeybranchlimits
            WHERE  branchid = :bid AND statuskeyid = :kid
        """),
        {"bid": branch_id, "kid": key_id},
    )).mappings().first()

    if not any(params[f"{field}_enabled"] for field, _ in _LIMIT_PAIRS):
        await db.execute(
            text("DELETE FROM payroll.payrollstatuskeybranchlimits "
                 "WHERE branchid = :bid AND statuskeyid = :kid"),
            {"bid": branch_id, "kid": key_id},
        )
    else:
        await db.execute(
            text("""
                INSERT INTO payroll.payrollstatuskeybranchlimits
                    (branchid, statuskeyid, companyid,
                     limitusesperperiodenabled, limitusesperperiod,
                     limitusesperdriverenabled, limitusesperdriver,
                     limitusesacrossdriversenabled, limitusesacrossdrivers,
                     limitusesperdayenabled, limitusesperday,
                     createdbyuserid, updatedbyuserid, updatedatutc)
                VALUES (:bid, :kid, :cid,
                        :limit_uses_per_period_enabled, :limit_uses_per_period,
                        :limit_uses_per_driver_enabled, :limit_uses_per_driver,
                        :limit_uses_across_drivers_enabled, :limit_uses_across_drivers,
                        :limit_uses_per_day_enabled, :limit_uses_per_day,
                        :uid, :uid, NOW())
                ON CONFLICT (branchid, statuskeyid) DO UPDATE SET
                    limitusesperperiodenabled = EXCLUDED.limitusesperperiodenabled,
                    limitusesperperiod = EXCLUDED.limitusesperperiod,
                    limitusesperdriverenabled = EXCLUDED.limitusesperdriverenabled,
                    limitusesperdriver = EXCLUDED.limitusesperdriver,
                    limitusesacrossdriversenabled = EXCLUDED.limitusesacrossdriversenabled,
                    limitusesacrossdrivers = EXCLUDED.limitusesacrossdrivers,
                    limitusesperdayenabled = EXCLUDED.limitusesperdayenabled,
                    limitusesperday = EXCLUDED.limitusesperday,
                    updatedbyuserid = EXCLUDED.updatedbyuserid,
                    updatedatutc = NOW()
            """),
            params,
        )

    await audit.write_settings_audit(
        db, company_id=company_id, branch_id=branch_id, user_id=user_id,
        action_code="STATUS_KEY_LIMITS_UPDATED", entity_name="PayrollStatusKeyBranchLimits",
        entity_id=f"{branch_id}:{key_id}",
        old_value=None if old is None else {k: v for k, v in dict(old).items()},
        new_value={k: params[k] for k in params
                   if k.startswith("limit_uses_")},
    )
    row = (await db.execute(
        text(f"{_LIMITS_SELECT} WHERE k.statuskeyid = :kid AND k.companyid = :cid"),
        {"bid": branch_id, "kid": key_id, "cid": company_id},
    )).mappings().one()
    return _limits_from_row(row)
