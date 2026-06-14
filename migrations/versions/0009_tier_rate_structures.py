"""M13c: Advanced rate structures — DriverRateTiers, Block columns, RateBehavior CHECK.

Adds:
  - payroll.DriverRateTiers table (OrdinalTier / RangeBracket / RangeProgressive tiers)
  - payroll.DriverRates.BlockSize + RoundingRule columns (Block behavior)
  - payroll.PayItems CHECK constraint on RateBehavior (enumerates all valid values)

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-30
"""
import re
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0009_tier_rate_structures.sql"


def _statements(path: Path) -> list[str]:
    sql = path.read_text(encoding="utf-8")
    clean = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in clean.split(";") if s.strip()]


def upgrade() -> None:
    for stmt in _statements(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    # Restore the M12 RateBehavior CHECK (drop M13c expanded version, re-add M12 version).
    op.execute(sa.text(
        "ALTER TABLE payroll.payitems DROP CONSTRAINT IF EXISTS ck_payitems_ratebehavior"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.payitems ADD CONSTRAINT ck_payitems_ratebehavior "
        "CHECK (ratebehavior IN ('PerUnit','Fixed','Calculated','None','EnteredAmount'))"
    ))
    # Remove Block metadata constraints + columns
    op.execute(sa.text(
        "ALTER TABLE payroll.driverrates "
        "DROP CONSTRAINT IF EXISTS ck_driverrates_blockconsistency, "
        "DROP CONSTRAINT IF EXISTS ck_driverrates_roundingrule, "
        "DROP CONSTRAINT IF EXISTS ck_driverrates_blocksize"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.driverrates "
        "DROP COLUMN IF EXISTS roundingrule, "
        "DROP COLUMN IF EXISTS blocksize"
    ))
    # Remove DriverRateTiers table (CASCADE removes index + constraints)
    op.execute(sa.text("DROP TABLE IF EXISTS payroll.driverratetiers CASCADE"))
