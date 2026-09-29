"""Enforce Driver company, branch, and effective-window integrity.

Revision ID: 0072
Revises: 0071
"""
import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0072_workforce_driver_integrity.sql"
_MIGRATIONS_DIR = Path(__file__).parent.parent
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_Drivers_HistoryImmutable ON core.Drivers"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS core.trg_Drivers_HistoryImmutable()"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_Drivers_IdentityImmutable ON core.Drivers"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS core.trg_Drivers_IdentityImmutable()"))
    op.execute(sa.text("ALTER TABLE core.Drivers DROP CONSTRAINT IF EXISTS excl_Drivers_Employee_EffectiveWindow"))
    op.execute(sa.text("ALTER TABLE core.Drivers DROP CONSTRAINT IF EXISTS ck_Drivers_ClosedHistory"))
    op.execute(sa.text("ALTER TABLE core.Drivers DROP CONSTRAINT IF EXISTS ck_Drivers_EffectiveWindow"))
    op.execute(sa.text("ALTER TABLE core.Employees DROP CONSTRAINT IF EXISTS ck_Employees_EmploymentStatus"))
    op.execute(sa.text("ALTER TABLE core.Drivers DROP CONSTRAINT IF EXISTS ck_Drivers_Status"))
    op.execute(sa.text("ALTER TABLE core.Drivers DROP CONSTRAINT IF EXISTS fk_Drivers_Employee_Company"))
    op.execute(sa.text("ALTER TABLE core.Drivers DROP CONSTRAINT fk_Drivers_Branch_Company"))
    op.execute(sa.text("""
        ALTER TABLE core.Drivers
        ADD CONSTRAINT fk_Drivers_Branch_Company
        FOREIGN KEY (BranchID, CompanyID)
        REFERENCES core.Branches (BranchID, CompanyID)
        NOT VALID
    """))
    op.execute(sa.text("DROP INDEX IF EXISTS core.ux_Employees_Employee_Company"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS core.fn_EffectiveDriverProfile(INTEGER, INTEGER, DATE)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS core.fn_CompanyToday(INTEGER)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS core.fn_DriverEffectiveRange(DATE, DATE)"))
