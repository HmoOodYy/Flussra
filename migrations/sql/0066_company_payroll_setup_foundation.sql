-- Company-owned schedule policy foundation. Legacy branch schedule authority remains intact.
CREATE TABLE payroll.PayrollSetups (
    PayrollSetupID BIGSERIAL PRIMARY KEY,
    CompanyID INTEGER NOT NULL REFERENCES core.Companies (CompanyID),
    SetupCode VARCHAR(50) NOT NULL,
    SetupName VARCHAR(200) NOT NULL,
    Description TEXT,
    Status VARCHAR(20) NOT NULL DEFAULT 'Active',
    CreatedByUserID INTEGER REFERENCES sec.Users (UserID),
    CreatedAtUtc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UpdatedByUserID INTEGER REFERENCES sec.Users (UserID),
    UpdatedAtUtc TIMESTAMPTZ,
    CONSTRAINT uq_PayrollSetups_Company_Code UNIQUE (CompanyID, SetupCode),
    CONSTRAINT uq_PayrollSetups_ID_Company UNIQUE (PayrollSetupID, CompanyID),
    CONSTRAINT ck_PayrollSetups_Status CHECK (Status IN ('Active', 'Archived')),
    CONSTRAINT ck_PayrollSetups_Code CHECK (SetupCode ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$')
);

CREATE OR REPLACE FUNCTION payroll.fn_guard_payroll_setup_identity()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.PayrollSetupID, NEW.CompanyID, NEW.SetupCode)
       IS DISTINCT FROM (OLD.PayrollSetupID, OLD.CompanyID, OLD.SetupCode) THEN
        RAISE EXCEPTION 'payroll_setup_identity: company and integration code cannot change'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_PayrollSetups_Identity
    BEFORE UPDATE OF PayrollSetupID, CompanyID, SetupCode ON payroll.PayrollSetups
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_payroll_setup_identity();

CREATE TABLE payroll.PayrollSetupVersions (
    PayrollSetupVersionID BIGSERIAL PRIMARY KEY,
    CompanyID INTEGER NOT NULL,
    PayrollSetupID BIGINT NOT NULL,
    LifecycleState VARCHAR(20) NOT NULL DEFAULT 'Draft',
    VersionNumber INTEGER,
    EffectiveFromDate DATE,
    PayrollFrequency VARCHAR(20),
    AnchorStartDate DATE,
    CustomIntervalDays INTEGER,
    NormalDaysOffMask SMALLINT,
    ConfigHash VARCHAR(64),
    ReplacesVersionID BIGINT,
    CreatedByUserID INTEGER REFERENCES sec.Users (UserID),
    CreatedAtUtc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    DiscardedByUserID INTEGER REFERENCES sec.Users (UserID),
    DiscardedAtUtc TIMESTAMPTZ,
    PublishedByUserID INTEGER REFERENCES sec.Users (UserID),
    PublishedAtUtc TIMESTAMPTZ,
    CONSTRAINT uq_PayrollSetupVersions_Setup_Number UNIQUE (PayrollSetupID, VersionNumber),
    CONSTRAINT uq_PayrollSetupVersions_ID_Company_Setup
        UNIQUE (PayrollSetupVersionID, CompanyID, PayrollSetupID),
    CONSTRAINT uq_PayrollSetupVersions_ID_Company_Setup_Hash
        UNIQUE (PayrollSetupVersionID, CompanyID, PayrollSetupID, ConfigHash),
    CONSTRAINT fk_PayrollSetupVersions_Setup
        FOREIGN KEY (PayrollSetupID, CompanyID)
        REFERENCES payroll.PayrollSetups (PayrollSetupID, CompanyID) ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollSetupVersions_Replaces
        FOREIGN KEY (ReplacesVersionID, CompanyID, PayrollSetupID)
        REFERENCES payroll.PayrollSetupVersions (PayrollSetupVersionID, CompanyID, PayrollSetupID)
        ON DELETE RESTRICT,
    CONSTRAINT ck_PayrollSetupVersions_State CHECK (LifecycleState IN ('Draft', 'Published')),
    CONSTRAINT ck_PayrollSetupVersions_Frequency
        CHECK (PayrollFrequency IS NULL OR PayrollFrequency IN ('Week', 'Biweek', 'Month', 'Custom')),
    CONSTRAINT ck_PayrollSetupVersions_Interval
        CHECK (CustomIntervalDays IS NULL OR CustomIntervalDays > 0),
    CONSTRAINT ck_PayrollSetupVersions_Mask
        CHECK (NormalDaysOffMask IS NULL OR NormalDaysOffMask BETWEEN 0 AND 127),
    CONSTRAINT ck_PayrollSetupVersions_Published
        CHECK (
            LifecycleState = 'Draft'
            OR (
                VersionNumber > 0 AND EffectiveFromDate IS NOT NULL
                AND PayrollFrequency IS NOT NULL AND AnchorStartDate IS NOT NULL
                AND (PayrollFrequency <> 'Custom' OR CustomIntervalDays IS NOT NULL)
                AND NormalDaysOffMask IS NOT NULL
                AND ConfigHash ~ '^[0-9a-f]{64}$'
                AND PublishedByUserID IS NOT NULL AND PublishedAtUtc IS NOT NULL
            )
        ),
    CONSTRAINT ck_PayrollSetupVersions_DraftNumber
        CHECK (LifecycleState <> 'Draft' OR VersionNumber IS NULL),
    CONSTRAINT ck_PayrollSetupVersions_ReplacementState
        CHECK (ReplacesVersionID IS NULL OR LifecycleState = 'Published'),
    CONSTRAINT ck_PayrollSetupVersions_Discarded
        CHECK (
            (DiscardedAtUtc IS NULL AND DiscardedByUserID IS NULL)
            OR (LifecycleState = 'Draft'
                AND DiscardedAtUtc IS NOT NULL AND DiscardedByUserID IS NOT NULL)
        ),
    CONSTRAINT ck_PayrollSetupVersions_NoSelfReplacement
        CHECK (ReplacesVersionID IS NULL OR ReplacesVersionID <> PayrollSetupVersionID)
);

