-- 0038: PayrollFinalLines INSERT guard
--
-- Blocks direct INSERT into payroll.payrollfinallines unless the
-- transaction-local GUC app.allow_payroll_final_line_insert is set to 'true'.
--
-- The finalize_period service sets this GUC (is_local=true) inside its
-- engine.begin() transaction immediately before writing final lines.
-- The GUC resets automatically at commit/rollback, so no path other than
-- the controlled finalization service can insert final lines.
--
-- Limitation: any DB session that can run
--   SELECT set_config('app.allow_payroll_final_line_insert','true',false)
-- can bypass this guard.  This is an application-convention guard; it catches
-- accidental/ORM-level inserts but does not replace a role-level REVOKE.

-- 1. Guard trigger function
CREATE OR REPLACE FUNCTION payroll.fn_guard_final_line_insert()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_allowed TEXT;
    v_status  TEXT;
BEGIN
    -- Check transaction-local GUC set by finalize_period.
    v_allowed := current_setting('app.allow_payroll_final_line_insert', true);

    IF v_allowed IS DISTINCT FROM 'true' THEN
        -- Include period status in the message for diagnostics.
        SELECT p.status INTO v_status
        FROM   payroll.payrollperiods p
        WHERE  p.payrollperiodid = NEW.payrollperiodid;

        RAISE EXCEPTION
            'payroll_insert_guard: Payroll final lines can only be inserted by the '
            'finalization process. (period_id=%, period_status=%)',
            NEW.payrollperiodid, COALESCE(v_status, 'unknown')
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$;

-- 2. Trigger (BEFORE INSERT, every row)
DROP TRIGGER IF EXISTS trg_guard_final_line_insert ON payroll.payrollfinallines;
CREATE TRIGGER trg_guard_final_line_insert
    BEFORE INSERT ON payroll.payrollfinallines
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_final_line_insert();
