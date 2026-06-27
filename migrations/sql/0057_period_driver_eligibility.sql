-- =============================================================================
-- Migration 0057: Period Driver Eligibility Snapshot
--
-- Introduces payroll.PayrollPeriodDriverEligibility: a period-level snapshot
-- of which drivers are eligible for each payroll period, so that all payroll
-- paths agree on eligibility without re-querying mutable live EmploymentStatus
-- or DriverStatus after the period is frozen.
--
-- Changes:
--   1. Create payroll.PayrollPeriodDriverEligibility with full constraints.
--   2. Add ownership enforcement trigger.
--   3. Backfill non-finalized periods (Draft/Open/Returned/InReview/Approved).
--   4. Add hot-path indexes on PayrollDraftLines and
--      PayrollPeriodDriverDayEntryState if missing.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. Create payroll.PayrollPeriodDriverEligibility
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payroll.PayrollPeriodDriverEligibility (

    PayrollPeriodDriverEligibilityID  SERIAL          NOT NULL,
    CompanyID               INTEGER         NOT NULL,
    BranchID                INTEGER         NOT NULL,
    PayrollPeriodID         INTEGER         NOT NULL,
    DriverID                INTEGER         NOT NULL,
    SourceEmployeeID        INTEGER,

    -- Snapshotted identity
    DriverCodeSnapshot      VARCHAR(100),
    DriverNameSnapshot      VARCHAR(255),
    EmployeeKeySnapshot     VARCHAR(100),
    DriverStatusSnapshot    VARCHAR(50),
    EmploymentStatusSnapshot VARCHAR(50),
    TransferredFromDriverIDSnapshot INTEGER,
    TransferredToDriverIDSnapshot   INTEGER,

    -- Snapshotted date windows
    HireDateSnapshot            DATE,
    TerminationDateSnapshot     DATE,
    DriverEffectiveFromSnapshot DATE,
    DriverEffectiveToSnapshot   DATE,

    -- Eligibility classification
    IsEligibleForPeriod     BOOLEAN         NOT NULL DEFAULT TRUE,
    EligibilityReasonCode   VARCHAR(50)     NOT NULL,
    SnapshotSource          VARCHAR(50)     NOT NULL,

    -- Audit
    CreatedAtUtc            TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CreatedByUserID         INTEGER,
    UpdatedAtUtc            TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    FrozenAtUtc             TIMESTAMPTZ,
    FrozenByUserID          INTEGER,

    CONSTRAINT pk_PayrollPeriodDriverEligibility
        PRIMARY KEY (PayrollPeriodDriverEligibilityID),

    CONSTRAINT fk_PPDE_Period
        FOREIGN KEY (PayrollPeriodID)
        REFERENCES payroll.PayrollPeriods(PayrollPeriodID)
        ON DELETE CASCADE,

    CONSTRAINT fk_PPDE_Driver
        FOREIGN KEY (DriverID)
        REFERENCES core.Drivers(DriverID),

    CONSTRAINT fk_PPDE_Branch
        FOREIGN KEY (BranchID)
        REFERENCES core.Branches(BranchID),

    CONSTRAINT fk_PPDE_Company
        FOREIGN KEY (CompanyID)
        REFERENCES core.Companies(CompanyID),

    CONSTRAINT uq_PPDE_Period_Driver
        UNIQUE (PayrollPeriodID, DriverID),

    CONSTRAINT chk_PPDE_ReasonCode
        CHECK (EligibilityReasonCode IN (
            'Active', 'TerminatedHistorical', 'Transferred', 'IncludedByExistingData'
        )),

    CONSTRAINT chk_PPDE_SnapshotSource
        CHECK (SnapshotSource IN ('Generated', 'Backfill', 'ExistingData'))
);


-- ---------------------------------------------------------------------------
-- Ownership enforcement trigger
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION payroll.trg_fn_ppde_ownership()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    period_company  INTEGER;
    period_branch   INTEGER;
    driver_company  INTEGER;
    driver_branch   INTEGER;
    branch_company  INTEGER;
