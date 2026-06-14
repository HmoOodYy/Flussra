-- Migration 0024: Add FinalGross and FinalDriverCount to vw_PayrollPeriodList
--
-- Purpose:
--   The Ledger page needs per-period total gross pay and driver count for
--   finalized (Locked/Archived) periods.  These are derivable from
--   PayrollFinalLines, which the view already aggregates for FinalLines count.
--   We extend the same fl_agg subquery to also compute FinalGross and
--   FinalDriverCount so the list endpoint returns everything the Ledger card
--   needs without N+1 queries.
--
-- This is a safe CREATE OR REPLACE — no data loss, no destructive DDL.

CREATE OR REPLACE VIEW app.vw_PayrollPeriodList AS
SELECT
    p.CompanyID,
    p.BranchID,
    b.BranchName,
    p.PayrollPeriodID,
    p.ParentPayrollPeriodID,
    p.PeriodCode,
    p.PeriodName,
    p.PeriodType,
    p.StartDate,
    p.EndDate,
    p.Status,
    COALESCE(dl_agg.DraftDrivers,               0)    AS DraftDrivers,
    COALESCE(dl_agg.DraftLines,                  0)    AS DraftLines,
    COALESCE(dl_agg.DraftLinesNeedingAttention,  0)    AS DraftLinesNeedingAttention,
    COALESCE(fl_agg.FinalLines,                  0)    AS FinalLines,
    COALESCE(fl_agg.FinalGross,                  0)    AS FinalGross,
    COALESCE(fl_agg.FinalDriverCount,            0)    AS FinalDriverCount
FROM payroll.PayrollPeriods AS p
JOIN core.Branches AS b ON b.BranchID = p.BranchID
LEFT JOIN (
    SELECT
        PayrollPeriodID,
        COUNT(DISTINCT DriverID)                                                                           AS DraftDrivers,
        COUNT(DraftLineID)                                                                                 AS DraftLines,
        SUM(CASE WHEN Status IN ('NeedsReview','Rejected') OR NeedsManagerReview THEN 1 ELSE 0 END)       AS DraftLinesNeedingAttention
    FROM payroll.PayrollDraftLines
    GROUP BY PayrollPeriodID
) AS dl_agg ON dl_agg.PayrollPeriodID = p.PayrollPeriodID
LEFT JOIN (
    SELECT
        PayrollPeriodID,
        COUNT(FinalLineID)          AS FinalLines,
        SUM(FinalAmount)            AS FinalGross,
        COUNT(DISTINCT DriverID)    AS FinalDriverCount
    FROM payroll.PayrollFinalLines
    GROUP BY PayrollPeriodID
) AS fl_agg ON fl_agg.PayrollPeriodID = p.PayrollPeriodID;
