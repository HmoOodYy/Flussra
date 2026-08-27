"""0061: CP-4C immutable calculation snapshot foundation.

Creates the empty immutable snapshot tables and hashing-storage constraints used
by later CP-4D through CP-4F work. This revision deliberately adds no lifecycle
writer, reader, pointer, backfill, or public API.

Revision ID: 0061
Revises: 0060
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0061"
down_revision: Union[str, None] = "0060"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0061_cp4c_calculation_snapshots.sql"
_SNAPSHOT_TABLES = (
    "payroll.payrollcalculationsnapshotlines",
    "payroll.payrollcalculationdrivertotals",
    "payroll.payrollcalculationsnapshots",
)

_migrations_dir = Path(__file__).parent.parent
if str(_migrations_dir) not in sys.path:
    sys.path.insert(0, str(_migrations_dir))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def _assert_snapshot_tables_empty(conn) -> None:
    """Refuse to remove immutable financial evidence during downgrade."""
    for table in _SNAPSHOT_TABLES:
        count = conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar()
        if count and count > 0:
            raise RuntimeError(
                "Downgrade of 0061 refused: immutable calculation snapshot data "
                f"exists in {table}. Resolve the retained financial history "
                "manually before downgrading."
            )


def downgrade() -> None:
    conn = op.get_bind()
    _assert_snapshot_tables_empty(conn)

    for table, trigger in (
        ("payroll.PayrollCalculationSnapshotLines", "trg_PayrollCalculationSnapshotLines_Immutable"),
        ("payroll.PayrollCalculationDriverTotals", "trg_PayrollCalculationDriverTotals_Immutable"),
        ("payroll.PayrollCalculationSnapshots", "trg_PayrollCalculationSnapshots_Immutable"),
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger} ON {table}"))

    op.execute(sa.text("DROP TABLE payroll.PayrollCalculationSnapshotLines"))
    op.execute(sa.text("DROP TABLE payroll.PayrollCalculationDriverTotals"))
    op.execute(sa.text("DROP TABLE payroll.PayrollCalculationSnapshots"))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS payroll.fn_guard_calculation_snapshot_immutable()"
    ))
