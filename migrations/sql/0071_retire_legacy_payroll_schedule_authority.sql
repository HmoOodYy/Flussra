-- 0071: Retire legacy branch-owned payroll schedule authority.
--
-- Phases 1-7 established Company Payroll Setup / Published Version /
-- Branch Payroll Setup Assignment as the single canonical payroll-schedule
-- authority. The legacy branch-owned model (payroll.BranchPayrollSettings,
-- payroll.PayrollScheduleVersions, and the ScheduleVersionID columns on
-- PayrollPeriods/PayrollPeriodDays) has had zero reachable runtime read/write
-- path since the Phase 3 cutover: the legacy HTTP routes are no longer
-- registered (test_payroll_setup_phase3_legacy_routes.py),
-- and canonical period creation (create_period_from_candidate) never
-- populates ScheduleVersionID.
--
-- This migration physically retires that legacy model. It is FAIL-CLOSED:
-- before any destructive DDL, it verifies that no retained PayrollPeriods or
-- PayrollPeriodDays row still depends on legacy ScheduleVersionID authority.
-- If any such row exists, the migration aborts with no schema or data
-- changes -- it never deletes, truncates, nulls, or fabricates a canonical
-- mapping for retained business history.
--
-- Historical migrations (0001, 0029, 0051, 0052, 0067) are NOT rewritten;
-- they remain the accurate record of how this schema evolved.

-- ---------------------------------------------------------------------------
-- 0. PREFLIGHT: fail closed if any legacy root row or retained business row
--    still depends on legacy ScheduleVersionID authority.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_period_count  INTEGER;
    v_day_count     INTEGER;
    v_incomplete_day_count INTEGER;
    v_settings_count INTEGER;
    v_schedule_version_count INTEGER;
    v_settings_sample TEXT;
    v_schedule_version_sample TEXT;
    v_period_sample TEXT;
    v_day_sample    TEXT;
