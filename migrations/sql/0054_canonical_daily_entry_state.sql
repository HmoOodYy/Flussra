-- =============================================================================
-- 0054: Canonical driver/day entry state
--
-- Introduces payroll.PayrollPeriodDriverDayEntryState: one row per driver per
-- work-date per period, created/upserted when a payroll user sets a status
-- or note in the Daily Grid.
--
-- Purpose:
--   Provide a canonical, relational source for the selected StatusKey and note
--   text per driver/day.  Replaces the anti-pattern of storing this information
--   as fake PayrollDraftLines rows (linetype='DailyStatus' / 'DailyNote').
--
--   DailyStatus/DailyNote DraftLines continue to be written (dual-write) during
--   this migration window so that existing consumers (usage-limit enforcement,
--   off-driver query, finalization Step 3) are unaffected.
--
-- Status Key dropdown behavior:
--   Available status keys are loaded LIVE from PayrollStatusKeys at every
--   get_day_grid call.  They are NOT snapshotted at period creation.
--   A newly-added active Status Key appears in the dropdown after the next
--   get_day_grid refresh — no period reopen required.
--
-- Finalization snapshot:
--   After the period is atomically claimed as Locked (finalize_period Step 2),
--   the selected StatusKey's live fields are copied into the snapshot columns
--   (StatusCodeSnapshot, StatusLabelSnapshot, StatusIsOffReasonSnapshot,
--   StatusHoursValueSnapshot, FinalizedAtUtc).  Those columns remain NULL for
--   editable periods and are frozen once set.
--
-- Schema changes:
--   1. payroll.PayrollPeriodDriverDayEntryState (new table)
--
-- Downgrade:
--   Refuses if any rows exist.  On zero rows: drops indexes and table.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. Create payroll.PayrollPeriodDriverDayEntryState
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payroll.PayrollPeriodDriverDayEntryState (

    PayrollPeriodDriverDayEntryStateID  BIGSERIAL       NOT NULL,

    -- Tenant identity
    CompanyID                           INTEGER         NOT NULL,
    BranchID                            INTEGER         NOT NULL,

    -- Period linkage
    PayrollPeriodID                     INTEGER         NOT NULL,
    PayrollPeriodDayID                  BIGINT,         -- nullable: legacy periods have no day rows

    -- Driver / date key
    WorkDate                            DATE            NOT NULL,
    DriverID                            INTEGER         NOT NULL,

    -- Selected status (editable — written at save_day_grid / direct API time)
    -- NULL means no status selected (or cleared)
    StatusKeyID                         INTEGER,

    -- User-entered note text — independent of StatusKeyID
    NoteText                            TEXT,

    -- Finalization snapshot (NULL until finalize_period runs; frozen at lock)
    StatusCodeSnapshot                  VARCHAR(60),
    StatusLabelSnapshot                 VARCHAR(200),
    StatusIsOffReasonSnapshot           BOOLEAN,
    StatusHoursValueSnapshot            NUMERIC(5,2),
    FinalizedAtUtc                      TIMESTAMPTZ,

    -- Soft-delete: TRUE when both StatusKeyID and NoteText are cleared
    IsVoided                            BOOLEAN         NOT NULL DEFAULT FALSE,

    -- Audit
    CreatedByUserID                     INTEGER         NOT NULL,
    UpdatedByUserID                     INTEGER,
    CreatedAtUtc                        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UpdatedAtUtc                        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    -- Primary key
    CONSTRAINT pk_PayrollPeriodDriverDayEntryState
        PRIMARY KEY (PayrollPeriodDriverDayEntryStateID),

    -- One row per driver per day per period
    CONSTRAINT uq_PPDES_Period_Driver_Date
        UNIQUE (PayrollPeriodID, DriverID, WorkDate),

    -- FK to owning period; CASCADE so entry-state rows are removed when the
    -- period is deleted (test _clean helpers and cancellation flows).
    CONSTRAINT fk_PPDES_Period
        FOREIGN KEY (PayrollPeriodID)
        REFERENCES payroll.PayrollPeriods (PayrollPeriodID)
        ON DELETE CASCADE,

    -- FK to the period-day snapshot row (nullable — legacy periods have none).
    CONSTRAINT fk_PPDES_PeriodDay
        FOREIGN KEY (PayrollPeriodDayID)
        REFERENCES payroll.PayrollPeriodDays (PayrollPeriodDayID),

    -- FK to the StatusKey catalog — ON DELETE RESTRICT so that a StatusKey
    -- that is referenced by any entry-state row cannot be physically deleted.
    -- Admins must deactivate (IsActive=FALSE) instead.
    CONSTRAINT fk_PPDES_StatusKey
        FOREIGN KEY (StatusKeyID)
        REFERENCES payroll.PayrollStatusKeys (StatusKeyID)
        ON DELETE RESTRICT
);


-- ---------------------------------------------------------------------------
-- 2. Indexes
-- ---------------------------------------------------------------------------

-- Primary read pattern: all entry-state rows for a period on a given date
CREATE INDEX IF NOT EXISTS ix_PPDES_Period_Date
    ON payroll.PayrollPeriodDriverDayEntryState (PayrollPeriodID, WorkDate);

-- Driver-centric lookup within a period
CREATE INDEX IF NOT EXISTS ix_PPDES_Period_Driver
    ON payroll.PayrollPeriodDriverDayEntryState (PayrollPeriodID, DriverID);

-- Tenant isolation / cross-company existence checks
CREATE INDEX IF NOT EXISTS ix_PPDES_Company_Branch
    ON payroll.PayrollPeriodDriverDayEntryState (CompanyID, BranchID);

-- FK-side lookup: block physical delete of a StatusKey that has entry-state rows
CREATE INDEX IF NOT EXISTS ix_PPDES_StatusKey
    ON payroll.PayrollPeriodDriverDayEntryState (StatusKeyID)
    WHERE StatusKeyID IS NOT NULL;

-- Fast lookup of finalized rows (e.g. for locked-period display queries)
CREATE INDEX IF NOT EXISTS ix_PPDES_Period_Finalized
    ON payroll.PayrollPeriodDriverDayEntryState (PayrollPeriodID, FinalizedAtUtc)
    WHERE FinalizedAtUtc IS NOT NULL;
