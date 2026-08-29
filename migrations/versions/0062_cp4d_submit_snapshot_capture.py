"""0062: CP-4D submitted calculation snapshot capture link.

Adds the nullable, tenant-safe immutable calculation snapshot link to review
items. CP-4D service code is the first writer; no legacy rows are backfilled.

Revision ID: 0062
Revises: 0061
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0062"
down_revision: Union[str, None] = "0061"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0062_cp4d_submit_snapshot_capture.sql"

_migrations_dir = Path(__file__).parent.parent
if str(_migrations_dir) not in sys.path:
    sys.path.insert(0, str(_migrations_dir))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    conn = op.get_bind()
    count = conn.execute(sa.text(
        "SELECT COUNT(*) FROM review.managerreviewitems "
        "WHERE payrollcalculationsnapshotid IS NOT NULL"
    )).scalar()
    if count and count > 0:
        raise RuntimeError(
            "Downgrade of 0062 refused: review items reference immutable "
            "calculation snapshots. Resolve retained financial history manually."
        )

    op.execute(sa.text(
        "ALTER TABLE review.ManagerReviewItems "
        "DROP CONSTRAINT fk_ManagerReviewItems_CalculationSnapshot_Company_Branch"
    ))
    op.execute(sa.text(
        "ALTER TABLE review.ManagerReviewItems "
        "DROP CONSTRAINT uq_ManagerReviewItems_PayrollCalculationSnapshot"
    ))
    op.execute(sa.text(
        "ALTER TABLE review.ManagerReviewItems "
        "DROP COLUMN PayrollCalculationSnapshotID"
    ))
