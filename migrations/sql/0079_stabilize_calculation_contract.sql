-- 0079: Stabilize the payroll calculation contract (G0.4C).
--
-- The legacy generic PeriodPay bucket has no source after G0.4B. This
-- migration removes it from the immutable calculation packet schema and
-- establishes the first stable calculation contract identity,
-- 'payroll-calculation-v1' (financial semantics + canonical snapshot/hash
-- shape):
--
--     ExpectedPay = DailyPay + StatusPay
--                 + MinimumAdjustment + MaximumAdjustment + BonusTotal
--
-- Every calculation snapshot persisted so far was captured under the retired
-- pre-production contract 'current-payroll-v1' and carries the PeriodPay
-- column in its canonical hash. Immutable evidence is never reinterpreted or
-- rehashed, and no compatibility reader is built. The migration therefore
-- FAILS CLOSED when any calculation snapshot exists: reset and reseed the
-- development database, then rerun.

DO $$
DECLARE
    v_count BIGINT;
BEGIN
    SELECT COUNT(*) INTO v_count FROM payroll.PayrollCalculationSnapshots;
    IF v_count > 0 THEN
        RAISE EXCEPTION
            'G04C_BLOCKED_RETIRED_CALCULATION_CONTRACT: % PayrollCalculationSnapshots row(s) were captured under the retired pre-production calculation contract (current-payroll-v1) and cannot be reinterpreted as payroll-calculation-v1. Reset and reseed the development database, then rerun.',
            v_count
            USING ERRCODE = 'check_violation';
    END IF;
END;
$$;

ALTER TABLE payroll.PayrollCalculationDriverTotals DROP COLUMN PeriodPay;

ALTER TABLE payroll.PayrollCalculationSnapshots
    ADD CONSTRAINT ck_PayrollCalculationSnapshots_CalculationVersion
    CHECK (CalculationVersion = 'payroll-calculation-v1');
