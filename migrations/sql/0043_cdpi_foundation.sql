-- Migration 0043: Custom Daily Pay Item (CDPI) schema foundation.
--
-- Adds three new tables to support the Custom Daily Pay Item request
-- workflow. No existing tables are modified. The legacy table
-- payroll.CustomPayItemRequests is unchanged.
--
-- UUID strategy: UUIDv4 via gen_random_uuid() built into PostgreSQL 17,
-- no extension required. Every identifier is also protected by a
-- PRIMARY KEY or UNIQUE constraint.
--
-- Tables added:
--   payroll.CdpiRequests       - request header (Draft -> Approved lifecycle)
--   payroll.CdpiRequestEvents  - append-only audit/event log
--   payroll.CdpiDefinitions    - approved-definition marker (1-to-1 with PayItems)
--
-- Lifecycle statuses (CdpiRequests.Status):
--   Draft -> PendingCompanyApproval -> Approved
--                                   -> Rejected
--   Rejected -> Draft  (via CopiedFromRejected; new RequestID issued)
--   PendingCompanyApproval -> Draft (ReturnedToDraft)
--
-- NOTE: The append-only trigger for CdpiRequestEvents is created by the
-- Alembic Python migration (0043_cdpi_foundation.py) and not included here.
-- This avoids PL/pgSQL dollar-quote conflicts with the SQL-statement splitter
-- used by both the Alembic wrapper and the test conftest.


-- ---------------------------------------------------------------------------
-- 1. payroll.CdpiRequests -- request header
-- ---------------------------------------------------------------------------

CREATE TABLE payroll.CdpiRequests (
    RequestID              UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    CompanyID              INTEGER       NOT NULL,
    RequestingBranchID     INTEGER       NOT NULL,

    -- Request content (nullable: Draft requests may be incomplete)
    ItemName               VARCHAR(200),
    InputType              VARCHAR(10),
    Unit                   VARCHAR(50),
    CalcMethodKey          VARCHAR(30),
    Notes                  TEXT,

    -- Workflow state
    Status                 VARCHAR(30)   NOT NULL DEFAULT 'Draft',
    Revision               INTEGER       NOT NULL DEFAULT 1,

    -- Submission tracking (set when first submitted or resubmitted)
    SubmittedByUserID      INTEGER,
    SubmittedAtUtc         TIMESTAMPTZ,

    -- Approval outcome (NULL until Approved)
    ApprovedPayItemID      INTEGER,

    -- Audit
    CreatedByUserID        INTEGER,
    CreatedAtUtc           TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UpdatedByUserID        INTEGER,
    UpdatedAtUtc           TIMESTAMPTZ,

    CONSTRAINT ck_CdpiRequests_Status
        CHECK (Status IN ('Draft', 'PendingCompanyApproval', 'Rejected', 'Approved')),

    CONSTRAINT ck_CdpiRequests_InputType
        CHECK (InputType IN ('Time', 'Number')),

    CONSTRAINT ck_CdpiRequests_CalcMethodKey
        CHECK (CalcMethodKey IN ('PerUnit', 'OrdinalTier', 'RangeBracket', 'RangeProgressive', 'Block')),

    -- One approved PayItem can originate from at most one request.
    -- NULL values are excluded from uniqueness (PostgreSQL NULL != NULL),
    -- so non-approved requests do not conflict.
    CONSTRAINT ux_CdpiRequests_ApprovedPayItemID
        UNIQUE (ApprovedPayItemID),

    CONSTRAINT fk_CdpiRequests_Company
        FOREIGN KEY (CompanyID)           REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_CdpiRequests_Branch
        FOREIGN KEY (RequestingBranchID)  REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_CdpiRequests_SubmittedBy
        FOREIGN KEY (SubmittedByUserID)   REFERENCES sec.Users(UserID),
    CONSTRAINT fk_CdpiRequests_ApprovedItem
        FOREIGN KEY (ApprovedPayItemID)   REFERENCES payroll.PayItems(PayItemID),
    CONSTRAINT fk_CdpiRequests_CreatedBy
        FOREIGN KEY (CreatedByUserID)     REFERENCES sec.Users(UserID),
    CONSTRAINT fk_CdpiRequests_UpdatedBy
        FOREIGN KEY (UpdatedByUserID)     REFERENCES sec.Users(UserID)
);

