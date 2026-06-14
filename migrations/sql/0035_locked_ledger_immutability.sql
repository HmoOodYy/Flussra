-- =============================================================================
-- Migration 0035: DB-Level Immutability for Locked/Archived Payroll Ledger
--
-- Adds two pairs of trigger function + trigger:
--
--  1. payroll.fn_guard_final_line_immutable  /  payroll.trg_final_line_immutable
--     BEFORE UPDATE OR DELETE on payroll.PayrollFinalLines
--     Raises an exception when the parent PayrollPeriods row is Locked or Archived.
--     INSERT is intentionally NOT blocked: finalize_period sets status='Locked'
--     (Step 2) and then INSERTs final lines (Step 3) within the same transaction,
--     so blocking INSERT would break the finalization flow.
--
--  2. payroll.fn_guard_period_status_revert  /  payroll.trg_period_status_revert
--     BEFORE UPDATE OF Status on payroll.PayrollPeriods
--     Rules:
--       - Locked -> Archived  : allowed (supported forward transition)
--       - Locked -> anything else : rejected
--       - Archived -> anything  : rejected (terminal status)
--       - Non-final statuses   : pass through unchanged
--
-- Remaining lower-priority gap (INSERT not blocked):
--   A future migration could add a guard blocking INSERT into PayrollFinalLines
--   for Locked/Archived periods from outside the finalization transaction.
--   This requires either (a) a session variable / GUC flag set by the service
--   to signal "finalization in progress", or (b) restructuring finalization to
--   set Locked AFTER inserting lines.  Out of scope for Phase 3C.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Guard function: final-line immutability
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION payroll.fn_guard_final_line_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_status TEXT;
BEGIN
    SELECT p.status
    INTO   v_status
    FROM   payroll.payrollperiods p
    WHERE  p.payrollperiodid = OLD.payrollperiodid;

    IF v_status IN ('Locked', 'Archived') THEN
        RAISE EXCEPTION
            'payroll_ledger_immutable: Payroll final lines for Locked or Archived '
            'periods cannot be modified or deleted. (period_id=%, status=%)',
            OLD.payrollperiodid, v_status
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN OLD;  -- for DELETE; for UPDATE the trigger framework uses RETURN NEW
                 -- but PG only uses the return value of BEFORE DELETE triggers
                 -- to decide whether to proceed.  Returning OLD for both is safe:
                 -- for UPDATE PG ignores the return from a BEFORE DELETE trigger.
END;
$$;

-- For BEFORE UPDATE triggers PG requires RETURN NEW to proceed.
-- Re-define with correct return logic per TG_OP:
CREATE OR REPLACE FUNCTION payroll.fn_guard_final_line_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_status TEXT;
BEGIN
    SELECT p.status
    INTO   v_status
    FROM   payroll.payrollperiods p
    WHERE  p.payrollperiodid = OLD.payrollperiodid;

    IF v_status IN ('Locked', 'Archived') THEN
        RAISE EXCEPTION
            'payroll_ledger_immutable: Payroll final lines for Locked or Archived '
            'periods cannot be modified or deleted. (period_id=%, status=%)',
            OLD.payrollperiodid, v_status
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- Allow the operation to proceed.
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    ELSE
        RETURN NEW;
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- 2. Trigger: final-line immutability
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_final_line_immutable ON payroll.payrollfinallines;
CREATE TRIGGER trg_final_line_immutable
    BEFORE UPDATE OR DELETE ON payroll.payrollfinallines
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_final_line_immutable();

-- ---------------------------------------------------------------------------
-- 3. Guard function: period-status revert
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION payroll.fn_guard_period_status_revert()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    -- Only act when Status is actually changing.
    IF OLD.status = NEW.status THEN
        RETURN NEW;
    END IF;

    IF OLD.status = 'Locked' THEN
        -- Locked -> Archived is the only permitted forward transition.
        IF NEW.status = 'Archived' THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'payroll_status_immutable: Locked or Archived payroll periods cannot be '
            'reverted to an editable status. (period_id=%, attempted_transition=%->%)',
            OLD.payrollperiodid, OLD.status, NEW.status
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'Archived' THEN
        -- Archived is terminal — no transitions allowed.
        RAISE EXCEPTION
            'payroll_status_immutable: Locked or Archived payroll periods cannot be '
            'reverted to an editable status. (period_id=%, attempted_transition=%->%)',
            OLD.payrollperiodid, OLD.status, NEW.status
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- Non-final statuses: no restriction — pass through.
    RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- 4. Trigger: period-status revert
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_period_status_revert ON payroll.payrollperiods;
CREATE TRIGGER trg_period_status_revert
    BEFORE UPDATE OF status ON payroll.payrollperiods
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_period_status_revert();
