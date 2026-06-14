"""0040: Advanced rate source immutability.

Part A — Extends ``fn_guard_driverrate_used_mutation`` (migration 0039) to also
         block UPDATE of ``BlockSize`` and ``RoundingRule`` on used DriverRates.
         Block-behavior final amounts depend on both fields; they must be frozen
         once the rate has been referenced in finalized payroll.

Part B — Adds ``fn_guard_driverratetier_used_mutation`` and
         ``trg_guard_driverratetier_used_mutation`` (BEFORE UPDATE OR DELETE) on
         ``payroll.driverratetiers``.  Blocks any mutation of a tier row whose
         parent DriverRate is referenced in ``payroll.payrollfinallines``.
         OrdinalTier / RangeBracket / RangeProgressive calculations are driven
         entirely by their tier rows; those rows must not change after finalization.

This is Phase 11 of the Payroll Trust series.

Revision ID: 0040
Revises: 0039
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0040"
down_revision: str = "0039"
branch_labels = None
depends_on = None


_REPLACE_FN_DRIVERRATE_GUARD = """
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
            NEW.effectivefrom IS DISTINCT FROM OLD.effectivefrom OR
            NEW.blocksize     IS DISTINCT FROM OLD.blocksize     OR
            NEW.roundingrule  IS DISTINCT FROM OLD.roundingrule
        ) THEN
            IF EXISTS (
                SELECT 1 FROM payroll.payrollfinallines
                WHERE  driverrateid = OLD.driverrateid
                LIMIT  1
            ) THEN
                RAISE EXCEPTION
                    'driverrate_mutation_guard: DriverRate has been used in finalized '
                    'payroll. Critical fields (Amount, DriverID, RateTypeID, CompanyID, '
                    'BranchID, EffectiveFrom, BlockSize, RoundingRule) cannot be changed. '
                    '(driverrateid=%)',
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

_CREATE_FN_TIER_GUARD = """
CREATE OR REPLACE FUNCTION payroll.fn_guard_driverratetier_used_mutation()
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
                'driverratetier_mutation_guard: DriverRateTier belongs to a DriverRate '
                'used in finalized payroll and cannot be deleted. '
                '(driverrateid=%, tiersequence=%)',
                OLD.driverrateid, OLD.tiersequence
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF EXISTS (
            SELECT 1 FROM payroll.payrollfinallines
            WHERE  driverrateid = OLD.driverrateid
            LIMIT  1
        ) THEN
            RAISE EXCEPTION
                'driverratetier_mutation_guard: DriverRateTier belongs to a DriverRate '
                'used in finalized payroll and cannot be modified. '
                '(driverrateid=%, tiersequence=%)',
                OLD.driverrateid, OLD.tiersequence
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    RETURN NEW;
END;
$$
"""

_DROP_TRIGGER_TIER_GUARD = """
DROP TRIGGER IF EXISTS trg_guard_driverratetier_used_mutation ON payroll.driverratetiers
"""

_CREATE_TRIGGER_TIER_GUARD = """
CREATE TRIGGER trg_guard_driverratetier_used_mutation
    BEFORE UPDATE OR DELETE ON payroll.driverratetiers
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_driverratetier_used_mutation()
"""


def upgrade() -> None:
    # Part A: replace the DriverRate mutation guard with the extended version
    op.execute(sa.text(_REPLACE_FN_DRIVERRATE_GUARD))
    # The existing trigger (trg_guard_driverrate_used_mutation) calls this function
    # and does not need to be recreated — CREATE OR REPLACE updates the function body.

    # Part B: new tier-row guard
    op.execute(sa.text(_CREATE_FN_TIER_GUARD))
    op.execute(sa.text(_DROP_TRIGGER_TIER_GUARD))
    op.execute(sa.text(_CREATE_TRIGGER_TIER_GUARD))


def downgrade() -> None:
    op.execute(sa.text(_DROP_TRIGGER_TIER_GUARD))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS payroll.fn_guard_driverratetier_used_mutation()"
    ))
    # Restore fn_guard_driverrate_used_mutation to 0039 version (without BlockSize/RoundingRule)
    op.execute(sa.text("""
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
"""))
