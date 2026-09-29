"""0073: establish Workforce Employee authority.

Revision ID: 0073
Revises: 0072
"""
import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0073_workforce_employee_authority.sql"
_MIGRATIONS_DIR = Path(__file__).parent.parent
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    bind = op.get_bind()
    null_count = bind.execute(sa.text(
        "SELECT COUNT(*) FROM core.Employees WHERE EmployeeType IS NULL"
    )).scalar_one()
    if null_count:
        raise RuntimeError(
            "Downgrade of 0073 refused: "
            f"{null_count} core.Employees rows have NULL EmployeeType. "
            "Restore EmployeeType values through an explicit product decision; "
            "this migration will not fabricate classifications."
        )

    bind.execute(sa.text("DELETE FROM sec.CompanyRolePermissions WHERE PermissionCode IN ('employees.view', 'employees.manage')"))
    bind.execute(sa.text("DELETE FROM sec.UserPermissionOverrides WHERE PermissionCode IN ('employees.view', 'employees.manage')"))
    bind.execute(sa.text("""
        DELETE FROM sec.RolePermissions rp
        USING sec.Permissions p
        WHERE rp.PermissionID = p.PermissionID
          AND p.PermissionCode IN ('employees.view', 'employees.manage')
    """))
    bind.execute(sa.text("DELETE FROM sec.Permissions WHERE PermissionCode IN ('employees.view', 'employees.manage')"))
    op.execute(sa.text("ALTER TABLE core.Employees ALTER COLUMN EmployeeType SET NOT NULL"))
