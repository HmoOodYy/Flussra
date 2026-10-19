-- 0088: Driver-owned compensation.
-- Historical migrations remain immutable. Ordinary rate schedules (formerly
-- DriverRateAssignments / DriverRateValues) and DriverPayRules stop being keyed to a
-- branch-bound DriverAssignment: they are owned by the permanent Driver, so a permanent
-- transfer continues them without any copy. Min/Max rules record the home pay calendar
-- they were set for. The legacy status rates (DriverRates / DriverRateTiers) stay
-- assignment-owned and keep core.trg_CompensationAssignmentWritable.
-- This pre-production cutover requires a clean rebuild when any rate schedule, schedule
-- value or pay rule exists: assignment windows of one Driver can overlap once merged onto
-- the Driver, and a pay rule has no truthful home pay calendar to backfill.

DO $preflight$
BEGIN
    IF EXISTS (SELECT 1 FROM payroll.DriverRateAssignments)
       OR EXISTS (SELECT 1 FROM payroll.DriverRateValues)
       OR EXISTS (SELECT 1 FROM payroll.DriverPayRules) THEN
        RAISE EXCEPTION
            'DURABLE_COMPENSATION_REBUILD_REQUIRED: 0088 cannot infer Driver-owned rate schedules or captured pay calendars from assignment-owned rows. '
            'Reset and reseed this preproduction database, then apply migrations again.';
    END IF;
END
$preflight$;

-- ---------------------------------------------------------------------------
-- 1. The ended-assignment guard no longer covers the moved tables.
-- ---------------------------------------------------------------------------

DROP TRIGGER trg_CompensationAssignmentWritable ON payroll.DriverRateAssignments;
DROP TRIGGER trg_CompensationAssignmentWritable ON payroll.DriverRateValues;
DROP TRIGGER trg_CompensationAssignmentWritable ON payroll.DriverPayRules;

CREATE OR REPLACE FUNCTION core.trg_CompensationAssignmentWritable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_Old INTEGER;
    v_New INTEGER;
    v_AssignmentID INTEGER;
    v_EndedAt TIMESTAMPTZ;
BEGIN
    IF TG_TABLE_NAME = 'driverrates' THEN
        IF TG_OP IN ('UPDATE', 'DELETE') THEN
            v_Old := OLD.DriverAssignmentID;
        END IF;
        IF TG_OP IN ('INSERT', 'UPDATE') THEN
            v_New := NEW.DriverAssignmentID;
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

-- ---------------------------------------------------------------------------
-- 2. DriverRateAssignments becomes DriverRateSchedules (owned by the Driver).
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.DriverRateAssignments
    DROP CONSTRAINT excl_driverrateassignments_authoritativeoverlap,
    DROP CONSTRAINT fk_driverrateassignments_driverassignment_company_branch;
DROP INDEX payroll.ux_driverrateassignments_approved;
DROP INDEX payroll.ux_driverrateassignments_pending;
DROP INDEX payroll.ix_driverrateassignments_company_branch_assignment;

ALTER TABLE payroll.DriverRateAssignments RENAME TO DriverRateSchedules;
ALTER TABLE payroll.DriverRateSchedules RENAME COLUMN DriverRateAssignmentID TO DriverRateScheduleID;
ALTER SEQUENCE payroll.driverrateassignments_driverrateassignmentid_seq
    RENAME TO driverrateschedules_driverratescheduleid_seq;
