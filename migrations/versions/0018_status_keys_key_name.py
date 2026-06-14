"""0018 - Add KeyName + usage-limit columns to payroll.PayrollStatusKeys.

Two groups of changes:

1.  KeyName (VARCHAR 200):
    User-facing label — what the user types ("Vacation", "Sick Day").
    StatusCode is now auto-generated (SK_XXXXXXXX) by the service layer;
    it is no longer entered by the user.
    Existing rows are backfilled: KeyName = StatusCode.

2.  Usage-limit columns (8 columns):
    LimitUsesPerPeriodEnabled / LimitUsesPerPeriod
    LimitUsesPerDriverEnabled / LimitUsesPerDriver
    LimitUsesAcrossDriversEnabled / LimitUsesAcrossDrivers
    LimitUsesPerDayEnabled  / LimitUsesPerDay

    Each pair: enabled (boolean) + value (nullable integer).
    DB-level CHECK constraints ensure: if enabled = TRUE then value > 0.
    Enforcement at the payroll-line level is deferred to a future migration.

Revision ID: 0018
Revises:     0017
"""
from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. KeyName column — temporary default so NOT NULL works on existing rows
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollStatusKeys "
        "ADD COLUMN KeyName VARCHAR(200) NOT NULL DEFAULT ''"
    ))
    op.execute(sa.text(
        "UPDATE payroll.PayrollStatusKeys SET KeyName = StatusCode"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollStatusKeys "
        "ALTER COLUMN KeyName DROP DEFAULT"
    ))

    # 2. Usage-limit columns
    op.execute(sa.text("""
        ALTER TABLE payroll.PayrollStatusKeys
        ADD COLUMN LimitUsesPerPeriodEnabled     BOOLEAN NOT NULL DEFAULT FALSE,
        ADD COLUMN LimitUsesPerPeriod            INTEGER,
        ADD COLUMN LimitUsesPerDriverEnabled     BOOLEAN NOT NULL DEFAULT FALSE,
        ADD COLUMN LimitUsesPerDriver            INTEGER,
        ADD COLUMN LimitUsesAcrossDriversEnabled BOOLEAN NOT NULL DEFAULT FALSE,
        ADD COLUMN LimitUsesAcrossDrivers        INTEGER,
        ADD COLUMN LimitUsesPerDayEnabled        BOOLEAN NOT NULL DEFAULT FALSE,
        ADD COLUMN LimitUsesPerDay               INTEGER
    """))

    # 3. Check constraints: enabled=TRUE requires value > 0
    op.execute(sa.text("""
        ALTER TABLE payroll.PayrollStatusKeys
        ADD CONSTRAINT ck_StatusKeys_LimitPerPeriod
            CHECK (LimitUsesPerPeriodEnabled = FALSE
                OR (LimitUsesPerPeriod IS NOT NULL AND LimitUsesPerPeriod > 0)),
        ADD CONSTRAINT ck_StatusKeys_LimitPerDriver
            CHECK (LimitUsesPerDriverEnabled = FALSE
                OR (LimitUsesPerDriver IS NOT NULL AND LimitUsesPerDriver > 0)),
        ADD CONSTRAINT ck_StatusKeys_LimitAcrossDrivers
            CHECK (LimitUsesAcrossDriversEnabled = FALSE
                OR (LimitUsesAcrossDrivers IS NOT NULL AND LimitUsesAcrossDrivers > 0)),
        ADD CONSTRAINT ck_StatusKeys_LimitPerDay
            CHECK (LimitUsesPerDayEnabled = FALSE
                OR (LimitUsesPerDay IS NOT NULL AND LimitUsesPerDay > 0))
    """))

    # 4. Index on KeyName for fast sort
    op.execute(sa.text("""
        CREATE INDEX ix_PayrollStatusKeys_KeyName
            ON payroll.PayrollStatusKeys (CompanyID, BranchID, KeyName)
            WHERE IsActive = TRUE
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_PayrollStatusKeys_KeyName"))
    op.execute(sa.text("""
        ALTER TABLE payroll.PayrollStatusKeys
        DROP CONSTRAINT IF EXISTS ck_StatusKeys_LimitPerPeriod,
        DROP CONSTRAINT IF EXISTS ck_StatusKeys_LimitPerDriver,
        DROP CONSTRAINT IF EXISTS ck_StatusKeys_LimitAcrossDrivers,
        DROP CONSTRAINT IF EXISTS ck_StatusKeys_LimitPerDay
    """))
    op.execute(sa.text("""
        ALTER TABLE payroll.PayrollStatusKeys
        DROP COLUMN IF EXISTS LimitUsesPerDayEnabled,
        DROP COLUMN IF EXISTS LimitUsesPerDay,
        DROP COLUMN IF EXISTS LimitUsesAcrossDriversEnabled,
        DROP COLUMN IF EXISTS LimitUsesAcrossDrivers,
        DROP COLUMN IF EXISTS LimitUsesPerDriverEnabled,
        DROP COLUMN IF EXISTS LimitUsesPerDriver,
        DROP COLUMN IF EXISTS LimitUsesPerPeriodEnabled,
        DROP COLUMN IF EXISTS LimitUsesPerPeriod
    """))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollStatusKeys DROP COLUMN IF EXISTS KeyName"
    ))
