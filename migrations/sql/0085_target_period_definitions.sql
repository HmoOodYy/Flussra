-- 0085: Target period definition runtime foundation (P4b).
--
-- Pre-production clean cutover. The Payroll Period is refounded around Company
-- PayDefinitions: the immutable period layout is payroll.PayrollPeriodDefinitions
-- (the former PayrollPeriodPayItems aggregate, renamed and re-keyed), and ordinary
-- source facts (PayrollDraftLines) reference a period definition by identity.
--
-- There is one period model after this migration. Nothing is converted, matched
-- by code or name, or backfilled, and no legacy-period runtime survives.
--
-- Preflight: the cutover is refused while ANY PayrollPeriod exists (every status).
-- Pre-P4b payroll-run data is disposable development data. Reset and reseed the
-- development database, then run the migration again. The check runs before any
-- structural statement and the migration is one transaction, so a failure leaves
-- the database exactly as it was.
--
-- Historical migrations 0001-0084 are not rewritten.

DO $preflight$
DECLARE
    existing_periods INTEGER;
BEGIN
    SELECT count(*) INTO existing_periods FROM payroll.PayrollPeriods;

    IF existing_periods > 0 THEN
        RAISE EXCEPTION
            'P4B_PRE_CUTOVER_PERIODS_REQUIRE_REBUILD: % payroll period(s) exist. '
            'Migration 0085 does not convert or delete payroll periods. '
            'Reset and reseed the development database, then run the migration again.',
            existing_periods;
    END IF;
END
$preflight$;

-- ---------------------------------------------------------------------------
-- 1. Keys that let period definitions and branch configuration be referenced
--    with exact ownership.
-- ---------------------------------------------------------------------------
ALTER TABLE payroll.RateDefinitions
    ADD CONSTRAINT uq_RateDefinitions_ID_PayDefinition
    UNIQUE (RateDefinitionID, PayDefinitionID);

ALTER TABLE payroll.BranchPayItemConfig
    ADD CONSTRAINT uq_BranchPayItemConfig_Scope
    UNIQUE (ConfigID, CompanyID, BranchID, PayDefinitionID);

-- ---------------------------------------------------------------------------
-- 2. PayrollPeriodPayItems -> PayrollPeriodDefinitions (the same aggregate).
-- ---------------------------------------------------------------------------
ALTER TABLE payroll.PayrollPeriodPayItems RENAME TO PayrollPeriodDefinitions;
ALTER TABLE payroll.PayrollPeriodDefinitions
    RENAME COLUMN PayrollPeriodPayItemID TO PayrollPeriodDefinitionID;
ALTER SEQUENCE payroll.payrollperiodpayitems_payrollperiodpayitemid_seq
    RENAME TO payrollperioddefinitions_payrollperioddefinitionid_seq;
ALTER TABLE payroll.PayrollPeriodDefinitions
    RENAME CONSTRAINT pk_PayrollPeriodPayItems TO pk_PayrollPeriodDefinitions;
ALTER TABLE payroll.PayrollPeriodDefinitions
    RENAME CONSTRAINT ck_PPPI_SortOrder TO ck_PPD_SortOrder;

ALTER TABLE payroll.PayrollPeriodDefinitions
    DROP CONSTRAINT fk_PPPI_Period,
    DROP CONSTRAINT fk_PPPI_PayItem,
    DROP CONSTRAINT ck_PPPI_ItemScope,
    DROP CONSTRAINT uq_PayrollPeriodPayItems_Period_PayItem;

DROP INDEX payroll.ix_PayrollPeriodPayItems_Period;
DROP INDEX payroll.ix_PayrollPeriodPayItems_Branch;
DROP INDEX payroll.ix_PayrollPeriodPayItems_PayItem;

