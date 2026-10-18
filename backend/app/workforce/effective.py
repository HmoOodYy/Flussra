"""Instant-based resolution of a Company's effective Driver profile."""

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def resolve_effective_driver_profile(
    company_id: int,
    employee_id: int,
    effective_at: datetime | None,
    db: AsyncConnection,
) -> dict[str, Any] | None:
    """Return the profile whose half-open assignment contains the instant."""
    if effective_at is None:
        result = await db.execute(text("""
            SELECT d.*
            FROM core.drivers d
            WHERE d.driverid = core.fn_EffectiveDriverProfile(:company_id, :employee_id)
        """), {"company_id": company_id, "employee_id": employee_id})
    else:
        if effective_at.tzinfo is None or effective_at.utcoffset() is None:
            raise ValueError("effective_at must be timezone-aware")
        result = await db.execute(text("""
            SELECT d.*
            FROM core.drivers d
            WHERE d.driverid = core.fn_EffectiveDriverProfile(:company_id, :employee_id, :effective_at)
        """), {
            "company_id": company_id,
            "employee_id": employee_id,
            "effective_at": effective_at,
        })
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None
