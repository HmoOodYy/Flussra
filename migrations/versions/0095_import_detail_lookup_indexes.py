"""0095: Index Import detail child lookups for bounded retirement cost."""
import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0095"
down_revision: str | None = "0094"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATIONS_DIR = Path(__file__).parent.parent
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file


def upgrade() -> None:
    for statement in statements_from_file(_MIGRATIONS_DIR / "sql" / "0095_import_detail_lookup_indexes.sql"):
        op.execute(sa.text(statement))


def downgrade() -> None:
    op.execute("DROP INDEX import.ix_PayrollWorkRecordIdentifiers_MatchScope")
    op.execute("DROP INDEX import.ix_ImportIssues_SourceRecordScope")
