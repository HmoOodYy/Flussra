"""Transitional Payroll Period and PeriodDay Setup authority provenance.

Revision ID: 0067
Revises: 0066
"""
import sys
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0067_payroll_setup_period_provenance.sql"
_MIGRATIONS_DIR = Path(__file__).parent.parent
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM payroll.PayrollPeriods "
        "WHERE BranchPayrollSetupAssignmentID IS NOT NULL)"
    )).scalar() or bind.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM payroll.PayrollPeriodDays "
        "WHERE PayrollSetupVersionID IS NOT NULL OR ScheduleVersionID IS NULL)"
    )).scalar():
        raise RuntimeError("Downgrade of 0067 refused: new-authority period history exists.")
    op.execute(sa.text("DROP INDEX payroll.ix_PayrollPeriodDays_SetupAuthority"))
    op.execute(sa.text("ALTER TABLE payroll.PayrollPeriodDays DROP CONSTRAINT fk_PayrollPeriodDays_ExactLegacyAuthority"))
    op.execute(sa.text("ALTER TABLE payroll.PayrollPeriodDays DROP CONSTRAINT fk_PayrollPeriodDays_ExactNewAuthority"))
    op.execute(sa.text("ALTER TABLE payroll.PayrollPeriodDays DROP CONSTRAINT ck_PayrollPeriodDays_AuthorityRepresentation"))
    op.execute(sa.text("ALTER TABLE payroll.PayrollPeriodDays DROP COLUMN BranchPayrollSetupAssignmentID, DROP COLUMN PayrollSetupVersionID"))
    op.execute(sa.text("ALTER TABLE payroll.PayrollPeriodDays ALTER COLUMN ScheduleVersionID SET NOT NULL"))
    op.execute(sa.text("DROP TRIGGER trg_PayrollPeriods_SetupProvenance ON payroll.PayrollPeriods"))
    op.execute(sa.text("DROP FUNCTION payroll.fn_guard_period_setup_provenance()"))
    op.execute(sa.text("DROP INDEX payroll.ux_PayrollPeriods_LegacyDayAuthority"))
    op.execute(sa.text("DROP INDEX payroll.ux_PayrollPeriods_SetupDayAuthority"))
    op.execute(sa.text("ALTER TABLE payroll.PayrollPeriods DROP CONSTRAINT fk_PayrollPeriods_SetupVersion"))
    op.execute(sa.text("ALTER TABLE payroll.PayrollPeriods DROP CONSTRAINT fk_PayrollPeriods_SetupAssignment"))
    op.execute(sa.text("ALTER TABLE payroll.PayrollPeriods DROP CONSTRAINT ck_PayrollPeriods_AuthorityRepresentation"))
    for column in (
        "BranchPayrollSetupAssignmentID", "PayrollSetupVersionID", "FrozenPayrollSetupID",
        "FrozenPayrollSetupCode", "FrozenPayrollSetupVersionNumber", "FrozenPayrollFrequency",
        "FrozenAnchorStartDate", "FrozenCustomIntervalDays", "FrozenNormalDaysOffMask",
        "ScheduleConfigHash",
    ):
        op.execute(sa.text(f"ALTER TABLE payroll.PayrollPeriods DROP COLUMN {column}"))
