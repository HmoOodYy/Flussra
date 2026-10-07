-- 0083: Generic PayDefinition governance and provenance.
--
-- Dormant: legacy PayItems, CDPI and BranchPayItemConfig remain the only
-- operational authority. These tables record how a target PayDefinition came
-- to exist; they do not make it operational and carry no Branch applicability.
--
--   PayDefinitionRequests       Branch-scoped request lifecycle
--   PayDefinitionRequestEvents  append-only request history
--   PayDefinitionProvenance     one immutable record per governed PayDefinition
--
-- Structure-lock provenance is the canonical RateDefinitions.StructureLockedAtUtc.
-- Legacy CdpiDefinitions is not a source or a backfill target.

-- ---------------------------------------------------------------------------
-- 1. Requests
-- ---------------------------------------------------------------------------
CREATE TABLE payroll.PayDefinitionRequests (
    PayDefinitionRequestID  UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    CompanyID               INTEGER       NOT NULL,
    RequestingBranchID      INTEGER       NOT NULL,
    DefinitionCode          VARCHAR(50),
    DefinitionName          VARCHAR(200),
    InputType               VARCHAR(30),
    Unit                    VARCHAR(50),
    CalculationMethod       VARCHAR(30),
    Notes                   TEXT,
    Status                  VARCHAR(30)   NOT NULL DEFAULT 'Draft',
    Revision                INTEGER       NOT NULL DEFAULT 1,
    SubmittedByUserID       INTEGER,
    SubmittedAtUtc          TIMESTAMPTZ,
    ApprovedPayDefinitionID INTEGER,
    CopiedFromRequestID     UUID,
    CreatedByUserID         INTEGER       NOT NULL,
    CreatedAtUtc            TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UpdatedByUserID         INTEGER,
    UpdatedAtUtc            TIMESTAMPTZ,

    CONSTRAINT fk_PayDefinitionRequests_Company FOREIGN KEY (CompanyID)
        REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_PayDefinitionRequests_Branch_Company
        FOREIGN KEY (RequestingBranchID, CompanyID)
        REFERENCES core.Branches (BranchID, CompanyID),
    CONSTRAINT fk_PayDefinitionRequests_ApprovedDefinition_Company
        FOREIGN KEY (ApprovedPayDefinitionID, CompanyID)
        REFERENCES payroll.PayDefinitions (PayDefinitionID, CompanyID),
    CONSTRAINT fk_PayDefinitionRequests_CopiedFrom_Company
        FOREIGN KEY (CopiedFromRequestID, CompanyID)
        REFERENCES payroll.PayDefinitionRequests (PayDefinitionRequestID, CompanyID),
    CONSTRAINT fk_PayDefinitionRequests_Creator FOREIGN KEY (CreatedByUserID)
        REFERENCES sec.Users(UserID),
    CONSTRAINT fk_PayDefinitionRequests_Updater FOREIGN KEY (UpdatedByUserID)
        REFERENCES sec.Users(UserID),
    CONSTRAINT fk_PayDefinitionRequests_Submitter FOREIGN KEY (SubmittedByUserID)
        REFERENCES sec.Users(UserID),
    CONSTRAINT uq_PayDefinitionRequests_ID_Company
        UNIQUE (PayDefinitionRequestID, CompanyID),
    CONSTRAINT uq_PayDefinitionRequests_ApprovedDefinition
        UNIQUE (ApprovedPayDefinitionID),
    CONSTRAINT ck_PayDefinitionRequests_Status
        CHECK (Status IN ('Draft', 'PendingCompanyApproval', 'Rejected', 'Approved')),
    CONSTRAINT ck_PayDefinitionRequests_InputType
        CHECK (InputType IS NULL OR InputType IN ('Decimal', 'WholeNumber')),
    CONSTRAINT ck_PayDefinitionRequests_Method
        CHECK (CalculationMethod IS NULL OR CalculationMethod IN ('PerUnit', 'OrdinalTier')),
    CONSTRAINT ck_PayDefinitionRequests_Revision CHECK (Revision >= 1),
    CONSTRAINT ck_PayDefinitionRequests_ApprovalLink
        CHECK ((Status = 'Approved') = (ApprovedPayDefinitionID IS NOT NULL)),
    CONSTRAINT ck_PayDefinitionRequests_NotSelfCopy
        CHECK (CopiedFromRequestID IS NULL OR CopiedFromRequestID <> PayDefinitionRequestID)
);

