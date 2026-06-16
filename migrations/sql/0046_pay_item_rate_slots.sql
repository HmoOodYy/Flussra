-- =============================================================================
-- 0046: Generic PayItemRateSlots foundation
--
-- Adds payroll.PayItemRateSlots — a generic slot-definition layer that sits
-- between PayItems and the existing RateTypes + PayItemRateTypeMap layer.
--
-- Purpose:
--   PayItemRateTypeMap records the structural link (PayItem -> RateType).
--   PayItemRateSlots adds the business meaning of each mapped RateType:
--     stable SlotKey, business SlotRole, SortOrder, requiredness, and source
--     metadata.  This table is the foundation for configuring PerUnit rate
--     assignment for CDPI items and, eventually, all system pay items.
--
-- Design notes:
--   - PayItemRateTypeMap is preserved exactly as-is.  It remains the
--     compatibility link used by the Pay Rates matrix and DriverRates.
--   - PayItemRateSlots does NOT replace PayItemRateTypeMap in this migration.
--   - Filtered unique indexes on (PayItemID, SlotKey) and (PayItemID, RateTypeID)
--     prevent duplicate active slots while allowing archived/Inactive rows to
--     share previously-used keys.
--   - The backfill is idempotent: re-running this file will not insert duplicate
--     rows because of the NOT EXISTS guard.
--
-- Changes to existing tables: NONE.
-- Changes to existing data: NONE (existing rows are never updated or deleted).
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. payroll.PayItemRateSlots
-- ---------------------------------------------------------------------------

CREATE TABLE payroll.PayItemRateSlots (
    PayItemRateSlotID   SERIAL          NOT NULL,
    PayItemID           INTEGER         NOT NULL,
    RateTypeID          INTEGER         NOT NULL,

    -- Business meaning of this slot
    SlotKey             VARCHAR(100)    NOT NULL,   -- stable identifier; never changes
    SlotRole            VARCHAR(50)     NOT NULL,   -- e.g. legacy_primary, legacy_rate, perunit

    -- Ordering and requiredness
    SortOrder           INTEGER         NOT NULL DEFAULT 1,
    IsRequired          BOOLEAN         NOT NULL DEFAULT TRUE,

    -- Source / generation metadata
    IsSystemGenerated   BOOLEAN         NOT NULL DEFAULT FALSE,
    SourceKind          VARCHAR(50)     NOT NULL DEFAULT 'Seeded',
    -- SourceKind values: 'Seeded', 'LegacyBackfill', 'CDPI', 'Manual'

    -- Lifecycle
    Status              VARCHAR(30)     NOT NULL DEFAULT 'Active',

    -- Audit
    CreatedByUserID     INTEGER,
    CreatedAtUtc        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UpdatedByUserID     INTEGER,
    UpdatedAtUtc        TIMESTAMPTZ,

    -- Constraints

    CONSTRAINT pk_PayItemRateSlots
        PRIMARY KEY (PayItemRateSlotID),

    CONSTRAINT fk_PayItemRateSlots_PayItem
        FOREIGN KEY (PayItemID)  REFERENCES payroll.PayItems(PayItemID),

    CONSTRAINT fk_PayItemRateSlots_RateType
        FOREIGN KEY (RateTypeID) REFERENCES payroll.RateTypes(RateTypeID),

    CONSTRAINT ck_PayItemRateSlots_SlotKey
        CHECK (LENGTH(TRIM(SlotKey)) > 0),

    CONSTRAINT ck_PayItemRateSlots_SlotRole
        CHECK (LENGTH(TRIM(SlotRole)) > 0),

    CONSTRAINT ck_PayItemRateSlots_SortOrder
        CHECK (SortOrder > 0),

    CONSTRAINT ck_PayItemRateSlots_Status
        CHECK (Status IN ('Active', 'Inactive'))
);


-- ---------------------------------------------------------------------------
-- 2. Indexes
-- ---------------------------------------------------------------------------

