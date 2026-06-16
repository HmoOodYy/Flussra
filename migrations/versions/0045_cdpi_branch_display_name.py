"""0045: CDPI branch display-name override.

Adds BranchDisplayName VARCHAR(200) NULL to payroll.BranchPayItemConfig.

Branch-level CDPI controls (Task 7) allow a branch to set a display-label
override for an approved/direct-created CDPI PayItem without touching the
company-level PayItems.Name.  Storing the override in BranchPayItemConfig
keeps it versioned with the existing effective-dated config rows.

Revision ID: 0045
Revises: 0044
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0045"
down_revision: Union[str, None] = "0044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0045_cdpi_branch_display_name.sql"

_migrations_dir = Path(__file__).parent.parent
if str(_migrations_dir) not in sys.path:
    sys.path.insert(0, str(_migrations_dir))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for stmt in statements_from_file(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE payroll.BranchPayItemConfig "
        "DROP COLUMN IF EXISTS BranchDisplayName"
    ))
