-- 0078: Retire the legacy Period source and false PayItem architecture (G0.4B).
--
-- G0.4A removed every reachable application authority for generic
-- period-level monetary DraftLines. This migration removes the data model that
-- made that architecture look legitimate:
--
--   * PayrollDraftLines becomes structurally Daily-only: LineScope is dropped,
--     WorkDate becomes NOT NULL, and the daily business-key uniqueness
--     invariant is rebuilt without the now-meaningless Daily predicate.
--   * PayrollBonusEvents.SourceDraftLineID (the migration bridge from legacy
--     BONUS DraftLines) is dropped. PayrollBonusEvents is the sole Bonus
--     authority.
--   * The BONUS / ADJUSTMENT / GUARANTEED_MINIMUM PayItems and every custom
--     Period PayItem are physically deleted together with their mutable
--     configuration, mappings, and frozen-period snapshot rows.
--   * PayItems.ItemScope and PayrollPeriodPayItems.ItemScope are kept but
--     narrowed: 'Period' is representable only for the two system-generated
--     output identities SYS_MIN_TOPUP and SYS_MAX_CAP, whose RateBehavior is
--     normalized from the false 'EnteredAmount' to 'Calculated'.
--   * 'EnteredAmount' is removed from the PayItems RateBehavior vocabulary.
--
-- Flussra is pre-production, so disposable legacy rows are deleted and no
-- compatibility layer is built. The migration is FAIL-CLOSED: before any
-- destructive statement it verifies that no immutable financial or evidence
-- row (final ledger, calculation snapshot, used-rate definition) and no
-- finalized period depends on the legacy rows it is about to delete. If one
-- does, it aborts with no changes; reset and reseed the development database
-- and rerun.
--
-- Historical migrations (0003, 0005, 0010, 0033, 0053, 0058, ...) are NOT
-- rewritten; they remain the accurate record of how this schema evolved.

-- ---------------------------------------------------------------------------
-- 0. Characterize what is being retired.
-- ---------------------------------------------------------------------------

-- Every PayItem that is not Daily operational pay and not one of the two
-- supported system-generated Period outputs.
CREATE TEMP TABLE g04b_retired_payitems AS
SELECT pi.PayItemID, pi.PayItemCode
FROM payroll.PayItems pi
WHERE NOT (pi.CompanyID IS NULL
           AND pi.IsSystemStandard
           AND pi.PayItemCode IN ('SYS_MIN_TOPUP', 'SYS_MAX_CAP'))
  AND (   (pi.CompanyID IS NULL
           AND pi.PayItemCode IN ('BONUS', 'ADJUSTMENT', 'GUARANTEED_MINIMUM'))
       OR pi.ItemScope = 'Period'
       OR pi.RateBehavior = 'EnteredAmount');

-- Every DraftLine that is not a day-bound operational source row.
CREATE TEMP TABLE g04b_legacy_draftlines AS
SELECT dl.DraftLineID, dl.PayrollPeriodID
FROM payroll.PayrollDraftLines dl
WHERE dl.LineScope <> 'Daily'
   OR dl.WorkDate IS NULL
   OR dl.LineType IN (SELECT PayItemCode FROM g04b_retired_payitems);

