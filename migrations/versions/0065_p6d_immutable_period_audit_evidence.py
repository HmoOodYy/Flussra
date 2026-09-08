"""0065: P6D immutable period audit evidence.

Adds append-only, period-scoped change-history evidence and snapshot-cycle
membership. Existing history remains legacy and is deliberately not backfilled.
"""
import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0065_p6d_immutable_period_audit_evidence.sql"
_MIGRATIONS_DIR = Path(__file__).parent.parent
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    conn = op.get_bind()
    for table in (
        "payroll.payrollperiodauditevidencecoverage",
        "payroll.payrollperiodauditevidenceevents",
        "payroll.payrollperiodauditevidencesnapshotevents",
    ):
        count = conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar()
        if count and count > 0:
            raise RuntimeError(
                "Downgrade of 0065 refused: immutable P6D evidence exists in " + table + "."
            )
    for table, trigger in (
        ("payroll.PayrollPeriodAuditEvidenceSnapshotEvents", "trg_PayrollPeriodAuditEvidenceSnapshotEvents_Immutable"),
        ("payroll.PayrollPeriodAuditEvidenceEvents", "trg_PayrollPeriodAuditEvidenceEvents_Immutable"),
        ("payroll.PayrollPeriodAuditEvidenceCoverage", "trg_PayrollPeriodAuditEvidenceCoverage_Immutable"),
        ("payroll.PayrollPeriodAuditEvidenceEvents", "trg_PayrollPeriodAuditEvidenceEvents_ReviewMembership"),
        ("payroll.PayrollPeriodAuditEvidenceSnapshotEvents", "trg_PayrollPeriodAuditEvidenceSnapshotEvents_Scope"),
        ("payroll.PayrollPeriodAuditEvidenceEvents", "trg_PayrollPeriodAuditEvidenceEvents_Scope"),
        ("payroll.PayrollPeriodAuditEvidenceCoverage", "trg_PayrollPeriodAuditEvidenceCoverage_Scope"),
        ("payroll.PayrollPeriods", "trg_PayrollPeriods_AuditEvidenceDelete"),
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger} ON {table}"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS payroll.fn_guard_period_audit_evidence_membership_scope()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS payroll.fn_guard_period_audit_evidence_event_scope()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS payroll.fn_guard_period_audit_evidence_scope()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS payroll.fn_guard_period_audit_evidence_immutable()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS payroll.fn_guard_period_audit_evidence_review_membership()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS payroll.fn_guard_period_delete_with_audit_evidence()"))
    op.execute(sa.text("DROP TABLE payroll.PayrollPeriodAuditEvidenceSnapshotEvents"))
    op.execute(sa.text("DROP TABLE payroll.PayrollPeriodAuditEvidenceEvents"))
    op.execute(sa.text("DROP TABLE payroll.PayrollPeriodAuditEvidenceCoverage"))
