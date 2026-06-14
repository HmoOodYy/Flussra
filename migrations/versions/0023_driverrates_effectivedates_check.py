"""0023: Add CHECK constraint EffectiveTo >= EffectiveFrom on payroll.DriverRates.

The existing EXCLUDE USING gist constraint already prevents overlapping
Approved/Superseded date ranges.  This CHECK adds a belt-and-suspenders guard
ensuring EffectiveTo is never set to a date earlier than EffectiveFrom on any
status (including PendingApproval and Voided rows).

Revision ID: 0023
Revises: 0022
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0023"
down_revision: str = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        ALTER TABLE payroll.DriverRates
            ADD CONSTRAINT ck_DriverRates_EffectiveDates
            CHECK (EffectiveTo IS NULL OR EffectiveTo >= EffectiveFrom)
    """))


def downgrade() -> None:
    op.execute(sa.text("""
        ALTER TABLE payroll.DriverRates
            DROP CONSTRAINT ck_DriverRates_EffectiveDates
    """))
