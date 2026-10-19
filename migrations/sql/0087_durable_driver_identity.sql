-- 0087: Durable Driver identity and Driver assignments.
-- Historical migrations remain immutable. core.Drivers (a branch-bound profile)
-- becomes core.DriverAssignments (one continuous home-Branch tenure) and a new
-- core.Drivers holds the permanent Driver. This pre-production cutover requires a
-- clean rebuild when Driver, transfer or payroll evidence already exists because
-- a permanent Driver cannot be truthfully inferred from existing profiles.

DO $preflight$
BEGIN
    IF EXISTS (SELECT 1 FROM core.Drivers)
       OR EXISTS (SELECT 1 FROM core.DriverTransferRequests)
       OR EXISTS (SELECT 1 FROM payroll.PayrollPeriods) THEN
        RAISE EXCEPTION
            'DURABLE_DRIVER_REBUILD_REQUIRED: 0087 cannot infer permanent Drivers from existing profiles or rewrite payroll evidence. '
            'Reset and reseed this preproduction database, then apply migrations again.';
    END IF;
END
$preflight$;

-- ---------------------------------------------------------------------------
-- 1. Retire objects with no application consumer and objects that embed the
--    old profile meaning.
-- ---------------------------------------------------------------------------

-- Unused legacy views; app.vw_PayrollPeriodList is recreated below.
DROP VIEW app.vw_HomeSummary;
DROP VIEW app.vw_PayrollDraftSummary;
DROP VIEW app.vw_ActiveDrivers;

-- Aliases are designed in C2 Import as source system + external key + Driver.
DROP TABLE core.DriverAliases;

DROP TRIGGER trg_Drivers_HistoryImmutable ON core.Drivers;
DROP TRIGGER trg_Drivers_IdentityImmutable ON core.Drivers;
DROP FUNCTION core.trg_Drivers_HistoryImmutable();
DROP FUNCTION core.trg_Drivers_IdentityImmutable();
DROP FUNCTION core.fn_EffectiveDriverProfile(INTEGER, INTEGER, TIMESTAMPTZ);
DROP FUNCTION core.fn_DriverCoversWorkDate(INTEGER, INTEGER, INTEGER, DATE);

ALTER TABLE core.Drivers
    DROP CONSTRAINT ck_Drivers_EffectiveWindowAt,
    DROP CONSTRAINT ck_Drivers_ClosedHistoryAt,
    DROP CONSTRAINT ck_Drivers_Status,
    DROP CONSTRAINT excl_Drivers_Employee_AssignmentWindow,
    DROP CONSTRAINT fk_Drivers_Branch,
    DROP CONSTRAINT fk_Drivers_Employee,
    DROP CONSTRAINT fk_Drivers_Employee_Company,
    DROP CONSTRAINT fk_Drivers_TransferredFrom,
    DROP CONSTRAINT fk_Drivers_TransferredTo;
DROP INDEX core.ux_Drivers_Company_DriverCode;
DROP INDEX core.ux_Drivers_Employee_Branch_Active;

-- Transfer requests stop pointing at branch profiles; re-pointed in section 4.
ALTER TABLE core.DriverTransferRequests
    DROP CONSTRAINT fk_DTR_Driver,
    DROP CONSTRAINT fk_DTR_Driver_Company,
    DROP CONSTRAINT fk_DTR_NewDriver,
    DROP CONSTRAINT ck_DTR_Status,
    DROP CONSTRAINT ck_DTR_CompletedEffectiveAt;

-- Import matching targets the permanent Driver; re-pointed in section 3.
ALTER TABLE import.ImportRows DROP CONSTRAINT fk_ImportRows_Driver;

-- ---------------------------------------------------------------------------
-- 2. core.Drivers becomes core.DriverAssignments.
-- ---------------------------------------------------------------------------

ALTER TABLE core.Drivers RENAME TO DriverAssignments;
ALTER TABLE core.DriverAssignments RENAME COLUMN DriverID TO DriverAssignmentID;
ALTER TABLE core.DriverAssignments RENAME COLUMN TransferredFromDriverID TO TransferredFromAssignmentID;
ALTER TABLE core.DriverAssignments RENAME COLUMN TransferredToDriverID TO TransferredToAssignmentID;
ALTER SEQUENCE core.Drivers_DriverID_seq RENAME TO DriverAssignments_DriverAssignmentID_seq;
ALTER TABLE core.DriverAssignments RENAME CONSTRAINT Drivers_pkey TO DriverAssignments_pkey;
ALTER TABLE core.DriverAssignments RENAME CONSTRAINT fk_Drivers_Company TO fk_DriverAssignments_Company;
ALTER TABLE core.DriverAssignments RENAME CONSTRAINT fk_Drivers_Branch_Company TO fk_DriverAssignments_Branch_Company;
ALTER INDEX core.ux_Drivers_ID_Company RENAME TO ux_DriverAssignments_ID_Company;
ALTER INDEX core.ux_Drivers_ID_Company_Branch RENAME TO ux_DriverAssignments_ID_Company_Branch;

ALTER TABLE core.DriverAssignments
    DROP COLUMN DriverCode,
    DROP COLUMN CDLNumber,
    DROP COLUMN ExternalDriverID,
    DROP COLUMN DriverStatus,
    ADD COLUMN DriverID INTEGER NOT NULL,
    ADD COLUMN EndReason VARCHAR(20);

-- ---------------------------------------------------------------------------
-- 3. The permanent Driver.
-- ---------------------------------------------------------------------------

-- Every NOT NULL name below follows <table>_<column>_not_null once the column
-- renames of this migration are done; this first pass covers DriverAssignments
-- so that the new core.Drivers can take the freed Drivers_* names.
DO $rename_assignment_not_null$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT c.conname, a.attname
        FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
        WHERE c.conrelid = 'core.DriverAssignments'::regclass AND c.contype = 'n'
    LOOP
        IF r.conname <> format('driverassignments_%s_not_null', r.attname) THEN
            EXECUTE format(
                'ALTER TABLE core.DriverAssignments RENAME CONSTRAINT %I TO %I',
                r.conname, format('driverassignments_%s_not_null', r.attname));
        END IF;
    END LOOP;
END
$rename_assignment_not_null$;

CREATE TABLE core.Drivers (
    DriverID SERIAL PRIMARY KEY,
    CompanyID INTEGER NOT NULL,
    EmployeeID INTEGER NOT NULL,
    DriverNumber VARCHAR(100),
    CDLNumber VARCHAR(100),
    DriverStatus VARCHAR(30) NOT NULL DEFAULT 'Active',
    CreatedAtUtc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UpdatedAtUtc TIMESTAMPTZ,
    CONSTRAINT uq_Drivers_Employee UNIQUE (EmployeeID),
    CONSTRAINT ck_Drivers_Status CHECK (DriverStatus IN ('Active', 'Inactive', 'OnLeave')),
    CONSTRAINT fk_Drivers_Company FOREIGN KEY (CompanyID) REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_Drivers_Employee_Company FOREIGN KEY (EmployeeID, CompanyID)
        REFERENCES core.Employees(EmployeeID, CompanyID)
);

