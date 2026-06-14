-- =============================================================================
-- Precheck queries for migrations 0025 and 0026 (NOT VALID constraint validation)
--
-- Purpose:
--   Migrations 0025 and 0026 add composite FK constraints with NOT VALID.
--   NOT VALID skips the initial scan of existing rows and only enforces on
--   future INSERT / UPDATE.  Before running VALIDATE CONSTRAINT (which scans
--   all existing rows), a DBA must confirm that no existing rows would fail.
--
--   Run each SELECT below on the target database.  Every query should return
--   zero rows.  If any rows are returned, do NOT run VALIDATE CONSTRAINT --
--   investigate and remediate first (contact the engineering team; do not
--   silently rewrite business data).
--
-- How to run:
--   psql -d <database> -f precheck_0025_0026.sql
--
-- After confirming zero rows, validate constraints one at a time:
--   ALTER TABLE payroll.PayrollDraftLines VALIDATE CONSTRAINT fk_DraftLines_Period_Company_Branch;
--   ALTER TABLE payroll.PayrollDraftLines VALIDATE CONSTRAINT fk_DraftLines_Driver_Company_Branch;
--   ALTER TABLE payroll.PayrollFinalLines VALIDATE CONSTRAINT fk_FinalLines_Period_Company_Branch;
--   ALTER TABLE payroll.PayrollFinalLines VALIDATE CONSTRAINT fk_FinalLines_Driver_Company_Branch;
--   ALTER TABLE payroll.DriverRates       VALIDATE CONSTRAINT fk_DriverRates_Driver_Company_Branch;
--   ALTER TABLE payroll.DriverPayRules    VALIDATE CONSTRAINT fk_DriverPayRules_Driver_Company_Branch;
--   ALTER TABLE core.Drivers             VALIDATE CONSTRAINT fk_Drivers_Branch_Company;
--   ALTER TABLE payroll.PayrollPeriods   VALIDATE CONSTRAINT fk_PayrollPeriods_Branch_Company;
--
-- Each VALIDATE CONSTRAINT acquires a SHARE UPDATE EXCLUSIVE lock (non-blocking
-- for reads and DML).  Run during a low-traffic window.
-- =============================================================================

-- 1. core.Drivers: BranchID / CompanyID mismatch
-- Finds Driver rows where the (BranchID, CompanyID) combination does not match
-- any row in core.Branches -- the branch belongs to a different company.
-- Expected result: 0 rows.
SELECT
    d.driverid,
    d.companyid             AS driver_companyid,
    d.branchid              AS driver_branchid,
    b.companyid             AS branch_actual_companyid,
    b.branchcode,
    b.branchname
FROM core.Drivers d
JOIN core.Branches b ON b.branchid = d.branchid
WHERE b.companyid <> d.companyid;


-- 2. payroll.PayrollPeriods: BranchID / CompanyID mismatch
-- Expected result: 0 rows.
SELECT
    p.payrollperiodid,
    p.companyid             AS period_companyid,
    p.branchid              AS period_branchid,
    b.companyid             AS branch_actual_companyid,
    b.branchcode,
    b.branchname
FROM payroll.PayrollPeriods p
JOIN core.Branches b ON b.branchid = p.branchid
WHERE b.companyid <> p.companyid;


-- 3. payroll.PayrollDraftLines: period branch / company mismatch
-- DraftLine.CompanyID or DraftLine.BranchID does not match its period's.
-- Expected result: 0 rows.
SELECT
    dl.draftlineid,
    dl.companyid            AS line_companyid,
    dl.branchid             AS line_branchid,
    pp.companyid            AS period_companyid,
    pp.branchid             AS period_branchid,
    pp.payrollperiodid
FROM payroll.PayrollDraftLines dl
JOIN payroll.PayrollPeriods pp ON pp.payrollperiodid = dl.payrollperiodid
WHERE dl.companyid <> pp.companyid
   OR dl.branchid  <> pp.branchid;


-- 4. payroll.PayrollDraftLines: driver branch / company mismatch
-- DraftLine.CompanyID or DraftLine.BranchID does not match its driver's.
-- Expected result: 0 rows.
SELECT
    dl.draftlineid,
    dl.companyid            AS line_companyid,
    dl.branchid             AS line_branchid,
    d.companyid             AS driver_companyid,
    d.branchid              AS driver_branchid,
    d.driverid
FROM payroll.PayrollDraftLines dl
JOIN core.Drivers d ON d.driverid = dl.driverid
WHERE dl.companyid <> d.companyid
   OR dl.branchid  <> d.branchid;


-- 5. payroll.PayrollFinalLines: period branch / company mismatch
-- Expected result: 0 rows.
SELECT
    fl.finallineid,
    fl.companyid            AS line_companyid,
    fl.branchid             AS line_branchid,
    pp.companyid            AS period_companyid,
    pp.branchid             AS period_branchid
FROM payroll.PayrollFinalLines fl
JOIN payroll.PayrollPeriods pp ON pp.payrollperiodid = fl.payrollperiodid
WHERE fl.companyid <> pp.companyid
   OR fl.branchid  <> pp.branchid;


-- 6. payroll.PayrollFinalLines: driver branch / company mismatch
-- Expected result: 0 rows.
SELECT
    fl.finallineid,
    fl.companyid            AS line_companyid,
    fl.branchid             AS line_branchid,
    d.companyid             AS driver_companyid,
    d.branchid              AS driver_branchid
FROM payroll.PayrollFinalLines fl
JOIN core.Drivers d ON d.driverid = fl.driverid
WHERE fl.companyid <> d.companyid
   OR fl.branchid  <> d.branchid;


-- 7. payroll.DriverRates: driver branch / company mismatch
-- Expected result: 0 rows.
SELECT
    dr.driverrateid,
    dr.companyid            AS rate_companyid,
    dr.branchid             AS rate_branchid,
    d.companyid             AS driver_companyid,
    d.branchid              AS driver_branchid,
    dr.status
FROM payroll.DriverRates dr
JOIN core.Drivers d ON d.driverid = dr.driverid
WHERE dr.companyid <> d.companyid
   OR dr.branchid  <> d.branchid;


-- 8. payroll.DriverPayRules: driver branch / company mismatch
-- Expected result: 0 rows.
SELECT
    dpr.driverpayruleid,
    dpr.companyid           AS rule_companyid,
    dpr.branchid            AS rule_branchid,
    d.companyid             AS driver_companyid,
    d.branchid              AS driver_branchid,
    dpr.status
FROM payroll.DriverPayRules dpr
JOIN core.Drivers d ON d.driverid = dpr.driverid
WHERE dpr.companyid <> d.companyid
   OR dpr.branchid  <> d.branchid;