-- Basic FK indexes for join performance.
CREATE INDEX ix_PayItemRateSlots_PayItemID
    ON payroll.PayItemRateSlots (PayItemID);

CREATE INDEX ix_PayItemRateSlots_RateTypeID
    ON payroll.PayItemRateSlots (RateTypeID);

-- Unique active (PayItemID, SlotKey): a slot key must be unique per pay item
-- among Active rows.  Inactive/archived rows are excluded so a key may be
-- reused after retirement without a DDL change.
CREATE UNIQUE INDEX uix_PayItemRateSlots_PayItem_SlotKey
    ON payroll.PayItemRateSlots (PayItemID, SlotKey)
    WHERE Status = 'Active';

-- Unique active (PayItemID, RateTypeID): a rate type may map to at most one
-- active slot per pay item.  Inactive rows are excluded.
CREATE UNIQUE INDEX uix_PayItemRateSlots_PayItem_RateTypeID
    ON payroll.PayItemRateSlots (PayItemID, RateTypeID)
    WHERE Status = 'Active';

-- Composite index for ordered slot listing (e.g. building rate-entry forms).
CREATE INDEX ix_PayItemRateSlots_PayItem_SortOrder
    ON payroll.PayItemRateSlots (PayItemID, SortOrder)
    WHERE Status = 'Active';


-- ---------------------------------------------------------------------------
-- 3. Backfill existing active PayItemRateTypeMap rows
--
-- Each active PayItemRateTypeMap row gets one PayItemRateSlots row with
-- conservative defaults that make no business claims about what the rate does.
--
-- Slot key derivation:
--   'legacy_rate_<RateTypeID>' — deterministic, stable, independent of any
--   mutable display name.  RateTypeID is immutable (SERIAL PK, never reused).
--
-- Slot role:
--   'legacy_primary' — for IsPrimary = TRUE rows (the main/only rate type).
--   'legacy_rate'    — for IsPrimary = FALSE rows (supplementary types).
--
-- SortOrder:
--   Derived by PARTITION BY PayItemID ORDER BY IsPrimary DESC, RateTypeID ASC.
--   This places the primary rate first and is deterministic across reruns.
--
-- IsRequired / IsSystemGenerated / SourceKind:
--   All TRUE / TRUE / 'LegacyBackfill' — conservative; represents the fact that
--   these slots existed before this layer was introduced.
--
-- Idempotency guard:
--   NOT EXISTS (active slot for same PayItemID + RateTypeID) prevents duplicates
--   on rerun.  The filtered unique index on (PayItemID, RateTypeID) WHERE Active
--   also enforces this at the constraint level.
-- ---------------------------------------------------------------------------

INSERT INTO payroll.PayItemRateSlots (
    PayItemID,
    RateTypeID,
    SlotKey,
    SlotRole,
    SortOrder,
    IsRequired,
    IsSystemGenerated,
    SourceKind,
    Status,
    CreatedAtUtc
)
SELECT
    m.PayItemID,
    m.RateTypeID,
    'legacy_rate_' || m.RateTypeID::TEXT                                        AS SlotKey,
    CASE WHEN m.IsPrimary THEN 'legacy_primary' ELSE 'legacy_rate' END          AS SlotRole,
    ROW_NUMBER() OVER (
        PARTITION BY m.PayItemID
        ORDER BY     m.IsPrimary DESC, m.RateTypeID ASC
    )                                                                            AS SortOrder,
    TRUE                                                                         AS IsRequired,
    TRUE                                                                         AS IsSystemGenerated,
    'LegacyBackfill'                                                             AS SourceKind,
    'Active'                                                                     AS Status,
    NOW()                                                                        AS CreatedAtUtc
FROM payroll.PayItemRateTypeMap m
WHERE m.Status = 'Active'
  AND NOT EXISTS (
      SELECT 1
      FROM   payroll.PayItemRateSlots s
      WHERE  s.PayItemID  = m.PayItemID
        AND  s.RateTypeID = m.RateTypeID
        AND  s.Status     = 'Active'
  );
