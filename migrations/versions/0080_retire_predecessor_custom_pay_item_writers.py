"""0080: Retire predecessor Custom Pay Item writers (G0.5)."""

import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0080"
down_revision: str | None = "0079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATIONS_DIR = Path(__file__).parent.parent
_SQL_FILE = _MIGRATIONS_DIR / "sql" / "0080_retire_predecessor_custom_pay_item_writers.sql"
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    raise RuntimeError(
        "Migration 0080 (predecessor custom PayItem writer retirement) is "
        "irreversible. It drops payroll.CustomPayItemRequests and "
        "payroll.PayItemSettings and physically deletes ownerless predecessor "
        "company PayItems with their disposable configuration, source "
        "DraftLines and predecessor-only RateTypes. None of that data can be "
        "reconstructed. Restore from a pre-upgrade database backup if a "
        "downgrade is genuinely required."
    )
