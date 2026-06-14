-- =============================================================================
-- Migration 0011: M14 fix - LineScope on PayrollFinalLines
--
-- PayrollDraftLines.LineScope was added in migration 0010 to distinguish
-- daily lines from Period Pay lines.  The finalization INSERT copies draft
-- lines into PayrollFinalLines, which must preserve this discriminator so
-- that the immutable ledger can distinguish daily vs period-level pay.
--
-- Without this column, the final ledger loses the Daily/Period distinction
-- and any reporting or audit query against PayrollFinalLines cannot
-- correctly separate daily earnings from period-level bonuses/adjustments.
-- =============================================================================

-- 1. Add LineScope to the final ledger
ALTER TABLE payroll.PayrollFinalLines
    ADD COLUMN LineScope VARCHAR(10) NOT NULL DEFAULT 'Daily';

ALTER TABLE payroll.PayrollFinalLines
    ADD CONSTRAINT ck_PayrollFinalLines_LineScope
    CHECK (LineScope IN ('Daily', 'Period'));

-- 2. Partial index for efficient Period Pay final-line lookups
CREATE INDEX ix_PayrollFinalLines_Period_PeriodPay
    ON payroll.PayrollFinalLines (PayrollPeriodID, DriverID)
    WHERE LineScope = 'Period';
