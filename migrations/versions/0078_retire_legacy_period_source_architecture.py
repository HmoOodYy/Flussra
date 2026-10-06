"""0078: Retire the legacy Period source and false PayItem architecture (G0.4B)."""

import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0078"
down_revision: str | None = "0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATIONS_DIR = Path(__file__).parent.parent
_SQL_FILE = _MIGRATIONS_DIR / "sql" / "0078_retire_legacy_period_source_architecture.sql"
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    raise RuntimeError(
        "Migration 0078 (legacy Period source and false PayItem architecture "
        "retirement) is irreversible. It physically deletes legacy Period "
        "DraftLines, the BONUS / ADJUSTMENT / GUARANTEED_MINIMUM and custom "
        "Period PayItems with their configuration and frozen-period rows, "
        "drops PayrollDraftLines.LineScope and PayrollBonusEvents."
        "SourceDraftLineID, and narrows the PayItem ItemScope and RateBehavior "
        "invariants. None of that data can be reconstructed. Restore from a "
        "pre-upgrade database backup if a downgrade is genuinely required."
    )
