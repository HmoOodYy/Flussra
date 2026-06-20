-- =============================================================================
-- 0048: Returned-for-Correction lifecycle
--
-- Adds the Returned period status, the CurrentReturnReviewItemID pointer,
-- and the one-Returned-per-branch partial unique index.
--
-- Schema changes:
--   1. payroll.PayrollPeriods.CurrentReturnReviewItemID BIGINT NULL
--   2. Unique index on review.ManagerReviewItems(ReviewItemID, CompanyID, BranchID)
--      - required as the referenced side of the composite FK.
--   3. Composite FK: PayrollPeriods(CurrentReturnReviewItemID, CompanyID, BranchID)
--      -> ManagerReviewItems(ReviewItemID, CompanyID, BranchID).
--   4. Replace ck_PayrollPeriods_Status to include 'Returned'.
--   5. Consistency CHECK: Status='Returned' iff CurrentReturnReviewItemID IS NOT NULL.
--   6. Preflight: refuse if any company/branch already has multiple Returned periods.
--   7. Partial unique index ux_PayrollPeriods_OneReturnedPerBranch
--      ON (CompanyID, BranchID) WHERE Status = 'Returned'.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. Add CurrentReturnReviewItemID column
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.PayrollPeriods
    ADD COLUMN CurrentReturnReviewItemID BIGINT NULL;


-- ---------------------------------------------------------------------------
-- 2. Unique index on ManagerReviewItems to support the composite FK
--    ReviewItemID is the PK (already unique), so (ReviewItemID, CompanyID,
--    BranchID) is trivially unique -- but PostgreSQL requires an explicit
--    unique constraint as the referenced side of a multi-column FK.
-- ---------------------------------------------------------------------------

CREATE UNIQUE INDEX ux_ManagerReviewItems_ReviewItemID_Company_Branch
    ON review.ManagerReviewItems (ReviewItemID, CompanyID, BranchID);


-- ---------------------------------------------------------------------------
-- 3. Composite FK: PayrollPeriods -> ManagerReviewItems
--    Ensures the review item belongs to the same company and branch as the
--    period, preventing cross-company / cross-branch pointer corruption.
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.PayrollPeriods
    ADD CONSTRAINT fk_PayrollPeriods_CurrentReturnReviewItem
    FOREIGN KEY (CurrentReturnReviewItemID, CompanyID, BranchID)
    REFERENCES review.ManagerReviewItems (ReviewItemID, CompanyID, BranchID);


-- ---------------------------------------------------------------------------
-- 4. Replace ck_PayrollPeriods_Status to add 'Returned'
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.PayrollPeriods
    DROP CONSTRAINT ck_PayrollPeriods_Status;

ALTER TABLE payroll.PayrollPeriods
    ADD CONSTRAINT ck_PayrollPeriods_Status
    CHECK (Status IN ('Draft', 'Open', 'InReview', 'Returned', 'Approved', 'Locked', 'Archived', 'Cancelled'));


-- ---------------------------------------------------------------------------
-- 5. Pointer-consistency CHECK
--    Status = 'Returned'  iff  CurrentReturnReviewItemID IS NOT NULL.
--    Prevents orphaned Returned periods (pointer without status) and
--    dangling pointers (status without pointer).
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.PayrollPeriods
    ADD CONSTRAINT ck_PayrollPeriods_ReturnedPointerConsistency
    CHECK (
        (Status = 'Returned' AND CurrentReturnReviewItemID IS NOT NULL)
        OR
        (Status != 'Returned' AND CurrentReturnReviewItemID IS NULL)
    );


-- ---------------------------------------------------------------------------
-- 6. Preflight: reject migration if duplicate Returned periods already exist
--    (they would violate the partial unique index about to be created).
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    dup_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO dup_count
    FROM (
        SELECT CompanyID, BranchID
        FROM   payroll.PayrollPeriods
        WHERE  Status = 'Returned'
        GROUP  BY CompanyID, BranchID
        HAVING COUNT(*) > 1
    ) dups;

    IF dup_count > 0 THEN
        RAISE EXCEPTION
            'Migration 0048 preflight failed: % company/branch pair(s) already have '
            'multiple Returned periods. Resolve them before running this migration.',
            dup_count;
    END IF;
END $$;


-- ---------------------------------------------------------------------------
-- 7. Partial unique index: one Returned period per company/branch
--    This is the concurrency authority -- when two concurrent review returns
--    race, the losing INSERT/UPDATE fails here with a unique violation.
-- ---------------------------------------------------------------------------

CREATE UNIQUE INDEX ux_PayrollPeriods_OneReturnedPerBranch
    ON payroll.PayrollPeriods (CompanyID, BranchID)
    WHERE Status = 'Returned';
