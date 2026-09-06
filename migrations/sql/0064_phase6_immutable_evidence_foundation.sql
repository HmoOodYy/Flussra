-- =============================================================================
-- Migration 0064: Phase 6 immutable-evidence foundation
--
-- Adds only the immutable evidence that is not already supplied by FinalLines,
-- calculation snapshots, period metadata, or CP-5C Status/Bonus evidence.
-- Existing history remains legacy; it is deliberately not backfilled.
-- =============================================================================

INSERT INTO sec.permissions (permissioncode, permissionname, modulecode)
VALUES
    ('ledger.view', 'View Finalized Payroll Library', 'ledger'),
    ('ledger.audit.view', 'View Finalized Payroll Audit', 'ledger')
ON CONFLICT (permissioncode) DO NOTHING;


CREATE TABLE payroll.PayrollPeriodWorkflowActionEvidence (
    PayrollPeriodWorkflowActionEvidenceID BIGSERIAL    PRIMARY KEY,
    CompanyID                              INTEGER      NOT NULL,
    BranchID                               INTEGER      NOT NULL,
    PayrollPeriodID                        INTEGER      NOT NULL,
    PayrollCalculationSnapshotID           BIGINT,
    ReviewItemID                           BIGINT,
    ReviewDecisionID                       BIGINT,
    ActionCode                             VARCHAR(50)  NOT NULL,
    ActorUserID                            INTEGER      NOT NULL,
    ActorDisplayNameSnapshot               VARCHAR(200) NOT NULL,
    RequiredPermissionCode                 VARCHAR(100) NOT NULL,
    ResponsibilityContextSnapshot          JSONB        NOT NULL,
    ReasonSnapshot                         TEXT,
    ActionAtUtc                            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_PayrollPeriodWorkflowActionEvidence_ActionCode
        CHECK (ActionCode IN (
            'SUBMITTED', 'RESUBMITTED', 'REVIEW_APPROVED',
            'REVIEW_EDIT_REQUESTED', 'REVIEW_REJECTED', 'FINALIZED'
        )),
    CONSTRAINT ck_PayrollPeriodWorkflowActionEvidence_RoleContext
        CHECK (jsonb_typeof(ResponsibilityContextSnapshot) = 'object'),
    CONSTRAINT fk_PayrollPeriodWorkflowActionEvidence_Period
        FOREIGN KEY (PayrollPeriodID)
        REFERENCES payroll.PayrollPeriods (PayrollPeriodID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollPeriodWorkflowActionEvidence_Actor
        FOREIGN KEY (ActorUserID)
        REFERENCES sec.Users (UserID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollPeriodWorkflowActionEvidence_ReviewItem
        FOREIGN KEY (ReviewItemID)
        REFERENCES review.ManagerReviewItems (ReviewItemID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollPeriodWorkflowActionEvidence_ReviewDecision
        FOREIGN KEY (ReviewDecisionID)
        REFERENCES review.ManagerReviewDecisions (ReviewDecisionID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollPeriodWorkflowActionEvidence_SnapshotScope
        FOREIGN KEY (PayrollCalculationSnapshotID, CompanyID, BranchID, PayrollPeriodID)
        REFERENCES payroll.PayrollCalculationSnapshots
            (PayrollCalculationSnapshotID, CompanyID, BranchID, PayrollPeriodID)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX ux_PayrollPeriodWorkflowActionEvidence_Snapshot_Action
    ON payroll.PayrollPeriodWorkflowActionEvidence
        (PayrollCalculationSnapshotID, ActionCode)
    WHERE PayrollCalculationSnapshotID IS NOT NULL;

CREATE UNIQUE INDEX ux_PayrollPeriodWorkflowActionEvidence_Decision_Action
    ON payroll.PayrollPeriodWorkflowActionEvidence
        (ReviewDecisionID, ActionCode)
    WHERE ReviewDecisionID IS NOT NULL;

CREATE UNIQUE INDEX ux_PayrollPeriodWorkflowActionEvidence_Finalized
    ON payroll.PayrollPeriodWorkflowActionEvidence
        (PayrollPeriodID, ActionCode)
    WHERE ActionCode = 'FINALIZED';

CREATE INDEX ix_PayrollPeriodWorkflowActionEvidence_Period_ActionAt
    ON payroll.PayrollPeriodWorkflowActionEvidence
        (CompanyID, BranchID, PayrollPeriodID, ActionAtUtc);


CREATE TABLE payroll.PayrollCalculationSnapshotUsedRateDefinitions (
    PayrollCalculationSnapshotUsedRateDefinitionID BIGSERIAL     PRIMARY KEY,
    PayrollCalculationSnapshotID                   BIGINT        NOT NULL,
    CompanyID                                      INTEGER       NOT NULL,
    BranchID                                       INTEGER       NOT NULL,
    PayrollPeriodID                                INTEGER       NOT NULL,
    DriverID                                       INTEGER       NOT NULL,
    DefinitionFingerprint                          VARCHAR(64)   NOT NULL,
    EvidenceKind                                   VARCHAR(30)   NOT NULL,
    SourceTypeSnapshot                             VARCHAR(30)   NOT NULL,
    PayItemID                                      INTEGER,
    RateTypeID                                     INTEGER,
    DriverRateID                                   INTEGER,
    DriverPayRuleID                                INTEGER,
    RateBehaviorSnapshot                           VARCHAR(30),
    RateTypeCodeSnapshot                           VARCHAR(50),
    RateTypeNameSnapshot                           VARCHAR(200),
    UnitNameSnapshot                               VARCHAR(100),
    RateAmountSnapshot                             NUMERIC(18,4),
    EffectiveFromSnapshot                          DATE,
    EffectiveToSnapshot                            DATE,
    RateStatusSnapshot                             VARCHAR(30),
    BlockSizeSnapshot                              NUMERIC(18,4),
    RoundingRuleSnapshot                           VARCHAR(50),
    RuleTypeSnapshot                               VARCHAR(30),
    RuleAmountSnapshot                             NUMERIC(18,4),
    RuleStatusSnapshot                             VARCHAR(30),
    CreatedAtUtc                                   TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_PayrollCalculationSnapshotUsedRateDefinitions_Fingerprint
        CHECK (DefinitionFingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_PayrollCalculationSnapshotUsedRateDefinitions_Kind
        CHECK (
            (EvidenceKind = 'DriverRate'
                AND DriverRateID IS NOT NULL
                AND DriverPayRuleID IS NULL)
            OR
            (EvidenceKind = 'DriverPayRule'
                AND DriverPayRuleID IS NOT NULL
                AND DriverRateID IS NULL)
        ),
    CONSTRAINT fk_PayrollCalculationSnapshotUsedRateDefinitions_SnapshotScope
        FOREIGN KEY (PayrollCalculationSnapshotID, CompanyID, BranchID, PayrollPeriodID)
        REFERENCES payroll.PayrollCalculationSnapshots
            (PayrollCalculationSnapshotID, CompanyID, BranchID, PayrollPeriodID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollCalculationSnapshotUsedRateDefinitions_DriverScope
        FOREIGN KEY (DriverID, CompanyID, BranchID)
        REFERENCES core.Drivers (DriverID, CompanyID, BranchID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollCalculationSnapshotUsedRateDefinitions_PayItem
        FOREIGN KEY (PayItemID)
        REFERENCES payroll.PayItems (PayItemID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollCalculationSnapshotUsedRateDefinitions_RateType
        FOREIGN KEY (RateTypeID)
        REFERENCES payroll.RateTypes (RateTypeID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollCalculationSnapshotUsedRateDefinitions_DriverRate
        FOREIGN KEY (DriverRateID)
        REFERENCES payroll.DriverRates (DriverRateID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollCalculationSnapshotUsedRateDefinitions_DriverPayRule
        FOREIGN KEY (DriverPayRuleID)
        REFERENCES payroll.DriverPayRules (DriverPayRuleID)
        ON DELETE RESTRICT,
    CONSTRAINT uq_PayrollCalculationSnapshotUsedRateDefinitions_Snapshot_Fingerprint
        UNIQUE (PayrollCalculationSnapshotID, DefinitionFingerprint)
);

CREATE INDEX ix_PayrollCalculationSnapshotUsedRateDefinitions_Snapshot
    ON payroll.PayrollCalculationSnapshotUsedRateDefinitions
        (PayrollCalculationSnapshotID, DriverID, EvidenceKind);


ALTER TABLE payroll.PayrollCalculationSnapshotLines
    ADD COLUMN UsedRateDefinitionID BIGINT;

ALTER TABLE payroll.PayrollCalculationSnapshotLines
    ADD CONSTRAINT fk_PayrollCalculationSnapshotLines_UsedRateDefinition
    FOREIGN KEY (UsedRateDefinitionID)
    REFERENCES payroll.PayrollCalculationSnapshotUsedRateDefinitions
        (PayrollCalculationSnapshotUsedRateDefinitionID)
    ON DELETE RESTRICT;

CREATE INDEX ix_PayrollCalculationSnapshotLines_UsedRateDefinition
    ON payroll.PayrollCalculationSnapshotLines (UsedRateDefinitionID)
    WHERE UsedRateDefinitionID IS NOT NULL;


CREATE OR REPLACE FUNCTION payroll.fn_guard_workflow_action_evidence_scope()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_company_id INTEGER;
    v_branch_id INTEGER;
    v_period_id INTEGER;
    v_decision_item_id BIGINT;
    v_decision_actor_id INTEGER;
BEGIN
    SELECT companyid, branchid
    INTO v_company_id, v_branch_id
    FROM payroll.payrollperiods
    WHERE payrollperiodid = NEW.payrollperiodid;

    IF v_company_id IS DISTINCT FROM NEW.companyid
       OR v_branch_id IS DISTINCT FROM NEW.branchid THEN
        RAISE EXCEPTION
            'workflow_action_evidence_scope: period scope does not match evidence scope'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.payrollcalculationsnapshotid IS NOT NULL THEN
        SELECT companyid, branchid, payrollperiodid
        INTO v_company_id, v_branch_id, v_period_id
        FROM payroll.payrollcalculationsnapshots
        WHERE payrollcalculationsnapshotid = NEW.payrollcalculationsnapshotid;

        IF v_company_id IS DISTINCT FROM NEW.companyid
           OR v_branch_id IS DISTINCT FROM NEW.branchid
           OR v_period_id IS DISTINCT FROM NEW.payrollperiodid THEN
            RAISE EXCEPTION
                'workflow_action_evidence_scope: snapshot scope does not match evidence scope'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    IF NEW.reviewitemid IS NOT NULL THEN
        SELECT companyid, branchid
        INTO v_company_id, v_branch_id
        FROM review.managerreviewitems
        WHERE reviewitemid = NEW.reviewitemid;

        IF v_company_id IS DISTINCT FROM NEW.companyid
           OR v_branch_id IS DISTINCT FROM NEW.branchid THEN
            RAISE EXCEPTION
                'workflow_action_evidence_scope: review item scope does not match evidence scope'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    IF NEW.reviewdecisionid IS NOT NULL THEN
        SELECT reviewitemid, decidedbyuserid
        INTO v_decision_item_id, v_decision_actor_id
        FROM review.managerreviewdecisions
        WHERE reviewdecisionid = NEW.reviewdecisionid;

        IF v_decision_item_id IS DISTINCT FROM NEW.reviewitemid
           OR v_decision_actor_id IS DISTINCT FROM NEW.actoruserid THEN
            RAISE EXCEPTION
                'workflow_action_evidence_scope: review decision does not match evidence identity'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION payroll.fn_guard_used_rate_definition_scope()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_company_id INTEGER;
    v_branch_id INTEGER;
    v_period_id INTEGER;
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
        SELECT companyid, branchid, driverid, ratetypeid
        INTO v_company_id, v_branch_id, v_driver_id, v_rate_type_id
        FROM payroll.driverrates
        WHERE driverrateid = NEW.driverrateid;

        IF v_company_id IS DISTINCT FROM NEW.companyid
           OR v_branch_id IS DISTINCT FROM NEW.branchid
           OR v_driver_id IS DISTINCT FROM NEW.driverid
           OR v_rate_type_id IS DISTINCT FROM NEW.ratetypeid THEN
            RAISE EXCEPTION
                'used_rate_definition_scope: driver rate does not match definition scope'
                USING ERRCODE = 'check_violation';
        END IF;
    ELSE
        SELECT companyid, branchid, driverid
        INTO v_company_id, v_branch_id, v_driver_id
        FROM payroll.driverpayrules
        WHERE driverpayruleid = NEW.driverpayruleid;

        IF v_company_id IS DISTINCT FROM NEW.companyid
           OR v_branch_id IS DISTINCT FROM NEW.branchid
           OR v_driver_id IS DISTINCT FROM NEW.driverid THEN
            RAISE EXCEPTION
                'used_rate_definition_scope: driver pay rule does not match definition scope'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION payroll.fn_guard_snapshot_line_used_rate_definition_scope()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_line_snapshot_id BIGINT;
    v_definition_snapshot_id BIGINT;
BEGIN
    IF NEW.usedratedefinitionid IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT payrollcalculationsnapshotid
    INTO v_line_snapshot_id
    FROM payroll.payrollcalculationdrivertotals
    WHERE payrollcalculationdrivertotalid = NEW.payrollcalculationdrivertotalid;

    SELECT payrollcalculationsnapshotid
    INTO v_definition_snapshot_id
    FROM payroll.payrollcalculationsnapshotusedratedefinitions
    WHERE payrollcalculationsnapshotusedratedefinitionid = NEW.usedratedefinitionid;

    IF v_line_snapshot_id IS DISTINCT FROM v_definition_snapshot_id THEN
        RAISE EXCEPTION
            'snapshot_line_used_rate_definition_scope: definition belongs to another snapshot'
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_PayrollPeriodWorkflowActionEvidence_Scope
    BEFORE INSERT ON payroll.PayrollPeriodWorkflowActionEvidence
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_workflow_action_evidence_scope();

CREATE TRIGGER trg_PayrollCalculationSnapshotUsedRateDefinitions_Scope
    BEFORE INSERT ON payroll.PayrollCalculationSnapshotUsedRateDefinitions
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_used_rate_definition_scope();

CREATE TRIGGER trg_PayrollCalculationSnapshotLines_UsedRateDefinitionScope
    BEFORE INSERT OR UPDATE OF UsedRateDefinitionID
    ON payroll.PayrollCalculationSnapshotLines
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_snapshot_line_used_rate_definition_scope();

CREATE TRIGGER trg_PayrollPeriodWorkflowActionEvidence_Immutable
    BEFORE UPDATE OR DELETE ON payroll.PayrollPeriodWorkflowActionEvidence
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_calculation_snapshot_immutable();

CREATE TRIGGER trg_PayrollCalculationSnapshotUsedRateDefinitions_Immutable
    BEFORE UPDATE OR DELETE ON payroll.PayrollCalculationSnapshotUsedRateDefinitions
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_calculation_snapshot_immutable();
