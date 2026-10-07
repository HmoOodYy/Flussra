"""0084: PayDefinition branch applicability authority (P4a)."""

import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0084"
down_revision: str | None = "0083"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATIONS_DIR = Path(__file__).parent.parent
_SQL_FILE = _MIGRATIONS_DIR / "sql" / "0084_pay_definition_branch_applicability.sql"
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    raise RuntimeError(
        "Migration 0084 is irreversible. The legacy PayItem branch configuration "
        "it removed cannot be reconstructed. Restore from a pre-upgrade database "
        "backup if a downgrade is genuinely required."
    )
