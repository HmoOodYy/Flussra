-- =============================================================================
-- Migration 0036: RateTypes.CompanyID -- structural ownership column
--
-- Goal (Phase 4C):
--   Make RateType ownership structural in the DB rather than relying solely on
--   service-layer PayItemRateTypeMap analysis.
--
--   payroll.RateTypes.CompanyID IS NULL     -> system/global RateType
--   payroll.RateTypes.CompanyID IS NOT NULL -> company-owned custom RateType
--
-- Steps:
--   1. Add CompanyID column (nullable, FK to core.Companies)
--   2. Backfill:
--      a. System types:   mapped to any PayItem with CompanyID IS NULL -> stay NULL
--      b. Company types:  mapped only to exactly one company's PayItems -> set to that company
--      c. Ambiguous:      mapped to >1 distinct non-null company -> RAISE EXCEPTION (fail fast)
--      d. Orphaned CPI_:  active, starts with CPI_, zero PayItemRateTypeMap rows -> deactivate
--      e. Other unmapped: non-CPI, unmapped -> leave NULL (treated as legacy system)
--   3. Add index on CompanyID
--   4. Add index on RateCode (the unique constraint already functions as an index; explicit
--      covering index is not required -- the unique index covers eq-lookups on RateCode)
--   5. Add DB trigger: block PayItemRateTypeMap INSERT/UPDATE that would cross-map
--      a company-owned RateType (CompanyID IS NOT NULL) to a PayItem from a different company.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Step 1: Add CompanyID column
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.ratetypes
    ADD COLUMN IF NOT EXISTS companyid INTEGER;

ALTER TABLE payroll.ratetypes
    ADD CONSTRAINT fk_ratetypes_company
        FOREIGN KEY (companyid) REFERENCES core.companies(companyid);

-- ---------------------------------------------------------------------------
-- Step 2: Backfill CompanyID
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    r           RECORD;
    v_cid       INTEGER;
    v_cnt       INTEGER;
    v_ambiguous TEXT := '';
BEGIN
    -- 2a. Set NULL for RateTypes that have a system mapping (CompanyID IS NULL PayItem).
    --     These are already NULL, so this is a no-op safety check.
    UPDATE payroll.ratetypes rt
    SET    companyid = NULL
    WHERE  rt.companyid IS NULL
      AND  EXISTS (
               SELECT 1
               FROM   payroll.payitemratetypemap pirm
               JOIN   payroll.payitems pi ON pi.payitemid = pirm.payitemid
               WHERE  pirm.ratetypeid = rt.ratetypeid
                 AND  pi.companyid IS NULL
           );

    -- 2b+2c. For each RateType that has NO system mapping, determine the owning company.
    FOR r IN
        SELECT rt.ratetypeid, rt.ratecode
        FROM   payroll.ratetypes rt
        WHERE  rt.companyid IS NULL
          AND  NOT EXISTS (
                   SELECT 1
                   FROM   payroll.payitemratetypemap pirm
                   JOIN   payroll.payitems pi ON pi.payitemid = pirm.payitemid
                   WHERE  pirm.ratetypeid = rt.ratetypeid
                     AND  pi.companyid IS NULL
               )
    LOOP
        -- Count distinct non-null companies mapped to this RateType
        SELECT COUNT(DISTINCT pi.companyid), MIN(pi.companyid)
        INTO   v_cnt, v_cid
        FROM   payroll.payitemratetypemap pirm
        JOIN   payroll.payitems pi ON pi.payitemid = pirm.payitemid
        WHERE  pirm.ratetypeid = r.ratetypeid
          AND  pi.companyid IS NOT NULL;

        IF v_cnt = 0 THEN
            -- Unmapped: no PayItemRateTypeMap rows at all (or all mappings are system-null)
            -- Deactivate if it looks like an orphaned CPI_ type; leave others as NULL.
            IF r.ratecode LIKE 'CPI_%' THEN
                UPDATE payroll.ratetypes
                SET    isactive = FALSE
                WHERE  ratetypeid = r.ratetypeid;
                RAISE NOTICE 'Migration 0036: deactivated orphaned CPI_ RateType id=% code=%',
                             r.ratetypeid, r.ratecode;
            ELSE
                -- Non-CPI unmapped: leave companyid NULL (legacy/system-safe assumption)
                RAISE NOTICE 'Migration 0036: unmapped non-CPI RateType id=% code=% left as system-NULL',
                             r.ratetypeid, r.ratecode;
            END IF;

        ELSIF v_cnt = 1 THEN
            -- Exactly one company: assign ownership
            UPDATE payroll.ratetypes
            SET    companyid = v_cid
            WHERE  ratetypeid = r.ratetypeid;
            RAISE NOTICE 'Migration 0036: assigned companyid=% to RateType id=% code=%',
                         v_cid, r.ratetypeid, r.ratecode;

        ELSE
            -- More than one company mapped to this non-system RateType: ambiguous/contaminated.
            v_ambiguous := v_ambiguous || format(
                ' [ratetypeid=%s ratecode=%s companies=%s]',
                r.ratetypeid, r.ratecode, v_cnt
            );
        END IF;
    END LOOP;

    IF v_ambiguous <> '' THEN
        RAISE EXCEPTION
            'Migration 0036 ABORTED: ambiguous RateType ownership detected. '
            'The following RateTypes are mapped to PayItems from more than one company '
            'and have no system mapping. Investigate and correct before re-running:%',
            v_ambiguous;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Step 3: Index on CompanyID (for ownership-filter queries)
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS ix_ratetypes_companyid
    ON payroll.ratetypes (companyid);