BEGIN
    SELECT COUNT(*) INTO v_settings_count
    FROM payroll.BranchPayrollSettings;

    SELECT COUNT(*) INTO v_schedule_version_count
    FROM payroll.PayrollScheduleVersions;

    SELECT COUNT(*) INTO v_period_count
    FROM payroll.PayrollPeriods
    WHERE ScheduleVersionID IS NOT NULL;

    SELECT COUNT(*) INTO v_day_count
    FROM payroll.PayrollPeriodDays
    WHERE ScheduleVersionID IS NOT NULL;

    -- At 0070, 0067's authority check requires every PeriodDay with a NULL
    -- ScheduleVersionID to carry both canonical authority columns. Keep this
    -- explicit preflight because the next DDL removes that transitional check
    -- and makes the canonical columns NOT NULL; malformed rows must fail
    -- closed before any destructive statement runs.
    SELECT COUNT(*) INTO v_incomplete_day_count
    FROM payroll.PayrollPeriodDays
    WHERE ScheduleVersionID IS NULL
      AND (BranchPayrollSetupAssignmentID IS NULL
           OR PayrollSetupVersionID IS NULL);

    IF v_settings_count > 0 OR v_schedule_version_count > 0
       OR v_period_count > 0 OR v_day_count > 0 OR v_incomplete_day_count > 0 THEN
        SELECT string_agg(
            format('BranchPayrollSettingsID=%s CompanyID=%s BranchID=%s CurrentScheduleVersionID=%s',
                   BranchPayrollSettingsID, CompanyID, BranchID, CurrentScheduleVersionID),
            '; ' ORDER BY BranchPayrollSettingsID
        ) INTO v_settings_sample
        FROM (
            SELECT BranchPayrollSettingsID, CompanyID, BranchID, CurrentScheduleVersionID
            FROM payroll.BranchPayrollSettings
            ORDER BY BranchPayrollSettingsID LIMIT 20
        ) AS offenders;

        SELECT string_agg(
            format('ScheduleVersionID=%s CompanyID=%s BranchID=%s VersionNumber=%s',
                   ScheduleVersionID, CompanyID, BranchID, VersionNumber),
            '; ' ORDER BY ScheduleVersionID
        ) INTO v_schedule_version_sample
        FROM (
            SELECT ScheduleVersionID, CompanyID, BranchID, VersionNumber
            FROM payroll.PayrollScheduleVersions
            ORDER BY ScheduleVersionID LIMIT 20
        ) AS offenders;

        SELECT string_agg(
            format('PayrollPeriodID=%s CompanyID=%s BranchID=%s ScheduleVersionID=%s',
                   PayrollPeriodID, CompanyID, BranchID, ScheduleVersionID),
            '; ' ORDER BY PayrollPeriodID
        ) INTO v_period_sample
        FROM (
            SELECT PayrollPeriodID, CompanyID, BranchID, ScheduleVersionID
            FROM payroll.PayrollPeriods
            WHERE ScheduleVersionID IS NOT NULL
            ORDER BY PayrollPeriodID LIMIT 20
        ) AS offenders;

        SELECT string_agg(
            format('PayrollPeriodDayID=%s PayrollPeriodID=%s ScheduleVersionID=%s',
                   PayrollPeriodDayID, PayrollPeriodID, ScheduleVersionID),
            '; ' ORDER BY PayrollPeriodDayID
        ) INTO v_day_sample
        FROM (
            SELECT PayrollPeriodDayID, PayrollPeriodID, ScheduleVersionID
            FROM payroll.PayrollPeriodDays
            WHERE ScheduleVersionID IS NOT NULL
            ORDER BY PayrollPeriodDayID LIMIT 20
        ) AS offenders;

        RAISE EXCEPTION
            'payroll_legacy_schedule_authority_retirement_blocked: % BranchPayrollSettings row(s), % PayrollScheduleVersions row(s), % PayrollPeriods row(s), and % PayrollPeriodDays row(s) still retain legacy authority; % PeriodDays row(s) have incomplete canonical authority. Sample settings: [%]. Sample schedule versions: [%]. Sample periods: [%]. Sample days: [%].',
            v_settings_count, v_schedule_version_count, v_period_count, v_day_count,
            v_incomplete_day_count, COALESCE(v_settings_sample, 'none'),
            COALESCE(v_schedule_version_sample, 'none'),
            COALESCE(v_period_sample, 'none'), COALESCE(v_day_sample, 'none')
            USING HINT = 'Phase 8 legacy authority retirement requires zero BranchPayrollSettings/PayrollScheduleVersions rows and zero retained PayrollPeriods/PayrollPeriodDays rows bound to legacy ScheduleVersionID. This migration never deletes, nulls, or reconciles business history automatically. Resolve with a separately reviewed reconciliation migration that maps each affected row to its exact canonical BranchPayrollSetupAssignment and PayrollSetupVersion before retrying.';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 1. Retire PayrollPeriodDays legacy authority representation.
-- ---------------------------------------------------------------------------
DROP INDEX payroll.ix_PayrollPeriodDays_SetupAuthority;

ALTER TABLE payroll.PayrollPeriodDays
    DROP CONSTRAINT ck_PayrollPeriodDays_AuthorityRepresentation;
ALTER TABLE payroll.PayrollPeriodDays
    DROP CONSTRAINT fk_PayrollPeriodDays_ExactLegacyAuthority;
ALTER TABLE payroll.PayrollPeriodDays
    DROP CONSTRAINT fk_PPD_ScheduleVersion;
ALTER TABLE payroll.PayrollPeriodDays
    DROP COLUMN ScheduleVersionID;

-- Preflight guarantees every retained row already carries canonical
-- authority, so this NOT NULL upgrade is safe.
ALTER TABLE payroll.PayrollPeriodDays
    ALTER COLUMN BranchPayrollSetupAssignmentID SET NOT NULL,
    ALTER COLUMN PayrollSetupVersionID SET NOT NULL;

CREATE INDEX ix_PayrollPeriodDays_SetupAuthority
    ON payroll.PayrollPeriodDays (PayrollSetupVersionID);