CREATE UNIQUE INDEX ux_Drivers_ID_Company ON core.Drivers (DriverID, CompanyID);
CREATE UNIQUE INDEX ux_Drivers_ID_Employee_Company ON core.Drivers (DriverID, EmployeeID, CompanyID);
CREATE UNIQUE INDEX ux_Drivers_Company_DriverNumber ON core.Drivers (CompanyID, DriverNumber)
    WHERE DriverNumber IS NOT NULL;

ALTER TABLE import.ImportRows
    ADD CONSTRAINT fk_ImportRows_Driver FOREIGN KEY (MatchedDriverID) REFERENCES core.Drivers(DriverID);

CREATE FUNCTION core.trg_Drivers_IdentityImmutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.EmployeeID IS DISTINCT FROM OLD.EmployeeID
       OR NEW.CompanyID IS DISTINCT FROM OLD.CompanyID THEN
        RAISE EXCEPTION 'Driver EmployeeID and CompanyID are immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_Drivers_IdentityImmutable
BEFORE UPDATE OF EmployeeID, CompanyID ON core.Drivers
FOR EACH ROW EXECUTE FUNCTION core.trg_Drivers_IdentityImmutable();

-- ---------------------------------------------------------------------------
-- 4. DriverAssignments constraints, functions and triggers.
-- ---------------------------------------------------------------------------

CREATE UNIQUE INDEX ux_DriverAssignments_ID_Driver ON core.DriverAssignments (DriverAssignmentID, DriverID);
CREATE INDEX ix_DriverAssignments_Employee ON core.DriverAssignments (EmployeeID);

ALTER TABLE core.DriverAssignments
    ADD CONSTRAINT fk_DriverAssignments_Driver FOREIGN KEY (DriverID, EmployeeID, CompanyID)
        REFERENCES core.Drivers(DriverID, EmployeeID, CompanyID),
    ADD CONSTRAINT fk_DriverAssignments_TransferredFrom
        FOREIGN KEY (TransferredFromAssignmentID, DriverID)
        REFERENCES core.DriverAssignments(DriverAssignmentID, DriverID),
    ADD CONSTRAINT fk_DriverAssignments_TransferredTo
        FOREIGN KEY (TransferredToAssignmentID, DriverID)
        REFERENCES core.DriverAssignments(DriverAssignmentID, DriverID),
    ADD CONSTRAINT ck_DriverAssignments_EndReason
        CHECK (EndReason IS NULL OR EndReason IN ('Transferred', 'Terminated')),
    ADD CONSTRAINT ck_DriverAssignments_EndReasonWindow
        CHECK ((EffectiveToAt IS NULL) = (EndReason IS NULL)),
    ADD CONSTRAINT ck_DriverAssignments_EffectiveWindowAt
        CHECK (EffectiveToAt IS NULL OR EffectiveToAt > EffectiveFromAt
               OR (EndReason = 'Terminated' AND EffectiveToAt = EffectiveFromAt)),
    ADD CONSTRAINT excl_DriverAssignments_Driver_Window
        EXCLUDE USING gist (
            DriverID WITH =,
            core.fn_DriverEffectiveRange(EffectiveFromAt, EffectiveToAt) WITH &&
        ) DEFERRABLE INITIALLY DEFERRED;

CREATE FUNCTION core.trg_DriverAssignments_IdentityImmutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.BranchID IS DISTINCT FROM OLD.BranchID THEN
        RAISE EXCEPTION 'DriverAssignment BranchID is immutable; create a new DriverAssignment for a branch change';
    END IF;
    IF NEW.DriverID IS DISTINCT FROM OLD.DriverID
       OR NEW.EmployeeID IS DISTINCT FROM OLD.EmployeeID
       OR NEW.CompanyID IS DISTINCT FROM OLD.CompanyID THEN
        RAISE EXCEPTION 'DriverAssignment DriverID, EmployeeID and CompanyID are immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_DriverAssignments_IdentityImmutable
BEFORE UPDATE OF BranchID, DriverID, EmployeeID, CompanyID ON core.DriverAssignments
FOR EACH ROW EXECUTE FUNCTION core.trg_DriverAssignments_IdentityImmutable();

