-- =============================================================================
-- Migration 0055: Retire PTO_STATUS system pay item
--
-- Product decision: Status is exclusively a Daily Grid column managed through
-- PayrollStatusKeys. PTO_STATUS was a legacy pay-item that served as a
-- dual-purpose "status + line" concept. It is fully removed.
--
-- Safety strategy:
--   1. Locate PTO_STATUS PayItemID.  If not found -- migration is a no-op
--      (already applied or never seeded).
--   2. Preflight: refuse if PayrollFinalLines references PTO_STATUS (by linetype
--      OR by PayItemID -- migration 0034 added a PayItemID FK column).
--      Immutable financial history -- must be cleaned manually.
--   3. Preflight: refuse if non-voided PayrollDraftLines exist for PTO_STATUS
--      (active payroll entries -- period must be cancelled/voided first).
--   4. Check PayItemRateTypeMap, PayItemSettings, PayItemRateSlots for any
--      PTO_STATUS rows (should be 0 because RateBehavior='None'; refuse if not).
--   4b.Explicitly delete PayItemLineTypeMap rows (mapping/config; not financial;
--      FK to PayItems requires removal before PayItem can be deleted).
--   5. Explicitly delete BranchPayItemConfig rows (config; safe to remove).
--   6. Explicitly delete PayrollPeriodPayItems snapshot rows (safe to remove).
--   7. DELETE the PayItems row itself.  All FK dependencies cleared above;
--      no raw FK failure is possible at this point.
--
-- Downgrade note: re-inserts the PTO_STATUS row (idempotent via ON CONFLICT).
-- =============================================================================

DO $$
DECLARE
    v_payitemid       INTEGER;
    v_final_count     BIGINT;
    v_draft_count     BIGINT;
    v_rate_map_count  BIGINT;
    v_settings_count  BIGINT;
    v_rate_slots_count BIGINT;
