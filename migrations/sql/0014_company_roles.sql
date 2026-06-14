-- =============================================================================
-- 0014_company_roles.sql
--
-- Creates the company-scoped role system:
--   sec.CompanyRoles          - per-company role catalog (default + custom)
--   sec.CompanyRolePermissions - permission codes granted to each company role
--
-- Design decisions:
--   * CompanyRolePermissions stores PermissionCode directly (VARCHAR) rather
--     than a FK to sec.Permissions.  The code string IS the canonical key;
--     avoiding the FK join simplifies the permission check function and makes
--     the table self-contained.  The Permissions catalogue remains the
--     authoritative list of valid codes.
--   * sec.Roles / sec.RolePermissions are kept intact.  The new tables run in
--     parallel during the transition; fn_UserHasPermission (updated in
--     0016) checks both paths.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Company-scoped role catalog
-- ---------------------------------------------------------------------------

CREATE TABLE sec.CompanyRoles (
    CompanyRoleID  SERIAL        PRIMARY KEY,
    CompanyID      INTEGER       NOT NULL,
    RoleCode       VARCHAR(80)   NOT NULL,
    RoleName       VARCHAR(120)  NOT NULL,
    RoleLevel      INTEGER       NOT NULL DEFAULT 0,
    IsDefault      BOOLEAN       NOT NULL DEFAULT FALSE,   -- seeded with company (Owner, Driver)
    IsProtected    BOOLEAN       NOT NULL DEFAULT FALSE,   -- cannot be deleted via API
    IsCustom       BOOLEAN       NOT NULL DEFAULT FALSE,   -- user-created
    IsActive       BOOLEAN       NOT NULL DEFAULT TRUE,
    Notes          TEXT,
    CreatedAtUtc   TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UpdatedAtUtc   TIMESTAMPTZ,
    CONSTRAINT uq_CompanyRoles_Company_Code
        UNIQUE (CompanyID, RoleCode),
    CONSTRAINT fk_CompanyRoles_Company
        FOREIGN KEY (CompanyID) REFERENCES core.Companies(CompanyID)
);

CREATE INDEX ix_CompanyRoles_CompanyID
    ON sec.CompanyRoles (CompanyID);

CREATE INDEX ix_CompanyRoles_CompanyID_Active
    ON sec.CompanyRoles (CompanyID, IsActive);

-- ---------------------------------------------------------------------------
-- Permissions granted to each company role
-- ---------------------------------------------------------------------------

CREATE TABLE sec.CompanyRolePermissions (
    ID             SERIAL        PRIMARY KEY,
    CompanyRoleID  INTEGER       NOT NULL,
    PermissionCode VARCHAR(100)  NOT NULL,
    CreatedAtUtc   TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_CompanyRolePermissions_Role_Perm
        UNIQUE (CompanyRoleID, PermissionCode),
    CONSTRAINT fk_CompanyRolePermissions_Role
        FOREIGN KEY (CompanyRoleID)
        REFERENCES sec.CompanyRoles(CompanyRoleID)
        ON DELETE CASCADE
);

CREATE INDEX ix_CompanyRolePermissions_CompanyRoleID
    ON sec.CompanyRolePermissions (CompanyRoleID);
