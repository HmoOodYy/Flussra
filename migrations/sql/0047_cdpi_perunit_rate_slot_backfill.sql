-- =============================================================================
-- 0047: CDPI PerUnit rate-slot backfill
--
-- Purpose:
--   After PR-1B, the CDPI approval and direct-create paths create a RateType,
--   PayItemRateTypeMap, and PayItemRateSlots row for every new PerUnit item.
--   This migration repairs existing approved/direct-created CDPI PerUnit items
--   that were created before PR-1B and may be missing some or all of the
--   rate-slot triple.
--
-- What this migration does (for EVERY CDPI-managed PerUnit PayItem):
--   1. Sets PayItems.RequiresRate = TRUE
--   2. If an active PayItemRateTypeMap already exists, uses its RateTypeID
--   3. Otherwise, finds or creates the deterministic company-scoped RateType
--      (code = 'CDPI_{id}_PER_UNIT') then creates a PayItemRateTypeMap row
--   4. Ensures exactly one active PayItemRateSlots row exists for
--      (PayItemID, RateTypeID, slot_key='per_unit_rate')
--
-- Partial states repaired:
--   - No RateType, no Map, no Slot (fully missing: creates all three)
--   - RateType exists but Map and Slot missing (reuses RateType, creates Map+Slot)
--   - RateType + Map exist but Slot missing (reuses both, creates Slot only)
--   - All three exist (no-op per idempotency guards)
--
-- Idempotency:
--   - RateType: SELECT first, INSERT only if missing
--   - PayItemRateTypeMap: ON CONFLICT DO NOTHING
--   - PayItemRateSlots: NOT EXISTS guard before INSERT
--   - RequiresRate: UPDATE only WHERE requiresrate = FALSE
--   Re-running this migration is fully safe.
--
-- Changes to schema: NONE (only data inserts/updates).
-- Changes to existing rows: RequiresRate set to TRUE for affected PayItems.
-- =============================================================================

DO $$
DECLARE
    r            RECORD;
    v_rate_code  VARCHAR(100);
    v_rate_name  VARCHAR(200);
    v_unit_name  VARCHAR(50);
    v_rt_id      INTEGER;
    v_slot_order INTEGER;
BEGIN
    FOR r IN
        SELECT
            pi.payitemid,
            pi.payitemname,
            pi.unit,
            pi.datatype,
            pi.companyid
        FROM   payroll.payitems pi
        JOIN   payroll.cdpidefinitions cd ON cd.payitemid = pi.payitemid
        WHERE  pi.ratebehavior = 'PerUnit'
          AND  pi.status != 'Retired'
          AND  pi.companyid IS NOT NULL
    LOOP
        -- Step 1: Set RequiresRate = TRUE.
        UPDATE payroll.payitems
        SET    requiresrate = TRUE
        WHERE  payitemid    = r.payitemid
          AND  requiresrate = FALSE;

        -- Step 2: Resolve the RateTypeID for this item.
        -- Prefer the RateTypeID already mapped to this item (active map wins).
        SELECT m.ratetypeid INTO v_rt_id
        FROM   payroll.payitemratetypemap m
        WHERE  m.payitemid = r.payitemid
          AND  m.status   = 'Active'
        LIMIT  1;

        IF v_rt_id IS NULL THEN
            -- No active map: find or create the deterministic RateType.
            v_rate_code := 'CDPI_' || r.payitemid::TEXT || '_PER_UNIT';
            v_rate_name := TRIM(r.payitemname) || ' Rate';
            v_unit_name := CASE
                WHEN r.unit IS NOT NULL AND TRIM(r.unit) != '' THEN TRIM(r.unit)
                WHEN r.datatype = 'Time'                       THEN 'Hour'
                ELSE 'Unit'
            END;

            SELECT ratetypeid INTO v_rt_id
            FROM   payroll.ratetypes
            WHERE  ratecode = v_rate_code;

            IF v_rt_id IS NULL THEN
                INSERT INTO payroll.ratetypes
                    (ratecode, ratename, unitname, isactive, companyid)
                VALUES
                    (v_rate_code, v_rate_name, v_unit_name, TRUE, r.companyid)
                RETURNING ratetypeid INTO v_rt_id;
            END IF;

            -- Step 3: Ensure PayItemRateTypeMap.
            INSERT INTO payroll.payitemratetypemap
                (payitemid, ratetypeid, isprimary, status)
            VALUES
                (r.payitemid, v_rt_id, TRUE, 'Active')
            ON CONFLICT (payitemid, ratetypeid) DO NOTHING;
        END IF;

        -- Step 4: Ensure PayItemRateSlots.
        IF NOT EXISTS (
            SELECT 1
            FROM   payroll.payitemrateslots s
            WHERE  s.payitemid  = r.payitemid
              AND  s.ratetypeid = v_rt_id
              AND  s.status     = 'Active'
        ) THEN
            SELECT COALESCE(MAX(sortorder), 0) + 1 INTO v_slot_order
            FROM   payroll.payitemrateslots
            WHERE  payitemid = r.payitemid
              AND  status    = 'Active';

            INSERT INTO payroll.payitemrateslots
                (payitemid, ratetypeid, slotkey, slotrole, sortorder,
                 isrequired, issystemgenerated, sourcekind, status, createdatutc)
            VALUES
                (r.payitemid, v_rt_id, 'per_unit_rate', 'per_unit', v_slot_order,
                 TRUE, TRUE, 'CDPI', 'Active', NOW());
        END IF;

        RAISE NOTICE 'Ensured CDPI PerUnit item id=% rate_type_id=%',
                     r.payitemid, v_rt_id;
    END LOOP;
END $$;
