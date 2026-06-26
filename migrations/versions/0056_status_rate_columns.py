"""0056: Status Rate Columns for CP-2D2 status payment.

Introduces payroll.StatusRateColumns (per-branch rate-column configs backed
by RateTypes) and wires StatusKey.StatusRateColumnID so a status key can
specify which driver rate column is used to compute the paid-hours amount.

Also adds:
  - NormalizedColumnName + data-integrity triggers on StatusRateColumns
  - Trigger to validate StatusKey.StatusRateColumnID branch/company match
  - SourceSnapshot JSONB to PayrollDraftLines
  - Partial unique index preventing duplicate active STATUS_PAYMENT lines

Revision ID: 0056
Revises: 0055
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0056"
down_revision: Union[str, None] = "0055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0056_status_rate_columns.sql"

_migrations_dir = Path(__file__).parent.parent
if str(_migrations_dir) not in sys.path:
    sys.path.insert(0, str(_migrations_dir))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for stmt in statements_from_file(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    # Drop objects in reverse dependency order.
    # Triggers must be dropped before the tables/functions they reference.
    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_psk_src_branch "
        "ON payroll.PayrollStatusKeys"
    ))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS payroll.trg_fn_psk_src_branch()"
    ))
    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_src_ratetype_owner "
        "ON payroll.StatusRateColumns"
    ))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS payroll.trg_fn_src_ratetype_owner()"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ux_DraftLines_StatusPayment_Slot"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollDraftLines DROP COLUMN IF EXISTS SourceSnapshot"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ix_PSK_StatusRateColumn"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollStatusKeys "
        "DROP CONSTRAINT IF EXISTS fk_PSK_StatusRateColumn, "
        "DROP COLUMN IF EXISTS StatusRateColumnID"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ux_SRC_RateType_Branch"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ux_SRC_NormalizedName"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ux_StatusRateColumns_BranchDefault"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ix_StatusRateColumns_Branch"
    ))
    op.execute(sa.text("DROP TABLE IF EXISTS payroll.StatusRateColumns"))
    # Preserve STATUS_PAY RateType if business data depends on it;
    # only remove if no DriverRates reference it.
    op.execute(sa.text("""
        DELETE FROM payroll.RateTypes
        WHERE  RateCode = 'STATUS_PAY'
          AND  NOT EXISTS (
              SELECT 1 FROM payroll.DriverRates dr
              JOIN payroll.RateTypes rt ON rt.RateTypeID = dr.RateTypeID
              WHERE rt.RateCode = 'STATUS_PAY'
          )
    """))