ALTER TABLE payroll.DriverRateSchedules RENAME CONSTRAINT driverrateassignments_pkey TO driverrateschedules_pkey;
ALTER TABLE payroll.DriverRateSchedules RENAME CONSTRAINT uq_driverrateassignments_id_ratedefinition TO uq_driverrateschedules_id_ratedefinition;
ALTER TABLE payroll.DriverRateSchedules RENAME CONSTRAINT ck_driverrateassignments_approvalmetadata TO ck_driverrateschedules_approvalmetadata;
ALTER TABLE payroll.DriverRateSchedules RENAME CONSTRAINT ck_driverrateassignments_effectivedates TO ck_driverrateschedules_effectivedates;
ALTER TABLE payroll.DriverRateSchedules RENAME CONSTRAINT ck_driverrateassignments_pendingmetadata TO ck_driverrateschedules_pendingmetadata;
ALTER TABLE payroll.DriverRateSchedules RENAME CONSTRAINT ck_driverrateassignments_status TO ck_driverrateschedules_status;
ALTER TABLE payroll.DriverRateSchedules RENAME CONSTRAINT ck_driverrateassignments_supersededclosed TO ck_driverrateschedules_supersededclosed;
ALTER TABLE payroll.DriverRateSchedules RENAME CONSTRAINT ck_driverrateassignments_voidmetadata TO ck_driverrateschedules_voidmetadata;
ALTER TABLE payroll.DriverRateSchedules RENAME CONSTRAINT fk_driverrateassignments_approver TO fk_driverrateschedules_approver;
ALTER TABLE payroll.DriverRateSchedules RENAME CONSTRAINT fk_driverrateassignments_creator TO fk_driverrateschedules_creator;
ALTER TABLE payroll.DriverRateSchedules RENAME CONSTRAINT fk_driverrateassignments_updater TO fk_driverrateschedules_updater;
ALTER TABLE payroll.DriverRateSchedules RENAME CONSTRAINT fk_driverrateassignments_voider TO fk_driverrateschedules_voider;
ALTER TABLE payroll.DriverRateSchedules RENAME CONSTRAINT fk_driverrateassignments_ratedefinition_company TO fk_driverrateschedules_ratedefinition_company;
ALTER INDEX payroll.ix_driverrateassignments_pendingbydefinition RENAME TO ix_driverrateschedules_pendingbydefinition;

-- Ownership moves from the branch-bound assignment to the permanent Driver.
-- PayDefinitionID is NOT NULL and bound to the RateDefinition, so a status-owned
-- RateDefinition (which has no PayDefinition) cannot have a schedule.
ALTER TABLE payroll.DriverRateSchedules
    DROP COLUMN DriverAssignmentID,
    DROP COLUMN BranchID,
    ADD COLUMN DriverID INTEGER NOT NULL,
    ADD COLUMN PayDefinitionID INTEGER NOT NULL;

ALTER TABLE payroll.DriverRateSchedules
    ADD CONSTRAINT fk_driverrateschedules_driver_company
        FOREIGN KEY (DriverID, CompanyID) REFERENCES core.Drivers(DriverID, CompanyID),
    ADD CONSTRAINT fk_driverrateschedules_ratedefinition_paydefinition
        FOREIGN KEY (RateDefinitionID, PayDefinitionID)
        REFERENCES payroll.RateDefinitions(RateDefinitionID, PayDefinitionID),
    ADD CONSTRAINT excl_driverrateschedules_authoritativeoverlap
        EXCLUDE USING gist (
            DriverID WITH =,
            RateDefinitionID WITH =,
            daterange(EffectiveFrom, EffectiveTo, '[]') WITH &&
        ) WHERE (Status IN ('Approved', 'Superseded'));

CREATE UNIQUE INDEX ux_driverrateschedules_pending
    ON payroll.DriverRateSchedules (DriverID, RateDefinitionID) WHERE Status = 'Pending';
CREATE UNIQUE INDEX ux_driverrateschedules_approved
    ON payroll.DriverRateSchedules (DriverID, RateDefinitionID) WHERE Status = 'Approved';
CREATE INDEX ix_driverrateschedules_company_driver
    ON payroll.DriverRateSchedules (CompanyID, DriverID);

-- ---------------------------------------------------------------------------
-- 3. DriverRateValues becomes DriverRateScheduleValues.
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.DriverRateValues RENAME TO DriverRateScheduleValues;
ALTER TABLE payroll.DriverRateScheduleValues RENAME COLUMN DriverRateValueID TO DriverRateScheduleValueID;
ALTER TABLE payroll.DriverRateScheduleValues RENAME COLUMN DriverRateAssignmentID TO DriverRateScheduleID;
ALTER SEQUENCE payroll.driverratevalues_driverratevalueid_seq
    RENAME TO driverrateschedulevalues_driverrateschedulevalueid_seq;
ALTER TABLE payroll.DriverRateScheduleValues RENAME CONSTRAINT driverratevalues_pkey TO driverrateschedulevalues_pkey;
ALTER TABLE payroll.DriverRateScheduleValues RENAME CONSTRAINT uq_driverratevalues_assignment_component TO uq_driverrateschedulevalues_schedule_component;
ALTER TABLE payroll.DriverRateScheduleValues RENAME CONSTRAINT ck_driverratevalues_amount TO ck_driverrateschedulevalues_amount;
ALTER TABLE payroll.DriverRateScheduleValues RENAME CONSTRAINT fk_driverratevalues_assignment_ratedefinition TO fk_driverrateschedulevalues_schedule_ratedefinition;
ALTER TABLE payroll.DriverRateScheduleValues RENAME CONSTRAINT fk_driverratevalues_component_ratedefinition TO fk_driverrateschedulevalues_component_ratedefinition;
ALTER INDEX payroll.ix_driverratevalues_component RENAME TO ix_driverrateschedulevalues_component;

