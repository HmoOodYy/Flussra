-- =============================================================================
-- Migration 0026: Parent-root branch/company integrity
--
-- Problem (P1 from code review):
--   core.Drivers and payroll.PayrollPeriods each have individual FKs to
--   core.Branches(BranchID) and core.Companies(CompanyID) separately, but
--   nothing prevents a Driver or Period from combining a branch from Company A
--   with a company_id of Company B.
--
-- Solution:
--   Add composite FK constraints so that Drivers(BranchID, CompanyID) and
--   PayrollPeriods(BranchID, CompanyID) must match an existing row in
--   core.Branches(BranchID, CompanyID).
--
--   The supporting UNIQUE index ux_Branches_ID_Company was already created
--   in migration 0025 so no new index is needed on the parent.
--
-- Both constraints are added NOT VALID (same rationale as 0025):
--   existing rows are not scanned; the constraints enforce going forward.
-- =============================================================================

-- Drivers(BranchID, CompanyID) must match core.Branches(BranchID, CompanyID)
ALTER TABLE core.Drivers
    ADD CONSTRAINT fk_Drivers_Branch_Company
    FOREIGN KEY (BranchID, CompanyID)
    REFERENCES core.Branches (BranchID, CompanyID)
    NOT VALID;

-- PayrollPeriods(BranchID, CompanyID) must match core.Branches(BranchID, CompanyID)
ALTER TABLE payroll.PayrollPeriods
    ADD CONSTRAINT fk_PayrollPeriods_Branch_Company
    FOREIGN KEY (BranchID, CompanyID)
    REFERENCES core.Branches (BranchID, CompanyID)
    NOT VALID;