-- History rules for assignment boundaries.
--   * The origin lineage (TransferredFromAssignmentID) never changes after insert.
--   * EffectiveFromAt changes only for a not-yet-started assignment, to another
--     future instant, under transfer_reschedule.
--   * An ended assignment (EffectiveToAt <= now) is immutable, except that
--     termination may cap a transferred assignment at the Branch-local cutoff.
--   * Closing an open assignment needs no operation when the end is the transaction
--     start or later (immediate and scheduled transfer); a past cap requires terminate.
--   * A future end changes only under transfer_cancel or transfer_reschedule (to
--     NULL or a later future instant) or under terminate.
--   * TransferredToAssignmentID changes only together with an end change.
--   * An assignment of a Terminated Employee is never reopened (its end and end
--     reason are never cleared), whatever the workforce operation.
-- Deletion is limited to a never-effective destination removed by a transfer cancel
-- or by the termination that disposes of that transfer; foreign keys stop any other
-- delete.
--
-- Clock: every "has it started / has it ended / is it still in the future" test uses
-- clock_timestamp(), not NOW(). NOW() is the transaction start, so a transaction that
-- waited on a lock would otherwise judge a boundary that has since passed as future.
-- The application takes its own decision instant (clock_timestamp()) after its locks;
-- the trigger runs later, so the database is at least as strict as that decision. The
-- one bound that keeps NOW() is the no-backdating rule for closing an open assignment:
-- an immediate close is taken at the application's decision instant, which is never
-- earlier than the transaction start.
CREATE FUNCTION core.trg_DriverAssignments_HistoryImmutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_WorkforceOp TEXT := current_setting('flussra.workforce_op', TRUE);
    v_Clock TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF v_WorkforceOp IN ('transfer_cancel', 'terminate') AND OLD.EffectiveFromAt > v_Clock THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'A DriverAssignment can be deleted only when a transfer cancel or the termination that disposes of the transfer removes a never-effective destination';
    END IF;

    IF NEW.EffectiveFromAt IS NOT DISTINCT FROM OLD.EffectiveFromAt
       AND NEW.EffectiveToAt IS NOT DISTINCT FROM OLD.EffectiveToAt
       AND NEW.EndReason IS NOT DISTINCT FROM OLD.EndReason
       AND NEW.TransferredFromAssignmentID IS NOT DISTINCT FROM OLD.TransferredFromAssignmentID
       AND NEW.TransferredToAssignmentID IS NOT DISTINCT FROM OLD.TransferredToAssignmentID THEN
        RETURN NEW;
    END IF;

    IF NEW.TransferredFromAssignmentID IS DISTINCT FROM OLD.TransferredFromAssignmentID THEN
        RAISE EXCEPTION 'DriverAssignment origin lineage is immutable';
    END IF;

    -- Termination is authoritative: a terminated Employee's assignment end is never
    -- cleared, so no cancel or reschedule can reopen authority after termination.
    IF ((OLD.EffectiveToAt IS NOT NULL AND NEW.EffectiveToAt IS NULL)
        OR (OLD.EndReason IS NOT NULL AND NEW.EndReason IS NULL))
       AND EXISTS (
           SELECT 1 FROM core.Employees e
           WHERE e.EmployeeID = OLD.EmployeeID AND e.EmploymentStatus = 'Terminated'
       ) THEN
        RAISE EXCEPTION 'A DriverAssignment of a terminated Employee cannot be reopened';
    END IF;

    -- Termination may cap a transferred assignment at the Branch-local end of
    -- the termination date and mark it Terminated. When it disposes of the
    -- never-effective destination, the transfer link is cleared in the same update.
    IF v_WorkforceOp = 'terminate'
       AND OLD.EndReason = 'Transferred'
       AND NEW.EndReason = 'Terminated'
       AND NEW.EffectiveFromAt IS NOT DISTINCT FROM OLD.EffectiveFromAt
       AND NEW.EffectiveToAt IS NOT NULL
       AND EXISTS (
           SELECT 1
           FROM core.Employees e
           JOIN core.Branches b
             ON b.CompanyID = OLD.CompanyID AND b.BranchID = OLD.BranchID
           WHERE e.EmployeeID = OLD.EmployeeID
             AND e.EmploymentStatus = 'Terminated'
             AND e.TerminationDate IS NOT NULL
             AND NEW.EffectiveToAt = GREATEST(
                 OLD.EffectiveFromAt,
                 LEAST(OLD.EffectiveToAt, core.fn_LocalDateBoundary(
                     b.TimeZoneId, e.TerminationDate + 1
                 ))
             )
       )
       AND (NEW.TransferredToAssignmentID IS NOT DISTINCT FROM OLD.TransferredToAssignmentID
            OR NEW.TransferredToAssignmentID IS NULL) THEN
        RETURN NEW;
    END IF;

    IF NEW.EffectiveFromAt IS DISTINCT FROM OLD.EffectiveFromAt
       AND NOT (OLD.EffectiveFromAt > v_Clock
                AND NEW.EffectiveFromAt > v_Clock
                AND v_WorkforceOp = 'transfer_reschedule') THEN
        RAISE EXCEPTION 'DriverAssignment start changes only for a not-yet-started assignment under transfer reschedule';
    END IF;

    IF NEW.EffectiveToAt IS DISTINCT FROM OLD.EffectiveToAt
       OR NEW.EndReason IS DISTINCT FROM OLD.EndReason
       OR NEW.TransferredToAssignmentID IS DISTINCT FROM OLD.TransferredToAssignmentID THEN
        IF NEW.TransferredToAssignmentID IS DISTINCT FROM OLD.TransferredToAssignmentID
           AND NEW.EffectiveToAt IS NOT DISTINCT FROM OLD.EffectiveToAt THEN
            RAISE EXCEPTION 'DriverAssignment transfer link changes only together with the assignment end';
        END IF;
        IF OLD.EffectiveToAt IS NULL THEN
            IF NEW.EffectiveToAt IS NULL THEN
                RAISE EXCEPTION 'An open DriverAssignment changes its end reason or transfer link only by closing';
            END IF;
            -- Transaction start on purpose: an immediate close is taken at the
            -- application's clock-based decision instant, which is >= NOW().
            IF NEW.EffectiveToAt < NOW() AND v_WorkforceOp IS DISTINCT FROM 'terminate' THEN
                RAISE EXCEPTION 'A DriverAssignment is closed in the past only by termination';
            END IF;
        ELSIF OLD.EffectiveToAt <= v_Clock THEN
            RAISE EXCEPTION 'Historical DriverAssignment window, end reason, and lineage are immutable';
        ELSIF NOT (v_WorkforceOp = 'terminate'
                   OR (v_WorkforceOp IN ('transfer_cancel', 'transfer_reschedule')
                       AND (NEW.EffectiveToAt IS NULL OR NEW.EffectiveToAt > v_Clock))) THEN
            RAISE EXCEPTION 'A future DriverAssignment end changes only through transfer cancel, reschedule or termination';
        END IF;
    END IF;

    RETURN NEW;
END
$$;

CREATE TRIGGER trg_DriverAssignments_HistoryImmutable
BEFORE UPDATE OF EffectiveFromAt, EffectiveToAt, EndReason,
                 TransferredFromAssignmentID, TransferredToAssignmentID
   OR DELETE ON core.DriverAssignments
FOR EACH ROW EXECUTE FUNCTION core.trg_DriverAssignments_HistoryImmutable();

CREATE FUNCTION core.fn_EffectiveDriverAssignment(
    p_CompanyID INTEGER,
    p_DriverID INTEGER,
    p_At TIMESTAMPTZ DEFAULT NOW()
)
RETURNS INTEGER
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_DriverAssignmentID INTEGER;
    v_Count INTEGER;
BEGIN
    IF p_At IS NULL THEN
        RAISE EXCEPTION 'DRIVER_ASSIGNMENT_INSTANT_REQUIRED: instant must not be null';
    END IF;
    SELECT count(*), min(a.DriverAssignmentID)
      INTO v_Count, v_DriverAssignmentID
      FROM core.DriverAssignments a
     WHERE a.CompanyID = p_CompanyID
       AND a.DriverID = p_DriverID
       AND core.fn_DriverEffectiveRange(a.EffectiveFromAt, a.EffectiveToAt) @> p_At;
    IF v_Count > 1 THEN
        RAISE EXCEPTION 'Multiple effective DriverAssignments for Driver % in Company % at %',
            p_DriverID, p_CompanyID, p_At
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN v_DriverAssignmentID;
END
$$;

CREATE FUNCTION core.fn_DriverAssignmentCoversWorkDate(
    p_CompanyID INTEGER,
    p_DriverAssignmentID INTEGER,
    p_BranchID INTEGER,
    p_WorkDate DATE
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM core.DriverAssignments a
        CROSS JOIN LATERAL (
            SELECT core.fn_BranchWorkDateRange(p_CompanyID, p_BranchID, p_WorkDate, p_WorkDate) AS WorkWindow
        ) w
        WHERE a.CompanyID = p_CompanyID
          AND a.DriverAssignmentID = p_DriverAssignmentID
          AND a.BranchID = p_BranchID
          AND core.fn_DriverEffectiveRange(a.EffectiveFromAt, a.EffectiveToAt) && w.WorkWindow
    )
$$;

-- A stored Scheduled transfer reads as Completed once its EffectiveAt is reached;
-- the assignment windows already switch currentness at that instant.
CREATE FUNCTION core.fn_DriverTransferStatus(
    p_Status TEXT,
    p_EffectiveAt TIMESTAMPTZ,
    p_At TIMESTAMPTZ DEFAULT NOW()
)
RETURNS TEXT
LANGUAGE SQL
STABLE
AS $$
    SELECT CASE
               WHEN p_Status = 'Scheduled' AND p_EffectiveAt IS NOT NULL AND p_EffectiveAt <= p_At
                   THEN 'Completed'
               ELSE p_Status
           END
$$;

-- ---------------------------------------------------------------------------
-- 5. Transfer requests: permanent Driver subject, source and destination
--    assignments, and the Scheduled lifecycle.
-- ---------------------------------------------------------------------------

