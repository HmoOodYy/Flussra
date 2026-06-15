"""0044: CDPI schema corrections.

Corrects nine issues in the 0043 CDPI foundation:

1. Trigger parity: the append-only event guard is now in the SQL file so both
   the test bootstrap (conftest psycopg2 path) and a fresh Alembic upgrade
   produce identical schemas.  The Python wrapper uses migrations.utils.
   statements_from_file() which understands PostgreSQL dollar-quoted blocks.

2. Tenant integrity: composite FKs ensure CdpiRequests.RequestingBranchID
   and ApprovedPayItemID belong to the same company as the request.

3. Bidirectional link removed: CdpiDefinitions.SourceRequestID dropped.
   The canonical approval link is CdpiRequests.ApprovedPayItemID (unique).
   A source request is reachable via that link with no second writable path.

4. Lifecycle integrity: CHECK enforces Approved <-> non-null ApprovedPayItemID.

5. Positive numeric constraints on Revision, RequestRevision, SchemaVersion.

6. CreatedByUserID made NOT NULL on CdpiRequests and CdpiDefinitions.

7. CopiedFromRequestID self-referencing FK with same-company composite check.

8. ix_CdpiDefinitions_SourceRequest index removed with the column.

9. Supporting unique indexes for composite FK references added.

Revision ID: 0044
Revises: 0043
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0044_cdpi_corrections.sql"

# Add the migrations directory to sys.path so we can import the shared utility.
_migrations_dir = Path(__file__).parent.parent
if str(_migrations_dir) not in sys.path:
    sys.path.insert(0, str(_migrations_dir))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for stmt in statements_from_file(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    # Reverse order of upgrade steps.

    # 5. Restore SourceRequestID and its constraints on CdpiDefinitions
    op.execute(sa.text(
        "ALTER TABLE payroll.CdpiDefinitions "
        "ALTER COLUMN CreatedByUserID DROP NOT NULL"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.CdpiDefinitions "
        "DROP CONSTRAINT IF EXISTS ck_CdpiDefinitions_SchemaVersion"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.CdpiDefinitions ADD COLUMN IF NOT EXISTS SourceRequestID UUID"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.CdpiDefinitions "
        "ADD CONSTRAINT fk_CdpiDefinitions_SourceRequest "
        "FOREIGN KEY (SourceRequestID) REFERENCES payroll.CdpiRequests(RequestID) "
        "ON DELETE RESTRICT"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.CdpiDefinitions "
        "ADD CONSTRAINT ux_CdpiDefinitions_SourceRequestID UNIQUE (SourceRequestID)"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_CdpiDefinitions_SourceRequest "
        "ON payroll.CdpiDefinitions (SourceRequestID) "
        "WHERE SourceRequestID IS NOT NULL"
    ))

    # 4. Remove CdpiRequestEvents revision constraint
    op.execute(sa.text(
        "ALTER TABLE payroll.CdpiRequestEvents "
        "DROP CONSTRAINT IF EXISTS ck_CdpiRequestEvents_Revision"
    ))

    # 3. Undo CdpiRequests changes
    op.execute(sa.text(
        "ALTER TABLE payroll.CdpiRequests "
        "ALTER COLUMN CreatedByUserID DROP NOT NULL"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.CdpiRequests "
        "DROP CONSTRAINT IF EXISTS fk_CdpiRequests_CopiedFrom_Company"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.CdpiRequests "
        "DROP CONSTRAINT IF EXISTS fk_CdpiRequests_CopiedFrom"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.CdpiRequests "
        "DROP CONSTRAINT IF EXISTS fk_CdpiRequests_ApprovedItem_Company"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.CdpiRequests "
        "DROP CONSTRAINT IF EXISTS fk_CdpiRequests_Branch_Company"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.CdpiRequests "
        "DROP CONSTRAINT IF EXISTS ck_CdpiRequests_Revision"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.CdpiRequests "
        "DROP CONSTRAINT IF EXISTS ck_CdpiRequests_ApprovalLink"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.CdpiRequests "
        "DROP COLUMN IF EXISTS CopiedFromRequestID"
    ))

    # 2. Drop supporting unique indexes
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ux_CdpiRequests_ID_Company"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ux_PayItems_ID_Company"
    ))
    # Do NOT drop ux_Branches_ID_Company — it was added by migration 0025

    # 1. Remove trigger and function
    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_guard_cdpi_request_events_immutable "
        "ON payroll.CdpiRequestEvents"
    ))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS payroll.fn_guard_cdpi_request_events_immutable()"
    ))