CREATE INDEX ix_PayDefinitionRequests_Company_Status
    ON payroll.PayDefinitionRequests (CompanyID, Status);

CREATE INDEX ix_PayDefinitionRequests_Branch_Status
    ON payroll.PayDefinitionRequests (RequestingBranchID, Status);

CREATE FUNCTION payroll.trg_PayDefinitionRequests_Guard()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.CompanyID IS DISTINCT FROM OLD.CompanyID
       OR NEW.RequestingBranchID IS DISTINCT FROM OLD.RequestingBranchID
       OR NEW.CreatedByUserID IS DISTINCT FROM OLD.CreatedByUserID
       OR NEW.CopiedFromRequestID IS DISTINCT FROM OLD.CopiedFromRequestID THEN
        RAISE EXCEPTION 'PAY_DEFINITION_REQUEST_IDENTITY_IMMUTABLE'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.Status IN ('Approved', 'Rejected') THEN
        RAISE EXCEPTION 'PAY_DEFINITION_REQUEST_TERMINAL'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('Status=%s', OLD.Status);
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_PayDefinitionRequests_Guard
BEFORE UPDATE ON payroll.PayDefinitionRequests
FOR EACH ROW EXECUTE FUNCTION payroll.trg_PayDefinitionRequests_Guard();