ALTER TABLE core.DriverTransferRequests
    RENAME COLUMN NewDriverID TO DestinationDriverAssignmentID;

ALTER TABLE core.DriverTransferRequests
    ADD COLUMN SourceDriverAssignmentID INTEGER NOT NULL,
    ADD COLUMN ScheduledAtUtc TIMESTAMPTZ,
    ADD COLUMN ScheduledByUserID INTEGER;

ALTER TABLE core.DriverTransferRequests
    ADD CONSTRAINT fk_DTR_Driver FOREIGN KEY (DriverID, CompanyID)
        REFERENCES core.Drivers(DriverID, CompanyID),
    ADD CONSTRAINT fk_DTR_SourceAssignment
        FOREIGN KEY (SourceDriverAssignmentID, CompanyID, SourceBranchID)
        REFERENCES core.DriverAssignments(DriverAssignmentID, CompanyID, BranchID),
    ADD CONSTRAINT fk_DTR_SourceAssignmentDriver
        FOREIGN KEY (SourceDriverAssignmentID, DriverID)
        REFERENCES core.DriverAssignments(DriverAssignmentID, DriverID),
    ADD CONSTRAINT fk_DTR_DestinationAssignment
        FOREIGN KEY (DestinationDriverAssignmentID, CompanyID, TargetBranchID)
        REFERENCES core.DriverAssignments(DriverAssignmentID, CompanyID, BranchID),
    ADD CONSTRAINT fk_DTR_DestinationAssignmentDriver
        FOREIGN KEY (DestinationDriverAssignmentID, DriverID)
        REFERENCES core.DriverAssignments(DriverAssignmentID, DriverID),
    ADD CONSTRAINT fk_DTR_ScheduledBy FOREIGN KEY (ScheduledByUserID) REFERENCES sec.Users(UserID),
    ADD CONSTRAINT ck_DTR_Status
        CHECK (Status IN ('PendingSourceApproval', 'PendingTargetApproval', 'Returned', 'Rejected',
                          'Approved', 'Scheduled', 'Completed', 'Cancelled')),
    ADD CONSTRAINT ck_DTR_CompletedScheduledEffectiveAt
        CHECK (Status NOT IN ('Completed', 'Scheduled') OR EffectiveAt IS NOT NULL),
    ADD CONSTRAINT ck_DTR_CompletedScheduledDestination
        CHECK (Status NOT IN ('Completed', 'Scheduled') OR DestinationDriverAssignmentID IS NOT NULL),
    ADD CONSTRAINT ck_DTR_ScheduledAudit
        CHECK (Status <> 'Scheduled' OR ScheduledAtUtc IS NOT NULL),
    ADD CONSTRAINT ck_DTR_CancelledNoDestination
        CHECK (Status <> 'Cancelled' OR DestinationDriverAssignmentID IS NULL);

CREATE INDEX ix_DTR_SourceAssignment ON core.DriverTransferRequests (SourceDriverAssignmentID);
CREATE INDEX ix_DTR_DestinationAssignment ON core.DriverTransferRequests (DestinationDriverAssignmentID)
    WHERE DestinationDriverAssignmentID IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 6. Branch payroll and work provenance is keyed by DriverAssignmentID.
-- ---------------------------------------------------------------------------

-- 6a. Column renames.
ALTER TABLE payroll.PayrollDraftLines RENAME COLUMN DriverID TO DriverAssignmentID;
ALTER TABLE payroll.PayrollPeriodDriverDayEntryState RENAME COLUMN DriverID TO DriverAssignmentID;
ALTER TABLE payroll.PayrollPeriodDriverEligibility RENAME COLUMN DriverID TO DriverAssignmentID;
ALTER TABLE payroll.PayrollPeriodDriverEligibility RENAME COLUMN DriverCodeSnapshot TO DriverNumberSnapshot;
ALTER TABLE payroll.PayrollPeriodDriverEligibility
    RENAME COLUMN TransferredFromDriverIDSnapshot TO TransferredFromAssignmentIDSnapshot;
ALTER TABLE payroll.PayrollPeriodDriverEligibility
    RENAME COLUMN TransferredToDriverIDSnapshot TO TransferredToAssignmentIDSnapshot;
ALTER TABLE payroll.PayrollBonusEvents RENAME COLUMN DriverID TO DriverAssignmentID;
ALTER TABLE payroll.PayrollFinalLines RENAME COLUMN DriverID TO DriverAssignmentID;
ALTER TABLE payroll.PayrollCalculationDriverTotals RENAME COLUMN DriverID TO DriverAssignmentID;
ALTER TABLE payroll.PayrollCalculationDriverTotals RENAME COLUMN DriverCodeSnapshot TO DriverNumberSnapshot;
ALTER TABLE payroll.PayrollCalculationSnapshotBonusEvents RENAME COLUMN DriverID TO DriverAssignmentID;
ALTER TABLE payroll.PayrollCalculationSnapshotStatusEntries RENAME COLUMN DriverID TO DriverAssignmentID;
ALTER TABLE payroll.PayrollCalculationSnapshotUsedRateDefinitions RENAME COLUMN DriverID TO DriverAssignmentID;
ALTER TABLE payroll.PayrollPeriodAuditEvidenceEvents RENAME COLUMN DriverID TO DriverAssignmentID;
ALTER TABLE payroll.DriverRates RENAME COLUMN DriverID TO DriverAssignmentID;
ALTER TABLE payroll.DriverRateAssignments RENAME COLUMN DriverID TO DriverAssignmentID;
ALTER TABLE payroll.DriverPayRules RENAME COLUMN DriverID TO DriverAssignmentID;

-- 6b. NOT NULL constraint names follow <table>_<column>_not_null.
DO $rename_assignment_column_not_null$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT c.conrelid::regclass::TEXT AS table_name, rel.relname, c.conname, a.attname
        FROM pg_constraint c
        JOIN pg_class rel ON rel.oid = c.conrelid
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
        WHERE c.contype = 'n'
          AND rel.relnamespace = 'payroll'::regnamespace
          AND a.attname = 'driverassignmentid'
    LOOP
        IF r.conname <> format('%s_%s_not_null', r.relname, r.attname) THEN
            EXECUTE format(
                'ALTER TABLE %s RENAME CONSTRAINT %I TO %I',
                r.table_name, r.conname, format('%s_%s_not_null', r.relname, r.attname));
        END IF;
    END LOOP;
END
$rename_assignment_column_not_null$;

-- 6c. Single-column FKs made redundant by a composite FK are dropped; composite
--     FKs are renamed and validated (these tables are empty).
ALTER TABLE payroll.PayrollDraftLines
    DROP CONSTRAINT fk_DraftLines_Driver;
ALTER TABLE payroll.PayrollDraftLines
    RENAME CONSTRAINT fk_DraftLines_Driver_Company_Branch TO fk_DraftLines_DriverAssignment_Company_Branch;
ALTER TABLE payroll.PayrollDraftLines
    VALIDATE CONSTRAINT fk_DraftLines_DriverAssignment_Company_Branch;
