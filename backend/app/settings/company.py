"""Company profile and supported-currency settings operations."""

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.branch_access import check_branch_access
from app.company_concurrency import lock_company_for_mutation
from app.company_currency import company_has_durable_monetary_state, currency_error
from app.settings import audit, security
from app.settings.schemas import (
    CompanyProfile,
    CompanyUpdate,
    SupportedCurrency,
)


async def get_company_profile(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> CompanyProfile:
    """
    Return the company profile including the default branch name.

    Any authenticated user in the company can read this.
    """
    # Verifies the user has at least some access to this company.
    await check_branch_access(company_id, user_id, db)

    result = await db.execute(
        text("""
            SELECT
                c.companyid,
                c.companycode,
                c.companyname,
                c.legalname,
                c.status,
                c.issuspended,
                c.timezonename,
                c.notes,
                c.allowselfapproval,
                c.currencycode,
                sc.currencyname,
                sc.minorunitdigits AS currency_minor_unit_digits,
                core.fn_company_has_durable_monetary_state(c.companyid) AS has_monetary_state,
                c.createdatutc,
                c.updatedatutc,
                b.branchid   AS default_branch_id,
                b.branchname AS default_branch_name
            FROM  core.companies c
            LEFT  JOIN core.supportedcurrencies sc
                   ON sc.currencycode = c.currencycode
            LEFT  JOIN core.branches b
                   ON  b.companyid = c.companyid
                   AND b.isdefault = TRUE
            WHERE  c.companyid = :cid
            ORDER  BY b.branchname NULLS LAST
            LIMIT  1
        """),
        {"cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found.",
        )

    if row["currencycode"] is None and row["has_monetary_state"]:
        raise currency_error(
            "COMPANY_CURRENCY_INVARIANT_VIOLATION",
            "Unconfigured Company has durable monetary state; reset/reseed is required.",
            409,
        )

    return CompanyProfile(
        company_id=row["companyid"],
        company_code=row["companycode"],
        company_name=row["companyname"],
        legal_name=row.get("legalname"),
        status=row["status"],
        is_suspended=bool(row["issuspended"]),
        timezone_name=row["timezonename"],
        notes=row.get("notes"),
        allow_self_approval=bool(row.get("allowselfapproval", True)),
        currency_code=row["currencycode"],
        currency_name=row["currencyname"],
        currency_minor_unit_digits=row["currency_minor_unit_digits"],
        currency_change_locked=bool(row["currencycode"] and row["has_monetary_state"]),
        default_branch_id=row.get("default_branch_id"),
        default_branch_name=row.get("default_branch_name"),
        created_at_utc=row["createdatutc"],
        updated_at_utc=row.get("updatedatutc"),
    )


async def update_company_profile(
    company_id: int,
    user_id: int,
    data: CompanyUpdate,
    db: AsyncConnection,
) -> CompanyProfile:
    """
    Update editable company profile fields (name, legal name, timezone, notes).

    Status and IsSuspended are system-controlled and cannot be changed here.
    Requires AllCompanyBranches scope.
    """
    await security.ensure_company_admin(company_id, user_id, db)

    # Company mutation takes the conflicting row lock first. It must never take
    # the shared monetary guard and then upgrade: two such transactions would
    # each hold SHARE and deadlock on the UPDATE.
    await lock_company_for_mutation(company_id, db)
    current = await get_company_profile(company_id, user_id, db)
    currency_changed = "currency_code" in data.model_fields_set
    new_currency = current.currency_code
    if currency_changed:
        if data.currency_code is None:
            raise currency_error(
                "COMPANY_CURRENCY_REQUIRED", "Company currency cannot be cleared."
            )
        new_currency = data.currency_code
        supported = (await db.execute(
            text("SELECT 1 FROM core.supportedcurrencies WHERE currencycode = :code"),
            {"code": new_currency},
        )).scalar_one_or_none()
        if supported is None:
            raise currency_error(
                "UNSUPPORTED_CURRENCY_CODE",
                f"Currency code '{new_currency}' is not supported.",
            )
        if new_currency != current.currency_code:
            if await company_has_durable_monetary_state(company_id, db):
                raise currency_error(
                    "COMPANY_CURRENCY_CHANGE_BLOCKED",
                    "Company currency is locked after the first monetary write.",
                    409,
                )
            await db.execute(
                text("UPDATE core.companies SET currencycode = :code WHERE companyid = :cid"),
                {"cid": company_id, "code": new_currency},
            )

    # Preserve existing values when none are supplied in the patch.
    new_tz             = data.timezone_name      if data.timezone_name      is not None else current.timezone_name
    new_self_approval  = data.allow_self_approval if data.allow_self_approval is not None else current.allow_self_approval

    await db.execute(
        text("""
            UPDATE core.companies
            SET    companyname       = :name,
                   legalname         = :legal,
                   timezonename      = :tz,
                   notes             = :notes,
                   allowselfapproval = :allow_self,
                   updatedatutc      = NOW()
            WHERE  companyid = :cid
        """),
        {
            "cid":        company_id,
            "name":       data.company_name,
            "legal":      data.legal_name,
            "tz":         new_tz,
            "notes":      data.notes,
            "allow_self": new_self_approval,
        },
    )

    await audit.write_settings_audit(
        db,
        company_id=company_id,
        branch_id=None,
        user_id=user_id,
        action_code="COMPANY_UPDATED",
        entity_name="Companies",
        entity_id=str(company_id),
        old_value={
            "company_name":       current.company_name,
            "legal_name":         current.legal_name,
            "timezone_name":      current.timezone_name,
            "notes":              current.notes,
            "allow_self_approval": current.allow_self_approval,
            "currency_code": current.currency_code,
        },
        new_value={
            "company_name":       data.company_name,
            "legal_name":         data.legal_name,
            "timezone_name":      new_tz,
            "notes":              data.notes,
            "allow_self_approval": new_self_approval,
            "currency_code": new_currency,
        },
    )

    return await get_company_profile(company_id, user_id, db)


async def list_supported_currencies(
    company_id: int, user_id: int, db: AsyncConnection
) -> list[SupportedCurrency]:
    await security.ensure_company_admin(company_id, user_id, db)
    rows = (await db.execute(text("""
        SELECT currencycode, currencyname, numericcode, minorunitdigits
        FROM core.supportedcurrencies ORDER BY currencycode
    """))).mappings().all()
    return [
        SupportedCurrency(
            currency_code=row["currencycode"],
            currency_name=row["currencyname"],
            numeric_code=row["numericcode"].strip(),
            minor_unit_digits=row["minorunitdigits"],
        )
        for row in rows
    ]
