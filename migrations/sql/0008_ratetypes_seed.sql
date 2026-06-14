-- =============================================================================
-- Migration 0008: Seed payroll.RateTypes + idempotent PayItemRateTypeMap re-seed
-- =============================================================================
--
-- Root cause fixed here:
--   Migration 0007 inserts into PayItemRateTypeMap by joining payroll.RateTypes,
--   but RateTypes were never seeded by Alembic -- they were only seeded by the
--   test-session fixture (conftest._SEED_STMTS).  On a fresh 'alembic upgrade head'
--   without the test fixture, 0007 produces zero rows in PayItemRateTypeMap because
--   the JOIN finds nothing.
--
-- Fix:
--   This migration seeds all 7 production rate types first, then re-runs the same
--   PayItemRateTypeMap insert from 0007 (idempotent via ON CONFLICT DO NOTHING).
--
-- Rate types seeded (all system-standard):
--   HOURLY    -> Hours      PerUnit items
--   MILEAGE   -> Miles      PerUnit items
--   LOAD      -> Loads      PerUnit items
--   OVERNIGHT -> Overnight  Fixed items (rate stored but not used for calc in M13b)
--   WAIT      -> Wait       PerUnit items
--   PALLET    -> Pallets    PerUnit items
--   SILO      -> Silos      PerUnit items
--
-- ON CONFLICT DO NOTHING is safe for repeated runs (idempotent on RateCode).

INSERT INTO payroll.RateTypes (RateCode, RateName, UnitName, IsActive)
VALUES
    ('HOURLY',    'Hourly Rate',    'Hour',   TRUE),
    ('MILEAGE',   'Mileage Rate',   'Mile',   TRUE),
    ('LOAD',      'Load Rate',      'Load',   TRUE),
    ('OVERNIGHT', 'Overnight Rate', 'Night',  TRUE),
    ('WAIT',      'Wait Time Rate', 'Hour',   TRUE),
    ('PALLET',    'Pallet Rate',    'Pallet', TRUE),
    ('SILO',      'Silo Rate',      'Silo',   TRUE)
ON CONFLICT (RateCode) DO NOTHING;

-- Re-seed PayItemRateTypeMap for the 6 system PerUnit pay items.
-- Idempotent with migration 0007 (ON CONFLICT DO NOTHING on the unique key).
-- Now succeeds on a fresh DB because RateTypes are seeded above.

INSERT INTO payroll.PayItemRateTypeMap (PayItemID, RateTypeID, IsPrimary, Status)
SELECT
    pi.PayItemID,
    rt.RateTypeID,
    TRUE       AS IsPrimary,
    'Active'   AS Status
FROM   payroll.PayItems  pi
JOIN   payroll.RateTypes rt
    ON rt.RateCode = CASE pi.PayItemCode
        WHEN 'HOURS'     THEN 'HOURLY'
        WHEN 'MILES'     THEN 'MILEAGE'
        WHEN 'LOADS'     THEN 'LOAD'
        WHEN 'WAIT_TIME' THEN 'WAIT'
        WHEN 'PALLETS'   THEN 'PALLET'
        WHEN 'SILOS'     THEN 'SILO'
    END
WHERE  pi.PayItemCode IN ('HOURS', 'MILES', 'LOADS', 'WAIT_TIME', 'PALLETS', 'SILOS')
  AND  pi.CompanyID IS NULL
ON CONFLICT DO NOTHING;