-- ---------------------------------------------------------------------------
-- 1. PREFLIGHT: fail closed if immutable or finalized state depends on any
--    row this migration deletes.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_count BIGINT;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM payroll.PayrollFinalLines f
    WHERE f.PayItemID IN (SELECT PayItemID FROM g04b_retired_payitems)
       OR f.DraftLineID IN (SELECT DraftLineID FROM g04b_legacy_draftlines);
    IF v_count > 0 THEN
        RAISE EXCEPTION
            'G04B_BLOCKED_FINAL_LEDGER_DEPENDENCY: % PayrollFinalLines row(s) reference legacy Period DraftLines or retired PayItems. Reset and reseed the development database, then rerun.',
            v_count
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM payroll.PayrollCalculationSnapshotLines sl
    WHERE sl.PayItemID IN (SELECT PayItemID FROM g04b_retired_payitems)
       OR (sl.SourceType = 'DraftLine'
           AND (sl.LineScope IS DISTINCT FROM 'Daily' OR sl.WorkDate IS NULL))
       OR (sl.SourceType = 'DraftLine'
           AND sl.SourceID IN (SELECT DraftLineID::TEXT FROM g04b_legacy_draftlines));
    IF v_count > 0 THEN
        RAISE EXCEPTION
            'G04B_BLOCKED_CALCULATION_SNAPSHOT_DEPENDENCY: % PayrollCalculationSnapshotLines row(s) reference legacy Period DraftLines or retired PayItems. Reset and reseed the development database, then rerun.',
            v_count
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM payroll.PayrollCalculationSnapshotUsedRateDefinitions ud
    WHERE ud.PayItemID IN (SELECT PayItemID FROM g04b_retired_payitems);
    IF v_count > 0 THEN
        RAISE EXCEPTION
            'G04B_BLOCKED_USED_RATE_DEFINITION_DEPENDENCY: % PayrollCalculationSnapshotUsedRateDefinitions row(s) reference retired PayItems. Reset and reseed the development database, then rerun.',
            v_count
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM payroll.PayrollDraftLines dl
    JOIN payroll.PayrollPeriods p ON p.PayrollPeriodID = dl.PayrollPeriodID
    WHERE dl.DraftLineID IN (SELECT DraftLineID FROM g04b_legacy_draftlines)
      AND p.Status IN ('Locked', 'Archived');
    IF v_count > 0 THEN
        RAISE EXCEPTION
            'G04B_BLOCKED_FINALIZED_PERIOD_DEPENDENCY: % legacy Period DraftLine(s) belong to Locked or Archived periods. Reset and reseed the development database, then rerun.',
            v_count
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM payroll.CdpiDefinitions d
    WHERE d.PayItemID IN (SELECT PayItemID FROM g04b_retired_payitems);
    IF v_count > 0 THEN
        RAISE EXCEPTION
            'G04B_BLOCKED_CDPI_DEPENDENCY: % CDPI definition(s) reference retired PayItems. Reset and reseed the development database, then rerun.',
            v_count
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM payroll.CdpiRequests r
    WHERE r.ApprovedPayItemID IN (SELECT PayItemID FROM g04b_retired_payitems);
    IF v_count > 0 THEN
        RAISE EXCEPTION
            'G04B_BLOCKED_CDPI_DEPENDENCY: % CDPI request(s) were approved into retired PayItems. Reset and reseed the development database, then rerun.',
            v_count
            USING ERRCODE = 'check_violation';
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- 2. Remove the Bonus migration bridge. PayrollBonusEvents is the sole Bonus
--    authority; SourceDraftLineID was lineage from legacy BONUS DraftLines.
-- ---------------------------------------------------------------------------
DROP INDEX IF EXISTS payroll.ux_PayrollBonusEvents_SourceDraftLine;

ALTER TABLE payroll.PayrollBonusEvents
    DROP CONSTRAINT IF EXISTS fk_PayrollBonusEvents_SourceDraftLine;

ALTER TABLE payroll.PayrollBonusEvents
    DROP COLUMN IF EXISTS SourceDraftLineID;

-- ---------------------------------------------------------------------------
-- 3. Delete the disposable legacy Period / non-daily DraftLines.
-- ---------------------------------------------------------------------------
DELETE FROM payroll.PayrollDraftLines
WHERE DraftLineID IN (SELECT DraftLineID FROM g04b_legacy_draftlines);

-- ---------------------------------------------------------------------------
-- 4. Delete the retired PayItems and everything that depends on them.
-- ---------------------------------------------------------------------------
DELETE FROM payroll.PayrollPeriodPayItems
WHERE PayItemID IN (SELECT PayItemID FROM g04b_retired_payitems);

DELETE FROM payroll.BranchPayItemConfig
WHERE PayItemID IN (SELECT PayItemID FROM g04b_retired_payitems);

DELETE FROM payroll.PayItemSettings
WHERE PayItemID IN (SELECT PayItemID FROM g04b_retired_payitems);

DELETE FROM payroll.PayItemLineTypeMap
WHERE PayItemID IN (SELECT PayItemID FROM g04b_retired_payitems);

DELETE FROM payroll.PayItemRateTypeMap
WHERE PayItemID IN (SELECT PayItemID FROM g04b_retired_payitems);

DELETE FROM payroll.PayItemRateSlots
WHERE PayItemID IN (SELECT PayItemID FROM g04b_retired_payitems);

-- The predecessor custom-item request writer (retired by G0.5) may only
-- describe Daily PerUnit items. Requests that proposed or produced anything
-- else describe a capability that no longer exists.
DELETE FROM payroll.CustomPayItemRequests
WHERE ItemScope <> 'Daily'
   OR RateBehavior <> 'PerUnit'
   OR ApprovedPayItemID IN (SELECT PayItemID FROM g04b_retired_payitems);

DELETE FROM payroll.PayItems
WHERE PayItemID IN (SELECT PayItemID FROM g04b_retired_payitems);