BEGIN
    -- Period must belong to this company/branch
    SELECT companyid, branchid INTO period_company, period_branch
    FROM payroll.payrollperiods
    WHERE payrollperiodid = NEW.payrollperiodid;

    IF period_company IS DISTINCT FROM NEW.companyid
    OR period_branch  IS DISTINCT FROM NEW.branchid THEN
        RAISE EXCEPTION
            'PayrollPeriodDriverEligibility: period % belongs to company/branch %/% but row is for %/%',
            NEW.payrollperiodid, period_company, period_branch,
            NEW.companyid, NEW.branchid
            USING ERRCODE = 'check_violation';
    END IF;

    -- Driver must belong to this company/branch
    SELECT companyid, branchid INTO driver_company, driver_branch
    FROM core.drivers
    WHERE driverid = NEW.driverid;

    IF driver_company IS DISTINCT FROM NEW.companyid
    OR driver_branch  IS DISTINCT FROM NEW.branchid THEN
        RAISE EXCEPTION
            'PayrollPeriodDriverEligibility: driver % belongs to company/branch %/% but row is for %/%',
            NEW.driverid, driver_company, driver_branch,
            NEW.companyid, NEW.branchid
            USING ERRCODE = 'check_violation';
    END IF;

    -- Branch must belong to this company
    SELECT companyid INTO branch_company
    FROM core.branches
    WHERE branchid = NEW.branchid;

    IF branch_company IS DISTINCT FROM NEW.companyid THEN
        RAISE EXCEPTION
            'PayrollPeriodDriverEligibility: branch % belongs to company % but row is for company %',
            NEW.branchid, branch_company, NEW.companyid
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_ppde_ownership
    BEFORE INSERT OR UPDATE ON payroll.PayrollPeriodDriverEligibility
    FOR EACH ROW EXECUTE FUNCTION payroll.trg_fn_ppde_ownership();


-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_PPDE_Company_Branch_Period
    ON payroll.PayrollPeriodDriverEligibility (CompanyID, BranchID, PayrollPeriodID);

CREATE INDEX IF NOT EXISTS ix_PPDE_Period_Driver
    ON payroll.PayrollPeriodDriverEligibility (PayrollPeriodID, DriverID);

CREATE INDEX IF NOT EXISTS ix_PPDE_Driver_Company
    ON payroll.PayrollPeriodDriverEligibility (DriverID, CompanyID);

CREATE INDEX IF NOT EXISTS ix_PPDE_Active
    ON payroll.PayrollPeriodDriverEligibility (PayrollPeriodID, CompanyID, BranchID)
    WHERE IsEligibleForPeriod = TRUE;


-- ---------------------------------------------------------------------------
-- 2. Ensure hot-path indexes exist on dependent tables
-- ---------------------------------------------------------------------------

-- PayrollDraftLines: (PayrollPeriodID, DriverID, WorkDate) for IncludedByExistingData lookup
CREATE INDEX IF NOT EXISTS ix_DraftLines_Period_Driver_WorkDate
    ON payroll.PayrollDraftLines (PayrollPeriodID, DriverID, WorkDate)
    WHERE Status != 'Void' AND WorkDate IS NOT NULL;

-- PayrollPeriodDriverDayEntryState: fast lookups for (PayrollPeriodID, DriverID, WorkDate)
-- The table has a UNIQUE constraint on (PayrollPeriodID, DriverID, WorkDate) from CP-2D1
-- so the uniqueness index is already implicit; add explicit one for non-voided fast lookups.
CREATE INDEX IF NOT EXISTS ix_EntryState_Period_Driver_WorkDate
    ON payroll.PayrollPeriodDriverDayEntryState (PayrollPeriodID, DriverID, WorkDate)
    WHERE IsVoided = FALSE;


-- ---------------------------------------------------------------------------
-- 3. Backfill non-finalized periods
-- ---------------------------------------------------------------------------

-- Step 3a: Insert Active, TerminatedHistorical, Transferred rows
INSERT INTO payroll.PayrollPeriodDriverEligibility
    (CompanyID, BranchID, PayrollPeriodID, DriverID, SourceEmployeeID,
     DriverCodeSnapshot, DriverNameSnapshot, EmployeeKeySnapshot,
     DriverStatusSnapshot, EmploymentStatusSnapshot,
     TransferredFromDriverIDSnapshot, TransferredToDriverIDSnapshot,
     HireDateSnapshot, TerminationDateSnapshot,
     DriverEffectiveFromSnapshot, DriverEffectiveToSnapshot,
     IsEligibleForPeriod, EligibilityReasonCode, SnapshotSource,
     CreatedAtUtc)
SELECT
    pp.companyid,
    pp.branchid,
    pp.payrollperiodid,
    d.driverid,
    e.employeeid,
    d.drivercode,
    e.fullname,
    e.employeekey,
    d.driverstatus,
    e.employmentstatus,
    d.transferredfromdriverid,
    d.transferredtodriverid,
    e.hiredate,
    e.terminationdate,
    d.effectivefrom,
    d.effectiveto,
    TRUE,
    CASE
        WHEN d.driverstatus = 'Terminated' THEN 'TerminatedHistorical'
        WHEN d.driverstatus = 'Transferred' THEN 'Transferred'
        ELSE 'Active'
    END,
    'Backfill',
    NOW()
FROM payroll.payrollperiods pp
JOIN core.drivers d
    ON  d.companyid = pp.companyid
    AND d.branchid  = pp.branchid
