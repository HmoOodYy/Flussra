"""
CDPI permission guards.

Two reusable helpers that enforce access to the CDPI workflow using the
existing `payitems.edit` permission and the project's scope model.

Both helpers delegate to sec.fn_UserHasPermission, which is called by
_check_permission in app.core.service.

Branch guard (require_cdpi_branch_edit):
  - Passes for SpecificBranch users whose assignment covers the target branch.
  - Passes for AllCompanyBranches users.
  - Requires payitems.edit on that branch.

Company guard (require_cdpi_company_edit):
  - Passes only for AllCompanyBranches users with payitems.edit.
  - SpecificBranch users with payitems.edit are correctly denied because
    fn_UserHasPermission with branch_id=NULL returns false for them.
"""
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import _check_permission

_CDPI_PERMISSION = "payitems.edit"


async def require_cdpi_branch_edit(
    company_id: int,
    user_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> None:
    """
    Gate for branch-level CDPI actions.

    Future callers: Draft create/edit/submit/copy, branch activation,
    display-name override.

    Raises HTTP 403 if the user does not hold payitems.edit on branch_id.
    """
    await _check_permission(company_id, user_id, branch_id, _CDPI_PERMISSION, db)


async def require_cdpi_company_edit(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> None:
    """
    Gate for company-level CDPI actions.

    Future callers: company approval, return, reject, direct company creation
    of a Pay Item.

    Passing branch_id=None to fn_UserHasPermission means only
    AllCompanyBranches assignments satisfy the check.  SpecificBranch users
    holding payitems.edit on their own branch are correctly denied.

    Raises HTTP 403 if the user does not have AllCompanyBranches scope with
    payitems.edit.
    """
    await _check_permission(company_id, user_id, None, _CDPI_PERMISSION, db)
