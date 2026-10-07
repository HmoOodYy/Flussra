"""Permission guards for target Compensation.

PayDefinition governance reuses the existing Pay Item definition permission
and Driver assignment authoring reuses the existing Pay Rates permissions.
Every guard denies DRIVER/Self subjects through the shared permission helpers.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.policy import require_non_driver_subject
from app.compensation.errors import compensation_error
from app.core.service import _check_any_permission, _check_permission

_DEFINITION_EDIT = "payitems.edit"
_DEFINITION_READ = ["payitems.view", "payitems.edit", "settings.manage", "setup.manage"]
_RATE_EDIT = ["payrates.edit", "settings.manage", "setup.manage"]
_RATE_READ = ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"]


async def require_definition_branch_edit(
    company_id: int, user_id: int, branch_id: int, db: AsyncConnection,
) -> None:
    """Branch-scoped request actions: draft, edit, submit and copy."""
    await _check_permission(company_id, user_id, branch_id, _DEFINITION_EDIT, db)


async def require_definition_company_edit(
    company_id: int, user_id: int, db: AsyncConnection,
) -> None:
    """Company-wide actions: decide and direct creation (AllCompanyBranches scope)."""
    await _check_permission(company_id, user_id, None, _DEFINITION_EDIT, db)


async def require_definition_company_read(
    company_id: int, user_id: int, db: AsyncConnection,
) -> None:
    await _check_any_permission(company_id, user_id, None, _DEFINITION_READ, db)


async def require_rate_edit(
    company_id: int, user_id: int, branch_id: int, db: AsyncConnection,
) -> None:
    await _check_any_permission(company_id, user_id, branch_id, _RATE_EDIT, db)


async def require_rate_read(
    company_id: int, user_id: int, branch_id: int, db: AsyncConnection,
) -> None:
    await _check_any_permission(company_id, user_id, branch_id, _RATE_READ, db)


async def load_company_driver_branch(
    company_id: int, user_id: int, driver_id: int, db: AsyncConnection,
) -> int:
    """Return the Driver's BranchID or raise 404 when it is not in the Company."""
    await require_non_driver_subject(company_id, user_id, db)
    branch_id = (await db.execute(
        text("SELECT branchid FROM core.drivers WHERE driverid = :did AND companyid = :cid"),
        {"did": driver_id, "cid": company_id},
    )).scalar_one_or_none()
    if branch_id is None:
        raise compensation_error("DRIVER_NOT_FOUND", "Driver not found.", 404)
    return int(branch_id)