ALTER INDEX payroll.ix_draftlines_period_driver_workdate RENAME TO ix_DraftLines_Period_Assignment_WorkDate;
ALTER INDEX payroll.ix_payrolldraftlines_period_driver RENAME TO ix_PayrollDraftLines_Period_Assignment;

ALTER TABLE payroll.PayrollFinalLines
    DROP CONSTRAINT fk_FinalLines_Driver;
ALTER TABLE payroll.PayrollFinalLines
    RENAME CONSTRAINT fk_FinalLines_Driver_Company_Branch TO fk_FinalLines_DriverAssignment_Company_Branch;
ALTER TABLE payroll.PayrollFinalLines
    VALIDATE CONSTRAINT fk_FinalLines_DriverAssignment_Company_Branch;
ALTER INDEX payroll.ix_payrollfinallines_period_driver RENAME TO ix_PayrollFinalLines_Period_Assignment;

ALTER TABLE payroll.PayrollBonusEvents
    RENAME CONSTRAINT fk_PayrollBonusEvents_Driver TO fk_PayrollBonusEvents_DriverAssignment;
ALTER INDEX payroll.ix_payrollbonusevents_driver_status RENAME TO ix_PayrollBonusEvents_Assignment_Status;
ALTER INDEX payroll.ix_payrollbonusevents_period_driver RENAME TO ix_PayrollBonusEvents_Period_Assignment;

ALTER TABLE payroll.PayrollPeriodDriverDayEntryState
    RENAME CONSTRAINT uq_ppdes_period_driver_date TO uq_PPDES_Period_Assignment_Date;
ALTER INDEX payroll.ix_entrystate_period_driver_workdate RENAME TO ix_EntryState_Period_Assignment_WorkDate;
ALTER INDEX payroll.ix_ppdes_period_driver RENAME TO ix_PPDES_Period_Assignment;

ALTER TABLE payroll.PayrollPeriodDriverEligibility
    RENAME CONSTRAINT fk_PPDE_Driver TO fk_PPDE_DriverAssignment;
ALTER TABLE payroll.PayrollPeriodDriverEligibility
    RENAME CONSTRAINT uq_ppde_period_driver TO uq_PPDE_Period_Assignment;
ALTER INDEX payroll.ix_ppde_driver_company RENAME TO ix_PPDE_Assignment_Company;
ALTER INDEX payroll.ix_ppde_period_driver RENAME TO ix_PPDE_Period_Assignment;

ALTER TABLE payroll.PayrollCalculationDriverTotals
    RENAME CONSTRAINT fk_PayrollCalculationDriverTotals_Driver_Company_Branch
        TO fk_PayrollCalculationDriverTotals_Assignment_Scope;
ALTER TABLE payroll.PayrollCalculationDriverTotals
    RENAME CONSTRAINT uq_PayrollCalculationDriverTotals_Snapshot_Driver
        TO uq_PayrollCalculationDriverTotals_Snapshot_Assignment;

ALTER TABLE payroll.PayrollCalculationSnapshotBonusEvents
    RENAME CONSTRAINT fk_PayrollCalculationSnapshotBonusEvents_Driver_Company_Branch
        TO fk_PayrollCalculationSnapshotBonusEvents_Assignment_Scope;
ALTER INDEX payroll.ix_payrollcalculationsnapshotbonusevents_snapshot_driver
    RENAME TO ix_PayrollCalculationSnapshotBonusEvents_Snapshot_Assignment;

ALTER TABLE payroll.PayrollCalculationSnapshotStatusEntries
    RENAME CONSTRAINT fk_payrollcalculationsnapshotstatusentries_driver_company_branc
        TO fk_PayrollCalculationSnapshotStatusEntries_Assignment_Scope;
ALTER TABLE payroll.PayrollCalculationSnapshotStatusEntries
    RENAME CONSTRAINT uq_payrollcalculationsnapshotstatusentries_snapshot_driver_date
        TO uq_PayrollCalculationSnapshotStatusEntries_Snapshot_Assign_Date;

ALTER TABLE payroll.PayrollCalculationSnapshotUsedRateDefinitions
    RENAME CONSTRAINT fk_PayrollCalculationSnapshotUsedRateDefinitions_DriverScope
        TO fk_PayrollCalculationSnapshotUsedRateDefinitions_AssignScope;

ALTER TABLE payroll.DriverRates
    DROP CONSTRAINT fk_DriverRates_Driver;
ALTER TABLE payroll.DriverRates
    RENAME CONSTRAINT fk_DriverRates_Driver_Company_Branch TO fk_DriverRates_DriverAssignment_Company_Branch;
ALTER TABLE payroll.DriverRates
    VALIDATE CONSTRAINT fk_DriverRates_DriverAssignment_Company_Branch;
ALTER INDEX payroll.ix_driverrates_driver_type_dates RENAME TO ix_DriverRates_Assignment_Type_Dates;
ALTER INDEX payroll.ux_driverrates_driver_type_approved RENAME TO ux_DriverRates_Assignment_Type_Approved;

ALTER TABLE payroll.DriverRateAssignments
    RENAME CONSTRAINT fk_DriverRateAssignments_Driver_Company_Branch
        TO fk_DriverRateAssignments_DriverAssignment_Company_Branch;
ALTER INDEX payroll.ix_driverrateassignments_company_branch_driver
    RENAME TO ix_DriverRateAssignments_Company_Branch_Assignment;

ALTER TABLE payroll.DriverPayRules
    DROP CONSTRAINT fk_DriverPayRules_Driver;
ALTER TABLE payroll.DriverPayRules
    RENAME CONSTRAINT fk_DriverPayRules_Driver_Company_Branch TO fk_DriverPayRules_DriverAssignment_Company_Branch;
ALTER TABLE payroll.DriverPayRules
    VALIDATE CONSTRAINT fk_DriverPayRules_DriverAssignment_Company_Branch;
ALTER INDEX payroll.ix_driverpayrules_driver_type_dates RENAME TO ix_DriverPayRules_Assignment_Type_Dates;
ALTER INDEX payroll.ux_driverpayrules_driver_type_openended RENAME TO ux_DriverPayRules_Assignment_Type_OpenEnded;

-- 6d. Eligibility snapshots: the Driver's status plus why the assignment ended.
ALTER TABLE payroll.PayrollPeriodDriverEligibility
    ADD COLUMN EndReasonSnapshot VARCHAR(20),
    ADD CONSTRAINT ck_PPDE_DriverStatusSnapshot
        CHECK (DriverStatusSnapshot IS NULL OR DriverStatusSnapshot IN ('Active', 'Inactive', 'OnLeave')),
    ADD CONSTRAINT ck_PPDE_EndReasonSnapshot
        CHECK (EndReasonSnapshot IS NULL OR EndReasonSnapshot IN ('Transferred', 'Terminated'));

-- 6e. Evidence and final tables also carry the permanent Driver, bound to the
--     assignment so the two cannot disagree.
ALTER TABLE payroll.PayrollFinalLines
    ADD COLUMN DriverID INTEGER NOT NULL,
    ADD CONSTRAINT fk_FinalLines_Assignment_Driver FOREIGN KEY (DriverAssignmentID, DriverID)
        REFERENCES core.DriverAssignments(DriverAssignmentID, DriverID);
