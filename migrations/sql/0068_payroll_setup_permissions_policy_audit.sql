-- Payroll Setup permissions and immutable policy evidence foundation.
-- Runtime authority and policy-event emission are introduced in later phases.

INSERT INTO sec.Permissions (PermissionCode, PermissionName, ModuleCode)
VALUES
    ('payroll_setup.view',    'View Payroll Setup',             'payroll_setup'),
    ('payroll_setup.manage',  'Manage Payroll Setup Drafts',    'payroll_setup'),
    ('payroll_setup.publish', 'Publish Payroll Setup Versions', 'payroll_setup'),
    ('payroll_setup.assign',  'Assign Payroll Setup to Branches','payroll_setup')
ON CONFLICT (PermissionCode) DO NOTHING;

CREATE TABLE payroll.PayrollSetupPolicyAuditEvents (
    PayrollSetupPolicyAuditEventID BIGSERIAL PRIMARY KEY,
    CompanyID INTEGER NOT NULL,
    ActorUserID INTEGER NOT NULL,
    EventType VARCHAR(40) NOT NULL,
    PayrollSetupID BIGINT,
    PayrollSetupVersionID BIGINT,
    BranchPayrollSetupAssignmentID BIGINT,
    PayrollPeriodID INTEGER,
    BranchID INTEGER,
    OldPayrollSetupID BIGINT,
    NewPayrollSetupID BIGINT,
    OldPayrollSetupVersionID BIGINT,
    NewPayrollSetupVersionID BIGINT,
    OldBranchPayrollSetupAssignmentID BIGINT,
    NewBranchPayrollSetupAssignmentID BIGINT,
    EffectiveDate DATE,
    OldConfigHash VARCHAR(64),
    NewConfigHash VARCHAR(64),
    OldStateJSON JSONB,
    NewStateJSON JSONB,
    CorrelationID UUID,
    CreatedTransactionID BIGINT NOT NULL DEFAULT txid_current(),
    OccurredAtUtc TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_PayrollSetupPolicyAuditEvents_ID_Company
        UNIQUE (PayrollSetupPolicyAuditEventID, CompanyID),
    CONSTRAINT ck_PayrollSetupPolicyAuditEvents_EventType
        CHECK (EventType IN (
            'SetupCreated', 'SetupMetadataChanged',
            'DraftCreated', 'DraftChanged', 'DraftDiscarded',
            'VersionPublished', 'FutureVersionScheduled', 'VersionReplaced',
            'BranchAssigned', 'BranchReassigned', 'AssignmentWithdrawn',
            'DefaultChanged', 'SetupArchived', 'PeriodCreated'
        )),
    CONSTRAINT ck_PayrollSetupPolicyAuditEvents_TargetShape
        CHECK (
            (EventType IN ('SetupCreated', 'SetupMetadataChanged', 'SetupArchived')
                AND PayrollSetupID IS NOT NULL
                AND PayrollSetupVersionID IS NULL
                AND BranchPayrollSetupAssignmentID IS NULL
                AND PayrollPeriodID IS NULL)
            OR
            (EventType IN (
                'DraftCreated', 'DraftChanged', 'DraftDiscarded',
                'VersionPublished', 'FutureVersionScheduled', 'VersionReplaced'
            )
                AND PayrollSetupID IS NOT NULL
                AND PayrollSetupVersionID IS NOT NULL
                AND BranchPayrollSetupAssignmentID IS NULL
                AND PayrollPeriodID IS NULL)
            OR
            (EventType IN ('BranchAssigned', 'BranchReassigned', 'AssignmentWithdrawn')
                AND PayrollSetupID IS NOT NULL
                AND PayrollSetupVersionID IS NULL
                AND BranchPayrollSetupAssignmentID IS NOT NULL
                AND PayrollPeriodID IS NULL
                AND BranchID IS NOT NULL)
            OR
            (EventType = 'DefaultChanged'
                AND PayrollSetupID IS NULL
                AND PayrollSetupVersionID IS NULL
                AND BranchPayrollSetupAssignmentID IS NULL
                AND PayrollPeriodID IS NULL
                AND (OldPayrollSetupID IS NOT NULL OR NewPayrollSetupID IS NOT NULL))
            OR
            (EventType = 'PeriodCreated'
                AND PayrollSetupID IS NOT NULL
                AND PayrollSetupVersionID IS NOT NULL
                AND BranchPayrollSetupAssignmentID IS NOT NULL
                AND PayrollPeriodID IS NOT NULL
                AND BranchID IS NOT NULL)
        ),
    CONSTRAINT ck_PayrollSetupPolicyAuditEvents_OldHash
        CHECK (OldConfigHash IS NULL OR OldConfigHash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_PayrollSetupPolicyAuditEvents_NewHash
        CHECK (NewConfigHash IS NULL OR NewConfigHash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_PayrollSetupPolicyAuditEvents_PeriodHash
        CHECK (EventType <> 'PeriodCreated' OR NewConfigHash IS NOT NULL),
    CONSTRAINT ck_PayrollSetupPolicyAuditEvents_OldState
        CHECK (OldStateJSON IS NULL OR jsonb_typeof(OldStateJSON) = 'object'),
    CONSTRAINT ck_PayrollSetupPolicyAuditEvents_NewState
        CHECK (NewStateJSON IS NULL OR jsonb_typeof(NewStateJSON) = 'object'),
    CONSTRAINT ck_PayrollSetupPolicyAuditEvents_OldVersionSetup
        CHECK (OldPayrollSetupVersionID IS NULL OR OldPayrollSetupID IS NOT NULL),
    CONSTRAINT ck_PayrollSetupPolicyAuditEvents_NewVersionSetup
        CHECK (NewPayrollSetupVersionID IS NULL OR NewPayrollSetupID IS NOT NULL),
    CONSTRAINT ck_PayrollSetupPolicyAuditEvents_OldAssignmentScope
        CHECK (OldBranchPayrollSetupAssignmentID IS NULL OR
               (OldPayrollSetupID IS NOT NULL AND BranchID IS NOT NULL)),
    CONSTRAINT ck_PayrollSetupPolicyAuditEvents_NewAssignmentScope
        CHECK (NewBranchPayrollSetupAssignmentID IS NULL OR
               (NewPayrollSetupID IS NOT NULL AND BranchID IS NOT NULL)),
    CONSTRAINT fk_PayrollSetupPolicyAuditEvents_Company
        FOREIGN KEY (CompanyID) REFERENCES core.Companies (CompanyID) ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollSetupPolicyAuditEvents_Actor
        FOREIGN KEY (ActorUserID) REFERENCES sec.Users (UserID) ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollSetupPolicyAuditEvents_Setup
        FOREIGN KEY (PayrollSetupID, CompanyID)
        REFERENCES payroll.PayrollSetups (PayrollSetupID, CompanyID) ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollSetupPolicyAuditEvents_Version
        FOREIGN KEY (PayrollSetupVersionID, CompanyID, PayrollSetupID)
        REFERENCES payroll.PayrollSetupVersions
            (PayrollSetupVersionID, CompanyID, PayrollSetupID) ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollSetupPolicyAuditEvents_Assignment
        FOREIGN KEY (BranchPayrollSetupAssignmentID, CompanyID, BranchID, PayrollSetupID)
        REFERENCES payroll.BranchPayrollSetupAssignments
            (BranchPayrollSetupAssignmentID, CompanyID, BranchID, PayrollSetupID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollSetupPolicyAuditEvents_Period
        FOREIGN KEY (PayrollPeriodID) REFERENCES payroll.PayrollPeriods (PayrollPeriodID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollSetupPolicyAuditEvents_Branch
        FOREIGN KEY (BranchID, CompanyID)
        REFERENCES core.Branches (BranchID, CompanyID) ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollSetupPolicyAuditEvents_OldSetup
        FOREIGN KEY (OldPayrollSetupID, CompanyID)
        REFERENCES payroll.PayrollSetups (PayrollSetupID, CompanyID) ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollSetupPolicyAuditEvents_NewSetup
        FOREIGN KEY (NewPayrollSetupID, CompanyID)
        REFERENCES payroll.PayrollSetups (PayrollSetupID, CompanyID) ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollSetupPolicyAuditEvents_OldVersion
        FOREIGN KEY (OldPayrollSetupVersionID, CompanyID, OldPayrollSetupID)
        REFERENCES payroll.PayrollSetupVersions
            (PayrollSetupVersionID, CompanyID, PayrollSetupID) ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollSetupPolicyAuditEvents_NewVersion
        FOREIGN KEY (NewPayrollSetupVersionID, CompanyID, NewPayrollSetupID)
        REFERENCES payroll.PayrollSetupVersions
            (PayrollSetupVersionID, CompanyID, PayrollSetupID) ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollSetupPolicyAuditEvents_OldAssignment
        FOREIGN KEY (OldBranchPayrollSetupAssignmentID, CompanyID, BranchID, OldPayrollSetupID)
        REFERENCES payroll.BranchPayrollSetupAssignments
            (BranchPayrollSetupAssignmentID, CompanyID, BranchID, PayrollSetupID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollSetupPolicyAuditEvents_NewAssignment
        FOREIGN KEY (NewBranchPayrollSetupAssignmentID, CompanyID, BranchID, NewPayrollSetupID)
        REFERENCES payroll.BranchPayrollSetupAssignments
            (BranchPayrollSetupAssignmentID, CompanyID, BranchID, PayrollSetupID)
        ON DELETE RESTRICT
);

CREATE TABLE payroll.PayrollSetupPolicyAuditEventBranches (
    PayrollSetupPolicyAuditEventID BIGINT NOT NULL,
    CompanyID INTEGER NOT NULL,
    BranchID INTEGER NOT NULL,
    PRIMARY KEY (PayrollSetupPolicyAuditEventID, BranchID),
    CONSTRAINT fk_PayrollSetupPolicyAuditEventBranches_Event
        FOREIGN KEY (PayrollSetupPolicyAuditEventID, CompanyID)
        REFERENCES payroll.PayrollSetupPolicyAuditEvents
            (PayrollSetupPolicyAuditEventID, CompanyID) ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollSetupPolicyAuditEventBranches_Branch
        FOREIGN KEY (BranchID, CompanyID)
        REFERENCES core.Branches (BranchID, CompanyID) ON DELETE RESTRICT
);

CREATE INDEX ix_PayrollSetupPolicyAuditEvents_CompanyChronology
    ON payroll.PayrollSetupPolicyAuditEvents
        (CompanyID, OccurredAtUtc, PayrollSetupPolicyAuditEventID);
CREATE INDEX ix_PayrollSetupPolicyAuditEvents_SetupChronology
    ON payroll.PayrollSetupPolicyAuditEvents
        (CompanyID, PayrollSetupID, OccurredAtUtc, PayrollSetupPolicyAuditEventID)
    WHERE PayrollSetupID IS NOT NULL;
CREATE INDEX ix_PayrollSetupPolicyAuditEventBranches_BranchChronology
    ON payroll.PayrollSetupPolicyAuditEventBranches
        (CompanyID, BranchID, PayrollSetupPolicyAuditEventID);
CREATE OR REPLACE FUNCTION payroll.fn_guard_payroll_setup_policy_audit_scope()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_period_company_id INTEGER;
    v_period_branch_id INTEGER;
    v_period_assignment_id BIGINT;
    v_period_version_id BIGINT;
    v_period_setup_id BIGINT;
    v_period_config_hash VARCHAR(64);
BEGIN
    IF TG_TABLE_NAME = 'payrollsetuppolicyauditevents' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM sec.Users u
            JOIN core.Companies c ON c.CompanyID = NEW.CompanyID
            WHERE u.UserID = NEW.ActorUserID
              AND (u.CompanyID = NEW.CompanyID OR c.OwnerUserID = u.UserID)
        ) THEN
            RAISE EXCEPTION 'payroll_setup_policy_audit_scope: actor is not a member or owner of the company'
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.PayrollPeriodID IS NOT NULL THEN
            SELECT CompanyID, BranchID, BranchPayrollSetupAssignmentID,
                   PayrollSetupVersionID, FrozenPayrollSetupID, ScheduleConfigHash
            INTO v_period_company_id, v_period_branch_id, v_period_assignment_id,
                 v_period_version_id, v_period_setup_id, v_period_config_hash
            FROM payroll.PayrollPeriods
            WHERE PayrollPeriodID = NEW.PayrollPeriodID;

            IF v_period_company_id IS DISTINCT FROM NEW.CompanyID
               OR v_period_branch_id IS DISTINCT FROM NEW.BranchID THEN
                RAISE EXCEPTION 'payroll_setup_policy_audit_scope: period scope does not match event scope'
                    USING ERRCODE = 'check_violation';
            END IF;

            IF NEW.EventType = 'PeriodCreated'
               AND (v_period_assignment_id IS DISTINCT FROM NEW.BranchPayrollSetupAssignmentID
                    OR v_period_version_id IS DISTINCT FROM NEW.PayrollSetupVersionID
                    OR v_period_setup_id IS DISTINCT FROM NEW.PayrollSetupID
                    OR v_period_config_hash IS DISTINCT FROM NEW.NewConfigHash) THEN
                RAISE EXCEPTION 'payroll_setup_policy_audit_scope: PeriodCreated authority does not match period provenance'
                    USING ERRCODE = 'check_violation';
            END IF;
        END IF;
        RETURN NEW;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM payroll.PayrollSetupPolicyAuditEvents e
        WHERE e.PayrollSetupPolicyAuditEventID = NEW.PayrollSetupPolicyAuditEventID
          AND e.CompanyID = NEW.CompanyID
          AND e.CreatedTransactionID = txid_current()
    ) THEN
        RAISE EXCEPTION 'payroll_setup_policy_audit_scope: affected Branches must be recorded with their event transaction'
                USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION payroll.fn_guard_payroll_setup_policy_audit_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'payroll_setup_policy_audit_immutable: % on %.% is forbidden once written.',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER trg_PayrollSetupPolicyAuditEvents_Scope
    BEFORE INSERT ON payroll.PayrollSetupPolicyAuditEvents
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_payroll_setup_policy_audit_scope();
CREATE TRIGGER trg_PayrollSetupPolicyAuditEventBranches_Scope
    BEFORE INSERT ON payroll.PayrollSetupPolicyAuditEventBranches
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_payroll_setup_policy_audit_scope();
CREATE TRIGGER trg_PayrollSetupPolicyAuditEvents_Immutable
    BEFORE UPDATE OR DELETE ON payroll.PayrollSetupPolicyAuditEvents
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_payroll_setup_policy_audit_immutable();
CREATE TRIGGER trg_PayrollSetupPolicyAuditEventBranches_Immutable
    BEFORE UPDATE OR DELETE ON payroll.PayrollSetupPolicyAuditEventBranches
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_payroll_setup_policy_audit_immutable();
CREATE TRIGGER trg_PayrollSetupPolicyAuditEvents_NoTruncate
    BEFORE TRUNCATE ON payroll.PayrollSetupPolicyAuditEvents
    FOR EACH STATEMENT EXECUTE FUNCTION payroll.fn_guard_payroll_setup_policy_audit_immutable();
CREATE TRIGGER trg_PayrollSetupPolicyAuditEventBranches_NoTruncate
    BEFORE TRUNCATE ON payroll.PayrollSetupPolicyAuditEventBranches
    FOR EACH STATEMENT EXECUTE FUNCTION payroll.fn_guard_payroll_setup_policy_audit_immutable();
