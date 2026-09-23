"""Seed Payroll Setup permissions and add immutable policy audit evidence.

Revision ID: 0068
Revises: 0067
"""
import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0068_payroll_setup_permissions_policy_audit.sql"
_MIGRATIONS_DIR = Path(__file__).parent.parent
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM payroll.PayrollSetupPolicyAuditEvents)"
    )).scalar():
        raise RuntimeError(
            "Downgrade of 0068 refused: immutable Payroll Setup policy evidence exists."
        )

    assigned = bind.execute(sa.text(
        """
        SELECT EXISTS (
            SELECT 1
            FROM sec.Permissions p
            WHERE p.PermissionCode IN (
                'payroll_setup.view', 'payroll_setup.manage',
                'payroll_setup.publish', 'payroll_setup.assign'
            )
              AND (
                  EXISTS (
                      SELECT 1 FROM sec.RolePermissions rp
                      WHERE rp.PermissionID = p.PermissionID
                  )
                  OR EXISTS (
                      SELECT 1 FROM sec.CompanyRolePermissions crp
                      WHERE crp.PermissionCode = p.PermissionCode
                  )
                  OR EXISTS (
                      SELECT 1 FROM sec.UserPermissionOverrides upo
                      WHERE upo.PermissionCode = p.PermissionCode
                  )
              )
        )
        """
    )).scalar()
    if assigned:
        raise RuntimeError(
            "Downgrade of 0068 refused: Payroll Setup permissions have grants or overrides."
        )

    op.execute(sa.text(
        "DROP TRIGGER trg_PayrollSetupPolicyAuditEventBranches_NoTruncate "
        "ON payroll.PayrollSetupPolicyAuditEventBranches"
    ))
    op.execute(sa.text(
        "DROP TRIGGER trg_PayrollSetupPolicyAuditEvents_NoTruncate "
        "ON payroll.PayrollSetupPolicyAuditEvents"
    ))
    op.execute(sa.text(
        "DROP TRIGGER trg_PayrollSetupPolicyAuditEventBranches_Immutable "
        "ON payroll.PayrollSetupPolicyAuditEventBranches"
    ))
    op.execute(sa.text(
        "DROP TRIGGER trg_PayrollSetupPolicyAuditEvents_Immutable "
        "ON payroll.PayrollSetupPolicyAuditEvents"
    ))
    op.execute(sa.text(
        "DROP TRIGGER trg_PayrollSetupPolicyAuditEventBranches_Scope "
        "ON payroll.PayrollSetupPolicyAuditEventBranches"
    ))
    op.execute(sa.text(
        "DROP TRIGGER trg_PayrollSetupPolicyAuditEvents_Scope "
        "ON payroll.PayrollSetupPolicyAuditEvents"
    ))
    op.execute(sa.text("DROP FUNCTION payroll.fn_guard_payroll_setup_policy_audit_immutable()"))
    op.execute(sa.text("DROP FUNCTION payroll.fn_guard_payroll_setup_policy_audit_scope()"))
    op.execute(sa.text("DROP TABLE payroll.PayrollSetupPolicyAuditEventBranches"))
    op.execute(sa.text("DROP TABLE payroll.PayrollSetupPolicyAuditEvents"))
    bind.execute(sa.text(
        """
        DELETE FROM sec.Permissions
        WHERE PermissionCode IN (
            'payroll_setup.view', 'payroll_setup.manage',
            'payroll_setup.publish', 'payroll_setup.assign'
        )
        """
    ))
