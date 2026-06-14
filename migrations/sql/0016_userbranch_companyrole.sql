-- =============================================================================
-- 0016_userbranch_companyrole.sql
--
-- Three changes to wire up the company-role system:
--
-- 1. Make RoleID nullable on sec.UserBranchRoles
--    New-path assignments reference CompanyRoleID instead of RoleID.
--    During the transition both columns are populated for new assignments so
--    both the old permission function (RolePermissions) and the new one
--    (CompanyRolePermissions) work simultaneously.
--
-- 2. Add CompanyRoleID (nullable) to sec.UserBranchRoles + backfill
--    For any existing rows (production backfill, no-op on empty test DB):
--      a. Create COMPANY_OWNER + DRIVER defaults for each company.
--      b. Mirror each distinct global role used in UserBranchRoles as a
--         custom CompanyRole and copy its RolePermissions.
--      c. Backfill CompanyRoleID on all existing UserBranchRoles rows.
--
-- 3. Update sec.fn_UserHasPermission and app.vw_UserBranchAccess
--    The function now checks CompanyRolePermissions first, then falls back
--    to the legacy RolePermissions path.  The view uses LEFT JOINs so that
--    rows with either (RoleID) or (CompanyRoleID) or both are returned.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Make RoleID nullable
-- ---------------------------------------------------------------------------

ALTER TABLE sec.UserBranchRoles
    ALTER COLUMN RoleID DROP NOT NULL;

-- ---------------------------------------------------------------------------
-- 2a. Add CompanyRoleID column (nullable during transition)
-- ---------------------------------------------------------------------------

ALTER TABLE sec.UserBranchRoles
    ADD COLUMN CompanyRoleID INTEGER
    REFERENCES sec.CompanyRoles(CompanyRoleID);

CREATE INDEX ix_UserBranchRoles_CompanyRoleID
    ON sec.UserBranchRoles (CompanyRoleID)
    WHERE CompanyRoleID IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2b. Backfill: seed defaults + link existing rows (no-op on empty DB)
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    v_company   RECORD;
    v_role      RECORD;
    v_cr_id     INTEGER;
    v_all_perms TEXT[];
    v_pcode     TEXT;
BEGIN
    FOR v_company IN SELECT companyid FROM core.companies LOOP

        -- -- COMPANY_OWNER (protected default, all permissions) --------------
        INSERT INTO sec.companyroles
            (companyid, rolecode, rolename, rolelevel,
             isdefault, isprotected, iscustom, isactive)
        VALUES
            (v_company.companyid, 'COMPANY_OWNER', 'Company Owner', 100,
             TRUE, TRUE, FALSE, TRUE)
        ON CONFLICT (companyid, rolecode) DO NOTHING;

        SELECT companyroleid INTO v_cr_id
        FROM   sec.companyroles
        WHERE  companyid = v_company.companyid AND rolecode = 'COMPANY_OWNER';

        -- Grant all known permissions to Company Owner
        INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
        SELECT v_cr_id, permissioncode
        FROM   sec.permissions
        ON CONFLICT (companyroleid, permissioncode) DO NOTHING;

        -- -- DRIVER (protected default, minimal permissions) -----------------
        INSERT INTO sec.companyroles
            (companyid, rolecode, rolename, rolelevel,
             isdefault, isprotected, iscustom, isactive)
        VALUES
            (v_company.companyid, 'DRIVER', 'Driver', 10,
             TRUE, TRUE, FALSE, TRUE)
        ON CONFLICT (companyid, rolecode) DO NOTHING;

        SELECT companyroleid INTO v_cr_id
        FROM   sec.companyroles
        WHERE  companyid = v_company.companyid AND rolecode = 'DRIVER';

        -- Driver gets only drivers.view by default
        INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
        VALUES (v_cr_id, 'drivers.view')
        ON CONFLICT (companyroleid, permissioncode) DO NOTHING;

        -- -- Mirror existing global roles used by this company ---------------
        FOR v_role IN
            SELECT DISTINCT r.roleid, r.rolecode, r.rolename, r.rolelevel
            FROM   sec.userbranchroles ubr
            JOIN   sec.roles r ON r.roleid = ubr.roleid
            WHERE  ubr.companyid = v_company.companyid
        LOOP
            INSERT INTO sec.companyroles
                (companyid, rolecode, rolename, rolelevel,
                 isdefault, isprotected, iscustom, isactive)
            VALUES
                (v_company.companyid, v_role.rolecode, v_role.rolename,
                 v_role.rolelevel, FALSE, FALSE, TRUE, TRUE)
            ON CONFLICT (companyid, rolecode) DO NOTHING;

            SELECT companyroleid INTO v_cr_id
            FROM   sec.companyroles
            WHERE  companyid = v_company.companyid
              AND  rolecode  = v_role.rolecode;

            -- Copy permissions from sec.RolePermissions -> CompanyRolePermissions
            INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
            SELECT v_cr_id, p.permissioncode
            FROM   sec.rolepermissions rp
            JOIN   sec.permissions p ON p.permissionid = rp.permissionid
            WHERE  rp.roleid = v_role.roleid
            ON CONFLICT (companyroleid, permissioncode) DO NOTHING;
        END LOOP;

    END LOOP;
