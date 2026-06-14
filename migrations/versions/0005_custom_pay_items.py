"""Custom Pay Items catalog — DisplayLabel, RequestingBranchID, CHECK constraints,
and payroll.CustomPayItemRequests table.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-29
"""
import re
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0005_custom_pay_items.sql"


def _statements(path: Path) -> list[str]:
    sql = path.read_text(encoding="utf-8")
    clean = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in clean.split(";") if s.strip()]


def upgrade() -> None:
    for stmt in _statements(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS payroll.CustomPayItemRequests"))
    op.execute(sa.text("DROP INDEX IF EXISTS ux_PayItems_Company_PayItemCode"))
    op.execute(sa.text("ALTER TABLE payroll.PayItems DROP CONSTRAINT IF EXISTS ck_PayItems_ItemScope"))
    op.execute(sa.text("ALTER TABLE payroll.PayItems DROP CONSTRAINT IF EXISTS ck_PayItems_RateBehavior"))
    op.execute(sa.text("ALTER TABLE payroll.PayItems DROP CONSTRAINT IF EXISTS ck_PayItems_Status"))
    op.execute(sa.text("ALTER TABLE payroll.PayItems DROP COLUMN IF EXISTS RequestingBranchID"))
    op.execute(sa.text("ALTER TABLE payroll.PayItems DROP COLUMN IF EXISTS Notes"))
    op.execute(sa.text("ALTER TABLE payroll.PayItems DROP COLUMN IF EXISTS DisplayLabel"))
