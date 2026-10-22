-- 0091: OrdinalTier as the second first-production calculation method.
--
-- Historical migrations remain immutable. The target Compensation schema already owns the
-- OrdinalTier topology (RateComponentDefinitions), the complete-value-set schedule lifecycle
-- and the structure lock; this migration adds only what OrdinalTier needs beyond that:
--
--   1. A governance request stores the proposed tier topology beside its other draft fields,
--      so an OrdinalTier definition is requested, reviewed and approved like any other.
--   2. Calculation contract v4: a used-rate evidence row describes the whole schedule a line
--      used, which for OrdinalTier is a set of ordered tier values rather than one scalar rate.
--      The tier values are frozen in their own immutable child table, and an ordinary line may
--      carry no single resolved rate when its method has none.
--
-- The evidence cutover is clean, not compatible: v3 snapshots hash a narrower used-rate
-- descriptor, so the migration refuses (CALCULATION_EVIDENCE_REBUILD_REQUIRED, database
-- unchanged) while a calculation snapshot or final line exists. Reset and reseed the
-- development database, then apply the migrations again. The check runs before any
-- structural statement and the migration is one transaction.

DO $preflight$
DECLARE
    v_Count BIGINT;
BEGIN
    SELECT (SELECT COUNT(*) FROM payroll.PayrollCalculationSnapshots)
         + (SELECT COUNT(*) FROM payroll.PayrollFinalLines)
    INTO v_Count;

    IF v_Count > 0 THEN
        RAISE EXCEPTION
            'CALCULATION_EVIDENCE_REBUILD_REQUIRED: % calculation snapshot or final line row(s) exist under calculation contract v3. '
            'Their evidence cannot be reinterpreted or backfilled. Reset and reseed this preproduction database, then apply migrations again.',
            v_Count
            USING ERRCODE = 'check_violation';
    END IF;
END
$preflight$;

-- ---------------------------------------------------------------------------
-- 1. Governance requests carry the proposed tier topology
-- ---------------------------------------------------------------------------
--
-- An ordered JSON array of {"ordinal_from", "ordinal_to"} objects, meaningful only for an
-- OrdinalTier request. Its validity is decided by the application when the request is
-- submitted and approved, and by the structure triggers when the definition is created.

ALTER TABLE payroll.PayDefinitionRequests
    ADD COLUMN OrdinalTiers JSONB,
    ADD CONSTRAINT ck_PayDefinitionRequests_OrdinalTiers
        CHECK (OrdinalTiers IS NULL OR jsonb_typeof(OrdinalTiers) = 'array');

-- ---------------------------------------------------------------------------
-- 2. Calculation contract v4
-- ---------------------------------------------------------------------------

ALTER TABLE payroll.PayrollCalculationSnapshots
    DROP CONSTRAINT ck_PayrollCalculationSnapshots_CalculationVersion;
ALTER TABLE payroll.PayrollCalculationSnapshots
    ADD CONSTRAINT ck_PayrollCalculationSnapshots_CalculationVersion
        CHECK (CalculationVersion = 'payroll-calculation-v4');

-- A used-rate row names the shape of the schedule it describes. A Scalar row carries its one
-- component and amount; an OrdinalTierSchedule row carries neither, because its values are the
-- rows of PayrollCalculationSnapshotUsedRateComponents. The shape is the shape of the
-- RateDefinition the row names.
ALTER TABLE payroll.PayrollCalculationSnapshotUsedRateDefinitions
    DROP CONSTRAINT ck_PayrollCalculationSnapshotUsedRateDefinitions_Kind;
ALTER TABLE payroll.PayrollCalculationSnapshotUsedRateDefinitions
    ADD COLUMN RateShapeSnapshot VARCHAR(30) NOT NULL;

