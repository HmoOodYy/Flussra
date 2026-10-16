"""Payroll StatusKey lifecycle and validation operations."""
import random as _random
import string as _string

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.branch_access import check_branch_access
from app.settings import audit, security, status_rate_columns
from app.settings.schemas import (
    StatusKey,
    StatusKeyCreate,
    StatusKeyUpdate,
)

_SK_CODE_CHARS = _string.ascii_uppercase + _string.digits


def _generate_status_key_code() -> str:
    """Generate a random SK_XXXXXXXX status key code (8 uppercase alphanumerics)."""
    suffix = "".join(_random.choices(_SK_CODE_CHARS, k=8))
    return f"SK_{suffix}"


def _normalize_status_code(code: str) -> str:
    """Uppercase and strip — used for the NormalizedStatusCode uniqueness check."""
    return code.strip().upper()


def _row_to_status_key(row) -> StatusKey:
    return StatusKey(
        status_key_id=row["statuskeyid"],
        company_id=row["companyid"],
        branch_id=row["branchid"],
        key_name=row["keyname"],
        status_code=row["statuscode"],
        normalized_status_code=row["normalizedstatuscode"],
        hours_value=row["hoursvalue"],
        is_off_reason=bool(row["isoffreason"]),
        deducts_from_yearly_allowance=bool(row["deductsfromyearlyallowance"]),
        allowance_category=row.get("allowancecategory"),
        is_active=bool(row["isactive"]),
        display_order=int(row.get("displayorder") or 0),
        status_rate_column_id=row.get("statusratecolumnid"),
        # Usage limits
        limit_uses_per_period_enabled=bool(row.get("limitusesperperiodenabled") or False),
        limit_uses_per_period=row.get("limitusesperperiod"),
        limit_uses_per_driver_enabled=bool(row.get("limitusesperdriverenabled") or False),
        limit_uses_per_driver=row.get("limitusesperdriver"),
        limit_uses_across_drivers_enabled=bool(row.get("limitusesacrossdriversenabled") or False),
        limit_uses_across_drivers=row.get("limitusesacrossdrivers"),
        limit_uses_per_day_enabled=bool(row.get("limitusesperdayenabled") or False),
        limit_uses_per_day=row.get("limitusesperday"),
        created_at_utc=row["createdatutc"],
        updated_at_utc=row.get("updatedatutc"),
    )


_STATUS_KEY_COLS = """
    statuskeyid, companyid, branchid,
    keyname, statuscode, normalizedstatuscode,
    hoursvalue, isoffreason, deductsfromyearlyallowance,
    allowancecategory, isactive, displayorder,
    limitusesperperiodenabled, limitusesperperiod,
    limitusesperdriverenabled, limitusesperdriver,
    limitusesacrossdriversenabled, limitusesacrossdrivers,
    limitusesperdayenabled, limitusesperday,
    statusratecolumnid,
    createdatutc, updatedatutc
"""


def _validate_deduction_rules(
    deducts: bool,
    is_off_reason: bool,
    allowance_category: str | None,
) -> None:
    """Raise 422 if the deduction-related fields are inconsistent."""
    if deducts:
        raise HTTPException(
            status_code=422,
            detail="Yearly allowance tracking is not available yet.",
        )


async def _check_status_key_code_unique(
    branch_id: int,
    company_id: int,
    normalized_code: str,
    db: AsyncConnection,
    *,
    exclude_key_id: int | None = None,
) -> None:
    """Raise 422 if an active status key with the same normalized code exists."""
    params: dict = {
        "bid":  branch_id,
        "cid":  company_id,
        "code": normalized_code,
    }
    extra = ""
    if exclude_key_id is not None:
        extra = " AND statuskeyid <> :kid"
        params["kid"] = exclude_key_id

    result = await db.execute(
        text(f"""
            SELECT statuskeyid
            FROM   payroll.payrollstatuskeys
            WHERE  branchid             = :bid
              AND  companyid            = :cid
              AND  normalizedstatuscode = :code
              AND  isactive             = TRUE
              {extra}
        """),
        params,
    )
    if result.first() is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"An active status key with code '{normalized_code}' "
                "already exists for this branch."
            ),
        )


