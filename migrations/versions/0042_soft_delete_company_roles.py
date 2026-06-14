"""0042: Soft-delete (archive) support for company roles.

Adds isarchived + archivedat columns to sec.companyroles so that
role "deletion" becomes a reversible archive operation rather than
a physical row removal.  Historical audit logs, assignments, and
payroll records continue to reference the role id without breakage.

Revision ID: 0042
Revises: 0041
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0042"
down_revision: str = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE sec.companyroles
            ADD COLUMN IF NOT EXISTS isarchived  BOOLEAN     NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS archivedat  TIMESTAMPTZ NULL
    """)
    # Partial index — fast lookup of archived rows for admin tooling
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_companyroles_archived
            ON sec.companyroles (companyid, isarchived)
            WHERE isarchived = TRUE
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_companyroles_archived")
    op.execute("""
        ALTER TABLE sec.companyroles
            DROP COLUMN IF EXISTS archivedat,
            DROP COLUMN IF EXISTS isarchived
    """)