JOIN core.employees e ON e.employeeid = d.employeeid
WHERE pp.status NOT IN ('Locked', 'Archived', 'Cancelled')
  AND (
    -- Path 1: Active
    (    d.driverstatus     = 'Active'
     AND e.employmentstatus = 'Active'
     AND (e.hiredate IS NULL OR e.hiredate <= pp.enddate)
     AND (e.terminationdate IS NULL OR e.terminationdate >= pp.startdate)
     AND (d.effectivefrom IS NULL OR d.effectivefrom <= pp.enddate)
     AND (d.effectiveto   IS NULL OR d.effectiveto   >= pp.startdate)
    )
    OR
    -- Path 2: TerminatedHistorical
    (    d.driverstatus     = 'Terminated'
     AND e.employmentstatus = 'Terminated'
     AND e.terminationdate IS NOT NULL
     AND e.terminationdate >= pp.startdate
     AND (e.hiredate IS NULL OR e.hiredate <= pp.enddate)
     AND (d.effectivefrom IS NULL OR d.effectivefrom <= pp.enddate)
     AND (d.effectiveto   IS NULL OR d.effectiveto   >= pp.startdate)
    )
    OR
    -- Path 3: Transferred
    (    d.driverstatus     = 'Transferred'
     AND e.employmentstatus = 'Active'
     AND d.effectiveto IS NOT NULL
     AND d.effectiveto >= pp.startdate
     AND (d.effectivefrom IS NULL OR d.effectivefrom <= pp.enddate)
     AND (e.hiredate IS NULL OR e.hiredate <= pp.enddate)
     AND (e.terminationdate IS NULL OR e.terminationdate >= pp.startdate)
    )
  )
ON CONFLICT (PayrollPeriodID, DriverID) DO NOTHING;


-- Step 3b: Insert IncludedByExistingData rows for drivers with existing source not captured above
INSERT INTO payroll.PayrollPeriodDriverEligibility
    (CompanyID, BranchID, PayrollPeriodID, DriverID, SourceEmployeeID,
     DriverCodeSnapshot, DriverNameSnapshot, EmployeeKeySnapshot,
     DriverStatusSnapshot, EmploymentStatusSnapshot,
     TransferredFromDriverIDSnapshot, TransferredToDriverIDSnapshot,
     HireDateSnapshot, TerminationDateSnapshot,
     DriverEffectiveFromSnapshot, DriverEffectiveToSnapshot,
     IsEligibleForPeriod, EligibilityReasonCode, SnapshotSource,
     CreatedAtUtc)
SELECT DISTINCT
    pp.companyid, pp.branchid, pp.payrollperiodid,
    d.driverid, e.employeeid,
    d.drivercode, e.fullname, e.employeekey, d.driverstatus, e.employmentstatus,
    d.transferredfromdriverid, d.transferredtodriverid,
    e.hiredate, e.terminationdate, d.effectivefrom, d.effectiveto,
    TRUE, 'IncludedByExistingData', 'Backfill',
    NOW()
FROM payroll.payrollperiods pp
JOIN (
    SELECT payrollperiodid, driverid, companyid
    FROM   payroll.payrolldraftlines
    WHERE  status != 'Void'
    UNION
    SELECT payrollperiodid, driverid, companyid
    FROM   payroll.payrollperioddriverdayentrystate
    WHERE  isvoided = FALSE
) src ON src.payrollperiodid = pp.payrollperiodid
      AND src.companyid = pp.companyid
JOIN core.drivers   d ON d.driverid   = src.driverid
                      AND d.companyid  = pp.companyid
                      AND d.branchid   = pp.branchid
JOIN core.employees e ON e.employeeid = d.employeeid
WHERE pp.status NOT IN ('Locked', 'Archived', 'Cancelled')
ON CONFLICT (PayrollPeriodID, DriverID) DO NOTHING;


-- Step 3c: Freeze rows for periods that are already Open or later
UPDATE payroll.PayrollPeriodDriverEligibility ppde
SET    frozenatutc = NOW()
FROM   payroll.payrollperiods pp
WHERE  pp.payrollperiodid = ppde.payrollperiodid
  AND  pp.status IN ('Open', 'Returned', 'InReview', 'Approved')
  AND  ppde.frozenatutc IS NULL;


