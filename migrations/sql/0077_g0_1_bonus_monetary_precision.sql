-- G0.1: align the canonical Bonus source amount with internal NUMERIC(18,4) precision.
-- Existing 18,2 values fit; reject old-schema values outside the target integer range.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM payroll.PayrollBonusEvents
        WHERE ABS(Amount) >= 100000000000000::NUMERIC
    ) THEN
        RAISE EXCEPTION
            'G0.1 preflight failed: PayrollBonusEvents.Amount contains a value outside NUMERIC(18,4); inspect development data before retrying.'
            USING ERRCODE = 'numeric_value_out_of_range';
    END IF;
END;
$$;

ALTER TABLE payroll.PayrollBonusEvents
    ALTER COLUMN Amount TYPE NUMERIC(18,4)
    USING Amount::NUMERIC(18,4);
