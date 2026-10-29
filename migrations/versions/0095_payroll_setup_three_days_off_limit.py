"""Raise the Normal Days Off product limit on Payroll Setup Versions to three days.

Revision ID: 0095
Revises: 0094
"""
import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0095"
down_revision: str | None = "0094"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0095_payroll_setup_three_days_off_limit.sql"
_MIGRATIONS_DIR = Path(__file__).parent.parent
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file  # noqa: E402

# Restoring the two-day limit would reject any live Draft or Published Version that
# already uses three days off, so the downgrade fails closed instead of rewriting rows.
_DOWNGRADE_PRECHECK = """
DO $$
DECLARE
    v_offenders TEXT;
BEGIN
    SELECT string_agg(
        format(
            'PayrollSetupVersionID=%s CompanyID=%s PayrollSetupID=%s LifecycleState=%s VersionNumber=%s NormalDaysOffMask=%s',
            PayrollSetupVersionID, CompanyID, PayrollSetupID, LifecycleState,
            COALESCE(VersionNumber::text, 'NULL'), NormalDaysOffMask
        ),
        '; ' ORDER BY PayrollSetupVersionID
    )
    INTO v_offenders
    FROM payroll.PayrollSetupVersions
    WHERE DiscardedAtUtc IS NULL
      AND NormalDaysOffMask IS NOT NULL
      AND length(replace((NormalDaysOffMask::integer)::bit(7)::text, '0', '')) > 2;

    IF v_offenders IS NOT NULL THEN
        RAISE EXCEPTION 'payroll_setup_days_off_limit_downgrade_violation: %', v_offenders
            USING HINT = 'Edit or discard Drafts that use three normal days off, and remediate '
                'Published Versions only through a separately reviewed data migration, before '
                'downgrading.';
    END IF;
END $$
"""


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    op.execute(sa.text(_DOWNGRADE_PRECHECK))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollSetupVersions DROP CONSTRAINT ck_PayrollSetupVersions_Mask"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollSetupVersions ADD CONSTRAINT ck_PayrollSetupVersions_Mask "
        "CHECK (NormalDaysOffMask IS NULL OR (NormalDaysOffMask BETWEEN 0 AND 127 "
        "AND (DiscardedAtUtc IS NOT NULL "
        "OR length(replace((NormalDaysOffMask::integer)::bit(7)::text, '0', '')) <= 2)))"
    ))
