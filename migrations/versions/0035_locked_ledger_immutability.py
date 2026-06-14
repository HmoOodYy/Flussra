"""0035: DB-level immutability guards for Locked/Archived payroll ledger.

Adds two trigger-function + trigger pairs:

  1. payroll.fn_guard_final_line_immutable  /  payroll.trg_final_line_immutable
     BEFORE UPDATE OR DELETE on payroll.PayrollFinalLines.
     Raises restrict_violation when the parent period is Locked or Archived.
     INSERT is intentionally NOT blocked: finalize_period sets the period to
     Locked (Step 2) and then INSERTs the final lines (Step 3) in the same
     transaction.  Blocking INSERT would break finalization.

     Remaining lower-priority gap: a future migration may add INSERT protection
     via a session-level GUC flag set by the finalization service.

  2. payroll.fn_guard_period_status_revert  /  payroll.trg_period_status_revert
     BEFORE UPDATE OF status on payroll.PayrollPeriods.
     Locked → Archived: allowed (the only supported forward transition).
     Locked → anything else: rejected.
     Archived → anything: rejected (terminal status).
     Non-final statuses: pass through unchanged.

Revision ID: 0035
Revises: 0034
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0035"
down_revision: str = "0034"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Statement 1: final-line guard function
# ---------------------------------------------------------------------------
_CREATE_FN_FINAL_LINE = """
CREATE OR REPLACE FUNCTION payroll.fn_guard_final_line_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_status TEXT;
BEGIN
    SELECT p.status
    INTO   v_status
    FROM   payroll.payrollperiods p
    WHERE  p.payrollperiodid = OLD.payrollperiodid;

    IF v_status IN ('Locked', 'Archived') THEN
        RAISE EXCEPTION
            'payroll_ledger_immutable: Payroll final lines for Locked or Archived '
            'periods cannot be modified or deleted. (period_id=%, status=%)',
            OLD.payrollperiodid, v_status
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    ELSE
        RETURN NEW;
    END IF;
END;
$$
"""

# ---------------------------------------------------------------------------
# Statement 2: final-line trigger
# ---------------------------------------------------------------------------
_DROP_TRIGGER_FINAL_LINE = """
DROP TRIGGER IF EXISTS trg_final_line_immutable ON payroll.payrollfinallines
"""

_CREATE_TRIGGER_FINAL_LINE = """
CREATE TRIGGER trg_final_line_immutable
    BEFORE UPDATE OR DELETE ON payroll.payrollfinallines
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_final_line_immutable()
"""

# ---------------------------------------------------------------------------
# Statement 3: period-status revert guard function
# ---------------------------------------------------------------------------
_CREATE_FN_PERIOD_STATUS = """
CREATE OR REPLACE FUNCTION payroll.fn_guard_period_status_revert()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    -- Only act when Status is actually changing.
    IF OLD.status = NEW.status THEN
        RETURN NEW;
    END IF;

    IF OLD.status = 'Locked' THEN
        -- Locked -> Archived is the only permitted forward transition.
        IF NEW.status = 'Archived' THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'payroll_status_immutable: Locked or Archived payroll periods cannot be '
            'reverted to an editable status. (period_id=%, attempted_transition=%->%)',
            OLD.payrollperiodid, OLD.status, NEW.status
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'Archived' THEN
        -- Archived is terminal - no transitions allowed.
        RAISE EXCEPTION
            'payroll_status_immutable: Locked or Archived payroll periods cannot be '
            'reverted to an editable status. (period_id=%, attempted_transition=%->%)',
            OLD.payrollperiodid, OLD.status, NEW.status
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- Non-final statuses: no restriction.
    RETURN NEW;
END;
$$
"""

# ---------------------------------------------------------------------------
# Statement 4: period-status revert trigger
# ---------------------------------------------------------------------------
_DROP_TRIGGER_PERIOD_STATUS = """
DROP TRIGGER IF EXISTS trg_period_status_revert ON payroll.payrollperiods
"""

_CREATE_TRIGGER_PERIOD_STATUS = """
CREATE TRIGGER trg_period_status_revert
    BEFORE UPDATE OF status ON payroll.payrollperiods
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_period_status_revert()
"""


def upgrade() -> None:
    op.execute(sa.text(_CREATE_FN_FINAL_LINE))
    op.execute(sa.text(_DROP_TRIGGER_FINAL_LINE))
    op.execute(sa.text(_CREATE_TRIGGER_FINAL_LINE))
    op.execute(sa.text(_CREATE_FN_PERIOD_STATUS))
    op.execute(sa.text(_DROP_TRIGGER_PERIOD_STATUS))
    op.execute(sa.text(_CREATE_TRIGGER_PERIOD_STATUS))


def downgrade() -> None:
    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_period_status_revert ON payroll.payrollperiods"
    ))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS payroll.fn_guard_period_status_revert()"
    ))
    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_final_line_immutable ON payroll.payrollfinallines"
    ))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS payroll.fn_guard_final_line_immutable()"
    ))
