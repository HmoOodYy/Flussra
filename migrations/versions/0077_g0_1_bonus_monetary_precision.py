"""0077: G0.1 Bonus monetary precision closure."""

import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0077"
down_revision: str | None = "0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATIONS_DIR = Path(__file__).parent.parent
_SQL_FILE = _MIGRATIONS_DIR / "sql" / "0077_g0_1_bonus_monetary_precision.sql"
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    bind = op.get_bind()
    would_lose_precision = bind.execute(sa.text("""
        SELECT EXISTS (
            SELECT 1
            FROM payroll.PayrollBonusEvents
            WHERE Amount <> trunc(Amount, 2)
        )
    """)).scalar_one()
    if would_lose_precision:
        raise RuntimeError(
            "Downgrade of 0077 refused: PayrollBonusEvents.Amount contains "
            "values that cannot be represented by NUMERIC(18,2)."
        )

    op.execute(sa.text("""
        ALTER TABLE payroll.PayrollBonusEvents
        ALTER COLUMN Amount TYPE NUMERIC(18,2)
        USING Amount::NUMERIC(18,2)
    """))