-- ---------------------------------------------------------------------------
-- 4. DriverPayRules: Driver-owned, with the captured home pay calendar.
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.DriverPayRules
    DROP CONSTRAINT excl_driverpayrules_no_date_overlap,
    DROP CONSTRAINT fk_driverpayrules_driverassignment_company_branch,
    DROP CONSTRAINT fk_driverpayrules_branch;
DROP INDEX payroll.ux_driverpayrules_assignment_type_openended;
DROP INDEX payroll.ix_driverpayrules_assignment_type_dates;
DROP INDEX payroll.ix_driverpayrules_company_branch_status;

-- The calendar is a compatibility key (the period grid only, never the normal-days-off
-- mask), captured server-side from the Payroll Setup of the Branch of the rule's context
-- tenure. It is never a rescaling basis.
ALTER TABLE payroll.DriverPayRules
    DROP COLUMN DriverAssignmentID,
    DROP COLUMN BranchID,
    ADD COLUMN DriverID INTEGER NOT NULL,
    ADD COLUMN CalendarFrequency VARCHAR(20) NOT NULL,
    ADD COLUMN CalendarAnchorStartDate DATE NOT NULL,
    ADD COLUMN CalendarCustomIntervalDays INTEGER,
    ADD COLUMN CalendarSourceBranchID INTEGER NOT NULL,
    ADD COLUMN CalendarSourcePayrollSetupVersionID BIGINT NOT NULL;

-- A composite key lets the pay rule's calendar provenance be company-consistent.
ALTER TABLE payroll.PayrollSetupVersions
    ADD CONSTRAINT uq_payrollsetupversions_id_company UNIQUE (PayrollSetupVersionID, CompanyID);

ALTER TABLE payroll.DriverPayRules
    ADD CONSTRAINT fk_driverpayrules_driver_company
        FOREIGN KEY (DriverID, CompanyID) REFERENCES core.Drivers(DriverID, CompanyID),
    ADD CONSTRAINT fk_driverpayrules_calendarsourcebranch_company
        FOREIGN KEY (CalendarSourceBranchID, CompanyID) REFERENCES core.Branches(BranchID, CompanyID),
    ADD CONSTRAINT fk_driverpayrules_calendarsourceversion_company
        FOREIGN KEY (CalendarSourcePayrollSetupVersionID, CompanyID)
        REFERENCES payroll.PayrollSetupVersions(PayrollSetupVersionID, CompanyID),
    ADD CONSTRAINT ck_driverpayrules_calendarfrequency
        CHECK (CalendarFrequency IN ('Week', 'Biweek', 'Month', 'Custom')),
    ADD CONSTRAINT ck_driverpayrules_calendarinterval
        CHECK ((CalendarFrequency = 'Custom') = (CalendarCustomIntervalDays IS NOT NULL)),
    ADD CONSTRAINT ck_driverpayrules_calendarintervalpositive
        CHECK (CalendarCustomIntervalDays IS NULL OR CalendarCustomIntervalDays > 0),
    ADD CONSTRAINT excl_driverpayrules_no_date_overlap
        EXCLUDE USING gist (
            CompanyID WITH =,
            DriverID WITH =,
            RuleType WITH =,
            daterange(EffectiveFrom, EffectiveTo, '[]') WITH &&
        ) WHERE (Status IN ('Active', 'Ended'));

CREATE UNIQUE INDEX ux_driverpayrules_driver_type_openended
    ON payroll.DriverPayRules (DriverID, RuleType) WHERE Status = 'Active' AND EffectiveTo IS NULL;
CREATE INDEX ix_driverpayrules_driver_type_dates
    ON payroll.DriverPayRules (DriverID, RuleType, EffectiveFrom, EffectiveTo, Status);
CREATE INDEX ix_driverpayrules_company_driver_status
    ON payroll.DriverPayRules (CompanyID, DriverID, Status);

