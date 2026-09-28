"""Retire legacy branch-owned payroll schedule authority.

Revision ID: 0071
Revises: 0070
"""
import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0071_retire_legacy_payroll_schedule_authority.sql"
_MIGRATIONS_DIR = Path(__file__).parent.parent
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    raise RuntimeError(
        "Migration 0071 (legacy payroll schedule authority retirement) is "
        "irreversible. It physically drops payroll.BranchPayrollSettings and "
        "payroll.PayrollScheduleVersions, which cannot be reconstructed from "
        "the canonical Payroll Setup / Published Version / Branch Assignment "
        "model without fabricating historical schedule data that never "
        "existed in that legacy shape. Restore payroll.PayrollPeriods and "
        "payroll.PayrollPeriodDays' pre-0071 ScheduleVersionID authority, and "
        "the retired tables themselves, from a pre-upgrade database backup "
        "taken before running this migration if a downgrade is genuinely "
        "required."
    )
