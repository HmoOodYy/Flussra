-- 0084: PayDefinition branch applicability authority (P4a).
--
-- payroll.BranchPayItemConfig keeps its operational meaning (Company, Branch,
-- IsActive, effective-dated versions, Notes, creator) but its identity becomes
-- the Company-owned PayDefinition instead of the legacy PayItem.
--
-- Pre-production reset: every existing row is legacy PayItem configuration and
-- is disposable. Nothing is converted, matched by code or name, or backfilled.
-- A PayDefinition is not applicable to a Branch until it is configured there;
-- there is no implicit default activation.
--
-- PayItemID remains only as a deprecated, always-NULL column so unreachable
-- legacy readers of the period snapshot path still compile until the legacy
-- layer is removed in the cleanup phase. It is not a configuration authority:
-- the CHECK below makes it impossible to populate, and no key, index or
-- foreign key uses it.
--
-- Legacy PayItems, RateTypes, DriverRates and the period evidence that
-- references them are NOT touched here. Payroll periods carry immutable
-- evidence and Payroll Setup audit coupling, so they are not wiped by this
-- migration. Terminal periods (Locked, Archived, Cancelled) remain as history.
-- Non-terminal periods block the migration (see the preflight below).

-- Preflight: refuse to cut over while a pre-cutover period can still move through the
-- legacy payroll runtime. Nothing is converted, finalized or deleted here. The check
-- runs before any destructive statement and the whole migration is one transaction,
-- so a failure leaves the database at 0083 with its legacy configuration intact.
-- Locked, Archived and Cancelled periods are terminal history and are not touched.
DO $preflight$
DECLARE
    mutable_periods INTEGER;
BEGIN
    SELECT count(*) INTO mutable_periods
    FROM   payroll.PayrollPeriods
    WHERE  Status IN ('Draft', 'Open', 'InReview', 'Returned', 'Approved');

    IF mutable_periods > 0 THEN
        RAISE EXCEPTION
            'P4A_MUTABLE_LEGACY_PERIODS_REQUIRE_RESET: % non-terminal payroll period(s) '
            'still use the legacy PayItem runtime. Migration 0084 does not convert them. '
            'Reset and reseed the development database, then run the migration again.',
            mutable_periods;
    END IF;
END
$preflight$;

DELETE FROM payroll.BranchPayItemConfig;

ALTER TABLE payroll.BranchPayItemConfig
    DROP CONSTRAINT fk_BranchPayItemConfig_PayItem;

DROP INDEX payroll.ix_BranchPayItemConfig_Branch_Item_Dates;
DROP INDEX payroll.uix_BranchPayItemConfig_OpenVersion;

ALTER TABLE payroll.BranchPayItemConfig
    ALTER COLUMN PayItemID DROP NOT NULL,
    DROP COLUMN BranchDisplayName,
    ADD COLUMN PayDefinitionID INTEGER NOT NULL,
    ADD CONSTRAINT ck_BranchPayItemConfig_LegacyPayItemRetired
        CHECK (PayItemID IS NULL),
    ADD CONSTRAINT ck_BranchPayItemConfig_EffectiveDates
        CHECK (EffectiveTo IS NULL OR EffectiveTo >= EffectiveFrom),
    ADD CONSTRAINT fk_BranchPayItemConfig_PayDefinition_Company
        FOREIGN KEY (PayDefinitionID, CompanyID)
        REFERENCES payroll.PayDefinitions (PayDefinitionID, CompanyID),
    ADD CONSTRAINT fk_BranchPayItemConfig_Branch_Company
        FOREIGN KEY (BranchID, CompanyID)
        REFERENCES core.Branches (BranchID, CompanyID);

COMMENT ON COLUMN payroll.BranchPayItemConfig.PayItemID IS
    'Deprecated legacy identity. Always NULL, not a configuration authority.';

CREATE INDEX ix_BranchPayItemConfig_Branch_Definition_Dates
    ON payroll.BranchPayItemConfig (CompanyID, BranchID, PayDefinitionID, EffectiveFrom)
    INCLUDE (IsActive, EffectiveTo, Notes);

-- One open version per logical configuration identity.
CREATE UNIQUE INDEX uix_BranchPayItemConfig_OpenDefinitionVersion
    ON payroll.BranchPayItemConfig (CompanyID, BranchID, PayDefinitionID)
    WHERE EffectiveTo IS NULL;

-- Versions of one configuration identity never overlap.
ALTER TABLE payroll.BranchPayItemConfig
    ADD CONSTRAINT excl_BranchPayItemConfig_DefinitionVersionOverlap
    EXCLUDE USING gist (
        CompanyID WITH =,
        BranchID WITH =,
        PayDefinitionID WITH =,
        daterange(EffectiveFrom, EffectiveTo, '[]') WITH &&
    );
