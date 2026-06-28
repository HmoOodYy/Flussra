-- =============================================================================
-- Migration 0058: Canonical Bonus Event Domain (CP-3A)
--
-- Converts the unused payroll.PayrollRunBonuses table into the canonical
-- payroll.PayrollBonusEvents domain table.  This is the single source of
-- truth for all bonus events; generic Period Pay (PayrollDraftLines) can
-- no longer accept BONUS line type after this migration is applied.
--
-- Changes:
--   1. Preflight guards (fail if unsafe to migrate).
--   2. Rename PayrollRunBonuses -> PayrollBonusEvents; rename PK column.
--   3. Drop EmployeeID, PayItemID, IncludeInMinimumPayComparison (schema mismatches).
--   4. Update Status domain to 'Active' / 'Voided'.
--   5. Rename FK constraints to match new table name.
--   6. Add Notes, SourceDraftLineID, BatchCorrelationID, IdempotencyKey,
--      DataRevision columns.
--   7. Replace old indexes with canonical ones; add partial unique index
--      on SourceDraftLineID.
--   8. Backfill: migrate existing BONUS Period Pay DraftLines into
--      PayrollBonusEvents (positive amounts only).
--   9. Add BonusEventID (nullable FK) to PayrollFinalLines for the
--      finalization bridge; add partial unique index.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. Preflight guards
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    -- Refuse if any BONUS DraftLine has a zero or negative CalculatedAmount.
    -- The canonical domain only allows positive bonus amounts; migrated rows
    -- must satisfy the same invariant.
    IF EXISTS (
        SELECT 1
        FROM payroll.payrolldraftlines
        WHERE linetype          = 'BONUS'
          AND linescope         = 'Period'
          AND (calculatedamount IS NULL OR calculatedamount <= 0)
    ) THEN
        RAISE EXCEPTION
            'CP-3A preflight failed: one or more BONUS DraftLines have zero or '
            'negative CalculatedAmount.  Inspect and correct those rows before '
            'running migration 0058.';
    END IF;

    -- Refuse if PayrollRunBonuses already contains application-written rows.
    -- The table is expected to be empty (it was never used by application code).
    IF EXISTS (SELECT 1 FROM payroll.payrollrunbonuses LIMIT 1) THEN
        RAISE EXCEPTION
            'CP-3A preflight failed: payroll.PayrollRunBonuses contains existing '
            'rows.  This table was expected to be empty.  Inspect the rows and '
            'resolve manually before running migration 0058.';
    END IF;
END
$$;


-- ---------------------------------------------------------------------------
-- 2. Rename table, primary key column, and sequence
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.PayrollRunBonuses RENAME TO PayrollBonusEvents;

ALTER TABLE payroll.PayrollBonusEvents
    RENAME COLUMN PayrollRunBonusID TO PayrollBonusEventID;

ALTER SEQUENCE payroll.payrollrunbonuses_payrollrunbonusid_seq
    RENAME TO payrollbonusevents_payrollbonuseventid_seq;


-- ---------------------------------------------------------------------------
-- 3. Drop columns incompatible with the canonical domain model
-- ---------------------------------------------------------------------------

-- FK constraints must be dropped before the columns they reference can be removed.
ALTER TABLE payroll.PayrollBonusEvents DROP CONSTRAINT fk_PayrollRunBonuses_Employee;
ALTER TABLE payroll.PayrollBonusEvents DROP CONSTRAINT fk_PayrollRunBonuses_PayItem;

ALTER TABLE payroll.PayrollBonusEvents DROP COLUMN EmployeeID;
ALTER TABLE payroll.PayrollBonusEvents DROP COLUMN PayItemID;

-- Bonus is always excluded from min/max by domain rule (CP-3C will enforce
-- this in the formula).  The configurable flag is a product anti-pattern.
ALTER TABLE payroll.PayrollBonusEvents DROP COLUMN IncludeInMinimumPayComparison;


-- ---------------------------------------------------------------------------
-- 4. Update Status domain to 'Active' / 'Voided'
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.PayrollBonusEvents DROP CONSTRAINT ck_PayrollRunBonuses_Status;
ALTER TABLE payroll.PayrollBonusEvents DROP CONSTRAINT ck_PayrollRunBonuses_VoidConsistency;
ALTER TABLE payroll.PayrollBonusEvents DROP CONSTRAINT ck_PayrollRunBonuses_Amount;