-- ---------------------------------------------------------------------------
-- 5. SYS_MIN_TOPUP / SYS_MAX_CAP are system-generated from DriverPayRules at
--    finalization; no user ever enters them. Replace the false 'EnteredAmount'
--    metadata with the already-valid 'Calculated' representation, in the
--    catalog and in frozen period snapshots. Amounts are unaffected.
-- ---------------------------------------------------------------------------
UPDATE payroll.PayItems
SET RateBehavior = 'Calculated'
WHERE CompanyID IS NULL
  AND IsSystemStandard
  AND PayItemCode IN ('SYS_MIN_TOPUP', 'SYS_MAX_CAP')
  AND RateBehavior = 'EnteredAmount';

UPDATE payroll.PayrollPeriodPayItems
SET RateBehavior = 'Calculated'
WHERE IsSystemStandard
  AND PayItemCode IN ('SYS_MIN_TOPUP', 'SYS_MAX_CAP')
  AND RateBehavior = 'EnteredAmount';

-- ---------------------------------------------------------------------------
-- 6. Narrow the catalog invariants to the surviving product.
-- ---------------------------------------------------------------------------
ALTER TABLE payroll.PayItems
    DROP CONSTRAINT ck_PayItems_ItemScope,
    ADD CONSTRAINT ck_PayItems_ItemScope
        CHECK (
            ItemScope = 'Daily'
            OR (ItemScope = 'Period'
                AND CompanyID IS NULL
                AND IsSystemStandard
                AND PayItemCode IN ('SYS_MIN_TOPUP', 'SYS_MAX_CAP'))
        );

ALTER TABLE payroll.PayItems
    DROP CONSTRAINT ck_PayItems_RateBehavior,
    ADD CONSTRAINT ck_PayItems_RateBehavior
        CHECK (RateBehavior IN ('PerUnit', 'Fixed', 'Calculated', 'None',
                                'OrdinalTier', 'RangeBracket',
                                'RangeProgressive', 'Block'));

ALTER TABLE payroll.PayrollPeriodPayItems
    DROP CONSTRAINT ck_PPPI_ItemScope,
    ADD CONSTRAINT ck_PPPI_ItemScope
        CHECK (
            ItemScope = 'Daily'
            OR (ItemScope = 'Period'
                AND IsSystemStandard
                AND PayItemCode IN ('SYS_MIN_TOPUP', 'SYS_MAX_CAP'))
        );

ALTER TABLE payroll.CustomPayItemRequests
    DROP CONSTRAINT ck_CustomPayItemRequests_ItemScope,
    ADD CONSTRAINT ck_CustomPayItemRequests_ItemScope
        CHECK (ItemScope = 'Daily');

ALTER TABLE payroll.CustomPayItemRequests
    DROP CONSTRAINT ck_CustomPayItemRequests_RateBehavior,
    ADD CONSTRAINT ck_CustomPayItemRequests_RateBehavior
        CHECK (RateBehavior = 'PerUnit');

-- ---------------------------------------------------------------------------
-- 7. PayrollDraftLines is structurally Daily-only.
-- ---------------------------------------------------------------------------
DROP INDEX IF EXISTS payroll.ix_PayrollDraftLines_Period_PeriodPay;
DROP INDEX IF EXISTS payroll.uix_payrolldraftlines_daily_active_business_key;
DROP INDEX IF EXISTS payroll.ix_DraftLines_Period_Driver_WorkDate;

ALTER TABLE payroll.PayrollDraftLines
    DROP CONSTRAINT ck_PayrollDraftLines_LineScope;

ALTER TABLE payroll.PayrollDraftLines
    DROP COLUMN LineScope;

ALTER TABLE payroll.PayrollDraftLines
    ALTER COLUMN WorkDate SET NOT NULL;

-- The unique business-key invariant survives; only the Daily predicate goes.
CREATE UNIQUE INDEX uix_payrolldraftlines_daily_active_business_key
    ON payroll.PayrollDraftLines
        (CompanyID, PayrollPeriodID, DriverID, WorkDate, LineType)
    WHERE Status <> 'Void';

-- Daily existing-source / eligibility lookup; WorkDate IS NOT NULL is now
-- guaranteed by the column and no longer part of the predicate.
CREATE INDEX ix_DraftLines_Period_Driver_WorkDate
    ON payroll.PayrollDraftLines (PayrollPeriodID, DriverID, WorkDate)
    WHERE Status <> 'Void';

DROP TABLE g04b_legacy_draftlines;
DROP TABLE g04b_retired_payitems;