-- ---------------------------------------------------------------------------
-- 2. Retire PayrollPeriods legacy authority representation.
--    The provenance trigger references ScheduleVersionID in its column-watch
--    list, so it (and its function) must be dropped before the column.
-- ---------------------------------------------------------------------------
DROP TRIGGER trg_PayrollPeriods_SetupProvenance ON payroll.PayrollPeriods;
DROP FUNCTION payroll.fn_guard_period_setup_provenance();

ALTER TABLE payroll.PayrollPeriods
    DROP CONSTRAINT ck_PayrollPeriods_AuthorityRepresentation;
ALTER TABLE payroll.PayrollPeriods
    DROP CONSTRAINT fk_PP_ScheduleVersion;

DROP INDEX payroll.ix_PayrollPeriods_ScheduleVersion;
DROP INDEX payroll.ux_PayrollPeriods_LegacyDayAuthority;

ALTER TABLE payroll.PayrollPeriods
    DROP COLUMN ScheduleVersionID;

-- New-authority-only representation. The all-NULL branch is preserved: it
-- represents pre-CP2A historical periods that predate ANY schedule-authority
-- provenance (created before migration 0051 introduced ScheduleVersionID at
-- all), which is a distinct historical category from the retired
-- ScheduleVersionID-populated representation this migration removes.
ALTER TABLE payroll.PayrollPeriods
    ADD CONSTRAINT ck_PayrollPeriods_AuthorityRepresentation
    CHECK (
        (BranchPayrollSetupAssignmentID IS NULL
         AND PayrollSetupVersionID IS NULL
         AND FrozenPayrollSetupID IS NULL
         AND FrozenPayrollSetupCode IS NULL
         AND FrozenPayrollSetupVersionNumber IS NULL
         AND FrozenPayrollFrequency IS NULL
         AND FrozenAnchorStartDate IS NULL
         AND FrozenCustomIntervalDays IS NULL
         AND FrozenNormalDaysOffMask IS NULL
         AND ScheduleConfigHash IS NULL)
        OR
        (BranchPayrollSetupAssignmentID IS NOT NULL
         AND PayrollSetupVersionID IS NOT NULL
         AND FrozenPayrollSetupID IS NOT NULL
         AND FrozenPayrollSetupCode IS NOT NULL
         AND FrozenPayrollSetupVersionNumber IS NOT NULL
         AND FrozenPayrollFrequency IS NOT NULL
         AND FrozenAnchorStartDate IS NOT NULL
         AND (
             (FrozenPayrollFrequency = 'Custom' AND FrozenCustomIntervalDays > 0)
             OR (FrozenPayrollFrequency <> 'Custom' AND FrozenCustomIntervalDays IS NULL)
         )
         AND FrozenNormalDaysOffMask IS NOT NULL
         AND ScheduleConfigHash IS NOT NULL)
    );

