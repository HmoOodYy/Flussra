"""Company-wide monetary denomination and lock authority for P3a."""

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll_setup.locks import lock_company


@dataclass(frozen=True, slots=True)
class CompanyCurrency:
    code: str
    minor_unit_digits: int


def currency_error(code: str, message: str, status_code: int = 422) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


async def get_company_currency(
    company_id: int, db: AsyncConnection
) -> CompanyCurrency | None:
    """Read currency without locking; use only for reads and previews."""
    row = (await db.execute(text("""
        SELECT c.currencycode, sc.minorunitdigits
        FROM core.companies c
        LEFT JOIN core.supportedcurrencies sc ON sc.currencycode = c.currencycode
        WHERE c.companyid = :cid
    """), {"cid": company_id})).mappings().first()
    if row is None:
        raise currency_error("COMPANY_CURRENCY_INVARIANT_VIOLATION", "Company does not exist.", 409)
    if row["currencycode"] is None:
        if await company_has_durable_monetary_state(company_id, db):
            raise currency_error(
                "COMPANY_CURRENCY_INVARIANT_VIOLATION",
                "Unconfigured Company has durable monetary state.",
                409,
            )
        return None
    if row["minorunitdigits"] is None:
        raise currency_error(
            "COMPANY_CURRENCY_INVARIANT_VIOLATION",
            "Configured Company currency is not in the supported catalog.",
            409,
        )
    return CompanyCurrency(str(row["currencycode"]), int(row["minorunitdigits"]))


async def require_company_currency(
    company_id: int, db: AsyncConnection
) -> CompanyCurrency:
    """Read-only requirement; monetary mutations must use the locking variant."""
    currency = await get_company_currency(company_id, db)
    if currency is None:
        raise currency_error(
            "COMPANY_CURRENCY_REQUIRED",
            "Configure Company currency before creating monetary state.",
        )
    return currency


async def lock_and_get_company_currency(
    company_id: int, db: AsyncConnection, *, required: bool = True
) -> CompanyCurrency | None:
    """Lock the canonical Company row, then read its current currency."""
    await lock_company(company_id, db)
    currency = await get_company_currency(company_id, db)
    if required and currency is None:
        raise currency_error(
            "COMPANY_CURRENCY_REQUIRED",
            "Configure Company currency before creating monetary state.",
        )
    return currency


async def company_has_durable_monetary_state(
    company_id: int, db: AsyncConnection
) -> bool:
    result = await db.execute(
        text("SELECT core.fn_company_has_durable_monetary_state(:cid)"),
        {"cid": company_id},
    )
    return bool(result.scalar_one())


def require_matching_snapshot_currency(
    current: CompanyCurrency, snapshot_code: str | None, snapshot_minor: int | None
) -> CompanyCurrency:
    if snapshot_code is None or snapshot_minor is None:
        raise currency_error(
            "SNAPSHOT_CURRENCY_REQUIRED",
            "Immutable snapshot currency is missing.",
            409,
        )
    if snapshot_code != current.code or int(snapshot_minor) != current.minor_unit_digits:
        raise currency_error(
            "SNAPSHOT_CURRENCY_MISMATCH",
            "Immutable snapshot currency does not match Company currency.",
            409,
        )
    return CompanyCurrency(snapshot_code, int(snapshot_minor))


def frozen_currency(code: str | None, minor_unit_digits: int | None) -> CompanyCurrency:
    """Validate immutable monetary metadata without consulting current Company state."""
    if code is None or minor_unit_digits is None:
        raise currency_error("SNAPSHOT_CURRENCY_REQUIRED", "Immutable monetary currency is missing.", 409)
    return CompanyCurrency(str(code), int(minor_unit_digits))
