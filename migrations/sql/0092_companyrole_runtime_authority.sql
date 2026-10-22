-- P8a: CompanyRole is the sole runtime role authority. Legacy catalog tables and
-- UserBranchRoles.RoleID remain untouched for the later catalog cleanup phase.
DROP VIEW IF EXISTS app.vw_UserBranchAccess;

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
    cr.RoleCode::varchar(80) AS RoleCode,
    cr.RoleName::varchar(120) AS RoleName,
    u.IsActive AS UserIsActive,
    u.CanLogin,
    c.Status AS CompanyStatus,
    c.IsSuspended AS CompanyIsSuspended,
    b.Status AS BranchStatus,
    ubr.IsActive AS AccessIsActive
FROM sec.UserBranchRoles ubr
JOIN sec.Users u ON u.UserID = ubr.UserID
JOIN core.Companies c ON c.CompanyID = ubr.CompanyID
JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = ubr.CompanyRoleID
LEFT JOIN core.Branches b ON b.BranchID = ubr.BranchID
WHERE ubr.CompanyRoleID IS NOT NULL
  AND ubr.ScopeType IN ('AllCompanyBranches', 'SpecificBranch');

CREATE OR REPLACE FUNCTION sec.fn_UserHasPermission(
    p_UserID INTEGER,
    p_CompanyID INTEGER,
    p_BranchID INTEGER,
    p_PermissionCode VARCHAR(100)
) RETURNS BOOLEAN
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM sec.Users u
        JOIN core.Companies c ON c.CompanyID = u.CompanyID
        WHERE u.UserID = p_UserID
          AND u.CompanyID = p_CompanyID
          AND u.IsActive
          AND u.CanLogin
          AND NOT u.IsStaged
          AND c.Status = 'Active'
          AND NOT c.IsSuspended
    ) THEN
        RETURN FALSE;
    END IF;

    -- Generic permissions have no resource identity for Self; DRIVER grants are
    -- constrained even if a CompanyRole is mistakenly given generic permissions.
    IF EXISTS (
        SELECT 1
        FROM sec.UserBranchRoles ubr
        JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = ubr.CompanyRoleID
        WHERE ubr.UserID = p_UserID
          AND ubr.CompanyID = p_CompanyID
          AND ubr.IsActive
          AND (ubr.ScopeType = 'Self' OR cr.RoleCode = 'DRIVER')
    ) THEN
        RETURN FALSE;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM sec.UserBranchRoles ubr
        JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = ubr.CompanyRoleID
        WHERE ubr.UserID = p_UserID
          AND ubr.CompanyID = p_CompanyID
          AND ubr.IsActive
          AND cr.RoleCode = 'COMPANY_OWNER'
          AND ubr.ScopeType = 'AllCompanyBranches'
          AND ubr.BranchID IS NULL
    ) THEN
        RETURN EXISTS (
            SELECT 1 FROM sec.Permissions p
            WHERE p.PermissionCode = p_PermissionCode
        );
    END IF;

    IF EXISTS (
        SELECT 1
        FROM sec.UserBranchRoles ubr
        JOIN sec.CompanyRolePermissions crp ON crp.CompanyRoleID = ubr.CompanyRoleID
        WHERE ubr.UserID = p_UserID
          AND ubr.CompanyID = p_CompanyID
          AND ubr.IsActive
          AND crp.PermissionCode = p_PermissionCode
          AND (
               (p_BranchID IS NULL AND ubr.ScopeType = 'AllCompanyBranches' AND ubr.BranchID IS NULL)
            OR (p_BranchID IS NOT NULL AND ubr.ScopeType = 'AllCompanyBranches' AND ubr.BranchID IS NULL)
            OR (p_BranchID IS NOT NULL AND ubr.ScopeType = 'SpecificBranch' AND ubr.BranchID = p_BranchID)
          )
    ) THEN
        RETURN TRUE;
    END IF;

    -- Overrides widen actions only inside a target CompanyRole assignment's scope.
    RETURN EXISTS (
        SELECT 1
        FROM sec.UserPermissionOverrides upo
        WHERE upo.UserID = p_UserID
          AND upo.CompanyID = p_CompanyID
          AND upo.PermissionCode = p_PermissionCode
          AND upo.Effect = 'ALLOW'
          AND upo.IsActive
          AND EXISTS (
              SELECT 1
              FROM sec.UserBranchRoles ubr
              WHERE ubr.UserID = p_UserID
                AND ubr.CompanyID = p_CompanyID
                AND ubr.IsActive
                AND ubr.CompanyRoleID IS NOT NULL
                AND (
                     (p_BranchID IS NULL AND ubr.ScopeType = 'AllCompanyBranches' AND ubr.BranchID IS NULL)
                  OR (p_BranchID IS NOT NULL AND ubr.ScopeType = 'AllCompanyBranches' AND ubr.BranchID IS NULL)
                  OR (p_BranchID IS NOT NULL AND ubr.ScopeType = 'SpecificBranch' AND ubr.BranchID = p_BranchID)
                )
          )
    );
