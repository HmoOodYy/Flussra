-- =============================================================================
-- 0050: Period creation candidate key column + partial unique index
--
-- Supports CP-1C branch-locked candidate-based period creation.
-- Each period created via the candidate path stores a deterministic HMAC-signed
-- hash so that replays return the same period and duplicate keys are rejected.
--
-- Schema changes:
--   1. payroll.PayrollPeriods.CreationCandidateKeyHash  VARCHAR(64) NULL
--   2. ux_payrollperiods_creationcandidatekey  partial unique index
--      ON (CompanyID, BranchID, CreationCandidateKeyHash) WHERE NOT NULL
--
-- Legacy periods created via POST /payroll/periods retain NULL for this column.
--
-- Downgrade:
--   Refuses if any non-NULL hashes are present (data would be lost).
--   On empty: drops the index, then drops the column.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. Add the CreationCandidateKeyHash column
-- ---------------------------------------------------------------------------
ALTER TABLE payroll.PayrollPeriods
    ADD COLUMN IF NOT EXISTS CreationCandidateKeyHash VARCHAR(64) NULL;


-- ---------------------------------------------------------------------------
-- 2. Partial unique index: (CompanyID, BranchID, CreationCandidateKeyHash)
--    WHERE CreationCandidateKeyHash IS NOT NULL
--    Guarantees that the same candidate key can only ever produce one period.
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS ux_payrollperiods_creationcandidatekey
    ON payroll.PayrollPeriods (CompanyID, BranchID, CreationCandidateKeyHash)
    WHERE CreationCandidateKeyHash IS NOT NULL;
