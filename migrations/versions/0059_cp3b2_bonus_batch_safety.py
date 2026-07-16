"""0059: Bonus Batch Safety Foundation (CP-3B2a).

Adds the durable/concurrency scaffolding required before a transactional
bonus batch endpoint (CP-3B2b) can be added.  No batch endpoint exists yet.

Changes:
  - Add PayrollPeriods.BonusDataRevision BIGINT NOT NULL DEFAULT 0.
  - Add a DB-level ownership trigger on PayrollBonusEvents (CompanyID/
    BranchID must match the owning PayrollPeriods row).
  - Create payroll.PayrollBonusBatchRequests (foundation only).
  - Add a partial BatchCorrelationID index on PayrollBonusEvents.

Downgrade: refused if any PayrollBonusBatchRequests rows exist, if any
PayrollBonusEvents row has BatchCorrelationID IS NOT NULL, or if any
PayrollPeriods.BonusDataRevision <> 0.  No bonus events are ever deleted by
this migration's downgrade.

Revision ID: 0059
Revises: 0058
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0059"
down_revision: Union[str, None] = "0058"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0059_cp3b2_bonus_batch_safety.sql"

_migrations_dir = Path(__file__).parent.parent
if str(_migrations_dir) not in sys.path:
    sys.path.insert(0, str(_migrations_dir))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for stmt in statements_from_file(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    conn = op.get_bind()

    # Refuse downgrade if any batch requests were ever recorded.
    batch_count = conn.execute(sa.text(
        "SELECT COUNT(*) FROM payroll.payrollbonusbatchrequests"
    )).scalar()
    if batch_count and batch_count > 0:
        raise RuntimeError(
            f"Downgrade of 0059 refused: {batch_count} PayrollBonusBatchRequests "
            "row(s) exist. Resolve these rows manually before downgrading."
        )

    # Refuse downgrade if any event carries a batch correlation ID.
    correlated_count = conn.execute(sa.text(
        "SELECT COUNT(*) FROM payroll.payrollbonusevents "
        "WHERE batchcorrelationid IS NOT NULL"
    )).scalar()
    if correlated_count and correlated_count > 0:
        raise RuntimeError(
            f"Downgrade of 0059 refused: {correlated_count} PayrollBonusEvents "
            "row(s) have BatchCorrelationID set. Resolve these rows manually "
            "before downgrading."
        )

    # Refuse downgrade if any period's bonus revision has moved off its
    # initial value — that would silently discard concurrency history.
    moved_count = conn.execute(sa.text(
        "SELECT COUNT(*) FROM payroll.payrollperiods WHERE bonusdatarevision <> 0"
    )).scalar()
    if moved_count and moved_count > 0:
        raise RuntimeError(
            f"Downgrade of 0059 refused: {moved_count} PayrollPeriods row(s) "
            "have BonusDataRevision <> 0. Resolve these rows manually before "
            "downgrading."
        )

    # Drop PayrollBonusEvents BatchCorrelationID index.
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ix_PayrollBonusEvents_BatchCorrelation"
    ))

    # Drop PayrollBonusBatchRequests table (indexes drop with it).
    op.execute(sa.text(
        "DROP TABLE IF EXISTS payroll.PayrollBonusBatchRequests"
    ))

    # Drop the ownership trigger and its function.
    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_bonusevents_ownership "
        "ON payroll.PayrollBonusEvents"
    ))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS payroll.trg_fn_bonusevents_ownership()"
    ))

    # Drop PayrollPeriods.BonusDataRevision.
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollPeriods DROP COLUMN IF EXISTS BonusDataRevision"
    ))
