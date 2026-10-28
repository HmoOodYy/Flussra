"""0097: Ordered Company-scoped Driver identity lookup."""
import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0097"
down_revision: str | None = "0096"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATIONS_DIR = Path(__file__).parent.parent
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file  # noqa: E402 - migration-local SQL loader path


def upgrade() -> None:
    for statement in statements_from_file(_MIGRATIONS_DIR / "sql" / "0097_import_driver_identity_lookup.sql"):
        op.execute(sa.text(statement))


def downgrade() -> None:
    raise RuntimeError("Import Driver identity lookup migration is irreversible.")
