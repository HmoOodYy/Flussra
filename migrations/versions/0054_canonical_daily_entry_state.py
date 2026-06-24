"""0054: Canonical driver/day entry state.

Introduces payroll.PayrollPeriodDriverDayEntryState: one row per driver per
work-date per period.  This is the canonical source for the selected StatusKey
and note text per driver/day, replacing the DailyStatus/DailyNote DraftLines
anti-pattern over time.

Key design points:
  - StatusKey dropdown options remain LIVE from PayrollStatusKeys (not snapshotted
    at period creation).
  - Finalization snapshot fields (StatusCodeSnapshot, StatusLabelSnapshot, etc.)
    are NULL until finalize_period runs; frozen at lock time.
  - Legacy DailyStatus/DailyNote DraftLines continue to be written (dual-write).
  - Legacy periods (no entry-state rows) fall back to DraftLines reads.

Schema changes:
  - payroll.PayrollPeriodDriverDayEntryState (new table with FKs, indexes)

No historical backfill: legacy periods have no entry-state rows; the service
falls back to DraftLines reads for those periods.

Downgrade refuses if any rows exist in PayrollPeriodDriverDayEntryState.

Revision ID: 0054
Revises: 0053
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0054"
down_revision: Union[str, None] = "0053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0054_canonical_daily_entry_state.sql"

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
        "SELECT COUNT(*) FROM payroll.PayrollPeriodDriverDayEntryState"
    )).scalar()
    if row_count and row_count > 0:
        raise RuntimeError(
            f"Migration 0054 downgrade refused: {row_count} "
            "PayrollPeriodDriverDayEntryState row(s) exist. "
            "These are canonical driver/day entry-state rows created after CP-2D1 "
            "was deployed. Remove them (or archive the owning periods) before downgrading."
        )

    # Zero rows — safe to drop.
    op.execute(sa.text("DROP INDEX IF EXISTS payroll.ix_PPDES_Period_Finalized"))
    op.execute(sa.text("DROP INDEX IF EXISTS payroll.ix_PPDES_StatusKey"))
    op.execute(sa.text("DROP INDEX IF EXISTS payroll.ix_PPDES_Company_Branch"))
    op.execute(sa.text("DROP INDEX IF EXISTS payroll.ix_PPDES_Period_Driver"))
    op.execute(sa.text("DROP INDEX IF EXISTS payroll.ix_PPDES_Period_Date"))
    op.execute(sa.text("DROP TABLE IF EXISTS payroll.PayrollPeriodDriverDayEntryState"))
