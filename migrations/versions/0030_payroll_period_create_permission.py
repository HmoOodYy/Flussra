"""0030: Add payroll.period.create permission.

Introduces a dedicated permission code for creating payroll periods.
Previously the backend gated create_period on payroll.entry, which only
exists in the legacy/test seed (conftest.py) and not in the production
0015 seed.  Production company roles only receive payroll.edit, so real
users could never see the Create Period button.

This migration:
  1. Inserts payroll.period.create into sec.permissions.
  2. Grants it to every company role that already has payroll.edit or
     payroll.entry (covers both production and dev/test environments).
  3. Grants it to every global role (rolepermissions) that has payroll.entry.

The backend service.py create_period gate requires payroll.period.create
(separate from payroll.entry which gates data-entry operations).

Revision ID: 0030
Revises: 0029
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0030"
down_revision: str = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Register the new permission code.
    op.execute(sa.text("""
        INSERT INTO sec.permissions (permissioncode, permissionname, modulecode)
        VALUES ('payroll.period.create', 'Create Payroll Period', 'payroll')
        ON CONFLICT (permissioncode) DO NOTHING
    """))

    # 2. Grant to every company role that already has payroll.edit (production seed).
    op.execute(sa.text("""
        INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
        SELECT crp.companyroleid, 'payroll.period.create'
        FROM   sec.companyrolepermissions crp
        WHERE  crp.permissioncode = 'payroll.edit'
        ON CONFLICT DO NOTHING
    """))

    # 3. Grant to every company role that already has payroll.entry (legacy/test).
    op.execute(sa.text("""
        INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
        SELECT crp.companyroleid, 'payroll.period.create'
        FROM   sec.companyrolepermissions crp
        WHERE  crp.permissioncode = 'payroll.entry'
        ON CONFLICT DO NOTHING
    """))

    # 4. Grant to every global role (rolepermissions) that has payroll.entry.
    #    Runs AFTER step 1 so the payroll.period.create permissionid exists.
    op.execute(sa.text("""
        INSERT INTO sec.rolepermissions (roleid, permissionid)
        SELECT rp.roleid, p.permissionid
        FROM   sec.rolepermissions rp
        JOIN   sec.permissions p ON p.permissioncode = 'payroll.period.create'
        WHERE  rp.permissionid = (
            SELECT permissionid FROM sec.permissions
            WHERE  permissioncode = 'payroll.entry'
        )
        ON CONFLICT DO NOTHING
    """))


def downgrade() -> None:
    op.execute(sa.text(
        "DELETE FROM sec.companyrolepermissions "
        "WHERE permissioncode = 'payroll.period.create'"
    ))
    op.execute(sa.text("""
        DELETE FROM sec.rolepermissions
        WHERE permissionid = (
            SELECT permissionid FROM sec.permissions
            WHERE  permissioncode = 'payroll.period.create'
        )
    """))
    op.execute(sa.text(
        "DELETE FROM sec.permissions "
        "WHERE permissioncode = 'payroll.period.create'"
    ))
