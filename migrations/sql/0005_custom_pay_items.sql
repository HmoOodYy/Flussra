-- =============================================================================
-- Migration 0005: Custom Pay Items catalog
--
-- Adds:
--   1. DisplayLabel column on payroll.PayItems  (user-facing label)
--   2. RequestingBranchID column on payroll.PayItems  (provenance; not ownership)
--   3. Unique partial index: one PayItemCode per company (custom items)
--   4. CHECK constraints on ItemScope, RateBehavior, and Status
--   5. payroll.CustomPayItemRequests  (branch request / admin approval queue)
--
-- Notes on constraints:
--   - ck_PayItems_RateBehavior includes EnteredAmount (M12 period items) but NOT
--     OrdinalTier / RangeBracket / RangeProgressive / Block -- those are M13.
--   - ux_PayItems_Global_PayItemCode (0001) already guarantees system-item code
--     uniqueness (WHERE CompanyID IS NULL).
--   - ux_PayItems_Company_PayItemCode (this migration) guarantees custom-item code
--     uniqueness per company (WHERE CompanyID IS NOT NULL).
--   - All existing payroll.PayItems rows satisfy the new CHECK constraints.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. New columns on payroll.PayItems
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.PayItems
    ADD COLUMN IF NOT EXISTS DisplayLabel       VARCHAR(200);

ALTER TABLE payroll.PayItems
    ADD COLUMN IF NOT EXISTS Notes              TEXT;

ALTER TABLE payroll.PayItems
    ADD COLUMN IF NOT EXISTS RequestingBranchID INTEGER
    REFERENCES core.Branches(BranchID);

-- ---------------------------------------------------------------------------
-- 2. Unique index: one code per company for custom items
-- ---------------------------------------------------------------------------

CREATE UNIQUE INDEX ux_PayItems_Company_PayItemCode
    ON payroll.PayItems (CompanyID, PayItemCode)
    WHERE CompanyID IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. CHECK constraints on PayItems
--    Applied with ADD CONSTRAINT so they appear in pg_constraint and are
--    visible to schema-inspection tools.
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.PayItems
    ADD CONSTRAINT ck_PayItems_ItemScope
    CHECK (ItemScope IN ('Daily', 'Period'));

ALTER TABLE payroll.PayItems
    ADD CONSTRAINT ck_PayItems_RateBehavior
    CHECK (RateBehavior IN ('PerUnit', 'Fixed', 'Calculated', 'None', 'EnteredAmount'));

ALTER TABLE payroll.PayItems
    ADD CONSTRAINT ck_PayItems_Status
    CHECK (Status IN ('Active', 'Inactive', 'Retired'));

-- ---------------------------------------------------------------------------
-- 4. payroll.CustomPayItemRequests
-- ---------------------------------------------------------------------------

CREATE TABLE payroll.CustomPayItemRequests (
    RequestID             SERIAL        PRIMARY KEY,
    CompanyID             INTEGER       NOT NULL,
    RequestingBranchID    INTEGER       NOT NULL,
    RequestedByUserID     INTEGER       NOT NULL,
    RequestedAtUtc        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    -- Proposed item definition (mirrors PayItems fields used in M12)
    PayItemCode           VARCHAR(50)   NOT NULL,
    DisplayLabel          VARCHAR(200),
    PayItemName           VARCHAR(200)  NOT NULL,
    ItemScope             VARCHAR(30)   NOT NULL,   -- 'Daily' or 'Period'
    RateBehavior          VARCHAR(30)   NOT NULL,   -- 'PerUnit' or 'EnteredAmount'
    Category              VARCHAR(50)   NOT NULL,
    Unit                  VARCHAR(50),              -- required for PerUnit; NULL for EnteredAmount
    Notes                 TEXT,
    SortOrder             INTEGER       NOT NULL DEFAULT 0,

    -- Decision
    Status                VARCHAR(30)   NOT NULL DEFAULT 'PendingApproval',
    DecidedByUserID       INTEGER,
    DecidedAtUtc          TIMESTAMPTZ,
    DecisionReason        TEXT,
    ApprovedPayItemID     INTEGER,                  -- FK to PayItems (set on Approved only)

    CONSTRAINT ck_CustomPayItemRequests_Status
        CHECK (Status IN ('PendingApproval', 'Approved', 'Rejected')),
    CONSTRAINT ck_CustomPayItemRequests_ItemScope
        CHECK (ItemScope IN ('Daily', 'Period')),
    CONSTRAINT ck_CustomPayItemRequests_RateBehavior
        CHECK (RateBehavior IN ('PerUnit', 'EnteredAmount')),

    CONSTRAINT fk_CustomPayItemRequests_Company
        FOREIGN KEY (CompanyID)          REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_CustomPayItemRequests_Branch
        FOREIGN KEY (RequestingBranchID) REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_CustomPayItemRequests_Requester
        FOREIGN KEY (RequestedByUserID)  REFERENCES sec.Users(UserID),
    CONSTRAINT fk_CustomPayItemRequests_Decider
        FOREIGN KEY (DecidedByUserID)    REFERENCES sec.Users(UserID),
    CONSTRAINT fk_CustomPayItemRequests_ApprovedItem
        FOREIGN KEY (ApprovedPayItemID)  REFERENCES payroll.PayItems(PayItemID)
);

CREATE INDEX ix_CustomPayItemRequests_Company_Status
    ON payroll.CustomPayItemRequests (CompanyID, Status);

CREATE INDEX ix_CustomPayItemRequests_Branch_Status
    ON payroll.CustomPayItemRequests (RequestingBranchID, Status);
