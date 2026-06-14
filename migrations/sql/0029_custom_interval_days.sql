-- 0029: Add CustomIntervalDays to BranchPayrollSettings.
--
-- Stores the inclusive period length in days for fixed-cadence Custom payroll.
-- When payrollfrequency = 'Custom', this column holds the cycle length
-- (e.g. 10 for a 10-day cycle).  Non-Custom frequencies leave it NULL.

ALTER TABLE payroll.BranchPayrollSettings
    ADD COLUMN IF NOT EXISTS CustomIntervalDays INTEGER NULL;

ALTER TABLE payroll.BranchPayrollSettings
    ADD CONSTRAINT ck_BranchPayrollSettings_CustomIntervalDays_positive
    CHECK (CustomIntervalDays IS NULL OR CustomIntervalDays > 0);
