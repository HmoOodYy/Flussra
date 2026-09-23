-- Transitional authority provenance. The legacy period runtime remains authoritative.
ALTER TABLE payroll.PayrollPeriods
    ADD COLUMN BranchPayrollSetupAssignmentID BIGINT,
    ADD COLUMN PayrollSetupVersionID BIGINT,
    ADD COLUMN FrozenPayrollSetupID BIGINT,
    ADD COLUMN FrozenPayrollSetupCode VARCHAR(50),
    ADD COLUMN FrozenPayrollSetupVersionNumber INTEGER,
    ADD COLUMN FrozenPayrollFrequency VARCHAR(20),
    ADD COLUMN FrozenAnchorStartDate DATE,
    ADD COLUMN FrozenCustomIntervalDays INTEGER,
    ADD COLUMN FrozenNormalDaysOffMask SMALLINT,
    ADD COLUMN ScheduleConfigHash VARCHAR(64);

ALTER TABLE payroll.PayrollPeriods
    ADD CONSTRAINT ck_PayrollPeriods_AuthorityRepresentation
    CHECK (
        (BranchPayrollSetupAssignmentID IS NULL
         AND PayrollSetupVersionID IS NULL
         AND FrozenPayrollSetupID IS NULL
         AND FrozenPayrollSetupCode IS NULL
         AND FrozenPayrollSetupVersionNumber IS NULL
         AND FrozenPayrollFrequency IS NULL
         AND FrozenAnchorStartDate IS NULL
         AND FrozenCustomIntervalDays IS NULL
         AND FrozenNormalDaysOffMask IS NULL
         AND ScheduleConfigHash IS NULL)
        OR
        (BranchPayrollSetupAssignmentID IS NOT NULL
         AND PayrollSetupVersionID IS NOT NULL
         AND FrozenPayrollSetupID IS NOT NULL
         AND FrozenPayrollSetupCode IS NOT NULL
         AND FrozenPayrollSetupVersionNumber IS NOT NULL
         AND FrozenPayrollFrequency IS NOT NULL
         AND FrozenAnchorStartDate IS NOT NULL
         AND FrozenNormalDaysOffMask IS NOT NULL
         AND ScheduleConfigHash IS NOT NULL
         AND ScheduleVersionID IS NULL)
    );
ALTER TABLE payroll.PayrollPeriods
    ADD CONSTRAINT fk_PayrollPeriods_SetupAssignment
    FOREIGN KEY (BranchPayrollSetupAssignmentID, CompanyID, BranchID, FrozenPayrollSetupID)
    REFERENCES payroll.BranchPayrollSetupAssignments
        (BranchPayrollSetupAssignmentID, CompanyID, BranchID, PayrollSetupID)
    ON DELETE RESTRICT;
ALTER TABLE payroll.PayrollPeriods
    ADD CONSTRAINT fk_PayrollPeriods_SetupVersion
    FOREIGN KEY (PayrollSetupVersionID, CompanyID, FrozenPayrollSetupID, ScheduleConfigHash)
    REFERENCES payroll.PayrollSetupVersions
        (PayrollSetupVersionID, CompanyID, PayrollSetupID, ConfigHash)
    ON DELETE RESTRICT;

-- A parent's unique identity-plus-authority tuples allow exact child FKs.
CREATE UNIQUE INDEX ux_PayrollPeriods_SetupDayAuthority
    ON payroll.PayrollPeriods
        (PayrollPeriodID, CompanyID, BranchID,
         BranchPayrollSetupAssignmentID, PayrollSetupVersionID);
CREATE UNIQUE INDEX ux_PayrollPeriods_LegacyDayAuthority
    ON payroll.PayrollPeriods
        (PayrollPeriodID, CompanyID, BranchID, ScheduleVersionID);

