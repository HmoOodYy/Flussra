"""0025: Composite FK integrity for payroll tables.

Adds UNIQUE indexes on parent tables (Branches, Drivers, PayrollPeriods) and
composite FK constraints (NOT VALID) on PayrollDraftLines, PayrollFinalLines,
DriverRates, and DriverPayRules to enforce cross-table CompanyID/BranchID
consistency at the DB level going forward.

NOT VALID means existing historical rows are not scanned — the constraints
only fire on INSERT and UPDATE, preserving any pre-migration data.

Revision ID: 0025
Revises: 0024
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0025"
down_revision: str = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Step 1: Supporting UNIQUE indexes on parent tables ─────────────────── #
    # Each index is a strict superset of the existing PK so all current rows
    # satisfy it trivially — no data is at risk.
    op.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_Branches_ID_Company "
        "ON core.Branches (BranchID, CompanyID)"
    ))
    op.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_Drivers_ID_Company_Branch "
        "ON core.Drivers (DriverID, CompanyID, BranchID)"
    ))
    op.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_PayrollPeriods_ID_Company_Branch "
        "ON payroll.PayrollPeriods (PayrollPeriodID, CompanyID, BranchID)"
    ))

    # ── Step 2: PayrollDraftLines composite FKs ────────────────────────────── #
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollDraftLines "
        "ADD CONSTRAINT fk_DraftLines_Period_Company_Branch "
        "FOREIGN KEY (PayrollPeriodID, CompanyID, BranchID) "
        "REFERENCES payroll.PayrollPeriods (PayrollPeriodID, CompanyID, BranchID) "
        "NOT VALID"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollDraftLines "
        "ADD CONSTRAINT fk_DraftLines_Driver_Company_Branch "
        "FOREIGN KEY (DriverID, CompanyID, BranchID) "
        "REFERENCES core.Drivers (DriverID, CompanyID, BranchID) "
        "NOT VALID"
    ))

    # ── Step 3: PayrollFinalLines composite FKs ────────────────────────────── #
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollFinalLines "
        "ADD CONSTRAINT fk_FinalLines_Period_Company_Branch "
        "FOREIGN KEY (PayrollPeriodID, CompanyID, BranchID) "
        "REFERENCES payroll.PayrollPeriods (PayrollPeriodID, CompanyID, BranchID) "
        "NOT VALID"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollFinalLines "
        "ADD CONSTRAINT fk_FinalLines_Driver_Company_Branch "
        "FOREIGN KEY (DriverID, CompanyID, BranchID) "
        "REFERENCES core.Drivers (DriverID, CompanyID, BranchID) "
        "NOT VALID"
    ))

    # ── Step 4: DriverRates composite FK ──────────────────────────────────── #
    op.execute(sa.text(
        "ALTER TABLE payroll.DriverRates "
        "ADD CONSTRAINT fk_DriverRates_Driver_Company_Branch "
        "FOREIGN KEY (DriverID, CompanyID, BranchID) "
        "REFERENCES core.Drivers (DriverID, CompanyID, BranchID) "
        "NOT VALID"
    ))

    # ── Step 5: DriverPayRules composite FK ──────────────────────────────── #
    op.execute(sa.text(
        "ALTER TABLE payroll.DriverPayRules "
        "ADD CONSTRAINT fk_DriverPayRules_Driver_Company_Branch "
        "FOREIGN KEY (DriverID, CompanyID, BranchID) "
        "REFERENCES core.Drivers (DriverID, CompanyID, BranchID) "
        "NOT VALID"
    ))


def downgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE payroll.DriverPayRules "
        "DROP CONSTRAINT IF EXISTS fk_DriverPayRules_Driver_Company_Branch"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.DriverRates "
        "DROP CONSTRAINT IF EXISTS fk_DriverRates_Driver_Company_Branch"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollFinalLines "
        "DROP CONSTRAINT IF EXISTS fk_FinalLines_Driver_Company_Branch"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollFinalLines "
        "DROP CONSTRAINT IF EXISTS fk_FinalLines_Period_Company_Branch"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollDraftLines "
        "DROP CONSTRAINT IF EXISTS fk_DraftLines_Driver_Company_Branch"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollDraftLines "
        "DROP CONSTRAINT IF EXISTS fk_DraftLines_Period_Company_Branch"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ux_PayrollPeriods_ID_Company_Branch"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS core.ux_Drivers_ID_Company_Branch"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS core.ux_Branches_ID_Company"
    ))
