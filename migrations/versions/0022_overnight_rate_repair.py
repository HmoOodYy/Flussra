"""0022: Repair OVERNIGHT pay item — change RateBehavior to PerUnit and link rate type.

OVERNIGHT was seeded with RateBehavior='Fixed' and no PayItemRateTypeMap entry,
which means it cannot be priced via DriverRates and does not appear in the rate matrix.
This migration:
  - Changes OVERNIGHT.RateBehavior from 'Fixed' to 'PerUnit'
  - Ensures OVERNIGHT.RequiresRate = TRUE
  - Inserts a PayItemRateTypeMap row linking OVERNIGHT pay item to OVERNIGHT rate type

Revision ID: 0022
Revises: 0021
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0022"
down_revision: str = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        UPDATE payroll.PayItems
        SET RateBehavior = 'PerUnit', RequiresRate = TRUE
        WHERE PayItemCode = 'OVERNIGHT'
          AND RateBehavior = 'Fixed'
    """))

    op.execute(sa.text("""
        INSERT INTO payroll.PayItemRateTypeMap (PayItemID, RateTypeID, IsPrimary, Status)
        SELECT pi.PayItemID, rt.RateTypeID, TRUE, 'Active'
        FROM payroll.PayItems pi
        CROSS JOIN payroll.RateTypes rt
        WHERE pi.PayItemCode = 'OVERNIGHT'
          AND rt.RateCode = 'OVERNIGHT'
          AND NOT EXISTS (
            SELECT 1 FROM payroll.PayItemRateTypeMap existing
            WHERE existing.PayItemID = pi.PayItemID
              AND existing.RateTypeID = rt.RateTypeID
          )
    """))


def downgrade() -> None:
    # Remove the PayItemRateTypeMap entry for OVERNIGHT
    op.execute(sa.text("""
        DELETE FROM payroll.PayItemRateTypeMap
        WHERE PayItemID = (
            SELECT PayItemID FROM payroll.PayItems WHERE PayItemCode = 'OVERNIGHT'
        )
          AND RateTypeID = (
            SELECT RateTypeID FROM payroll.RateTypes WHERE RateCode = 'OVERNIGHT'
        )
    """))

    # Revert RateBehavior to Fixed
    op.execute(sa.text("""
        UPDATE payroll.PayItems
        SET RateBehavior = 'Fixed', RequiresRate = TRUE
        WHERE PayItemCode = 'OVERNIGHT'
    """))