-- ---------------------------------------------------------------------------
-- 5. NOT NULL constraint names follow <table>_<column>_not_null.
-- ---------------------------------------------------------------------------

DO $rename_compensation_not_null$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT c.conname, a.attname, cl.relname, cl.relnamespace::regnamespace::TEXT AS schema_name
        FROM pg_constraint c
        JOIN pg_class cl ON cl.oid = c.conrelid
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
        WHERE c.conrelid IN (
                  'payroll.DriverRateSchedules'::regclass,
                  'payroll.DriverRateScheduleValues'::regclass,
                  'payroll.DriverPayRules'::regclass)
          AND c.contype = 'n'
    LOOP
        IF r.conname <> format('%s_%s_not_null', r.relname, r.attname) THEN
            EXECUTE format(
                'ALTER TABLE %s.%I RENAME CONSTRAINT %I TO %I',
                r.schema_name, r.relname, r.conname,
                format('%s_%s_not_null', r.relname, r.attname));
        END IF;
    END LOOP;
END
$rename_compensation_not_null$;

-- ---------------------------------------------------------------------------
-- 6. Schedule structure guards, re-pointed to DriverRateSchedules.
-- ---------------------------------------------------------------------------

ALTER FUNCTION payroll.trg_driverrateassignments_beforeinsert() RENAME TO trg_driverrateschedules_beforeinsert;
ALTER FUNCTION payroll.trg_driverrateassignments_beforeupdate() RENAME TO trg_driverrateschedules_beforeupdate;
ALTER FUNCTION payroll.trg_driverrateassignments_beforedelete() RENAME TO trg_driverrateschedules_beforedelete;
ALTER FUNCTION payroll.trg_driverratevalues_pendingonly() RENAME TO trg_driverrateschedulevalues_pendingonly;
ALTER TRIGGER trg_driverrateassignments_beforeinsert ON payroll.DriverRateSchedules RENAME TO trg_driverrateschedules_beforeinsert;
ALTER TRIGGER trg_driverrateassignments_beforeupdate ON payroll.DriverRateSchedules RENAME TO trg_driverrateschedules_beforeupdate;
ALTER TRIGGER trg_driverrateassignments_beforedelete ON payroll.DriverRateSchedules RENAME TO trg_driverrateschedules_beforedelete;
ALTER TRIGGER trg_driverratevalues_pendingonly ON payroll.DriverRateScheduleValues RENAME TO trg_driverrateschedulevalues_pendingonly;

-- A schedule starts Pending. The owner Branch check is gone: a status-owned
-- RateDefinition is unrepresentable (PayDefinitionID is required and bound to it).
CREATE OR REPLACE FUNCTION payroll.trg_DriverRateSchedules_BeforeInsert()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM payroll.fn_LockRateDefinitionStructure(NEW.RateDefinitionID);

    IF NEW.Status <> 'Pending' THEN
        RAISE EXCEPTION 'RATE_SCHEDULE_MUST_START_PENDING'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION payroll.trg_DriverRateSchedules_BeforeUpdate()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_CurrencyCode VARCHAR(3);
BEGIN
    IF NEW.CompanyID IS DISTINCT FROM OLD.CompanyID
       OR NEW.DriverID IS DISTINCT FROM OLD.DriverID
       OR NEW.RateDefinitionID IS DISTINCT FROM OLD.RateDefinitionID
       OR NEW.PayDefinitionID IS DISTINCT FROM OLD.PayDefinitionID THEN
        RAISE EXCEPTION 'RATE_SCHEDULE_IDENTITY_IMMUTABLE'
            USING ERRCODE = 'check_violation';
    END IF;

    PERFORM payroll.fn_LockRateDefinitionStructure(OLD.RateDefinitionID);

    IF OLD.Status = 'Pending' THEN
        IF NEW.Status = 'Pending' THEN
            RETURN NEW;
        END IF;
        IF NEW.Status <> 'Approved' THEN
            RAISE EXCEPTION 'RATE_SCHEDULE_INVALID_TRANSITION'
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
              LEFT JOIN payroll.DriverRateScheduleValues v
                ON v.RateComponentDefinitionID = c.RateComponentDefinitionID
               AND v.DriverRateScheduleID = OLD.DriverRateScheduleID
             WHERE c.RateDefinitionID = NEW.RateDefinitionID
               AND v.Amount IS NULL
        ) THEN
            RAISE EXCEPTION 'RATE_SCHEDULE_INCOMPLETE'
                USING ERRCODE = 'check_violation',
                      DETAIL = format('DriverRateScheduleID=%s', OLD.DriverRateScheduleID);
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
        RAISE EXCEPTION 'RATE_SCHEDULE_AUTHORITATIVE_IMMUTABLE'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NOT (
        (OLD.Status = 'Approved' AND NEW.Status IN ('Approved', 'Superseded', 'Voided'))
        OR (OLD.Status = 'Superseded' AND NEW.Status IN ('Superseded', 'Voided'))
        OR (OLD.Status = 'Voided' AND NEW.Status = 'Voided')
    ) THEN
        RAISE EXCEPTION 'RATE_SCHEDULE_INVALID_TRANSITION'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('%s -> %s', OLD.Status, NEW.Status);
    END IF;
    RETURN NEW;
