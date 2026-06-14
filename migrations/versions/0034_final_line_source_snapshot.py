"""0034: Final ledger source snapshot.

Adds five nullable columns to payroll.PayrollFinalLines so that every locked
line can explain how its FinalAmount was produced:

  PayItemID          -- FK to payroll.PayItems (stable pay-item identity)
  RateTypeID         -- FK to payroll.RateTypes (HOURLY, MILEAGE, etc.)
  DriverRateID       -- FK to payroll.DriverRates (exact row used at finalization)
  ResolvedRateAmount -- per-unit rate locked at finalization time
  RateBehavior       -- calculation method ('PerUnit', 'EnteredAmount', 'System', etc.)

All columns are nullable so existing rows are unaffected.
PayItemID is backfilled for existing rows where the LineType unambiguously
matches a non-Retired PayItem code.

Revision ID: 0034
Revises: 0033
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0034"
down_revision: str = "0033"
branch_labels = None
depends_on = None


# --------------------------------------------------------------------------- #
# Statement 1: Add the five new nullable columns.                              #
# asyncpg requires one op.execute() per statement.                            #
# --------------------------------------------------------------------------- #
_ADD_COLUMNS_SQL = """
ALTER TABLE payroll.PayrollFinalLines
    ADD COLUMN IF NOT EXISTS PayItemID          INTEGER      REFERENCES payroll.PayItems(PayItemID),
    ADD COLUMN IF NOT EXISTS RateTypeID         INTEGER      REFERENCES payroll.RateTypes(RateTypeID),
    ADD COLUMN IF NOT EXISTS DriverRateID       BIGINT       REFERENCES payroll.DriverRates(DriverRateID),
    ADD COLUMN IF NOT EXISTS ResolvedRateAmount NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS RateBehavior       VARCHAR(30)
"""

# --------------------------------------------------------------------------- #
# Statement 2: Backfill PayItemID for existing rows.                          #
# Prefers company-specific items over system items (NULLS LAST on companyid). #
# Only fills rows where PayItemID is currently NULL and the LineType matches  #
# a non-Retired PayItem.                                                      #
# --------------------------------------------------------------------------- #
_BACKFILL_PAYITEMID_SQL = """
UPDATE payroll.PayrollFinalLines fl
SET    payitemid = sub.payitemid
FROM (
    SELECT DISTINCT ON (fl2.finallineid)
           fl2.finallineid,
           pi.payitemid
    FROM   payroll.PayrollFinalLines fl2
    JOIN   payroll.PayItems          pi  ON pi.payitemcode = fl2.linetype
                                        AND (pi.companyid IS NULL OR pi.companyid = fl2.companyid)
                                        AND pi.status != 'Retired'
    WHERE  fl2.payitemid IS NULL
    ORDER  BY fl2.finallineid, pi.companyid NULLS LAST
) sub
WHERE fl.finallineid = sub.finallineid
"""


def upgrade() -> None:
    op.execute(sa.text(_ADD_COLUMNS_SQL))
    op.execute(sa.text(_BACKFILL_PAYITEMID_SQL))


def downgrade() -> None:
    op.execute(sa.text("""
        ALTER TABLE payroll.PayrollFinalLines
            DROP COLUMN IF EXISTS RateBehavior,
            DROP COLUMN IF EXISTS ResolvedRateAmount,
            DROP COLUMN IF EXISTS DriverRateID,
            DROP COLUMN IF EXISTS RateTypeID,
            DROP COLUMN IF EXISTS PayItemID
    """))
