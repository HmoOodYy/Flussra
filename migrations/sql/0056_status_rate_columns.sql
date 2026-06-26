-- =============================================================================
-- Migration 0056: Status Rate Columns
--
-- Introduces the infrastructure for CP-2D2: status-key payment through
-- per-branch status rate columns backed by payroll.RateTypes.
--
-- Changes:
--   1. Seed STATUS_PAY system RateType (CompanyID=NULL).
--   2. Create payroll.StatusRateColumns with full data-integrity constraints:
--      - NormalizedColumnName for duplicate detection
--      - Unique active normalized name per (CompanyID, BranchID)
--      - Unique active RateTypeID per (CompanyID, BranchID)
--      - One active IsDefault row per BranchID (unique partial index)
--      - FK to Companies, Branches, RateTypes
--      - Trigger: RateType.CompanyID must be NULL or match StatusRateColumns.CompanyID
--   3. Seed one default StatusRateColumns row per (Company, Branch).
--   4. Add StatusRateColumnID FK column to PayrollStatusKeys with trigger:
--      - StatusRateColumn must belong to same branch/company as the StatusKey
--      - HoursValue must be > 0 when StatusRateColumnID is set
--   5. Add SourceSnapshot JSONB NULL to PayrollDraftLines.
--   6. Partial unique index: one active STATUS_PAYMENT draft line
--      per (period, driver, work-date) slot.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. Seed STATUS_PAY system RateType (CompanyID=NULL = system-level)
-- ---------------------------------------------------------------------------
INSERT INTO payroll.RateTypes (RateCode, RateName, UnitName, IsActive)
VALUES ('STATUS_PAY', 'Status Pay', 'Hour', TRUE)
ON CONFLICT (RateCode) DO UPDATE SET RateName = 'Status Pay';


-- ---------------------------------------------------------------------------
-- 2. Create payroll.StatusRateColumns
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payroll.StatusRateColumns (

    StatusRateColumnID      SERIAL          NOT NULL,
    CompanyID               INTEGER         NOT NULL,
    BranchID                INTEGER         NOT NULL,
    RateTypeID              INTEGER         NOT NULL,
    ColumnName              VARCHAR(200)    NOT NULL,
    NormalizedColumnName    VARCHAR(200)    NOT NULL DEFAULT '',
    IsDefault               BOOLEAN         NOT NULL DEFAULT FALSE,
    IsActive                BOOLEAN         NOT NULL DEFAULT TRUE,

    -- Audit
    CreatedByUserID         INTEGER,
    CreatedAtUtc            TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UpdatedByUserID         INTEGER,
    UpdatedAtUtc            TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_StatusRateColumns
        PRIMARY KEY (StatusRateColumnID),

    CONSTRAINT fk_SRC_Company
        FOREIGN KEY (CompanyID) REFERENCES core.Companies(CompanyID),

    CONSTRAINT fk_SRC_Branch
        FOREIGN KEY (BranchID)  REFERENCES core.Branches(BranchID),

    CONSTRAINT fk_SRC_RateType
        FOREIGN KEY (RateTypeID) REFERENCES payroll.RateTypes(RateTypeID)
);

-- Index for branch-scoped lookups
CREATE INDEX IF NOT EXISTS ix_StatusRateColumns_Branch
    ON payroll.StatusRateColumns (CompanyID, BranchID, IsActive);

-- At most one active default column per branch
CREATE UNIQUE INDEX IF NOT EXISTS ux_StatusRateColumns_BranchDefault
    ON payroll.StatusRateColumns (BranchID)
    WHERE IsDefault = TRUE AND IsActive = TRUE;

-- No two active columns in the same branch can share a normalized name
CREATE UNIQUE INDEX IF NOT EXISTS ux_SRC_NormalizedName
    ON payroll.StatusRateColumns (CompanyID, BranchID, NormalizedColumnName)
    WHERE IsActive = TRUE;

-- No two active columns in the same branch can share a RateType
CREATE UNIQUE INDEX IF NOT EXISTS ux_SRC_RateType_Branch
    ON payroll.StatusRateColumns (CompanyID, BranchID, RateTypeID)
    WHERE IsActive = TRUE;


-- Trigger: enforce ownership invariants on StatusRateColumns:
--   1. BranchID must belong to CompanyID (branch/company ownership).
--   2. RateTypes.CompanyID must be NULL (system) or equal StatusRateColumns.CompanyID.
--   3. Auto-populate NormalizedColumnName when not supplied.
CREATE OR REPLACE FUNCTION payroll.trg_fn_src_ratetype_owner()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    rt_company_id  INTEGER;
    branch_owner   INTEGER;
