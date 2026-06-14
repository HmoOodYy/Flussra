-- 0012: DriverPayRules table + SYS_MIN_TOPUP / SYS_MAX_CAP PayItems

CREATE TABLE payroll.DriverPayRules (
    DriverPayRuleID  SERIAL        PRIMARY KEY,
    CompanyID        INTEGER       NOT NULL,
    BranchID         INTEGER       NOT NULL,
    DriverID         INTEGER       NOT NULL,
    RuleType         VARCHAR(30)   NOT NULL,
    Amount           NUMERIC(18,4) NOT NULL,
    EffectiveFrom    DATE          NOT NULL,
    EffectiveTo      DATE,
    Status           VARCHAR(30)   NOT NULL DEFAULT 'Active',
    CreatedByUserID  INTEGER,
    CreatedAtUtc     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UpdatedByUserID  INTEGER,
    UpdatedAtUtc     TIMESTAMPTZ,
    Notes            TEXT,

    CONSTRAINT ck_DriverPayRules_RuleType
        CHECK (RuleType IN ('MinimumPay', 'MaximumPay')),
    CONSTRAINT ck_DriverPayRules_Amount
        CHECK (Amount > 0),
    CONSTRAINT ck_DriverPayRules_Status
        CHECK (Status IN ('Active', 'Ended', 'Voided')),
    CONSTRAINT ck_DriverPayRules_Dates
        CHECK (EffectiveTo IS NULL OR EffectiveTo >= EffectiveFrom),
    CONSTRAINT ck_DriverPayRules_EndedRequiresEffectiveTo
        CHECK (Status != 'Ended' OR EffectiveTo IS NOT NULL),

    CONSTRAINT fk_DriverPayRules_Company  FOREIGN KEY (CompanyID)       REFERENCES core.Companies(CompanyID),
    CONSTRAINT fk_DriverPayRules_Branch   FOREIGN KEY (BranchID)        REFERENCES core.Branches(BranchID),
    CONSTRAINT fk_DriverPayRules_Driver   FOREIGN KEY (DriverID)        REFERENCES core.Drivers(DriverID),
    CONSTRAINT fk_DriverPayRules_Creator  FOREIGN KEY (CreatedByUserID) REFERENCES sec.Users(UserID),
    CONSTRAINT fk_DriverPayRules_Updater  FOREIGN KEY (UpdatedByUserID) REFERENCES sec.Users(UserID)
);

ALTER TABLE payroll.DriverPayRules
    ADD CONSTRAINT excl_DriverPayRules_no_date_overlap
    EXCLUDE USING gist (
        CompanyID WITH =,
        DriverID  WITH =,
        RuleType  WITH =,
        daterange(EffectiveFrom, EffectiveTo, '[]') WITH &&
    ) WHERE (Status IN ('Active', 'Ended'));

CREATE UNIQUE INDEX ux_DriverPayRules_Driver_Type_OpenEnded
    ON payroll.DriverPayRules (DriverID, RuleType)
    WHERE Status = 'Active' AND EffectiveTo IS NULL;

CREATE INDEX ix_DriverPayRules_Driver_Type_Dates
    ON payroll.DriverPayRules (DriverID, RuleType, EffectiveFrom, EffectiveTo, Status);

CREATE INDEX ix_DriverPayRules_Company_Branch_Status
    ON payroll.DriverPayRules (CompanyID, BranchID, Status);

-- Seed system finalization-only PayItems
INSERT INTO payroll.PayItems
    (CompanyID, BranchID, PayItemCode, PayItemName, Category, DataType,
     ItemScope, RateBehavior, IsSystemStandard,
     AppearsInPayrollEntry, AppearsInLedger, AppearsInReports, Status)
VALUES
    (NULL, NULL, 'SYS_MIN_TOPUP', 'Minimum Pay Top-Up', 'SystemAdjustment', 'Currency',
     'Period', 'EnteredAmount', TRUE, FALSE, TRUE, TRUE, 'Active'),
    (NULL, NULL, 'SYS_MAX_CAP',   'Maximum Pay Cap',    'SystemAdjustment', 'Currency',
     'Period', 'EnteredAmount', TRUE, FALSE, TRUE, TRUE, 'Active')