-- ---------------------------------------------------------------------------
-- 4. Snapshot marker table
--    One row per period that has been snapshotted — distinguishes "zero eligible
--    drivers (snapshotted)" from "legacy period (no snapshot at all)".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payroll.PayrollPeriodEligibilitySnapshots (
    PayrollPeriodEligibilitySnapshotID  SERIAL       NOT NULL,
    PayrollPeriodID   INTEGER          NOT NULL,
    CompanyID         INTEGER          NOT NULL,
    BranchID          INTEGER          NOT NULL,
    SnapshotSource    VARCHAR(50)      NOT NULL,
    CreatedAtUtc      TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    CreatedByUserID   INTEGER,
    UpdatedAtUtc      TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    FrozenAtUtc       TIMESTAMPTZ,
    FrozenByUserID    INTEGER,

    CONSTRAINT pk_PPES
        PRIMARY KEY (PayrollPeriodEligibilitySnapshotID),

    CONSTRAINT uq_PPES_Period
        UNIQUE (PayrollPeriodID),

    CONSTRAINT fk_PPES_Period
        FOREIGN KEY (PayrollPeriodID)
        REFERENCES payroll.PayrollPeriods(PayrollPeriodID)
        ON DELETE CASCADE,

    CONSTRAINT fk_PPES_Company
        FOREIGN KEY (CompanyID)
        REFERENCES core.Companies(CompanyID),

    CONSTRAINT fk_PPES_Branch
        FOREIGN KEY (BranchID)
        REFERENCES core.Branches(BranchID),

    CONSTRAINT chk_PPES_Source
        CHECK (SnapshotSource IN ('Generated', 'Backfill', 'ExistingData'))
);

CREATE INDEX IF NOT EXISTS ix_PPES_Period
    ON payroll.PayrollPeriodEligibilitySnapshots (PayrollPeriodID);

CREATE INDEX IF NOT EXISTS ix_PPES_Company_Branch
    ON payroll.PayrollPeriodEligibilitySnapshots (CompanyID, BranchID);


-- ---------------------------------------------------------------------------
-- Ownership trigger for marker table
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION payroll.trg_fn_ppes_ownership()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    period_company  INTEGER;
    period_branch   INTEGER;
    branch_company  INTEGER;
BEGIN
    SELECT companyid, branchid INTO period_company, period_branch
    FROM payroll.payrollperiods
    WHERE payrollperiodid = NEW.payrollperiodid;

    IF period_company IS DISTINCT FROM NEW.companyid
    OR period_branch  IS DISTINCT FROM NEW.branchid THEN
        RAISE EXCEPTION
            'PayrollPeriodEligibilitySnapshots: period % belongs to company/branch %/% but row is for %/%',
            NEW.payrollperiodid, period_company, period_branch,
            NEW.companyid, NEW.branchid
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT companyid INTO branch_company
    FROM core.branches
    WHERE branchid = NEW.branchid;

    IF branch_company IS DISTINCT FROM NEW.companyid THEN
        RAISE EXCEPTION
            'PayrollPeriodEligibilitySnapshots: branch % belongs to company % but row is for company %',
            NEW.branchid, branch_company, NEW.companyid
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_ppes_ownership
    BEFORE INSERT OR UPDATE ON payroll.PayrollPeriodEligibilitySnapshots
    FOR EACH ROW EXECUTE FUNCTION payroll.trg_fn_ppes_ownership();


-- ---------------------------------------------------------------------------
-- 5. Backfill marker rows for ALL non-finalized periods
--    (regardless of whether they have detail rows — a zero-driver period is
--    still a valid empty snapshot and must not fall back to live roster).
-- ---------------------------------------------------------------------------

-- 5a: Insert marker for every non-finalized period (Draft/Open/Returned/InReview/Approved).
--     Draft periods get FrozenAtUtc = NULL; all others get FrozenAtUtc = NOW().
--     ON CONFLICT DO NOTHING so periods that already received a marker from
--     Step 3 (via detail rows) are not clobbered.
INSERT INTO payroll.PayrollPeriodEligibilitySnapshots
    (PayrollPeriodID, CompanyID, BranchID, SnapshotSource, CreatedAtUtc, FrozenAtUtc)
SELECT
    pp.payrollperiodid,
    pp.companyid,
    pp.branchid,
    'Backfill',
    NOW(),
    CASE
        WHEN pp.status = 'Draft' THEN NULL
        ELSE NOW()
    END
FROM payroll.payrollperiods pp
WHERE pp.status IN ('Draft', 'Open', 'Returned', 'InReview', 'Approved')
ON CONFLICT (PayrollPeriodID) DO NOTHING;

-- 5b: Freeze marker for periods already Open or later that still have FrozenAtUtc = NULL
--     (handles any edge case where a row was inserted without a FrozenAtUtc value,
--     e.g. from an earlier partial run).
UPDATE payroll.PayrollPeriodEligibilitySnapshots ppes
SET    FrozenAtUtc  = NOW(),
       UpdatedAtUtc = NOW()
FROM   payroll.payrollperiods pp
WHERE  pp.payrollperiodid = ppes.payrollperiodid
  AND  pp.status IN ('Open', 'Returned', 'InReview', 'Approved')
  AND  ppes.FrozenAtUtc IS NULL;
