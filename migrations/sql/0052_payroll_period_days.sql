-- =============================================================================
-- 0052: Payroll period-day calendar snapshots
--
-- Introduces payroll.PayrollPeriodDays: one row per calendar day in a payroll
-- period, created at period-creation time and immutable afterward.
--
-- Purpose:
--   Freeze the schedule mask interpretation at period-creation time so that
--   later payroll-setup changes do not retroactively alter which days are
--   scheduled work days on already-created periods.
--
-- Schema changes:
--   1. payroll.PayrollPeriodDays (new table)
--
-- Design notes:
--   - Rows are created once (at period creation) and never updated.
--   - IsDefaultWorkDay = TRUE when the NormalDaysOffMask does not mark this
--     weekday as off. IsConfiguredOffDay = TRUE when it does. Exactly one
--     of the two is TRUE (enforced by ck_PayrollPeriodDays_DefaultXorOff).
--   - IsAddedWorkDay is always FALSE in CP-2B; Add Day activation is deferred.
--   - AddedByUserID / AddedAtUtc / AddedReason are always NULL in CP-2B.
--   - Legacy periods (created before 0052) have no rows; the service falls
--     back to StartDate/EndDate bounds check for those periods.
--   - ScheduleVersionID is NOT NULL: only periods with a non-NULL sv_id get
--     day rows, which is guaranteed for all periods created after CP-2A (0051).
--   - Two FKs:
--       fk_PPD_Period: PayrollPeriodDays(PayrollPeriodID) -> PayrollPeriods(PK)
--       fk_PPD_ScheduleVersion: (ScheduleVersionID, CompanyID, BranchID) ->
--         PayrollScheduleVersions via uq_PayrollScheduleVersions_Comp index
--   - Service additionally asserts PeriodDay.ScheduleVersionID equals the
--     period's own ScheduleVersionID at insertion time.
--
-- Downgrade:
--   Refuses if any rows exist. On zero rows: drops indexes and table.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. Create payroll.PayrollPeriodDays
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payroll.PayrollPeriodDays (
    PayrollPeriodDayID  BIGSERIAL    NOT NULL,
    PayrollPeriodID     BIGINT       NOT NULL,
    CompanyID           INTEGER      NOT NULL,
    BranchID            INTEGER      NOT NULL,
    ScheduleVersionID   BIGINT       NOT NULL,
    WorkDate            DATE         NOT NULL,
    DayOfWeek           SMALLINT     NOT NULL,
    IsDefaultWorkDay    BOOLEAN      NOT NULL,
    IsConfiguredOffDay  BOOLEAN      NOT NULL,
    IsAddedWorkDay      BOOLEAN      NOT NULL DEFAULT FALSE,
    AddedByUserID       INTEGER,
    AddedAtUtc          TIMESTAMPTZ,
    AddedReason         TEXT,
    CreatedAtUtc        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- Primary key
    CONSTRAINT pk_PayrollPeriodDays
        PRIMARY KEY (PayrollPeriodDayID),

    -- No duplicate day per period
    CONSTRAINT uq_PayrollPeriodDays_Period_Date
        UNIQUE (PayrollPeriodID, WorkDate),

    -- FK to the owning period; CASCADE ensures day rows are removed when the period is deleted.
    -- Period deletion is rare but occurs in test cleanup and period cancellation flows.
    CONSTRAINT fk_PPD_Period
        FOREIGN KEY (PayrollPeriodID)
        REFERENCES payroll.PayrollPeriods (PayrollPeriodID)
        ON DELETE CASCADE,

    -- FK to the schedule version (binds to correct company + branch)
    -- Uses the uq_PayrollScheduleVersions_Comp index created by migration 0051.
    CONSTRAINT fk_PPD_ScheduleVersion
        FOREIGN KEY (ScheduleVersionID, CompanyID, BranchID)
        REFERENCES payroll.PayrollScheduleVersions (ScheduleVersionID, CompanyID, BranchID),

    -- Weekday range
    CONSTRAINT ck_PayrollPeriodDays_DayOfWeek
        CHECK (DayOfWeek BETWEEN 0 AND 6),

    -- Exactly one of IsDefaultWorkDay / IsConfiguredOffDay is TRUE
    CONSTRAINT ck_PayrollPeriodDays_DefaultXorOff
        CHECK (IsDefaultWorkDay <> IsConfiguredOffDay),

    -- IsAddedWorkDay may only be TRUE for configured-off days
    CONSTRAINT ck_PayrollPeriodDays_AddedOnlyIfOff
        CHECK (IsAddedWorkDay = FALSE OR IsConfiguredOffDay = TRUE),

    -- Add Day metadata must be NULL when IsAddedWorkDay is FALSE
    CONSTRAINT ck_PayrollPeriodDays_AddedMeta
        CHECK (
            IsAddedWorkDay = TRUE
            OR (AddedByUserID IS NULL AND AddedAtUtc IS NULL AND AddedReason IS NULL)
        )
);


-- ---------------------------------------------------------------------------
-- 2. Indexes
-- ---------------------------------------------------------------------------

-- Efficient lookup of all days for a period (primary access pattern)
CREATE INDEX IF NOT EXISTS ix_PayrollPeriodDays_Period
    ON payroll.PayrollPeriodDays (PayrollPeriodID);

-- Efficient date-range scans per company/branch (reporting / audit access)
CREATE INDEX IF NOT EXISTS ix_PayrollPeriodDays_Branch_Date
    ON payroll.PayrollPeriodDays (CompanyID, BranchID, WorkDate);
