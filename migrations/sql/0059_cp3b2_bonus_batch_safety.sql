-- =============================================================================
-- Migration 0059: Bonus Batch Safety Foundation (CP-3B2a)
--
-- Adds the durable/concurrency scaffolding required before a transactional
-- bonus batch endpoint (CP-3B2b) can be added.  No batch endpoint, batch
-- writer, or idempotent-replay logic is added by this migration or by
-- CP-3B2a — this is foundation only.
--
-- Changes:
--   1. Preflight: refuse if any PayrollBonusEvents row's Company/Branch is
--      inconsistent with its owning PayrollPeriods row (contamination must
--      be fixed manually, not silently corrected by this migration).
--   2. Add PayrollPeriods.BonusDataRevision BIGINT NOT NULL DEFAULT 0 — a
--      relative, per-period bonus-mutation concurrency token.
--   3. Add a DB-level ownership trigger on PayrollBonusEvents: INSERT/UPDATE
--      is rejected if CompanyID/BranchID do not match the owning
--      PayrollPeriods row's CompanyID/BranchID (PayrollPeriodID is the
--      trigger's join key, so it is inherently consistent).
--   4. Create payroll.PayrollBonusBatchRequests — durable idempotency/
--      correlation storage for the future batch endpoint (CP-3B2b).
--   5. Add a partial index on PayrollBonusEvents.BatchCorrelationID.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. Preflight: reject contaminated PayrollBonusEvents rows
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    bad_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO bad_count
    FROM   payroll.payrollbonusevents be
    JOIN   payroll.payrollperiods     pp ON pp.payrollperiodid = be.payrollperiodid
    WHERE  be.companyid <> pp.companyid
        OR be.branchid  <> pp.branchid;

    IF bad_count > 0 THEN
        RAISE EXCEPTION
            'CP-3B2a preflight failed: % PayrollBonusEvents row(s) have '
            'CompanyID/BranchID inconsistent with their owning PayrollPeriods '
            'row. Inspect and correct these rows manually before running '
            'migration 0059.', bad_count;
    END IF;
END
$$;


-- ---------------------------------------------------------------------------
-- 2. PayrollPeriods.BonusDataRevision
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.PayrollPeriods
    ADD COLUMN BonusDataRevision BIGINT NOT NULL DEFAULT 0;


-- ---------------------------------------------------------------------------
-- 3. DB-level ownership trigger on PayrollBonusEvents
-- ---------------------------------------------------------------------------
-- Belt-and-suspenders on top of the service-level eligibility/branch guard:
-- a bonus event's CompanyID/BranchID must always match its owning period's
-- CompanyID/BranchID.  This closes the known CP-3A P2 gap (no DB-level
-- enforcement existed; CP-3B1 only mitigated it at the read/aggregation
-- layer by filtering on BranchID explicitly).
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION payroll.trg_fn_bonusevents_ownership()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    period_company INTEGER;
    period_branch  INTEGER;
BEGIN
    SELECT companyid, branchid INTO period_company, period_branch
    FROM   payroll.payrollperiods
    WHERE  payrollperiodid = NEW.payrollperiodid;

    IF period_company IS DISTINCT FROM NEW.companyid
    OR period_branch  IS DISTINCT FROM NEW.branchid THEN
        RAISE EXCEPTION
            'PayrollBonusEvents: period % belongs to company/branch %/% but row is for %/%',
            NEW.payrollperiodid, period_company, period_branch,
            NEW.companyid, NEW.branchid
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_bonusevents_ownership
    BEFORE INSERT OR UPDATE ON payroll.PayrollBonusEvents
    FOR EACH ROW
    EXECUTE FUNCTION payroll.trg_fn_bonusevents_ownership();


-- ---------------------------------------------------------------------------
-- 4. payroll.PayrollBonusBatchRequests (foundation only — no writer yet)
-- ---------------------------------------------------------------------------

