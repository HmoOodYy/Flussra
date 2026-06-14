-- =============================================================================
-- Migration 0028: DriverTransferRequests — composite FK integrity
--
-- Adds company-composite FK constraints to core.DriverTransferRequests so that
-- cross-company inconsistency is impossible at the DB level.
--
-- Requires:
--   ux_Branches_ID_Company      (BranchID, CompanyID) — added in 0025
--   ux_Drivers_ID_Company       (DriverID, CompanyID) — added here
--
-- All constraints use NOT VALID: existing rows are not scanned;
-- enforced going forward on INSERT/UPDATE.
-- =============================================================================

-- Support index needed as FK target for (DriverID, CompanyID)
-- ux_Drivers_ID_Company_Branch already exists on (DriverID, CompanyID, BranchID)
-- but PostgreSQL FK targets require the exact column list, so we add this index.
CREATE UNIQUE INDEX IF NOT EXISTS ux_Drivers_ID_Company
    ON core.Drivers (DriverID, CompanyID);

-- (SourceBranchID, CompanyID) must reference a real Branches row in same company
ALTER TABLE core.DriverTransferRequests
    ADD CONSTRAINT fk_DTR_SourceBranch_Company
    FOREIGN KEY (SourceBranchID, CompanyID)
    REFERENCES core.Branches (BranchID, CompanyID)
    NOT VALID;

-- (TargetBranchID, CompanyID) must reference a real Branches row in same company
ALTER TABLE core.DriverTransferRequests
    ADD CONSTRAINT fk_DTR_TargetBranch_Company
    FOREIGN KEY (TargetBranchID, CompanyID)
    REFERENCES core.Branches (BranchID, CompanyID)
    NOT VALID;

-- (DriverID, CompanyID) must reference a real Driver in same company
ALTER TABLE core.DriverTransferRequests
    ADD CONSTRAINT fk_DTR_Driver_Company
    FOREIGN KEY (DriverID, CompanyID)
    REFERENCES core.Drivers (DriverID, CompanyID)
    NOT VALID;
