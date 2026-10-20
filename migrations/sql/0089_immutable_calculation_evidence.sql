-- 0089: Immutable payroll calculation evidence (calculation contract v2).
--
-- Historical migrations remain immutable. The calculation snapshot is reshaped to freeze
-- exactly what the live calculation packet produces: official components (one rounding per
-- component, payable-rounding-v1), the Min/Max evaluation with its proration and rule
-- evidence, driver/employee identity, and target definition and rate provenance. Money is
-- never recalculated when it is frozen; FinalLines are a projection of the official
-- components of the approved snapshot.
--
-- This pre-production cutover keeps no legacy evidence shape: a snapshot, a final line or a
-- Locked/Archived period from before this contract cannot be reinterpreted, so the migration
-- refuses to run while any exists. Reset and reseed the development database, then apply
-- the migrations again. The check runs before any structural statement and the migration
-- is one transaction, so a refusal leaves the database exactly as it was.

DO $preflight$
DECLARE
    v_Count BIGINT;
BEGIN
    SELECT (SELECT COUNT(*) FROM payroll.PayrollCalculationSnapshots)
         + (SELECT COUNT(*) FROM payroll.PayrollFinalLines)
         + (SELECT COUNT(*) FROM payroll.PayrollPeriods WHERE Status IN ('Locked', 'Archived'))
    INTO v_Count;

    IF v_Count > 0 THEN
        RAISE EXCEPTION
            'IMMUTABLE_EVIDENCE_REBUILD_REQUIRED: % calculation snapshot, final line or finalized period row(s) exist from before calculation contract payroll-calculation-v2. '
            'They cannot be reinterpreted or backfilled. Reset and reseed this preproduction database, then apply migrations again.',
            v_Count
            USING ERRCODE = 'check_violation';
    END IF;
END
$preflight$;

-- ---------------------------------------------------------------------------
-- 1. Helpers
-- ---------------------------------------------------------------------------

-- A canonical date set is non-null, strictly ascending and free of NULL members, so the same
-- set always has one persisted form (the hash is computed over it).
CREATE OR REPLACE FUNCTION payroll.fn_IsCanonicalDateSet(p_Dates DATE[])
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT p_Dates IS NOT NULL
       AND COALESCE((
            SELECT bool_and(x.d IS NOT NULL AND (x.previous IS NULL OR x.d > x.previous))
            FROM (
                SELECT t.d, lag(t.d) OVER (ORDER BY t.ordinal) AS previous
                FROM unnest(p_Dates) WITH ORDINALITY AS t(d, ordinal)
            ) AS x
       ), TRUE)
$$;

-- ---------------------------------------------------------------------------
-- 2. Snapshot header
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.PayrollCalculationSnapshots
    DROP CONSTRAINT ck_payrollcalculationsnapshots_calculationversion;

ALTER TABLE payroll.PayrollCalculationSnapshots
    ADD COLUMN PeriodStartDate        DATE         NOT NULL,
    ADD COLUMN PeriodEndDate          DATE         NOT NULL,
    ADD COLUMN PayableRoundingVersion VARCHAR(50)  NOT NULL,
    ADD COLUMN MinMaxProrationVersion VARCHAR(50)  NOT NULL,
    ADD COLUMN ScheduledWorkDates     DATE[]       NOT NULL,
    ADD COLUMN Warnings               TEXT[]       NOT NULL;

ALTER TABLE payroll.PayrollCalculationSnapshots
    ADD CONSTRAINT ck_PayrollCalculationSnapshots_CalculationVersion
        CHECK (CalculationVersion = 'payroll-calculation-v2'),
    ADD CONSTRAINT ck_PayrollCalculationSnapshots_PeriodDates
        CHECK (PeriodEndDate >= PeriodStartDate),
    ADD CONSTRAINT ck_PayrollCalculationSnapshots_RuleVersions
        CHECK (btrim(PayableRoundingVersion) <> '' AND btrim(MinMaxProrationVersion) <> ''),
    ADD CONSTRAINT ck_PayrollCalculationSnapshots_ScheduledWorkDates
        CHECK (payroll.fn_IsCanonicalDateSet(ScheduledWorkDates)
               AND (cardinality(ScheduledWorkDates) = 0
                    OR (ScheduledWorkDates[1] >= PeriodStartDate
                        AND ScheduledWorkDates[cardinality(ScheduledWorkDates)] <= PeriodEndDate))),
    ADD CONSTRAINT ck_PayrollCalculationSnapshots_Warnings
        CHECK (array_position(Warnings, NULL) IS NULL);

-- ---------------------------------------------------------------------------
-- 3. Driver totals: Employee identity and reconciliation by construction
-- ---------------------------------------------------------------------------

-- BranchID is payroll/work provenance. No constraint added here couples it to the home
-- Branch of the DriverAssignment.
ALTER TABLE payroll.PayrollCalculationDriverTotals
    ADD COLUMN EmployeeID INTEGER NOT NULL;

ALTER TABLE payroll.PayrollCalculationDriverTotals
    ADD CONSTRAINT fk_PayrollCalculationDriverTotals_Driver_Employee
        FOREIGN KEY (DriverID, EmployeeID, CompanyID)
        REFERENCES core.Drivers (DriverID, EmployeeID, CompanyID),
    ADD CONSTRAINT uq_PayrollCalculationDriverTotals_Scope
        UNIQUE (PayrollCalculationDriverTotalID, PayrollCalculationSnapshotID, CompanyID, BranchID),
    ADD CONSTRAINT ck_PayrollCalculationDriverTotals_Reconciles
        CHECK (ExpectedPay = DailyPay + StatusPay + MinimumAdjustment + MaximumAdjustment + BonusTotal),
    ADD CONSTRAINT ck_PayrollCalculationDriverTotals_AdjustmentSigns
        CHECK (MinimumAdjustment >= 0 AND MaximumAdjustment <= 0);

-- ---------------------------------------------------------------------------
-- 4. Official components: one row per official payable amount
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.PayrollPeriodDefinitions
    ADD CONSTRAINT uq_PPD_Evidence_Scope
        UNIQUE (PayrollPeriodDefinitionID, PayrollPeriodID, CompanyID, BranchID,
                PayDefinitionID, RateDefinitionID);

ALTER TABLE payroll.DriverPayRules
    ADD CONSTRAINT uq_DriverPayRules_ID_Company UNIQUE (DriverPayRuleID, CompanyID);

