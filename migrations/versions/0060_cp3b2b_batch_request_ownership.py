"""0060: Bonus Batch Request Ownership Hardening (CP-3B2b).

Hardens payroll.PayrollBonusBatchRequests before CP-3B2b's POST /bonuses/batch
endpoint becomes its first writer:

  - CreatedByUserID NOT NULL.
  - Revision non-negativity, SHA-256-hex RequestHash, JSON-object payload,
    JSON-array CreatedEventIDs, and CreatedEventCount/array-length agreement
    check constraints.
  - A BEFORE INSERT OR UPDATE ownership trigger (Company/Branch must match the
    owning period; branch must belong to that company).

Downgrade: refused if any PayrollBonusBatchRequests rows exist — dropping the
integrity trigger/constraints while applied batch-request data is live would
weaken that data's guarantees.  No bonus events or batch-request rows are ever
deleted by this migration's downgrade.

Revision ID: 0060
Revises: 0059
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0060"
down_revision: Union[str, None] = "0059"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0060_cp3b2b_batch_request_ownership.sql"

_migrations_dir = Path(__file__).parent.parent
if str(_migrations_dir) not in sys.path:
    sys.path.insert(0, str(_migrations_dir))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for stmt in statements_from_file(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    conn = op.get_bind()

    # Refuse downgrade if any batch requests exist — removing the ownership
    # trigger and integrity constraints while applied batch data is live would
    # silently weaken that data's guarantees.
    batch_count = conn.execute(sa.text(
        "SELECT COUNT(*) FROM payroll.payrollbonusbatchrequests"
    )).scalar()
    if batch_count and batch_count > 0:
        raise RuntimeError(
            f"Downgrade of 0060 refused: {batch_count} PayrollBonusBatchRequests "
            "row(s) exist. Removing the ownership trigger and integrity "
            "constraints would weaken live applied-batch data. Resolve these "
            "rows manually before downgrading."
        )

    # Drop the ownership trigger and its function.
    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_bonusbatchrequests_ownership "
        "ON payroll.PayrollBonusBatchRequests"
    ))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS payroll.trg_fn_bonusbatchrequests_ownership()"
    ))

    # Drop the integrity constraints added by this migration.
    for constraint in (
        "ck_PayrollBonusBatchRequests_EventCountMatches",
        "ck_PayrollBonusBatchRequests_EventIDsArray",
        "ck_PayrollBonusBatchRequests_PayloadObject",
        "ck_PayrollBonusBatchRequests_HashHex",
        "ck_PayrollBonusBatchRequests_ResultRevision",
        "ck_PayrollBonusBatchRequests_ExpectedRevision",
    ):
        op.execute(sa.text(
            f"ALTER TABLE payroll.PayrollBonusBatchRequests "
            f"DROP CONSTRAINT IF EXISTS {constraint}"
        ))

    # Revert CreatedByUserID to nullable (its 0059 state).
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusBatchRequests "
        "ALTER COLUMN CreatedByUserID DROP NOT NULL"
    ))