CREATE OR REPLACE FUNCTION payroll.fn_guard_period_setup_provenance()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_version payroll.PayrollSetupVersions%ROWTYPE;
    v_assignment payroll.BranchPayrollSetupAssignments%ROWTYPE;
    v_code VARCHAR(50);
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.BranchPayrollSetupAssignmentID IS NOT NULL AND
       (NEW.CompanyID, NEW.BranchID, NEW.StartDate, NEW.EndDate,
        NEW.ScheduleVersionID, NEW.BranchPayrollSetupAssignmentID, NEW.PayrollSetupVersionID,
        NEW.FrozenPayrollSetupID, NEW.FrozenPayrollSetupCode,
        NEW.FrozenPayrollSetupVersionNumber, NEW.FrozenPayrollFrequency,
        NEW.FrozenAnchorStartDate, NEW.FrozenCustomIntervalDays,
        NEW.FrozenNormalDaysOffMask, NEW.ScheduleConfigHash)
       IS DISTINCT FROM
       (OLD.CompanyID, OLD.BranchID, OLD.StartDate, OLD.EndDate,
        OLD.ScheduleVersionID, OLD.BranchPayrollSetupAssignmentID, OLD.PayrollSetupVersionID,
        OLD.FrozenPayrollSetupID, OLD.FrozenPayrollSetupCode,
        OLD.FrozenPayrollSetupVersionNumber, OLD.FrozenPayrollFrequency,
        OLD.FrozenAnchorStartDate, OLD.FrozenCustomIntervalDays,
        OLD.FrozenNormalDaysOffMask, OLD.ScheduleConfigHash) THEN
        RAISE EXCEPTION 'payroll_period_setup_provenance: frozen authority cannot change'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.BranchPayrollSetupAssignmentID IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT * INTO v_assignment FROM payroll.BranchPayrollSetupAssignments
    WHERE BranchPayrollSetupAssignmentID = NEW.BranchPayrollSetupAssignmentID;
    SELECT * INTO v_version FROM payroll.PayrollSetupVersions
    WHERE PayrollSetupVersionID = NEW.PayrollSetupVersionID;
    SELECT SetupCode INTO v_code FROM payroll.PayrollSetups
    WHERE PayrollSetupID = NEW.FrozenPayrollSetupID;
    IF NOT FOUND OR v_assignment.WithdrawnAtUtc IS NOT NULL
       OR v_assignment.EffectiveFromDate > NEW.StartDate
       OR (v_assignment.EffectiveToDate IS NOT NULL
           AND v_assignment.EffectiveToDate <= NEW.EndDate)
       OR v_version.LifecycleState IS DISTINCT FROM 'Published'
       OR v_version.EffectiveFromDate > NEW.StartDate
       OR v_version.VersionNumber IS DISTINCT FROM NEW.FrozenPayrollSetupVersionNumber
       OR v_version.PayrollFrequency IS DISTINCT FROM NEW.FrozenPayrollFrequency
       OR v_version.AnchorStartDate IS DISTINCT FROM NEW.FrozenAnchorStartDate
       OR v_version.CustomIntervalDays IS DISTINCT FROM NEW.FrozenCustomIntervalDays
       OR v_version.NormalDaysOffMask IS DISTINCT FROM NEW.FrozenNormalDaysOffMask
       OR v_code IS DISTINCT FROM NEW.FrozenPayrollSetupCode THEN
        RAISE EXCEPTION 'payroll_period_setup_provenance: authority snapshot is invalid'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_PayrollPeriods_SetupProvenance
    BEFORE INSERT OR UPDATE OF CompanyID, BranchID, StartDate, EndDate, ScheduleVersionID,
        BranchPayrollSetupAssignmentID, PayrollSetupVersionID,
        FrozenPayrollSetupID, FrozenPayrollSetupCode, FrozenPayrollSetupVersionNumber,
        FrozenPayrollFrequency, FrozenAnchorStartDate, FrozenCustomIntervalDays,
        FrozenNormalDaysOffMask, ScheduleConfigHash
    ON payroll.PayrollPeriods FOR EACH ROW
    EXECUTE FUNCTION payroll.fn_guard_period_setup_provenance();

-- Existing days retain their old version; new-authority days need no synthetic one.
ALTER TABLE payroll.PayrollPeriodDays
    ALTER COLUMN ScheduleVersionID DROP NOT NULL,
    ADD COLUMN BranchPayrollSetupAssignmentID BIGINT,
    ADD COLUMN PayrollSetupVersionID BIGINT;
ALTER TABLE payroll.PayrollPeriodDays
    ADD CONSTRAINT ck_PayrollPeriodDays_AuthorityRepresentation
    CHECK (
        (ScheduleVersionID IS NOT NULL
         AND BranchPayrollSetupAssignmentID IS NULL AND PayrollSetupVersionID IS NULL)
        OR
        (ScheduleVersionID IS NULL
         AND BranchPayrollSetupAssignmentID IS NOT NULL
         AND PayrollSetupVersionID IS NOT NULL)
    );
ALTER TABLE payroll.PayrollPeriodDays
    ADD CONSTRAINT fk_PayrollPeriodDays_ExactNewAuthority
    FOREIGN KEY (PayrollPeriodID, CompanyID, BranchID,
                 BranchPayrollSetupAssignmentID, PayrollSetupVersionID)
    REFERENCES payroll.PayrollPeriods
        (PayrollPeriodID, CompanyID, BranchID,
         BranchPayrollSetupAssignmentID, PayrollSetupVersionID)
    ON DELETE CASCADE;
ALTER TABLE payroll.PayrollPeriodDays
    ADD CONSTRAINT fk_PayrollPeriodDays_ExactLegacyAuthority
    FOREIGN KEY (PayrollPeriodID, CompanyID, BranchID, ScheduleVersionID)
    REFERENCES payroll.PayrollPeriods
        (PayrollPeriodID, CompanyID, BranchID, ScheduleVersionID)
    ON DELETE CASCADE;

CREATE INDEX ix_PayrollPeriodDays_SetupAuthority
    ON payroll.PayrollPeriodDays (PayrollSetupVersionID)
    WHERE PayrollSetupVersionID IS NOT NULL;
