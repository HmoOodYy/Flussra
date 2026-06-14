-- =============================================================================
-- Migration 0010: M14 - Period Pay
--
-- Period Pay lines are stored in the existing payroll.PayrollDraftLines table.
-- This migration adds:
--
--   1. LineScope column - explicit discriminator ('Daily' | 'Period').
--      Needed because WorkDate IS NULL is not a unique identifier: daily lines
--      can also legitimately omit WorkDate.  LineScope is the authoritative
--      flag that identifies Period Pay lines.
--
--   2. Partial index on (PayrollPeriodID, DriverID) WHERE LineScope = 'Period'
--      for efficient period-pay queries and finalization guard sub-selects.
-- =============================================================================

-- 1. LineScope column (DEFAULT 'Daily' back-fills all existing rows correctly)
ALTER TABLE payroll.PayrollDraftLines
    ADD COLUMN LineScope VARCHAR(10) NOT NULL DEFAULT 'Daily';

ALTER TABLE payroll.PayrollDraftLines
    ADD CONSTRAINT ck_PayrollDraftLines_LineScope
    CHECK (LineScope IN ('Daily', 'Period'));

-- 2. Partial index for Period Pay line lookups
CREATE INDEX ix_PayrollDraftLines_Period_PeriodPay
    ON payroll.PayrollDraftLines (PayrollPeriodID, DriverID)
    WHERE LineScope = 'Period';
