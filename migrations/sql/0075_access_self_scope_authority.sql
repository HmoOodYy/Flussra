-- P2b: replace projected ODA scope with resource-aware Self authority.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM sec.UserBranchRoles ubr
        LEFT JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = ubr.CompanyRoleID
        LEFT JOIN sec.Roles r ON r.RoleID = ubr.RoleID
        WHERE ubr.ScopeType = 'OwnDriverDataOnly'
          AND cr.RoleCode IS DISTINCT FROM 'DRIVER'
          AND r.RoleCode IS DISTINCT FROM 'DRIVER'
    ) THEN
        RAISE EXCEPTION '0075 refused: non-DRIVER OwnDriverDataOnly assignment exists; reset development data or reconcile the role and scope explicitly';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM sec.UserBranchRoles ubr
        LEFT JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = ubr.CompanyRoleID
        LEFT JOIN sec.Roles r ON r.RoleID = ubr.RoleID
        WHERE (cr.RoleCode = 'DRIVER' OR r.RoleCode = 'DRIVER')
          AND (cr.RoleCode = 'COMPANY_OWNER' OR r.RoleCode = 'COMPANY_OWNER')
    ) THEN
        RAISE EXCEPTION '0075 refused: one access assignment identifies both DRIVER and COMPANY_OWNER; reconcile the conflicting role identities';
    END IF;

    IF EXISTS (
        SELECT ubr.CompanyID
        FROM sec.UserBranchRoles ubr
        LEFT JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = ubr.CompanyRoleID
        LEFT JOIN sec.Roles r ON r.RoleID = ubr.RoleID
        WHERE ubr.IsActive
          AND (cr.RoleCode = 'COMPANY_OWNER' OR r.RoleCode = 'COMPANY_OWNER')
        GROUP BY ubr.CompanyID
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION '0075 refused: multiple active COMPANY_OWNER assignments exist in one company; reconcile owner authority before retrying';
    END IF;
END $$;

-- Remove the legacy shape checks before converting rows to Self. The new
-- constraints are installed after every legacy assignment has been normalized.
ALTER TABLE sec.UserBranchRoles
    DROP CONSTRAINT ck_UserBranchRoles_ScopeType,
    DROP CONSTRAINT ck_UserBranchRoles_BranchScope;

-- Exact DRIVER rows become Self without carrying branch authority.
UPDATE sec.UserBranchRoles ubr
SET ScopeType = 'Self', BranchID = NULL
FROM sec.CompanyRoles cr
WHERE cr.CompanyRoleID = ubr.CompanyRoleID
  AND cr.RoleCode = 'DRIVER';

UPDATE sec.UserBranchRoles ubr
SET ScopeType = 'Self', BranchID = NULL
FROM sec.Roles r
WHERE r.RoleID = ubr.RoleID
  AND r.RoleCode = 'DRIVER';

-- Preserve the single valid company-wide Company Owner authority shape.
UPDATE sec.UserBranchRoles ubr
SET ScopeType = 'AllCompanyBranches', BranchID = NULL
WHERE ubr.IsActive
  AND (
      EXISTS (
          SELECT 1 FROM sec.CompanyRoles cr
          WHERE cr.CompanyRoleID = ubr.CompanyRoleID
            AND cr.RoleCode = 'COMPANY_OWNER'
      )
      OR EXISTS (
          SELECT 1 FROM sec.Roles r
          WHERE r.RoleID = ubr.RoleID
            AND r.RoleCode = 'COMPANY_OWNER'
      )
  );

-- Any remaining ODA row passed preflight and therefore belongs to exact DRIVER.
UPDATE sec.UserBranchRoles
SET ScopeType = 'Self', BranchID = NULL
WHERE ScopeType = 'OwnDriverDataOnly';

-- DRIVER has no generic permission codes in P2b.
DELETE FROM sec.CompanyRolePermissions crp
USING sec.CompanyRoles cr
WHERE cr.CompanyRoleID = crp.CompanyRoleID
  AND cr.RoleCode = 'DRIVER';

DELETE FROM sec.RolePermissions rp
USING sec.Roles r
WHERE r.RoleID = rp.RoleID
  AND r.RoleCode = 'DRIVER';

-- Preserve override history while removing active generic grants for DRIVER/Self.
UPDATE sec.UserPermissionOverrides upo
SET IsActive = FALSE,
    RevokedAtUtc = COALESCE(upo.RevokedAtUtc, NOW())
WHERE upo.IsActive
  AND EXISTS (
      SELECT 1
      FROM sec.UserBranchRoles ubr
      LEFT JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = ubr.CompanyRoleID
      LEFT JOIN sec.Roles r ON r.RoleID = ubr.RoleID
      WHERE ubr.UserID = upo.UserID
        AND ubr.CompanyID = upo.CompanyID
        AND ubr.IsActive
        AND (
             ubr.ScopeType = 'Self'
          OR cr.RoleCode = 'DRIVER'
          OR r.RoleCode = 'DRIVER'
        )
  );

ALTER TABLE sec.UserBranchRoles
    ADD CONSTRAINT ck_UserBranchRoles_ScopeType
        CHECK (ScopeType IN ('AllCompanyBranches', 'SpecificBranch', 'Self')),
    ADD CONSTRAINT ck_UserBranchRoles_BranchScope
        CHECK (
            (ScopeType = 'AllCompanyBranches' AND BranchID IS NULL)
            OR (ScopeType = 'SpecificBranch' AND BranchID IS NOT NULL)
            OR (ScopeType = 'Self' AND BranchID IS NULL)
        );

CREATE OR REPLACE FUNCTION sec.fn_enforce_userbranchrole_scope_authority()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_is_driver BOOLEAN;
    v_is_owner BOOLEAN;
BEGIN
    SELECT
        EXISTS (SELECT 1 FROM sec.CompanyRoles cr
                WHERE cr.CompanyRoleID = NEW.CompanyRoleID AND cr.RoleCode = 'DRIVER')
        OR EXISTS (SELECT 1 FROM sec.Roles r
                   WHERE r.RoleID = NEW.RoleID AND r.RoleCode = 'DRIVER'),
        EXISTS (SELECT 1 FROM sec.CompanyRoles cr
                WHERE cr.CompanyRoleID = NEW.CompanyRoleID AND cr.RoleCode = 'COMPANY_OWNER')
        OR EXISTS (SELECT 1 FROM sec.Roles r
                   WHERE r.RoleID = NEW.RoleID AND r.RoleCode = 'COMPANY_OWNER')
    INTO v_is_driver, v_is_owner;

    IF v_is_driver AND (NEW.ScopeType <> 'Self' OR NEW.BranchID IS NOT NULL) THEN
        RAISE EXCEPTION 'access_scope_driver_self_required: DRIVER assignments require Self scope with NULL BranchID'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.ScopeType = 'Self' AND NOT v_is_driver THEN
        RAISE EXCEPTION 'access_scope_self_driver_only: Self scope requires exact RoleCode DRIVER'
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

CREATE TRIGGER trg_userbranchroles_scope_authority
    BEFORE INSERT OR UPDATE ON sec.UserBranchRoles
    FOR EACH ROW EXECUTE FUNCTION sec.fn_enforce_userbranchrole_scope_authority();

-- Company Owner must remain a single active company-wide assignment across both role paths.
CREATE OR REPLACE FUNCTION sec.fn_check_company_owner_unique()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_is_owner BOOLEAN;
    v_conflict INTEGER;
BEGIN
    IF NOT NEW.IsActive THEN
        RETURN NEW;
    END IF;

    SELECT
        EXISTS (SELECT 1 FROM sec.CompanyRoles cr
                WHERE cr.CompanyRoleID = NEW.CompanyRoleID AND cr.RoleCode = 'COMPANY_OWNER')
        OR EXISTS (SELECT 1 FROM sec.Roles r
                   WHERE r.RoleID = NEW.RoleID AND r.RoleCode = 'COMPANY_OWNER')
    INTO v_is_owner;

    IF NOT v_is_owner THEN
        RETURN NEW;
    END IF;

    IF NEW.ScopeType <> 'AllCompanyBranches' OR NEW.BranchID IS NOT NULL THEN
        RAISE EXCEPTION 'company_owner_scope_invalid: active COMPANY_OWNER requires AllCompanyBranches with NULL BranchID'
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT COUNT(*) INTO v_conflict
    FROM sec.UserBranchRoles ubr
    LEFT JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = ubr.CompanyRoleID
    LEFT JOIN sec.Roles r ON r.RoleID = ubr.RoleID
    WHERE ubr.CompanyID = NEW.CompanyID
      AND ubr.IsActive
      AND (cr.RoleCode = 'COMPANY_OWNER' OR r.RoleCode = 'COMPANY_OWNER')
      AND (TG_OP = 'INSERT' OR ubr.UserBranchRoleID <> NEW.UserBranchRoleID);

    IF v_conflict > 0 THEN
        RAISE EXCEPTION 'company_owner_duplicate: only one active COMPANY_OWNER assignment is allowed per company (company_id=%)', NEW.CompanyID
            USING ERRCODE = 'unique_violation';
    END IF;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE VIEW app.vw_UserBranchAccess AS
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
    u.IsActive AS UserIsActive,
    u.CanLogin,
    c.Status AS CompanyStatus,
    c.IsSuspended AS CompanyIsSuspended,
    b.Status AS BranchStatus,
    ubr.IsActive AS AccessIsActive
FROM sec.UserBranchRoles ubr
JOIN sec.Users u ON u.UserID = ubr.UserID
JOIN core.Companies c ON c.CompanyID = ubr.CompanyID
LEFT JOIN core.Branches b ON b.BranchID = ubr.BranchID
LEFT JOIN sec.Roles r ON r.RoleID = ubr.RoleID
LEFT JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = ubr.CompanyRoleID
WHERE ubr.ScopeType IN ('AllCompanyBranches', 'SpecificBranch');

CREATE OR REPLACE FUNCTION sec.fn_UserHasPermission(
    p_UserID INTEGER,
    p_CompanyID INTEGER,
    p_BranchID INTEGER,
    p_PermissionCode VARCHAR(100)
) RETURNS BOOLEAN
LANGUAGE plpgsql STABLE AS $$
BEGIN
    -- Generic company/branch permission evaluation has no resource identity for Self.
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

    IF EXISTS (
        SELECT 1
        FROM sec.UserBranchRoles ubr
        LEFT JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = ubr.CompanyRoleID
        LEFT JOIN sec.Roles r ON r.RoleID = ubr.RoleID
        WHERE ubr.UserID = p_UserID
          AND ubr.CompanyID = p_CompanyID
          AND ubr.IsActive
          AND (
               ubr.ScopeType = 'Self'
            OR cr.RoleCode = 'DRIVER'
            OR r.RoleCode = 'DRIVER'
          )
    ) THEN
        RETURN FALSE;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM sec.UserBranchRoles ubr
        LEFT JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = ubr.CompanyRoleID
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

    IF EXISTS (
        SELECT 1
        FROM sec.UserBranchRoles ubr
        JOIN sec.RolePermissions rp ON rp.RoleID = ubr.RoleID
        JOIN sec.Permissions p ON p.PermissionID = rp.PermissionID
        WHERE ubr.UserID = p_UserID
          AND ubr.CompanyID = p_CompanyID
          AND ubr.IsActive
          AND p.PermissionCode = p_PermissionCode
          AND (
               (p_BranchID IS NULL AND ubr.ScopeType = 'AllCompanyBranches' AND ubr.BranchID IS NULL)
            OR (p_BranchID IS NOT NULL AND ubr.ScopeType = 'AllCompanyBranches' AND ubr.BranchID IS NULL)
            OR (p_BranchID IS NOT NULL AND ubr.ScopeType = 'SpecificBranch' AND ubr.BranchID = p_BranchID)
          )
    ) THEN
        RETURN TRUE;
    END IF;

    -- An override widens actions only within a non-Self assignment's resource set.
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
                AND (
                     (p_BranchID IS NULL AND ubr.ScopeType = 'AllCompanyBranches' AND ubr.BranchID IS NULL)
                  OR (p_BranchID IS NOT NULL AND ubr.ScopeType = 'AllCompanyBranches' AND ubr.BranchID IS NULL)
                  OR (p_BranchID IS NOT NULL AND ubr.ScopeType = 'SpecificBranch' AND ubr.BranchID = p_BranchID)
                )
          )
    );
END;
$$;
