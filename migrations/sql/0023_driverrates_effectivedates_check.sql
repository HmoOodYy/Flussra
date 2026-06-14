-- =============================================================================
-- Migration 0023: DriverRates EffectiveDates validity CHECK constraint
--
-- Adds a CHECK constraint ensuring EffectiveTo is never earlier than
-- EffectiveFrom on the payroll.DriverRates table.
--
-- Context:
--   The existing EXCLUDE USING gist constraint (excl_DriverRates_no_date_overlap)
--   prevents overlapping Approved/Superseded date ranges at the DB level, which
--   means any existing row that passes the EXCLUDE constraint already satisfies
--   this CHECK.  PendingApproval and Voided rows are exempt from the EXCLUDE but
--   should also never have inverted date ranges.
--
--   The service layer enforces this invariant, but this constraint provides
--   belt-and-suspenders protection against any direct SQL that might bypass the
--   service layer.
-- =============================================================================

ALTER TABLE payroll.DriverRates
    ADD CONSTRAINT ck_DriverRates_EffectiveDates
    CHECK (EffectiveTo IS NULL OR EffectiveTo >= EffectiveFrom);
