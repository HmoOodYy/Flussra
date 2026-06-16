-- =============================================================================
-- 0045: CDPI branch display-name override
--
-- Adds an optional branch-level display-name column to BranchPayItemConfig.
--
-- Motivation:
--   CDPI branch controls (Task 7) allow each branch to override the display
--   label for an approved/direct-created CDPI PayItem without changing the
--   company-level PayItems.Name or any other structural field.
--   Storing the override in BranchPayItemConfig keeps it naturally versioned
--   with the existing effective-dated active/inactive config rows.
--
-- Column:
--   BranchDisplayName  VARCHAR(200)  NULL
--   - NULL means no override; the branch falls back to PayItems.Name.
--   - Non-NULL is the branch-specific display label (trimmed by the service).
--   - The column is nullable so existing rows (inserted before 0045) remain
--     valid without requiring a data migration.
--
-- No index needed: lookups are always by PayItemID + BranchID + date range,
-- which is already covered by ix_BranchPayItemConfig_Branch_Item_Dates.
-- =============================================================================

ALTER TABLE payroll.BranchPayItemConfig
    ADD COLUMN IF NOT EXISTS BranchDisplayName VARCHAR(200) NULL;