ALTER TABLE payroll.PayrollCalculationSnapshotUsedRateDefinitions
    ADD CONSTRAINT fk_PayrollCalculationSnapshotUsedRateDefinitions_RateShape
        FOREIGN KEY (RateDefinitionID, RateShapeSnapshot)
        REFERENCES payroll.RateDefinitions (RateDefinitionID, Shape),
    ADD CONSTRAINT uq_PayrollCalculationSnapshotUsedRateDefinitions_Components
        UNIQUE (PayrollCalculationSnapshotUsedRateDefinitionID, PayrollCalculationSnapshotID,
                RateDefinitionID),
    ADD CONSTRAINT ck_PayrollCalculationSnapshotUsedRateDefinitions_Kind
        CHECK (EvidenceKind = 'DriverRateSchedule'
               AND DriverRateScheduleID IS NOT NULL AND RateDefinitionID IS NOT NULL
               AND EffectiveFromSnapshot IS NOT NULL AND RateStatusSnapshot IS NOT NULL
               AND num_nonnulls(PayDefinitionID, StatusRateColumnID) = 1
               AND ((SourceTypeSnapshot = 'DraftLine' AND PayDefinitionID IS NOT NULL)
                    OR (SourceTypeSnapshot = 'StatusEntryState' AND StatusRateColumnID IS NOT NULL))
               AND ((RateShapeSnapshot = 'Scalar'
                        AND RateComponentDefinitionID IS NOT NULL AND RateAmountSnapshot IS NOT NULL)
                    OR (RateShapeSnapshot = 'OrdinalTierSchedule'
                        AND SourceTypeSnapshot = 'DraftLine'
                        AND RateComponentDefinitionID IS NULL AND RateAmountSnapshot IS NULL)));

