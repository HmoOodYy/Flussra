"""0033: Partial unique index — no duplicate active Daily draft lines.

Adds a partial unique index on PayrollDraftLines to enforce the business key
  (CompanyID, PayrollPeriodID, DriverID, WorkDate, LineType)
only for rows WHERE linescope = 'Daily' AND status != 'Void'.

Voided lines are excluded so void_draft_line + re-add remains safe.
Period Pay lines (LineScope='Period') are excluded — different key, different
endpoint (M14).

Pre-check: the migration raises an error if any existing duplicate active Daily
lines are detected, so the database is clean before the index is created.

Revision ID: 0033
Revises: 0032
"""
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision: str = "0033"
down_revision: str = "0032"
branch_labels = None
depends_on = None


# asyncpg cannot execute multiple statements in a single op.execute() call.
# Split the pre-check DO block and the CREATE INDEX into two separate calls.

_PRECHECK_SQL = """
DO $$
DECLARE
    v_dup_count INTEGER;
    v_examples  TEXT;
BEGIN
    SELECT COUNT(*)
    INTO   v_dup_count
    FROM (
        SELECT companyid, payrollperiodid, driverid, workdate, linetype
        FROM   payroll.payrolldraftlines
        WHERE  linescope = 'Daily'
          AND  status   != 'Void'
        GROUP BY companyid, payrollperiodid, driverid, workdate, linetype
        HAVING COUNT(*) > 1
    ) dupes;

    IF v_dup_count > 0 THEN
        SELECT string_agg(
                   format('company=%s period=%s driver=%s date=%s type=%s x%s',
                          companyid, payrollperiodid, driverid, workdate, linetype, cnt),
                   '; '
               )
        INTO   v_examples
        FROM (
            SELECT companyid, payrollperiodid, driverid, workdate, linetype,
                   COUNT(*) AS cnt
            FROM   payroll.payrolldraftlines
            WHERE  linescope = 'Daily'
              AND  status   != 'Void'
            GROUP BY companyid, payrollperiodid, driverid, workdate, linetype
            HAVING COUNT(*) > 1
            LIMIT 5
        ) sub;

        RAISE EXCEPTION
            'Migration 0033 blocked: % duplicate active Daily draft-line group(s) found. '
            'Void the extra lines before running this migration. Examples: %',
            v_dup_count, v_examples;
    END IF;
END $$;
"""

_CREATE_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS
    uix_payrolldraftlines_daily_active_business_key
ON payroll.payrolldraftlines
    (companyid, payrollperiodid, driverid, workdate, linetype)
WHERE linescope = 'Daily'
  AND status   != 'Void';
"""


def upgrade() -> None:
    op.execute(sa.text(_PRECHECK_SQL))
    op.execute(sa.text(_CREATE_INDEX_SQL))


def downgrade() -> None:
    op.execute(
        sa.text("""
            DROP INDEX IF EXISTS
                payroll.uix_payrolldraftlines_daily_active_business_key;
        """)
    )
