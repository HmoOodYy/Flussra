"""Persist the planned effective date for Draft Payroll Setup Versions.

Revision ID: 0070
Revises: 0069
"""
import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0070"
down_revision: str | None = "0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0070_payroll_setup_planned_effective_date.sql"
_MIGRATIONS_DIR = Path(__file__).parent.parent
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollSetupVersions "
        "DROP CONSTRAINT IF EXISTS ck_PayrollSetupVersions_PlannedEffectiveDraft"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollSetupVersions DROP COLUMN IF EXISTS PlannedEffectiveFromDate"
    ))
