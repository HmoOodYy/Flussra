-- P1a: database-enforced Driver identity and effective-window invariants.

CREATE OR REPLACE FUNCTION core.fn_DriverEffectiveRange(p_From DATE, p_To DATE)
RETURNS DATERANGE
LANGUAGE SQL
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT CASE
        WHEN p_From IS NOT NULL AND p_To IS NOT NULL AND p_To < p_From
            THEN 'empty'::daterange
        ELSE daterange(p_From, p_To, '[]')
    END
$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM core.drivers d
        JOIN core.employees e ON e.employeeid = d.employeeid
        WHERE d.companyid <> e.companyid
    ) THEN
        RAISE EXCEPTION '0072 blocked: core.Drivers contains Employee/Company mismatches';
    END IF;

    IF EXISTS (
        SELECT 1 FROM core.drivers d
        LEFT JOIN core.branches b
          ON b.branchid = d.branchid AND b.companyid = d.companyid
        WHERE b.branchid IS NULL
    ) THEN
        RAISE EXCEPTION '0072 blocked: core.Drivers contains Branch/Company mismatches';
    END IF;

    IF EXISTS (
        SELECT 1 FROM core.drivers
        WHERE driverstatus NOT IN ('Active', 'Inactive', 'OnLeave', 'Transferred', 'Terminated')
    ) THEN
        RAISE EXCEPTION '0072 blocked: core.Drivers contains unsupported DriverStatus values';
    END IF;

    IF EXISTS (
        SELECT 1 FROM core.employees
        WHERE employmentstatus NOT IN ('Active', 'Inactive', 'Terminated')
    ) THEN
        RAISE EXCEPTION '0072 blocked: core.Employees contains unsupported EmploymentStatus values';
    END IF;

    IF EXISTS (
        SELECT 1 FROM core.drivers
        WHERE effectiveto IS NOT NULL AND effectivefrom IS NOT NULL
          AND effectiveto < effectivefrom
          AND NOT (driverstatus = 'Terminated' AND effectiveto = effectivefrom - 1)
    ) THEN
        RAISE EXCEPTION '0072 blocked: core.Drivers contains invalid effective windows';
    END IF;

    IF EXISTS (
        SELECT 1 FROM core.drivers
        WHERE driverstatus IN ('Transferred', 'Terminated') AND effectiveto IS NULL
    ) THEN
        RAISE EXCEPTION '0072 blocked: closed Driver history has an open effective window';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM core.drivers a
        JOIN core.drivers b ON b.employeeid = a.employeeid AND b.driverid > a.driverid
        WHERE core.fn_DriverEffectiveRange(a.effectivefrom, a.effectiveto)
           && core.fn_DriverEffectiveRange(b.effectivefrom, b.effectiveto)
    ) THEN
        RAISE EXCEPTION '0072 blocked: core.Drivers contains overlapping Employee effective windows';
    END IF;
END $$;

CREATE UNIQUE INDEX ux_Employees_Employee_Company
    ON core.Employees (EmployeeID, CompanyID);

ALTER TABLE core.Drivers
    ADD CONSTRAINT fk_Drivers_Employee_Company
    FOREIGN KEY (EmployeeID, CompanyID)
    REFERENCES core.Employees (EmployeeID, CompanyID);

ALTER TABLE core.Drivers
    VALIDATE CONSTRAINT fk_Drivers_Branch_Company;

ALTER TABLE core.Drivers
    ADD CONSTRAINT ck_Drivers_Status
    CHECK (DriverStatus IN ('Active', 'Inactive', 'OnLeave', 'Transferred', 'Terminated'));

ALTER TABLE core.Employees
    ADD CONSTRAINT ck_Employees_EmploymentStatus
    CHECK (EmploymentStatus IN ('Active', 'Inactive', 'Terminated'));

ALTER TABLE core.Drivers
    ADD CONSTRAINT ck_Drivers_EffectiveWindow
    CHECK (
        EffectiveFrom IS NULL OR EffectiveTo IS NULL OR EffectiveTo >= EffectiveFrom
        OR (DriverStatus = 'Terminated' AND EffectiveTo = EffectiveFrom - 1)
    ),
    ADD CONSTRAINT ck_Drivers_ClosedHistory
    CHECK (DriverStatus NOT IN ('Transferred', 'Terminated') OR EffectiveTo IS NOT NULL);