async def get_status_keys(
    branch_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    include_inactive: bool = False,
) -> list[StatusKey]:
    """
    List status keys for a branch, ordered by display_order then code.
    By default returns only active keys; pass include_inactive=True for all.
    Any authenticated user with branch access can read this.
    """
    can_see_all, branch_ids = await check_branch_access(company_id, user_id, db)
    if not can_see_all and branch_id not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this branch.",
        )

    filters = ["branchid = :bid", "companyid = :cid"]
    params: dict = {"bid": branch_id, "cid": company_id}
    if not include_inactive:
        filters.append("isactive = TRUE")

    where = " AND ".join(filters)
    result = await db.execute(
        text(f"""
            SELECT {_STATUS_KEY_COLS}
            FROM   payroll.payrollstatuskeys
            WHERE  {where}
            ORDER  BY keyname ASC
        """),
        params,
    )
    return [_row_to_status_key(r) for r in result.mappings().all()]


async def create_status_key(
    branch_id: int,
    company_id: int,
    user_id: int,
    data: StatusKeyCreate,
    db: AsyncConnection,
) -> StatusKey:
    """
    Create a new payroll status key for a branch.

    The status_code is generated server-side (SK_XXXXXXXX); the user provides
    key_name (the human-readable label).  Up to 5 retries on code collision.
    Requires AllCompanyBranches scope.
    """
    await security.ensure_company_admin(company_id, user_id, db)

    # Verify the branch belongs to this company.
    branch_check = await db.execute(
        text("SELECT branchid FROM core.branches WHERE branchid=:bid AND companyid=:cid"),
        {"bid": branch_id, "cid": company_id},
    )
    if branch_check.first() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Branch {branch_id} not found.",
        )

    _validate_deduction_rules(
        data.deducts_from_yearly_allowance,
        data.is_off_reason,
        data.allowance_category,
    )

    # Validate StatusRateColumnID if provided
    if data.status_rate_column_id is not None:
        await status_rate_columns.validate_status_rate_column_for_branch(
            data.status_rate_column_id, branch_id, company_id, db,
        )
        if data.hours_value <= 0:
            raise HTTPException(
                status_code=422,
                detail="hours_value must be greater than 0 when status_rate_column_id is set.",
            )

    # Validate usage limits
    if data.limit_uses_per_period_enabled and not (data.limit_uses_per_period and data.limit_uses_per_period > 0):
        raise HTTPException(status_code=422, detail="limit_uses_per_period must be a positive integer when enabled.")
    if data.limit_uses_per_driver_enabled and not (data.limit_uses_per_driver and data.limit_uses_per_driver > 0):
        raise HTTPException(status_code=422, detail="limit_uses_per_driver must be a positive integer when enabled.")
    if data.limit_uses_across_drivers_enabled and not (data.limit_uses_across_drivers and data.limit_uses_across_drivers > 0):
        raise HTTPException(status_code=422, detail="limit_uses_across_drivers must be a positive integer when enabled.")
    if data.limit_uses_per_day_enabled and not (data.limit_uses_per_day and data.limit_uses_per_day > 0):
        raise HTTPException(status_code=422, detail="limit_uses_per_day must be a positive integer when enabled.")

    # Generate a unique SK_ code with up to 5 retries on collision.
    new_id: int | None = None
    generated_code: str = ""
    for _ in range(5):
        generated_code = _generate_status_key_code()
        normalized = _normalize_status_code(generated_code)
        try:
            ins = await db.execute(
                text("""
                    INSERT INTO payroll.payrollstatuskeys (
                        companyid, branchid,
                        keyname, statuscode, normalizedstatuscode,
                        hoursvalue, isoffreason, deductsfromyearlyallowance,
                        allowancecategory, isactive, displayorder,
                        limitusesperperiodenabled, limitusesperperiod,
                        limitusesperdriverenabled, limitusesperdriver,
                        limitusesacrossdriversenabled, limitusesacrossdrivers,
                        limitusesperdayenabled, limitusesperday,
                        statusratecolumnid,
                        createdbyuserid
                    ) VALUES (
                        :cid, :bid,
                        :kname, :code, :ncode,
                        :hours, :off, :deducts,
                        :cat, :active, 0,
                        :lpp_en, :lpp,
                        :lpd_en, :lpd,
                        :lad_en, :lad,
                        :lpday_en, :lpday,
                        :src_col_id,
                        :uid
                    )
                    RETURNING statuskeyid
                """),
                {
                    "cid":       company_id,
                    "bid":       branch_id,
                    "kname":     data.key_name.strip(),
                    "code":      generated_code,
                    "ncode":     normalized,
                    "hours":     data.hours_value,
                    "off":       data.is_off_reason,
                    "deducts":   data.deducts_from_yearly_allowance,
                    "cat":       data.allowance_category,
                    "active":    data.is_active,
                    "lpp_en":    data.limit_uses_per_period_enabled,
                    "lpp":       data.limit_uses_per_period,
                    "lpd_en":    data.limit_uses_per_driver_enabled,
                    "lpd":       data.limit_uses_per_driver,
                    "lad_en":    data.limit_uses_across_drivers_enabled,
                    "lad":       data.limit_uses_across_drivers,
                    "lpday_en":  data.limit_uses_per_day_enabled,
                    "lpday":     data.limit_uses_per_day,
                    "src_col_id": data.status_rate_column_id,
                    "uid":       user_id,
                },
            )
            new_id = ins.scalar_one()
            break
        except SAIntegrityError:
            # Unique violation on NormalizedStatusCode — retry with new code
            continue

    if new_id is None:
        raise HTTPException(
            status_code=500,
            detail="Could not generate a unique status key code after 5 attempts. Please try again.",
        )

    await audit.write_settings_audit(
        db,
        company_id=company_id,
        branch_id=branch_id,
        user_id=user_id,
        action_code="STATUS_KEY_CREATED",
        entity_name="PayrollStatusKeys",
        entity_id=str(new_id),
        new_value={
            "key_name":      data.key_name,
            "status_code":   generated_code,
            "hours_value":   str(data.hours_value),
            "is_off_reason": data.is_off_reason,
            "deducts_from_yearly_allowance": data.deducts_from_yearly_allowance,
            "allowance_category": data.allowance_category,
        },
    )

    row_result = await db.execute(
        text(f"SELECT {_STATUS_KEY_COLS} FROM payroll.payrollstatuskeys WHERE statuskeyid=:kid"),
        {"kid": new_id},
    )
    return _row_to_status_key(row_result.mappings().one())


