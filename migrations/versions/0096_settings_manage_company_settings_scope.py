"""Narrow settings.manage to company settings and retire setup.manage.

Revision ID: 0096
Revises: 0095
"""
import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0096"
down_revision: str | None = "0095"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0096_settings_manage_company_settings_scope.sql"
_MIGRATIONS_DIR = Path(__file__).parent.parent
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    # Only the catalogue wording is restored. The specific permissions granted on upgrade
    # cannot be told apart from permissions assigned independently, so they stay: removing
    # them could revoke access that was granted on purpose, and the earlier release already
    # accepted holders of settings.manage on every route they cover. The retired
    # setup.manage grants were converted, not preserved, and are not recreated.
    op.execute(sa.text(
        "UPDATE sec.Permissions SET PermissionName = 'Manage Settings', Notes = NULL "
        "WHERE PermissionCode = 'settings.manage'"
    ))
