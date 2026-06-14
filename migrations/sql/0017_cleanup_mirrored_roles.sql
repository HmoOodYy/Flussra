-- =============================================================================
-- 0017_cleanup_mirrored_roles.sql
--
-- Removes legacy global roles that were auto-mirrored into sec.companyroles
-- during migration 0016's DO block backfill.
--
-- Mirrored roles are identified by all three conditions:
--   iscustom = TRUE  AND  isprotected = FALSE  AND  isdefault = FALSE
--   AND rolecode IN (SELECT rolecode FROM sec.roles)
--
-- Step 1: Null out companyroleId on any userbranchroles rows that pointed
--         to the mirrored roles (legacy roleid stays intact).
-- Step 2: Delete the mirrored company-role rows.
-- =============================================================================

-- Step 1: Remove FK references before deleting the parent rows
UPDATE sec.userbranchroles
SET    companyroleId = NULL
WHERE  companyroleId IN (
    SELECT companyroleid
    FROM   sec.companyroles
    WHERE  iscustom    = TRUE
      AND  isprotected = FALSE
      AND  isdefault   = FALSE
      AND  rolecode IN (SELECT rolecode FROM sec.roles)
);

-- Step 2: Delete the auto-mirrored roles
DELETE FROM sec.companyroles
WHERE  iscustom    = TRUE
  AND  isprotected = FALSE
  AND  isdefault   = FALSE
  AND  rolecode IN (SELECT rolecode FROM sec.roles);
