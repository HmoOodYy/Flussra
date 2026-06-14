"""Partial unique index on CustomPayItemRequests (CompanyID, PayItemCode)
WHERE active status — prevents duplicate active requests for the same code.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-29
"""
import re
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0006_request_active_code_index.sql"


def _statements(path: Path) -> list[str]:
    sql = path.read_text(encoding="utf-8")
    clean = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in clean.split(";") if s.strip()]


def upgrade() -> None:
    for stmt in _statements(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ux_CustomPayItemRequests_ActiveCode"
    ))
