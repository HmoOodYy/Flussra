"""Company-local calendar date for Payroll Setup product guardrails and display; never payroll authority."""

from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from .errors import PolicyError


async def company_today(company_id: int, db: AsyncConnection) -> date:
    result = await db.execute(text("""
        SELECT (NOW() AT TIME ZONE c.TimeZoneName)::date
        FROM core.Companies c WHERE c.CompanyID = :cid
    """), {"cid": company_id})
    today = result.scalar_one_or_none()
    if today is None:
        raise PolicyError("COMPANY_NOT_FOUND", "Company not found")
    return today
