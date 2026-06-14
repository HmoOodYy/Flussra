"""0029: Add CustomIntervalDays to BranchPayrollSettings.

Adds the CustomIntervalDays INTEGER NULL column required for fixed-cadence
Custom payroll setup.  When payrollfrequency = 'Custom', this column stores
the inclusive period length in days (e.g. 10 for a 10-day cycle).

Non-Custom frequencies leave it NULL.

Revision ID: 0029
Revises: 0028
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0029"
down_revision: str = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE payroll.BranchPayrollSettings "
        "ADD COLUMN IF NOT EXISTS CustomIntervalDays INTEGER NULL"
    ))

    # Constraint: when set, must be a positive integer (≥ 1).
    op.execute(sa.text(
        "ALTER TABLE payroll.BranchPayrollSettings "
        "ADD CONSTRAINT ck_BranchPayrollSettings_CustomIntervalDays_positive "
        "CHECK (CustomIntervalDays IS NULL OR CustomIntervalDays > 0)"
    ))


def downgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE payroll.BranchPayrollSettings "
        "DROP CONSTRAINT IF EXISTS ck_BranchPayrollSettings_CustomIntervalDays_positive"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.BranchPayrollSettings "
        "DROP COLUMN IF EXISTS CustomIntervalDays"
    ))