-- The legacy PayItem identity and catalog metadata are not target authority.
ALTER TABLE payroll.PayrollPeriodDefinitions
    DROP COLUMN PayItemID,
    DROP COLUMN PayItemCode,
    DROP COLUMN PayItemName,
    DROP COLUMN DisplayLabel,
    DROP COLUMN Category,
    DROP COLUMN DataType,
    DROP COLUMN Unit,
    DROP COLUMN ItemScope,
    DROP COLUMN RateBehavior,
    DROP COLUMN AppearsInPayrollEntry,
    DROP COLUMN AppearsInLedger,
    DROP COLUMN AppearsInReports,
    DROP COLUMN RequiresRate,
    DROP COLUMN IsSystemStandard,
    DROP COLUMN IsCustom,
    DROP COLUMN PayItemStatusAtSnapshot,
    DROP COLUMN SnapshotEffectiveFrom,
    DROP COLUMN SourceBranchPayItemConfigID;

ALTER TABLE payroll.PayrollPeriodDefinitions
    ALTER COLUMN PayrollPeriodID TYPE INTEGER;

ALTER TABLE payroll.PayrollPeriodDefinitions
    ADD COLUMN PayDefinitionID                  INTEGER      NOT NULL,
    ADD COLUMN RateDefinitionID                 INTEGER      NOT NULL,
    ADD COLUMN DefinitionCodeSnapshot           VARCHAR(50)  NOT NULL,
    ADD COLUMN DefinitionNameSnapshot           VARCHAR(200) NOT NULL,
    ADD COLUMN InputTypeSnapshot                VARCHAR(30)  NOT NULL,
    ADD COLUMN UnitSnapshot                     VARCHAR(50),
    ADD COLUMN CalculationMethodSnapshot        VARCHAR(30)  NOT NULL,
    ADD COLUMN CalculationMethodVersionSnapshot INTEGER      NOT NULL,
    ADD COLUMN RateShapeSnapshot                VARCHAR(30)  NOT NULL,
    ADD COLUMN DefinitionStatusAtSnapshot       VARCHAR(30)  NOT NULL,
    ADD COLUMN SourceBranchConfigID             INTEGER      NOT NULL,
    ADD COLUMN SourceBranchConfigEffectiveFrom  DATE         NOT NULL,
    ADD COLUMN SourceBranchConfigEffectiveTo    DATE;

ALTER TABLE payroll.PayrollPeriodDefinitions
    ADD CONSTRAINT uq_PPD_Period_PayDefinition
        UNIQUE (PayrollPeriodID, PayDefinitionID),
    ADD CONSTRAINT uq_PPD_Scope
        UNIQUE (PayrollPeriodDefinitionID, PayrollPeriodID, CompanyID, BranchID),
    ADD CONSTRAINT fk_PPD_Period_Company_Branch
        FOREIGN KEY (PayrollPeriodID, CompanyID, BranchID)
        REFERENCES payroll.PayrollPeriods (PayrollPeriodID, CompanyID, BranchID)
        ON DELETE CASCADE,
    ADD CONSTRAINT fk_PPD_PayDefinition_Company
        FOREIGN KEY (PayDefinitionID, CompanyID)
        REFERENCES payroll.PayDefinitions (PayDefinitionID, CompanyID),
    ADD CONSTRAINT fk_PPD_RateDefinition_PayDefinition
        FOREIGN KEY (RateDefinitionID, PayDefinitionID)
        REFERENCES payroll.RateDefinitions (RateDefinitionID, PayDefinitionID),
    ADD CONSTRAINT fk_PPD_BranchConfig_Scope
        FOREIGN KEY (SourceBranchConfigID, CompanyID, BranchID, PayDefinitionID)
        REFERENCES payroll.BranchPayItemConfig (ConfigID, CompanyID, BranchID, PayDefinitionID),
    ADD CONSTRAINT ck_PPD_Code CHECK (btrim(DefinitionCodeSnapshot) <> ''),
    ADD CONSTRAINT ck_PPD_Name CHECK (btrim(DefinitionNameSnapshot) <> ''),
    ADD CONSTRAINT ck_PPD_InputType
        CHECK (InputTypeSnapshot IN ('Decimal', 'WholeNumber')),
    ADD CONSTRAINT ck_PPD_Method
        CHECK (CalculationMethodSnapshot IN ('PerUnit', 'OrdinalTier')),
    ADD CONSTRAINT ck_PPD_MethodVersion CHECK (CalculationMethodVersionSnapshot >= 1),
    ADD CONSTRAINT ck_PPD_RateShape
        CHECK (RateShapeSnapshot IN ('Scalar', 'OrdinalTierSchedule')),
    ADD CONSTRAINT ck_PPD_ShapeMatchesMethod
        CHECK ((CalculationMethodSnapshot = 'PerUnit'     AND RateShapeSnapshot = 'Scalar')
            OR (CalculationMethodSnapshot = 'OrdinalTier' AND RateShapeSnapshot = 'OrdinalTierSchedule')),
    ADD CONSTRAINT ck_PPD_OrdinalWholeNumber
        CHECK (CalculationMethodSnapshot <> 'OrdinalTier' OR InputTypeSnapshot = 'WholeNumber'),
    ADD CONSTRAINT ck_PPD_StatusAtSnapshot
        CHECK (DefinitionStatusAtSnapshot = 'Active'),
    ADD CONSTRAINT ck_PPD_ConfigBounds
        CHECK (SourceBranchConfigEffectiveTo IS NULL
               OR SourceBranchConfigEffectiveTo >= SourceBranchConfigEffectiveFrom);

