"""0026: Parent-root branch/company integrity.

Adds composite FK constraints so core.Drivers and payroll.PayrollPeriods
cannot reference a branch from a different company than their own CompanyID.

The supporting UNIQUE index ux_Branches_ID_Company on core.Branches was
created in migration 0025 and is reused here as the FK target.

Both constraints are NOT VALID: existing rows are not scanned; enforced
going forward on INSERT and UPDATE.

Revision ID: 0026
Revises: 0025
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0026"
down_revision: str = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drivers(BranchID, CompanyID) must map to a real Branches row in same company
    op.execute(sa.text(
        "ALTER TABLE core.Drivers "
        "ADD CONSTRAINT fk_Drivers_Branch_Company "
        "FOREIGN KEY (BranchID, CompanyID) "
        "REFERENCES core.Branches (BranchID, CompanyID) "
        "NOT VALID"
    ))

    # PayrollPeriods(BranchID, CompanyID) must map to a real Branches row in same company
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollPeriods "
        "ADD CONSTRAINT fk_PayrollPeriods_Branch_Company "
        "FOREIGN KEY (BranchID, CompanyID) "
        "REFERENCES core.Branches (BranchID, CompanyID) "
        "NOT VALID"
    ))


def downgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollPeriods "
        "DROP CONSTRAINT IF EXISTS fk_PayrollPeriods_Branch_Company"
    ))
    op.execute(sa.text(
        "ALTER TABLE core.Drivers "
        "DROP CONSTRAINT IF EXISTS fk_Drivers_Branch_Company"
    ))