END
$$;

-- Discarding a Pending schedule is audited with its full value set. The audit row has
-- no Branch: the schedule belongs to the Driver, not to a Branch.
CREATE OR REPLACE FUNCTION payroll.trg_DriverRateSchedules_BeforeDelete()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_Actor TEXT := nullif(current_setting('flussra.actor_user_id', TRUE), '');
BEGIN
    PERFORM payroll.fn_LockRateDefinitionStructure(OLD.RateDefinitionID);

    IF OLD.Status <> 'Pending' THEN
        RAISE EXCEPTION 'RATE_SCHEDULE_DELETE_BLOCKED'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('Status=%s', OLD.Status);
    END IF;

    INSERT INTO audit.AuditLog
        (CompanyID, BranchID, ActorUserID, ActionCode, EntitySchema, EntityName,
         EntityID, OldValueJson, SourceType)
    SELECT OLD.CompanyID, NULL, v_Actor::INTEGER,
           'DRIVER_RATE_SCHEDULE_DISCARDED', 'payroll', 'DriverRateSchedules',
           OLD.DriverRateScheduleID::TEXT,
           jsonb_build_object(
               'DriverRateScheduleID', OLD.DriverRateScheduleID,
               'DriverID', OLD.DriverID,
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
                      FROM payroll.DriverRateScheduleValues v
                     WHERE v.DriverRateScheduleID = OLD.DriverRateScheduleID),
                   '[]'::jsonb))::TEXT,
           'Database';
    RETURN OLD;
END
$$;

CREATE OR REPLACE FUNCTION payroll.trg_DriverRateScheduleValues_PendingOnly()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_RateDefinitionID INTEGER;
    v_ScheduleID INTEGER;
    v_Status VARCHAR(30);
BEGIN
    IF TG_OP = 'DELETE' THEN
        v_RateDefinitionID := OLD.RateDefinitionID;
        v_ScheduleID := OLD.DriverRateScheduleID;
    ELSE
        v_RateDefinitionID := NEW.RateDefinitionID;
        v_ScheduleID := NEW.DriverRateScheduleID;
    END IF;

    IF TG_OP = 'UPDATE'
       AND (NEW.DriverRateScheduleID IS DISTINCT FROM OLD.DriverRateScheduleID
            OR NEW.RateDefinitionID IS DISTINCT FROM OLD.RateDefinitionID
            OR NEW.RateComponentDefinitionID IS DISTINCT FROM OLD.RateComponentDefinitionID) THEN
        RAISE EXCEPTION 'RATE_VALUE_IDENTITY_IMMUTABLE'
            USING ERRCODE = 'check_violation';
    END IF;

    PERFORM payroll.fn_LockRateDefinitionStructure(v_RateDefinitionID);

    SELECT s.Status INTO v_Status
      FROM payroll.DriverRateSchedules s
     WHERE s.DriverRateScheduleID = v_ScheduleID;

    -- A missing parent is a foreign-key violation (insert) or a cascade from
    -- discarding a Pending schedule (delete); both are handled elsewhere.
    IF FOUND AND v_Status <> 'Pending' THEN
        RAISE EXCEPTION 'RATE_VALUES_IMMUTABLE_AFTER_APPROVAL'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('DriverRateScheduleID=%s Status=%s', v_ScheduleID, v_Status);
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION payroll.fn_AssertRateStructureMutable(p_RateDefinitionID INTEGER)
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
    IF payroll.fn_LockRateDefinitionStructure(p_RateDefinitionID) IS NOT NULL THEN
        RAISE EXCEPTION 'RATE_STRUCTURE_LOCKED'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('RateDefinitionID=%s', p_RateDefinitionID);
    END IF;
    IF EXISTS (
        SELECT 1
          FROM payroll.DriverRateSchedules s
         WHERE s.RateDefinitionID = p_RateDefinitionID
           AND s.Status = 'Pending'
    ) THEN
        RAISE EXCEPTION 'RATE_STRUCTURE_PENDING_SCHEDULE'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('RateDefinitionID=%s', p_RateDefinitionID);
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- 7. Pay-rule integrity: identity, amount, window start and calendar are
--    immutable after insert; the lifecycle is Active -> Ended / Voided and
--    Ended -> Voided; the end date changes only when the rule is ended.
-- ---------------------------------------------------------------------------

