-- 0086: Branch timezone authority and instant Driver assignment windows.
-- Historical migrations remain immutable. This pre-production cutover requires
-- a clean rebuild when Driver/transfer/payroll evidence already exists because
-- DATE assignment history cannot be converted to truthful instants.

DO $preflight$
BEGIN
    IF EXISTS (SELECT 1 FROM core.Drivers)
       OR EXISTS (SELECT 1 FROM core.DriverTransferRequests)
       OR EXISTS (SELECT 1 FROM payroll.PayrollPeriods) THEN
        RAISE EXCEPTION
            'BRANCH_TIMEZONE_REBUILD_REQUIRED: 0086 cannot infer Driver assignment instants or rewrite payroll evidence. '
            'Reset and reseed this preproduction database, then apply migrations again.';
    END IF;
END
$preflight$;

CREATE TABLE core.SupportedTimeZones (
    TimeZoneId VARCHAR(100) PRIMARY KEY
);

INSERT INTO core.SupportedTimeZones (TimeZoneId)
SELECT name
FROM pg_timezone_names
WHERE name = 'UTC'
   OR name ~ '^(Africa|America|Antarctica|Arctic|Asia|Atlantic|Australia|Europe|Indian|Pacific)/'
ON CONFLICT (TimeZoneId) DO NOTHING;

CREATE OR REPLACE FUNCTION core.fn_IsSupportedTimeZone(p_TimeZoneId TEXT)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
AS $$
    SELECT p_TimeZoneId IS NOT NULL
       AND EXISTS (SELECT 1 FROM core.SupportedTimeZones z WHERE z.TimeZoneId = p_TimeZoneId)
$$;

DO $branch_timezone_preflight$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM core.Companies c
        JOIN core.Branches b ON b.CompanyID = c.CompanyID
        WHERE NOT core.fn_IsSupportedTimeZone(c.TimeZoneName)
    ) THEN
        RAISE EXCEPTION
            'BRANCH_TIMEZONE_INVALID_COMPANY_ZONE: at least one existing Company.TimeZoneName is unsupported. '
            'Correct old configuration explicitly or rebuild this preproduction database before applying 0086.';
    END IF;
END
$branch_timezone_preflight$;

ALTER TABLE core.Branches ADD COLUMN TimeZoneId VARCHAR(100);
UPDATE core.Branches b
SET TimeZoneId = c.TimeZoneName
FROM core.Companies c
WHERE c.CompanyID = b.CompanyID;
ALTER TABLE core.Branches
    ALTER COLUMN TimeZoneId SET NOT NULL,
    ADD CONSTRAINT fk_Branches_TimeZone FOREIGN KEY (TimeZoneId)
        REFERENCES core.SupportedTimeZones(TimeZoneId);

CREATE OR REPLACE FUNCTION core.fn_BusinessDate(
    p_TimeZoneId TEXT,
    p_At TIMESTAMPTZ DEFAULT NOW()
)
RETURNS DATE
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    IF p_At IS NULL THEN
        RAISE EXCEPTION 'BUSINESS_DATE_INSTANT_REQUIRED: instant must not be null';
    END IF;
    IF NOT core.fn_IsSupportedTimeZone(p_TimeZoneId) THEN
        RAISE EXCEPTION 'BUSINESS_DATE_TIME_ZONE_INVALID: unsupported timezone %', p_TimeZoneId;
    END IF;
    RETURN (p_At AT TIME ZONE p_TimeZoneId)::DATE;
END
$$;

CREATE OR REPLACE FUNCTION core.fn_BranchToday(
    p_CompanyID INTEGER,
    p_BranchID INTEGER,
    p_At TIMESTAMPTZ DEFAULT NOW()
)
RETURNS DATE
LANGUAGE SQL
STABLE
AS $$
    SELECT core.fn_BusinessDate(b.TimeZoneId, p_At)
    FROM core.Branches b
    WHERE b.CompanyID = p_CompanyID AND b.BranchID = p_BranchID
