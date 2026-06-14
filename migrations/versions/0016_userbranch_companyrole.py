"""0016 - Link UserBranchRoles to CompanyRoles; update permission function and view.

Three changes:
  1. Make sec.UserBranchRoles.RoleID nullable so new-path assignments (via
     CompanyRoleID only) do not require a global RoleID.
  2. Add CompanyRoleID (nullable FK) to sec.UserBranchRoles.
     For existing production rows: seed COMPANY_OWNER + DRIVER defaults for
     each company, mirror existing global roles as custom company roles,
     and backfill CompanyRoleID on all existing UserBranchRoles rows.
  3. Replace sec.fn_UserHasPermission with a dual-path version that checks
     CompanyRolePermissions first (new path) then RolePermissions (legacy).
     Recreate app.vw_UserBranchAccess to LEFT JOIN both Roles and CompanyRoles.

WARNING: 0016 contains a PL/pgSQL DO block and a CREATE FUNCTION with $$
dollar-quoting that include internal semicolons.  These are executed as
individual op.execute() calls rather than using a semicolon-split helper.

Revision ID: 0016
Revises:     0015
"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- 1. Make RoleID nullable ----
    op.execute(sa.text(
        "ALTER TABLE sec.UserBranchRoles ALTER COLUMN RoleID DROP NOT NULL"
    ))

    # ---- 2a. Add CompanyRoleID column ----
    op.execute(sa.text(
        "ALTER TABLE sec.UserBranchRoles "
        "ADD COLUMN CompanyRoleID INTEGER "
        "REFERENCES sec.CompanyRoles(CompanyRoleID)"
    ))

    op.execute(sa.text(
        "CREATE INDEX ix_UserBranchRoles_CompanyRoleID "
        "ON sec.UserBranchRoles (CompanyRoleID) "
        "WHERE CompanyRoleID IS NOT NULL"
    ))

    # ---- 2b. Backfill: seed defaults + link existing rows ----
    # This DO block is a SINGLE PostgreSQL statement — the semicolons inside
    # are PL/pgSQL statement separators, NOT SQL-level statement terminators.
    op.execute(sa.text("""
DO $$
DECLARE
    v_company   RECORD;
    v_role      RECORD;
    v_cr_id     INTEGER;
BEGIN
    FOR v_company IN SELECT companyid FROM core.companies LOOP

        -- COMPANY_OWNER: default protected, all permissions
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

        INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
        SELECT v_cr_id, permissioncode
        FROM   sec.permissions
        ON CONFLICT (companyroleid, permissioncode) DO NOTHING;

        -- DRIVER: default protected, minimal permissions
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

        INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
        VALUES (v_cr_id, 'drivers.view')
        ON CONFLICT (companyroleid, permissioncode) DO NOTHING;

        -- Mirror each distinct global role used by this company
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

            INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
            SELECT v_cr_id, p.permissioncode
            FROM   sec.rolepermissions rp
            JOIN   sec.permissions p ON p.permissionid = rp.permissionid
            WHERE  rp.roleid = v_role.roleid
            ON CONFLICT (companyroleid, permissioncode) DO NOTHING;
        END LOOP;

    END LOOP;
END $$
"""))

    # ---- 2c. Backfill CompanyRoleID on existing UserBranchRoles rows ----
    op.execute(sa.text("""
UPDATE sec.userbranchroles ubr
SET    companyroleId = cr.companyroleid
FROM   sec.roles r,
       sec.companyroles cr
WHERE  ubr.roleid        = r.roleid
  AND  cr.companyid      = ubr.companyid
  AND  cr.rolecode       = r.rolecode
  AND  ubr.companyroleId IS NULL
"""))

    # ---- 3a. Update sec.fn_UserHasPermission — dual-path check ----
    # CREATE OR REPLACE FUNCTION is a single SQL statement even though it
    # contains semicolons inside the PL/pgSQL body (dollar-quoted string).
    op.execute(sa.text("""
