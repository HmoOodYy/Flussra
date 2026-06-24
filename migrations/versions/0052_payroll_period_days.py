"""0052: Payroll period-day calendar snapshots.

Introduces payroll.PayrollPeriodDays: one immutable row per calendar day in a
payroll period, created at period-creation time so that later setup changes
do not retroactively alter which days were scheduled for existing periods.

Schema changes:
  - payroll.PayrollPeriodDays (new table with FKs, checks, indexes)

No historical backfill: legacy periods (created before 0052) have no day rows.
The service falls back to StartDate/EndDate bounds for those periods.

Downgrade refuses if any rows exist in PayrollPeriodDays.

Revision ID: 0052
Revises: 0051
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0052"
down_revision: Union[str, None] = "0051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0052_payroll_period_days.sql"

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
        "SELECT COUNT(*) FROM payroll.PayrollPeriodDays"
    )).scalar()
    if row_count and row_count > 0:
        raise RuntimeError(
            f"Migration 0052 downgrade refused: {row_count} PayrollPeriodDays row(s) exist. "
            "These are period calendar snapshots created after CP-2B was deployed. "
            "Remove them (or archive the owning periods) before downgrading."
        )

    # Zero rows — safe to drop.
    op.execute(sa.text("DROP INDEX IF EXISTS payroll.ix_PayrollPeriodDays_Branch_Date"))
    op.execute(sa.text("DROP INDEX IF EXISTS payroll.ix_PayrollPeriodDays_Period"))
    op.execute(sa.text("DROP TABLE IF EXISTS payroll.PayrollPeriodDays"))
