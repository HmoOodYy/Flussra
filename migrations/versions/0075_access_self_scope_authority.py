"""0075: migrate Access assignments to generic Self scope authority."""
import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0075"
down_revision: str | None = "0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATIONS_DIR = Path(__file__).parent.parent
_SQL_FILE = _MIGRATIONS_DIR / "sql" / "0075_access_self_scope_authority.sql"
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    bind = op.get_bind()
    self_count = bind.execute(sa.text(
        "SELECT COUNT(*) FROM sec.UserBranchRoles WHERE ScopeType = 'Self'"
    )).scalar_one()
    if self_count:
        raise RuntimeError(
            "Downgrade of 0075 refused: Self assignment data cannot be converted "
            "back to OwnDriverDataOnly without fabricating BranchID authority."
        )

    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_userbranchroles_scope_authority ON sec.UserBranchRoles"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS sec.fn_enforce_userbranchrole_scope_authority()"))
    op.execute(sa.text("ALTER TABLE sec.UserBranchRoles DROP CONSTRAINT ck_UserBranchRoles_ScopeType, DROP CONSTRAINT ck_UserBranchRoles_BranchScope"))
    op.execute(sa.text("""
        ALTER TABLE sec.UserBranchRoles
            ADD CONSTRAINT ck_UserBranchRoles_ScopeType
                CHECK (ScopeType IN ('AllCompanyBranches', 'SpecificBranch', 'OwnDriverDataOnly')),
            ADD CONSTRAINT ck_UserBranchRoles_BranchScope
                CHECK ((ScopeType = 'AllCompanyBranches' AND BranchID IS NULL)
                    OR (ScopeType IN ('OwnDriverDataOnly', 'SpecificBranch') AND BranchID IS NOT NULL))
    """))

    owner_sql = _MIGRATIONS_DIR / "sql" / "0020_company_owner_unique_trigger.sql"
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_company_owner_unique ON sec.UserBranchRoles"))
    for statement in statements_from_file(owner_sql):
        op.execute(sa.text(statement))

    permission_sql = _MIGRATIONS_DIR / "sql" / "0021_fn_user_has_permission_v3.sql"
    for statement in statements_from_file(permission_sql):
        op.execute(sa.text(statement))

    op.execute(sa.text("DROP VIEW app.vw_UserBranchAccess"))
    op.execute(sa.text("""
        CREATE VIEW app.vw_UserBranchAccess AS
        SELECT u.UserID, u.Username, u.DisplayName, c.CompanyID, c.CompanyCode,
               c.CompanyName, ubr.ScopeType, b.BranchID, b.BranchCode, b.BranchName,
               r.RoleID, COALESCE(r.RoleCode, cr.RoleCode)::varchar(80) AS RoleCode,
               COALESCE(r.RoleName, cr.RoleName)::varchar(120) AS RoleName,
               u.IsActive AS UserIsActive, u.CanLogin, c.Status AS CompanyStatus,
               c.IsSuspended AS CompanyIsSuspended, b.Status AS BranchStatus,
               ubr.IsActive AS AccessIsActive
        FROM sec.UserBranchRoles ubr
        JOIN sec.Users u ON u.UserID = ubr.UserID
        JOIN core.Companies c ON c.CompanyID = ubr.CompanyID
        LEFT JOIN core.Branches b ON b.BranchID = ubr.BranchID
        LEFT JOIN sec.Roles r ON r.RoleID = ubr.RoleID
        LEFT JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = ubr.CompanyRoleID
    """))
