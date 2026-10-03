"""0076: P3a company currency authority with clean-state preflight."""

import sys
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0076"
down_revision: str | None = "0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATIONS_DIR = Path(__file__).parent.parent
_SQL_FILE = _MIGRATIONS_DIR / "sql" / "0076_company_currency_authority.sql"
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
from utils import statements_from_file


def upgrade() -> None:
    for statement in statements_from_file(_SQL_FILE):
        op.execute(sa.text(statement))


def downgrade() -> None:
    bind = op.get_bind()
    has_currency = bind.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM core.companies WHERE currencycode IS NOT NULL)"
    )).scalar_one()
    has_evidence = bind.execute(sa.text("""
        SELECT EXISTS (SELECT 1 FROM payroll.payrollcalculationsnapshots)
            OR EXISTS (SELECT 1 FROM payroll.payrollcalculationsnapshotusedratedefinitions)
            OR EXISTS (SELECT 1 FROM payroll.payrollcalculationsnapshotbonusevents)
            OR EXISTS (SELECT 1 FROM payroll.payrollfinallines)
    """)).scalar_one()
    if has_currency or has_evidence:
        raise RuntimeError(
            "Downgrade of 0076 refused: configured Company currency or frozen "
            "currency evidence would be destroyed."
        )

    op.execute(sa.text("DROP TRIGGER trg_Companies_CurrencyChange ON core.Companies"))
    op.execute(sa.text("DROP FUNCTION core.fn_guard_company_currency_change()"))
    op.execute(sa.text("DROP FUNCTION core.fn_company_has_durable_monetary_state(INTEGER)"))
    for table in (
        "payroll.PayrollFinalLines",
        "payroll.PayrollCalculationSnapshotBonusEvents",
        "payroll.PayrollCalculationSnapshotUsedRateDefinitions",
        "payroll.PayrollCalculationSnapshots",
    ):
        if table.endswith(("BonusEvents", "UsedRateDefinitions")):
            op.execute(sa.text(
                f"ALTER TABLE {table} DROP COLUMN CurrencyCodeSnapshot, "
                "DROP COLUMN CurrencyMinorUnitDigitsSnapshot"
            ))
        else:
            op.execute(sa.text(
                f"ALTER TABLE {table} DROP COLUMN CurrencyCode, "
                "DROP COLUMN CurrencyMinorUnitDigits"
            ))
    op.execute(sa.text("ALTER TABLE core.Companies DROP COLUMN CurrencyCode"))
    op.execute(sa.text("DROP TABLE core.SupportedCurrencies"))
