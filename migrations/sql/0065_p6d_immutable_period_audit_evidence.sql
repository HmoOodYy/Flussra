-- =============================================================================
-- Migration 0065: P6D immutable period audit evidence
--
-- Generic audit.AuditLog is not a complete, immutable period-history source.
-- This migration adds the narrow append-only evidence required by P6D without
-- changing generic audit or financial-snapshot contracts.
-- =============================================================================

CREATE TABLE payroll.PayrollPeriodAuditEvidenceCoverage (
    PayrollPeriodAuditEvidenceCoverageID BIGSERIAL PRIMARY KEY,
    CompanyID                            INTEGER NOT NULL,
    BranchID                             INTEGER NOT NULL,
    PayrollPeriodID                      INTEGER NOT NULL,
    EvidenceDomain                       VARCHAR(30) NOT NULL,
    EvidenceVersion                      INTEGER NOT NULL DEFAULT 1,
    CoverageState                        VARCHAR(20) NOT NULL,
    CaptureStartedAtUtc                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_PayrollPeriodAuditEvidenceCoverage_Domain
        CHECK (EvidenceDomain IN ('SOURCE', 'STATUS_NOTE', 'BONUS', 'REVIEW_COMMENT')),
    CONSTRAINT ck_PayrollPeriodAuditEvidenceCoverage_State
        CHECK (CoverageState IN ('COMPLETE', 'PARTIAL')),
    CONSTRAINT uq_PayrollPeriodAuditEvidenceCoverage_Period_Domain
        UNIQUE (PayrollPeriodID, EvidenceDomain),
    CONSTRAINT fk_PayrollPeriodAuditEvidenceCoverage_Period
        FOREIGN KEY (PayrollPeriodID)
        REFERENCES payroll.PayrollPeriods (PayrollPeriodID)
        ON DELETE RESTRICT
);

