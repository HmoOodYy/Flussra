-- 0037: DriverRate void guard
-- Partial index + DB trigger blocking void of finalized-used DriverRates.

-- 1. Partial index for O(1) finalized-use lookup
CREATE INDEX IF NOT EXISTS ix_payrollfinallines_driverrateid
    ON payroll.payrollfinallines (driverrateid)
    WHERE driverrateid IS NOT NULL;

-- 2. Guard trigger function
CREATE OR REPLACE FUNCTION payroll.fn_guard_driverrate_void()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
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
$$;

-- 3. Trigger
DROP TRIGGER IF EXISTS trg_guard_driverrate_void ON payroll.driverrates;
CREATE TRIGGER trg_guard_driverrate_void
    BEFORE UPDATE OF status ON payroll.driverrates
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_driverrate_void();
