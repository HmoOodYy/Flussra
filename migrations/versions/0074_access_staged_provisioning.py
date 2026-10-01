"""0074: explicit staged Access accounts and Employee links.

Revision ID: 0074
Revises: 0073
"""
import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0074"
down_revision: str | None = "0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0074_access_staged_provisioning.sql"
_MIGRATIONS_DIR = Path(__file__).parent.parent
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX sec.ux_Users_EmployeeID_NotNull"))
    op.execute(sa.text("ALTER TABLE sec.Users DROP CONSTRAINT fk_Users_Employee_Company"))
    op.execute(sa.text("ALTER TABLE sec.Users DROP CONSTRAINT ck_Users_EmployeeNeedsCompany"))
    op.execute(sa.text("ALTER TABLE sec.Users DROP CONSTRAINT ck_Users_StagedCannotLogin"))
    op.execute(sa.text("ALTER TABLE sec.Users DROP COLUMN IsStaged"))