ALTER TABLE payroll.PayrollBonusEvents
    ALTER COLUMN Status SET DEFAULT 'Active';

ALTER TABLE payroll.PayrollBonusEvents
    ADD CONSTRAINT ck_PayrollBonusEvents_Amount
    CHECK (Amount > 0);

ALTER TABLE payroll.PayrollBonusEvents
    ADD CONSTRAINT ck_PayrollBonusEvents_Status
    CHECK (Status IN ('Active', 'Voided'));

ALTER TABLE payroll.PayrollBonusEvents
    ADD CONSTRAINT ck_PayrollBonusEvents_VoidConsistency
    CHECK (
        (Status = 'Voided' AND VoidedAtUtc IS NOT NULL)
        OR (Status <> 'Voided' AND VoidedAtUtc IS NULL)
    );


-- ---------------------------------------------------------------------------
-- 5. Rename remaining FK constraints to match new table name
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.PayrollBonusEvents
    RENAME CONSTRAINT fk_PayrollRunBonuses_Company TO fk_PayrollBonusEvents_Company;

ALTER TABLE payroll.PayrollBonusEvents
    RENAME CONSTRAINT fk_PayrollRunBonuses_Branch TO fk_PayrollBonusEvents_Branch;

ALTER TABLE payroll.PayrollBonusEvents
    RENAME CONSTRAINT fk_PayrollRunBonuses_Period TO fk_PayrollBonusEvents_Period;

ALTER TABLE payroll.PayrollBonusEvents
    RENAME CONSTRAINT fk_PayrollRunBonuses_Driver TO fk_PayrollBonusEvents_Driver;

ALTER TABLE payroll.PayrollBonusEvents
    RENAME CONSTRAINT fk_PayrollRunBonuses_Creator TO fk_PayrollBonusEvents_Creator;

ALTER TABLE payroll.PayrollBonusEvents
    RENAME CONSTRAINT fk_PayrollRunBonuses_Updater TO fk_PayrollBonusEvents_Updater;

ALTER TABLE payroll.PayrollBonusEvents
    RENAME CONSTRAINT fk_PayrollRunBonuses_Voider TO fk_PayrollBonusEvents_Voider;


-- ---------------------------------------------------------------------------
-- 6. Add canonical columns
-- ---------------------------------------------------------------------------

-- Notes (free-form, separate from Reason which is a short label)
ALTER TABLE payroll.PayrollBonusEvents ADD COLUMN Notes TEXT;

-- Bridge FK to the legacy BONUS DraftLine this event was migrated from.
-- NULL for events created directly through the /bonuses API after cutover.
ALTER TABLE payroll.PayrollBonusEvents ADD COLUMN SourceDraftLineID BIGINT;

-- Correlation + idempotency fields for batch bonus support (CP-3B).
ALTER TABLE payroll.PayrollBonusEvents ADD COLUMN BatchCorrelationID UUID;
ALTER TABLE payroll.PayrollBonusEvents ADD COLUMN IdempotencyKey VARCHAR(200);

-- Optimistic-concurrency revision counter.
ALTER TABLE payroll.PayrollBonusEvents ADD COLUMN DataRevision BIGINT NOT NULL DEFAULT 1;

-- FK for the legacy bridge column.
ALTER TABLE payroll.PayrollBonusEvents
    ADD CONSTRAINT fk_PayrollBonusEvents_SourceDraftLine
    FOREIGN KEY (SourceDraftLineID)
    REFERENCES payroll.PayrollDraftLines(DraftLineID);


-- ---------------------------------------------------------------------------
-- 7. Replace old indexes with canonical ones
-- ---------------------------------------------------------------------------

DROP INDEX IF EXISTS payroll.ix_PayrollRunBonuses_Company_Branch_Status;
DROP INDEX IF EXISTS payroll.ix_PayrollRunBonuses_CreatedAtUtc;
DROP INDEX IF EXISTS payroll.ix_PayrollRunBonuses_Driver_Status;
DROP INDEX IF EXISTS payroll.ix_PayrollRunBonuses_Employee_Status;
DROP INDEX IF EXISTS payroll.ix_PayrollRunBonuses_MinimumPayComparison;
DROP INDEX IF EXISTS payroll.ix_PayrollRunBonuses_PayItem_Status;
DROP INDEX IF EXISTS payroll.ix_PayrollRunBonuses_Period_Status;

