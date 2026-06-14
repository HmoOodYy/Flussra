-- 0020: DB-level uniqueness for active Company Owner assignment.
--
-- Enforces the product rule: at most one active COMPANY_OWNER assignment
-- per company at any time.  The service layer already prevents this via logic,
-- but this trigger gives a hard DB-level guarantee that survives any future
-- code path that bypasses the service.

CREATE OR REPLACE FUNCTION sec.fn_check_company_owner_unique()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_is_owner  BOOLEAN := FALSE;
    v_conflict  INT;
BEGIN
    -- Only care about active rows that reference a company role
    IF NEW.companyroleId IS NULL OR NOT NEW.isactive THEN
        RETURN NEW;
    END IF;

    -- Is this a COMPANY_OWNER role?
    SELECT (rolecode = 'COMPANY_OWNER')
    INTO   v_is_owner
    FROM   sec.companyroles
    WHERE  companyroleid = NEW.companyroleId;

    IF NOT FOUND OR NOT v_is_owner THEN
        RETURN NEW;
    END IF;

    -- Count other active COMPANY_OWNER assignments for this company.
    -- For UPDATE, exclude the row being updated so we don't self-conflict.
    -- For INSERT, include all existing rows.
    SELECT COUNT(*)
    INTO   v_conflict
    FROM   sec.userbranchroles  ubr
    JOIN   sec.companyroles     cr  ON cr.companyroleid = ubr.companyroleId
    WHERE  ubr.companyid  = NEW.companyid
      AND  cr.rolecode    = 'COMPANY_OWNER'
      AND  ubr.isactive   = TRUE
      AND  (TG_OP = 'INSERT' OR ubr.userbranchroleid <> NEW.userbranchroleid);

    IF v_conflict > 0 THEN
        RAISE EXCEPTION
            'company_owner_duplicate: Only one active Company Owner assignment '
            'is allowed per company (company_id=%).',
            NEW.companyid
            USING ERRCODE = 'unique_violation';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_company_owner_unique
    BEFORE INSERT OR UPDATE ON sec.userbranchroles
    FOR EACH ROW EXECUTE FUNCTION sec.fn_check_company_owner_unique();
