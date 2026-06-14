"""0013 – Partial unique index: one Pending PeriodApproval review item per period.

Revision ID: 0013
Revises:     0012
"""
from pathlib import Path
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_SQL_FILE = (
    Path(__file__).parent.parent / "sql" / "0013_period_approval_index.sql"
)


def upgrade() -> None:
    op.execute(_SQL_FILE.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS review.ux_ReviewItems_OnePendingPeriodApproval;"
    )