-- ---------------------------------------------------------------------------
-- 2. Append-only request history
-- ---------------------------------------------------------------------------
CREATE TABLE payroll.PayDefinitionRequestEvents (
    EventID          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    PayDefinitionRequestID UUID   NOT NULL,
    EventType        VARCHAR(50)  NOT NULL,
    FromStatus       VARCHAR(30),
    ToStatus         VARCHAR(30)  NOT NULL,
    ActorUserID      INTEGER      NOT NULL,
    Reason           TEXT,
    RequestRevision  INTEGER      NOT NULL,
    OccurredAtUtc    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_PayDefinitionRequestEvents_Request
        FOREIGN KEY (PayDefinitionRequestID)
        REFERENCES payroll.PayDefinitionRequests (PayDefinitionRequestID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayDefinitionRequestEvents_Actor FOREIGN KEY (ActorUserID)
        REFERENCES sec.Users(UserID),
    CONSTRAINT ck_PayDefinitionRequestEvents_EventType CHECK (EventType IN (
        'DraftCreated', 'Submitted', 'Resubmitted', 'ReturnedToDraft',
        'Rejected', 'Approved', 'CopiedFromRejected')),
    CONSTRAINT ck_PayDefinitionRequestEvents_FromStatus
        CHECK (FromStatus IS NULL
               OR FromStatus IN ('Draft', 'PendingCompanyApproval', 'Rejected', 'Approved')),
    CONSTRAINT ck_PayDefinitionRequestEvents_ToStatus
        CHECK (ToStatus IN ('Draft', 'PendingCompanyApproval', 'Rejected', 'Approved'))
);

CREATE INDEX ix_PayDefinitionRequestEvents_Request_Occurred
    ON payroll.PayDefinitionRequestEvents (PayDefinitionRequestID, OccurredAtUtc);

CREATE FUNCTION payroll.trg_PayDefinitionRecords_AppendOnly()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'PAY_DEFINITION_HISTORY_APPEND_ONLY'
        USING ERRCODE = 'check_violation',
              DETAIL = format('%s on %s.%s', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME);
END
$$;

CREATE TRIGGER trg_PayDefinitionRequestEvents_AppendOnly
BEFORE UPDATE OR DELETE ON payroll.PayDefinitionRequestEvents
FOR EACH ROW EXECUTE FUNCTION payroll.trg_PayDefinitionRecords_AppendOnly();

-- ---------------------------------------------------------------------------
-- 3. Provenance: one immutable record per governed PayDefinition
-- ---------------------------------------------------------------------------
CREATE TABLE payroll.PayDefinitionProvenance (
    PayDefinitionID             INTEGER      PRIMARY KEY,
    CompanyID                   INTEGER      NOT NULL,
    CreationMode                VARCHAR(20)  NOT NULL,
    SourceRequestID             UUID,
    RequestingBranchID          INTEGER,
    CreatedByUserID             INTEGER      NOT NULL,
    SubmittedByUserID           INTEGER,
    SubmittedAtUtc              TIMESTAMPTZ,
    ApprovedByUserID            INTEGER,
    ApprovedAtUtc               TIMESTAMPTZ,
    GovernanceSchemaVersion     INTEGER      NOT NULL DEFAULT 1,
    CalculationMethodVersion    INTEGER      NOT NULL DEFAULT 1,
    CreatedAtUtc                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_PayDefinitionProvenance_PayDefinition_Company
        FOREIGN KEY (PayDefinitionID, CompanyID)
        REFERENCES payroll.PayDefinitions (PayDefinitionID, CompanyID),
    CONSTRAINT fk_PayDefinitionProvenance_Request_Company
        FOREIGN KEY (SourceRequestID, CompanyID)
        REFERENCES payroll.PayDefinitionRequests (PayDefinitionRequestID, CompanyID),
    CONSTRAINT fk_PayDefinitionProvenance_Branch_Company
        FOREIGN KEY (RequestingBranchID, CompanyID)
        REFERENCES core.Branches (BranchID, CompanyID),
    CONSTRAINT fk_PayDefinitionProvenance_Creator FOREIGN KEY (CreatedByUserID)
        REFERENCES sec.Users(UserID),
    CONSTRAINT fk_PayDefinitionProvenance_Submitter FOREIGN KEY (SubmittedByUserID)
        REFERENCES sec.Users(UserID),
    CONSTRAINT fk_PayDefinitionProvenance_Approver FOREIGN KEY (ApprovedByUserID)
        REFERENCES sec.Users(UserID),
    CONSTRAINT uq_PayDefinitionProvenance_SourceRequest UNIQUE (SourceRequestID),
    CONSTRAINT ck_PayDefinitionProvenance_Mode
        CHECK (CreationMode IN ('Request', 'DirectCreate')),
    CONSTRAINT ck_PayDefinitionProvenance_ModeFields CHECK (
        (CreationMode = 'Request'
            AND SourceRequestID IS NOT NULL AND RequestingBranchID IS NOT NULL
            AND SubmittedByUserID IS NOT NULL AND SubmittedAtUtc IS NOT NULL
            AND ApprovedByUserID IS NOT NULL AND ApprovedAtUtc IS NOT NULL)
        OR
        (CreationMode = 'DirectCreate'
            AND SourceRequestID IS NULL AND RequestingBranchID IS NULL
            AND SubmittedByUserID IS NULL AND SubmittedAtUtc IS NULL
            AND ApprovedByUserID IS NULL AND ApprovedAtUtc IS NULL)
    ),
    CONSTRAINT ck_PayDefinitionProvenance_Versions
        CHECK (GovernanceSchemaVersion >= 1 AND CalculationMethodVersion >= 1)
);

CREATE TRIGGER trg_PayDefinitionProvenance_Immutable
BEFORE UPDATE OR DELETE ON payroll.PayDefinitionProvenance
FOR EACH ROW EXECUTE FUNCTION payroll.trg_PayDefinitionRecords_AppendOnly();

-- A request-created definition must be the approved outcome of exactly the
-- request it names, from the same requesting Branch.
CREATE FUNCTION payroll.trg_PayDefinitionProvenance_RequestLink()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.CreationMode = 'Request' AND NOT EXISTS (
        SELECT 1
          FROM payroll.PayDefinitionRequests r
         WHERE r.PayDefinitionRequestID = NEW.SourceRequestID
           AND r.CompanyID = NEW.CompanyID
           AND r.Status = 'Approved'
           AND r.ApprovedPayDefinitionID = NEW.PayDefinitionID
           AND r.RequestingBranchID = NEW.RequestingBranchID
    ) THEN
        RAISE EXCEPTION 'PAY_DEFINITION_PROVENANCE_REQUEST_MISMATCH'
            USING ERRCODE = 'check_violation',
                  DETAIL = format('PayDefinitionID=%s', NEW.PayDefinitionID);
    END IF;
    RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER trg_PayDefinitionProvenance_RequestLink
AFTER INSERT ON payroll.PayDefinitionProvenance
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION payroll.trg_PayDefinitionProvenance_RequestLink();
