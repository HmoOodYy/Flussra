-- 0042: Soft-delete (archive) support for company roles.
-- Adds isarchived + archivedat columns to sec.companyroles so that
-- role "deletion" becomes a reversible archive operation rather than
-- a physical row removal.

ALTER TABLE sec.companyroles
    ADD COLUMN IF NOT EXISTS isarchived  BOOLEAN     NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS archivedat  TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS ix_companyroles_archived
    ON sec.companyroles (companyid, isarchived)
    WHERE isarchived = TRUE;
