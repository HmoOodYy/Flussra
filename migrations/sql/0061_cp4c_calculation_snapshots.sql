-- =============================================================================
-- Migration 0061: CP-4C immutable calculation snapshot foundation
--
-- These tables intentionally have no writer in CP-4C. CP-4D will capture a
-- submitted calculation packet; CP-4E and CP-4F will consume that packet.
-- =============================================================================

CREATE TABLE payroll.PayrollCalculationSnapshots (
    PayrollCalculationSnapshotID BIGSERIAL PRIMARY KEY,
    CompanyID                    INTEGER       NOT NULL,
    BranchID                     INTEGER       NOT NULL,
    PayrollPeriodID              INTEGER       NOT NULL,
    RevisionNumber               INTEGER       NOT NULL,
    CalculationVersion           VARCHAR(50)   NOT NULL,
    SourceConfigHash             VARCHAR(64)   NOT NULL,
    SnapshotHash                 VARCHAR(64)   NOT NULL,
    CreatedByUserID              INTEGER       NOT NULL,
    CreatedAtUtc                 TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    TotalExpectedPay             NUMERIC(18,4) NOT NULL,

    CONSTRAINT ck_PayrollCalculationSnapshots_RevisionPositive
        CHECK (RevisionNumber > 0),
    CONSTRAINT ck_PayrollCalculationSnapshots_SourceConfigHashHex
        CHECK (SourceConfigHash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_PayrollCalculationSnapshots_SnapshotHashHex
        CHECK (SnapshotHash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT uq_PayrollCalculationSnapshots_Period_Revision
        UNIQUE (PayrollPeriodID, RevisionNumber),
    CONSTRAINT uq_PayrollCalculationSnapshots_ID_Company_Branch
        UNIQUE (PayrollCalculationSnapshotID, CompanyID, BranchID),
    CONSTRAINT fk_PayrollCalculationSnapshots_Period_Company_Branch
        FOREIGN KEY (PayrollPeriodID, CompanyID, BranchID)
        REFERENCES payroll.PayrollPeriods (PayrollPeriodID, CompanyID, BranchID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollCalculationSnapshots_CreatedBy
        FOREIGN KEY (CreatedByUserID)
        REFERENCES sec.Users (UserID)
        ON DELETE RESTRICT
);

CREATE INDEX ix_PayrollCalculationSnapshots_Company_Branch_Period_Revision
    ON payroll.PayrollCalculationSnapshots
        (CompanyID, BranchID, PayrollPeriodID, RevisionNumber DESC);


CREATE TABLE payroll.PayrollCalculationDriverTotals (
    PayrollCalculationDriverTotalID BIGSERIAL PRIMARY KEY,
    PayrollCalculationSnapshotID    BIGINT         NOT NULL,
    CompanyID                        INTEGER        NOT NULL,
    BranchID                         INTEGER        NOT NULL,
    DriverID                         INTEGER        NOT NULL,
    DriverCodeSnapshot               VARCHAR(100),
    DriverNameSnapshot               VARCHAR(255),
    DailyPay                         NUMERIC(18,4)  NOT NULL,
    StatusPay                        NUMERIC(18,4)  NOT NULL,
    PeriodPay                        NUMERIC(18,4)  NOT NULL,
    MinimumAdjustment                NUMERIC(18,4)  NOT NULL,
    MaximumAdjustment                NUMERIC(18,4)  NOT NULL,
    BonusTotal                       NUMERIC(18,4)  NOT NULL,
    ExpectedPay                      NUMERIC(18,4)  NOT NULL,

    CONSTRAINT uq_PayrollCalculationDriverTotals_Snapshot_Driver
        UNIQUE (PayrollCalculationSnapshotID, DriverID),
    CONSTRAINT fk_PayrollCalculationDriverTotals_Snapshot_Company_Branch
        FOREIGN KEY (PayrollCalculationSnapshotID, CompanyID, BranchID)
        REFERENCES payroll.PayrollCalculationSnapshots
            (PayrollCalculationSnapshotID, CompanyID, BranchID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollCalculationDriverTotals_Driver_Company_Branch
        FOREIGN KEY (DriverID, CompanyID, BranchID)
        REFERENCES core.Drivers (DriverID, CompanyID, BranchID)
        ON DELETE RESTRICT
);


CREATE TABLE payroll.PayrollCalculationSnapshotLines (
    PayrollCalculationSnapshotLineID BIGSERIAL PRIMARY KEY,
    PayrollCalculationDriverTotalID  BIGINT        NOT NULL,
    SourceType                       VARCHAR(30)   NOT NULL,
    SourceID                         VARCHAR(100),
    LineType                         VARCHAR(50)   NOT NULL,
    LineScope                        VARCHAR(10),
    WorkDate                         DATE,
    PayItemID                        INTEGER,
    RateTypeID                       INTEGER,
    DriverRateID                     BIGINT,
    BonusEventID                     BIGINT,
    Quantity                         NUMERIC(18,4),
    ResolvedRateAmount               NUMERIC(18,4),
    CalculatedAmount                 NUMERIC(18,4) NOT NULL,
    SourceEvidenceJSONB              JSONB         NOT NULL,

    CONSTRAINT fk_PayrollCalculationSnapshotLines_DriverTotal
        FOREIGN KEY (PayrollCalculationDriverTotalID)
        REFERENCES payroll.PayrollCalculationDriverTotals
            (PayrollCalculationDriverTotalID)
        ON DELETE RESTRICT
);

CREATE INDEX ix_PayrollCalculationSnapshotLines_DriverTotal
    ON payroll.PayrollCalculationSnapshotLines (PayrollCalculationDriverTotalID);

CREATE INDEX ix_PayrollCalculationSnapshotLines_SourceIdentity
    ON payroll.PayrollCalculationSnapshotLines (SourceType, SourceID)
    WHERE SourceID IS NOT NULL;


CREATE OR REPLACE FUNCTION payroll.fn_guard_calculation_snapshot_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'payroll_calculation_snapshot_immutable: % on %.% is forbidden once written.',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER trg_PayrollCalculationSnapshots_Immutable
    BEFORE UPDATE OR DELETE ON payroll.PayrollCalculationSnapshots
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_calculation_snapshot_immutable();

CREATE TRIGGER trg_PayrollCalculationDriverTotals_Immutable
    BEFORE UPDATE OR DELETE ON payroll.PayrollCalculationDriverTotals
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_calculation_snapshot_immutable();

CREATE TRIGGER trg_PayrollCalculationSnapshotLines_Immutable
    BEFORE UPDATE OR DELETE ON payroll.PayrollCalculationSnapshotLines
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_calculation_snapshot_immutable();