-- Note: uq_RateTypes_RateCode (the existing unique constraint) already functions
-- as a B-tree index on RateCode. A separate plain index would be redundant.

-- ---------------------------------------------------------------------------
-- Step 5: Trigger -- block cross-company PayItemRateTypeMap mappings
--
-- Rule: if RateType.CompanyID IS NOT NULL (company-owned custom type), then
-- the PayItem being mapped must belong to the same company.
--
-- Allowed:
--   RateType.CompanyID IS NULL           -> system type, any PayItem is fine
--   RateType.CompanyID = PayItem.CompanyID -> same-company mapping
--   PayItem.CompanyID IS NULL            -> system PayItem mapping onto any RateType is fine
--
-- Rejected:
--   RateType.CompanyID IS NOT NULL
--   AND PayItem.CompanyID IS NOT NULL
--   AND RateType.CompanyID != PayItem.CompanyID
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION payroll.fn_guard_payitemratetypemap_ownership()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_rt_company  INTEGER;
    v_pi_company  INTEGER;
BEGIN
    SELECT companyid INTO v_rt_company
    FROM   payroll.ratetypes
    WHERE  ratetypeid = NEW.ratetypeid;

    SELECT companyid INTO v_pi_company
    FROM   payroll.payitems
    WHERE  payitemid = NEW.payitemid;

    -- Only enforce when the RateType is company-owned (non-NULL)
    -- and the PayItem is also company-owned (non-NULL) and mismatched.
    IF v_rt_company IS NOT NULL
       AND v_pi_company IS NOT NULL
       AND v_rt_company <> v_pi_company
    THEN
        RAISE EXCEPTION
            'payitemratetypemap_ownership_violation: Cannot map PayItem (id=%, companyid=%) '
            'to RateType (id=%, companyid=%) -- RateType belongs to a different company.',
            NEW.payitemid, v_pi_company, NEW.ratetypeid, v_rt_company
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_guard_payitemratetypemap_ownership
    ON payroll.payitemratetypemap;

CREATE TRIGGER trg_guard_payitemratetypemap_ownership
    BEFORE INSERT OR UPDATE ON payroll.payitemratetypemap
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_payitemratetypemap_ownership();
