-- =============================================================================
-- 0018_status_keys_key_name.sql
--
-- Adds the user-facing KeyName column to payroll.PayrollStatusKeys and eight
-- usage-limit columns.
--
-- Design:
--   KeyName  -- the human-readable label the user types ("Vacation", "Sick Day").
--              For existing rows the initial value mirrors StatusCode so old data
--              continues to display correctly.
--   StatusCode will now be auto-generated as SK_XXXXXXXX for new rows.
--
-- Usage limit columns let the admin cap how many times a status key can be
-- applied in various scopes.  Enforcement is stored but deferred (no payroll
-- line check yet).
-- =============================================================================

-- 1. Add KeyName column with a temporary DB-level default so NOT NULL is safe
--    on a table that already has rows.
ALTER TABLE payroll.PayrollStatusKeys
ADD COLUMN KeyName VARCHAR(200) NOT NULL DEFAULT '';

-- 2. Backfill: existing rows use their StatusCode as the display name.
UPDATE payroll.PayrollStatusKeys SET KeyName = StatusCode;

-- 3. Drop the default so the application layer must always supply a value.
ALTER TABLE payroll.PayrollStatusKeys
ALTER COLUMN KeyName DROP DEFAULT;

-- 4. Add usage-limit columns (all nullable; enabled=FALSE means limit not active).
ALTER TABLE payroll.PayrollStatusKeys
ADD COLUMN LimitUsesPerPeriodEnabled     BOOLEAN NOT NULL DEFAULT FALSE,
ADD COLUMN LimitUsesPerPeriod            INTEGER,
ADD COLUMN LimitUsesPerDriverEnabled     BOOLEAN NOT NULL DEFAULT FALSE,
ADD COLUMN LimitUsesPerDriver            INTEGER,
ADD COLUMN LimitUsesAcrossDriversEnabled BOOLEAN NOT NULL DEFAULT FALSE,
ADD COLUMN LimitUsesAcrossDrivers        INTEGER,
ADD COLUMN LimitUsesPerDayEnabled        BOOLEAN NOT NULL DEFAULT FALSE,
ADD COLUMN LimitUsesPerDay               INTEGER;

-- 5. Add a check: if a limit is enabled, its value must be a positive integer.
ALTER TABLE payroll.PayrollStatusKeys
ADD CONSTRAINT ck_StatusKeys_LimitPerPeriod
    CHECK (LimitUsesPerPeriodEnabled = FALSE OR (LimitUsesPerPeriod IS NOT NULL AND LimitUsesPerPeriod > 0)),
ADD CONSTRAINT ck_StatusKeys_LimitPerDriver
    CHECK (LimitUsesPerDriverEnabled = FALSE OR (LimitUsesPerDriver IS NOT NULL AND LimitUsesPerDriver > 0)),
ADD CONSTRAINT ck_StatusKeys_LimitAcrossDrivers
    CHECK (LimitUsesAcrossDriversEnabled = FALSE OR (LimitUsesAcrossDrivers IS NOT NULL AND LimitUsesAcrossDrivers > 0)),
ADD CONSTRAINT ck_StatusKeys_LimitPerDay
    CHECK (LimitUsesPerDayEnabled = FALSE OR (LimitUsesPerDay IS NOT NULL AND LimitUsesPerDay > 0));

-- 6. Index on KeyName for fast sort + name-collision checks.
CREATE INDEX ix_PayrollStatusKeys_KeyName
    ON payroll.PayrollStatusKeys (CompanyID, BranchID, KeyName)
    WHERE IsActive = TRUE;