-- ---------------------------------------------------------------------------
-- 3. Rebuild the frozen-provenance guard trigger for canonical authority
--    only. Preserves every existing protection (frozen authority
--    immutability, exact assignment/version ownership, assignment coverage,
--    Published-only versions, frozen snapshot fidelity) -- only the
--    ScheduleVersionID concept is removed.
-- ---------------------------------------------------------------------------
CREATE FUNCTION payroll.fn_guard_period_setup_provenance()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_version payroll.PayrollSetupVersions%ROWTYPE;
    v_assignment payroll.BranchPayrollSetupAssignments%ROWTYPE;
    v_code VARCHAR(50);
    v_assignment_found BOOLEAN;
    v_version_found BOOLEAN;
    v_setup_found BOOLEAN;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.BranchPayrollSetupAssignmentID IS NOT NULL AND
       (NEW.CompanyID, NEW.BranchID, NEW.StartDate, NEW.EndDate,
        NEW.BranchPayrollSetupAssignmentID, NEW.PayrollSetupVersionID,
        NEW.FrozenPayrollSetupID, NEW.FrozenPayrollSetupCode,
        NEW.FrozenPayrollSetupVersionNumber, NEW.FrozenPayrollFrequency,
        NEW.FrozenAnchorStartDate, NEW.FrozenCustomIntervalDays,
        NEW.FrozenNormalDaysOffMask, NEW.ScheduleConfigHash)
       IS DISTINCT FROM
       (OLD.CompanyID, OLD.BranchID, OLD.StartDate, OLD.EndDate,
        OLD.BranchPayrollSetupAssignmentID, OLD.PayrollSetupVersionID,
        OLD.FrozenPayrollSetupID, OLD.FrozenPayrollSetupCode,
        OLD.FrozenPayrollSetupVersionNumber, OLD.FrozenPayrollFrequency,
        OLD.FrozenAnchorStartDate, OLD.FrozenCustomIntervalDays,
        OLD.FrozenNormalDaysOffMask, OLD.ScheduleConfigHash) THEN
        RAISE EXCEPTION 'payroll_period_setup_provenance: frozen authority cannot change'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.BranchPayrollSetupAssignmentID IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT * INTO v_assignment FROM payroll.BranchPayrollSetupAssignments
    WHERE BranchPayrollSetupAssignmentID = NEW.BranchPayrollSetupAssignmentID;
    v_assignment_found := FOUND;
    SELECT * INTO v_version FROM payroll.PayrollSetupVersions
    WHERE PayrollSetupVersionID = NEW.PayrollSetupVersionID;
    v_version_found := FOUND;
    SELECT SetupCode INTO v_code FROM payroll.PayrollSetups
    WHERE PayrollSetupID = NEW.FrozenPayrollSetupID;
    v_setup_found := FOUND;
    IF NOT v_assignment_found OR NOT v_version_found OR NOT v_setup_found
       OR v_assignment.WithdrawnAtUtc IS NOT NULL
       OR v_assignment.EffectiveFromDate > NEW.StartDate
       OR (v_assignment.EffectiveToDate IS NOT NULL
           AND v_assignment.EffectiveToDate <= NEW.EndDate)
       OR v_version.LifecycleState IS DISTINCT FROM 'Published'
       OR v_version.EffectiveFromDate > NEW.StartDate
       OR v_version.VersionNumber IS DISTINCT FROM NEW.FrozenPayrollSetupVersionNumber
       OR v_version.PayrollFrequency IS DISTINCT FROM NEW.FrozenPayrollFrequency
       OR v_version.AnchorStartDate IS DISTINCT FROM NEW.FrozenAnchorStartDate
       OR v_version.CustomIntervalDays IS DISTINCT FROM NEW.FrozenCustomIntervalDays
       OR v_version.NormalDaysOffMask IS DISTINCT FROM NEW.FrozenNormalDaysOffMask
       OR v_code IS DISTINCT FROM NEW.FrozenPayrollSetupCode THEN
        RAISE EXCEPTION 'payroll_period_setup_provenance: authority snapshot is invalid'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_PayrollPeriods_SetupProvenance
    BEFORE INSERT OR UPDATE OF CompanyID, BranchID, StartDate, EndDate,
        BranchPayrollSetupAssignmentID, PayrollSetupVersionID,
        FrozenPayrollSetupID, FrozenPayrollSetupCode, FrozenPayrollSetupVersionNumber,
        FrozenPayrollFrequency, FrozenAnchorStartDate, FrozenCustomIntervalDays,
        FrozenNormalDaysOffMask, ScheduleConfigHash
    ON payroll.PayrollPeriods FOR EACH ROW
    EXECUTE FUNCTION payroll.fn_guard_period_setup_provenance();

-- ---------------------------------------------------------------------------
-- 4. Retire the obsolete root model: BranchPayrollSettings (with
--    CurrentScheduleVersionID) and PayrollScheduleVersions. Explicit,
--    dependency-ordered, no CASCADE -- if any unexpected object still
--    depends on either table, these statements fail rather than silently
--    dropping that dependent object.
-- ---------------------------------------------------------------------------
ALTER TABLE payroll.BranchPayrollSettings
    DROP CONSTRAINT fk_BPS_CurrentScheduleVersion;

DROP TABLE payroll.BranchPayrollSettings;
DROP TABLE payroll.PayrollScheduleVersions;
