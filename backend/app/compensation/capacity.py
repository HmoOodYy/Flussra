"""Serialized product-capacity checks for canonical compensation writers."""

from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.compensation.errors import compensation_error
from app.product_policy import DEFAULT_PRODUCT_POLICY, ProductPolicy

# Status is one shared payroll-input concept, regardless of selectable Status keys.
# It consumes one Branch slot but is not a separately configurable allowance.
STATUS_CONCEPT_COUNT = 1


async def lock_company_pay_definition_capacity(
    company_id: int, db: AsyncConnection,
) -> None:
    """Serialize Company definition creation and retirement through transaction end."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"company-pay-definition-capacity:{company_id}"},
    )


async def enforce_company_pay_definition_capacity(
    company_id: int, db: AsyncConnection,
    policy: ProductPolicy = DEFAULT_PRODUCT_POLICY,
) -> int:
    """Reject creation when the Company already has its configured active item count."""
    await lock_company_pay_definition_capacity(company_id, db)
    count = int((await db.execute(
        text("""
            SELECT count(*)
            FROM payroll.paydefinitions
            WHERE companyid = :company_id AND status = 'Active'
        """),
        {"company_id": company_id},
    )).scalar_one())
    limit = policy.max_company_pay_items
    if count >= limit:
        raise compensation_error(
            "COMPANY_PAY_ITEM_CAPACITY_EXCEEDED",
            f"This Company has {count} active pay items; its current limit is {limit}.",
        )
    return count


async def enforce_branch_input_capacity(
    company_id: int, branch_id: int, from_date: date, db: AsyncConnection,
    policy: ProductPolicy = DEFAULT_PRODUCT_POLICY,
) -> None:
    """Check active effective-dated Branch concepts with a grouped SQL event sweep."""
    row = (await db.execute(
        text("""
            WITH active_intervals AS (
                SELECT GREATEST(bpic.effectivefrom, :from_date)::date AS starts_on,
                       CASE WHEN bpic.effectiveto IS NULL THEN NULL
                            ELSE bpic.effectiveto + 1 END AS ends_after
                FROM payroll.branchpaydefinitionconfig AS bpic
                JOIN payroll.paydefinitions AS pd
                  ON pd.paydefinitionid = bpic.paydefinitionid
                 AND pd.companyid = bpic.companyid
                WHERE bpic.companyid = :company_id
                  AND bpic.branchid = :branch_id
                  AND pd.status = 'Active'
                  AND bpic.isactive
                  AND (bpic.effectiveto IS NULL OR bpic.effectiveto >= :from_date)
            ), events AS (
                SELECT starts_on AS event_date, 1::bigint AS delta
                FROM active_intervals
                UNION ALL
                SELECT ends_after AS event_date, -1::bigint AS delta
                FROM active_intervals
                WHERE ends_after IS NOT NULL
            ), grouped_events AS (
                SELECT event_date, sum(delta) AS delta
                FROM events
                GROUP BY event_date
            ), timeline AS (
                SELECT event_date,
                       sum(delta) OVER (
                           ORDER BY event_date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ) + :status_concepts AS concept_count
                FROM grouped_events
            )
            SELECT event_date, concept_count
            FROM timeline
            WHERE concept_count > :capacity_limit
            ORDER BY event_date
            LIMIT 1
        """),
        {
            "company_id": company_id,
            "branch_id": branch_id,
            "from_date": from_date,
            "status_concepts": STATUS_CONCEPT_COUNT,
            "capacity_limit": policy.max_branch_input_concepts,
        },
    )).mappings().first()
    if row is not None:
        count = int(row["concept_count"])
        limit = policy.max_branch_input_concepts
        raise compensation_error(
            "BRANCH_INPUT_CAPACITY_EXCEEDED",
            f"Branch {branch_id} would have {count} active input concepts on "
            f"{row['event_date']}; its current limit is {limit} (including one Status concept).",
        )
