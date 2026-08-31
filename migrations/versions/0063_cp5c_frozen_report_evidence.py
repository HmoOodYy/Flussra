"""0063: CP-5C immutable frozen report-evidence persistence.

Adds versioned Status and Bonus report evidence to new calculation snapshots.
Existing snapshots remain explicitly legacy: no evidence version/hash and no
synthetic backfill from mutable sources.

Revision ID: 0063
Revises: 0062
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0063"
down_revision: Union[str, None] = "0062"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0063_cp5c_frozen_report_evidence.sql"

_migrations_dir = Path(__file__).parent.parent
if str(_migrations_dir) not in sys.path:
    sys.path.insert(0, str(_migrations_dir))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    conn = op.get_bind()
    for table in (
        "payroll.payrollcalculationsnapshotstatusentries",
        "payroll.payrollcalculationsnapshotbonusevents",
    ):
        count = conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar()
        if count and count > 0:
            raise RuntimeError(
                "Downgrade of 0063 refused: immutable frozen report evidence exists "
                f"in {table}. Resolve retained historical evidence manually."
            )
    header_count = conn.execute(sa.text("""
        SELECT COUNT(*)
        FROM payroll.payrollcalculationsnapshots
        WHERE reportevidenceversion IS NOT NULL OR reportevidencehash IS NOT NULL
    """)).scalar()
    if header_count and header_count > 0:
        raise RuntimeError(
            "Downgrade of 0063 refused: calculation snapshots declare frozen "
            "report evidence. Resolve retained historical evidence manually."
        )

    for table, trigger in (
        ("payroll.PayrollCalculationSnapshotStatusEntries", "trg_PayrollCalculationSnapshotStatusEntries_Immutable"),
        ("payroll.PayrollCalculationSnapshotBonusEvents", "trg_PayrollCalculationSnapshotBonusEvents_Immutable"),
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger} ON {table}"))
    op.execute(sa.text("DROP TABLE payroll.PayrollCalculationSnapshotBonusEvents"))
    op.execute(sa.text("DROP TABLE payroll.PayrollCalculationSnapshotStatusEntries"))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollCalculationSnapshots "
        "DROP CONSTRAINT ck_PayrollCalculationSnapshots_ReportEvidenceMarker"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollCalculationSnapshots "
        "DROP CONSTRAINT uq_PayrollCalculationSnapshots_ID_Company_Branch_Period"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollCalculationSnapshots DROP COLUMN ReportEvidenceHash"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollCalculationSnapshots DROP COLUMN ReportEvidenceVersion"
    ))