CREATE FUNCTION payroll.trg_DriverPayRules_BeforeUpdate()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.CompanyID IS DISTINCT FROM OLD.CompanyID
       OR NEW.DriverID IS DISTINCT FROM OLD.DriverID
       OR NEW.RuleType IS DISTINCT FROM OLD.RuleType
       OR NEW.Amount IS DISTINCT FROM OLD.Amount
       OR NEW.EffectiveFrom IS DISTINCT FROM OLD.EffectiveFrom
       OR NEW.CalendarFrequency IS DISTINCT FROM OLD.CalendarFrequency
       OR NEW.CalendarAnchorStartDate IS DISTINCT FROM OLD.CalendarAnchorStartDate
       OR NEW.CalendarCustomIntervalDays IS DISTINCT FROM OLD.CalendarCustomIntervalDays
       OR NEW.CalendarSourceBranchID IS DISTINCT FROM OLD.CalendarSourceBranchID
       OR NEW.CalendarSourcePayrollSetupVersionID IS DISTINCT FROM OLD.CalendarSourcePayrollSetupVersionID THEN
        RAISE EXCEPTION 'DRIVER_PAY_RULE_IMMUTABLE'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('DriverPayRuleID=%s', OLD.DriverPayRuleID);
    END IF;

    IF NEW.Status IS DISTINCT FROM OLD.Status
       AND NOT ((OLD.Status = 'Active' AND NEW.Status IN ('Ended', 'Voided'))
                OR (OLD.Status = 'Ended' AND NEW.Status = 'Voided')) THEN
        RAISE EXCEPTION 'DRIVER_PAY_RULE_INVALID_TRANSITION'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('%s -> %s', OLD.Status, NEW.Status);
    END IF;

    IF NEW.EffectiveTo IS DISTINCT FROM OLD.EffectiveTo
       AND NOT (OLD.Status = 'Active' AND NEW.Status = 'Ended') THEN
        RAISE EXCEPTION 'DRIVER_PAY_RULE_EFFECTIVE_TO_IMMUTABLE'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('DriverPayRuleID=%s Status=%s -> %s', OLD.DriverPayRuleID, OLD.Status, NEW.Status);
    END IF;

    RETURN NEW;
END
$$;

CREATE TRIGGER trg_driverpayrules_beforeupdate
BEFORE UPDATE ON payroll.DriverPayRules
FOR EACH ROW EXECUTE FUNCTION payroll.trg_DriverPayRules_BeforeUpdate();

-- ---------------------------------------------------------------------------
-- 8. Used-rate-definition evidence scope and company monetary state.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION payroll.fn_guard_used_rate_definition_scope()
RETURNS TRIGGER AS $$
DECLARE
    v_company_id INTEGER;
    v_branch_id INTEGER;
    v_period_id INTEGER;
    v_assignment_id INTEGER;
    v_driver_id INTEGER;
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
        -- A pay rule belongs to the permanent Driver; the evidence row names the
        -- Driver (bound to its assignment by foreign key) and the Branch work scope.
        SELECT companyid, driverid
        INTO v_company_id, v_driver_id
        FROM payroll.driverpayrules
        WHERE driverpayruleid = NEW.driverpayruleid;

        IF v_company_id IS DISTINCT FROM NEW.companyid
           OR v_driver_id IS DISTINCT FROM NEW.driverid THEN
            RAISE EXCEPTION
                'used_rate_definition_scope: driver pay rule does not match definition scope'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION core.fn_company_has_durable_monetary_state(p_company_id INTEGER)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