BEGIN
    -- -------------------------------------------------------------------------
    -- Step 1: Locate PTO_STATUS PayItemID
    -- -------------------------------------------------------------------------
    SELECT payitemid
      INTO v_payitemid
      FROM payroll.payitems
     WHERE payitemcode = 'PTO_STATUS'
       AND companyid   IS NULL;

    IF v_payitemid IS NULL THEN
        RAISE NOTICE 'migration 0055: PTO_STATUS not found -- migration is a no-op (already applied or never seeded).';
        RETURN;
    END IF;

    RAISE NOTICE 'migration 0055: Found PTO_STATUS at PayItemID=%', v_payitemid;

    -- -------------------------------------------------------------------------
    -- Step 2: Refuse if PayrollFinalLines references PTO_STATUS
    -- Final lines are immutable financial history; do not silently delete them.
    -- Check BOTH linetype and payitemid: migration 0034 added payitemid FK so
    -- a row could reference PTO_STATUS by payitemid even with a different linetype.
    -- -------------------------------------------------------------------------
    SELECT COUNT(*)
      INTO v_final_count
      FROM payroll.payrollfinallines
     WHERE linetype = 'PTO_STATUS'
        OR payitemid = v_payitemid;

    IF v_final_count > 0 THEN
        RAISE EXCEPTION
            'migration 0055 ABORTED: % row(s) in payroll.payrollfinallines '
            'reference PTO_STATUS (by linetype or PayItemID=%). '
            'These are immutable financial records and must be cleaned up manually '
            'before this migration can run. '
            'Do NOT delete final lines without a deliberate data-governance review.',
            v_final_count, v_payitemid;
    END IF;

    -- -------------------------------------------------------------------------
    -- Step 3: Refuse if non-voided PayrollDraftLines exist for PTO_STATUS
    -- Active draft entries must be voided / cancelled before migration.
    -- -------------------------------------------------------------------------
    SELECT COUNT(*)
      INTO v_draft_count
      FROM payroll.payrolldraftlines
     WHERE linetype = 'PTO_STATUS'
       AND status   <> 'Void';

    IF v_draft_count > 0 THEN
        RAISE EXCEPTION
            'migration 0055 ABORTED: % non-voided row(s) in payroll.payrolldraftlines '
            'have linetype=''PTO_STATUS''. Cancel or void those payroll periods '
            'before running this migration.',
            v_draft_count;
    END IF;

    -- -------------------------------------------------------------------------
    -- Step 4: Refuse if PayItemRateTypeMap / PayItemSettings / PayItemRateSlots
    -- reference PTO_STATUS (should be 0 because RateBehavior='None').
    -- -------------------------------------------------------------------------
    SELECT COUNT(*)
      INTO v_rate_map_count
      FROM payroll.payitemratetypemap
     WHERE payitemid = v_payitemid;

    IF v_rate_map_count > 0 THEN
        RAISE EXCEPTION
            'migration 0055 ABORTED: % row(s) in payroll.payitemratetypemap '
            'reference PTO_STATUS (PayItemID=%). Unexpected -- PTO_STATUS has '
            'RateBehavior=''None'' and should have no rate-type mappings.',
            v_rate_map_count, v_payitemid;
    END IF;

    SELECT COUNT(*)
      INTO v_settings_count
      FROM payroll.payitemsettings
     WHERE payitemid = v_payitemid;

    IF v_settings_count > 0 THEN
        RAISE EXCEPTION
            'migration 0055 ABORTED: % row(s) in payroll.payitemsettings '
            'reference PTO_STATUS (PayItemID=%). Remove these first.',
            v_settings_count, v_payitemid;
    END IF;

    SELECT COUNT(*)
      INTO v_rate_slots_count
      FROM payroll.payitemrateslots
     WHERE payitemid = v_payitemid;

    IF v_rate_slots_count > 0 THEN
        RAISE EXCEPTION
            'migration 0055 ABORTED: % row(s) in payroll.payitemrateslots '
            'reference PTO_STATUS (PayItemID=%). Remove these first.',
            v_rate_slots_count, v_payitemid;
    END IF;

    -- -------------------------------------------------------------------------
    -- Step 4b: Explicitly delete PayItemLineTypeMap rows for PTO_STATUS
    -- PayItemLineTypeMap has a real FK to PayItems; rows must be deleted before
    -- the PayItems row or the DELETE will fail with a FK violation.
    -- These are mapping/config rows (not financial data) so deletion is safe.
    -- -------------------------------------------------------------------------
    DELETE FROM payroll.payitemlinetypemap
     WHERE payitemid = v_payitemid;

    GET DIAGNOSTICS v_draft_count = ROW_COUNT;
    IF v_draft_count > 0 THEN
        RAISE NOTICE 'migration 0055: deleted % PayItemLineTypeMap row(s) for PTO_STATUS.', v_draft_count;
    END IF;

    -- -------------------------------------------------------------------------
    -- Step 5: Delete BranchPayItemConfig rows (config; safe to remove)
    -- Confirmed 0 rows in dev/demo DB; explicit for safety.
    -- -------------------------------------------------------------------------
    DELETE FROM payroll.branchpayitemconfig
     WHERE payitemid = v_payitemid;

    GET DIAGNOSTICS v_draft_count = ROW_COUNT;  -- reuse variable for row count
    IF v_draft_count > 0 THEN
        RAISE NOTICE 'migration 0055: deleted % BranchPayItemConfig row(s) for PTO_STATUS.', v_draft_count;
    END IF;

    -- -------------------------------------------------------------------------
    -- Step 6: Delete PayrollPeriodPayItems snapshot rows (safe to remove)
    -- These are per-period copies of pay-item metadata; not financial data.
    -- -------------------------------------------------------------------------
    DELETE FROM payroll.payrollperiodpayitems
     WHERE payitemcode = 'PTO_STATUS';

    GET DIAGNOSTICS v_draft_count = ROW_COUNT;
    IF v_draft_count > 0 THEN
        RAISE NOTICE 'migration 0055: deleted % PayrollPeriodPayItems snapshot row(s) for PTO_STATUS.', v_draft_count;
    END IF;

    -- -------------------------------------------------------------------------
    -- Step 7: Delete the PayItems row itself
    -- All FK dependencies confirmed clear above; no raw FK error possible.
    -- -------------------------------------------------------------------------
    DELETE FROM payroll.payitems
     WHERE payitemid = v_payitemid;

    RAISE NOTICE 'migration 0055: PTO_STATUS (PayItemID=%) deleted from payroll.payitems.', v_payitemid;

END;
$$;