CREATE UNIQUE INDEX ux_PayrollSetupVersions_Replacement
    ON payroll.PayrollSetupVersions (ReplacesVersionID)
    WHERE ReplacesVersionID IS NOT NULL;
CREATE UNIQUE INDEX ux_PayrollSetupVersions_PublishedDateRoot
    ON payroll.PayrollSetupVersions (PayrollSetupID, EffectiveFromDate)
    WHERE LifecycleState = 'Published' AND ReplacesVersionID IS NULL;
CREATE INDEX ix_PayrollSetupVersions_Effective
    ON payroll.PayrollSetupVersions (PayrollSetupID, EffectiveFromDate, VersionNumber)
    WHERE LifecycleState = 'Published';

CREATE OR REPLACE FUNCTION payroll.fn_guard_payroll_setup_version()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE replaced payroll.PayrollSetupVersions%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.LifecycleState = 'Published' OR OLD.DiscardedAtUtc IS NOT NULL THEN
            RAISE EXCEPTION 'retained_payroll_setup_version_immutable: DELETE forbidden'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.LifecycleState = 'Published' THEN
        RAISE EXCEPTION 'published_payroll_setup_version_immutable: UPDATE forbidden'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.DiscardedAtUtc IS NOT NULL THEN
        RAISE EXCEPTION 'discarded_payroll_setup_version_immutable: UPDATE forbidden'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'UPDATE' AND
       (NEW.PayrollSetupVersionID, NEW.CompanyID, NEW.PayrollSetupID)
       IS DISTINCT FROM
       (OLD.PayrollSetupVersionID, OLD.CompanyID, OLD.PayrollSetupID) THEN
        RAISE EXCEPTION 'payroll_setup_version_identity: draft ownership cannot change'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.DiscardedAtUtc IS NULL
       AND NEW.DiscardedAtUtc IS NOT NULL AND
       (NEW.LifecycleState, NEW.VersionNumber, NEW.EffectiveFromDate,
        NEW.PayrollFrequency, NEW.AnchorStartDate, NEW.CustomIntervalDays,
        NEW.NormalDaysOffMask, NEW.ConfigHash, NEW.ReplacesVersionID,
        NEW.PublishedByUserID, NEW.PublishedAtUtc)
       IS DISTINCT FROM
       (OLD.LifecycleState, OLD.VersionNumber, OLD.EffectiveFromDate,
        OLD.PayrollFrequency, OLD.AnchorStartDate, OLD.CustomIntervalDays,
        OLD.NormalDaysOffMask, OLD.ConfigHash, OLD.ReplacesVersionID,
        OLD.PublishedByUserID, OLD.PublishedAtUtc) THEN
        RAISE EXCEPTION 'payroll_setup_version_discard: configuration cannot change during discard'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.ReplacesVersionID IS NOT NULL THEN
        SELECT * INTO replaced FROM payroll.PayrollSetupVersions
        WHERE PayrollSetupVersionID = NEW.ReplacesVersionID;
        IF NOT FOUND OR replaced.CompanyID <> NEW.CompanyID
           OR replaced.PayrollSetupID <> NEW.PayrollSetupID
           OR replaced.LifecycleState <> 'Published'
           OR replaced.EffectiveFromDate IS DISTINCT FROM NEW.EffectiveFromDate
           OR replaced.VersionNumber >= NEW.VersionNumber THEN
            RAISE EXCEPTION 'payroll_setup_version_replacement: invalid predecessor'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_PayrollSetupVersions_Guard
    BEFORE INSERT OR UPDATE OR DELETE ON payroll.PayrollSetupVersions
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_payroll_setup_version();

