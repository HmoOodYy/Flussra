"""0082: Target Compensation schema and invariants (P3b)."""

import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0082"
down_revision: str | None = "0081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATIONS_DIR = Path(__file__).parent.parent
_SQL_FILE = _MIGRATIONS_DIR / "sql" / "0082_target_compensation_schema.sql"
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file  # noqa: E402

_TARGET_TABLES = (
    "payroll.DriverRateValues",
    "payroll.DriverRateAssignments",
    "payroll.RateComponentDefinitions",
    "payroll.RateDefinitions",
    "payroll.PayDefinitions",
)


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    bind = op.get_bind()
    for table in _TARGET_TABLES:
        if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar_one():
            raise RuntimeError(
                f"Downgrade of 0082 refused: {table} contains rows that would be lost."
            )

    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION core.fn_company_has_durable_monetary_state(p_company_id INTEGER)
        RETURNS BOOLEAN
        LANGUAGE sql STABLE AS $$
            SELECT
                EXISTS (SELECT 1 FROM payroll.driverrates r
                        WHERE r.companyid = p_company_id)
                OR EXISTS (SELECT 1 FROM payroll.driverratetiers t
                           JOIN payroll.driverrates r ON r.driverrateid = t.driverrateid
                           WHERE r.companyid = p_company_id)
                OR EXISTS (SELECT 1 FROM payroll.payrollbonusevents b
                           WHERE b.companyid = p_company_id)
                OR EXISTS (SELECT 1 FROM payroll.driverpayrules p
                           WHERE p.companyid = p_company_id)
                OR EXISTS (SELECT 1 FROM payroll.payrolldraftlines d
                           WHERE d.companyid = p_company_id
                             AND (d.rateamount IS NOT NULL OR d.calculatedamount IS NOT NULL))
                OR EXISTS (SELECT 1 FROM payroll.payrollcalculationsnapshots s
                           WHERE s.companyid = p_company_id)
                OR EXISTS (SELECT 1 FROM payroll.payrollfinallines f
                           WHERE f.companyid = p_company_id);
        $$
    """))
    for table in _TARGET_TABLES:
        op.execute(sa.text(f"DROP TABLE {table}"))
    for function in (
        "payroll.fn_AssertRateStructureComplete(INTEGER)",
        "payroll.fn_AssertRateStructureMutable(INTEGER)",
        "payroll.fn_LockRateDefinitionStructure(INTEGER)",
        "payroll.fn_ShapeForCalculationMethod(VARCHAR)",
        "payroll.trg_PayDefinitions_StructureGuard()",
        "payroll.trg_RateDefinitions_BeforeInsert()",
        "payroll.trg_RateDefinitions_BeforeUpdate()",
        "payroll.trg_RateDefinitions_BeforeDelete()",
        "payroll.trg_RateDefinitions_ShapeMatchesMethod()",
        "payroll.trg_RateComponents_StructureGuard()",
        "payroll.trg_DriverRateAssignments_BeforeInsert()",
        "payroll.trg_DriverRateAssignments_BeforeUpdate()",
        "payroll.trg_DriverRateAssignments_BeforeDelete()",
        "payroll.trg_DriverRateValues_PendingOnly()",
    ):
        op.execute(sa.text(f"DROP FUNCTION {function}"))
    op.execute(sa.text("DROP INDEX payroll.ux_StatusRateColumns_ID_Company_Branch"))
