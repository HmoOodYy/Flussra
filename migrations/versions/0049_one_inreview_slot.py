"""0049: One-InReview-per-branch slot enforcement.

Adds the ux_payrollperiods_oneinreviewperbranch partial unique index so that
at most one payroll period may have Status='InReview' per CompanyID/BranchID.

The SQL file runs a blocking preflight (RAISE EXCEPTION) if any branch already
has duplicate InReview periods. Only this index is added; no columns, FKs, or
CHECK constraints are changed.

Downgrade drops only ux_payrollperiods_oneinreviewperbranch.

Revision ID: 0049
Revises: 0048
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0049"
down_revision: Union[str, None] = "0048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0049_one_inreview_slot.sql"

_migrations_dir = Path(__file__).parent.parent
if str(_migrations_dir) not in sys.path:
    sys.path.insert(0, str(_migrations_dir))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for stmt in statements_from_file(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ux_payrollperiods_oneinreviewperbranch"
    ))
