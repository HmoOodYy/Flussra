"""0089: Immutable payroll calculation evidence (calculation contract v2)."""

import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0089"
down_revision: str | None = "0088"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATIONS_DIR = Path(__file__).parent.parent
_SQL_FILE = _MIGRATIONS_DIR / "sql" / "0089_immutable_calculation_evidence.sql"
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    raise RuntimeError(
        "Migration 0089 is irreversible. It reshapes calculation snapshots, their "
        "components and Min/Max evidence, and FinalLines into the v2 evidence contract, "
        "which cannot be converted back. Restore a pre-upgrade database backup if a "
        "downgrade is genuinely required."
    )
