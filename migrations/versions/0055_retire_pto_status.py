"""0055: Retire PTO_STATUS system pay item.

Product decision: Status is exclusively a Daily Grid column managed through
PayrollStatusKeys.  PTO_STATUS was a legacy pay-item concept where a driver's
daily status was treated as a Pay Item.  It is fully removed from the catalog
so it no longer appears in Pay Items setup, Payroll Setup pay-item config,
Daily Grid pay-item columns, or period snapshots.

Safety: the migration runs a preflight DO block that:
  - Locates PTO_STATUS PayItemID (no-op if already gone)
  - Raises if PayrollFinalLines has PTO_STATUS rows (immutable financial history)
  - Raises if non-voided PayrollDraftLines have PTO_STATUS rows
  - Raises if PayItemRateTypeMap / PayItemSettings / PayItemRateSlots reference it
  - Explicitly deletes BranchPayItemConfig rows (config; safe)
  - Explicitly deletes PayrollPeriodPayItems snapshot rows (safe)
  - Deletes the PayItems row itself only after all checks pass

Downgrade: re-inserts the row (idempotent via ON CONFLICT DO NOTHING).

Revision ID: 0055
Revises: 0054
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0055"
down_revision: Union[str, None] = "0054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0055_retire_pto_status.sql"

_migrations_dir = Path(__file__).parent.parent
if str(_migrations_dir) not in sys.path:
    sys.path.insert(0, str(_migrations_dir))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for stmt in statements_from_file(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    op.execute(sa.text("""
        INSERT INTO payroll.PayItems (
            CompanyID, BranchID,
            PayItemCode, PayItemName, Category,
            DataType, Unit,
            Status, SortOrder,
            AppearsInPayrollEntry, AppearsInLedger, AppearsInReports,
            RequiresRate, IsSystemStandard,
            ItemScope, RateBehavior, IsDefaultBranchActive
        ) VALUES (
            NULL, NULL, 'PTO_STATUS', 'PTO / Time Off', 'Leave',
            'Status', NULL,
            'Active', 8, TRUE, TRUE, TRUE, FALSE, TRUE,
            'Daily', 'None', FALSE
        )
        ON CONFLICT DO NOTHING
    """))
