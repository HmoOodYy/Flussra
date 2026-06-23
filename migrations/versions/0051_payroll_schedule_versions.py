"""0051: Payroll schedule versioning.

Introduces an immutable version history for BranchPayrollSettings so that
every payroll period created after this migration references the exact schedule
configuration that governed its date derivation.

Schema changes:
  - payroll.PayrollScheduleVersions (new append-only table)
  - Composite unique index uq_PayrollScheduleVersions_Comp
      supports composite FK references on (ScheduleVersionID, CompanyID, BranchID)
  - payroll.BranchPayrollSettings.CurrentScheduleVersionID BIGINT NULL
      + composite FK → PayrollScheduleVersions
  - payroll.PayrollPeriods.ScheduleVersionID BIGINT NULL
      + composite FK → PayrollScheduleVersions
  - Backfill: one VersionNumber=1/BACKFILL row per existing BranchPayrollSettings
  - BranchPayrollSettings.CurrentScheduleVersionID updated from backfill

Downgrade refuses if:
  - any PayrollPeriods.ScheduleVersionID is not NULL (post-migration created periods), or
  - any BranchPayrollSettings.CurrentScheduleVersionID points to a version with
    VersionNumber > 1 (i.e. setup was updated after CP-2A was deployed).
On clean-backfill-only state (VersionNumber=1 everywhere): drops FKs, columns,
indexes, and the table in dependency order.

Revision ID: 0051
Revises: 0050
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0051"
down_revision: Union[str, None] = "0050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0051_payroll_schedule_versions.sql"

_migrations_dir = Path(__file__).parent.parent
if str(_migrations_dir) not in sys.path:
    sys.path.insert(0, str(_migrations_dir))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for stmt in statements_from_file(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    conn = op.get_bind()

    # Refuse if any period has ScheduleVersionID set (post-CP-2A created periods).
    period_count = conn.execute(sa.text(
        "SELECT COUNT(*) FROM payroll.PayrollPeriods "
        "WHERE ScheduleVersionID IS NOT NULL"
    )).scalar()
    if period_count and period_count > 0:
        raise RuntimeError(
            f"Migration 0051 downgrade refused: {period_count} payroll period(s) have "
            "a non-NULL ScheduleVersionID. Archive or cancel these periods before "
            "downgrading. Never rewrite or delete business data during downgrade."
        )

    # Refuse if any non-backfill version rows exist (SourceAction != 'BACKFILL').
    # Covers VersionNumber=1 rows created by SETUP_UPDATED or REPAIR — these represent
    # real post-CP-2A activity even when no period references them yet.
    non_backfill_count = conn.execute(sa.text(
        "SELECT COUNT(*) FROM payroll.PayrollScheduleVersions "
        "WHERE SourceAction != 'BACKFILL'"
    )).scalar()
    if non_backfill_count and non_backfill_count > 0:
        raise RuntimeError(
            f"Migration 0051 downgrade refused: {non_backfill_count} schedule version(s) "
            "with SourceAction != 'BACKFILL' exist (SETUP_UPDATED or REPAIR). "
            "These represent post-CP-2A history. Remove them before downgrading."
        )

    # Also refuse if VersionNumber > 1 for any remaining rows (belt-and-suspenders).
    new_version_count = conn.execute(sa.text(
        "SELECT COUNT(*) FROM payroll.PayrollScheduleVersions "
        "WHERE VersionNumber > 1"
    )).scalar()
    if new_version_count and new_version_count > 0:
        raise RuntimeError(
            f"Migration 0051 downgrade refused: {new_version_count} schedule version(s) "
            "with VersionNumber > 1 exist. These represent payroll setup updates made "
            "after CP-2A was deployed. Remove them before downgrading."
        )

    # Safe to downgrade: only BACKFILL rows with VersionNumber=1 remain.

    # 1. Drop FK and column from PayrollPeriods
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollPeriods "
        "DROP CONSTRAINT IF EXISTS fk_PP_ScheduleVersion"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ix_PayrollPeriods_ScheduleVersion"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollPeriods "
        "DROP COLUMN IF EXISTS ScheduleVersionID"
    ))

    # 2. Drop FK and column from BranchPayrollSettings
    op.execute(sa.text(
        "ALTER TABLE payroll.BranchPayrollSettings "
        "DROP CONSTRAINT IF EXISTS fk_BPS_CurrentScheduleVersion"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.BranchPayrollSettings "
        "DROP COLUMN IF EXISTS CurrentScheduleVersionID"
    ))

    # 3. Drop composite unique index and main table
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.uq_PayrollScheduleVersions_Comp"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ix_PayrollScheduleVersions_Branch"
    ))
    op.execute(sa.text(
        "DROP TABLE IF EXISTS payroll.PayrollScheduleVersions"
    ))
