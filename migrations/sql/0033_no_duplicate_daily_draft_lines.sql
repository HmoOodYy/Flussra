-- Migration 0033: Partial unique index — no duplicate active Daily draft lines.
--
-- Business key for Daily draft lines:
--   (CompanyID, PayrollPeriodID, DriverID, WorkDate, LineType)
--   WHERE LineScope = 'Daily' AND Status != 'Void'
--
-- Voided lines are excluded so void_draft_line + re-add remains possible.
-- Period Pay lines (LineScope = 'Period') are also excluded — they have a
-- different business key (no WorkDate) and are handled by a separate endpoint.
--
-- Pre-check: if any existing duplicates are found, the migration raises an
-- error with details so the entry team can clean up before upgrading.

DO $$
DECLARE
    v_dup_count INTEGER;
    v_examples  TEXT;
BEGIN
    -- Count duplicate groups
    SELECT COUNT(*)
    INTO   v_dup_count
    FROM (
        SELECT companyid, payrollperiodid, driverid, workdate, linetype
        FROM   payroll.payrolldraftlines
        WHERE  linescope = 'Daily'
          AND  status   != 'Void'
        GROUP BY companyid, payrollperiodid, driverid, workdate, linetype
        HAVING COUNT(*) > 1
    ) dupes;

    IF v_dup_count > 0 THEN
        SELECT string_agg(
                   format('company=%s period=%s driver=%s date=%s type=%s x%s',
                          companyid, payrollperiodid, driverid, workdate, linetype, cnt),
                   '; '
               )
        INTO   v_examples
        FROM (
            SELECT companyid, payrollperiodid, driverid, workdate, linetype,
                   COUNT(*) AS cnt
            FROM   payroll.payrolldraftlines
            WHERE  linescope = 'Daily'
              AND  status   != 'Void'
            GROUP BY companyid, payrollperiodid, driverid, workdate, linetype
            HAVING COUNT(*) > 1
            LIMIT 5
        ) sub;

        RAISE EXCEPTION
            'Migration 0033 blocked: % duplicate active Daily draft-line group(s) found. '
            'Void the extra lines before running this migration. Examples: %',
            v_dup_count, v_examples;
    END IF;
END $$;

-- Create partial unique index (only on active Daily lines)
CREATE UNIQUE INDEX IF NOT EXISTS
    uix_payrolldraftlines_daily_active_business_key
ON payroll.payrolldraftlines
    (companyid, payrollperiodid, driverid, workdate, linetype)
WHERE linescope = 'Daily'
  AND status   != 'Void';