END $$;

-- 2c. Backfill CompanyRoleID on existing UserBranchRoles rows
-- NOTE: In PostgreSQL UPDATE ... FROM, the target table alias (ubr) cannot
-- be referenced inside FROM JOIN conditions.  Use WHERE instead.
UPDATE sec.userbranchroles ubr
SET    companyroleId = cr.companyroleid
FROM   sec.roles r,
       sec.companyroles cr
WHERE  ubr.roleid        = r.roleid
  AND  cr.companyid      = ubr.companyid
  AND  cr.rolecode       = r.rolecode
  AND  ubr.companyroleId IS NULL;

-- ---------------------------------------------------------------------------
-- 3a. Update sec.fn_UserHasPermission - dual-path check
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION sec.fn_UserHasPermission(
    p_UserID         INTEGER,
    p_CompanyID      INTEGER,
    p_BranchID       INTEGER,
    p_PermissionCode VARCHAR(100)
) RETURNS BOOLEAN
LANGUAGE plpgsql STABLE AS $$
BEGIN
    -- -- New path: CompanyRolePermissions (company-scoped roles) -------------
    -- Checked first; covers all users whose assignments have CompanyRoleID set.
    IF EXISTS (
        SELECT 1
        FROM   sec.userbranchroles         AS ubr
        JOIN   sec.companyrolepermissions  AS crp
               ON  crp.companyroleid  = ubr.companyroleId
        WHERE  ubr.userid          = p_UserID
          AND  ubr.companyid       = p_CompanyID
          AND  ubr.isactive        = TRUE
          AND  ubr.companyroleId  IS NOT NULL
          AND  crp.permissioncode  = p_PermissionCode
          AND  (
               ubr.scopetype = 'AllCompanyBranches'
            OR ubr.branchid  = p_BranchID
               )
    ) THEN
        RETURN TRUE;
    END IF;

    -- -- Legacy path: global RolePermissions ---------------------------------
    -- Kept for backward compatibility during the transition period.
    -- Covers existing assignments that do not yet have CompanyRoleID set.
    RETURN EXISTS (
        SELECT 1
        FROM   sec.userbranchroles AS ubr
        JOIN   sec.rolepermissions AS rp
               ON  rp.roleid        = ubr.roleid
        JOIN   sec.permissions     AS perm
               ON  perm.permissionid = rp.permissionid
        WHERE  ubr.userid          = p_UserID
          AND  ubr.companyid       = p_CompanyID
          AND  ubr.isactive        = TRUE
          AND  ubr.roleid         IS NOT NULL
          AND  perm.permissioncode = p_PermissionCode
          AND  (
               ubr.scopetype = 'AllCompanyBranches'
            OR ubr.branchid  = p_BranchID
               )
    );
END;
$$;

-- ---------------------------------------------------------------------------
-- 3b. Update app.vw_UserBranchAccess - handle nullable RoleID
--
-- Must DROP + recreate (not CREATE OR REPLACE) because the column type of
-- RoleCode would change (VARCHAR(50) -> VARCHAR) due to COALESCE widening.
-- vw_UserBranchAccess has no dependent views, so CASCADE is safe.
-- ---------------------------------------------------------------------------

DROP VIEW IF EXISTS app.vw_UserBranchAccess CASCADE;

CREATE VIEW app.vw_UserBranchAccess AS
SELECT
    u.UserID,
    u.Username,
    u.DisplayName,
    c.CompanyID,
    c.CompanyCode,
    c.CompanyName,
    ubr.ScopeType,
    b.BranchID,
    b.BranchCode,
    b.BranchName,
    r.RoleID,
    COALESCE(r.RoleCode, cr.RoleCode)::varchar(80) AS RoleCode,
    COALESCE(r.RoleName, cr.RoleName)::varchar(120) AS RoleName,
    u.IsActive             AS UserIsActive,
    u.CanLogin,
    c.Status               AS CompanyStatus,
    c.IsSuspended          AS CompanyIsSuspended,
    b.Status               AS BranchStatus,
    ubr.IsActive           AS AccessIsActive
FROM sec.UserBranchRoles  AS ubr
INNER JOIN sec.Users       AS u  ON u.UserID    = ubr.UserID
INNER JOIN core.Companies  AS c  ON c.CompanyID = ubr.CompanyID
LEFT  JOIN core.Branches   AS b  ON b.BranchID  = ubr.BranchID
LEFT  JOIN sec.Roles       AS r  ON r.RoleID     = ubr.RoleID
LEFT  JOIN sec.CompanyRoles AS cr ON cr.CompanyRoleID = ubr.CompanyRoleID;
