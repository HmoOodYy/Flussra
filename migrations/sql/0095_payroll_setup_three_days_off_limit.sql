-- Raise the Normal Days Off product limit from two to three days (four-day work weeks).
-- This only loosens the constraint, so no existing row can violate it. Discarded
-- Drafts remain exempt, and the mask keeps its 7-bit 0..127 range.

ALTER TABLE payroll.PayrollSetupVersions DROP CONSTRAINT ck_PayrollSetupVersions_Mask;

ALTER TABLE payroll.PayrollSetupVersions ADD CONSTRAINT ck_PayrollSetupVersions_Mask
    CHECK (NormalDaysOffMask IS NULL OR (NormalDaysOffMask BETWEEN 0 AND 127
        AND (DiscardedAtUtc IS NOT NULL
             OR length(replace((NormalDaysOffMask::integer)::bit(7)::text, '0', '')) <= 3)));
