-- Preserve the user's intended effective date while a Payroll Setup Version is Draft.
-- Published Versions remain authoritative through EffectiveFromDate only.

ALTER TABLE payroll.PayrollSetupVersions
    ADD COLUMN PlannedEffectiveFromDate DATE;

ALTER TABLE payroll.PayrollSetupVersions
    ADD CONSTRAINT ck_PayrollSetupVersions_PlannedEffectiveDraft
    CHECK (LifecycleState = 'Draft' OR PlannedEffectiveFromDate IS NULL);
