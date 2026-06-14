-- =============================================================================
-- Migration 0027: Driver Transfer Workflow
--
-- Allows one Employee to have multiple Driver profiles (one per branch) so
-- drivers can be transferred between branches without losing payroll history.
--
-- Changes:
--   1. Drop the old UNIQUE(EmployeeID) constraint on core.Drivers
--   2. Add lineage columns TransferredFromDriverID / TransferredToDriverID
--   3. Add partial UNIQUE index: one *active* driver per (EmployeeID, BranchID)
--   4. Create core.DriverTransferRequests workflow table
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Drop old unique constraint (one employee = one driver record, globally)
-- ---------------------------------------------------------------------------
ALTER TABLE core.Drivers DROP CONSTRAINT uq_Drivers_EmployeeID;

-- ---------------------------------------------------------------------------
-- 2. Add lineage columns
--    TransferredFromDriverID: the source-branch driver record this profile
--                             was created from (populated on completion).
--    TransferredToDriverID:   the new-branch driver record created when this
--                             profile was transferred away.
-- ---------------------------------------------------------------------------
ALTER TABLE core.Drivers
    ADD COLUMN TransferredFromDriverID INTEGER,
    ADD COLUMN TransferredToDriverID   INTEGER;

ALTER TABLE core.Drivers
    ADD CONSTRAINT fk_Drivers_TransferredFrom
        FOREIGN KEY (TransferredFromDriverID) REFERENCES core.Drivers(DriverID),
    ADD CONSTRAINT fk_Drivers_TransferredTo
        FOREIGN KEY (TransferredToDriverID)   REFERENCES core.Drivers(DriverID);

-- ---------------------------------------------------------------------------
-- 3. Partial unique index: at most one active profile per (Employee, Branch)
--    Allows the same employee to have a 'Transferred' old record AND a new
--    active record in the same branch (re-transfer-back scenario).
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX ux_Drivers_Employee_Branch_Active
    ON core.Drivers(EmployeeID, BranchID)
    WHERE DriverStatus NOT IN ('Transferred', 'Terminated');

-- ---------------------------------------------------------------------------
-- 4. Driver Transfer Requests workflow table
-- ---------------------------------------------------------------------------
CREATE TABLE core.DriverTransferRequests (
    TransferRequestID       SERIAL       PRIMARY KEY,
    CompanyID               INTEGER      NOT NULL,
    DriverID                INTEGER      NOT NULL,   -- source branch driver record
    SourceBranchID          INTEGER      NOT NULL,
    TargetBranchID          INTEGER      NOT NULL,
    RequestedByUserID       INTEGER      NOT NULL,
    InitiatedBy             VARCHAR(20)  NOT NULL,   -- 'Driver' | 'SourceBranch'

    -- Workflow status
    Status                  VARCHAR(30)  NOT NULL DEFAULT 'PendingSourceApproval',

    EffectiveDate           DATE         NOT NULL,
    Reason                  TEXT,
    Notes                   TEXT,

    -- Source branch approval
    SourceApprovedByUserID  INTEGER,
    SourceApprovedAtUtc     TIMESTAMPTZ,

    -- Target branch decision
    TargetDecidedByUserID   INTEGER,
    TargetDecidedAtUtc      TIMESTAMPTZ,
    TargetDecisionNotes     TEXT,

    -- Completion (new profile created)
    NewDriverID             INTEGER,     -- populated when Completed
    CompletedAtUtc          TIMESTAMPTZ,
    CompletedByUserID       INTEGER,

    -- Cancellation
    CancelledAtUtc          TIMESTAMPTZ,
    CancelledByUserID       INTEGER,
    CancelReason            TEXT,

    -- Audit
    CreatedAtUtc            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UpdatedAtUtc            TIMESTAMPTZ,

    -- Constraints
    CONSTRAINT ck_DTR_Status CHECK (Status IN (
        'PendingSourceApproval',
        'PendingTargetApproval',
        'Returned',
        'Rejected',
        'Approved',
        'Completed',
        'Cancelled'
    )),
    CONSTRAINT ck_DTR_InitiatedBy CHECK (InitiatedBy IN ('Driver', 'SourceBranch')),
    CONSTRAINT ck_DTR_SourceTarget CHECK (SourceBranchID != TargetBranchID),

    CONSTRAINT fk_DTR_Company
        FOREIGN KEY (CompanyID)          REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_DTR_Driver
        FOREIGN KEY (DriverID)           REFERENCES core.Drivers(DriverID),
    CONSTRAINT fk_DTR_SourceBranch
        FOREIGN KEY (SourceBranchID)     REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_DTR_TargetBranch
        FOREIGN KEY (TargetBranchID)     REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_DTR_RequestedBy
        FOREIGN KEY (RequestedByUserID)  REFERENCES sec.Users(UserID),
    CONSTRAINT fk_DTR_SourceApprovedBy
        FOREIGN KEY (SourceApprovedByUserID) REFERENCES sec.Users(UserID),
    CONSTRAINT fk_DTR_TargetDecidedBy
        FOREIGN KEY (TargetDecidedByUserID)  REFERENCES sec.Users(UserID),
    CONSTRAINT fk_DTR_NewDriver
        FOREIGN KEY (NewDriverID)        REFERENCES core.Drivers(DriverID),
    CONSTRAINT fk_DTR_CompletedBy
        FOREIGN KEY (CompletedByUserID)  REFERENCES sec.Users(UserID),
    CONSTRAINT fk_DTR_CancelledBy
        FOREIGN KEY (CancelledByUserID)  REFERENCES sec.Users(UserID)
);

CREATE INDEX ix_DTR_Company_Status  ON core.DriverTransferRequests(CompanyID, Status);
CREATE INDEX ix_DTR_SourceBranch    ON core.DriverTransferRequests(SourceBranchID, Status);
CREATE INDEX ix_DTR_TargetBranch    ON core.DriverTransferRequests(TargetBranchID, Status);
CREATE INDEX ix_DTR_Driver          ON core.DriverTransferRequests(DriverID);
