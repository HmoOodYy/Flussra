-- =============================================================================
-- 0051: Payroll schedule versioning
--
-- Introduces an immutable version history for BranchPayrollSettings so that
-- every payroll period created after this migration references the exact
-- schedule configuration that governed its date derivation.
--
-- Schema changes:
--   1. payroll.PayrollScheduleVersions  (new append-only table)
--   2. Unique index for composite FK support:
--         uq_PayrollScheduleVersions_Comp (ScheduleVersionID, CompanyID, BranchID)
--   3. payroll.BranchPayrollSettings.CurrentScheduleVersionID  BIGINT NULL
--         + composite FK to PayrollScheduleVersions
--   4. payroll.PayrollPeriods.ScheduleVersionID  BIGINT NULL
--         + composite FK to PayrollScheduleVersions
--   5. Backfill: one version row (VersionNumber=1, SourceAction='BACKFILL')
--         for every existing BranchPayrollSettings row
--   6. Update BranchPayrollSettings.CurrentScheduleVersionID from backfill
--
-- Design notes:
--   - PayrollScheduleVersions rows are NEVER updated after insert.
--   - VersionNumber is per (CompanyID, BranchID); the UNIQUE constraint enforces
--     this. The service computes max+1 under the branch advisory lock.
--   - Composite FK:
--       (ScheduleVersionID, CompanyID, BranchID) references the same triple on
--       PayrollScheduleVersions so a period or settings row cannot reference a
--       schedule version belonging to a different company or branch.
--   - PayDayOfWeek / FirstPayDate / IncludePayDayAsWorkDay are passively copied
--     from BranchPayrollSettings. No PayDate behaviour is implemented here.
--   - SemiMonthly cadence is not supported and is not referenced anywhere in
--     this migration.
--   - Existing historical PayrollPeriods retain NULL ScheduleVersionID;
--     only new periods created via the candidate or legacy path after 0051 will
--     have a non-NULL ScheduleVersionID.
--
-- Downgrade:
--   Refuses if any non-NULL ScheduleVersionID exists on PayrollPeriods, or
--   if any CurrentScheduleVersionID on BranchPayrollSettings differs from
--   what was backfilled (i.e. if new version rows were created by the service).
--   On clean state: drops FKs, columns, index, and table in dependency order.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. Create payroll.PayrollScheduleVersions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payroll.PayrollScheduleVersions (
    ScheduleVersionID       BIGSERIAL   NOT NULL,
    CompanyID               INTEGER     NOT NULL,
    BranchID                INTEGER     NOT NULL,
    VersionNumber           INTEGER     NOT NULL,
    PayrollFrequency        VARCHAR(20) NOT NULL,
    AnchorStartDate         DATE        NOT NULL,
    CustomIntervalDays      INTEGER,
    NormalDaysOffMask       SMALLINT,
    PayDayOfWeek            SMALLINT,
    FirstPayDate            DATE,
    IncludePayDayAsWorkDay  BOOLEAN     NOT NULL DEFAULT FALSE,
    EffectiveFromDate       DATE,
    EffectiveToDate         DATE,
    CreatedByUserID         INTEGER,
    CreatedAtUtc            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    SourceAction            VARCHAR(50) NOT NULL,
    ConfigHash              TEXT,

    CONSTRAINT pk_PayrollScheduleVersions
        PRIMARY KEY (ScheduleVersionID),

    CONSTRAINT uq_PayrollScheduleVersions_Branch_Version
        UNIQUE (CompanyID, BranchID, VersionNumber),

    CONSTRAINT fk_PayrollScheduleVersions_Company
        FOREIGN KEY (CompanyID)
        REFERENCES core.Companies (CompanyID),

    CONSTRAINT fk_PayrollScheduleVersions_Branch
        FOREIGN KEY (BranchID)
        REFERENCES core.Branches (BranchID),

    CONSTRAINT ck_PayrollScheduleVersions_VersionPositive
        CHECK (VersionNumber > 0),

    CONSTRAINT ck_PayrollScheduleVersions_Frequency
        CHECK (PayrollFrequency IN ('Week', 'Biweek', 'Month', 'Custom'))
);

CREATE INDEX IF NOT EXISTS ix_PayrollScheduleVersions_Branch
    ON payroll.PayrollScheduleVersions (CompanyID, BranchID);


-- ---------------------------------------------------------------------------
-- 2. Unique index on (ScheduleVersionID, CompanyID, BranchID) for composite FK
--    ScheduleVersionID is already a PK (unique) so this index adds no
--    duplicate-key overhead; it purely enables composite FK references.
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_PayrollScheduleVersions_Comp
    ON payroll.PayrollScheduleVersions (ScheduleVersionID, CompanyID, BranchID);