CREATE TABLE payroll.PayrollCalculationSnapshotMinMaxEvaluations (
    PayrollCalculationSnapshotMinMaxEvaluationID BIGSERIAL PRIMARY KEY,
    PayrollCalculationSnapshotID                 BIGINT        NOT NULL,
    CompanyID                                    INTEGER       NOT NULL,
    BranchID                                     INTEGER       NOT NULL,
    PayrollPeriodID                              INTEGER       NOT NULL,
    PayrollCalculationDriverTotalID              BIGINT        NOT NULL,
    Status                                       VARCHAR(40)   NOT NULL,
    SelectionDate                                DATE          NOT NULL,
    ProrationVersion                             VARCHAR(50)   NOT NULL,
    Numerator                                    INTEGER,
    Denominator                                  INTEGER,
    BaseAmount                                   NUMERIC(18,4) NOT NULL,
    BaseOrdinaryAmount                           NUMERIC(18,4) NOT NULL,
    BaseStatusAmount                             NUMERIC(18,4) NOT NULL,
    ExcludedBonusAmount                          NUMERIC(18,4) NOT NULL,
    CountedWorkDates                             DATE[]        NOT NULL,

    CONSTRAINT uq_PayrollCalculationSnapshotMinMaxEvaluations_DriverTotal
        UNIQUE (PayrollCalculationDriverTotalID),
    CONSTRAINT uq_PayrollCalculationSnapshotMinMaxEvaluations_Scope
        UNIQUE (PayrollCalculationSnapshotMinMaxEvaluationID, PayrollCalculationSnapshotID,
                CompanyID, BranchID, PayrollPeriodID, PayrollCalculationDriverTotalID),
    CONSTRAINT fk_PayrollCalculationSnapshotMinMaxEvaluations_Snapshot
        FOREIGN KEY (PayrollCalculationSnapshotID, CompanyID, BranchID, PayrollPeriodID)
        REFERENCES payroll.PayrollCalculationSnapshots
            (PayrollCalculationSnapshotID, CompanyID, BranchID, PayrollPeriodID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollCalculationSnapshotMinMaxEvaluations_DriverTotal
        FOREIGN KEY (PayrollCalculationDriverTotalID, PayrollCalculationSnapshotID, CompanyID, BranchID)
        REFERENCES payroll.PayrollCalculationDriverTotals
            (PayrollCalculationDriverTotalID, PayrollCalculationSnapshotID, CompanyID, BranchID)
        ON DELETE RESTRICT,
    -- Only outcomes that do not block a submission can be frozen.
    CONSTRAINT ck_PayrollCalculationSnapshotMinMaxEvaluations_Status
        CHECK (Status IN ('APPLIED', 'NO_RULE', 'NO_SCHEDULED_DAYS', 'NO_ELIGIBLE_SCHEDULED_DAYS')),
    CONSTRAINT ck_PayrollCalculationSnapshotMinMaxEvaluations_ProrationVersion
        CHECK (btrim(ProrationVersion) <> ''),
    CONSTRAINT ck_PayrollCalculationSnapshotMinMaxEvaluations_Fraction
        CHECK ((Status = 'NO_RULE' AND Numerator IS NULL AND Denominator IS NULL)
            OR (Status = 'NO_SCHEDULED_DAYS' AND Numerator = 0 AND Denominator = 0)
            OR (Status = 'NO_ELIGIBLE_SCHEDULED_DAYS' AND Numerator = 0 AND Denominator > 0)
            OR (Status = 'APPLIED' AND Numerator > 0 AND Denominator >= Numerator)),
    CONSTRAINT ck_PayrollCalculationSnapshotMinMaxEvaluations_CountedWorkDates
        CHECK (payroll.fn_IsCanonicalDateSet(CountedWorkDates)
               AND cardinality(CountedWorkDates) = COALESCE(Numerator, 0)),
    CONSTRAINT ck_PayrollCalculationSnapshotMinMaxEvaluations_BaseComposition
        CHECK (BaseAmount = BaseOrdinaryAmount + BaseStatusAmount)
);

CREATE TABLE payroll.PayrollCalculationSnapshotMinMaxRules (
    PayrollCalculationSnapshotMinMaxRuleID       BIGSERIAL PRIMARY KEY,
    PayrollCalculationSnapshotMinMaxEvaluationID BIGINT        NOT NULL,
    PayrollCalculationSnapshotID                 BIGINT        NOT NULL,
    CompanyID                                    INTEGER       NOT NULL,
    BranchID                                     INTEGER       NOT NULL,
    PayrollPeriodID                              INTEGER       NOT NULL,
    PayrollCalculationDriverTotalID              BIGINT        NOT NULL,
    DriverPayRuleID                              INTEGER       NOT NULL,
    RuleType                                     VARCHAR(30)   NOT NULL,
    Amount                                       NUMERIC(18,4) NOT NULL,
    RuleStatus                                   VARCHAR(30)   NOT NULL,
    EffectiveFrom                                DATE          NOT NULL,
    EffectiveTo                                  DATE,
    ProratedAmount                               NUMERIC(18,4),
    CalendarFrequency                            VARCHAR(20)   NOT NULL,
    CalendarAnchorStartDate                      DATE          NOT NULL,
    CalendarCustomIntervalDays                   INTEGER,
    CalendarSourceBranchID                       INTEGER       NOT NULL,
    CalendarSourcePayrollSetupVersionID          BIGINT        NOT NULL,

    CONSTRAINT uq_PayrollCalculationSnapshotMinMaxRules_Evaluation_Type
        UNIQUE (PayrollCalculationSnapshotMinMaxEvaluationID, RuleType),
    CONSTRAINT uq_PayrollCalculationSnapshotMinMaxRules_Component
        UNIQUE (PayrollCalculationSnapshotMinMaxRuleID, DriverPayRuleID,
                PayrollCalculationDriverTotalID, RuleType),
    CONSTRAINT fk_PayrollCalculationSnapshotMinMaxRules_Evaluation
        FOREIGN KEY (PayrollCalculationSnapshotMinMaxEvaluationID, PayrollCalculationSnapshotID,
                     CompanyID, BranchID, PayrollPeriodID, PayrollCalculationDriverTotalID)
        REFERENCES payroll.PayrollCalculationSnapshotMinMaxEvaluations
            (PayrollCalculationSnapshotMinMaxEvaluationID, PayrollCalculationSnapshotID,
             CompanyID, BranchID, PayrollPeriodID, PayrollCalculationDriverTotalID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollCalculationSnapshotMinMaxRules_DriverPayRule
        FOREIGN KEY (DriverPayRuleID, CompanyID)
        REFERENCES payroll.DriverPayRules (DriverPayRuleID, CompanyID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollCalculationSnapshotMinMaxRules_CalendarBranch
        FOREIGN KEY (CalendarSourceBranchID, CompanyID)
        REFERENCES core.Branches (BranchID, CompanyID),
    CONSTRAINT fk_PayrollCalculationSnapshotMinMaxRules_CalendarVersion
        FOREIGN KEY (CalendarSourcePayrollSetupVersionID, CompanyID)
        REFERENCES payroll.PayrollSetupVersions (PayrollSetupVersionID, CompanyID),
    CONSTRAINT ck_PayrollCalculationSnapshotMinMaxRules_RuleType
        CHECK (RuleType IN ('MinimumPay', 'MaximumPay')),
    CONSTRAINT ck_PayrollCalculationSnapshotMinMaxRules_Amount
        CHECK (Amount > 0),
    CONSTRAINT ck_PayrollCalculationSnapshotMinMaxRules_RuleStatus
        CHECK (RuleStatus IN ('Active', 'Ended')),
    CONSTRAINT ck_PayrollCalculationSnapshotMinMaxRules_EffectiveDates
        CHECK (EffectiveTo IS NULL OR EffectiveTo >= EffectiveFrom),
    CONSTRAINT ck_PayrollCalculationSnapshotMinMaxRules_ProratedAmount
        CHECK (ProratedAmount IS NULL OR ProratedAmount >= 0),
    CONSTRAINT ck_PayrollCalculationSnapshotMinMaxRules_CalendarFrequency
        CHECK (CalendarFrequency IN ('Week', 'Biweek', 'Month', 'Custom')),
    CONSTRAINT ck_PayrollCalculationSnapshotMinMaxRules_CalendarInterval
        CHECK ((CalendarFrequency = 'Custom') = (CalendarCustomIntervalDays IS NOT NULL)
               AND (CalendarCustomIntervalDays IS NULL OR CalendarCustomIntervalDays > 0))
);

CREATE TABLE payroll.PayrollCalculationSnapshotComponents (
    PayrollCalculationSnapshotComponentID BIGSERIAL PRIMARY KEY,
    PayrollCalculationSnapshotID          BIGINT        NOT NULL,
    CompanyID                             INTEGER       NOT NULL,
    BranchID                              INTEGER       NOT NULL,
    PayrollPeriodID                       INTEGER       NOT NULL,
    PayrollCalculationDriverTotalID       BIGINT        NOT NULL,
    ComponentKind                         VARCHAR(30)   NOT NULL,
    PayrollPeriodDefinitionID             INTEGER,
    PayDefinitionID                       INTEGER,
    RateDefinitionID                      INTEGER,
    CalculationMethod                     VARCHAR(30),
    CalculationMethodVersion              INTEGER,
    DefinitionCodeSnapshot                VARCHAR(50),
    DefinitionNameSnapshot                VARCHAR(200),
    StatusRateColumnID                    INTEGER,
    StatusRateColumnNameSnapshot          VARCHAR(200),
    DriverPayRuleID                       INTEGER,
    PayrollCalculationSnapshotMinMaxRuleID BIGINT,
    RuleType                              VARCHAR(30),
    PreRoundingAmount                     NUMERIC(18,4) NOT NULL,
    OfficialAmount                        NUMERIC(18,4) NOT NULL,
    SourceCount                           INTEGER       NOT NULL,
    UnresolvedLineCount                   INTEGER       NOT NULL,

    CONSTRAINT uq_PayrollCalculationSnapshotComponents_Line
        UNIQUE (PayrollCalculationSnapshotComponentID, PayrollCalculationDriverTotalID,
                ComponentKind),
    CONSTRAINT uq_PayrollCalculationSnapshotComponents_FinalLine
        UNIQUE (PayrollCalculationSnapshotComponentID, PayrollCalculationSnapshotID),
    CONSTRAINT fk_PayrollCalculationSnapshotComponents_Snapshot
        FOREIGN KEY (PayrollCalculationSnapshotID, CompanyID, BranchID, PayrollPeriodID)
        REFERENCES payroll.PayrollCalculationSnapshots
            (PayrollCalculationSnapshotID, CompanyID, BranchID, PayrollPeriodID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollCalculationSnapshotComponents_DriverTotal
        FOREIGN KEY (PayrollCalculationDriverTotalID, PayrollCalculationSnapshotID, CompanyID, BranchID)
        REFERENCES payroll.PayrollCalculationDriverTotals
            (PayrollCalculationDriverTotalID, PayrollCalculationSnapshotID, CompanyID, BranchID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollCalculationSnapshotComponents_PeriodDefinition
        FOREIGN KEY (PayrollPeriodDefinitionID, PayrollPeriodID, CompanyID, BranchID,
                     PayDefinitionID, RateDefinitionID)
        REFERENCES payroll.PayrollPeriodDefinitions
            (PayrollPeriodDefinitionID, PayrollPeriodID, CompanyID, BranchID,
             PayDefinitionID, RateDefinitionID),
    CONSTRAINT fk_PayrollCalculationSnapshotComponents_StatusRateColumn
        FOREIGN KEY (StatusRateColumnID, CompanyID, BranchID)
        REFERENCES payroll.StatusRateColumns (StatusRateColumnID, CompanyID, BranchID),
    CONSTRAINT fk_PayrollCalculationSnapshotComponents_Rule
        FOREIGN KEY (PayrollCalculationSnapshotMinMaxRuleID, DriverPayRuleID,
                     PayrollCalculationDriverTotalID, RuleType)
        REFERENCES payroll.PayrollCalculationSnapshotMinMaxRules
            (PayrollCalculationSnapshotMinMaxRuleID, DriverPayRuleID,
             PayrollCalculationDriverTotalID, RuleType)
        ON DELETE RESTRICT,
    CONSTRAINT ck_PayrollCalculationSnapshotComponents_Kind
        CHECK (ComponentKind IN ('ORDINARY', 'STATUS', 'BONUS',
                                 'MINIMUM_ADJUSTMENT', 'MAXIMUM_ADJUSTMENT')),
    -- Which identity columns a component of each kind must (and must not) carry.
    CONSTRAINT ck_PayrollCalculationSnapshotComponents_Identity
        CHECK (
            (ComponentKind = 'ORDINARY'
                AND PayrollPeriodDefinitionID IS NOT NULL AND PayDefinitionID IS NOT NULL
                AND RateDefinitionID IS NOT NULL AND CalculationMethod IS NOT NULL
                AND CalculationMethodVersion IS NOT NULL
                AND DefinitionCodeSnapshot IS NOT NULL AND DefinitionNameSnapshot IS NOT NULL
                AND StatusRateColumnID IS NULL AND StatusRateColumnNameSnapshot IS NULL
                AND DriverPayRuleID IS NULL AND PayrollCalculationSnapshotMinMaxRuleID IS NULL
                AND RuleType IS NULL)
         OR (ComponentKind = 'STATUS'
                AND StatusRateColumnID IS NOT NULL AND StatusRateColumnNameSnapshot IS NOT NULL
                AND PayrollPeriodDefinitionID IS NULL AND PayDefinitionID IS NULL
                AND RateDefinitionID IS NULL AND CalculationMethod IS NULL
                AND CalculationMethodVersion IS NULL
                AND DefinitionCodeSnapshot IS NULL AND DefinitionNameSnapshot IS NULL
                AND DriverPayRuleID IS NULL AND PayrollCalculationSnapshotMinMaxRuleID IS NULL
                AND RuleType IS NULL)
         OR (ComponentKind = 'BONUS'
                AND PayrollPeriodDefinitionID IS NULL AND PayDefinitionID IS NULL
                AND RateDefinitionID IS NULL AND CalculationMethod IS NULL
                AND CalculationMethodVersion IS NULL
                AND DefinitionCodeSnapshot IS NULL AND DefinitionNameSnapshot IS NULL
                AND StatusRateColumnID IS NULL AND StatusRateColumnNameSnapshot IS NULL
                AND DriverPayRuleID IS NULL AND PayrollCalculationSnapshotMinMaxRuleID IS NULL
                AND RuleType IS NULL)
         OR (ComponentKind = 'MINIMUM_ADJUSTMENT'
                AND DriverPayRuleID IS NOT NULL AND PayrollCalculationSnapshotMinMaxRuleID IS NOT NULL
                AND RuleType = 'MinimumPay'
                AND PayrollPeriodDefinitionID IS NULL AND PayDefinitionID IS NULL
                AND RateDefinitionID IS NULL AND CalculationMethod IS NULL
                AND CalculationMethodVersion IS NULL
                AND DefinitionCodeSnapshot IS NULL AND DefinitionNameSnapshot IS NULL
                AND StatusRateColumnID IS NULL AND StatusRateColumnNameSnapshot IS NULL)
         OR (ComponentKind = 'MAXIMUM_ADJUSTMENT'
                AND DriverPayRuleID IS NOT NULL AND PayrollCalculationSnapshotMinMaxRuleID IS NOT NULL
                AND RuleType = 'MaximumPay'
                AND PayrollPeriodDefinitionID IS NULL AND PayDefinitionID IS NULL
                AND RateDefinitionID IS NULL AND CalculationMethod IS NULL
                AND CalculationMethodVersion IS NULL
                AND DefinitionCodeSnapshot IS NULL AND DefinitionNameSnapshot IS NULL
                AND StatusRateColumnID IS NULL AND StatusRateColumnNameSnapshot IS NULL)
        ),
    CONSTRAINT ck_PayrollCalculationSnapshotComponents_Method
        CHECK (CalculationMethod IS NULL OR CalculationMethod IN ('PerUnit', 'OrdinalTier')),
    CONSTRAINT ck_PayrollCalculationSnapshotComponents_MethodVersion
        CHECK (CalculationMethodVersion IS NULL OR CalculationMethodVersion >= 1),
    CONSTRAINT ck_PayrollCalculationSnapshotComponents_Labels
        CHECK ((DefinitionCodeSnapshot IS NULL OR btrim(DefinitionCodeSnapshot) <> '')
           AND (DefinitionNameSnapshot IS NULL OR btrim(DefinitionNameSnapshot) <> '')
           AND (StatusRateColumnNameSnapshot IS NULL OR btrim(StatusRateColumnNameSnapshot) <> '')),
    -- Blockers reject a submission, so a frozen component never has an unresolved line.
    CONSTRAINT ck_PayrollCalculationSnapshotComponents_Resolved
        CHECK (UnresolvedLineCount = 0),
    -- Rule adjustments have no source lines; every other component has at least one.
    CONSTRAINT ck_PayrollCalculationSnapshotComponents_SourceCount
        CHECK ((ComponentKind IN ('MINIMUM_ADJUSTMENT', 'MAXIMUM_ADJUSTMENT') AND SourceCount = 0)
            OR (ComponentKind IN ('ORDINARY', 'STATUS', 'BONUS') AND SourceCount >= 1)),
    CONSTRAINT ck_PayrollCalculationSnapshotComponents_AdjustmentSigns
        CHECK (ComponentKind <> 'MINIMUM_ADJUSTMENT' OR (PreRoundingAmount >= 0 AND OfficialAmount >= 0)),
    CONSTRAINT ck_PayrollCalculationSnapshotComponents_AdjustmentSignsMax
        CHECK (ComponentKind <> 'MAXIMUM_ADJUSTMENT' OR (PreRoundingAmount <= 0 AND OfficialAmount <= 0))
);

-- One component per (driver total, kind, identity).
CREATE UNIQUE INDEX ux_PayrollCalculationSnapshotComponents_Identity
    ON payroll.PayrollCalculationSnapshotComponents
        (PayrollCalculationDriverTotalID, ComponentKind,
         COALESCE(PayrollPeriodDefinitionID, 0),
         COALESCE(StatusRateColumnID, 0),
         COALESCE(DriverPayRuleID, 0));
CREATE UNIQUE INDEX ux_PayrollCalculationSnapshotComponents_SingleKind
    ON payroll.PayrollCalculationSnapshotComponents (PayrollCalculationDriverTotalID, ComponentKind)
    WHERE ComponentKind IN ('BONUS', 'MINIMUM_ADJUSTMENT', 'MAXIMUM_ADJUSTMENT');
CREATE INDEX ix_PayrollCalculationSnapshotComponents_Snapshot
    ON payroll.PayrollCalculationSnapshotComponents (PayrollCalculationSnapshotID);

-- ---------------------------------------------------------------------------
-- 5. Snapshot lines: per-source detail with target provenance
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.PayrollCalculationSnapshotLines
    DROP COLUMN PayItemID;

ALTER TABLE payroll.PayrollCalculationSnapshotLines
    ADD COLUMN PayrollCalculationSnapshotComponentID BIGINT      NOT NULL,
    ADD COLUMN PayrollPeriodDefinitionID             INTEGER,
    ADD COLUMN PayDefinitionID                       INTEGER,
    ADD COLUMN RateDefinitionID                      INTEGER,
    ADD COLUMN DriverRateScheduleID                  INTEGER,
    ADD COLUMN CalculationStatus                     VARCHAR(30),
    ADD COLUMN CalculationVersion                    VARCHAR(50),
    ADD COLUMN StatusRateColumnID                    INTEGER;

ALTER TABLE payroll.PayrollCalculationSnapshotLines
    ADD CONSTRAINT uq_PayrollCalculationSnapshotLines_Source
        UNIQUE (PayrollCalculationDriverTotalID, SourceType, SourceID),
    ADD CONSTRAINT fk_PayrollCalculationSnapshotLines_Component
        FOREIGN KEY (PayrollCalculationSnapshotComponentID, PayrollCalculationDriverTotalID, LineType)
        REFERENCES payroll.PayrollCalculationSnapshotComponents
            (PayrollCalculationSnapshotComponentID, PayrollCalculationDriverTotalID, ComponentKind)
        ON DELETE RESTRICT,
    ADD CONSTRAINT fk_PayrollCalculationSnapshotLines_DriverRateSchedule
        FOREIGN KEY (DriverRateScheduleID, RateDefinitionID)
        REFERENCES payroll.DriverRateSchedules (DriverRateScheduleID, RateDefinitionID),
    ADD CONSTRAINT fk_PayrollCalculationSnapshotLines_StatusRateColumn
        FOREIGN KEY (StatusRateColumnID)
        REFERENCES payroll.StatusRateColumns (StatusRateColumnID),
    -- The line type is the kind of the one component the line contributes to.
    ADD CONSTRAINT ck_PayrollCalculationSnapshotLines_Source
        CHECK (
            (SourceType = 'DraftLine' AND LineType = 'ORDINARY' AND LineScope = 'Daily'
                AND SourceID IS NOT NULL AND WorkDate IS NOT NULL AND Quantity IS NOT NULL
                AND PayrollPeriodDefinitionID IS NOT NULL AND PayDefinitionID IS NOT NULL
                AND RateDefinitionID IS NOT NULL AND DriverRateScheduleID IS NOT NULL
                AND CalculationStatus = 'Calculated' AND CalculationVersion IS NOT NULL
                AND ResolvedRateAmount IS NOT NULL AND UsedRateDefinitionID IS NOT NULL
                AND StatusRateColumnID IS NULL AND RateTypeID IS NULL AND DriverRateID IS NULL
                AND BonusEventID IS NULL)
         OR (SourceType = 'StatusEntryState' AND LineType = 'STATUS' AND LineScope = 'Daily'
                AND SourceID IS NOT NULL AND WorkDate IS NOT NULL AND Quantity IS NOT NULL
                AND StatusRateColumnID IS NOT NULL AND RateTypeID IS NOT NULL
                AND DriverRateID IS NOT NULL AND ResolvedRateAmount IS NOT NULL
                AND UsedRateDefinitionID IS NOT NULL
                AND PayrollPeriodDefinitionID IS NULL AND PayDefinitionID IS NULL
                AND RateDefinitionID IS NULL AND DriverRateScheduleID IS NULL
                AND CalculationStatus IS NULL AND CalculationVersion IS NULL
                AND BonusEventID IS NULL)
         OR (SourceType = 'BonusEvent' AND LineType = 'BONUS' AND LineScope = 'Period'
                AND SourceID IS NOT NULL AND BonusEventID IS NOT NULL AND WorkDate IS NULL
                AND Quantity IS NULL AND ResolvedRateAmount IS NULL
                AND UsedRateDefinitionID IS NULL
                AND PayrollPeriodDefinitionID IS NULL AND PayDefinitionID IS NULL
                AND RateDefinitionID IS NULL AND DriverRateScheduleID IS NULL
                AND CalculationStatus IS NULL AND CalculationVersion IS NULL
                AND StatusRateColumnID IS NULL AND RateTypeID IS NULL AND DriverRateID IS NULL)
        );

CREATE INDEX ix_PayrollCalculationSnapshotLines_Component
    ON payroll.PayrollCalculationSnapshotLines (PayrollCalculationSnapshotComponentID);
CREATE INDEX ix_PayrollCalculationSnapshotLines_DriverRate
    ON payroll.PayrollCalculationSnapshotLines (DriverRateID) WHERE DriverRateID IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 6. Used-rate evidence: target rate schedules and the legacy Status rates
-- ---------------------------------------------------------------------------
--
-- DriverPayRules are no longer captured here: the Min/Max rule evidence above is their one
-- authority.

ALTER TABLE payroll.PayrollCalculationSnapshotUsedRateDefinitions
    DROP CONSTRAINT ck_payrollcalculationsnapshotusedratedefinitions_kind,
    DROP CONSTRAINT fk_payrollcalculationsnapshotusedratedefinitions_payitem,
    DROP CONSTRAINT fk_payrollcalculationsnapshotusedratedefinitions_driverpayrule;

ALTER TABLE payroll.PayrollCalculationSnapshotUsedRateDefinitions
    DROP COLUMN PayItemID,
    DROP COLUMN DriverPayRuleID,
    DROP COLUMN RateBehaviorSnapshot,
    DROP COLUMN BlockSizeSnapshot,
    DROP COLUMN RoundingRuleSnapshot,
    DROP COLUMN RuleTypeSnapshot,
    DROP COLUMN RuleAmountSnapshot,
    DROP COLUMN RuleStatusSnapshot;

ALTER TABLE payroll.PayrollCalculationSnapshotUsedRateDefinitions
    ADD COLUMN DriverRateScheduleID        INTEGER,
    ADD COLUMN RateDefinitionID            INTEGER,
    ADD COLUMN PayDefinitionID             INTEGER,
    ADD COLUMN RateComponentDefinitionID   INTEGER;

ALTER TABLE payroll.PayrollCalculationSnapshotUsedRateDefinitions
    ADD CONSTRAINT fk_PayrollCalculationSnapshotUsedRateDefinitions_Schedule
        FOREIGN KEY (DriverRateScheduleID, RateDefinitionID)
        REFERENCES payroll.DriverRateSchedules (DriverRateScheduleID, RateDefinitionID)
        ON DELETE RESTRICT,
    ADD CONSTRAINT fk_PayrollCalculationSnapshotUsedRateDefinitions_RateDefinition
        FOREIGN KEY (RateDefinitionID, PayDefinitionID)
        REFERENCES payroll.RateDefinitions (RateDefinitionID, PayDefinitionID),
    ADD CONSTRAINT fk_PayrollCalculationSnapshotUsedRateDefinitions_Component
        FOREIGN KEY (RateComponentDefinitionID, RateDefinitionID)
        REFERENCES payroll.RateComponentDefinitions (RateComponentDefinitionID, RateDefinitionID),
    ADD CONSTRAINT ck_PayrollCalculationSnapshotUsedRateDefinitions_Kind
        CHECK (
            (EvidenceKind = 'DriverRateSchedule' AND SourceTypeSnapshot = 'DraftLine'
                AND DriverRateScheduleID IS NOT NULL AND RateDefinitionID IS NOT NULL
                AND PayDefinitionID IS NOT NULL AND RateComponentDefinitionID IS NOT NULL
                AND RateAmountSnapshot IS NOT NULL AND EffectiveFromSnapshot IS NOT NULL
                AND RateStatusSnapshot IS NOT NULL
                AND RateTypeID IS NULL AND DriverRateID IS NULL)
         OR (EvidenceKind = 'StatusDriverRate' AND SourceTypeSnapshot = 'StatusEntryState'
                AND DriverRateID IS NOT NULL AND RateTypeID IS NOT NULL
                AND RateAmountSnapshot IS NOT NULL AND EffectiveFromSnapshot IS NOT NULL
                AND RateStatusSnapshot IS NOT NULL
                AND DriverRateScheduleID IS NULL AND RateDefinitionID IS NULL
                AND PayDefinitionID IS NULL AND RateComponentDefinitionID IS NULL)
        );

CREATE INDEX ix_PayrollCalculationSnapshotUsedRateDefinitions_DriverRate
    ON payroll.PayrollCalculationSnapshotUsedRateDefinitions (DriverRateID)
    WHERE DriverRateID IS NOT NULL;

CREATE OR REPLACE FUNCTION payroll.fn_guard_used_rate_definition_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    v_company_id INTEGER;
    v_branch_id INTEGER;
    v_period_id INTEGER;
    v_assignment_id INTEGER;
    v_driver_id INTEGER;
    v_rate_type_id INTEGER;
    v_rate_definition_id INTEGER;
BEGIN
    SELECT companyid, branchid, payrollperiodid
    INTO v_company_id, v_branch_id, v_period_id
    FROM payroll.payrollcalculationsnapshots
    WHERE payrollcalculationsnapshotid = NEW.payrollcalculationsnapshotid;

    IF v_company_id IS DISTINCT FROM NEW.companyid
       OR v_branch_id IS DISTINCT FROM NEW.branchid
       OR v_period_id IS DISTINCT FROM NEW.payrollperiodid THEN
        RAISE EXCEPTION
            'used_rate_definition_scope: snapshot scope does not match definition scope'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.evidencekind = 'DriverRateSchedule' THEN
        -- An ordinary rate schedule belongs to the permanent Driver, so it is in scope for
        -- any assignment of that Driver whatever Branch the work was attributed to.
        SELECT companyid, driverid, ratedefinitionid
        INTO v_company_id, v_driver_id, v_rate_definition_id
        FROM payroll.driverrateschedules
        WHERE driverratescheduleid = NEW.driverratescheduleid;

        IF v_company_id IS DISTINCT FROM NEW.companyid
           OR v_driver_id IS DISTINCT FROM NEW.driverid
           OR v_rate_definition_id IS DISTINCT FROM NEW.ratedefinitionid THEN
            RAISE EXCEPTION
                'used_rate_definition_scope: driver rate schedule does not match definition scope'
                USING ERRCODE = 'check_violation';
        END IF;
    ELSE
        SELECT companyid, branchid, driverassignmentid, ratetypeid
        INTO v_company_id, v_branch_id, v_assignment_id, v_rate_type_id
        FROM payroll.driverrates
        WHERE driverrateid = NEW.driverrateid;

        IF v_company_id IS DISTINCT FROM NEW.companyid
           OR v_branch_id IS DISTINCT FROM NEW.branchid
           OR v_assignment_id IS DISTINCT FROM NEW.driverassignmentid
           OR v_rate_type_id IS DISTINCT FROM NEW.ratetypeid THEN
            RAISE EXCEPTION
                'used_rate_definition_scope: driver rate does not match definition scope'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$function$;

-- A line is tied to its component, and its rate evidence to the same snapshot, the same
-- DriverAssignment and the same rate identity and amount the line itself records.
CREATE OR REPLACE FUNCTION payroll.fn_guard_snapshot_line_used_rate_definition_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    v_line_snapshot_id BIGINT;
    v_line_assignment_id INTEGER;
    v_definition RECORD;
BEGIN
    SELECT payrollcalculationsnapshotid, driverassignmentid
    INTO v_line_snapshot_id, v_line_assignment_id
    FROM payroll.payrollcalculationdrivertotals
    WHERE payrollcalculationdrivertotalid = NEW.payrollcalculationdrivertotalid;

    IF NEW.usedratedefinitionid IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT * INTO v_definition
    FROM payroll.payrollcalculationsnapshotusedratedefinitions
    WHERE payrollcalculationsnapshotusedratedefinitionid = NEW.usedratedefinitionid;

    IF v_line_snapshot_id IS DISTINCT FROM v_definition.payrollcalculationsnapshotid THEN
        RAISE EXCEPTION
            'snapshot_line_used_rate_definition_scope: definition belongs to another snapshot'
            USING ERRCODE = 'check_violation';
    END IF;

    IF v_line_assignment_id IS DISTINCT FROM v_definition.driverassignmentid
       OR v_definition.rateamountsnapshot IS DISTINCT FROM NEW.resolvedrateamount
       OR NOT (
            (NEW.sourcetype = 'DraftLine'
                AND v_definition.evidencekind = 'DriverRateSchedule'
                AND v_definition.driverratescheduleid IS NOT DISTINCT FROM NEW.driverratescheduleid
                AND v_definition.ratedefinitionid IS NOT DISTINCT FROM NEW.ratedefinitionid)
         OR (NEW.sourcetype = 'StatusEntryState'
                AND v_definition.evidencekind = 'StatusDriverRate'
                AND v_definition.driverrateid IS NOT DISTINCT FROM NEW.driverrateid
                AND v_definition.ratetypeid IS NOT DISTINCT FROM NEW.ratetypeid)
       ) THEN
        RAISE EXCEPTION
            'snapshot_line_used_rate_definition_scope: definition does not describe the line rate'
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$function$;

-- ---------------------------------------------------------------------------
-- 7. Scope guards for the Min/Max evidence and the components
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION payroll.fn_guard_snapshot_minmax_evaluation_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    v_snapshot RECORD;
BEGIN
    SELECT periodstartdate, scheduledworkdates
    INTO v_snapshot
    FROM payroll.payrollcalculationsnapshots
    WHERE payrollcalculationsnapshotid = NEW.payrollcalculationsnapshotid;

    -- The rule is selected at the period start, and its fraction is counted over the
    -- frozen scheduled days of the snapshot.
    IF NEW.selectiondate IS DISTINCT FROM v_snapshot.periodstartdate THEN
        RAISE EXCEPTION
            'snapshot_minmax_evaluation_scope: the selection date is not the period start'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.denominator IS NOT NULL
       AND NEW.denominator IS DISTINCT FROM cardinality(v_snapshot.scheduledworkdates) THEN
        RAISE EXCEPTION
            'snapshot_minmax_evaluation_scope: the denominator is not the scheduled day count'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NOT (NEW.countedworkdates <@ v_snapshot.scheduledworkdates) THEN
        RAISE EXCEPTION
            'snapshot_minmax_evaluation_scope: counted days are not scheduled days'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$function$;

CREATE TRIGGER trg_PayrollCalculationSnapshotMinMaxEvaluations_Scope
    BEFORE INSERT ON payroll.PayrollCalculationSnapshotMinMaxEvaluations
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_snapshot_minmax_evaluation_scope();

CREATE OR REPLACE FUNCTION payroll.fn_guard_snapshot_minmax_rule_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    v_status VARCHAR(40);
    v_driver_id INTEGER;
    v_rule RECORD;
BEGIN
    SELECT e.status, dt.driverid
    INTO v_status, v_driver_id
    FROM payroll.payrollcalculationsnapshotminmaxevaluations e
    JOIN payroll.payrollcalculationdrivertotals dt
      ON dt.payrollcalculationdrivertotalid = e.payrollcalculationdrivertotalid
    WHERE e.payrollcalculationsnapshotminmaxevaluationid = NEW.payrollcalculationsnapshotminmaxevaluationid;

    -- A rule belongs to the permanent Driver of the evaluated total, whatever Branch the
    -- work was attributed to.
    SELECT companyid, driverid, ruletype
    INTO v_rule
    FROM payroll.driverpayrules
    WHERE driverpayruleid = NEW.driverpayruleid;

    IF v_rule.companyid IS DISTINCT FROM NEW.companyid
       OR v_rule.driverid IS DISTINCT FROM v_driver_id
       OR v_rule.ruletype IS DISTINCT FROM NEW.ruletype THEN
        RAISE EXCEPTION
            'snapshot_minmax_rule_scope: the pay rule does not belong to the evaluated Driver'
            USING ERRCODE = 'check_violation';
    END IF;
    IF v_status = 'NO_RULE' THEN
        RAISE EXCEPTION
            'snapshot_minmax_rule_scope: an evaluation without a rule records no rule'
            USING ERRCODE = 'check_violation';
    END IF;
    -- A prorated amount exists exactly when the rule was applied.
    IF (v_status = 'APPLIED') <> (NEW.proratedamount IS NOT NULL) THEN
        RAISE EXCEPTION
            'snapshot_minmax_rule_scope: a prorated amount exists only for an applied evaluation'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$function$;

CREATE TRIGGER trg_PayrollCalculationSnapshotMinMaxRules_Scope
    BEFORE INSERT ON payroll.PayrollCalculationSnapshotMinMaxRules
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_snapshot_minmax_rule_scope();

-- A line contributes to a component of the same DriverAssignment total (the foreign key)
-- and a rule adjustment component names a rule evidence row of the same Driver total, so
-- the FinalLine projection of a component is exact.
CREATE OR REPLACE FUNCTION payroll.fn_guard_snapshot_line_component_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    v_component RECORD;
BEGIN
    SELECT payrollperioddefinitionid, statusratecolumnid
    INTO v_component
    FROM payroll.payrollcalculationsnapshotcomponents
    WHERE payrollcalculationsnapshotcomponentid = NEW.payrollcalculationsnapshotcomponentid;

    IF v_component.payrollperioddefinitionid IS DISTINCT FROM NEW.payrollperioddefinitionid
       OR v_component.statusratecolumnid IS DISTINCT FROM NEW.statusratecolumnid THEN
        RAISE EXCEPTION
            'snapshot_line_component_scope: the line does not match its component identity'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$function$;

CREATE TRIGGER trg_PayrollCalculationSnapshotLines_ComponentScope
    BEFORE INSERT ON payroll.PayrollCalculationSnapshotLines
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_snapshot_line_component_scope();

-- ---------------------------------------------------------------------------
-- 8. Immutability of the new evidence
-- ---------------------------------------------------------------------------

CREATE TRIGGER trg_PayrollCalculationSnapshotComponents_Immutable
    BEFORE UPDATE OR DELETE ON payroll.PayrollCalculationSnapshotComponents
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_calculation_snapshot_immutable();

CREATE TRIGGER trg_PayrollCalculationSnapshotMinMaxEvaluations_Immutable
    BEFORE UPDATE OR DELETE ON payroll.PayrollCalculationSnapshotMinMaxEvaluations
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_calculation_snapshot_immutable();

CREATE TRIGGER trg_PayrollCalculationSnapshotMinMaxRules_Immutable
    BEFORE UPDATE OR DELETE ON payroll.PayrollCalculationSnapshotMinMaxRules
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_calculation_snapshot_immutable();

-- ---------------------------------------------------------------------------
-- 9. FinalLines: one line per official component of the approved snapshot
-- ---------------------------------------------------------------------------
--
-- FinalAmount is the component's official amount, so the FinalLines of a DriverAssignment
-- sum exactly to the frozen ExpectedPay. Per-day detail stays readable from the immutable
-- snapshot lines. WorkDate, DraftLineID, BonusEventID, PayItemID, RateTypeID, DriverRateID
-- and RateBehavior are not used by a component line. Nothing here limits a Driver or
-- period to one financial result for ever: the key is the component of a snapshot.

ALTER TABLE payroll.PayrollFinalLines
    ADD COLUMN PayrollCalculationSnapshotID          BIGINT       NOT NULL,
    ADD COLUMN PayrollCalculationSnapshotComponentID BIGINT       NOT NULL,
    ADD COLUMN EmployeeID                            INTEGER      NOT NULL,
    ADD COLUMN ComponentKind                         VARCHAR(30)  NOT NULL,
    ADD COLUMN PayrollPeriodDefinitionID             INTEGER,
    ADD COLUMN PayDefinitionID                       INTEGER;

ALTER TABLE payroll.PayrollFinalLines
    ALTER COLUMN SourceSnapshot SET NOT NULL;

ALTER TABLE payroll.PayrollFinalLines
    ADD CONSTRAINT uq_PayrollFinalLines_Component
        UNIQUE (PayrollCalculationSnapshotComponentID),
    ADD CONSTRAINT fk_PayrollFinalLines_Component
        FOREIGN KEY (PayrollCalculationSnapshotComponentID, PayrollCalculationSnapshotID)
        REFERENCES payroll.PayrollCalculationSnapshotComponents
            (PayrollCalculationSnapshotComponentID, PayrollCalculationSnapshotID)
        ON DELETE RESTRICT,
    ADD CONSTRAINT fk_PayrollFinalLines_Snapshot
        FOREIGN KEY (PayrollCalculationSnapshotID, CompanyID, BranchID, PayrollPeriodID)
        REFERENCES payroll.PayrollCalculationSnapshots
            (PayrollCalculationSnapshotID, CompanyID, BranchID, PayrollPeriodID)
        ON DELETE RESTRICT,
    ADD CONSTRAINT fk_PayrollFinalLines_Driver_Employee
        FOREIGN KEY (DriverID, EmployeeID, CompanyID)
        REFERENCES core.Drivers (DriverID, EmployeeID, CompanyID),
    ADD CONSTRAINT ck_PayrollFinalLines_ComponentKind
        CHECK (ComponentKind IN ('ORDINARY', 'STATUS', 'BONUS',
                                 'MINIMUM_ADJUSTMENT', 'MAXIMUM_ADJUSTMENT')),
    -- LineType keeps the ledger vocabulary: the kind of the component.
    ADD CONSTRAINT ck_PayrollFinalLines_ComponentLine
        CHECK (LineScope = 'Period'
           AND SourceType = 'SnapshotComponent' AND SourceID IS NOT NULL
           AND DraftLineID IS NULL AND BonusEventID IS NULL AND WorkDate IS NULL
           AND PayItemID IS NULL AND RateTypeID IS NULL AND DriverRateID IS NULL
           AND RateBehavior IS NULL
           AND LineType = CASE ComponentKind
                WHEN 'ORDINARY' THEN 'ORDINARY'
                WHEN 'STATUS' THEN 'STATUS_PAY'
                WHEN 'BONUS' THEN 'BONUS'
                WHEN 'MINIMUM_ADJUSTMENT' THEN 'SYS_MIN_TOPUP'
                WHEN 'MAXIMUM_ADJUSTMENT' THEN 'SYS_MAX_CAP' END
           AND ((ComponentKind = 'ORDINARY')
                = (PayrollPeriodDefinitionID IS NOT NULL AND PayDefinitionID IS NOT NULL))
           AND (ComponentKind = 'ORDINARY'
                OR (PayrollPeriodDefinitionID IS NULL AND PayDefinitionID IS NULL)));

CREATE INDEX ix_PayrollFinalLines_Snapshot
    ON payroll.PayrollFinalLines (PayrollCalculationSnapshotID);

-- The one controlled insert path (finalization) also has to project exactly the component
-- it names: same snapshot, DriverAssignment, Driver, Employee, kind, definition, currency
-- and official amount.
CREATE OR REPLACE FUNCTION payroll.fn_guard_final_line_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    v_allowed   TEXT;
    v_status    TEXT;
    v_component RECORD;
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

    SELECT c.payrollcalculationsnapshotid, c.componentkind, c.officialamount,
           c.payrollperioddefinitionid, c.paydefinitionid,
           dt.driverassignmentid, dt.driverid, dt.employeeid,
           s.currencycode, s.currencyminorunitdigits
    INTO v_component
    FROM payroll.payrollcalculationsnapshotcomponents c
    JOIN payroll.payrollcalculationdrivertotals dt
      ON dt.payrollcalculationdrivertotalid = c.payrollcalculationdrivertotalid
    JOIN payroll.payrollcalculationsnapshots s
      ON s.payrollcalculationsnapshotid = c.payrollcalculationsnapshotid
    WHERE c.payrollcalculationsnapshotcomponentid = NEW.payrollcalculationsnapshotcomponentid;

    IF NOT FOUND
       OR v_component.payrollcalculationsnapshotid IS DISTINCT FROM NEW.payrollcalculationsnapshotid
       OR v_component.componentkind IS DISTINCT FROM NEW.componentkind
       OR v_component.officialamount IS DISTINCT FROM NEW.finalamount
       OR v_component.payrollperioddefinitionid IS DISTINCT FROM NEW.payrollperioddefinitionid
       OR v_component.paydefinitionid IS DISTINCT FROM NEW.paydefinitionid
       OR v_component.driverassignmentid IS DISTINCT FROM NEW.driverassignmentid
       OR v_component.driverid IS DISTINCT FROM NEW.driverid
       OR v_component.employeeid IS DISTINCT FROM NEW.employeeid
       OR v_component.currencycode IS DISTINCT FROM NEW.currencycode
       OR v_component.currencyminorunitdigits IS DISTINCT FROM NEW.currencyminorunitdigits THEN
        RAISE EXCEPTION
            'payroll_insert_guard: a final line must project exactly its snapshot component. '
            '(period_id=%, component_id=%)',
            NEW.payrollperiodid, NEW.payrollcalculationsnapshotcomponentid
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$function$;

-- ---------------------------------------------------------------------------
-- 10. Legacy Status DriverRate use guards follow the evidence link
-- ---------------------------------------------------------------------------
--
-- A FinalLine now projects a whole component and no longer names one DriverRate, so a
-- legacy Status DriverRate is "used in finalized payroll" when the used-rate evidence of a
-- snapshot with FinalLines records it.

CREATE OR REPLACE FUNCTION payroll.fn_DriverRateUsedInFinalizedPayroll(p_DriverRateID BIGINT)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM payroll.PayrollCalculationSnapshotUsedRateDefinitions u
        JOIN payroll.PayrollFinalLines fl
          ON fl.PayrollCalculationSnapshotID = u.PayrollCalculationSnapshotID
        WHERE u.DriverRateID = p_DriverRateID
    )
$$;

CREATE OR REPLACE FUNCTION payroll.fn_guard_driverrate_void()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.status = 'Voided' AND OLD.status <> 'Voided' THEN
        IF payroll.fn_DriverRateUsedInFinalizedPayroll(OLD.driverrateid) THEN
            RAISE EXCEPTION
                'driverrate_void_guard: DriverRate has been used in finalized payroll '
                'and cannot be voided. (driverrateid=%)',
                OLD.driverrateid
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION payroll.fn_guard_driverrate_used_mutation()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF payroll.fn_DriverRateUsedInFinalizedPayroll(OLD.driverrateid) THEN
            RAISE EXCEPTION
                'driverrate_mutation_guard: DriverRate has been used in finalized '
                'payroll and cannot be deleted. (driverrateid=%)',
                OLD.driverrateid
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF (
            NEW.amount        IS DISTINCT FROM OLD.amount        OR
            NEW.driverassignmentid      IS DISTINCT FROM OLD.driverassignmentid      OR
            NEW.ratetypeid    IS DISTINCT FROM OLD.ratetypeid    OR
            NEW.companyid     IS DISTINCT FROM OLD.companyid     OR
            NEW.branchid      IS DISTINCT FROM OLD.branchid      OR
            NEW.effectivefrom IS DISTINCT FROM OLD.effectivefrom OR
            NEW.blocksize     IS DISTINCT FROM OLD.blocksize     OR
            NEW.roundingrule  IS DISTINCT FROM OLD.roundingrule
        ) THEN
            IF payroll.fn_DriverRateUsedInFinalizedPayroll(OLD.driverrateid) THEN
                RAISE EXCEPTION
                    'driverrate_mutation_guard: DriverRate has been used in finalized '
                    'payroll. Critical fields (Amount, DriverAssignmentID, RateTypeID, CompanyID, '
                    'BranchID, EffectiveFrom, BlockSize, RoundingRule) cannot be changed. '
                    '(driverrateid=%)',
                    OLD.driverrateid
                    USING ERRCODE = 'restrict_violation';
            END IF;
        END IF;
        RETURN NEW;
    END IF;

    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION payroll.fn_guard_driverratetier_used_mutation()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    -- DELETE: blocked if the parent DriverRate is used by finalized payroll
    IF TG_OP = 'DELETE' THEN
        IF payroll.fn_DriverRateUsedInFinalizedPayroll(OLD.driverrateid) THEN
            RAISE EXCEPTION
                'driverratetier_mutation_guard: DriverRateTier belongs to a DriverRate '
                'used in finalized payroll and cannot be deleted. '
                '(driverrateid=%, tiersequence=%)',
                OLD.driverrateid, OLD.tiersequence
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    -- UPDATE: block any field change if the parent DriverRate is used
    IF TG_OP = 'UPDATE' THEN
        IF payroll.fn_DriverRateUsedInFinalizedPayroll(OLD.driverrateid) THEN
            RAISE EXCEPTION
                'driverratetier_mutation_guard: DriverRateTier belongs to a DriverRate '
                'used in finalized payroll and cannot be modified. '
                '(driverrateid=%, tiersequence=%)',
                OLD.driverrateid, OLD.tiersequence
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    RETURN NEW;
END;
$function$;

-- ---------------------------------------------------------------------------
-- 11. Dated finalized work
-- ---------------------------------------------------------------------------
--
-- A FinalLine projects a whole component and carries no WorkDate. The WorkDates of finalized
-- work are the dates of the snapshot lines of every snapshot that has FinalLines, so the
-- guards that protect dated finalized activity (hire date, termination, assignment
-- coverage) read them from here.

CREATE VIEW payroll.vw_FinalizedWorkDates AS
SELECT DISTINCT
       dt.CompanyID,
       dt.BranchID,
       s.PayrollPeriodID,
       dt.DriverAssignmentID,
       dt.DriverID,
       sl.WorkDate
FROM payroll.PayrollCalculationSnapshotLines sl
JOIN payroll.PayrollCalculationDriverTotals dt
  ON dt.PayrollCalculationDriverTotalID = sl.PayrollCalculationDriverTotalID
JOIN payroll.PayrollCalculationSnapshots s
  ON s.PayrollCalculationSnapshotID = dt.PayrollCalculationSnapshotID
WHERE sl.WorkDate IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM payroll.PayrollFinalLines fl
      WHERE fl.PayrollCalculationSnapshotID = s.PayrollCalculationSnapshotID);