END;
$$;

-- Self and protected-owner assignment validation uses CompanyRole identity only.
CREATE OR REPLACE FUNCTION sec.fn_enforce_userbranchrole_scope_authority()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_is_driver BOOLEAN;
    v_is_owner BOOLEAN;
BEGIN
    SELECT
        EXISTS (SELECT 1 FROM sec.CompanyRoles cr
                WHERE cr.CompanyRoleID = NEW.CompanyRoleID AND cr.RoleCode = 'DRIVER'),
        EXISTS (SELECT 1 FROM sec.CompanyRoles cr
                WHERE cr.CompanyRoleID = NEW.CompanyRoleID AND cr.RoleCode = 'COMPANY_OWNER')
    INTO v_is_driver, v_is_owner;

    IF v_is_driver AND (NEW.ScopeType <> 'Self' OR NEW.BranchID IS NOT NULL) THEN
        RAISE EXCEPTION 'access_scope_driver_self_required: DRIVER assignments require Self scope with NULL BranchID'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.ScopeType = 'Self' AND NOT v_is_driver THEN
        RAISE EXCEPTION 'access_scope_self_driver_only: Self scope requires exact CompanyRole DRIVER'
            USING ERRCODE = 'check_violation';
    END IF;
    IF v_is_owner AND NEW.IsActive
       AND (NEW.ScopeType <> 'AllCompanyBranches' OR NEW.BranchID IS NOT NULL) THEN
        RAISE EXCEPTION 'company_owner_scope_invalid: active COMPANY_OWNER requires AllCompanyBranches with NULL BranchID'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION sec.fn_check_company_owner_unique()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_is_owner BOOLEAN;
    v_conflict INTEGER;
BEGIN
    IF NOT NEW.IsActive THEN
        RETURN NEW;
    END IF;
    SELECT EXISTS (
        SELECT 1 FROM sec.CompanyRoles cr
        WHERE cr.CompanyRoleID = NEW.CompanyRoleID AND cr.RoleCode = 'COMPANY_OWNER'
    ) INTO v_is_owner;
    IF NOT v_is_owner THEN
        RETURN NEW;
    END IF;
    IF NEW.ScopeType <> 'AllCompanyBranches' OR NEW.BranchID IS NOT NULL THEN
        RAISE EXCEPTION 'company_owner_scope_invalid: active COMPANY_OWNER requires AllCompanyBranches with NULL BranchID'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT COUNT(*) INTO v_conflict
    FROM sec.UserBranchRoles ubr
    JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = ubr.CompanyRoleID
    WHERE ubr.CompanyID = NEW.CompanyID
      AND ubr.IsActive
      AND cr.RoleCode = 'COMPANY_OWNER'
      AND (TG_OP = 'INSERT' OR ubr.UserBranchRoleID <> NEW.UserBranchRoleID);
    IF v_conflict > 0 THEN
        RAISE EXCEPTION 'company_owner_duplicate: only one active COMPANY_OWNER assignment is allowed per company (company_id=%)', NEW.CompanyID
            USING ERRCODE = 'unique_violation';
    END IF;
    RETURN NEW;
END;
$$;