async def update_status_key(
    key_id: int,
    branch_id: int,
    company_id: int,
    user_id: int,
    data: StatusKeyUpdate,
    db: AsyncConnection,
) -> StatusKey:
    """
    Partially update a status key — only non-None fields are applied.
    Requires AllCompanyBranches scope.
    """
    await security.ensure_company_admin(company_id, user_id, db)

    # Fetch and lock the target row.
    cur = await db.execute(
        text(f"""
            SELECT {_STATUS_KEY_COLS}
            FROM   payroll.payrollstatuskeys
            WHERE  statuskeyid = :kid
              AND  branchid    = :bid
              AND  companyid   = :cid
            FOR UPDATE
        """),
        {"kid": key_id, "bid": branch_id, "cid": company_id},
    )
    row = cur.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Status key {key_id} not found.",
        )

    # Merge: apply patch where non-None, otherwise keep current.
    new_key_name = data.key_name.strip() if data.key_name is not None else row["keyname"]
    new_hours    = data.hours_value      if data.hours_value  is not None else row["hoursvalue"]
    new_off      = data.is_off_reason    if data.is_off_reason is not None else row["isoffreason"]
    new_deducts  = (
        data.deducts_from_yearly_allowance
        if data.deducts_from_yearly_allowance is not None
        else row["deductsfromyearlyallowance"]
    )
    new_cat      = data.allowance_category  if data.allowance_category  is not None else row.get("allowancecategory")
    new_active   = data.is_active           if data.is_active           is not None else row["isactive"]
    new_order    = data.display_order       if data.display_order       is not None else int(row.get("displayorder") or 0)

    # status_rate_column_id: -1 is the clear sentinel (set to NULL)
    if data.status_rate_column_id is not None:
        if data.status_rate_column_id == -1:
            new_src_col_id: int | None = None
        else:
            new_src_col_id = data.status_rate_column_id
    else:
        new_src_col_id = row.get("statusratecolumnid")

    # Usage limits
    new_lpp_en  = data.limit_uses_per_period_enabled    if data.limit_uses_per_period_enabled    is not None else bool(row.get("limitusesperperiodenabled") or False)
    new_lpp     = data.limit_uses_per_period            if data.limit_uses_per_period            is not None else row.get("limitusesperperiod")
    new_lpd_en  = data.limit_uses_per_driver_enabled    if data.limit_uses_per_driver_enabled    is not None else bool(row.get("limitusesperdriverenabled") or False)
    new_lpd     = data.limit_uses_per_driver            if data.limit_uses_per_driver            is not None else row.get("limitusesperdriver")
    new_lad_en  = data.limit_uses_across_drivers_enabled if data.limit_uses_across_drivers_enabled is not None else bool(row.get("limitusesacrossdriversenabled") or False)
    new_lad     = data.limit_uses_across_drivers        if data.limit_uses_across_drivers        is not None else row.get("limitusesacrossdrivers")
    new_lpday_en = data.limit_uses_per_day_enabled      if data.limit_uses_per_day_enabled       is not None else bool(row.get("limitusesperdayenabled") or False)
    new_lpday   = data.limit_uses_per_day               if data.limit_uses_per_day               is not None else row.get("limitusesperday")

    # StatusCode is immutable — always keep the existing generated code.
    existing_normalized = row["normalizedstatuscode"]

    _validate_deduction_rules(new_deducts, new_off, new_cat)

    # Validate status_rate_column_id if it is being set (not cleared)
    if new_src_col_id is not None:
        await status_rate_columns.validate_status_rate_column_for_branch(
            new_src_col_id, branch_id, company_id, db,
        )
        if new_hours <= 0:
            raise HTTPException(
                status_code=422,
                detail="hours_value must be greater than 0 when status_rate_column_id is set.",
            )

    # Validate merged usage limits
    if new_lpp_en and not (new_lpp and new_lpp > 0):
        raise HTTPException(status_code=422, detail="limit_uses_per_period must be a positive integer when enabled.")
    if new_lpd_en and not (new_lpd and new_lpd > 0):
        raise HTTPException(status_code=422, detail="limit_uses_per_driver must be a positive integer when enabled.")
    if new_lad_en and not (new_lad and new_lad > 0):
        raise HTTPException(status_code=422, detail="limit_uses_across_drivers must be a positive integer when enabled.")
    if new_lpday_en and not (new_lpday and new_lpday > 0):
        raise HTTPException(status_code=422, detail="limit_uses_per_day must be a positive integer when enabled.")

    # Reactivation uniqueness check: if key is being activated and another
    # active key already holds the same normalized code, reject.
    being_activated = (not bool(row["isactive"])) and new_active
    if being_activated:
        await _check_status_key_code_unique(
            branch_id, company_id, existing_normalized, db, exclude_key_id=key_id
        )

    try:
        await db.execute(
            text("""
                UPDATE payroll.payrollstatuskeys
                SET    keyname                    = :kname,
                       hoursvalue                = :hours,
                       isoffreason               = :off,
                       deductsfromyearlyallowance = :deducts,
                       allowancecategory         = :cat,
                       isactive                  = :active,
                       displayorder              = :order,
                       limitusesperperiodenabled  = :lpp_en,
                       limitusesperperiod         = :lpp,
                       limitusesperdriverenabled  = :lpd_en,
                       limitusesperdriver         = :lpd,
                       limitusesacrossdriversenabled = :lad_en,
                       limitusesacrossdrivers     = :lad,
                       limitusesperdayenabled     = :lpday_en,
                       limitusesperday            = :lpday,
                       statusratecolumnid        = :src_col_id,
                       updatedbyuserid           = :uid,
                       updatedatutc              = NOW()
                WHERE  statuskeyid = :kid
            """),
            {
                "kid":     key_id,
                "kname":   new_key_name,
                "hours":   new_hours,
                "off":     new_off,
                "deducts": new_deducts,
                "cat":     new_cat,
                "active":  new_active,
                "order":   new_order,
                "lpp_en":  new_lpp_en,
                "lpp":     new_lpp,
                "lpd_en":  new_lpd_en,
                "lpd":     new_lpd,
                "lad_en":  new_lad_en,
                "lad":     new_lad,
                "lpday_en":   new_lpday_en,
                "lpday":      new_lpday,
                "src_col_id": new_src_col_id,
                "uid":        user_id,
            },
        )
    except SAIntegrityError as exc:
        msg = str(exc.orig).lower() if exc.orig else str(exc).lower()
        if "ux_payrollstatuskeys_activecode" in msg:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"A status key with code '{existing_normalized}' "
                    "is already active for this branch."
                ),
            )
        if "ck_statuskeys_" in msg:
            raise HTTPException(
                status_code=422,
                detail="Usage limit value must be a positive integer when the limit is enabled.",
            )
        raise HTTPException(
            status_code=422,
            detail="Status key could not be saved — a uniqueness constraint was violated.",
        )

    await audit.write_settings_audit(
        db,
        company_id=company_id,
        branch_id=branch_id,
        user_id=user_id,
        action_code="STATUS_KEY_UPDATED",
        entity_name="PayrollStatusKeys",
        entity_id=str(key_id),
        old_value={
            "key_name":    row["keyname"],
            "hours_value": str(row["hoursvalue"]),
            "is_off_reason": row["isoffreason"],
            "is_active":   row["isactive"],
        },
        new_value={
            "key_name":    new_key_name,
            "hours_value": str(new_hours),
            "is_off_reason": new_off,
            "is_active":   new_active,
        },
    )

    row_result = await db.execute(
        text(f"SELECT {_STATUS_KEY_COLS} FROM payroll.payrollstatuskeys WHERE statuskeyid=:kid"),
        {"kid": key_id},
    )
    return _row_to_status_key(row_result.mappings().one())


