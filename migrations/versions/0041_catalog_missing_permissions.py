"""0041: Surface enforced-but-uncatalogued permissions in sec.Permissions.

Two permission codes are actively enforced by the backend but were never
added to the production permission catalogue via a migration:

  payroll.entry  -- required for entering/editing payroll lines and
                    transitioning periods (Draft->Open, Open->InReview)
  review.decide  -- required for approving/rejecting review items

These codes existed only in conftest.py (test setup) and ensure_dev_admin.py
(dev seed script), meaning:
  1. sec.fn_UserHasPermission Path A (Company Owner dynamic grant) returned
     FALSE for these codes in production.
  2. Custom roles could not be granted these permissions via the UI.
  3. The frontend /admin/permissions endpoint never listed them.

NOTE: payroll.period.create was already added by migration 0030.

Revision ID: 0041
Revises: 0040
"""
from alembic import op

revision: str = "0041"
down_revision: str = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Register payroll.entry in the permission catalogue
    op.execute("""
        INSERT INTO sec.permissions (permissioncode, permissionname, modulecode)
        VALUES ('payroll.entry', 'Enter Payroll Data', 'payroll')
        ON CONFLICT (permissioncode)
            DO UPDATE SET
                permissionname = EXCLUDED.permissionname,
                modulecode     = EXCLUDED.modulecode
    """)

    # 2. Register review.decide in the permission catalogue
    op.execute("""
        INSERT INTO sec.permissions (permissioncode, permissionname, modulecode)
        VALUES ('review.decide', 'Approve / Reject Review Items', 'review')
        ON CONFLICT (permissioncode)
            DO UPDATE SET
                permissionname = EXCLUDED.permissionname,
                modulecode     = EXCLUDED.modulecode
    """)

    # 3. Grant payroll.entry to every company role that already holds payroll.edit
    op.execute("""
        INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
        SELECT crp.companyroleid, 'payroll.entry'
        FROM   sec.companyrolepermissions crp
        WHERE  crp.permissioncode = 'payroll.edit'
        ON CONFLICT DO NOTHING
    """)

    # 4. Grant payroll.entry to legacy global roles (sec.rolepermissions) that
    #    already hold payroll.edit, so the backward-compat path (Path C) works.
    op.execute("""
        INSERT INTO sec.rolepermissions (roleid, permissionid)
        SELECT rp.roleid, p.permissionid
        FROM   sec.rolepermissions rp
        JOIN   sec.permissions     p  ON p.permissioncode = 'payroll.entry'
        WHERE  rp.permissionid = (
            SELECT permissionid FROM sec.permissions WHERE permissioncode = 'payroll.edit'
        )
        ON CONFLICT DO NOTHING
    """)

    # 5. Ensure COMPANY_OWNER company roles have explicit rows for both new codes.
    op.execute("""
        INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
        SELECT cr.companyroleid, perms.permissioncode
        FROM   sec.companyroles cr
        CROSS  JOIN (VALUES ('payroll.entry'), ('review.decide')) AS perms(permissioncode)
        WHERE  cr.rolecode = 'COMPANY_OWNER'
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    # Removing catalogue rows is intentionally skipped: these permission codes
    # may already be referenced by companyrolepermissions rows written by users
    # after this migration ran. Removing them would silently strip permissions
    # from custom roles. If a true rollback is needed, do it manually.
    pass