ALTER TABLE payroll.PayrollCalculationDriverTotals
    ADD COLUMN DriverID INTEGER NOT NULL,
    ADD CONSTRAINT fk_PayrollCalculationDriverTotals_Assignment_Driver FOREIGN KEY (DriverAssignmentID, DriverID)
        REFERENCES core.DriverAssignments(DriverAssignmentID, DriverID);
ALTER TABLE payroll.PayrollCalculationSnapshotBonusEvents
    ADD COLUMN DriverID INTEGER NOT NULL,
    ADD CONSTRAINT fk_PayrollCalculationSnapshotBonusEvents_Assignment_Driver FOREIGN KEY (DriverAssignmentID, DriverID)
        REFERENCES core.DriverAssignments(DriverAssignmentID, DriverID);
ALTER TABLE payroll.PayrollCalculationSnapshotStatusEntries
    ADD COLUMN DriverID INTEGER NOT NULL,
    ADD CONSTRAINT fk_PayrollCalculationSnapshotStatusEntries_Assignment_Driver FOREIGN KEY (DriverAssignmentID, DriverID)
        REFERENCES core.DriverAssignments(DriverAssignmentID, DriverID);
ALTER TABLE payroll.PayrollCalculationSnapshotUsedRateDefinitions
    ADD COLUMN DriverID INTEGER NOT NULL,
    ADD CONSTRAINT fk_PayrollCalculationSnapshotUsedRateDefinitions_AssignDriver FOREIGN KEY (DriverAssignmentID, DriverID)
        REFERENCES core.DriverAssignments(DriverAssignmentID, DriverID);
-- Audit evidence events are not always about a Driver; when they are, both ids
-- are present and consistent.
ALTER TABLE payroll.PayrollPeriodAuditEvidenceEvents
    ADD COLUMN DriverID INTEGER,
    ADD CONSTRAINT ck_PayrollPeriodAuditEvidenceEvents_DriverPair
        CHECK ((DriverAssignmentID IS NULL) = (DriverID IS NULL)),
    ADD CONSTRAINT fk_PayrollPeriodAuditEvidenceEvents_Assignment_Driver FOREIGN KEY (DriverAssignmentID, DriverID)
        REFERENCES core.DriverAssignments(DriverAssignmentID, DriverID);

-- ---------------------------------------------------------------------------
-- 7. Functions and triggers that referenced the profile meaning of DriverID.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION payroll.fn_guard_driverrate_used_mutation()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF EXISTS (
            SELECT 1 FROM payroll.payrollfinallines
            WHERE  driverrateid = OLD.driverrateid
            LIMIT  1
        ) THEN
            RAISE EXCEPTION
                'driverrate_mutation_guard: DriverRate has been used in finalized '
                'payroll and cannot be deleted. (driverrateid=%)',
                OLD.driverrateid
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF (
            NEW.amount        IS DISTINCT FROM OLD.amount        OR
            NEW.driverassignmentid      IS DISTINCT FROM OLD.driverassignmentid      OR
            NEW.ratetypeid    IS DISTINCT FROM OLD.ratetypeid    OR
            NEW.companyid     IS DISTINCT FROM OLD.companyid     OR
            NEW.branchid      IS DISTINCT FROM OLD.branchid      OR
            NEW.effectivefrom IS DISTINCT FROM OLD.effectivefrom OR
            NEW.blocksize     IS DISTINCT FROM OLD.blocksize     OR
            NEW.roundingrule  IS DISTINCT FROM OLD.roundingrule
        ) THEN
            IF EXISTS (
                SELECT 1 FROM payroll.payrollfinallines
                WHERE  driverrateid = OLD.driverrateid
                LIMIT  1
            ) THEN
                RAISE EXCEPTION
                    'driverrate_mutation_guard: DriverRate has been used in finalized '
                    'payroll. Critical fields (Amount, DriverAssignmentID, RateTypeID, CompanyID, '
                    'BranchID, EffectiveFrom, BlockSize, RoundingRule) cannot be changed. '
                    '(driverrateid=%)',
                    OLD.driverrateid
                    USING ERRCODE = 'restrict_violation';
            END IF;
        END IF;
        RETURN NEW;
    END IF;

    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION payroll.fn_guard_used_rate_definition_scope()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
    v_company_id INTEGER;
    v_branch_id INTEGER;
    v_period_id INTEGER;
    v_assignment_id INTEGER;
    v_rate_type_id INTEGER;
BEGIN
    SELECT companyid, branchid, payrollperiodid
    INTO v_company_id, v_branch_id, v_period_id
    FROM payroll.payrollcalculationsnapshots
    WHERE payrollcalculationsnapshotid = NEW.payrollcalculationsnapshotid;

    IF v_company_id IS DISTINCT FROM NEW.companyid
       OR v_branch_id IS DISTINCT FROM NEW.branchid
       OR v_period_id IS DISTINCT FROM NEW.payrollperiodid THEN
        RAISE EXCEPTION
            'used_rate_definition_scope: snapshot scope does not match definition scope'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.evidencekind = 'DriverRate' THEN
        SELECT companyid, branchid, driverassignmentid, ratetypeid
        INTO v_company_id, v_branch_id, v_assignment_id, v_rate_type_id
        FROM payroll.driverrates
        WHERE driverrateid = NEW.driverrateid;

        IF v_company_id IS DISTINCT FROM NEW.companyid
           OR v_branch_id IS DISTINCT FROM NEW.branchid
           OR v_assignment_id IS DISTINCT FROM NEW.driverassignmentid
           OR v_rate_type_id IS DISTINCT FROM NEW.ratetypeid THEN
            RAISE EXCEPTION
                'used_rate_definition_scope: driver rate does not match definition scope'
                USING ERRCODE = 'check_violation';
        END IF;
    ELSE
        SELECT companyid, branchid, driverassignmentid
        INTO v_company_id, v_branch_id, v_assignment_id
        FROM payroll.driverpayrules
        WHERE driverpayruleid = NEW.driverpayruleid;

        IF v_company_id IS DISTINCT FROM NEW.companyid
           OR v_branch_id IS DISTINCT FROM NEW.branchid
           OR v_assignment_id IS DISTINCT FROM NEW.driverassignmentid THEN
            RAISE EXCEPTION
                'used_rate_definition_scope: driver pay rule does not match definition scope'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION payroll.trg_fn_ppde_ownership()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
    period_company  INTEGER;
    period_branch   INTEGER;
    assignment_company  INTEGER;
    assignment_branch   INTEGER;
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

    -- Driver assignment must belong to this company/branch
    SELECT companyid, branchid INTO assignment_company, assignment_branch
    FROM core.driverassignments
    WHERE driverassignmentid = NEW.driverassignmentid;

    IF assignment_company IS DISTINCT FROM NEW.companyid
    OR assignment_branch  IS DISTINCT FROM NEW.branchid THEN
        RAISE EXCEPTION
            'PayrollPeriodDriverEligibility: driver assignment % belongs to company/branch %/% but row is for %/%',
            NEW.driverassignmentid, assignment_company, assignment_branch,
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
$function$;

