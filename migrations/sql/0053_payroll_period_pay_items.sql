-- =============================================================================
-- 0053: Payroll period pay-item layout snapshots
--
-- Introduces payroll.PayrollPeriodPayItems: one row per PayItem per payroll
-- period, created at period-creation time and immutable afterward.
--
-- Purpose:
--   Freeze the set, order, and metadata of pay items available to a period so
--   that later Pay Items or Branch Pay Item Config changes do not retroactively
--   alter the columns visible on already-created periods.
--
-- Schema changes:
--   1. payroll.PayrollPeriodPayItems (new table)
--
-- Snapshot rules:
--   - All non-Retired PayItems (system companyid IS NULL + custom companyid = :cid).
--   - Branch activation resolved as of period.StartDate using BranchPayItemConfig.
--   - Both Daily and Period scope items are snapshotted.
--   - DailyStatus / DailyNote pseudo-lines are NOT in the PayItems catalog and
--     are excluded from the snapshot.
--
-- Design notes:
--   - Rows are created once (at period creation) and never updated.
--   - IsActiveInPeriod = COALESCE(bpic.isactive, pi.isdefaultbranchactive)
--     resolved at snapshot time.
--   - SnapshotEffectiveFrom = the BranchPayItemConfig.EffectiveFrom of the
--     config row used, or NULL when the default was used.
--   - SourceBranchPayItemConfigID = BranchPayItemConfig.ConfigID used, or NULL.
--   - Legacy periods (created before 0053) have no rows; the service falls back
--     to live BranchPayItemConfig queries for those periods.
--   - Two FKs:
--       fk_PPPI_Period:    PayrollPeriodPayItems(PayrollPeriodID) ->
--                          PayrollPeriods(PayrollPeriodID) ON DELETE CASCADE
--       fk_PPPI_PayItem:   PayrollPeriodPayItems(PayItemID) ->
--                          PayItems(PayItemID) — no cascade (retire, don't delete)
--
-- Downgrade:
--   Refuses if any rows exist. On zero rows: drops indexes and table.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. Create payroll.PayrollPeriodPayItems
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payroll.PayrollPeriodPayItems (
    PayrollPeriodPayItemID   BIGSERIAL    NOT NULL,
    PayrollPeriodID          BIGINT       NOT NULL,
    CompanyID                INTEGER      NOT NULL,
    BranchID                 INTEGER      NOT NULL,
    PayItemID                INTEGER      NOT NULL,
    PayItemCode              VARCHAR(50)  NOT NULL,
    PayItemName              VARCHAR(200) NOT NULL,
    DisplayLabel             VARCHAR(200),
    Category                 VARCHAR(50)  NOT NULL,
    DataType                 VARCHAR(50)  NOT NULL,
    Unit                     VARCHAR(50),
    ItemScope                VARCHAR(30)  NOT NULL,
    RateBehavior             VARCHAR(30)  NOT NULL,
    AppearsInPayrollEntry    BOOLEAN      NOT NULL,
    AppearsInLedger          BOOLEAN      NOT NULL,
    AppearsInReports         BOOLEAN      NOT NULL,
    RequiresRate             BOOLEAN      NOT NULL,
    IsSystemStandard         BOOLEAN      NOT NULL,
    IsCustom                 BOOLEAN      NOT NULL,
    PayItemStatusAtSnapshot  VARCHAR(30)  NOT NULL,
    IsActiveInPeriod         BOOLEAN      NOT NULL,
    SortOrder                INTEGER      NOT NULL DEFAULT 0,
    SnapshotEffectiveFrom    DATE,
    SourceBranchPayItemConfigID  INTEGER,
    CreatedAtUtc             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- Primary key
    CONSTRAINT pk_PayrollPeriodPayItems
        PRIMARY KEY (PayrollPeriodPayItemID),

    -- One row per PayItem per period
    CONSTRAINT uq_PayrollPeriodPayItems_Period_PayItem
        UNIQUE (PayrollPeriodID, PayItemID),

    -- FK to owning period; CASCADE so day/item rows are removed when the period
    -- is deleted (test _clean helpers and period cancellation flows delete periods).
    CONSTRAINT fk_PPPI_Period
        FOREIGN KEY (PayrollPeriodID)
        REFERENCES payroll.PayrollPeriods (PayrollPeriodID)
        ON DELETE CASCADE,

    -- FK to the pay item catalog — no cascade.
    -- Physical delete of a PayItem is blocked when snapshot rows exist;
    -- retire (Status = 'Retired') is used instead.
    CONSTRAINT fk_PPPI_PayItem
        FOREIGN KEY (PayItemID)
        REFERENCES payroll.PayItems (PayItemID),

    -- Scope values
    CONSTRAINT ck_PPPI_ItemScope
        CHECK (ItemScope IN ('Daily', 'Period')),

    -- SortOrder must be non-negative
    CONSTRAINT ck_PPPI_SortOrder
        CHECK (SortOrder >= 0)
);


-- ---------------------------------------------------------------------------
-- 2. Indexes
-- ---------------------------------------------------------------------------

-- Efficient lookup of all snapshot items for a period (primary access pattern)
CREATE INDEX IF NOT EXISTS ix_PayrollPeriodPayItems_Period
    ON payroll.PayrollPeriodPayItems (PayrollPeriodID);

-- Efficient lookup by company+branch for snapshot-existence checks
CREATE INDEX IF NOT EXISTS ix_PayrollPeriodPayItems_Branch
    ON payroll.PayrollPeriodPayItems (CompanyID, BranchID);

-- Efficient FK-side lookup to block physical delete of a PayItem that has
-- snapshot rows
CREATE INDEX IF NOT EXISTS ix_PayrollPeriodPayItems_PayItem
    ON payroll.PayrollPeriodPayItems (PayItemID);
