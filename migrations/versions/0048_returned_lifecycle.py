"""0048: Returned-for-Correction lifecycle.

Adds the Returned period status, CurrentReturnReviewItemID pointer column,
and the one-Returned-per-branch partial unique index.

Schema changes:
  - payroll.PayrollPeriods.CurrentReturnReviewItemID BIGINT NULL
  - Unique index on review.ManagerReviewItems(ReviewItemID, CompanyID, BranchID)
  - Composite FK: PayrollPeriods(CurrentReturnReviewItemID, CompanyID, BranchID)
    -> ManagerReviewItems(ReviewItemID, CompanyID, BranchID)
  - ck_PayrollPeriods_Status extended to include 'Returned'
  - ck_PayrollPeriods_ReturnedPointerConsistency CHECK
  - ux_PayrollPeriods_OneReturnedPerBranch partial unique index

Downgrade refuses if any period has Status='Returned' or a non-NULL
CurrentReturnReviewItemID -- never rewrites or deletes business data.

Revision ID: 0048
Revises: 0047
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0048"
down_revision: Union[str, None] = "0047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0048_returned_lifecycle.sql"

_migrations_dir = Path(__file__).parent.parent
if str(_migrations_dir) not in sys.path:
    sys.path.insert(0, str(_migrations_dir))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for stmt in statements_from_file(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(sa.text(
        "SELECT COUNT(*) FROM payroll.payrollperiods "
        "WHERE status = 'Returned' OR currentreturnreviewitemid IS NOT NULL"
    )).scalar()
    if row and row > 0:
        raise Exception(
            f"Cannot downgrade migration 0048: {row} period(s) have Status='Returned' "
            "or a non-NULL CurrentReturnReviewItemID. Resolve all Returned periods "
            "before downgrading. Never rewrite or delete business data during downgrade."
        )

    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ux_PayrollPeriods_OneReturnedPerBranch"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollPeriods "
        "DROP CONSTRAINT IF EXISTS ck_PayrollPeriods_ReturnedPointerConsistency"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollPeriods "
        "DROP CONSTRAINT IF EXISTS ck_PayrollPeriods_Status"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollPeriods "
        "ADD CONSTRAINT ck_PayrollPeriods_Status "
        "CHECK (Status IN ('Draft','Open','InReview','Approved','Locked','Cancelled','Archived'))"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollPeriods "
        "DROP CONSTRAINT IF EXISTS fk_PayrollPeriods_CurrentReturnReviewItem"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS review.ux_ManagerReviewItems_ReviewItemID_Company_Branch"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollPeriods "
        "DROP COLUMN IF EXISTS CurrentReturnReviewItemID"
    ))
