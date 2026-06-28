"""0058: Canonical Bonus Event Domain (CP-3A).

Converts the unused payroll.PayrollRunBonuses table into the canonical
payroll.PayrollBonusEvents domain table.

Changes:
  - Rename PayrollRunBonuses → PayrollBonusEvents (rename PK, sequence, FKs,
    indexes).
  - Drop EmployeeID, PayItemID, IncludeInMinimumPayComparison columns.
  - Update Status domain to 'Active' / 'Voided'.
  - Add Notes, SourceDraftLineID, BatchCorrelationID, IdempotencyKey,
    DataRevision columns.
  - Backfill existing BONUS Period Pay DraftLines into PayrollBonusEvents.
  - Add BonusEventID nullable FK + unique index to PayrollFinalLines.

Downgrade: refused when any app-written PayrollBonusEvents rows exist
(SourceDraftLineID IS NULL indicates a row was written by the application,
not the migration backfill).  Migrated rows (SourceDraftLineID IS NOT NULL)
are deleted on downgrade and the original schema is restored.

Revision ID: 0058
Revises: 0057
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0058"
down_revision: Union[str, None] = "0057"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0058_payroll_bonus_events.sql"

_migrations_dir = Path(__file__).parent.parent
if str(_migrations_dir) not in sys.path:
    sys.path.insert(0, str(_migrations_dir))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for stmt in statements_from_file(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    conn = op.get_bind()

    # Refuse downgrade if any application-written rows exist.
    # SourceDraftLineID IS NULL means the row was created by app code (not the
    # migration backfill), and cannot be losslessly reconstructed as a DraftLine.
    result = conn.execute(sa.text(
        "SELECT COUNT(*) FROM payroll.payrollbonusevents "
        "WHERE sourcedraftlineid IS NULL"
    ))
    app_written = result.scalar()
    if app_written and app_written > 0:
        raise RuntimeError(
            f"Downgrade of 0058 refused: {app_written} PayrollBonusEvents row(s) "
            "were written by application code (SourceDraftLineID IS NULL) and "
            "cannot be losslessly restored to DraftLines.  Resolve these rows "
            "manually before downgrading."
        )

    # Remove finalization bridge additions on PayrollFinalLines.
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ux_PayrollFinalLines_Period_BonusEvent"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollFinalLines "
        "DROP CONSTRAINT IF EXISTS fk_FinalLines_BonusEvent"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollFinalLines "
        "DROP COLUMN IF EXISTS BonusEventID"
    ))

    # Delete only the backfill-migrated rows (SourceDraftLineID IS NOT NULL).
    op.execute(sa.text(
        "DELETE FROM payroll.payrollbonusevents "
        "WHERE sourcedraftlineid IS NOT NULL"
    ))

    # Drop canonical indexes.
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ux_PayrollBonusEvents_SourceDraftLine"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ix_PayrollBonusEvents_Period_Driver"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ix_PayrollBonusEvents_Company_Branch_Status"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ix_PayrollBonusEvents_Driver_Status"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ix_PayrollBonusEvents_CreatedAtUtc"
    ))

    # Drop new FK for SourceDraftLineID.
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents "
        "DROP CONSTRAINT IF EXISTS fk_PayrollBonusEvents_SourceDraftLine"
    ))

    # Drop new columns.
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents DROP COLUMN IF EXISTS DataRevision"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents DROP COLUMN IF EXISTS IdempotencyKey"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents DROP COLUMN IF EXISTS BatchCorrelationID"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents DROP COLUMN IF EXISTS SourceDraftLineID"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents DROP COLUMN IF EXISTS Notes"
    ))

    # Restore Status domain.
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents "
        "DROP CONSTRAINT IF EXISTS ck_PayrollBonusEvents_VoidConsistency"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents "
        "DROP CONSTRAINT IF EXISTS ck_PayrollBonusEvents_Status"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents "
        "DROP CONSTRAINT IF EXISTS ck_PayrollBonusEvents_Amount"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents "
        "ALTER COLUMN Status SET DEFAULT 'Approved'"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents "
        "ADD CONSTRAINT ck_PayrollRunBonuses_Amount CHECK (Amount > 0)"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents "
        "ADD CONSTRAINT ck_PayrollRunBonuses_Status "
        "CHECK (Status IN ('Pending','Approved','Voided','Finalized'))"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents "
        "ADD CONSTRAINT ck_PayrollRunBonuses_VoidConsistency CHECK ("
        "    (Status = 'Voided' AND VoidedAtUtc IS NOT NULL) "
        "    OR (Status <> 'Voided' AND VoidedAtUtc IS NULL)"
        ")"
    ))

    # Restore removed FK constraints and columns.
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents "
        "ADD COLUMN IncludeInMinimumPayComparison BOOLEAN NOT NULL DEFAULT FALSE"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents "
        "ADD COLUMN PayItemID INTEGER NOT NULL DEFAULT 0"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents "
        "ADD COLUMN EmployeeID INTEGER NOT NULL DEFAULT 0"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents "
        "ADD CONSTRAINT fk_PayrollRunBonuses_Employee "
        "FOREIGN KEY (EmployeeID) REFERENCES core.Employees(EmployeeID)"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents "
        "ADD CONSTRAINT fk_PayrollRunBonuses_PayItem "
        "FOREIGN KEY (PayItemID) REFERENCES payroll.PayItems(PayItemID)"
    ))

    # Rename FK constraints back.
    for old, new in [
        ("fk_PayrollBonusEvents_Company",  "fk_PayrollRunBonuses_Company"),
        ("fk_PayrollBonusEvents_Branch",   "fk_PayrollRunBonuses_Branch"),
        ("fk_PayrollBonusEvents_Period",   "fk_PayrollRunBonuses_Period"),
        ("fk_PayrollBonusEvents_Driver",   "fk_PayrollRunBonuses_Driver"),
        ("fk_PayrollBonusEvents_Creator",  "fk_PayrollRunBonuses_Creator"),
        ("fk_PayrollBonusEvents_Updater",  "fk_PayrollRunBonuses_Updater"),
        ("fk_PayrollBonusEvents_Voider",   "fk_PayrollRunBonuses_Voider"),
    ]:
        op.execute(sa.text(
            f"ALTER TABLE payroll.PayrollBonusEvents "
            f"RENAME CONSTRAINT {old} TO {new}"
        ))

    # Recreate original indexes.
    for idx_sql in [
        "CREATE INDEX ix_PayrollRunBonuses_Company_Branch_Status "
        "    ON payroll.PayrollBonusEvents (CompanyID, BranchID, Status)",
        "CREATE INDEX ix_PayrollRunBonuses_CreatedAtUtc "
        "    ON payroll.PayrollBonusEvents (CreatedAtUtc)",
        "CREATE INDEX ix_PayrollRunBonuses_Driver_Status "
        "    ON payroll.PayrollBonusEvents (DriverID, Status)",
        "CREATE INDEX ix_PayrollRunBonuses_Employee_Status "
        "    ON payroll.PayrollBonusEvents (EmployeeID, Status)",
        "CREATE INDEX ix_PayrollRunBonuses_MinimumPayComparison "
        "    ON payroll.PayrollBonusEvents (IncludeInMinimumPayComparison, Status)",
        "CREATE INDEX ix_PayrollRunBonuses_PayItem_Status "
        "    ON payroll.PayrollBonusEvents (PayItemID, Status)",
        "CREATE INDEX ix_PayrollRunBonuses_Period_Status "
        "    ON payroll.PayrollBonusEvents (PayrollPeriodID, Status)",
    ]:
        op.execute(sa.text(idx_sql))

    # Rename column and table back.
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents "
        "RENAME COLUMN PayrollBonusEventID TO PayrollRunBonusID"
    ))
    op.execute(sa.text(
        "ALTER SEQUENCE payroll.payrollbonusevents_payrollbonuseventid_seq "
        "RENAME TO payrollrunbonuses_payrollrunbonusid_seq"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollBonusEvents RENAME TO PayrollRunBonuses"
    ))