CREATE OR REPLACE FUNCTION payroll.trg_driverrateassignments_beforeupdate()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
    v_CurrencyCode VARCHAR(3);
BEGIN
    IF NEW.CompanyID IS DISTINCT FROM OLD.CompanyID
       OR NEW.BranchID IS DISTINCT FROM OLD.BranchID
       OR NEW.DriverAssignmentID IS DISTINCT FROM OLD.DriverAssignmentID
       OR NEW.RateDefinitionID IS DISTINCT FROM OLD.RateDefinitionID THEN
        RAISE EXCEPTION 'RATE_ASSIGNMENT_IDENTITY_IMMUTABLE'
            USING ERRCODE = 'check_violation';
    END IF;

    PERFORM payroll.fn_LockRateDefinitionStructure(OLD.RateDefinitionID);

    IF OLD.Status = 'Pending' THEN
        IF NEW.Status = 'Pending' THEN
            RETURN NEW;
        END IF;
        IF NEW.Status <> 'Approved' THEN
            RAISE EXCEPTION 'RATE_ASSIGNMENT_INVALID_TRANSITION'
                USING ERRCODE = 'check_violation',
                      DETAIL = format('%s -> %s', OLD.Status, NEW.Status);
        END IF;

        -- Approval: Company currency gate, complete structure, complete value set.
        PERFORM 1 FROM core.Companies c
         WHERE c.CompanyID = NEW.CompanyID
           FOR SHARE;
        SELECT c.CurrencyCode INTO v_CurrencyCode
          FROM core.Companies c
         WHERE c.CompanyID = NEW.CompanyID;
        IF v_CurrencyCode IS NULL THEN
            RAISE EXCEPTION 'COMPANY_CURRENCY_REQUIRED'
                USING ERRCODE = 'check_violation';
        END IF;

        PERFORM payroll.fn_AssertRateStructureComplete(NEW.RateDefinitionID);

        IF EXISTS (
            SELECT 1
              FROM payroll.RateComponentDefinitions c
              LEFT JOIN payroll.DriverRateValues v
                ON v.RateComponentDefinitionID = c.RateComponentDefinitionID
               AND v.DriverRateAssignmentID = OLD.DriverRateAssignmentID
             WHERE c.RateDefinitionID = NEW.RateDefinitionID
               AND v.Amount IS NULL
        ) THEN
            RAISE EXCEPTION 'RATE_ASSIGNMENT_INCOMPLETE'
                USING ERRCODE = 'check_violation',
                      DETAIL = format('DriverRateAssignmentID=%s', OLD.DriverRateAssignmentID);
        END IF;

        UPDATE payroll.RateDefinitions
           SET StructureLockedAtUtc = NOW()
         WHERE RateDefinitionID = NEW.RateDefinitionID
           AND StructureLockedAtUtc IS NULL;
        RETURN NEW;
    END IF;

    IF NEW.EffectiveFrom IS DISTINCT FROM OLD.EffectiveFrom
       OR NEW.ApprovedByUserID IS DISTINCT FROM OLD.ApprovedByUserID
       OR NEW.ApprovedAtUtc IS DISTINCT FROM OLD.ApprovedAtUtc
       OR (NEW.EffectiveTo IS DISTINCT FROM OLD.EffectiveTo
           AND NOT (OLD.Status = 'Approved' AND NEW.Status = 'Superseded'))
       OR (OLD.Status = 'Voided'
           AND (NEW.Status <> 'Voided'
                OR NEW.VoidedByUserID IS DISTINCT FROM OLD.VoidedByUserID
                OR NEW.VoidedAtUtc IS DISTINCT FROM OLD.VoidedAtUtc
                OR NEW.VoidReason IS DISTINCT FROM OLD.VoidReason)) THEN
        RAISE EXCEPTION 'RATE_ASSIGNMENT_AUTHORITATIVE_IMMUTABLE'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NOT (
        (OLD.Status = 'Approved' AND NEW.Status IN ('Approved', 'Superseded', 'Voided'))
        OR (OLD.Status = 'Superseded' AND NEW.Status IN ('Superseded', 'Voided'))
        OR (OLD.Status = 'Voided' AND NEW.Status = 'Voided')
    ) THEN
        RAISE EXCEPTION 'RATE_ASSIGNMENT_INVALID_TRANSITION'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('%s -> %s', OLD.Status, NEW.Status);
    END IF;
    RETURN NEW;
END
$function$;

CREATE OR REPLACE FUNCTION payroll.trg_driverrateassignments_beforedelete()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
    v_Actor TEXT := nullif(current_setting('flussra.actor_user_id', TRUE), '');
BEGIN
    PERFORM payroll.fn_LockRateDefinitionStructure(OLD.RateDefinitionID);

    IF OLD.Status <> 'Pending' THEN
        RAISE EXCEPTION 'RATE_ASSIGNMENT_DELETE_BLOCKED'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('Status=%s', OLD.Status);
    END IF;

    INSERT INTO audit.AuditLog
        (CompanyID, BranchID, ActorUserID, ActionCode, EntitySchema, EntityName,
         EntityID, OldValueJson, SourceType)
    SELECT OLD.CompanyID, OLD.BranchID, v_Actor::INTEGER,
           'DRIVER_RATE_ASSIGNMENT_DISCARDED', 'payroll', 'DriverRateAssignments',
           OLD.DriverRateAssignmentID::TEXT,
           jsonb_build_object(
               'DriverRateAssignmentID', OLD.DriverRateAssignmentID,
               'DriverAssignmentID', OLD.DriverAssignmentID,
               'RateDefinitionID', OLD.RateDefinitionID,
               'EffectiveFrom', OLD.EffectiveFrom,
               'EffectiveTo', OLD.EffectiveTo,
               'Notes', OLD.Notes,
               'Values', COALESCE(
                   (SELECT jsonb_agg(
                               jsonb_build_object(
                                   'RateComponentDefinitionID', v.RateComponentDefinitionID,
                                   'Amount', v.Amount)
                               ORDER BY v.RateComponentDefinitionID)
                      FROM payroll.DriverRateValues v
                     WHERE v.DriverRateAssignmentID = OLD.DriverRateAssignmentID),
                   '[]'::jsonb))::TEXT,
           'Database';
    RETURN OLD;
END
$function$;

