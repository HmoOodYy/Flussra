"""0046: Generic PayItemRateSlots foundation.

Adds payroll.PayItemRateSlots — a generic slot-definition layer for all
PayItems that records the business meaning of each PayItem -> RateType link:
stable SlotKey, SlotRole, SortOrder, requiredness, and source metadata.

PayItemRateTypeMap is preserved unchanged and continues to serve as the
compatibility link for the Pay Rates matrix and DriverRates.

Backfills existing active PayItemRateTypeMap rows into PayItemRateSlots with
conservative defaults:
  SlotKey     = 'legacy_rate_<RateTypeID>'   (deterministic, immutable)
  SlotRole    = 'legacy_primary' | 'legacy_rate'
  SourceKind  = 'LegacyBackfill'

No existing tables, rows, or constraints are modified.

Revision ID: 0046
Revises: 0045
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0046"
down_revision: Union[str, None] = "0045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0046_pay_item_rate_slots.sql"

_migrations_dir = Path(__file__).parent.parent
if str(_migrations_dir) not in sys.path:
    sys.path.insert(0, str(_migrations_dir))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for stmt in statements_from_file(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS payroll.PayItemRateSlots"))
