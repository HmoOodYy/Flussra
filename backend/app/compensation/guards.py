"""Permission guards for target Compensation.

PayDefinition governance reuses the existing Pay Item definition permission
and Driver rate schedule authoring reuses the existing Pay Rates permissions
(the rate-edit and rate-read sets below; see app.compensation.driver_context for who holds
them over a given Driver).
Every guard denies DRIVER/Self subjects through the shared permission helpers.
"""

from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.permissions import check_any_permission, check_permission

_DEFINITION_EDIT = "payitems.edit"
_DEFINITION_READ = ["payitems.view", "payitems.edit"]
RATE_EDIT = ["payrates.edit"]
RATE_READ = ["payrates.view", "payrates.edit"]


async def require_definition_branch_edit(
    company_id: int, user_id: int, branch_id: int, db: AsyncConnection,
) -> None:
    """Branch-scoped request actions: draft, edit, submit and copy."""
    await check_permission(company_id, user_id, branch_id, _DEFINITION_EDIT, db)


async def require_definition_company_edit(
    company_id: int, user_id: int, db: AsyncConnection,
) -> None:
    """Company-wide actions: decide and direct creation (AllCompanyBranches scope)."""
    await check_permission(company_id, user_id, None, _DEFINITION_EDIT, db)


async def require_definition_company_read(
    company_id: int, user_id: int, db: AsyncConnection,
) -> None:
    await check_any_permission(company_id, user_id, None, _DEFINITION_READ, db)
