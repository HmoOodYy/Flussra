"""0038: PayrollFinalLines INSERT guard.

Adds a BEFORE INSERT trigger on payroll.payrollfinallines that blocks all
direct inserts unless the transaction-local GUC
  app.allow_payroll_final_line_insert
is set to 'true'.

The controlled finalization path (finalize_period in service.py) sets this
GUC with is_local=true inside its engine.begin() transaction, so the setting
resets automatically at commit/rollback.

This is Phase 6 of the Payroll Trust series.  It closes the remaining
INSERT gap noted in migration 0035 (which only protected UPDATE/DELETE).

Limitation: any database session that can run
  SELECT set_config('app.allow_payroll_final_line_insert','true',false)
can bypass the trigger.  The guard provides application-convention protection
(accidental direct inserts, ORM bugs) but not cryptographic isolation.
A role-level REVOKE on the application DB user would provide deeper isolation.

Revision ID: 0038
Revises: 0037
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0038"
down_revision: str = "0037"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Guard trigger function
# ---------------------------------------------------------------------------
_CREATE_FN_INSERT_GUARD = """
CREATE OR REPLACE FUNCTION payroll.fn_guard_final_line_insert()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_allowed TEXT;
    v_status  TEXT;
BEGIN
    v_allowed := current_setting('app.allow_payroll_final_line_insert', true);

    IF v_allowed IS DISTINCT FROM 'true' THEN
        SELECT p.status INTO v_status
        FROM   payroll.payrollperiods p
        WHERE  p.payrollperiodid = NEW.payrollperiodid;

        RAISE EXCEPTION
            'payroll_insert_guard: Payroll final lines can only be inserted by the '
            'finalization process. (period_id=%, period_status=%)',
            NEW.payrollperiodid, COALESCE(v_status, 'unknown')
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$
"""

_DROP_TRIGGER_INSERT_GUARD = """
DROP TRIGGER IF EXISTS trg_guard_final_line_insert ON payroll.payrollfinallines
"""

_CREATE_TRIGGER_INSERT_GUARD = """
CREATE TRIGGER trg_guard_final_line_insert
    BEFORE INSERT ON payroll.payrollfinallines
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_final_line_insert()
"""


def upgrade() -> None:
    op.execute(sa.text(_CREATE_FN_INSERT_GUARD))
    op.execute(sa.text(_DROP_TRIGGER_INSERT_GUARD))
    op.execute(sa.text(_CREATE_TRIGGER_INSERT_GUARD))


def downgrade() -> None:
    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_guard_final_line_insert "
        "ON payroll.payrollfinallines"
    ))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS payroll.fn_guard_final_line_insert()"
    ))
