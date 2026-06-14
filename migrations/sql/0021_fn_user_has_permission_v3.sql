-- 0021: Update sec.fn_UserHasPermission to include:
--   A. Company Owner dynamic grant - any permission that exists in sec.Permissions
--   B. CompanyRolePermissions (new path, unchanged)
--   C. Legacy RolePermissions (backward compat, unchanged)
--   D. UserPermissionOverrides - ALLOW overrides granted directly to the user

CREATE OR REPLACE FUNCTION sec.fn_UserHasPermission(
    p_UserID         INTEGER,
    p_CompanyID      INTEGER,
    p_BranchID       INTEGER,
    p_PermissionCode VARCHAR(100)
) RETURNS BOOLEAN
LANGUAGE plpgsql STABLE AS $$
BEGIN
    -- Path A: Company Owner dynamic grant
    -- If the user holds an active COMPANY_OWNER company role, they have every
    -- permission that exists in the catalogue - no CompanyRolePermissions row
    -- required.  This makes new permissions instantly available to the owner.
    IF EXISTS (
        SELECT 1
        FROM   sec.userbranchroles ubr
        JOIN   sec.companyroles    cr ON cr.companyroleid = ubr.companyroleId
        WHERE  ubr.userid    = p_UserID
          AND  ubr.companyid = p_CompanyID
          AND  ubr.isactive  = TRUE
          AND  cr.rolecode   = 'COMPANY_OWNER'
    ) THEN
        RETURN EXISTS (
            SELECT 1 FROM sec.permissions WHERE permissioncode = p_PermissionCode
        );
    END IF;

    -- Path B: CompanyRolePermissions (new company-scoped path)
    IF EXISTS (
        SELECT 1
        FROM   sec.userbranchroles        AS ubr
        JOIN   sec.companyrolepermissions AS crp
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

    -- Path C: Legacy RolePermissions
    -- Kept for backward compatibility during the transition period.
    IF EXISTS (
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
    ) THEN
        RETURN TRUE;
    END IF;

    -- Path D: UserPermissionOverrides (member-specific ALLOW grants)
    RETURN EXISTS (
        SELECT 1
        FROM   sec.userpermissionoverrides AS upo
        WHERE  upo.userid         = p_UserID
          AND  upo.companyid      = p_CompanyID
          AND  upo.permissioncode = p_PermissionCode
          AND  upo.effect         = 'ALLOW'
          AND  upo.isactive       = TRUE
    );
END;
$$;
