-- =============================================================================
-- Migration 0062: CP-4D submitted calculation snapshot link
--
-- CP-4D records the immutable calculation packet submitted with each new
-- PeriodApproval review item. Existing review history remains NULL.
-- =============================================================================

ALTER TABLE review.ManagerReviewItems
    ADD COLUMN PayrollCalculationSnapshotID BIGINT NULL;

ALTER TABLE review.ManagerReviewItems
    ADD CONSTRAINT uq_ManagerReviewItems_PayrollCalculationSnapshot
    UNIQUE (PayrollCalculationSnapshotID);

ALTER TABLE review.ManagerReviewItems
    ADD CONSTRAINT fk_ManagerReviewItems_CalculationSnapshot_Company_Branch
    FOREIGN KEY (PayrollCalculationSnapshotID, CompanyID, BranchID)
    REFERENCES payroll.PayrollCalculationSnapshots
        (PayrollCalculationSnapshotID, CompanyID, BranchID)
    ON DELETE RESTRICT;
