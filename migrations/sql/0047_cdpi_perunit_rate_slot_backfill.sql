-- =============================================================================
-- 0047: CDPI PerUnit rate-slot backfill
--
-- Purpose:
--   After PR-1B, the CDPI approval and direct-create paths create a RateType,
--   PayItemRateTypeMap, and PayItemRateSlots row for every new PerUnit item.
--   This migration repairs existing approved/direct-created CDPI PerUnit items
--   that were created before PR-1B and therefore have no rate-slot triple.
--
-- What this migration does:
--   For each PayItem that:
--     - Has a CdpiDefinitions row (CDPI-managed)
--     - Has RateBehavior = 'PerUnit'
--     - Is missing an active PayItemRateTypeMap entry
--   It creates:
--     1. A company-scoped RateType with code 'CDPI_{id}_PER_UNIT'
--     2. A PayItemRateTypeMap row (primary, Active)
--     3. A PayItemRateSlots row (slot_key='per_unit_rate', SourceKind='CDPI')
--     4. Sets PayItems.RequiresRate = TRUE
--
-- Idempotency:
--   Each INSERT step is guarded by NOT EXISTS or ON CONFLICT DO NOTHING, so
--   re-running this migration is safe.
--
-- Changes to schema: NONE (only data inserts/updates).
-- Changes to existing rows: RequiresRate set to TRUE for affected PayItems.
-- =============================================================================

DO $$
DECLARE
    r RECORD;
    v_rate_code  VARCHAR(100);
    v_rate_name  VARCHAR(200);
    v_unit_name  VARCHAR(50);
    v_rt_id      INTEGER;
    v_map_exists INTEGER;
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
          AND  NOT EXISTS (
              SELECT 1
              FROM   payroll.payitemratetypemap m
              WHERE  m.payitemid = pi.payitemid
                AND  m.status   = 'Active'
          )
    LOOP
        v_rate_code := 'CDPI_' || r.payitemid::TEXT || '_PER_UNIT';
        v_rate_name := TRIM(r.payitemname) || ' Rate';
        v_unit_name := CASE
            WHEN r.unit IS NOT NULL AND TRIM(r.unit) != '' THEN TRIM(r.unit)
            WHEN r.datatype = 'Time'                       THEN 'Hour'
            ELSE 'Unit'
        END;

        -- Ensure RateType (company-scoped).
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

        -- Ensure PayItemRateTypeMap.
        INSERT INTO payroll.payitemratetypemap
            (payitemid, ratetypeid, isprimary, status)
        VALUES
            (r.payitemid, v_rt_id, TRUE, 'Active')
        ON CONFLICT (payitemid, ratetypeid) DO NOTHING;

        -- Ensure PayItemRateSlots.
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

        -- Set RequiresRate = TRUE.
        UPDATE payroll.payitems
        SET    requiresrate = TRUE
        WHERE  payitemid    = r.payitemid
          AND  requiresrate = FALSE;

        RAISE NOTICE 'Repaired CDPI PerUnit item id=% name=% rate_code=%',
                     r.payitemid, r.payitemname, v_rate_code;
    END LOOP;
END $$;