-- Company-scoped listing and status queues
CREATE INDEX ix_CdpiRequests_Company_Status
    ON payroll.CdpiRequests (CompanyID, Status);

-- Branch-scoped listing
CREATE INDEX ix_CdpiRequests_Branch_Status
    ON payroll.CdpiRequests (RequestingBranchID, Status);

-- Fast queue for the company reviewer
CREATE INDEX ix_CdpiRequests_PendingApproval
    ON payroll.CdpiRequests (CompanyID, CreatedAtUtc)
    WHERE Status = 'PendingCompanyApproval';


-- ---------------------------------------------------------------------------
-- 2. payroll.CdpiRequestEvents -- append-only event/audit log
--
-- Every state transition is recorded here. Rows are never updated or deleted;
-- the trigger in the Python migration enforces this.
-- ---------------------------------------------------------------------------

CREATE TABLE payroll.CdpiRequestEvents (
    EventID              UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    RequestID            UUID          NOT NULL,
    EventType            VARCHAR(50)   NOT NULL,
    FromStatus           VARCHAR(30),
    ToStatus             VARCHAR(30)   NOT NULL,
    ActorUserID          INTEGER       NOT NULL,
    Reason               TEXT,
    RequestRevision      INTEGER       NOT NULL,
    OccurredAtUtc        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    EventData            JSONB,

    CONSTRAINT ck_CdpiRequestEvents_EventType
        CHECK (EventType IN (
            'DraftCreated', 'Submitted', 'Resubmitted',
            'ReturnedToDraft', 'Rejected', 'Approved', 'CopiedFromRejected'
        )),

    CONSTRAINT ck_CdpiRequestEvents_ToStatus
        CHECK (ToStatus IN ('Draft', 'PendingCompanyApproval', 'Rejected', 'Approved')),

    CONSTRAINT fk_CdpiRequestEvents_Request
        FOREIGN KEY (RequestID)    REFERENCES payroll.CdpiRequests(RequestID)
        ON DELETE RESTRICT,

    CONSTRAINT fk_CdpiRequestEvents_Actor
        FOREIGN KEY (ActorUserID)  REFERENCES sec.Users(UserID)
);

-- Full history for a single request in chronological order
CREATE INDEX ix_CdpiRequestEvents_Request_Occurred
    ON payroll.CdpiRequestEvents (RequestID, OccurredAtUtc);


-- ---------------------------------------------------------------------------
-- 3. payroll.CdpiDefinitions -- approved-definition marker
--
-- Created once when a CdpiRequest is approved or via direct company create.
-- Does NOT duplicate canonical fields already in PayItems.
-- ---------------------------------------------------------------------------

CREATE TABLE payroll.CdpiDefinitions (
    PayItemID               INTEGER       PRIMARY KEY,
    DefinitionSchemaVersion INTEGER       NOT NULL DEFAULT 1,
    SourceRequestID         UUID,
    LockedAtUtc             TIMESTAMPTZ   NOT NULL,
    CreatedByUserID         INTEGER,
    CreatedAtUtc            TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    -- One request can produce at most one approved definition.
    -- NULL SourceRequestIDs (direct creates) are excluded from uniqueness.
    CONSTRAINT ux_CdpiDefinitions_SourceRequestID
        UNIQUE (SourceRequestID),

    CONSTRAINT fk_CdpiDefinitions_PayItem
        FOREIGN KEY (PayItemID)        REFERENCES payroll.PayItems(PayItemID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_CdpiDefinitions_SourceRequest
        FOREIGN KEY (SourceRequestID)  REFERENCES payroll.CdpiRequests(RequestID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_CdpiDefinitions_CreatedBy
        FOREIGN KEY (CreatedByUserID)  REFERENCES sec.Users(UserID)
);

-- Reverse lookup: given a SourceRequestID find its approved definition.
CREATE INDEX ix_CdpiDefinitions_SourceRequest
    ON payroll.CdpiDefinitions (SourceRequestID)
    WHERE SourceRequestID IS NOT NULL;