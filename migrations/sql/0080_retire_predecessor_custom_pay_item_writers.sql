-- 0080: Retire predecessor Custom Pay Item writers (G0.5).
--
-- Custom Daily PayItem definition and governance authority is CDPI
-- (CdpiRequests / CdpiRequestEvents / CdpiDefinitions). Every company-owned,
-- non-system PayItem created by the current product has a CdpiDefinitions row.
-- This migration removes the predecessor architecture that CDPI replaced:
--
--   * payroll.CustomPayItemRequests (the predecessor request workflow) is
--     dropped. It is NOT converted into CDPI history: the request/approval
--     semantics differ and no CDPI provenance can be honestly invented.
--   * payroll.PayItemSettings is dropped. Its only content was the predecessor
--     wizard's rate_name_* labels; no current authority reads or writes it.
--   * Company-owned, non-system PayItems with no CdpiDefinitions row are
--     predecessor residue with no surviving owner. They are deleted together
--     with their disposable configuration chain: branch configuration, frozen
--     period layout rows, structural maps/slots, source DraftLines, and the
--     predecessor-only RateTypes (with their DriverRates) that no surviving
--     PayItem uses.
--
-- Kept deliberately: system PayItems; CDPI-managed PayItems and all CDPI
-- tables; PayItems.RequestingBranchID and DisplayLabel (both written by CDPI);
-- RateTypes, PayItemRateTypeMap, PayItemRateSlots, DriverRates for surviving
-- items; BranchPayItemConfig for surviving items.
--
-- Pre-production policy: understood disposable residue is deleted; the
-- migration is FAIL-CLOSED when finalized/immutable evidence or another
-- phase's data (Pay Profiles, Status rate columns) depends on a row it would
-- delete. Reset and reseed the development database, then rerun.
--
-- Historical migrations are NOT rewritten.

-- ---------------------------------------------------------------------------
-- 0. Characterize the ownerless predecessor PayItems.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE g05_ownerless_payitems AS
SELECT pi.PayItemID, pi.CompanyID, pi.PayItemCode
FROM payroll.PayItems pi
WHERE pi.CompanyID IS NOT NULL
  AND NOT pi.IsSystemStandard
  AND NOT EXISTS (
      SELECT 1 FROM payroll.CdpiDefinitions d WHERE d.PayItemID = pi.PayItemID
  );

-- RateTypes reachable only through ownerless PayItems (via map or slot).
CREATE TEMP TABLE g05_orphan_ratetypes AS
SELECT DISTINCT x.RateTypeID
FROM (
    SELECT m.RateTypeID, m.PayItemID FROM payroll.PayItemRateTypeMap m
    UNION
    SELECT s.RateTypeID, s.PayItemID FROM payroll.PayItemRateSlots s
) x
WHERE x.PayItemID IN (SELECT PayItemID FROM g05_ownerless_payitems)
  AND NOT EXISTS (
      SELECT 1 FROM payroll.PayItemRateTypeMap m2
      WHERE m2.RateTypeID = x.RateTypeID
        AND m2.PayItemID NOT IN (SELECT PayItemID FROM g05_ownerless_payitems)
  )
  AND NOT EXISTS (
      SELECT 1 FROM payroll.PayItemRateSlots s2
      WHERE s2.RateTypeID = x.RateTypeID
        AND s2.PayItemID NOT IN (SELECT PayItemID FROM g05_ownerless_payitems)
  );

-- Source DraftLines that exist only because of an ownerless PayItem.
CREATE TEMP TABLE g05_ownerless_draftlines AS
SELECT dl.DraftLineID, dl.PayrollPeriodID
FROM payroll.PayrollDraftLines dl
JOIN g05_ownerless_payitems o
  ON o.CompanyID = dl.CompanyID AND o.PayItemCode = dl.LineType;

