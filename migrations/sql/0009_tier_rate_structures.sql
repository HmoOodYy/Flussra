-- =============================================================================
-- Migration 0009: M13c - Advanced Rate Structures
--
-- Adds:
--   1. payroll.DriverRateTiers  - child rows holding tier breakpoints for
--      OrdinalTier, RangeBracket, and RangeProgressive rate behaviors.
--
--   2. BlockSize / RoundingRule columns on payroll.DriverRates - metadata for
--      Block behavior (amount per complete block + how to round partial blocks).
--
--   3. CHECK constraint on payroll.PayItems.RateBehavior - enumerates all
--      valid values including the four M13c additions.
--
-- Notes:
--   ON DELETE CASCADE on DriverRateTiers.DriverRateID handles physical row
--   deletion only.  Void/Supersede are status UPDATEs and never delete tier rows,
--   so historical tier data is preserved for all non-physically-deleted rates.
-- =============================================================================

-- 1. DriverRateTiers child table
CREATE TABLE payroll.DriverRateTiers (
    DriverRateTierID  SERIAL         PRIMARY KEY,
    DriverRateID      INTEGER        NOT NULL,
    TierSequence      INTEGER        NOT NULL,
    FromUnit          NUMERIC(18,4)  NOT NULL,
    ToUnit            NUMERIC(18,4),
    TierAmount        NUMERIC(18,4)  NOT NULL,

    CONSTRAINT uq_DriverRateTiers_Rate_Seq  UNIQUE (DriverRateID, TierSequence),
    CONSTRAINT ck_DriverRateTiers_Sequence  CHECK (TierSequence > 0),
    CONSTRAINT ck_DriverRateTiers_Amount    CHECK (TierAmount > 0),
    CONSTRAINT ck_DriverRateTiers_FromUnit  CHECK (FromUnit >= 0),
    -- >= allows single-position ordinal tiers (e.g. FromUnit=1, ToUnit=1).
    -- Behavior-specific contiguity rules are enforced in the service layer.
    CONSTRAINT ck_DriverRateTiers_Bounds    CHECK (ToUnit IS NULL OR ToUnit >= FromUnit),
    CONSTRAINT fk_DriverRateTiers_Rate      FOREIGN KEY (DriverRateID)
        REFERENCES payroll.DriverRates(DriverRateID) ON DELETE CASCADE
);

CREATE INDEX ix_DriverRateTiers_Rate_Seq
    ON payroll.DriverRateTiers (DriverRateID, TierSequence);

-- 2. Block rate metadata columns on DriverRates
ALTER TABLE payroll.DriverRates
    ADD COLUMN BlockSize    NUMERIC(18,4),
    ADD COLUMN RoundingRule VARCHAR(20);

ALTER TABLE payroll.DriverRates
    ADD CONSTRAINT ck_DriverRates_BlockSize        CHECK (BlockSize IS NULL OR BlockSize > 0),
    ADD CONSTRAINT ck_DriverRates_RoundingRule     CHECK (
        RoundingRule IS NULL
        OR RoundingRule IN ('Floor', 'Ceiling', 'NearestHalfUp')
    ),
    -- Both columns must be NULL together or both non-NULL together.
    ADD CONSTRAINT ck_DriverRates_BlockConsistency CHECK (
        (BlockSize IS NULL) = (RoundingRule IS NULL)
    );

-- 3. Expand RateBehavior CHECK on PayItems to include M13c behaviors.
--    Migration 0005 created ck_PayItems_RateBehavior with the M12 value set.
--    Drop that constraint and recreate with the expanded M13c set.
ALTER TABLE payroll.PayItems
    DROP CONSTRAINT ck_PayItems_RateBehavior;

ALTER TABLE payroll.PayItems
    ADD CONSTRAINT ck_PayItems_RateBehavior
    CHECK (RateBehavior IN (
        'PerUnit',
        'EnteredAmount',
        'Fixed',
        'Calculated',
        'None',
        'OrdinalTier',
        'RangeBracket',
        'RangeProgressive',
        'Block'
    ));
