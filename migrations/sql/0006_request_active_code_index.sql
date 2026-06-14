-- =============================================================================
-- Migration 0006: Partial unique index on CustomPayItemRequests
--
-- Prevents two active (PendingApproval or Approved) requests for the same
-- PayItemCode within the same company from existing simultaneously.
--
-- This is the DB-level backstop for the race condition where two concurrent
-- requests both pass the service-level _block_if_code_taken check before
-- either INSERT commits.
-- =============================================================================

CREATE UNIQUE INDEX ux_CustomPayItemRequests_ActiveCode
    ON payroll.CustomPayItemRequests (CompanyID, PayItemCode)
    WHERE (Status = 'PendingApproval' OR Status = 'Approved');
