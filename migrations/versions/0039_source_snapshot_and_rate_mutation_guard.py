"""0039: SourceSnapshot JSONB column + DriverRate used-mutation guard.

Part A — adds ``sourcesnapshot JSONB NULL`` to ``payroll.payrollfinallines``.
         Stores the driver rate, pay item, and rate type metadata that were in
         effect at the exact moment of finalization.  NULL for rows finalized
         before this migration.

Part B — adds ``trg_guard_driverrate_used_mutation`` (BEFORE UPDATE OR DELETE)
         on ``payroll.driverrates``.  Blocks DELETE and UPDATE of identity /
         calculation fields (Amount, DriverID, RateTypeID, CompanyID, BranchID,
         EffectiveFrom) when the rate has been referenced in a finalized
         PayrollFinalLines row.  Allows EffectiveTo and Status changes so the
         normal supersession path (_supersede_current_approved_rates) continues
         to work without modification.

This is Phase 9 of the Payroll Trust series.

Revision ID: 0039
Revises: 0038
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0039"
down_revision: str = "0038"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# SQL strings
# ---------------------------------------------------------------------------

_ADD_COLUMN = """
ALTER TABLE payroll.payrollfinallines
    ADD COLUMN IF NOT EXISTS sourcesnapshot JSONB NULL
"""

_DROP_COLUMN = """
ALTER TABLE payroll.payrollfinallines
    DROP COLUMN IF EXISTS sourcesnapshot
"""

_CREATE_FN_MUTATION_GUARD = """
CREATE OR REPLACE FUNCTION payroll.fn_guard_driverrate_used_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF EXISTS (
            SELECT 1 FROM payroll.payrollfinallines
            WHERE  driverrateid = OLD.driverrateid
            LIMIT  1
        ) THEN
            RAISE EXCEPTION
                'driverrate_mutation_guard: DriverRate has been used in finalized '
                'payroll and cannot be deleted. (driverrateid=%)',
                OLD.driverrateid
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF (
            NEW.amount        IS DISTINCT FROM OLD.amount        OR
            NEW.driverid      IS DISTINCT FROM OLD.driverid      OR
            NEW.ratetypeid    IS DISTINCT FROM OLD.ratetypeid    OR
            NEW.companyid     IS DISTINCT FROM OLD.companyid     OR
            NEW.branchid      IS DISTINCT FROM OLD.branchid      OR
            NEW.effectivefrom IS DISTINCT FROM OLD.effectivefrom
        ) THEN
            IF EXISTS (
                SELECT 1 FROM payroll.payrollfinallines
                WHERE  driverrateid = OLD.driverrateid
                LIMIT  1
            ) THEN
                RAISE EXCEPTION
                    'driverrate_mutation_guard: DriverRate has been used in finalized '
                    'payroll. Critical fields (Amount, DriverID, RateTypeID, CompanyID, '
                    'BranchID, EffectiveFrom) cannot be changed. (driverrateid=%)',
                    OLD.driverrateid
                    USING ERRCODE = 'restrict_violation';
            END IF;
        END IF;
        RETURN NEW;
    END IF;

    RETURN NEW;
END;
$$
"""

_DROP_TRIGGER_MUTATION_GUARD = """
DROP TRIGGER IF EXISTS trg_guard_driverrate_used_mutation ON payroll.driverrates
"""

_CREATE_TRIGGER_MUTATION_GUARD = """
CREATE TRIGGER trg_guard_driverrate_used_mutation
    BEFORE UPDATE OR DELETE ON payroll.driverrates
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_driverrate_used_mutation()
"""


def upgrade() -> None:
    op.execute(sa.text(_ADD_COLUMN))
    op.execute(sa.text(_CREATE_FN_MUTATION_GUARD))
    op.execute(sa.text(_DROP_TRIGGER_MUTATION_GUARD))
    op.execute(sa.text(_CREATE_TRIGGER_MUTATION_GUARD))


def downgrade() -> None:
    op.execute(sa.text(_DROP_TRIGGER_MUTATION_GUARD))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS payroll.fn_guard_driverrate_used_mutation()"
    ))
    op.execute(sa.text(_DROP_COLUMN))