CREATE OR REPLACE FUNCTION sec.fn_UserHasPermission(
    p_UserID         INTEGER,
    p_CompanyID      INTEGER,
    p_BranchID       INTEGER,
    p_PermissionCode VARCHAR(100)
) RETURNS BOOLEAN
LANGUAGE plpgsql STABLE AS $$
BEGIN
    -- New path: CompanyRolePermissions
    IF EXISTS (
        SELECT 1
        FROM   sec.userbranchroles        AS ubr
        JOIN   sec.companyrolepermissions AS crp
               ON  crp.companyroleid = ubr.companyroleId
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

    -- Legacy path: global RolePermissions (backward compatibility)
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
$$
"""))

    # ---- 3b. Recreate vw_UserBranchAccess to handle nullable RoleID ----
    # Must DROP + CREATE (not CREATE OR REPLACE) because the column type of
    # RoleCode would widen from varchar(50) to varchar due to COALESCE.
    # vw_UserBranchAccess has no dependent views, so CASCADE is safe.
    op.execute(sa.text(
        "DROP VIEW IF EXISTS app.vw_UserBranchAccess CASCADE"
    ))

    op.execute(sa.text("""
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
    COALESCE(r.RoleCode, cr.RoleCode)::varchar(80)  AS RoleCode,
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
LEFT  JOIN sec.CompanyRoles AS cr ON cr.CompanyRoleID = ubr.CompanyRoleID
"""))


def downgrade() -> None:
    # Restore the original view (INNER JOIN on sec.Roles, RoleID NOT NULL)
    op.execute(sa.text(
        "DROP VIEW IF EXISTS app.vw_UserBranchAccess CASCADE"
    ))
    op.execute(sa.text("""
CREATE OR REPLACE VIEW app.vw_UserBranchAccess AS
SELECT
    u.UserID, u.Username, u.DisplayName,
    c.CompanyID, c.CompanyCode, c.CompanyName,
    ubr.ScopeType,
    b.BranchID, b.BranchCode, b.BranchName,
    r.RoleID, r.RoleCode, r.RoleName,
    u.IsActive AS UserIsActive, u.CanLogin,
    c.Status AS CompanyStatus, c.IsSuspended AS CompanyIsSuspended,
    b.Status AS BranchStatus, ubr.IsActive AS AccessIsActive
FROM sec.UserBranchRoles AS ubr
INNER JOIN sec.Users      AS u ON u.UserID    = ubr.UserID
INNER JOIN core.Companies AS c ON c.CompanyID = ubr.CompanyID
LEFT  JOIN core.Branches  AS b ON b.BranchID  = ubr.BranchID
INNER JOIN sec.Roles      AS r ON r.RoleID    = ubr.RoleID
"""))

    # Restore original fn_UserHasPermission (legacy path only)
    op.execute(sa.text("""
CREATE OR REPLACE FUNCTION sec.fn_UserHasPermission(
    p_UserID         INTEGER,
    p_CompanyID      INTEGER,
    p_BranchID       INTEGER,
    p_PermissionCode VARCHAR(100)
) RETURNS BOOLEAN
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1
        FROM   sec.userbranchroles AS ubr
        INNER JOIN sec.rolepermissions AS rp   ON rp.roleid        = ubr.roleid
        INNER JOIN sec.permissions     AS perm ON perm.permissionid = rp.permissionid
        WHERE  ubr.userid          = p_UserID
          AND  ubr.companyid       = p_CompanyID
          AND  ubr.isactive        = TRUE
          AND  perm.permissioncode = p_PermissionCode
          AND  (
               ubr.scopetype = 'AllCompanyBranches'
            OR ubr.branchid  = p_BranchID
               )
    );
END;
$$
"""))

    # Remove CompanyRoleID column + index
    op.execute(sa.text(
        "DROP INDEX IF EXISTS ix_UserBranchRoles_CompanyRoleID"
    ))
    op.execute(sa.text(
        "ALTER TABLE sec.UserBranchRoles DROP COLUMN IF EXISTS CompanyRoleID"
    ))

    # Restore RoleID NOT NULL
    op.execute(sa.text(
        "ALTER TABLE sec.UserBranchRoles ALTER COLUMN RoleID SET NOT NULL"
    ))
