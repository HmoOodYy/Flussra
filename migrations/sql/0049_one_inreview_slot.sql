-- =============================================================================
-- 0049: One-InReview-per-branch slot enforcement
--
-- Adds a partial unique index so that at most one payroll period may have
-- Status = 'InReview' per CompanyID/BranchID at any time.
--
-- Schema changes:
--   1. ux_payrollperiods_oneinreviewperbranch  partial unique index
--
-- Preflight checks (run in order before DDL):
--   a. Diagnostic (non-blocking): multi-slot occupancy across all active statuses.
--   b. Diagnostic (non-blocking): Draft-alone anomalies (no Open sibling).
--   c. BLOCKING: refuse if any branch already has more than one InReview period.
--
-- Downgrade drops only ux_payrollperiods_oneinreviewperbranch.
-- No columns, CHECK constraints, FKs, or existing indexes are altered.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. Diagnostic: multi-slot occupancy across all active statuses
--    Non-blocking -- emits NOTICE; does not block the migration.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    _row  RECORD;
    _msg  TEXT := '';
BEGIN
    FOR _row IN (
        SELECT
            CompanyID,
            BranchID,
            Status,
            COUNT(*)                                         AS SlotCount,
            ARRAY_AGG(PayrollPeriodID ORDER BY StartDate)   AS PeriodIDs
        FROM   payroll.PayrollPeriods
        WHERE  Status IN ('Draft', 'Open', 'InReview', 'Returned')
        GROUP  BY CompanyID, BranchID, Status
        HAVING COUNT(*) > 1
    ) LOOP
        _msg := _msg || format(
            'Status=%s CompanyID=%s BranchID=%s count=%s IDs=%s; ',
            _row.Status, _row.CompanyID, _row.BranchID,
            _row.SlotCount, _row.PeriodIDs
        );
    END LOOP;
    IF _msg <> '' THEN
        RAISE NOTICE 'CP-1B diagnostic (non-blocking multi-slot): %', _msg;
    END IF;
END;
$$;


-- ---------------------------------------------------------------------------
-- 2. Diagnostic: Draft-alone anomalies (Draft present, no Open sibling)
--    Non-blocking -- emits NOTICE; does not block the migration.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    _count INT;
BEGIN
    SELECT COUNT(*) INTO _count
    FROM (
        SELECT CompanyID, BranchID
        FROM   payroll.PayrollPeriods
        GROUP  BY CompanyID, BranchID
        HAVING COUNT(*) FILTER (WHERE Status = 'Draft') > 0
           AND COUNT(*) FILTER (WHERE Status = 'Open')  = 0
    ) AS _sub;
    IF _count > 0 THEN
        RAISE NOTICE
            'CP-1B diagnostic (non-blocking draft-alone): % company/branch pair(s) '
            'have Draft period(s) with no Open sibling. Not repaired in CP-1B.',
            _count;
    END IF;
END;
$$;


-- ---------------------------------------------------------------------------
-- 3. BLOCKING preflight: refuse if any branch has duplicate InReview periods.
--    RAISE EXCEPTION aborts the migration; callers must reconcile first.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    _row  RECORD;
    _msg  TEXT := '';
BEGIN
    FOR _row IN (
        SELECT
            CompanyID,
            BranchID,
            COUNT(*)                                          AS InReviewCount,
            ARRAY_AGG(PayrollPeriodID ORDER BY StartDate)    AS PeriodIDs
        FROM   payroll.PayrollPeriods
        WHERE  Status = 'InReview'
        GROUP  BY CompanyID, BranchID
        HAVING COUNT(*) > 1
    ) LOOP
        _msg := _msg || format(
            'CompanyID=%s BranchID=%s count=%s PeriodIDs=%s; ',
            _row.CompanyID, _row.BranchID,
            _row.InReviewCount, _row.PeriodIDs
        );
    END LOOP;
    IF _msg <> '' THEN
        RAISE EXCEPTION
            'Migration 0049 preflight failed: duplicate InReview periods detected. '
            'Resolve all duplicate InReview periods before upgrading. '
            'Affected entries: %', _msg;
    END IF;
END;
$$;


-- ---------------------------------------------------------------------------
-- 4. Create the one-InReview-per-branch partial unique index
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX ux_payrollperiods_oneinreviewperbranch
    ON payroll.PayrollPeriods (CompanyID, BranchID)
    WHERE Status = 'InReview';
