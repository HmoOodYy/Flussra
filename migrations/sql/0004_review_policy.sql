-- =============================================================================
-- 0004: Review policy - AllowSelfApproval company setting
--
-- Adds a company-level flag that controls whether the same user who submitted
-- a review item is allowed to record a substantive decision (Approved, Rejected,
-- EditRequested) on it.
--
-- Default TRUE preserves the existing behaviour for all current companies.
-- Set to FALSE to require separation of duties in the review workflow.
-- =============================================================================

ALTER TABLE core.Companies
    ADD COLUMN IF NOT EXISTS AllowSelfApproval BOOLEAN NOT NULL DEFAULT TRUE;

COMMENT ON COLUMN core.Companies.AllowSelfApproval IS
    'When FALSE the user who submitted a review item cannot approve, reject, '
    'or request edits on it.  Comment decisions are always allowed.  '
    'Defaults to TRUE to preserve existing behaviour.';
