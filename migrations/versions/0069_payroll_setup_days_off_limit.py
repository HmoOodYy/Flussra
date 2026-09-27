"""Enforce the two-day Normal Days Off product limit on Payroll Setup Versions.

Revision ID: 0069
Revises: 0068
"""
import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0069_payroll_setup_days_off_limit.sql"
_MIGRATIONS_DIR = Path(__file__).parent.parent
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollSetupVersions DROP CONSTRAINT ck_PayrollSetupVersions_Mask"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollSetupVersions ADD CONSTRAINT ck_PayrollSetupVersions_Mask "
        "CHECK (NormalDaysOffMask IS NULL OR NormalDaysOffMask BETWEEN 0 AND 127)"
    ))
