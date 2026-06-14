"""0037: DriverRate void guard -- block voiding finalized-used rates.

Adds:
  1. Partial index on payroll.PayrollFinalLines(DriverRateID) WHERE DriverRateID IS NOT NULL
     -- supports O(1) finalized-use check.

  2. payroll.fn_guard_driverrate_void() / trg_guard_driverrate_void
     BEFORE UPDATE OF status ON payroll.DriverRates.
     Raises restrict_violation when NEW.status = 'Voided' and the DriverRateID
     is already referenced by at least one PayrollFinalLines row.
     Non-void status updates and updates on unreferenced rates pass through.

Revision ID: 0037
Revises: 0036
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0037"
down_revision: str = "0036"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Statement 1: partial index on PayrollFinalLines(DriverRateID)
# ---------------------------------------------------------------------------
_ADD_INDEX = """
CREATE INDEX IF NOT EXISTS ix_payrollfinallines_driverrateid
    ON payroll.payrollfinallines (driverrateid)
    WHERE driverrateid IS NOT NULL
"""

# ---------------------------------------------------------------------------
# Statement 2: guard trigger function
# ---------------------------------------------------------------------------
_CREATE_FN_VOID_GUARD = """
CREATE OR REPLACE FUNCTION payroll.fn_guard_driverrate_void()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    -- Only fire when status is transitioning TO Voided.
    IF NEW.status = 'Voided' AND OLD.status <> 'Voided' THEN
        IF EXISTS (
            SELECT 1
            FROM   payroll.payrollfinallines
            WHERE  driverrateid = OLD.driverrateid
            LIMIT  1
        ) THEN
            RAISE EXCEPTION
                'driverrate_void_guard: DriverRate has been used in finalized payroll '
                'and cannot be voided. (driverrateid=%)',
                OLD.driverrateid
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""

# ---------------------------------------------------------------------------
# Statement 3: drop + create trigger
# ---------------------------------------------------------------------------
_DROP_TRIGGER_VOID_GUARD = """
DROP TRIGGER IF EXISTS trg_guard_driverrate_void ON payroll.driverrates
"""

_CREATE_TRIGGER_VOID_GUARD = """
CREATE TRIGGER trg_guard_driverrate_void
    BEFORE UPDATE OF status ON payroll.driverrates
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_driverrate_void()
"""


def upgrade() -> None:
    op.execute(sa.text(_ADD_INDEX))
    op.execute(sa.text(_CREATE_FN_VOID_GUARD))
    op.execute(sa.text(_DROP_TRIGGER_VOID_GUARD))
    op.execute(sa.text(_CREATE_TRIGGER_VOID_GUARD))


def downgrade() -> None:
    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_guard_driverrate_void ON payroll.driverrates"
    ))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS payroll.fn_guard_driverrate_void()"
    ))
    op.execute(sa.text(
        "DROP INDEX IF EXISTS payroll.ix_payrollfinallines_driverrateid"
    ))
