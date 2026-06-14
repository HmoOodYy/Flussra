"""M15: DriverPayRules table + SYS_MIN_TOPUP / SYS_MAX_CAP PayItems.

Adds the payroll.DriverPayRules table for Minimum/Maximum Pay rules
and seeds two system PayItems used only by the finalization engine.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-30
"""
import re
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0012_driver_pay_rules.sql"


def _statements(path: Path) -> list[str]:
    sql = path.read_text(encoding="utf-8")
    clean = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in clean.split(";") if s.strip()]


def upgrade() -> None:
    for stmt in _statements(_SQL_FILE):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    op.execute(sa.text(
        "DELETE FROM payroll.payitems "
        "WHERE payitemcode IN ('SYS_MIN_TOPUP', 'SYS_MAX_CAP') AND companyid IS NULL"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ux_driverpayrules_driver_type_openended"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ix_driverpayrules_driver_type_dates"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ix_driverpayrules_company_branch_status"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.driverpayrules DROP CONSTRAINT IF EXISTS excl_driverpayrules_no_date_overlap"
    ))
    op.execute(sa.text(
        "DROP TABLE IF EXISTS payroll.driverpayrules"
    ))
