"""Date-explicit resolution of a company's effective Driver profile."""

from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from .clock import company_today


async def resolve_effective_driver_profile(
    company_id: int,
    employee_id: int,
    effective_date: date,
    db: AsyncConnection,
) -> dict[str, Any] | None:
    """Return the one profile effective for the Employee on the supplied date."""
    result = await db.execute(
        text("""
            SELECT d.*
            FROM core.drivers d
            WHERE d.driverid = core.fn_EffectiveDriverProfile(
                :company_id, :employee_id, :effective_date
            )
        """),
        {"company_id": company_id, "employee_id": employee_id, "effective_date": effective_date},
    )
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


async def resolve_current_driver_profile(
    company_id: int,
    employee_id: int,
    db: AsyncConnection,
) -> dict[str, Any] | None:
    """Resolve today's profile using the existing company-local business clock."""
    business_date = await company_today(company_id, db)
    return await resolve_effective_driver_profile(company_id, employee_id, business_date, db)
