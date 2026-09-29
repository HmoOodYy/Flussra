"""Company-local business date for Workforce current-profile resolution."""

from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def company_today(company_id: int, db: AsyncConnection) -> date:
    result = await db.execute(
        text("SELECT core.fn_CompanyToday(:company_id)"),
        {"company_id": company_id},
    )
    business_date = result.scalar_one_or_none()
    if business_date is None:
        raise LookupError("Company not found")
    return business_date
