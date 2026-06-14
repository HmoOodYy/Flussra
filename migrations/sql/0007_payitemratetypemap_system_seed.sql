-- =============================================================================
-- Migration 0007: Seed PayItemRateTypeMap for system PerUnit pay items
-- =============================================================================
--
-- Establishes the canonical PayItem -> RateType mapping for the 6 system-standard
-- PerUnit items.  After this migration the calculation engine can look up
-- rate_code via the DB (PayItemRateTypeMap) instead of the transitional
-- hardcoded _SYSTEM_LINE_TYPE_INFO dict in payroll/service.py.
--
-- Items seeded:
--   HOURS     -> HOURLY   (Hourly Rate)
--   MILES     -> MILEAGE  (Mileage Rate)
--   LOADS     -> LOAD     (Load Rate)
--   WAIT_TIME -> WAIT     (Wait Time Rate)
--   PALLETS   -> PALLET   (Pallet Rate)
--   SILOS     -> SILO     (Silo Rate)
--
-- OVERNIGHT is Fixed-rate (no PerUnit calculation), so no mapping is needed.
-- PTO_STATUS, BONUS, ADJUSTMENT, GUARANTEED_MINIMUM have no PerUnit rate -> omitted.
--
-- ON CONFLICT DO NOTHING is safe for repeated runs (idempotent).

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
