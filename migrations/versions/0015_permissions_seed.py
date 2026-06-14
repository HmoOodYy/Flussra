"""0015 - Expand sec.Permissions with full company-role permission set.

Inserts 29 permission codes (company, roles, users, payroll, pay items,
pay rates, drivers, dispatch, reports, settings).

All inserts use ON CONFLICT DO NOTHING so the migration is idempotent and
safe alongside existing seeds that already inserted a subset of these codes
(e.g. setup.manage, drivers.manage from ensure_dev_admin.py).

Revision ID: 0015
Revises:     0014
"""
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0015_permissions_seed.sql"


def upgrade() -> None:
    # Single multi-row INSERT ... ON CONFLICT DO NOTHING — safe to execute as one call.
    op.execute(sa.text(_SQL_FILE.read_text(encoding="utf-8")))


def downgrade() -> None:
    # Remove only the NEW codes added by this migration.
    # Legacy codes (setup.manage, drivers.manage, etc.) that were already in
    # the database are left untouched.
    new_codes = [
        "company.view", "company.edit",
        "branches.view", "branches.create", "branches.edit",
        "roles.view", "roles.create", "roles.edit", "roles.delete",
        "users.view", "users.create", "users.edit", "users.deactivate",
        "payroll.view", "payroll.edit", "payroll.approve", "payroll.finalize",
        "payitems.view", "payitems.edit",
        "payrates.view", "payrates.edit",
        "drivers.view", "drivers.create", "drivers.edit",
        "dispatch.view", "dispatch.edit",
        "reports.view",
        "settings.view", "settings.manage",
    ]
    in_clause = ", ".join(f"'{c}'" for c in new_codes)
    op.execute(sa.text(
        f"DELETE FROM sec.permissions WHERE permissioncode IN ({in_clause})"
    ))