$$;

-- Replace Driver DATE intervals with absolute half-open assignment intervals.
DROP TRIGGER IF EXISTS trg_Drivers_HistoryImmutable ON core.Drivers;
DROP FUNCTION IF EXISTS core.trg_Drivers_HistoryImmutable();
ALTER TABLE core.Drivers
    DROP CONSTRAINT IF EXISTS ck_Drivers_EffectiveWindow,
    DROP CONSTRAINT IF EXISTS ck_Drivers_ClosedHistory,
    DROP CONSTRAINT IF EXISTS excl_Drivers_Employee_EffectiveWindow;
DROP FUNCTION IF EXISTS core.fn_EffectiveDriverProfile(INTEGER, INTEGER, DATE);
DROP FUNCTION IF EXISTS core.fn_DriverEffectiveRange(DATE, DATE);
ALTER TABLE core.Drivers
    DROP COLUMN EffectiveFrom,
    DROP COLUMN EffectiveTo,
    ADD COLUMN EffectiveFromAt TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN EffectiveToAt TIMESTAMPTZ;

CREATE OR REPLACE FUNCTION core.fn_DriverEffectiveRange(
    p_From TIMESTAMPTZ,
    p_To TIMESTAMPTZ
)
RETURNS TSTZRANGE
LANGUAGE SQL
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT tstzrange(p_From, p_To, '[)')
$$;

ALTER TABLE core.Drivers
    ADD CONSTRAINT ck_Drivers_EffectiveWindowAt
        CHECK (EffectiveToAt IS NULL OR EffectiveToAt > EffectiveFromAt
               OR (DriverStatus = 'Terminated' AND EffectiveToAt = EffectiveFromAt)),
    ADD CONSTRAINT ck_Drivers_ClosedHistoryAt
        CHECK (DriverStatus NOT IN ('Transferred', 'Terminated') OR EffectiveToAt IS NOT NULL),
    ADD CONSTRAINT excl_Drivers_Employee_AssignmentWindow
        EXCLUDE USING gist (
            EmployeeID WITH =,
            core.fn_DriverEffectiveRange(EffectiveFromAt, EffectiveToAt) WITH &&
        ) DEFERRABLE INITIALLY DEFERRED;

CREATE OR REPLACE FUNCTION core.trg_Drivers_HistoryImmutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_WorkforceOp TEXT := current_setting('flussra.workforce_op', TRUE);
BEGIN
    IF OLD.DriverStatus IN ('Transferred', 'Terminated')
       AND (NEW.DriverStatus IS DISTINCT FROM OLD.DriverStatus
            OR NEW.EffectiveFromAt IS DISTINCT FROM OLD.EffectiveFromAt
            OR NEW.EffectiveToAt IS DISTINCT FROM OLD.EffectiveToAt
            OR NEW.TransferredFromDriverID IS DISTINCT FROM OLD.TransferredFromDriverID
            OR NEW.TransferredToDriverID IS DISTINCT FROM OLD.TransferredToDriverID) THEN
        IF v_WorkforceOp = 'terminate'
           AND OLD.DriverStatus = 'Transferred'
           AND NEW.DriverStatus = 'Terminated'
           AND NEW.EffectiveFromAt IS NOT DISTINCT FROM OLD.EffectiveFromAt
           AND NEW.EffectiveToAt IS NOT NULL
           AND EXISTS (
               SELECT 1
               FROM core.Employees e
               JOIN core.Branches b
                 ON b.CompanyID = OLD.CompanyID AND b.BranchID = OLD.BranchID
               WHERE e.EmployeeID = OLD.EmployeeID
                 AND e.EmploymentStatus = 'Terminated'
                 AND e.TerminationDate IS NOT NULL
                 AND NEW.EffectiveToAt = GREATEST(
                     OLD.EffectiveFromAt,
                     LEAST(OLD.EffectiveToAt, core.fn_LocalDateBoundary(
                         b.TimeZoneId, e.TerminationDate + 1
                     ))
                 )
           )
           AND NEW.TransferredFromDriverID IS NOT DISTINCT FROM OLD.TransferredFromDriverID
           AND NEW.TransferredToDriverID IS NOT DISTINCT FROM OLD.TransferredToDriverID THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'Historical Driver status, assignment window, and lineage are immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_Drivers_HistoryImmutable
