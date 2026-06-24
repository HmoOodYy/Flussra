"""0053: Payroll period pay-item layout snapshots.

Introduces payroll.PayrollPeriodPayItems: one immutable row per PayItem per
payroll period, created at period-creation time so that later Pay Items or
Branch Pay Item Config changes do not retroactively alter the columns visible
on already-created periods.

Schema changes:
  - payroll.PayrollPeriodPayItems (new table with FKs, checks, indexes)

No historical backfill: legacy periods (created before 0053) have no snapshot
rows. The service falls back to live BranchPayItemConfig queries for those.

Downgrade refuses if any rows exist in PayrollPeriodPayItems.

Revision ID: 0053
Revises: 0052
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0053"
down_revision: Union[str, None] = "0052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0053_payroll_period_pay_items.sql"

_migrations_dir = Path(__file__).parent.parent
if str(_migrations_dir) not in sys.path:
    sys.path.insert(0, str(_migrations_dir))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for stmt in statements_from_file(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    conn = op.get_bind()

    row_count = conn.execute(sa.text(
        "SELECT COUNT(*) FROM payroll.PayrollPeriodPayItems"
    )).scalar()
    if row_count and row_count > 0:
        raise RuntimeError(
            f"Migration 0053 downgrade refused: {row_count} PayrollPeriodPayItems row(s) exist. "
            "These are period pay-item snapshots created after CP-2C was deployed. "
            "Remove them (or archive the owning periods) before downgrading."
        )

    # Zero rows — safe to drop.
    op.execute(sa.text("DROP INDEX IF EXISTS payroll.ix_PayrollPeriodPayItems_PayItem"))
    op.execute(sa.text("DROP INDEX IF EXISTS payroll.ix_PayrollPeriodPayItems_Branch"))
    op.execute(sa.text("DROP INDEX IF EXISTS payroll.ix_PayrollPeriodPayItems_Period"))
    op.execute(sa.text("DROP TABLE IF EXISTS payroll.PayrollPeriodPayItems"))
