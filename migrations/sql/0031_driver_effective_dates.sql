-- =============================================================================
-- Migration 0031: Driver Effective-Date Window
--
-- Adds EffectiveFrom and EffectiveTo DATE columns to core.Drivers so that
-- a completed transfer can control exactly which day each driver profile
-- appears in a branch's day-grid, rather than activating immediately.
--
-- Existing rows: both columns NULL — treated as always valid (legacy-safe).
--
-- On transfer completion:
--   Old source profile: EffectiveTo = effective_date - 1 day
--   New target profile: EffectiveFrom = effective_date
--
-- Day-grid eligibility filter added:
--   AND (d.effectivefrom IS NULL OR d.effectivefrom <= :dt)
--   AND (d.effectiveto   IS NULL OR d.effectiveto   >= :dt)
-- =============================================================================

ALTER TABLE core.Drivers
    ADD COLUMN IF NOT EXISTS EffectiveFrom DATE,
    ADD COLUMN IF NOT EXISTS EffectiveTo   DATE;
