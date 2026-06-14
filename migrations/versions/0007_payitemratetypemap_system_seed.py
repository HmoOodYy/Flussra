"""Seed PayItemRateTypeMap for the 6 system-standard PerUnit pay items.

After this migration the calculation engine resolves rate_code via the DB
rather than the transitional hardcoded _SYSTEM_LINE_TYPE_INFO dictionary.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-29
"""
import re
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0007_payitemratetypemap_system_seed.sql"


def _statements(path: Path) -> list[str]:
    sql = path.read_text(encoding="utf-8")
    clean = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in clean.split(";") if s.strip()]


def upgrade() -> None:
    for stmt in _statements(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    # Remove map rows for the 6 seeded system items.
    op.execute(sa.text("""
        DELETE FROM payroll.payitemratetypemap
        WHERE payitemid IN (
            SELECT payitemid FROM payroll.payitems
            WHERE payitemcode IN ('HOURS', 'MILES', 'LOADS', 'WAIT_TIME', 'PALLETS', 'SILOS')
              AND companyid IS NULL
        )
    """))
