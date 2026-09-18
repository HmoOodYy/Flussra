"""
Period pay-item snapshot read accessors — shared read access to
payroll.payrollperiodpayitems.

Extracted from app.payroll.service (Stage B4-11E) as a dependency-closed leaf
module — no behavior change, pure relocation.

_period_has_pay_item_snapshot and _get_period_pay_item_snapshot are read
accessors over the PayrollPeriodPayItems snapshot table. They are NOT owned
by app.payroll.period_creation, Period Pay, or Day Grid: these two helpers
were briefly moved into app.payroll.period_creation during Stage B4-4A and
then returned, because period_creation.py only ever *writes*
PayrollPeriodDays/PayrollPeriodPayItems once (via its own
_create_period_day_rows/_create_period_pay_item_rows) and has no caller of
either helper inside its own logic. They are read/validate accessors
consumed by Period Pay and Day Grid — neither of which is more entitled to
own them than the other.

This module owns only shared read access to snapshot rows: determining
whether a period has any snapshot rows, and retrieving snapshot rows for a
period. It does not create snapshot rows, does not mutate PayItems, does not
perform source locking, and does not infer or reconstruct missing historical
snapshot state from live/mutable PayItems data.
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def _period_has_pay_item_snapshot(
    period_id: int,
    db: AsyncConnection,
) -> bool:
    """Return True if the period has any PayrollPeriodPayItems rows (post-0053 period)."""
    result = await db.execute(
        text("SELECT 1 FROM payroll.payrollperiodpayitems WHERE payrollperiodid = :pid LIMIT 1"),
        {"pid": period_id},
    )
    return result.first() is not None


async def _get_period_pay_item_snapshot(
    period_id: int,
    company_id: int,
    db: AsyncConnection,
    scope: str | None = None,
    active_only: bool = False,
) -> list:
    """
    Return PayrollPeriodPayItems rows for a period.

    scope: 'Daily' | 'Period' | None (all)
    active_only: if True, only rows with IsActiveInPeriod = TRUE

    Returns list of mapping rows. Empty list if no snapshot exists (legacy period).
    """
    filters = ["payrollperiodid = :pid", "companyid = :cid"]
    params: dict = {"pid": period_id, "cid": company_id}
    if scope:
        filters.append("itemscope = :scope")
        params["scope"] = scope
    if active_only:
        filters.append("isactiveinperiod = TRUE")
    where = " AND ".join(filters)
    result = await db.execute(
        text(f"""
            SELECT payitemcode, payitemname, displaylabel, category, datatype, unit,
                   itemscope, ratebehavior, appearsinpayrollentry, isactiveinperiod,
                   payitemid, payitemstatusatsnapshot, sortorder
            FROM payroll.payrollperiodpayitems
            WHERE {where}
            ORDER BY sortorder NULLS LAST, payitemcode
        """),
        params,
    )
    return result.mappings().all()