BEFORE UPDATE OF DriverStatus, EffectiveFromAt, EffectiveToAt,
                 TransferredFromDriverID, TransferredToDriverID ON core.Drivers
FOR EACH ROW EXECUTE FUNCTION core.trg_Drivers_HistoryImmutable();

CREATE OR REPLACE FUNCTION core.fn_EffectiveDriverProfile(
    p_CompanyID INTEGER,
    p_EmployeeID INTEGER,
    p_At TIMESTAMPTZ DEFAULT NOW()
)
RETURNS INTEGER
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_DriverID INTEGER;
    v_Count INTEGER;
BEGIN
    IF p_At IS NULL THEN
        RAISE EXCEPTION 'DRIVER_ASSIGNMENT_INSTANT_REQUIRED: instant must not be null';
    END IF;
    SELECT count(*), min(d.DriverID)
      INTO v_Count, v_DriverID
      FROM core.Drivers d
     WHERE d.CompanyID = p_CompanyID
       AND d.EmployeeID = p_EmployeeID
       AND core.fn_DriverEffectiveRange(d.EffectiveFromAt, d.EffectiveToAt) @> p_At;
    IF v_Count > 1 THEN
        RAISE EXCEPTION 'Multiple effective Driver profiles for Employee % in Company % at %',
            p_EmployeeID, p_CompanyID, p_At
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN v_DriverID;
END
$$;

CREATE OR REPLACE FUNCTION core.fn_LocalDateBoundary(
    p_TimeZoneId TEXT,
    p_LocalDate DATE
)
RETURNS TIMESTAMPTZ
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_LocalMidnight TIMESTAMP := p_LocalDate::TIMESTAMP;
    v_Nominal TIMESTAMPTZ;
    v_BeforeOffset INTERVAL;
    v_AfterOffset INTERVAL;
    v_Candidate TIMESTAMPTZ;
BEGIN
    IF p_LocalDate IS NULL OR NOT core.fn_IsSupportedTimeZone(p_TimeZoneId) THEN
        RAISE EXCEPTION 'WORK_DATE_BOUNDARY_INVALID: supported timezone and local date are required';
    END IF;
    v_Nominal := v_LocalMidnight AT TIME ZONE p_TimeZoneId;
    v_BeforeOffset := ((v_Nominal - INTERVAL '48 hours') AT TIME ZONE p_TimeZoneId)
                      - ((v_Nominal - INTERVAL '48 hours') AT TIME ZONE 'UTC');
    v_AfterOffset := ((v_Nominal + INTERVAL '48 hours') AT TIME ZONE p_TimeZoneId)
                     - ((v_Nominal + INTERVAL '48 hours') AT TIME ZONE 'UTC');
    SELECT candidate
      INTO v_Candidate
      FROM (
          SELECT v_Nominal AS candidate
          UNION ALL
          SELECT (v_LocalMidnight - v_BeforeOffset)::TIMESTAMP AT TIME ZONE 'UTC'
          UNION ALL
          SELECT (v_LocalMidnight - v_AfterOffset)::TIMESTAMP AT TIME ZONE 'UTC'
      ) candidates
     WHERE candidate AT TIME ZONE p_TimeZoneId = v_LocalMidnight
     ORDER BY candidate
     LIMIT 1;
    RETURN COALESCE(v_Candidate, v_Nominal);
END
$$;

