"""0047: CDPI PerUnit rate-slot backfill.

Repairs existing approved/direct-created CDPI PerUnit PayItems that were
created before PR-1B and therefore have no RateType, PayItemRateTypeMap,
or PayItemRateSlots rows.

For each affected item creates:
  - A company-scoped RateType with code 'CDPI_{id}_PER_UNIT'
  - A PayItemRateTypeMap row (primary, Active)
  - A PayItemRateSlots row (slot_key='per_unit_rate', SourceKind='CDPI')
  - Sets PayItems.RequiresRate = TRUE

Idempotent: all INSERT steps are guarded by NOT EXISTS / ON CONFLICT.
No schema changes.

Revision ID: 0047
Revises: 0046
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0047"
down_revision: Union[str, None] = "0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0047_cdpi_perunit_rate_slot_backfill.sql"

_migrations_dir = Path(__file__).parent.parent
if str(_migrations_dir) not in sys.path:
    sys.path.insert(0, str(_migrations_dir))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for stmt in statements_from_file(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    # Data-only migration: downgrade is a no-op.
    # The created RateType/Map/Slot rows cannot be safely removed without
    # knowing which items existed pre-migration.
    pass