BEGIN
    -- 1. BranchID must belong to CompanyID
    SELECT companyid INTO branch_owner
    FROM core.branches
    WHERE branchid = NEW.branchid;

    IF branch_owner IS DISTINCT FROM NEW.companyid THEN
        RAISE EXCEPTION
            'StatusRateColumns: BranchID % belongs to company % but column is for company %',
            NEW.branchid, branch_owner, NEW.companyid
            USING ERRCODE = 'check_violation';
    END IF;

    -- 2. RateType.CompanyID must be NULL (system) or equal StatusRateColumns.CompanyID
    SELECT companyid INTO rt_company_id
    FROM payroll.ratetypes
    WHERE ratetypeid = NEW.ratetypeid;

    IF rt_company_id IS NOT NULL AND rt_company_id != NEW.companyid THEN
        RAISE EXCEPTION
            'StatusRateColumns: RateType % belongs to company % but column is for company %',
            NEW.ratetypeid, rt_company_id, NEW.companyid
            USING ERRCODE = 'check_violation';
    END IF;

    -- 3. Auto-populate NormalizedColumnName if not set
    IF NEW.normalizedcolumnname = '' THEN
        NEW.normalizedcolumnname := UPPER(TRIM(NEW.columnname));
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_src_ratetype_owner
    BEFORE INSERT OR UPDATE ON payroll.StatusRateColumns
    FOR EACH ROW EXECUTE FUNCTION payroll.trg_fn_src_ratetype_owner();


-- ---------------------------------------------------------------------------
-- 3. Seed one default StatusRateColumns row per (Company, Branch)
-- ---------------------------------------------------------------------------
INSERT INTO payroll.StatusRateColumns
    (CompanyID, BranchID, RateTypeID, ColumnName, NormalizedColumnName, IsDefault, IsActive)
SELECT
    b.CompanyID,
    b.BranchID,
    rt.RateTypeID,
    'Status Pay',
    'STATUS PAY',
    TRUE,
    TRUE
FROM core.Branches b
CROSS JOIN payroll.RateTypes rt
WHERE rt.RateCode = 'STATUS_PAY'
  AND b.BranchID IS NOT NULL
ON CONFLICT DO NOTHING;


-- ---------------------------------------------------------------------------
-- 4. Add StatusRateColumnID FK to PayrollStatusKeys
-- ---------------------------------------------------------------------------
ALTER TABLE payroll.PayrollStatusKeys
    ADD COLUMN IF NOT EXISTS StatusRateColumnID INTEGER;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'fk_psk_statusratecolumn'
          AND table_schema = 'payroll'
          AND table_name   = 'payrollstatuskeys'
    ) THEN
        ALTER TABLE payroll.PayrollStatusKeys
            ADD CONSTRAINT fk_PSK_StatusRateColumn
                FOREIGN KEY (StatusRateColumnID)
                REFERENCES payroll.StatusRateColumns(StatusRateColumnID)
                DEFERRABLE INITIALLY DEFERRED;
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS ix_PSK_StatusRateColumn
    ON payroll.PayrollStatusKeys (StatusRateColumnID)
    WHERE StatusRateColumnID IS NOT NULL;


-- Trigger: StatusKey.StatusRateColumnID must belong to same branch/company;
-- HoursValue must be > 0 when a rate column is linked.
CREATE OR REPLACE FUNCTION payroll.trg_fn_psk_src_branch()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    src_branch  INTEGER;
    src_company INTEGER;
BEGIN
    IF NEW.statusratecolumnid IS NOT NULL THEN
        SELECT branchid, companyid
          INTO src_branch, src_company
          FROM payroll.statusratecolumns
         WHERE statusratecolumnid = NEW.statusratecolumnid;

        IF src_branch IS DISTINCT FROM NEW.branchid
        OR src_company IS DISTINCT FROM NEW.companyid THEN
            RAISE EXCEPTION
                'StatusKey branch/company mismatch: StatusRateColumn % belongs to branch %, company % but StatusKey is for branch %, company %',
                NEW.statusratecolumnid, src_branch, src_company, NEW.branchid, NEW.companyid
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.hoursvalue IS NULL OR NEW.hoursvalue <= 0 THEN
            RAISE EXCEPTION
                'StatusKey: HoursValue must be > 0 when StatusRateColumnID is set'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_psk_src_branch
    BEFORE INSERT OR UPDATE ON payroll.PayrollStatusKeys
    FOR EACH ROW EXECUTE FUNCTION payroll.trg_fn_psk_src_branch();


-- ---------------------------------------------------------------------------
-- 5. Add SourceSnapshot JSONB to PayrollDraftLines
--    (PayrollFinalLines already has this column from migration 0039)
-- ---------------------------------------------------------------------------
ALTER TABLE payroll.PayrollDraftLines
    ADD COLUMN IF NOT EXISTS SourceSnapshot JSONB NULL;


-- ---------------------------------------------------------------------------
-- 6. Partial unique index: one active STATUS_PAYMENT line per slot
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS ux_DraftLines_StatusPayment_Slot
    ON payroll.PayrollDraftLines (PayrollPeriodID, DriverID, WorkDate)
    WHERE status != 'Void'
      AND SourceID LIKE 'STATUS_PAYMENT:%';