-- ---------------------------------------------------------------------------
-- 1. PREFLIGHT: fail closed on finalized/immutable evidence or cross-phase
--    data that depends on rows this migration deletes.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_count BIGINT;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM payroll.PayrollFinalLines f
    WHERE f.PayItemID IN (SELECT PayItemID FROM g05_ownerless_payitems)
       OR f.DraftLineID IN (SELECT DraftLineID FROM g05_ownerless_draftlines)
       OR f.RateTypeID IN (SELECT RateTypeID FROM g05_orphan_ratetypes)
       OR f.DriverRateID IN (
            SELECT dr.DriverRateID FROM payroll.DriverRates dr
            WHERE dr.RateTypeID IN (SELECT RateTypeID FROM g05_orphan_ratetypes));
    IF v_count > 0 THEN
        RAISE EXCEPTION
            'G05_BLOCKED_FINAL_LEDGER_DEPENDENCY: % PayrollFinalLines row(s) depend on predecessor custom PayItems. Reset and reseed the development database, then rerun.',
            v_count
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM payroll.PayrollCalculationSnapshotLines sl
    WHERE sl.PayItemID IN (SELECT PayItemID FROM g05_ownerless_payitems)
       OR sl.RateTypeID IN (SELECT RateTypeID FROM g05_orphan_ratetypes)
       OR (sl.SourceType = 'DraftLine'
           AND sl.SourceID IN (SELECT DraftLineID::TEXT FROM g05_ownerless_draftlines));
    IF v_count > 0 THEN
        RAISE EXCEPTION
            'G05_BLOCKED_CALCULATION_SNAPSHOT_DEPENDENCY: % PayrollCalculationSnapshotLines row(s) depend on predecessor custom PayItems. Reset and reseed the development database, then rerun.',
            v_count
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM payroll.PayrollCalculationSnapshotUsedRateDefinitions ud
    WHERE ud.PayItemID IN (SELECT PayItemID FROM g05_ownerless_payitems)
       OR ud.RateTypeID IN (SELECT RateTypeID FROM g05_orphan_ratetypes);
    IF v_count > 0 THEN
        RAISE EXCEPTION
            'G05_BLOCKED_USED_RATE_DEFINITION_DEPENDENCY: % PayrollCalculationSnapshotUsedRateDefinitions row(s) depend on predecessor custom PayItems. Reset and reseed the development database, then rerun.',
            v_count
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM g05_ownerless_draftlines dl
    JOIN payroll.PayrollPeriods p ON p.PayrollPeriodID = dl.PayrollPeriodID
    WHERE p.Status IN ('Locked', 'Archived');
    IF v_count > 0 THEN
        RAISE EXCEPTION
            'G05_BLOCKED_FINALIZED_PERIOD_DEPENDENCY: % predecessor custom PayItem DraftLine(s) belong to Locked or Archived periods. Reset and reseed the development database, then rerun.',
            v_count
            USING ERRCODE = 'check_violation';
    END IF;

    -- PayrollPeriodPayItems is the frozen PayItem layout of a period. Once a
    -- period has left the editable states it is evidence, and this migration
    -- must neither delete it nor rewrite it.
    SELECT COUNT(*) INTO v_count
    FROM payroll.PayrollPeriodPayItems pppi
    JOIN payroll.PayrollPeriods p ON p.PayrollPeriodID = pppi.PayrollPeriodID
    WHERE pppi.PayItemID IN (SELECT PayItemID FROM g05_ownerless_payitems)
      AND p.Status IN ('InReview', 'Approved', 'Locked', 'Archived');
    IF v_count > 0 THEN
        RAISE EXCEPTION
            'G05_BLOCKED_FROZEN_PERIOD_LAYOUT_DEPENDENCY: % frozen PayrollPeriodPayItems row(s) of InReview, Approved, Locked or Archived periods contain predecessor custom PayItems that have no CDPI definition. Reset and reseed the development database, then rerun.',
            v_count
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM payroll.CdpiRequests r
    WHERE r.ApprovedPayItemID IN (SELECT PayItemID FROM g05_ownerless_payitems);
    IF v_count > 0 THEN
        RAISE EXCEPTION
            'G05_BLOCKED_CDPI_DEPENDENCY: % CDPI request(s) were approved into PayItems that have no CDPI definition. Reset and reseed the development database, then rerun.',
            v_count
            USING ERRCODE = 'check_violation';
    END IF;

    -- Pay Profiles (G0.6) and Status rate columns own their own data; this
    -- migration must not silently delete another phase's rows.
    SELECT (SELECT COUNT(*) FROM payroll.PayProfilePayItems
            WHERE RateTypeID IN (SELECT RateTypeID FROM g05_orphan_ratetypes))
         + (SELECT COUNT(*) FROM payroll.PayProfileRates
            WHERE RateTypeID IN (SELECT RateTypeID FROM g05_orphan_ratetypes))
         + (SELECT COUNT(*) FROM payroll.StatusRateColumns
            WHERE RateTypeID IN (SELECT RateTypeID FROM g05_orphan_ratetypes))
    INTO v_count;
    IF v_count > 0 THEN
        RAISE EXCEPTION
            'G05_BLOCKED_CROSS_PHASE_DEPENDENCY: % Pay Profile or Status rate row(s) use RateTypes that belong only to predecessor custom PayItems. Reset and reseed the development database, then rerun.',
            v_count
            USING ERRCODE = 'check_violation';
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- 2. Remove the ownerless predecessor PayItems and their disposable chain,
--    and drop the predecessor tables.
-- ---------------------------------------------------------------------------
DELETE FROM payroll.PayrollDraftLines
WHERE DraftLineID IN (SELECT DraftLineID FROM g05_ownerless_draftlines);

DELETE FROM payroll.PayrollPeriodPayItems
WHERE PayItemID IN (SELECT PayItemID FROM g05_ownerless_payitems);

DELETE FROM payroll.BranchPayItemConfig
WHERE PayItemID IN (SELECT PayItemID FROM g05_ownerless_payitems);

DELETE FROM payroll.PayItemLineTypeMap
WHERE PayItemID IN (SELECT PayItemID FROM g05_ownerless_payitems);

DELETE FROM payroll.PayItemRateTypeMap
WHERE PayItemID IN (SELECT PayItemID FROM g05_ownerless_payitems);

DELETE FROM payroll.PayItemRateSlots
WHERE PayItemID IN (SELECT PayItemID FROM g05_ownerless_payitems);

DELETE FROM payroll.DriverRates
WHERE RateTypeID IN (SELECT RateTypeID FROM g05_orphan_ratetypes);

DELETE FROM payroll.RateTypes
WHERE RateTypeID IN (SELECT RateTypeID FROM g05_orphan_ratetypes);

-- The predecessor tables reference PayItems. Their rows are disposable
-- pre-production data and are intentionally not converted into any CDPI
-- structure, so the tables are dropped before the PayItems they reference.
DROP TABLE payroll.CustomPayItemRequests;
DROP TABLE payroll.PayItemSettings;

DELETE FROM payroll.PayItems
WHERE PayItemID IN (SELECT PayItemID FROM g05_ownerless_payitems);

DROP TABLE g05_orphan_ratetypes;
DROP TABLE g05_ownerless_draftlines;
DROP TABLE g05_ownerless_payitems;
