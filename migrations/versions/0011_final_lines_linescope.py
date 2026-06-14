"""M14 fix: LineScope column on PayrollFinalLines.

Migration 0010 added LineScope to PayrollDraftLines so that Period Pay lines
(LineScope='Period') are distinguished from daily lines (LineScope='Daily').
The finalization INSERT must copy LineScope into PayrollFinalLines so the
immutable ledger preserves the Daily/Period discriminator.

Adds:
  - PayrollFinalLines.LineScope  VARCHAR(10) NOT NULL DEFAULT 'Daily'
  - ck_PayrollFinalLines_LineScope CHECK
  - ix_PayrollFinalLines_Period_PeriodPay  (partial index WHERE LineScope='Period')

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-30
"""
import re
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0011_final_lines_linescope.sql"


def _statements(path: Path) -> list[str]:
    sql = path.read_text(encoding="utf-8")
    clean = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in clean.split(";") if s.strip()]


def upgrade() -> None:
    for stmt in _statements(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ix_payrollfinallines_period_periodpay"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.payrollfinallines "
        "DROP CONSTRAINT IF EXISTS ck_payrollfinallines_linescope"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.payrollfinallines DROP COLUMN IF EXISTS linescope"
    ))
