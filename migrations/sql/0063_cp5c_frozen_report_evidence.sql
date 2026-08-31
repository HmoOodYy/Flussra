-- =============================================================================
-- Migration 0063: CP-5C frozen report-evidence persistence
--
-- New CP-4D snapshots capture immutable Status and Bonus report evidence.
-- Existing snapshots deliberately remain legacy (NULL markers; no backfill).
-- =============================================================================

ALTER TABLE payroll.PayrollCalculationSnapshots
    ADD COLUMN ReportEvidenceVersion INTEGER NULL,
    ADD COLUMN ReportEvidenceHash VARCHAR(64) NULL;

ALTER TABLE payroll.PayrollCalculationSnapshots
    ADD CONSTRAINT ck_PayrollCalculationSnapshots_ReportEvidenceMarker
    CHECK (
        (ReportEvidenceVersion IS NULL AND ReportEvidenceHash IS NULL)
        OR (
            ReportEvidenceVersion > 0
            AND ReportEvidenceHash ~ '^[0-9a-f]{64}$'
        )
    );

ALTER TABLE payroll.PayrollCalculationSnapshots
    ADD CONSTRAINT uq_PayrollCalculationSnapshots_ID_Company_Branch_Period
    UNIQUE (PayrollCalculationSnapshotID, CompanyID, BranchID, PayrollPeriodID);


CREATE TABLE payroll.PayrollCalculationSnapshotStatusEntries (
    PayrollCalculationSnapshotStatusEntryID BIGSERIAL    PRIMARY KEY,
    PayrollCalculationSnapshotID            BIGINT       NOT NULL,
    CompanyID                               INTEGER      NOT NULL,
    BranchID                                INTEGER      NOT NULL,
    PayrollPeriodID                         INTEGER      NOT NULL,
    DriverID                                INTEGER      NOT NULL,
    WorkDate                                DATE         NOT NULL,
    PayrollPeriodDriverDayEntryStateID      BIGINT       NOT NULL,
    StatusKeyID                             INTEGER      NOT NULL,
    StatusCodeSnapshot                      VARCHAR(50)  NOT NULL,
    StatusLabelSnapshot                     VARCHAR(200) NOT NULL,
    StatusIsOffReasonSnapshot               BOOLEAN      NOT NULL,

    CONSTRAINT uq_PayrollCalculationSnapshotStatusEntries_Snapshot_Driver_Date
        UNIQUE (PayrollCalculationSnapshotID, DriverID, WorkDate),
    CONSTRAINT fk_PayrollCalculationSnapshotStatusEntries_Snapshot_Scope
        FOREIGN KEY (PayrollCalculationSnapshotID, CompanyID, BranchID, PayrollPeriodID)
        REFERENCES payroll.PayrollCalculationSnapshots
            (PayrollCalculationSnapshotID, CompanyID, BranchID, PayrollPeriodID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollCalculationSnapshotStatusEntries_Driver_Company_Branch
        FOREIGN KEY (DriverID, CompanyID, BranchID)
        REFERENCES core.Drivers (DriverID, CompanyID, BranchID)
        ON DELETE RESTRICT
);

CREATE INDEX ix_PayrollCalculationSnapshotStatusEntries_Snapshot
    ON payroll.PayrollCalculationSnapshotStatusEntries
        (PayrollCalculationSnapshotID, DriverID, WorkDate);


CREATE TABLE payroll.PayrollCalculationSnapshotBonusEvents (
    PayrollCalculationSnapshotBonusEventID BIGSERIAL      PRIMARY KEY,
    PayrollCalculationSnapshotID           BIGINT         NOT NULL,
    CompanyID                              INTEGER        NOT NULL,
    BranchID                               INTEGER        NOT NULL,
    PayrollPeriodID                        INTEGER        NOT NULL,
    PayrollBonusEventID                    BIGINT         NOT NULL,
    DriverID                               INTEGER        NOT NULL,
    Amount                                 NUMERIC(18,4)  NOT NULL,
    Reason                                 VARCHAR(500),
    Notes                                  TEXT,
    DataRevision                           BIGINT         NOT NULL,
    CreatedByUserID                        INTEGER,
    CreatorDisplayNameSnapshot             VARCHAR(200),
    CreatedAtUtc                           TIMESTAMPTZ    NOT NULL,

    CONSTRAINT ck_PayrollCalculationSnapshotBonusEvents_Amount
        CHECK (Amount > 0),
    CONSTRAINT uq_PayrollCalculationSnapshotBonusEvents_Snapshot_BonusEvent
        UNIQUE (PayrollCalculationSnapshotID, PayrollBonusEventID),
    CONSTRAINT fk_PayrollCalculationSnapshotBonusEvents_Snapshot_Scope
        FOREIGN KEY (PayrollCalculationSnapshotID, CompanyID, BranchID, PayrollPeriodID)
        REFERENCES payroll.PayrollCalculationSnapshots
            (PayrollCalculationSnapshotID, CompanyID, BranchID, PayrollPeriodID)
        ON DELETE RESTRICT,
    CONSTRAINT fk_PayrollCalculationSnapshotBonusEvents_Driver_Company_Branch
        FOREIGN KEY (DriverID, CompanyID, BranchID)
        REFERENCES core.Drivers (DriverID, CompanyID, BranchID)
        ON DELETE RESTRICT
);

CREATE INDEX ix_PayrollCalculationSnapshotBonusEvents_Snapshot_Driver
    ON payroll.PayrollCalculationSnapshotBonusEvents
        (PayrollCalculationSnapshotID, DriverID, PayrollBonusEventID);


CREATE TRIGGER trg_PayrollCalculationSnapshotStatusEntries_Immutable
    BEFORE UPDATE OR DELETE ON payroll.PayrollCalculationSnapshotStatusEntries
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_calculation_snapshot_immutable();

CREATE TRIGGER trg_PayrollCalculationSnapshotBonusEvents_Immutable
    BEFORE UPDATE OR DELETE ON payroll.PayrollCalculationSnapshotBonusEvents
    FOR EACH ROW EXECUTE FUNCTION payroll.fn_guard_calculation_snapshot_immutable();
