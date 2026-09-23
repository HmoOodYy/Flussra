"""Company-owned Payroll Setup, versions, assignments, and onboarding default.

Revision ID: 0066
Revises: 0065
"""
import sys
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0066_company_payroll_setup_foundation.sql"
_MIGRATIONS_DIR = Path(__file__).parent.parent
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("payroll.PayrollSetupVersions", "payroll.BranchPayrollSetupAssignments", "payroll.PayrollSetups"):
        if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar():
            raise RuntimeError("Downgrade of 0066 refused: Payroll Setup business data exists.")
    op.execute(sa.text("DROP TRIGGER trg_PayrollSetups_DefaultArchive ON payroll.PayrollSetups"))
    op.execute(sa.text("DROP FUNCTION payroll.fn_guard_archived_default_payroll_setup()"))
    op.execute(sa.text("DROP TRIGGER trg_Companies_DefaultPayrollSetup ON core.Companies"))
    op.execute(sa.text("DROP FUNCTION payroll.fn_guard_company_default_payroll_setup()"))
    op.execute(sa.text("ALTER TABLE core.Companies DROP COLUMN DefaultPayrollSetupID"))
    op.execute(sa.text("DROP TRIGGER trg_BranchPayrollSetupAssignments_Identity ON payroll.BranchPayrollSetupAssignments"))
    op.execute(sa.text("DROP FUNCTION payroll.fn_guard_payroll_setup_assignment_identity()"))
    op.execute(sa.text("DROP TABLE payroll.BranchPayrollSetupAssignments"))
    op.execute(sa.text("DROP TRIGGER trg_PayrollSetupVersions_Guard ON payroll.PayrollSetupVersions"))
    op.execute(sa.text("DROP FUNCTION payroll.fn_guard_payroll_setup_version()"))
    op.execute(sa.text("DROP TABLE payroll.PayrollSetupVersions"))
    op.execute(sa.text("DROP TRIGGER trg_PayrollSetups_Identity ON payroll.PayrollSetups"))
    op.execute(sa.text("DROP FUNCTION payroll.fn_guard_payroll_setup_identity()"))
    op.execute(sa.text("DROP TABLE payroll.PayrollSetups"))
