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
-- This is a clean contract cutover. Every calculation snapshot persisted so
-- far was captured under the retired pre-production contract
-- 'current-payroll-v1', and finalized history (FinalLines, Locked/Archived
-- periods) can predate calculation snapshots entirely. Immutable evidence is
-- never reinterpreted, rewritten, rehashed or deleted, and no compatibility
-- reader is built. The migration therefore FAILS CLOSED when any calculation
-- snapshot, FinalLine, or Locked/Archived period exists: reset and reseed the
-- development database, then rerun.

DO $$
DECLARE
    v_count BIGINT;
BEGIN
    SELECT (SELECT COUNT(*) FROM payroll.PayrollCalculationSnapshots)
         + (SELECT COUNT(*) FROM payroll.PayrollFinalLines)
         + (SELECT COUNT(*) FROM payroll.PayrollPeriods
            WHERE Status IN ('Locked', 'Archived'))
    INTO v_count;
    IF v_count > 0 THEN
        RAISE EXCEPTION
            'G04C_BLOCKED_RETIRED_CALCULATION_CONTRACT: the database contains % pre-production payroll history row(s) (calculation snapshots, final lines, or Locked/Archived periods) from before the stable calculation contract payroll-calculation-v1. They cannot be reinterpreted. Reset and reseed the development database, then rerun.',
            v_count
            USING ERRCODE = 'check_violation';
    END IF;
END;
$$;

ALTER TABLE payroll.PayrollCalculationDriverTotals DROP COLUMN PeriodPay;

ALTER TABLE payroll.PayrollCalculationSnapshots
    ADD CONSTRAINT ck_PayrollCalculationSnapshots_CalculationVersion
    CHECK (CalculationVersion = 'payroll-calculation-v1');
