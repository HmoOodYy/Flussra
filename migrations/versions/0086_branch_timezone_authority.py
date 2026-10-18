"""0086: Branch timezone authority and instant Driver assignment windows."""

import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0086"
down_revision: str | None = "0085"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATIONS_DIR = Path(__file__).parent.parent
_SQL_FILE = _MIGRATIONS_DIR / "sql" / "0086_branch_timezone_authority.sql"
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    raise RuntimeError(
        "Migration 0086 is irreversible. It replaces Driver DATE assignment history "
        "with instants and removes the Company timezone authority. Restore a pre-upgrade "
        "database backup if a downgrade is genuinely required."
    )
