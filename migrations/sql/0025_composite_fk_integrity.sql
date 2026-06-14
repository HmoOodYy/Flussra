-- =============================================================================
-- Migration 0025: Composite FK integrity for payroll tables
--
-- Problem:
--   PayrollDraftLines, PayrollFinalLines, DriverRates, and DriverPayRules all
--   carry their own CompanyID and BranchID columns alongside FKs to Periods
--   and Drivers — but those individual FKs allow mismatched combinations, e.g.
--   a DraftLine whose BranchID disagrees with its Period's BranchID, or a
--   DriverRate whose BranchID disagrees with the Driver's BranchID.
--
-- Solution:
--   1. Add supporting UNIQUE indexes on parent tables (safe — each is a strict
--      superset of the existing PRIMARY KEY, so all existing rows satisfy them
--      trivially).
--   2. Add composite FK constraints on the four child tables using NOT VALID so
--      existing historical rows are not scanned.  The constraints enforce going
--      forward (INSERT / UPDATE).
--
-- NOT VALID rationale:
--   Production data pre-migration is assumed clean; DO NOT delete data to make
--   constraints pass (project rule).  A DBA may run VALIDATE CONSTRAINT on each
--   at a maintenance window once data quality is confirmed.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Step 1 — Supporting UNIQUE indexes on parent tables
-- ---------------------------------------------------------------------------

-- core.Branches: trivially unique because BranchID is PK; index lets child
-- tables reference (BranchID, CompanyID) as a composite FK target.
CREATE UNIQUE INDEX IF NOT EXISTS ux_Branches_ID_Company
    ON core.Branches (BranchID, CompanyID);

-- core.Drivers: trivially unique because DriverID is PK.
CREATE UNIQUE INDEX IF NOT EXISTS ux_Drivers_ID_Company_Branch
    ON core.Drivers (DriverID, CompanyID, BranchID);

-- payroll.PayrollPeriods: trivially unique because PayrollPeriodID is PK.
CREATE UNIQUE INDEX IF NOT EXISTS ux_PayrollPeriods_ID_Company_Branch
    ON payroll.PayrollPeriods (PayrollPeriodID, CompanyID, BranchID);

-- ---------------------------------------------------------------------------
-- Step 2 — Composite FK constraints on PayrollDraftLines (NOT VALID)
-- ---------------------------------------------------------------------------

-- Ensures the period referenced by this line belongs to the same
-- company and branch that the line itself claims.
ALTER TABLE payroll.PayrollDraftLines
    ADD CONSTRAINT fk_DraftLines_Period_Company_Branch
    FOREIGN KEY (PayrollPeriodID, CompanyID, BranchID)
    REFERENCES payroll.PayrollPeriods (PayrollPeriodID, CompanyID, BranchID)
    NOT VALID;

-- Ensures the driver referenced by this line belongs to the same
-- company and branch that the line itself claims.
ALTER TABLE payroll.PayrollDraftLines
    ADD CONSTRAINT fk_DraftLines_Driver_Company_Branch
    FOREIGN KEY (DriverID, CompanyID, BranchID)
    REFERENCES core.Drivers (DriverID, CompanyID, BranchID)
    NOT VALID;

-- ---------------------------------------------------------------------------
-- Step 3 — Composite FK constraints on PayrollFinalLines (NOT VALID)
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.PayrollFinalLines
    ADD CONSTRAINT fk_FinalLines_Period_Company_Branch
    FOREIGN KEY (PayrollPeriodID, CompanyID, BranchID)
    REFERENCES payroll.PayrollPeriods (PayrollPeriodID, CompanyID, BranchID)
    NOT VALID;

ALTER TABLE payroll.PayrollFinalLines
    ADD CONSTRAINT fk_FinalLines_Driver_Company_Branch
    FOREIGN KEY (DriverID, CompanyID, BranchID)
    REFERENCES core.Drivers (DriverID, CompanyID, BranchID)
    NOT VALID;

-- ---------------------------------------------------------------------------
-- Step 4 — Composite FK constraints on DriverRates (NOT VALID)
-- ---------------------------------------------------------------------------

-- Ensures a rate's CompanyID/BranchID match the driver it belongs to.
ALTER TABLE payroll.DriverRates
    ADD CONSTRAINT fk_DriverRates_Driver_Company_Branch
    FOREIGN KEY (DriverID, CompanyID, BranchID)
    REFERENCES core.Drivers (DriverID, CompanyID, BranchID)
    NOT VALID;

-- ---------------------------------------------------------------------------
-- Step 5 — Composite FK constraints on DriverPayRules (NOT VALID)
-- ---------------------------------------------------------------------------

-- Ensures a pay rule's CompanyID/BranchID match the driver it belongs to.
ALTER TABLE payroll.DriverPayRules
    ADD CONSTRAINT fk_DriverPayRules_Driver_Company_Branch
    FOREIGN KEY (DriverID, CompanyID, BranchID)
    REFERENCES core.Drivers (DriverID, CompanyID, BranchID)
    NOT VALID;