CREATE OR REPLACE FUNCTION core.fn_WorkDateRange(
    p_TimeZoneId TEXT,
    p_Start DATE,
    p_End DATE
)
RETURNS TSTZRANGE
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    IF p_Start IS NULL OR p_End IS NULL OR p_End < p_Start THEN
        RAISE EXCEPTION 'WORK_DATE_RANGE_INVALID: start and end dates are required and end must not precede start';
    END IF;
    IF NOT core.fn_IsSupportedTimeZone(p_TimeZoneId) THEN
        RAISE EXCEPTION 'WORK_DATE_TIME_ZONE_INVALID: unsupported timezone %', p_TimeZoneId;
    END IF;
    RETURN tstzrange(
        core.fn_LocalDateBoundary(p_TimeZoneId, p_Start),
        core.fn_LocalDateBoundary(p_TimeZoneId, p_End + 1),
        '[)'
    );
END
$$;

CREATE OR REPLACE FUNCTION core.fn_BranchWorkDateRange(
    p_CompanyID INTEGER,
    p_BranchID INTEGER,
    p_Start DATE,
    p_End DATE
)
RETURNS TSTZRANGE
LANGUAGE SQL
STABLE
AS $$
    SELECT core.fn_WorkDateRange(b.TimeZoneId, p_Start, p_End)
    FROM core.Branches b
    WHERE b.CompanyID = p_CompanyID AND b.BranchID = p_BranchID
$$;

CREATE OR REPLACE FUNCTION core.fn_DriverCoversWorkDate(
    p_CompanyID INTEGER,
    p_DriverID INTEGER,
    p_BranchID INTEGER,
    p_WorkDate DATE
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM core.Drivers d
        CROSS JOIN LATERAL (
            SELECT core.fn_BranchWorkDateRange(p_CompanyID, p_BranchID, p_WorkDate, p_WorkDate) AS WorkWindow
        ) w
        WHERE d.CompanyID = p_CompanyID
          AND d.DriverID = p_DriverID
          AND d.BranchID = p_BranchID
          AND core.fn_DriverEffectiveRange(d.EffectiveFromAt, d.EffectiveToAt) && w.WorkWindow
    )
$$;

-- Transfer requests carry the requested boundary as an instant; NULL is an
-- immediate request resolved at administrative completion time.
ALTER TABLE core.DriverTransferRequests
    DROP CONSTRAINT IF EXISTS ck_DTR_EffectiveDate;
ALTER TABLE core.DriverTransferRequests
    DROP COLUMN EffectiveDate,
    ADD COLUMN EffectiveAt TIMESTAMPTZ;
ALTER TABLE core.DriverTransferRequests
    ADD CONSTRAINT ck_DTR_CompletedEffectiveAt
        CHECK (Status <> 'Completed' OR EffectiveAt IS NOT NULL);

-- Eligibility is frozen as exact Branch-local WorkDates. Existing periods are
-- prohibited by the preflight, so no historical snapshots are fabricated.
ALTER TABLE payroll.PayrollPeriodDriverEligibility
    DROP COLUMN DriverEffectiveFromSnapshot,
    DROP COLUMN DriverEffectiveToSnapshot,
    ADD COLUMN DriverEffectiveFromAtSnapshot TIMESTAMPTZ NOT NULL,
    ADD COLUMN DriverEffectiveToAtSnapshot TIMESTAMPTZ,
    ADD COLUMN BranchTimeZoneIdSnapshot VARCHAR(100) NOT NULL,
    ADD COLUMN EligibleWorkDates DATE[] NOT NULL,
    ADD CONSTRAINT fk_PPDE_SnapshotTimeZone FOREIGN KEY (BranchTimeZoneIdSnapshot)
        REFERENCES core.SupportedTimeZones(TimeZoneId);

-- Company-wide timezone authority is retired at this head.
DROP FUNCTION IF EXISTS core.fn_CompanyToday(INTEGER);
ALTER TABLE core.Companies DROP COLUMN TimeZoneName;