CREATE TABLE payroll.BranchPayrollSetupAssignments (
    BranchPayrollSetupAssignmentID BIGSERIAL PRIMARY KEY,
    CompanyID INTEGER NOT NULL,
    BranchID INTEGER NOT NULL,
    PayrollSetupID BIGINT NOT NULL,
    EffectiveFromDate DATE NOT NULL,
    EffectiveToDate DATE,
    CreatedByUserID INTEGER REFERENCES sec.Users (UserID),
    CreatedAtUtc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ChangeReason TEXT,
    CorrelationID UUID,
    WithdrawnByUserID INTEGER REFERENCES sec.Users (UserID),
    WithdrawnAtUtc TIMESTAMPTZ,
    WithdrawalReason TEXT,
    CONSTRAINT uq_BranchPayrollSetupAssignments_Scope
        UNIQUE (BranchPayrollSetupAssignmentID, CompanyID, BranchID, PayrollSetupID),
    CONSTRAINT fk_BranchPayrollSetupAssignments_Branch
        FOREIGN KEY (BranchID, CompanyID) REFERENCES core.Branches (BranchID, CompanyID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_BranchPayrollSetupAssignments_Setup
        FOREIGN KEY (PayrollSetupID, CompanyID)
        REFERENCES payroll.PayrollSetups (PayrollSetupID, CompanyID) ON DELETE RESTRICT,
    CONSTRAINT ck_BranchPayrollSetupAssignments_Range
        CHECK (EffectiveToDate IS NULL OR EffectiveToDate > EffectiveFromDate),
    CONSTRAINT ck_BranchPayrollSetupAssignments_Withdrawal
        CHECK ((WithdrawnAtUtc IS NULL) = (WithdrawnByUserID IS NULL)),
    CONSTRAINT ex_BranchPayrollSetupAssignments_NoOverlap
        EXCLUDE USING gist (
            BranchID WITH =,
            daterange(EffectiveFromDate, EffectiveToDate, '[)') WITH &&
        ) WHERE (WithdrawnAtUtc IS NULL)
);
CREATE INDEX ix_BranchPayrollSetupAssignments_Timeline
    ON payroll.BranchPayrollSetupAssignments (CompanyID, BranchID, EffectiveFromDate)
    WHERE WithdrawnAtUtc IS NULL;

CREATE OR REPLACE FUNCTION payroll.fn_guard_payroll_setup_assignment_identity()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.BranchPayrollSetupAssignmentID, NEW.CompanyID, NEW.BranchID,
        NEW.PayrollSetupID, NEW.EffectiveFromDate)
       IS DISTINCT FROM
       (OLD.BranchPayrollSetupAssignmentID, OLD.CompanyID, OLD.BranchID,
        OLD.PayrollSetupID, OLD.EffectiveFromDate) THEN
        RAISE EXCEPTION 'payroll_setup_assignment_identity: assignment authority cannot be rewritten'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_BranchPayrollSetupAssignments_Identity
    BEFORE UPDATE OF BranchPayrollSetupAssignmentID, CompanyID, BranchID,
        PayrollSetupID, EffectiveFromDate
    ON payroll.BranchPayrollSetupAssignments
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_payroll_setup_assignment_identity();

ALTER TABLE core.Companies ADD COLUMN DefaultPayrollSetupID BIGINT;
ALTER TABLE core.Companies ADD CONSTRAINT fk_Companies_DefaultPayrollSetup
    FOREIGN KEY (DefaultPayrollSetupID, CompanyID)
    REFERENCES payroll.PayrollSetups (PayrollSetupID, CompanyID) ON DELETE RESTRICT;

CREATE OR REPLACE FUNCTION payroll.fn_guard_company_default_payroll_setup()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.DefaultPayrollSetupID IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM payroll.PayrollSetups s
        WHERE s.PayrollSetupID = NEW.DefaultPayrollSetupID
          AND s.CompanyID = NEW.CompanyID AND s.Status = 'Active'
        FOR UPDATE
    ) THEN
        RAISE EXCEPTION 'company_default_payroll_setup: default must be an active company setup'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_Companies_DefaultPayrollSetup
    BEFORE INSERT OR UPDATE OF DefaultPayrollSetupID, CompanyID ON core.Companies
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_company_default_payroll_setup();

CREATE OR REPLACE FUNCTION payroll.fn_guard_archived_default_payroll_setup()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.Status = 'Archived' AND OLD.Status <> 'Archived' AND EXISTS (
        SELECT 1 FROM core.Companies c WHERE c.DefaultPayrollSetupID = NEW.PayrollSetupID
    ) THEN
        RAISE EXCEPTION 'payroll_setup_archive: company default cannot be archived'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_PayrollSetups_DefaultArchive
    BEFORE UPDATE OF Status ON payroll.PayrollSetups
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_archived_default_payroll_setup();
