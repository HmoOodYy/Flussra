-- =============================================================================
-- Migration 0002: Safety Indexes
-- =============================================================================
--
-- Issue 6: Duplicate active role assignments under concurrent requests
-- ------------------------------------------------------------------
-- The app-layer soft-check (SELECT then INSERT) has a TOCTOU window.
-- Two partial unique indexes enforce the invariant at the DB level so
-- a concurrent INSERT raising IntegrityError is caught and translated
-- to a clean 422 in the service layer.
--
-- Two indexes are needed because PostgreSQL partial indexes cannot mix
-- IS NULL and IS NOT NULL in the same index predicate cleanly:
--   - AllCompanyBranches assignments have BranchID IS NULL
--   - SpecificBranch / OwnDriverDataOnly have BranchID IS NOT NULL
--
-- Issue 7: Default branch uniqueness not DB-backed
-- ------------------------------------------------
-- The "clear existing defaults then set new default" sequence in the
-- app layer is correct in serial but has a race window under concurrency.
-- A partial unique index enforces at most one IsDefault=TRUE row per
-- company in the database, regardless of how many concurrent requests
-- race to set a new default.

-- ---------------------------------------------------------------------------
-- Active role assignments: no duplicate (user, company, role, scope) combos
-- ---------------------------------------------------------------------------

-- Company-wide scope: BranchID is always NULL
CREATE UNIQUE INDEX ux_UserBranchRoles_Active_AllCompany
    ON sec.UserBranchRoles (UserID, CompanyID, RoleID, ScopeType)
    WHERE IsActive = TRUE AND BranchID IS NULL;

-- Branch-specific scope: BranchID is always NOT NULL
CREATE UNIQUE INDEX ux_UserBranchRoles_Active_Branch
    ON sec.UserBranchRoles (UserID, CompanyID, RoleID, ScopeType, BranchID)
    WHERE IsActive = TRUE AND BranchID IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Default branch: at most one IsDefault=TRUE per company
-- ---------------------------------------------------------------------------

CREATE UNIQUE INDEX ux_Branches_Company_Default
    ON core.Branches (CompanyID)
    WHERE IsDefault = TRUE;
