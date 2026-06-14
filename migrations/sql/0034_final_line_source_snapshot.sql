-- =============================================================================
-- Migration 0034: Final Ledger Source Snapshot
--
-- Adds five nullable columns to payroll.PayrollFinalLines so that every
-- locked line can explain, post-hoc, how its FinalAmount was produced:
--
--   PayItemID          — FK to payroll.PayItems; stable identifier for the
--                        pay item (survives code renames; NULL for SYS lines
--                        that have no PayItems counterpart).
--
--   RateTypeID         — FK to payroll.RateTypes; the rate type (HOURLY,
--                        MILEAGE, …) used to look up the driver's rate.
--                        NULL for EnteredAmount / Fixed / System / None lines.
--
--   DriverRateID       — FK to payroll.DriverRates; the exact Approved or
--                        Superseded row whose Amount was used at finalization.
--                        NULL for EnteredAmount / Fixed / System / None lines.
--
--   ResolvedRateAmount — The per-unit rate amount from the DriverRate row that
--                        was used in the calculation (qty × rate = finalamount).
--                        Captured at finalization so it remains correct even
--                        if the DriverRate is later superseded or voided.
--                        NULL for tiered/block (no single per-unit rate) and
--                        for direct-money / SYS lines.
--
--   RateBehavior       — The PayItem's calculation method at finalization time:
--                        'PerUnit', 'EnteredAmount', 'Fixed', 'OrdinalTier',
--                        'RangeBracket', 'RangeProgressive', 'Block',
--                        'None', 'System'.
--
-- All columns are nullable — safe for existing rows.  Old final lines that
-- pre-date this migration will have NULL for the new columns; PayItemID is
-- backfilled below where possible.
--
-- Backfill strategy:
--   PayItemID: backfill via JOIN on PayItemCode = LineType where the PayItem
--              exists and is not Retired.  Safe; only fills what is unambiguous.
--   RateTypeID, DriverRateID, ResolvedRateAmount, RateBehavior: NOT backfilled
--              for old rows — the information was never stored and cannot be
--              reliably reconstructed.
-- =============================================================================

-- 1. Add the new nullable columns
ALTER TABLE payroll.PayrollFinalLines
    ADD COLUMN IF NOT EXISTS PayItemID          INTEGER      REFERENCES payroll.PayItems(PayItemID),
    ADD COLUMN IF NOT EXISTS RateTypeID         INTEGER      REFERENCES payroll.RateTypes(RateTypeID),
    ADD COLUMN IF NOT EXISTS DriverRateID       BIGINT       REFERENCES payroll.DriverRates(DriverRateID),
    ADD COLUMN IF NOT EXISTS ResolvedRateAmount NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS RateBehavior       VARCHAR(30);

-- 2. Backfill PayItemID for existing rows where a matching PayItem exists.
--    Prefer the custom (company-specific) item over the system item when both
--    exist (ORDER BY pi.companyid NULLS LAST picks system item last).
--    Only backfill where PayItemID is currently NULL and the LineType resolves
--    unambiguously to a non-Retired pay item.
UPDATE payroll.PayrollFinalLines fl
SET    payitemid = sub.payitemid
FROM (
    SELECT DISTINCT ON (fl2.finallineid)
           fl2.finallineid,
           pi.payitemid
    FROM   payroll.PayrollFinalLines fl2
    JOIN   payroll.PayItems          pi  ON pi.payitemcode = fl2.linetype
                                        AND (pi.companyid IS NULL OR pi.companyid = fl2.companyid)
                                        AND pi.status != 'Retired'
    WHERE  fl2.payitemid IS NULL
    ORDER  BY fl2.finallineid, pi.companyid NULLS LAST
) sub
WHERE fl.finallineid = sub.finallineid;
