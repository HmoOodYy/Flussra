"""0014 - Company-scoped role system: sec.CompanyRoles + sec.CompanyRolePermissions.

Adds:
  sec.CompanyRoles          -- per-company role catalog (default + custom)
  sec.CompanyRolePermissions -- permission codes granted to each company role

Design note: CompanyRolePermissions stores PermissionCode directly (VARCHAR)
rather than a FK to sec.Permissions.  The code string is the canonical key;
storing it directly avoids the join in the permission check function and keeps
the table self-contained.

Revision ID: 0014
Revises:     0013
"""
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0014_company_roles.sql"


def _statements(path: Path) -> list[str]:
    """Strip single-line comments and split on semicolons."""
    import re
    sql = path.read_text(encoding="utf-8")
    clean = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in clean.split(";") if s.strip()]


def upgrade() -> None:
    # 0014_company_roles.sql has only CREATE TABLE / CREATE INDEX statements,
    # no internal semicolons in $$ blocks, so the simple split helper is safe.
    for stmt in _statements(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS sec.CompanyRolePermissions CASCADE"))
    op.execute(sa.text("DROP TABLE IF EXISTS sec.CompanyRoles CASCADE"))
