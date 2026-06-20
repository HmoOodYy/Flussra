"""0050: Period creation candidate key column and partial unique index.

Adds CreationCandidateKeyHash (VARCHAR 64, nullable) to payroll.PayrollPeriods
and creates a partial unique index so the same candidate key can only produce
one period per company/branch.

Downgrade refuses if any hashes are present; otherwise drops index then column.

Revision ID: 0050
Revises: 0049
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0050"
down_revision: Union[str, None] = "0049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0050_period_creation_candidate_key.sql"

_migrations_dir = Path(__file__).parent.parent
if str(_migrations_dir) not in sys.path:
    sys.path.insert(0, str(_migrations_dir))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for stmt in statements_from_file(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    conn = op.get_bind()
    count = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM payroll.PayrollPeriods "
            "WHERE CreationCandidateKeyHash IS NOT NULL"
        )
    ).scalar()
    if count:
        raise RuntimeError(
            f"Migration 0050 downgrade refused: {count} period(s) have a "
            "CreationCandidateKeyHash set. Clear or archive them before downgrading."
        )
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ux_payrollperiods_creationcandidatekey"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollPeriods "
        "DROP COLUMN IF EXISTS CreationCandidateKeyHash"
    ))
