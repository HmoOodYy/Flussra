"""0083: Generic PayDefinition governance and provenance."""

import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0083"
down_revision: str | None = "0082"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATIONS_DIR = Path(__file__).parent.parent
_SQL_FILE = _MIGRATIONS_DIR / "sql" / "0083_pay_definition_governance.sql"
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file  # noqa: E402

_TABLES = (
    "payroll.PayDefinitionProvenance",
    "payroll.PayDefinitionRequestEvents",
    "payroll.PayDefinitionRequests",
)


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar_one():
            raise RuntimeError(
                f"Downgrade of 0083 refused: {table} contains rows that would be lost."
            )
    for table in _TABLES:
        op.execute(sa.text(f"DROP TABLE {table}"))
    for function in (
        "payroll.trg_PayDefinitionProvenance_RequestLink()",
        "payroll.trg_PayDefinitionRecords_AppendOnly()",
        "payroll.trg_PayDefinitionRequests_Guard()",
    ):
        op.execute(sa.text(f"DROP FUNCTION {function}"))
