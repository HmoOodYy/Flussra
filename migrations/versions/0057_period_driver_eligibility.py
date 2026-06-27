"""0057: Period Driver Eligibility Snapshot for CP-2E.

Introduces payroll.PayrollPeriodDriverEligibility: a period-level snapshot of
which drivers are eligible for each payroll period.  Eliminates repeated
live queries against mutable EmploymentStatus/DriverStatus after the period
is frozen.

Also adds:
  - Ownership enforcement trigger (trg_ppde_ownership)
  - Hot-path indexes on PayrollDraftLines and PayrollPeriodDriverDayEntryState
  - Backfill for all non-finalized periods (Draft/Open/Returned/InReview/Approved)

Revision ID: 0057
Revises: 0056
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0057"
down_revision: Union[str, None] = "0056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0057_period_driver_eligibility.sql"

_migrations_dir = Path(__file__).parent.parent
if str(_migrations_dir) not in sys.path:
    sys.path.insert(0, str(_migrations_dir))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for stmt in statements_from_file(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    # Drop marker table first (no data dependency on detail table)
    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_ppes_ownership "
        "ON payroll.PayrollPeriodEligibilitySnapshots"
    ))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS payroll.trg_fn_ppes_ownership() CASCADE"
    ))
    op.execute(sa.text(
        "DROP TABLE IF EXISTS payroll.PayrollPeriodEligibilitySnapshots"
    ))
    # Drop detail table
    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_ppde_ownership "
        "ON payroll.PayrollPeriodDriverEligibility"
    ))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS payroll.trg_fn_ppde_ownership() CASCADE"
    ))
    op.execute(sa.text(
        "DROP TABLE IF EXISTS payroll.PayrollPeriodDriverEligibility"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ix_DraftLines_Period_Driver_WorkDate"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ix_EntryState_Period_Driver_WorkDate"
    ))
