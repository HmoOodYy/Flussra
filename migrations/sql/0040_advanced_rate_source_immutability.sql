-- 0040: Advanced Rate Source Immutability
--
-- Extends payroll trust immutability to cover advanced-rate calculation inputs:
--
-- Part A: Extend fn_guard_driverrate_used_mutation (from migration 0039) to also
--         block UPDATE of BlockSize and RoundingRule on used DriverRates.
--         (Block behavior: the final amount depends on both fields at finalization.)
--
-- Part B: Add fn_guard_driverratetier_used_mutation + trigger on DriverRateTiers.
--         Blocks DELETE and any UPDATE of a tier row whose parent DriverRate has
--         been referenced in PayrollFinalLines.
--         Tier rows (TierSequence, FromUnit, ToUnit, TierAmount) determine the
--         OrdinalTier/RangeBracket/RangeProgressive calculation; they must be
--         frozen once the parent rate is used in finalized payroll.
--
-- Supersession note: superseding a DriverRate sets Status='Superseded' and
-- EffectiveTo on the parent row — those are STATUS/EFFECTIVETO updates, not tier
-- updates, so Part B does not block them. Creating a new rate with new tier rows
-- (a separate DriverRateID) is always allowed. Only mutations to the EXISTING
-- tier rows of a used rate are blocked.

-- Part A: Replace fn_guard_driverrate_used_mutation to also cover BlockSize/RoundingRule

CREATE OR REPLACE FUNCTION payroll.fn_guard_driverrate_used_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    -- DELETE: always blocked if this rate is referenced in finalized lines
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

    -- UPDATE: block changes to identity and calculation fields
    -- Allowed: EffectiveTo (supersession), Status (supersession / void guard)
    IF TG_OP = 'UPDATE' THEN
        IF (
            NEW.amount        IS DISTINCT FROM OLD.amount        OR
            NEW.driverid      IS DISTINCT FROM OLD.driverid      OR
            NEW.ratetypeid    IS DISTINCT FROM OLD.ratetypeid    OR
            NEW.companyid     IS DISTINCT FROM OLD.companyid     OR
            NEW.branchid      IS DISTINCT FROM OLD.branchid      OR
            NEW.effectivefrom IS DISTINCT FROM OLD.effectivefrom OR
            -- Phase 11: also protect Block calculation fields
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
$$;

-- Part B: Guard function for DriverRateTiers

CREATE OR REPLACE FUNCTION payroll.fn_guard_driverratetier_used_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    -- DELETE: blocked if parent DriverRate is referenced in finalized lines
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

    -- UPDATE: block any field change if parent DriverRate is used
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
$$;

-- Trigger on DriverRateTiers
DROP TRIGGER IF EXISTS trg_guard_driverratetier_used_mutation ON payroll.driverratetiers;
CREATE TRIGGER trg_guard_driverratetier_used_mutation
    BEFORE UPDATE OR DELETE ON payroll.driverratetiers
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_driverratetier_used_mutation();
