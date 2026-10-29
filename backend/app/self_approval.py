"""Company policy: may the person who prepared something also approve it?"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def company_allows_self_approval(company_id: int, db: AsyncConnection) -> bool:
    """False only when the Company explicitly disallows self-approval.

    A missing Company row or a NULL flag counts as allowed, matching the column default.
    """
    allowed = (await db.execute(
        text("SELECT allowselfapproval FROM core.companies WHERE companyid = :cid"),
        {"cid": company_id},
    )).scalar_one_or_none()
    return allowed is not False
