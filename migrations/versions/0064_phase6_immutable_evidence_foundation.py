"""0064: Phase 6 immutable workflow and used-rate evidence foundation.

Adds append-only evidence for the workflow participants and the rate/rule
definitions actually used by new immutable calculation snapshots. Existing
history is intentionally left legacy with no synthetic backfill.

Revision ID: 0064
Revises: 0063
"""
import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0064"
down_revision: Union[str, None] = "0063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SQL_FILE = Path(__file__).parent.parent / "sql" / "0064_phase6_immutable_evidence_foundation.sql"

_migrations_dir = Path(__file__).parent.parent
if str(_migrations_dir) not in sys.path:
    sys.path.insert(0, str(_migrations_dir))

from utils import statements_from_file  # noqa: E402


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    conn = op.get_bind()
    for table in (
        "payroll.payrollperiodworkflowactionevidence",
        "payroll.payrollcalculationsnapshotusedratedefinitions",
    ):
        count = conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar()
        if count and count > 0:
            raise RuntimeError(
                "Downgrade of 0064 refused: immutable Phase 6 evidence exists "
                f"in {table}. Resolve retained historical evidence manually."
            )
    referenced = conn.execute(sa.text("""
        SELECT 1
        FROM sec.companyrolepermissions
        WHERE permissioncode IN ('ledger.view', 'ledger.audit.view')
        UNION ALL
        SELECT 1
        FROM sec.userpermissionoverrides
        WHERE permissioncode IN ('ledger.view', 'ledger.audit.view')
        LIMIT 1
    """)).first()
    if referenced is not None:
        raise RuntimeError(
            "Downgrade of 0064 refused: finalized-library permissions are assigned."
        )

    for table, trigger in (
        ("payroll.PayrollPeriodWorkflowActionEvidence", "trg_PayrollPeriodWorkflowActionEvidence_Immutable"),
        ("payroll.PayrollCalculationSnapshotUsedRateDefinitions", "trg_PayrollCalculationSnapshotUsedRateDefinitions_Immutable"),
        ("payroll.PayrollCalculationSnapshotLines", "trg_PayrollCalculationSnapshotLines_UsedRateDefinitionScope"),
        ("payroll.PayrollCalculationSnapshotUsedRateDefinitions", "trg_PayrollCalculationSnapshotUsedRateDefinitions_Scope"),
        ("payroll.PayrollPeriodWorkflowActionEvidence", "trg_PayrollPeriodWorkflowActionEvidence_Scope"),
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger} ON {table}"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS payroll.fn_guard_snapshot_line_used_rate_definition_scope()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS payroll.fn_guard_used_rate_definition_scope()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS payroll.fn_guard_workflow_action_evidence_scope()"))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollCalculationSnapshotLines "
        "DROP CONSTRAINT fk_PayrollCalculationSnapshotLines_UsedRateDefinition"
    ))
    op.execute(sa.text(
        "ALTER TABLE payroll.PayrollCalculationSnapshotLines DROP COLUMN UsedRateDefinitionID"
    ))
    op.execute(sa.text("DROP TABLE payroll.PayrollCalculationSnapshotUsedRateDefinitions"))
    op.execute(sa.text("DROP TABLE payroll.PayrollPeriodWorkflowActionEvidence"))
    op.execute(sa.text(
        "DELETE FROM sec.Permissions "
        "WHERE PermissionCode IN ('ledger.view', 'ledger.audit.view')"
    ))