-- Hot-path: list bonus events for a period / driver
CREATE INDEX ix_PayrollBonusEvents_Period_Driver
    ON payroll.PayrollBonusEvents (PayrollPeriodID, DriverID, Status);

-- Tenant-scoped read path
CREATE INDEX ix_PayrollBonusEvents_Company_Branch_Status
    ON payroll.PayrollBonusEvents (CompanyID, BranchID, Status);

-- Per-driver lookups (eligibility, history)
CREATE INDEX ix_PayrollBonusEvents_Driver_Status
    ON payroll.PayrollBonusEvents (DriverID, Status);

-- Audit / time-ordered listing
CREATE INDEX ix_PayrollBonusEvents_CreatedAtUtc
    ON payroll.PayrollBonusEvents (CreatedAtUtc);

-- Idempotency guard: each legacy DraftLine maps to at most one BonusEvent.
CREATE UNIQUE INDEX ux_PayrollBonusEvents_SourceDraftLine
    ON payroll.PayrollBonusEvents (SourceDraftLineID)
    WHERE SourceDraftLineID IS NOT NULL;


-- ---------------------------------------------------------------------------
-- 8. Backfill: migrate BONUS Period Pay DraftLines -> PayrollBonusEvents
-- ---------------------------------------------------------------------------
-- Only positive-amount rows are migrated (enforced by the preflight check
-- above and the ck_PayrollBonusEvents_Amount constraint).
-- VoidedAtUtc for Void DraftLines is set to AddedAtUtc because DraftLines
-- do not store a separate void timestamp; AddedAtUtc is the closest proxy.
-- ---------------------------------------------------------------------------

INSERT INTO payroll.PayrollBonusEvents
    (CompanyID, BranchID, PayrollPeriodID, DriverID,
     Amount, Reason, Notes, Status,
     SourceDraftLineID,
     CreatedByUserID, CreatedAtUtc,
     UpdatedByUserID, UpdatedAtUtc,
     VoidedByUserID,  VoidedAtUtc, VoidReason,
     DataRevision)
SELECT
    dl.companyid,
    dl.branchid,
    dl.payrollperiodid,
    dl.driverid,
    dl.calculatedamount,
    NULL,                   -- Reason: not tracked in legacy DraftLines
    dl.notes,
    CASE WHEN dl.status = 'Void' THEN 'Voided' ELSE 'Active' END,
    dl.draftlineid,         -- SourceDraftLineID bridge FK
    dl.addedbyuserid,
    dl.addedatutc,
    NULL,                   -- UpdatedByUserID: not tracked in DraftLines
    NULL,                   -- UpdatedAtUtc
    NULL,                   -- VoidedByUserID: not tracked in DraftLines
    CASE WHEN dl.status = 'Void'
         THEN COALESCE(dl.addedatutc, NOW())
         ELSE NULL
    END,
    NULL,                   -- VoidReason
    1                       -- DataRevision
FROM payroll.payrolldraftlines dl
WHERE dl.linetype          = 'BONUS'
  AND dl.linescope         = 'Period'
  AND dl.calculatedamount  > 0
ON CONFLICT (sourcedraftlineid)
    WHERE sourcedraftlineid IS NOT NULL
DO NOTHING;


-- ---------------------------------------------------------------------------
-- 9. Add BonusEventID to PayrollFinalLines (finalization bridge)
-- ---------------------------------------------------------------------------
-- Nullable: DraftLine-sourced rows keep DraftLineID and have BonusEventID=NULL.
-- BonusEvent-sourced rows have BonusEventID set and DraftLineID=NULL.
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.PayrollFinalLines
    ADD COLUMN IF NOT EXISTS BonusEventID BIGINT;

ALTER TABLE payroll.PayrollFinalLines
    ADD CONSTRAINT fk_FinalLines_BonusEvent
    FOREIGN KEY (BonusEventID)
    REFERENCES payroll.PayrollBonusEvents(PayrollBonusEventID);

-- Prevent a bonus event from being finalized into the same period twice.
CREATE UNIQUE INDEX ux_PayrollFinalLines_Period_BonusEvent
    ON payroll.PayrollFinalLines (PayrollPeriodID, BonusEventID)
    WHERE BonusEventID IS NOT NULL;
