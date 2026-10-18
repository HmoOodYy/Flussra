"""SQL-backed Branch business dates and supported timezone catalog access."""

from datetime import date, datetime

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


def _aware_instant(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("at must be a timezone-aware datetime")
    return value


async def branch_today(
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
    *,
    at: datetime | None = None,
) -> date:
    """Return the database-authoritative business date for one Branch."""
    instant = _aware_instant(at)
    if instant is None:
        result = await db.execute(
            text("SELECT core.fn_BranchToday(:company_id, :branch_id)"),
            {"company_id": company_id, "branch_id": branch_id},
        )
    else:
        result = await db.execute(
            text("SELECT core.fn_BranchToday(:company_id, :branch_id, :at)"),
            {"company_id": company_id, "branch_id": branch_id, "at": instant},
        )
    value = result.scalar_one_or_none()
    if value is None:
        raise LookupError(f"Branch {branch_id} was not found in Company {company_id}")
    return value


async def prospective_today(
    time_zone_id: str,
    db: AsyncConnection,
    *,
    at: datetime | None = None,
) -> date:
    """Return a prospective Branch business date before a Branch exists."""
    if not await validate_time_zone(time_zone_id, db):
        raise HTTPException(status_code=422, detail="Unsupported timezone identifier.")
    instant = _aware_instant(at)
    if instant is None:
        result = await db.execute(
            text("SELECT core.fn_BusinessDate(:time_zone_id)"),
            {"time_zone_id": time_zone_id},
        )
    else:
        result = await db.execute(
            text("SELECT core.fn_BusinessDate(:time_zone_id, :at)"),
            {"time_zone_id": time_zone_id, "at": instant},
        )
    return result.scalar_one()


async def validate_time_zone(time_zone_id: str, db: AsyncConnection) -> bool:
    """Check a timezone against PostgreSQL's supported-zone catalog."""
    result = await db.execute(
        text("SELECT core.fn_IsSupportedTimeZone(:time_zone_id)"),
        {"time_zone_id": time_zone_id},
    )
    return bool(result.scalar_one())


async def supported_time_zones(db: AsyncConnection) -> list[str]:
    result = await db.execute(
        text("SELECT TimeZoneId FROM core.SupportedTimeZones ORDER BY TimeZoneId")
    )
    return list(result.scalars().all())