async def delete_status_key(
    key_id: int,
    branch_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> StatusKey:
    """
    Soft-delete (deactivate) a status key.

    The key is set to is_active=FALSE; it stays in the database for historical
    reference and is visible with include_inactive=true.
    Idempotent: if the key is already inactive, returns it unchanged.
    Requires AllCompanyBranches scope.
    """
    await security.ensure_company_admin(company_id, user_id, db)

    cur = await db.execute(
        text(f"""
            SELECT {_STATUS_KEY_COLS}
            FROM   payroll.payrollstatuskeys
            WHERE  statuskeyid = :kid
              AND  branchid    = :bid
              AND  companyid   = :cid
            FOR UPDATE
        """),
        {"kid": key_id, "bid": branch_id, "cid": company_id},
    )
    row = cur.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Status key {key_id} not found.",
        )

    # Idempotent: already inactive → return as-is (no audit write).
    if not bool(row["isactive"]):
        return _row_to_status_key(row)

    await db.execute(
        text("""
            UPDATE payroll.payrollstatuskeys
            SET    isactive        = FALSE,
                   updatedbyuserid = :uid,
                   updatedatutc    = NOW()
            WHERE  statuskeyid = :kid
        """),
        {"kid": key_id, "uid": user_id},
    )

    await audit.write_settings_audit(
        db,
        company_id=company_id,
        branch_id=branch_id,
        user_id=user_id,
        action_code="STATUS_KEY_DEACTIVATED",
        entity_name="PayrollStatusKeys",
        entity_id=str(key_id),
        old_value={"is_active": True,  "status_code": row["statuscode"]},
        new_value={"is_active": False},
    )

    row_result = await db.execute(
        text(f"SELECT {_STATUS_KEY_COLS} FROM payroll.payrollstatuskeys WHERE statuskeyid=:kid"),
        {"kid": key_id},
    )
    return _row_to_status_key(row_result.mappings().one())