-- ---------------------------------------------------------------------------
-- 3. BranchPayrollSettings.CurrentScheduleVersionID + composite FK
-- ---------------------------------------------------------------------------
ALTER TABLE payroll.BranchPayrollSettings
    ADD COLUMN IF NOT EXISTS CurrentScheduleVersionID BIGINT;

ALTER TABLE payroll.BranchPayrollSettings
    DROP CONSTRAINT IF EXISTS fk_BPS_CurrentScheduleVersion;

ALTER TABLE payroll.BranchPayrollSettings
    ADD CONSTRAINT fk_BPS_CurrentScheduleVersion
        FOREIGN KEY (CurrentScheduleVersionID, CompanyID, BranchID)
        REFERENCES payroll.PayrollScheduleVersions (ScheduleVersionID, CompanyID, BranchID)
        DEFERRABLE INITIALLY DEFERRED;


-- ---------------------------------------------------------------------------
-- 4. PayrollPeriods.ScheduleVersionID + composite FK
-- ---------------------------------------------------------------------------
ALTER TABLE payroll.PayrollPeriods
    ADD COLUMN IF NOT EXISTS ScheduleVersionID BIGINT;

ALTER TABLE payroll.PayrollPeriods
    DROP CONSTRAINT IF EXISTS fk_PP_ScheduleVersion;

ALTER TABLE payroll.PayrollPeriods
    ADD CONSTRAINT fk_PP_ScheduleVersion
        FOREIGN KEY (ScheduleVersionID, CompanyID, BranchID)
        REFERENCES payroll.PayrollScheduleVersions (ScheduleVersionID, CompanyID, BranchID)
        DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX IF NOT EXISTS ix_PayrollPeriods_ScheduleVersion
    ON payroll.PayrollPeriods (ScheduleVersionID)
    WHERE ScheduleVersionID IS NOT NULL;


-- ---------------------------------------------------------------------------
-- 5. Backfill one schedule version row per existing BranchPayrollSettings row
--
--    VersionNumber = 1
--    SourceAction  = 'BACKFILL'
--    EffectiveFromDate = AnchorStartDate (best available date for pre-2A periods)
--    ConfigHash: JSON representation matching Python _setup_fingerprint format
--                {"anchor":"YYYY-MM-DD","freq":"...","interval":N}
--                Note: PostgreSQL json_build_object key order may differ from
--                Python sort_keys=True; this is acceptable for BACKFILL rows.
--                Service-created rows will use the canonical Python-computed hash.
-- ---------------------------------------------------------------------------
INSERT INTO payroll.PayrollScheduleVersions
    (CompanyID, BranchID, VersionNumber,
     PayrollFrequency, AnchorStartDate,
     CustomIntervalDays, NormalDaysOffMask,
     PayDayOfWeek, FirstPayDate, IncludePayDayAsWorkDay,
     EffectiveFromDate, EffectiveToDate,
     CreatedByUserID, SourceAction, ConfigHash)
SELECT
    bps.CompanyID,
    bps.BranchID,
    1,
    bps.PayrollFrequency,
    bps.AnchorStartDate,
    bps.CustomIntervalDays,
    bps.NormalDaysOffMask,
    bps.PayDayOfWeek,
    bps.FirstPayDate,
    COALESCE(bps.IncludePayDayAsWorkDay, FALSE),
    bps.AnchorStartDate,
    NULL,
    bps.CreatedByUserID,
    'BACKFILL',
    json_build_object(
        'anchor',   bps.AnchorStartDate::text,
        'freq',     bps.PayrollFrequency,
        'interval', bps.CustomIntervalDays
    )::text
FROM payroll.BranchPayrollSettings bps
ON CONFLICT (CompanyID, BranchID, VersionNumber) DO NOTHING;


-- ---------------------------------------------------------------------------
-- 6. Update BranchPayrollSettings.CurrentScheduleVersionID from backfill
-- ---------------------------------------------------------------------------
UPDATE payroll.BranchPayrollSettings bps
SET    CurrentScheduleVersionID = sv.ScheduleVersionID
FROM   payroll.PayrollScheduleVersions sv
WHERE  sv.CompanyID     = bps.CompanyID
  AND  sv.BranchID      = bps.BranchID
  AND  sv.VersionNumber = 1
  AND  bps.CurrentScheduleVersionID IS NULL;
