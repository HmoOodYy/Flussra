-- 0039: SourceSnapshot JSONB column + DriverRate used-mutation guard
--
-- Part A: Add SourceSnapshot JSONB column to PayrollFinalLines.
--         Stores a rich audit record of the driver rate, pay item, and rate type
--         that were in effect at the moment of finalization.
--         NULL for pre-Phase-9 rows (finalized before this migration).
--
-- Part B: Extend DriverRate protection beyond the void guard (migration 0037).
--         Blocks DELETE and critical-field UPDATE on any DriverRate that has
--         been referenced in a finalized PayrollFinalLines row.
--         Allowed: EffectiveTo and Status changes (needed for supersession).

-- Part A: SourceSnapshot column

ALTER TABLE payroll.payrollfinallines
    ADD COLUMN IF NOT EXISTS sourcesnapshot JSONB NULL;

-- Part B: DriverRate used-mutation guard

-- Guard trigger function
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

    -- UPDATE: block changes to identity/calculation fields only
    -- Allow: EffectiveTo (supersession), Status (supersession/void guard handles void separately)
    IF TG_OP = 'UPDATE' THEN
        -- Check if any critical field changed
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
$$;

-- Trigger covering both UPDATE and DELETE
DROP TRIGGER IF EXISTS trg_guard_driverrate_used_mutation ON payroll.driverrates;
CREATE TRIGGER trg_guard_driverrate_used_mutation
    BEFORE UPDATE OR DELETE ON payroll.driverrates
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_driverrate_used_mutation();
