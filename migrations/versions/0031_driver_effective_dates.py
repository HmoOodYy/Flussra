"""0031: Add EffectiveFrom / EffectiveTo to core.Drivers.

Adds two nullable DATE columns to core.Drivers so that a completed
driver transfer can use DriverTransferRequests.EffectiveDate to control
exactly when each profile appears in a branch day-grid.

Existing rows: both columns default to NULL, which the day-grid query
treats as "always valid" — fully backward-compatible.

On transfer completion:
  - Old source profile: EffectiveTo  = effective_date - 1 day
  - New target profile: EffectiveFrom = effective_date

Revision ID: 0031
Revises: 0030
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0031"
down_revision: str = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        ALTER TABLE core.Drivers
            ADD COLUMN IF NOT EXISTS EffectiveFrom DATE,
            ADD COLUMN IF NOT EXISTS EffectiveTo   DATE
    """))


def downgrade() -> None:
    op.execute(sa.text("""
        ALTER TABLE core.Drivers
            DROP COLUMN IF EXISTS EffectiveFrom,
            DROP COLUMN IF EXISTS EffectiveTo
    """))