CREATE TABLE payroll.PayrollPeriodAuditEvidenceEvents (
    PayrollPeriodAuditEvidenceEventID BIGSERIAL PRIMARY KEY,
    CompanyID                         INTEGER NOT NULL,
    BranchID                          INTEGER NOT NULL,
    PayrollPeriodID                   INTEGER NOT NULL,
    EvidenceVersion                   INTEGER NOT NULL DEFAULT 1,
    EvidenceDomain                    VARCHAR(30) NOT NULL,
    ActionCode                        VARCHAR(50) NOT NULL,
    SourceEntityType                  VARCHAR(100) NOT NULL,
    SourceEntityID                    VARCHAR(100) NOT NULL,
    ReviewItemID                      BIGINT,
    DriverID                          INTEGER,
    WorkDate                          DATE,
    PayItemID                         INTEGER,
    BeforeStateJson                   JSONB,
    AfterStateJson                    JSONB,
    ActorUserID                       INTEGER NOT NULL,
    ActorDisplayNameSnapshot          VARCHAR(200) NOT NULL,
    ResponsibilityContextSnapshot     JSONB NOT NULL,
    ReasonSnapshot                    TEXT,
    CorrelationID                     UUID,
    SourceRevision                    BIGINT,
    OccurredAtUtc                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_PayrollPeriodAuditEvidenceEvents_Domain
        CHECK (EvidenceDomain IN ('SOURCE', 'STATUS_NOTE', 'BONUS', 'REVIEW_COMMENT')),
    CONSTRAINT ck_PayrollPeriodAuditEvidenceEvents_Action
        CHECK (ActionCode IN (
            'SOURCE_CREATED', 'SOURCE_UPDATED', 'SOURCE_VOIDED',
            'STATUS_SET', 'STATUS_CHANGED', 'STATUS_CLEARED',
            'NOTE_SET', 'NOTE_CHANGED', 'NOTE_CLEARED',
            'BONUS_CREATED', 'BONUS_UPDATED', 'BONUS_VOIDED',
            'REVIEW_COMMENT_ADDED'
        )),
    CONSTRAINT ck_PayrollPeriodAuditEvidenceEvents_Context
        CHECK (jsonb_typeof(ResponsibilityContextSnapshot) = 'object'),
    CONSTRAINT ck_PayrollPeriodAuditEvidenceEvents_ReviewItem
        CHECK (
            (ActionCode = 'REVIEW_COMMENT_ADDED' AND ReviewItemID IS NOT NULL)
            OR (ActionCode <> 'REVIEW_COMMENT_ADDED' AND ReviewItemID IS NULL)
        ),
    CONSTRAINT fk_PayrollPeriodAuditEvidenceEvents_Period
        FOREIGN KEY (PayrollPeriodID)
        REFERENCES payroll.PayrollPeriods (PayrollPeriodID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollPeriodAuditEvidenceEvents_Actor
        FOREIGN KEY (ActorUserID)
        REFERENCES sec.Users (UserID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollPeriodAuditEvidenceEvents_ReviewItemScope
        FOREIGN KEY (ReviewItemID, CompanyID, BranchID)
        REFERENCES review.ManagerReviewItems (ReviewItemID, CompanyID, BranchID)
        ON DELETE RESTRICT
);

CREATE TABLE payroll.PayrollPeriodAuditEvidenceSnapshotEvents (
    PayrollPeriodAuditEvidenceSnapshotEventID BIGSERIAL PRIMARY KEY,
    PayrollPeriodAuditEvidenceEventID         BIGINT NOT NULL,
    PayrollCalculationSnapshotID               BIGINT NOT NULL,
    CompanyID                                  INTEGER NOT NULL,
    BranchID                                   INTEGER NOT NULL,
    PayrollPeriodID                            INTEGER NOT NULL,
    LinkedAtUtc                                TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_PayrollPeriodAuditEvidenceSnapshotEvents_Event
        UNIQUE (PayrollPeriodAuditEvidenceEventID),
    CONSTRAINT fk_PayrollPeriodAuditEvidenceSnapshotEvents_Event
        FOREIGN KEY (PayrollPeriodAuditEvidenceEventID)
        REFERENCES payroll.PayrollPeriodAuditEvidenceEvents (PayrollPeriodAuditEvidenceEventID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollPeriodAuditEvidenceSnapshotEvents_SnapshotScope
        FOREIGN KEY (PayrollCalculationSnapshotID, CompanyID, BranchID, PayrollPeriodID)
        REFERENCES payroll.PayrollCalculationSnapshots
            (PayrollCalculationSnapshotID, CompanyID, BranchID, PayrollPeriodID)
        ON DELETE RESTRICT
);

CREATE INDEX ix_PayrollPeriodAuditEvidenceEvents_Period_Chronology
    ON payroll.PayrollPeriodAuditEvidenceEvents
        (CompanyID, BranchID, PayrollPeriodID, OccurredAtUtc, PayrollPeriodAuditEvidenceEventID);

CREATE INDEX ix_PayrollPeriodAuditEvidenceSnapshotEvents_Snapshot
    ON payroll.PayrollPeriodAuditEvidenceSnapshotEvents
        (PayrollCalculationSnapshotID, PayrollPeriodAuditEvidenceEventID);


CREATE OR REPLACE FUNCTION payroll.fn_guard_period_audit_evidence_scope()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_company_id INTEGER;
    v_branch_id INTEGER;
BEGIN
    SELECT CompanyID, BranchID INTO v_company_id, v_branch_id
    FROM payroll.PayrollPeriods
    WHERE PayrollPeriodID = NEW.PayrollPeriodID;

    IF v_company_id IS DISTINCT FROM NEW.CompanyID
       OR v_branch_id IS DISTINCT FROM NEW.BranchID THEN
        RAISE EXCEPTION 'period_audit_evidence_scope: period scope does not match evidence scope'
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION payroll.fn_guard_period_audit_evidence_event_scope()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_company_id INTEGER;
    v_branch_id INTEGER;
    v_review_company_id INTEGER;
    v_review_branch_id INTEGER;
    v_review_entity_id VARCHAR(100);
    v_review_type VARCHAR(50);
    v_review_snapshot_id BIGINT;
BEGIN
    SELECT CompanyID, BranchID INTO v_company_id, v_branch_id
    FROM payroll.PayrollPeriods
    WHERE PayrollPeriodID = NEW.PayrollPeriodID;

    IF v_company_id IS DISTINCT FROM NEW.CompanyID
       OR v_branch_id IS DISTINCT FROM NEW.BranchID THEN
        RAISE EXCEPTION 'period_audit_evidence_scope: period scope does not match evidence scope'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.ActionCode = 'REVIEW_COMMENT_ADDED' THEN
        SELECT CompanyID, BranchID, EntityID, RequestType, PayrollCalculationSnapshotID
        INTO v_review_company_id, v_review_branch_id, v_review_entity_id,
             v_review_type, v_review_snapshot_id
        FROM review.ManagerReviewItems
        WHERE ReviewItemID = NEW.ReviewItemID;

        IF NOT FOUND
           OR v_review_company_id IS DISTINCT FROM NEW.CompanyID
           OR v_review_branch_id IS DISTINCT FROM NEW.BranchID
           OR v_review_type IS DISTINCT FROM 'PeriodApproval'
           OR v_review_entity_id IS DISTINCT FROM NEW.PayrollPeriodID::VARCHAR
           OR v_review_snapshot_id IS NULL THEN
            RAISE EXCEPTION 'period_audit_evidence_scope: review comment does not match a snapshot-backed period approval'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION payroll.fn_guard_period_audit_evidence_membership_scope()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_company_id INTEGER;
    v_branch_id INTEGER;
    v_period_id INTEGER;
    v_review_snapshot_id BIGINT;
    v_review_entity_id VARCHAR(100);
    v_review_type VARCHAR(50);
BEGIN
    SELECT CompanyID, BranchID, PayrollPeriodID
    INTO v_company_id, v_branch_id, v_period_id
    FROM payroll.PayrollPeriodAuditEvidenceEvents
    WHERE PayrollPeriodAuditEvidenceEventID = NEW.PayrollPeriodAuditEvidenceEventID;

    IF v_company_id IS DISTINCT FROM NEW.CompanyID
       OR v_branch_id IS DISTINCT FROM NEW.BranchID
       OR v_period_id IS DISTINCT FROM NEW.PayrollPeriodID THEN
        RAISE EXCEPTION 'period_audit_evidence_membership_scope: event scope does not match membership scope'
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT e.reviewitemid INTO v_review_snapshot_id
    FROM payroll.PayrollPeriodAuditEvidenceEvents e
    WHERE e.PayrollPeriodAuditEvidenceEventID = NEW.PayrollPeriodAuditEvidenceEventID;

    IF v_review_snapshot_id IS NOT NULL THEN
        SELECT PayrollCalculationSnapshotID, EntityID, RequestType
        INTO v_review_snapshot_id, v_review_entity_id, v_review_type
        FROM review.ManagerReviewItems
        WHERE ReviewItemID = (
            SELECT ReviewItemID
            FROM payroll.PayrollPeriodAuditEvidenceEvents
            WHERE PayrollPeriodAuditEvidenceEventID = NEW.PayrollPeriodAuditEvidenceEventID
        );

        IF v_review_snapshot_id IS DISTINCT FROM NEW.PayrollCalculationSnapshotID
           OR v_review_entity_id IS DISTINCT FROM NEW.PayrollPeriodID::VARCHAR
           OR v_review_type IS DISTINCT FROM 'PeriodApproval' THEN
            RAISE EXCEPTION 'period_audit_evidence_membership_scope: review item does not match snapshot period scope'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;


CREATE OR REPLACE FUNCTION payroll.fn_guard_period_audit_evidence_review_membership()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.ActionCode = 'REVIEW_COMMENT_ADDED'
       AND NOT EXISTS (
           SELECT 1
           FROM payroll.PayrollPeriodAuditEvidenceSnapshotEvents m
           WHERE m.PayrollPeriodAuditEvidenceEventID = NEW.PayrollPeriodAuditEvidenceEventID
       ) THEN
        RAISE EXCEPTION 'period_audit_evidence_review_membership: review comment requires exact snapshot membership'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$;


CREATE OR REPLACE FUNCTION payroll.fn_guard_period_delete_with_audit_evidence()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM payroll.PayrollCalculationSnapshots s
        WHERE s.PayrollPeriodID = OLD.PayrollPeriodID
    ) THEN
        RAISE EXCEPTION 'payroll_period_delete: snapshot-backed periods cannot be deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM payroll.PayrollPeriodAuditEvidenceCoverage c
        WHERE c.PayrollPeriodID = OLD.PayrollPeriodID
    ) OR EXISTS (
        SELECT 1
        FROM payroll.PayrollPeriodAuditEvidenceEvents e
        WHERE e.PayrollPeriodID = OLD.PayrollPeriodID
    ) THEN
        RAISE EXCEPTION 'payroll_period_delete: periods with P6D audit evidence cannot be deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN OLD;
END;
$$;


CREATE OR REPLACE FUNCTION payroll.fn_guard_period_audit_evidence_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'payroll_period_audit_evidence_immutable: % on %.% is forbidden once written.',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER trg_PayrollPeriodAuditEvidenceCoverage_Scope
    BEFORE INSERT ON payroll.PayrollPeriodAuditEvidenceCoverage
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_period_audit_evidence_scope();

CREATE TRIGGER trg_PayrollPeriodAuditEvidenceEvents_Scope
    BEFORE INSERT ON payroll.PayrollPeriodAuditEvidenceEvents
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_period_audit_evidence_event_scope();

CREATE TRIGGER trg_PayrollPeriodAuditEvidenceSnapshotEvents_Scope
    BEFORE INSERT ON payroll.PayrollPeriodAuditEvidenceSnapshotEvents
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_period_audit_evidence_membership_scope();

CREATE CONSTRAINT TRIGGER trg_PayrollPeriodAuditEvidenceEvents_ReviewMembership
    AFTER INSERT ON payroll.PayrollPeriodAuditEvidenceEvents
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_period_audit_evidence_review_membership();

CREATE TRIGGER trg_PayrollPeriods_AuditEvidenceDelete
    BEFORE DELETE ON payroll.PayrollPeriods
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_period_delete_with_audit_evidence();

CREATE TRIGGER trg_PayrollPeriodAuditEvidenceCoverage_Immutable
    BEFORE UPDATE OR DELETE ON payroll.PayrollPeriodAuditEvidenceCoverage
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_period_audit_evidence_immutable();

CREATE TRIGGER trg_PayrollPeriodAuditEvidenceEvents_Immutable
    BEFORE UPDATE OR DELETE ON payroll.PayrollPeriodAuditEvidenceEvents
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_period_audit_evidence_immutable();

CREATE TRIGGER trg_PayrollPeriodAuditEvidenceSnapshotEvents_Immutable
    BEFORE UPDATE OR DELETE ON payroll.PayrollPeriodAuditEvidenceSnapshotEvents
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_period_audit_evidence_immutable();
