-- =============================================================================
-- Migration 0060: Bonus Batch Request Ownership Hardening (CP-3B2b)
--
-- CP-3B2a created payroll.PayrollBonusBatchRequests as foundation only, with
-- no writer.  CP-3B2b adds the POST /bonuses/batch endpoint, which becomes the
-- first writer of that table.  Before that writer exists, this migration
-- hardens the table the same way migration 0059 hardened PayrollBonusEvents:
--
--   1. Preflight: refuse if any existing PayrollBonusBatchRequests row's
--      Company/Branch is inconsistent with its owning PayrollPeriods row
--      (contamination must be fixed manually, not silently corrected).
--   2. Tighten column integrity before the table becomes writable:
--        - CreatedByUserID NOT NULL;
--        - ExpectedBonusDataRevision >= 0;
--        - ResultBonusDataRevision   >= 0;
--        - RequestHash is lowercase SHA-256 hex (64 chars);
--        - RequestPayloadJSON is a JSON object;
--        - CreatedEventIDs is a JSON array;
--        - CreatedEventCount matches the CreatedEventIDs array length.
--   3. A BEFORE INSERT OR UPDATE ownership trigger enforcing that the row's
--      CompanyID/BranchID match the owning period, and that the branch
--      belongs to that company.
--
-- No writer is added by this migration; the endpoint is added by CP-3B2b's
-- service/router code on top of this hardened table.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. Preflight: reject contaminated PayrollBonusBatchRequests rows
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    bad_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO bad_count
    FROM   payroll.payrollbonusbatchrequests bbr
    JOIN   payroll.payrollperiods            pp ON pp.payrollperiodid = bbr.payrollperiodid
    WHERE  bbr.companyid <> pp.companyid
        OR bbr.branchid  <> pp.branchid;

    IF bad_count > 0 THEN
        RAISE EXCEPTION
            'CP-3B2b preflight failed: % PayrollBonusBatchRequests row(s) have '
            'CompanyID/BranchID inconsistent with their owning PayrollPeriods '
            'row. Inspect and correct these rows manually before running '
            'migration 0060.', bad_count;
    END IF;
END
$$;


-- ---------------------------------------------------------------------------
-- 2. Column integrity constraints
-- ---------------------------------------------------------------------------

-- The table becomes writable in CP-3B2b; the writer always sets the actor.
ALTER TABLE payroll.PayrollBonusBatchRequests
    ALTER COLUMN CreatedByUserID SET NOT NULL;

ALTER TABLE payroll.PayrollBonusBatchRequests
    ADD CONSTRAINT ck_PayrollBonusBatchRequests_ExpectedRevision
    CHECK (ExpectedBonusDataRevision >= 0);

ALTER TABLE payroll.PayrollBonusBatchRequests
    ADD CONSTRAINT ck_PayrollBonusBatchRequests_ResultRevision
    CHECK (ResultBonusDataRevision >= 0);

-- Lowercase SHA-256 hex — subsumes the existing char_length(64) check from 0059
-- but is stricter (rejects non-hex 64-char strings).
ALTER TABLE payroll.PayrollBonusBatchRequests
    ADD CONSTRAINT ck_PayrollBonusBatchRequests_HashHex
    CHECK (RequestHash ~ '^[0-9a-f]{64}$');

ALTER TABLE payroll.PayrollBonusBatchRequests
    ADD CONSTRAINT ck_PayrollBonusBatchRequests_PayloadObject
    CHECK (jsonb_typeof(RequestPayloadJSON) = 'object');

ALTER TABLE payroll.PayrollBonusBatchRequests
    ADD CONSTRAINT ck_PayrollBonusBatchRequests_EventIDsArray
    CHECK (jsonb_typeof(CreatedEventIDs) = 'array');

-- Self-guarding: if CreatedEventIDs is not an array the array-type check above
-- fails; this check only asserts the count/length agreement when it is one.
ALTER TABLE payroll.PayrollBonusBatchRequests
    ADD CONSTRAINT ck_PayrollBonusBatchRequests_EventCountMatches
    CHECK (
        jsonb_typeof(CreatedEventIDs) <> 'array'
        OR CreatedEventCount = jsonb_array_length(CreatedEventIDs)
    );


-- ---------------------------------------------------------------------------
-- 3. Ownership trigger
-- ---------------------------------------------------------------------------
-- Mirrors trg_bonusevents_ownership (0059): the row's CompanyID/BranchID must
-- match its owning period, and the branch must belong to that company.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION payroll.trg_fn_bonusbatchrequests_ownership()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    period_company INTEGER;
    period_branch  INTEGER;
    branch_company INTEGER;
BEGIN
    SELECT companyid, branchid INTO period_company, period_branch
    FROM   payroll.payrollperiods
    WHERE  payrollperiodid = NEW.payrollperiodid;

    IF period_company IS DISTINCT FROM NEW.companyid
    OR period_branch  IS DISTINCT FROM NEW.branchid THEN
        RAISE EXCEPTION
            'PayrollBonusBatchRequests: period % belongs to company/branch %/% but row is for %/%',
            NEW.payrollperiodid, period_company, period_branch,
            NEW.companyid, NEW.branchid
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT companyid INTO branch_company
    FROM   core.branches
    WHERE  branchid = NEW.branchid;

    IF branch_company IS DISTINCT FROM NEW.companyid THEN
        RAISE EXCEPTION
            'PayrollBonusBatchRequests: branch % belongs to company % but row is for company %',
            NEW.branchid, branch_company, NEW.companyid
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_bonusbatchrequests_ownership
    BEFORE INSERT OR UPDATE ON payroll.PayrollBonusBatchRequests
    FOR EACH ROW
    EXECUTE FUNCTION payroll.trg_fn_bonusbatchrequests_ownership();
