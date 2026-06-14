"""Standard pay items seed — 11 system-standard pay items.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-29
"""
import re
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0003_pay_items_seed.sql"


def _statements(path: Path) -> list[str]:
    """
    Read a SQL file, strip single-line (--) comments, split on semicolons,
    and return non-empty statement strings ready for execution.

    Stripping comments *before* splitting prevents chunks that open with a
    comment block from being silently discarded by a naive startswith("--")
    guard — which would skip the actual SQL that follows.
    """
    sql = path.read_text(encoding="utf-8")
    clean = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in clean.split(";") if s.strip()]


def upgrade() -> None:
    for stmt in _statements(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM payroll.PayItems "
            "WHERE IsSystemStandard = TRUE AND CompanyID IS NULL"
        )
    )