CREATE TABLE payroll.PayrollBonusBatchRequests (

    PayrollBonusBatchRequestID  BIGSERIAL     NOT NULL,
    CompanyID                   INTEGER       NOT NULL,
    BranchID                    INTEGER       NOT NULL,
    PayrollPeriodID             INTEGER       NOT NULL,

    IdempotencyKey              VARCHAR(200)  NOT NULL,
    RequestHash                 VARCHAR(64)   NOT NULL,
    RequestPayloadJSON          JSONB         NOT NULL,
    BatchCorrelationID          UUID          NOT NULL,

    ExpectedBonusDataRevision   BIGINT        NOT NULL,
    ResultBonusDataRevision     BIGINT        NOT NULL,

    CreatedEventIDs             JSONB         NOT NULL DEFAULT '[]'::jsonb,
    CreatedEventCount           INTEGER       NOT NULL DEFAULT 0,

    Status                      VARCHAR(20)   NOT NULL DEFAULT 'Applied',

    CreatedByUserID             INTEGER,
    CreatedAtUtc                TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    AppliedAtUtc                TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_PayrollBonusBatchRequests
        PRIMARY KEY (PayrollBonusBatchRequestID),

    CONSTRAINT fk_PayrollBonusBatchRequests_Company
        FOREIGN KEY (CompanyID)
        REFERENCES core.Companies(CompanyID),

    CONSTRAINT fk_PayrollBonusBatchRequests_Branch
        FOREIGN KEY (BranchID)
        REFERENCES core.Branches(BranchID),

    CONSTRAINT fk_PayrollBonusBatchRequests_Period
        FOREIGN KEY (PayrollPeriodID)
        REFERENCES payroll.PayrollPeriods(PayrollPeriodID),

    CONSTRAINT fk_PayrollBonusBatchRequests_Creator
        FOREIGN KEY (CreatedByUserID)
        REFERENCES sec.Users(UserID),

    -- CP-3B2a: only 'Applied' exists — a batch either commits entirely
    -- (all-or-nothing) or the request transaction rolls back and no row is
    -- ever written.  Additional statuses (e.g. 'Failed') are not needed
    -- until/unless a future unit introduces durable failure records.
    CONSTRAINT ck_PayrollBonusBatchRequests_Status
        CHECK (Status IN ('Applied')),

    -- CP-3B2a: no writer exists yet, so this simply guards the column's
    -- domain. CP-3B2b's service layer will additionally reject empty
    -- batches (CreatedEventCount > 0) before it ever reaches this table.
    CONSTRAINT ck_PayrollBonusBatchRequests_EventCount
        CHECK (CreatedEventCount >= 0),

    CONSTRAINT ck_PayrollBonusBatchRequests_HashLength
        CHECK (char_length(RequestHash) = 64)
);

-- Idempotency backbone: a client retry with the same key inside the same
-- period is detected here; the future batch endpoint (CP-3B2b) looks this
-- row up before writing anything.
CREATE UNIQUE INDEX ux_PayrollBonusBatchRequests_Idempotency
    ON payroll.PayrollBonusBatchRequests (CompanyID, BranchID, PayrollPeriodID, IdempotencyKey);

-- Every batch gets one durable, globally unique correlation ID.
CREATE UNIQUE INDEX ux_PayrollBonusBatchRequests_Correlation
    ON payroll.PayrollBonusBatchRequests (BatchCorrelationID);

-- Hot-path: list/paginate batch requests for a period (e.g. for a future
-- audit/history view).
CREATE INDEX ix_PayrollBonusBatchRequests_Period
    ON payroll.PayrollBonusBatchRequests (PayrollPeriodID, CreatedAtUtc DESC);


-- ---------------------------------------------------------------------------
-- 5. BatchCorrelationID index on PayrollBonusEvents
-- ---------------------------------------------------------------------------
-- Partial: BatchCorrelationID is NULL for every event until CP-3B2b's batch
-- endpoint exists, so a partial index keeps it cheap until then.
-- ---------------------------------------------------------------------------

CREATE INDEX ix_PayrollBonusEvents_BatchCorrelation
    ON payroll.PayrollBonusEvents (BatchCorrelationID)
    WHERE BatchCorrelationID IS NOT NULL;