-- The tier values one OrdinalTier schedule contributed to a snapshot: the topology (the ordinal
-- range of every tier) and the value each tier had, exactly as the calculation used them.
CREATE TABLE payroll.PayrollCalculationSnapshotUsedRateComponents (
    PayrollCalculationSnapshotUsedRateComponentID  BIGSERIAL     PRIMARY KEY,
    PayrollCalculationSnapshotUsedRateDefinitionID BIGINT        NOT NULL,
    PayrollCalculationSnapshotID                   BIGINT        NOT NULL,
    RateDefinitionID                               INTEGER       NOT NULL,
    SequenceNo                                     INTEGER       NOT NULL,
    RateComponentDefinitionID                      INTEGER       NOT NULL,
    OrdinalFrom                                    INTEGER       NOT NULL,
    OrdinalTo                                      INTEGER,
    AmountSnapshot                                 NUMERIC(18,4) NOT NULL,
    CreatedAtUtc                                   TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_PayrollCalculationSnapshotUsedRateComponents_UsedRate
        FOREIGN KEY (PayrollCalculationSnapshotUsedRateDefinitionID,
                     PayrollCalculationSnapshotID, RateDefinitionID)
        REFERENCES payroll.PayrollCalculationSnapshotUsedRateDefinitions
            (PayrollCalculationSnapshotUsedRateDefinitionID, PayrollCalculationSnapshotID,
             RateDefinitionID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollCalculationSnapshotUsedRateComponents_Component
        FOREIGN KEY (RateComponentDefinitionID, RateDefinitionID)
        REFERENCES payroll.RateComponentDefinitions (RateComponentDefinitionID, RateDefinitionID)
        ON DELETE RESTRICT,
    CONSTRAINT uq_PayrollCalculationSnapshotUsedRateComponents_Sequence
        UNIQUE (PayrollCalculationSnapshotUsedRateDefinitionID, SequenceNo),
    CONSTRAINT uq_PayrollCalculationSnapshotUsedRateComponents_Component
        UNIQUE (PayrollCalculationSnapshotUsedRateDefinitionID, RateComponentDefinitionID),
    CONSTRAINT ck_PayrollCalculationSnapshotUsedRateComponents_Sequence
        CHECK (SequenceNo >= 1),
    CONSTRAINT ck_PayrollCalculationSnapshotUsedRateComponents_Ordinals
        CHECK (OrdinalFrom >= 1 AND (OrdinalTo IS NULL OR OrdinalTo >= OrdinalFrom)),
    CONSTRAINT ck_PayrollCalculationSnapshotUsedRateComponents_Amount
        CHECK (AmountSnapshot >= 0)
);

CREATE INDEX ix_PayrollCalculationSnapshotUsedRateComponents_Snapshot
    ON payroll.PayrollCalculationSnapshotUsedRateComponents (PayrollCalculationSnapshotID);

-- Tier values belong only to a used-rate row of the OrdinalTierSchedule shape, and every such row
-- has them: the first is checked as a value is written, the second when the transaction commits.
CREATE FUNCTION payroll.fn_guard_used_rate_component_shape()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    v_Shape VARCHAR(30);
BEGIN
    SELECT RateShapeSnapshot INTO v_Shape
    FROM payroll.PayrollCalculationSnapshotUsedRateDefinitions
    WHERE PayrollCalculationSnapshotUsedRateDefinitionID = NEW.PayrollCalculationSnapshotUsedRateDefinitionID;

    IF v_Shape IS DISTINCT FROM 'OrdinalTierSchedule' THEN
        RAISE EXCEPTION
            'used_rate_component_shape: tier values belong only to an OrdinalTierSchedule used-rate row'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$function$;

CREATE TRIGGER trg_PayrollCalculationSnapshotUsedRateComponents_Shape
    BEFORE INSERT ON payroll.PayrollCalculationSnapshotUsedRateComponents
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_used_rate_component_shape();

CREATE FUNCTION payroll.fn_assert_used_rate_tiers_present()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.RateShapeSnapshot = 'OrdinalTierSchedule' AND NOT EXISTS (
        SELECT 1 FROM payroll.PayrollCalculationSnapshotUsedRateComponents c
        WHERE c.PayrollCalculationSnapshotUsedRateDefinitionID = NEW.PayrollCalculationSnapshotUsedRateDefinitionID
    ) THEN
        RAISE EXCEPTION
            'used_rate_tiers_missing: an OrdinalTierSchedule used-rate row has no tier values'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$function$;

CREATE CONSTRAINT TRIGGER trg_PayrollCalculationSnapshotUsedRateDefinitions_Tiers
    AFTER INSERT ON payroll.PayrollCalculationSnapshotUsedRateDefinitions
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_assert_used_rate_tiers_present();

CREATE TRIGGER trg_PayrollCalculationSnapshotUsedRateComponents_Immutable
    BEFORE UPDATE OR DELETE ON payroll.PayrollCalculationSnapshotUsedRateComponents
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_calculation_snapshot_immutable();

-- An ordinary line of a method with a single rate still records it (the used-rate row it links
-- to must carry the same amount, which the line to used-rate guard checks); a line of a method
-- with several tier values records none, so the line itself names no single rate.
ALTER TABLE payroll.PayrollCalculationSnapshotLines
    DROP CONSTRAINT ck_PayrollCalculationSnapshotLines_Source;
ALTER TABLE payroll.PayrollCalculationSnapshotLines
    ADD CONSTRAINT ck_PayrollCalculationSnapshotLines_Source
        CHECK (
            (SourceType = 'DraftLine' AND LineType = 'ORDINARY' AND LineScope = 'Daily'
                AND SourceID IS NOT NULL AND WorkDate IS NOT NULL AND Quantity IS NOT NULL
                AND PayrollPeriodDefinitionID IS NOT NULL AND PayDefinitionID IS NOT NULL
                AND RateDefinitionID IS NOT NULL AND DriverRateScheduleID IS NOT NULL
                AND CalculationStatus = 'Calculated' AND CalculationVersion IS NOT NULL
                AND UsedRateDefinitionID IS NOT NULL
                AND StatusRateColumnID IS NULL AND BonusEventID IS NULL)
         OR (SourceType = 'StatusEntryState' AND LineType = 'STATUS' AND LineScope = 'Daily'
                AND SourceID IS NOT NULL AND WorkDate IS NOT NULL AND Quantity IS NOT NULL
                AND StatusRateColumnID IS NOT NULL AND RateDefinitionID IS NOT NULL
                AND DriverRateScheduleID IS NOT NULL
                AND CalculationStatus = 'Calculated' AND CalculationVersion IS NOT NULL
                AND ResolvedRateAmount IS NOT NULL AND UsedRateDefinitionID IS NOT NULL
                AND PayrollPeriodDefinitionID IS NULL AND PayDefinitionID IS NULL
                AND BonusEventID IS NULL)
         OR (SourceType = 'BonusEvent' AND LineType = 'BONUS' AND LineScope = 'Period'
                AND SourceID IS NOT NULL AND BonusEventID IS NOT NULL AND WorkDate IS NULL
                AND Quantity IS NULL AND ResolvedRateAmount IS NULL
                AND UsedRateDefinitionID IS NULL
                AND PayrollPeriodDefinitionID IS NULL AND PayDefinitionID IS NULL
                AND RateDefinitionID IS NULL AND DriverRateScheduleID IS NULL
                AND CalculationStatus IS NULL AND CalculationVersion IS NULL
                AND StatusRateColumnID IS NULL)
        );
