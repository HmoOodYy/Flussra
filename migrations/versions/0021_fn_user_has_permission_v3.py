"""0021: Update sec.fn_UserHasPermission — overrides + Owner dynamic permissions.

Adds two new resolution paths to the permission-check function:

  A. Company Owner: if the user has an active COMPANY_OWNER company role,
     return TRUE for any permission that exists in sec.Permissions (dynamic,
     no CompanyRolePermissions row required).

  D. UserPermissionOverrides: ALLOW overrides granted directly to the user
     now authorize backend endpoints, not just inform the frontend.

Paths B (CompanyRolePermissions) and C (legacy RolePermissions) are unchanged.

Revision ID: 0021
Revises: 0020
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0021"
down_revision: str = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION sec.fn_UserHasPermission(
            p_UserID         INTEGER,
            p_CompanyID      INTEGER,
            p_BranchID       INTEGER,
            p_PermissionCode VARCHAR(100)
        ) RETURNS BOOLEAN
        LANGUAGE plpgsql STABLE AS $$
        BEGIN
            -- Path A: Company Owner dynamic grant
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

            -- Path C: Legacy RolePermissions (backward compat)
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
        $$
    """))


def downgrade() -> None:
    # Restore the 0016 version (Paths B + C only)
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION sec.fn_UserHasPermission(
            p_UserID         INTEGER,
            p_CompanyID      INTEGER,
            p_BranchID       INTEGER,
            p_PermissionCode VARCHAR(100)
        ) RETURNS BOOLEAN
        LANGUAGE plpgsql STABLE AS $$
        BEGIN
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