ALTER TABLE core.Drivers
    ADD CONSTRAINT excl_Drivers_Employee_EffectiveWindow
    EXCLUDE USING gist (
        EmployeeID WITH =,
        core.fn_DriverEffectiveRange(EffectiveFrom, EffectiveTo) WITH &&
    ) DEFERRABLE INITIALLY DEFERRED;

CREATE OR REPLACE FUNCTION core.fn_CompanyToday(p_CompanyID INTEGER)
RETURNS DATE
LANGUAGE SQL
STABLE
AS $$
    SELECT (NOW() AT TIME ZONE c.TimeZoneName)::DATE
    FROM core.Companies c
    WHERE c.CompanyID = p_CompanyID
$$;

CREATE OR REPLACE FUNCTION core.fn_EffectiveDriverProfile(
    p_CompanyID INTEGER,
    p_EmployeeID INTEGER,
    p_OnDate DATE
)
RETURNS INTEGER
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_DriverID INTEGER;
    v_Count INTEGER;
BEGIN
    SELECT count(*), min(d.DriverID)
      INTO v_Count, v_DriverID
      FROM core.Drivers d
     WHERE d.CompanyID = p_CompanyID
       AND d.EmployeeID = p_EmployeeID
       AND core.fn_DriverEffectiveRange(d.EffectiveFrom, d.EffectiveTo) @> p_OnDate;

    IF v_Count > 1 THEN
        RAISE EXCEPTION 'Multiple effective Driver profiles for Employee % in Company % on %',
            p_EmployeeID, p_CompanyID, p_OnDate
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN v_DriverID;
END
$$;

CREATE OR REPLACE FUNCTION core.trg_Drivers_IdentityImmutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.BranchID IS DISTINCT FROM OLD.BranchID THEN
        RAISE EXCEPTION 'Driver BranchID is immutable; create a new Driver profile for a branch change';
    END IF;
    IF NEW.EmployeeID IS DISTINCT FROM OLD.EmployeeID
       OR NEW.CompanyID IS DISTINCT FROM OLD.CompanyID THEN
        RAISE EXCEPTION 'Driver EmployeeID and CompanyID are immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_Drivers_IdentityImmutable
BEFORE UPDATE OF BranchID, EmployeeID, CompanyID ON core.Drivers
FOR EACH ROW EXECUTE FUNCTION core.trg_Drivers_IdentityImmutable();

CREATE OR REPLACE FUNCTION core.trg_Drivers_HistoryImmutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_WorkforceOp TEXT := current_setting('flussra.workforce_op', TRUE);
BEGIN
    IF OLD.DriverStatus IN ('Transferred', 'Terminated')
       AND (NEW.DriverStatus IS DISTINCT FROM OLD.DriverStatus
            OR NEW.EffectiveFrom IS DISTINCT FROM OLD.EffectiveFrom
            OR NEW.EffectiveTo IS DISTINCT FROM OLD.EffectiveTo
            OR NEW.TransferredFromDriverID IS DISTINCT FROM OLD.TransferredFromDriverID
            OR NEW.TransferredToDriverID IS DISTINCT FROM OLD.TransferredToDriverID) THEN
        IF v_WorkforceOp = 'terminate'
           AND OLD.DriverStatus = 'Transferred'
           AND NEW.DriverStatus = 'Terminated'
           AND NEW.EffectiveFrom IS NOT DISTINCT FROM OLD.EffectiveFrom
           AND NEW.EffectiveTo IS NOT NULL
           AND NEW.EffectiveTo <= OLD.EffectiveTo
           AND EXISTS (
               SELECT 1
               FROM core.Employees e
               WHERE e.EmployeeID = OLD.EmployeeID
                 AND e.EmploymentStatus = 'Terminated'
                 AND e.TerminationDate IS NOT NULL
                 AND NEW.EffectiveTo = LEAST(OLD.EffectiveTo, e.TerminationDate)
           )
           AND NEW.TransferredFromDriverID IS NOT DISTINCT FROM OLD.TransferredFromDriverID
           AND NEW.TransferredToDriverID IS NOT DISTINCT FROM OLD.TransferredToDriverID THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'Historical Driver status, effective window, and lineage are immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_Drivers_HistoryImmutable
BEFORE UPDATE OF DriverStatus, EffectiveFrom, EffectiveTo,
                 TransferredFromDriverID, TransferredToDriverID ON core.Drivers
FOR EACH ROW EXECUTE FUNCTION core.trg_Drivers_HistoryImmutable();
