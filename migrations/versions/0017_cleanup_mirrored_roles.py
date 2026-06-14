"""0017 - Remove legacy global roles auto-mirrored into sec.CompanyRoles.

Migration 0016 included a DO $$ block that looped over every company and
created a custom CompanyRole for each global sec.Roles entry that was already
assigned to a user in that company.  That mirroring was a migration-time
convenience; the new Company Roles UI should only show explicitly created
roles (COMPANY_OWNER, DRIVER, and user-created custom roles).

This migration:
  1. Nulls out CompanyRoleID on any UserBranchRoles rows that reference a
     mirrored role (the legacy RoleID is preserved for backward-compat auth).
  2. Deletes the mirrored CompanyRole rows.

Mirrored roles are identified by:
  iscustom=TRUE AND isprotected=FALSE AND isdefault=FALSE
  AND rolecode IN (SELECT rolecode FROM sec.roles)

Revision ID: 0017
Revises:     0016
"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Step 1: Remove FK references so the DELETE does not violate the FK constraint
    op.execute(sa.text("""
        UPDATE sec.userbranchroles
        SET    companyroleId = NULL
        WHERE  companyroleId IN (
            SELECT companyroleid
            FROM   sec.companyroles
            WHERE  iscustom    = TRUE
              AND  isprotected = FALSE
              AND  isdefault   = FALSE
              AND  rolecode IN (SELECT rolecode FROM sec.roles)
        )
    """))

    # Step 2: Delete the mirrored roles
    op.execute(sa.text("""
        DELETE FROM sec.companyroles
        WHERE  iscustom    = TRUE
          AND  isprotected = FALSE
          AND  isdefault   = FALSE
          AND  rolecode IN (SELECT rolecode FROM sec.roles)
    """))


def downgrade() -> None:
    # Re-creating the mirrored roles is complex and rarely needed.
    # We re-run the same seeding logic from 0016's DO block.
    op.execute(sa.text("""
DO $$
DECLARE
    v_company   RECORD;
    v_role      RECORD;
    v_cr_id     INTEGER;
BEGIN
    FOR v_company IN SELECT companyid FROM core.companies LOOP
        FOR v_role IN
            SELECT DISTINCT r.roleid, r.rolecode, r.rolename, r.rolelevel
            FROM   sec.userbranchroles ubr
            JOIN   sec.roles r ON r.roleid = ubr.roleid
            WHERE  ubr.companyid = v_company.companyid
        LOOP
            -- Skip protected defaults
            IF v_role.rolecode IN ('COMPANY_OWNER', 'DRIVER') THEN
                CONTINUE;
            END IF;

            INSERT INTO sec.companyroles
                (companyid, rolecode, rolename, rolelevel,
                 isdefault, isprotected, iscustom, isactive)
            VALUES
                (v_company.companyid, v_role.rolecode, v_role.rolename,
                 v_role.rolelevel, FALSE, FALSE, TRUE, TRUE)
            ON CONFLICT (companyid, rolecode) DO NOTHING;

            SELECT companyroleid INTO v_cr_id
            FROM   sec.companyroles
            WHERE  companyid = v_company.companyid AND rolecode = v_role.rolecode;

            INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
            SELECT v_cr_id, p.permissioncode
            FROM   sec.rolepermissions rp
            JOIN   sec.permissions p ON p.permissionid = rp.permissionid
            WHERE  rp.roleid = v_role.roleid
            ON CONFLICT (companyroleid, permissioncode) DO NOTHING;

            UPDATE sec.userbranchroles
            SET    companyroleId = v_cr_id
            WHERE  roleid = v_role.roleid
              AND  companyid = v_company.companyid
              AND  companyroleId IS NULL;
        END LOOP;
    END LOOP;
END $$
"""))
