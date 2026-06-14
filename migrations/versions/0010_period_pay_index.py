"""M14: Period Pay — LineScope column and partial index on PayrollDraftLines.

Adds:
  - PayrollDraftLines.LineScope  VARCHAR(10) NOT NULL DEFAULT 'Daily'
    CHECK (LineScope IN ('Daily', 'Period'))
    The authoritative discriminator for Period Pay lines.  WorkDate IS NULL is
    NOT used as the discriminator because daily lines can also omit WorkDate.

  - ix_PayrollDraftLines_Period_PeriodPay  (partial index WHERE LineScope='Period')

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-30
"""
import re
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0010_period_pay_index.sql"


def _statements(path: Path) -> list[str]:
    sql = path.read_text(encoding="utf-8")
    clean = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in clean.split(";") if s.strip()]


def upgrade() -> None:
    for stmt in _statements(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ix_payrolldraftlines_period_periodpay"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.payrolldraftlines "
        "DROP CONSTRAINT IF EXISTS ck_payrolldraftlines_linescope"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.payrolldraftlines DROP COLUMN IF EXISTS linescope"
    ))