AS $$
    SELECT
        EXISTS (SELECT 1 FROM payroll.driverrates r
                WHERE r.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.driverratetiers t
                   JOIN payroll.driverrates r ON r.driverrateid = t.driverrateid
                   WHERE r.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.driverrateschedules s
                   WHERE s.companyid = p_company_id
                     AND s.status IN ('Approved', 'Superseded', 'Voided'))
        OR EXISTS (SELECT 1 FROM payroll.payrollbonusevents b
                   WHERE b.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.driverpayrules p
                   WHERE p.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.payrolldraftlines d
                   WHERE d.companyid = p_company_id
                     AND (d.rateamount IS NOT NULL OR d.calculatedamount IS NOT NULL))
        OR EXISTS (SELECT 1 FROM payroll.payrollcalculationsnapshots s
                   WHERE s.companyid = p_company_id)
        OR EXISTS (SELECT 1 FROM payroll.payrollfinallines f
                   WHERE f.companyid = p_company_id);
$$;

-- ---------------------------------------------------------------------------
-- 9. Driver-owned compensation context.
-- ---------------------------------------------------------------------------

-- The home DriverAssignment that is the authority context for a business date: the
-- latest-starting assignment of the Driver whose instant window overlaps that date's
-- Branch-local day in the assignment's own Branch timezone. NULL when none does (an
-- empty never-effective window never overlaps anything). STRICT: a NULL date has no context.
CREATE FUNCTION core.fn_DriverAssignmentForBusinessDate(
    p_CompanyID INTEGER,
    p_DriverID INTEGER,
    p_Date DATE
)
RETURNS INTEGER
LANGUAGE sql
STABLE
STRICT
AS $$
    SELECT a.DriverAssignmentID
    FROM core.DriverAssignments a
    WHERE a.CompanyID = p_CompanyID
      AND a.DriverID = p_DriverID
      AND core.fn_DriverEffectiveRange(a.EffectiveFromAt, a.EffectiveToAt)
          && core.fn_BranchWorkDateRange(a.CompanyID, a.BranchID, p_Date, p_Date)
    ORDER BY a.EffectiveFromAt DESC, a.DriverAssignmentID DESC
    LIMIT 1
$$;

