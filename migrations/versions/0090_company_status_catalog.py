"""0090: Company Status catalog and direct Status compensation."""

import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0090"
down_revision: str | None = "0089"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATIONS_DIR = Path(__file__).parent.parent
_SQL_FILE = _MIGRATIONS_DIR / "sql" / "0090_company_status_catalog.sql"
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    raise RuntimeError(
        "Migration 0090 is irreversible. It replaces Branch-owned Status keys and Status rate "
        "columns with the Company-owned catalog, and it reshapes Driver rate schedules and "
        "calculation evidence, none of which can be converted back. Restore a pre-upgrade "
        "database backup if a downgrade is genuinely required."
    )