CREATE INDEX ix_PPD_Period ON payroll.PayrollPeriodDefinitions (PayrollPeriodID);
CREATE INDEX ix_PPD_Branch ON payroll.PayrollPeriodDefinitions (CompanyID, BranchID);
CREATE INDEX ix_PPD_PayDefinition ON payroll.PayrollPeriodDefinitions (PayDefinitionID);
CREATE INDEX ix_PPD_RateDefinition ON payroll.PayrollPeriodDefinitions (RateDefinitionID);

-- A period definition is an immutable snapshot of what the period was created with.
CREATE FUNCTION payroll.fn_guard_period_definition_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'PERIOD_DEFINITION_IMMUTABLE: a payroll period definition snapshot cannot be modified'
        USING ERRCODE = 'check_violation';
END
$$;

CREATE TRIGGER trg_PayrollPeriodDefinitions_Immutable
BEFORE UPDATE ON payroll.PayrollPeriodDefinitions
FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_period_definition_immutable();

-- ---------------------------------------------------------------------------
-- 3. PayrollDraftLines: ordinary source rows are identified by period definition.
--    A row is EITHER a target ordinary source fact (PayrollPeriodDefinitionID,
--    no LineType, no stored money) OR a temporary Status/internal compatibility
--    row (LineType, no PayrollPeriodDefinitionID).
-- ---------------------------------------------------------------------------
ALTER TABLE payroll.PayrollDraftLines
    ADD COLUMN PayrollPeriodDefinitionID BIGINT,
    ALTER COLUMN LineType DROP NOT NULL;

ALTER TABLE payroll.PayrollDraftLines
    ADD CONSTRAINT fk_DraftLines_PeriodDefinition_Scope
        FOREIGN KEY (PayrollPeriodDefinitionID, PayrollPeriodID, CompanyID, BranchID)
        REFERENCES payroll.PayrollPeriodDefinitions
            (PayrollPeriodDefinitionID, PayrollPeriodID, CompanyID, BranchID),
    ADD CONSTRAINT ck_DraftLines_RowIdentity CHECK (
        (PayrollPeriodDefinitionID IS NOT NULL
            AND LineType IS NULL
            AND RateAmount IS NULL
            AND CalculatedAmount IS NULL
            AND NeedsManagerReview = FALSE
            AND SourceType IN ('Manual', 'Import'))
        OR
        (PayrollPeriodDefinitionID IS NULL AND LineType IS NOT NULL)
    );

CREATE UNIQUE INDEX ux_DraftLines_TargetBusinessKey
    ON payroll.PayrollDraftLines
        (CompanyID, PayrollPeriodID, DriverID, WorkDate, PayrollPeriodDefinitionID)
    WHERE Status <> 'Void' AND PayrollPeriodDefinitionID IS NOT NULL;

CREATE INDEX ix_DraftLines_PeriodDefinition
    ON payroll.PayrollDraftLines (PayrollPeriodDefinitionID)
    WHERE PayrollPeriodDefinitionID IS NOT NULL;