-- Payroll periods of a Driver that a compensation footprint is relevant to, through ANY of
-- the Driver's assignments in ANY Branch, whatever the period's status: periods with an
-- eligibility row of one of those assignments. This is the ONE predicate behind both the
-- finalized-payroll guard and the writers' Branch workflow-lock discovery. It compares
-- DATE ranges against frozen eligibility facts (the period's own dates and its roster), so no
-- live Branch-timezone conversion can make it disagree with what the guard judges. Mode
-- 'WorkDate' tests the period's work dates against the footprint (rate schedules); mode
-- 'PeriodStart' tests the period's start date (pay rules, selected at period start).
CREATE FUNCTION payroll.fn_DriverPayrollPeriodsForFootprint(
    p_CompanyID INTEGER,
    p_DriverID INTEGER,
    p_Footprint DATERANGE,
    p_Mode TEXT
)
RETURNS TABLE (PayrollPeriodID INTEGER, BranchID INTEGER, StartDate DATE, EndDate DATE, Status VARCHAR)
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    IF p_Mode IS NULL OR p_Mode NOT IN ('WorkDate', 'PeriodStart') THEN
        RAISE EXCEPTION 'DRIVER_FINALIZED_GUARD_MODE_INVALID: mode must be WorkDate or PeriodStart'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_Footprint IS NULL THEN
        RAISE EXCEPTION 'DRIVER_FINALIZED_GUARD_FOOTPRINT_REQUIRED: footprint must not be null'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    RETURN QUERY
    SELECT pp.PayrollPeriodID, pp.BranchID, pp.StartDate, pp.EndDate, pp.Status::VARCHAR
    FROM payroll.PayrollPeriods pp
    WHERE pp.CompanyID = p_CompanyID
      AND CASE p_Mode
              WHEN 'WorkDate' THEN daterange(pp.StartDate, pp.EndDate, '[]') && p_Footprint
              ELSE p_Footprint @> pp.StartDate
          END
      AND EXISTS (
          SELECT 1
          FROM payroll.PayrollPeriodDriverEligibility e
          JOIN core.DriverAssignments a ON a.DriverAssignmentID = e.DriverAssignmentID
          WHERE e.PayrollPeriodID = pp.PayrollPeriodID
            AND a.CompanyID = p_CompanyID
            AND a.DriverID = p_DriverID
      )
    ORDER BY pp.StartDate, pp.PayrollPeriodID;
END
$$;

-- Finalized payroll touched by a compensation change of a Driver: the periods of
-- payroll.fn_DriverPayrollPeriodsForFootprint that are Locked or Archived. Callers refuse
-- when any row is returned.
CREATE FUNCTION payroll.fn_DriverFinalizedPayrollTouched(
    p_CompanyID INTEGER,
    p_DriverID INTEGER,
    p_Footprint DATERANGE,
    p_Mode TEXT
)
RETURNS TABLE (PayrollPeriodID INTEGER, StartDate DATE, EndDate DATE, Status VARCHAR)
LANGUAGE sql
STABLE
AS $$
    SELECT f.PayrollPeriodID, f.StartDate, f.EndDate, f.Status
    FROM payroll.fn_DriverPayrollPeriodsForFootprint(p_CompanyID, p_DriverID, p_Footprint, p_Mode) f
    WHERE f.Status IN ('Locked', 'Archived')
    ORDER BY f.StartDate, f.PayrollPeriodID
$$;

-- Mutation-instant enforcement. A change that alters authoritative compensation from a
-- start date is refused unless the Driver's home tenure that is the context for that
-- date exists and has not ended at the wall clock (clock_timestamp(), read after every
-- lock), so an operation that waited on a lock cannot slip into a tenure that has since
-- ended. Pending-only edits, creating a Pending schedule and discarding one have no
-- payroll effect and are exempt.
CREATE FUNCTION core.trg_DriverCompensationContextWritable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_IsPayRule BOOLEAN := (TG_TABLE_NAME = 'driverpayrules');
    v_Start DATE;
    v_TailStart DATE;
    v_ContextID INTEGER;
    v_EndedAt TIMESTAMPTZ;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT v_IsPayRule THEN
            RETURN NULL;
        END IF;
        v_Start := NEW.EffectiveFrom;
    ELSIF NOT v_IsPayRule AND OLD.Status = 'Pending' THEN
        IF NEW.Status = 'Approved' THEN
            v_Start := NEW.EffectiveFrom;
        END IF;
    ELSE
        IF NEW.Status = 'Voided' AND OLD.Status <> 'Voided' THEN
            v_Start := OLD.EffectiveFrom;
        END IF;
        IF NEW.EffectiveTo IS DISTINCT FROM OLD.EffectiveTo THEN
            -- The first date whose resolved compensation changes: the day after the
            -- earlier of the old and the new end (LEAST ignores an open end).
            v_TailStart := LEAST(OLD.EffectiveTo, NEW.EffectiveTo) + 1;
            v_Start := LEAST(v_Start, v_TailStart);
        END IF;
    END IF;

    IF v_Start IS NULL THEN
        RETURN NULL;
    END IF;

    v_ContextID := core.fn_DriverAssignmentForBusinessDate(NEW.CompanyID, NEW.DriverID, v_Start);
    IF v_ContextID IS NOT NULL THEN
        SELECT a.EffectiveToAt INTO v_EndedAt
        FROM core.DriverAssignments a
        WHERE a.DriverAssignmentID = v_ContextID;
    END IF;

    IF v_ContextID IS NULL OR (v_EndedAt IS NOT NULL AND v_EndedAt <= clock_timestamp()) THEN
        RAISE EXCEPTION 'DRIVER_COMPENSATION_HISTORICAL: Driver-owned compensation cannot change from a date whose home tenure has ended'
            USING ERRCODE = 'FL001',
                  DETAIL = format('DriverID=%s StartDate=%s ContextDriverAssignmentID=%s Table=%s Operation=%s',
                                  NEW.DriverID, v_Start, coalesce(v_ContextID::TEXT, 'none'), TG_TABLE_NAME, TG_OP);
    END IF;

    RETURN NULL;
END
$$;

CREATE TRIGGER trg_DriverCompensationContextWritable
AFTER INSERT OR UPDATE ON payroll.DriverRateSchedules
FOR EACH ROW EXECUTE FUNCTION core.trg_DriverCompensationContextWritable();

CREATE TRIGGER trg_DriverCompensationContextWritable
AFTER INSERT OR UPDATE ON payroll.DriverPayRules
FOR EACH ROW EXECUTE FUNCTION core.trg_DriverCompensationContextWritable();