-- ---------------------------------------------------------------------------
-- 7b. Compensation of an ENDED DriverAssignment is read-only, decided at the
--     mutation instant.
-- ---------------------------------------------------------------------------
-- An ended assignment (EffectiveToAt IS NOT NULL AND EffectiveToAt <= now) is
-- historical. The application refuses early (require_writable_driver_assignment),
-- but a writer can still BLOCK afterwards (advisory lock, row lock, unique-index
-- wait), and a Scheduled source assignment's future end can pass during that wait.
-- FOR SHARE freezes the EffectiveToAt value, not time. So the database re-decides
-- here: an AFTER ROW trigger runs after the statement has finished every wait of its
-- own, and compares with clock_timestamp() (never NOW(), the transaction start), the
-- same clock as trg_DriverAssignments_HistoryImmutable. Whatever blocked before it,
-- a mutation executed after the boundary is refused.
--
-- Covered: every compensation table keyed to a DriverAssignment, directly
-- (DriverRateAssignments, DriverPayRules, DriverRates) or through its parent
-- (DriverRateValues -> DriverRateAssignments, DriverRateTiers -> DriverRates).
-- Evidence, draft, final and eligibility tables are payroll history, not
-- compensation authoring, and keep their own guards.
--
-- A child row whose parent is already gone (foreign-key cascade of a parent delete)
-- resolves to no assignment and is allowed: the parent's own trigger decided.
-- SQLSTATE FL001 is a custom class unused by PostgreSQL; the application maps it to
-- HTTP 409 DRIVER_ASSIGNMENT_ENDED in one place.
CREATE FUNCTION core.trg_CompensationAssignmentWritable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_Old INTEGER;
    v_New INTEGER;
    v_AssignmentID INTEGER;
    v_EndedAt TIMESTAMPTZ;
BEGIN
    IF TG_TABLE_NAME IN ('driverrateassignments', 'driverpayrules', 'driverrates') THEN
        IF TG_OP IN ('UPDATE', 'DELETE') THEN
            v_Old := OLD.DriverAssignmentID;
        END IF;
        IF TG_OP IN ('INSERT', 'UPDATE') THEN
            v_New := NEW.DriverAssignmentID;
        END IF;
    ELSIF TG_TABLE_NAME = 'driverratevalues' THEN
        IF TG_OP IN ('UPDATE', 'DELETE') THEN
            SELECT a.DriverAssignmentID INTO v_Old
            FROM payroll.DriverRateAssignments a
            WHERE a.DriverRateAssignmentID = OLD.DriverRateAssignmentID;
        END IF;
        IF TG_OP IN ('INSERT', 'UPDATE') THEN
            SELECT a.DriverAssignmentID INTO v_New
            FROM payroll.DriverRateAssignments a
            WHERE a.DriverRateAssignmentID = NEW.DriverRateAssignmentID;
        END IF;
    ELSIF TG_TABLE_NAME = 'driverratetiers' THEN
        IF TG_OP IN ('UPDATE', 'DELETE') THEN
            SELECT r.DriverAssignmentID INTO v_Old
            FROM payroll.DriverRates r
            WHERE r.DriverRateID = OLD.DriverRateID;
        END IF;
        IF TG_OP IN ('INSERT', 'UPDATE') THEN
            SELECT r.DriverAssignmentID INTO v_New
            FROM payroll.DriverRates r
            WHERE r.DriverRateID = NEW.DriverRateID;
        END IF;
    ELSE
        RAISE EXCEPTION 'trg_CompensationAssignmentWritable is not defined for %.%',
            TG_TABLE_SCHEMA, TG_TABLE_NAME;
    END IF;

    -- A row that moves between assignments is a write to both.
    FOR v_AssignmentID IN
        SELECT DISTINCT t.x FROM (VALUES (v_Old), (v_New)) AS t(x) WHERE t.x IS NOT NULL
    LOOP
        SELECT a.EffectiveToAt INTO v_EndedAt
        FROM core.DriverAssignments a
        WHERE a.DriverAssignmentID = v_AssignmentID;

        IF v_EndedAt IS NOT NULL AND v_EndedAt <= clock_timestamp() THEN
            RAISE EXCEPTION 'DRIVER_ASSIGNMENT_ENDED: compensation of an ended DriverAssignment is read-only'
                USING ERRCODE = 'FL001',
                      DETAIL = format('DriverAssignmentID=%s Table=%s Operation=%s',
                                      v_AssignmentID, TG_TABLE_NAME, TG_OP);
        END IF;
    END LOOP;

    RETURN NULL;
END
$$;

CREATE TRIGGER trg_CompensationAssignmentWritable
AFTER INSERT OR UPDATE OR DELETE ON payroll.DriverRateAssignments
FOR EACH ROW EXECUTE FUNCTION core.trg_CompensationAssignmentWritable();

CREATE TRIGGER trg_CompensationAssignmentWritable
AFTER INSERT OR UPDATE OR DELETE ON payroll.DriverRateValues
FOR EACH ROW EXECUTE FUNCTION core.trg_CompensationAssignmentWritable();

CREATE TRIGGER trg_CompensationAssignmentWritable
AFTER INSERT OR UPDATE OR DELETE ON payroll.DriverPayRules
FOR EACH ROW EXECUTE FUNCTION core.trg_CompensationAssignmentWritable();

CREATE TRIGGER trg_CompensationAssignmentWritable
AFTER INSERT OR UPDATE OR DELETE ON payroll.DriverRates
FOR EACH ROW EXECUTE FUNCTION core.trg_CompensationAssignmentWritable();

CREATE TRIGGER trg_CompensationAssignmentWritable
AFTER INSERT OR UPDATE OR DELETE ON payroll.DriverRateTiers
FOR EACH ROW EXECUTE FUNCTION core.trg_CompensationAssignmentWritable();

-- ---------------------------------------------------------------------------
-- 8. app.vw_PayrollPeriodList against the renamed columns (same output columns).
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW app.vw_PayrollPeriodList AS
SELECT p.CompanyID,
       p.BranchID,
       b.BranchName,
       p.PayrollPeriodID,
       p.ParentPayrollPeriodID,
       p.PeriodCode,
       p.PeriodName,
       p.PeriodType,
       p.StartDate,
       p.EndDate,
       p.Status,
       COALESCE(dl_agg.DraftDrivers, 0::BIGINT) AS DraftDrivers,
       COALESCE(dl_agg.DraftLines, 0::BIGINT) AS DraftLines,
       COALESCE(dl_agg.DraftLinesNeedingAttention, 0::BIGINT) AS DraftLinesNeedingAttention,
       COALESCE(fl_agg.FinalLines, 0::BIGINT) AS FinalLines,
       COALESCE(fl_agg.FinalGross, 0::NUMERIC) AS FinalGross,
       COALESCE(fl_agg.FinalDriverCount, 0::BIGINT) AS FinalDriverCount
FROM payroll.PayrollPeriods p
JOIN core.Branches b ON b.BranchID = p.BranchID
LEFT JOIN (
    SELECT PayrollPeriodID,
           count(DISTINCT DriverAssignmentID) AS DraftDrivers,
           count(DraftLineID) AS DraftLines,
           sum(CASE WHEN Status IN ('NeedsReview', 'Rejected') OR NeedsManagerReview THEN 1 ELSE 0 END)
               AS DraftLinesNeedingAttention
    FROM payroll.PayrollDraftLines
    GROUP BY PayrollPeriodID
) dl_agg ON dl_agg.PayrollPeriodID = p.PayrollPeriodID
LEFT JOIN (
    SELECT PayrollPeriodID,
           count(FinalLineID) AS FinalLines,
           sum(FinalAmount) AS FinalGross,
           count(DISTINCT DriverAssignmentID) AS FinalDriverCount
    FROM payroll.PayrollFinalLines
    GROUP BY PayrollPeriodID
) fl_agg ON fl_agg.PayrollPeriodID = p.PayrollPeriodID;
