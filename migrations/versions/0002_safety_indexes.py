"""Safety indexes — unique constraints for role assignments and default branch.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-29
"""
import re
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0002_safety_indexes.sql"


def _statements(path: Path) -> list[str]:
    """
    Read a SQL file, strip single-line (--) comments, split on semicolons,
    and return non-empty statement strings ready for execution.

    Stripping comments *before* splitting is critical: when a chunk produced
    by split(";") begins with a comment block, a naive startswith("--") guard
    would silently discard the entire chunk — including any real SQL that
    follows the comments.
    """
    sql = path.read_text(encoding="utf-8")
    # Remove everything from '--' to the end of its line.
    # This is safe for DDL files that contain no string literals with '--'.
    clean = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in clean.split(";") if s.strip()]


def upgrade() -> None:
    for stmt in _statements(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    for idx in (
        "ux_UserBranchRoles_Active_AllCompany",
        "ux_UserBranchRoles_Active_Branch",
        "ux_Branches_Company_Default",
    ):
        op.execute(sa.text(f"DROP INDEX IF EXISTS {idx}"))
